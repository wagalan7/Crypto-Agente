import { useEffect, useRef, forwardRef, useImperativeHandle } from 'react'
import {
  createChart,
  IChartApi,
  ISeriesApi,
  CandlestickData,
  HistogramData,
  LineStyle,
  ColorType,
  Time,
  IPriceLine,
} from 'lightweight-charts'
import type { OHLCVCandle, TradeSignal } from '../../types'

interface Props {
  candles: OHLCVCandle[]
  signal: TradeSignal | null
  height?: number
}

export interface CandleChartHandle {
  yToPrice: (y: number) => number | null
  xToTime: (x: number) => number | null
  priceToY: (price: number) => number | null
  timeToX: (time: number) => number | null
  /** Add a horizontal price line identified by id */
  addHLine: (id: string, price: number, color: string, title: string) => void
  /** Add a trend line identified by id */
  addTrendLine: (
    id: string,
    p1: { time: number; price: number },
    p2: { time: number; price: number },
    color: string
  ) => void
  /** Remove a specific drawing by id */
  removeDrawingById: (id: string) => void
  /** Remove all user-drawn elements */
  clearDrawings: () => void
  /** Subscribe to chart viewport changes (zoom/pan); returns unsubscribe fn */
  subscribeToViewport: (cb: () => void) => () => void
}

const PATTERN_COLORS: Record<string, string> = {
  lta: '#22c55e', ltb: '#ef4444',
  ascending_channel: '#22c55e', descending_channel: '#ef4444',
  horizontal_channel: '#a78bfa', symmetric_triangle: '#f59e0b',
  ascending_triangle: '#22c55e', descending_triangle: '#ef4444',
  ascending_wedge: '#ef4444', descending_wedge: '#22c55e',
  head_and_shoulders: '#f97316', inverse_head_and_shoulders: '#22c55e',
  double_top: '#ef4444', double_bottom: '#22c55e',
  bull_flag: '#22c55e', bear_flag: '#ef4444',
}

export const CandleChart = forwardRef<CandleChartHandle, Props>(
  function CandleChart({ candles, signal, height }, ref) {
    const containerRef = useRef<HTMLDivElement>(null)
    const chartRef = useRef<IChartApi | null>(null)
    const candleSeriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
    const volumeSeriesRef = useRef<ISeriesApi<'Histogram'> | null>(null)
    const patternLinesRef = useRef<ISeriesApi<'Line'>[]>([])
    const levelLinesRef = useRef<ISeriesApi<'Line'>[]>([])
    // Map: drawing id → cleanup function
    const drawingMapRef = useRef<Map<string, { remove: () => void }>>(new Map())

    useImperativeHandle(ref, () => ({
      yToPrice: (y) => {
        if (!candleSeriesRef.current) return null
        return candleSeriesRef.current.coordinateToPrice(y) ?? null
      },
      xToTime: (x) => {
        if (!chartRef.current) return null
        const t = chartRef.current.timeScale().coordinateToTime(x)
        return t != null ? (t as number) : null
      },
      priceToY: (price) => {
        if (!candleSeriesRef.current) return null
        return candleSeriesRef.current.priceToCoordinate(price) ?? null
      },
      timeToX: (time) => {
        if (!chartRef.current) return null
        return chartRef.current.timeScale().timeToCoordinate(time as Time) ?? null
      },

      addHLine: (id, price, color, title) => {
        if (!candleSeriesRef.current) return
        // Remove existing entry with same id if any
        drawingMapRef.current.get(id)?.remove()
        const pl: IPriceLine = candleSeriesRef.current.createPriceLine({
          price, color, lineWidth: 1,
          lineStyle: LineStyle.Dashed,
          axisLabelVisible: true, title,
        })
        drawingMapRef.current.set(id, {
          remove: () => { try { candleSeriesRef.current?.removePriceLine(pl) } catch {} }
        })
      },

      addTrendLine: (id, p1, p2, color) => {
        if (!chartRef.current) return
        drawingMapRef.current.get(id)?.remove()
        const s = chartRef.current.addLineSeries({
          color, lineWidth: 1, lineStyle: LineStyle.Solid,
          priceLineVisible: false, lastValueVisible: false,
          crosshairMarkerVisible: false,
        })
        const [first, second] = p1.time <= p2.time ? [p1, p2] : [p2, p1]
        s.setData([
          { time: first.time as Time, value: first.price },
          { time: second.time as Time, value: second.price },
        ])
        drawingMapRef.current.set(id, {
          remove: () => { try { chartRef.current?.removeSeries(s) } catch {} }
        })
      },

      removeDrawingById: (id) => {
        drawingMapRef.current.get(id)?.remove()
        drawingMapRef.current.delete(id)
      },

      clearDrawings: () => {
        drawingMapRef.current.forEach(entry => entry.remove())
        drawingMapRef.current.clear()
      },

      subscribeToViewport: (cb) => {
        if (!chartRef.current) return () => {}
        const handler = () => cb()
        chartRef.current.timeScale().subscribeVisibleLogicalRangeChange(handler)
        return () => {
          try { chartRef.current?.timeScale().unsubscribeVisibleLogicalRangeChange(handler) } catch {}
        }
      },
    }))

    // Create chart once
    useEffect(() => {
      if (!containerRef.current) return
      const el = containerRef.current
      const chart = createChart(el, {
        layout: { background: { type: ColorType.Solid, color: '#0f172a' }, textColor: '#94a3b8' },
        grid: { vertLines: { color: '#1e293b' }, horzLines: { color: '#1e293b' } },
        crosshair: { mode: 1 },
        rightPriceScale: { borderColor: '#334155' },
        timeScale: { borderColor: '#334155', timeVisible: true, secondsVisible: false },
        width: el.clientWidth || 600,
        height: height ?? (el.clientHeight || 480),
      })
      chartRef.current = chart

      const candleSeries = chart.addCandlestickSeries({
        upColor: '#22c55e', downColor: '#ef4444',
        borderUpColor: '#22c55e', borderDownColor: '#ef4444',
        wickUpColor: '#22c55e', wickDownColor: '#ef4444',
      })
      candleSeriesRef.current = candleSeries

      const volumeSeries = chart.addHistogramSeries({
        color: '#26a69a', priceFormat: { type: 'volume' }, priceScaleId: 'volume',
      })
      chart.priceScale('volume').applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } })
      volumeSeriesRef.current = volumeSeries

      const ro = new ResizeObserver(() => {
        if (!containerRef.current || !chartRef.current) return
        const { clientWidth, clientHeight } = containerRef.current
        chartRef.current.applyOptions({ width: clientWidth, height: height ?? clientHeight })
      })
      ro.observe(el)

      return () => {
        ro.disconnect()
        drawingMapRef.current.forEach(e => e.remove())
        drawingMapRef.current.clear()
        chart.remove()
        chartRef.current = null
        candleSeriesRef.current = null
        volumeSeriesRef.current = null
      }
    }, [height])

    // Update candles
    useEffect(() => {
      if (!candleSeriesRef.current || !volumeSeriesRef.current || candles.length === 0) return
      candleSeriesRef.current.setData(
        candles.map(c => ({
          time: Math.floor(c.timestamp / 1000) as Time,
          open: c.open, high: c.high, low: c.low, close: c.close,
        })) as CandlestickData[]
      )
      volumeSeriesRef.current.setData(
        candles.map(c => ({
          time: Math.floor(c.timestamp / 1000) as Time,
          value: c.volume,
          color: c.close >= c.open ? '#22c55e44' : '#ef444444',
        })) as HistogramData[]
      )
      chartRef.current?.timeScale().fitContent()
    }, [candles])

    // Draw pattern overlays and signal levels
    useEffect(() => {
      if (!chartRef.current || candles.length === 0) return

      patternLinesRef.current.forEach(s => { try { chartRef.current?.removeSeries(s) } catch {} })
      patternLinesRef.current = []
      levelLinesRef.current.forEach(s => { try { chartRef.current?.removeSeries(s) } catch {} })
      levelLinesRef.current = []

      if (!signal) return

      const timeUnit = candles.length > 1 ? candles[1].timestamp - candles[0].timestamp : 3600000

      signal.patterns.forEach(pattern => {
        if (!pattern.lines?.length) return
        const color = PATTERN_COLORS[pattern.type] || '#a78bfa'
        pattern.lines.forEach(lineData => {
          if (lineData.length === 4) {
            const [x0, y0, x1, y1] = lineData
            const t0 = Math.floor((candles[0].timestamp + x0 * timeUnit) / 1000) as Time
            const t1 = Math.floor((candles[0].timestamp + x1 * timeUnit) / 1000) as Time
            const s = chartRef.current!.addLineSeries({
              color, lineWidth: 1, lineStyle: LineStyle.Dashed,
              priceLineVisible: false, lastValueVisible: false,
            })
            s.setData([{ time: t0, value: y0 }, { time: t1, value: y1 }])
            patternLinesRef.current.push(s)
          }
        })
        if (pattern.points.length > 0 && candleSeriesRef.current) {
          try {
            candleSeriesRef.current.setMarkers(
              pattern.points.map(pt => ({
                time: Math.floor(pt.timestamp / 1000) as Time,
                position: (pattern.direction === 'long' ? 'belowBar' : 'aboveBar') as 'belowBar' | 'aboveBar',
                color, shape: 'circle' as const, size: 1,
              }))
            )
          } catch {}
        }
      })

      if (!candleSeriesRef.current) return
      const lastTime = Math.floor(candles[candles.length - 1].timestamp / 1000) as Time
      const startTime = Math.floor(candles[Math.max(0, candles.length - 20)].timestamp / 1000) as Time
      const levels = [
        { price: signal.entry, color: '#f59e0b' },
        { price: signal.stop_loss, color: '#ef4444' },
        { price: signal.tp1, color: '#22c55e' },
        { price: signal.tp2, color: '#22c55e' },
        { price: signal.tp3, color: '#22c55e' },
      ]
      levels.forEach(({ price, color }) => {
        const s = chartRef.current!.addLineSeries({
          color, lineWidth: 1, lineStyle: LineStyle.Dotted,
          priceLineVisible: false, lastValueVisible: true, lastPriceAnimation: 0,
        })
        s.setData([{ time: startTime, value: price }, { time: lastTime, value: price }])
        levelLinesRef.current.push(s)
      })
    }, [signal, candles])

    return (
      <div
        ref={containerRef}
        className="w-full rounded-lg overflow-hidden"
        style={{ height: height ?? '100%' }}
      />
    )
  }
)
