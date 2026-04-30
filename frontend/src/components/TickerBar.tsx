import { useEffect, useState } from 'react'

interface TickerItem {
  symbol: string
  price: number
  change: number
}

function fmtPrice(p: number): string {
  if (p >= 10000) return p.toLocaleString('pt-BR', { maximumFractionDigits: 0 })
  if (p >= 1000) return p.toLocaleString('pt-BR', { maximumFractionDigits: 2 })
  if (p >= 1) return p.toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 4 })
  return p.toLocaleString('pt-BR', { minimumFractionDigits: 4, maximumFractionDigits: 6 })
}

export default function TickerBar() {
  const [items, setItems] = useState<TickerItem[]>([])

  useEffect(() => {
    const load = () => {
      fetch('https://fapi.binance.com/fapi/v1/ticker/24hr')
        .then(r => r.json())
        .then((data: Array<{ symbol: string; lastPrice: string; priceChangePercent: string; quoteVolume: string }>) => {
          const top = data
            .filter(t => t.symbol.endsWith('USDT'))
            .sort((a, b) => parseFloat(b.quoteVolume) - parseFloat(a.quoteVolume))
            .slice(0, 28)
            .map(t => ({
              symbol: t.symbol.replace('USDT', ''),
              price: parseFloat(t.lastPrice),
              change: parseFloat(t.priceChangePercent),
            }))
          setItems(top)
        })
        .catch(() => {})
    }
    load()
    const id = setInterval(load, 30000)
    return () => clearInterval(id)
  }, [])

  if (!items.length) return <div className="h-7 bg-black border-b border-slate-800/60" />

  return (
    <div className="h-7 bg-black border-b border-slate-800/60 overflow-hidden flex items-center select-none">
      <div className="ticker-scroll flex items-center gap-8 whitespace-nowrap px-4">
        {[...items, ...items].map((item, i) => (
          <span key={i} className="inline-flex items-center gap-1.5 text-xs">
            <span className="text-slate-400 font-medium">{item.symbol}</span>
            <span className="text-white font-mono">${fmtPrice(item.price)}</span>
            <span className={`font-semibold ${item.change >= 0 ? 'text-green-400' : 'text-red-400'}`}>
              {item.change >= 0 ? '+' : ''}{item.change.toFixed(2)}%
            </span>
          </span>
        ))}
      </div>
    </div>
  )
}
