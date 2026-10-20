import { useState, useCallback, useEffect, useRef } from 'react'
import { api } from '../services/api'
import { createLatestAnalysisRunner } from '../lib/analysisLifecycle'
import type { TradeSignal, OHLCVCandle } from '../types'

export function useAnalysis() {
  const [signal, setSignal] = useState<TradeSignal | null>(null)
  const [candles, setCandles] = useState<OHLCVCandle[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const runner = useRef<ReturnType<typeof createLatestAnalysisRunner> | null>(null)
  if (!runner.current) runner.current = createLatestAnalysisRunner(api, {
    start: () => { setLoading(true); setError(null); setSignal(null); setCandles([]) },
    success: (nextCandles, nextSignal) => { setCandles(nextCandles); setSignal(nextSignal) },
    failure: setError,
    finish: () => setLoading(false),
  })
  useEffect(() => () => runner.current?.invalidate(), [])
  const analyze = useCallback((symbol: string, timeframe: string, withAi = true) =>
    runner.current!.analyze(symbol, timeframe, withAi), [])

  return { signal, candles, loading, error, analyze }
}
