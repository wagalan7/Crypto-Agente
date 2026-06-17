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
    const ema12Ref = useRef<ISeriesApi<'Line'> | null>(null)
    const ema26Ref = useRef<ISeriesApi<'Line'> | null>(null)
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

      const ema12Series = chart.addLineSeries({
        color: '#3b82f6', lineWidth: 1, lineStyle: LineStyle.Solid,
        priceLineVisible: false, lastValueVisible: false,
        crosshairMarkerVisible: false, title: 'EMA12',
      })
      ema12Ref.current = ema12Series

      const ema26Series = chart.addLineSeries({
        color: '#f97316', lineWidth: 1, lineStyle: LineStyle.Solid,
        priceLineVisible: false, lastValueVisible: false,
        crosshairMarkerVisible: false, title: 'EMA26',
      })
      ema26Ref.current = ema26Series

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
        ema12Ref.current = null
        ema26Ref.current = null
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

      // EMA calculation
      function calcEMA(values: number[], period: number): number[] {
        const k = 2 / (period + 1)
        const out = [values[0]]
        for (let i = 1; i < values.length; i++) out.push(values[i] * k + out[i-1] * (1-k))
        return out
      }
      const closes = candles.map(c => c.close)
      const times = candles.map(c => Math.floor(c.timestamp / 1000) as Time)
      const ema12Data = calcEMA(closes, 12)
      const ema26Data = calcEMA(closes, 26)
      ema12Ref.current?.setData(times.map((t, i) => ({ time: t, value: ema12Data[i] })))
      ema26Ref.current?.setData(times.map((t, i) => ({ time: t, value: ema26Data[i] })))
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
      const firstTs = candles[0].timestamp
      const lastTs = candles[candles.length - 1].timestamp
      const lastClose = candles[candles.length - 1].close

      // ── Zonas SMC (Order Blocks / FVG) — bandas horizontais ───────────────
      // Estes são os "padrões" nomeados no card (ex.: Order Block / FVG). Antes
      // não eram desenhados; o chart mostrava só as diagonais (LTB/canal/cunha),
      // que pareciam "desconfiguradas" pra quem esperava uma zona horizontal.
      // Desenha topo+fundo como linhas horizontais, só as zonas ativas perto do
      // preço (≤12%) e no máx. 3, pra não poluir.
      const drawZone = (top: number, bottom: number, tsStart: number, color: string) => {
        const tStart = Math.floor(Math.max(firstTs, tsStart) / 1000) as Time
        const tEnd = Math.floor(lastTs / 1000) as Time
        for (const price of [top, bottom]) {
          const s = chartRef.current!.addLineSeries({
            color, lineWidth: 1, lineStyle: LineStyle.Solid,
            priceLineVisible: false, lastValueVisible: false,
          })
          s.setData([{ time: tStart, value: price }, { time: tEnd, value: price }])
          patternLinesRef.current.push(s)
        }
      }
      const smcZones = [
        ...(signal.smc?.order_blocks ?? []),
        ...(signal.smc?.fvgs ?? []),
      ]
        .filter(z =>
          z.active && lastClose > 0 &&
          Math.abs((z.top + z.bottom) / 2 - lastClose) / lastClose <= 0.12
        )
        .sort((a, b) =>
          Math.abs((a.top + a.bottom) / 2 - lastClose) -
          Math.abs((b.top + b.bottom) / 2 - lastClose)
        )
        .slice(0, 3)
      smcZones.forEach(z => {
        const color = z.direction === 'bullish' ? 'rgba(34,197,94,0.85)' : 'rgba(239,68,68,0.85)'
        drawZone(z.top, z.bottom, z.timestamp, color)
      })

      // ── Padrões geométricos (LTA/LTB/canais/cunhas/triângulos) ────────────
      // Só os alinhados à direção do sinal (ou neutros) e com dedupe de linhas
      // idênticas — o backend pode emitir canal e cunha com a MESMA geometria,
      // o que dobrava as diagonais na tela.
      const seenLines = new Set<string>()
      const markers: {
        time: Time
        position: 'belowBar' | 'aboveBar'
        color: string
        shape: 'circle'
        size: number
      }[] = []
      signal.patterns
        .filter(p => p.direction === signal.direction || p.direction === 'neutral')
        .forEach(pattern => {
          const color = PATTERN_COLORS[pattern.type] || '#a78bfa'
          if (pattern.lines?.length) {
            pattern.lines.forEach(lineData => {
              if (lineData.length !== 4) return
              const key = lineData.map(v => Math.round(v * 100) / 100).join(',')
              if (seenLines.has(key)) return
              seenLines.add(key)
              const [x0, y0, x1, y1] = lineData
              const p0 = { t: Math.floor((firstTs + x0 * timeUnit) / 1000), v: y0 }
              const p1 = { t: Math.floor((firstTs + x1 * timeUnit) / 1000), v: y1 }
              const [a, b] = p0.t <= p1.t ? [p0, p1] : [p1, p0]
              const s = chartRef.current!.addLineSeries({
                color, lineWidth: 1, lineStyle: LineStyle.Dashed,
                priceLineVisible: false, lastValueVisible: false,
              })
              s.setData([{ time: a.t as Time, value: a.v }, { time: b.t as Time, value: b.v }])
              patternLinesRef.current.push(s)
            })
          }
          pattern.points.forEach(pt => {
            markers.push({
              time: Math.floor(pt.timestamp / 1000) as Time,
              position: pattern.direction === 'long' ? 'belowBar' : 'aboveBar',
              color, shape: 'circle', size: 1,
            })
          })
        })
      if (candleSeriesRef.current) {
        try {
          markers.sort((a, b) => (a.time as number) - (b.time as number))
          candleSeriesRef.current.setMarkers(markers)
        } catch {}
      }

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
