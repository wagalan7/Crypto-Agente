"""
Calibration Versions Service (#9) — snapshot + comparação de modelos PAV.

Tarefas:
  - snapshot_current(): captura o estado atual de `calibration_service`
    e salva em `calibration_versions` (com métricas retroativas computadas
    dos últimos 30 dias).
  - list_versions(): histórico ordenado.
  - get_version(): inspeciona uma versão específica.
  - compare(): diff entre 2 versões (delta de P por bin + métricas).
  - run_monthly_snapshot(): wrapper pra ser chamado pelo scheduler/cron.

A/B test ao vivo (50% novo / 50% antigo) é Fase 9.2 segundo o roadmap
e requer mudar o scan loop pra randomizar; fica fora deste service.
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import select, desc

from db import DB_ENABLED, get_session
from models.calibration_version import CalibrationVersion
from models.recommendation_snapshot import RecommendationSnapshot

log = logging.getLogger(__name__)


async def _compute_retroactive_metrics(session, lookback_days: int = 30) -> dict:
    """
    Computa win_rate / avg_r / sharpe dos últimos N dias.
    Sharpe simplificado: avg_r / std_r (assume distribuição normal-ish dos R).
    """
    since = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    stmt = (
        select(RecommendationSnapshot.realized_r, RecommendationSnapshot.status)
        .where(RecommendationSnapshot.outcome_at >= since)
        .where(RecommendationSnapshot.realized_r.is_not(None))
    )
    rows = (await session.execute(stmt)).all()
    if not rows:
        return {"win_rate": None, "avg_r": None, "sharpe": None}
    rs = [float(r[0]) for r in rows]
    wins = sum(1 for r in rs if r > 0)
    n = len(rs)
    wr = wins / n
    avg = sum(rs) / n
    var = sum((r - avg) ** 2 for r in rs) / n
    std = var ** 0.5
    sharpe = (avg / std) if std > 0 else None
    return {
        "win_rate": round(wr, 4),
        "avg_r": round(avg, 4),
        "sharpe": round(sharpe, 4) if sharpe is not None else None,
    }


async def snapshot_current(notes: Optional[str] = None, make_active: bool = True) -> Optional[dict]:
    """
    Captura o estado atual do calibration_service e grava versão.
    `make_active=True` marca esta como a versão "live" (e desativa as outras).
    Retorna dict com a versão criada, ou None se calibração não está pronta.
    """
    if not DB_ENABLED:
        return None
    from services import calibration_service
    calib = await calibration_service.get_calibration()
    if calib is None:
        log.info("[calib-versions] snapshot skip: calibração não está pronta ainda")
        return None

    version_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")

    async with get_session() as session:
        # Já existe versão com mesmo ts? (acontece se chamarem 2x no mesmo minuto)
        existing = (await session.execute(
            select(CalibrationVersion).where(CalibrationVersion.version == version_str)
        )).scalar_one_or_none()
        if existing is not None:
            log.info(f"[calib-versions] versão {version_str} já existe, skip")
            return _to_dict(existing)

        metrics = await _compute_retroactive_metrics(session, lookback_days=30)

        if make_active:
            # Desativa todas
            from sqlalchemy import update
            await session.execute(
                update(CalibrationVersion).values(active=False).where(CalibrationVersion.active.is_(True))
            )

        ver = CalibrationVersion(
            version=version_str,
            total_resolved=int(calib.get("total_resolved") or 0),
            p_global=float(calib.get("p_global") or 0.0),
            lookback_days=int(calib.get("lookback_days") if calib.get("lookback_days") is not None else 90),
            source=str(calib.get("source") or "db"),
            bins_json={"bins": calib.get("bins", [])},
            win_rate=metrics["win_rate"],
            avg_r=metrics["avg_r"],
            sharpe=metrics["sharpe"],
            active=bool(make_active),
            notes=notes,
        )
        session.add(ver)
        await session.flush()
        await session.commit()
        log.info(
            f"[calib-versions] snapshot {version_str} criado · "
            f"n={ver.total_resolved} wr={ver.win_rate} sharpe={ver.sharpe} "
            f"active={ver.active}"
        )
        return _to_dict(ver)


async def list_versions(limit: int = 20) -> list[dict]:
    if not DB_ENABLED:
        return []
    async with get_session() as session:
        stmt = (
            select(CalibrationVersion)
            .order_by(desc(CalibrationVersion.computed_at))
            .limit(limit)
        )
        rows = (await session.execute(stmt)).scalars().all()
        return [_to_dict(r) for r in rows]


async def get_active() -> Optional[dict]:
    if not DB_ENABLED:
        return None
    async with get_session() as session:
        stmt = select(CalibrationVersion).where(CalibrationVersion.active.is_(True)).limit(1)
        row = (await session.execute(stmt)).scalar_one_or_none()
        return _to_dict(row) if row else None


async def compare(version_a: str, version_b: str) -> Optional[dict]:
    """
    Diff entre 2 versões: delta de P_calibrada por bin + delta das métricas.
    A é o baseline, B é o candidato (positivo = B melhor).
    """
    if not DB_ENABLED:
        return None
    async with get_session() as session:
        stmt = select(CalibrationVersion).where(
            CalibrationVersion.version.in_([version_a, version_b])
        )
        rows = {r.version: r for r in (await session.execute(stmt)).scalars().all()}
        a = rows.get(version_a)
        b = rows.get(version_b)
        if a is None or b is None:
            return None

        # Diff por bin
        a_bins = {bin_["label"]: bin_ for bin_ in (a.bins_json or {}).get("bins", [])}
        b_bins = {bin_["label"]: bin_ for bin_ in (b.bins_json or {}).get("bins", [])}
        diffs = []
        for label in sorted(set(a_bins.keys()) | set(b_bins.keys())):
            pa = a_bins.get(label, {}).get("p_calibrated")
            pb = b_bins.get(label, {}).get("p_calibrated")
            na = a_bins.get(label, {}).get("n_total", 0)
            nb = b_bins.get(label, {}).get("n_total", 0)
            diffs.append({
                "bin": label,
                "p_a": pa,
                "p_b": pb,
                "delta_p": (round(pb - pa, 4) if (pa is not None and pb is not None) else None),
                "n_a": na,
                "n_b": nb,
            })

        return {
            "version_a": _to_dict(a),
            "version_b": _to_dict(b),
            "bins_diff": diffs,
            "metrics_delta": {
                "win_rate": _delta(a.win_rate, b.win_rate),
                "avg_r": _delta(a.avg_r, b.avg_r),
                "sharpe": _delta(a.sharpe, b.sharpe),
            },
            "verdict": _verdict(a, b),
        }


def _delta(a, b):
    if a is None or b is None:
        return None
    return round(b - a, 4)


def _verdict(a: CalibrationVersion, b: CalibrationVersion) -> str:
    """
    Critério de aceite #9: modelo novo só substitui antigo se Sharpe ≥ antigo.
    Aplica esse critério aqui.
    """
    if a.sharpe is None or b.sharpe is None:
        return "inconclusive (sharpe missing)"
    if b.sharpe >= a.sharpe:
        return "B accepted (sharpe ≥ A)"
    return f"B rejected (sharpe {b.sharpe:.3f} < A {a.sharpe:.3f})"


def _to_dict(ver: CalibrationVersion | None) -> dict | None:
    if ver is None:
        return None
    return {
        "id": ver.id,
        "version": ver.version,
        "total_resolved": ver.total_resolved,
        "p_global": ver.p_global,
        "lookback_days": ver.lookback_days,
        "source": ver.source,
        "win_rate": ver.win_rate,
        "avg_r": ver.avg_r,
        "sharpe": ver.sharpe,
        "active": ver.active,
        "notes": ver.notes,
        "computed_at": ver.computed_at.isoformat() if ver.computed_at else None,
        "bins": (ver.bins_json or {}).get("bins", []),
    }


async def last_recalibration_at() -> Optional[datetime]:
    """
    Retorna o `computed_at` da recalibração mais recente (manual ou automática),
    identificada pelo marcador "recalibra" nas notes. None se nunca houve uma.
    Usado pelo loop automático pra decidir se já passou o intervalo.
    """
    if not DB_ENABLED:
        return None
    async with get_session() as session:
        stmt = (
            select(CalibrationVersion.computed_at)
            .where(CalibrationVersion.notes.ilike("%recalibra%"))
            .order_by(desc(CalibrationVersion.computed_at))
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none()


async def recalibrate(notes: Optional[str] = None, make_active: bool = True) -> dict:
    """
    Recalibração manual da autoaprendizagem (#10).

    Força o modelo a reaprender com TODO o histórico de trades resolvidos
    (won_tp2, won_tp1_be, won_tp1, lost, expired), não só a janela móvel:
      1. Invalida os caches de calibração E de learning (buckets).
      2. Recomputa a tabela score→P(TP1) sobre o histórico completo.
      3. Recomputa os multiplicadores/blocks de bucket sobre o histórico.
      4. Versiona o resultado como nova calibração ativa e compara com a
         anterior (verdict por Sharpe, política log-only da Fase 1).

    Retorna um relatório com o que mudou — seguro pra chamar a qualquer hora.
    """
    from services import calibration_service
    from services import learning_service

    # 1) Invalida caches pra garantir recomputo do zero
    calibration_service.invalidate_cache()
    learning_service.invalidate_cache()

    # 2) Recomputa calibração (full history via LOOKBACK_DAYS<=0)
    calib = await calibration_service.get_calibration()

    # 3) Recomputa auto-adjust/blocks sobre o histórico completo
    try:
        auto_adj = await learning_service.compute_auto_adjustments()
    except Exception as e:
        log.warning(f"[recalibrate] auto-adjust falhou (fail-open): {e}")
        auto_adj = {"enabled": False, "reason": str(e)}

    if calib is None:
        return {
            "recalibrated": False,
            "reason": "Calibração ainda não pronta — amostra de trades resolvidos insuficiente.",
            "calibration": None,
            "auto_adjust": auto_adj,
            "version": None,
            "comparison": None,
        }

    # 4) Captura active anterior antes de versionar
    old_version = None
    if DB_ENABLED:
        async with get_session() as session:
            old = (await session.execute(
                select(CalibrationVersion).where(CalibrationVersion.active.is_(True)).limit(1)
            )).scalar_one_or_none()
            old_version = old.version if old else None

    new = await snapshot_current(
        notes=notes or "recalibração manual (histórico completo)",
        make_active=make_active,
    )

    cmp = None
    if new is not None and old_version and old_version != new["version"]:
        cmp = await compare(old_version, new["version"])
        if cmp and "rejected" in cmp.get("verdict", ""):
            log.warning(
                f"[recalibrate] nova calibração tem Sharpe menor: {cmp['verdict']} "
                f"(mantida ativa — política log-only)"
            )

    return {
        "recalibrated": True,
        "lookback_days": calib.get("lookback_days"),
        "total_resolved": calib.get("total_resolved"),
        "p_global": calib.get("p_global"),
        "calibration": calib,
        "auto_adjust": {
            "enabled": auto_adj.get("enabled"),
            "active_buckets": auto_adj.get("active_buckets"),
            "blocked_buckets": auto_adj.get("blocked_buckets"),
            "total_trades": auto_adj.get("total_trades"),
        },
        "version": new,
        "comparison": cmp,
    }


async def run_monthly_snapshot() -> Optional[dict]:
    """
    Wrapper pra ser chamado por cron mensal (1º do mês, por ex).
    Tira snapshot do estado atual, marca como active, e (opcional)
    compara com versão anterior — se Sharpe regrediu, loga warning.
    """
    if not DB_ENABLED:
        return None

    async with get_session() as session:
        # Pega a active atual ANTES de criar a nova
        old = (await session.execute(
            select(CalibrationVersion).where(CalibrationVersion.active.is_(True)).limit(1)
        )).scalar_one_or_none()
        old_version = old.version if old else None

    new = await snapshot_current(notes="monthly snapshot", make_active=True)
    if new is None:
        return None

    if old_version:
        cmp = await compare(old_version, new["version"])
        if cmp and "rejected" in cmp.get("verdict", ""):
            log.warning(
                f"[calib-versions] ATENÇÃO monthly snapshot regrediu: "
                f"{cmp['verdict']} (mantida ativa mesmo assim — "
                f"política Fase 1 é log-only)"
            )
        return {"snapshot": new, "comparison": cmp}
    return {"snapshot": new, "comparison": None}
