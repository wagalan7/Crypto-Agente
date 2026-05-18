"""
Detector de divergências entre preço e indicadores (RSI, MACD).

Divergência regular bullish: preço faz LL, indicador faz HL → reversão para alta.
Divergência regular bearish: preço faz HH, indicador faz LH → reversão para baixa.
Divergência oculta bullish: preço faz HL, indicador faz LL → continuação de alta.
Divergência oculta bearish: preço faz LH, indicador faz HH → continuação de baixa.

Trabalha com swings/pivots locais nos últimos ~60-100 candles.
"""
from __future__ import annotations
from typing import List, Optional
from pydantic import BaseModel
import pandas as pd
import numpy as np
import ta


class Divergence(BaseModel):
    indicator: str           # "RSI" | "MACD"
    type: str                # "regular" | "hidden"
    direction: str           # "bullish" | "bearish"
    price_p1: float
    price_p2: float
    ind_p1: float
    ind_p2: float
    index_p1: int
    index_p2: int
    strength: float          # 0-1
    description: str


def _find_pivots(series: np.ndarray, lookback: int = 3) -> tuple[list[int], list[int]]:
    """Retorna índices de pivots de máxima e mínima local."""
    highs, lows = [], []
    n = len(series)
    for i in range(lookback, n - lookback):
        window = series[i - lookback:i + lookback + 1]
        if series[i] == np.max(window):
            highs.append(i)
        if series[i] == np.min(window):
            lows.append(i)
    return highs, lows


def _check_divergence_pair(
    p_i1: int, p_i2: int,
    price_arr: np.ndarray,
    ind_arr: np.ndarray,
    direction: str,
    div_class: str,
) -> Optional[Divergence]:
    """direction: bullish/bearish; div_class: regular/hidden."""
    price_v1, price_v2 = float(price_arr[p_i1]), float(price_arr[p_i2])
    ind_v1, ind_v2 = float(ind_arr[p_i1]), float(ind_arr[p_i2])

    if np.isnan(ind_v1) or np.isnan(ind_v2):
        return None

    if direction == "bullish" and div_class == "regular":
        # Price LL + ind HL
        if not (price_v2 < price_v1 and ind_v2 > ind_v1):
            return None
    elif direction == "bearish" and div_class == "regular":
        # Price HH + ind LH
        if not (price_v2 > price_v1 and ind_v2 < ind_v1):
            return None
    elif direction == "bullish" and div_class == "hidden":
        # Price HL + ind LL
        if not (price_v2 > price_v1 and ind_v2 < ind_v1):
            return None
    elif direction == "bearish" and div_class == "hidden":
        # Price LH + ind HH
        if not (price_v2 < price_v1 and ind_v2 > ind_v1):
            return None
    else:
        return None

    # Força = magnitude relativa do desvio
    price_delta = abs(price_v2 - price_v1) / max(abs(price_v1), 1e-9)
    ind_delta = abs(ind_v2 - ind_v1) / max(abs(ind_v1), 1e-9)
    strength = min(1.0, (price_delta + ind_delta) * 5)
    return price_v1, price_v2, ind_v1, ind_v2, strength


def _scan_indicator(
    df: pd.DataFrame,
    ind_arr: np.ndarray,
    indicator_name: str,
) -> List[Divergence]:
    """Procura divergências entre preço e o indicador nos últimos ~60 candles."""
    out: List[Divergence] = []
    n = len(df)
    if n < 30:
        return out

    # Limita ao final do histórico para divergências recentes
    start = max(0, n - 80)
    sub_close = df["close"].values[start:]
    sub_ind = ind_arr[start:]
    if np.all(np.isnan(sub_ind)):
        return out

    # Pivots no preço
    p_highs, p_lows = _find_pivots(sub_close, lookback=3)

    # Bullish regular: 2 últimos lows com price LL + ind HL
    for cls in ("regular", "hidden"):
        # Bullish
        if len(p_lows) >= 2:
            i1, i2 = p_lows[-2], p_lows[-1]
            res = _check_divergence_pair(i1, i2, sub_close, sub_ind, "bullish", cls)
            if res:
                pv1, pv2, iv1, iv2, strength = res
                if strength >= 0.15:
                    desc_kind = "regular (reversão)" if cls == "regular" else "oculta (continuação)"
                    out.append(Divergence(
                        indicator=indicator_name, type=cls, direction="bullish",
                        price_p1=pv1, price_p2=pv2, ind_p1=iv1, ind_p2=iv2,
                        index_p1=start + i1, index_p2=start + i2,
                        strength=round(strength, 2),
                        description=f"Divergência {desc_kind} bullish no {indicator_name}: preço {('caiu' if cls=='regular' else 'subiu menos')}, {indicator_name} {('subiu' if cls=='regular' else 'caiu')} — viés de alta.",
                    ))
        # Bearish
        if len(p_highs) >= 2:
            i1, i2 = p_highs[-2], p_highs[-1]
            res = _check_divergence_pair(i1, i2, sub_close, sub_ind, "bearish", cls)
            if res:
                pv1, pv2, iv1, iv2, strength = res
                if strength >= 0.15:
                    desc_kind = "regular (reversão)" if cls == "regular" else "oculta (continuação)"
                    out.append(Divergence(
                        indicator=indicator_name, type=cls, direction="bearish",
                        price_p1=pv1, price_p2=pv2, ind_p1=iv1, ind_p2=iv2,
                        index_p1=start + i1, index_p2=start + i2,
                        strength=round(strength, 2),
                        description=f"Divergência {desc_kind} bearish no {indicator_name}: preço {('subiu' if cls=='regular' else 'caiu menos')}, {indicator_name} {('caiu' if cls=='regular' else 'subiu')} — viés de baixa.",
                    ))
    return out


def detect_divergences(df: pd.DataFrame) -> List[Divergence]:
    """Roda detector sobre RSI e MACD."""
    if len(df) < 30:
        return []
    close = df["close"]
    try:
        rsi_series = ta.momentum.RSIIndicator(close, window=14).rsi().values
    except Exception:
        rsi_series = np.full(len(df), np.nan)
    try:
        macd_hist = ta.trend.MACD(close, window_slow=26, window_fast=12, window_sign=9).macd_diff().values
    except Exception:
        macd_hist = np.full(len(df), np.nan)

    divs = _scan_indicator(df, rsi_series, "RSI")
    divs += _scan_indicator(df, macd_hist, "MACD")
    # Mantém apenas as 4 mais fortes/recentes
    divs.sort(key=lambda d: (d.index_p2, d.strength), reverse=True)
    return divs[:4]
