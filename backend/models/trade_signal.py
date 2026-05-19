from pydantic import BaseModel
from typing import Optional, List
from enum import Enum


class TradeType(str, Enum):
    SCALP = "scalp"
    DAY_TRADE = "day_trade"
    SWING = "swing"
    HODL = "hodl"


class SignalDirection(str, Enum):
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"


class PatternType(str, Enum):
    ASCENDING_WEDGE = "ascending_wedge"
    DESCENDING_WEDGE = "descending_wedge"
    SYMMETRIC_TRIANGLE = "symmetric_triangle"
    ASCENDING_TRIANGLE = "ascending_triangle"
    DESCENDING_TRIANGLE = "descending_triangle"
    ASCENDING_CHANNEL = "ascending_channel"
    DESCENDING_CHANNEL = "descending_channel"
    HORIZONTAL_CHANNEL = "horizontal_channel"
    LTA = "lta"
    LTB = "ltb"
    HEAD_AND_SHOULDERS = "head_and_shoulders"
    INVERSE_HEAD_AND_SHOULDERS = "inverse_head_and_shoulders"
    DOUBLE_TOP = "double_top"
    DOUBLE_BOTTOM = "double_bottom"
    CUP_AND_HANDLE = "cup_and_handle"
    BULL_FLAG = "bull_flag"
    BEAR_FLAG = "bear_flag"


class PatternPoint(BaseModel):
    index: int
    timestamp: int
    price: float


class DetectedPattern(BaseModel):
    type: PatternType
    confidence: float
    direction: SignalDirection
    points: List[PatternPoint]
    lines: Optional[List[List[float]]] = None
    description: str
    breakout_target: Optional[float] = None


class Indicator(BaseModel):
    rsi: Optional[float] = None
    macd: Optional[float] = None
    macd_signal: Optional[float] = None
    macd_hist: Optional[float] = None
    bb_upper: Optional[float] = None
    bb_middle: Optional[float] = None
    bb_lower: Optional[float] = None
    ema9: Optional[float] = None
    ema21: Optional[float] = None
    ema50: Optional[float] = None
    ema200: Optional[float] = None
    atr: Optional[float] = None
    adx: Optional[float] = None
    stoch_k: Optional[float] = None
    stoch_d: Optional[float] = None
    obv: Optional[float] = None
    volume_avg: Optional[float] = None
    supertrend: Optional[float] = None
    supertrend_direction: Optional[int] = None
    pivot_high: Optional[float] = None
    pivot_low: Optional[float] = None


class ConfluenceFactor(BaseModel):
    name: str           # ex: "RSI sobrevenda"
    category: str       # ex: "momentum" | "trend" | "volume" | "pattern" | "macro" | "structure"
    points: float       # pontos somados (positivo = a favor da direção)
    max_points: float   # pontos máximos possíveis dessa categoria
    description: str    # justificativa em PT-BR
    aligned: bool       # True se contribui para a direção do sinal


class ConfluenceScore(BaseModel):
    total: float
    max_total: float
    pct: float                       # 0–100
    factors: List[ConfluenceFactor]
    warnings: List[str] = []         # red-flags (ex: "divergência baixista no RSI")


class TradeSignal(BaseModel):
    symbol: str
    timeframe: str
    direction: SignalDirection
    trade_type: TradeType
    confidence: float
    entry: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    risk_reward: float
    patterns: List[DetectedPattern]
    indicators: Indicator
    ai_analysis: Optional[str] = None
    ai_critique: Optional[str] = None         # self-critique da IA
    confluence: Optional[ConfluenceScore] = None
    smc: Optional[dict] = None                # SMCAnalysis serializado
    derivatives: Optional[dict] = None        # DerivativesData serializado
    pattern_stats: Optional[dict] = None      # {pattern_type: PatternStat}
    divergences: Optional[list] = None        # List[Divergence] serializado
    vp_vwap: Optional[dict] = None            # VPVWAPAnalysis serializado
    mtf: Optional[dict] = None                # MTFAlignment serializado
    trade_plan: Optional[dict] = None         # TradePlan (Sprint B): zona de entrada, stop estrutural, alvos por liquidez + reasoning
    timestamp: int
    signal_strength: str
