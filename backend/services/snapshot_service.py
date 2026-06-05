"""
Snapshot Service — salva recomendações ao serem geradas, monitora outcome
(stop/tp atingido) e agrega P&L diário.

Como funciona:
- Quando o frontend recebe um lote de recomendações via /recommendations-batch,
  o backend persiste cada uma com `status="open"`.
- Periodicamente (a cada 5 min), um job checa o preço atual dos snapshots
  abertos e marca won_tp1/won_tp2/lost conforme a barreira tocada.
- "Expired": se passar 48h sem hit, marca expired (não conta no P&L).
- Snapshot é desduplicado por (symbol, timeframe, direction, entry) dentro de
  uma janela de 2h pra não inflar com a varredura rodando a cada 2 min.
"""
from __future__ import annotations
import logging
import os
from datetime import datetime, timedelta, timezone, date
from typing import List, Dict, Any, Optional

from sqlalchemy import select, and_, func, update

from db import DB_ENABLED, get_session
from models.recommendation_snapshot import RecommendationSnapshot
from services.push_service import notify_outcome

log = logging.getLogger(__name__)

# ── Configuração ─────────────────────────────────────────────────────────
DEDUP_WINDOW_HOURS = 2       # mesma rec não entra 2× nesse intervalo
EXPIRY_HOURS = 48            # ceiling absoluto — qualquer trade > 48h é encerrado

# 1 rec por SÍMBOLO (não só por símbolo+direção): bloqueia gerar uma rec
# na direção OPOSTA enquanto já há uma aberta no mesmo símbolo. Evita o
# absurdo de ter long e short simultâneos na mesma moeda (sinais contraditórios
# poluindo o painel). Default ligado; desligue com ONE_REC_PER_SYMBOL=false.
ONE_REC_PER_SYMBOL = os.getenv("ONE_REC_PER_SYMBOL", "true").strip().lower() in ("1", "true", "yes")

# Time-stop por timeframe: se TP1 não bater em N candles, encerra o trade
# (evita "morte lenta" que trava capital sem stop nem TP). Cada TF tem
# horizonte próprio — scalp deve resolver em horas, swing aguenta dias.
# Valor = horas máximas SEM tocar TP1. Se TP1 já tocou, NÃO aplica
# (trade está em lucro, deixa o trail/TP2 cuidar).
TIME_STOP_HOURS_BY_TF = {
    "1m": 1, "3m": 2, "5m": 3, "15m": 4, "30m": 8,
    "1h": 12, "2h": 24, "4h": 36, "6h": 48,
    "8h": 48, "12h": 48, "1d": 48,  # cappado pelo EXPIRY_HOURS
}


def _time_stop_hours(tf: str) -> float:
    """Horas máximas sem tocar TP1 antes de encerrar por time-stop."""
    return float(TIME_STOP_HOURS_BY_TF.get(tf, EXPIRY_HOURS))
# ── R esperado (Step 2b v2: parcial 50% no TP1 + BE+ assimétrico) ────────
# Premissa: ao tocar TP1, fecha 50% da posição (+1R em metade), restante segue
# com stop EFETIVO em "BE+" (entry + 0.2R lock) e trail por ATR mais largo.
# Mudança vs v1: trail era 1.5×ATR e piso em entry puro. Resultado: 55% dos
# TP1-hits viravam won_tp1_be (+0.5R) em vez de won_tp2 (+1.5R). Diagnóstico
# via /api/debug/status-distribution em 2026-05 mostrou 23 won_tp1_be vs 19
# won_tp2 nos últimos 30 dias. Conclusão: trail apertado demais. Agora:
#   • Trail mais largo (K=2.2) → dá respiro pra retração normal
#   • Buffer mínimo (peak precisa avançar ≥0.5×ATR além do TP1 pra trail ativar)
#   • Piso = BE+ lock (0.2R) → ainda garante lucro mínimo
# R reportado é média ponderada das duas metades:
#   • Parcial sai em TP1 (+1R) → metade = +0.5R
#   • Final em TP2 (+2R)       → metade = +1.0R  → total +1.5R (won_tp2)
#   • Final em BE+ lock        → metade = +0.2R  → total +0.6R (won_tp1_be)
#   • Final expira após TP1    → conservador     → total +0.6R (won_tp1)
# Stop original (antes de TP1) = -1R cheio (não houve parcial).
REALIZED_R_TP1 = 0.6           # 50% TP1 (+1R) + 50% BE+lock (+0.2R) = +0.6R
REALIZED_R_TP2 = 1.5           # 50% TP1 + 50% TP2
REALIZED_R_STOP = -1.0         # stop original (antes de TP1)
REALIZED_R_BREAKEVEN = 0.6     # 50% TP1 (+1R) + 50% BE+lock (+0.2R) = +0.6R

# Trail por ATR após TP1 hit (v2 — mais largo).
ATR_TRAIL_K = 2.2

# BE+ lock: parcela do range (tp1-entry) que vira piso do stop após TP1.
# 0.2 = 20% do caminho do entry até o TP1, garantindo +0.2R no pior caso.
BE_PLUS_LOCK_R = 0.2

# Buffer antes do trail por ATR ativar: peak precisa avançar ≥ esse múltiplo
# do ATR ALÉM do TP1 antes do trail dinâmico começar a apertar. Evita o
# whipsaw imediato pós-TP1 (vela seguinte retrai e mata posição).
TRAIL_ACTIVATION_BUFFER_ATR = 0.5


def _extract_features(rec: Dict[str, Any], created_at: datetime) -> Dict[str, Any]:
    """Captura vetor de features pro learning loop. Robust a campos ausentes."""
    sig = rec.get("signal") or {}
    if not isinstance(sig, dict):
        return {"hour_utc": created_at.hour, "day_of_week": created_at.weekday()}

    ind = sig.get("indicators") or {}
    mtf = sig.get("mtf") or {}
    confluence = sig.get("confluence") or {}
    derivatives = sig.get("derivatives") or {}
    patterns = sig.get("patterns") or []

    # Padrões: lista de strings
    pattern_types = []
    if isinstance(patterns, list):
        for p in patterns:
            if isinstance(p, dict):
                t = p.get("type")
                if t:
                    pattern_types.append(t)

    # ATR como % do entry (medida de volatilidade)
    atr = ind.get("atr")
    entry = sig.get("entry") or rec.get("entry") or 0
    atr_pct = None
    if atr and entry:
        try:
            atr_pct = round((float(atr) / float(entry)) * 100, 3)
        except Exception:
            atr_pct = None

    return {
        "rsi": ind.get("rsi"),
        "adx": ind.get("adx"),
        "atr_pct": atr_pct,
        "mtf_score": mtf.get("alignment_score") if mtf else None,
        "mtf_aligned": mtf.get("aligned_count") if mtf else None,
        "confluence_pct": confluence.get("pct") if confluence else None,
        "patterns": pattern_types,
        "funding_pct": derivatives.get("funding_rate_pct") if derivatives else None,
        "funding_sentiment": derivatives.get("funding_sentiment") if derivatives else None,
        "oi_change_pct": derivatives.get("oi_change_24h_pct") if derivatives else None,
        "hour_utc": created_at.hour,
        "day_of_week": created_at.weekday(),    # 0 = Monday
    }


def _tf_rank(tf: str) -> int:
    """Rank do timeframe pra comparar 'tamanho' (SCALP < DAY < SWING).
    Maior número = TF maior. Usado pra bloquear duplicação intra-direção.
    Sincronizar com frontend/DailyPnLPanel.operationType()."""
    if not tf:
        return 0
    t = tf.strip().lower()
    if t in ("1m", "3m", "5m", "15m"):
        return 1  # SCALP
    if t in ("30m", "1h", "2h"):
        return 2  # DAY
    return 3      # SWING (4h, 1d, 1w, etc)


async def _has_open_opposite_real_trade(session, symbol: str, direction: str) -> bool:
    """True se existe RealTrade auto OPEN no símbolo em direção OPOSTA.

    Usado por save_recommendations rule (b): quando há um trade real oposto
    aberto, a rec oposta NÃO deve ser bloqueada — ela precisa fluir até o
    avaliador de flip (Fase 2) no shadow_trade_service. Bloquear aqui mataria
    o auto-flip. Quando NÃO há trade real oposto, o ONE_REC_PER_SYMBOL segue
    valendo (evita long+short simultâneo só de snapshots informativos)."""
    try:
        from models.real_trade import RealTrade
        opposite_side = "long" if direction == "short" else "short"
        stmt = select(RealTrade.id).where(
            and_(
                RealTrade.symbol == symbol,
                RealTrade.status == "open",
                RealTrade.source == "auto",
                RealTrade.side == opposite_side,
            )
        ).limit(1)
        return (await session.execute(stmt)).scalar_one_or_none() is not None
    except Exception as e:
        log.warning(f"[snapshot] check trade real oposto falhou {symbol}: {e}")
        return False


async def save_recommendations(recommendations: List[Dict[str, Any]]) -> int:
    """
    Salva snapshots de recomendações novas (desduplicadas).
    Retorna quantos foram efetivamente inseridos.

    Side-effect: marca cada rec com `_just_saved=True` se foi inserida nesta
    chamada (não duplicata). Permite ao caller filtrar quais notificar via
    push sem precisar re-consultar o DB.

    Regras de bloqueio (Fase 1 — 1 rec ativa por símbolo+direção):
      1. Dedup janela curta: mesmo (symbol, tf, direction) recente → skip
      2. Anti-duplicação intra-direção: se já existe snapshot OPEN com
         (symbol, direction) MESMO TF ou TF MENOR → skip. Só permite
         se a nova rec é TF MAIOR (upgrade — Fase 3 trata a transição).
    """
    if not DB_ENABLED or not recommendations:
        return 0

    inserted = 0
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=DEDUP_WINDOW_HOURS)

    async with get_session() as session:
        for rec in recommendations:
            try:
                # Dedup: existe registro recente do mesmo setup?
                stmt = select(RecommendationSnapshot.id).where(
                    and_(
                        RecommendationSnapshot.symbol == rec["symbol"],
                        RecommendationSnapshot.timeframe == rec["timeframe"],
                        RecommendationSnapshot.direction == rec["direction"],
                        RecommendationSnapshot.created_at >= cutoff,
                    )
                ).limit(1)
                existing = (await session.execute(stmt)).scalar_one_or_none()
                if existing:
                    rec["_just_saved"] = False
                    continue

                # Fase 1: bloqueia rec adicional pro mesmo (symbol, direction)
                # se já houver snapshot OPEN em TF igual ou MENOR. Permite
                # passar quando a nova é TF MAIOR — vira candidato a upgrade.
                # Busca TODOS os abertos do símbolo (qualquer direção) pra também
                # aplicar o "1 rec por símbolo" (bloqueia direção oposta).
                new_rank = _tf_rank(rec.get("timeframe"))
                open_stmt = select(
                    RecommendationSnapshot.id,
                    RecommendationSnapshot.timeframe,
                    RecommendationSnapshot.direction,
                ).where(
                    and_(
                        RecommendationSnapshot.symbol == rec["symbol"],
                        RecommendationSnapshot.status == "open",
                    )
                )
                open_rows = (await session.execute(open_stmt)).all()
                same_dir_rows = [r for r in open_rows if r.direction == rec["direction"]]
                opp_dir_rows = [r for r in open_rows if r.direction != rec["direction"]]

                # (a) Mesma direção: se qualquer aberto tem rank >= novo →
                # bloqueia (mesmo TF ou maior já cobre). Só passa se TODOS os
                # abertos forem TF MENOR que o novo (upgrade legítimo).
                if any(_tf_rank(r.timeframe) >= new_rank for r in same_dir_rows):
                    rec["_just_saved"] = False
                    log.debug(
                        f"[snapshot] skip rec {rec['symbol']} {rec['direction']} "
                        f"tf={rec.get('timeframe')} — já há OPEN em TF >= nesse par"
                    )
                    continue

                # (b) 1 rec por símbolo: bloqueia direção OPOSTA enquanto há
                # uma aberta no mesmo símbolo (evita long+short simultâneo).
                # EXCEÇÃO: se há um TRADE REAL aberto na direção oposta, a rec
                # oposta DEVE fluir (não bloquear) pra alimentar o avaliador de
                # flip (Fase 2) no shadow_trade_service. Bloquear aqui setaria
                # _just_saved=False e o flip nunca seria avaliado.
                if ONE_REC_PER_SYMBOL and opp_dir_rows:
                    if await _has_open_opposite_real_trade(
                        session, rec["symbol"], rec["direction"]
                    ):
                        log.debug(
                            f"[snapshot] rec OPOSTA {rec['symbol']} {rec['direction']} "
                            f"tf={rec.get('timeframe')} FLUI — há trade real oposto aberto "
                            f"(candidato a flip)"
                        )
                        # não dá continue — segue pro insert abaixo (flip pipeline)
                    else:
                        rec["_just_saved"] = False
                        log.debug(
                            f"[snapshot] skip rec {rec['symbol']} {rec['direction']} "
                            f"tf={rec.get('timeframe')} — já há OPEN na direção oposta "
                            f"(ONE_REC_PER_SYMBOL, sem trade real)"
                        )
                        continue

                # tp1 pode estar em signal.tp1 ou ausente
                tp1 = None
                sig = rec.get("signal") or {}
                if isinstance(sig, dict):
                    tp1 = sig.get("tp1")

                snap = RecommendationSnapshot(
                    symbol=rec["symbol"],
                    timeframe=rec["timeframe"],
                    tier=rec["tier"],
                    direction=rec["direction"],
                    entry=float(rec["entry"]),
                    stop_loss=float(rec["stop_loss"]),
                    tp1=float(tp1) if tp1 is not None else None,
                    tp2=float(rec["tp2"]),
                    score=float(rec["score"]),
                    risk_reward=float(rec["risk_reward"]),
                    leverage=int(rec.get("leverage", 1)),
                    risk_pct=float(rec.get("risk_pct", 1.0)),
                    stop_distance_pct=float(rec.get("stop_distance_pct", 0.0)),
                    status="open",
                    created_at=now,
                    features=_extract_features(rec, now),
                )
                session.add(snap)
                rec["_just_saved"] = True
                inserted += 1
            except Exception as e:
                log.warning(f"Falha ao salvar snapshot {rec.get('symbol')}: {e}")
        await session.commit()

    if inserted:
        log.info(f"Snapshots persistidos: {inserted}")
    return inserted


async def expire_open_snapshot(symbol: str, direction: str, reason: str = "flip_advisory") -> int:
    """Marca como 'expired' os snapshots OPEN de (symbol, direction).

    Chamado quando o flip é REJEITADO (advisory): a rec oposta foi deixada
    fluir por save_recommendations (pra alimentar o avaliador de flip), mas
    como o flip não disparou, não queremos poluir o painel 'Abertos' com um
    par long+short do mesmo símbolo. Expira a rec oposta — sem afetar o trade
    real, que continua aberto. Retorna quantos snapshots foram expirados."""
    if not DB_ENABLED:
        return 0
    try:
        async with get_session() as session:
            stmt = (
                update(RecommendationSnapshot)
                .where(
                    and_(
                        RecommendationSnapshot.symbol == symbol,
                        RecommendationSnapshot.direction == direction,
                        RecommendationSnapshot.status == "open",
                    )
                )
                .values(status="expired", outcome_at=datetime.now(timezone.utc))
            )
            res = await session.execute(stmt)
            await session.commit()
            n = res.rowcount or 0
            if n:
                log.debug(
                    f"[snapshot] expirado {n} snapshot(s) {symbol} {direction} ({reason})"
                )
            return n
    except Exception as e:
        log.warning(f"[snapshot] expire_open_snapshot falhou {symbol} {direction}: {e}")
        return 0


def _atr_abs(snap: RecommendationSnapshot) -> Optional[float]:
    """ATR absoluto do setup, derivado das features (atr_pct × entry)."""
    feats = snap.features or {}
    atr_pct = feats.get("atr_pct")
    if atr_pct is None or snap.entry is None:
        return None
    try:
        return float(atr_pct) / 100.0 * float(snap.entry)
    except Exception:
        return None


def _classify_outcome_candles(snap: RecommendationSnapshot, df_window) -> Optional[tuple]:
    """
    Steps 2a+2b v2: processa candles em ordem cronológica.

    Lógica (v2 — BE+ assimétrico + buffer):
      • Se TP1 toca → fecha 50% (+1R parcial). Stop EFETIVO da metade restante
        vira BE+ lock (entry + 0.2×(tp1-entry)) = piso garantindo +0.2R.
      • Trail por ATR (K=2.2) só "aperta" o stop acima do BE+ se o peak
        avançou ≥0.5×ATR além do TP1 (buffer de ativação).
      • Se TP2 toca a qualquer momento → fecha como won_tp2 (+1.5R total).
      • Se stop ORIGINAL bate antes de TP1 → lost (-1R, posição cheia).
      • Se stop EFETIVO (BE+/trail) bate APÓS TP1 → won_tp1_be (+0.6R).

    Retorna uma das opções:
      ("won_tp2", price, +1.5, tp1_just_hit_bool, new_peak)   → lucro máximo
      ("won_tp1_be", price, +0.5, tp1_just_hit_bool, new_peak) → trail/BE
      ("lost", stop_loss, -1.0, False, None)                   → stop original
      ("open_after_tp1", None, None, True, new_peak)           → segue aberto
      ("open_update", None, None, False, new_peak)             → só atualiza peak
      None                                                      → segue aberto

    `new_peak` é o pico do preço a favor desde TP1 hit (None se ainda não houve
    TP1). O caller persiste em snap.peak_price_since_tp1.

    Regra conservadora: na MESMA vela, stop tem prioridade SE TP1 ainda não
    foi marcado em rounds anteriores.
    """
    if df_window is None or df_window.empty:
        return None

    is_long = snap.direction == "long"
    tp1_already = snap.tp1_hit_at is not None
    tp1_hit_now = False
    peak = snap.peak_price_since_tp1  # pode ser None
    atr = _atr_abs(snap)  # pode ser None — sem ATR, vira só BE puro

    for _, c in df_window.iterrows():
        h = float(c["high"])
        l = float(c["low"])
        cl = float(c["close"])

        # Atualiza peak se TP1 já foi (passado ou agora)
        if tp1_already or tp1_hit_now:
            cand_peak = h if is_long else l
            if peak is None:
                peak = cand_peak
            else:
                peak = max(peak, cand_peak) if is_long else min(peak, cand_peak)

        # Stop efetivo (v2 — BE+ assimétrico + trail com buffer de ativação):
        #   • Antes de TP1: stop original
        #   • Após TP1: PISO = BE+ lock (entry + 0.2 × (tp1-entry)) — garante
        #     mínimo +0.2R no pior caso. Trail dinâmico só "aperta" o stop
        #     ACIMA do BE+ se peak avançou ≥0.5×ATR além do TP1 (buffer).
        if tp1_already or tp1_hit_now:
            if snap.tp1 is not None:
                tp1_distance = snap.tp1 - snap.entry  # signed
                be_plus = snap.entry + BE_PLUS_LOCK_R * tp1_distance
            else:
                be_plus = snap.entry
            if atr is not None and peak is not None:
                # Trail só ativa depois do peak avançar além do buffer
                if is_long:
                    activation_threshold = (snap.tp1 or snap.entry) + TRAIL_ACTIVATION_BUFFER_ATR * atr
                    if peak >= activation_threshold:
                        trail = peak - ATR_TRAIL_K * atr
                        effective_stop = max(be_plus, trail)
                    else:
                        effective_stop = be_plus
                else:
                    activation_threshold = (snap.tp1 or snap.entry) - TRAIL_ACTIVATION_BUFFER_ATR * atr
                    if peak <= activation_threshold:
                        trail = peak + ATR_TRAIL_K * atr
                        effective_stop = min(be_plus, trail)
                    else:
                        effective_stop = be_plus
            else:
                effective_stop = be_plus
        else:
            effective_stop = snap.stop_loss

        # Stop ORIGINAL (pre-TP1) é close-based: só conta loss se a vela
        # FECHAR além do stop. Wick que toca e volta NÃO é stop. Em cripto,
        # wicks de 0.3-1% em pivôs são rotineiros — usar low/high pra stop
        # vira hunt de liquidez.
        # Stop POST-TP1 (BE+ / trail) permanece wick-based: protege lucro,
        # comportamento de stop order real em exchange.
        post_tp1 = tp1_already or tp1_hit_now
        if is_long:
            if post_tp1:
                stop_hit = l <= effective_stop
            else:
                stop_hit = cl <= effective_stop
            tp1_hit = (snap.tp1 is not None) and (h >= snap.tp1)
            tp2_hit = h >= snap.tp2
        else:
            if post_tp1:
                stop_hit = h >= effective_stop
            else:
                stop_hit = cl >= effective_stop
            tp1_hit = (snap.tp1 is not None) and (l <= snap.tp1)
            tp2_hit = l <= snap.tp2

        # Stop original antes de TP1 → loss cheio (worst-case)
        if stop_hit and not (tp1_already or tp1_hit_now):
            return ("lost", snap.stop_loss, REALIZED_R_STOP, False, None)

        # TP2 a qualquer momento → won_tp2 (lucro max). Se TP1 e TP2 batem
        # na MESMA vela sem TP1 prévio, ambos efeitos contam.
        if tp2_hit:
            # Garante que peak refletiu o evento se TP1 foi marcado agora
            if (tp1_hit and not tp1_already) or tp1_hit_now:
                tp1_hit_now = True
                if atr is not None:
                    cand_peak = h if is_long else l
                    peak = cand_peak if peak is None else (max(peak, cand_peak) if is_long else min(peak, cand_peak))
            return ("won_tp2", snap.tp2, REALIZED_R_TP2, tp1_hit_now, peak)

        # TP1 acabou de bater nesta vela (e não tinha batido antes)
        if tp1_hit and not (tp1_already or tp1_hit_now):
            tp1_hit_now = True
            # Inicializa peak com o high (long) / low (short) da vela
            cand_peak = h if is_long else l
            peak = cand_peak if peak is None else (max(peak, cand_peak) if is_long else min(peak, cand_peak))
            # Continua iterando — pode bater TP2 ou stop trail nesta janela
            continue

        # Stop trail/BE bate após TP1 já estar marcado
        if stop_hit and (tp1_already or tp1_hit_now):
            # Saída efetiva é o stop trail/BE
            return ("won_tp1_be", effective_stop, REALIZED_R_BREAKEVEN, tp1_hit_now, peak)

    # Não fechou. Se TP1 acabou de ser tocado, sinaliza com peak novo.
    if tp1_hit_now:
        return ("open_after_tp1", None, None, True, peak)
    # Se TP1 já era hit no passado, peak pode ter mudado — sinaliza update
    if tp1_already and peak != snap.peak_price_since_tp1:
        return ("open_update", None, None, False, peak)
    return None


async def check_open_snapshots() -> int:
    """
    Roda periodicamente. Busca todos abertos, consulta preço high/low desde
    last_check_at (ou created_at), classifica outcome.

    Retorna quantos snapshots foram resolvidos nesta chamada.
    """
    if not DB_ENABLED:
        return 0

    # Import lazy pra evitar ciclo
    from services.binance_service import fetch_ohlcv

    resolved = 0
    now = datetime.now(timezone.utc)

    async with get_session() as session:
        stmt = select(RecommendationSnapshot).where(
            RecommendationSnapshot.status == "open"
        )
        result = await session.execute(stmt)
        open_snaps = result.scalars().all()

        for snap in open_snaps:
            try:
                # Time-stop por TF: se TP1 NÃO tocou ainda E passou o limite
                # do TF (ex: 4h pro 15m), encerra como expired (0R, sem perda
                # nem ganho). Evita capital travado em trade que não anda.
                # Se TP1 já tocou, ignora time-stop (deixa trail/TP2 cuidar).
                age = now - snap.created_at
                tf_limit_h = _time_stop_hours(snap.timeframe)
                if snap.tp1_hit_at is None and age > timedelta(hours=tf_limit_h):
                    snap.status = "expired"
                    snap.realized_r = 0.0
                    snap.outcome_at = now
                    log.info(
                        f"[time-stop] {snap.symbol} {snap.timeframe} {snap.direction} "
                        f"expirado: {age.total_seconds()/3600:.1f}h sem TP1 "
                        f"(limite {tf_limit_h}h)"
                    )
                    resolved += 1
                    continue

                # Ceiling absoluto (48h): independente de TF, fecha.
                # Step 2a: se TP1 já tinha sido tocado, expira como won_tp1 (+0.5R)
                # — lucro parcial travado. Caso contrário, expired (0R).
                if age > timedelta(hours=EXPIRY_HOURS):
                    if snap.tp1_hit_at is not None:
                        snap.status = "won_tp1"
                        snap.outcome_price = snap.tp1
                        snap.realized_r = REALIZED_R_TP1
                        # Push: expirou pós-TP1, parcial travada.
                        try:
                            await notify_outcome(snap, "expired_tp1")
                        except Exception as e:
                            log.warning(f"notify_outcome expired_tp1 falhou: {e}")
                    else:
                        snap.status = "expired"
                        snap.realized_r = 0.0
                        # Não notifica expired sem TP1 (não aconteceu nada).
                    snap.outcome_at = now
                    resolved += 1
                    continue

                # Busca candles 5m desde o último check (no mínimo 1 candle)
                # Conservador: pega ~12 candles de 5m = 1h pra cobrir.
                df = await fetch_ohlcv(snap.symbol, "5m", 50)
                if df.empty:
                    continue
                # Filtra apenas candles após last_check_at
                ref_ts = int((snap.last_check_at or snap.created_at).timestamp() * 1000)
                df_window = df[df["timestamp"] >= ref_ts]
                if df_window.empty:
                    df_window = df.tail(1)

                outcome = _classify_outcome_candles(snap, df_window)
                if outcome is not None:
                    status, price, r, tp1_just_hit, new_peak = outcome

                    if status == "open_after_tp1":
                        # Step 2a+2b: TP1 tocou agora — marca timestamp + peak,
                        # stop vira BE/trail, posição segue aberta.
                        snap.tp1_hit_at = now
                        if new_peak is not None:
                            snap.peak_price_since_tp1 = new_peak
                        log.info(
                            f"[step-2b] {snap.symbol} {snap.timeframe} {snap.direction} "
                            f"TP1 hit (parcial 50%) → trail ativo (peak={new_peak})"
                        )
                        # Push: TP1 parcial — usuário sabe que 50% saiu e
                        # stop subiu pra BE+.
                        try:
                            await notify_outcome(snap, "tp1_partial")
                        except Exception as e:
                            log.warning(f"notify_outcome tp1_partial falhou: {e}")
                    elif status == "open_update":
                        # Só atualiza peak (TP1 já era passado, mas peak melhorou)
                        if new_peak is not None:
                            snap.peak_price_since_tp1 = new_peak
                    else:
                        # Fecha snapshot
                        snap.status = status
                        snap.outcome_price = price
                        snap.outcome_at = now
                        snap.realized_r = r
                        # Se TP1 bateu na MESMA janela (sem ter sido marcado
                        # antes), persiste tp1_hit_at e peak pra rastreio.
                        if tp1_just_hit and snap.tp1_hit_at is None:
                            snap.tp1_hit_at = now
                        if new_peak is not None:
                            snap.peak_price_since_tp1 = new_peak
                        resolved += 1
                        # Push de saída — emoji + R realizado.
                        # Mapeia status interno → evento user-facing.
                        event_map = {
                            "won_tp2": "tp2",
                            "won_tp1_be": "be_plus",
                            "won_tp1": "be_plus",  # tratado como saída pós-TP1
                            "lost": "lost",
                        }
                        ev = event_map.get(status)
                        if ev:
                            try:
                                await notify_outcome(snap, ev)
                            except Exception as e:
                                log.warning(f"notify_outcome {ev} falhou: {e}")
                        # Shadow #11.3: fecha trade sombra ligado a essa rec
                        try:
                            from services.shadow_trade_service import close_shadow_for_snapshot
                            await close_shadow_for_snapshot(snap)
                        except Exception as e:
                            log.warning(f"shadow close falhou: {e}")

                snap.last_check_at = now
            except Exception as e:
                log.warning(f"Erro checando snapshot {snap.id} ({snap.symbol}): {e}")

        await session.commit()

    if resolved:
        log.info(f"Snapshots resolvidos: {resolved}")
    return resolved


async def get_recently_stopped_symbols(hours: int = 6) -> set[str]:
    """
    Retorna o set de símbolos que tiveram stop ('lost') ou expiry sem TP1
    nas últimas N horas. Usado como cooldown — não recomendar de novo
    enquanto a tese técnica não 'resfria'.
    """
    if not DB_ENABLED:
        return set()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        async with get_session() as session:
            stmt = select(RecommendationSnapshot.symbol).where(
                RecommendationSnapshot.outcome_at >= cutoff,
                RecommendationSnapshot.status.in_(("lost", "expired")),
            )
            result = await session.execute(stmt)
            return {row[0] for row in result.all()}
    except Exception as e:
        log.warning(f"[cooldown] falha buscando símbolos estopados: {e}")
        return set()


async def get_daily_pnl(
    target_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> Dict[str, Any]:
    """
    Agrega P&L do dia especificado (ou hoje) — ou range [target_date, end_date].
    Considera snapshots cujo `outcome_at` cai no intervalo.
    """
    if not DB_ENABLED:
        return {"enabled": False, "message": "Banco de dados não configurado."}

    if target_date is None:
        target_date = datetime.now(timezone.utc).date()
    if end_date is None:
        end_date = target_date

    day_start = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc)
    day_end = datetime.combine(end_date, datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=1)

    async with get_session() as session:
        # Snapshots resolvidos NESTE dia (outcome_at). O dia do fechamento
        # manda — trade aberto ontem mas que estopou hoje aparece em hoje.
        # Inclui expired pra ficar visível (não conta em win/loss, r=0).
        stmt = select(RecommendationSnapshot).where(
            and_(
                RecommendationSnapshot.outcome_at >= day_start,
                RecommendationSnapshot.outcome_at < day_end,
                RecommendationSnapshot.status.in_(
                    ("won_tp1", "won_tp1_be", "won_tp2", "lost", "expired")
                ),
            )
        )
        result = await session.execute(stmt)
        snaps = result.scalars().all()

        # Snapshots ainda abertos — TODOS, independente do dia em que foram
        # criados. Trade de ontem que ainda não bateu TP/stop é tão relevante
        # quanto um aberto hoje (capital travado em ambos).
        open_stmt = select(RecommendationSnapshot).where(
            RecommendationSnapshot.status == "open"
        )
        open_snaps = (await session.execute(open_stmt)).scalars().all()
        open_count = len(open_snaps)

    wins = [s for s in snaps if s.realized_r and s.realized_r > 0]
    losses = [s for s in snaps if s.realized_r and s.realized_r < 0]
    total_r = sum(s.realized_r or 0 for s in snaps)
    win_count = len(wins)
    loss_count = len(losses)
    total = win_count + loss_count
    win_rate = (win_count / total * 100) if total else 0

    # Detalhe por trade (resolvido + aberto, em listas separadas)
    def _serialize(s):
        return {
            "symbol": s.symbol,
            "timeframe": s.timeframe,
            "tier": s.tier,
            "direction": s.direction,
            "entry": s.entry,
            "stop_loss": s.stop_loss,
            "tp1": s.tp1,
            "tp2": s.tp2,
            "leverage": s.leverage,
            "status": s.status,
            "realized_r": s.realized_r,
            "risk_pct": s.risk_pct,
            "score": s.score,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "outcome_at": s.outcome_at.isoformat() if s.outcome_at else None,
            "tp1_hit_at": s.tp1_hit_at.isoformat() if s.tp1_hit_at else None,
        }

    trades = [_serialize(s) for s in sorted(snaps, key=lambda x: x.outcome_at or x.created_at)]
    open_trades = [_serialize(s) for s in sorted(open_snaps, key=lambda x: x.created_at)]

    # Soma o % real da banca afetado no dia (cada trade tem seu risco próprio:
    # A+=1.5%, A=1%, B=0.5%). Não é total_r × risk_pct[0] — isso só vale se
    # todos os trades fossem do mesmo tier. Aqui somamos por trade.
    total_pct_banca = sum((s.realized_r or 0) * s.risk_pct for s in snaps)

    return {
        "enabled": True,
        "date": target_date.isoformat(),
        "end_date": end_date.isoformat(),
        "is_range": target_date != end_date,
        "summary": {
            "total_trades": total,
            "wins": win_count,
            "losses": loss_count,
            "win_rate_pct": round(win_rate, 1),
            "total_r": round(total_r, 2),
            "total_pct_banca": round(total_pct_banca, 3),
            "still_open": open_count,
        },
        "trades": trades,
        "open_trades": open_trades,
    }


async def get_history_stats(days: int = 30) -> Dict[str, Any]:
    """Estatísticas dos últimos N dias — alimenta o planejador da banca."""
    if not DB_ENABLED:
        return {"enabled": False}

    since = datetime.now(timezone.utc) - timedelta(days=days)
    async with get_session() as session:
        stmt = select(RecommendationSnapshot).where(
            and_(
                RecommendationSnapshot.outcome_at >= since,
                RecommendationSnapshot.status.in_(("won_tp1", "won_tp1_be", "won_tp2", "lost")),
            )
        )
        snaps = (await session.execute(stmt)).scalars().all()

    if not snaps:
        return {"enabled": True, "days": days, "trades": 0}

    wins = [s for s in snaps if (s.realized_r or 0) > 0]
    losses = [s for s in snaps if (s.realized_r or 0) < 0]
    total = len(snaps)
    win_rate = len(wins) / total if total else 0
    avg_win_r = sum(s.realized_r or 0 for s in wins) / len(wins) if wins else 0
    trades_per_day = total / days

    # Risco médio por trade
    avg_risk_pct = sum(s.risk_pct for s in snaps) / total if total else 1.0

    # E[R] por trade = win_rate * avg_win_R + (1-win_rate) * (-1)
    expected_r = win_rate * avg_win_r - (1 - win_rate) * 1.0
    # Retorno diário esperado em fração da banca = trades_dia × risk_pct × E[R]
    daily_return = trades_per_day * (avg_risk_pct / 100) * expected_r

    return {
        "enabled": True,
        "days": days,
        "trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(win_rate * 100, 1),
        "avg_win_r": round(avg_win_r, 2),
        "expected_r": round(expected_r, 3),
        "trades_per_day": round(trades_per_day, 2),
        "avg_risk_pct": round(avg_risk_pct, 2),
        "expected_daily_return_pct": round(daily_return * 100, 3),
    }
