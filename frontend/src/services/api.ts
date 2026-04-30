import type { TradeSignal, OHLCVCandle, Ticker, WatchlistItem } from '../types'

const BACKEND = import.meta.env.VITE_API_URL ?? 'https://crypto-agente-production.up.railway.app'
const BASE = `${BACKEND}/api`

// Binance Futures public API — requests come from the user's browser IP, no geo-blocking
const BINANCE_FAPI = 'https://fapi.binance.com/fapi/v1'
const BINANCE_INTERVAL: Record<string, string> = {
  '1m': '1m', '5m': '5m', '15m': '15m', '1h': '1h', '4h': '4h', '1d': '1d',
}

function toBinance(symbol: string): string {
  return symbol.split(':')[0].replace('/', '')  // 'BTC/USDT:USDT' → 'BTCUSDT'
}

async function fetchBinanceOHLCV(symbol: string, timeframe: string, limit = 300): Promise<OHLCVCandle[]> {
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
