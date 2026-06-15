"""
Assertiveness Service — agregação READ-ONLY pro "painel de assertividade".

Responde, num único lugar, "o quão confiável o bot está sendo?" cruzando:
  1. real_money  — trades reais source=auto resolvidos (dinheiro de verdade):
                   win-rate, TP1/TP2 hit, expectancy em R, P&L USD, por status.
  2. shadow      — recommendation_snapshots resolvidos (amostra maior, mesma
                   base que calibra P(TP1)): win-rate, TP1/TP2 hit, expectancy R.
  3. gates       — contadores PERSISTIDOS de skip por gate (skip_reason_stats),
                   sobrevivem redeploy → "qual gate mais barrou na janela?".
  4. calibration — maturidade da calibração (>= MIN_SAMPLE_TOTAL resolvidos).

Tudo fail-soft: qualquer erro de DB vira seção vazia, nunca derruba a API.
Não escreve nada, não toca no loop de execução — só lê e soma.
"""
from __future__ import annotations
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from sqlalchemy import select, func, and_, not_

from services import calibration_service as _calib

log = logging.getLogger(__name__)

# Status de trade real (real_trades) — espelha real_trade.py
_REAL_TP1_HIT = ("closed_tp1", "closed_tp2", "closed_be")  # tocou TP1 em algum momento
_REAL_TP2_HIT = ("closed_tp2",)

# Status de snapshot (recommendation_snapshots) — espelha calibration_service
_SNAP_WIN = ("won_tp1", "won_tp1_be", "won_tp2")
_SNAP_TP2 = ("won_tp2",)
_SNAP_RESOLVED = _SNAP_WIN + ("lost", "expired")


async def _real_money_stats(days: int) -> Dict[str, Any]:
    from db import get_session
    from models.real_trade import RealTrade
    since = datetime.now(timezone.utc) - timedelta(days=days)
    out = {
        "count": 0, "wins": 0, "losses": 0, "win_rate_pct": None,
        "avg_r": None, "expectancy_r": None, "sum_pnl_usd": 0.0,
        "tp1_hit_rate_pct": None, "tp2_hit_rate_pct": None,
        "by_status": {},
    }
    try:
        async with get_session() as session:
            stmt = (
                select(RealTrade)
                .where(RealTrade.source == "auto")
                .where(RealTrade.status != "open")
                .where(RealTrade.opened_at >= since)
            )
            rows = list((await session.execute(stmt)).scalars().all())
    except Exception as e:
        log.warning(f"[assertiveness] real_money read falhou: {e}")
        return out
    n = len(rows)
    if n == 0:
        return out
    by_status: dict[str, int] = defaultdict(int)
    rs = []
    pnl_sum = 0.0
    tp1_hit = 0
    tp2_hit = 0
    wins = 0
    for t in rows:
        by_status[t.status] += 1
        r = float(t.realized_r) if t.realized_r is not None else 0.0
        rs.append(r)
        pnl_sum += float(t.pnl_usd or 0)
        if r > 0:
            wins += 1
        if t.status in _REAL_TP1_HIT:
            tp1_hit += 1
        if t.status in _REAL_TP2_HIT:
            tp2_hit += 1
    avg_r = sum(rs) / n
    out.update({
        "count": n,
        "wins": wins,
        "losses": n - wins,
        "win_rate_pct": round(wins / n * 100, 1),
        "avg_r": round(avg_r, 3),
        "expectancy_r": round(avg_r, 3),
        "sum_pnl_usd": round(pnl_sum, 2),
        "tp1_hit_rate_pct": round(tp1_hit / n * 100, 1),
        "tp2_hit_rate_pct": round(tp2_hit / n * 100, 1),
        "by_status": dict(by_status),
    })
    return out


async def _shadow_stats(days: int) -> Dict[str, Any]:
    from db import get_session
    from models.recommendation_snapshot import RecommendationSnapshot
    since = datetime.now(timezone.utc) - timedelta(days=days)
    out = {
        "count": 0, "wins": 0, "win_rate_pct": None,
        "tp1_hit_rate_pct": None, "tp2_hit_rate_pct": None,
        "avg_r": None, "expectancy_r": None, "by_status": {},
    }
    try:
        async with get_session() as session:
            stmt = (
                select(
                    RecommendationSnapshot.status,
                    RecommendationSnapshot.realized_r,
                )
                .where(RecommendationSnapshot.status.in_(_SNAP_RESOLVED))
                # DE-POLUIÇÃO (fonte única com calibration_service): exclui voids
                # — 'expired' que nunca teve avaliação justa (no-data / flip).
                # Mantém a win-rate do painel honesta.
                .where(_calib._not_fast_void())
                .where(RecommendationSnapshot.outcome_at >= since)
            )
            rows = list((await session.execute(stmt)).all())
    except Exception as e:
        log.warning(f"[assertiveness] shadow read falhou: {e}")
        return out
    n = len(rows)
    if n == 0:
        return out
    by_status: dict[str, int] = defaultdict(int)
    wins = 0
    tp1_hit = 0
    tp2_hit = 0
    rs = []
    for status, r in rows:
        by_status[status] += 1
        rs.append(float(r) if r is not None else 0.0)
        if status in _SNAP_WIN:
            wins += 1
            tp1_hit += 1
        if status in _SNAP_TP2:
            tp2_hit += 1
    avg_r = sum(rs) / n
    out.update({
        "count": n,
        "wins": wins,
        "win_rate_pct": round(wins / n * 100, 1),
        "tp1_hit_rate_pct": round(tp1_hit / n * 100, 1),
        "tp2_hit_rate_pct": round(tp2_hit / n * 100, 1),
        "avg_r": round(avg_r, 3),
        "expectancy_r": round(avg_r, 3),
        "by_status": dict(by_status),
    })
    return out


async def _gate_stats(days: int) -> Dict[str, Any]:
    from db import get_session
    from models.skip_reason_stat import SkipReasonStat
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date()
    out = {"window_days": days, "total_skips": 0, "items": []}
    try:
        async with get_session() as session:
            stmt = (
                select(
                    SkipReasonStat.gate,
                    func.sum(SkipReasonStat.count).label("total"),
                    func.max(SkipReasonStat.last_seen).label("last_seen"),
                )
                .where(SkipReasonStat.day >= since)
                .group_by(SkipReasonStat.gate)
                .order_by(func.sum(SkipReasonStat.count).desc())
            )
            grouped = list((await session.execute(stmt)).all())
            # último motivo/símbolo por gate (linha mais recente)
            last_stmt = (
                select(SkipReasonStat)
                .where(SkipReasonStat.day >= since)
                .order_by(SkipReasonStat.last_seen.desc())
            )
            recent = list((await session.execute(last_stmt)).scalars().all())
    except Exception as e:
        log.warning(f"[assertiveness] gate read falhou: {e}")
        return out
    last_by_gate: dict[str, Any] = {}
    for row in recent:
        if row.gate not in last_by_gate:
            last_by_gate[row.gate] = row
    items = []
    total = 0
    for gate, cnt, last_seen in grouped:
        cnt_i = int(cnt or 0)
        total += cnt_i
        ex = last_by_gate.get(gate)
        items.append({
            "gate": gate,
            "count": cnt_i,
            "last_reason": ex.last_reason if ex else None,
            "last_symbol": ex.last_symbol if ex else None,
            "last_seen": last_seen.isoformat() if last_seen else None,
        })
    out["items"] = items
    out["total_skips"] = total
    return out


async def _calibration_stats() -> Dict[str, Any]:
    out = {"mature": False, "total_resolved": 0, "min_sample": 30,
           "p_global": None, "win_rate_pct": None, "computed_at": None}
    try:
        from services import calibration_service as cs
        out["min_sample"] = cs.MIN_SAMPLE_TOTAL
        calib = await cs.get_calibration()
        if calib:
            out.update({
                "mature": True,
                "total_resolved": calib.get("total_resolved", 0),
                "p_global": calib.get("p_global"),
                "win_rate_pct": round((calib.get("p_global") or 0) * 100, 1),
                "computed_at": calib.get("computed_at"),
            })
        else:
            # imatura: ainda assim reporta quantos resolvidos existem
            try:
                from db import get_session
                from models.recommendation_snapshot import RecommendationSnapshot
                async with get_session() as session:
                    cnt = int((await session.execute(
                        select(func.count(RecommendationSnapshot.id))
                        .where(RecommendationSnapshot.status.in_(_SNAP_RESOLVED))
                    )).scalar() or 0)
                out["total_resolved"] = cnt
            except Exception:
                pass
    except Exception as e:
        log.warning(f"[assertiveness] calibration read falhou: {e}")
    return out


async def get_assertiveness(days: int = 30, gate_days: int = 7) -> Dict[str, Any]:
    """Agrega tudo pro painel. days = janela de outcomes; gate_days = janela de
    skips (mais curta porque skips são muito mais frequentes)."""
    from db import DB_ENABLED
    if not DB_ENABLED:
        return {"enabled": False, "reason": "DB desabilitado"}
    real_money = await _real_money_stats(days)
    shadow = await _shadow_stats(days)
    gates = await _gate_stats(gate_days)
    calibration = await _calibration_stats()
    return {
        "enabled": True,
        "window_days": days,
        "real_money": real_money,
        "shadow": shadow,
        "gates": gates,
        "calibration": calibration,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }
