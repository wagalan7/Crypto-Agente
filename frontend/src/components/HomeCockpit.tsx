import { useState, useEffect, useCallback, useRef } from 'react'
import { X, RefreshCw } from 'lucide-react'
import { api } from '../services/api'
import type { RealTradeRow, Recommendation } from '../types'

const BACKEND = import.meta.env.VITE_API_URL ?? 'https://crypto-agente-production.up.railway.app'

// ─── Shapes dos endpoints reusados (mesmos que StatusPanel/DailyPnLPanel usam) ─
interface RiskStatus {
  enabled: boolean
  trading_paused: boolean
  pause_reason: string | null
  pause_manual?: boolean
  paused_at?: string | null
  daily_dd_pct: number
  weekly_dd_pct: number
  daily_limit_pct: number
  weekly_limit_pct: number
}
interface RegimeStatus {
  regime: string
  btc_24h_pct: number | null
  btc_dominance: number | null
  btc_trend_pct?: number | null
  block_all?: boolean
  block_alt_longs?: boolean
  downgrade_alt_longs?: boolean
  downgrade_shorts?: boolean
  block_shorts?: boolean
  reasons?: string[]
}
interface DailySummary {
  total_trades: number
  wins: number
  losses: number
  win_rate_pct: number
  total_r: number
  total_pct_banca?: number
  still_open: number
}
interface PaperSummary {
  enabled: boolean
  equity: { final_pnl_pct: number; trades_total: number; curve: { date: string; cumulative_pct: number }[] }
}
interface HealthStatus {
  enabled: boolean
  status: 'healthy' | 'degraded' | 'unknown'
  gap_seconds: number | null
  last_source: string | null
}
interface MacroData {
  btc_direction: string
  btc_dominance: number | null
  context_text: string
}

interface Props {
  onClose: () => void
  onSelectSymbol?: (symbol: string, timeframe?: string) => void
  onOpenRecs?: () => void
  onOpenTrades?: () => void
  onOpenStatus?: () => void
}

// ─── Helpers ──────────────────────────────────────────────────────────────────
function todayUtc(): string {
  return new Date().toISOString().slice(0, 10)
}
function toBinance(symbol: string): string {
  return symbol.split(':')[0].replace('/', '')
}
function fmtUsd(v: number | null | undefined): string {
  if (v == null) return '—'
  const s = Math.abs(v) >= 1000 ? v.toLocaleString('pt-BR', { maximumFractionDigits: 0 }) : v.toFixed(2)
  return `${v >= 0 ? '+' : '-'}$${s.replace('-', '')}`
}
function fmtPrice(p: number): string {
  if (p >= 1000) return p.toLocaleString('pt-BR', { maximumFractionDigits: 2 })
  if (p >= 1) return p.toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 4 })
  return p.toLocaleString('pt-BR', { minimumFractionDigits: 4, maximumFractionDigits: 6 })
}
function clamp01(x: number): number {
  return Math.max(0, Math.min(1, x))
}
function baseOf(symbol: string): string {
  return symbol.split('/')[0].replace(':USDT', '')
}

// Preço ao vivo (Binance Futures 24hr) só p/ os símbolos abertos — best-effort.
async function fetchLivePrices(symbols: string[]): Promise<Map<string, number>> {
  const out = new Map<string, number>()
  if (symbols.length === 0) return out
  try {
    const res = await fetch('https://fapi.binance.com/fapi/v1/ticker/24hr', { signal: AbortSignal.timeout(9000) })
    if (!res.ok) return out
    const rows: { symbol: string; lastPrice: string }[] = await res.json()
    const wanted = new Set(symbols.map(toBinance))
    rows.forEach(r => { if (wanted.has(r.symbol)) out.set(r.symbol, parseFloat(r.lastPrice)) })
  } catch { /* sem preço ao vivo → cai no realized_r */ }
  return out
}

// R ao vivo a partir do preço atual (não realizado). Fallback: realized_r.
function liveR(t: RealTradeRow, price: number | undefined): number | null {
  if (price == null || t.planned_stop == null) return t.realized_r ?? null
  const risk = Math.abs(t.entry_price - t.planned_stop)
  if (risk <= 0) return t.realized_r ?? null
  const dir = String(t.side).toLowerCase() === 'short' ? -1 : 1
  return ((price - t.entry_price) * dir) / risk
}

// Fração 0..1 do preço no trilho SL──entrada──preço──TP (long e short).
function trackFrac(t: RealTradeRow, price: number | undefined): { price: number | null; entry: number } {
  const stop = t.planned_stop
  const tp = t.planned_tp2 ?? t.planned_tp1
  if (stop == null || tp == null || stop === tp) return { price: null, entry: 0.26 }
  const lo = Math.min(stop, tp), hi = Math.max(stop, tp)
  const norm = (v: number) => clamp01((v - lo) / (hi - lo))
  // Para short, SL fica à direita → invertemos p/ "andar pra direita = a favor".
  const isShort = String(t.side).toLowerCase() === 'short'
  const flip = (f: number) => (isShort ? 1 - f : f)
  return {
    price: price != null ? flip(norm(price)) : null,
    entry: flip(norm(t.entry_price)),
  }
}

export default function HomeCockpit({ onClose, onSelectSymbol, onOpenRecs, onOpenTrades, onOpenStatus }: Props) {
  const [risk, setRisk] = useState<RiskStatus | null>(null)
  const [summary, setSummary] = useState<DailySummary | null>(null)
  const [positions, setPositions] = useState<RealTradeRow[]>([])
  const [prices, setPrices] = useState<Map<string, number>>(new Map())
  const [recs, setRecs] = useState<Recommendation[]>([])
  const [paper, setPaper] = useState<PaperSummary | null>(null)
  const [health, setHealth] = useState<HealthStatus | null>(null)
  const [macro, setMacro] = useState<MacroData | null>(null)
  const [regime, setRegime] = useState<RegimeStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [resuming, setResuming] = useState(false)
  const [clock, setClock] = useState(new Date())
  const firstLoad = useRef(true)

  useEffect(() => {
    const id = setInterval(() => setClock(new Date()), 1000)
    return () => clearInterval(id)
  }, [])

  const load = useCallback(async () => {
    if (firstLoad.current) setLoading(true)
    else setRefreshing(true)
    // Cada fonte é best-effort: falha de uma não derruba a Home.
    const settle = <T,>(p: Promise<T>) => p.then(v => v).catch(() => null)

    const [riskJson, dailyJson, tradesRes, recsRes, paperJson, healthJson, macroJson, regimeJson] = await Promise.all([
      settle(fetch(`${BACKEND}/api/risk/status`).then(r => r.ok ? r.json() : null)),
      settle(fetch(`${BACKEND}/api/daily-pnl?date=${todayUtc()}`).then(r => r.ok ? r.json() : null)),
      settle(api.listRealTrades({ status: 'open', limit: 50 })),
      settle(api.recommendations(6)),
      settle(fetch(`${BACKEND}/api/paper/summary?days=30`).then(r => r.ok ? r.json() : null)),
      settle(fetch(`${BACKEND}/api/admin/health`).then(r => r.ok ? r.json() : null)),
      settle(api.macro('BTC/USDT:USDT')),
      settle(fetch(`${BACKEND}/api/regime-status`).then(r => r.ok ? r.json() : null)),
    ])

    if (riskJson && riskJson.enabled !== false) setRisk(riskJson as RiskStatus)
    if (regimeJson) setRegime(regimeJson as RegimeStatus)
    if (dailyJson?.summary) setSummary(dailyJson.summary as DailySummary)
    const openTrades = (tradesRes?.trades ?? []) as RealTradeRow[]
    setPositions(openTrades)
    if (recsRes?.recommendations) setRecs(recsRes.recommendations.slice(0, 3))
    if (paperJson && paperJson.enabled !== false) setPaper(paperJson as PaperSummary)
    if (healthJson && healthJson.enabled !== false) setHealth(healthJson as HealthStatus)
    if (macroJson) setMacro(macroJson as MacroData)

    if (openTrades.length > 0) {
      const pm = await fetchLivePrices(openTrades.map(t => t.symbol))
      if (pm.size > 0) setPrices(pm)
    }

    firstLoad.current = false
    setLoading(false)
    setRefreshing(false)
  }, [])

  useEffect(() => {
    load()
    const id = setInterval(load, 20_000)
    return () => clearInterval(id)
  }, [load])

  // Retomar trading após pausa do circuit breaker (kill switch → paused=false).
  // Ação sensível: confirma antes. Só o próprio usuário aciona clicando aqui.
  const resumeTrading = useCallback(async () => {
    if (!window.confirm('Retomar o trading agora? Isto desliga a pausa do circuit breaker.')) return
    setResuming(true)
    try {
      const res = await fetch(`${BACKEND}/api/risk/kill-switch?paused=false`, { method: 'POST' })
      if (res.ok) {
        const j = await res.json().catch(() => null)
        if (j && j.enabled !== false) setRisk(j as RiskStatus)
        await load()
      } else {
        window.alert('Não consegui retomar agora. Tente pela tela de Risco.')
      }
    } catch {
      window.alert('Falha de rede ao retomar. Tente novamente.')
    } finally {
      setResuming(false)
    }
  }, [load])

  const totalR = summary?.total_r ?? 0
  const pnlPct = summary?.total_pct_banca ?? null
  const pnlPositive = totalR >= 0

  // Split de direção das recomendações visíveis (long vs short)
  const recsLong = recs.filter(r => r.direction !== 'short').length
  const recsShort = recs.filter(r => r.direction === 'short').length

  // Freio de regime ativo (trava simétrica de short / downgrade de long)
  const regimeBrake = !!regime && (
    regime.block_all || regime.block_alt_longs || regime.downgrade_alt_longs ||
    regime.downgrade_shorts || regime.block_shorts
  )
  const regimeMsg = (() => {
    if (!regime) return null
    if (regime.block_shorts) return 'Pernada forte de alta — shorts bloqueados'
    if (regime.downgrade_shorts) return 'Pernada de alta — shorts rebaixados (não short contra a tendência)'
    if (regime.block_alt_longs) return 'Capital migrando p/ BTC — longs de alt bloqueados'
    if (regime.downgrade_alt_longs) return 'Dominância BTC alta — longs de alt rebaixados'
    if (regime.block_all) return 'Risk-off — novas entradas bloqueadas'
    return null
  })()

  // Curva de capital → pontos p/ o sparkline
  const curve = paper?.equity?.curve ?? []
  const sparkPath = (() => {
    if (curve.length < 2) return null
    const vals = curve.map(c => c.cumulative_pct)
    const min = Math.min(...vals), max = Math.max(...vals)
    const span = max - min || 1
    const W = 600, H = 60
    const pts = vals.map((v, i) => {
      const x = (i / (vals.length - 1)) * W
      const y = H - ((v - min) / span) * H
      return `${x.toFixed(1)},${y.toFixed(1)}`
    })
    const line = `M${pts.join(' L')}`
    const area = `${line} L${W},${H + 10} L0,${H + 10} Z`
    return { line, area }
  })()

  return (
    <div className="fixed inset-0 z-50 bg-[#0a0e1a] text-white overflow-y-auto lg:pl-16 pb-16 lg:pb-4">
      <div className="max-w-[1180px] mx-auto p-4 sm:p-6">

        {/* Header */}
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2.5">
            <img src="/logo.jpg" alt="Crypto Win" className="w-8 h-8 rounded-md object-cover border border-yellow-500/40 shadow-[0_0_8px_rgba(234,179,8,0.25)]" />
            <div className="flex flex-col leading-tight">
              <h1 className="text-[15px] font-bold bg-gradient-to-r from-yellow-300 to-emerald-300 bg-clip-text text-transparent">Crypto Win</h1>
              <span className="text-[11px] text-slate-500">Visão geral do dia</span>
            </div>
          </div>
          <div className="flex items-center gap-3">
            <span className="text-xs text-slate-500 font-mono tabular-nums hidden sm:block">{clock.toLocaleTimeString('pt-BR')}</span>
            <button onClick={load} className="p-1.5 rounded-lg hover:bg-slate-800 text-slate-400" title="Atualizar">
              <RefreshCw className={`w-4 h-4 ${refreshing ? 'animate-spin' : ''}`} />
            </button>
            <button onClick={onClose} className="p-1.5 rounded-lg hover:bg-slate-800 text-slate-400" title="Fechar">
              <X className="w-5 h-5" />
            </button>
          </div>
        </div>

        {loading ? (
          <div className="flex flex-col items-center justify-center py-24 gap-3">
            <div className="w-8 h-8 border-2 border-emerald-500 border-t-transparent rounded-full animate-spin" />
            <span className="text-sm text-slate-500">Carregando cockpit...</span>
          </div>
        ) : (
          <>
            {/* ── STATUS STRIP ── */}
            <div className="flex flex-wrap items-center gap-x-5 gap-y-3 bg-[#0f1524] border border-slate-800 rounded-2xl px-4 py-3 mb-4">
              <span className="inline-flex items-center gap-2 text-[12.5px] font-semibold">
                <span className={`w-2.5 h-2.5 rounded-full ${risk?.trading_paused ? 'bg-red-500' : 'bg-green-500 animate-pulse'}`} />
                {risk?.trading_paused ? 'Pausado' : 'Operando'}
              </span>
              <div className="w-px h-6 bg-slate-800 hidden sm:block" />
              {(['daily', 'weekly'] as const).map(k => {
                // dd = P&L acumulado da janela COM SINAL (positivo = ganho,
                // negativo = drawdown). A trava (lim) é negativa: -3% dia / -6% semana.
                const dd = k === 'daily' ? (risk?.daily_dd_pct ?? 0) : (risk?.weekly_dd_pct ?? 0)
                const lim = k === 'daily' ? (risk?.daily_limit_pct ?? -3) : (risk?.weekly_limit_pct ?? -6)
                // Barra mede só o quão perto da trava, e SÓ quando há drawdown real.
                const frac = lim < 0 && dd < 0 ? clamp01(dd / lim) * 100 : 0
                const warn = frac >= 45
                const gain = dd >= 0
                return (
                  <div key={k} className="flex flex-col gap-1 min-w-[150px] flex-1">
                    <span className="flex justify-between text-[11px] text-slate-400">
                      Resultado {k === 'daily' ? 'hoje' : 'semana'}
                      <b className="tabular-nums">
                        <span className={gain ? 'text-green-400' : 'text-red-400'}>{gain ? '+' : ''}{dd.toFixed(1)}%</span>
                        <span className="text-slate-500 font-normal"> · trava {lim.toFixed(0)}%</span>
                      </b>
                    </span>
                    <span className="h-1.5 rounded-full bg-[#0b1220] border border-slate-800 overflow-hidden">
                      <i className={`block h-full rounded-full ${warn ? 'bg-gradient-to-r from-red-500 to-amber-500' : 'bg-gradient-to-r from-emerald-500 to-green-400'}`} style={{ width: `${frac}%` }} />
                    </span>
                  </div>
                )
              })}
              <div className="w-px h-6 bg-slate-800 hidden sm:block" />
              <button onClick={onOpenStatus} className="text-[11.5px] text-slate-400 hover:text-slate-200">
                Risco &amp; circuit breaker ›
              </button>
            </div>

            {/* ── BANNER: TRADING PAUSADO (circuit breaker) ── */}
            {risk?.trading_paused && (
              <div className="flex flex-col sm:flex-row sm:items-center gap-3 bg-red-950/40 border border-red-500/40 rounded-2xl px-4 py-3 mb-4">
                <div className="flex items-start gap-2.5 flex-1 min-w-0">
                  <span className="text-lg leading-none mt-0.5">🛑</span>
                  <div className="min-w-0">
                    <div className="text-[13px] font-bold text-red-200">
                      Trading pausado {risk.pause_manual ? '(manual)' : '(circuit breaker automático)'}
                    </div>
                    <div className="text-[11.5px] text-red-300/80 mt-0.5">
                      {risk.pause_reason ?? 'Limite de drawdown atingido.'}
                      {!risk.pause_manual && ' · Retoma sozinho na virada do dia UTC quando o DD recuperar.'}
                    </div>
                  </div>
                </div>
                <button
                  onClick={resumeTrading}
                  disabled={resuming}
                  className="shrink-0 text-[12px] font-bold px-3.5 py-2 rounded-lg bg-red-500/20 hover:bg-red-500/30 border border-red-500/50 text-red-100 disabled:opacity-50"
                >
                  {resuming ? 'Retomando…' : 'Retomar agora'}
                </button>
              </div>
            )}

            {/* ── BANNER: FREIO DE REGIME (trava de short / downgrade de long) ── */}
            {!risk?.trading_paused && regimeBrake && regimeMsg && (
              <div className="flex items-start gap-2.5 bg-amber-950/30 border border-amber-500/30 rounded-2xl px-4 py-2.5 mb-4">
                <span className="text-base leading-none mt-0.5">🧭</span>
                <div className="min-w-0">
                  <div className="text-[12.5px] font-semibold text-amber-200">{regimeMsg}</div>
                  <div className="text-[11px] text-amber-300/70 mt-0.5">
                    Regime {regime?.regime}
                    {regime?.btc_trend_pct != null && ` · BTC ${regime.btc_trend_pct >= 0 ? '+' : ''}${regime.btc_trend_pct.toFixed(1)}% multi-dia`}
                    {regime?.btc_dominance != null && ` · dominância ${regime.btc_dominance.toFixed(1)}%`}
                  </div>
                </div>
              </div>
            )}

            {/* ── BENTO GRID ── */}
            <div className="grid grid-cols-1 lg:grid-cols-12 gap-3.5">

              {/* P&L HOJE */}
              <div className="bg-[#0f1524] border border-slate-800 rounded-2xl p-4 lg:col-span-4">
                <h2 className="text-[11.5px] uppercase tracking-wider text-slate-500 font-bold mb-3">💰 Resultado de hoje</h2>
                <div className={`text-[34px] font-extrabold leading-none tabular-nums ${pnlPositive ? 'text-green-400' : 'text-red-400'}`}>
                  {pnlPct != null ? `${pnlPct >= 0 ? '+' : ''}${pnlPct.toFixed(2)}%` : `${totalR >= 0 ? '+' : ''}${totalR.toFixed(2)} R`}
                </div>
                <div className="text-[12.5px] text-slate-400 mt-1.5">
                  {totalR >= 0 ? '+' : ''}{totalR.toFixed(2)} R · {summary?.total_trades ?? 0} trades resolvidos
                </div>
                <div className="flex gap-2 mt-3.5">
                  {[
                    { n: summary?.wins ?? 0, k: 'Ganhos', cls: 'text-green-400' },
                    { n: summary?.losses ?? 0, k: 'Perdas', cls: 'text-red-400' },
                    { n: summary?.win_rate_pct != null ? `${summary.win_rate_pct.toFixed(0)}%` : '—', k: 'Win rate', cls: 'text-white' },
                  ].map(c => (
                    <div key={c.k} className="flex-1 bg-[#131b2e] border border-slate-800 rounded-xl py-2 text-center">
                      <div className={`text-base font-bold tabular-nums ${c.cls}`}>{c.n}</div>
                      <div className="text-[10px] text-slate-500 uppercase tracking-wide mt-0.5">{c.k}</div>
                    </div>
                  ))}
                </div>
              </div>

              {/* POSIÇÕES ABERTAS */}
              <div className="bg-[#0f1524] border border-slate-800 rounded-2xl p-4 lg:col-span-8">
                <h2 className="text-[11.5px] uppercase tracking-wider text-slate-500 font-bold mb-3 flex items-center gap-2">
                  📡 Posições abertas · {positions.length}
                  <button onClick={onOpenTrades} className="ml-auto text-cyan-400 text-[11px] normal-case tracking-normal font-semibold">gerenciar ›</button>
                </h2>
                {positions.length === 0 ? (
                  <div className="py-8 text-center text-slate-600 text-sm">Nenhuma posição aberta agora.</div>
                ) : positions.slice(0, 5).map(t => {
                  const px = prices.get(toBinance(t.symbol))
                  const r = liveR(t, px)
                  const { price: pf, entry: ef } = trackFrac(t, px)
                  const isShort = String(t.side).toLowerCase() === 'short'
                  const rPos = (r ?? 0) >= 0
                  return (
                    <div key={t.id} className="py-3 border-b border-slate-800/60 last:border-0 cursor-pointer" onClick={() => onSelectSymbol?.(t.symbol)}>
                      <div className="flex items-center gap-2 mb-2">
                        <span className="font-bold text-sm">{baseOf(t.symbol)}</span>
                        <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded uppercase ${isShort ? 'bg-red-500/15 text-red-300 border border-red-500/30' : 'bg-green-500/15 text-green-300 border border-green-500/30'}`}>
                          {isShort ? 'Short' : 'Long'}
                        </span>
                        {t.leverage != null && <span className="text-[10.5px] text-slate-500 border border-slate-700 rounded px-1.5 py-px">{t.leverage}×</span>}
                        {t.phase && <span className="text-[10px] text-slate-500">{t.phase === 'post_tp1' ? 'pós-TP1' : t.phase}</span>}
                        <span className={`ml-auto font-bold tabular-nums text-sm ${rPos ? 'text-green-400' : 'text-red-400'}`}>
                          {r != null ? `${r >= 0 ? '+' : ''}${r.toFixed(2)} R` : (t.pnl_pct != null ? `${t.pnl_pct >= 0 ? '+' : ''}${t.pnl_pct.toFixed(2)}%` : '—')}
                        </span>
                      </div>
                      {/* trilho SL ── entrada ── preço ── TP */}
                      <div className="relative h-1.5 my-3.5 rounded-full bg-[#0b1220] border border-slate-800">
                        {pf != null && (
                          <div className={`absolute inset-y-0 left-0 rounded-full ${rPos ? 'bg-gradient-to-r from-emerald-500/25 to-green-400' : 'bg-gradient-to-r from-red-500/25 to-red-400'}`} style={{ width: `${pf * 100}%` }} />
                        )}
                        <Marker frac={0} label={isShort ? 'TP' : 'SL'} color={isShort ? 'bg-green-400' : 'bg-red-400'} />
                        <Marker frac={ef} label="Entr." color="bg-slate-400" />
                        {pf != null && <Marker frac={pf} label={px != null ? `$${fmtPrice(px)}` : ''} color="bg-white" strong />}
                        <Marker frac={1} label={isShort ? 'SL' : 'TP'} color={isShort ? 'bg-red-400' : 'bg-green-400'} />
                      </div>
                    </div>
                  )
                })}
              </div>

              {/* RECOMENDAÇÕES */}
              <div className="bg-[#0f1524] border border-slate-800 rounded-2xl p-4 lg:col-span-5">
                <h2 className="text-[11.5px] uppercase tracking-wider text-slate-500 font-bold mb-3 flex items-center gap-2">
                  ✨ Recomendações agora
                  {recs.length > 0 && (
                    <span className="normal-case tracking-normal font-semibold text-[10.5px] flex items-center gap-1.5">
                      <span className="text-green-300">{recsLong}L</span>
                      <span className="text-slate-600">·</span>
                      <span className="text-red-300">{recsShort}S</span>
                    </span>
                  )}
                  <button onClick={onOpenRecs} className="ml-auto text-cyan-400 text-[11px] normal-case tracking-normal font-semibold">ver todas ›</button>
                </h2>
                {recs.length === 0 ? (
                  <div className="py-8 text-center text-slate-600 text-sm">Sem recomendações no momento.</div>
                ) : recs.map(r => {
                  const okVerdict = r.bot_verdict?.ok ?? true
                  const isShort = r.direction === 'short'
                  const tierCls = r.tier === 'A+' ? 'bg-gradient-to-br from-green-500 to-green-700 text-[#04220f]'
                    : r.tier === 'A' ? 'bg-gradient-to-br from-blue-500 to-blue-800 text-blue-100'
                    : 'bg-slate-700 text-slate-200'
                  return (
                    <div key={`${r.symbol}-${r.timeframe}`} className="flex items-center gap-2.5 py-2.5 border-b border-slate-800/60 last:border-0 cursor-pointer" onClick={() => onSelectSymbol?.(r.symbol, r.timeframe)}>
                      <div className={`w-8 h-8 rounded-lg grid place-items-center font-extrabold text-[13px] ${tierCls}`}>{r.tier}</div>
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-1.5">
                          <span className="font-bold text-sm">{baseOf(r.symbol)}</span>
                          <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded uppercase ${isShort ? 'bg-red-500/15 text-red-300' : 'bg-green-500/15 text-green-300'}`}>{isShort ? 'Short' : 'Long'}</span>
                        </div>
                        <div className="text-[11px] text-slate-500 mt-0.5 truncate">
                          R:R {r.risk_reward?.toFixed(1) ?? '—'}
                          {r.prob_tp1 != null && ` · P(TP1) ${(r.prob_tp1 * 100).toFixed(0)}%`}
                          {r.edge_tags && r.edge_tags.length > 0 && ` · ${r.edge_tags.slice(0, 2).join(', ')}`}
                        </div>
                      </div>
                      <span className={`text-[10px] font-bold px-2 py-0.5 rounded ${okVerdict ? 'bg-green-500/15 text-green-300' : 'bg-yellow-500/15 text-yellow-300'}`}>
                        {okVerdict ? 'OK' : 'espera'}
                      </span>
                    </div>
                  )
                })}
              </div>

              {/* MERCADO */}
              <div className="bg-[#0f1524] border border-slate-800 rounded-2xl p-4 lg:col-span-3">
                <h2 className="text-[11.5px] uppercase tracking-wider text-slate-500 font-bold mb-3">🌡️ Mercado</h2>
                {macro ? (
                  <div className="flex items-center gap-2.5">
                    <div className="w-10 h-10 rounded-xl bg-emerald-500/12 grid place-items-center text-lg">
                      {macro.btc_direction?.toLowerCase().includes('alta') || macro.btc_direction?.toLowerCase().includes('long') || macro.btc_direction?.toLowerCase().includes('up') ? '📈' : macro.btc_direction?.toLowerCase().includes('baixa') || macro.btc_direction?.toLowerCase().includes('short') || macro.btc_direction?.toLowerCase().includes('down') ? '📉' : '➡️'}
                    </div>
                    <div>
                      <div className="font-bold text-[15px]">BTC {macro.btc_direction ?? '—'}</div>
                      <div className="text-[11px] text-slate-500">{macro.btc_dominance != null ? `Dominância ${macro.btc_dominance.toFixed(1)}%` : 'sem dados'}</div>
                    </div>
                  </div>
                ) : <div className="text-sm text-slate-600">sem dados de mercado</div>}
              </div>

              {/* SAÚDE DO BOT */}
              <div className="bg-[#0f1524] border border-slate-800 rounded-2xl p-4 lg:col-span-4">
                <h2 className="text-[11.5px] uppercase tracking-wider text-slate-500 font-bold mb-3">🩺 Saúde do bot</h2>
                <div className="flex items-center gap-2.5">
                  <div className={`w-10 h-10 rounded-xl grid place-items-center text-lg ${health?.status === 'healthy' ? 'bg-emerald-500/12' : health?.status === 'degraded' ? 'bg-yellow-500/12' : 'bg-slate-500/12'}`}>
                    {health?.status === 'healthy' ? '💚' : health?.status === 'degraded' ? '⚠️' : '❔'}
                  </div>
                  <div>
                    <div className="font-bold text-[15px]">
                      {health?.status === 'healthy' ? 'Saudável' : health?.status === 'degraded' ? 'Degradado' : 'Desconhecido'}
                    </div>
                    <div className="text-[11px] text-slate-500">
                      {health?.gap_seconds != null ? `último tick há ${Math.round(health.gap_seconds)}s` : 'sem heartbeat'}
                      {health?.last_source ? ` · ${health.last_source}` : ''}
                    </div>
                  </div>
                </div>
              </div>

              {/* CURVA DE CAPITAL */}
              <div className="bg-[#0f1524] border border-slate-800 rounded-2xl p-4 lg:col-span-12">
                <h2 className="text-[11.5px] uppercase tracking-wider text-slate-500 font-bold mb-3">📈 Curva de capital · 30 dias (paper)</h2>
                {sparkPath ? (
                  <>
                    <div className="flex items-baseline gap-2.5 mb-1.5">
                      <span className={`text-[13px] font-semibold ${(paper?.equity?.final_pnl_pct ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                        {(paper?.equity?.final_pnl_pct ?? 0) >= 0 ? '+' : ''}{(paper?.equity?.final_pnl_pct ?? 0).toFixed(2)}% no período
                      </span>
                      <span className="text-[11px] text-slate-500">{paper?.equity?.trades_total ?? 0} trades</span>
                    </div>
                    <svg className="w-full h-[70px] block" viewBox="0 0 600 70" preserveAspectRatio="none">
                      <defs>
                        <linearGradient id="eqgrad" x1="0" y1="0" x2="0" y2="1">
                          <stop offset="0%" stopColor="rgba(34,197,94,.35)" />
                          <stop offset="100%" stopColor="rgba(34,197,94,0)" />
                        </linearGradient>
                      </defs>
                      <path d={sparkPath.area} fill="url(#eqgrad)" />
                      <path d={sparkPath.line} fill="none" stroke="#22c55e" strokeWidth="2" />
                    </svg>
                  </>
                ) : (
                  <div className="py-6 text-center text-slate-600 text-sm">Curva indisponível.</div>
                )}
              </div>

            </div>
          </>
        )}
      </div>
    </div>
  )
}

// Marcador posicionado no trilho SL──entrada──preço──TP
function Marker({ frac, label, color, strong }: { frac: number; label: string; color: string; strong?: boolean }) {
  return (
    <div className="absolute top-1/2 -translate-x-1/2 -translate-y-1/2 whitespace-nowrap text-center" style={{ left: `${clamp01(frac) * 100}%` }}>
      <span className={`block ${strong ? 'w-[3px] h-4' : 'w-0.5 h-3'} ${color} mx-auto mb-0.5 ${strong ? 'shadow-[0_0_6px_rgba(255,255,255,0.5)]' : ''}`} />
      <span className={`text-[9px] ${strong ? 'text-white font-bold' : 'text-slate-400'}`}>{label}</span>
    </div>
  )
}
