import { useState, useEffect, useCallback } from 'react'
import { Search } from 'lucide-react'
import TickerBar from './components/TickerBar'
import ChartPanel from './components/ChartPanel'
import TradeManager from './components/TradeManager'
import { api } from './services/api'
import type { SignalDirection, TradeType } from './types'

// ─── Types ────────────────────────────────────────────────────────────────────

interface ScannerAsset {
  symbol: string       // 'BTC/USDT:USDT'
  baseAsset: string    // 'BTC'
  binanceSymbol: string// 'BTCUSDT'
  price: number
  change24h: number
  volume24h: number
  direction: SignalDirection
  confidence: number
  signal_strength: string
  trade_type: TradeType
  rsi: number | null
  patterns_count: number
}

type TradeMode = 'scalp' | 'day' | 'swing'
type Filter = 'all' | 'long' | 'short' | 'neutral' | 'forte' | 'rsi70' | 'rsi30'
type Sort = 'rr' | 'volume' | 'high' | 'low' | 'rsi_asc' | 'rsi_desc' | 'price' | 'az'

// ─── Constants ────────────────────────────────────────────────────────────────

const TRADE_MODES: Record<TradeMode, {
  label: string; icon: string; tfs: string; chartTf: string; timeframe: string
  color: string; border: string; bg: string; glow: string
}> = {
  scalp: {
    label: 'SCALP', icon: '⚡', tfs: '5m·15m·30m', chartTf: '5m/15m', timeframe: '5m',
    color: 'text-yellow-400', border: 'border-yellow-500/50', bg: 'bg-yellow-500/10', glow: 'shadow-yellow-500/10',
  },
  day: {
    label: 'DAY', icon: '📅', tfs: '15m·1h·4h', chartTf: '15m/1h', timeframe: '1h',
    color: 'text-blue-400', border: 'border-blue-500/50', bg: 'bg-blue-500/10', glow: 'shadow-blue-500/10',
  },
  swing: {
    label: 'SWING', icon: '📈', tfs: '4h·12h·1D', chartTf: '12h/1D', timeframe: '1d',
    color: 'text-purple-400', border: 'border-purple-500/50', bg: 'bg-purple-500/10', glow: 'shadow-purple-500/10',
  },
}

const ASSET_COLORS: Record<string, string> = {
  BTC: 'bg-orange-500', ETH: 'bg-indigo-500', BNB: 'bg-yellow-500',
  SOL: 'bg-purple-500', XRP: 'bg-blue-500', DOGE: 'bg-yellow-400',
  ADA: 'bg-blue-600', AVAX: 'bg-red-500', LINK: 'bg-blue-400',
  DOT: 'bg-pink-500', MATIC: 'bg-purple-600', LTC: 'bg-slate-400',
  UNI: 'bg-pink-400', ATOM: 'bg-indigo-400', OP: 'bg-red-400',
  ARB: 'bg-sky-500', NEAR: 'bg-green-500', PEPE: 'bg-green-400',
  SUI: 'bg-cyan-500', TON: 'bg-blue-500', APT: 'bg-red-400',
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function fmtVolume(usd: number): string {
  if (usd >= 1e9) return `$${(usd / 1e9).toFixed(1)}B`
  if (usd >= 1e6) return `$${(usd / 1e6).toFixed(0)}M`
  return `$${(usd / 1e3).toFixed(0)}K`
}

function fmtPrice(p: number): string {
  if (p >= 10000) return p.toLocaleString('pt-BR', { maximumFractionDigits: 0 })
  if (p >= 1000) return p.toLocaleString('pt-BR', { maximumFractionDigits: 2 })
  if (p >= 1) return p.toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 4 })
  return p.toLocaleString('pt-BR', { minimumFractionDigits: 4, maximumFractionDigits: 6 })
}

function assetColor(base: string): string {
  return ASSET_COLORS[base] ?? 'bg-slate-600'
}

function signalBadge(direction: SignalDirection, strength: string) {
  const forte = strength?.toLowerCase().includes('fort') || strength?.toLowerCase().includes('strong')
  if (direction === 'long') {
    return forte
      ? { label: 'COMPRA FORTE', cls: 'bg-green-500 text-white' }
      : { label: 'COMPRA', cls: 'bg-green-500/15 text-green-400 border border-green-500/40' }
  }
  if (direction === 'short') {
    return forte
      ? { label: 'VENDA FORTE', cls: 'bg-red-500 text-white' }
      : { label: 'VENDA', cls: 'bg-red-500/15 text-red-400 border border-red-500/40' }
  }
  return { label: 'NEUTRO', cls: 'bg-slate-700/80 text-slate-400 border border-slate-600/60' }
}

function rsiLabel(rsi: number | null) {
  if (rsi == null) return null
  if (rsi < 30) return { text: `RSI ${rsi.toFixed(0)}·Sobrevendido`, cls: 'text-green-400' }
  if (rsi > 70) return { text: `RSI ${rsi.toFixed(0)}·Sobrecomprado`, cls: 'text-red-400' }
  return { text: `RSI ${rsi.toFixed(0)}·Neutro`, cls: 'text-slate-500' }
}

// ─── Asset Row ────────────────────────────────────────────────────────────────

function AssetRow({ asset, rank, tradeMode, onClick }: {
  asset: ScannerAsset; rank: number; tradeMode: TradeMode; onClick: () => void
}) {
  const badge = signalBadge(asset.direction, asset.signal_strength)
  const rsi = rsiLabel(asset.rsi)
  const cfg = TRADE_MODES[tradeMode]
  const tfs = cfg.tfs.split('·')

  return (
    <div
      className="flex items-center px-4 py-3 border-b border-slate-800/50 hover:bg-slate-800/25 cursor-pointer transition-colors active:bg-slate-800/50"
      onClick={onClick}
    >
      {/* Rank */}
      <span className="w-6 text-xs text-slate-700 flex-shrink-0 text-right mr-3">{rank}</span>

      {/* Icon */}
      <div className={`w-9 h-9 rounded-full ${assetColor(asset.baseAsset)} flex items-center justify-center flex-shrink-0 mr-3 shadow-lg`}>
        <span className="text-white font-bold text-[10px] leading-none text-center">
          {asset.baseAsset.length <= 4 ? asset.baseAsset : asset.baseAsset.slice(0, 4)}
        </span>
      </div>

      {/* Left info */}
      <div className="flex-1 min-w-0">
        <div className="font-bold text-white text-sm leading-tight">{asset.baseAsset}</div>
        <div className="text-xs text-slate-500 mt-0.5">
          {asset.binanceSymbol}·{fmtVolume(asset.volume24h)}
        </div>
        <div className="flex items-center gap-1.5 mt-1 flex-wrap">
          <span className="text-xs text-slate-600">
            TF:{tfs[0]}/{tfs[1]}·Padrão:{asset.patterns_count > 0 ? tfs[0] : '-'}
          </span>
          <span className={`text-xs px-1.5 py-0.5 rounded font-semibold ${badge.cls}`}>
            {badge.label}
          </span>
        </div>
      </div>

      {/* Right: price + change + rsi */}
      <div className="text-right flex-shrink-0 ml-2">
        <div className={`text-sm font-mono font-bold ${asset.change24h >= 0 ? 'text-green-400' : 'text-red-400'}`}>
          ${fmtPrice(asset.price)}
        </div>
        <div className={`text-xs font-semibold mt-0.5 ${asset.change24h >= 0 ? 'text-green-400' : 'text-red-400'}`}>
          {asset.change24h >= 0 ? '+' : ''}{asset.change24h.toFixed(2)}%
        </div>
        {rsi && (
          <div className={`text-xs mt-0.5 ${rsi.cls}`}>{rsi.text}</div>
        )}
      </div>
    </div>
  )
}

// ─── Main App ─────────────────────────────────────────────────────────────────

export default function App() {
  const [showTradeManager, setShowTradeManager] = useState(false)
  const [tradeMode, setTradeMode] = useState<TradeMode>('swing')
  const [filter, setFilter] = useState<Filter>('all')
  const [sort, setSort] = useState<Sort>('volume')
  const [search, setSearch] = useState('')
  const [assets, setAssets] = useState<ScannerAsset[]>([])
  const [loadingSignals, setLoadingSignals] = useState(false)
  const [loadingProgress, setLoadingProgress] = useState(0)
  const [selectedSymbol, setSelectedSymbol] = useState<string | null>(null)
  const [clock, setClock] = useState(new Date())
  const [isMobile, setIsMobile] = useState(window.innerWidth < 768)

  useEffect(() => {
    const id = setInterval(() => setClock(new Date()), 1000)
    return () => clearInterval(id)
  }, [])

  useEffect(() => {
    const handler = () => setIsMobile(window.innerWidth < 768)
    window.addEventListener('resize', handler)
    return () => window.removeEventListener('resize', handler)
  }, [])

  const loadData = useCallback(async (mode: TradeMode) => {
    setLoadingSignals(true)
    setLoadingProgress(5)

    try {
      // 1. Load Binance 24hr tickers directly from browser
      const tickerRes = await fetch('https://fapi.binance.com/fapi/v1/ticker/24hr')
      const tickers: Array<{ symbol: string; lastPrice: string; priceChangePercent: string; quoteVolume: string }> =
        await tickerRes.json()

      const top = tickers
        .filter(t => t.symbol.endsWith('USDT'))
        .sort((a, b) => parseFloat(b.quoteVolume) - parseFloat(a.quoteVolume))

      const initial: ScannerAsset[] = top.map(t => {
        const base = t.symbol.replace('USDT', '')
        return {
          symbol: `${base}/USDT:USDT`,
          baseAsset: base,
          binanceSymbol: t.symbol,
          price: parseFloat(t.lastPrice),
          change24h: parseFloat(t.priceChangePercent),
          volume24h: parseFloat(t.quoteVolume),
          direction: 'neutral',
          confidence: 0,
          signal_strength: '',
          trade_type: 'day_trade',
          rsi: null,
          patterns_count: 0,
        }
      })

      setAssets(initial)
      setLoadingProgress(25)

      // 2. Load signals from backend in batches
      const symbols = initial.map(a => a.symbol)
      const tf = TRADE_MODES[mode].timeframe
      const batchSize = 20
      const signalMap = new Map<string, Partial<ScannerAsset>>()

      for (let i = 0; i < symbols.length; i += batchSize) {
        const batch = symbols.slice(i, i + batchSize)
        try {
          const result = await api.watchlistAnalyze(batch, tf)
          result.results.forEach(r => {
            signalMap.set(r.symbol, {
              direction: r.direction ?? 'neutral',
              confidence: r.confidence ?? 0,
              signal_strength: r.signal_strength ?? '',
              trade_type: r.trade_type ?? 'day_trade',
              rsi: r.rsi ?? null,
              patterns_count: r.patterns_count ?? 0,
            })
          })
        } catch { /* batch failed, skip */ }
        const progress = 25 + Math.round(((i + batchSize) / symbols.length) * 75)
        setLoadingProgress(Math.min(progress, 99))
        setAssets(prev => prev.map(a => ({ ...a, ...(signalMap.get(a.symbol) ?? {}) })))
      }
    } catch {
      // Binance unavailable: fall back to backend symbols + signals
      try {
        const { symbols: backendSyms } = await api.getSymbols()
        const initial: ScannerAsset[] = backendSyms.map(sym => {
          const base = sym.split('/')[0]
          return {
            symbol: sym, baseAsset: base, binanceSymbol: `${base}USDT`,
            price: 0, change24h: 0, volume24h: 0,
            direction: 'neutral' as SignalDirection, confidence: 0, signal_strength: '',
            trade_type: 'day_trade' as TradeType, rsi: null, patterns_count: 0,
          }
        })
        setAssets(initial)
      } catch { /* nothing */ }
    } finally {
      setLoadingSignals(false)
      setLoadingProgress(100)
    }
  }, [])

  useEffect(() => {
    loadData(tradeMode)
  }, [tradeMode, loadData])

  // Filters
  const filtered = assets.filter(a => {
    if (search) {
      const q = search.toLowerCase()
      if (!a.baseAsset.toLowerCase().includes(q) && !a.binanceSymbol.toLowerCase().includes(q)) return false
    }
    if (filter === 'long') return a.direction === 'long'
    if (filter === 'short') return a.direction === 'short'
    if (filter === 'neutral') return a.direction === 'neutral'
    if (filter === 'forte') return a.signal_strength?.toLowerCase().includes('fort') || a.signal_strength?.toLowerCase().includes('strong') || a.confidence >= 0.70
    if (filter === 'rsi70') return (a.rsi ?? 0) > 70
    if (filter === 'rsi30') return (a.rsi ?? 100) < 30
    return true
  })

  const sorted = [...filtered].sort((a, b) => {
    switch (sort) {
      case 'volume':   return b.volume24h - a.volume24h
      case 'high':     return b.change24h - a.change24h
      case 'low':      return a.change24h - b.change24h
      case 'rsi_asc':  return (a.rsi ?? 50) - (b.rsi ?? 50)
      case 'rsi_desc': return (b.rsi ?? 50) - (a.rsi ?? 50)
      case 'price':    return b.price - a.price
      case 'az':       return a.baseAsset.localeCompare(b.baseAsset)
      case 'rr':       return b.confidence - a.confidence
      default:         return 0
    }
  })

  const statsLong    = assets.filter(a => a.direction === 'long').length
  const statsShort   = assets.filter(a => a.direction === 'short').length
  const statsNeutral = assets.filter(a => a.direction === 'neutral').length

  const FILTERS: { key: Filter; label: string }[] = [
    { key: 'all',     label: 'TODOS'    },
    { key: 'long',    label: '🟢 COMPRA'  },
    { key: 'short',   label: '🔴 VENDA'   },
    { key: 'neutral', label: '🟡 NEUTRO'  },
    { key: 'forte',   label: '🔥 FORTE'   },
    { key: 'rsi70',   label: 'RSI>70'   },
    { key: 'rsi30',   label: 'RSI<30'   },
  ]

  const SORTS: { key: Sort; label: string }[] = [
    { key: 'rr',       label: 'R/R↓'     },
    { key: 'volume',   label: 'Volume↓'  },
    { key: 'high',     label: 'Alta%↓'   },
    { key: 'low',      label: 'Baixa%↓'  },
    { key: 'rsi_asc',  label: 'RSI↓'     },
    { key: 'rsi_desc', label: 'RSI↑'     },
    { key: 'price',    label: 'Preço↓'   },
    { key: 'az',       label: 'A-Z'      },
  ]

  // ─── Scanner panel (left column) ──────────────────────────────────────────
  const ScannerPanel = (
    <div className="flex flex-col h-full overflow-hidden bg-[#0a0e1a]">
      {/* Sub-header */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-slate-800/50 flex-shrink-0">
        <div className="flex items-center gap-1.5">
          <span className="w-2 h-2 rounded-full bg-green-500 animate-pulse" />
          <span className="text-xs text-slate-300 font-medium">Binance Futures – Tempo Real</span>
        </div>
        <span className="text-slate-500 text-xs">{assets.length} pares</span>
        <div className="flex items-center gap-1.5">
          <button
            onClick={() => setShowTradeManager(v => !v)}
            className="flex items-center gap-1 px-2 py-1 bg-slate-800 hover:bg-slate-700 border border-slate-700 rounded text-xs font-semibold text-slate-300"
          >
            <span>📋</span>
            <span className="hidden sm:block">Trades</span>
          </button>
          <span className="text-slate-600 text-xs font-mono tabular-nums">
            {clock.toLocaleTimeString('pt-BR')}
          </span>
        </div>
      </div>

      {/* Trade type cards */}
      <div className="grid grid-cols-3 gap-1.5 px-2 py-2 flex-shrink-0">
        {(Object.entries(TRADE_MODES) as [TradeMode, typeof TRADE_MODES[TradeMode]][]).map(([key, cfg]) => (
          <button
            key={key}
            onClick={() => setTradeMode(key)}
            className={`relative rounded-lg p-2 border transition-all text-left ${
              tradeMode === key
                ? `${cfg.bg} ${cfg.border}`
                : 'bg-slate-800/30 border-slate-700/30 hover:bg-slate-800/50'
            }`}
          >
            <div className="text-base mb-0.5 leading-none">{cfg.icon}</div>
            <div className={`text-xs font-bold tracking-wider ${tradeMode === key ? cfg.color : 'text-slate-400'}`}>
              {cfg.label}
            </div>
            <div className="text-[10px] text-slate-500 leading-tight">{cfg.tfs}</div>
            {tradeMode === key && (
              <div className="absolute bottom-0 left-0 right-0 h-0.5 rounded-b-lg overflow-hidden bg-slate-800">
                <div
                  className="h-full bg-green-500 transition-all duration-500"
                  style={{ width: loadingSignals ? `${loadingProgress}%` : '100%' }}
                />
              </div>
            )}
          </button>
        ))}
      </div>

      {/* Search */}
      <div className="px-2 pb-1.5 flex-shrink-0">
        <div className="relative">
          <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-slate-500" />
          <input
            type="text"
            placeholder="Buscar par... BTC, SOL, PEPE"
            value={search}
            onChange={e => setSearch(e.target.value)}
            className="w-full bg-slate-800/50 border border-slate-700/50 rounded-lg pl-8 pr-3 py-2 text-xs text-slate-200 placeholder-slate-500 focus:outline-none focus:border-slate-500 transition-colors"
          />
        </div>
      </div>

      {/* Filters */}
      <div className="flex items-center gap-1 px-2 pb-1.5 overflow-x-auto scrollbar-hide flex-shrink-0">
        {FILTERS.map(f => (
          <button
            key={f.key}
            onClick={() => setFilter(f.key)}
            className={`whitespace-nowrap px-2 py-0.5 rounded-full text-xs font-semibold transition-colors border flex-shrink-0 ${
              filter === f.key
                ? 'bg-white/10 border-slate-500 text-white'
                : 'border-slate-700/50 text-slate-500 hover:text-slate-300 hover:border-slate-600'
            }`}
          >
            {f.label}
          </button>
        ))}
      </div>

      {/* Sort */}
      <div className="flex items-center gap-1 px-2 pb-1.5 overflow-x-auto scrollbar-hide flex-shrink-0">
        {SORTS.map(s => (
          <button
            key={s.key}
            onClick={() => setSort(s.key)}
            className={`whitespace-nowrap px-2 py-0.5 rounded text-xs font-medium transition-colors border flex-shrink-0 ${
              sort === s.key
                ? 'bg-white/10 border-slate-500 text-white'
                : 'border-slate-700/40 text-slate-600 hover:text-slate-300 hover:border-slate-600'
            }`}
          >
            {s.label}
          </button>
        ))}
      </div>

      {/* Stats bar */}
      <div className="grid grid-cols-4 divide-x divide-slate-800/80 border-y border-slate-800/80 flex-shrink-0">
        {[
          { val: statsLong,    label: 'COMPRA', cls: 'text-green-400'  },
          { val: statsShort,   label: 'VENDA',  cls: 'text-red-400'    },
          { val: statsNeutral, label: 'NEUTRO', cls: 'text-yellow-400' },
          { val: assets.length,label: 'TOTAL',  cls: 'text-white'      },
        ].map(({ val, label, cls }) => (
          <div key={label} className="py-1.5 text-center">
            <div className={`text-base font-bold ${cls}`}>{val}</div>
            <div className="text-[10px] text-slate-600 uppercase tracking-wider">{label}</div>
          </div>
        ))}
      </div>

      {/* Asset list */}
      <div className="flex-1 overflow-y-auto">
        {sorted.map((asset, i) => (
          <AssetRow
            key={asset.symbol}
            asset={asset}
            rank={i + 1}
            tradeMode={tradeMode}
            onClick={() => setSelectedSymbol(asset.symbol)}
          />
        ))}
        {sorted.length === 0 && !loadingSignals && assets.length > 0 && (
          <div className="flex items-center justify-center py-16 text-slate-600 text-sm">
            Nenhum ativo encontrado
          </div>
        )}
        {loadingSignals && assets.length === 0 && (
          <div className="flex flex-col items-center justify-center py-16 gap-3">
            <div className="w-8 h-8 border-2 border-blue-500 border-t-transparent rounded-full animate-spin" />
            <span className="text-sm text-slate-500">Carregando mercado...</span>
          </div>
        )}
      </div>
    </div>
  )

  return (
    <div className="h-screen bg-[#0a0e1a] text-white flex flex-col overflow-hidden">
      {/* Scrolling ticker — full width at top */}
      <TickerBar />

      {/* Body */}
      <div className="flex flex-1 overflow-hidden">
        {/* Left: scanner list — hidden on mobile when chart is open */}
        <div className={`flex-shrink-0 overflow-hidden transition-all duration-300 ${
          selectedSymbol
            ? isMobile ? 'w-0' : 'w-[340px] xl:w-[380px]'
            : 'w-full'
        }`}>
          {ScannerPanel}
        </div>

        {/* Right: chart panel (only when symbol selected) */}
        {selectedSymbol && (
          <div className="flex-1 overflow-hidden">
            <ChartPanel
              symbol={selectedSymbol}
              timeframe={TRADE_MODES[tradeMode].timeframe}
              onClose={() => setSelectedSymbol(null)}
              isMobile={isMobile}
            />
          </div>
        )}
      </div>

      {showTradeManager && (
        <TradeManager
          onClose={() => setShowTradeManager(false)}
          onSelectSymbol={(sym) => {
            setSelectedSymbol(sym)
            setShowTradeManager(false)
          }}
        />
      )}
    </div>
  )
}
