"""
Trade Manager — gerenciamento ativo de trades em aberto (Fase 2).

Loop async que poll-eia posições na exchange e gerencia transições de fase:

  Fase "pre_tp1": SL inicial em planned_stop, TP1 parcial 45% pendente, TP2 100%
  Fase "post_tp1": TP1 bateu (parcial executada) → SL movido pra BE estrutural
                   (swing logo abaixo/acima do entry; fallback pro BE exato)

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
# Folga (s) pós-criação do trade antes da auto-cura agir: dá tempo da colocação
# inicial do bracket persistir os IDs no DB, evitando bracket duplicado por
# corrida confirm-entry × tick. Trabalha junto com dedup_live (rede principal).
PROTECTION_HEAL_GRACE_SECONDS = float(os.getenv("PROTECTION_HEAL_GRACE_SECONDS", "60"))
# Verificação ao vivo: além de ID None no DB, confirma na corretora que SL/TP2
# estão REALMENTE vivos (GET /fapi/v1/openAlgoOrders). Pega a perna cujo ID
# existe no DB mas sumiu da exchange (cancelada/expirada/disparada por fora sem
# fechar a posição). Fail-safe: se a query falhar, NÃO recria por "sumiço"
# (evita duplicar ordem com base em leitura incerta) — mantém a cura por ID-None.
PROTECTION_VERIFY_LIVE = os.getenv("PROTECTION_VERIFY_LIVE", "true").strip().lower() in ("1", "true", "yes")
# Fração da posição destinada ao TP1 parcial (espelha tp1_qty_pct=0.45 do open).
_TP1_QTY_PCT = float(os.getenv("PROTECTION_TP1_QTY_PCT", "0.45"))
# Poeira: residual cujo notional fica abaixo disto é tratado como "fechado".
# Evita travar o trade aberto por sobra de stepSize (ex.: 0,1 FARTCOIN ≈ $0,01).
DUST_NOTIONAL_USD = float(os.getenv("TRADE_MANAGER_DUST_USD", "1.0"))

# ── BE estrutural pós-TP1 (Opção B) ─────────────────────────────────────────
# Problema: ao bater TP1, o SL ia pro BREAKEVEN EXATO (= entry). No reteste
# pós-TP1 (recuo normal antes de seguir pro TP2), um pavio tocava o entry e
# estopava em BE; o preço retomava e ia pro TP2 sem a posição. Solução: ancorar
# o novo SL na ESTRUTURA (último swing logo abaixo/acima do entry) com folga de
# ATR, em vez do número exato — dando espaço pro ruído do reteste.
# Seguro por construção (long; short é simétrico):
#   • clamp em [floor, entry] — nunca afrouxa acima do entry nem além do floor;
#   • give-back limitado DINAMICAMENTE pelo que a parcial do TP1 (45%) já cobre,
#     de forma que o pior caso (estopar no floor) fique ≥ breakeven AGREGADO —
#     ou seja, o trade não vira negativo por causa dessa folga;
#   • nunca abaixo do planned_stop original; fallback pro BE exato se algo falhar.
BE_STRUCTURAL_ENABLED = os.getenv("BE_STRUCTURAL_ENABLED", "true").strip().lower() in ("1", "true", "yes")
# Folga de ATR além do swing estrutural (mesma filosofia do entry_planner: 0.3).
BE_STRUCT_ATR_BUFFER = float(os.getenv("BE_STRUCT_ATR_BUFFER", "0.25"))
# Teto duro de give-back pós-TP1 em múltiplos do risco original (R). O limite
# efetivo é o MENOR entre isto e o que a parcial do TP1 cobre (garantia de não
# virar negativo). 0 desliga a folga (volta ao BE exato).
BE_MAX_GIVEBACK_R = float(os.getenv("BE_MAX_GIVEBACK_R", "0.5"))

# ── #4 Proteção PRÉ-TP1 (lock parcial de risco) ─────────────────────────────
# Dado: 68% dos setups TOCAM o TP1, mas só 31% correm até o TP2. A perna pré-TP1
# fica no stop CHEIO (1R) o tempo todo — então um trade que andou 60-70% rumo ao
# TP1 e reverteu devolve 1R inteiro. Esta proteção, quando o preço cruza
# PROTECT_TRIGGER_FRAC do caminho entry→TP1 (sem ter batido o TP1 ainda), aperta o
# SL de pre_tp1 pra entry ∓ PROTECT_LOCK_R·R — reduzindo a perda máxima daquela
# perna SEM ir ao BE exato (que tomaria pavio). Reusa o mesmo mecanismo de troca
# de SL do post_tp1 (cria-novo → cancela-antigo: posição nunca fica nua). Só
# APERTA (nunca afrouxa) e age UMA vez por trade. DEFAULT OFF (dinheiro real):
# liga via env após revisar. É a mudança mais delicada (mexe em SL ao vivo).
PRE_TP1_PROTECT_ENABLED = os.getenv("PRE_TP1_PROTECT_ENABLED", "false").strip().lower() in ("1", "true", "yes")
PRE_TP1_PROTECT_TRIGGER_FRAC = float(os.getenv("PRE_TP1_PROTECT_TRIGGER_FRAC", "0.6"))  # 60% rumo ao TP1
PRE_TP1_PROTECT_LOCK_R = float(os.getenv("PRE_TP1_PROTECT_LOCK_R", "0.5"))  # SL novo a entry ∓ 0.5R


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


async def _structural_be_stop(trade: RealTrade, entry: float) -> float | None:
    """Calcula um SL pós-TP1 ancorado em ESTRUTURA (swing logo abaixo do entry
    pra long / acima pra short) com folga de ATR, em vez do breakeven exato. Dá
    espaço pro reteste pós-TP1 sem devolver mais do que a parcial do TP1 cobre.
    Retorna o preço do SL, ou None pra o caller cair no BE exato (fail-soft).

    Garantias de segurança (long; short é simétrico):
      floor ≤ s ≤ entry, com floor = entry − giveback·R e
      giveback = min(BE_MAX_GIVEBACK_R, (qty_tp1/qty_resto)·R_tp1) — assim o pior
      caso (estopar no floor) fica ≥ breakeven AGREGADO, pois a perna do TP1
      (≈45%) já foi embolsada. Nunca abaixo do planned_stop original.
    """
    if not BE_STRUCTURAL_ENABLED or BE_MAX_GIVEBACK_R <= 0:
        return None
    try:
        from services.binance_service import fetch_ohlcv
        from services.indicator_service import calculate_indicators
        from services.entry_planner import _swing_points

        planned_stop = float(trade.planned_stop or 0)
        planned_tp1 = float(trade.planned_tp1 or 0)
        if planned_stop <= 0 or entry <= 0:
            return None
        R = abs(entry - planned_stop)
        if R <= 0:
            return None

        # Give-back seguro: com ~45% embolsado no TP1 a R_tp1, o restante (55%)
        # pode devolver no máx (0.45/0.55)·R_tp1 sem o trade virar negativo.
        rem_frac = max(1e-6, 1.0 - _TP1_QTY_PCT)
        r_tp1 = (abs(planned_tp1 - entry) / R) if planned_tp1 > 0 else 0.0
        safe_giveback_r = (_TP1_QTY_PCT / rem_frac) * r_tp1 if r_tp1 > 0 else 0.0
        giveback_r = min(BE_MAX_GIVEBACK_R, safe_giveback_r)
        if giveback_r <= 0:
            return None  # sem margem segura → BE exato

        tf = (await _resolve_trade_timeframe(trade)) or "15m"
        df = await fetch_ohlcv(trade.symbol, tf, 150)
        if df is None or df.empty or len(df) < 30:
            return None
        ind = calculate_indicators(df)
        atr = float(getattr(ind, "atr", 0) or 0)
        if atr <= 0:
            return None
        buffer = atr * BE_STRUCT_ATR_BUFFER
        current = float(df["close"].iloc[-1])
        highs_idx, lows_idx = _swing_points(df, lookback=3)

        if trade.side == "long":
            floor = entry - giveback_r * R
            lows = [float(df["low"].iloc[i]) for i in lows_idx[-6:]
                    if float(df["low"].iloc[i]) < entry]
            if not lows:
                return None
            swing = min(lows[-2:]) if len(lows) >= 2 else lows[-1]
            s = swing - buffer
            s = min(s, entry)                 # nunca acima do BE
            s = max(s, floor, planned_stop)   # respeita give-back e o stop original
            if s >= current:                  # estoparia na hora → inútil
                return None
            return round(s, 8)
        else:  # short
            ceil = entry + giveback_r * R
            highs = [float(df["high"].iloc[i]) for i in highs_idx[-6:]
                     if float(df["high"].iloc[i]) > entry]
            if not highs:
                return None
            swing = max(highs[-2:]) if len(highs) >= 2 else highs[-1]
            s = swing + buffer
            s = max(s, entry)                 # nunca abaixo do BE
            s = min(s, ceil, planned_stop)    # respeita give-back e o stop original
            if s <= current:
                return None
            return round(s, 8)
    except Exception as e:
        log.warning(f"[trade-manager] BE estrutural {trade.symbol} falhou (fallback BE exato): {e}")
        return None


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

    # BE estrutural (Opção B): ancora o novo SL na estrutura logo abaixo/acima do
    # entry (com folga de ATR) em vez do BE exato — dá espaço pro reteste pós-TP1.
    # Fallback total pro entry exato se não houver estrutura segura/dado.
    be_price = entry
    struct = await _structural_be_stop(trade, entry)
    if struct is not None:
        be_price = struct
        log.info(
            f"[trade-manager] {sym} #{trade.id} BE estrutural: SL @ {be_price} "
            f"(entry {entry}, folga {abs(entry - be_price):.6g})"
        )

    # 1. Cria novo SL em BE PRIMEIRO
    entry_side = "Buy" if trade.side == "long" else "Sell"
    try:
        prot = await binance_signed_service.place_protection_orders(
            sym, entry_side, qty=qty_rem,
            stop_loss=be_price,
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

    # 2b. Captura P&L parcial REAL embolsada no TP1 (go-live Opção B). A perna
    #     parcial já saiu na corretora; sem persistir isso, o close_trade só
    #     contaria o restante e subcontaria o ganho (ex: trade que vira breakeven
    #     marcava R=0 apesar de ter embolsado o TP1). Usa o fill real; cai pro
    #     planned_tp1 se a corretora não devolver execuções.
    tp1_partial_usd = None
    try:
        qty_init = trade.qty_initial
        if not qty_init or qty_init <= 0:
            qty_init = (qty_rem / (1.0 - _TP1_QTY_PCT)) if qty_rem else 0.0
        filled_tp1 = max(0.0, float(qty_init) - float(qty_rem))
        if filled_tp1 > 0 and entry > 0:
            sign = 1 if trade.side == "long" else -1
            close_side = "SELL" if trade.side == "long" else "BUY"
            fill_price = float(trade.planned_tp1 or 0.0)
            fill_fee = 0.0
            ex = await exchange_service.get_executions(sym, limit=20)
            if ex.get("ok"):
                cfills = sorted(
                    [f for f in (ex.get("fills") or [])
                     if str(f.get("side", "")).upper() == close_side
                     and float(f.get("price") or 0) > 0],
                    key=lambda f: int(f.get("time") or 0), reverse=True,
                )
                acc_q = acc_val = acc_fee = 0.0
                for f in cfills:
                    q = float(f.get("qty") or 0)
                    if q <= 0:
                        continue
                    take = min(q, filled_tp1 - acc_q)
                    acc_q += take
                    acc_val += take * float(f["price"])
                    acc_fee += float(f.get("fee") or 0) * (take / q)
                    if acc_q >= filled_tp1 * 0.999:
                        break
                if acc_q > 0:
                    fill_price = acc_val / acc_q
                    fill_fee = acc_fee
            if fill_price > 0:
                tp1_partial_usd = round(
                    (fill_price - entry) * sign * filled_tp1 - fill_fee, 4
                )
                log.info(
                    f"[trade-manager] {sym} #{trade.id} TP1 parcial embolsada: "
                    f"qty={filled_tp1} @ {fill_price} → ${tp1_partial_usd:+.4f}"
                )
    except Exception as e:
        log.warning(f"[trade-manager] captura TP1 parcial #{trade.id} falhou: {e}")

    # 3. Atualiza DB
    async with get_session() as session:
        fresh = (await session.execute(
            select(RealTrade).where(RealTrade.id == trade.id)
        )).scalar_one_or_none()
        if fresh is None:
            return False
        fresh.phase = "post_tp1"
        fresh.sl_order_id = new_sl_id
        fresh.sl_current_price = be_price
        if tp1_partial_usd is not None:
            fresh.tp1_realized_usd = tp1_partial_usd
        # Slippage a partir do fill REAL da exchange (entry_real). Corrige tanto o
        # -100% (entry_price<=0) quanto o 0% FALSO (entry teórico gravado como fill).
        if entry_real and entry_real > 0:
            from services import real_trade_service
            # entry_price (base de PnL) só é sobrescrito se estava inválido (<=0).
            if not fresh.entry_price or fresh.entry_price <= 0:
                fresh.entry_price = entry_real
            await real_trade_service.recompute_entry_slippage(session, fresh, fill_price=entry_real)
        fresh.updated_at = datetime.now(timezone.utc)
        await session.commit()

    log.info(
        f"[trade-manager] {sym} #{trade.id} → post_tp1: SL @ {be_price} "
        f"(entry={entry}, {'estrutural' if be_price != entry else 'BE exato'}) "
        f"qty={qty_rem} (novo algoId={new_sl_id})"
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

    # 1. Preço de saída REAL: último fill no lado de fechamento. Mais preciso
    #    que estimar por planned_stop/tp2 — e corrige o PnL $0,00 que acontecia
    #    quando sl_current_price/planned_stop estavam vazios e o exit caía no
    #    entry_price (diff = 0 → pnl 0).
    try:
        close_side = "SELL" if trade.side == "long" else "BUY"
        ex = await exchange_service.get_executions(trade.symbol, limit=20)
        if ex.get("ok"):
            fills = [
                f for f in (ex.get("fills") or [])
                if str(f.get("side", "")).upper() == close_side and float(f.get("price") or 0) > 0
            ]
            if fills:
                latest = max(fills, key=lambda f: int(f.get("time") or 0))
                exit_price = float(latest["price"])
                log.info(f"[trade-manager] exit real #{trade.id} via fill: {exit_price}")
    except Exception as e:
        log.warning(f"[trade-manager] exit real via fills #{trade.id} falhou: {e}")

    # 2. Fallback: níveis planejados conforme o motivo do fechamento.
    if not exit_price or exit_price <= 0:
        if reason == "tp2":
            exit_price = trade.planned_tp2 or trade.entry_price
        elif reason == "stop":
            exit_price = trade.sl_current_price or trade.planned_stop or trade.entry_price
        else:
            exit_price = trade.entry_price

    # ── Classificação refinada (post_tp1) ────────────────────────────────
    # O caller passa "tp2" pra qualquer fechamento em post_tp1, mas pode ter
    # sido BE ou SL. Se temos exit real, reclassifica pela proximidade: perto
    # do TP2 = tp2; perto do entry = be (lucro≥0) ou stop (lucro<0). Mantém a
    # atribuição de P&L correta no dashboard.
    if reason == "tp2" and exit_price and exit_price > 0 and trade.entry_price and trade.entry_price > 0:
        sign = 1 if trade.side == "long" else -1
        d_be = abs(exit_price - trade.entry_price)
        d_tp2 = abs(exit_price - trade.planned_tp2) if trade.planned_tp2 else float("inf")
        if d_be < d_tp2:
            pnl_dir = (exit_price - trade.entry_price) * sign
            reason = "be" if pnl_dir >= 0 else "stop"
            log.info(
                f"[trade-manager] #{trade.id} reclassificado post_tp1 → {reason} "
                f"(exit={exit_price} entry={trade.entry_price} tp2={trade.planned_tp2})"
            )

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
    Auto-cura: recria pernas de proteção que faltam no DB. Cobre o bug SUI
    (sem TP2) / DOGE (sem SL nem TP) — falhas transitórias no algoOrder durante
    a abertura/transição deixavam a posição parcialmente nua.

    Cobre AS DUAS fases:
      - pre_tp1: SL (no stop original) + TP1 parcial + TP2.
      - post_tp1: SL no breakeven (sl_current_price) + TP2. NÃO recria TP1
        (já foi consumido). Pega o caso de a transição ter criado o SL@BE mas
        o TP2 ter sumido, ou vice-versa.

    Estratégia conservadora (evita ordens duplicadas):
      - "Faltando" = ID None no DB. Com o retry no algoOrder, ID None significa
        de fato que a perna não foi criada. (Não verifica ordem viva na
        exchange — só ausência de criação; cobre o cenário real de falha.)
      - Guarda do TP1 legitimamente pulado: se tp1_order_id é None mas
        tp2_order_id existe, assume skip (qty*0.45 arredondou pra 0) e NÃO
        recria TP1 — senão duplicaria cobertura.

    Retorna True se recriou ao menos uma perna.
    """
    if not PROTECTION_AUTOHEAL_ENABLED:
        return False
    if qty_now <= 0 or trade.phase not in ("pre_tp1", "post_tp1"):
        return False

    # ── Grace pós-abertura: não cura trade recém-criado ──────────────────────
    # A colocação inicial do bracket (confirm-entry / abertura auto) leva alguns
    # segundos pra colocar as 3 pernas E persistir os IDs no DB. Se o tick rodar
    # nessa janela, lê sl_order_id=None e dispara auto-cura → bracket duplicado.
    # Dar uma folga garante que a colocação inicial settle. O dedup_live é a
    # rede principal; isso é cinto + suspensório.
    created = trade.created_at or trade.opened_at
    if created is not None:
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age_s = (datetime.now(timezone.utc) - created).total_seconds()
        if age_s < PROTECTION_HEAL_GRACE_SECONDS:
            return False

    is_post = trade.phase == "post_tp1"
    # SL: em pre_tp1 protege no stop original; em post_tp1 protege no breakeven
    # (sl_current_price setado pela transição, com fallback no entry).
    sl_price = (trade.sl_current_price or trade.entry_price) if is_post else trade.planned_stop

    sl_missing = (not trade.sl_order_id) and bool(sl_price)
    tp2_missing = bool(trade.planned_tp2) and not trade.tp2_order_id
    # TP1 só recria em pre_tp1 (em post_tp1 já foi consumido). "Bracket" só
    # quando há TP2 planejado; skip legítimo: tp2 já existe.
    tp1_missing = (
        (not is_post)
        and bool(trade.planned_tp1) and bool(trade.planned_tp2)
        and not trade.tp1_order_id and not trade.tp2_order_id
    )

    # ── Verificação ao vivo na corretora (perna sumiu apesar de ID no DB) ──
    # openAlgoOrders só devolve ordens ABERTAS → presença = viva. A posição
    # ainda tem qty (qty_now > 0), então se um SL/TP2 com closePosition sumiu,
    # ele NÃO disparou (senão a posição teria fechado) — sumiu de fato.
    sl_vanished = tp2_vanished = tp1_vanished = False
    if PROTECTION_VERIFY_LIVE and (trade.sl_order_id or trade.tp2_order_id or trade.tp1_order_id):
        try:
            from services import exchange_service
            res = await exchange_service.get_open_algo_orders(trade.symbol)
            if res.get("ok"):
                live_ids = {str(o.get("algo_id")) for o in (res.get("orders") or [])}
                if trade.sl_order_id and str(trade.sl_order_id) not in live_ids and bool(sl_price):
                    sl_vanished = True
                if trade.tp2_order_id and str(trade.tp2_order_id) not in live_ids and bool(trade.planned_tp2):
                    tp2_vanished = True
                # TP1 parcial: só conta como sumido se a posição ainda está CHEIA
                # (parcial NÃO bateu). Se a qty já caiu abaixo do limiar de detecção,
                # a ausência do TP1 é execução legítima — não recria (senão duplicaria
                # cobertura e bagunçaria a transição pra post_tp1).
                if (not is_post) and trade.tp1_order_id and bool(trade.planned_tp1):
                    qty_initial = trade.qty_initial or trade.qty or qty_now
                    still_full = qty_now > (qty_initial * _TP1_DETECTED_AT)
                    if still_full and str(trade.tp1_order_id) not in live_ids:
                        tp1_vanished = True
            else:
                # Leitura incerta → não age por sumiço (fail-safe).
                log.info(
                    f"[autoheal] {trade.symbol} #{trade.id} verify-live indisponível "
                    f"({res.get('msg') or res.get('error')}) — só cura por ID-None"
                )
        except Exception as e:
            log.warning(f"[autoheal] {trade.symbol} #{trade.id} verify-live erro: {e}")

    sl_missing = sl_missing or sl_vanished
    tp2_missing = tp2_missing or tp2_vanished
    tp1_missing = tp1_missing or tp1_vanished

    # ── Anti-race do disparo do SL/TP ────────────────────────────────────────
    # Uma perna pode "sumir" do openAlgoOrders porque DISPAROU (a posição está
    # fechando AGORA) — não porque foi deletada. A qty da posição lê com lag
    # entre os endpoints, então o tick que pegou qty_now>0 ainda enxerga a perna
    # sumida e o trade vivo. Sem guarda, o autoheal recria o SL e NOTIFICA bem na
    # hora do stop — o usuário vê "Auto-cura" ANTES do aviso de Loss. Antes de
    # curar uma perna SUMIDA (não por ID-None de abertura), reconfirma a qty: se
    # já zerou, a perna disparou → não cura, deixa o caminho de fechamento agir.
    if sl_vanished or tp2_vanished or tp1_vanished:
        qty_confirm = await _fetch_exchange_qty(trade.symbol)
        if qty_confirm is not None and qty_confirm <= 0:
            log.info(
                f"[autoheal] {trade.symbol} #{trade.id} perna sumida mas posição "
                f"zerou na reconfirmação (qty={qty_confirm}) → disparou, não cura"
            )
            return False

    if not (sl_missing or tp1_missing or tp2_missing):
        return False

    from services import binance_signed_service
    sym = trade.symbol
    entry_side = "Buy" if trade.side == "long" else "Sell"

    log.warning(
        f"[autoheal] {sym} #{trade.id} proteção incompleta — "
        f"sl_missing={sl_missing} tp1_missing={tp1_missing} tp2_missing={tp2_missing} "
        f"(vanished: sl={sl_vanished} tp1={tp1_vanished} tp2={tp2_vanished}) "
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
                stop_loss=sl_price, tp1=None, tp2=None,
                client_order_id_prefix=f"cw-heal-sl-{trade.id}",
                dedup_live=True,
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
        # Em post_tp1 a posição já é o restante (TP1 saiu) → qty_now cobre tudo.
        # Em pre_tp1, se há TP1 parcial, TP2 cobre os 55%. (Com closePosition=true
        # a qty é ignorada de qualquer forma; só importa no fallback reduceOnly.)
        tp1_present = bool(trade.tp1_order_id) or tp1_missing
        qty_tp2 = qty_now if is_post else (qty_now * (1.0 - _TP1_QTY_PCT) if tp1_present else qty_now)
        try:
            r = await binance_signed_service.place_protection_orders(
                sym, entry_side, qty=qty_tp2,
                stop_loss=None, tp1=None, tp2=trade.planned_tp2,
                client_order_id_prefix=f"cw-heal-tp2-{trade.id}",
                dedup_live=True,
            )
            if r.get("tp2_ok") and r.get("tp2_order_id"):
                new_tp2_id = r.get("tp2_order_id")
                healed.append("TP2")
            else:
                failed.append(f"TP2({r.get('tp2_msg')})")
        except Exception as e:
            failed.append(f"TP2({e})")

    # ── TP1 parcial (abertura falhou com SL+TP nus, OU TP1 sumiu da exchange
    #    com a posição ainda cheia = deleção manual sem o parcial ter batido) ─
    if tp1_missing:
        qty_tp1 = qty_now * _TP1_QTY_PCT
        try:
            r = await binance_signed_service.place_protection_orders(
                sym, entry_side, qty=qty_tp1,
                stop_loss=None, tp1=None, tp2=trade.planned_tp1,
                client_order_id_prefix=f"cw-heal-tp1-{trade.id}",
                dedup_live=True,
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
                fresh.sl_current_price = sl_price
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


async def _maybe_pre_tp1_protect(trade: RealTrade, qty_now: float) -> bool:
    """#4 — Em pre_tp1, se o preço já andou >= TRIGGER_FRAC rumo ao TP1, aperta o
    SL pra entry ∓ LOCK_R·R (reduz a perda máxima dessa perna). Age UMA vez (guard
    via sl_current_price já apertado vs planned_stop). Só APERTA. Reusa o padrão
    cria-novo→cancela-antigo. Retorna True se moveu. Fail-soft total."""
    if not PRE_TP1_PROTECT_ENABLED or trade.phase != "pre_tp1":
        return False
    try:
        entry = float(trade.entry_price or 0)
        planned_stop = float(trade.planned_stop or 0)
        planned_tp1 = float(trade.planned_tp1 or 0)
        if entry <= 0 or planned_stop <= 0 or planned_tp1 <= 0:
            return False
        is_long = trade.side == "long"
        R = abs(entry - planned_stop)
        if R <= 0:
            return False

        # Guard idempotência: SL atual já mais apertado que o planned_stop? Já moveu.
        cur_sl = float(trade.sl_current_price or planned_stop)
        already_moved = (cur_sl > planned_stop + 1e-12) if is_long else (cur_sl < planned_stop - 1e-12)
        if already_moved:
            return False

        # Progresso rumo ao TP1 pelo mark price atual.
        mark = await _fetch_mark_price(trade.symbol)
        if mark is None or mark <= 0:
            return False
        denom = (planned_tp1 - entry) if is_long else (entry - planned_tp1)
        if denom <= 0:
            return False
        progress = ((mark - entry) if is_long else (entry - mark)) / denom
        if progress < PRE_TP1_PROTECT_TRIGGER_FRAC:
            return False

        # Novo SL: entry ∓ LOCK_R·R. Só aplica se for MAIS APERTADO que o atual e
        # não passar do entry (não vira lucro travado aqui — isso é o pós-TP1).
        new_sl = (entry - PRE_TP1_PROTECT_LOCK_R * R) if is_long else (entry + PRE_TP1_PROTECT_LOCK_R * R)
        tighter = (new_sl > cur_sl + 1e-12) if is_long else (new_sl < cur_sl - 1e-12)
        if not tighter:
            return False
        # Trava de segurança: nunca acima/abaixo do entry (mantém em pre-BE).
        if is_long and new_sl >= entry:
            new_sl = entry - 1e-9
        if (not is_long) and new_sl <= entry:
            new_sl = entry + 1e-9

        from services import exchange_service, binance_signed_service
        entry_side = "Buy" if is_long else "Sell"
        prot = await binance_signed_service.place_protection_orders(
            trade.symbol, entry_side, qty=qty_now,
            stop_loss=new_sl, tp1=None, tp2=None,
            client_order_id_prefix=f"cw-pre-{trade.id}",
        )
        if not prot.get("sl_ok"):
            log.warning(
                f"[pre-tp1-protect] {trade.symbol} #{trade.id} novo SL falhou: "
                f"{prot.get('sl_msg')} — mantém SL atual"
            )
            return False
        new_sl_id = prot.get("sl_order_id")
        # Cancela SL antigo só depois de ter cobertura nova.
        if trade.sl_order_id:
            try:
                await exchange_service.cancel_algo_order(trade.sl_order_id)
            except Exception as e:
                log.warning(f"[pre-tp1-protect] cancel SL antigo #{trade.id}: {e}")
        async with get_session() as session:
            fresh = (await session.execute(
                select(RealTrade).where(RealTrade.id == trade.id)
            )).scalar_one_or_none()
            if fresh:
                fresh.sl_order_id = new_sl_id
                fresh.sl_current_price = new_sl
                await session.commit()
                trade.sl_order_id = new_sl_id
                trade.sl_current_price = new_sl
        log.info(
            f"[pre-tp1-protect] {trade.symbol} #{trade.id} progress {progress*100:.0f}% "
            f"rumo ao TP1 → SL {cur_sl} → {new_sl} (entry∓{PRE_TP1_PROTECT_LOCK_R}R)"
        )
        return True
    except Exception as e:
        log.warning(f"[pre-tp1-protect] #{trade.id} falhou (fail-soft): {e}")
        return False


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

        # ── #4 Proteção pré-TP1: aperta SL quando andou rumo ao TP1 (default OFF)
        try:
            await _maybe_pre_tp1_protect(trade, qty_now)
        except Exception as e:
            log.warning(f"[pre-tp1-protect] check #{trade.id} erro: {e}")

    qty_initial = trade.qty_initial or trade.qty

    # ── Poeira: residual minúsculo (sobra de stepSize em fechamentos sem
    # closePosition) trava o trade aberto. Se o notional do residual < limiar,
    # tenta zerar a mercado e trata como fechado. ──────────────────────────
    if qty_now > 0:
        ref_price = float(trade.sl_current_price or trade.planned_stop or trade.entry_price or 0)
        residual_notional = qty_now * ref_price
        if 0 < residual_notional < DUST_NOTIONAL_USD:
            log.info(
                f"[trade-manager] {trade.symbol} #{trade.id} POEIRA qty={qty_now} "
                f"(~${residual_notional:.2f}) — zerando e fechando"
            )
            try:
                from services import exchange_service
                close_side = "Sell" if trade.side == "long" else "Buy"
                await exchange_service.place_order(
                    symbol=trade.symbol, side=close_side, qty=qty_now,
                    order_type="Market", reduce_only=True,
                )
            except Exception as e:
                log.warning(f"[trade-manager] flatten poeira #{trade.id} falhou: {e}")
            await _close_trade(trade, "tp2" if trade.phase == "post_tp1" else "stop")
            return

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


# ══════════════════════════════════════════════════════════════════════════
# Monitor ADVISE-ONLY de trades MANUAIS (v1 pré go-live)
# ══════════════════════════════════════════════════════════════════════════
# Trades source="manual" rodam na MESMA conta demo (em moedas diferentes das
# do bot). O bot NÃO cria nem mexe em NENHUMA ordem dessas posições — só
# observa e aconselha:
#   - preço tocou TP1   → sugere realizar parcial e subir SL pro BE (seu entry)
#   - preço tocou TP2   → sugere fechar a posição
#   - posição sumiu     → marca o trade fechado (classifica por proximidade)
#
# "TP1 já avisado" persiste no DB via phase pre_tp1 → post_tp1 (sobrevive
# restart). "TP2 já avisado" fica em memória (re-push no máx. 1x após restart —
# aceitável). Toggle: MANUAL_MONITOR_ENABLED env (default "true").
MANUAL_MONITOR_ENABLED = os.getenv("MANUAL_MONITOR_ENABLED", "true").strip().lower() in ("1", "true", "yes")
_MANUAL_TP2_ADVISED: set[int] = set()


def _hit(side: str, price: float, level: float, kind: str) -> bool:
    """price cruzou `level`? kind='tp' (alvo) | 'sl' (stop).
    long: TP acima (price≥level), SL abaixo (price≤level). short: invertido."""
    if not level or level <= 0:
        return False
    is_long = side == "long"
    if kind == "tp":
        return price >= level if is_long else price <= level
    # sl
    return price <= level if is_long else price >= level


async def _fetch_mark_price(symbol: str) -> float | None:
    try:
        from services.binance_service import fetch_ticker
        t = await fetch_ticker(symbol)
        last = float(t.get("last") or 0) if isinstance(t, dict) else 0.0
        return last or None
    except Exception as e:
        log.warning(f"[manual-monitor] preço {symbol} falhou: {e}")
        return None


async def _advise(trade: RealTrade, title: str, body: str, event_type: str) -> None:
    """Envia conselho (Telegram) sobre trade manual — sem tocar em ordem."""
    try:
        from services.notification_service import send_telegram
        await send_telegram(f"{title}\n{body}", event_type=event_type)
        log.info(f"[manual-monitor] advise #{trade.id} {trade.symbol}: {title}")
    except Exception as e:
        log.warning(f"[manual-monitor] advise #{trade.id} falhou: {e}")


async def _close_manual_trade(trade: RealTrade) -> None:
    """Posição manual sumiu da conta → marca fechado. NÃO cancela ordens
    (não são nossas). Classifica por proximidade do preço de saída aos níveis."""
    from services import real_trade_service, exchange_service
    sym_short = (trade.symbol or "").split("/")[0]

    # Preço de saída real: último fill no lado de fechamento
    exit_price = None
    try:
        close_side = "SELL" if trade.side == "long" else "BUY"
        ex = await exchange_service.get_executions(trade.symbol, limit=20)
        if ex.get("ok"):
            fills = [
                f for f in (ex.get("fills") or [])
                if str(f.get("side", "")).upper() == close_side and float(f.get("price") or 0) > 0
            ]
            if fills:
                exit_price = float(max(fills, key=lambda f: int(f.get("time") or 0))["price"])
    except Exception as e:
        log.warning(f"[manual-monitor] exit fill #{trade.id} falhou: {e}")

    # Classifica por proximidade aos níveis planejados
    reason = "manual"
    if exit_price and trade.entry_price:
        dists: dict[str, float] = {"be": abs(exit_price - trade.entry_price)}
        if trade.planned_tp2:
            dists["tp2"] = abs(exit_price - trade.planned_tp2)
        if trade.planned_tp1:
            dists["tp1"] = abs(exit_price - trade.planned_tp1)
        if trade.planned_stop:
            dists["stop"] = abs(exit_price - trade.planned_stop)
        reason = min(dists, key=lambda k: dists[k])

    status_map = {"tp2": "closed_tp2", "tp1": "closed_tp1", "stop": "closed_stop", "be": "closed_be"}
    status = status_map.get(reason, "closed_manual")
    if not exit_price or exit_price <= 0:
        exit_price = trade.entry_price

    try:
        result = await real_trade_service.close_trade(
            trade.id, exit_price=exit_price, status=status,
            notes="manual-monitor: posição fechada na conta",
        )
        pnl = (result or {}).get("pnl_usd")
        pnl_txt = f" · {pnl:+.2f} USD" if isinstance(pnl, (int, float)) else ""
        log.info(f"[manual-monitor] CLOSE #{trade.id} {trade.symbol} → {status} @ {exit_price}{pnl_txt}")
        await _advise(
            trade,
            f"✅ Fechado · {sym_short} {trade.side.upper()}",
            f"Posição manual encerrada @ {exit_price} ({status.replace('closed_', '').upper()}){pnl_txt}.",
            "close",
        )
    except Exception as e:
        log.error(f"[manual-monitor] close_trade #{trade.id} erro: {e}")


async def _process_manual_trade(trade: RealTrade) -> None:
    """Observa um trade manual e aconselha. Nunca cria/cancela ordem."""
    sym = trade.symbol
    sym_short = (sym or "").split("/")[0]
    qty_now, entry_real = await _fetch_exchange_position(sym)

    # Posição sumiu da conta (user fechou, ou bateu SL/TP na corretora) → fecha
    if qty_now is not None and qty_now <= 0:
        await _close_manual_trade(trade)
        _MANUAL_TP2_ADVISED.discard(trade.id)
        return

    price = await _fetch_mark_price(sym)
    if not price:
        return

    side = trade.side
    entry = entry_real or trade.entry_price

    # TP1 tocado → avisa 1x (persiste via phase pre_tp1 → post_tp1)
    if trade.phase != "post_tp1" and _hit(side, price, trade.planned_tp1 or 0, "tp"):
        await _advise(
            trade,
            f"🎯 TP1 tocado · {sym_short} {side.upper()}",
            f"Preço {price} atingiu o TP1 {trade.planned_tp1}. Sugiro realizar a "
            f"parcial e subir o SL pro seu break-even (entrada {entry}).",
            "tp1",
        )
        async with get_session() as session:
            fresh = (await session.execute(
                select(RealTrade).where(RealTrade.id == trade.id)
            )).scalar_one_or_none()
            if fresh:
                fresh.phase = "post_tp1"
                fresh.sl_current_price = entry
                fresh.updated_at = datetime.now(timezone.utc)
                await session.commit()
        return

    # TP2 tocado → avisa 1x (em memória)
    if trade.id not in _MANUAL_TP2_ADVISED and _hit(side, price, trade.planned_tp2 or 0, "tp"):
        await _advise(
            trade,
            f"🚀 TP2 tocado · {sym_short} {side.upper()}",
            f"Preço {price} atingiu o TP2 {trade.planned_tp2}. Considere fechar a posição.",
            "tp2",
        )
        _MANUAL_TP2_ADVISED.add(trade.id)


async def _tick() -> None:
    """Uma iteração do loop: processa trades open auto (gestão ativa) e
    manuais (monitor advise-only, sem tocar em ordem)."""
    if not DB_ENABLED:
        return
    # "auto" (trades do bot) e "managed" (entradas manuais que o bot gerencia:
    # coloca bracket + move SL pro BE pós-TP1) seguem o MESMO lifecycle ativo.
    async with get_session() as session:
        stmt = (
            select(RealTrade)
            .where(RealTrade.status == "open")
            .where(RealTrade.source.in_(["auto", "managed"]))
        )
        trades = (await session.execute(stmt)).scalars().all()

    for t in trades:
        try:
            await _process_trade(t)
        except Exception as e:
            log.warning(f"[trade-manager] processar #{t.id} {t.symbol} erro: {e}", exc_info=True)

    # ── Monitor advise-only de trades manuais puros (source="manual", sem
    #    bracket do bot — fluxo shadow). "managed" NÃO entra aqui (já é tratado
    #    no lifecycle ativo acima). ──
    if MANUAL_MONITOR_ENABLED:
        async with get_session() as session:
            manual = (await session.execute(
                select(RealTrade)
                .where(RealTrade.status == "open")
                .where(RealTrade.source == "manual")
            )).scalars().all()
        for t in manual:
            try:
                await _process_manual_trade(t)
            except Exception as e:
                log.warning(f"[manual-monitor] processar #{t.id} {t.symbol} erro: {e}", exc_info=True)


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
            .where(RealTrade.source.in_(["auto", "managed"]))
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
                dedup_live=True,
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
                if entry_real and entry_real > 0:
                    from services import real_trade_service
                    if not fresh.entry_price or fresh.entry_price <= 0:
                        fresh.entry_price = entry_real
                    await real_trade_service.recompute_entry_slippage(session, fresh, fill_price=entry_real)
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
            .where(RealTrade.source.in_(["auto", "managed"]))
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
