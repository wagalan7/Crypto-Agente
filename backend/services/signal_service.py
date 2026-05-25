import os
import pandas as pd
import numpy as np
import time
from typing import List, Optional
from models.trade_signal import (
    TradeSignal, TradeType, SignalDirection, DetectedPattern, Indicator
)

# Step 2c: confirmação de candle no entry. Default ON — preferimos zero
# pushes do que push em vela contra-direção. Pode desligar via env var:
#   REQUIRE_CANDLE_CONFIRMATION=0
REQUIRE_CANDLE_CONFIRMATION = os.getenv(
    "REQUIRE_CANDLE_CONFIRMATION", "1"
).strip() not in ("0", "false", "False", "no", "off", "")
# Posição do close dentro do range (0.0 = no low, 1.0 = no high).
# Long precisa fechar acima desse percentil; short abaixo de (1 - percentil).
CANDLE_CONFIRM_CLOSE_POS = 0.55
from services.indicator_service import get_indicator_signals
from services.confluence_service import calculate_confluence
from services.smc_service import analyze_smc, SMCAnalysis
from services.derivatives_service import DerivativesData
from services.backtest_service import compute_pattern_stats, PatternStats
from services.divergence_service import detect_divergences
from services.vp_service import analyze_vp_vwap
from services.mtf_service import MTFAlignment
from services.entry_planner import plan_trade, TradePlan


TIMEFRAME_TRADE_TYPE = {
    "1m":  TradeType.SCALP,
    "5m":  TradeType.SCALP,
    "15m": TradeType.SCALP,
    "30m": TradeType.DAY_TRADE,
    "1h":  TradeType.DAY_TRADE,
    "4h":  TradeType.SWING,
    "6h":  TradeType.SWING,
    "8h":  TradeType.SWING,
    "12h": TradeType.SWING,
    "1d":  TradeType.HODL,
    "3d":  TradeType.HODL,
}

TRADE_TYPE_LABELS = {
    TradeType.SCALP: "Scalp (minutos)",
    TradeType.DAY_TRADE: "Day Trade (horas)",
    TradeType.SWING: "Swing Trade (dias/semanas)",
    TradeType.HODL: "HODL (longo prazo)",
}

ATR_MULTIPLIERS = {
    TradeType.SCALP:     {"sl": 1.0, "tp1": 1.5, "tp2": 2.5, "tp3": 4.0},
    TradeType.DAY_TRADE: {"sl": 1.5, "tp1": 2.0, "tp2": 3.5, "tp3": 5.0},
    TradeType.SWING:     {"sl": 2.0, "tp1": 3.0, "tp2": 5.0, "tp3": 8.0},
    TradeType.HODL:      {"sl": 3.0, "tp1": 5.0, "tp2": 10.0, "tp3": 20.0},
}


def _candle_confirms(df: pd.DataFrame, direction: SignalDirection) -> bool:
    """
    Step 2c: a última vela fechada deve confirmar a direção do sinal.

    Critérios (ambos precisam passar):
      • Corpo na direção: close > open (long) / close < open (short)
      • Close na metade favorável do range: pelo menos 55% pra cima (long)
        ou pelo menos 55% pra baixo (short)

    Filtra: dojis, velas de rejeição (long upper wick em long, long lower wick
    em short), reversões intra-vela. Mantém: velas momentum claras na direção.

    Retorna True se NÃO houver direção (NEUTRAL não precisa confirmar) ou se a
    vela confirmar. False se vela contradiz o sinal.
    """
    if direction == SignalDirection.NEUTRAL or df is None or df.empty:
        return True

    last = df.iloc[-1]
    try:
        o = float(last["open"])
        h = float(last["high"])
        l = float(last["low"])
        c = float(last["close"])
    except (KeyError, ValueError, TypeError):
        return True  # falha de leitura → não bloqueia

    rng = h - l
    if rng <= 0:
        return False  # vela degenerada — bloqueia

    close_pos = (c - l) / rng  # 0..1, posição do close no range

    if direction == SignalDirection.LONG:
        body_ok = c > o
        pos_ok = close_pos >= CANDLE_CONFIRM_CLOSE_POS
    else:  # SHORT
        body_ok = c < o
        pos_ok = close_pos <= (1.0 - CANDLE_CONFIRM_CLOSE_POS)

    return body_ok and pos_ok


def determine_direction(ind: Indicator, patterns: List[DetectedPattern], current_price: float) -> SignalDirection:
    signals = get_indicator_signals(ind, current_price)
    score = sum(signals.values())

    pattern_score = 0
    for p in patterns[:3]:
        if p.direction == SignalDirection.LONG:
            pattern_score += p.confidence
        elif p.direction == SignalDirection.SHORT:
            pattern_score -= p.confidence

    total = score + pattern_score * 2
    if total > 1.0:
        return SignalDirection.LONG
    elif total < -1.0:
        return SignalDirection.SHORT
    return SignalDirection.NEUTRAL


def calculate_confidence(ind: Indicator, patterns: List[DetectedPattern], direction: SignalDirection, current_price: float) -> float:
    signals = get_indicator_signals(ind, current_price)
    total = len(signals)
    if total == 0:
        return 0.5

    aligned = sum(
        1 for v in signals.values()
        if (direction == SignalDirection.LONG and v == 1) or
           (direction == SignalDirection.SHORT and v == -1)
    )
    indicator_score = aligned / total

    pattern_conf = 0.0
    aligned_patterns = [
        p for p in patterns if p.direction == direction or p.direction == SignalDirection.NEUTRAL
    ]
    if aligned_patterns:
        pattern_conf = sum(p.confidence for p in aligned_patterns[:3]) / 3

    confidence = (indicator_score * 0.6) + (pattern_conf * 0.4)

    # Boost if ADX shows strong trend
    if ind.adx and ind.adx > 25:
        confidence = min(confidence * 1.1, 0.99)

    return round(confidence, 2)


def calculate_levels(
    current_price: float,
    atr: float,
    direction: SignalDirection,
    trade_type: TradeType,
    patterns: List[DetectedPattern],
    ind: Indicator,
) -> dict:
    mults = ATR_MULTIPLIERS[trade_type]

    # Use pattern breakout targets as TP anchors when available
    pattern_targets = [
        p.breakout_target for p in patterns
        if p.breakout_target and p.direction == direction
    ]

    if direction == SignalDirection.LONG:
        entry = current_price
        stop_loss = entry - atr * mults["sl"]

        # Snap to support if available
        if ind.pivot_low and ind.pivot_low < entry and ind.pivot_low > stop_loss * 0.95:
            stop_loss = ind.pivot_low * 0.998

        tp1 = entry + atr * mults["tp1"]
        tp2 = entry + atr * mults["tp2"]
        tp3 = entry + atr * mults["tp3"]

        if pattern_targets:
            best = max(pattern_targets)
            if best > tp1:
                tp2 = best
                tp3 = best * 1.05

    elif direction == SignalDirection.SHORT:
        entry = current_price
        stop_loss = entry + atr * mults["sl"]

        # Snap to resistance if available
        if ind.pivot_high and ind.pivot_high > entry and ind.pivot_high < stop_loss * 1.05:
            stop_loss = ind.pivot_high * 1.002

        tp1 = entry - atr * mults["tp1"]
        tp2 = entry - atr * mults["tp2"]
        tp3 = entry - atr * mults["tp3"]

        if pattern_targets:
            best = min(pattern_targets)
            if best < tp1:
                tp2 = best
                tp3 = best * 0.95

    else:
        entry = current_price
        stop_loss = entry - atr * mults["sl"]
        tp1 = entry + atr * mults["tp1"]
        tp2 = entry + atr * mults["tp2"]
        tp3 = entry + atr * mults["tp3"]

    risk = abs(entry - stop_loss)
    reward = abs(tp2 - entry)
    rr = round(reward / risk, 2) if risk > 0 else 0.0

    return {
        "entry": round(entry, 8),
        "stop_loss": round(stop_loss, 8),
        "tp1": round(tp1, 8),
        "tp2": round(tp2, 8),
        "tp3": round(tp3, 8),
        "risk_reward": rr,
    }


def signal_strength_label(confidence: float) -> str:
    if confidence >= 0.75:
        return "Forte"
    elif confidence >= 0.60:
        return "Moderado"
    else:
        return "Fraco"


def build_trade_signal(
    symbol: str,
    timeframe: str,
    df: pd.DataFrame,
    ind: Indicator,
    patterns: List[DetectedPattern],
    derivatives: Optional[DerivativesData] = None,
    mtf: Optional[MTFAlignment] = None,
    with_backtest: bool = True,
) -> TradeSignal:
    current_price = float(df["close"].iloc[-1])
    atr = ind.atr or (current_price * 0.01)
    trade_type = TIMEFRAME_TRADE_TYPE.get(timeframe, TradeType.DAY_TRADE)

    direction = determine_direction(ind, patterns, current_price)

    # Step 2c: se a vela do sinal NÃO confirmar a direção, descarta o sinal.
    # Filtra dojis, velas de rejeição (wick contra), reversões intra-vela.
    if REQUIRE_CANDLE_CONFIRMATION and direction != SignalDirection.NEUTRAL:
        if not _candle_confirms(df, direction):
            direction = SignalDirection.NEUTRAL

    # SMC analysis (sempre roda)
    try:
        smc = analyze_smc(df)
    except Exception:
        smc = None

    # Divergências RSI/MACD
    try:
        divergences = detect_divergences(df)
    except Exception:
        divergences = []

    # Volume Profile + VWAP
    try:
        vp_vwap = analyze_vp_vwap(df)
    except Exception:
        vp_vwap = None

    # Backtest histórico (cacheado)
    pattern_stats: Optional[PatternStats] = None
    if with_backtest and patterns:
        try:
            pattern_stats = compute_pattern_stats(symbol, timeframe, df)
        except Exception:
            pattern_stats = None

    # Score de confluência ponderado e transparente
    if direction != SignalDirection.NEUTRAL:
        confluence = calculate_confluence(
            ind, patterns, df, direction, current_price,
            smc=smc, derivatives=derivatives, pattern_stats=pattern_stats,
            divergences=divergences, vp_vwap=vp_vwap, mtf=mtf,
        )
        confidence = round(confluence.pct / 100, 2)
    else:
        confluence = calculate_confluence(
            ind, patterns, df, SignalDirection.NEUTRAL, current_price,
            smc=smc, derivatives=derivatives, pattern_stats=pattern_stats,
            divergences=divergences, vp_vwap=vp_vwap, mtf=mtf,
        )
        confidence = calculate_confidence(ind, patterns, direction, current_price)

    # Sprint B: plano de trade estruturado (zona de entrada + stop estrutural + alvos por liquidez)
    trade_plan: Optional[TradePlan] = None
    if direction != SignalDirection.NEUTRAL:
        try:
            trade_plan = plan_trade(
                direction=direction,
                current_price=current_price,
                df=df,
                ind=ind,
                patterns=patterns,
                smc=smc.model_dump() if smc else None,
                vp_vwap=vp_vwap.model_dump() if vp_vwap else None,
            )
        except Exception:
            trade_plan = None

    if trade_plan is not None:
        levels = {
            "entry": trade_plan.entry,
            "stop_loss": trade_plan.stop_loss,
            "tp1": trade_plan.tp1,
            "tp2": trade_plan.tp2,
            "tp3": trade_plan.tp3,
            "risk_reward": trade_plan.risk_reward,
        }
    else:
        levels = calculate_levels(current_price, atr, direction, trade_type, patterns, ind)

    return TradeSignal(
        symbol=symbol,
        timeframe=timeframe,
        direction=direction,
        trade_type=trade_type,
        confidence=confidence,
        entry=levels["entry"],
        stop_loss=levels["stop_loss"],
        tp1=levels["tp1"],
        tp2=levels["tp2"],
        tp3=levels["tp3"],
        risk_reward=levels["risk_reward"],
        patterns=patterns,
        indicators=ind,
        ai_analysis=None,
        ai_critique=None,
        confluence=confluence,
        smc=smc.model_dump() if smc else None,
        derivatives=derivatives.model_dump() if derivatives else None,
        pattern_stats=pattern_stats.model_dump() if pattern_stats else None,
        divergences=[d.model_dump() for d in divergences] if divergences else None,
        vp_vwap=vp_vwap.model_dump() if vp_vwap else None,
        mtf=mtf.model_dump() if mtf else None,
        trade_plan=trade_plan.model_dump() if trade_plan else None,
        current_price=current_price,
        timestamp=int(df["timestamp"].iloc[-1]),
        signal_strength=signal_strength_label(confidence),
    )
