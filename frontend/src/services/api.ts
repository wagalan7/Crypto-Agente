import type { TradeSignal, OHLCVCandle, Ticker, WatchlistItem } from '../types'

// Em produção (Vercel), VITE_API_URL aponta para o backend no Railway.
// Em desenvolvimento local, usa o proxy do Vite (/api → localhost:8000).
const BACKEND = import.meta.env.VITE_API_URL ?? ''
const BASE = `${BACKEND}/api`

async function get<T>(path: string, params?: Record<string, string | number | boolean>): Promise<T> {
  const url = new URL(BASE + path, BACKEND ? BACKEND : window.location.origin)
  if (params) {
    Object.entries(params).forEach(([k, v]) => url.searchParams.set(k, String(v)))
  }
  const res = await fetch(url.toString())
  if (!res.ok) throw new Error(`API error ${res.status}: ${await res.text()}`)
  return res.json()
}

export const api = {
  getSymbols: () => get<{ symbols: string[]; count: number }>('/symbols'),

  getOHLCV: (symbol: string, timeframe: string, limit = 300) =>
    get<{ symbol: string; timeframe: string; data: OHLCVCandle[] }>('/ohlcv', {
      symbol,
      timeframe,
      limit,
    }),

  analyze: (symbol: string, timeframe: string, withAi = true) =>
    get<TradeSignal>('/analyze', { symbol, timeframe, with_ai: withAi }),

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
