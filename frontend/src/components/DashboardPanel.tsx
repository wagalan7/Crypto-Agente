import { useState, useEffect, useCallback, useMemo } from 'react'
import { X, TrendingUp, TrendingDown, BarChart3, Lock, RefreshCw } from 'lucide-react'

const BACKEND = import.meta.env.VITE_API_URL ?? 'https://crypto-agente-production.up.railway.app'

interface EquityPoint {
  date: string
  pnl_pct: number
  cumulative_pct: number
  trades: number
}

interface EquityCurve {
  enabled: boolean
  mode: string
  days: number
  curve: EquityPoint[]
  trades_total: number
  final_pnl_pct: number
}

interface TierStat {
  n: number
  wins: number
  losses: number
  wr_pct: number | null
  avg_r: number | null
  expectancy_r: number | null
  max_consec_losses: number
  pnl_pct: number
}

interface PaperSummary {
  enabled: boolean
  mode: string
  days: number
  equity: EquityCurve
  tier_stats: Record<string, TierStat>
}

interface CalibrationVersion {
  id: number
  version: string
  total_resolved: number
  p_global: number | null
  win_rate: number | null
  avg_r: number | null
  sharpe: number | null
  active: boolean
  computed_at: string | null
}

interface Props {
  onClose: () => void
}

type Period = 7 | 30 | 90

const PERIODS: { value: Period; label: string }[] = [
  { value: 7, label: '7d' },
  { value: 30, label: '30d' },
  { value: 90, label: '90d' },
]

/**
 * Dashboard comparativo (#10) — visão consolidada do desempenho do bot.
 *
 * Hoje é paper-only (sem real trading, vem com #11/Bybit). Mostra:
 *  - KPIs do período: PnL%, trades, WR, avgR, expectancy, Sharpe
 *  - Equity curve (SVG line chart) — acumulativo % da banca
 *  - Breakdown por tier (A+/A/B): WR/avgR/expectancy/maxDD/pnl
 *  - Coluna "Real" placeholder com lock — habilita quando #11 estiver pronto
 *  - Snapshot de calibração ativo (PAV) + link pro versionamento
 */
export default function DashboardPanel({ onClose }: Props) {
  const [period, setPeriod] = useState<Period>(30)
  const [paper, setPaper] = useState<PaperSummary | null>(null)
  const [real, setReal] = useState<PaperSummary | null>(null)
  const [calib, setCalib] = useState<CalibrationVersion | null>(null)
  const [calibVersions, setCalibVersions] = useState<CalibrationVersion[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [sumRes, realRes, activeRes, versionsRes] = await Promise.all([
        fetch(`${BACKEND}/api/paper/summary?days=${period}`),
        fetch(`${BACKEND}/api/real-trades/summary?days=${period}`).catch(() => null),
        fetch(`${BACKEND}/api/calibration/active`).catch(() => null),
        fetch(`${BACKEND}/api/calibration/versions?limit=10`).catch(() => null),
      ])
      if (!sumRes.ok) throw new Error(`paper summary ${sumRes.status}`)
      const sum = await sumRes.json()
      setPaper(sum)
      if (realRes && realRes.ok) {
        const r = await realRes.json()
        setReal(r)
      } else {
        setReal(null)
      }
      if (activeRes && activeRes.ok) {
        const a = await activeRes.json()
        setCalib(a)
      }
      if (versionsRes && versionsRes.ok) {
        const v = await versionsRes.json()
        setCalibVersions(Array.isArray(v) ? v : (v?.versions ?? []))
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'erro desconhecido')
    } finally {
      setLoading(false)
    }
  }, [period])

  useEffect(() => {
    load()
  }, [load])

  // KPIs derivados da equity curve
  const kpis = useMemo(() => {
    if (!paper || !paper.equity?.curve?.length) {
      return {
        finalPct: 0, trades: 0, wr: null as number | null,
        avgR: null as number | null, expectancy: null as number | null,
        sharpe: null as number | null, maxDD: 0, bestDay: 0, worstDay: 0,
      }
    }
    const curve = paper.equity.curve
    const finalPct = paper.equity.final_pnl_pct
    const trades = paper.equity.trades_total
    const dailyPcts = curve.map(p => p.pnl_pct)
    const avgDaily = dailyPcts.reduce((a, b) => a + b, 0) / dailyPcts.length
    const variance = dailyPcts.reduce((acc, x) => acc + (x - avgDaily) ** 2, 0) / dailyPcts.length
    const std = Math.sqrt(variance)
    const sharpe = std > 0 ? (avgDaily / std) * Math.sqrt(365) : null
    // Max drawdown da cumulative
    let peak = -Infinity
    let maxDD = 0
    for (const p of curve) {
      if (p.cumulative_pct > peak) peak = p.cumulative_pct
      const dd = p.cumulative_pct - peak
      if (dd < maxDD) maxDD = dd
    }
    const bestDay = Math.max(...dailyPcts)
    const worstDay = Math.min(...dailyPcts)
    // WR/avgR agregando os tiers
    const tiers = Object.values(paper.tier_stats ?? {})
    const totalN = tiers.reduce((a, t) => a + (t?.n ?? 0), 0)
    const totalWins = tiers.reduce((a, t) => a + (t?.wins ?? 0), 0)
    const wr = totalN ? (totalWins / totalN) * 100 : null
    const weightedR = totalN
      ? tiers.reduce((a, t) => a + (t?.avg_r ?? 0) * (t?.n ?? 0), 0) / totalN
      : null
    return {
      finalPct, trades, wr,
      avgR: weightedR, expectancy: weightedR, sharpe,
      maxDD, bestDay, worstDay,
    }
  }, [paper])

  return (
    <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-2 md:p-6">
      <div className="bg-[#0a0e1a] border border-slate-700/50 rounded-xl shadow-2xl w-full max-w-6xl max-h-[95vh] flex flex-col overflow-hidden">
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-slate-800 flex-shrink-0">
          <div className="flex items-center gap-2">
            <BarChart3 className="w-5 h-5 text-cyan-400" />
            <h2 className="text-base font-bold bg-gradient-to-r from-cyan-300 to-emerald-300 bg-clip-text text-transparent">
              Dashboard de Performance
            </h2>
            <span className="px-1.5 py-0.5 text-[10px] font-bold bg-amber-500/15 text-amber-400 border border-amber-500/30 rounded">
              PAPER
            </span>
          </div>
          <div className="flex items-center gap-2">
            <div className="flex bg-slate-800/60 rounded-md overflow-hidden border border-slate-700/50">
              {PERIODS.map(p => (
                <button
                  key={p.value}
                  onClick={() => setPeriod(p.value)}
                  className={`px-2.5 py-1 text-xs font-bold transition-colors ${
                    period === p.value
                      ? 'bg-cyan-500/20 text-cyan-300'
                      : 'text-slate-400 hover:text-slate-200'
                  }`}
                >
                  {p.label}
                </button>
              ))}
            </div>
            <button
              onClick={load}
              disabled={loading}
              className="p-1.5 hover:bg-slate-800 rounded text-slate-400 hover:text-slate-200 transition-colors disabled:opacity-40"
              title="Atualizar"
            >
              <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
            </button>
            <button
              onClick={onClose}
              className="p-1.5 hover:bg-slate-800 rounded text-slate-400 hover:text-slate-200 transition-colors"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto p-4 space-y-4">
          {error && (
            <div className="bg-red-500/10 border border-red-500/30 rounded p-3 text-sm text-red-300">
              Erro carregando: {error}
            </div>
          )}

          {/* KPI grid */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
            <KPI
              label="P&L Total"
              value={`${kpis.finalPct >= 0 ? '+' : ''}${kpis.finalPct.toFixed(2)}%`}
              cls={kpis.finalPct >= 0 ? 'text-green-400' : 'text-red-400'}
              icon={kpis.finalPct >= 0 ? <TrendingUp className="w-3.5 h-3.5" /> : <TrendingDown className="w-3.5 h-3.5" />}
            />
            <KPI
              label="Trades resolvidos"
              value={kpis.trades.toString()}
              cls="text-slate-200"
            />
            <KPI
              label="Win rate"
              value={kpis.wr != null ? `${kpis.wr.toFixed(1)}%` : '—'}
              cls={(kpis.wr ?? 0) >= 50 ? 'text-green-400' : 'text-amber-400'}
            />
            <KPI
              label="Expectancy (R)"
              value={kpis.expectancy != null ? `${kpis.expectancy >= 0 ? '+' : ''}${kpis.expectancy.toFixed(2)}` : '—'}
              cls={(kpis.expectancy ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'}
            />
            <KPI
              label="Sharpe (anualizado)"
              value={kpis.sharpe != null ? kpis.sharpe.toFixed(2) : '—'}
              cls={(kpis.sharpe ?? 0) >= 1 ? 'text-green-400' : 'text-slate-300'}
            />
            <KPI
              label="Max DD"
              value={`${kpis.maxDD.toFixed(2)}%`}
              cls="text-red-400"
            />
            <KPI
              label="Melhor dia"
              value={`+${kpis.bestDay.toFixed(2)}%`}
              cls="text-green-400"
            />
            <KPI
              label="Pior dia"
              value={`${kpis.worstDay.toFixed(2)}%`}
              cls="text-red-400"
            />
          </div>

          {/* Equity curve */}
          <div className="bg-slate-900/50 border border-slate-800 rounded-lg p-3">
            <div className="flex items-center justify-between mb-2">
              <h3 className="text-xs font-bold text-slate-300 uppercase tracking-wider">
                Equity Curve — P&L acumulado (%)
              </h3>
              <span className="text-[10px] text-slate-500">{paper?.equity?.curve?.length ?? 0} dias</span>
            </div>
            <EquityChart points={paper?.equity?.curve ?? []} />
          </div>

          {/* Side-by-side Paper vs Real */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {/* Paper */}
            <div className="bg-slate-900/50 border border-emerald-700/30 rounded-lg p-3">
              <div className="flex items-center justify-between mb-2">
                <h3 className="text-xs font-bold text-emerald-300 uppercase tracking-wider">
                  📄 Paper Trading
                </h3>
                <span className="text-[10px] text-emerald-400/60">live</span>
              </div>
              <TierTable tiers={paper?.tier_stats ?? {}} />
            </div>

            {/* Real */}
            <div className={`bg-slate-900/50 border rounded-lg p-3 ${
              (real?.equity?.trades_total ?? 0) > 0
                ? 'border-cyan-700/30'
                : 'border-slate-700/30'
            }`}>
              <div className="flex items-center justify-between mb-2">
                <h3 className={`text-xs font-bold uppercase tracking-wider flex items-center gap-1.5 ${
                  (real?.equity?.trades_total ?? 0) > 0 ? 'text-cyan-300' : 'text-slate-500'
                }`}>
                  {(real?.equity?.trades_total ?? 0) > 0 ? '💵' : <Lock className="w-3 h-3" />}
                  Real Trading
                </h3>
                <span className={`text-[10px] ${
                  (real?.equity?.trades_total ?? 0) > 0 ? 'text-cyan-400/60' : 'text-slate-600'
                }`}>
                  {(real?.equity?.trades_total ?? 0) > 0
                    ? `${real?.equity?.trades_total} trades · ${(real?.equity?.final_pnl_pct ?? 0) >= 0 ? '+' : ''}${(real?.equity?.final_pnl_pct ?? 0).toFixed(2)}%`
                    : 'sem fills registrados'}
                </span>
              </div>
              {(real?.equity?.trades_total ?? 0) > 0 ? (
                <TierTable tiers={real?.tier_stats ?? {}} />
              ) : (
                <div className="flex flex-col items-center justify-center py-8 text-center">
                  <Lock className="w-8 h-8 text-slate-700 mb-2" />
                  <p className="text-xs text-slate-500 max-w-[260px]">
                    Nenhum fill real registrado ainda. Quando você executar uma rec
                    na corretora, faça <code className="text-cyan-400">POST /api/real-trades</code> com
                    o entry_price real — o sistema calcula slippage vs paper.
                  </p>
                </div>
              )}
            </div>
          </div>

          {/* Calibração ativa */}
          {calib && (
            <div className="bg-slate-900/50 border border-violet-700/30 rounded-lg p-3">
              <div className="flex items-center justify-between mb-2">
                <h3 className="text-xs font-bold text-violet-300 uppercase tracking-wider">
                  🎯 Calibração ativa (PAV)
                </h3>
                <span className="text-[10px] text-violet-400/60">{calib.version}</span>
              </div>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-xs">
                <div>
                  <div className="text-slate-500 text-[10px] uppercase">Amostra</div>
                  <div className="text-slate-200 font-mono">{calib.total_resolved} trades</div>
                </div>
                <div>
                  <div className="text-slate-500 text-[10px] uppercase">P global</div>
                  <div className="text-slate-200 font-mono">
                    {calib.p_global != null ? `${(calib.p_global * 100).toFixed(1)}%` : '—'}
                  </div>
                </div>
                <div>
                  <div className="text-slate-500 text-[10px] uppercase">WR retro</div>
                  <div className="text-slate-200 font-mono">
                    {calib.win_rate != null ? `${(calib.win_rate * 100).toFixed(1)}%` : '—'}
                  </div>
                </div>
                <div>
                  <div className="text-slate-500 text-[10px] uppercase">Sharpe</div>
                  <div className="text-slate-200 font-mono">
                    {calib.sharpe != null ? calib.sharpe.toFixed(2) : '—'}
                  </div>
                </div>
              </div>
              {calibVersions.length > 1 && (
                <div className="mt-3 pt-2 border-t border-slate-800">
                  <div className="text-[10px] text-slate-500 uppercase mb-1">Versões recentes</div>
                  <div className="flex flex-wrap gap-1">
                    {calibVersions.slice(0, 6).map(v => (
                      <span
                        key={v.id}
                        className={`px-1.5 py-0.5 rounded text-[10px] font-mono ${
                          v.active
                            ? 'bg-violet-500/20 text-violet-300 border border-violet-500/40'
                            : 'bg-slate-800/60 text-slate-500 border border-slate-700/40'
                        }`}
                        title={`Sharpe ${v.sharpe ?? '—'} · WR ${v.win_rate ?? '—'}`}
                      >
                        {v.version.slice(0, 10)}
                      </span>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}

          {loading && !paper && (
            <div className="flex flex-col items-center justify-center py-12 gap-3">
              <div className="w-8 h-8 border-2 border-cyan-500 border-t-transparent rounded-full animate-spin" />
              <span className="text-sm text-slate-500">Carregando dashboard...</span>
            </div>
          )}

          {paper && paper.equity?.trades_total === 0 && (
            <div className="bg-slate-900/50 border border-slate-800 rounded-lg p-6 text-center">
              <p className="text-sm text-slate-500">
                Nenhum trade resolvido nos últimos {period} dias.
                Mantenha o scan rodando — as recomendações viram entradas paper automaticamente.
              </p>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="px-4 py-2 border-t border-slate-800 flex items-center justify-between text-[10px] text-slate-600 flex-shrink-0">
          <span>
            Paper-trade = snapshots de recomendações com TP1/TP2/BE/Stop simulados.
          </span>
          <span>
            {paper?.equity?.days ? `Janela: ${paper.equity.days}d` : ''}
          </span>
        </div>
      </div>
    </div>
  )
}

// ─── KPI Card ─────────────────────────────────────────────────────────────────

function KPI({ label, value, cls, icon }: { label: string; value: string; cls?: string; icon?: React.ReactNode }) {
  return (
    <div className="bg-slate-900/50 border border-slate-800 rounded-lg p-2.5">
      <div className="flex items-center gap-1 text-[10px] text-slate-500 uppercase tracking-wider mb-1">
        {icon}
        {label}
      </div>
      <div className={`text-lg font-bold font-mono ${cls ?? 'text-slate-200'}`}>{value}</div>
    </div>
  )
}

// ─── Equity curve (SVG) ───────────────────────────────────────────────────────

function EquityChart({ points }: { points: EquityPoint[] }) {
  if (!points || points.length < 2) {
    return (
      <div className="h-32 flex items-center justify-center text-xs text-slate-600">
        {points?.length === 1 ? 'Apenas 1 dia de dados' : 'Sem dados suficientes'}
      </div>
    )
  }
  const W = 800
  const H = 140
  const PAD = 8
  const ys = points.map(p => p.cumulative_pct)
  const minY = Math.min(0, ...ys)
  const maxY = Math.max(0, ...ys)
  const rangeY = maxY - minY || 1
  const stepX = (W - 2 * PAD) / (points.length - 1)
  const yOf = (v: number) => H - PAD - ((v - minY) / rangeY) * (H - 2 * PAD)
  const xOf = (i: number) => PAD + i * stepX
  const zeroY = yOf(0)
  const linePath = points
    .map((p, i) => `${i === 0 ? 'M' : 'L'} ${xOf(i).toFixed(1)} ${yOf(p.cumulative_pct).toFixed(1)}`)
    .join(' ')
  const areaPath =
    `M ${xOf(0)} ${zeroY} ` +
    points.map((p, i) => `L ${xOf(i).toFixed(1)} ${yOf(p.cumulative_pct).toFixed(1)}`).join(' ') +
    ` L ${xOf(points.length - 1)} ${zeroY} Z`
  const finalPositive = (points[points.length - 1]?.cumulative_pct ?? 0) >= 0
  const stroke = finalPositive ? '#22c55e' : '#ef4444'
  const fill = finalPositive ? 'rgba(34,197,94,0.12)' : 'rgba(239,68,68,0.12)'

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-32" preserveAspectRatio="none">
      {/* Zero line */}
      <line x1={PAD} y1={zeroY} x2={W - PAD} y2={zeroY} stroke="rgba(148,163,184,0.25)" strokeDasharray="3 3" />
      {/* Area */}
      <path d={areaPath} fill={fill} />
      {/* Line */}
      <path d={linePath} stroke={stroke} strokeWidth={1.5} fill="none" />
      {/* End dot */}
      <circle
        cx={xOf(points.length - 1)}
        cy={yOf(points[points.length - 1].cumulative_pct)}
        r={3}
        fill={stroke}
      />
      {/* Labels min/max */}
      <text x={PAD} y={PAD + 8} fontSize="9" fill="rgba(148,163,184,0.6)">
        {maxY >= 0 ? '+' : ''}{maxY.toFixed(1)}%
      </text>
      <text x={PAD} y={H - PAD - 2} fontSize="9" fill="rgba(148,163,184,0.6)">
        {minY.toFixed(1)}%
      </text>
    </svg>
  )
}

// ─── Tier breakdown table ─────────────────────────────────────────────────────

function TierTable({ tiers }: { tiers: Record<string, TierStat> }) {
  const order: string[] = ['A+', 'A', 'B']
  const tierColor: Record<string, string> = {
    'A+': 'text-yellow-300 bg-yellow-500/10 border-yellow-500/30',
    'A': 'text-emerald-300 bg-emerald-500/10 border-emerald-500/30',
    'B': 'text-slate-300 bg-slate-500/10 border-slate-500/30',
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="text-[10px] text-slate-500 uppercase tracking-wider">
            <th className="text-left py-1 font-semibold">Tier</th>
            <th className="text-right py-1 font-semibold">N</th>
            <th className="text-right py-1 font-semibold">WR</th>
            <th className="text-right py-1 font-semibold">avg R</th>
            <th className="text-right py-1 font-semibold">Exp.</th>
            <th className="text-right py-1 font-semibold" title="Max consec. losses">str</th>
            <th className="text-right py-1 font-semibold">P&L</th>
          </tr>
        </thead>
        <tbody>
          {order.map(t => {
            const s = tiers[t]
            const n = s?.n ?? 0
            return (
              <tr key={t} className="border-t border-slate-800/60">
                <td className="py-1.5">
                  <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold border ${tierColor[t]}`}>
                    {t}
                  </span>
                </td>
                <td className="text-right py-1.5 font-mono text-slate-300">{n}</td>
                <td className="text-right py-1.5 font-mono">
                  {s?.wr_pct != null ? (
                    <span className={s.wr_pct >= 50 ? 'text-green-400' : 'text-amber-400'}>
                      {s.wr_pct.toFixed(1)}%
                    </span>
                  ) : <span className="text-slate-600">—</span>}
                </td>
                <td className="text-right py-1.5 font-mono">
                  {s?.avg_r != null ? (
                    <span className={s.avg_r >= 0 ? 'text-green-400' : 'text-red-400'}>
                      {s.avg_r >= 0 ? '+' : ''}{s.avg_r.toFixed(2)}
                    </span>
                  ) : <span className="text-slate-600">—</span>}
                </td>
                <td className="text-right py-1.5 font-mono">
                  {s?.expectancy_r != null ? (
                    <span className={s.expectancy_r >= 0 ? 'text-green-400' : 'text-red-400'}>
                      {s.expectancy_r >= 0 ? '+' : ''}{s.expectancy_r.toFixed(2)}
                    </span>
                  ) : <span className="text-slate-600">—</span>}
                </td>
                <td className="text-right py-1.5 font-mono text-slate-400">{s?.max_consec_losses ?? 0}</td>
                <td className="text-right py-1.5 font-mono">
                  {s?.pnl_pct != null ? (
                    <span className={s.pnl_pct >= 0 ? 'text-green-400' : 'text-red-400'}>
                      {s.pnl_pct >= 0 ? '+' : ''}{s.pnl_pct.toFixed(2)}%
                    </span>
                  ) : <span className="text-slate-600">—</span>}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
