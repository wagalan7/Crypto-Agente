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

export interface ConfluenceFactor {
  name: string
  category: string
  points: number
  max_points: number
  description: string
  aligned: boolean
}

export interface ConfluenceScore {
  total: number
  max_total: number
  pct: number
  factors: ConfluenceFactor[]
  warnings: string[]
}

export interface SMCZone {
  type: 'order_block' | 'fvg' | 'liquidity_sweep'
  direction: 'bullish' | 'bearish'
  top: number
  bottom: number
  index: number
  timestamp: number
  description: string
  active: boolean
}

export interface StructureSignal {
  type: 'BOS' | 'CHoCH'
  direction: 'bullish' | 'bearish'
  price: number
  index: number
  timestamp: number
  description: string
}

export interface SMCAnalysis {
  order_blocks: SMCZone[]
  fvgs: SMCZone[]
  liquidity_sweeps: SMCZone[]
  structure?: StructureSignal | null
  trend_bias: 'bullish' | 'bearish' | 'neutral'
}

export interface DerivativesData {
  funding_rate?: number | null
  funding_rate_pct?: number | null
  funding_sentiment: 'bullish_squeeze' | 'bearish_squeeze' | 'neutral' | 'extreme_long' | 'extreme_short'
  open_interest?: number | null
  oi_change_24h_pct?: number | null
  oi_sentiment: 'bullish' | 'bearish' | 'neutral'
  description: string
  warnings: string[]
}

export interface PatternStat {
  pattern_type: string
  occurrences: number
  wins: number
  losses: number
  win_rate: number
  avg_bars_to_resolve: number
  sample_size_warning: boolean
}

export interface PatternStats {
  symbol: string
  timeframe: string
  stats: Record<string, PatternStat>
  computed_at: number
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
  ai_critique?: string
  confluence?: ConfluenceScore
  smc?: SMCAnalysis | null
  derivatives?: DerivativesData | null
  pattern_stats?: PatternStats | null
  divergences?: Divergence[] | null
  vp_vwap?: VPVWAPAnalysis | null
  mtf?: MTFAlignment | null
  timestamp: number
  signal_strength: string
}

export interface TFDirection {
  timeframe: string
  direction: 'bullish' | 'bearish' | 'neutral'
  rsi?: number | null
  ema_aligned?: 'bullish' | 'bearish' | 'mixed' | null
  adx?: number | null
  description: string
}

export interface MTFAlignment {
  primary_tf: string
  primary_direction: 'bullish' | 'bearish' | 'neutral'
  higher_tfs: TFDirection[]
  alignment_score: number
  aligned_count: number
  contrary_count: number
  neutral_count: number
  summary: string
}

export interface VolumeProfile {
  poc: number
  vah: number
  val: number
  bins: number[][]
}

export interface VWAPData {
  vwap: number
  upper_1sd: number
  lower_1sd: number
  upper_2sd: number
  lower_2sd: number
  distance_pct: number
}

export interface VPVWAPAnalysis {
  volume_profile: VolumeProfile
  vwap: VWAPData
  description: string
}

export interface Divergence {
  indicator: 'RSI' | 'MACD'
  type: 'regular' | 'hidden'
  direction: 'bullish' | 'bearish'
  price_p1: number
  price_p2: number
  ind_p1: number
  ind_p2: number
  index_p1: number
  index_p2: number
  strength: number
  description: string
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

export interface FibonacciDrawing {
  id: string
  type: 'fibonacci'
  p1: { price: number; time: number }
  p2: { price: number; time: number }
  color: string
}

export interface RectangleDrawing {
  id: string
  type: 'rectangle'
  p1: { price: number; time: number }
  p2: { price: number; time: number }
  color: string
}

export type UserDrawing = HLineDrawing | TrendLineDrawing | FibonacciDrawing | RectangleDrawing
export type DrawingTool = 'cursor' | 'hline' | 'trendline' | 'fibonacci' | 'rectangle'
