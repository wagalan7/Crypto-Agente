"""
Volume Profile + VWAP.

Volume Profile: distribuição de volume por níveis de preço.
- POC (Point of Control): preço com maior volume → ímã/equilíbrio.
- VAH / VAL (Value Area High/Low): faixa que concentra ~70% do volume.

VWAP (Volume Weighted Average Price): preço médio ponderado pelo volume.
- Calculado em janela rolling (últimas N barras) sem reset por sessão
  (cripto é 24/7, sem open de sessão tradicional).
- Bandas ±1σ e ±2σ baseadas em desvio padrão ponderado.
"""
from __future__ import annotations
from typing import Optional, List
from pydantic import BaseModel
import pandas as pd
import numpy as np


class VolumeProfile(BaseModel):
    poc: float                  # Point of Control
    vah: float                  # Value Area High (70%)
    val: float                  # Value Area Low (70%)
    bins: List[List[float]]     # [[price_low, price_high, volume], ...]


class VWAPData(BaseModel):
    vwap: float
    upper_1sd: float
    lower_1sd: float
    upper_2sd: float
    lower_2sd: float
    distance_pct: float         # preço atual vs VWAP em %


class VPVWAPAnalysis(BaseModel):
    volume_profile: VolumeProfile
    vwap: VWAPData
    description: str


def _volume_profile(df: pd.DataFrame, n_bins: int = 30, value_area: float = 0.7) -> VolumeProfile:
    high = df["high"].values
    low = df["low"].values
    vol = df["volume"].values
    typical = (high + low) / 2

    min_p = float(np.min(low))
    max_p = float(np.max(high))
    if max_p <= min_p:
        return VolumeProfile(poc=max_p, vah=max_p, val=min_p, bins=[])

    bin_edges = np.linspace(min_p, max_p, n_bins + 1)
    volumes = np.zeros(n_bins)
    for i in range(len(df)):
        idx = int(np.clip(np.searchsorted(bin_edges, typical[i]) - 1, 0, n_bins - 1))
        volumes[idx] += vol[i]

    poc_idx = int(np.argmax(volumes))
    poc_price = float((bin_edges[poc_idx] + bin_edges[poc_idx + 1]) / 2)

    # Value Area: expande do POC até cobrir 70% do volume total
    total_vol = volumes.sum()
    target = total_vol * value_area
    accum = volumes[poc_idx]
    lo, hi = poc_idx, poc_idx
    while accum < target and (lo > 0 or hi < n_bins - 1):
        left_vol = volumes[lo - 1] if lo > 0 else -1
        right_vol = volumes[hi + 1] if hi < n_bins - 1 else -1
        if right_vol >= left_vol:
            hi += 1
            accum += volumes[hi]
        else:
            lo -= 1
            accum += volumes[lo]

    val_price = float(bin_edges[lo])
    vah_price = float(bin_edges[hi + 1])

    bins = [
        [float(bin_edges[i]), float(bin_edges[i + 1]), float(volumes[i])]
        for i in range(n_bins)
    ]
    return VolumeProfile(poc=poc_price, vah=vah_price, val=val_price, bins=bins)


def _vwap(df: pd.DataFrame, window: int = 100) -> VWAPData:
    """VWAP rolling com bandas de desvio padrão ponderado."""
    sub = df.tail(window) if len(df) > window else df
    high = sub["high"].values
    low = sub["low"].values
    close = sub["close"].values
    vol = sub["volume"].values

    typical = (high + low + close) / 3
    tp_vol = typical * vol
    cum_v = vol.sum()
    vwap = float(np.sum(tp_vol) / cum_v) if cum_v > 0 else float(close[-1])

    # Desvio ponderado
    var = np.sum(((typical - vwap) ** 2) * vol) / cum_v if cum_v > 0 else 0
    sd = float(np.sqrt(var))

    current = float(close[-1])
    distance_pct = ((current - vwap) / vwap * 100) if vwap > 0 else 0

    return VWAPData(
        vwap=round(vwap, 8),
        upper_1sd=round(vwap + sd, 8),
        lower_1sd=round(vwap - sd, 8),
        upper_2sd=round(vwap + 2 * sd, 8),
        lower_2sd=round(vwap - 2 * sd, 8),
        distance_pct=round(distance_pct, 2),
    )


def analyze_vp_vwap(df: pd.DataFrame) -> Optional[VPVWAPAnalysis]:
    if len(df) < 30:
        return None
    try:
        vp = _volume_profile(df)
        vw = _vwap(df)
    except Exception:
        return None

    current = float(df["close"].iloc[-1])
    parts = []
    if vp.val <= current <= vp.vah:
        parts.append("preço dentro do Value Area (zona de equilíbrio)")
    elif current > vp.vah:
        parts.append("preço acima do VAH (extensão de alta)")
    else:
        parts.append("preço abaixo do VAL (extensão de baixa)")

    if abs(vw.distance_pct) < 0.3:
        parts.append("colado no VWAP")
    elif vw.distance_pct > 2:
        parts.append(f"esticado +{vw.distance_pct:.1f}% acima do VWAP")
    elif vw.distance_pct < -2:
        parts.append(f"esticado {vw.distance_pct:.1f}% abaixo do VWAP")

    return VPVWAPAnalysis(
        volume_profile=vp,
        vwap=vw,
        description=" · ".join(parts),
    )
