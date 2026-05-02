import type { TradeSignal, SignalDirection, TradeType } from '../../types'
import { TrendingUp, TrendingDown, Minus, Target, ShieldAlert, Brain, Activity, FileText } from 'lucide-react'

interface Props {
  signal: TradeSignal
  livePrice?: number
}

const DIRECTION_CONFIG: Record<SignalDirection, { icon: typeof TrendingUp; color: string; bg: string; label: string }> = {
  long:    { icon: TrendingUp,   color: 'text-green-400',  bg: 'bg-green-400/10 border-green-400/30',  label: 'COMPRA (Long)'  },
  short:   { icon: TrendingDown, color: 'text-red-400',    bg: 'bg-red-400/10 border-red-400/30',      label: 'VENDA (Short)'  },
  neutral: { icon: Minus,        color: 'text-yellow-400', bg: 'bg-yellow-400/10 border-yellow-400/30', label: 'NEUTRO'         },
}

const TRADE_TYPE_CONFIG: Record<TradeType, { label: string; color: string }> = {
  scalp:     { label: 'Scalp',       color: 'bg-purple-500/20 text-purple-300 border-purple-500/30'  },
  day_trade: { label: 'Day Trade',   color: 'bg-blue-500/20 text-blue-300 border-blue-500/30'        },
  swing:     { label: 'Swing Trade', color: 'bg-orange-500/20 text-orange-300 border-orange-500/30'  },
  hodl:      { label: 'HODL',        color: 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30'},
}

function fmt(n: number) {
  if (n >= 1000) return n.toFixed(2)
  if (n >= 1) return n.toFixed(4)
  return n.toFixed(8)
}

function pct(a: number, b: number) {
  return ((Math.abs(a - b) / b) * 100).toFixed(2)
}

function generateAnalysis(signal: TradeSignal): string {
  const { indicators: ind, direction, confidence, patterns } = signal
  const lines: string[] = []
  const conf = Math.round(confidence * 100)
  const dirLabel = direction === 'long' ? 'COMPRA (Alta)' : direction === 'short' ? 'VENDA (Baixa)' : 'NEUTRO'

  lines.push(`Direção: ${dirLabel} com ${conf}% de confiança.\n`)

  // RSI
  if (ind.rsi != null) {
    const r = ind.rsi
    if (r < 30) lines.push(`RSI ${r.toFixed(1)}: Sobrevendido — pressão compradora crescente, possível reversão de alta.`)
    else if (r > 70) lines.push(`RSI ${r.toFixed(1)}: Sobrecomprado — ativo esticado, risco de correção.`)
    else if (r < 45) lines.push(`RSI ${r.toFixed(1)}: Levemente baixista — momentum vendedor predomina.`)
    else if (r > 55) lines.push(`RSI ${r.toFixed(1)}: Levemente altista — momentum comprador predomina.`)
    else lines.push(`RSI ${r.toFixed(1)}: Zona neutra — sem pressão direcional clara.`)
  }

  // MACD
  if (ind.macd != null && ind.macd_hist != null) {
    const h = ind.macd_hist
    if (h > 0) lines.push(`MACD: Histograma positivo e ${h > (ind.macd ?? 0) * 0.1 ? 'crescente' : 'estável'} — momentum de alta ativo.`)
    else lines.push(`MACD: Histograma negativo — momentum vendedor no controle.`)
  }

  // Stochastic
  if (ind.stoch_k != null) {
    const k = ind.stoch_k
    if (k < 20) lines.push(`Stoch RSI ${k.toFixed(0)}: Zona de sobrevenda — probabilidade de bounce.`)
    else if (k > 80) lines.push(`Stoch RSI ${k.toFixed(0)}: Zona de sobrecompra — cuidado com reversão.`)
    else lines.push(`Stoch RSI ${k.toFixed(0)}: Zona neutra.`)
  }

  // ADX
  if (ind.adx != null) {
    const a = ind.adx
    if (a > 40) lines.push(`ADX ${a.toFixed(1)}: Tendência muito forte — seguir a direção.`)
    else if (a > 25) lines.push(`ADX ${a.toFixed(1)}: Tendência presente — sinal mais confiável.`)
    else lines.push(`ADX ${a.toFixed(1)}: Tendência fraca — mercado lateral, maior risco de falso sinal.`)
  }

  // EMAs
  if (ind.ema9 != null && ind.ema21 != null && ind.ema50 != null) {
    const [e9, e21, e50] = [ind.ema9, ind.ema21, ind.ema50]
    if (e9 > e21 && e21 > e50) lines.push(`EMAs alinhadas em alta (12 > 26 > 50) — tendência bullish confirmada.`)
    else if (e9 < e21 && e21 < e50) lines.push(`EMAs alinhadas em baixa (12 < 26 < 50) — tendência bearish confirmada.`)
    else lines.push(`EMAs sem alinhamento claro — mercado em transição ou lateral.`)
  }

  // Supertrend
  if (ind.supertrend_direction != null) {
    lines.push(`Supertrend: ${ind.supertrend_direction === 1 ? 'Sinal de ALTA ativo — suporte dinâmico abaixo do preço.' : 'Sinal de BAIXA ativo — resistência dinâmica acima do preço.'}`)
  }

  // Bollinger Bands
  if (ind.bb_upper != null && ind.bb_lower != null && ind.bb_middle != null) {
    const bw = (ind.bb_upper - ind.bb_lower) / ind.bb_middle
    if (bw < 0.015) lines.push(`Bollinger Bands: Compressão extrema — breakout explosivo iminente.`)
    else if (signal.entry > ind.bb_upper * 0.99) lines.push(`Bollinger Bands: Preço na banda superior — momentum forte de alta.`)
    else if (signal.entry < ind.bb_lower * 1.01) lines.push(`Bollinger Bands: Preço na banda inferior — possível suporte ou continuação de queda.`)
  }

  // Patterns
  if (patterns.length > 0) {
    lines.push(`\nPadrões gráficos detectados:`)
    patterns.slice(0, 3).forEach(p => {
      const dLabel = p.direction === 'long' ? 'alta' : p.direction === 'short' ? 'baixa' : 'neutro'
      lines.push(`• ${p.description} (conf. ${(p.confidence * 100).toFixed(0)}%) — viés de ${dLabel}.`)
    })
  }

  // Trade type suitability
  const adx = ind.adx ?? 0
  const rsi = ind.rsi ?? 50
  const hasPatterns = patterns.length > 0
  const highConf = confidence >= 0.65
  const midConf = confidence >= 0.50

  const scalpOk = adx > 25 && highConf
  const dayOk = midConf && (adx > 18 || Math.abs(rsi - 50) > 10)
  const swingOk = hasPatterns || (adx > 15 && midConf)

  const scalpLabel = scalpOk ? '✅ Bom' : adx > 15 ? '⚠️ Moderado' : '❌ Fraco'
  const dayLabel   = dayOk   ? '✅ Bom' : midConf ? '⚠️ Moderado' : '❌ Fraco'
  const swingLabel = swingOk ? '✅ Bom' : '⚠️ Moderado'

  lines.push(`\nAdequação por tipo de operação:`)
  lines.push(`⚡ Scalp: ${scalpLabel}${!scalpOk && adx <= 25 ? ' (ADX baixo)' : ''}`)
  lines.push(`📅 Day Trade: ${dayLabel}`)
  lines.push(`📈 Swing Trade: ${swingLabel}${hasPatterns ? ' (padrões detectados)' : ''}`)

  const best = scalpOk ? 'Scalp' : dayOk ? 'Day Trade' : 'Swing Trade'
  lines.push(`\nMelhor para: ${best}`)

  return lines.join('\n')
}

function ConfidenceBar({ value }: { value: number }) {
  const pctVal = Math.round(value * 100)
  const color = value >= 0.75 ? 'bg-green-400' : value >= 0.55 ? 'bg-yellow-400' : 'bg-red-400'
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 h-2 bg-slate-700 rounded-full overflow-hidden">
        <div className={`h-full rounded-full transition-all ${color}`} style={{ width: `${pctVal}%` }} />
      </div>
      <span className="text-xs text-slate-300 w-10 text-right">{pctVal}%</span>
    </div>
  )
}

function LevelRow({ label, price, entry, color }: { label: string; price: number; entry: number; color: string }) {
  return (
    <div className="flex items-center justify-between py-1.5 border-b border-slate-800">
      <span className={`text-xs font-semibold ${color}`}>{label}</span>
      <div className="text-right">
        <div className="text-sm font-mono text-white">{fmt(price)}</div>
        <div className="text-xs text-slate-500">{pct(price, entry)}%</div>
      </div>
    </div>
  )
}

export function SignalPanel({ signal, livePrice }: Props) {
  const dir = DIRECTION_CONFIG[signal.direction]
  const DirIcon = dir.icon
  const tt = TRADE_TYPE_CONFIG[signal.trade_type]
  const currentPrice = livePrice ?? signal.entry

  return (
    <div className="flex flex-col gap-3 h-full overflow-y-auto pr-1">
      {/* Direction + trade type */}
      <div className={`flex items-center justify-between p-3 rounded-lg border ${dir.bg}`}>
        <div className="flex items-center gap-2">
          <DirIcon className={`w-6 h-6 ${dir.color}`} />
          <span className={`text-lg font-bold ${dir.color}`}>{dir.label}</span>
        </div>
        <span className={`text-xs font-semibold px-2 py-1 rounded border ${tt.color}`}>{tt.label}</span>
      </div>

      {/* Low confidence warning */}
      {signal.confidence < 0.80 && (
        <div className="bg-yellow-500/10 border border-yellow-500/30 rounded-lg p-3 flex items-center gap-2">
          <span className="text-yellow-400 text-lg">⚠️</span>
          <div>
            <p className="text-xs font-bold text-yellow-400">AGUARDAR CONFLUÊNCIA</p>
            <p className="text-xs text-yellow-300/70">Probabilidade {(signal.confidence * 100).toFixed(0)}% abaixo do mínimo de 80% para operar</p>
          </div>
        </div>
      )}

      {/* Confidence */}
      <div className="bg-slate-800/60 rounded-lg p-3">
        <div className="flex justify-between mb-1">
          <span className="text-xs text-slate-400">Força do Sinal</span>
          <span className="text-xs font-semibold text-white">{signal.signal_strength}</span>
        </div>
        <ConfidenceBar value={signal.confidence} />
      </div>

      {/* Detailed analysis */}
      <div className="bg-slate-800/60 rounded-lg p-3">
        <div className="flex items-center gap-1 mb-2">
          <FileText className="w-4 h-4 text-blue-400" />
          <span className="text-xs font-semibold text-blue-400">ANÁLISE DETALHADA</span>
        </div>
        <p className="text-xs text-slate-300 leading-relaxed whitespace-pre-line">
          {generateAnalysis(signal)}
        </p>
      </div>

      {/* Live price */}
      {livePrice && (
        <div className="bg-slate-800/60 rounded-lg p-3 flex justify-between items-center">
          <span className="text-xs text-slate-400">Preço Atual</span>
          <span className="text-base font-mono font-bold text-white">{fmt(livePrice)}</span>
        </div>
      )}

      {/* Levels */}
      <div className="bg-slate-800/60 rounded-lg p-3">
        <div className="flex items-center gap-1 mb-2">
          <Target className="w-4 h-4 text-slate-400" />
          <span className="text-xs font-semibold text-slate-400">NÍVEIS DE OPERAÇÃO</span>
        </div>
        <LevelRow label="ENTRADA"   price={signal.entry}      entry={signal.entry} color="text-yellow-400" />
        <LevelRow label="STOP LOSS" price={signal.stop_loss}  entry={signal.entry} color="text-red-400"    />
        <LevelRow label="ALVO 1"    price={signal.tp1}        entry={signal.entry} color="text-green-400"  />
        <LevelRow label="ALVO 2"    price={signal.tp2}        entry={signal.entry} color="text-green-500"  />
        <LevelRow label="ALVO 3"    price={signal.tp3}        entry={signal.entry} color="text-emerald-400"/>
        <div className="flex justify-between pt-2">
          <span className="text-xs text-slate-400">Risco/Retorno</span>
          <span className="text-sm font-bold text-white">1 : {signal.risk_reward}</span>
        </div>
      </div>

      {/* Indicators */}
      <div className="bg-slate-800/60 rounded-lg p-3">
        <div className="flex items-center gap-1 mb-2">
          <Activity className="w-4 h-4 text-slate-400" />
          <span className="text-xs font-semibold text-slate-400">INDICADORES</span>
        </div>
        <div className="grid grid-cols-2 gap-x-4 gap-y-1">
          {signal.indicators.rsi != null && (
            <>
              <span className="text-xs text-slate-500">RSI(14)</span>
              <span className={`text-xs font-mono font-semibold ${signal.indicators.rsi < 30 ? 'text-green-400' : signal.indicators.rsi > 70 ? 'text-red-400' : 'text-slate-300'}`}>
                {signal.indicators.rsi.toFixed(1)}
              </span>
            </>
          )}
          {signal.indicators.adx != null && (
            <>
              <span className="text-xs text-slate-500">ADX</span>
              <span className={`text-xs font-mono font-semibold ${signal.indicators.adx > 25 ? 'text-yellow-400' : 'text-slate-300'}`}>
                {signal.indicators.adx.toFixed(1)}
              </span>
            </>
          )}
          {signal.indicators.macd != null && (
            <>
              <span className="text-xs text-slate-500">MACD</span>
              <span className={`text-xs font-mono font-semibold ${(signal.indicators.macd_hist ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                {signal.indicators.macd.toFixed(4)}
              </span>
            </>
          )}
          {signal.indicators.stoch_k != null && (
            <>
              <span className="text-xs text-slate-500">Stoch RSI</span>
              <span className={`text-xs font-mono font-semibold ${signal.indicators.stoch_k < 20 ? 'text-green-400' : signal.indicators.stoch_k > 80 ? 'text-red-400' : 'text-slate-300'}`}>
                {signal.indicators.stoch_k.toFixed(1)}
              </span>
            </>
          )}
          {signal.indicators.ema9 != null && (
            <>
              <span className="text-xs text-slate-500">EMA 12/26/50</span>
              <span className={`text-xs font-mono font-semibold ${
                signal.indicators.ema9 > (signal.indicators.ema21 ?? 0) ? 'text-green-400' : 'text-red-400'
              }`}>
                {fmt(signal.indicators.ema9)}
              </span>
            </>
          )}
          {signal.indicators.supertrend_direction != null && (
            <>
              <span className="text-xs text-slate-500">Supertrend</span>
              <span className={`text-xs font-semibold ${signal.indicators.supertrend_direction === 1 ? 'text-green-400' : 'text-red-400'}`}>
                {signal.indicators.supertrend_direction === 1 ? 'ALTA' : 'BAIXA'}
              </span>
            </>
          )}
        </div>
      </div>

      {/* Patterns */}
      {signal.patterns.length > 0 && (
        <div className="bg-slate-800/60 rounded-lg p-3">
          <div className="flex items-center gap-1 mb-2">
            <ShieldAlert className="w-4 h-4 text-slate-400" />
            <span className="text-xs font-semibold text-slate-400">PADRÕES DETECTADOS</span>
          </div>
          <div className="flex flex-col gap-1.5">
            {signal.patterns.slice(0, 5).map((p, i) => (
              <div key={i} className="flex items-start gap-2">
                <span className={`mt-0.5 w-1.5 h-1.5 rounded-full flex-shrink-0 ${
                  p.direction === 'long' ? 'bg-green-400' : p.direction === 'short' ? 'bg-red-400' : 'bg-yellow-400'
                }`} />
                <div className="flex-1 min-w-0">
                  <p className="text-xs text-slate-300 leading-tight">{p.description}</p>
                  <div className="flex items-center gap-2 mt-0.5">
                    <div className="flex-1 h-1 bg-slate-700 rounded-full">
                      <div
                        className={`h-full rounded-full ${p.direction === 'long' ? 'bg-green-400' : p.direction === 'short' ? 'bg-red-400' : 'bg-yellow-400'}`}
                        style={{ width: `${p.confidence * 100}%` }}
                      />
                    </div>
                    <span className="text-xs text-slate-500">{(p.confidence * 100).toFixed(0)}%</span>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* AI Analysis */}
      {signal.ai_analysis && (
        <div className="bg-slate-800/60 rounded-lg p-3">
          <div className="flex items-center gap-1 mb-2">
            <Brain className="w-4 h-4 text-violet-400" />
            <span className="text-xs font-semibold text-violet-400">ANÁLISE IA (Claude)</span>
          </div>
          <p className="text-xs text-slate-300 leading-relaxed whitespace-pre-line">
            {signal.ai_analysis}
          </p>
        </div>
      )}
    </div>
  )
}
