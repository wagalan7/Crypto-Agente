import { useState, useEffect, useCallback, useMemo } from 'react'
import { X, BarChart3, TrendingUp, TrendingDown, Clock, RefreshCw, Calendar, ChevronLeft } from 'lucide-react'

interface Trade {
  symbol: string
  timeframe: string
  tier: string
  direction: 'long' | 'short'
  entry: number
  stop_loss: number
  tp1?: number
  tp2: number
  leverage: number
  status: string
  realized_r: number | null
  risk_pct: number
  score?: number
  created_at: string
  outcome_at: string | null
  tp1_hit_at?: string | null
}

interface DailyPnL {
  enabled: boolean
  message?: string
  date?: string
  summary?: {
    total_trades: number
    wins: number
    losses: number
    win_rate_pct: number
    total_r: number
    total_pct_banca?: number
    still_open: number
  }
  trades?: Trade[]
  open_trades?: Trade[]
}

interface Props {
  onClose: () => void
}

const BACKEND = import.meta.env.VITE_API_URL ?? 'https://crypto-agente-production.up.railway.app'

function todayISO(): string {
  return new Date().toISOString().slice(0, 10)
}

function fmtPct(n: number): string {
  return `${n >= 0 ? '+' : ''}${n.toFixed(2)}%`
}

function fmt(n: number) {
  if (n >= 1000) return n.toFixed(2)
  if (n >= 1) return n.toFixed(4)
  return n.toFixed(6)
}

// Mesma lógica do RecommendationsPanel — manter em sincronia.
function operationType(tf: string): string {
  const t = tf.toLowerCase()
  if (['1m', '3m', '5m', '15m'].includes(t)) return 'SCALP'
  if (['30m', '1h', '2h'].includes(t)) return 'DAY'
  return 'SWING'
}

const STATUS_BADGE: Record<string, { label: string; cls: string }> = {
  won_tp2: { label: 'TP2 ✓', cls: 'bg-emerald-500/20 text-emerald-300 border-emerald-500/40' },
  won_tp1: { label: 'TP1 ✓', cls: 'bg-green-500/20 text-green-300 border-green-500/40' },
  won_tp1_be: { label: 'BE (TP1)', cls: 'bg-sky-500/20 text-sky-300 border-sky-500/40' },
  lost:    { label: 'STOP ✗', cls: 'bg-red-500/20 text-red-300 border-red-500/40' },
  open:    { label: 'aberto', cls: 'bg-slate-500/20 text-slate-300 border-slate-500/40' },
  expired: { label: 'expirado', cls: 'bg-yellow-500/20 text-yellow-300 border-yellow-500/40' },
}

const STATUS_REASON: Record<string, string> = {
  won_tp2: 'Preço atingiu o TP2 — 50% saiu em TP1 (+0.5R) e 50% em TP2 (+1.0R). Total +1.5R.',
  won_tp1: 'Preço atingiu TP1 (50% saiu, +0.5R) mas snapshot expirou antes de TP2. Conservador: contou +0.5R.',
  won_tp1_be: 'Preço atingiu TP1 (50% saiu) e depois voltou pra entry. Stop subiu pra breakeven nos 50% restantes. Total +0.5R.',
  lost: 'Preço bateu o stop ANTES de tocar TP1 (sem parcial). Perda total de −1R.',
  expired: 'Trade fechado pelo time-stop (janela do TF expirou sem tocar TP1 nem stop). Sem perda, sem ganho — capital liberado.',
  open: 'Tracker ainda monitorando — aguardando preço tocar TP1, TP2 ou stop.',
}

type DrillKind = 'wins' | 'losses' | 'open' | 'all' | null

export default function DailyPnLPanel({ onClose }: Props) {
  const [date, setDate] = useState(todayISO())
  const [data, setData] = useState<DailyPnL | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [drill, setDrill] = useState<DrillKind>(null)

  const load = useCallback(async (d: string) => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch(`${BACKEND}/api/daily-pnl?date=${d}`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      setData(await res.json())
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Erro')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load(date) }, [load, date])

  const s = data?.summary
  const totalR = s?.total_r ?? 0
  const rColor = totalR > 0 ? 'text-emerald-300' : totalR < 0 ? 'text-red-300' : 'text-slate-300'

  // % real da banca (soma por trade, não total_r × risk_pct[0])
  const realPctBanca = useMemo(() => {
    if (typeof s?.total_pct_banca === 'number') return s.total_pct_banca
    // fallback (versão antiga do backend): soma cliente-side
    if (!data?.trades) return 0
    return data.trades.reduce((acc, t) => acc + (t.realized_r ?? 0) * t.risk_pct, 0)
  }, [s, data?.trades])

  // Detalhamento de % por categoria
  const pctBreakdown = useMemo(() => {
    if (!data?.trades) return { wins: 0, losses: 0 }
    let w = 0, l = 0
    for (const t of data.trades) {
      const pct = (t.realized_r ?? 0) * t.risk_pct
      if (pct > 0) w += pct
      else if (pct < 0) l += pct
    }
    return { wins: w, losses: l }
  }, [data?.trades])

  const winsList = useMemo(
    () => (data?.trades ?? []).filter(t => (t.realized_r ?? 0) > 0),
    [data?.trades]
  )
  const lossesList = useMemo(
    () => (data?.trades ?? []).filter(t => (t.realized_r ?? 0) < 0),
    [data?.trades]
  )
  const openList = data?.open_trades ?? []

  // ── Drill-down render ────────────────────────────────────────────────────
  if (drill !== null && data) {
    const title =
      drill === 'wins' ? 'Vencedores do dia' :
      drill === 'losses' ? 'Perdedores do dia' :
      drill === 'open' ? 'Trades em aberto' :
      'Todos os trades'
    const list =
      drill === 'wins' ? winsList :
      drill === 'losses' ? lossesList :
      drill === 'open' ? openList :
      [...(data.trades ?? []), ...openList]
    const subtitle =
      drill === 'wins' ? `${winsList.length} trade(s) · ${fmtPct(pctBreakdown.wins)} da banca` :
      drill === 'losses' ? `${lossesList.length} trade(s) · ${fmtPct(pctBreakdown.losses)} da banca` :
      drill === 'open' ? `${openList.length} aguardando preço bater TP ou stop` :
      `${list.length} trade(s) no total`

    return (
      <div className="fixed inset-0 bg-black/80 backdrop-blur-sm z-50 flex items-center justify-center p-2 sm:p-4">
        <div className="w-full max-w-3xl max-h-[92vh] bg-[#0a0e1a] border border-slate-700 rounded-xl flex flex-col overflow-hidden shadow-2xl">
          <div className="flex items-center justify-between px-4 py-3 border-b border-slate-800 bg-gradient-to-r from-slate-900 to-slate-800">
            <button onClick={() => setDrill(null)} className="flex items-center gap-1.5 text-xs text-slate-300 hover:text-white transition-colors">
              <ChevronLeft className="w-4 h-4" />
              voltar
            </button>
            <div className="flex-1 text-center">
              <h2 className="text-sm font-bold text-white">{title}</h2>
              <p className="text-[10px] text-slate-500">{subtitle}</p>
            </div>
            <button onClick={onClose} className="p-1 hover:bg-slate-800 rounded">
              <X className="w-5 h-5 text-slate-400" />
            </button>
          </div>

          <div className="flex-1 overflow-y-auto p-3">
            {list.length === 0 && (
              <div className="text-center py-12 text-sm text-slate-500">Nada por aqui ainda.</div>
            )}
            <div className="flex flex-col gap-2">
              {list.map((t, i) => {
                const badge = STATUS_BADGE[t.status] || STATUS_BADGE.open
                const reason = STATUS_REASON[t.status] || '—'
                const isLong = t.direction === 'long'
                const DirIcon = isLong ? TrendingUp : TrendingDown
                const r = t.realized_r ?? 0
                const pct = r * t.risk_pct
                const opType = operationType(t.timeframe)
                return (
                  <div key={i} className="p-3 rounded-lg border border-slate-800 bg-slate-900/40">
                    <div className="flex items-center gap-2 flex-wrap mb-2">
                      <span className={`px-2 py-0.5 rounded text-[10px] font-bold border ${badge.cls}`}>{badge.label}</span>
                      <DirIcon className={`w-4 h-4 ${isLong ? 'text-green-400' : 'text-red-400'}`} />
                      <span className="text-sm font-bold text-white">{t.symbol.split('/')[0]}</span>
                      <span className="text-[10px] text-slate-500 font-mono">{t.timeframe}</span>
                      <span className="text-[10px] text-slate-400 px-1.5 py-0.5 rounded border border-slate-700">{opType}</span>
                      <span className="text-[10px] text-orange-300 font-mono">{t.leverage}x</span>
                      <span className="text-[10px] text-slate-500">tier {t.tier}</span>
                      <span className="ml-auto font-mono text-sm font-bold text-right">
                        <span className={r > 0 ? 'text-emerald-300' : r < 0 ? 'text-red-300' : 'text-slate-400'}>
                          {r > 0 ? `+${r.toFixed(1)}R` : r < 0 ? `${r.toFixed(1)}R` : '–'}
                        </span>
                        <span className="block text-[10px] text-slate-500 font-normal font-sans">
                          {t.status !== 'open' && fmtPct(pct)}
                        </span>
                      </span>
                    </div>

                    <div className="grid grid-cols-3 sm:grid-cols-4 gap-2 text-[11px] mb-2">
                      <div>
                        <div className="text-slate-600 text-[9px]">Entrada</div>
                        <div className="font-mono text-yellow-300">{fmt(t.entry)}</div>
                      </div>
                      <div>
                        <div className="text-slate-600 text-[9px]">Stop</div>
                        <div className="font-mono text-red-300">{fmt(t.stop_loss)}</div>
                      </div>
                      {t.tp1 != null && (
                        <div>
                          <div className="text-slate-600 text-[9px]">TP1</div>
                          <div className="font-mono text-emerald-300">{fmt(t.tp1)}</div>
                        </div>
                      )}
                      <div>
                        <div className="text-slate-600 text-[9px]">TP2</div>
                        <div className="font-mono text-green-300">{fmt(t.tp2)}</div>
                      </div>
                    </div>

                    <p className="text-[10px] text-slate-400 leading-snug">{reason}</p>
                    {(t.created_at || t.outcome_at) && (
                      <p className="text-[9px] text-slate-600 mt-1 font-mono">
                        criado {t.created_at && new Date(t.created_at).toLocaleString('pt-BR', { hour: '2-digit', minute: '2-digit', day: '2-digit', month: '2-digit' })}
                        {t.outcome_at && (
                          <> · resolvido {new Date(t.outcome_at).toLocaleString('pt-BR', { hour: '2-digit', minute: '2-digit', day: '2-digit', month: '2-digit' })}</>
                        )}
                      </p>
                    )}
                  </div>
                )
              })}
            </div>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="fixed inset-0 bg-black/80 backdrop-blur-sm z-50 flex items-center justify-center p-2 sm:p-4">
      <div className="w-full max-w-5xl max-h-[92vh] bg-[#0a0e1a] border border-slate-700 rounded-xl flex flex-col overflow-hidden shadow-2xl">

        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-slate-800 bg-gradient-to-r from-slate-900 to-slate-800">
          <div className="flex items-center gap-2">
            <BarChart3 className="w-5 h-5 text-emerald-400" />
            <h2 className="text-base font-bold text-white">Resultado do Dia</h2>
            <span className="text-xs text-slate-500 hidden sm:inline">· P&L das recomendações</span>
          </div>
          <div className="flex items-center gap-2">
            <div className="flex items-center gap-1 bg-slate-800 border border-slate-700 rounded px-2 py-1">
              <Calendar className="w-3.5 h-3.5 text-slate-400" />
              <input
                type="date"
                value={date}
                onChange={e => setDate(e.target.value)}
                className="bg-transparent text-xs text-slate-200 outline-none"
                max={todayISO()}
              />
            </div>
            <button onClick={() => load(date)} disabled={loading}
              className="flex items-center gap-1 px-2 py-1 bg-slate-800 hover:bg-slate-700 border border-slate-700 rounded text-xs text-slate-300 disabled:opacity-50">
              <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
            </button>
            <button onClick={onClose} className="p-1 hover:bg-slate-800 rounded">
              <X className="w-5 h-5 text-slate-400" />
            </button>
          </div>
        </div>

        {data && !data.enabled && (
          <div className="m-4 p-4 bg-yellow-500/10 border border-yellow-500/40 rounded-lg text-sm text-yellow-200">
            ⚠ {data.message || 'Banco de dados não configurado. Configure DATABASE_URL no Railway.'}
          </div>
        )}

        {error && (
          <div className="m-4 p-3 bg-red-500/10 border border-red-500/40 rounded-lg text-sm text-red-300">⚠ {error}</div>
        )}

        {/* Resumo cards — clicáveis pra drill-down */}
        {s && (
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2 p-3 border-b border-slate-800">
            <button
              onClick={() => setDrill('all')}
              className="text-left bg-slate-900/60 border border-slate-800 hover:border-slate-600 rounded-lg p-3 transition-colors"
              title="Ver todos os trades do dia"
            >
              <div className="text-[10px] text-slate-500 uppercase">Total R</div>
              <div className={`text-xl font-bold font-mono ${rColor}`}>
                {totalR > 0 ? '+' : ''}{totalR.toFixed(2)}R
              </div>
              <div className={`text-[10px] mt-0.5 font-semibold ${rColor}`}>
                {fmtPct(realPctBanca)} da banca
              </div>
              {(pctBreakdown.wins !== 0 || pctBreakdown.losses !== 0) && (
                <div className="text-[9px] text-slate-600 mt-0.5 leading-tight">
                  {pctBreakdown.wins !== 0 && <span className="text-emerald-400">+{pctBreakdown.wins.toFixed(2)}%</span>}
                  {pctBreakdown.losses !== 0 && <span className="text-red-400 ml-1">{pctBreakdown.losses.toFixed(2)}%</span>}
                </div>
              )}
            </button>
            <button
              onClick={() => setDrill('all')}
              className="text-left bg-slate-900/60 border border-slate-800 hover:border-slate-600 rounded-lg p-3 transition-colors"
              title="Ver desempenho"
            >
              <div className="text-[10px] text-slate-500 uppercase">Win Rate</div>
              <div className="text-xl font-bold text-white font-mono">{s.win_rate_pct.toFixed(1)}%</div>
              <div className="text-[10px] text-slate-600 mt-0.5">{s.wins}W · {s.losses}L</div>
            </button>
            <button
              onClick={() => setDrill('all')}
              className="text-left bg-slate-900/60 border border-slate-800 hover:border-slate-600 rounded-lg p-3 transition-colors"
              title="Ver todos os trades resolvidos"
            >
              <div className="text-[10px] text-slate-500 uppercase">Trades</div>
              <div className="text-xl font-bold text-white font-mono">{s.total_trades}</div>
              <div className="text-[10px] text-slate-600 mt-0.5">resolvidos</div>
            </button>
            <button
              onClick={() => setDrill('wins')}
              className="text-left bg-emerald-500/5 border border-emerald-500/30 hover:border-emerald-400 rounded-lg p-3 transition-colors"
              title="Ver vencedores e por quê venceram"
            >
              <div className="text-[10px] text-emerald-400/70 uppercase">Vencedores</div>
              <div className="text-xl font-bold text-emerald-300 font-mono">{s.wins}</div>
              <div className="text-[10px] text-emerald-400/60 mt-0.5">{fmtPct(pctBreakdown.wins)}</div>
            </button>
            <button
              onClick={() => setDrill('losses')}
              className="text-left bg-red-500/5 border border-red-500/30 hover:border-red-400 rounded-lg p-3 transition-colors"
              title="Ver perdedores e por quê perderam"
            >
              <div className="text-[10px] text-red-400/70 uppercase">Perdedores</div>
              <div className="text-xl font-bold text-red-300 font-mono">{s.losses}</div>
              <div className="text-[10px] text-red-400/60 mt-0.5">{fmtPct(pctBreakdown.losses)}</div>
            </button>
            <button
              onClick={() => setDrill('open')}
              className="text-left bg-slate-900/60 border border-slate-800 hover:border-slate-600 rounded-lg p-3 transition-colors"
              title="Ver trades em aberto"
            >
              <div className="text-[10px] text-slate-500 uppercase">Abertos</div>
              <div className="text-xl font-bold text-slate-300 font-mono">{s.still_open}</div>
              <div className="text-[10px] text-slate-600 mt-0.5">aguardando</div>
            </button>
          </div>
        )}

        {/* Lista */}
        <div className="flex-1 overflow-y-auto">
          {loading && !data && (
            <div className="flex items-center justify-center py-20">
              <div className="w-8 h-8 border-2 border-emerald-500 border-t-transparent rounded-full animate-spin" />
            </div>
          )}

          {data?.enabled && (!data.trades || data.trades.length === 0) && (
            <div className="flex flex-col items-center justify-center py-16 px-4 text-center gap-2">
              <Clock className="w-10 h-10 text-slate-600" />
              <p className="text-sm text-slate-300 font-semibold">Nenhum trade resolvido neste dia</p>
              <p className="text-xs text-slate-500 max-w-md">
                Recomendações aparecem aqui depois que o preço bater o TP ou o stop.
                Cheque amanhã ou abra o painel ✨ Recomendados pra gerar novos snapshots.
              </p>
            </div>
          )}

          <div className="flex flex-col gap-1 p-3">
            {data?.trades?.map((t, i) => {
              const badge = STATUS_BADGE[t.status] || STATUS_BADGE.open
              const isLong = t.direction === 'long'
              const DirIcon = isLong ? TrendingUp : TrendingDown
              const r = t.realized_r ?? 0
              const rText = r > 0 ? `+${r.toFixed(1)}R` : r < 0 ? `${r.toFixed(1)}R` : '–'
              const opType = operationType(t.timeframe)
              return (
                <div key={i} className="flex items-center gap-3 p-2.5 rounded-lg border border-slate-800 bg-slate-900/40 hover:bg-slate-900/80 transition-colors">
                  <span className={`px-2 py-0.5 rounded text-[10px] font-bold border ${badge.cls}`}>{badge.label}</span>
                  <DirIcon className={`w-4 h-4 ${isLong ? 'text-green-400' : 'text-red-400'}`} />
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="text-sm font-bold text-white">{t.symbol.split('/')[0]}</span>
                      <span className="text-[10px] text-slate-500 font-mono">{t.timeframe}</span>
                      <span className="text-[9px] text-slate-400 px-1.5 py-0.5 rounded border border-slate-700">{opType}</span>
                      <span className="text-[10px] text-orange-300 font-mono">{t.leverage}x</span>
                      <span className="text-[10px] text-slate-500">tier {t.tier}</span>
                    </div>
                    <div className="text-[10px] text-slate-500 font-mono mt-0.5">
                      entry {fmt(t.entry)} → stop {fmt(t.stop_loss)}
                      {t.tp1 != null && <> · tp1 {fmt(t.tp1)}</>}
                      {' · '}tp2 {fmt(t.tp2)}
                    </div>
                  </div>
                  <div className={`text-right font-mono text-sm font-bold ${r > 0 ? 'text-emerald-300' : r < 0 ? 'text-red-300' : 'text-slate-400'}`}>
                    {rText}
                    <div className="text-[10px] text-slate-600 font-sans font-normal">
                      {fmtPct(r * t.risk_pct)}
                    </div>
                  </div>
                </div>
              )
            })}
          </div>
        </div>

        {/* Footer */}
        <div className="px-4 py-2 border-t border-slate-800 bg-slate-900/60 text-[10px] text-slate-500 leading-relaxed">
          <strong className="text-slate-400">R:</strong> múltiplo de risco com gestão parcial:
          +1.5R = TP2 cheio (50% TP1 + 50% TP2) · +0.5R = breakeven após TP1 (50% TP1 + 50% trail/BE) · −1R = stop original.
          A coluna de <strong>% da banca</strong> soma o impacto real por trade (cada tier tem seu risco_pct: A+ 1.5% / A 1% / B 0.5%).
          <br />
          Tracker verifica preço a cada 5 min · stop trail por ATR após TP1 · snapshots expiram em 48h ·
          <span className="text-slate-400"> clique nos cards pra detalhar.</span>
        </div>
      </div>
    </div>
  )
}
