"""
Entry Planner — Sprint B:

1) Entradas inteligentes (limit orders, não market):
   - Pullback ao EMA21 / VWAP / OrderBlock / FVG / VAL-VAH
   - Retest de breakout

2) Stops estruturais (não ATR cego):
   - Abaixo da última estrutura (HL/LH)
   - Abaixo do bottom do OB ou FVG quando esses justificam a entrada
   - Buffer de 0.3×ATR
   - Se a única estrutura óbvia é uma liquidity pool perigosa, usa fallback ATR

3) Alvos por liquidez e estrutura:
   - TP1 no próximo swing estrutural
   - TP2 em pool de liquidez (igual highs/lows) ou VAH/VAL
   - TP3 em estrutura HTF (pattern target ou múltiplos do ATR)
   - Filtra R:R mínimo (2:1 com TP2)

Retorna TradePlan rica com justificativas PT-BR por nível.
"""
from __future__ import annotations
from typing import List, Optional, Tuple
from pydantic import BaseModel
import pandas as pd
import numpy as np

from models.trade_signal import (
    Indicator, DetectedPattern, SignalDirection, TradeType
)


# ─── Modelos ──────────────────────────────────────────────────────────────────
class LevelReasoning(BaseModel):
    price: float
    reason: str             # PT-BR
    source: str             # ex: "ema21" | "pivot_low" | "order_block" | "fvg" | "vah" | "atr_fallback"


class EntryZone(BaseModel):
    top: float
    bottom: float
    mid: float
    type: str               # "limit_pullback" | "limit_retest" | "limit_fvg_fill" | "limit_ob" | "market" | "limit_value_area"
    description: str


class TradePlan(BaseModel):
    entry: float                          # mid da zona (preço de referência)
    entry_zone: Optional[EntryZone] = None
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    risk_reward: float                    # baseado em TP2
    risk_reward_tp1: float
    risk_reward_tp3: float
    reasoning_entry: str                  # PT-BR
    reasoning_stop: LevelReasoning
    reasoning_tp1: LevelReasoning
    reasoning_tp2: LevelReasoning
    reasoning_tp3: LevelReasoning
    quality_warnings: List[str] = []      # ex: "R:R < 2", "stop muito distante"


# ─── Auxiliares ───────────────────────────────────────────────────────────────
def _swing_points(df: pd.DataFrame, lookback: int = 3) -> Tuple[List[int], List[int]]:
    h, l = df["high"].values, df["low"].values
    highs, lows = [], []
    n = len(df)
    for i in range(lookback, n - lookback):
        if h[i] == max(h[i - lookback:i + lookback + 1]):
            highs.append(i)
        if l[i] == min(l[i - lookback:i + lookback + 1]):
            lows.append(i)
    return highs, lows


def _equal_highs_lows(df: pd.DataFrame, tolerance_pct: float = 0.3) -> Tuple[List[float], List[float]]:
    """Retorna níveis com clusters de topos/fundos iguais (liquidity pools)."""
    highs_idx, lows_idx = _swing_points(df, lookback=3)
    h_prices = [float(df["high"].iloc[i]) for i in highs_idx[-20:]]
    l_prices = [float(df["low"].iloc[i]) for i in lows_idx[-20:]]

    def cluster(prices: List[float]) -> List[float]:
        if not prices:
            return []
        prices_sorted = sorted(prices)
        clusters: List[List[float]] = []
        current = [prices_sorted[0]]
        for p in prices_sorted[1:]:
            if abs(p - current[-1]) / current[-1] * 100 <= tolerance_pct:
                current.append(p)
            else:
                if len(current) >= 2:
                    clusters.append(current)
                current = [p]
        if len(current) >= 2:
            clusters.append(current)
        return [sum(c) / len(c) for c in clusters]

    return cluster(h_prices), cluster(l_prices)


# ─── Núcleo: cálculo do plano ────────────────────────────────────────────────
ATR_ENTRY_BAND = 0.4    # tamanho da zona de entrada como múltiplo de ATR
ATR_BUFFER = 0.3        # folga no stop além da estrutura
MIN_RR_TP2 = 1.8        # alvo mínimo aceitável pra TP2
EMA_PULLBACK_MAX_DIST = 0.04   # 4% — distância máxima do preço atual pra considerar pullback ao EMA


def _pick_entry_zone_long(
    current_price: float, atr: float, ind: Indicator,
    smc: Optional[dict], vp_vwap: Optional[dict],
) -> Optional[EntryZone]:
    """Acha a melhor zona de entrada limit para LONG abaixo do preço atual."""
    candidates: List[EntryZone] = []
    band = atr * ATR_ENTRY_BAND

    # EMA21 pullback
    ema21 = ind.ema21
    if ema21 and ema21 < current_price * 0.999:
        dist_pct = (current_price - ema21) / current_price
        if dist_pct <= EMA_PULLBACK_MAX_DIST:
            candidates.append(EntryZone(
                top=ema21 + band / 2, bottom=ema21 - band / 2, mid=ema21,
                type="limit_pullback",
                description=f"Pullback à EMA21 ({ema21:.6g}) — média dinâmica como suporte.",
            ))

    # VWAP pullback
    if vp_vwap:
        vw = vp_vwap.get("vwap", {})
        vwap_price = vw.get("vwap")
        if vwap_price and vwap_price < current_price * 0.999:
            dist_pct = (current_price - vwap_price) / current_price
            if dist_pct <= EMA_PULLBACK_MAX_DIST:
                candidates.append(EntryZone(
                    top=vwap_price + band / 2, bottom=vwap_price - band / 2, mid=vwap_price,
                    type="limit_pullback",
                    description=f"Pullback ao VWAP ({vwap_price:.6g}) — referência institucional.",
                ))

    # Bullish Order Block ativo
    if smc:
        obs = smc.get("order_blocks", [])
        for ob in obs:
            if ob.get("direction") == "bullish" and ob.get("active") and ob.get("top", 0) < current_price:
                candidates.append(EntryZone(
                    top=float(ob["top"]), bottom=float(ob["bottom"]),
                    mid=(float(ob["top"]) + float(ob["bottom"])) / 2,
                    type="limit_ob",
                    description=f"Order Block bullish em {ob['bottom']:.6g}–{ob['top']:.6g} — zona de demanda institucional.",
                ))
                break

        # Bullish FVG
        fvgs = smc.get("fvgs", [])
        for fvg in fvgs:
            if fvg.get("direction") == "bullish" and fvg.get("active") and fvg.get("top", 0) < current_price:
                candidates.append(EntryZone(
                    top=float(fvg["top"]), bottom=float(fvg["bottom"]),
                    mid=(float(fvg["top"]) + float(fvg["bottom"])) / 2,
                    type="limit_fvg_fill",
                    description=f"Preenchimento de FVG bullish ({fvg['bottom']:.6g}–{fvg['top']:.6g}).",
                ))
                break

    # Value Area Low (VAL)
    if vp_vwap:
        vp = vp_vwap.get("volume_profile", {})
        val = vp.get("val")
        if val and val < current_price * 0.999:
            dist_pct = (current_price - val) / current_price
            if dist_pct <= EMA_PULLBACK_MAX_DIST * 1.5:
                candidates.append(EntryZone(
                    top=val + band / 2, bottom=val - band / 2, mid=val,
                    type="limit_value_area",
                    description=f"Entrada no Value Area Low ({val:.6g}) — base de demanda do range.",
                ))

    # Pega o mais próximo do preço atual (entrada mais provável de bater)
    if not candidates:
        return None
    candidates.sort(key=lambda z: current_price - z.mid)  # menor distância primeiro
    return candidates[0]


def _pick_entry_zone_short(
    current_price: float, atr: float, ind: Indicator,
    smc: Optional[dict], vp_vwap: Optional[dict],
) -> Optional[EntryZone]:
    candidates: List[EntryZone] = []
    band = atr * ATR_ENTRY_BAND

    ema21 = ind.ema21
    if ema21 and ema21 > current_price * 1.001:
        dist_pct = (ema21 - current_price) / current_price
        if dist_pct <= EMA_PULLBACK_MAX_DIST:
            candidates.append(EntryZone(
                top=ema21 + band / 2, bottom=ema21 - band / 2, mid=ema21,
                type="limit_pullback",
                description=f"Pullback à EMA21 ({ema21:.6g}) — média dinâmica como resistência.",
            ))

    if vp_vwap:
        vw = vp_vwap.get("vwap", {})
        vwap_price = vw.get("vwap")
        if vwap_price and vwap_price > current_price * 1.001:
            dist_pct = (vwap_price - current_price) / current_price
            if dist_pct <= EMA_PULLBACK_MAX_DIST:
                candidates.append(EntryZone(
                    top=vwap_price + band / 2, bottom=vwap_price - band / 2, mid=vwap_price,
                    type="limit_pullback",
                    description=f"Pullback ao VWAP ({vwap_price:.6g}) — referência institucional.",
                ))

    if smc:
        obs = smc.get("order_blocks", [])
        for ob in obs:
            if ob.get("direction") == "bearish" and ob.get("active") and ob.get("bottom", 0) > current_price:
                candidates.append(EntryZone(
                    top=float(ob["top"]), bottom=float(ob["bottom"]),
                    mid=(float(ob["top"]) + float(ob["bottom"])) / 2,
                    type="limit_ob",
                    description=f"Order Block bearish em {ob['bottom']:.6g}–{ob['top']:.6g} — zona de oferta institucional.",
                ))
                break

        fvgs = smc.get("fvgs", [])
        for fvg in fvgs:
            if fvg.get("direction") == "bearish" and fvg.get("active") and fvg.get("bottom", 0) > current_price:
                candidates.append(EntryZone(
                    top=float(fvg["top"]), bottom=float(fvg["bottom"]),
                    mid=(float(fvg["top"]) + float(fvg["bottom"])) / 2,
                    type="limit_fvg_fill",
                    description=f"Preenchimento de FVG bearish ({fvg['bottom']:.6g}–{fvg['top']:.6g}).",
                ))
                break

    if vp_vwap:
        vp = vp_vwap.get("volume_profile", {})
        vah = vp.get("vah")
        if vah and vah > current_price * 1.001:
            dist_pct = (vah - current_price) / current_price
            if dist_pct <= EMA_PULLBACK_MAX_DIST * 1.5:
                candidates.append(EntryZone(
                    top=vah + band / 2, bottom=vah - band / 2, mid=vah,
                    type="limit_value_area",
                    description=f"Entrada no Value Area High ({vah:.6g}) — topo de oferta do range.",
                ))

    if not candidates:
        return None
    candidates.sort(key=lambda z: z.mid - current_price)
    return candidates[0]


def _structural_stop(
    direction: SignalDirection, entry: float, atr: float,
    df: pd.DataFrame, ind: Indicator, smc: Optional[dict], zone: Optional[EntryZone],
) -> LevelReasoning:
    """Stop baseado em estrutura: último HL/LH, OB bottom, FVG bottom — com buffer ATR."""
    buffer = atr * ATR_BUFFER
    highs_idx, lows_idx = _swing_points(df, lookback=3)

    if direction == SignalDirection.LONG:
        # Prioridade 1: bottom da zona de entrada (OB/FVG) — stop logo abaixo
        if zone and zone.type in ("limit_ob", "limit_fvg_fill"):
            stop = zone.bottom - buffer
            return LevelReasoning(
                price=round(stop, 8),
                reason=f"Stop logo abaixo da zona de entrada ({zone.bottom:.6g}) com folga de {ATR_BUFFER:.1f}×ATR.",
                source="zone_bottom",
            )

        # Prioridade 2: último swing low (HL)
        recent_lows = [df["low"].iloc[i] for i in lows_idx[-5:] if df["low"].iloc[i] < entry]
        if recent_lows:
            swing_low = float(min(recent_lows[-2:])) if len(recent_lows) >= 2 else float(recent_lows[-1])
            stop = swing_low - buffer
            return LevelReasoning(
                price=round(stop, 8),
                reason=f"Stop abaixo do último swing low ({swing_low:.6g}) com folga de {ATR_BUFFER:.1f}×ATR — invalida o HL.",
                source="swing_low",
            )

        # Prioridade 3: pivot_low do Indicator
        if ind.pivot_low and ind.pivot_low < entry:
            stop = float(ind.pivot_low) - buffer
            return LevelReasoning(
                price=round(stop, 8),
                reason=f"Stop abaixo do pivot baixo ({ind.pivot_low:.6g}).",
                source="pivot_low",
            )

        # Fallback: ATR
        stop = entry - atr * 1.5
        return LevelReasoning(
            price=round(stop, 8),
            reason=f"Stop por volatilidade (1.5×ATR) — sem estrutura clara abaixo.",
            source="atr_fallback",
        )

    else:  # SHORT
        if zone and zone.type in ("limit_ob", "limit_fvg_fill"):
            stop = zone.top + buffer
            return LevelReasoning(
                price=round(stop, 8),
                reason=f"Stop logo acima da zona de entrada ({zone.top:.6g}) com folga de {ATR_BUFFER:.1f}×ATR.",
                source="zone_top",
            )

        recent_highs = [df["high"].iloc[i] for i in highs_idx[-5:] if df["high"].iloc[i] > entry]
        if recent_highs:
            swing_high = float(max(recent_highs[-2:])) if len(recent_highs) >= 2 else float(recent_highs[-1])
            stop = swing_high + buffer
            return LevelReasoning(
                price=round(stop, 8),
                reason=f"Stop acima do último swing high ({swing_high:.6g}) com folga de {ATR_BUFFER:.1f}×ATR — invalida o LH.",
                source="swing_high",
            )

        if ind.pivot_high and ind.pivot_high > entry:
            stop = float(ind.pivot_high) + buffer
            return LevelReasoning(
                price=round(stop, 8),
                reason=f"Stop acima do pivot alto ({ind.pivot_high:.6g}).",
                source="pivot_high",
            )

        stop = entry + atr * 1.5
        return LevelReasoning(
            price=round(stop, 8),
            reason=f"Stop por volatilidade (1.5×ATR) — sem estrutura clara acima.",
            source="atr_fallback",
        )


def _liquidity_targets_long(
    entry: float, stop: float, atr: float, df: pd.DataFrame,
    patterns: List[DetectedPattern], vp_vwap: Optional[dict],
) -> Tuple[LevelReasoning, LevelReasoning, LevelReasoning]:
    """TP1=swing high estrutural, TP2=pool de liquidez/VAH, TP3=pattern target ou ATR."""
    highs_idx, _ = _swing_points(df, lookback=3)
    above_highs = sorted([float(df["high"].iloc[i]) for i in highs_idx if df["high"].iloc[i] > entry])
    eq_highs, _ = _equal_highs_lows(df)
    eq_above = sorted([p for p in eq_highs if p > entry])

    # TP1: primeiro swing high acima
    if above_highs:
        tp1_price = above_highs[0]
        tp1 = LevelReasoning(
            price=round(tp1_price, 8),
            reason=f"TP1 no próximo swing high ({tp1_price:.6g}) — primeira resistência estrutural.",
            source="swing_high",
        )
    else:
        tp1_price = entry + atr * 1.5
        tp1 = LevelReasoning(
            price=round(tp1_price, 8),
            reason="TP1 por extensão de 1.5×ATR (sem swing high acima).",
            source="atr_fallback",
        )

    # TP2: pool de liquidez (equal highs) acima do TP1, ou VAH, ou 2º swing high
    tp2_price = None
    if eq_above:
        candidates = [p for p in eq_above if p > tp1_price * 1.005]
        if candidates:
            tp2_price = candidates[0]
            tp2 = LevelReasoning(
                price=round(tp2_price, 8),
                reason=f"TP2 em pool de liquidez ({tp2_price:.6g}) — cluster de topos onde stops vão ser varridos.",
                source="liquidity_pool",
            )
    if tp2_price is None and vp_vwap:
        vah = vp_vwap.get("volume_profile", {}).get("vah")
        if vah and vah > tp1_price * 1.005:
            tp2_price = vah
            tp2 = LevelReasoning(
                price=round(tp2_price, 8),
                reason=f"TP2 no Value Area High ({tp2_price:.6g}) — topo do range estatístico.",
                source="vah",
            )
    if tp2_price is None:
        # 2º swing high ou extensão ATR
        if len(above_highs) >= 2:
            tp2_price = above_highs[1]
            tp2 = LevelReasoning(
                price=round(tp2_price, 8),
                reason=f"TP2 no 2º swing high ({tp2_price:.6g}).",
                source="swing_high",
            )
        else:
            tp2_price = entry + atr * 3.0
            tp2 = LevelReasoning(
                price=round(tp2_price, 8),
                reason="TP2 por extensão de 3×ATR.",
                source="atr_fallback",
            )

    # TP3: pattern target ou último swing high HTF ou extensão ATR
    pattern_targets = [p.breakout_target for p in patterns if p.breakout_target and p.direction == SignalDirection.LONG]
    tp3_price = None
    if pattern_targets:
        best = max(pattern_targets)
        if best > tp2_price * 1.01:
            tp3_price = best
            tp3 = LevelReasoning(
                price=round(tp3_price, 8),
                reason=f"TP3 no alvo de padrão gráfico ({tp3_price:.6g}).",
                source="pattern_target",
            )
    if tp3_price is None:
        tp3_price = entry + atr * 5.0
        tp3 = LevelReasoning(
            price=round(tp3_price, 8),
            reason="TP3 por extensão de 5×ATR (alvo estendido).",
            source="atr_fallback",
        )

    return tp1, tp2, tp3


def _liquidity_targets_short(
    entry: float, stop: float, atr: float, df: pd.DataFrame,
    patterns: List[DetectedPattern], vp_vwap: Optional[dict],
) -> Tuple[LevelReasoning, LevelReasoning, LevelReasoning]:
    _, lows_idx = _swing_points(df, lookback=3)
    below_lows = sorted([float(df["low"].iloc[i]) for i in lows_idx if df["low"].iloc[i] < entry], reverse=True)
    _, eq_lows = _equal_highs_lows(df)
    eq_below = sorted([p for p in eq_lows if p < entry], reverse=True)

    if below_lows:
        tp1_price = below_lows[0]
        tp1 = LevelReasoning(
            price=round(tp1_price, 8),
            reason=f"TP1 no próximo swing low ({tp1_price:.6g}) — primeiro suporte estrutural.",
            source="swing_low",
        )
    else:
        tp1_price = entry - atr * 1.5
        tp1 = LevelReasoning(
            price=round(tp1_price, 8),
            reason="TP1 por extensão de 1.5×ATR.",
            source="atr_fallback",
        )

    tp2_price = None
    if eq_below:
        candidates = [p for p in eq_below if p < tp1_price * 0.995]
        if candidates:
            tp2_price = candidates[0]
            tp2 = LevelReasoning(
                price=round(tp2_price, 8),
                reason=f"TP2 em pool de liquidez ({tp2_price:.6g}) — cluster de fundos.",
                source="liquidity_pool",
            )
    if tp2_price is None and vp_vwap:
        val = vp_vwap.get("volume_profile", {}).get("val")
        if val and val < tp1_price * 0.995:
            tp2_price = val
            tp2 = LevelReasoning(
                price=round(tp2_price, 8),
                reason=f"TP2 no Value Area Low ({tp2_price:.6g}).",
                source="val",
            )
    if tp2_price is None:
        if len(below_lows) >= 2:
            tp2_price = below_lows[1]
            tp2 = LevelReasoning(
                price=round(tp2_price, 8),
                reason=f"TP2 no 2º swing low ({tp2_price:.6g}).",
                source="swing_low",
            )
        else:
            tp2_price = entry - atr * 3.0
            tp2 = LevelReasoning(
                price=round(tp2_price, 8),
                reason="TP2 por extensão de 3×ATR.",
                source="atr_fallback",
            )

    pattern_targets = [p.breakout_target for p in patterns if p.breakout_target and p.direction == SignalDirection.SHORT]
    tp3_price = None
    if pattern_targets:
        best = min(pattern_targets)
        if best < tp2_price * 0.99:
            tp3_price = best
            tp3 = LevelReasoning(
                price=round(tp3_price, 8),
                reason=f"TP3 no alvo de padrão gráfico ({tp3_price:.6g}).",
                source="pattern_target",
            )
    if tp3_price is None:
        tp3_price = entry - atr * 5.0
        tp3 = LevelReasoning(
            price=round(tp3_price, 8),
            reason="TP3 por extensão de 5×ATR.",
            source="atr_fallback",
        )

    return tp1, tp2, tp3


# ─── Entrada pública ──────────────────────────────────────────────────────────
def plan_trade(
    direction: SignalDirection,
    current_price: float,
    df: pd.DataFrame,
    ind: Indicator,
    patterns: List[DetectedPattern],
    smc: Optional[dict] = None,
    vp_vwap: Optional[dict] = None,
) -> TradePlan:
    atr = ind.atr or (current_price * 0.01)
    warnings: List[str] = []

    # ── Zona de entrada ───────────────────────────────────────────────────
    if direction == SignalDirection.LONG:
        zone = _pick_entry_zone_long(current_price, atr, ind, smc, vp_vwap)
    elif direction == SignalDirection.SHORT:
        zone = _pick_entry_zone_short(current_price, atr, ind, smc, vp_vwap)
    else:
        zone = None

    if zone:
        entry = zone.mid
        reasoning_entry = zone.description
    else:
        entry = current_price
        zone = EntryZone(
            top=current_price * 1.002, bottom=current_price * 0.998,
            mid=current_price, type="market",
            description="Sem zona limit clara — entrada a mercado.",
        )
        reasoning_entry = "Entrada a mercado (sem zona estrutural ideal abaixo/acima)."
        warnings.append("Entrada a mercado — R:R sub-ótimo vs limit em zona.")

    # ── Stop ──────────────────────────────────────────────────────────────
    stop_reasoning = _structural_stop(direction, entry, atr, df, ind, smc, zone)
    stop_loss = stop_reasoning.price

    # ── TPs ───────────────────────────────────────────────────────────────
    if direction == SignalDirection.LONG:
        tp1_r, tp2_r, tp3_r = _liquidity_targets_long(entry, stop_loss, atr, df, patterns, vp_vwap)
    elif direction == SignalDirection.SHORT:
        tp1_r, tp2_r, tp3_r = _liquidity_targets_short(entry, stop_loss, atr, df, patterns, vp_vwap)
    else:
        # Neutro: alvos simétricos por ATR (sinal nem deveria operar)
        tp1_r = LevelReasoning(price=round(entry + atr * 1.5, 8), reason="Alvo neutro 1.5×ATR.", source="atr_fallback")
        tp2_r = LevelReasoning(price=round(entry + atr * 3.0, 8), reason="Alvo neutro 3×ATR.", source="atr_fallback")
        tp3_r = LevelReasoning(price=round(entry + atr * 5.0, 8), reason="Alvo neutro 5×ATR.", source="atr_fallback")

    # ── Ordenação coerente e R:R ──────────────────────────────────────────
    if direction == SignalDirection.LONG:
        prices = sorted([tp1_r.price, tp2_r.price, tp3_r.price])
        tp1_r.price, tp2_r.price, tp3_r.price = prices[0], prices[1], prices[2]
    elif direction == SignalDirection.SHORT:
        prices = sorted([tp1_r.price, tp2_r.price, tp3_r.price], reverse=True)
        tp1_r.price, tp2_r.price, tp3_r.price = prices[0], prices[1], prices[2]

    risk = abs(entry - stop_loss)
    rr_tp1 = round(abs(tp1_r.price - entry) / risk, 2) if risk > 0 else 0
    rr_tp2 = round(abs(tp2_r.price - entry) / risk, 2) if risk > 0 else 0
    rr_tp3 = round(abs(tp3_r.price - entry) / risk, 2) if risk > 0 else 0

    if rr_tp2 < MIN_RR_TP2 and direction != SignalDirection.NEUTRAL:
        warnings.append(f"R:R do TP2 ({rr_tp2}) abaixo de {MIN_RR_TP2} — setup com retorno fraco.")

    # Stop muito distante (>5% do preço) = warning
    stop_dist_pct = abs(entry - stop_loss) / entry * 100 if entry else 0
    if stop_dist_pct > 5:
        warnings.append(f"Stop a {stop_dist_pct:.1f}% do preço — exposição alta, considere tamanho reduzido.")

    return TradePlan(
        entry=round(entry, 8),
        entry_zone=zone,
        stop_loss=round(stop_loss, 8),
        tp1=tp1_r.price, tp2=tp2_r.price, tp3=tp3_r.price,
        risk_reward=rr_tp2,
        risk_reward_tp1=rr_tp1,
        risk_reward_tp3=rr_tp3,
        reasoning_entry=reasoning_entry,
        reasoning_stop=stop_reasoning,
        reasoning_tp1=tp1_r,
        reasoning_tp2=tp2_r,
        reasoning_tp3=tp3_r,
        quality_warnings=warnings,
    )
