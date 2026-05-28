"""
Backtest histórico de padrões — win-rate por (symbol, timeframe, pattern_type).

Roda o detector de padrões em janelas deslizantes do histórico e verifica,
para cada detecção passada, se o preço atingiu o `breakout_target` ou foi
invalidado (atingiu stop conceitual baseado em ATR) primeiro.

Resultado cacheado em memória por (symbol, timeframe).
"""
from __future__ import annotations
from typing import Dict, List, Optional
from pydantic import BaseModel
import pandas as pd
import time

from services.pattern_service import detect_all_patterns
from models.trade_signal import PatternType, SignalDirection


class PatternStat(BaseModel):
    pattern_type: str
    occurrences: int
    wins: int
    losses: int
    win_rate: float       # 0–1
    avg_bars_to_resolve: float
    sample_size_warning: bool   # True se occurrences < 5


class PatternStats(BaseModel):
    symbol: str
    timeframe: str
    stats: Dict[str, PatternStat]
    computed_at: int


# Cache em memória: (symbol, tf) → PatternStats
_cache: Dict[str, PatternStats] = {}
_CACHE_TTL = 3600  # 1h


def _resolve_outcome(
    df: pd.DataFrame,
    idx: int,
    direction: SignalDirection,
    target: Optional[float],
    horizon: int = 50,
) -> tuple[Optional[bool], int]:
    """
    A partir da barra `idx`, verifica se preço atinge `target` (win) ou
    invalida (move 1.5x do range do padrão na direção oposta) dentro de `horizon` barras.
    Retorna (win?, bars_to_resolve). None se inconclusivo.
    """
    if target is None or idx + 1 >= len(df):
        return None, 0

    entry = float(df["close"].iloc[idx])
    # Stop = 1.5% para o lado oposto (heurística sem ATR completo aqui)
    stop_pct = 0.015
    if direction == SignalDirection.LONG:
        stop = entry * (1 - stop_pct)
    elif direction == SignalDirection.SHORT:
        stop = entry * (1 + stop_pct)
    else:
        return None, 0

    end = min(idx + 1 + horizon, len(df))
    for j in range(idx + 1, end):
        hi = float(df["high"].iloc[j])
        lo = float(df["low"].iloc[j])
        if direction == SignalDirection.LONG:
            if hi >= target:
                return True, j - idx
            if lo <= stop:
                return False, j - idx
        else:
            if lo <= target:
                return True, j - idx
            if hi >= stop:
                return False, j - idx

    return None, end - idx


def compute_pattern_stats(symbol: str, timeframe: str, df: pd.DataFrame) -> PatternStats:
    """
    Roda detector em janelas deslizantes do histórico. Cacheia o resultado.
    """
    key = f"{symbol}|{timeframe}"
    now = int(time.time())
    cached = _cache.get(key)
    if cached and (now - cached.computed_at) < _CACHE_TTL:
        return cached

    stats_acc: Dict[str, Dict] = {}   # type → {wins, losses, total, bars[]}
    n = len(df)
    if n < 100:
        result = PatternStats(symbol=symbol, timeframe=timeframe, stats={}, computed_at=now)
        _cache[key] = result
        return result

    # Janelas deslizantes a cada 10 barras, mínimo 50 barras de contexto
    step = 10
    window = 80
    for start in range(0, n - window - 30, step):
        sub = df.iloc[start:start + window].reset_index(drop=True)
        try:
            pats = detect_all_patterns(sub)
        except Exception:
            continue

        absolute_idx = start + window - 1
        for p in pats[:3]:  # top 3 do snapshot
            t = p.type.value
            acc = stats_acc.setdefault(t, {"wins": 0, "losses": 0, "total": 0, "bars": []})
            win, bars = _resolve_outcome(df, absolute_idx, p.direction, p.breakout_target)
            if win is None:
                continue
            acc["total"] += 1
            if win:
                acc["wins"] += 1
            else:
                acc["losses"] += 1
            acc["bars"].append(bars)

    out: Dict[str, PatternStat] = {}
    for t, acc in stats_acc.items():
        total = acc["wins"] + acc["losses"]
        if total == 0:
            continue
        wr = acc["wins"] / total
        avg_bars = sum(acc["bars"]) / len(acc["bars"]) if acc["bars"] else 0
        out[t] = PatternStat(
            pattern_type=t,
            occurrences=total,
            wins=acc["wins"],
            losses=acc["losses"],
            win_rate=round(wr, 3),
            avg_bars_to_resolve=round(avg_bars, 1),
            sample_size_warning=total < 5,
        )

    result = PatternStats(symbol=symbol, timeframe=timeframe, stats=out, computed_at=now)
    _cache[key] = result
    return result


def get_win_rate(symbol: str, timeframe: str, pattern_type: str, df: pd.DataFrame) -> Optional[PatternStat]:
    stats = compute_pattern_stats(symbol, timeframe, df)
    return stats.stats.get(pattern_type)
