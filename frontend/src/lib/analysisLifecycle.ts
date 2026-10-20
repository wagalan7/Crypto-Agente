import type { OHLCVCandle, Ticker, TradeSignal } from '../types'

// UI only: prevents an older symbol/TF response from replacing the current one.
// The API does not accept AbortSignal; do not pretend that invalidation cancels I/O.
export function createLatestAnalysisRunner(
  source: {
    getOHLCV: (symbol: string, timeframe: string) => Promise<{ data: OHLCVCandle[] }>
    analyze: (symbol: string, timeframe: string, withAi: boolean) => Promise<TradeSignal>
  },
  sink: {
    start: () => void
    success: (candles: OHLCVCandle[], signal: TradeSignal) => void
    failure: (message: string) => void
    finish: () => void
  },
) {
  let generation = 0
  return {
    invalidate() { generation++ },
    async analyze(symbol: string, timeframe: string, withAi = true) {
      const current = ++generation
      sink.start()
      try {
        const [ohlcv, signal] = await Promise.all([
          source.getOHLCV(symbol, timeframe), source.analyze(symbol, timeframe, withAi),
        ])
        if (current === generation) sink.success(ohlcv.data, signal)
      } catch (error) {
        if (current === generation) sink.failure(error instanceof Error ? error.message : 'Erro ao analisar')
      } finally {
        if (current === generation) sink.finish()
      }
    },
  }
}

function marketKey(symbol: string) {
  // CCXT settle suffix does not change the pair. Never collapse USDT and USDC.
  return symbol.split(':')[0].replace('/', '').toUpperCase()
}

export function subscribeCurrentTicker(
  symbol: string,
  onTicker: (ticker: Ticker) => void,
  connect: (symbol: string, onMessage: (message: unknown) => void) => { close: () => void },
) {
  let active = true
  const socket = connect(symbol, message => {
    if (!active || !message || typeof message !== 'object') return
    const event = message as { type?: string; data?: Ticker }
    const ticker = event.data
    if (event.type !== 'ticker' || !ticker || typeof ticker.symbol !== 'string') return
    if (marketKey(ticker.symbol) !== marketKey(symbol)) return
    if (!Number.isFinite(ticker.last) || ticker.last <= 0) return
    onTicker(ticker)
  })
  return () => {
    if (!active) return
    active = false
    socket.close()
  }
}
