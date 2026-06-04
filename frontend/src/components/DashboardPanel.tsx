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
  final_pnl_usd?: number
  open_positions?: number
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
  pnl_usd?: number
}

interface RealTradeRow {
  id: number
  symbol: string
  side: 'long' | 'short' | string
  source: string
  qty: number
  leverage: number | null
  notional_usd: number | null
  entry_price: number
  exit_price: number | null
  planned_stop: number | null
  planned_tp1: number | null
  planned_tp2: number | null
  opened_at: string | null
  closed_at: string | null
  status: string
  pnl_usd: number | null
  pnl_pct: number | null
  realized_r: number | null
  exchange: string | null
  exchange_order_id: string | null
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
  const [realTrades, setRealTrades] = useState<RealTradeRow[]>([])
  const [liveEquity, setLiveEquity] = useState<{ total: number; available: number; source: string; exchange: string } | null>(null)
  const [calib, setCalib] = useState<CalibrationVersion | null>(null)
  const [calibVersions, setCalibVersions] = useState<CalibrationVersion[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [sumRes, realRes, tradesRes, equityRes, activeRes, versionsRes] = await Promise.all([
        fetch(`${BACKEND}/api/paper/summary?days=${period}`),
        fetch(`${BACKEND}/api/real-trades/summary?days=${period}`).catch(() => null),
        fetch(`${BACKEND}/api/real-trades?days=${period}&limit=100`).catch(() => null),
        fetch(`${BACKEND}/api/exchange/equity`).catch(() => null),
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
      if (tradesRes && tradesRes.ok) {
        const t = await tradesRes.json()
        setRealTrades(Array.isArray(t) ? t : (t?.trades ?? []))
      } else {
        setRealTrades([])
      }
      if (equityRes && equityRes.ok) {
        const e = await equityRes.json()
        if (e?.ok) {
          setLiveEquity({
            total: Number(e.total_usd ?? 0),
            available: Number(e.available_usd ?? 0),
            source: String(e.source ?? 'unknown'),
            exchange: String(e.exchange ?? ''),
          })
        }
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
        {/* Header — responsivo: título + badge à esquerda (encolhe/trunca em mobile),
            controles à direita NUNCA somem (shrink-0 + ordem garantida pro X). */}
        <div className="flex items-center gap-2 px-3 md:px-4 py-2.5 md:py-3 border-b border-slate-800 flex-shrink-0">
          <div className="flex items-center gap-2 min-w-0 flex-1">
            <BarChart3 className="w-5 h-5 text-cyan-400 flex-shrink-0" />
            <h2 className="text-sm md:text-base font-bold bg-gradient-to-r from-cyan-300 to-emerald-300 bg-clip-text text-transparent truncate">
              <span className="hidden sm:inline">Dashboard de Performance</span>
              <span className="sm:hidden">Dashboard</span>
            </h2>
            <span className="hidden sm:inline px-1.5 py-0.5 text-[10px] font-bold bg-amber-500/15 text-amber-400 border border-amber-500/30 rounded flex-shrink-0">
              PAPER
            </span>
          </div>
          <div className="flex items-center gap-1 md:gap-2 flex-shrink-0">
            <div className="flex bg-slate-800/60 rounded-md overflow-hidden border border-slate-700/50">
              {PERIODS.map(p => (
                <button
                  key={p.value}
                  onClick={() => setPeriod(p.value)}
                  className={`px-2 md:px-2.5 py-1 text-xs font-bold transition-colors ${
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
              className="p-1.5 hover:bg-slate-800 rounded text-slate-400 hover:text-slate-200 transition-colors disabled:opacity-40 flex-shrink-0"
              title="Atualizar"
            >
              <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
            </button>
            <button
              onClick={onClose}
              aria-label="Fechar"
              className="p-2 hover:bg-slate-800 rounded text-slate-400 hover:text-slate-200 transition-colors flex-shrink-0"
            >
              <X className="w-5 h-5 md:w-4 md:h-4" />
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
                  Real / Shadow
                </h3>
                <span className={`text-[10px] ${
                  (real?.equity?.trades_total ?? 0) > 0 ? 'text-cyan-400/60' : 'text-slate-600'
                }`}>
                  {(real?.equity?.trades_total ?? 0) > 0
                    ? (() => {
                        const n = real?.equity?.trades_total ?? 0
                        const pct = real?.equity?.final_pnl_pct ?? 0
                        const usd = real?.equity?.final_pnl_usd ?? 0
                        const open = real?.equity?.open_positions ?? 0
                        const sign = (v: number) => (v >= 0 ? '+' : '')
                        return `${n} fechados${open ? ` · ${open} abertos` : ''} · ${sign(pct)}${pct.toFixed(2)}% · ${sign(usd)}$${usd.toFixed(2)}`
                      })()
                    : 'aguardando 1ª execução'}
                </span>
              </div>
              {(real?.equity?.trades_total ?? 0) > 0 ? (
                <TierTable tiers={real?.tier_stats ?? {}} showUsd />
              ) : (
                <div className="flex flex-col items-center justify-center py-8 text-center">
                  <Lock className="w-8 h-8 text-slate-700 mb-2" />
                  <p className="text-xs text-slate-500 max-w-[280px] leading-relaxed">
                    Vai mostrar trades executados: <strong className="text-slate-400">shadow</strong> (sistema
                    abre auto sem mexer em saldo real) e <strong className="text-slate-400">live</strong> quando
                    flipar <code className="text-cyan-400">EXCHANGE_SHADOW=false</code>. Aparece P&L, WR,
                    Sharpe e tier breakdown — idêntico ao Paper, mas com fills reais.
                  </p>
                </div>
              )}
            </div>
          </div>

          {/* Histórico de trades reais/shadow */}
          {realTrades.length > 0 && (
            <TradeHistory trades={realTrades} liveEquity={liveEquity} />
          )}

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

function TierTable({ tiers, showUsd = false }: { tiers: Record<string, TierStat>; showUsd?: boolean }) {
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
            {showUsd && <th className="text-right py-1 font-semibold">USD</th>}
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
                {showUsd && (
                  <td className="text-right py-1.5 font-mono">
                    {s?.pnl_usd != null ? (
                      <span className={s.pnl_usd >= 0 ? 'text-cyan-400' : 'text-red-400'}>
                        {s.pnl_usd >= 0 ? '+' : ''}${s.pnl_usd.toFixed(2)}
                      </span>
                    ) : <span className="text-slate-600">—</span>}
                  </td>
                )}
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

// ─── Trade history / journal ──────────────────────────────────────────────────

const FALLBACK_INITIAL_BALANCE_USD = 5000 // só se a API de equity estiver fora

function statusToTarget(status: string): { label: string; cls: string } {
  switch (status) {
    case 'closed_tp2': return { label: 'TP2', cls: 'text-green-300 bg-green-500/15 border-green-500/30' }
    case 'closed_tp1': return { label: 'TP1', cls: 'text-emerald-300 bg-emerald-500/15 border-emerald-500/30' }
    case 'closed_be':  return { label: 'BE',  cls: 'text-amber-300 bg-amber-500/15 border-amber-500/30' }
    case 'closed_stop':return { label: 'SL',  cls: 'text-red-300 bg-red-500/15 border-red-500/30' }
    case 'closed_manual':return { label: 'Manual', cls: 'text-slate-300 bg-slate-500/15 border-slate-500/30' }
    case 'open':       return { label: 'Aberto', cls: 'text-cyan-300 bg-cyan-500/15 border-cyan-500/30' }
    default:           return { label: status, cls: 'text-slate-300 bg-slate-500/15 border-slate-500/30' }
  }
}

function fmtDate(iso: string | null): string {
  if (!iso) return '—'
  try {
    const d = new Date(iso)
    return d.toLocaleString('pt-BR', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
  } catch { return iso.slice(0, 16) }
}

function fmtNum(v: number | null | undefined, digits = 4): string {
  if (v == null || Number.isNaN(v)) return '—'
  const abs = Math.abs(v)
  if (abs >= 1000) return v.toFixed(2)
  if (abs >= 1) return v.toFixed(digits)
  return v.toFixed(Math.max(digits, 6))
}

function TradeHistory({ trades, liveEquity }: {
  trades: RealTradeRow[]
  liveEquity: { total: number; available: number; source: string; exchange: string } | null
}) {
  // Filtrar só auto/shadow (ignorar manual)
  const filtered = trades.filter(t => t.source === 'auto' || t.source === 'shadow')

  // Computar saldo cumulativo:
  // - Shadow trades NÃO afetam o saldo real (são simulações paralelas).
  //   Mostramos before==after pra deixar claro que não houve impacto.
  // - Auto trades fechados afetam o saldo pelo pnl_usd real.
  // - Saldo inicial é derivado: saldo_atual_live − Σ pnl_usd(auto fechados)
  //   Assim a última linha sempre bate com a banca real da exchange.
  const sortedAsc = [...filtered].sort((a, b) => {
    const ta = a.opened_at ? new Date(a.opened_at).getTime() : 0
    const tb = b.opened_at ? new Date(b.opened_at).getTime() : 0
    return ta - tb
  })
  // Só auto FECHADO conta pro saldo real
  const totalRealClosedPnl = sortedAsc
    .filter(t => t.source === 'auto' && t.status !== 'open')
    .reduce((acc, t) => acc + (t.pnl_usd ?? 0), 0)
  const initialBalance = liveEquity
    ? liveEquity.total - totalRealClosedPnl
    : FALLBACK_INITIAL_BALANCE_USD

  // Saldo simulado paralelo: igual ao real, mas conta shadows também.
  // Útil pra ver "quanto teria sido" se tudo fosse executado.
  const totalSimClosedPnl = sortedAsc
    .filter(t => t.status !== 'open')
    .reduce((acc, t) => acc + (t.pnl_usd ?? 0), 0)
  const simulatedBalance = initialBalance + totalSimClosedPnl

  const balanceMap = new Map<number, { before: number; after: number }>()
  let running = initialBalance
  for (const t of sortedAsc) {
    const before = running
    const pnl = t.pnl_usd ?? 0
    // Shadow: balance não muda. Auto fechado: aplica pnl. Auto aberto: balance não muda ainda.
    const impactsBalance = t.source === 'auto' && t.status !== 'open'
    const after = impactsBalance ? running + pnl : running
    balanceMap.set(t.id, { before, after })
    if (impactsBalance) running = after
  }

  // Exibir DESC (mais recente primeiro)
  const rows = [...filtered].sort((a, b) => {
    const ta = a.opened_at ? new Date(a.opened_at).getTime() : 0
    const tb = b.opened_at ? new Date(b.opened_at).getTime() : 0
    return tb - ta
  })

  return (
    <div className="bg-slate-900/50 border border-slate-700/40 rounded-lg p-3">
      <div className="flex items-center justify-between mb-2 flex-wrap gap-1">
        <h3 className="text-xs font-bold text-slate-300 uppercase tracking-wider">
          📋 Histórico de operações (real / shadow)
        </h3>
        <span className="text-[10px] text-slate-500">
          {rows.length} trade{rows.length === 1 ? '' : 's'} · {liveEquity ? (
            <>
              real <span className="text-cyan-300 font-semibold">${liveEquity.total.toFixed(2)}</span>
              {' '}({liveEquity.exchange} live)
              {totalSimClosedPnl !== totalRealClosedPnl && (
                <> · simulado <span className="text-violet-300 font-semibold">${simulatedBalance.toFixed(2)}</span></>
              )}
            </>
          ) : (
            <>saldo inicial ${FALLBACK_INITIAL_BALANCE_USD.toLocaleString('pt-BR')} <span className="text-amber-400">(fallback — equity API offline)</span></>
          )}
        </span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-[11px]">
          <thead>
            <tr className="text-[9px] text-slate-500 uppercase tracking-wider border-b border-slate-800">
              <th className="text-left py-1.5 px-1 font-semibold">Data</th>
              <th className="text-left py-1.5 px-1 font-semibold">Moeda</th>
              <th className="text-left py-1.5 px-1 font-semibold">Side</th>
              <th className="text-right py-1.5 px-1 font-semibold">Qty</th>
              <th className="text-right py-1.5 px-1 font-semibold" title="Alavancagem">Lev</th>
              <th className="text-right py-1.5 px-1 font-semibold">Entrada</th>
              <th className="text-right py-1.5 px-1 font-semibold" title="Tamanho da posição em USD">Notional</th>
              <th className="text-right py-1.5 px-1 font-semibold" title="Margem usada = notional / leverage">Margem</th>
              <th className="text-right py-1.5 px-1 font-semibold" title="Margem como % do saldo no momento da entrada">% Banca</th>
              <th className="text-right py-1.5 px-1 font-semibold">Stop</th>
              <th className="text-right py-1.5 px-1 font-semibold">TP1</th>
              <th className="text-right py-1.5 px-1 font-semibold">TP2</th>
              <th className="text-right py-1.5 px-1 font-semibold">Saída</th>
              <th className="text-center py-1.5 px-1 font-semibold">Alvo</th>
              <th className="text-right py-1.5 px-1 font-semibold">P&L USD</th>
              <th className="text-right py-1.5 px-1 font-semibold" title="Saldo antes da operação">Saldo ant.</th>
              <th className="text-right py-1.5 px-1 font-semibold" title="Saldo após a operação">Saldo atual</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(t => {
              const target = statusToTarget(t.status)
              const lev = t.leverage ?? 1
              // notional_usd no DB pode ter sido salvo como 0 (entry_price=0 no momento da open
              // de market orders, vem preenchido depois). Recalcula se DB veio inválido.
              const notionalDb = t.notional_usd
              const notional = (notionalDb && notionalDb > 0) ? notionalDb : (t.entry_price * t.qty)
              const margem = lev > 0 ? notional / lev : notional
              const bal = balanceMap.get(t.id) ?? { before: 0, after: 0 }
              const sideCls = t.side === 'long'
                ? 'text-green-300 bg-green-500/10 border-green-500/30'
                : 'text-red-300 bg-red-500/10 border-red-500/30'
              const pnl = t.pnl_usd
              return (
                <tr key={t.id} className="border-b border-slate-800/40 hover:bg-slate-800/30">
                  <td className="py-1.5 px-1 text-slate-400 font-mono whitespace-nowrap">{fmtDate(t.opened_at)}</td>
                  <td className="py-1.5 px-1 text-slate-200 font-semibold">{t.symbol}</td>
                  <td className="py-1.5 px-1">
                    <span className={`px-1.5 py-0.5 rounded text-[9px] font-bold border uppercase ${sideCls}`}>
                      {t.side}
                    </span>
                  </td>
                  <td className="text-right py-1.5 px-1 font-mono text-slate-300">{fmtNum(t.qty, 4)}</td>
                  <td className="text-right py-1.5 px-1 font-mono text-slate-400">{lev}x</td>
                  <td className="text-right py-1.5 px-1 font-mono text-slate-200">{fmtNum(t.entry_price, 4)}</td>
                  <td className="text-right py-1.5 px-1 font-mono text-slate-300">${notional.toFixed(2)}</td>
                  <td className="text-right py-1.5 px-1 font-mono text-cyan-400">${margem.toFixed(2)}</td>
                  <td className="text-right py-1.5 px-1 font-mono">
                    {(() => {
                      // Base = saldo antes da operação; fallback pra liveEquity se before=0 (shadow / dados antigos)
                      const base = bal.before > 0 ? bal.before : (liveEquity?.total ?? 0)
                      if (!base || base <= 0) return <span className="text-slate-600">—</span>
                      const pct = (margem / base) * 100
                      const cls = pct <= 5 ? 'text-emerald-300'
                        : pct <= 15 ? 'text-amber-300'
                        : 'text-red-300 font-bold'
                      return <span className={cls} title={`${margem.toFixed(2)} / ${base.toFixed(2)}`}>{pct.toFixed(1)}%</span>
                    })()}
                  </td>
                  <td className="text-right py-1.5 px-1 font-mono text-red-300/80">{fmtNum(t.planned_stop, 4)}</td>
                  <td className="text-right py-1.5 px-1 font-mono text-emerald-300/80">{fmtNum(t.planned_tp1, 4)}</td>
                  <td className="text-right py-1.5 px-1 font-mono text-green-300/80">{fmtNum(t.planned_tp2, 4)}</td>
                  <td className="text-right py-1.5 px-1 font-mono text-slate-200">{fmtNum(t.exit_price, 4)}</td>
                  <td className="text-center py-1.5 px-1">
                    <span className={`px-1.5 py-0.5 rounded text-[9px] font-bold border ${target.cls}`}>
                      {target.label}
                    </span>
                  </td>
                  <td className="text-right py-1.5 px-1 font-mono">
                    {pnl != null ? (
                      <span className={pnl >= 0 ? 'text-green-400 font-bold' : 'text-red-400 font-bold'}>
                        {pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}
                      </span>
                    ) : <span className="text-slate-600">—</span>}
                  </td>
                  <td className="text-right py-1.5 px-1 font-mono text-slate-400">${bal.before.toFixed(2)}</td>
                  <td className="text-right py-1.5 px-1 font-mono text-slate-200 font-semibold">${bal.after.toFixed(2)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <div className="mt-2 text-[9px] text-slate-600">
        {liveEquity
          ? `Saldo real lido ao vivo da ${liveEquity.exchange} (cache 60s). Inicial derivado: $${initialBalance.toFixed(2)}. Apenas trades source=auto fechados movem o saldo — shadows são simulações paralelas (before==after). "Simulado" no header soma shadow+auto pra ver o que teria sido se tudo executasse.`
          : `Equity API offline — usando fallback $${FALLBACK_INITIAL_BALANCE_USD.toLocaleString('pt-BR')}. Quando voltar, o saldo será derivado da banca real.`}
      </div>
    </div>
  )
}
