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

interface ViabilityItem {
  id: number
  symbol: string
  timeframe: string
  direction: 'long' | 'short'
  tier: string
  entry: number
  current_price: number
  distance_atr: number | null
  stop_progress_pct: number
  age_hours: number
  tp1_hit: boolean
  viability: 'valid' | 'wait' | 'missed' | 'tp1_done'
  advice: string
  created_at: string | null
}

const VIABILITY_BADGE: Record<string, { label: string; cls: string }> = {
  valid:    { label: '🟢 viável',    cls: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/40' },
  wait:     { label: '🟡 aguarde pullback', cls: 'bg-yellow-500/15 text-yellow-300 border-yellow-500/40' },
  missed:   { label: '🔴 perdeu o trem',    cls: 'bg-red-500/15 text-red-300 border-red-500/40' },
  tp1_done: { label: '🔵 TP1 hit',   cls: 'bg-sky-500/15 text-sky-300 border-sky-500/40' },
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
  won_tp2:    { label: '🏆 TP2',     cls: 'bg-emerald-500/20 text-emerald-300 border-emerald-500/40' },
  won_tp1:    { label: '✅ TP1',     cls: 'bg-green-500/20 text-green-300 border-green-500/40' },
  won_tp1_be: { label: '✅ TP1+BE',  cls: 'bg-sky-500/20 text-sky-300 border-sky-500/40' },
  lost:       { label: '✗ STOP',     cls: 'bg-red-500/20 text-red-300 border-red-500/40' },
  open:       { label: 'aberto',     cls: 'bg-slate-500/20 text-slate-300 border-slate-500/40' },
  expired:    { label: '⏱ expirado', cls: 'bg-yellow-500/20 text-yellow-300 border-yellow-500/40' },
}

const STATUS_REASON: Record<string, string> = {
  won_tp2: 'Preço atingiu o TP2 — 50% da posição saiu em TP1 (+0.5R) e 50% em TP2 (+1.0R). Total +1.5R.',
  won_tp1: 'Preço atingiu TP1 (50% saiu, +0.5R) e snapshot expirou em BE+ lock nos 50% restantes (+0.1R). Total +0.6R.',
  won_tp1_be: 'Preço atingiu TP1 (50% saiu, +0.5R), depois retraiu e bateu o BE+ lock (entry + 0.2R) nos 50% restantes (+0.1R). Total +0.6R. Trail v2 ampliado evita whipsaw.',
  lost: 'Preço bateu o stop ANTES de tocar TP1 (sem parcial). Perda total −1R.',
  expired: 'Time-stop por TF (15m=4h, 1h=12h, 4h=36h) sem tocar TP1 nem stop. Sem perda, sem ganho — capital liberado.',
  open: 'Tracker monitorando — aguardando preço tocar TP1, TP2 ou stop.',
}

type DrillKind = 'wins' | 'losses' | 'open' | 'open_today' | 'open_older' | 'all' | null

export default function DailyPnLPanel({ onClose }: Props) {
  const [date, setDate] = useState(todayISO())
  const [endDate, setEndDate] = useState(todayISO())
  const [data, setData] = useState<DailyPnL | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [drill, setDrill] = useState<DrillKind>(null)
  const [viability, setViability] = useState<Record<number, ViabilityItem>>({})
  const isRange = date !== endDate

  const load = useCallback(async (d: string, end: string) => {
    setLoading(true)
    setError(null)
    try {
      const url = d === end
        ? `${BACKEND}/api/daily-pnl?date=${d}`
        : `${BACKEND}/api/daily-pnl?date=${d}&end_date=${end}`
      const res = await fetch(url)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      setData(await res.json())
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Erro')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load(date, endDate) }, [load, date, endDate])

  // Fetch viability dos abertos sempre que data muda (viability é estado
  // atual — só relevante se range inclui hoje). Cache local — refresca
  // quando recarrega.
  useEffect(() => {
    const today = todayISO()
    if (endDate < today && date < today) return
    let alive = true
    ;(async () => {
      try {
        const res = await fetch(`${BACKEND}/api/snapshots/open-viability`)
        if (!res.ok) return
        const json = await res.json()
        if (!alive || !json.enabled) return
        const map: Record<number, ViabilityItem> = {}
        for (const it of (json.items ?? []) as ViabilityItem[]) map[it.id] = it
        setViability(map)
      } catch { /* fail-silent */ }
    })()
    return () => { alive = false }
  }, [date, endDate, data?.summary?.still_open])

  // Separa abertos em "hoje" (criados no dia atual) vs "anteriores"
  const openListAll = data?.open_trades ?? []
  const todayStr = todayISO()
  const openToday = useMemo(
    () => openListAll.filter(t => t.created_at?.slice(0, 10) === todayStr),
    [openListAll, todayStr]
  )
  const openOlder = useMemo(
    () => openListAll.filter(t => t.created_at?.slice(0, 10) !== todayStr),
    [openListAll, todayStr]
  )

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
  // ── Drill-down render ────────────────────────────────────────────────────
  if (drill !== null && data) {
    const title =
      drill === 'wins' ? 'Vencedores do dia' :
      drill === 'losses' ? 'Perdedores do dia' :
      drill === 'open' ? 'Trades em aberto (todos)' :
      drill === 'open_today' ? 'Abertos · criados hoje' :
      drill === 'open_older' ? 'Abertos · dias anteriores' :
      'Todos os trades'
    const list =
      drill === 'wins' ? winsList :
      drill === 'losses' ? lossesList :
      drill === 'open' ? openListAll :
      drill === 'open_today' ? openToday :
      drill === 'open_older' ? openOlder :
      [...(data.trades ?? []), ...openListAll]
    const subtitle =
      drill === 'wins' ? `${winsList.length} trade(s) · ${fmtPct(pctBreakdown.wins)} da banca` :
      drill === 'losses' ? `${lossesList.length} trade(s) · ${fmtPct(pctBreakdown.losses)} da banca` :
      drill === 'open' ? `${openListAll.length} aguardando preço bater TP ou stop` :
      drill === 'open_today' ? `${openToday.length} trade(s) abertos no dia de hoje` :
      drill === 'open_older' ? `${openOlder.length} trade(s) de dias anteriores · avaliando viabilidade atual` :
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
                // Match viability por símbolo+tf+direction+entry (snapshot.id não está no payload de daily-pnl)
                const viab = t.status === 'open'
                  ? Object.values(viability).find(v =>
                      v.symbol === t.symbol && v.timeframe === t.timeframe &&
                      v.direction === t.direction && Math.abs(v.entry - t.entry) < 1e-8
                    )
                  : undefined
                const vBadge = viab ? VIABILITY_BADGE[viab.viability] : null
                return (
                  <div key={i} className="p-3 rounded-lg border border-slate-800 bg-slate-900/40">
                    <div className="flex items-center gap-2 flex-wrap mb-2">
                      <span className={`px-2 py-0.5 rounded text-[10px] font-bold border ${badge.cls}`}>{badge.label}</span>
                      {vBadge && (
                        <span className={`px-2 py-0.5 rounded text-[10px] font-bold border ${vBadge.cls}`} title={viab?.advice}>
                          {vBadge.label}
                        </span>
                      )}
                      <DirIcon className={`w-4 h-4 ${isLong ? 'text-green-400' : 'text-red-400'}`} />
                      <span className="text-sm font-bold text-white">{t.symbol.split('/')[0]}</span>
                      <span className="text-[10px] text-slate-500 font-mono">{t.timeframe}</span>
                      <span className="text-[10px] text-slate-400 px-1.5 py-0.5 rounded border border-slate-700">{opType}</span>
                      <span className="text-[10px] text-orange-300 font-mono">{t.leverage}x</span>
                      <span className="text-[10px] text-slate-500">tier {t.tier}</span>
                      <span className="ml-auto font-mono text-sm font-bold text-right">
                        <span className={r > 0 ? 'text-emerald-300' : r < 0 ? 'text-red-300' : 'text-purple-300'}>
                          {r > 0 ? `+${r.toFixed(1)}R` : r < 0 ? `${r.toFixed(1)}R` : (t.score != null ? `score ${t.score.toFixed(0)}` : '–')}
                        </span>
                        <span className="block text-[10px] text-slate-500 font-normal font-sans">
                          {t.status !== 'open' && fmtPct(pct)}
                        </span>
                      </span>
                    </div>

                    <div className="grid grid-cols-3 sm:grid-cols-5 gap-2 text-[11px] mb-2">
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
                      {viab && (
                        <div>
                          <div className="text-slate-600 text-[9px]">Preço atual</div>
                          <div className="font-mono text-sky-300">{fmt(viab.current_price)}</div>
                        </div>
                      )}
                    </div>

                    {viab && (
                      <div className="mb-2 text-[10px] flex items-center gap-2 flex-wrap">
                        {viab.distance_atr !== null && (
                          <span className="text-slate-500">
                            dist:{' '}
                            <span className={
                              viab.distance_atr >= 1.0 ? 'text-red-400' :
                              viab.distance_atr >= 0.5 ? 'text-yellow-400' :
                              'text-emerald-400'
                            }>
                              {viab.distance_atr >= 0 ? '+' : ''}{viab.distance_atr}×ATR
                            </span>
                          </span>
                        )}
                        {viab.stop_progress_pct >= 0 ? (
                          <span className="text-slate-500">rumo ao stop: <span className="text-slate-300">{viab.stop_progress_pct.toFixed(0)}%</span></span>
                        ) : (
                          <span className="text-emerald-400">stop longe ✓</span>
                        )}
                        <span className="text-slate-500">há <span className="text-slate-300">{viab.age_hours.toFixed(1)}h</span></span>
                      </div>
                    )}
                    {viab && (
                      <p className="text-[10px] text-slate-300 leading-snug bg-slate-950/50 border border-slate-800 rounded p-2 mb-2">
                        💡 {viab.advice}
                      </p>
                    )}

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

        {/* Header — responsivo: 1 linha desktop, 2 linhas mobile (título+X em cima, controles embaixo) */}
        <div className="border-b border-slate-800 bg-gradient-to-r from-slate-900 to-slate-800">
          {/* Linha 1: título + close (X sempre visível, mesmo no mobile) */}
          <div className="flex items-center justify-between px-4 py-3 gap-2">
            <div className="flex items-center gap-2 min-w-0">
              <BarChart3 className="w-5 h-5 text-emerald-400 shrink-0" />
              <h2 className="text-base font-bold text-white truncate">{isRange ? 'Resultado do Período' : 'Resultado do Dia'}</h2>
              <span className="text-xs text-slate-500 hidden sm:inline">· P&L das recomendações</span>
            </div>
            <div className="flex items-center gap-1 shrink-0">
              {/* Controles inline no desktop (sm+) */}
              <div className="hidden sm:flex items-center gap-2">
                <div className="flex items-center gap-1 bg-slate-800 border border-slate-700 rounded px-2 py-1">
                  <Calendar className="w-3.5 h-3.5 text-slate-400" />
                  <input
                    type="date"
                    value={date}
                    onChange={e => {
                      const v = e.target.value
                      setDate(v)
                      if (endDate < v) setEndDate(v)
                    }}
                    className="bg-transparent text-xs text-slate-200 outline-none"
                    max={todayISO()}
                    title="Data inicial"
                  />
                  <span className="text-slate-600 text-xs">→</span>
                  <input
                    type="date"
                    value={endDate}
                    onChange={e => setEndDate(e.target.value)}
                    className="bg-transparent text-xs text-slate-200 outline-none"
                    min={date}
                    max={todayISO()}
                    title="Data final"
                  />
                </div>
                {(date !== todayISO() || endDate !== todayISO()) && (
                  <button
                    onClick={() => { setDate(todayISO()); setEndDate(todayISO()) }}
                    className="px-2 py-1 bg-slate-800 hover:bg-slate-700 border border-slate-700 rounded text-[10px] text-slate-300"
                    title="Voltar pra hoje"
                  >Hoje</button>
                )}
                <button onClick={() => load(date, endDate)} disabled={loading}
                  className="flex items-center gap-1 px-2 py-1 bg-slate-800 hover:bg-slate-700 border border-slate-700 rounded text-xs text-slate-300 disabled:opacity-50">
                  <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
                </button>
              </div>
              {/* X close — sempre visível, shrink-0 */}
              <button onClick={onClose} className="p-2 hover:bg-slate-800 rounded shrink-0" aria-label="Fechar">
                <X className="w-5 h-5 text-slate-300" />
              </button>
            </div>
          </div>
          {/* Linha 2: controles no mobile (sm:hidden) */}
          <div className="flex sm:hidden items-center gap-2 px-4 pb-3 flex-wrap">
            <div className="flex items-center gap-1 bg-slate-800 border border-slate-700 rounded px-2 py-1 flex-1 min-w-0">
              <Calendar className="w-3.5 h-3.5 text-slate-400 shrink-0" />
              <input
                type="date"
                value={date}
                onChange={e => {
                  const v = e.target.value
                  setDate(v)
                  if (endDate < v) setEndDate(v)
                }}
                className="bg-transparent text-xs text-slate-200 outline-none min-w-0 flex-1"
                max={todayISO()}
              />
              <span className="text-slate-600 text-xs shrink-0">→</span>
              <input
                type="date"
                value={endDate}
                onChange={e => setEndDate(e.target.value)}
                className="bg-transparent text-xs text-slate-200 outline-none min-w-0 flex-1"
                min={date}
                max={todayISO()}
              />
            </div>
            {(date !== todayISO() || endDate !== todayISO()) && (
              <button
                onClick={() => { setDate(todayISO()); setEndDate(todayISO()) }}
                className="px-2 py-1 bg-slate-800 hover:bg-slate-700 border border-slate-700 rounded text-[10px] text-slate-300 shrink-0"
              >Hoje</button>
            )}
            <button onClick={() => load(date, endDate)} disabled={loading}
              className="flex items-center gap-1 px-2 py-1 bg-slate-800 hover:bg-slate-700 border border-slate-700 rounded text-xs text-slate-300 disabled:opacity-50 shrink-0">
              <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
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
              type="button"
              onClick={(e) => { e.preventDefault(); e.stopPropagation(); setDrill('all') }}
              className="text-left bg-slate-900/60 border border-slate-800 hover:border-slate-600 rounded-lg p-3 transition-colors cursor-pointer"
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
              type="button"
              onClick={(e) => { e.preventDefault(); e.stopPropagation(); setDrill('all') }}
              className="text-left bg-slate-900/60 border border-slate-800 hover:border-slate-600 rounded-lg p-3 transition-colors cursor-pointer"
              title="Ver desempenho"
            >
              <div className="text-[10px] text-slate-500 uppercase">Win Rate</div>
              <div className="text-xl font-bold text-white font-mono">{s.win_rate_pct.toFixed(1)}%</div>
              <div className="text-[10px] text-slate-600 mt-0.5">{s.wins}W · {s.losses}L</div>
            </button>
            <button
              type="button"
              onClick={(e) => { e.preventDefault(); e.stopPropagation(); setDrill('all') }}
              className="text-left bg-slate-900/60 border border-slate-800 hover:border-slate-600 rounded-lg p-3 transition-colors cursor-pointer"
              title="Ver todos os trades resolvidos"
            >
              <div className="text-[10px] text-slate-500 uppercase">Trades</div>
              <div className="text-xl font-bold text-white font-mono">{s.total_trades}</div>
              <div className="text-[10px] text-slate-600 mt-0.5">resolvidos</div>
            </button>
            <button
              type="button"
              onClick={(e) => { e.preventDefault(); e.stopPropagation(); setDrill('wins') }}
              className="text-left bg-emerald-500/5 border border-emerald-500/30 hover:border-emerald-400 rounded-lg p-3 transition-colors cursor-pointer"
              title="Ver vencedores e por quê venceram"
            >
              <div className="text-[10px] text-emerald-400/70 uppercase">Vencedores</div>
              <div className="text-xl font-bold text-emerald-300 font-mono">{s.wins}</div>
              <div className="text-[10px] text-emerald-400/60 mt-0.5">{fmtPct(pctBreakdown.wins)}</div>
            </button>
            <button
              type="button"
              onClick={(e) => { e.preventDefault(); e.stopPropagation(); setDrill('losses') }}
              className="text-left bg-red-500/5 border border-red-500/30 hover:border-red-400 rounded-lg p-3 transition-colors cursor-pointer"
              title="Ver perdedores e por quê perderam"
            >
              <div className="text-[10px] text-red-400/70 uppercase">Perdedores</div>
              <div className="text-xl font-bold text-red-300 font-mono">{s.losses}</div>
              <div className="text-[10px] text-red-400/60 mt-0.5">{fmtPct(pctBreakdown.losses)}</div>
            </button>
            <button
              type="button"
              onClick={(e) => { e.preventDefault(); e.stopPropagation(); setDrill('open_today') }}
              className="text-left bg-slate-900/60 border border-slate-800 hover:border-slate-600 rounded-lg p-3 transition-colors cursor-pointer"
              title="Trades abertos criados hoje"
            >
              <div className="text-[10px] text-slate-500 uppercase">Abertos · hoje</div>
              <div className="text-xl font-bold text-slate-300 font-mono">{openToday.length}</div>
              <div className="text-[10px] text-slate-600 mt-0.5">criados hoje</div>
            </button>
            {openOlder.length > 0 && (
              <button
                type="button"
                onClick={(e) => { e.preventDefault(); e.stopPropagation(); setDrill('open_older') }}
                className="text-left bg-amber-500/5 border border-amber-500/30 hover:border-amber-400 rounded-lg p-3 transition-colors cursor-pointer"
                title="Trades abertos de dias anteriores — clique pra ver viabilidade"
              >
                <div className="text-[10px] text-amber-400/70 uppercase">Abertos · anteriores</div>
                <div className="text-xl font-bold text-amber-300 font-mono">{openOlder.length}</div>
                <div className="text-[10px] text-amber-400/60 mt-0.5">avaliando viabilidade</div>
              </button>
            )}
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
          <strong className="text-slate-400">R:</strong> múltiplo de risco com gestão parcial v2:
          <span className="text-emerald-400"> +1.5R = TP2</span> (50% TP1 + 50% TP2) ·
          <span className="text-sky-400"> +0.6R = TP1+BE+</span> (50% TP1 + 50% lock 0.2R) ·
          <span className="text-red-400"> −1R = stop</span>.
          A coluna de <strong>% da banca</strong> soma o impacto real por trade (A+ 1.5% / A 1% / B 0.5%).
          <br />
          Tracker checa a cada 5 min · trail v2 com K=2.2 ATR e buffer de 0.5×ATR pós-TP1 · snapshots expiram em 48h ·
          <span className="text-slate-400"> clique nos cards pra detalhar.</span>
        </div>
      </div>
    </div>
  )
}
