import { useState, useEffect, useCallback } from 'react'
import { X, ShieldCheck, RefreshCw, Target, Filter, Gauge } from 'lucide-react'

interface Props {
  onClose: () => void
}

const BACKEND = import.meta.env.VITE_API_URL ?? 'https://crypto-agente-production.up.railway.app'

interface OutcomeStats {
  count: number
  wins?: number
  losses?: number
  win_rate_pct: number | null
  avg_r?: number | null
  expectancy_r: number | null
  sum_pnl_usd?: number
  tp1_hit_rate_pct: number | null
  tp2_hit_rate_pct: number | null
  by_status: Record<string, number>
}

interface GateItem {
  gate: string
  count: number
  last_reason: string | null
  last_symbol: string | null
  last_seen: string | null
}

interface Assertiveness {
  enabled: boolean
  reason?: string
  window_days?: number
  real_money?: OutcomeStats
  shadow?: OutcomeStats
  gates?: { window_days: number; total_skips: number; items: GateItem[] }
  calibration?: {
    mature: boolean
    total_resolved: number
    min_sample: number
    p_global: number | null
    win_rate_pct: number | null
    computed_at: string | null
  }
  computed_at?: string
}

// gate → rótulo PT-BR (espelha gateLabel do RecommendationsPanel)
const GATE_LABEL: Record<string, string> = {
  'liquidity-gate': 'liquidez baixa',
  'prob-gate': 'P(TP1) baixa',
  'rr-gate': 'R:R fraco',
  'score-min': 'score abaixo do mínimo',
  'proximity': 'preço longe da entrada',
  'atr-gate': 'volatilidade fora da faixa',
  'exec-universe': 'fora do universo de execução',
  'blacklist': 'símbolo bloqueado',
  'time-block': 'janela de horário bloqueada',
  'funding-gate': 'funding extremo',
  'mtf-gate': 'timeframes desalinhados',
  'entry-throttle': 'limite de entradas/hora',
  'direction-cap': 'limite de posições na direção',
  'cluster-cap': 'limite do cluster',
  'cluster-cap-dir': 'limite do cluster (direção)',
  'symbol-sl-cooldown': 'cooldown pós-stop no símbolo',
  'regime-guard': 'guarda de regime (stops recentes)',
  'daily-sl-breaker': 'breaker diário de stops',
  'flip-advisory': 'flip recente (advisory)',
  'risk-budget': 'orçamento de risco agregado',
}

function gateLabel(g: string): string {
  return GATE_LABEL[g] || g
}

function fmtR(n: number | null | undefined): string {
  if (n === null || n === undefined) return '–'
  return `${n > 0 ? '+' : ''}${n.toFixed(2)}R`
}

function fmtPct(n: number | null | undefined): string {
  if (n === null || n === undefined) return '–'
  return `${n.toFixed(1)}%`
}

function rColor(n: number | null | undefined): string {
  if (n === null || n === undefined) return 'text-slate-300'
  return n > 0 ? 'text-emerald-300' : n < 0 ? 'text-red-300' : 'text-slate-300'
}

export default function AssertivenessPanel({ onClose }: Props) {
  const [data, setData] = useState<Assertiveness | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [days, setDays] = useState(30)

  const load = useCallback(async (d: number) => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch(`${BACKEND}/api/shadow/assertiveness?days=${d}&gate_days=7`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      setData(await res.json())
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Erro')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load(days) }, [load, days])

  const real = data?.real_money
  const shadow = data?.shadow
  const gates = data?.gates
  const calib = data?.calibration

  return (
    <div className="fixed inset-0 bg-black/80 backdrop-blur-sm z-50 flex items-center justify-center p-2 sm:p-4">
      <div className="w-full max-w-4xl max-h-[92vh] bg-[#0a0e1a] border border-slate-700 rounded-xl flex flex-col overflow-hidden shadow-2xl">

        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-slate-800 bg-gradient-to-r from-slate-900 to-slate-800">
          <div className="flex items-center gap-2 min-w-0">
            <ShieldCheck className="w-5 h-5 text-emerald-400 shrink-0" />
            <h2 className="text-base font-bold text-white truncate">Assertividade do Bot</h2>
            <span className="text-xs text-slate-500 hidden sm:inline">· o quão confiável está sendo</span>
          </div>
          <div className="flex items-center gap-1 shrink-0">
            <div className="hidden sm:flex items-center gap-1 mr-1">
              {[7, 30, 90].map(d => (
                <button
                  key={d}
                  onClick={() => setDays(d)}
                  className={`px-2 py-1 rounded text-[11px] font-semibold border transition-colors ${
                    days === d
                      ? 'bg-emerald-500/15 border-emerald-400/50 text-emerald-300'
                      : 'bg-slate-800 border-slate-700 text-slate-400 hover:text-slate-200'
                  }`}
                >{d}d</button>
              ))}
            </div>
            <button onClick={() => load(days)} disabled={loading}
              className="flex items-center gap-1 px-2 py-1 bg-slate-800 hover:bg-slate-700 border border-slate-700 rounded text-xs text-slate-300 disabled:opacity-50">
              <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
            </button>
            <button onClick={onClose} className="p-2 hover:bg-slate-800 rounded shrink-0" aria-label="Fechar">
              <X className="w-5 h-5 text-slate-300" />
            </button>
          </div>
        </div>

        {/* Mobile day picker */}
        <div className="flex sm:hidden items-center gap-1 px-4 py-2 border-b border-slate-800">
          {[7, 30, 90].map(d => (
            <button
              key={d}
              onClick={() => setDays(d)}
              className={`px-3 py-1 rounded text-[11px] font-semibold border transition-colors ${
                days === d
                  ? 'bg-emerald-500/15 border-emerald-400/50 text-emerald-300'
                  : 'bg-slate-800 border-slate-700 text-slate-400'
              }`}
            >{d} dias</button>
          ))}
        </div>

        {error && (
          <div className="m-4 p-3 bg-red-500/10 border border-red-500/40 rounded-lg text-sm text-red-300">⚠ {error}</div>
        )}

        {data && !data.enabled && (
          <div className="m-4 p-4 bg-yellow-500/10 border border-yellow-500/40 rounded-lg text-sm text-yellow-200">
            ⚠ {data.reason || 'Banco de dados não configurado.'}
          </div>
        )}

        <div className="flex-1 overflow-y-auto p-3 flex flex-col gap-4">
          {loading && !data && (
            <div className="flex items-center justify-center py-20">
              <div className="w-8 h-8 border-2 border-emerald-500 border-t-transparent rounded-full animate-spin" />
            </div>
          )}

          {data?.enabled && (
            <>
              {/* ── Dinheiro real (source=auto) ─────────────────────────── */}
              <section>
                <div className="flex items-center gap-2 mb-2">
                  <Target className="w-4 h-4 text-emerald-400" />
                  <h3 className="text-sm font-bold text-emerald-300">Dinheiro real</h3>
                  <span className="text-[10px] text-slate-500">· auto-trades resolvidos · {data.window_days}d</span>
                </div>
                {real && real.count > 0 ? (
                  <>
                    <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
                      <StatCard label="Win rate" value={fmtPct(real.win_rate_pct)} sub={`${real.wins}W · ${real.losses}L`} />
                      <StatCard label="Expectancy" value={fmtR(real.expectancy_r)} valueCls={rColor(real.expectancy_r)} sub="média por trade" />
                      <StatCard label="TP1 / TP2 hit" value={`${fmtPct(real.tp1_hit_rate_pct)} / ${fmtPct(real.tp2_hit_rate_pct)}`} sub="taxa de alvos" />
                      <StatCard label="P&L (USD)" value={`$${(real.sum_pnl_usd ?? 0).toFixed(2)}`} valueCls={rColor(real.sum_pnl_usd)} sub={`${real.count} trades`} />
                    </div>
                    <StatusBreakdown by={real.by_status} />
                  </>
                ) : (
                  <p className="text-xs text-slate-500 italic px-1">Nenhum auto-trade resolvido na janela ainda.</p>
                )}
              </section>

              {/* ── Shadow (snapshots — amostra maior) ───────────────────── */}
              <section>
                <div className="flex items-center gap-2 mb-2">
                  <Gauge className="w-4 h-4 text-sky-400" />
                  <h3 className="text-sm font-bold text-sky-300">Shadow (amostra ampla)</h3>
                  <span className="text-[10px] text-slate-500">· recomendações rastreadas · base da calibração</span>
                </div>
                {shadow && shadow.count > 0 ? (
                  <>
                    <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
                      <StatCard label="Win rate" value={fmtPct(shadow.win_rate_pct)} sub={`${shadow.count} resolvidos`} />
                      <StatCard label="Expectancy" value={fmtR(shadow.expectancy_r)} valueCls={rColor(shadow.expectancy_r)} sub="média por setup" />
                      <StatCard label="TP1 hit" value={fmtPct(shadow.tp1_hit_rate_pct)} sub="tocou TP1" />
                      <StatCard label="TP2 hit" value={fmtPct(shadow.tp2_hit_rate_pct)} sub="chegou no TP2" />
                    </div>
                    <StatusBreakdown by={shadow.by_status} />
                  </>
                ) : (
                  <p className="text-xs text-slate-500 italic px-1">Sem snapshots resolvidos na janela.</p>
                )}
              </section>

              {/* ── Calibração ───────────────────────────────────────────── */}
              {calib && (
                <section>
                  <div className="flex items-center gap-2 mb-2">
                    <ShieldCheck className="w-4 h-4 text-violet-400" />
                    <h3 className="text-sm font-bold text-violet-300">Calibração P(TP1)</h3>
                  </div>
                  <div className="p-3 rounded-lg border border-slate-800 bg-slate-900/40 text-xs text-slate-300 flex flex-wrap items-center gap-x-4 gap-y-1">
                    <span>
                      Status:{' '}
                      {calib.mature
                        ? <span className="text-emerald-300 font-bold">madura ✓</span>
                        : <span className="text-yellow-300 font-bold">aquecendo</span>}
                    </span>
                    <span>Resolvidos: <span className="font-mono text-white">{calib.total_resolved}</span> / {calib.min_sample} mín</span>
                    {calib.win_rate_pct !== null && (
                      <span>P(TP1) global: <span className="font-mono text-white">{fmtPct(calib.win_rate_pct)}</span></span>
                    )}
                  </div>
                  {!calib.mature && (
                    <p className="mt-1 text-[10px] text-slate-500 leading-snug px-1">
                      Enquanto imatura, o gate de P(TP1) não filtra nada (no-op seguro) — começa a morder sozinho ao amadurecer.
                    </p>
                  )}
                </section>
              )}

              {/* ── Gates (skips persistidos) ────────────────────────────── */}
              <section>
                <div className="flex items-center gap-2 mb-2">
                  <Filter className="w-4 h-4 text-amber-400" />
                  <h3 className="text-sm font-bold text-amber-300">Gates que mais barraram</h3>
                  <span className="text-[10px] text-slate-500">
                    · últimos {gates?.window_days ?? 7}d · {gates?.total_skips ?? 0} skips
                  </span>
                </div>
                {gates && gates.items.length > 0 ? (
                  <div className="flex flex-col gap-1.5">
                    {gates.items.map(g => {
                      const pct = gates.total_skips > 0 ? (g.count / gates.total_skips) * 100 : 0
                      return (
                        <div key={g.gate} className="p-2 rounded-lg border border-slate-800 bg-slate-900/40">
                          <div className="flex items-center gap-2">
                            <span className="text-xs font-bold text-amber-200 capitalize">{gateLabel(g.gate)}</span>
                            <span className="text-[9px] text-slate-600 font-mono">{g.gate}</span>
                            <span className="ml-auto font-mono text-sm font-bold text-white">{g.count}</span>
                          </div>
                          <div className="mt-1 h-1.5 rounded-full bg-slate-800 overflow-hidden">
                            <div className="h-full bg-amber-500/60" style={{ width: `${Math.min(100, pct)}%` }} />
                          </div>
                          {g.last_reason && (
                            <p className="mt-1 text-[10px] text-slate-500 leading-snug truncate" title={g.last_reason}>
                              ex.: {g.last_symbol ? `${g.last_symbol.split('/')[0]} · ` : ''}{g.last_reason}
                            </p>
                          )}
                        </div>
                      )
                    })}
                  </div>
                ) : (
                  <p className="text-xs text-slate-500 italic px-1">
                    Nenhum skip registrado ainda na janela (contadores começam a acumular a partir do deploy desta versão).
                  </p>
                )}
              </section>
            </>
          )}
        </div>

        {/* Footer */}
        <div className="px-4 py-2 border-t border-slate-800 bg-slate-900/60 text-[10px] text-slate-500 leading-relaxed">
          <strong className="text-slate-400">Dinheiro real</strong> = auto-trades executados (amostra pequena, alta confiança).{' '}
          <strong className="text-slate-400">Shadow</strong> = recomendações rastreadas (amostra ampla, mesma base da calibração).{' '}
          <strong className="text-slate-400">Gates</strong> = motivos de veto persistidos — sobrevivem a redeploy.
        </div>
      </div>
    </div>
  )
}

function StatCard({ label, value, sub, valueCls }: { label: string; value: string; sub?: string; valueCls?: string }) {
  return (
    <div className="bg-slate-900/60 border border-slate-800 rounded-lg p-3">
      <div className="text-[10px] text-slate-500 uppercase">{label}</div>
      <div className={`text-lg font-bold font-mono ${valueCls ?? 'text-white'}`}>{value}</div>
      {sub && <div className="text-[10px] text-slate-600 mt-0.5">{sub}</div>}
    </div>
  )
}

function StatusBreakdown({ by }: { by: Record<string, number> }) {
  const entries = Object.entries(by || {}).sort((a, b) => b[1] - a[1])
  if (entries.length === 0) return null
  return (
    <div className="mt-2 flex flex-wrap gap-1.5">
      {entries.map(([status, n]) => (
        <span key={status} className="px-2 py-0.5 rounded text-[10px] font-mono border border-slate-700 bg-slate-800/60 text-slate-300">
          {status} <span className="text-white font-bold">{n}</span>
        </span>
      ))}
    </div>
  )
}
