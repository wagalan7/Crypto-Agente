import { useState, useCallback, useRef } from 'react'
import { api } from '../services/api'
import type { TradeSignal, OHLCVCandle } from '../types'

export function useAnalysis() {
  const [signal, setSignal] = useState<TradeSignal | null>(null)
  const [candles, setCandles] = useState<OHLCVCandle[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)

  const analyze = useCallback(async (symbol: string, timeframe: string, withAi = true) => {
    if (abortRef.current) abortRef.current.abort()
    abortRef.current = new AbortController()

    setLoading(true)
    setError(null)
    setSignal(null)

    try {
      const [ohlcvRes, signalRes] = await Promise.all([
        api.getOHLCV(symbol, timeframe),
        api.analyze(symbol, timeframe, withAi),
      ])
      setCandles(ohlcvRes.data)
      setSignal(signalRes)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Erro ao analisar')
    } finally {
      setLoading(false)
    }
  }, [])

  return { signal, candles, loading, error, analyze }
}
