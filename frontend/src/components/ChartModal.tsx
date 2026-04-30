import { useState, useEffect, useCallback } from 'react'
import { X, RefreshCw, Layers, BarChart2 } from 'lucide-react'
import { CandleChart } from './Chart/CandleChart'
import { SignalPanel } from './SignalPanel/SignalPanel'
import { useAnalysis } from '../hooks/useAnalysis'
import { useLivePrice } from '../hooks/useLivePrice'
import { api } from '../services/api'
import type { TradeSignal } from '../types'

interface Props {
  symbol: string
  timeframe: string
  onClose: () => void
}

const TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h', '1d']

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

export default function ChartModal({ symbol, timeframe: initialTf, onClose }: Props) {
  const [tf, setTf] = useState(initialTf)
  const [withAi, setWithAi] = useState(false)
  const [activeTab, setActiveTab] = useState<'signal' | 'mtf'>('signal')
  const { signal, candles, loading, error, analyze } = useAnalysis()
  const ticker = useLivePrice(symbol)

  const runAnalysis = useCallback(() => {
    analyze(symbol, tf, withAi)
  }, [analyze, symbol, tf, withAi])

  useEffect(() => { runAnalysis() }, [symbol, tf])

  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [onClose])

  return (
    <div className="fixed inset-0 z-50 bg-black/85 flex items-center justify-center p-3">
      <div className="bg-[#0d1320] border border-slate-700/60 rounded-xl w-full max-w-6xl h-[92vh] flex flex-col shadow-2xl">
        {/* Modal header */}
        <div className="flex items-center justify-between px-4 py-2.5 border-b border-slate-800 flex-shrink-0">
          <div className="flex items-center gap-3">
            <span className="font-bold text-white text-base">{cleanName(symbol)}</span>
            {ticker && (
              <>
                <span className="font-mono text-white text-sm">
                  ${ticker.last.toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 4 })}
                </span>
                <span className={`text-sm font-semibold ${ticker.change >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                  {ticker.change >= 0 ? '+' : ''}{ticker.change.toFixed(2)}%
                </span>
              </>
            )}
          </div>
          <div className="flex items-center gap-1.5">
            <div className="flex gap-1">
              {TIMEFRAMES.map(t => (
                <button
                  key={t}
                  onClick={() => setTf(t)}
                  className={`px-2 py-1 text-xs font-semibold rounded transition-colors ${
                    tf === t ? 'bg-blue-600 text-white' : 'bg-slate-800 text-slate-400 hover:bg-slate-700'
                  }`}
                >
                  {t}
                </button>
              ))}
            </div>
            <button
              onClick={() => { setWithAi(v => !v); setTimeout(runAnalysis, 50) }}
              className={`px-2 py-1 text-xs rounded font-semibold border transition-colors ${
                withAi
                  ? 'bg-violet-600/30 text-violet-300 border-violet-500/40'
                  : 'bg-slate-800 text-slate-500 border-slate-700'
              }`}
            >
              IA
            </button>
            <button
              onClick={runAnalysis}
              disabled={loading}
              className="flex items-center gap-1 px-2.5 py-1 bg-blue-600 hover:bg-blue-500 disabled:opacity-50 rounded text-xs font-semibold transition-colors"
            >
              <RefreshCw className={`w-3 h-3 ${loading ? 'animate-spin' : ''}`} />
              Analisar
            </button>
            <button
              onClick={onClose}
              className="p-1.5 bg-slate-800 hover:bg-slate-700 rounded transition-colors"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>

        {/* Modal body */}
        <div className="flex flex-1 overflow-hidden">
          {/* Chart */}
          <div className="flex-1 relative min-w-0">
            {loading && (
              <div className="absolute inset-0 flex items-center justify-center bg-slate-900/60 z-10">
                <div className="w-8 h-8 border-2 border-blue-500 border-t-transparent rounded-full animate-spin" />
              </div>
            )}
            {error && (
              <div className="absolute inset-x-2 top-2 z-10 bg-red-900/80 border border-red-500/50 rounded px-3 py-2 text-xs text-red-300">
                {error}
              </div>
            )}
            <CandleChart candles={candles} signal={signal} />
          </div>

          {/* Right panel */}
          <div className="w-72 flex-shrink-0 border-l border-slate-800 flex flex-col">
            <div className="flex border-b border-slate-800">
              <button
                onClick={() => setActiveTab('signal')}
                className={`flex-1 py-2 text-xs font-semibold flex items-center justify-center gap-1 transition-colors ${
                  activeTab === 'signal' ? 'text-white border-b-2 border-blue-500' : 'text-slate-500 hover:text-slate-300'
                }`}
              >
                <Layers className="w-3.5 h-3.5" /> Análise
              </button>
              <button
                onClick={() => setActiveTab('mtf')}
                className={`flex-1 py-2 text-xs font-semibold flex items-center justify-center gap-1 transition-colors ${
                  activeTab === 'mtf' ? 'text-white border-b-2 border-blue-500' : 'text-slate-500 hover:text-slate-300'
                }`}
              >
                <BarChart2 className="w-3.5 h-3.5" /> Multi-TF
              </button>
            </div>
            <div className="flex-1 overflow-y-auto p-2.5">
              {activeTab === 'signal' && signal ? (
                <SignalPanel signal={signal} livePrice={ticker?.last} />
              ) : activeTab === 'signal' && !signal && !loading ? (
                <div className="flex flex-col items-center justify-center h-full text-slate-600 text-sm gap-2">
                  <BarChart2 className="w-8 h-8" />
                  <p>Clique em Analisar</p>
                </div>
              ) : activeTab === 'mtf' ? (
                <MultiTFPanel symbol={symbol} />
              ) : null}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
