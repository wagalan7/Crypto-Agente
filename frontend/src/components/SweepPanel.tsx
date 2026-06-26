import { useState, useEffect, useCallback } from 'react'
import { X, Radar, CheckCircle2, Loader2, TrendingUp, AlertTriangle } from 'lucide-react'

const BACKEND = import.meta.env.VITE_API_URL ?? 'https://crypto-agente-production.up.railway.app'

// ─── Tipos ──────────────────────────────────────────────────────────────────

interface SweepProgress {
  tfs: string[]
  done: number
  mode: string
  pool: number
  limit: number
  total: number
  errors: number
  offset: number
  current: string | null
  running: boolean
  skipped: number
  computed: number
  excluded: number
  started_at: string | null
  finished_at: string | null
  _persisted_at: string | null
}

interface StatusResp {
  enabled: boolean
  progress: SweepProgress
}

interface RankRow {
  symbol: string
  timeframe: string
  n_trades: number
  wins: number
  losses: number
  expired: number
  wr_pct: number | null
  wr_clean_pct: number | null
  expiry_pct: number | null
  avg_r: number | null
  total_r: number | null
  profit_factor: number | null
  wf_avg_r: number | null
  wf_n_trades: number | null
  base: string
  in_allowlist: boolean
  perp_tradeable: boolean | null
  calibrated_avg_r: number | null
}

interface RankingResp {
  enabled: boolean
  sort: string
  tf: string | null
  min_trades: number
  allowlist_size: number
  perp_check: string
  n: number
  ranking: RankRow[]
  candidates_to_promote: RankRow[]
  nota?: string
}

interface Props {
  onClose: () => void
}

/**
 * SweepPanel — visão do BACKTEST MASSIVO (sweep) rodando no worker separado.
 *
 * - Progresso ao vivo (done/total, computados/pulados/erros, símbolo atual)
 * - Ranking das moedas por edge out-of-sample (wf_avg_r)
 * - Candidatas a PROMOÇÃO (fora da allowlist + edge forte) por timeframe
 *
 * Leitura pura: NÃO dispara o sweep (isso é função do worker). Espelha
 * StatusPanel no padrão de modal + polling 15s.
 */
export default function SweepPanel({ onClose }: Props) {
  const [status, setStatus] = useState<SweepProgress | null>(null)
  const [ranking, setRanking] = useState<RankingResp | null>(null)
  const [tfFilter, setTfFilter] = useState<'' | '1h' | '4h'>('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const t = Date.now()
      const tfq = tfFilter ? `&tf=${tfFilter}` : ''
      const [sRes, rRes] = await Promise.all([
        fetch(`${BACKEND}/api/backtest/universe/status?t=${t}`),
        fetch(`${BACKEND}/api/backtest/universe/ranking?min_trades=30&sort=wf_avg_r&limit=40${tfq}&t=${t}`),
      ])
      if (sRes.ok) {
        const j = (await sRes.json()) as StatusResp
        if (j.progress) setStatus(j.progress)
      }
      if (rRes.ok) {
        const j = (await rRes.json()) as RankingResp
        if (j.enabled !== false) setRanking(j)
      }
      setError(null)
    } catch (e) {
      setError(String(e))
    } finally {
      setLoading(false)
    }
  }, [tfFilter])

  useEffect(() => {
    load()
    const id = setInterval(load, 15_000)
    return () => clearInterval(id)
  }, [load])

  const pct = status && status.total > 0 ? Math.min(100, (status.done / status.total) * 100) : 0
  const running = status?.running ?? false
  const finished = !!status?.finished_at

  return (
    <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-start justify-center p-2 sm:p-4 overflow-y-auto">
      <div className="w-full max-w-3xl bg-[#0a0e1a] border border-slate-700 rounded-xl my-4">
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-slate-800">
          <div className="flex items-center gap-2">
            <Radar className={`w-5 h-5 ${running ? 'text-sky-400 animate-pulse' : finished ? 'text-emerald-400' : 'text-slate-400'}`} />
            <div>
              <h2 className="text-base font-bold text-white">Sweep · backtest massivo</h2>
              <p className="text-[11px] text-slate-500">
                Aprendizado de todo o universo · edge out-of-sample
              </p>
            </div>
          </div>
          <button onClick={onClose} className="p-1 rounded hover:bg-slate-800">
            <X className="w-5 h-5 text-slate-400" />
          </button>
        </div>

        {/* Conteúdo */}
        <div className="p-4 space-y-4">
          {loading && !status && (
            <div className="text-center py-8 text-slate-500 text-sm">Carregando…</div>
          )}
          {error && (
            <div className="p-2 rounded bg-red-500/10 border border-red-500/30 text-xs text-red-300">
              {error}
            </div>
          )}

          {status && (
            <>
              {/* Estado + progresso */}
              <div
                className={`p-3 rounded-lg border ${
                  finished
                    ? 'border-emerald-500/40 bg-emerald-500/10'
                    : running
                    ? 'border-sky-500/40 bg-sky-500/10'
                    : 'border-slate-700 bg-slate-900/50'
                }`}
              >
                <div className="flex items-center justify-between gap-2 flex-wrap">
                  <div className="flex items-center gap-2">
                    {finished ? (
                      <CheckCircle2 className="w-4 h-4 text-emerald-300" />
                    ) : running ? (
                      <Loader2 className="w-4 h-4 text-sky-300 animate-spin" />
                    ) : (
                      <Radar className="w-4 h-4 text-slate-400" />
                    )}
                    <span
                      className={`text-sm font-bold ${
                        finished ? 'text-emerald-200' : running ? 'text-sky-200' : 'text-slate-300'
                      }`}
                    >
                      {finished ? 'SWEEP CONCLUÍDO' : running ? 'SWEEP RODANDO' : 'SWEEP OCIOSO'}
                    </span>
                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-800 text-slate-400 border border-slate-700 font-mono">
                      {(status.tfs ?? []).join('+')} · {status.mode}
                    </span>
                  </div>
                  <span className="text-sm font-mono font-bold text-slate-200">
                    {status.done}/{status.total}
                  </span>
                </div>

                {/* Barra de progresso */}
                <div className="mt-2 h-2 rounded bg-slate-800 overflow-hidden">
                  <div
                    className={`h-full transition-all ${finished ? 'bg-emerald-500' : 'bg-sky-500'}`}
                    style={{ width: `${pct}%` }}
                  />
                </div>

                {/* Contadores */}
                <div className="grid grid-cols-3 gap-2 mt-2 text-[10px]">
                  <Counter label="computados" value={status.computed} cls="text-emerald-300" />
                  <Counter label="pulados (fresh)" value={status.skipped} cls="text-slate-300" />
                  <Counter label="erros" value={status.errors} cls={status.errors > 0 ? 'text-red-300' : 'text-slate-400'} />
                </div>

                {/* Símbolo atual / conclusão */}
                {running && status.current && (
                  <p className="mt-2 text-[11px] text-slate-400 font-mono truncate">
                    ▶ atual: <span className="text-sky-300">{status.current}</span>
                  </p>
                )}
                {finished && status.finished_at && (
                  <p className="mt-2 text-[11px] text-emerald-300 font-mono">
                    ✅ concluído em {new Date(status.finished_at).toLocaleString('pt-BR')}
                  </p>
                )}
                {status._persisted_at && (
                  <p className="mt-1 text-[10px] text-slate-600 font-mono">
                    último update {new Date(status._persisted_at).toLocaleTimeString('pt-BR')}
                    {' · '}pool {status.pool} · limite {status.limit}
                  </p>
                )}
              </div>

              {/* Candidatas a promoção */}
              {ranking && ranking.candidates_to_promote.length > 0 && (
                <div className="p-3 rounded-lg border border-amber-500/30 bg-amber-500/5">
                  <div className="flex items-center gap-2 mb-2">
                    <TrendingUp className="w-4 h-4 text-amber-300" />
                    <h3 className="text-xs font-bold text-amber-200">
                      Candidatas a promoção
                    </h3>
                    <span className="text-[10px] text-slate-500">
                      · {ranking.candidates_to_promote.length} fora da allowlist
                    </span>
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    {ranking.candidates_to_promote.slice(0, 30).map((c, i) => (
                      <span
                        key={`${c.symbol}-${c.timeframe}-${i}`}
                        className="text-[10px] px-1.5 py-0.5 rounded bg-amber-500/15 text-amber-200 border border-amber-500/40 font-mono"
                        title={`wf_avg_R ${c.wf_avg_r?.toFixed(2)} · n ${c.n_trades} · expiry ${c.expiry_pct?.toFixed(0)}%`}
                      >
                        {c.base} <span className="text-slate-500">{c.timeframe}</span>{' '}
                        <span className="text-emerald-300">
                          {c.wf_avg_r != null ? (c.wf_avg_r >= 0 ? '+' : '') + c.wf_avg_r.toFixed(2) : '—'}R
                        </span>
                      </span>
                    ))}
                  </div>
                  <p className="mt-2 text-[9px] text-slate-600 leading-tight">
                    Critério: fora da allowlist + perp negociável + wf_avg_R&gt;0.10 + avg_R&gt;0.10 + expiry&lt;35%.
                    Veredito final = shadow + rotação ao vivo.
                  </p>
                </div>
              )}

              {/* Ranking */}
              <div>
                <div className="flex items-center justify-between gap-2 mb-2">
                  <div className="flex items-center gap-2">
                    <h3 className="text-xs font-bold text-slate-300">Ranking por edge (OOS)</h3>
                    <span className="text-[10px] text-slate-500">
                      · {ranking?.n ?? 0} moedas · allowlist {ranking?.allowlist_size ?? '—'}
                    </span>
                  </div>
                  <div className="flex items-center gap-1">
                    {(['', '1h', '4h'] as const).map(tf => (
                      <button
                        key={tf || 'all'}
                        onClick={() => setTfFilter(tf)}
                        className={`text-[10px] px-1.5 py-0.5 rounded border font-mono ${
                          tfFilter === tf
                            ? 'bg-sky-600/30 border-sky-500/60 text-sky-200'
                            : 'bg-slate-800 border-slate-700 text-slate-400 hover:bg-slate-700'
                        }`}
                      >
                        {tf || 'todos'}
                      </button>
                    ))}
                  </div>
                </div>

                {!ranking || ranking.ranking.length === 0 ? (
                  <p className="text-xs text-slate-500 p-3 rounded bg-slate-900/40 border border-slate-800 flex items-center gap-2">
                    <AlertTriangle className="w-3.5 h-3.5" />
                    Sem moedas com ≥30 trades ainda. O ranking aparece conforme o sweep computa.
                  </p>
                ) : (
                  <div className="overflow-x-auto rounded border border-slate-800">
                    <table className="w-full text-[11px]">
                      <thead>
                        <tr className="bg-slate-900/60 text-slate-500 text-[10px]">
                          <th className="text-left px-2 py-1.5 font-medium">#</th>
                          <th className="text-left px-2 py-1.5 font-medium">Moeda</th>
                          <th className="text-right px-2 py-1.5 font-medium">wf_R</th>
                          <th className="text-right px-2 py-1.5 font-medium">avg_R</th>
                          <th className="text-right px-2 py-1.5 font-medium">n</th>
                          <th className="text-right px-2 py-1.5 font-medium">WR</th>
                          <th className="text-right px-2 py-1.5 font-medium">exp%</th>
                          <th className="text-right px-2 py-1.5 font-medium">PF</th>
                        </tr>
                      </thead>
                      <tbody>
                        {ranking.ranking.map((r, i) => (
                          <tr
                            key={`${r.symbol}-${r.timeframe}`}
                            className="border-t border-slate-800/60 hover:bg-slate-900/40"
                          >
                            <td className="px-2 py-1.5 text-slate-600 font-mono">{i + 1}</td>
                            <td className="px-2 py-1.5">
                              <span className="font-mono text-slate-200">{r.base}</span>
                              <span className="text-slate-600 ml-1">{r.timeframe}</span>
                              {r.in_allowlist && (
                                <span className="ml-1 text-[9px] px-1 py-0.5 rounded bg-emerald-500/15 text-emerald-300 border border-emerald-500/30">
                                  ✓
                                </span>
                              )}
                              {r.perp_tradeable === false && (
                                <span className="ml-1 text-[9px] px-1 py-0.5 rounded bg-red-500/15 text-red-300 border border-red-500/30">
                                  delisted
                                </span>
                              )}
                            </td>
                            <td className={`px-2 py-1.5 text-right font-mono font-bold ${rCls(r.wf_avg_r)}`}>
                              {fmtR(r.wf_avg_r)}
                            </td>
                            <td className={`px-2 py-1.5 text-right font-mono ${rCls(r.avg_r)}`}>
                              {fmtR(r.avg_r)}
                            </td>
                            <td className="px-2 py-1.5 text-right font-mono text-slate-400">{r.n_trades}</td>
                            <td className="px-2 py-1.5 text-right font-mono text-slate-300">
                              {r.wr_clean_pct != null ? `${r.wr_clean_pct.toFixed(0)}%` : '—'}
                            </td>
                            <td
                              className={`px-2 py-1.5 text-right font-mono ${
                                (r.expiry_pct ?? 0) >= 45 ? 'text-red-300' : 'text-slate-400'
                              }`}
                            >
                              {r.expiry_pct != null ? `${r.expiry_pct.toFixed(0)}` : '—'}
                            </td>
                            <td className="px-2 py-1.5 text-right font-mono text-slate-400">
                              {r.profit_factor != null ? r.profit_factor.toFixed(2) : '—'}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>

              {/* Rodapé */}
              <div className="text-[10px] text-slate-600 font-mono text-center pt-2 border-t border-slate-800">
                wf_R = avg_R out-of-sample (walk-forward) · ✓ = já na allowlist · backtest é PRÉ-FILTRO
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

function Counter({ label, value, cls }: { label: string; value: number; cls: string }) {
  return (
    <div className="p-1.5 rounded bg-slate-900/40 border border-slate-800 text-center">
      <div className={`font-mono font-bold text-sm ${cls}`}>{value}</div>
      <div className="text-slate-600">{label}</div>
    </div>
  )
}

function fmtR(v: number | null): string {
  if (v == null) return '—'
  return (v >= 0 ? '+' : '') + v.toFixed(2)
}

function rCls(v: number | null): string {
  if (v == null) return 'text-slate-500'
  return v >= 0 ? 'text-emerald-300' : 'text-red-300'
}
