"""
RealTrade Service (#11.2) — registra execuções reais paralelo ao paper-trade.

Fluxo manual:
  1. User vê rec no painel, executa na corretora (Bybit/Binance/qualquer)
  2. POST /api/real-trades { recommendation_id, entry_price, qty } → grava open
  3. Quando fechar, PATCH /api/real-trades/{id}/close { exit_price, status }
  4. Sistema calcula realized_r, pnl_usd, pnl_pct, slippage vs rec

Fluxo auto (futuro #11.3):
  - Bot dispara ordem na Bybit via bybit_signed_service
  - Grava RealTrade com source='auto' + bybit_order_id
  - Tracker monitora order_history pra atualizar status

Stats:
  - summary() retorna mesmo shape que paper_trade_service.summary() →
    dashboard #10 já pluga sem refactor
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
from collections import defaultdict

from sqlalchemy import select, desc, func

from db import DB_ENABLED, get_session
from models.real_trade import RealTrade
from models.recommendation_snapshot import RecommendationSnapshot

log = logging.getLogger(__name__)


# ─── Create / update ──────────────────────────────────────────────────────────


async def open_trade(
    symbol: str,
    side: str,
    qty: float,
    entry_price: float,
    *,
    recommendation_id: Optional[int] = None,
    leverage: Optional[int] = None,
    planned_stop: Optional[float] = None,
    planned_tp1: Optional[float] = None,
    planned_tp2: Optional[float] = None,
    entry_fee: float = 0.0,
    source: str = "manual",
    exchange: Optional[str] = None,
    exchange_order_id: Optional[str] = None,
    client_order_id: Optional[str] = None,
    notes: Optional[str] = None,
    # Bracket / trade manager (Fase 2)
    sl_order_id: Optional[str] = None,
    tp1_order_id: Optional[str] = None,
    tp2_order_id: Optional[str] = None,
    sl_current_price: Optional[float] = None,
) -> Optional[dict]:
    if not DB_ENABLED:
        return None
    async with get_session() as session:
        # Se ligou a uma rec, copia os níveis planejados dela (caso não foram passados)
        rec = None
        if recommendation_id:
            rec = (await session.execute(
                select(RecommendationSnapshot).where(RecommendationSnapshot.id == recommendation_id)
            )).scalar_one_or_none()
            if rec:
                planned_stop = planned_stop if planned_stop is not None else rec.stop_loss
                planned_tp1 = planned_tp1 if planned_tp1 is not None else rec.tp1
                planned_tp2 = planned_tp2 if planned_tp2 is not None else rec.tp2
                leverage = leverage or rec.leverage

        # Slippage vs entry teórico da rec
        slippage = None
        if rec is not None and rec.entry:
            diff = (entry_price - rec.entry) / rec.entry * 100
            # Em long, slippage positivo é ruim (pagou caro); em short, negativo é ruim.
            slippage = round(diff if side == "long" else -diff, 4)

        notional = qty * entry_price

        trade = RealTrade(
            symbol=symbol,
            side=side,
            source=source,
            recommendation_id=recommendation_id,
            exchange=exchange,
            exchange_order_id=exchange_order_id,
            client_order_id=client_order_id,
            qty=qty,
            leverage=leverage,
            notional_usd=notional,
            entry_price=entry_price,
            entry_fee=entry_fee,
            opened_at=datetime.now(timezone.utc),
            planned_stop=planned_stop,
            planned_tp1=planned_tp1,
            planned_tp2=planned_tp2,
            status="open",
            entry_slippage_pct=slippage,
            notes=notes,
            # Bracket state
            phase="pre_tp1",
            qty_initial=qty,
            sl_order_id=sl_order_id,
            tp1_order_id=tp1_order_id,
            tp2_order_id=tp2_order_id,
            sl_current_price=sl_current_price if sl_current_price is not None else planned_stop,
        )
        session.add(trade)
        await session.flush()
        await session.commit()
        log.info(
            f"[real-trade] OPEN #{trade.id} {symbol} {side} qty={qty} @ {entry_price} "
            f"(rec={recommendation_id}, source={source}, slip={slippage}%)"
        )
        return _to_dict(trade)


async def close_trade(
    trade_id: int,
    exit_price: float,
    *,
    status: str = "closed_manual",
    exit_fee: float = 0.0,
    notes: Optional[str] = None,
) -> Optional[dict]:
    if not DB_ENABLED:
        return None
    async with get_session() as session:
        trade = (await session.execute(
            select(RealTrade).where(RealTrade.id == trade_id)
        )).scalar_one_or_none()
        if trade is None:
            return None
        if trade.status != "open":
            log.warning(f"[real-trade] close skip #{trade_id}: já está {trade.status}")
            return _to_dict(trade)

        trade.exit_price = exit_price
        trade.exit_fee = exit_fee
        trade.closed_at = datetime.now(timezone.utc)
        trade.status = status
        if notes:
            trade.notes = (trade.notes + " | " + notes) if trade.notes else notes

        # Calcula P&L
        sign = 1 if trade.side == "long" else -1
        price_diff = (exit_price - trade.entry_price) * sign
        pnl_usd = price_diff * trade.qty - (trade.entry_fee or 0) - (exit_fee or 0)
        pnl_pct = (price_diff / trade.entry_price) * 100 if trade.entry_price else 0

        # realized_r = pnl em múltiplos do risco original (entry → stop)
        realized_r = None
        if trade.planned_stop and trade.entry_price:
            risk_dist = abs(trade.entry_price - trade.planned_stop)
            if risk_dist > 0:
                realized_r = round((price_diff) / risk_dist, 3)

        trade.pnl_usd = round(pnl_usd, 4)
        trade.pnl_pct = round(pnl_pct, 4)
        trade.realized_r = realized_r

        await session.commit()
        log.info(
            f"[real-trade] CLOSE #{trade.id} {trade.symbol} → status={status} "
            f"R={realized_r} pnl=${pnl_usd:.2f} ({pnl_pct:+.2f}%)"
        )
        return _to_dict(trade)


# ─── Reads ────────────────────────────────────────────────────────────────────


async def list_trades(
    status: Optional[str] = None,
    days: int = 30,
    limit: int = 200,
) -> list[dict]:
    if not DB_ENABLED:
        return []
    since = datetime.now(timezone.utc) - timedelta(days=days)
    async with get_session() as session:
        stmt = (
            select(RealTrade)
            .where(RealTrade.opened_at >= since)
            .order_by(desc(RealTrade.opened_at))
            .limit(limit)
        )
        if status:
            stmt = stmt.where(RealTrade.status == status)
        rows = (await session.execute(stmt)).scalars().all()
        return [_to_dict(r) for r in rows]


async def get_trade(trade_id: int) -> Optional[dict]:
    if not DB_ENABLED:
        return None
    async with get_session() as session:
        trade = (await session.execute(
            select(RealTrade).where(RealTrade.id == trade_id)
        )).scalar_one_or_none()
        return _to_dict(trade) if trade else None


async def summary(days: int = 30) -> dict:
    """
    Mesmo shape que paper_trade_service.summary() — dashboard #10 já pluga.
    Inclui equity curve + breakdown (por source, já que real não tem tier nativo —
    mas se tiver recommendation_id, copia o tier da rec).
    """
    if not DB_ENABLED:
        return {"enabled": False, "mode": "real", "trades_total": 0}

    since = datetime.now(timezone.utc) - timedelta(days=days)
    async with get_session() as session:
        stmt = (
            select(RealTrade, RecommendationSnapshot.tier)
            .outerjoin(RecommendationSnapshot, RealTrade.recommendation_id == RecommendationSnapshot.id)
            .where(RealTrade.opened_at >= since)
            .where(RealTrade.status != "open")
            .order_by(RealTrade.closed_at.asc())
        )
        rows = (await session.execute(stmt)).all()

    if not rows:
        async with get_session() as session:
            open_count = int((await session.execute(
                select(func.count(RealTrade.id)).where(RealTrade.status == "open")
            )).scalar() or 0)
        return {
            "enabled": True,
            "mode": "real",
            "days": days,
            "equity": {
                "curve": [], "trades_total": 0, "final_pnl_pct": 0.0,
                "final_pnl_usd": 0.0, "open_positions": open_count,
            },
            "tier_stats": {},
        }

    # Equity curve diária
    daily_pnl: dict[str, float] = defaultdict(float)
    daily_count: dict[str, int] = defaultdict(int)
    by_tier: dict[str, list] = defaultdict(list)
    total_pnl_usd = 0.0
    by_tier_pnl_usd: dict[str, float] = defaultdict(float)
    for trade, tier in rows:
        if trade.closed_at is None or trade.pnl_pct is None:
            continue
        day = trade.closed_at.strftime("%Y-%m-%d")
        # No paper, contribution = realized_r * risk_pct. No real, já temos pnl_pct
        # absoluto da posição — pra ficar comparável usamos pnl_usd / equity_estimado
        # (sem equity tracking ainda) então: contribuição = realized_r * risk_pct_planejado
        # se disponível, senão pnl_pct dividido por leverage (aproximação rude)
        contrib = 0.0
        if trade.realized_r is not None:
            # Assume risk_pct=1% (mesma convenção do paper) — futuro: ler de config
            risk_pct = 1.0
            contrib = float(trade.realized_r) * risk_pct
        else:
            contrib = float(trade.pnl_pct or 0) / max(trade.leverage or 1, 1)
        daily_pnl[day] += contrib
        daily_count[day] += 1

        # Soma USD reais (do campo pnl_usd, não da contribuição percentual)
        pnl_usd_val = float(trade.pnl_usd or 0)
        total_pnl_usd += pnl_usd_val

        tier_key = tier or "?"
        by_tier[tier_key].append({
            "realized_r": float(trade.realized_r) if trade.realized_r is not None else 0.0,
            "risk_pct": 1.0,
            "status": trade.status,
            "pnl_usd": pnl_usd_val,
        })
        by_tier_pnl_usd[tier_key] += pnl_usd_val

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

    # Tier breakdown (mesmo formato do paper)
    tier_summary: dict[str, dict] = {}
    for tier_key in list(by_tier.keys()) + ["A+", "A", "B"]:
        if tier_key in tier_summary:
            continue
        items = by_tier.get(tier_key, [])
        n = len(items)
        if n == 0:
            tier_summary[tier_key] = {
                "n": 0, "wins": 0, "losses": 0, "wr_pct": None,
                "avg_r": None, "expectancy_r": None,
                "max_consec_losses": 0, "pnl_pct": 0.0,
            }
            continue
        wins = sum(1 for x in items if x["realized_r"] > 0)
        losses = n - wins
        wr = wins / n
        avg_r = sum(x["realized_r"] for x in items) / n
        max_consec, cur = 0, 0
        for x in items:
            if x["realized_r"] <= 0:
                cur += 1
                max_consec = max(max_consec, cur)
            else:
                cur = 0
        pnl = sum(x["realized_r"] * x["risk_pct"] for x in items)
        tier_summary[tier_key] = {
            "n": n, "wins": wins, "losses": losses,
            "wr_pct": round(wr * 100, 2),
            "avg_r": round(avg_r, 3),
            "expectancy_r": round(avg_r, 3),
            "max_consec_losses": max_consec,
            "pnl_pct": round(pnl, 3),
            "pnl_usd": round(by_tier_pnl_usd.get(tier_key, 0.0), 2),
        }

    total = sum(t["n"] for t in tier_summary.values())

    # Open positions snapshot (não entram nos closed stats mas aparecem na UI)
    open_count = 0
    async with get_session() as session:
        stmt = (
            select(func.count(RealTrade.id))
            .where(RealTrade.status == "open")
        )
        open_count = int((await session.execute(stmt)).scalar() or 0)

    return {
        "enabled": True,
        "mode": "real",
        "days": days,
        "equity": {
            "enabled": True,
            "mode": "real",
            "days": days,
            "curve": curve,
            "trades_total": total,
            "final_pnl_pct": round(acc, 3),
            "final_pnl_usd": round(total_pnl_usd, 2),
            "open_positions": open_count,
        },
        "tier_stats": tier_summary,
    }


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _to_dict(t: RealTrade | None) -> dict | None:
    if t is None:
        return None
    return {
        "id": t.id,
        "symbol": t.symbol,
        "side": t.side,
        "source": t.source,
        "recommendation_id": t.recommendation_id,
        "exchange": t.exchange,
        "exchange_order_id": t.exchange_order_id,
        "client_order_id": t.client_order_id,
        "qty": t.qty,
        "leverage": t.leverage,
        "notional_usd": t.notional_usd,
        "entry_price": t.entry_price,
        "entry_fee": t.entry_fee,
        "opened_at": t.opened_at.isoformat() if t.opened_at else None,
        "planned_stop": t.planned_stop,
        "planned_tp1": t.planned_tp1,
        "planned_tp2": t.planned_tp2,
        "exit_price": t.exit_price,
        "exit_fee": t.exit_fee,
        "closed_at": t.closed_at.isoformat() if t.closed_at else None,
        "status": t.status,
        "realized_r": t.realized_r,
        "pnl_usd": t.pnl_usd,
        "pnl_pct": t.pnl_pct,
        "entry_slippage_pct": t.entry_slippage_pct,
        "notes": t.notes,
    }
