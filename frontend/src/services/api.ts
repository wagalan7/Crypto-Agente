import type { TradeSignal, OHLCVCandle, Ticker, WatchlistItem, Recommendation, RealTradeRow } from '../types'

const BACKEND = import.meta.env.VITE_API_URL ?? 'https://crypto-agente-production.up.railway.app'
const BASE = `${BACKEND}/api`

// Ambiente de TESTES (champion/challenger) — fonte das recs de OBSERVAÇÃO.
// Universo amplo (perpétuos), shadow, DB próprio. Mantido SEPARADO do PRD de
// propósito: o bot real (sexta) opera só as 60 do PRD; aqui é só pra analisar.
// Override via VITE_OBSERVATION_API_URL. '' desliga (painel mostra só o bot).
const OBSERVATION_BACKEND =
  import.meta.env.VITE_OBSERVATION_API_URL ??
  'https://crypto-agente-production-c6c4.up.railway.app'

// Binance Futures public API — requests come from the user's browser IP, no geo-blocking
const BINANCE_FAPI = 'https://fapi.binance.com/fapi/v1'
const BINANCE_INTERVAL: Record<string, string> = {
  '1m': '1m', '5m': '5m', '15m': '15m', '30m': '30m',
  '1h': '1h', '4h': '4h', '6h': '6h', '8h': '8h', '12h': '12h',
  '1d': '1d', '3d': '3d',
}

function toBinance(symbol: string): string {
  return symbol.split(':')[0].replace('/', '')  // 'BTC/USDT:USDT' → 'BTCUSDT'
}

// ─── Bybit V5 public API (perp linear USDT) ────────────────────────────────────
// Universo de pares ~2x maior que OKX/Binance Futures. Requests vão do IP
// do browser (residencial), sem geo-block. Mesmo formato de retorno do
// fetchBinanceOHLCV pra ser drop-in.
const BYBIT_BASE = 'https://api.bybit.com'
const BYBIT_INTERVAL: Record<string, string> = {
  '1m': '1', '3m': '3', '5m': '5', '15m': '15', '30m': '30',
  '1h': '60', '2h': '120', '4h': '240', '6h': '360', '12h': '720',
  '1d': 'D', '1w': 'W', '1M': 'M',
}

function toBybit(symbol: string): string {
  return symbol.split(':')[0].replace('/', '')  // 'BTC/USDT:USDT' → 'BTCUSDT'
}

export async function fetchBybitOHLCV(symbol: string, timeframe: string, limit = 300): Promise<OHLCVCandle[]> {
  const interval = BYBIT_INTERVAL[timeframe] ?? '60'
  const url = `${BYBIT_BASE}/v5/market/kline?category=linear&symbol=${toBybit(symbol)}&interval=${interval}&limit=${Math.min(limit, 1000)}`
  const res = await fetch(url, { signal: AbortSignal.timeout(10000) })
  if (!res.ok) throw new Error(`Bybit ${res.status}`)
  const json = await res.json()
  const raw: string[][] = json?.result?.list ?? []
  // Bybit: newest first. Inverte e parsa.
  return raw.slice().reverse().map(c => ({
    timestamp: parseInt(c[0], 10),
    open: parseFloat(c[1]),
    high: parseFloat(c[2]),
    low: parseFloat(c[3]),
    close: parseFloat(c[4]),
    volume: parseFloat(c[5]),
  }))
}

export async function fetchTopBybitSymbols(limit = 50): Promise<string[]> {
  const res = await fetch(
    `${BYBIT_BASE}/v5/market/tickers?category=linear`,
    { signal: AbortSignal.timeout(10000) },
  )
  if (!res.ok) throw new Error(`Bybit tickers ${res.status}`)
  const json = await res.json()
  const rows: { symbol: string; turnover24h: string }[] = json?.result?.list ?? []
  return rows
    .filter(r => r.symbol.endsWith('USDT'))
    .map(r => ({ s: r.symbol, t: parseFloat(r.turnover24h || '0') }))
    .sort((a, b) => b.t - a.t)
    .slice(0, limit)
    .map(r => `${r.s.replace(/USDT$/, '')}/USDT:USDT`)
}

export async function fetchBinanceOHLCV(symbol: string, timeframe: string, limit = 300): Promise<OHLCVCandle[]> {
  const interval = BINANCE_INTERVAL[timeframe] ?? '1h'
  const res = await fetch(
    `${BINANCE_FAPI}/klines?symbol=${toBinance(symbol)}&interval=${interval}&limit=${limit}`,
    { signal: AbortSignal.timeout(10000) },
  )
  if (!res.ok) throw new Error(`Binance ${res.status}`)
  const raw: unknown[][] = await res.json()
  return raw.map(c => ({
    timestamp: c[0] as number,
    open: parseFloat(c[1] as string),
    high: parseFloat(c[2] as string),
    low: parseFloat(c[3] as string),
    close: parseFloat(c[4] as string),
    volume: parseFloat(c[5] as string),
  }))
}

async function fetchBinanceSymbols(): Promise<{ symbols: string[]; count: number }> {
  const res = await fetch(`${BINANCE_FAPI}/exchangeInfo`, { signal: AbortSignal.timeout(10000) })
  if (!res.ok) throw new Error(`Binance exchangeInfo ${res.status}`)
  const data = await res.json()
  const symbols: string[] = data.symbols
    .filter((s: { contractType: string; quoteAsset: string; status: string }) =>
      s.contractType === 'PERPETUAL' && s.quoteAsset === 'USDT' && s.status === 'TRADING',
    )
    .map((s: { baseAsset: string }) => `${s.baseAsset}/USDT:USDT`)
    .sort()
  return { symbols, count: symbols.length }
}

// ─── Backend REST helper ───────────────────────────────────────────────────────

async function get<T>(path: string, params?: Record<string, string | number | boolean>): Promise<T> {
  const url = new URL(BASE + path, BACKEND ? BACKEND : window.location.origin)
  if (params) {
    Object.entries(params).forEach(([k, v]) => url.searchParams.set(k, String(v)))
  }
  const res = await fetch(url.toString())
  if (!res.ok) throw new Error(`API error ${res.status}: ${await res.text()}`)
  return res.json()
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(BASE + path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(`API error ${res.status}: ${await res.text()}`)
  return res.json()
}

async function patch<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(BASE + path, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(`API error ${res.status}: ${await res.text()}`)
  return res.json()
}

// Payload pra confirmar entrada manual a partir de uma recomendação.
export interface ConfirmEntryPayload {
  symbol: string
  side: 'long' | 'short' | string
  entry_price: number
  qty?: number | null
  timeframe?: string
  leverage?: number | null
  planned_stop?: number | null
  planned_tp1?: number | null
  planned_tp2?: number | null
  recommendation_id?: number | null
  notes?: string | null
}

// ─── Public API ────────────────────────────────────────────────────────────────

export const api = {
  getSymbols: async () => {
    try {
      return await fetchBinanceSymbols()
    } catch {
      return get<{ symbols: string[]; count: number }>('/symbols')
    }
  },

  getOHLCV: async (symbol: string, timeframe: string, limit = 300) => {
    try {
      const data = await fetchBinanceOHLCV(symbol, timeframe, limit)
      return { symbol, timeframe, data }
    } catch {
      return get<{ symbol: string; timeframe: string; data: OHLCVCandle[] }>('/ohlcv', {
        symbol, timeframe, limit,
      })
    }
  },

  analyze: async (symbol: string, timeframe: string, withAi = true) => {
    try {
      // Fetch candles from Binance in browser, send to backend for analysis
      const candles = await fetchBinanceOHLCV(symbol, timeframe, 300)
      return await post<TradeSignal>('/analyze-data', {
        symbol,
        timeframe,
        candles,
        with_ai: withAi,
      })
    } catch {
      // Fallback: let backend fetch from OKX and analyze
      return get<TradeSignal>('/analyze', { symbol, timeframe, with_ai: withAi })
    }
  },

  multiTimeframe: (symbol: string) =>
    get<Record<string, TradeSignal>>('/multi-timeframe', { symbol, with_ai: false }),

  marketData: (symbol: string) =>
    get<{ ticker: Ticker; funding_rate: number | null; open_interest: number | null }>('/market-data', {
      symbol,
    }),

  watchlistAnalyze: (symbols: string[], timeframe: string) =>
    get<{ results: WatchlistItem[] }>('/watchlist/analyze', {
      symbols: symbols.join(','),
      timeframe,
    }),

  getTickers: (symbols: string[]) =>
    get<{ tickers: Ticker[] }>('/tickers', { symbols: symbols.join(',') }),

  macro: (symbol: string) =>
    get<{
      btc_direction: string
      btc_rsi: number | null
      btc_adx: number | null
      btc_dominance: number | null
      market_data: { dxy?: { price: number; change: number }; sp500?: { price: number; change: number }; nasdaq?: { price: number; change: number } }
      context_text: string
    }>('/macro', { symbol }),

  recommendations: (topN = 30) =>
    get<{ count: number; recommendations: Recommendation[] }>('/recommendations', { top_n: topN }),

  // Recomendações de OBSERVAÇÃO — vêm do ambiente de TESTES (universo amplo,
  // shadow, DB separado). O bot NÃO opera essas; são só pro usuário analisar/
  // aprender (TradingView). Fonte desacoplada do PRD de propósito: o caminho de
  // execução real (sexta) fica congelado em 60. Falha graciosamente (retorna []).
  recommendationsObservation: async (topN = 300): Promise<Recommendation[]> => {
    if (!OBSERVATION_BACKEND) return []
    try {
      const url = `${OBSERVATION_BACKEND}/api/recommendations?top_n=${topN}`
      const res = await fetch(url, { signal: AbortSignal.timeout(12000) })
      if (!res.ok) return []
      const json = (await res.json()) as { recommendations?: Recommendation[] }
      return json.recommendations ?? []
    } catch {
      return []  // ambiente de testes fora do ar → painel mostra só as do bot
    }
  },

  recommendationsBatch: (items: { symbol: string; timeframe: string; candles: OHLCVCandle[] }[]) =>
    post<{ count: number; recommendations: Recommendation[] }>('/recommendations-batch', { items }),

  // Top símbolos por volume (Binance Futures, direto do browser — IP residencial)
  fetchTopBinanceSymbols: async (limit = 30): Promise<string[]> => {
    const res = await fetch(`${BINANCE_FAPI}/ticker/24hr`, { signal: AbortSignal.timeout(10000) })
    if (!res.ok) throw new Error(`Binance 24hr ${res.status}`)
    const rows: { symbol: string; quoteVolume: string }[] = await res.json()
    return rows
      .filter(r => r.symbol.endsWith('USDT'))
      .sort((a, b) => parseFloat(b.quoteVolume) - parseFloat(a.quoteVolume))
      .slice(0, limit)
      .map(r => `${r.symbol.replace(/USDT$/, '')}/USDT:USDT`)
  },

  bestTimeframe: (symbol: string) =>
    get<{ best_timeframe: string; score: number; signal: TradeSignal; all_scores: Record<string, number> }>(
      '/best-timeframe', { symbol }
    ),

  // ── Operações reais/manuais (backend RealTrade) ──────────────────────────
  // Menu "Operações Ativas": lista o que está vivo (status=open), unificando
  // trades manuais (você) e automáticos (bot).
  listRealTrades: (params?: { status?: string; days?: number; limit?: number }) =>
    get<{ trades: RealTradeRow[]; count: number; days: number }>('/real-trades', {
      ...(params?.status ? { status: params.status } : {}),
      days: params?.days ?? 30,
      limit: params?.limit ?? 200,
    }),

  // Confirma que você entrou numa recomendação (modo híbrido: o bot coloca o
  // bracket SL+TP1+TP2 e gerencia o break-even pós-TP1).
  confirmEntry: (body: ConfirmEntryPayload) =>
    post<RealTradeRow & {
      qty_source: string
      linked_recommendation_id: number | null
      protection?: {
        placed: boolean
        error?: string
        sl_ok?: boolean; sl_msg?: string | null
        tp1_ok?: boolean; tp1_msg?: string | null; tp1_skipped?: boolean
        tp2_ok?: boolean; tp2_msg?: string | null
      }
    }>(
      '/real-trades/from-recommendation', body,
    ),

  // Fecha manualmente uma operação real (remove do menu de ativos).
  closeRealTrade: (id: number, body: { exit_price: number; status?: string; notes?: string }) =>
    patch<RealTradeRow>(`/real-trades/${id}/close`, body),

  loadTrades: (userId: string) =>
    get<{ trades: unknown[] }>(`/trades/${userId}`),

  syncTrades: async (userId: string, trades: unknown[]) => {
    const res = await fetch(BASE + `/trades/${userId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ trades }),
    })
    if (!res.ok) throw new Error(`API error ${res.status}`)
    return res.json()
  },

  validateDrawing: async (symbol: string, timeframe: string, drawings: unknown[]) => {
    const res = await fetch(BASE + '/validate-drawing', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol, timeframe, drawings }),
    })
    if (!res.ok) throw new Error(`API error ${res.status}`)
    return res.json() as Promise<{ analysis: string }>
  },
}

export function createPriceWebSocket(symbol: string, onMessage: (data: unknown) => void): WebSocket {
  const wsBase = BACKEND
    ? BACKEND.replace(/^https/, 'wss').replace(/^http/, 'ws')
    : `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}`
  const ws = new WebSocket(`${wsBase}/ws/price/${encodeURIComponent(symbol)}`)
  ws.onmessage = (e) => {
    try { onMessage(JSON.parse(e.data)) } catch {}
  }
  return ws
}
