import { useState, useEffect, useCallback } from 'react'
import { X, RefreshCw, Layers, BarChart2, ArrowLeft, Maximize2, Minimize2 } from 'lucide-react'
import { TradingViewWidget } from './Chart/TradingViewWidget'
import { DrawingPanel } from './Chart/DrawingPanel'
import { SignalPanel } from './SignalPanel/SignalPanel'
import { useAnalysis } from '../hooks/useAnalysis'
import { useLivePrice } from '../hooks/useLivePrice'
import { api } from '../services/api'
import type { TradeSignal } from '../types'

interface Props {
  symbol: string
  timeframe: string
  onClose: () => void
  isMobile?: boolean
  onAddSignalToManager?: (signal: TradeSignal) => void
}

const TIMEFRAMES = ['1m', '5m', '15m', '30m', '1h', '4h', '6h', '8h', '12h', '1d', '3d']

function cleanName(s: string) {
  return s.replace('/USDT:USDT', '/USDT').replace(':USDT', '')
}

function MultiTFPanel({ symbol }: { symbol: string }) {
  const [mtf, setMtf] = useState<Record<string, TradeSignal> | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
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
      <p className="text-xs text-slate-500 font-semibold uppercase tracking-wide mb-1">Multi-Timeframe</p>
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
            <p className="text-xs text-slate-500 mt-1 truncate">{sig.patterns[0].description}</p>
          )}
        </div>
      ))}
    </div>
  )
}

export default function ChartPanel({ symbol, timeframe: initialTf, onClose, isMobile, onAddSignalToManager }: Props) {
  const [tf, setTf] = useState(initialTf)
  const [withAi, setWithAi] = useState(false)
  const [activeTab, setActiveTab] = useState<'signal' | 'mtf'>('signal')
  const [isFullscreen, setIsFullscreen] = useState(false)
  const { signal, loading, error, analyze } = useAnalysis()
  const [displaySignal, setDisplaySignal] = useState<typeof signal>(null)
  const ticker = useLivePrice(symbol)

  const runAnalysis = useCallback(() => {
    analyze(symbol, tf, withAi)
  }, [analyze, symbol, tf, withAi])

  useEffect(() => {
    setDisplaySignal(null)
    runAnalysis()
  }, [symbol, tf])

  useEffect(() => {
    if (signal) setDisplaySignal(signal)
  }, [signal])

  return (
    <div className={`flex flex-col bg-[#0d1320] border-l border-slate-800 ${isFullscreen ? 'fixed inset-0 z-50' : 'h-full'}`}>
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-slate-800 flex-shrink-0 gap-2">
        <div className="flex items-center gap-2 min-w-0">
          {isMobile && (
            <button onClick={onClose} className="p-1 bg-slate-800 hover:bg-slate-700 rounded transition-colors flex-shrink-0">
              <ArrowLeft className="w-3.5 h-3.5" />
            </button>
          )}
          <span className="font-bold text-white text-sm truncate">{cleanName(symbol)}</span>
          {ticker && (
            <>
              <span className="font-mono text-white text-xs hidden xl:block">
                ${ticker.last.toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 4 })}
              </span>
              <span className={`text-xs font-semibold flex-shrink-0 ${ticker.change >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                {ticker.change >= 0 ? '+' : ''}{ticker.change.toFixed(2)}%
              </span>
            </>
          )}
        </div>
        <div className="flex items-center gap-1 flex-shrink-0">
          <div className="hidden sm:flex gap-0.5">
            {TIMEFRAMES.map(t => (
              <button key={t} onClick={() => setTf(t)}
                className={`px-1.5 py-0.5 text-xs font-semibold rounded transition-colors ${tf === t ? 'bg-blue-600 text-white' : 'bg-slate-800 text-slate-400 hover:bg-slate-700'}`}>
                {t}
              </button>
            ))}
          </div>
          <button
            onClick={() => { setWithAi(v => !v); setTimeout(runAnalysis, 50) }}
            className={`px-1.5 py-0.5 text-xs rounded font-semibold border transition-colors ${withAi ? 'bg-violet-600/30 text-violet-300 border-violet-500/40' : 'bg-slate-800 text-slate-500 border-slate-700'}`}
          >IA</button>
          <button onClick={runAnalysis} disabled={loading}
            className="flex items-center gap-1 px-2 py-0.5 bg-blue-600 hover:bg-blue-500 disabled:opacity-50 rounded text-xs font-semibold transition-colors">
            <RefreshCw className={`w-3 h-3 ${loading ? 'animate-spin' : ''}`} />
            <span className="hidden sm:block">Analisar</span>
          </button>
          <button onClick={() => setIsFullscreen(v => !v)}
            className="p-1 bg-slate-800 hover:bg-slate-700 rounded transition-colors"
            title={isFullscreen ? 'Sair da tela cheia' : 'Tela cheia'}>
            {isFullscreen ? <Minimize2 className="w-3.5 h-3.5" /> : <Maximize2 className="w-3.5 h-3.5" />}
          </button>
          <button onClick={onClose} className="p-1 bg-slate-800 hover:bg-slate-700 rounded transition-colors">
            <X className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>

      {/* Mobile timeframe row */}
      <div className="sm:hidden flex gap-0.5 px-2 py-1.5 border-b border-slate-800 flex-shrink-0">
        {TIMEFRAMES.map(t => (
          <button key={t} onClick={() => setTf(t)}
            className={`px-2 py-1 text-xs font-semibold rounded flex-1 ${tf === t ? 'bg-blue-600 text-white' : 'bg-slate-800 text-slate-400'}`}>
            {t}
          </button>
        ))}
      </div>

      {/* Body */}
      <div className="flex flex-1 overflow-hidden">
        {/* TradingView chart */}
        <div className="flex-1 relative min-w-0">
          {error && (
            <div className="absolute inset-x-2 top-2 z-10 bg-red-900/80 border border-red-500/50 rounded px-3 py-2 text-xs text-red-300 pointer-events-none">
              {error}
            </div>
          )}
          <TradingViewWidget symbol={symbol} interval={tf} />
          <DrawingPanel symbol={symbol} timeframe={tf} />
        </div>

        {/* Signal / MTF panel */}
        <div className="w-64 xl:w-72 flex-shrink-0 border-l border-slate-800 flex flex-col">
          <div className="flex border-b border-slate-800 flex-shrink-0">
            <button onClick={() => setActiveTab('signal')}
              className={`flex-1 py-1.5 text-xs font-semibold flex items-center justify-center gap-1 transition-colors ${activeTab === 'signal' ? 'text-white border-b-2 border-blue-500' : 'text-slate-500 hover:text-slate-300'}`}>
              <Layers className="w-3 h-3" /> Análise
            </button>
            <button onClick={() => setActiveTab('mtf')}
              className={`flex-1 py-1.5 text-xs font-semibold flex items-center justify-center gap-1 transition-colors ${activeTab === 'mtf' ? 'text-white border-b-2 border-blue-500' : 'text-slate-500 hover:text-slate-300'}`}>
              <BarChart2 className="w-3 h-3" /> Multi-TF
            </button>
          </div>
          <div className="flex-1 overflow-y-auto p-2">
            {activeTab === 'signal' && loading && (
              <div className="flex items-center justify-center h-full">
                <div className="w-7 h-7 border-2 border-blue-500 border-t-transparent rounded-full animate-spin" />
              </div>
            )}
            {activeTab === 'signal' && !loading && displaySignal && (
              <SignalPanel
                signal={displaySignal}
                livePrice={ticker?.last}
                onAddToManager={() => { if (displaySignal) onAddSignalToManager?.(displaySignal) }}
              />
            )}
            {activeTab === 'signal' && !loading && !displaySignal && (
              <div className="flex flex-col items-center justify-center h-full text-slate-600 text-sm gap-2">
                <BarChart2 className="w-7 h-7" />
                <p className="text-xs">Clique em Analisar</p>
              </div>
            )}
            {activeTab === 'mtf' && <MultiTFPanel symbol={symbol} />}
          </div>
        </div>
      </div>
    </div>
  )
}
