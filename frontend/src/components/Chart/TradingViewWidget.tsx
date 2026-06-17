import { useEffect, useRef } from 'react'

declare global {
  interface Window {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    TradingView: any
  }
}

const TF_MAP: Record<string, string> = {
  '1m': '1', '5m': '5', '15m': '15', '30m': '30',
  '1h': '60', '4h': '240', '6h': '360', '8h': '480',
  '12h': '720', '1d': 'D', '3d': '3D',
}

function toTVSymbol(symbol: string): string {
  const isPerp = symbol.includes(':')
  const base = symbol.split(':')[0].replace('/', '')
  // Perps: BINANCE perpétuo (sufixo .P) — é EXATAMENTE o mercado que o bot
  //   opera (Binance Futures USDT-M). Antes usava BYBIT, que abria o SPOT da
  //   Bybit (mercado/preço diferentes do trade). allow_symbol_change=true deixa
  //   o usuário trocar de corretora se algum símbolo de observação não existir.
  // Spots: BINANCE spot.
  return isPerp ? `BINANCE:${base}.P` : `BINANCE:${base}`
}

const DEFAULT_STUDIES = [
  { id: 'MAExp@tv-basicstudies', inputs: { length: 9 } },
  { id: 'MAExp@tv-basicstudies', inputs: { length: 21 } },
  { id: 'MAExp@tv-basicstudies', inputs: { length: 50 } },
  { id: 'MAExp@tv-basicstudies', inputs: { length: 200 } },
  { id: 'RSI@tv-basicstudies', inputs: { length: 14 } },
  { id: 'MACD@tv-basicstudies' },
  { id: 'BB@tv-basicstudies' },
  'Volume@tv-basicstudies',
]

let tvScriptLoaded = false
const tvReadyCallbacks: (() => void)[] = []

function loadTVScript(cb: () => void) {
  if (tvScriptLoaded) { cb(); return }
  tvReadyCallbacks.push(cb)
  if (tvReadyCallbacks.length > 1) return

  const script = document.createElement('script')
  script.src = 'https://s3.tradingview.com/tv.js'
  script.async = true
  script.onload = () => {
    tvScriptLoaded = true
    tvReadyCallbacks.forEach(fn => fn())
    tvReadyCallbacks.length = 0
  }
  document.head.appendChild(script)
}

interface Props {
  symbol: string
  interval: string
  onSymbolNotFound?: () => void
}

export function TradingViewWidget({ symbol, interval }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const widgetRef = useRef<any>(null)
  const idRef = useRef(`tv_${Math.random().toString(36).slice(2)}`)

  useEffect(() => {
    const el = containerRef.current
    if (!el) return

    el.innerHTML = ''
    const inner = document.createElement('div')
    inner.id = idRef.current
    inner.style.cssText = 'width:100%;height:100%'
    el.appendChild(inner)

    const createWidget = () => {
      if (!window.TradingView || !document.getElementById(idRef.current)) return
      if (widgetRef.current) {
        try { widgetRef.current.remove() } catch { /* ignore */ }
        widgetRef.current = null
      }

      widgetRef.current = new window.TradingView.widget({
        container_id: idRef.current,
        autosize: true,
        symbol: toTVSymbol(symbol),
        interval: TF_MAP[interval] ?? '60',
        timezone: 'America/Sao_Paulo',
        theme: 'dark',
        style: '1',
        locale: 'pt',
        toolbar_bg: '#131722',
        enable_publishing: false,
        allow_symbol_change: true,   // permite buscar exchange alternativa
        hide_side_toolbar: false,
        hide_top_toolbar: false,
        withdateranges: true,
        save_image: true,
        studies: DEFAULT_STUDIES,
        overrides: {
          'mainSeriesProperties.candleStyle.upColor': '#26a69a',
          'mainSeriesProperties.candleStyle.downColor': '#ef5350',
          'mainSeriesProperties.candleStyle.borderUpColor': '#26a69a',
          'mainSeriesProperties.candleStyle.borderDownColor': '#ef5350',
          'mainSeriesProperties.candleStyle.wickUpColor': '#26a69a',
          'mainSeriesProperties.candleStyle.wickDownColor': '#ef5350',
        },
      })
    }

    loadTVScript(createWidget)

    return () => {
      if (widgetRef.current) {
        try { widgetRef.current.remove() } catch { /* ignore */ }
        widgetRef.current = null
      }
      if (el) el.innerHTML = ''
    }
  }, [symbol, interval])

  return (
    <div
      ref={containerRef}
      style={{ width: '100%', height: '100%', minHeight: 400 }}
    />
  )
}
