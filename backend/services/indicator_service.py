from __future__ import annotations
import pandas as pd
import numpy as np
import ta
from models.trade_signal import Indicator


def _safe(series: pd.Series) -> float | None:
    if series is None or series.empty:
        return None
    v = series.iloc[-1]
    return float(v) if pd.notna(v) else None


def calculate_indicators(df: pd.DataFrame) -> Indicator:
    if len(df) < 50:
        return Indicator()

    close = df["close"]
    high  = df["high"]
    low   = df["low"]
    volume = df["volume"]

    # ── RSI ──────────────────────────────────────────────
    rsi = _safe(ta.momentum.RSIIndicator(close, window=14).rsi())

    # ── MACD ─────────────────────────────────────────────
    macd_ind = ta.trend.MACD(close, window_slow=26, window_fast=12, window_sign=9)
    macd       = _safe(macd_ind.macd())
    macd_signal = _safe(macd_ind.macd_signal())
    macd_hist  = _safe(macd_ind.macd_diff())

    # ── Bollinger Bands ───────────────────────────────────
    bb = ta.volatility.BollingerBands(close, window=20, window_dev=2)
    bb_upper  = _safe(bb.bollinger_hband())
    bb_middle = _safe(bb.bollinger_mavg())
    bb_lower  = _safe(bb.bollinger_lband())

    # ── EMAs ─────────────────────────────────────────────
    ema9   = _safe(ta.trend.EMAIndicator(close, window=12).ema_indicator())
    ema21  = _safe(ta.trend.EMAIndicator(close, window=26).ema_indicator())
    ema50  = _safe(ta.trend.EMAIndicator(close, window=50).ema_indicator())
    ema200 = _safe(ta.trend.EMAIndicator(close, window=200).ema_indicator()) if len(df) >= 200 else None

    # ── ATR ──────────────────────────────────────────────
    atr = _safe(ta.volatility.AverageTrueRange(high, low, close, window=14).average_true_range())

    # ── ADX ──────────────────────────────────────────────
    adx = _safe(ta.trend.ADXIndicator(high, low, close, window=14).adx())

    # ── Stochastic Oscillator (substituindo StochRSI) ────
    stoch = ta.momentum.StochasticOscillator(high, low, close, window=14, smooth_window=3)
    stoch_k = _safe(stoch.stoch())
    stoch_d = _safe(stoch.stoch_signal())

    # ── OBV ──────────────────────────────────────────────
    obv = _safe(ta.volume.OnBalanceVolumeIndicator(close, volume).on_balance_volume())

    # ── Volume médio 20 períodos ──────────────────────────
    volume_avg = float(volume.tail(20).mean())

    # ── Supertrend (implementação manual) ────────────────
    supertrend, supertrend_dir = _calc_supertrend(high, low, close, atr)

    # ── Pivot High / Low ─────────────────────────────────
    pivot_high = float(high.tail(20).max())
    pivot_low  = float(low.tail(20).min())

    def r(v, n=6):
        return round(v, n) if v is not None else None

    return Indicator(
        rsi=r(rsi, 2),
        macd=r(macd),
        macd_signal=r(macd_signal),
        macd_hist=r(macd_hist),
        bb_upper=r(bb_upper),
        bb_middle=r(bb_middle),
        bb_lower=r(bb_lower),
        ema9=r(ema9),
        ema21=r(ema21),
        ema50=r(ema50),
        ema200=r(ema200),
        atr=r(atr),
        adx=r(adx, 2),
        stoch_k=r(stoch_k, 2),
        stoch_d=r(stoch_d, 2),
        obv=r(obv, 2),
        volume_avg=r(volume_avg, 2),
        supertrend=r(supertrend),
        supertrend_direction=supertrend_dir,
        pivot_high=r(pivot_high),
        pivot_low=r(pivot_low),
    )


def _calc_supertrend(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    atr_value: float | None,
    period: int = 10,
    multiplier: float = 3.0,
) -> tuple:
    if atr_value is None or len(close) < period + 1:
        return None, None
    try:
        atr_series = ta.volatility.AverageTrueRange(high, low, close, window=period).average_true_range()
        hl2 = (high + low) / 2
        basic_upper = hl2 + multiplier * atr_series
        basic_lower = hl2 - multiplier * atr_series

        upper = basic_upper.copy()
        lower = basic_lower.copy()

        for i in range(1, len(close)):
            upper.iloc[i] = min(basic_upper.iloc[i], upper.iloc[i - 1]) if close.iloc[i - 1] <= upper.iloc[i - 1] else basic_upper.iloc[i]
            lower.iloc[i] = max(basic_lower.iloc[i], lower.iloc[i - 1]) if close.iloc[i - 1] >= lower.iloc[i - 1] else basic_lower.iloc[i]

        direction = pd.Series(1, index=close.index)
        for i in range(1, len(close)):
            if close.iloc[i] > upper.iloc[i - 1]:
                direction.iloc[i] = 1
            elif close.iloc[i] < lower.iloc[i - 1]:
                direction.iloc[i] = -1
            else:
                direction.iloc[i] = direction.iloc[i - 1]

        last_dir = int(direction.iloc[-1])
        last_st = float(lower.iloc[-1]) if last_dir == 1 else float(upper.iloc[-1])
        return last_st, last_dir
    except Exception:
        return None, None


def get_indicator_signals(ind: Indicator, current_price: float) -> dict:
    """Returns individual indicator signals: +1 bullish, -1 bearish, 0 neutral"""
    signals = {}

    if ind.rsi is not None:
        signals["rsi"] = 1 if ind.rsi < 30 else (-1 if ind.rsi > 70 else 0)

    if ind.macd is not None and ind.macd_signal is not None:
        signals["macd"] = 1 if ind.macd > ind.macd_signal else -1

    if ind.ema9 is not None and ind.ema21 is not None and ind.ema50 is not None:
        if ind.ema9 > ind.ema21 > ind.ema50:
            signals["ema_trend"] = 1
        elif ind.ema9 < ind.ema21 < ind.ema50:
            signals["ema_trend"] = -1
        else:
            signals["ema_trend"] = 0

    if ind.bb_upper is not None and ind.bb_lower is not None:
        signals["bb"] = 1 if current_price <= ind.bb_lower else (-1 if current_price >= ind.bb_upper else 0)

    if ind.stoch_k is not None and ind.stoch_d is not None:
        if ind.stoch_k < 20 and ind.stoch_d < 20:
            signals["stoch"] = 1
        elif ind.stoch_k > 80 and ind.stoch_d > 80:
            signals["stoch"] = -1
        else:
            signals["stoch"] = 0

    if ind.supertrend_direction is not None:
        signals["supertrend"] = 1 if ind.supertrend_direction == 1 else -1

    return signals
