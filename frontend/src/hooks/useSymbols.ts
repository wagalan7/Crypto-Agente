import { useState, useEffect } from 'react'
import { api } from '../services/api'

const TOP_SYMBOLS = [
  'BTC/USDT:USDT', 'ETH/USDT:USDT', 'BNB/USDT:USDT', 'SOL/USDT:USDT',
  'XRP/USDT:USDT', 'DOGE/USDT:USDT', 'ADA/USDT:USDT', 'AVAX/USDT:USDT',
  'LINK/USDT:USDT', 'DOT/USDT:USDT', 'MATIC/USDT:USDT', 'LTC/USDT:USDT',
  'UNI/USDT:USDT', 'ATOM/USDT:USDT', 'OP/USDT:USDT', 'ARB/USDT:USDT',
]

export function useSymbols() {
  const [symbols, setSymbols] = useState<string[]>(TOP_SYMBOLS)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    setLoading(true)
    api.getSymbols()
      .then(res => setSymbols(res.symbols.length ? res.symbols : TOP_SYMBOLS))
      .catch(() => setSymbols(TOP_SYMBOLS))
      .finally(() => setLoading(false))
  }, [])

  return { symbols, loading }
}
