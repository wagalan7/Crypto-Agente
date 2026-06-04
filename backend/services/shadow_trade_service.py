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

# Cap de margem por trade (% banca). Quando SL é apertado, sizing por risco
# fixo (1%) infla notional. Esse cap limita: margin_used = notional/leverage
# nunca passa de MAX_MARGIN_PCT × equity. Risco real cai abaixo do alvo, mas
# a banca não fica refém de SL apertado.
MAX_MARGIN_PCT_PER_TRADE = float(os.getenv("EXCHANGE_MAX_MARGIN_PCT", "15"))

# Cap de exposição agregada (notional somado / equity × 100). Bloqueia abrir
# nova posição se notional_total + nova_trade > esse limite. 150% = 1.5×
# banca em exposição total (com 10x lev = 15% margem agregada).
MAX_TOTAL_NOTIONAL_PCT = float(os.getenv("EXCHANGE_MAX_TOTAL_NOTIONAL_PCT", "150"))

# ── Direction flip (Fase 2) ────────────────────────────────────────────────
# Quando aparece rec na direção OPOSTA a um trade aberto, avalia se a reversão
# é forte o bastante pra justificar fechar a atual e abrir contra. Por padrão
# bloqueia (advisory mode) — só flipa se gate de qualidade + risco passa.
FLIP_ENABLED = os.getenv("FLIP_ENABLED", "true").strip().lower() in ("1", "true", "yes")
FLIP_MIN_SCORE_DELTA = float(os.getenv("FLIP_MIN_SCORE_DELTA", "10"))
FLIP_MIN_TIER_UPGRADE = int(os.getenv("FLIP_MIN_TIER_UPGRADE", "1"))  # nível de upgrade exigido
FLIP_MAX_CURRENT_R = float(os.getenv("FLIP_MAX_CURRENT_R", "0.3"))    # se trade atual > 0.3R, não flipa
FLIP_COOLDOWN_HOURS = float(os.getenv("FLIP_COOLDOWN_HOURS", "4"))     # min horas entre flips no mesmo símbolo

_TIER_RANK = {"B": 1, "A": 2, "A+": 3}


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
        "max_margin_pct_per_trade": MAX_MARGIN_PCT_PER_TRADE,
        "max_total_notional_pct": MAX_TOTAL_NOTIONAL_PCT,
        "exchange_active": os.getenv("EXCHANGE", "binance"),
        "note": "Sizing: risk_pct nominal; eleva ao mín notional; capa em margin%/trade e total notional%.",
    }


async def _open_notional_usd() -> float:
    """Soma notional (entry × qty) dos trades reais auto abertos. Pra cap agregado."""
    if not DB_ENABLED:
        return 0.0
    try:
        from sqlalchemy import select
        from db import get_session
        from models.real_trade import RealTrade
        async with get_session() as session:
            stmt = select(RealTrade).where(
                RealTrade.status == "open",
                RealTrade.source == "auto",
            )
            rows = (await session.execute(stmt)).scalars().all()
            total = 0.0
            for t in rows:
                ep = float(t.entry_price or 0)
                q = float(t.qty or 0)
                total += ep * q
            return total
    except Exception as e:
        log.warning(f"[shadow] _open_notional_usd falhou: {e}")
        return 0.0


# ── Direction flip helpers ──────────────────────────────────────────────────


async def _find_opposite_open_trade(symbol: str, new_direction: str):
    """Procura RealTrade auto OPEN no símbolo, direção oposta. Retorna o objeto
    ou None. Usado pra detectar se há candidato a flip."""
    if not DB_ENABLED:
        return None
    try:
        from sqlalchemy import select
        from db import get_session
        from models.real_trade import RealTrade
        opposite_side = "long" if new_direction == "short" else "short"
        async with get_session() as session:
            stmt = select(RealTrade).where(
                RealTrade.symbol == symbol,
                RealTrade.status == "open",
                RealTrade.source == "auto",
                RealTrade.side == opposite_side,
            )
            return (await session.execute(stmt)).scalar_one_or_none()
    except Exception as e:
        log.warning(f"[flip] busca opposite falhou {symbol}: {e}")
        return None


async def _flip_cooldown_active(symbol: str) -> bool:
    """True se houve flip nesse símbolo há menos de FLIP_COOLDOWN_HOURS horas.
    Detecta via notes contendo 'closed_flip' nos closed_at recentes."""
    if not DB_ENABLED:
        return False
    try:
        from datetime import datetime, timezone, timedelta
        from sqlalchemy import select
        from db import get_session
        from models.real_trade import RealTrade
        cutoff = datetime.now(timezone.utc) - timedelta(hours=FLIP_COOLDOWN_HOURS)
        async with get_session() as session:
            stmt = select(RealTrade.id).where(
                RealTrade.symbol == symbol,
                RealTrade.closed_at >= cutoff,
                RealTrade.status.like("closed_flip%"),
            ).limit(1)
            return (await session.execute(stmt)).scalar_one_or_none() is not None
    except Exception as e:
        log.warning(f"[flip] cooldown check falhou {symbol}: {e}")
        return False


async def _get_mark_price(symbol: str) -> float:
    """Mark price atual do símbolo via positionRisk. 0 se falhar."""
    try:
        from services import exchange_service
        res = await exchange_service.get_positions(symbol=symbol)
        if not res.get("ok"):
            return 0.0
        for p in res.get("positions") or []:
            return float(p.get("mark_price") or 0)
    except Exception as e:
        log.warning(f"[flip] mark_price falhou {symbol}: {e}")
    return 0.0


async def _get_current_tier_score(rec_id: int) -> tuple[str, float]:
    """Tier e score da rec original que abriu o trade. ('', 0) se não achou."""
    if not DB_ENABLED or not rec_id:
        return ("", 0.0)
    try:
        from sqlalchemy import select
        from db import get_session
        from models.recommendation_snapshot import RecommendationSnapshot
        async with get_session() as session:
            stmt = select(RecommendationSnapshot.tier, RecommendationSnapshot.score).where(
                RecommendationSnapshot.id == rec_id
            )
            row = (await session.execute(stmt)).first()
            if row:
                return (row.tier or "", float(row.score or 0))
    except Exception as e:
        log.warning(f"[flip] get_current_tier_score falhou: {e}")
    return ("", 0.0)


async def _evaluate_flip_gate(current_trade, new_rec: dict) -> tuple[bool, str]:
    """
    Avalia se rec na direção oposta justifica flip automático.
    Retorna (should_flip, reason).
    """
    if not FLIP_ENABLED:
        return (False, "FLIP_ENABLED=false")

    # 1. Fase: nunca flipa pós-TP1 (lock garantido seria destruído)
    phase = getattr(current_trade, "phase", None) or "pre_tp1"
    if phase != "pre_tp1":
        return (False, f"phase={phase} (pós-TP1 nunca flipa)")

    # 2. Cooldown
    if await _flip_cooldown_active(current_trade.symbol):
        return (False, f"cooldown ativo (último flip < {FLIP_COOLDOWN_HOURS}h)")

    # 3. Qualidade — tier upgrade OU score delta
    new_tier = new_rec.get("tier") or ""
    new_score = float(new_rec.get("score") or 0)
    cur_tier, cur_score = await _get_current_tier_score(current_trade.recommendation_id)
    tier_delta = _TIER_RANK.get(new_tier, 0) - _TIER_RANK.get(cur_tier, 0)
    score_delta = new_score - cur_score
    tier_ok = tier_delta >= FLIP_MIN_TIER_UPGRADE
    score_ok = score_delta >= FLIP_MIN_SCORE_DELTA
    if not (tier_ok or score_ok):
        return (False, (
            f"qualidade insuficiente: tier {cur_tier}→{new_tier} (Δ{tier_delta}, "
            f"precisa ≥{FLIP_MIN_TIER_UPGRADE}), score {cur_score:.0f}→{new_score:.0f} "
            f"(Δ{score_delta:+.0f}, precisa ≥{FLIP_MIN_SCORE_DELTA})"
        ))

    # 4. R atual — não flipa trade já ganhando bem
    mark = await _get_mark_price(current_trade.symbol)
    entry = float(current_trade.entry_price or 0)
    planned_stop = float(current_trade.planned_stop or 0)
    if mark > 0 and entry > 0 and planned_stop > 0:
        sign = 1 if current_trade.side == "long" else -1
        risk_dist = abs(entry - planned_stop)
        if risk_dist > 0:
            r_now = ((mark - entry) * sign) / risk_dist
            if r_now > FLIP_MAX_CURRENT_R:
                return (False, f"trade atual ganhando {r_now:+.2f}R > {FLIP_MAX_CURRENT_R}R (deixa fluir)")

    return (True, f"approved: tier {cur_tier}→{new_tier} (Δ{tier_delta}), score Δ{score_delta:+.0f}")


async def _execute_flip(current_trade) -> bool:
    """
    Fecha trade atual via market (reduceOnly), cancela ordens condicionais,
    marca como closed_flip no DB. Retorna True se conseguiu.
    """
    from services import exchange_service, real_trade_service
    symbol = current_trade.symbol
    try:
        # 1. Cancela algo orders pendentes (SL/TP1/TP2)
        for oid_field in ("sl_order_id", "tp1_order_id", "tp2_order_id"):
            oid = getattr(current_trade, oid_field, None)
            if oid:
                try:
                    await exchange_service.cancel_algo_order(str(oid))
                except Exception as e:
                    log.warning(f"[flip] cancel {oid_field}={oid} falhou: {e}")

        # 2. Market close (reduceOnly)
        close_side = "Sell" if current_trade.side == "long" else "Buy"
        close_res = await exchange_service.place_order(
            symbol=symbol,
            side=close_side,
            qty=float(current_trade.qty),
            order_type="Market",
            reduce_only=True,
            client_order_id=f"cw-flip-{current_trade.id}",
        )
        if not close_res.get("ok"):
            log.error(f"[flip] market close falhou trade#{current_trade.id}: {close_res.get('msg') or close_res.get('error')}")
            return False

        # 3. Exit price aproximado via avgPrice
        result = close_res.get("result") or {}
        exit_price = float(result.get("avgPrice") or 0) or await _get_mark_price(symbol) or float(current_trade.entry_price or 0)

        # 4. Fecha no DB
        await real_trade_service.close_trade(
            trade_id=current_trade.id,
            exit_price=exit_price,
            status="closed_flip",
            notes=f"auto-flip: fechado pra reversão de direção",
        )
        log.info(f"[flip] EXECUTED close trade#{current_trade.id} {symbol} {current_trade.side} → flipping")
        return True
    except Exception as e:
        log.error(f"[flip] erro flipando trade#{current_trade.id}: {e}")
        return False


def _compute_qty(
    entry: float, stop: float, risk_pct: float, equity_usd: float,
    leverage: int = 1,
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

    # Cap de margem por trade — se notional/lev > max_margin% × equity, reduz qty.
    # Isso protege quando SL é apertado (risk_dist pequeno → qty explode).
    lev = max(int(leverage or 1), 1)
    max_margin_usd = equity_usd * (MAX_MARGIN_PCT_PER_TRADE / 100.0)
    max_notional_by_margin = max_margin_usd * lev
    capped_reason = None
    if notional_nominal > max_notional_by_margin:
        qty_capped = max_notional_by_margin / entry
        risk_capped_usd = qty_capped * risk_dist
        risk_pct_capped = (risk_capped_usd / equity_usd) * 100.0
        capped_reason = (
            f"margin cap: notional ${notional_nominal:.0f} → ${max_notional_by_margin:.0f} "
            f"(margem {MAX_MARGIN_PCT_PER_TRADE}% × lev {lev}); "
            f"risco real {risk_pct:.2f}% → {risk_pct_capped:.2f}%"
        )
        qty_nominal = qty_capped
        notional_nominal = qty_capped * entry
        risk_pct = risk_pct_capped  # reflete risco real reduzido

    if notional_nominal >= MIN_NOTIONAL_USD:
        return {
            "qty": round(qty_nominal, 6),
            "status": "capped" if capped_reason else "ok",
            "notional_usd": round(notional_nominal, 2),
            "risk_pct_real": round(risk_pct, 3),
            "reason": capped_reason or "nominal sizing",
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

            # ── Direction flip (Fase 2): se há trade aberto na direção oposta,
            # avalia gate. Passa → fecha atual primeiro. Bloqueia → advisory
            # (não abre, snapshot fica como referência informativa).
            if not SHADOW_ENABLED:
                opposite = await _find_opposite_open_trade(rec["symbol"], rec["direction"])
                if opposite is not None:
                    should_flip, reason = await _evaluate_flip_gate(opposite, rec)
                    if should_flip:
                        log.info(
                            f"[flip] {rec['symbol']} {opposite.side}→{rec['direction']}: {reason}"
                        )
                        ok = await _execute_flip(opposite)
                        if not ok:
                            log.warning(f"[flip] {rec['symbol']} falhou — pulando entrada nova")
                            continue
                        # flip executado — segue fluxo abrindo a nova direção
                    else:
                        log.info(
                            f"[flip] {rec['symbol']} ADVISORY (não executa): {reason}"
                        )
                        continue

            entry = float(rec.get("entry") or 0)
            stop = float(rec.get("stop_loss") or 0)
            risk_pct = float(rec.get("risk_pct") or 1.0)
            equity_usd, equity_src = await _resolve_equity_usd()
            lev = int(rec.get("leverage") or 1)
            sizing = _compute_qty(entry, stop, risk_pct, equity_usd, leverage=lev)
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

            # Cap de exposição agregada — bloqueia se total notional > X% banca
            try:
                open_notional = await _open_notional_usd()
                new_notional = float(sizing["notional_usd"])
                total_after = open_notional + new_notional
                cap_usd = equity_usd * (MAX_TOTAL_NOTIONAL_PCT / 100.0)
                if total_after > cap_usd:
                    log.warning(
                        f"[shadow] {rec.get('symbol')} BLOCKED total-notional cap: "
                        f"open=${open_notional:.0f} + new=${new_notional:.0f} = "
                        f"${total_after:.0f} > cap ${cap_usd:.0f} "
                        f"({MAX_TOTAL_NOTIONAL_PCT}% × equity ${equity_usd:.0f})"
                    )
                    continue
            except Exception as e:
                log.warning(f"[shadow] total-notional check falhou: {e}")

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
                    take_profit=tp2,  # TP2 — alvo final (closePosition=true)
                    tp1=float(tp1) if tp1 is not None else None,  # bracket 45/55 quando ambos vierem
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

                # Captura IDs das ordens condicionais pro trade manager (Fase 2)
                sl_oid = order_res.get("sl_order_id")
                tp1_oid = order_res.get("tp1_order_id")
                tp2_oid = order_res.get("tp2_order_id")
                if not order_res.get("sl_ok"):
                    log.error(
                        f"[shadow→live] ⚠ {rec['symbol']} ABERTO SEM STOP — "
                        f"posição precisa atenção manual"
                    )
                if order_res.get("tp1_skipped"):
                    log.warning(f"[shadow→live] {rec['symbol']} TP1 skip (qty parcial=0); 100% no TP2")
                elif not order_res.get("tp1_ok"):
                    log.warning(f"[shadow→live] {rec['symbol']} TP1 falhou (sem parcial)")
                if not order_res.get("tp2_ok"):
                    log.warning(f"[shadow→live] {rec['symbol']} TP2 falhou")

                log.info(
                    f"[shadow→live] EXECUTED {rec['symbol']} {exch_side} qty={qty} "
                    f"order_id={exchange_order_id} avg={entry_actual} "
                    f"SL={sl_oid} TP1={tp1_oid} TP2={tp2_oid}"
                )

            # IDs das ordens condicionais (só existem no fluxo "auto"; em shadow ficam None)
            _sl_oid = locals().get("sl_oid") if source == "auto" else None
            _tp1_oid = locals().get("tp1_oid") if source == "auto" else None
            _tp2_oid = locals().get("tp2_oid") if source == "auto" else None

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
                sl_order_id=_sl_oid,
                tp1_order_id=_tp1_oid,
                tp2_order_id=_tp2_oid,
                sl_current_price=stop,
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

    # FIX CRÍTICO: paper-trade NÃO fecha trades reais (source="auto").
    # Antes, snap resolvendo via candle simulado fechava o RealTrade no DB,
    # mas a posição na exchange seguia aberta (preço só passou perto do TP,
    # não bateu o trigger real). Resultado: DB "closed" + posição órfã +
    # PnL errado calculado com exit=planned_tp2 e entry possivelmente 0.
    #
    # Comportamento correto:
    #   - source="shadow": fecha via paper (simulação é a fonte da verdade)
    #   - source="auto" + qualquer outcome (tp1/tp2/be/stop): NÃO fecha,
    #     deixa o trade_manager (que poll a exchange) detectar qty=0 e fechar.
    #   - source="auto" + expired: ainda emite market close (snap expirou,
    #     posição precisa ser fechada explicitamente — não há trigger pendente).
    if trade.source == "auto" and snap.status != "expired":
        log.debug(
            f"[shadow] skip close paper-resolved trade#{trade.id} {trade.symbol} "
            f"source=auto snap={snap.status} — trade_manager cuida via polling"
        )
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
