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
import type { OHLCVCandle, TradeSignal, UserDrawing } from '../../types'

interface Props {
  candles: OHLCVCandle[]
  signal: TradeSignal | null
  height?: number
}

export interface CandleChartHandle {
  /** Convert chart-relative y pixel to price */
  yToPrice: (y: number) => number | null
  /** Convert chart-relative x pixel to unix timestamp (seconds) */
  xToTime: (x: number) => number | null
  /** Convert price to chart-relative y pixel */
  priceToY: (price: number) => number | null
  /** Convert unix timestamp (seconds) to chart-relative x pixel */
  timeToX: (time: number) => number | null
  /** Add a horizontal price line; returns cleanup fn */
  addHLine: (price: number, color: string, title: string) => void
  /** Add a trend line between two price/time points; returns cleanup fn */
  addTrendLine: (
    p1: { time: number; price: number },
    p2: { time: number; price: number },
    color: string
  ) => void
  /** Remove all user-drawn elements */
  clearDrawings: () => void
}

// Suppress unused import warning — UserDrawing is referenced in the Props type chain
type _UserDrawing = UserDrawing

const PATTERN_COLORS: Record<string, string> = {
  lta: '#22c55e',
  ltb: '#ef4444',
  ascending_channel: '#22c55e',
  descending_channel: '#ef4444',
  horizontal_channel: '#a78bfa',
  symmetric_triangle: '#f59e0b',
  ascending_triangle: '#22c55e',
  descending_triangle: '#ef4444',
  ascending_wedge: '#ef4444',
  descending_wedge: '#22c55e',
  head_and_shoulders: '#f97316',
  inverse_head_and_shoulders: '#22c55e',
  double_top: '#ef4444',
  double_bottom: '#22c55e',
  bull_flag: '#22c55e',
  bear_flag: '#ef4444',
}

export const CandleChart = forwardRef<CandleChartHandle, Props>(
  function CandleChart({ candles, signal, height }, ref) {
    const containerRef = useRef<HTMLDivElement>(null)
    const chartRef = useRef<IChartApi | null>(null)
    const candleSeriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
    const volumeSeriesRef = useRef<ISeriesApi<'Histogram'> | null>(null)
    const patternLinesRef = useRef<ISeriesApi<'Line'>[]>([])
    const levelLinesRef = useRef<ISeriesApi<'Line'>[]>([])
    // User-drawn elements
    const userPriceLinesRef = useRef<IPriceLine[]>([])
    const userTrendSeriesRef = useRef<ISeriesApi<'Line'>[]>([])

    // Expose handle to parent via forwardRef
    useImperativeHandle(ref, () => ({
      yToPrice: (y: number) => {
        if (!candleSeriesRef.current) return null
        const p = candleSeriesRef.current.coordinateToPrice(y)
        return p ?? null
      },
      xToTime: (x: number) => {
        if (!chartRef.current) return null
        const t = chartRef.current.timeScale().coordinateToTime(x)
        return t != null ? (t as number) : null
      },
      priceToY: (price: number) => {
        if (!candleSeriesRef.current) return null
        const y = candleSeriesRef.current.priceToCoordinate(price)
        return y ?? null
      },
      timeToX: (time: number) => {
        if (!chartRef.current) return null
        const x = chartRef.current.timeScale().timeToCoordinate(time as Time)
        return x ?? null
      },
      addHLine: (price: number, color: string, title: string) => {
        if (!candleSeriesRef.current) return
        const pl = candleSeriesRef.current.createPriceLine({
          price,
          color,
          lineWidth: 1,
          lineStyle: LineStyle.Dashed,
          axisLabelVisible: true,
          title,
        })
        userPriceLinesRef.current.push(pl)
      },
      addTrendLine: (
        p1: { time: number; price: number },
        p2: { time: number; price: number },
        color: string
      ) => {
        if (!chartRef.current) return
        const s = chartRef.current.addLineSeries({
          color,
          lineWidth: 1,
          lineStyle: LineStyle.Solid,
          priceLineVisible: false,
          lastValueVisible: false,
          crosshairMarkerVisible: false,
        })
        // Ensure times are in order
        const [first, second] = p1.time <= p2.time ? [p1, p2] : [p2, p1]
        s.setData([
          { time: first.time as Time, value: first.price },
          { time: second.time as Time, value: second.price },
        ])
        userTrendSeriesRef.current.push(s)
      },
      clearDrawings: () => {
        userPriceLinesRef.current.forEach(pl => {
          try { candleSeriesRef.current?.removePriceLine(pl) } catch {}
        })
        userPriceLinesRef.current = []
        userTrendSeriesRef.current.forEach(s => {
          try { chartRef.current?.removeSeries(s) } catch {}
        })
        userTrendSeriesRef.current = []
      },
    }))

    useEffect(() => {
      if (!containerRef.current) return
      const el = containerRef.current
      const w = el.clientWidth || 600
      const h = height ?? (el.clientHeight || 480)

      const chart = createChart(el, {
        layout: {
          background: { type: ColorType.Solid, color: '#0f172a' },
          textColor: '#94a3b8',
        },
        grid: {
          vertLines: { color: '#1e293b' },
          horzLines: { color: '#1e293b' },
        },
        crosshair: { mode: 1 },
        rightPriceScale: { borderColor: '#334155' },
        timeScale: { borderColor: '#334155', timeVisible: true, secondsVisible: false },
        width: w,
        height: h,
      })
      chartRef.current = chart

      const candleSeries = chart.addCandlestickSeries({
        upColor: '#22c55e',
        downColor: '#ef4444',
        borderUpColor: '#22c55e',
        borderDownColor: '#ef4444',
        wickUpColor: '#22c55e',
        wickDownColor: '#ef4444',
      })
      candleSeriesRef.current = candleSeries

      const volumeSeries = chart.addHistogramSeries({
        color: '#26a69a',
        priceFormat: { type: 'volume' },
        priceScaleId: 'volume',
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
        chart.remove()
        chartRef.current = null
        candleSeriesRef.current = null
        volumeSeriesRef.current = null
        userPriceLinesRef.current = []
        userTrendSeriesRef.current = []
      }
    }, [height])

    useEffect(() => {
      if (!candleSeriesRef.current || !volumeSeriesRef.current || candles.length === 0) return
      const candleData: CandlestickData[] = candles.map(c => ({
        time: Math.floor(c.timestamp / 1000) as Time,
        open: c.open, high: c.high, low: c.low, close: c.close,
      }))
      const volumeData: HistogramData[] = candles.map(c => ({
        time: Math.floor(c.timestamp / 1000) as Time,
        value: c.volume,
        color: c.close >= c.open ? '#22c55e44' : '#ef444444',
      }))
      candleSeriesRef.current.setData(candleData)
      volumeSeriesRef.current.setData(volumeData)
      chartRef.current?.timeScale().fitContent()
    }, [candles])

    useEffect(() => {
      if (!chartRef.current || candles.length === 0) return

      patternLinesRef.current.forEach(s => { try { chartRef.current?.removeSeries(s) } catch {} })
      patternLinesRef.current = []
      levelLinesRef.current.forEach(s => { try { chartRef.current?.removeSeries(s) } catch {} })
      levelLinesRef.current = []

      if (!signal) return

      const timeUnit = candles.length > 1
        ? (candles[1].timestamp - candles[0].timestamp)
        : 3600000

      signal.patterns.forEach(pattern => {
        if (!pattern.lines || pattern.lines.length === 0) return
        const color = PATTERN_COLORS[pattern.type] || '#a78bfa'
        pattern.lines.forEach(lineData => {
          if (lineData.length === 4) {
            const [x0, y0, x1, y1] = lineData
            const t0 = Math.floor((candles[0].timestamp + x0 * timeUnit) / 1000) as Time
            const t1 = Math.floor((candles[0].timestamp + x1 * timeUnit) / 1000) as Time
            const lineSeries = chartRef.current!.addLineSeries({
              color, lineWidth: 1, lineStyle: LineStyle.Dashed,
              priceLineVisible: false, lastValueVisible: false,
            })
            lineSeries.setData([{ time: t0, value: y0 }, { time: t1, value: y1 }])
            patternLinesRef.current.push(lineSeries)
          }
        })
        if (pattern.points.length > 0 && candleSeriesRef.current) {
          const markers = pattern.points.map(pt => ({
            time: Math.floor(pt.timestamp / 1000) as Time,
            position: (pattern.direction === 'long' ? 'belowBar' : 'aboveBar') as 'belowBar' | 'aboveBar',
            color, shape: 'circle' as const, size: 1,
          }))
          try { candleSeriesRef.current.setMarkers(markers) } catch {}
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
