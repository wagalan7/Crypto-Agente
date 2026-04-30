import { useEffect, useRef } from 'react'
import {
  createChart,
  IChartApi,
  ISeriesApi,
  CandlestickData,
  HistogramData,
  LineStyle,
  ColorType,
  Time,
} from 'lightweight-charts'
import type { OHLCVCandle, DetectedPattern, TradeSignal } from '../../types'

interface Props {
  candles: OHLCVCandle[]
  signal: TradeSignal | null
  height?: number
}

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

export function CandleChart({ candles, signal, height = 480 }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const candleSeriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const volumeSeriesRef = useRef<ISeriesApi<'Histogram'> | null>(null)
  const patternLinesRef = useRef<ISeriesApi<'Line'>[]>([])
  const levelLinesRef = useRef<ISeriesApi<'Line'>[]>([])

  useEffect(() => {
    if (!containerRef.current) return

    const chart = createChart(containerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: '#0f172a' },
        textColor: '#94a3b8',
      },
      grid: {
        vertLines: { color: '#1e293b' },
        horzLines: { color: '#1e293b' },
      },
      crosshair: {
        mode: 1,
      },
      rightPriceScale: {
        borderColor: '#334155',
      },
      timeScale: {
        borderColor: '#334155',
        timeVisible: true,
        secondsVisible: false,
      },
      width: containerRef.current.clientWidth,
      height,
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
    chart.priceScale('volume').applyOptions({
      scaleMargins: { top: 0.8, bottom: 0 },
    })
    volumeSeriesRef.current = volumeSeries

    const handleResize = () => {
      if (containerRef.current) {
        chart.applyOptions({ width: containerRef.current.clientWidth })
      }
    }
    window.addEventListener('resize', handleResize)

    return () => {
      window.removeEventListener('resize', handleResize)
      chart.remove()
      chartRef.current = null
    }
  }, [height])

  // Update candle data
  useEffect(() => {
    if (!candleSeriesRef.current || !volumeSeriesRef.current || candles.length === 0) return

    const candleData: CandlestickData[] = candles.map(c => ({
      time: Math.floor(c.timestamp / 1000) as Time,
      open: c.open,
      high: c.high,
      low: c.low,
      close: c.close,
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

  // Draw pattern lines and signal levels
  useEffect(() => {
    if (!chartRef.current || candles.length === 0) return

    // Remove old pattern lines
    patternLinesRef.current.forEach(s => {
      try { chartRef.current?.removeSeries(s) } catch {}
    })
    patternLinesRef.current = []
    levelLinesRef.current.forEach(s => {
      try { chartRef.current?.removeSeries(s) } catch {}
    })
    levelLinesRef.current = []

    if (!signal) return

    const timeUnit = candles.length > 0
      ? (candles[1]?.timestamp - candles[0]?.timestamp) || 3600000
      : 3600000

    // Draw pattern trendlines
    signal.patterns.forEach(pattern => {
      if (!pattern.lines || pattern.lines.length === 0) return
      const color = PATTERN_COLORS[pattern.type] || '#a78bfa'

      // Each element in lines is [x0, y0, x1, y1] or a projection point
      pattern.lines.forEach(lineData => {
        if (lineData.length === 4) {
          const [x0, y0, x1, y1] = lineData
          const t0 = Math.floor((candles[0].timestamp + x0 * timeUnit) / 1000) as Time
          const t1 = Math.floor((candles[0].timestamp + x1 * timeUnit) / 1000) as Time

          const lineSeries = chartRef.current!.addLineSeries({
            color,
            lineWidth: 1,
            lineStyle: LineStyle.Dashed,
            priceLineVisible: false,
            lastValueVisible: false,
          })
          lineSeries.setData([
            { time: t0, value: y0 },
            { time: t1, value: y1 },
          ])
          patternLinesRef.current.push(lineSeries)
        }
      })

      // Draw pattern points as markers
      if (pattern.points.length > 0 && candleSeriesRef.current) {
        const markers = pattern.points.map(pt => ({
          time: Math.floor(pt.timestamp / 1000) as Time,
          position: (pattern.direction === 'long' ? 'belowBar' : 'aboveBar') as 'belowBar' | 'aboveBar',
          color,
          shape: 'circle' as const,
          size: 1,
        }))
        try {
          candleSeriesRef.current.setMarkers(markers)
        } catch {}
      }
    })

    // Draw signal levels
    if (!candleSeriesRef.current) return

    const lastTime = Math.floor(candles[candles.length - 1].timestamp / 1000) as Time
    const startTime = Math.floor(candles[Math.max(0, candles.length - 20)].timestamp / 1000) as Time

    const levels = [
      { price: signal.entry, color: '#f59e0b', label: 'Entry' },
      { price: signal.stop_loss, color: '#ef4444', label: 'SL' },
      { price: signal.tp1, color: '#22c55e', label: 'TP1' },
      { price: signal.tp2, color: '#22c55e', label: 'TP2' },
      { price: signal.tp3, color: '#22c55e', label: 'TP3' },
    ]

    levels.forEach(({ price, color }) => {
      const s = chartRef.current!.addLineSeries({
        color,
        lineWidth: 1,
        lineStyle: LineStyle.Dotted,
        priceLineVisible: false,
        lastValueVisible: true,
        lastPriceAnimation: 0,
      })
      s.setData([
        { time: startTime, value: price },
        { time: lastTime, value: price },
      ])
      levelLinesRef.current.push(s)
    })
  }, [signal, candles])

  return (
    <div
      ref={containerRef}
      className="w-full rounded-lg overflow-hidden"
      style={{ height }}
    />
  )
}
