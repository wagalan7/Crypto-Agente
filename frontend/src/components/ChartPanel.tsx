import { useState, useEffect, useCallback, useRef } from 'react'
import { X, RefreshCw, Layers, BarChart2, ArrowLeft, Maximize2, Minimize2, Bot } from 'lucide-react'
import { CandleChart, type CandleChartHandle } from './Chart/CandleChart'
import { SignalPanel } from './SignalPanel/SignalPanel'
import { useAnalysis } from '../hooks/useAnalysis'
import { useLivePrice } from '../hooks/useLivePrice'
import { api } from '../services/api'
import DrawingToolbar from './DrawingToolbar'
import type { TradeSignal, UserDrawing, DrawingTool, HLineDrawing, TrendLineDrawing, FibonacciDrawing, RectangleDrawing } from '../types'

interface Props {
  symbol: string
  timeframe: string
  onClose: () => void
  isMobile?: boolean
  onAddSignalToManager?: (signal: TradeSignal) => void
}

interface DragState {
  drawingId: string
  grabPoint: 'body' | 'p1' | 'p2'
  startClientX: number
  startClientY: number
  snapshot: UserDrawing
}

const TIMEFRAMES = ['1m', '5m', '15m', '30m', '1h', '4h', '6h', '8h', '12h', '1d', '3d']

const FIB_LEVELS = [
  { r: 0,     color: '#94a3b8', label: '0%'    },
  { r: 0.236, color: '#60a5fa', label: '23.6%' },
  { r: 0.382, color: '#4ade80', label: '38.2%' },
  { r: 0.5,   color: '#fbbf24', label: '50%'   },
  { r: 0.618, color: '#f97316', label: '61.8%' },
  { r: 0.786, color: '#f87171', label: '78.6%' },
  { r: 1,     color: '#94a3b8', label: '100%'  },
]

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
  const [displaySignal, setDisplaySignal] = useState<typeof signal>(null)
  const ticker = useLivePrice(symbol)

  // ── Drawing tools ─────────────────────────────────────────────────────────
  const chartHandleRef = useRef<CandleChartHandle>(null)
  const svgRef = useRef<SVGSVGElement>(null)

  const [drawingTool, setDrawingTool] = useState<DrawingTool>('cursor')
  const [userDrawings, setUserDrawings] = useState<UserDrawing[]>([])
  const [drawStart, setDrawStart] = useState<{ x: number; y: number; price: number; time: number } | null>(null)
  const [previewLine, setPreviewLine] = useState<{ x1: number; y1: number; x2: number; y2: number } | null>(null)
  const [mousePos, setMousePos] = useState<{ x: number; y: number } | null>(null)

  // Drag state (cursor mode)
  const [dragging, setDragging] = useState<DragState | null>(null)
  const [dragPreview, setDragPreview] = useState<{ x1: number; y1: number; x2: number; y2: number } | null>(null)

  // Bump to re-render SVG when chart viewport changes
  const [viewportTick, setViewportTick] = useState(0)

  // AI validation
  const [aiDrawingAnalysis, setAiDrawingAnalysis] = useState<string>('')
  const [validating, setValidating] = useState(false)

  // ── Analysis ──────────────────────────────────────────────────────────────
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

  // Subscribe to chart viewport changes so SVG re-renders correctly on zoom/pan
  useEffect(() => {
    if (!candles.length) return
    // Small delay to ensure chart is ready
    const t = setTimeout(() => {
      const unsub = chartHandleRef.current?.subscribeToViewport(() => {
        setViewportTick(v => v + 1)
      })
      return () => unsub?.()
    }, 300)
    return () => clearTimeout(t)
  }, [candles.length])

  // ── Coordinate helpers (called during render, uses viewportTick) ──────────
  const getDrawingCoords = (drawing: UserDrawing): { x1: number; y1: number; x2: number; y2: number } | null => {
    const h = chartHandleRef.current
    if (!h) return null
    if (drawing.type === 'hline') {
      const y = h.priceToY(drawing.price)
      return y != null ? { x1: 0, y1: y, x2: 9999, y2: y } : null
    } else {
      const x1 = h.timeToX(drawing.p1.time)
      const y1 = h.priceToY(drawing.p1.price)
      const x2 = h.timeToX(drawing.p2.time)
      const y2 = h.priceToY(drawing.p2.price)
      return (x1 != null && y1 != null && x2 != null && y2 != null) ? { x1, y1, x2, y2 } : null
    }
  }

  const getSvgPos = (clientX: number, clientY: number) => {
    const rect = svgRef.current?.getBoundingClientRect()
    if (!rect) return null
    return { x: clientX - rect.left, y: clientY - rect.top }
  }

  // ── Drawing (hline / trendline) ───────────────────────────────────────────
  const handleSvgMouseDown = (e: React.MouseEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect()
    const x = e.clientX - rect.left
    const y = e.clientY - rect.top
    const h = chartHandleRef.current
    const price = h?.yToPrice(y) ?? null
    const time = h?.xToTime(x) ?? null
    if (price == null || time == null) return

    if (drawingTool === 'hline') {
      const id = Date.now().toString()
      const color = '#4ade80'
      chartHandleRef.current?.addHLine(id, price, color, `HL ${id.slice(-4)}`)
      setUserDrawings(prev => [...prev, { id, type: 'hline', price, color, label: `HL ${id.slice(-4)}` }])
    } else if (drawingTool === 'trendline') {
      if (!drawStart) {
        setDrawStart({ x, y, price, time })
      } else {
        const id = Date.now().toString()
        const color = '#f59e0b'
        chartHandleRef.current?.addTrendLine(
          id,
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
    } else if (drawingTool === 'fibonacci') {
      if (!drawStart) {
        setDrawStart({ x, y, price, time })
      } else {
        const id = Date.now().toString()
        setUserDrawings(prev => [...prev, {
          id, type: 'fibonacci' as const,
          p1: { price: drawStart.price, time: drawStart.time },
          p2: { price, time },
          color: '#f59e0b',
        }])
        setDrawStart(null)
        setPreviewLine(null)
      }
    } else if (drawingTool === 'rectangle') {
      if (!drawStart) {
        setDrawStart({ x, y, price, time })
      } else {
        const id = Date.now().toString()
        setUserDrawings(prev => [...prev, {
          id, type: 'rectangle' as const,
          p1: { price: drawStart.price, time: drawStart.time },
          p2: { price, time },
          color: '#3b82f6',
        }])
        setDrawStart(null)
        setPreviewLine(null)
      }
    }
  }

  const handleSvgMouseMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect()
    const x = e.clientX - rect.left
    const y = e.clientY - rect.top
    setMousePos({ x, y })
    if ((drawingTool === 'trendline' || drawingTool === 'fibonacci') && drawStart) {
      setPreviewLine({ x1: drawStart.x, y1: drawStart.y, x2: x, y2: y })
    }
    if (drawingTool === 'rectangle' && drawStart) {
      setPreviewLine({ x1: drawStart.x, y1: drawStart.y, x2: x, y2: y })
    }
  }

  const handleSvgMouseLeave = () => {
    setMousePos(null)
    if (!drawStart) setPreviewLine(null)
  }

  // ── Drag handlers (attached to individual drawing SVG elements) ───────────
  const startHLineDrag = (e: React.MouseEvent, drawing: HLineDrawing) => {
    e.stopPropagation()
    e.preventDefault()
    chartHandleRef.current?.removeDrawingById(drawing.id)
    setDragging({ drawingId: drawing.id, grabPoint: 'body', startClientX: e.clientX, startClientY: e.clientY, snapshot: drawing })
  }

  const startTrendLineDrag = (e: React.MouseEvent, drawing: TrendLineDrawing, grabPoint: 'p1' | 'p2' | 'body') => {
    e.stopPropagation()
    e.preventDefault()
    chartHandleRef.current?.removeDrawingById(drawing.id)
    setDragging({ drawingId: drawing.id, grabPoint, startClientX: e.clientX, startClientY: e.clientY, snapshot: drawing })
  }

  const startFibonacciDrag = (e: React.MouseEvent, drawing: FibonacciDrawing, grabPoint: 'p1' | 'p2' | 'body') => {
    e.stopPropagation(); e.preventDefault()
    // Fibonacci is SVG-only — no chart series to remove
    setDragging({ drawingId: drawing.id, grabPoint, startClientX: e.clientX, startClientY: e.clientY, snapshot: drawing })
  }

  const startRectangleDrag = (e: React.MouseEvent, drawing: RectangleDrawing, grabPoint: 'p1' | 'p2' | 'body') => {
    e.stopPropagation(); e.preventDefault()
    setDragging({ drawingId: drawing.id, grabPoint, startClientX: e.clientX, startClientY: e.clientY, snapshot: drawing })
  }

  // Document-level drag events (fires while dragging, outside SVG too)
  useEffect(() => {
    if (!dragging) return

    const onMove = (e: MouseEvent) => {
      const pos = getSvgPos(e.clientX, e.clientY)
      if (!pos) return
      const h = chartHandleRef.current
      if (!h) return
      const { grabPoint, snapshot } = dragging
      const dx = e.clientX - dragging.startClientX
      const dy = e.clientY - dragging.startClientY

      if (snapshot.type === 'hline') {
        setDragPreview({ x1: 0, y1: pos.y, x2: 9999, y2: pos.y })
      } else {
        const origCoords = getDrawingCoords(snapshot)
        if (!origCoords) return
        if (grabPoint === 'p1') {
          setDragPreview({ x1: origCoords.x1 + dx, y1: origCoords.y1 + dy, x2: origCoords.x2, y2: origCoords.y2 })
        } else if (grabPoint === 'p2') {
          setDragPreview({ x1: origCoords.x1, y1: origCoords.y1, x2: origCoords.x2 + dx, y2: origCoords.y2 + dy })
        } else {
          setDragPreview({ x1: origCoords.x1 + dx, y1: origCoords.y1 + dy, x2: origCoords.x2 + dx, y2: origCoords.y2 + dy })
        }
      }
    }

    const onUp = (e: MouseEvent) => {
      const pos = getSvgPos(e.clientX, e.clientY)
      const h = chartHandleRef.current
      const { drawingId, grabPoint, snapshot } = dragging
      const dx = e.clientX - dragging.startClientX
      const dy = e.clientY - dragging.startClientY

      let newDrawing: UserDrawing

      if (snapshot.type === 'hline') {
        const newPrice = (h && pos) ? (h.yToPrice(pos.y) ?? snapshot.price) : snapshot.price
        newDrawing = { ...snapshot, price: newPrice }
        h?.addHLine(drawingId, newPrice, snapshot.color, snapshot.label)
      } else if (snapshot.type === 'trendline') {
        const origCoords = getDrawingCoords(snapshot)
        let newP1 = { ...snapshot.p1 }
        let newP2 = { ...snapshot.p2 }
        if (h && pos) {
          if (grabPoint === 'p1') {
            newP1 = { price: h.yToPrice(pos.y) ?? snapshot.p1.price, time: h.xToTime(pos.x) ?? snapshot.p1.time }
          } else if (grabPoint === 'p2') {
            newP2 = { price: h.yToPrice(pos.y) ?? snapshot.p2.price, time: h.xToTime(pos.x) ?? snapshot.p2.time }
          } else if (origCoords) {
            newP1 = { price: h.yToPrice(origCoords.y1 + dy) ?? snapshot.p1.price, time: h.xToTime(origCoords.x1 + dx) ?? snapshot.p1.time }
            newP2 = { price: h.yToPrice(origCoords.y2 + dy) ?? snapshot.p2.price, time: h.xToTime(origCoords.x2 + dx) ?? snapshot.p2.time }
          }
        }
        newDrawing = { ...snapshot, p1: newP1, p2: newP2 }
        h?.addTrendLine(drawingId, newP1, newP2, snapshot.color)
      } else {
        // fibonacci | rectangle — SVG only, no chart series
        const snap = snapshot as FibonacciDrawing | RectangleDrawing
        const origCoords = getDrawingCoords(snap)
        let newP1 = { ...snap.p1 }
        let newP2 = { ...snap.p2 }
        if (h && pos) {
          if (grabPoint === 'p1') {
            newP1 = { price: h.yToPrice(pos.y) ?? snap.p1.price, time: h.xToTime(pos.x) ?? snap.p1.time }
          } else if (grabPoint === 'p2') {
            newP2 = { price: h.yToPrice(pos.y) ?? snap.p2.price, time: h.xToTime(pos.x) ?? snap.p2.time }
          } else if (origCoords) {
            newP1 = { price: h.yToPrice(origCoords.y1 + dy) ?? snap.p1.price, time: h.xToTime(origCoords.x1 + dx) ?? snap.p1.time }
            newP2 = { price: h.yToPrice(origCoords.y2 + dy) ?? snap.p2.price, time: h.xToTime(origCoords.x2 + dx) ?? snap.p2.time }
          }
        }
        newDrawing = { ...snap, p1: newP1, p2: newP2 }
      }

      setUserDrawings(prev => prev.map(d => d.id === drawingId ? newDrawing : d))
      setDragging(null)
      setDragPreview(null)
    }

    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
    return () => {
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dragging])

  // ── Clear & validate ──────────────────────────────────────────────────────
  const clearDrawings = () => {
    chartHandleRef.current?.clearDrawings()
    setUserDrawings([])
    setDrawStart(null)
    setPreviewLine(null)
    setDragging(null)
    setDragPreview(null)
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

  // ── SVG pointer events ────────────────────────────────────────────────────
  // In drawing mode: SVG blocks events (for drawing). In cursor mode: SVG is
  // transparent to events except on interactive drawing elements.
  const svgPointerEvents = drawingTool !== 'cursor' ? 'all' : 'none'
  const svgCursor =
    dragging ? 'grabbing' :
    drawingTool !== 'cursor' ? 'crosshair' : 'default'

  // Used by getDrawingCoords to trigger re-render on zoom/pan (viewportTick is read)
  void viewportTick

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
        {/* Chart area */}
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

          {/* SVG overlay — interactive drawing elements */}
          <svg
            ref={svgRef}
            className="absolute inset-0 w-full h-full"
            style={{ pointerEvents: svgPointerEvents, zIndex: 5, cursor: svgCursor }}
            onMouseDown={drawingTool !== 'cursor' ? handleSvgMouseDown : undefined}
            onMouseMove={handleSvgMouseMove}
            onMouseLeave={handleSvgMouseLeave}
          >
            {/* ── Existing drawings (interactive in cursor mode) ──────────── */}
            {userDrawings
              .filter(d => !dragging || d.id !== dragging.drawingId)
              .map(drawing => {
                const coords = getDrawingCoords(drawing)
                if (!coords) return null

                if (drawing.type === 'hline') {
                  return (
                    <g key={drawing.id}>
                      {/* Visible ghost line (dim in cursor mode since chart has the real one) */}
                      <line x1="0" y1={coords.y1} x2="100%" y2={coords.y1}
                        stroke={drawing.color} strokeWidth="1" strokeDasharray="6,4" opacity="0.3"
                        style={{ pointerEvents: 'none' }} />
                      {/* Wide invisible hitbox — cursor mode: pointer events enabled */}
                      <line x1="0" y1={coords.y1} x2="100%" y2={coords.y1}
                        stroke="transparent" strokeWidth="18"
                        style={{
                          cursor: 'ns-resize',
                          pointerEvents: drawingTool === 'cursor' ? 'stroke' : 'none',
                        }}
                        onMouseDown={(e) => startHLineDrag(e, drawing as HLineDrawing)}
                      />
                    </g>
                  )
                }

                if (drawing.type === 'trendline') {
                  const td = drawing as TrendLineDrawing
                  return (
                    <g key={drawing.id}>
                      {/* Line body (dim ghost) */}
                      <line x1={coords.x1} y1={coords.y1} x2={coords.x2} y2={coords.y2}
                        stroke={drawing.color} strokeWidth="1" opacity="0.3"
                        style={{ pointerEvents: 'none' }} />
                      {/* Body hitbox */}
                      <line x1={coords.x1} y1={coords.y1} x2={coords.x2} y2={coords.y2}
                        stroke="transparent" strokeWidth="18"
                        style={{
                          cursor: 'move',
                          pointerEvents: drawingTool === 'cursor' ? 'stroke' : 'none',
                        }}
                        onMouseDown={(e) => startTrendLineDrag(e, td, 'body')}
                      />
                      {/* Endpoint P1 */}
                      <circle cx={coords.x1} cy={coords.y1} r="6"
                        fill={drawing.color} opacity="0.7"
                        style={{
                          cursor: 'crosshair',
                          pointerEvents: drawingTool === 'cursor' ? 'all' : 'none',
                        }}
                        onMouseDown={(e) => startTrendLineDrag(e, td, 'p1')}
                      />
                      {/* Endpoint P2 */}
                      <circle cx={coords.x2} cy={coords.y2} r="6"
                        fill={drawing.color} opacity="0.7"
                        style={{
                          cursor: 'crosshair',
                          pointerEvents: drawingTool === 'cursor' ? 'all' : 'none',
                        }}
                        onMouseDown={(e) => startTrendLineDrag(e, td, 'p2')}
                      />
                    </g>
                  )
                }
                if (drawing.type === 'fibonacci') {
                  const fd = drawing as FibonacciDrawing
                  const h = chartHandleRef.current
                  if (!h) return null
                  const cy1 = h.priceToY(fd.p1.price)
                  const cx1 = h.timeToX(fd.p1.time)
                  const cy2 = h.priceToY(fd.p2.price)
                  const cx2 = h.timeToX(fd.p2.time)
                  return (
                    <g key={drawing.id}>
                      {FIB_LEVELS.map(({ r, color, label }) => {
                        const price = fd.p1.price + r * (fd.p2.price - fd.p1.price)
                        const y = h.priceToY(price)
                        if (y == null) return null
                        return (
                          <g key={r}>
                            <line x1="0" y1={y} x2="100%" y2={y}
                              stroke={color} strokeWidth="0.8" strokeDasharray="5,3" opacity="0.7"
                              style={{ pointerEvents: 'none' }} />
                            <text x="4" y={y - 2} fill={color} fontSize="8.5" fontFamily="monospace" opacity="0.85"
                              style={{ pointerEvents: 'none' }}>
                              {label}
                            </text>
                            <text x="99%" y={y - 2} textAnchor="end" fill={color} fontSize="8.5" fontFamily="monospace" opacity="0.85"
                              style={{ pointerEvents: 'none' }}>
                              {price < 1 ? price.toFixed(6) : price < 100 ? price.toFixed(3) : price.toFixed(2)}
                            </text>
                          </g>
                        )
                      })}
                      {drawingTool === 'cursor' && cx1 != null && cy1 != null && (
                        <circle cx={cx1} cy={cy1} r="6" fill="#f59e0b" opacity="0.7"
                          style={{ cursor: 'crosshair', pointerEvents: 'all' }}
                          onMouseDown={(e) => startFibonacciDrag(e, fd, 'p1')} />
                      )}
                      {drawingTool === 'cursor' && cx2 != null && cy2 != null && (
                        <circle cx={cx2} cy={cy2} r="6" fill="#f59e0b" opacity="0.7"
                          style={{ cursor: 'crosshair', pointerEvents: 'all' }}
                          onMouseDown={(e) => startFibonacciDrag(e, fd, 'p2')} />
                      )}
                    </g>
                  )
                }

                if (drawing.type === 'rectangle') {
                  const rd = drawing as RectangleDrawing
                  const h = chartHandleRef.current
                  if (!h) return null
                  const rx1 = h.timeToX(rd.p1.time)
                  const ry1 = h.priceToY(rd.p1.price)
                  const rx2 = h.timeToX(rd.p2.time)
                  const ry2 = h.priceToY(rd.p2.price)
                  if (rx1 == null || ry1 == null || rx2 == null || ry2 == null) return null
                  const rxx = Math.min(rx1, rx2), ryy = Math.min(ry1, ry2)
                  const rw = Math.abs(rx2 - rx1), rh = Math.abs(ry2 - ry1)
                  return (
                    <g key={drawing.id}>
                      <rect x={rxx} y={ryy} width={rw} height={rh}
                        fill={`${rd.color}18`} stroke={rd.color} strokeWidth="1" strokeDasharray="5,3"
                        style={{ pointerEvents: 'none' }} />
                      {drawingTool === 'cursor' && (
                        <>
                          <rect x={rxx} y={ryy} width={rw} height={rh}
                            fill="transparent" stroke="transparent" strokeWidth="12"
                            style={{ cursor: 'move', pointerEvents: 'stroke' }}
                            onMouseDown={(e) => startRectangleDrag(e, rd, 'body')} />
                          <circle cx={rx1} cy={ry1} r="6" fill={rd.color} opacity="0.7"
                            style={{ cursor: 'crosshair', pointerEvents: 'all' }}
                            onMouseDown={(e) => startRectangleDrag(e, rd, 'p1')} />
                          <circle cx={rx2} cy={ry2} r="6" fill={rd.color} opacity="0.7"
                            style={{ cursor: 'crosshair', pointerEvents: 'all' }}
                            onMouseDown={(e) => startRectangleDrag(e, rd, 'p2')} />
                        </>
                      )}
                    </g>
                  )
                }

                return null
              })
            }

            {/* ── Drawing mode previews ───────────────────────────────────── */}
            {/* H-line ghost while hovering */}
            {drawingTool === 'hline' && mousePos && (
              <line x1="0" y1={mousePos.y} x2="100%" y2={mousePos.y}
                stroke="#4ade80" strokeWidth="1" strokeDasharray="6,4" opacity="0.5" />
            )}
            {/* Trend/Fibonacci line first-point dot */}
            {(drawingTool === 'trendline' || drawingTool === 'fibonacci') && drawStart && (
              <circle cx={drawStart.x} cy={drawStart.y} r="4" fill="#f59e0b" opacity="0.9" />
            )}
            {/* Trend/Fibonacci line ghost line */}
            {(drawingTool === 'trendline' || drawingTool === 'fibonacci') && previewLine && (
              <line x1={previewLine.x1} y1={previewLine.y1} x2={previewLine.x2} y2={previewLine.y2}
                stroke="#f59e0b" strokeWidth="1.5" strokeDasharray="6,4" opacity="0.7" />
            )}
            {/* Rectangle preview */}
            {drawingTool === 'rectangle' && drawStart && mousePos && (
              <rect
                x={Math.min(drawStart.x, mousePos.x)}
                y={Math.min(drawStart.y, mousePos.y)}
                width={Math.abs(mousePos.x - drawStart.x)}
                height={Math.abs(mousePos.y - drawStart.y)}
                fill="#3b82f618" stroke="#3b82f6" strokeWidth="1" strokeDasharray="4,3" opacity="0.7"
              />
            )}

            {/* ── Drag preview ───────────────────────────────────────────── */}
            {dragging && dragPreview && (() => {
              const snap = dragging.snapshot
              if (snap.type === 'hline') {
                return (
                  <line x1="0" y1={dragPreview.y1} x2="100%" y2={dragPreview.y1}
                    stroke={(snap as HLineDrawing).color} strokeWidth="1.5" strokeDasharray="6,4" opacity="0.8" />
                )
              }
              if (snap.type === 'trendline') {
                return (
                  <>
                    <line x1={dragPreview.x1} y1={dragPreview.y1} x2={dragPreview.x2} y2={dragPreview.y2}
                      stroke={(snap as TrendLineDrawing).color} strokeWidth="1.5" opacity="0.8" />
                    <circle cx={dragPreview.x1} cy={dragPreview.y1} r="5" fill={(snap as TrendLineDrawing).color} opacity="0.8" />
                    <circle cx={dragPreview.x2} cy={dragPreview.y2} r="5" fill={(snap as TrendLineDrawing).color} opacity="0.8" />
                  </>
                )
              }
              if (snap.type === 'fibonacci') {
                const fd = snap as FibonacciDrawing
                const h = chartHandleRef.current
                const { grabPoint } = dragging
                let preP1price = fd.p1.price, preP2price = fd.p2.price
                if (h) {
                  if (grabPoint === 'p1') { preP1price = h.yToPrice(dragPreview.y1) ?? fd.p1.price }
                  else if (grabPoint === 'p2') { preP2price = h.yToPrice(dragPreview.y2) ?? fd.p2.price }
                  else { preP1price = h.yToPrice(dragPreview.y1) ?? fd.p1.price; preP2price = h.yToPrice(dragPreview.y2) ?? fd.p2.price }
                }
                return (
                  <>
                    {FIB_LEVELS.map(({ r, color }) => {
                      const price = preP1price + r * (preP2price - preP1price)
                      const y = h?.priceToY(price)
                      if (y == null) return null
                      return <line key={r} x1="0" y1={y} x2="100%" y2={y} stroke={color} strokeWidth="0.8" strokeDasharray="5,3" opacity="0.4" />
                    })}
                    <circle cx={dragPreview.x1} cy={dragPreview.y1} r="5" fill="#f59e0b" opacity="0.8" />
                    <circle cx={dragPreview.x2} cy={dragPreview.y2} r="5" fill="#f59e0b" opacity="0.8" />
                  </>
                )
              }
              if (snap.type === 'rectangle') {
                const rd = snap as RectangleDrawing
                const rxx = Math.min(dragPreview.x1, dragPreview.x2)
                const ryy = Math.min(dragPreview.y1, dragPreview.y2)
                const rw = Math.abs(dragPreview.x2 - dragPreview.x1)
                const rh = Math.abs(dragPreview.y2 - dragPreview.y1)
                return (
                  <rect x={rxx} y={ryy} width={rw} height={rh}
                    fill={`${rd.color}20`} stroke={rd.color} strokeWidth="1.5" strokeDasharray="4,3" opacity="0.7" />
                )
              }
              return null
            })()}
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
            {activeTab === 'signal' && displaySignal ? (
              <SignalPanel
                signal={displaySignal}
                livePrice={ticker?.last}
                onAddToManager={() => { if (displaySignal) onAddSignalToManager?.(displaySignal) }}
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
