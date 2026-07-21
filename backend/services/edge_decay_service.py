"""
Edge Decay Service — detecta quando o edge VIVO de um símbolo/direção AZEDOU em
relação ao seu próprio histórico recente e devolve um multiplicador de size ≤1.0
para reduzir exposição ANTES de sangrar mais stops.

Motivação
---------
O bot já aprende size por moeda a partir do BACKTEST histórico completo
(symbol_learning_service), mas nada detecta que uma moeda/estratégia que
funcionava PAROU de funcionar ao vivo. Este módulo é a contraparte VIVA: compara
o R médio recente (janela curta) com o baseline do próprio símbolo (janela longa)
sobre os snapshots resolvidos, e quando o recente decaiu para território negativo
enquanto o baseline tinha edge, reduz o size de forma graduada.

Só REDUZ (nunca aumenta). Fail-soft: qualquer erro → multiplicador 1.0 (mão cheia).

Segurança / design
------------------
  • Gate mestre EDGE_DECAY_ENABLED (default OFF) — deploy não muda nada até ligar.
  • Cache em memória com TTL (default 30min) — a query pesada roda no máximo 1×/TTL,
    NUNCA por-trade no hot path. `get_mult()` é leitura pura de cache.
  • Keyed por (symbol, direction) com fallback (symbol, "any").
  • Só morde com amostra recente suficiente (EDGE_DECAY_MIN_SAMPLE).
  • Guardas de piso (nunca abaixo de EDGE_DECAY_MULT_MIN).
"""
from __future__ import annotations

import os
import time
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple

log = logging.getLogger(__name__)

_RESOLVED = ("won_tp1", "won_tp1_be", "won_tp2", "lost", "expired")

# Cache: {(symbol, direction|"any"): {"mult": float, "reason": str,
#         "recent_n": int, "recent_avg_r": float, "base_avg_r": float}}
_CACHE: dict[tuple[str, str], dict] = {}
_CACHE_AT: float = 0.0


def _b(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


def _cfg() -> dict:
    return {
        "enabled": _b("EDGE_DECAY_ENABLED", "false"),
        "window_days": _i("EDGE_DECAY_WINDOW_DAYS", 60),   # baseline (longo)
        "recent_days": _i("EDGE_DECAY_RECENT_DAYS", 14),   # janela recente
        "min_sample": _i("EDGE_DECAY_MIN_SAMPLE", 8),      # mín. de resolvidos recentes
        "r_floor": _f("EDGE_DECAY_R_FLOOR", 0.0),          # recent avg_r ≥ isto → sem corte
        "r_full": _f("EDGE_DECAY_R_FULL", -0.3),           # recent avg_r ≤ isto → corte máximo
        "base_min": _f("EDGE_DECAY_BASE_MIN", 0.1),        # baseline precisa ter tido edge
        "mult_min": _f("EDGE_DECAY_MULT_MIN", 0.5),        # piso do multiplicador
        "ttl_sec": _i("EDGE_DECAY_TTL_SEC", 1800),
    }


def is_enabled() -> bool:
    return _cfg()["enabled"]


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _mult_from(recent_avg_r: float, base_avg_r: float, recent_n: int, c: dict) -> Tuple[float, str]:
    """Regra pura: multiplicador ≤1.0 a partir do avg_r recente vs baseline."""
    if recent_n < c["min_sample"]:
        return 1.0, f"amostra recente pequena ({recent_n})"
    if base_avg_r < c["base_min"]:
        return 1.0, f"baseline sem edge (avg {base_avg_r:+.2f}) — não é decay"
    if recent_avg_r >= c["r_floor"]:
        return 1.0, f"recente ok (avg {recent_avg_r:+.2f} ≥ {c['r_floor']:+.2f})"
    span = max(1e-6, c["r_floor"] - c["r_full"])
    frac = _clamp((c["r_floor"] - recent_avg_r) / span, 0.0, 1.0)
    mult = round(1.0 - frac * (1.0 - c["mult_min"]), 4)
    mult = _clamp(mult, c["mult_min"], 1.0)
    return mult, (f"edge decaiu: recente avg {recent_avg_r:+.2f}R ({recent_n}) vs "
                  f"baseline {base_avg_r:+.2f}R ⇒ ×{mult:.2f}")


async def maybe_refresh(force: bool = False) -> None:
    """Recalcula o cache se vencido (TTL). Uma query por refresh. Fail-soft."""
    global _CACHE, _CACHE_AT
    c = _cfg()
    if not c["enabled"]:
        return
    now = time.time()
    if not force and (now - _CACHE_AT) < c["ttl_sec"] and _CACHE:
        return
    try:
        from sqlalchemy import select
        from db import get_session
        from models.recommendation_snapshot import RecommendationSnapshot as RS

        since_long = datetime.now(timezone.utc) - timedelta(days=c["window_days"])
        recent_cut = datetime.now(timezone.utc) - timedelta(days=c["recent_days"])
        rows = []
        async with get_session() as session:
            rows = list((await session.execute(
                select(RS.symbol, RS.direction, RS.realized_r, RS.outcome_at)
                .where(RS.status.in_(_RESOLVED))
                .where(RS.realized_r.isnot(None))
                .where(RS.outcome_at >= since_long)
            )).all())

        # Agrega por (symbol, direction) e por (symbol, "any").
        agg: dict[tuple[str, str], dict] = {}

        def _acc(key, r, is_recent):
            d = agg.setdefault(key, {"base_sum": 0.0, "base_n": 0,
                                     "rec_sum": 0.0, "rec_n": 0})
            d["base_sum"] += r
            d["base_n"] += 1
            if is_recent:
                d["rec_sum"] += r
                d["rec_n"] += 1

        for sym, direction, r, oat in rows:
            if sym is None or r is None or oat is None:
                continue
            rr = float(r)
            is_recent = oat >= recent_cut
            dl = str(direction or "").strip().lower()
            _acc((sym, dl if dl in ("long", "short") else "any"), rr, is_recent)
            _acc((sym, "any"), rr, is_recent)

        new_cache: dict[tuple[str, str], dict] = {}
        for key, d in agg.items():
            base_avg = d["base_sum"] / d["base_n"] if d["base_n"] else 0.0
            rec_avg = d["rec_sum"] / d["rec_n"] if d["rec_n"] else 0.0
            mult, reason = _mult_from(rec_avg, base_avg, d["rec_n"], c)
            if mult < 1.0:  # só guarda quem realmente corta (cache enxuto)
                new_cache[key] = {
                    "mult": mult, "reason": reason, "recent_n": d["rec_n"],
                    "recent_avg_r": round(rec_avg, 3), "base_avg_r": round(base_avg, 3),
                }
        _CACHE = new_cache
        _CACHE_AT = now
        log.info(f"[edge-decay] cache atualizado: {len(_CACHE)} símbolos/direções em decay "
                 f"(janela {c['window_days']}d/{c['recent_days']}d, {len(rows)} resolvidos).")
    except Exception as e:
        log.warning(f"[edge-decay] refresh falhou (fail-soft, mantém cache): {e}")


def get_mult(symbol: str, direction: Optional[str]) -> Tuple[float, str]:
    """Leitura PURA do cache. Retorna (mult ≤1.0, motivo). Nunca lança."""
    try:
        if not _CACHE:
            return 1.0, "sem decay em cache"
        dl = str(direction or "").strip().lower()
        if dl in ("long", "short"):
            hit = _CACHE.get((symbol, dl))
            if hit:
                return hit["mult"], hit["reason"]
        hit = _CACHE.get((symbol, "any"))
        if hit:
            return hit["mult"], hit["reason"]
        return 1.0, "sem decay"
    except Exception:
        return 1.0, "erro (fail-soft)"


def cache_snapshot() -> dict:
    """Introspecção read-only (para env_info/endpoints). Não muta nada."""
    return {
        "enabled": is_enabled(),
        "cached_at": _CACHE_AT,
        "decayed_count": len(_CACHE),
        "items": [
            {"symbol": k[0], "direction": k[1], **v}
            for k, v in list(_CACHE.items())[:50]
        ],
    }
