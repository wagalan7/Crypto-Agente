import { useState, useEffect, useCallback } from 'react'
import { RefreshCw, Settings, ChevronDown, BarChart2, Layers, Wifi, WifiOff } from 'lucide-react'
import { CandleChart } from './components/Chart/CandleChart'
import { SignalPanel } from './components/SignalPanel/SignalPanel'
import { SymbolList } from './components/SymbolList/SymbolList'
import { useAnalysis } from './hooks/useAnalysis'
import { useSymbols } from './hooks/useSymbols'
import { useLivePrice } from './hooks/useLivePrice'

const TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h', '1d']
const DEFAULT_SYMBOL = 'BTC/USDT:USDT'

function cleanName(s: string) {
  return s.replace('/USDT:USDT', '/USDT').replace(':USDT', '')
}

function LoadingOverlay() {
  return (
    <div className="absolute inset-0 flex items-center justify-center bg-slate-900/70 z-10 rounded-lg">
      <div className="flex flex-col items-center gap-3">
        <div className="w-8 h-8 border-2 border-blue-500 border-t-transparent rounded-full animate-spin" />
        <span className="text-sm text-slate-400">Analisando...</span>
      </div>
    </div>
  )
}

function TimeframeSelector({ selected, onChange }: { selected: string; onChange: (tf: string) => void }) {
  return (
    <div className="flex gap-1">
      {TIMEFRAMES.map(tf => (
        <button
          key={tf}
          onClick={() => onChange(tf)}
          className={`px-2.5 py-1 text-xs font-semibold rounded transition-colors ${
            selected === tf
              ? 'bg-blue-600 text-white'
              : 'bg-slate-800 text-slate-400 hover:bg-slate-700 hover:text-slate-200'
          }`}
        >
          {tf}
        </button>
      ))}
    </div>
  )
}

export default function App() {
  const [symbol, setSymbol] = useState(DEFAULT_SYMBOL)
  const [timeframe, setTimeframe] = useState('1h')
  const [withAi, setWithAi] = useState(true)
  const [sidebarOpen, setSidebarOpen] = useState(true)
  const [activeTab, setActiveTab] = useState<'signal' | 'mtf'>('signal')
  const [isMobile, setIsMobile] = useState(window.innerWidth < 768)

  const { signal, candles, loading, error, analyze } = useAnalysis()
  const { symbols } = useSymbols()
  const ticker = useLivePrice(symbol)

  useEffect(() => {
    const handler = () => setIsMobile(window.innerWidth < 768)
    window.addEventListener('resize', handler)
    return () => window.removeEventListener('resize', handler)
  }, [])

  const runAnalysis = useCallback(() => {
    analyze(symbol, timeframe, withAi)
  }, [analyze, symbol, timeframe, withAi])

  useEffect(() => {
    runAnalysis()
  }, [symbol, timeframe])

  const handleSymbolSelect = (sym: string) => {
    setSymbol(sym)
    if (isMobile) setSidebarOpen(false)
  }

  return (
    <div className="flex flex-col h-screen bg-slate-900 text-slate-100 overflow-hidden">
      {/* Top Bar */}
      <header className="flex items-center justify-between px-3 py-2 bg-slate-950 border-b border-slate-800 flex-shrink-0">
        <div className="flex items-center gap-2">
          <BarChart2 className="w-5 h-5 text-blue-400" />
          <span className="font-bold text-sm text-white hidden sm:block">Crypto AI Agent</span>
          <span className="font-bold text-sm text-white sm:hidden">CAI</span>
        </div>

        <div className="flex items-center gap-1 sm:gap-2">
          {/* Symbol display */}
          <button
            onClick={() => setSidebarOpen(v => !v)}
            className="flex items-center gap-1 px-2 py-1 bg-slate-800 rounded text-sm font-mono font-semibold text-slate-200 hover:bg-slate-700"
          >
            {cleanName(symbol)}
            <ChevronDown className="w-3 h-3" />
          </button>

          {/* Live price */}
          {ticker && (
            <div className="hidden sm:flex items-center gap-1 px-2 py-1 bg-slate-800 rounded">
              <span className="text-xs font-mono text-white">{ticker.last.toFixed(4)}</span>
              <span className={`text-xs font-semibold ${ticker.change >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                {ticker.change >= 0 ? '+' : ''}{ticker.change?.toFixed(2)}%
              </span>
            </div>
          )}

          {/* Timeframes */}
          <div className="hidden sm:block">
            <TimeframeSelector selected={timeframe} onChange={setTimeframe} />
          </div>

          {/* AI toggle */}
          <button
            onClick={() => setWithAi(v => !v)}
            className={`hidden sm:flex items-center gap-1 px-2 py-1 rounded text-xs font-semibold transition-colors ${
              withAi ? 'bg-violet-600/30 text-violet-300 border border-violet-500/40' : 'bg-slate-800 text-slate-500'
            }`}
          >
            <Settings className="w-3 h-3" />
            IA
          </button>

          {/* Refresh */}
          <button
            onClick={runAnalysis}
            disabled={loading}
            className="flex items-center gap-1 px-2 py-1 bg-blue-600 hover:bg-blue-500 disabled:opacity-50 rounded text-xs font-semibold transition-colors"
          >
            <RefreshCw className={`w-3 h-3 ${loading ? 'animate-spin' : ''}`} />
            <span className="hidden sm:block">Analisar</span>
          </button>
        </div>
      </header>

      {/* Mobile timeframe bar */}
      <div className="sm:hidden flex items-center gap-1 px-2 py-1.5 bg-slate-900 border-b border-slate-800 overflow-x-auto">
        <TimeframeSelector selected={timeframe} onChange={setTimeframe} />
        <button
          onClick={() => setWithAi(v => !v)}
          className={`ml-auto flex items-center gap-1 px-2 py-1 rounded text-xs font-semibold whitespace-nowrap ${
            withAi ? 'bg-violet-600/30 text-violet-300' : 'bg-slate-800 text-slate-500'
          }`}
        >
          <Settings className="w-3 h-3" />
          IA {withAi ? 'ON' : 'OFF'}
        </button>
      </div>

      {/* Main layout */}
      <div className="flex flex-1 overflow-hidden relative">
        {/* Symbol Sidebar */}
        {sidebarOpen && (
          <aside className={`
            ${isMobile ? 'absolute inset-0 z-20 bg-slate-900' : 'relative'}
            w-full sm:w-44 flex-shrink-0 border-r border-slate-800 flex flex-col p-2
          `}>
            {isMobile && (
              <button
                onClick={() => setSidebarOpen(false)}
                className="mb-2 text-xs text-slate-400 hover:text-slate-200 self-end"
              >
                Fechar ✕
              </button>
            )}
            <p className="text-xs text-slate-500 mb-2 font-semibold uppercase tracking-wide">Perpetuos Binance</p>
            <SymbolList symbols={symbols} selected={symbol} onSelect={handleSymbolSelect} />
          </aside>
        )}

        {/* Chart area */}
        <main className="flex-1 flex flex-col min-w-0 overflow-hidden">
          {/* Chart */}
          <div className="relative flex-1 min-h-0 p-2">
            {loading && <LoadingOverlay />}
            {error && (
              <div className="absolute inset-x-2 top-2 z-10 bg-red-900/80 border border-red-500/50 rounded-lg px-3 py-2 text-xs text-red-300">
                {error}
              </div>
            )}
            <CandleChart
              candles={candles}
              signal={signal}
              height={isMobile ? 280 : undefined}
            />
          </div>

          {/* Mobile signal panel */}
          {isMobile && signal && (
            <div className="border-t border-slate-800 p-2 max-h-64 overflow-y-auto bg-slate-900">
              <div className="flex gap-2 mb-2">
                <button
                  onClick={() => setActiveTab('signal')}
                  className={`text-xs px-2 py-1 rounded ${activeTab === 'signal' ? 'bg-blue-600 text-white' : 'bg-slate-800 text-slate-400'}`}
                >
                  Sinal
                </button>
                <button
                  onClick={() => setActiveTab('mtf')}
                  className={`text-xs px-2 py-1 rounded ${activeTab === 'mtf' ? 'bg-blue-600 text-white' : 'bg-slate-800 text-slate-400'}`}
                >
                  Multi-TF
                </button>
              </div>
              <SignalPanel signal={signal} livePrice={ticker?.last} />
            </div>
          )}
        </main>

        {/* Desktop right panel */}
        {!isMobile && (
          <aside className="w-72 xl:w-80 flex-shrink-0 border-l border-slate-800 flex flex-col">
            {/* Tabs */}
            <div className="flex border-b border-slate-800">
              <button
                onClick={() => setActiveTab('signal')}
                className={`flex-1 py-2 text-xs font-semibold flex items-center justify-center gap-1 transition-colors ${
                  activeTab === 'signal' ? 'text-white border-b-2 border-blue-500' : 'text-slate-500 hover:text-slate-300'
                }`}
              >
                <Layers className="w-3.5 h-3.5" />
                Análise
              </button>
              <button
                onClick={() => setActiveTab('mtf')}
                className={`flex-1 py-2 text-xs font-semibold flex items-center justify-center gap-1 transition-colors ${
                  activeTab === 'mtf' ? 'text-white border-b-2 border-blue-500' : 'text-slate-500 hover:text-slate-300'
                }`}
              >
                <BarChart2 className="w-3.5 h-3.5" />
                Multi-TF
              </button>
            </div>

            <div className="flex-1 overflow-y-auto p-2">
              {activeTab === 'signal' && signal ? (
                <SignalPanel signal={signal} livePrice={ticker?.last} />
              ) : activeTab === 'signal' && !signal && !loading ? (
                <div className="flex flex-col items-center justify-center h-full text-slate-600">
                  <BarChart2 className="w-10 h-10 mb-2" />
                  <p className="text-sm">Selecione um ativo e clique em Analisar</p>
                </div>
              ) : activeTab === 'mtf' ? (
                <MultiTimeframePanel symbol={symbol} />
              ) : null}
            </div>

            {/* Status bar */}
            <div className="border-t border-slate-800 px-3 py-1.5 flex items-center justify-between">
              <div className="flex items-center gap-1.5">
                {ticker ? (
                  <Wifi className="w-3 h-3 text-green-400" />
                ) : (
                  <WifiOff className="w-3 h-3 text-slate-600" />
                )}
                <span className="text-xs text-slate-500">
                  {ticker ? `Live: ${ticker.last.toFixed(4)}` : 'Desconectado'}
                </span>
              </div>
              {signal && (
                <span className="text-xs text-slate-600">
                  {new Date(signal.timestamp).toLocaleTimeString('pt-BR')}
                </span>
              )}
            </div>
          </aside>
        )}
      </div>
    </div>
  )
}

// ─── Multi-Timeframe Panel ────────────────────────────────────────────────────

import { useEffect as useEff, useState as useSt } from 'react'
import { api } from './services/api'
import type { TradeSignal } from './types'

function MultiTimeframePanel({ symbol }: { symbol: string }) {
  const [mtf, setMtf] = useSt<Record<string, TradeSignal> | null>(null)
  const [loading, setLoading] = useSt(false)

  useEff(() => {
    setLoading(true)
    api.multiTimeframe(symbol)
      .then(setMtf)
      .catch(() => setMtf(null))
      .finally(() => setLoading(false))
  }, [symbol])

  if (loading) return <div className="text-xs text-slate-500 text-center mt-4">Carregando...</div>
  if (!mtf) return null

  const dirColor = (d: string) =>
    d === 'long' ? 'text-green-400' : d === 'short' ? 'text-red-400' : 'text-yellow-400'

  return (
    <div className="flex flex-col gap-2">
      <p className="text-xs text-slate-500 font-semibold uppercase tracking-wide">Multi-Timeframe</p>
      {Object.entries(mtf).map(([tf, sig]) => (
        <div key={tf} className="bg-slate-800/60 rounded-lg p-2.5">
          <div className="flex items-center justify-between mb-1.5">
            <span className="text-xs font-bold text-white">{tf.toUpperCase()}</span>
            <span className={`text-xs font-bold ${dirColor(sig.direction)}`}>
              {sig.direction === 'long' ? '▲ LONG' : sig.direction === 'short' ? '▼ SHORT' : '◆ NEUTRO'}
            </span>
          </div>
          <div className="grid grid-cols-2 gap-x-2 text-xs">
            <span className="text-slate-500">Confiança</span>
            <span className="text-slate-300">{(sig.confidence * 100).toFixed(0)}%</span>
            <span className="text-slate-500">Tipo</span>
            <span className="text-slate-300">{sig.trade_type.replace('_', ' ')}</span>
            <span className="text-slate-500">Entrada</span>
            <span className="text-slate-300 font-mono">{sig.entry.toFixed(4)}</span>
            <span className="text-slate-500">Stop</span>
            <span className="text-red-400 font-mono">{sig.stop_loss.toFixed(4)}</span>
            <span className="text-slate-500">TP2</span>
            <span className="text-green-400 font-mono">{sig.tp2.toFixed(4)}</span>
          </div>
          {sig.patterns.length > 0 && (
            <p className="text-xs text-slate-500 mt-1 truncate">
              {sig.patterns[0].description}
            </p>
          )}
        </div>
      ))}
    </div>
  )
}
