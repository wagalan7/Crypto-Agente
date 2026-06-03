"""
Trade Manager — gerenciamento ativo de trades em aberto (Fase 2).

Loop async que poll-eia posições na exchange e gerencia transições de fase:

  Fase "pre_tp1": SL inicial em planned_stop, TP1 parcial 45% pendente, TP2 100%
  Fase "post_tp1": TP1 bateu (parcial executada) → SL movido pra entry (breakeven)

Detecção de TP1: comparar qty atual na exchange com qty_initial.
  Se qty_atual < qty_initial * 0.6 → parcial foi executada → transição pra post_tp1.

Ao transicionar:
  1. Cancela SL antigo (sl_order_id)
  2. Cria novo STOP_MARKET em entry_price com closePosition=true
  3. Atualiza RealTrade: phase='post_tp1', sl_current_price=entry, sl_order_id=novo

Detecção de fechamento (qty=0 na exchange):
  Marca trade como closed_tp2 ou closed_stop baseado em preço vs níveis.

Env:
  TRADE_MANAGER_ENABLED         — "true" (default) liga o loop
  TRADE_MANAGER_POLL_SECONDS    — default 15s
"""
from __future__ import annotations
import asyncio
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import select

from db import DB_ENABLED, get_session
from models.real_trade import RealTrade

log = logging.getLogger(__name__)

ENABLED = os.getenv("TRADE_MANAGER_ENABLED", "true").strip().lower() in ("1", "true", "yes")
POLL_SECONDS = int(os.getenv("TRADE_MANAGER_POLL_SECONDS", "15"))

# Fator de "parcial detectada" — se qty atual ≤ qty_initial * FATOR, considera TP1 hit.
# 0.6 dá folga pra arredondamentos (parcial planejada é 45%, resta 55% ≈ 0.55).
_TP1_DETECTED_AT = 0.60


async def _fetch_exchange_qty(symbol: str) -> float | None:
    """Busca a qty atual da posição na exchange. Retorna None se erro, 0 se fechada."""
    try:
        from services import exchange_service
        res = await exchange_service.get_positions(symbol=symbol)
        if not res.get("ok"):
            return None
        for p in res.get("positions") or []:
            # exchange_service retorna sym normalizado; comparar suficiente
            return float(p.get("size") or 0)
        return 0.0  # sem posição = qty 0
    except Exception as e:
        log.warning(f"[trade-manager] fetch qty {symbol} falhou: {e}")
        return None


async def _transition_to_post_tp1(trade: RealTrade) -> bool:
    """
    TP1 detectado: cancela SL antigo, cria novo SL em entry (breakeven).
    Retorna True se transitou com sucesso.
    """
    from services import exchange_service, binance_signed_service
    sym = trade.symbol
    entry = trade.entry_price

    # 1. Cancela SL antigo (algo order — endpoint diferente)
    if trade.sl_order_id:
        try:
            cancel_res = await exchange_service.cancel_algo_order(trade.sl_order_id)
            if not cancel_res.get("ok"):
                log.warning(
                    f"[trade-manager] cancel SL antigo {sym} algoId={trade.sl_order_id} falhou: "
                    f"{cancel_res.get('msg') or cancel_res.get('error')} (pode já ter sido executado)"
                )
        except Exception as e:
            log.warning(f"[trade-manager] cancel SL {sym} erro: {e}")

    # 2. Cria novo SL em entry (BE). Reusa place_protection_orders com só SL.
    entry_side = "Buy" if trade.side == "long" else "Sell"
    try:
        prot = await binance_signed_service.place_protection_orders(
            sym, entry_side, qty=trade.qty,  # qty restante (pós-parcial)
            stop_loss=entry,
            tp1=None, tp2=None,  # não recria TPs — TP2 antigo ainda está ativo
            client_order_id_prefix=f"cw-be-{trade.id}",
        )
    except Exception as e:
        log.error(f"[trade-manager] criar novo SL@BE {sym} erro: {e}")
        return False

    if not prot.get("sl_ok"):
        log.error(
            f"[trade-manager] CRITICAL: novo SL@BE {sym} falhou: {prot.get('sl_msg')} "
            f"— trade #{trade.id} sem SL ativo!"
        )
        return False

    # 3. Atualiza DB
    async with get_session() as session:
        fresh = (await session.execute(
            select(RealTrade).where(RealTrade.id == trade.id)
        )).scalar_one_or_none()
        if fresh is None:
            return False
        fresh.phase = "post_tp1"
        fresh.sl_order_id = prot.get("sl_order_id")
        fresh.sl_current_price = entry
        fresh.updated_at = datetime.now(timezone.utc)
        await session.commit()

    log.info(
        f"[trade-manager] {sym} #{trade.id} → post_tp1: SL movido pra BE {entry} "
        f"(novo id={prot.get('sl_order_id')})"
    )
    return True


async def _close_trade(trade: RealTrade, reason: str) -> None:
    """Detectou qty=0 na exchange → marca trade fechado. Usa mark price atual como exit."""
    from services import real_trade_service, exchange_service
    exit_price = None
    try:
        # Tenta pegar último preço pra estimar exit
        from services import binance_signed_service
        # /fapi/v1/ticker/price é público, mas usar mark price das posições é caro;
        # fallback: usa planned_tp2 ou planned_stop dependendo do reason.
        if reason == "tp2":
            exit_price = trade.planned_tp2 or trade.entry_price
        elif reason == "stop":
            exit_price = trade.sl_current_price or trade.planned_stop or trade.entry_price
        else:
            exit_price = trade.entry_price
    except Exception:
        exit_price = trade.entry_price

    status_map = {
        "tp2": "closed_tp2",
        "stop": "closed_stop",
        "be": "closed_be",
    }
    status = status_map.get(reason, "closed_manual")

    try:
        await real_trade_service.close_trade(
            trade.id,
            exit_price=exit_price or trade.entry_price,
            status=status,
            notes=f"auto-closed by trade_manager ({reason})",
        )
        log.info(f"[trade-manager] {trade.symbol} #{trade.id} CLOSED {status} @ {exit_price}")
    except Exception as e:
        log.error(f"[trade-manager] close_trade {trade.id} erro: {e}")


async def _process_trade(trade: RealTrade) -> None:
    """Avalia um trade aberto e age conforme a fase."""
    qty_now = await _fetch_exchange_qty(trade.symbol)
    if qty_now is None:
        return  # erro de leitura — pula esse ciclo

    qty_initial = trade.qty_initial or trade.qty

    # ── Posição fechou totalmente ────────────────────────────────────────
    if qty_now <= 0:
        # Heurística: se já passou pelo post_tp1, provavelmente bateu TP2 ou BE.
        # Se ainda em pre_tp1, foi stop direto.
        if trade.phase == "post_tp1":
            await _close_trade(trade, "tp2")
        else:
            await _close_trade(trade, "stop")
        return

    # ── Transição pre_tp1 → post_tp1 (parcial detectada) ─────────────────
    if trade.phase == "pre_tp1" and qty_now <= qty_initial * _TP1_DETECTED_AT:
        log.info(
            f"[trade-manager] {trade.symbol} #{trade.id} TP1 detectado: "
            f"qty {qty_initial} → {qty_now} (≤ {qty_initial * _TP1_DETECTED_AT:.4f})"
        )
        # Atualiza qty atual no DB
        async with get_session() as session:
            fresh = (await session.execute(
                select(RealTrade).where(RealTrade.id == trade.id)
            )).scalar_one_or_none()
            if fresh:
                fresh.qty = qty_now
                await session.commit()
                trade.qty = qty_now
        await _transition_to_post_tp1(trade)


async def _tick() -> None:
    """Uma iteração do loop: processa todos os trades open auto."""
    if not DB_ENABLED:
        return
    async with get_session() as session:
        stmt = (
            select(RealTrade)
            .where(RealTrade.status == "open")
            .where(RealTrade.source == "auto")
        )
        trades = (await session.execute(stmt)).scalars().all()

    if not trades:
        return

    for t in trades:
        try:
            await _process_trade(t)
        except Exception as e:
            log.warning(f"[trade-manager] processar #{t.id} {t.symbol} erro: {e}", exc_info=True)


async def loop() -> None:
    """Loop principal — chamar via asyncio.create_task no startup."""
    if not ENABLED:
        log.info("[trade-manager] DISABLED via env")
        return
    log.info(f"[trade-manager] iniciado (poll={POLL_SECONDS}s)")
    while True:
        try:
            await _tick()
        except asyncio.CancelledError:
            log.info("[trade-manager] cancelled")
            break
        except Exception as e:
            log.warning(f"[trade-manager] tick erro: {e}", exc_info=True)
        try:
            await asyncio.sleep(POLL_SECONDS)
        except asyncio.CancelledError:
            break


# ── Backfill de proteção pra trades já abertos sem SL/TP ─────────────────
async def backfill_protection() -> dict:
    """
    Itera trades RealTrade com status='open' que não têm sl_order_id e cria
    SL + TP1 + TP2 na exchange. Útil pra "consertar" posições que foram abertas
    antes desse sistema existir (ou cujas ordens condicionais falharam).
    """
    if not DB_ENABLED:
        return {"ok": False, "error": "DB disabled"}

    from services import binance_signed_service

    results = []
    async with get_session() as session:
        stmt = (
            select(RealTrade)
            .where(RealTrade.status == "open")
            .where(RealTrade.source == "auto")
        )
        trades = (await session.execute(stmt)).scalars().all()

    for t in trades:
        # Já tem SL ativo? pula
        if t.sl_order_id:
            results.append({"trade_id": t.id, "symbol": t.symbol, "skipped": True, "reason": "já tem sl_order_id"})
            continue
        if not t.planned_stop:
            results.append({"trade_id": t.id, "symbol": t.symbol, "skipped": True, "reason": "sem planned_stop"})
            continue

        # Confirma qty real na exchange
        qty_now = await _fetch_exchange_qty(t.symbol)
        if qty_now is None or qty_now <= 0:
            results.append({"trade_id": t.id, "symbol": t.symbol, "skipped": True, "reason": f"qty na exchange = {qty_now}"})
            continue

        entry_side = "Buy" if t.side == "long" else "Sell"
        try:
            prot = await binance_signed_service.place_protection_orders(
                t.symbol, entry_side, qty=qty_now,
                stop_loss=t.planned_stop,
                tp1=t.planned_tp1,
                tp2=t.planned_tp2,
                client_order_id_prefix=f"cw-bf-{t.id}",
            )
        except Exception as e:
            results.append({"trade_id": t.id, "symbol": t.symbol, "error": str(e)})
            continue

        # Salva os IDs
        async with get_session() as session:
            fresh = (await session.execute(
                select(RealTrade).where(RealTrade.id == t.id)
            )).scalar_one_or_none()
            if fresh:
                fresh.sl_order_id = prot.get("sl_order_id")
                fresh.tp1_order_id = prot.get("tp1_order_id")
                fresh.tp2_order_id = prot.get("tp2_order_id")
                fresh.sl_current_price = t.planned_stop
                if fresh.qty_initial is None:
                    fresh.qty_initial = qty_now
                fresh.updated_at = datetime.now(timezone.utc)
                await session.commit()

        results.append({
            "trade_id": t.id,
            "symbol": t.symbol,
            "side": t.side,
            "qty": qty_now,
            "planned_stop": t.planned_stop,
            "planned_tp1": t.planned_tp1,
            "planned_tp2": t.planned_tp2,
            "sl_ok": prot.get("sl_ok"),
            "sl_order_id": prot.get("sl_order_id"),
            "sl_msg": prot.get("sl_msg"),
            "tp1_ok": prot.get("tp1_ok"),
            "tp1_order_id": prot.get("tp1_order_id"),
            "tp1_msg": prot.get("tp1_msg"),
            "tp1_skipped": prot.get("tp1_skipped"),
            "tp1_qty": prot.get("tp1_qty"),
            "tp2_ok": prot.get("tp2_ok"),
            "tp2_order_id": prot.get("tp2_order_id"),
            "tp2_msg": prot.get("tp2_msg"),
        })
        log.info(
            f"[trade-manager] backfill #{t.id} {t.symbol}: "
            f"SL={prot.get('sl_order_id')} TP1={prot.get('tp1_order_id')} TP2={prot.get('tp2_order_id')}"
        )

    return {"ok": True, "processed": len(results), "results": results}


async def get_status() -> dict:
    """Snapshot pra debug — trades ativos e fase de cada um."""
    if not DB_ENABLED:
        return {"enabled": ENABLED, "trades": []}
    async with get_session() as session:
        stmt = (
            select(RealTrade)
            .where(RealTrade.status == "open")
            .where(RealTrade.source == "auto")
        )
        trades = (await session.execute(stmt)).scalars().all()
    return {
        "enabled": ENABLED,
        "poll_seconds": POLL_SECONDS,
        "trades": [
            {
                "id": t.id,
                "symbol": t.symbol,
                "side": t.side,
                "phase": t.phase,
                "qty": t.qty,
                "qty_initial": t.qty_initial,
                "entry_price": t.entry_price,
                "sl_current_price": t.sl_current_price,
                "planned_tp1": t.planned_tp1,
                "planned_tp2": t.planned_tp2,
                "sl_order_id": t.sl_order_id,
                "tp1_order_id": t.tp1_order_id,
                "tp2_order_id": t.tp2_order_id,
                "opened_at": t.opened_at.isoformat() if t.opened_at else None,
            }
            for t in trades
        ],
    }
