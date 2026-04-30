import { useState, useEffect, useRef } from 'react'
import { createPriceWebSocket } from '../services/api'
import type { Ticker } from '../types'

export function useLivePrice(symbol: string | null) {
  const [ticker, setTicker] = useState<Ticker | null>(null)
  const wsRef = useRef<WebSocket | null>(null)

  useEffect(() => {
    if (!symbol) return
    if (wsRef.current) wsRef.current.close()

    const ws = createPriceWebSocket(symbol, (msg: unknown) => {
      const data = msg as { type: string; data: Ticker }
      if (data?.type === 'ticker') setTicker(data.data)
    })
    wsRef.current = ws

    return () => {
      ws.close()
    }
  }, [symbol])

  return ticker
}
