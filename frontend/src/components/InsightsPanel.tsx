import { useState, useEffect, useCallback } from 'react'
import { X, Brain, TrendingUp, TrendingDown, AlertCircle, RefreshCw } from 'lucide-react'

interface BucketStat {
  trades: number
  wins: number
  losses: number
  win_rate: number
  avg_r: number
  total_r: number
}

interface Combo {
  category: string
  name: string
  trades: number
  win_rate: number
  avg_r: number
  total_r: number
}

interface Insights {
  enabled: boolean
  message?: string
  days?: number
  total_trades?: number
  overall?: { win_rate_pct: number; total_r: number; avg_r: number }
  by_tier?: Record<string, BucketStat>
  by_timeframe?: Record<string, BucketStat>
  by_direction?: Record<string, BucketStat>
  by_session?: Record<string, BucketStat>
  by_day_of_week?: Record<string, BucketStat>
  by_pattern?: Record<string, BucketStat>
  by_funding?: Record<string, BucketStat>
  by_symbol?: Record<string, BucketStat>
  winning_combos?: Combo[]
  losing_combos?: Combo[]
  baseline_win_rate?: number
  min_sample?: number
}

interface Props { onClose: () => void }

const BACKEND = import.meta.env.VITE_API_URL ?? 'https://crypto-agente-production.up.railway.app'

const CATEGORY_LABEL: Record<string, string> = {
  tier: 'Tier', tf: 'TF', direction: 'Direção', tier_tf: 'Tier·TF',
  session: 'Sessão', dow: 'Dia', pattern: 'Padrão', funding: 'Funding',
}

function colorForRate(rate: number, baseline: number): string {
  if (rate >= 60) return 'text-emerald-300'
  if (rate >= baseline) return 'text-green-400'
  if (rate >= 40) return 'text-yellow-300'
  return 'text-red-300'
}

function BucketTable({ title, data, baseline }: { title: string; data: Record<string, BucketStat> | undefined; baseline: number }) {
  if (!data || Object.keys(data).length === 0) return null
  return (
    <div className="bg-slate-900/40 border border-slate-800 rounded-lg p-3">
      <h4 className="text-xs font-bold text-slate-300 uppercase mb-2">{title}</h4>
      <div className="space-y-1">
        {Object.entries(data).slice(0, 10).map(([k, s]) => (
          <div key={k} className="flex items-center justify-between text-xs border-b border-slate-800/50 pb-1">
            <span className="text-slate-300 truncate flex-1">{k}</span>
            <span className="text-slate-500 mr-2 font-mono">{s.trades}</span>
            <span className={`font-mono font-bold w-12 text-right ${colorForRate(s.win_rate, baseline)}`}>
              {s.win_rate.toFixed(0)}%
            </span>
            <span className={`font-mono w-14 text-right ${s.avg_r >= 0 ? 'text-emerald-300' : 'text-red-300'}`}>
              {s.avg_r >= 0 ? '+' : ''}{s.avg_r.toFixed(2)}R
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

export default function InsightsPanel({ onClose }: Props) {
  const [data, setData] = useState<Insights | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [days, setDays] = useState(60)

  const load = useCallback(async (d: number) => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch(`${BACKEND}/api/learning-insights?days=${d}`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      setData(await res.json())
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Erro')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load(days) }, [load, days])

  const baseline = data?.baseline_win_rate ?? 50
  const totalTrades = data?.total_trades ?? 0

  return (
    <div className="fixed inset-0 bg-black/80 backdrop-blur-sm z-50 flex items-center justify-center p-2 sm:p-4">
      <div className="w-full max-w-6xl max-h-[92vh] bg-[#0a0e1a] border border-slate-700 rounded-xl flex flex-col overflow-hidden shadow-2xl">

        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-slate-800 bg-gradient-to-r from-slate-900 to-slate-800">
          <div className="flex items-center gap-2">
            <Brain className="w-5 h-5 text-violet-400" />
            <h2 className="text-base font-bold text-white">Insights de Aprendizado</h2>
            <span className="text-xs text-slate-500 hidden sm:inline">· o que o sistema descobriu</span>
          </div>
          <div className="flex items-center gap-2">
            <select
              value={days}
              onChange={e => setDays(parseInt(e.target.value))}
              className="bg-slate-800 border border-slate-700 rounded px-2 py-1 text-xs text-slate-200"
            >
              <option value={7}>7 dias</option>
              <option value={30}>30 dias</option>
              <option value={60}>60 dias</option>
              <option value={90}>90 dias</option>
              <option value={180}>180 dias</option>
            </select>
            <button onClick={() => load(days)} disabled={loading}
              className="flex items-center gap-1 px-2 py-1 bg-slate-800 hover:bg-slate-700 border border-slate-700 rounded text-xs text-slate-300 disabled:opacity-50">
              <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
            </button>
            <button onClick={onClose} className="p-1 hover:bg-slate-800 rounded">
              <X className="w-5 h-5 text-slate-400" />
            </button>
          </div>
        </div>

        {error && (
          <div className="m-4 p-3 bg-red-500/10 border border-red-500/40 rounded-lg text-sm text-red-300">⚠ {error}</div>
        )}

        {data && !data.enabled && (
          <div className="m-4 p-4 bg-yellow-500/10 border border-yellow-500/40 rounded-lg text-sm text-yellow-200">⚠ {data.message}</div>
        )}

        {data?.enabled && totalTrades === 0 && (
          <div className="flex flex-col items-center justify-center py-20 px-4 text-center gap-3">
            <Brain className="w-12 h-12 text-slate-700" />
            <p className="text-sm text-slate-300 font-semibold">Sistema ainda aprendendo</p>
            <p className="text-xs text-slate-500 max-w-md">
              Os insights aparecem aqui depois que as primeiras recomendações fecharem
              (atingindo TP ou stop). Volte em algumas horas/dias conforme os trades resolvem.
            </p>
          </div>
        )}

        {data?.enabled && totalTrades > 0 && (
          <div className="flex-1 overflow-y-auto p-3 space-y-3">

            {/* Overall */}
            {data.overall && (
              <div className="grid grid-cols-3 gap-2">
                <div className="bg-violet-500/10 border border-violet-500/30 rounded-lg p-3">
                  <div className="text-[10px] text-slate-500 uppercase">Win Rate Geral</div>
                  <div className={`text-2xl font-bold font-mono ${colorForRate(data.overall.win_rate_pct, baseline)}`}>
                    {data.overall.win_rate_pct.toFixed(1)}%
                  </div>
                  <div className="text-[10px] text-slate-600">baseline {baseline}%</div>
                </div>
                <div className="bg-emerald-500/10 border border-emerald-500/30 rounded-lg p-3">
                  <div className="text-[10px] text-slate-500 uppercase">R Total</div>
                  <div className={`text-2xl font-bold font-mono ${data.overall.total_r >= 0 ? 'text-emerald-300' : 'text-red-300'}`}>
                    {data.overall.total_r >= 0 ? '+' : ''}{data.overall.total_r.toFixed(1)}R
                  </div>
                  <div className="text-[10px] text-slate-600">{totalTrades} trades</div>
                </div>
                <div className="bg-slate-700/20 border border-slate-600/30 rounded-lg p-3">
                  <div className="text-[10px] text-slate-500 uppercase">R Médio/Trade</div>
                  <div className={`text-2xl font-bold font-mono ${data.overall.avg_r >= 0 ? 'text-emerald-300' : 'text-red-300'}`}>
                    {data.overall.avg_r >= 0 ? '+' : ''}{data.overall.avg_r.toFixed(2)}R
                  </div>
                  <div className="text-[10px] text-slate-600">expectativa por trade</div>
                </div>
              </div>
            )}

            {/* Winners / Losers */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              <div className="bg-emerald-500/5 border border-emerald-500/30 rounded-lg p-3">
                <h3 className="flex items-center gap-2 text-sm font-bold text-emerald-300 mb-2">
                  <TrendingUp className="w-4 h-4" /> Combos Vencedores
                </h3>
                {(!data.winning_combos || data.winning_combos.length === 0) ? (
                  <p className="text-xs text-slate-500">Nenhum combo com win rate ≥ 60% e amostra ≥ {data.min_sample} ainda.</p>
                ) : (
                  <div className="space-y-1.5">
                    {data.winning_combos.map((c, i) => (
                      <div key={i} className="flex items-center justify-between text-xs">
                        <span className="text-slate-400 w-16">{CATEGORY_LABEL[c.category] || c.category}</span>
                        <span className="text-white font-bold flex-1">{c.name}</span>
                        <span className="text-slate-500 mr-2 font-mono">{c.trades}t</span>
                        <span className="text-emerald-300 font-mono font-bold w-12 text-right">{c.win_rate.toFixed(0)}%</span>
                        <span className="text-emerald-300 font-mono w-14 text-right">+{c.avg_r.toFixed(2)}R</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              <div className="bg-red-500/5 border border-red-500/30 rounded-lg p-3">
                <h3 className="flex items-center gap-2 text-sm font-bold text-red-300 mb-2">
                  <TrendingDown className="w-4 h-4" /> Combos Perdedores
                </h3>
                {(!data.losing_combos || data.losing_combos.length === 0) ? (
                  <p className="text-xs text-slate-500">Nenhum combo com win rate ≤ 40% e amostra ≥ {data.min_sample} ainda.</p>
                ) : (
                  <div className="space-y-1.5">
                    {data.losing_combos.map((c, i) => (
                      <div key={i} className="flex items-center justify-between text-xs">
                        <span className="text-slate-400 w-16">{CATEGORY_LABEL[c.category] || c.category}</span>
                        <span className="text-white font-bold flex-1">{c.name}</span>
                        <span className="text-slate-500 mr-2 font-mono">{c.trades}t</span>
                        <span className="text-red-300 font-mono font-bold w-12 text-right">{c.win_rate.toFixed(0)}%</span>
                        <span className="text-red-300 font-mono w-14 text-right">{c.avg_r.toFixed(2)}R</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>

            {/* Buckets */}
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
              <BucketTable title="Por Tier" data={data.by_tier} baseline={baseline} />
              <BucketTable title="Por Timeframe" data={data.by_timeframe} baseline={baseline} />
              <BucketTable title="Por Direção" data={data.by_direction} baseline={baseline} />
              <BucketTable title="Por Sessão" data={data.by_session} baseline={baseline} />
              <BucketTable title="Por Dia da Semana" data={data.by_day_of_week} baseline={baseline} />
              <BucketTable title="Por Padrão" data={data.by_pattern} baseline={baseline} />
              <BucketTable title="Por Funding" data={data.by_funding} baseline={baseline} />
              <BucketTable title="Por Símbolo (top 10)" data={data.by_symbol} baseline={baseline} />
            </div>

            <div className="p-3 bg-slate-900/40 border border-slate-800 rounded-lg text-[11px] text-slate-500 flex items-start gap-2">
              <AlertCircle className="w-4 h-4 text-slate-600 flex-shrink-0 mt-0.5" />
              <span>
                Buckets com amostra menor que <strong className="text-slate-400">{data.min_sample}</strong> trades
                não viram "combo" porque a variância é alta demais. <strong>R médio</strong> = expectativa por trade:
                +0.5R é bom, +1R é excelente, negativo significa edge inverso (evite).
                Sistema usa esses dados pra ajustar score automaticamente quando atingir 50+ trades por bucket.
              </span>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
