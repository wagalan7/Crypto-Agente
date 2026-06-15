"""
Backfill da coluna `score` dos snapshots históricos com a fórmula V2.

CONTEXTO
  O diagnóstico (score_analysis_service) mostrou que a fórmula legada de score
  tem AUC≈0.52 (≈moeda). A V2 (recommendation_service._compute_score_v2) usa só
  confluence + ADX + derivatives renormalizados e atinge AUC≈0.586 com decis
  monotônicos. Pra que a CALIBRAÇÃO histórica (score→P(TP1)) e qualquer análise
  retroativa enxerguem o score novo, é preciso re-pontuar a coluna `score` dos
  snapshots já gravados — a partir das FEATURES persistidas (confluence_pct, adx,
  funding_pct), usando exatamente o mesmo `_compute_score_v2` do runtime.

  Quando V2 não é computável pra um snapshot (sem confluence E sem adx E sem
  funding nas features), o score legado é MANTIDO (não zera, não inventa).

SEGURANÇA
  • DRY-RUN por padrão: só mede e mostra o que mudaria (amostra + estatísticas).
    Nada é escrito sem `--apply`.
  • Idempotente: re-rodar com `--apply` recalcula a partir das features (não
    acumula). Guarda o score legado em features['score_legacy'] na 1ª aplicação
    pra permitir auditoria/rollback.
  • Rodar SÓ depois que o teste 0.50 fechar e em conjunto com a ativação da flag
    SCORE_FORMULA_V2. Antes disso, manter dry-run.

USO
  cd backend
  python -m scripts.backfill_score_v2                 # dry-run (default)
  python -m scripts.backfill_score_v2 --days 30        # só últimos 30 dias
  python -m scripts.backfill_score_v2 --apply          # ESCREVE no banco
  python -m scripts.backfill_score_v2 --apply --status-resolved   # só resolvidos
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_THIS = Path(__file__).resolve()
_BACKEND = _THIS.parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from sqlalchemy import select, and_  # noqa: E402

from db import DB_ENABLED, get_session  # noqa: E402
from models.recommendation_snapshot import RecommendationSnapshot  # noqa: E402
from services.recommendation_service import _compute_score_v2  # noqa: E402

RESOLVED_STATUSES = ("won_tp1", "won_tp1_be", "won_tp2", "lost")


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _v2_from_features(snap) -> "float | None":
    """Re-pontua um snapshot via _compute_score_v2 a partir das features
    persistidas. Mesma matemática do runtime e do reweight-sim."""
    feats = snap.features or {}
    return _compute_score_v2(
        conf_pct=_f(feats.get("confluence_pct")),
        adx_raw=_f(feats.get("adx")),
        funding_pct=_f(feats.get("funding_pct")),
    )


def _hist(deltas, edges=(-30, -20, -10, -5, 0, 5, 10, 20, 30)):
    """Histograma simples de deltas (novo - antigo) pra leitura rápida."""
    buckets = {}
    for d in deltas:
        placed = False
        for e in edges:
            if d <= e:
                buckets[f"≤{e}"] = buckets.get(f"≤{e}", 0) + 1
                placed = True
                break
        if not placed:
            buckets[f">{edges[-1]}"] = buckets.get(f">{edges[-1]}", 0) + 1
    return buckets


async def run(days: int, apply: bool, resolved_only: bool) -> int:
    if not DB_ENABLED:
        print("✗ Banco de dados não configurado (DB_ENABLED=False). Abortando.")
        return 1

    conds = []
    if resolved_only:
        conds.append(RecommendationSnapshot.status.in_(RESOLVED_STATUSES))
    if days and days > 0:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        conds.append(RecommendationSnapshot.created_at >= since)

    async with get_session() as session:
        stmt = select(RecommendationSnapshot)
        if conds:
            stmt = stmt.where(and_(*conds))
        snaps = (await session.execute(stmt)).scalars().all()

        total = len(snaps)
        if total == 0:
            print("Nenhum snapshot no filtro. Nada a fazer.")
            return 0

        changed = 0
        skipped_uncomputable = 0
        deltas = []
        sample = []

        for snap in snaps:
            old = _f(snap.score)
            new = _v2_from_features(snap)
            if new is None:
                skipped_uncomputable += 1
                continue
            if old is not None:
                deltas.append(new - old)
            if old is None or abs((new or 0) - (old or 0)) >= 0.05:
                changed += 1
                if len(sample) < 12:
                    sample.append((snap.id, getattr(snap, "symbol", "?"),
                                   snap.status, old, new))
            if apply:
                feats = dict(snap.features or {})
                if "score_legacy" not in feats and old is not None:
                    feats["score_legacy"] = old
                snap.features = feats
                snap.score = new

        # ── relatório ──
        mode = "APPLY (escrevendo)" if apply else "DRY-RUN (nada gravado)"
        print(f"\n=== Backfill score V2 — {mode} ===")
        print(f"Filtro: days={days or 'all'} resolved_only={resolved_only}")
        print(f"Snapshots: {total} | mudariam: {changed} | "
              f"não-computáveis (mantêm legado): {skipped_uncomputable}")
        if deltas:
            avg = sum(deltas) / len(deltas)
            mn, mx = min(deltas), max(deltas)
            print(f"Delta (novo-antigo): média={avg:+.1f} min={mn:+.1f} max={mx:+.1f}")
            print(f"Histograma de deltas: {_hist(deltas)}")
        print("\nAmostra (id | symbol | status | antigo → novo):")
        for sid, sym, st, o, n in sample:
            os_ = f"{o:.1f}" if o is not None else "—"
            print(f"  #{sid:<6} {str(sym):<14} {str(st):<12} {os_:>6} → {n:.1f}")

        if apply:
            await session.commit()
            print(f"\n✓ Commit OK. {changed} scores atualizados; "
                  f"score legado salvo em features['score_legacy'].")
        else:
            print("\n(Dry-run: rode novamente com --apply pra gravar. "
                  "Ativar SÓ junto da flag SCORE_FORMULA_V2, pós-teste 0.50.)")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Backfill da coluna score com a fórmula V2.")
    ap.add_argument("--days", type=int, default=0,
                    help="Limita aos últimos N dias (por created_at). 0 = tudo.")
    ap.add_argument("--apply", action="store_true",
                    help="ESCREVE no banco. Sem isso é dry-run.")
    ap.add_argument("--status-resolved", dest="resolved_only", action="store_true",
                    help="Só snapshots resolvidos (won_*/lost).")
    args = ap.parse_args()
    rc = asyncio.run(run(args.days, args.apply, args.resolved_only))
    sys.exit(rc)


if __name__ == "__main__":
    main()
