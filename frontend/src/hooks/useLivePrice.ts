import { useState, useEffect } from 'react'
import { createPriceWebSocket } from '../services/api'
import { subscribeCurrentTicker } from '../lib/analysisLifecycle'
import type { Ticker } from '../types'

export function useLivePrice(symbol: string | null) {
  const [reading, setReading] = useState<{ symbol: string; ticker: Ticker } | null>(null)

  useEffect(() => {
    setReading(null)
    if (!symbol) return
    return subscribeCurrentTicker(symbol, ticker => setReading({ symbol, ticker }), createPriceWebSocket)
  }, [symbol])

  // Hide the previous symbol even on the render before the effect cleanup.
  return symbol && reading?.symbol === symbol ? reading.ticker : null
}
