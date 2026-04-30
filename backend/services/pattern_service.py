import numpy as np
import pandas as pd
from scipy.signal import argrelextrema
from typing import List, Tuple, Optional
from models.trade_signal import DetectedPattern, PatternType, PatternPoint, SignalDirection


def find_pivots(df: pd.DataFrame, order: int = 5) -> Tuple[np.ndarray, np.ndarray]:
    """Find local highs and lows using scipy argrelextrema."""
    highs = df["high"].values
    lows = df["low"].values
    pivot_highs = argrelextrema(highs, np.greater_equal, order=order)[0]
    pivot_lows = argrelextrema(lows, np.less_equal, order=order)[0]
    return pivot_highs, pivot_lows


def fit_line(x: np.ndarray, y: np.ndarray) -> Tuple[float, float, float]:
    """Linear regression: returns slope, intercept, r2."""
    if len(x) < 2:
        return 0.0, 0.0, 0.0
    coeffs = np.polyfit(x, y, 1)
    slope, intercept = coeffs
    y_pred = np.polyval(coeffs, x)
    ss_res = np.sum((y - y_pred) ** 2)
    ss_tot = np.sum((y - np.mean(y)) ** 2)
    r2 = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0
    return float(slope), float(intercept), float(r2)


def make_point(idx: int, df: pd.DataFrame, price_col: str = "close") -> PatternPoint:
    return PatternPoint(
        index=int(idx),
        timestamp=int(df["timestamp"].iloc[idx]),
        price=float(df[price_col].iloc[idx]),
    )


def detect_trendlines(df: pd.DataFrame) -> List[DetectedPattern]:
    """Detect LTA (uptrend) and LTB (downtrend) lines."""
    patterns = []
    ph_idx, pl_idx = find_pivots(df, order=5)
    n = len(df)

    # LTA - connect rising lows
    if len(pl_idx) >= 2:
        lows_x = pl_idx[-6:]
        lows_y = df["low"].values[lows_x]
        slope, intercept, r2 = fit_line(lows_x.astype(float), lows_y)
        if slope > 0 and r2 > 0.7:
            pts = [make_point(i, df, "low") for i in lows_x[-3:]]
            # project trendline to current bar
            proj_price = slope * (n - 1) + intercept
            patterns.append(DetectedPattern(
                type=PatternType.LTA,
                confidence=round(min(r2, 0.99), 2),
                direction=SignalDirection.LONG,
                points=pts,
                lines=[[float(lows_x[0]), float(lows_y[0]), float(lows_x[-1]), lows_y[-1]], [float(n - 1), proj_price]],
                description=f"Linha de Tendência de Alta (LTA) — suporte dinâmico em {proj_price:.4f}",
                breakout_target=None,
            ))

    # LTB - connect falling highs
    if len(ph_idx) >= 2:
        highs_x = ph_idx[-6:]
        highs_y = df["high"].values[highs_x]
        slope, intercept, r2 = fit_line(highs_x.astype(float), highs_y)
        if slope < 0 and r2 > 0.7:
            pts = [make_point(i, df, "high") for i in highs_x[-3:]]
            proj_price = slope * (n - 1) + intercept
            patterns.append(DetectedPattern(
                type=PatternType.LTB,
                confidence=round(min(r2, 0.99), 2),
                direction=SignalDirection.SHORT,
                points=pts,
                lines=[[float(highs_x[0]), float(highs_y[0]), float(highs_x[-1]), highs_y[-1]], [float(n - 1), proj_price]],
                description=f"Linha de Tendência de Baixa (LTB) — resistência dinâmica em {proj_price:.4f}",
                breakout_target=None,
            ))

    return patterns


def detect_channels(df: pd.DataFrame) -> List[DetectedPattern]:
    patterns = []
    ph_idx, pl_idx = find_pivots(df, order=5)
    n = len(df)

    min_pts = 2
    if len(ph_idx) < min_pts or len(pl_idx) < min_pts:
        return patterns

    hx = ph_idx[-4:].astype(float)
    hy = df["high"].values[ph_idx[-4:]]
    lx = pl_idx[-4:].astype(float)
    ly = df["low"].values[pl_idx[-4:]]

    h_slope, h_int, h_r2 = fit_line(hx, hy)
    l_slope, l_int, l_r2 = fit_line(lx, ly)

    if h_r2 < 0.7 or l_r2 < 0.7:
        return patterns

    slope_diff = abs(h_slope - l_slope)
    avg_price = df["close"].mean()
    relative_diff = slope_diff / avg_price

    if relative_diff < 0.0005:
        if abs(h_slope) < avg_price * 0.0001:
            ptype = PatternType.HORIZONTAL_CHANNEL
            direction = SignalDirection.NEUTRAL
            desc = "Canal Horizontal — range definido"
        elif h_slope > 0:
            ptype = PatternType.ASCENDING_CHANNEL
            direction = SignalDirection.LONG
            desc = "Canal de Alta — tendência de alta com suporte/resistência paralelos"
        else:
            ptype = PatternType.DESCENDING_CHANNEL
            direction = SignalDirection.SHORT
            desc = "Canal de Baixa — tendência de queda com suporte/resistência paralelos"

        h_proj = h_slope * (n - 1) + h_int
        l_proj = l_slope * (n - 1) + l_int
        patterns.append(DetectedPattern(
            type=ptype,
            confidence=round(min((h_r2 + l_r2) / 2, 0.99), 2),
            direction=direction,
            points=[make_point(int(ph_idx[-1]), df, "high"), make_point(int(pl_idx[-1]), df, "low")],
            lines=[[hx[0], hy[0], hx[-1], hy[-1]], [lx[0], ly[0], lx[-1], ly[-1]]],
            description=f"{desc} — topo em {h_proj:.4f}, fundo em {l_proj:.4f}",
            breakout_target=h_proj * 1.02 if direction == SignalDirection.LONG else l_proj * 0.98,
        ))

    return patterns


def detect_triangles_wedges(df: pd.DataFrame) -> List[DetectedPattern]:
    patterns = []
    ph_idx, pl_idx = find_pivots(df, order=5)
    n = len(df)

    if len(ph_idx) < 2 or len(pl_idx) < 2:
        return patterns

    hx = ph_idx[-4:].astype(float)
    hy = df["high"].values[ph_idx[-4:]]
    lx = pl_idx[-4:].astype(float)
    ly = df["low"].values[pl_idx[-4:]]

    h_slope, h_int, h_r2 = fit_line(hx, hy)
    l_slope, l_int, l_r2 = fit_line(lx, ly)

    if h_r2 < 0.65 or l_r2 < 0.65:
        return patterns

    avg_price = df["close"].mean()
    h_proj = h_slope * (n - 1) + h_int
    l_proj = l_slope * (n - 1) + l_int
    channel_width = abs(h_proj - l_proj) / avg_price

    converging = (h_slope < 0 and l_slope > 0) or (abs(h_slope) + abs(l_slope) < abs(h_slope - l_slope) * 0.1)
    both_up = h_slope > 0 and l_slope > 0
    both_down = h_slope < 0 and l_slope < 0

    if converging and channel_width < 0.05:
        # Triangle variants
        if abs(h_slope) < avg_price * 0.0001 and l_slope > 0:
            ptype = PatternType.ASCENDING_TRIANGLE
            direction = SignalDirection.LONG
            target = h_proj * 1.05
            desc = "Triângulo Ascendente — resistência horizontal + suporte crescente"
        elif abs(l_slope) < avg_price * 0.0001 and h_slope < 0:
            ptype = PatternType.DESCENDING_TRIANGLE
            direction = SignalDirection.SHORT
            target = l_proj * 0.95
            desc = "Triângulo Descendente — suporte horizontal + resistência decrescente"
        else:
            ptype = PatternType.SYMMETRIC_TRIANGLE
            direction = SignalDirection.NEUTRAL
            target = None
            desc = "Triângulo Simétrico — aguardando rompimento"

        patterns.append(DetectedPattern(
            type=ptype,
            confidence=round(min((h_r2 + l_r2) / 2, 0.97), 2),
            direction=direction,
            points=[make_point(int(ph_idx[-1]), df, "high"), make_point(int(pl_idx[-1]), df, "low")],
            lines=[[hx[0], hy[0], hx[-1], hy[-1]], [lx[0], ly[0], lx[-1], ly[-1]]],
            description=desc,
            breakout_target=target,
        ))

    elif both_up and h_slope < l_slope * 1.5:
        # Rising wedge — bearish
        target = l_proj * 0.95
        patterns.append(DetectedPattern(
            type=PatternType.ASCENDING_WEDGE,
            confidence=round(min((h_r2 + l_r2) / 2, 0.95), 2),
            direction=SignalDirection.SHORT,
            points=[make_point(int(ph_idx[-1]), df, "high"), make_point(int(pl_idx[-1]), df, "low")],
            lines=[[hx[0], hy[0], hx[-1], hy[-1]], [lx[0], ly[0], lx[-1], ly[-1]]],
            description="Cunha Ascendente (Bearish) — compressão de alta sinalizando reversão",
            breakout_target=target,
        ))

    elif both_down and abs(l_slope) < abs(h_slope) * 1.5:
        # Falling wedge — bullish
        target = h_proj * 1.05
        patterns.append(DetectedPattern(
            type=PatternType.DESCENDING_WEDGE,
            confidence=round(min((h_r2 + l_r2) / 2, 0.95), 2),
            direction=SignalDirection.LONG,
            points=[make_point(int(ph_idx[-1]), df, "high"), make_point(int(pl_idx[-1]), df, "low")],
            lines=[[hx[0], hy[0], hx[-1], hy[-1]], [lx[0], ly[0], lx[-1], ly[-1]]],
            description="Cunha Descendente (Bullish) — compressão de baixa sinalizando reversão",
            breakout_target=target,
        ))

    return patterns


def detect_double_tops_bottoms(df: pd.DataFrame) -> List[DetectedPattern]:
    patterns = []
    ph_idx, pl_idx = find_pivots(df, order=8)

    if len(ph_idx) >= 2:
        last_two_highs = ph_idx[-2:]
        p1 = df["high"].iloc[last_two_highs[0]]
        p2 = df["high"].iloc[last_two_highs[1]]
        if abs(p1 - p2) / p1 < 0.02:
            valley_between = df["low"].iloc[last_two_highs[0]:last_two_highs[1]].min()
            neckline = valley_between
            target = neckline - (p1 - neckline)
            patterns.append(DetectedPattern(
                type=PatternType.DOUBLE_TOP,
                confidence=0.82,
                direction=SignalDirection.SHORT,
                points=[make_point(int(last_two_highs[0]), df, "high"), make_point(int(last_two_highs[1]), df, "high")],
                lines=[[float(last_two_highs[0]), float(p1), float(last_two_highs[1]), float(p2)]],
                description=f"Topo Duplo — neckline em {neckline:.4f}, alvo em {target:.4f}",
                breakout_target=float(target),
            ))

    if len(pl_idx) >= 2:
        last_two_lows = pl_idx[-2:]
        p1 = df["low"].iloc[last_two_lows[0]]
        p2 = df["low"].iloc[last_two_lows[1]]
        if abs(p1 - p2) / p1 < 0.02:
            peak_between = df["high"].iloc[last_two_lows[0]:last_two_lows[1]].max()
            neckline = peak_between
            target = neckline + (neckline - p1)
            patterns.append(DetectedPattern(
                type=PatternType.DOUBLE_BOTTOM,
                confidence=0.82,
                direction=SignalDirection.LONG,
                points=[make_point(int(last_two_lows[0]), df, "low"), make_point(int(last_two_lows[1]), df, "low")],
                lines=[[float(last_two_lows[0]), float(p1), float(last_two_lows[1]), float(p2)]],
                description=f"Fundo Duplo — neckline em {neckline:.4f}, alvo em {target:.4f}",
                breakout_target=float(target),
            ))

    return patterns


def detect_head_and_shoulders(df: pd.DataFrame) -> List[DetectedPattern]:
    patterns = []
    ph_idx, pl_idx = find_pivots(df, order=8)

    if len(ph_idx) >= 3:
        h = ph_idx[-3:]
        p1, p2, p3 = df["high"].iloc[h[0]], df["high"].iloc[h[1]], df["high"].iloc[h[2]]
        shoulders_avg = (p1 + p3) / 2
        if p2 > p1 and p2 > p3 and abs(p1 - p3) / p1 < 0.03:
            # H&S between pivot lows
            neckline = df["low"].iloc[h[0]:h[2]].mean()
            target = neckline - (p2 - neckline)
            patterns.append(DetectedPattern(
                type=PatternType.HEAD_AND_SHOULDERS,
                confidence=0.78,
                direction=SignalDirection.SHORT,
                points=[make_point(int(h[0]), df, "high"), make_point(int(h[1]), df, "high"), make_point(int(h[2]), df, "high")],
                lines=[[float(h[0]), float(p1), float(h[1]), float(p2)], [float(h[1]), float(p2), float(h[2]), float(p3)]],
                description=f"Ombro-Cabeça-Ombro — reversão bearish, neckline {neckline:.4f}, alvo {target:.4f}",
                breakout_target=float(target),
            ))

    if len(pl_idx) >= 3:
        l = pl_idx[-3:]
        p1, p2, p3 = df["low"].iloc[l[0]], df["low"].iloc[l[1]], df["low"].iloc[l[2]]
        if p2 < p1 and p2 < p3 and abs(p1 - p3) / p1 < 0.03:
            neckline = df["high"].iloc[l[0]:l[2]].mean()
            target = neckline + (neckline - p2)
            patterns.append(DetectedPattern(
                type=PatternType.INVERSE_HEAD_AND_SHOULDERS,
                confidence=0.78,
                direction=SignalDirection.LONG,
                points=[make_point(int(l[0]), df, "low"), make_point(int(l[1]), df, "low"), make_point(int(l[2]), df, "low")],
                lines=[[float(l[0]), float(p1), float(l[1]), float(p2)], [float(l[1]), float(p2), float(l[2]), float(p3)]],
                description=f"OCO Invertido — reversão bullish, neckline {neckline:.4f}, alvo {target:.4f}",
                breakout_target=float(target),
            ))

    return patterns


def detect_flags(df: pd.DataFrame) -> List[DetectedPattern]:
    patterns = []
    closes = df["close"].values
    n = len(closes)
    if n < 30:
        return patterns

    # Look for sharp move followed by consolidation
    window = 20
    recent = closes[-window:]
    pre_move = closes[-window * 2:-window]

    pre_change = (pre_move[-1] - pre_move[0]) / pre_move[0]
    recent_range = (recent.max() - recent.min()) / recent.mean()

    if abs(pre_change) > 0.05 and recent_range < 0.03:
        if pre_change > 0:
            target = recent[-1] + (pre_move[-1] - pre_move[0])
            patterns.append(DetectedPattern(
                type=PatternType.BULL_FLAG,
                confidence=0.72,
                direction=SignalDirection.LONG,
                points=[make_point(n - window * 2, df), make_point(n - window, df), make_point(n - 1, df)],
                lines=None,
                description=f"Bandeira de Alta (Bull Flag) — consolidação após impulso, alvo {target:.4f}",
                breakout_target=float(target),
            ))
        else:
            target = recent[-1] - abs(pre_move[-1] - pre_move[0])
            patterns.append(DetectedPattern(
                type=PatternType.BEAR_FLAG,
                confidence=0.72,
                direction=SignalDirection.SHORT,
                points=[make_point(n - window * 2, df), make_point(n - window, df), make_point(n - 1, df)],
                lines=None,
                description=f"Bandeira de Baixa (Bear Flag) — consolidação após queda, alvo {target:.4f}",
                breakout_target=float(target),
            ))

    return patterns


def detect_all_patterns(df: pd.DataFrame) -> List[DetectedPattern]:
    if len(df) < 50:
        return []

    all_patterns: List[DetectedPattern] = []
    for detector in [
        detect_trendlines,
        detect_channels,
        detect_triangles_wedges,
        detect_double_tops_bottoms,
        detect_head_and_shoulders,
        detect_flags,
    ]:
        try:
            all_patterns.extend(detector(df))
        except Exception:
            pass

    # Sort by confidence descending
    all_patterns.sort(key=lambda p: p.confidence, reverse=True)
    return all_patterns[:8]
