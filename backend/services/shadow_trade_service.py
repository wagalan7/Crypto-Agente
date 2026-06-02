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
VIRTUAL_EQUITY_USD = float(os.getenv("EXCHANGE_SHADOW_EQUITY_USD", "10000"))


def env_info() -> dict:
    """Diagnóstico — quanto o shadow está ativo + equity virtual usado pra sizing."""
    return {
        "shadow_enabled": SHADOW_ENABLED,
        "virtual_equity_usd": VIRTUAL_EQUITY_USD,
        "exchange_active": os.getenv("EXCHANGE", "binance"),
        "note": "shadow=true → registra trades sem chamar exchange. false → executa real.",
    }


def _compute_qty(entry: float, stop: float, risk_pct: float, equity_usd: float) -> Optional[float]:
    """
    Dimensiona a posição pelo método de risco fixo:
        risk_usd = equity × risk_pct/100
        qty = risk_usd / |entry - stop|
    Retorna None se a distância for zero (rec inválida).
    """
    risk_dist = abs(entry - stop)
    if risk_dist <= 0:
        return None
    risk_usd = equity_usd * (risk_pct / 100.0)
    qty = risk_usd / risk_dist
    return round(qty, 6)


async def open_shadow_for_recs(recs: list[dict]) -> int:
    """
    Pra cada rec marcada com `_just_saved=True` e tier A/A+, abre uma RealTrade
    sombra. Retorna quantas foram criadas.

    Idempotente: se já existe RealTrade com mesma recommendation_id e
    status='open', pula (snapshot_service.save_recommendations já dedupa, mas
    paranoia extra aqui).
    """
    if not SHADOW_ENABLED:
        log.debug("[shadow] desabilitado (EXCHANGE_SHADOW=false) — pulando")
        return 0
    if not DB_ENABLED or not recs:
        return 0

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
            qty = _compute_qty(entry, stop, risk_pct, VIRTUAL_EQUITY_USD)
            if qty is None:
                log.warning(f"[shadow] {rec.get('symbol')} risk_dist=0 — pulando")
                continue

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

            trade = await real_trade_service.open_trade(
                symbol=rec["symbol"],
                side=side,
                qty=qty,
                entry_price=entry,
                recommendation_id=snap_id,
                leverage=int(rec.get("leverage") or 1),
                planned_stop=stop,
                planned_tp1=float(tp1) if tp1 is not None else None,
                planned_tp2=float(rec.get("tp2") or 0),
                entry_fee=0.0,
                source="shadow",
                exchange=os.getenv("EXCHANGE", "binance"),
                notes=f"shadow auto-open (tier {tier})",
            )
            if trade is not None:
                opened += 1
                log.info(
                    f"[shadow] OPEN {rec['symbol']} {side} qty={qty} entry={entry} "
                    f"SL={stop} TP1={tp1} TP2={rec.get('tp2')} (snap={snap_id})"
                )
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
            .where(RealTrade.source == "shadow")
            .where(RealTrade.status == "open")
        )
        trade = (await session.execute(stmt)).scalar_one_or_none()
        if trade is None:
            return False

    new_status = _STATUS_MAP[snap.status]
    await real_trade_service.close_trade(
        trade_id=trade.id,
        exit_price=float(snap.outcome_price),
        status=new_status,
        exit_fee=0.0,
        notes=f"shadow auto-close from snap #{snap.id} ({snap.status})",
    )
    log.info(
        f"[shadow] CLOSE trade#{trade.id} {snap.symbol} → {new_status} "
        f"@ {snap.outcome_price} (snap_status={snap.status})"
    )
    return True
