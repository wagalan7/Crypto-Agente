"""
Paper-trade Service (#8) — formaliza o que já existia disfarçado.

Hoje toda rec emitida vira `recommendation_snapshot` e o tracker
monitora preço até resolver (TP1/TP2/BE/stop/expired). Isso ↑ é
PAPER-TRADE — só falta um endpoint que apresente isso como tal.

Quando #11 trouxer integração Bybit, criamos coluna/tabela paralela
pra distinguir "paper" de "real" e o dashboard #10 cruza os dois.

Por enquanto: snapshots = paper trades.

Métricas expostas:
  - equity curve (P&L acumulado por dia, em % da banca)
  - stats agregados por tier (WR, avgR, expectancy, max DD)
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone, timedelta
from collections import defaultdict

from sqlalchemy import select

from db import DB_ENABLED, get_session
from models.recommendation_snapshot import RecommendationSnapshot

log = logging.getLogger(__name__)


async def equity_curve(days: int = 30) -> dict:
    """
    Equity curve do paper-trading dos últimos N dias.
    Cada ponto = (date, pnl_pct_do_dia, pnl_pct_acumulado, trades_resolvidos).
    P&L é % da banca, calculado como sum(realized_r * risk_pct) — mesma
    convenção do DailyPnLPanel e do circuit breaker (consistência).
    """
    if not DB_ENABLED:
        return {"enabled": False, "curve": [], "trades_total": 0}

    since = datetime.now(timezone.utc) - timedelta(days=max(1, days))
    async with get_session() as session:
        stmt = (
            select(RecommendationSnapshot)
            .where(RecommendationSnapshot.outcome_at.is_not(None))
            .where(RecommendationSnapshot.outcome_at >= since)
            .where(RecommendationSnapshot.realized_r.is_not(None))
            .order_by(RecommendationSnapshot.outcome_at.asc())
        )
        rows = (await session.execute(stmt)).scalars().all()

    # Agrupa por dia UTC
    daily_pnl: dict[str, float] = defaultdict(float)
    daily_count: dict[str, int] = defaultdict(int)
    for r in rows:
        if r.outcome_at is None or r.realized_r is None:
            continue
        day = r.outcome_at.strftime("%Y-%m-%d")
        contribution = float(r.realized_r) * float(r.risk_pct or 0.0)
        daily_pnl[day] += contribution
        daily_count[day] += 1

    # Gera curva cumulativa cronológica
    sorted_days = sorted(daily_pnl.keys())
    curve = []
    acc = 0.0
    for day in sorted_days:
        acc += daily_pnl[day]
        curve.append({
            "date": day,
            "pnl_pct": round(daily_pnl[day], 3),
            "cumulative_pct": round(acc, 3),
            "trades": daily_count[day],
        })

    return {
        "enabled": True,
        "mode": "paper",
        "days": days,
        "curve": curve,
        "trades_total": sum(daily_count.values()),
        "final_pnl_pct": round(acc, 3),
    }


async def stats_by_tier(days: int = 30) -> dict:
    """
    Stats agregados por tier (A+/A/B) dos últimos N dias.
    Para cada tier: WR, avgR, expectancy, n_trades, max_consecutive_losses.
    """
    if not DB_ENABLED:
        return {"enabled": False, "tiers": {}}

    since = datetime.now(timezone.utc) - timedelta(days=max(1, days))
    async with get_session() as session:
        stmt = (
            select(RecommendationSnapshot)
            .where(RecommendationSnapshot.outcome_at.is_not(None))
            .where(RecommendationSnapshot.outcome_at >= since)
            .where(RecommendationSnapshot.realized_r.is_not(None))
            .order_by(RecommendationSnapshot.outcome_at.asc())
        )
        rows = (await session.execute(stmt)).scalars().all()

    by_tier: dict[str, list] = defaultdict(list)
    for r in rows:
        by_tier[r.tier].append({
            "realized_r": float(r.realized_r) if r.realized_r is not None else 0.0,
            "risk_pct": float(r.risk_pct or 0.0),
            "status": r.status,
        })

    tier_summary: dict[str, dict] = {}
    for tier in ("A+", "A", "B"):
        items = by_tier.get(tier, [])
        n = len(items)
        if n == 0:
            tier_summary[tier] = {
                "n": 0, "wins": 0, "losses": 0, "wr_pct": None,
                "avg_r": None, "expectancy_r": None,
                "max_consec_losses": 0, "pnl_pct": 0.0,
            }
            continue
        wins = sum(1 for x in items if x["realized_r"] > 0)
        losses = sum(1 for x in items if x["realized_r"] <= 0)
        wr = wins / n if n else 0
        avg_r = sum(x["realized_r"] for x in items) / n
        # Expectancy em R: avg_r já é a esperança matemática por trade
        expectancy_r = avg_r
        # Streak máxima de perdas consecutivas
        max_consec, cur = 0, 0
        for x in items:
            if x["realized_r"] <= 0:
                cur += 1
                max_consec = max(max_consec, cur)
            else:
                cur = 0
        pnl = sum(x["realized_r"] * x["risk_pct"] for x in items)

        tier_summary[tier] = {
            "n": n,
            "wins": wins,
            "losses": losses,
            "wr_pct": round(wr * 100, 2),
            "avg_r": round(avg_r, 3),
            "expectancy_r": round(expectancy_r, 3),
            "max_consec_losses": max_consec,
            "pnl_pct": round(pnl, 3),
        }

    return {
        "enabled": True,
        "mode": "paper",
        "days": days,
        "tiers": tier_summary,
        "trades_total": sum(t["n"] for t in tier_summary.values()),
    }


async def summary(days: int = 30) -> dict:
    """Visão única combinando equity + tier breakdown — pra dashboard."""
    eq = await equity_curve(days=days)
    st = await stats_by_tier(days=days)
    return {
        "enabled": eq.get("enabled", False),
        "mode": "paper",
        "days": days,
        "equity": eq,
        "tier_stats": st.get("tiers", {}),
    }
