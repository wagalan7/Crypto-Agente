"""
Shadow Trade Service (#11.3) — execução "sombra" de ordens em paralelo às recs.

Quando uma rec nova é emitida (A+/A), em vez de só salvar o snapshot e esperar
o paper-trade resolver via candles, o sistema também ABRE uma RealTrade com
`source="shadow"` representando a ordem que TERIA sido enviada à exchange.

Por que "shadow":
  - Não chama `place_order` na exchange (não depende de saldo/conexão real)
  - Mas calcula qty real (risk_pct × equity_virtual / risk_distance) e grava
    todos os níveis — assim, quando você flipar `EXCHANGE_SHADOW=false`, o
    mesmo código vira execução de verdade sem refactor
  - O dashboard #10 já enxerga essas trades (mesmo shape em /api/real-trades/summary)
  - Slippage vs paper fica em zero (shadow usa entry teórico da rec) — futuro
    podemos injetar mid-price real pra simular fill

Fluxo:
  1. main.py chama `open_shadow_for_recs(recs)` depois de `save_recommendations`
  2. Pra cada rec com `_just_saved=True`, abre RealTrade(source="shadow")
  3. snapshot_service.check_open_snapshots chama `close_shadow_for_snapshot(snap)`
     quando o snapshot resolve (won_tp1/tp2/be/lost/expired)
  4. Trade fecha com mesmo R do paper — slippage zero por design

Toggle:
  EXCHANGE_SHADOW=true  (default) → modo shadow ativo, sem chamada real
  EXCHANGE_SHADOW=false           → executa de verdade via exchange_service
  EXCHANGE_SHADOW_EQUITY_USD=10000 (default) → equity virtual pra dimensionar qty

Quando ativar execução real (futuro #11.4):
  - Setar EXCHANGE_SHADOW=false
  - exchange_service.place_order() será chamado com mesmos params
  - source vira "auto" ao invés de "shadow"
  - exchange_order_id preenchido com id retornado pela corretora
  - tracker passa a monitorar order_history pra status
"""
from __future__ import annotations
import os
import logging
from typing import Optional

from db import DB_ENABLED
from services import real_trade_service

log = logging.getLogger(__name__)

SHADOW_ENABLED = os.getenv("EXCHANGE_SHADOW", "true").strip().lower() in ("1", "true", "yes")
# Fallback estático — usado APENAS se a exchange estiver fora do ar.
# Em condições normais, exchange_service.get_equity() lê o saldo real.
VIRTUAL_EQUITY_USD = float(os.getenv("EXCHANGE_SHADOW_EQUITY_USD", "5000"))

# Guard de notional mínimo (Binance Futures: $50). Se o sizing por risco
# ficar abaixo do mínimo, inflamos o qty pra atingir — desde que isso não
# leve o risco real além de MAX_RISK_PCT_HARD. Caso contrário, pula a trade.
MIN_NOTIONAL_USD = float(os.getenv("EXCHANGE_MIN_NOTIONAL_USD", "50"))
MAX_RISK_PCT_HARD = float(os.getenv("EXCHANGE_MAX_RISK_PCT", "2.0"))


async def _resolve_equity_usd() -> tuple[float, str]:
    """
    Tenta ler equity ao vivo da exchange. Em caso de falha, usa fallback estático.
    Retorna (equity_usd, source) onde source ∈ {"live","cache","fallback"}.
    """
    try:
        from services import exchange_service
        eq = await exchange_service.get_equity()
        if eq.get("ok") and eq.get("total_usd", 0) > 0:
            return float(eq["total_usd"]), eq.get("source", "live")
    except Exception as e:
        log.warning(f"[shadow] get_equity falhou: {e}")
    return VIRTUAL_EQUITY_USD, "fallback"


def env_info() -> dict:
    """Diagnóstico — quanto o shadow está ativo + equity virtual usado pra sizing."""
    return {
        "shadow_enabled": SHADOW_ENABLED,
        "fallback_equity_usd": VIRTUAL_EQUITY_USD,
        "sizing_mode": "live (com fallback estático em erro)",
        "min_notional_usd": MIN_NOTIONAL_USD,
        "max_risk_pct_hard": MAX_RISK_PCT_HARD,
        "exchange_active": os.getenv("EXCHANGE", "binance"),
        "note": "Sizing: risk_pct nominal; eleva ao notional mínimo se < $50; pula se risco real > 2%.",
    }


def _compute_qty(
    entry: float, stop: float, risk_pct: float, equity_usd: float
) -> Optional[dict]:
    """
    Dimensiona a posição com guard de notional mínimo + cap de risco máximo.

    Fluxo:
      1. qty_nominal = (equity × risk_pct/100) / |entry−stop|
      2. notional_nominal = qty_nominal × entry
      3. Se notional_nominal >= MIN_NOTIONAL_USD → usa nominal (status="ok")
      4. Senão, qty_inflated = MIN_NOTIONAL_USD / entry
         - Calcula risco real = qty_inflated × |entry−stop| / equity × 100
         - Se risco_real <= MAX_RISK_PCT_HARD → usa inflated (status="inflated")
         - Senão → status="skip" (rec descartada)

    Retorna dict com {qty, status, notional, risk_pct_real, reason} ou None
    se rec é inválida (risk_dist=0).
    """
    risk_dist = abs(entry - stop)
    if risk_dist <= 0:
        return None

    risk_usd_target = equity_usd * (risk_pct / 100.0)
    qty_nominal = risk_usd_target / risk_dist
    notional_nominal = qty_nominal * entry

    if notional_nominal >= MIN_NOTIONAL_USD:
        return {
            "qty": round(qty_nominal, 6),
            "status": "ok",
            "notional_usd": round(notional_nominal, 2),
            "risk_pct_real": round(risk_pct, 3),
            "reason": "nominal sizing",
        }

    # Inflar pro mínimo
    qty_inflated = MIN_NOTIONAL_USD / entry
    risk_inflated_usd = qty_inflated * risk_dist
    risk_pct_inflated = (risk_inflated_usd / equity_usd) * 100.0

    if risk_pct_inflated <= MAX_RISK_PCT_HARD:
        return {
            "qty": round(qty_inflated, 6),
            "status": "inflated",
            "notional_usd": round(qty_inflated * entry, 2),
            "risk_pct_real": round(risk_pct_inflated, 3),
            "reason": f"inflated to min notional ${MIN_NOTIONAL_USD:.0f}; risk {risk_pct:.2f}% → {risk_pct_inflated:.2f}%",
        }

    return {
        "qty": round(qty_inflated, 6),
        "status": "skip",
        "notional_usd": round(qty_inflated * entry, 2),
        "risk_pct_real": round(risk_pct_inflated, 3),
        "reason": f"would inflate risk to {risk_pct_inflated:.2f}% > cap {MAX_RISK_PCT_HARD:.2f}%",
    }


async def open_shadow_for_recs(recs: list[dict]) -> int:
    """
    Pra cada rec marcada com `_just_saved=True` e tier A/A+, abre uma RealTrade.

    Modos:
      SHADOW_ENABLED=True  → source="shadow" (sem chamar exchange)
      SHADOW_ENABLED=False → source="auto" + chama exchange_service.place_order()
                              (passa pelo kill_switch_service.check_can_trade primeiro)

    Idempotente: snapshot_service.save_recommendations dedupa antes.
    """
    if not DB_ENABLED or not recs:
        return 0
    mode = "shadow" if SHADOW_ENABLED else "live"
    log.debug(f"[shadow] processando {len(recs)} recs em modo={mode}")

    opened = 0
    for rec in recs:
        try:
            if not rec.get("_just_saved"):
                continue
            tier = rec.get("tier")
            if tier not in ("A+", "A"):
                continue

            entry = float(rec.get("entry") or 0)
            stop = float(rec.get("stop_loss") or 0)
            risk_pct = float(rec.get("risk_pct") or 1.0)
            equity_usd, equity_src = await _resolve_equity_usd()
            sizing = _compute_qty(entry, stop, risk_pct, equity_usd)
            if sizing is None:
                log.warning(f"[shadow] {rec.get('symbol')} risk_dist=0 — pulando")
                continue
            log.info(
                f"[shadow] sizing {rec.get('symbol')}: equity=${equity_usd:.2f} "
                f"({equity_src}) → qty={sizing['qty']} notional=${sizing['notional_usd']} "
                f"risk_real={sizing['risk_pct_real']}% status={sizing['status']} ({sizing['reason']})"
            )
            if sizing["status"] == "skip":
                log.warning(
                    f"[shadow] {rec.get('symbol')} SKIP: {sizing['reason']} "
                    f"(would-be notional=${sizing['notional_usd']})"
                )
                continue
            qty = sizing["qty"]

            # Snapshot_id é setado em save_recommendations? Não — o `_just_saved`
            # flag é booleano. Precisamos do id do snapshot recém-criado pra
            # linkar. Resolvemos olhando o registro: filtra por symbol+direction
            # mais recente.
            from sqlalchemy import select, desc
            from db import get_session
            from models.recommendation_snapshot import RecommendationSnapshot

            async with get_session() as session:
                stmt = (
                    select(RecommendationSnapshot.id)
                    .where(RecommendationSnapshot.symbol == rec["symbol"])
                    .where(RecommendationSnapshot.direction == rec["direction"])
                    .where(RecommendationSnapshot.timeframe == rec["timeframe"])
                    .order_by(desc(RecommendationSnapshot.created_at))
                    .limit(1)
                )
                snap_id = (await session.execute(stmt)).scalar_one_or_none()

            if snap_id is None:
                log.warning(f"[shadow] snapshot_id não achado pra {rec.get('symbol')} — pulando")
                continue

            side = "long" if rec.get("direction") == "long" else "short"
            tp1 = None
            sig = rec.get("signal") or {}
            if isinstance(sig, dict):
                tp1 = sig.get("tp1")

            tp2 = float(rec.get("tp2") or 0) or None

            # ─── LIVE EXECUTION (kill-switch + exchange call) ────────────
            exchange_order_id = None
            client_order_id = None
            exchange_name = os.getenv("EXCHANGE", "binance")
            source = "shadow"
            entry_actual = entry

            if not SHADOW_ENABLED:
                # 1. Kill-switch
                from services import kill_switch_service
                ks = await kill_switch_service.check_can_trade()
                if not ks.get("allowed"):
                    log.warning(
                        f"[shadow→live] BLOCKED {rec['symbol']} {side}: {ks.get('reason')}"
                    )
                    continue

                # 2. Exchange order
                from services import exchange_service
                exch_side = "Buy" if side == "long" else "Sell"
                client_order_id = f"cw-{snap_id}"  # crypto-win + snap id
                order_res = await exchange_service.place_order(
                    symbol=rec["symbol"],
                    side=exch_side,
                    qty=qty,
                    order_type="Market",
                    stop_loss=stop,
                    take_profit=tp2,  # TP2 como target principal; TP1 fica manual
                    leverage=int(rec.get("leverage") or 1),
                    client_order_id=client_order_id,
                )
                if not order_res.get("ok"):
                    log.error(
                        f"[shadow→live] place_order falhou {rec['symbol']}: "
                        f"{order_res.get('msg') or order_res.get('error')}"
                    )
                    continue

                result = order_res.get("result") or {}
                exchange_order_id = str(result.get("orderId") or result.get("orderID") or "")
                # Binance retorna avgPrice; Bybit retorna em outro campo
                avg = result.get("avgPrice") or result.get("avgFillPrice")
                if avg:
                    try:
                        entry_actual = float(avg)
                    except Exception:
                        pass
                source = "auto"
                log.info(
                    f"[shadow→live] EXECUTED {rec['symbol']} {exch_side} qty={qty} "
                    f"order_id={exchange_order_id} avg={entry_actual}"
                )

            trade = await real_trade_service.open_trade(
                symbol=rec["symbol"],
                side=side,
                qty=qty,
                entry_price=entry_actual,
                recommendation_id=snap_id,
                leverage=int(rec.get("leverage") or 1),
                planned_stop=stop,
                planned_tp1=float(tp1) if tp1 is not None else None,
                planned_tp2=tp2,
                entry_fee=0.0,
                source=source,
                exchange=exchange_name,
                exchange_order_id=exchange_order_id,
                client_order_id=client_order_id,
                notes=f"{source} auto-open (tier {tier})",
            )
            if trade is not None:
                opened += 1
                log.info(
                    f"[{source}] OPEN {rec['symbol']} {side} qty={qty} entry={entry_actual} "
                    f"SL={stop} TP1={tp1} TP2={tp2} (snap={snap_id})"
                )
                # Push só pra execução real (auto). Shadow fica silencioso pra
                # não floodar enquanto o sistema simula em paralelo.
                if source == "auto":
                    try:
                        from services import push_service
                        await push_service.notify_trade_open({
                            **trade,
                            "planned_stop": stop,
                            "planned_tp1": float(tp1) if tp1 is not None else None,
                            "planned_tp2": tp2,
                        })
                    except Exception as e:
                        log.warning(f"[shadow] push trade-open falhou: {e}")
        except Exception as e:
            log.warning(f"[shadow] falha abrindo trade pra {rec.get('symbol')}: {e}")

    if opened:
        log.info(f"[shadow] trades abertos: {opened}")
    return opened


# Mapeia status interno do snapshot → status do RealTrade
_STATUS_MAP = {
    "won_tp2": "closed_tp2",
    "won_tp1": "closed_tp1",
    "won_tp1_be": "closed_be",
    "lost": "closed_stop",
    "expired": "closed_manual",  # sem hit, fecha "neutro"
}


async def close_shadow_for_snapshot(snap) -> bool:
    """
    Chamado por snapshot_service.check_open_snapshots quando um snap resolve.
    Procura o RealTrade shadow ligado e fecha com o mesmo outcome.

    Retorna True se fechou algo, False senão (não existia trade shadow).
    """
    if not DB_ENABLED or snap is None:
        return False
    if snap.status not in _STATUS_MAP:
        return False
    if snap.outcome_price is None:
        return False

    from sqlalchemy import select
    from db import get_session
    from models.real_trade import RealTrade

    async with get_session() as session:
        stmt = (
            select(RealTrade)
            .where(RealTrade.recommendation_id == snap.id)
            .where(RealTrade.source.in_(("shadow", "auto")))
            .where(RealTrade.status == "open")
        )
        trade = (await session.execute(stmt)).scalar_one_or_none()
        if trade is None:
            return False

    new_status = _STATUS_MAP[snap.status]
    # Se foi execução real (auto) com TP/SL já emitidos como ordens separadas,
    # o exchange resolveu sozinho — só atualizamos o DB pra refletir.
    # Se snap.status=expired (não bateu nada), pode ser que a posição esteja
    # aberta na exchange ainda; pra esse caso emitimos market close.
    if trade.source == "auto" and snap.status == "expired":
        try:
            from services import exchange_service
            close_side = "Sell" if trade.side == "long" else "Buy"
            close_res = await exchange_service.place_order(
                symbol=trade.symbol,
                side=close_side,
                qty=float(trade.qty),
                order_type="Market",
                reduce_only=True,
                client_order_id=f"cw-close-{trade.id}",
            )
            if not close_res.get("ok"):
                log.warning(
                    f"[live] close_position falhou trade#{trade.id}: "
                    f"{close_res.get('msg') or close_res.get('error')}"
                )
        except Exception as e:
            log.warning(f"[live] erro fechando posição #{trade.id}: {e}")

    await real_trade_service.close_trade(
        trade_id=trade.id,
        exit_price=float(snap.outcome_price),
        status=new_status,
        exit_fee=0.0,
        notes=f"{trade.source} auto-close from snap #{snap.id} ({snap.status})",
    )
    log.info(
        f"[shadow] CLOSE trade#{trade.id} {snap.symbol} → {new_status} "
        f"@ {snap.outcome_price} (snap_status={snap.status})"
    )
    return True
