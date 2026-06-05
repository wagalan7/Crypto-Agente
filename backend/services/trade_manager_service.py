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

# ── Time stop (Fase B Lite, postmortem N=237) ──────────────────────────────
# 42% dos SLs duraram > 2h (slow bleed). Fecha trade que não atingiu TP1
# após um teto por categoria de TF. Defaults:
#   SCALP (1m-15m)   → 240 min  (4h)
#   DAY   (30m-2h)   → 1440 min (24h)
#   SWING (4h+)      → 10080 min (1 semana)
TIME_STOP_ENABLED = os.getenv("TIME_STOP_ENABLED", "true").strip().lower() in ("1", "true", "yes")
TIME_STOP_SCALP_MIN = int(os.getenv("TIME_STOP_SCALP_MIN", "240"))
TIME_STOP_DAY_MIN = int(os.getenv("TIME_STOP_DAY_MIN", "1440"))
TIME_STOP_SWING_MIN = int(os.getenv("TIME_STOP_SWING_MIN", "10080"))

# ── Auto-cura de proteção (postmortem SUI/DOGE 05/06) ───────────────────────
# Bug em prod: posições abriram com pernas de proteção faltando (SUI sem TP2,
# DOGE sem SL nem TP) — falha transitória no algoOrder na hora da abertura.
# A cada poll, em pre_tp1, verifica os IDs de proteção no DB e recria as pernas
# ausentes. SL é a perna crítica (segurança); TPs evitam correr além do alvo.
PROTECTION_AUTOHEAL_ENABLED = os.getenv("PROTECTION_AUTOHEAL_ENABLED", "true").strip().lower() in ("1", "true", "yes")
# Fração da posição destinada ao TP1 parcial (espelha tp1_qty_pct=0.45 do open).
_TP1_QTY_PCT = float(os.getenv("PROTECTION_TP1_QTY_PCT", "0.45"))


def _tf_category(tf: str | None) -> str:
    """Mapeia timeframe → 'scalp' | 'day' | 'swing'."""
    if not tf:
        return "day"
    t = tf.strip().lower()
    if t in ("1m", "3m", "5m", "15m"):
        return "scalp"
    if t in ("30m", "1h", "2h"):
        return "day"
    return "swing"


def _time_stop_threshold_min(tf: str | None) -> int:
    cat = _tf_category(tf)
    if cat == "scalp":
        return TIME_STOP_SCALP_MIN
    if cat == "day":
        return TIME_STOP_DAY_MIN
    return TIME_STOP_SWING_MIN


async def _resolve_trade_timeframe(trade: RealTrade) -> str | None:
    """
    RealTrade NÃO tem coluna `timeframe`. Deriva da snapshot ligada via
    recommendation_id. Retorna None se não houver link — caller usa default DAY.
    """
    rec_id = getattr(trade, "recommendation_id", None)
    if not rec_id:
        return None
    try:
        from models.recommendation_snapshot import RecommendationSnapshot
        async with get_session() as session:
            stmt = select(RecommendationSnapshot.timeframe).where(
                RecommendationSnapshot.id == rec_id
            )
            row = (await session.execute(stmt)).first()
            if row and row[0]:
                return row[0]
    except Exception as e:
        log.warning(f"[time-stop] resolve tf #{trade.id} falhou: {e}")
    return None


async def _fetch_exchange_position(symbol: str) -> tuple[float | None, float | None]:
    """Busca (qty, entry_price) atuais da posição na exchange. (None, None) se erro, (0, None) se fechada."""
    try:
        from services import exchange_service
        res = await exchange_service.get_positions(symbol=symbol)
        if not res.get("ok"):
            return None, None
        for p in res.get("positions") or []:
            return float(p.get("size") or 0), float(p.get("entry_price") or 0) or None
        return 0.0, None
    except Exception as e:
        log.warning(f"[trade-manager] fetch position {symbol} falhou: {e}")
        return None, None


async def _fetch_exchange_qty(symbol: str) -> float | None:
    """Backward-compat wrapper — só retorna qty."""
    qty, _ = await _fetch_exchange_position(symbol)
    return qty


async def _transition_to_post_tp1(trade: RealTrade) -> bool:
    """
    TP1 detectado: cria novo SL em entry (breakeven), depois cancela SL antigo.
    Ordem importa: se create falhar, antigo permanece — posição NUNCA fica nua.
    """
    from services import exchange_service, binance_signed_service
    sym = trade.symbol

    # Resolve entry price: prefere o real da exchange (mais confiável que DB,
    # que pode ter avgPrice=0 em market orders).
    qty_now, entry_real = await _fetch_exchange_position(sym)
    entry = entry_real or trade.entry_price or trade.planned_tp1 or 0.0
    if entry <= 0:
        log.error(
            f"[trade-manager] {sym} #{trade.id} sem entry_price válido "
            f"(db={trade.entry_price}, exchange={entry_real}) — abortando transição"
        )
        return False

    qty_rem = qty_now if (qty_now and qty_now > 0) else trade.qty

    # 1. Cria novo SL em BE PRIMEIRO
    entry_side = "Buy" if trade.side == "long" else "Sell"
    try:
        prot = await binance_signed_service.place_protection_orders(
            sym, entry_side, qty=qty_rem,
            stop_loss=entry,
            tp1=None, tp2=None,
            client_order_id_prefix=f"cw-be-{trade.id}",
        )
    except Exception as e:
        log.error(f"[trade-manager] criar novo SL@BE {sym} erro: {e}")
        return False

    if not prot.get("sl_ok"):
        log.error(
            f"[trade-manager] CRITICAL: novo SL@BE {sym} falhou: {prot.get('sl_msg')} "
            f"— mantendo SL antigo (#{trade.sl_order_id})"
        )
        return False

    new_sl_id = prot.get("sl_order_id")

    # 2. SÓ AGORA cancela SL antigo (já temos cobertura nova)
    if trade.sl_order_id:
        try:
            cancel_res = await exchange_service.cancel_algo_order(trade.sl_order_id)
            if not cancel_res.get("ok"):
                log.warning(
                    f"[trade-manager] cancel SL antigo {sym} algoId={trade.sl_order_id}: "
                    f"{cancel_res.get('msg') or cancel_res.get('error')} (pode já ter executado)"
                )
        except Exception as e:
            log.warning(f"[trade-manager] cancel SL {sym} erro: {e}")

    # 3. Atualiza DB
    async with get_session() as session:
        fresh = (await session.execute(
            select(RealTrade).where(RealTrade.id == trade.id)
        )).scalar_one_or_none()
        if fresh is None:
            return False
        fresh.phase = "post_tp1"
        fresh.sl_order_id = new_sl_id
        fresh.sl_current_price = entry
        # Se entry no DB estava errado, atualiza
        if (not fresh.entry_price or fresh.entry_price <= 0) and entry_real:
            fresh.entry_price = entry_real
        fresh.updated_at = datetime.now(timezone.utc)
        await session.commit()

    log.info(
        f"[trade-manager] {sym} #{trade.id} → post_tp1: SL @ BE {entry} qty={qty_rem} "
        f"(novo algoId={new_sl_id})"
    )
    # Telegram notify TP1 (desacoplado)
    try:
        from services.notification_service import send_telegram, fmt_tp1_hit
        await send_telegram(fmt_tp1_hit(trade), event_type="tp1")
    except Exception as e:
        log.warning(f"[notify] telegram tp1 falhou: {e}")
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

    # Limpa algo orders órfãs antes de fechar — SL/TP que não dispararam ficam
    # pendentes na Binance e poluem o painel (apesar de reduceOnly=true impedir
    # reentrada real, acumular lixo eventualmente bate o cap de ~200/símbolo).
    for oid_field in ("sl_order_id", "tp1_order_id", "tp2_order_id"):
        oid = getattr(trade, oid_field, None)
        if oid:
            try:
                res = await exchange_service.cancel_algo_order(str(oid))
                if res.get("ok"):
                    log.info(f"[trade-manager] {trade.symbol} #{trade.id} cancelled orphan {oid_field}={oid}")
                else:
                    log.debug(f"[trade-manager] cancel {oid_field}={oid}: {res.get('error') or res.get('msg')}")
            except Exception as e:
                log.warning(f"[trade-manager] cancel {oid_field}={oid} falhou: {e}")

    try:
        await real_trade_service.close_trade(
            trade.id,
            exit_price=exit_price or trade.entry_price,
            status=status,
            notes=f"auto-closed by trade_manager ({reason})",
        )
        log.info(f"[trade-manager] {trade.symbol} #{trade.id} CLOSED {status} @ {exit_price}")
        # Telegram notify close (desacoplado)
        try:
            from services.notification_service import send_telegram, fmt_trade_closed
            # Tenta PnL atualizado do DB
            pnl_val = None
            try:
                fresh = await real_trade_service.get_trade(trade.id)
                if fresh:
                    pnl_val = fresh.get("pnl_usd")
            except Exception:
                pnl_val = getattr(trade, "pnl_usd", None)
            await send_telegram(
                fmt_trade_closed(trade, reason=reason, pnl=pnl_val),
                event_type="close",
            )
        except Exception as ne:
            log.warning(f"[notify] telegram close falhou: {ne}")
    except Exception as e:
        log.error(f"[trade-manager] close_trade {trade.id} erro: {e}")


async def _check_time_stop(trade: RealTrade, qty_now: float) -> bool:
    """
    Time stop: se trade está em pre_tp1 há mais que o threshold do TF, fecha
    mercado, envia alerta Telegram explicando o motivo, e marca como
    closed_manual com nota time_stop.

    Retorna True se fechou; False caso contrário.
    Pós-TP1 NÃO dispara — trade já parcialmente realizado fica protegido
    por BE e pode rodar TP2 quanto quiser.
    """
    if not TIME_STOP_ENABLED:
        return False
    if trade.phase == "post_tp1":
        return False
    if not trade.opened_at:
        return False

    # RealTrade não tem coluna timeframe → deriva da snapshot. Default DAY
    # (24h, conservador) se não houver link — evita fechar scalp cedo demais
    # por engano, mas ainda garante teto.
    tf = await _resolve_trade_timeframe(trade)
    threshold_min = _time_stop_threshold_min(tf)
    opened = trade.opened_at
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=timezone.utc)
    age_min = (datetime.now(timezone.utc) - opened).total_seconds() / 60.0
    if age_min < threshold_min:
        return False

    cat = _tf_category(tf)
    sym = trade.symbol
    log.info(
        f"[time-stop] {sym} #{trade.id} idade={age_min:.0f}min >= {threshold_min}min "
        f"({cat.upper()}) — fechando market"
    )

    # 1. Fecha posição market (reduce_only pra não inverter)
    try:
        from services import binance_signed_service, exchange_service
        exit_side = "Sell" if trade.side == "long" else "Buy"
        res = await binance_signed_service.place_order(
            sym, exit_side, qty=qty_now,
            order_type="Market",
            reduce_only=True,
            client_order_id=f"cw-ts-{trade.id}",
        )
        if not res.get("ok"):
            log.warning(f"[time-stop] {sym} close market falhou: {res.get('error')}")
            # Mesmo assim segue — o ciclo seguinte vai detectar qty=0 ou retry
            return False
    except Exception as e:
        log.warning(f"[time-stop] {sym} exchange close erro: {e}")
        return False

    # 2. Cancela algo orders órfãs (SL/TPs ainda pendentes)
    try:
        from services import exchange_service
        for oid_field in ("sl_order_id", "tp1_order_id", "tp2_order_id"):
            oid = getattr(trade, oid_field, None)
            if oid:
                try:
                    await exchange_service.cancel_algo_order(str(oid))
                except Exception:
                    pass
    except Exception:
        pass

    # 3. Marca closed_manual com nota time_stop e exit price = mark atual ou entry
    try:
        from services import real_trade_service
        _, entry_real = await _fetch_exchange_position(sym)  # talvez já zerado
        exit_price = entry_real or trade.entry_price
        await real_trade_service.close_trade(
            trade.id,
            exit_price=exit_price or trade.entry_price,
            status="closed_manual",
            notes=(
                f"time_stop {cat} age={age_min:.0f}min "
                f">= {threshold_min}min (sem TP1)"
            ),
        )
        log.info(f"[time-stop] {sym} #{trade.id} CLOSED time_stop @ {exit_price}")
    except Exception as e:
        log.error(f"[time-stop] close_trade {trade.id} erro: {e}")

    # 4. Telegram alerta com motivo
    try:
        from services.notification_service import send_telegram, fmt_time_stop
        await send_telegram(
            fmt_time_stop(
                trade, age_min=age_min, threshold_min=threshold_min,
                category=cat, tf=tf,
            ),
            event_type="time_stop",
        )
    except Exception as e:
        log.warning(f"[notify] telegram time_stop falhou: {e}")

    return True


async def _ensure_protection(trade: RealTrade, qty_now: float) -> bool:
    """
    Auto-cura: em pre_tp1, recria pernas de proteção (SL/TP1/TP2) que faltam
    no DB. Cobre o bug SUI (sem TP2) / DOGE (sem SL nem TP) — falhas transitórias
    no algoOrder durante a abertura deixavam a posição parcialmente nua.

    Estratégia conservadora (evita ordens duplicadas):
      - Só age em phase=pre_tp1 (em post_tp1 o TP1 já executou e o SL@BE é
        gerido pelo transition; os IDs antigos ficam "usados").
      - "Faltando" = ID None no DB. Com o retry no algoOrder, ID None significa
        de fato que a perna não foi criada.
      - Guarda do TP1 legitimamente pulado: se tp1_order_id é None mas
        tp2_order_id existe, assume skip (qty*0.45 arredondou pra 0) e NÃO
        recria TP1 — senão duplicaria cobertura.

    Retorna True se recriou ao menos uma perna.
    """
    if not PROTECTION_AUTOHEAL_ENABLED:
        return False
    if trade.phase != "pre_tp1" or qty_now <= 0:
        return False

    sl_missing = (not trade.sl_order_id) and bool(trade.planned_stop)
    tp2_missing = bool(trade.planned_tp2) and not trade.tp2_order_id
    # TP1 só é "bracket" quando há TP2 planejado. Skip legítimo: tp2 já existe.
    tp1_missing = (
        bool(trade.planned_tp1) and bool(trade.planned_tp2)
        and not trade.tp1_order_id and not trade.tp2_order_id
    )
    if not (sl_missing or tp1_missing or tp2_missing):
        return False

    from services import binance_signed_service
    sym = trade.symbol
    entry_side = "Buy" if trade.side == "long" else "Sell"

    log.warning(
        f"[autoheal] {sym} #{trade.id} proteção incompleta — "
        f"sl_missing={sl_missing} tp1_missing={tp1_missing} tp2_missing={tp2_missing} "
        f"(sl_id={trade.sl_order_id} tp1_id={trade.tp1_order_id} tp2_id={trade.tp2_order_id})"
    )

    healed: list[str] = []
    failed: list[str] = []
    new_sl_id = new_tp1_id = new_tp2_id = None

    # ── SL (perna crítica — sempre primeiro) ─────────────────────────────
    if sl_missing:
        try:
            r = await binance_signed_service.place_protection_orders(
                sym, entry_side, qty=qty_now,
                stop_loss=trade.planned_stop, tp1=None, tp2=None,
                client_order_id_prefix=f"cw-heal-sl-{trade.id}",
            )
            if r.get("sl_ok") and r.get("sl_order_id"):
                new_sl_id = r.get("sl_order_id")
                healed.append("SL")
            else:
                failed.append(f"SL({r.get('sl_msg')})")
        except Exception as e:
            failed.append(f"SL({e})")

    # ── TP2 / TP restante ────────────────────────────────────────────────
    if tp2_missing:
        # qty restante: se há (ou vamos recriar) TP1 parcial, TP2 cobre os 55%;
        # senão cobre o total. Como estamos em pre_tp1, TP1 ainda não bateu.
        # reduceOnly limita a execução ao tamanho real da posição de qualquer forma.
        tp1_present = bool(trade.tp1_order_id) or tp1_missing
        qty_tp2 = qty_now * (1.0 - _TP1_QTY_PCT) if tp1_present else qty_now
        try:
            r = await binance_signed_service.place_protection_orders(
                sym, entry_side, qty=qty_tp2,
                stop_loss=None, tp1=None, tp2=trade.planned_tp2,
                client_order_id_prefix=f"cw-heal-tp2-{trade.id}",
            )
            if r.get("tp2_ok") and r.get("tp2_order_id"):
                new_tp2_id = r.get("tp2_order_id")
                healed.append("TP2")
            else:
                failed.append(f"TP2({r.get('tp2_msg')})")
        except Exception as e:
            failed.append(f"TP2({e})")

    # ── TP1 parcial (só quando SL+TP também estavam nus = abertura falhou) ─
    if tp1_missing:
        qty_tp1 = qty_now * _TP1_QTY_PCT
        try:
            r = await binance_signed_service.place_protection_orders(
                sym, entry_side, qty=qty_tp1,
                stop_loss=None, tp1=None, tp2=trade.planned_tp1,
                client_order_id_prefix=f"cw-heal-tp1-{trade.id}",
            )
            if r.get("tp2_ok") and r.get("tp2_order_id"):
                new_tp1_id = r.get("tp2_order_id")  # placed via tp_final → vem em tp2_order_id
                healed.append("TP1")
            else:
                failed.append(f"TP1({r.get('tp2_msg')})")
        except Exception as e:
            failed.append(f"TP1({e})")

    if not healed and not new_sl_id and not new_tp1_id and not new_tp2_id:
        log.error(f"[autoheal] {sym} #{trade.id} nada curado — falhas: {failed}")
        return False

    # ── Persiste IDs novos ───────────────────────────────────────────────
    async with get_session() as session:
        fresh = (await session.execute(
            select(RealTrade).where(RealTrade.id == trade.id)
        )).scalar_one_or_none()
        if fresh:
            if new_sl_id:
                fresh.sl_order_id = new_sl_id
                fresh.sl_current_price = trade.planned_stop
            if new_tp1_id:
                fresh.tp1_order_id = new_tp1_id
            if new_tp2_id:
                fresh.tp2_order_id = new_tp2_id
            fresh.updated_at = datetime.now(timezone.utc)
            await session.commit()
            # reflete no objeto em memória pra não recurar no mesmo tick
            trade.sl_order_id = fresh.sl_order_id
            trade.tp1_order_id = fresh.tp1_order_id
            trade.tp2_order_id = fresh.tp2_order_id

    log.info(f"[autoheal] {sym} #{trade.id} recriou: {healed} (falhas: {failed or 'nenhuma'})")

    # ── Alerta Telegram (item 3) ─────────────────────────────────────────
    try:
        from services.notification_service import send_telegram
        side = str(trade.side).upper()
        ok_str = ", ".join(healed) if healed else "nenhuma"
        fail_str = ", ".join(failed) if failed else "—"
        msg = (
            f"\U0001F527 *Auto-cura de proteção* \u2014 `{sym}` ({side})\n"
            f"Posição estava sem proteção completa.\n"
            f"Pernas recriadas: `{ok_str}`\n"
            f"Falhas restantes: `{fail_str}`"
        )
        await send_telegram(msg, event_type="autoheal")
    except Exception as e:
        log.warning(f"[notify] telegram autoheal falhou: {e}")

    return True


async def _process_trade(trade: RealTrade) -> None:
    """Avalia um trade aberto e age conforme a fase."""
    qty_now = await _fetch_exchange_qty(trade.symbol)
    if qty_now is None:
        return  # erro de leitura — pula esse ciclo

    # ── Time stop check (antes de qualquer outra coisa) ──────────────────
    if qty_now > 0:
        try:
            if await _check_time_stop(trade, qty_now):
                return
        except Exception as e:
            log.warning(f"[time-stop] check #{trade.id} erro: {e}")

        # ── Auto-cura de proteção (recria pernas faltantes) ──────────────
        try:
            await _ensure_protection(trade, qty_now)
        except Exception as e:
            log.warning(f"[autoheal] check #{trade.id} erro: {e}")

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
async def backfill_protection(force: bool = False) -> dict:
    """
    Itera trades RealTrade com status='open' e cria SL + TP1 + TP2 na exchange.

    Comportamento:
      - force=False (default): só atua nos trades sem sl_order_id setado.
      - force=True: ignora sl_order_id, tenta criar novamente. Útil quando
        ordens antigas foram canceladas/expiraram (ex: bug do transition).
        Em modo force, SE o trade já está em phase=post_tp1, cria apenas SL
        em entry (BE) + TP2; pula TP1 (que já executou).
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
        # Já tem SL ativo e não estamos forçando? pula
        if t.sl_order_id and not force:
            results.append({"trade_id": t.id, "symbol": t.symbol, "skipped": True, "reason": "já tem sl_order_id"})
            continue
        if not t.planned_stop:
            results.append({"trade_id": t.id, "symbol": t.symbol, "skipped": True, "reason": "sem planned_stop"})
            continue

        # Confirma qty real na exchange + entry price atual
        qty_now, entry_real = await _fetch_exchange_position(t.symbol)
        if qty_now is None or qty_now <= 0:
            results.append({"trade_id": t.id, "symbol": t.symbol, "skipped": True, "reason": f"qty na exchange = {qty_now}"})
            continue

        # Em force + phase=post_tp1: SL em BE (entry), não em planned_stop. TP1 já bateu.
        is_post_tp1 = (t.phase == "post_tp1") or (t.qty_initial and qty_now <= t.qty_initial * _TP1_DETECTED_AT)
        if force and is_post_tp1:
            sl_price = entry_real or t.entry_price
            tp1_arg = None  # já executou
            tp2_arg = t.planned_tp2
            note = "force/post_tp1"
        else:
            sl_price = t.planned_stop
            tp1_arg = t.planned_tp1
            tp2_arg = t.planned_tp2
            note = "force" if force else "fresh"

        if not sl_price or sl_price <= 0:
            results.append({"trade_id": t.id, "symbol": t.symbol, "skipped": True, "reason": f"sl_price inválido ({sl_price}); entry_real={entry_real}"})
            continue

        entry_side = "Buy" if t.side == "long" else "Sell"
        try:
            prot = await binance_signed_service.place_protection_orders(
                t.symbol, entry_side, qty=qty_now,
                stop_loss=sl_price,
                tp1=tp1_arg,
                tp2=tp2_arg,
                client_order_id_prefix=f"cw-bf-{t.id}",
            )
        except Exception as e:
            results.append({"trade_id": t.id, "symbol": t.symbol, "error": str(e)})
            continue

        # Salva os IDs (só sobrescreve se a ordem foi criada com sucesso)
        async with get_session() as session:
            fresh = (await session.execute(
                select(RealTrade).where(RealTrade.id == t.id)
            )).scalar_one_or_none()
            if fresh:
                if prot.get("sl_ok"):
                    fresh.sl_order_id = prot.get("sl_order_id")
                    fresh.sl_current_price = sl_price
                if prot.get("tp1_ok") and tp1_arg:
                    fresh.tp1_order_id = prot.get("tp1_order_id")
                if prot.get("tp2_ok"):
                    fresh.tp2_order_id = prot.get("tp2_order_id")
                if fresh.qty_initial is None:
                    fresh.qty_initial = qty_now
                if (not fresh.entry_price or fresh.entry_price <= 0) and entry_real:
                    fresh.entry_price = entry_real
                if is_post_tp1 and fresh.phase != "post_tp1":
                    fresh.phase = "post_tp1"
                fresh.updated_at = datetime.now(timezone.utc)
                await session.commit()

        results.append({
            "trade_id": t.id,
            "symbol": t.symbol,
            "side": t.side,
            "qty": qty_now,
            "entry_real": entry_real,
            "sl_price_used": sl_price,
            "note": note,
            "is_post_tp1": is_post_tp1,
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
