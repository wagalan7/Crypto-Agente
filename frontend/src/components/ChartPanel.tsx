import { useState, useEffect, useCallback, useRef } from 'react'
import { X, RefreshCw, Layers, BarChart2, ArrowLeft, Maximize2, Minimize2, Bot } from 'lucide-react'
import { CandleChart, type CandleChartHandle } from './Chart/CandleChart'
import { SignalPanel } from './SignalPanel/SignalPanel'
import { useAnalysis } from '../hooks/useAnalysis'
import { useLivePrice } from '../hooks/useLivePrice'
import { api } from '../services/api'
import DrawingToolbar from './DrawingToolbar'
import type { TradeSignal, UserDrawing, DrawingTool } from '../types'

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
  const { signal, candles, loading, error, analyze } = useAnalysis()
  // displaySignal is cleared immediately on TF/symbol change so old patterns never
  // appear on mismatched candles while the new analysis is loading
  const [displaySignal, setDisplaySignal] = useState<typeof signal>(null)
  const ticker = useLivePrice(symbol)

  // Drawing tools state
  const chartHandleRef = useRef<CandleChartHandle>(null)
  const [drawingTool, setDrawingTool] = useState<DrawingTool>('cursor')
  const [userDrawings, setUserDrawings] = useState<UserDrawing[]>([])
  const [drawStart, setDrawStart] = useState<{ x: number; y: number; price: number; time: number } | null>(null)
  const [previewLine, setPreviewLine] = useState<{ x1: number; y1: number; x2: number; y2: number } | null>(null)
  const [mousePos, setMousePos] = useState<{ x: number; y: number } | null>(null)
  const [aiDrawingAnalysis, setAiDrawingAnalysis] = useState<string>('')
  const [validating, setValidating] = useState(false)
  const svgRef = useRef<SVGSVGElement>(null)

  const runAnalysis = useCallback(() => {
    analyze(symbol, tf, withAi)
  }, [analyze, symbol, tf, withAi])

  useEffect(() => {
    setDisplaySignal(null)  // clear patterns immediately when TF or symbol changes
    runAnalysis()
  }, [symbol, tf])

  useEffect(() => {
    if (signal) setDisplaySignal(signal)
  }, [signal])

  const getRelativePos = (e: React.MouseEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect()
    return { x: e.clientX - rect.left, y: e.clientY - rect.top }
  }

  const handleSvgMouseDown = (e: React.MouseEvent<SVGSVGElement>) => {
    const { x, y } = getRelativePos(e)
    const price = chartHandleRef.current?.yToPrice(y) ?? null
    const time = chartHandleRef.current?.xToTime(x) ?? null
    if (price == null || time == null) return

    if (drawingTool === 'hline') {
      const id = Date.now().toString()
      const color = '#4ade80'
      chartHandleRef.current?.addHLine(price, color, `HL ${id.slice(-4)}`)
      setUserDrawings(prev => [...prev, { id, type: 'hline', price, color, label: `HL ${id.slice(-4)}` }])
    } else if (drawingTool === 'trendline') {
      if (!drawStart) {
        setDrawStart({ x, y, price, time })
      } else {
        const id = Date.now().toString()
        const color = '#f59e0b'
        chartHandleRef.current?.addTrendLine(
          { time: drawStart.time, price: drawStart.price },
          { time, price },
          color
        )
        setUserDrawings(prev => [...prev, {
          id, type: 'trendline',
          p1: { price: drawStart.price, time: drawStart.time },
          p2: { price, time },
          color,
        }])
        setDrawStart(null)
        setPreviewLine(null)
      }
    }
  }

  const handleSvgMouseMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const { x, y } = getRelativePos(e)
    setMousePos({ x, y })
    if (drawingTool === 'trendline' && drawStart) {
      setPreviewLine({ x1: drawStart.x, y1: drawStart.y, x2: x, y2: y })
    }
  }

  const handleSvgMouseLeave = () => {
    setMousePos(null)
    if (drawingTool === 'trendline' && !drawStart) setPreviewLine(null)
  }

  const clearDrawings = () => {
    chartHandleRef.current?.clearDrawings()
    setUserDrawings([])
    setDrawStart(null)
    setPreviewLine(null)
    setAiDrawingAnalysis('')
  }

  const validateDrawingWithAI = async () => {
    if (userDrawings.length === 0) return
    setValidating(true)
    setAiDrawingAnalysis('')
    try {
      const result = await api.validateDrawing(symbol, tf, userDrawings)
      setAiDrawingAnalysis(result.analysis)
    } catch {
      setAiDrawingAnalysis('Erro ao validar com IA. Tente novamente.')
    } finally {
      setValidating(false)
    }
  }

  return (
    <div className={`flex flex-col bg-[#0d1320] border-l border-slate-800 ${isFullscreen ? 'fixed inset-0 z-50' : 'h-full'}`}>
      {/* Panel header */}
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
              <button
                key={t}
                onClick={() => setTf(t)}
                className={`px-1.5 py-0.5 text-xs font-semibold rounded transition-colors ${
                  tf === t ? 'bg-blue-600 text-white' : 'bg-slate-800 text-slate-400 hover:bg-slate-700'
                }`}
              >
                {t}
              </button>
            ))}
          </div>
          <button
            onClick={() => { setWithAi(v => !v); setTimeout(runAnalysis, 50) }}
            className={`px-1.5 py-0.5 text-xs rounded font-semibold border transition-colors ${
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
            className="flex items-center gap-1 px-2 py-0.5 bg-blue-600 hover:bg-blue-500 disabled:opacity-50 rounded text-xs font-semibold transition-colors"
          >
            <RefreshCw className={`w-3 h-3 ${loading ? 'animate-spin' : ''}`} />
            <span className="hidden sm:block">Analisar</span>
          </button>
          <button
            onClick={() => setIsFullscreen(v => !v)}
            className="p-1 bg-slate-800 hover:bg-slate-700 rounded transition-colors"
            title={isFullscreen ? 'Sair da tela cheia' : 'Tela cheia'}
          >
            {isFullscreen
              ? <Minimize2 className="w-3.5 h-3.5" />
              : <Maximize2 className="w-3.5 h-3.5" />}
          </button>
          <button
            onClick={onClose}
            className="p-1 bg-slate-800 hover:bg-slate-700 rounded transition-colors"
          >
            <X className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>

      {/* Mobile timeframe row */}
      <div className="sm:hidden flex gap-0.5 px-2 py-1.5 border-b border-slate-800 flex-shrink-0">
        {TIMEFRAMES.map(t => (
          <button
            key={t}
            onClick={() => setTf(t)}
            className={`px-2 py-1 text-xs font-semibold rounded flex-1 ${
              tf === t ? 'bg-blue-600 text-white' : 'bg-slate-800 text-slate-400'
            }`}
          >
            {t}
          </button>
        ))}
      </div>

      {/* Body: chart + right panel */}
      <div className="flex flex-1 overflow-hidden">
        {/* Chart */}
        <div className="flex-1 relative min-w-0">
          {loading && (
            <div className="absolute inset-0 flex items-center justify-center bg-slate-900/60 z-10">
              <div className="w-7 h-7 border-2 border-blue-500 border-t-transparent rounded-full animate-spin" />
            </div>
          )}
          {error && (
            <div className="absolute inset-x-2 top-2 z-10 bg-red-900/80 border border-red-500/50 rounded px-3 py-2 text-xs text-red-300">
              {error}
            </div>
          )}
          <CandleChart ref={chartHandleRef} candles={candles} signal={displaySignal} />

          {/* Drawing SVG overlay */}
          <svg
            ref={svgRef}
            className="absolute inset-0 w-full h-full"
            style={{
              pointerEvents: drawingTool !== 'cursor' ? 'all' : 'none',
              zIndex: 5,
              cursor: drawingTool === 'hline' ? 'crosshair' : drawingTool === 'trendline' ? 'crosshair' : 'default',
            }}
            onMouseDown={handleSvgMouseDown}
            onMouseMove={handleSvgMouseMove}
            onMouseLeave={handleSvgMouseLeave}
          >
            {/* Ghost H-line preview */}
            {drawingTool === 'hline' && mousePos && (
              <line x1="0" y1={mousePos.y} x2="100%" y2={mousePos.y}
                stroke="#4ade80" strokeWidth="1" strokeDasharray="6,4" opacity="0.5" />
            )}
            {/* Ghost trend line preview */}
            {drawingTool === 'trendline' && previewLine && (
              <line
                x1={previewLine.x1} y1={previewLine.y1}
                x2={previewLine.x2} y2={previewLine.y2}
                stroke="#f59e0b" strokeWidth="1.5" strokeDasharray="6,4" opacity="0.7"
              />
            )}
            {/* First point indicator */}
            {drawingTool === 'trendline' && drawStart && (
              <circle cx={drawStart.x} cy={drawStart.y} r="4"
                fill="#f59e0b" opacity="0.8" />
            )}
          </svg>

          {/* Drawing toolbar */}
          <DrawingToolbar
            activeTool={drawingTool}
            onToolChange={(t) => { setDrawingTool(t); setDrawStart(null); setPreviewLine(null) }}
            onClear={clearDrawings}
            onValidate={validateDrawingWithAI}
            hasDrawings={userDrawings.length > 0}
            validating={validating}
          />

          {/* AI drawing validation result */}
          {aiDrawingAnalysis && (
            <div className="absolute bottom-2 left-2 right-2 z-10 bg-slate-900/95 border border-violet-500/40 rounded-lg p-3 max-h-48 overflow-y-auto backdrop-blur-sm">
              <div className="flex items-center justify-between mb-1">
                <div className="flex items-center gap-1">
                  <Bot className="w-3.5 h-3.5 text-violet-400" />
                  <span className="text-xs font-semibold text-violet-400">Validação IA do Padrão</span>
                </div>
                <button onClick={() => setAiDrawingAnalysis('')} className="text-slate-500 hover:text-white text-xs">✕</button>
              </div>
              <p className="text-xs text-slate-300 whitespace-pre-line leading-relaxed">{aiDrawingAnalysis}</p>
            </div>
          )}
        </div>

        {/* Signal / MTF panel */}
        <div className="w-64 xl:w-72 flex-shrink-0 border-l border-slate-800 flex flex-col">
          <div className="flex border-b border-slate-800 flex-shrink-0">
            <button
              onClick={() => setActiveTab('signal')}
              className={`flex-1 py-1.5 text-xs font-semibold flex items-center justify-center gap-1 transition-colors ${
                activeTab === 'signal' ? 'text-white border-b-2 border-blue-500' : 'text-slate-500 hover:text-slate-300'
              }`}
            >
              <Layers className="w-3 h-3" /> Análise
            </button>
            <button
              onClick={() => setActiveTab('mtf')}
              className={`flex-1 py-1.5 text-xs font-semibold flex items-center justify-center gap-1 transition-colors ${
                activeTab === 'mtf' ? 'text-white border-b-2 border-blue-500' : 'text-slate-500 hover:text-slate-300'
              }`}
            >
              <BarChart2 className="w-3 h-3" /> Multi-TF
            </button>
          </div>
          <div className="flex-1 overflow-y-auto p-2">
            {activeTab === 'signal' && displaySignal ? (
              <SignalPanel
                signal={displaySignal}
                livePrice={ticker?.last}
                onAddToManager={() => {
                  if (displaySignal) onAddSignalToManager?.(displaySignal)
                }}
              />
            ) : activeTab === 'signal' && !displaySignal && !loading ? (
              <div className="flex flex-col items-center justify-center h-full text-slate-600 text-sm gap-2">
                <BarChart2 className="w-7 h-7" />
                <p className="text-xs">Clique em Analisar</p>
              </div>
            ) : activeTab === 'mtf' ? (
              <MultiTFPanel symbol={symbol} />
            ) : null}
          </div>
        </div>
      </div>
    </div>
  )
}
