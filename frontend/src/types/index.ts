export type SignalDirection = 'long' | 'short' | 'neutral'
export type TradeType = 'scalp' | 'day_trade' | 'swing' | 'hodl'
export type PatternType =
  | 'ascending_wedge' | 'descending_wedge'
  | 'symmetric_triangle' | 'ascending_triangle' | 'descending_triangle'
  | 'ascending_channel' | 'descending_channel' | 'horizontal_channel'
  | 'lta' | 'ltb'
  | 'head_and_shoulders' | 'inverse_head_and_shoulders'
  | 'double_top' | 'double_bottom'
  | 'cup_and_handle' | 'bull_flag' | 'bear_flag'

export interface PatternPoint {
  index: number
  timestamp: number
  price: number
}

export interface DetectedPattern {
  type: PatternType
  confidence: number
  direction: SignalDirection
  points: PatternPoint[]
  lines: number[][] | null
  description: string
  breakout_target: number | null
}

export interface Indicator {
  rsi?: number
  macd?: number
  macd_signal?: number
  macd_hist?: number
  bb_upper?: number
  bb_middle?: number
  bb_lower?: number
  ema9?: number
  ema21?: number
  ema50?: number
  ema200?: number
  atr?: number
  adx?: number
  stoch_k?: number
  stoch_d?: number
  obv?: number
  volume_avg?: number
  supertrend?: number
  supertrend_direction?: number
  pivot_high?: number
  pivot_low?: number
}

export interface TradeSignal {
  symbol: string
  timeframe: string
  direction: SignalDirection
  trade_type: TradeType
  confidence: number
  entry: number
  stop_loss: number
  tp1: number
  tp2: number
  tp3: number
  risk_reward: number
  patterns: DetectedPattern[]
  indicators: Indicator
  ai_analysis?: string
  timestamp: number
  signal_strength: string
}

export interface OHLCVCandle {
  timestamp: number
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export interface Ticker {
  symbol: string
  last: number
  change: number
  volume: number
  high: number
  low: number
  bid: number
  ask: number
}

export interface WatchlistItem {
  symbol: string
  direction?: SignalDirection
  confidence?: number
  signal_strength?: string
  trade_type?: TradeType
  rsi?: number
  patterns_count?: number
}

export interface HLineDrawing {
  id: string
  type: 'hline'
  price: number
  color: string
  label: string
}

export interface TrendLineDrawing {
  id: string
  type: 'trendline'
  p1: { price: number; time: number }
  p2: { price: number; time: number }
  color: string
}

export type UserDrawing = HLineDrawing | TrendLineDrawing
export type DrawingTool = 'cursor' | 'hline' | 'trendline' | 'rectangle'
