import { useState, useEffect, useCallback } from 'react'
import { X, Shield, ShieldAlert, Activity, History, AlertTriangle } from 'lucide-react'

const BACKEND = import.meta.env.VITE_API_URL ?? 'https://crypto-agente-production.up.railway.app'

interface RiskStatus {
  enabled: boolean
  trading_paused: boolean
  pause_reason: string | null
  pause_manual: boolean
  paused_at: string | null
  daily_dd_pct: number
  weekly_dd_pct: number
  daily_trades: number
  weekly_trades: number
  daily_limit_pct: number
  weekly_limit_pct: number
  current_day_utc: string | null
  current_week_utc: string | null
  updated_at: string | null
}

interface RiskEvent {
  id: number
  event_type: 'auto_pause' | 'auto_resume' | 'manual_pause' | 'manual_resume'
  reason: string | null
  daily_dd_pct: number | null
  weekly_dd_pct: number | null
  daily_trades: number | null
  weekly_trades: number | null
  ts: string
}

interface TierStat {
  n: number
  wins: number
  losses: number
  wr_pct: number | null
  avg_r: number | null
  expectancy_r: number | null
  pnl_pct: number
}

interface PaperSummary {
  enabled: boolean
  mode: string
  days: number
  equity: { final_pnl_pct: number; trades_total: number; curve: { date: string; cumulative_pct: number }[] }
  tier_stats: Record<string, TierStat>
}

interface HealthStatus {
  enabled: boolean
  status: 'healthy' | 'degraded' | 'unknown'
  last_alive_ts: string | null
  gap_seconds: number | null
  gap_alert_threshold: number
  last_source: string | null
  tick_count: number
}

interface Props {
  onClose: () => void
}

/**
 * StatusPanel — visão completa do health do bot.
 *
 * - DD diário/semanal com barra de progresso até o limite
 * - Trades resolvidos no dia/semana
 * - Kill switch com confirmação 2-step
 * - Histórico dos últimos 30 dias de eventos do circuit breaker
 */
export default function StatusPanel({ onClose }: Props) {
  const [status, setStatus] = useState<RiskStatus | null>(null)
  const [events, setEvents] = useState<RiskEvent[]>([])
  const [health, setHealth] = useState<HealthStatus | null>(null)
  const [paper, setPaper] = useState<PaperSummary | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [confirmKill, setConfirmKill] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const [sRes, eRes, hRes, pRes] = await Promise.all([
        fetch(`${BACKEND}/api/risk/status`),
        fetch(`${BACKEND}/api/risk/events?days=30&limit=100`),
        fetch(`${BACKEND}/api/admin/health`),
        fetch(`${BACKEND}/api/paper/summary?days=30`),
      ])
      if (sRes.ok) {
        const j = (await sRes.json()) as RiskStatus
        if (j.enabled !== false) setStatus(j)
      }
      if (eRes.ok) {
        const j = await eRes.json()
        setEvents(j.events ?? [])
      }
      if (hRes.ok) {
        const j = (await hRes.json()) as HealthStatus
        if (j.enabled !== false) setHealth(j)
      }
      if (pRes.ok) {
        const j = (await pRes.json()) as PaperSummary
        if (j.enabled !== false) setPaper(j)
      }
      setError(null)
    } catch (e) {
      setError(String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
    const id = setInterval(load, 15_000)
    return () => clearInterval(id)
  }, [load])

  const toggle = async (next: boolean) => {
    setBusy(true)
    try {
      const res = await fetch(`${BACKEND}/api/risk/kill-switch?paused=${next}`, {
        method: 'POST',
      })
      if (res.ok) {
        const j = (await res.json()) as RiskStatus
        setStatus(j)
        await load()
      }
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
      setConfirmKill(false)
    }
  }

  const paused = status?.trading_paused ?? false
  const Icon = paused ? ShieldAlert : Shield

  // % rumo ao limite (0 = OK, 100 = bateu)
  const dailyPct = status
    ? Math.max(0, Math.min(100, (status.daily_dd_pct / status.daily_limit_pct) * 100))
    : 0
  const weeklyPct = status
    ? Math.max(0, Math.min(100, (status.weekly_dd_pct / status.weekly_limit_pct) * 100))
    : 0

  const eventBadge = (t: RiskEvent['event_type']) => {
    if (t === 'auto_pause')
      return { label: '🛑 Pausa automática', cls: 'bg-red-500/15 border-red-500/40 text-red-300' }
    if (t === 'manual_pause')
      return { label: '🛑 Kill switch', cls: 'bg-orange-500/15 border-orange-500/40 text-orange-300' }
    if (t === 'auto_resume')
      return { label: '▶ Retomada (auto)', cls: 'bg-emerald-500/15 border-emerald-500/40 text-emerald-300' }
    return { label: '▶ Retomada manual', cls: 'bg-sky-500/15 border-sky-500/40 text-sky-300' }
  }

  return (
    <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-start justify-center p-2 sm:p-4 overflow-y-auto">
      <div className="w-full max-w-3xl bg-[#0a0e1a] border border-slate-700 rounded-xl my-4">
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-slate-800">
          <div className="flex items-center gap-2">
            <Icon className={`w-5 h-5 ${paused ? 'text-red-400' : 'text-emerald-400'}`} />
            <div>
              <h2 className="text-base font-bold text-white">Status do bot</h2>
              <p className="text-[11px] text-slate-500">
                Circuit breaker · DD · histórico
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
              {/* Estado principal */}
              <div
                className={`p-3 rounded-lg border ${
                  paused
                    ? 'border-red-500/40 bg-red-500/10'
                    : 'border-emerald-500/40 bg-emerald-500/10'
                }`}
              >
                <div className="flex items-center gap-2">
                  <Activity className={`w-4 h-4 ${paused ? 'text-red-300' : 'text-emerald-300'}`} />
                  <span className={`text-sm font-bold ${paused ? 'text-red-200' : 'text-emerald-200'}`}>
                    {paused ? 'TRADING PAUSADO' : 'TRADING ATIVO'}
                  </span>
                  {paused && status.pause_manual && (
                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-orange-500/20 text-orange-300 border border-orange-500/40">
                      manual
                    </span>
                  )}
                </div>
                {paused && status.pause_reason && (
                  <p className="mt-1 text-xs text-slate-300">{status.pause_reason}</p>
                )}
                {paused && status.paused_at && (
                  <p className="mt-1 text-[10px] text-slate-500 font-mono">
                    desde {new Date(status.paused_at).toLocaleString('pt-BR')}
                  </p>
                )}
              </div>

              {/* Métricas DD */}
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <DDCard
                  label="DD diário"
                  pct={status.daily_dd_pct}
                  limit={status.daily_limit_pct}
                  trades={status.daily_trades}
                  progress={dailyPct}
                />
                <DDCard
                  label="DD semanal"
                  pct={status.weekly_dd_pct}
                  limit={status.weekly_limit_pct}
                  trades={status.weekly_trades}
                  progress={weeklyPct}
                />
              </div>

              {/* Kill switch */}
              <div className="p-3 rounded-lg border border-slate-700 bg-slate-900/50">
                <div className="flex items-center justify-between gap-2 flex-wrap">
                  <div className="min-w-0">
                    <p className="text-xs font-bold text-slate-200">
                      🛑 Kill switch manual
                    </p>
                    <p className="text-[11px] text-slate-500 mt-0.5">
                      Bloqueia push de novas recs. Trades em andamento continuam.
                    </p>
                  </div>
                  {paused ? (
                    <button
                      disabled={busy}
                      onClick={() => toggle(false)}
                      className="px-3 py-1.5 rounded bg-emerald-600/20 border border-emerald-500/50 text-emerald-300 text-xs font-bold disabled:opacity-50 whitespace-nowrap"
                    >
                      ▶ Retomar
                    </button>
                  ) : !confirmKill ? (
                    <button
                      disabled={busy}
                      onClick={() => setConfirmKill(true)}
                      className="px-3 py-1.5 rounded bg-red-600/20 border border-red-500/50 text-red-300 text-xs font-bold disabled:opacity-50 whitespace-nowrap"
                    >
                      🛑 Pausar tudo
                    </button>
                  ) : (
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="text-[11px] text-orange-300 flex items-center gap-1">
                        <AlertTriangle className="w-3 h-3" />
                        confirmar?
                      </span>
                      <button
                        disabled={busy}
                        onClick={() => toggle(true)}
                        className="px-3 py-1.5 rounded bg-red-600 text-white text-xs font-bold disabled:opacity-50"
                      >
                        Sim, pausar
                      </button>
                      <button
                        onClick={() => setConfirmKill(false)}
                        className="px-2 py-1.5 rounded bg-slate-800 text-slate-300 text-xs"
                      >
                        Cancelar
                      </button>
                    </div>
                  )}
                </div>
              </div>

              {/* Paper-trade summary (#8) */}
              {paper && (
                <div className="p-3 rounded-lg border border-violet-500/30 bg-violet-500/5">
                  <div className="flex items-center justify-between gap-2 mb-2">
                    <div className="flex items-center gap-2">
                      <span className="text-violet-300 font-bold text-xs">
                        🧪 Paper-trade · últimos {paper.days}d
                      </span>
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-violet-500/20 text-violet-300 border border-violet-500/40">
                        {paper.equity.trades_total} trades
                      </span>
                    </div>
                    <span
                      className={`text-sm font-mono font-bold ${
                        paper.equity.final_pnl_pct >= 0 ? 'text-emerald-300' : 'text-red-300'
                      }`}
                    >
                      {paper.equity.final_pnl_pct >= 0 ? '+' : ''}
                      {paper.equity.final_pnl_pct.toFixed(2)}%
                    </span>
                  </div>
                  <div className="grid grid-cols-3 gap-2 text-[10px]">
                    {(['A+', 'A', 'B'] as const).map(t => {
                      const s = paper.tier_stats[t]
                      if (!s || s.n === 0) {
                        return (
                          <div key={t} className="p-1.5 rounded bg-slate-900/40 border border-slate-800 text-center">
                            <div className="font-bold text-slate-400">{t}</div>
                            <div className="text-slate-600">sem dados</div>
                          </div>
                        )
                      }
                      return (
                        <div key={t} className="p-1.5 rounded bg-slate-900/40 border border-slate-800 text-center">
                          <div className="font-bold text-slate-300">
                            {t} · <span className="text-slate-500">{s.n}</span>
                          </div>
                          <div className="text-slate-400 font-mono">
                            WR <span className={s.wr_pct && s.wr_pct >= 50 ? 'text-emerald-400' : 'text-red-400'}>
                              {s.wr_pct?.toFixed(0) ?? '—'}%
                            </span>
                          </div>
                          <div className="text-slate-400 font-mono">
                            R <span className={s.avg_r && s.avg_r >= 0 ? 'text-emerald-400' : 'text-red-400'}>
                              {s.avg_r != null ? (s.avg_r >= 0 ? '+' : '') + s.avg_r.toFixed(2) : '—'}
                            </span>
                          </div>
                        </div>
                      )
                    })}
                  </div>
                </div>
              )}

              {/* Heartbeat backend (#6) */}
              {health && (
                <div
                  className={`p-3 rounded-lg border text-xs ${
                    health.status === 'healthy'
                      ? 'border-emerald-500/30 bg-emerald-500/5'
                      : health.status === 'degraded'
                      ? 'border-red-500/40 bg-red-500/10'
                      : 'border-slate-700 bg-slate-900/40'
                  }`}
                >
                  <div className="flex items-center justify-between gap-2 flex-wrap">
                    <div className="flex items-center gap-2 min-w-0">
                      <span
                        className={`inline-block w-2 h-2 rounded-full ${
                          health.status === 'healthy'
                            ? 'bg-emerald-400 animate-pulse'
                            : health.status === 'degraded'
                            ? 'bg-red-400 animate-pulse'
                            : 'bg-slate-500'
                        }`}
                      />
                      <span className="font-bold text-slate-200">
                        Heartbeat backend ·{' '}
                        {health.status === 'healthy'
                          ? 'saudável'
                          : health.status === 'degraded'
                          ? 'DEGRADADO'
                          : 'desconhecido'}
                      </span>
                    </div>
                    <span className="text-[10px] font-mono text-slate-400">
                      gap {health.gap_seconds != null ? `${health.gap_seconds}s` : '—'} /{' '}
                      alerta {health.gap_alert_threshold}s
                    </span>
                  </div>
                  <div className="mt-1 text-[10px] text-slate-500 font-mono">
                    {health.tick_count} ticks · fonte {health.last_source ?? '—'}
                    {health.last_alive_ts && (
                      <> · último {new Date(health.last_alive_ts).toLocaleTimeString('pt-BR')}</>
                    )}
                  </div>
                </div>
              )}

              {/* Histórico */}
              <div>
                <div className="flex items-center gap-2 mb-2">
                  <History className="w-4 h-4 text-slate-400" />
                  <h3 className="text-xs font-bold text-slate-300">
                    Histórico (últimos 30 dias)
                  </h3>
                  <span className="text-[10px] text-slate-500">· {events.length} eventos</span>
                </div>
                {events.length === 0 ? (
                  <p className="text-xs text-slate-500 p-3 rounded bg-slate-900/40 border border-slate-800">
                    Nenhum evento registrado. Circuit breaker nunca acionou — sinal verde.
                  </p>
                ) : (
                  <div className="space-y-1.5 max-h-64 overflow-y-auto">
                    {events.map(ev => {
                      const b = eventBadge(ev.event_type)
                      return (
                        <div
                          key={ev.id}
                          className={`p-2 rounded border text-xs flex items-start gap-2 ${b.cls}`}
                        >
                          <span className="font-bold whitespace-nowrap">{b.label}</span>
                          <div className="flex-1 min-w-0">
                            <div className="text-slate-200 truncate">{ev.reason ?? '—'}</div>
                            <div className="text-[10px] text-slate-500 mt-0.5 font-mono">
                              {new Date(ev.ts).toLocaleString('pt-BR')}
                              {ev.daily_dd_pct != null && (
                                <span className="ml-2">
                                  DD dia {ev.daily_dd_pct.toFixed(2)}% · sem {(ev.weekly_dd_pct ?? 0).toFixed(2)}%
                                </span>
                              )}
                            </div>
                          </div>
                        </div>
                      )
                    })}
                  </div>
                )}
              </div>

              {/* Janelas UTC */}
              <div className="text-[10px] text-slate-600 font-mono text-center pt-2 border-t border-slate-800">
                Janela atual · dia {status.current_day_utc ?? '—'} · semana {status.current_week_utc ?? '—'} (UTC)
                {status.updated_at && <> · atualizado {new Date(status.updated_at).toLocaleTimeString('pt-BR')}</>}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

function DDCard({
  label,
  pct,
  limit,
  trades,
  progress,
}: {
  label: string
  pct: number
  limit: number
  trades: number
  progress: number
}) {
  const danger = progress >= 70
  const fill = danger ? 'bg-red-500' : progress >= 40 ? 'bg-amber-500' : 'bg-emerald-500'
  return (
    <div className="p-3 rounded-lg border border-slate-700 bg-slate-900/40">
      <div className="flex justify-between items-baseline">
        <span className="text-[11px] text-slate-400">{label}</span>
        <span className={`text-sm font-mono font-bold ${danger ? 'text-red-300' : 'text-slate-200'}`}>
          {pct.toFixed(2)}%
        </span>
      </div>
      <div className="mt-1.5 h-1.5 rounded bg-slate-800 overflow-hidden">
        <div className={`h-full ${fill} transition-all`} style={{ width: `${progress}%` }} />
      </div>
      <div className="flex justify-between mt-1.5 text-[10px] text-slate-500 font-mono">
        <span>limite {limit}%</span>
        <span>{trades} trades</span>
      </div>
    </div>
  )
}
