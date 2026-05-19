"""
Multi-Timeframe Alignment (MTF).

Verifica se a direção do sinal está alinhada com timeframes superiores.
Regra de mercado: operar A FAVOR da tendência maior aumenta drasticamente
o win-rate. Contrariar TF superior dobra o risco.

Para cada TF primário define-se 1-2 TFs superiores de confirmação.
"""
from __future__ import annotations
from typing import Optional, List, Dict
from pydantic import BaseModel
import asyncio

from services.binance_service import fetch_ohlcv
from services.indicator_service import calculate_indicators
from services.pattern_service import detect_all_patterns
from services.signal_service import determine_direction
from models.trade_signal import SignalDirection


# Mapa de TFs primários → superiores para conferir alinhamento
MTF_MAP: Dict[str, List[str]] = {
    "1m":  ["5m",  "15m"],
    "5m":  ["15m", "1h"],
    "15m": ["1h",  "4h"],
    "30m": ["1h",  "4h"],
    "1h":  ["4h",  "1d"],
    "4h":  ["1d",  "3d"],
    "6h":  ["1d",  "3d"],
    "8h":  ["1d",  "3d"],
    "12h": ["1d",  "3d"],
    "1d":  ["3d"],
    "3d":  ["1d"],
}


class TFDirection(BaseModel):
    timeframe: str
    direction: str         # bullish | bearish | neutral
    rsi: Optional[float] = None
    ema_aligned: Optional[str] = None   # bullish | bearish | mixed
    adx: Optional[float] = None
    description: str


class MTFAlignment(BaseModel):
    primary_tf: str
    primary_direction: str
    higher_tfs: List[TFDirection]
    alignment_score: float       # -1 (todos contra) a +1 (todos a favor)
    aligned_count: int
    contrary_count: int
    neutral_count: int
    summary: str


def _direction_word(d: SignalDirection) -> str:
    if d == SignalDirection.LONG:
        return "bullish"
    if d == SignalDirection.SHORT:
        return "bearish"
    return "neutral"


async def _analyze_tf(symbol: str, tf: str) -> Optional[TFDirection]:
    try:
        df = await fetch_ohlcv(symbol, tf, limit=200)
        if df.empty or len(df) < 50:
            return None
        ind = calculate_indicators(df)
        pats = detect_all_patterns(df)
        current = float(df["close"].iloc[-1])
        dir_enum = determine_direction(ind, pats, current)
        dir_word = _direction_word(dir_enum)

        # EMAs alignment label
        ema_label = None
        if ind.ema9 and ind.ema21 and ind.ema50:
            if ind.ema9 > ind.ema21 > ind.ema50:
                ema_label = "bullish"
            elif ind.ema9 < ind.ema21 < ind.ema50:
                ema_label = "bearish"
            else:
                ema_label = "mixed"

        # Descrição PT-BR
        bits = []
        if ind.rsi is not None:
            bits.append(f"RSI {ind.rsi:.1f}")
        if ema_label:
            bits.append(f"EMAs {ema_label}")
        if ind.adx is not None:
            bits.append(f"ADX {ind.adx:.0f}")
        desc = f"{tf}: {dir_word.upper()}" + (f" ({', '.join(bits)})" if bits else "")

        return TFDirection(
            timeframe=tf,
            direction=dir_word,
            rsi=ind.rsi,
            ema_aligned=ema_label,
            adx=ind.adx,
            description=desc,
        )
    except Exception:
        return None


async def analyze_mtf(symbol: str, primary_tf: str, primary_direction: SignalDirection) -> Optional[MTFAlignment]:
    higher_tfs = MTF_MAP.get(primary_tf, [])
    if not higher_tfs:
        return None

    results = await asyncio.gather(*[_analyze_tf(symbol, tf) for tf in higher_tfs])
    valid = [r for r in results if r is not None]
    if not valid:
        return None

    primary_word = _direction_word(primary_direction)
    aligned = sum(1 for r in valid if r.direction == primary_word and primary_word != "neutral")
    contrary = sum(1 for r in valid if r.direction != primary_word and r.direction != "neutral" and primary_word != "neutral")
    neutral = sum(1 for r in valid if r.direction == "neutral")
    total = len(valid)
    score = (aligned - contrary) / total if total > 0 else 0.0

    # Resumo PT-BR
    if primary_word == "neutral":
        summary = f"Sinal primário neutro. TFs superiores: " + ", ".join(f"{r.timeframe}={r.direction}" for r in valid) + "."
    elif aligned == total:
        summary = f"✓ Todos os TFs superiores ({', '.join(r.timeframe for r in valid)}) confirmam a direção — sinal de alta qualidade."
    elif contrary == total:
        summary = f"✗ Todos os TFs superiores ({', '.join(r.timeframe for r in valid)}) apontam direção CONTRÁRIA — risco extremo."
    elif aligned > contrary:
        summary = f"~ Alinhamento parcial: {aligned}/{total} TFs superiores a favor."
    elif contrary > aligned:
        summary = f"⚠ Maioria dos TFs superiores contraria o sinal ({contrary}/{total})."
    else:
        summary = f"TFs superiores mistos: {aligned} a favor, {contrary} contra, {neutral} neutros."

    return MTFAlignment(
        primary_tf=primary_tf,
        primary_direction=primary_word,
        higher_tfs=valid,
        alignment_score=round(score, 2),
        aligned_count=aligned,
        contrary_count=contrary,
        neutral_count=neutral,
        summary=summary,
    )
