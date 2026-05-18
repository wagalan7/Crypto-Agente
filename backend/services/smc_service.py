"""
Smart Money Concepts (SMC) — Order Blocks, Fair Value Gaps (FVG/Imbalance),
Liquidity Sweeps e Break of Structure / Change of Character (BOS/CHoCH).

Conceitos institucionais usados por traders profissionais para mapear
liquidez e zonas de interesse de smart money. Tudo opera apenas sobre
o DataFrame OHLCV — sem dependências externas.
"""
from __future__ import annotations
from typing import List, Optional
from pydantic import BaseModel
import pandas as pd
import numpy as np


# ─── Modelos ──────────────────────────────────────────────────────────────────
class SMCZone(BaseModel):
    type: str               # "order_block" | "fvg" | "liquidity_sweep"
    direction: str          # "bullish" | "bearish"
    top: float
    bottom: float
    index: int              # bar index onde foi formada
    timestamp: int
    description: str        # PT-BR
    active: bool            # True se preço ainda não invalidou


class StructureSignal(BaseModel):
    type: str               # "BOS" | "CHoCH"
    direction: str          # "bullish" | "bearish"
    price: float
    index: int
    timestamp: int
    description: str


class SMCAnalysis(BaseModel):
    order_blocks: List[SMCZone] = []
    fvgs: List[SMCZone] = []
    liquidity_sweeps: List[SMCZone] = []
    structure: Optional[StructureSignal] = None
    trend_bias: str = "neutral"     # "bullish" | "bearish" | "neutral"


# ─── Pivots auxiliares ────────────────────────────────────────────────────────
def _swing_points(df: pd.DataFrame, lookback: int = 3):
    """Retorna índices de swing highs/lows usando janela de N à esquerda/direita."""
    highs, lows = [], []
    h = df["high"].values
    l = df["low"].values
    n = len(df)
    for i in range(lookback, n - lookback):
        if h[i] == max(h[i - lookback:i + lookback + 1]):
            highs.append(i)
        if l[i] == min(l[i - lookback:i + lookback + 1]):
            lows.append(i)
    return highs, lows


# ─── Order Blocks ─────────────────────────────────────────────────────────────
def _detect_order_blocks(df: pd.DataFrame, max_zones: int = 3) -> List[SMCZone]:
    """
    Order Block bullish: última candle bearish ANTES de uma sequência forte
    de candles bullish que rompem estrutura.
    Bearish: inverso.
    """
    if len(df) < 20:
        return []

    o = df["open"].values
    c = df["close"].values
    h = df["high"].values
    l = df["low"].values
    ts = df["timestamp"].values

    zones: List[SMCZone] = []
    current = c[-1]

    # Olha as últimas 80 barras
    start = max(20, len(df) - 80)
    for i in range(start, len(df) - 5):
        # Bullish OB: candle bearish (i) seguida por 2-3 candles bullish fortes
        body_i = c[i] - o[i]
        if body_i < 0:  # candle bearish
            next_bullish = sum(1 for j in range(i + 1, min(i + 4, len(df))) if c[j] > o[j])
            move_up = (c[min(i + 3, len(df) - 1)] - h[i]) / h[i] if h[i] > 0 else 0
            if next_bullish >= 2 and move_up > 0.005:
                top, bottom = float(h[i]), float(l[i])
                # Invalida se preço fechou abaixo do bottom desde então
                broken = any(c[k] < bottom for k in range(i + 1, len(df)))
                active = not broken and current > bottom
                zones.append(SMCZone(
                    type="order_block",
                    direction="bullish",
                    top=top, bottom=bottom,
                    index=i, timestamp=int(ts[i]),
                    description=f"Order Block de compra em {bottom:.6g}–{top:.6g} (zona de demanda institucional).",
                    active=active,
                ))

        # Bearish OB: candle bullish seguida por 2-3 bearish fortes
        if body_i > 0:
            next_bearish = sum(1 for j in range(i + 1, min(i + 4, len(df))) if c[j] < o[j])
            move_down = (l[i] - c[min(i + 3, len(df) - 1)]) / l[i] if l[i] > 0 else 0
            if next_bearish >= 2 and move_down > 0.005:
                top, bottom = float(h[i]), float(l[i])
                broken = any(c[k] > top for k in range(i + 1, len(df)))
                active = not broken and current < top
                zones.append(SMCZone(
                    type="order_block",
                    direction="bearish",
                    top=top, bottom=bottom,
                    index=i, timestamp=int(ts[i]),
                    description=f"Order Block de venda em {bottom:.6g}–{top:.6g} (zona de oferta institucional).",
                    active=active,
                ))

    # Mantém só os mais recentes ativos
    zones = [z for z in zones if z.active]
    zones.sort(key=lambda z: z.index, reverse=True)
    return zones[:max_zones]


# ─── Fair Value Gaps (FVG) ────────────────────────────────────────────────────
def _detect_fvgs(df: pd.DataFrame, max_zones: int = 3) -> List[SMCZone]:
    """
    FVG bullish: candle[i-1].high < candle[i+1].low → gap entre os dois.
    FVG bearish: candle[i-1].low > candle[i+1].high.
    """
    if len(df) < 5:
        return []

    h = df["high"].values
    l = df["low"].values
    c = df["close"].values
    ts = df["timestamp"].values
    current = c[-1]

    zones: List[SMCZone] = []
    start = max(2, len(df) - 60)
    for i in range(start, len(df) - 1):
        # Bullish FVG
        if h[i - 1] < l[i + 1]:
            top = float(l[i + 1])
            bottom = float(h[i - 1])
            # Ativo se preço ainda não preencheu o gap
            filled = current < bottom or any(l[k] <= bottom for k in range(i + 2, len(df)))
            active = not filled and current >= bottom
            zones.append(SMCZone(
                type="fvg",
                direction="bullish",
                top=top, bottom=bottom,
                index=i, timestamp=int(ts[i]),
                description=f"FVG de alta (imbalance {bottom:.6g}–{top:.6g}) — preço tende a reagir aqui.",
                active=active,
            ))
        # Bearish FVG
        if l[i - 1] > h[i + 1]:
            top = float(l[i - 1])
            bottom = float(h[i + 1])
            filled = current > top or any(h[k] >= top for k in range(i + 2, len(df)))
            active = not filled and current <= top
            zones.append(SMCZone(
                type="fvg",
                direction="bearish",
                top=top, bottom=bottom,
                index=i, timestamp=int(ts[i]),
                description=f"FVG de baixa (imbalance {bottom:.6g}–{top:.6g}) — preço tende a reagir aqui.",
                active=active,
            ))

    zones = [z for z in zones if z.active]
    zones.sort(key=lambda z: z.index, reverse=True)
    return zones[:max_zones]


# ─── Liquidity Sweeps ─────────────────────────────────────────────────────────
def _detect_liquidity_sweeps(df: pd.DataFrame, max_zones: int = 3) -> List[SMCZone]:
    """
    Sweep bullish: candle quebra um swing low recente com pavio e fecha acima.
    Sweep bearish: candle quebra um swing high com pavio e fecha abaixo.
    """
    if len(df) < 30:
        return []

    h = df["high"].values
    l = df["low"].values
    c = df["close"].values
    o = df["open"].values
    ts = df["timestamp"].values

    highs, lows = _swing_points(df.iloc[:-5], lookback=3)
    if not highs and not lows:
        return []

    zones: List[SMCZone] = []
    recent_high = max(highs[-5:]) if highs else None
    recent_low = max(lows[-5:]) if lows else None

    # Olha últimas 5 candles
    for i in range(max(len(df) - 5, 0), len(df)):
        # Sweep de baixa (pega liquidez de buy-stops) → bearish
        if recent_high is not None and recent_high < i:
            swing_h = h[recent_high]
            if h[i] > swing_h and c[i] < swing_h:
                zones.append(SMCZone(
                    type="liquidity_sweep",
                    direction="bearish",
                    top=float(h[i]), bottom=float(swing_h),
                    index=i, timestamp=int(ts[i]),
                    description=f"Sweep de liquidez acima de {swing_h:.6g} — buy-stops varridos, viés de baixa.",
                    active=True,
                ))
        # Sweep de alta (pega sell-stops) → bullish
        if recent_low is not None and recent_low < i:
            swing_l = l[recent_low]
            if l[i] < swing_l and c[i] > swing_l:
                zones.append(SMCZone(
                    type="liquidity_sweep",
                    direction="bullish",
                    top=float(swing_l), bottom=float(l[i]),
                    index=i, timestamp=int(ts[i]),
                    description=f"Sweep de liquidez abaixo de {swing_l:.6g} — sell-stops varridos, viés de alta.",
                    active=True,
                ))

    zones.sort(key=lambda z: z.index, reverse=True)
    return zones[:max_zones]


# ─── BOS / CHoCH ──────────────────────────────────────────────────────────────
def _detect_structure(df: pd.DataFrame) -> tuple[Optional[StructureSignal], str]:
    """
    BOS (Break of Structure): rompimento do último swing high (bullish) ou low (bearish)
    NA DIREÇÃO da tendência vigente.
    CHoCH (Change of Character): rompimento na direção CONTRÁRIA — sinal de reversão.
    """
    if len(df) < 20:
        return None, "neutral"

    highs, lows = _swing_points(df, lookback=3)
    if len(highs) < 2 or len(lows) < 2:
        return None, "neutral"

    h = df["high"].values
    l = df["low"].values
    c = df["close"].values
    ts = df["timestamp"].values
    current = c[-1]

    last_high_idx = highs[-1]
    prev_high_idx = highs[-2]
    last_low_idx = lows[-1]
    prev_low_idx = lows[-2]

    last_high = h[last_high_idx]
    prev_high = h[prev_high_idx]
    last_low = l[last_low_idx]
    prev_low = l[prev_low_idx]

    # Trend bias por estrutura: HH+HL = bull, LH+LL = bear
    bull_struct = last_high > prev_high and last_low > prev_low
    bear_struct = last_high < prev_high and last_low < prev_low
    bias = "bullish" if bull_struct else "bearish" if bear_struct else "neutral"

    # Detecta rompimento na barra final
    if current > last_high and last_high_idx < len(df) - 1:
        sig_type = "BOS" if bias == "bullish" else "CHoCH"
        return StructureSignal(
            type=sig_type, direction="bullish",
            price=float(last_high), index=last_high_idx, timestamp=int(ts[last_high_idx]),
            description=(
                f"{sig_type}: preço rompeu swing high {last_high:.6g} — "
                + ("continuação de alta." if sig_type == "BOS" else "possível reversão para alta.")
            ),
        ), bias

    if current < last_low and last_low_idx < len(df) - 1:
        sig_type = "BOS" if bias == "bearish" else "CHoCH"
        return StructureSignal(
            type=sig_type, direction="bearish",
            price=float(last_low), index=last_low_idx, timestamp=int(ts[last_low_idx]),
            description=(
                f"{sig_type}: preço rompeu swing low {last_low:.6g} — "
                + ("continuação de baixa." if sig_type == "BOS" else "possível reversão para baixa.")
            ),
        ), bias

    return None, bias


# ─── Entrada pública ──────────────────────────────────────────────────────────
def analyze_smc(df: pd.DataFrame) -> SMCAnalysis:
    obs = _detect_order_blocks(df)
    fvgs = _detect_fvgs(df)
    sweeps = _detect_liquidity_sweeps(df)
    structure, bias = _detect_structure(df)
    return SMCAnalysis(
        order_blocks=obs,
        fvgs=fvgs,
        liquidity_sweeps=sweeps,
        structure=structure,
        trend_bias=bias,
    )
