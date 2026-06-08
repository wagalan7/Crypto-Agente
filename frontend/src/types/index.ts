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
  trade_plan?: TradePlan | null
  timestamp: number
  signal_strength: string
}

export interface LevelReasoning {
  price: number
  reason: string
  source: string
}

export interface EntryZone {
  top: number
  bottom: number
  mid: number
  type: 'limit_pullback' | 'limit_retest' | 'limit_fvg_fill' | 'limit_ob' | 'market' | 'limit_value_area'
  description: string
}

export interface TradePlan {
  entry: number
  entry_zone?: EntryZone | null
  stop_loss: number
  tp1: number
  tp2: number
  tp3: number
  risk_reward: number
  risk_reward_tp1: number
  risk_reward_tp3: number
  reasoning_entry: string
  reasoning_stop: LevelReasoning
  reasoning_tp1: LevelReasoning
  reasoning_tp2: LevelReasoning
  reasoning_tp3: LevelReasoning
  quality_warnings: string[]
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

export type RecommendationTier = 'A+' | 'A' | 'B'

export interface Recommendation {
  tier: RecommendationTier
  score: number
  symbol: string
  timeframe: string
  direction: 'long' | 'short'
  confidence: number
  risk_reward: number
  entry: number
  stop_loss: number
  tp2: number
  summary: string
  warnings: string[]
  signal: TradeSignal
  leverage: number
  risk_pct: number
  margin_pct: number
  stop_distance_pct: number
  // Entry zone + chase flag (Sprint v2)
  entry_zone_low?: number | null
  entry_zone_high?: number | null
  entry_zone_type?: string | null
  current_price?: number | null
  chase_atr?: number | null
  chase_level?: 'ok' | 'extended' | 'chasing' | null
  // P(TP1) calibrada empiricamente — null se calibração não disponível
  prob_tp1?: number | null
  // Position sizing dinâmico (Kelly fracionado × score × vol) — % da banca sugerido
  suggested_size_pct?: number | null
  size_rationale?: string | null
  // Setup já foi resolvido nas últimas 2h (mesmo symbol+tf+direction)
  recent_outcome?: {
    status: 'won_tp1' | 'won_tp1_be' | 'won_tp2' | 'lost'
    realized_r: number | null
    resolved_at: string | null
    entry: number
  } | null
}

// Operação real/manual em aberto ou fechada (backend RealTrade). Espelha o
// shape de _to_dict do real_trade_service.
export interface RealTradeRow {
  id: number
  symbol: string
  side: 'long' | 'short' | string
  source: 'manual' | 'auto' | 'shadow' | string
  recommendation_id: number | null
  qty: number
  qty_initial: number | null
  leverage: number | null
  notional_usd: number | null
  entry_price: number
  opened_at: string | null
  planned_stop: number | null
  planned_tp1: number | null
  planned_tp2: number | null
  exit_price: number | null
  closed_at: string | null
  status: string
  phase: string | null
  sl_current_price: number | null
  realized_r: number | null
  pnl_usd: number | null
  pnl_pct: number | null
  entry_slippage_pct: number | null
  notes: string | null
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
