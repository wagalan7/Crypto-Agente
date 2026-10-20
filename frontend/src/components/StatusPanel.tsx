import { useState, useEffect, useCallback } from 'react'
import { X, Shield, ShieldAlert, Activity, History, AlertTriangle } from 'lucide-react'
import { api, executionIncidentsObservedAt } from '../services/api'
import type { ExecutionIncidentsStatus } from '../services/api'
import {
  deriveOperationalState, emptyReading, p03View, readingQuality,
  DEFAULT_STALE_AFTER_MS, finiteNumber, canOfferResume,
  type SourceReading,
} from '../lib/operationalState'
import { settleReading } from '../lib/readingLifecycle'
import { useOverlayDismiss, useReadingClock } from '../hooks/useReadingLifecycle'
import { OperationalStateCard, P03IncidentCard } from './status/OperationalCards'

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
  useOverlayDismiss(onClose)
  // Leitura COM qualidade por fonte (Lote 04): erro/ausência não viram zero.
  const [riskRead, setRiskRead] = useState<SourceReading<RiskStatus>>(emptyReading)
  const [p03Read, setP03Read] = useState<SourceReading<ExecutionIncidentsStatus>>(emptyReading)
  const [events, setEvents] = useState<RiskEvent[]>([])
  const [health, setHealth] = useState<HealthStatus | null>(null)
  const [paper, setPaper] = useState<PaperSummary | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [confirmKill, setConfirmKill] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const tick = useReadingClock()
  const status = riskRead.value

  const load = useCallback(async (opts?: { force?: boolean }) => {
    const settle = settleReading
    const asJson = async (path: string) => {
      const r = await fetch(`${BACKEND}${path}`, { signal: AbortSignal.timeout(10_000) })
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      return r.json()
    }
    const [sRes, eRes, hRes, pRes, iRes] = await Promise.all([
      settle(asJson('/api/risk/status')),
      settle(asJson('/api/risk/events?days=30&limit=100')),
      settle(asJson('/api/admin/health')),
      settle(asJson('/api/paper/summary?days=30')),
      // Mesmo cache/single-flight da Home: duas telas abertas = UM GET.
      settle(api.executionIncidentsStatus(opts?.force ? { maxAgeMs: 0 } : undefined)),
    ])
    const why = (e: unknown) => (e instanceof Error ? e.message : 'leitura falhou')
    setRiskRead(prev => {
      if (!sRes.ok) return { ...prev, state: 'ERROR', errorReason: why(sRes.e) }
      const j = sRes.v as RiskStatus | null
      if (!j || j.enabled === false) {
        return { ...prev, state: 'ERROR', errorReason: 'risco desabilitado no backend' }
      }
      return { state: 'OK', value: j, lastOkAtMs: sRes.receivedAtMs, errorReason: null }
    })
    setP03Read(prev => {
      if (!iRes.ok) return { ...prev, state: 'ERROR', errorReason: why(iRes.e) }
      const observedAt = executionIncidentsObservedAt(iRes.v)
      if (observedAt == null) return { ...prev, state: 'ERROR', errorReason: 'incidentes sem instante de leitura confirmado' }
      return { state: 'OK', value: iRes.v, lastOkAtMs: observedAt, errorReason: null }
    })
    if (eRes.ok) setEvents(((eRes.v as { events?: RiskEvent[] })?.events) ?? [])
    if (hRes.ok) {
      const j = hRes.v as HealthStatus | null
      if (j && j.enabled !== false) setHealth(j)
    }
    if (pRes.ok) {
      const j = pRes.v as PaperSummary | null
      if (j && j.enabled !== false) setPaper(j)
    }
    setError(sRes.ok ? null : why(sRes.e))
    setLoading(false)
  }, [])

  useEffect(() => {
    void load()
    const id = setInterval(() => { void load() }, 15_000)
    return () => clearInterval(id)
  }, [load])

  const toggle = async (next: boolean) => {
    if (!next && !canOfferResume({ risk: riskRead, p03: p03Read, nowMs: Date.now() })) return
    setBusy(true)
    try {
      const res = await fetch(`${BACKEND}/api/risk/kill-switch?paused=${next}`, {
        method: 'POST',
      })
      if (res.ok) {
        const j = (await res.json()) as RiskStatus
        setRiskRead({ state: 'OK', value: j, lastOkAtMs: Date.now(), errorReason: null })
        await load({ force: true })
      }
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
      setConfirmKill(false)
    }
  }

  const paused = status?.trading_paused === true
  const canResume = canOfferResume({ risk: riskRead, p03: p03Read, nowMs: tick })
  // MESMA função pura da Home e do selo do header.
  const opState = deriveOperationalState({
    risk: riskRead, p03: p03Read, nowMs: tick, staleAfterMs: DEFAULT_STALE_AFTER_MS,
  })
  const p03 = p03View(p03Read, tick, DEFAULT_STALE_AFTER_MS)
  const riskQuality = readingQuality(riskRead, tick, DEFAULT_STALE_AFTER_MS)
  const p03Quality = readingQuality(p03Read, tick, DEFAULT_STALE_AFTER_MS)
  const Icon = opState.blocked ? ShieldAlert : Shield

  // % rumo ao limite (0 = OK, 100 = bateu). Sem leitura, a barra não existe.
  const pctToLimit = (dd: number | null, lim: number | null) =>
    dd != null && lim != null && lim !== 0
      ? Math.max(0, Math.min(100, (dd / lim) * 100))
      : 0
  const dailyPct = pctToLimit(finiteNumber(status?.daily_dd_pct), finiteNumber(status?.daily_limit_pct))
  const weeklyPct = pctToLimit(finiteNumber(status?.weekly_dd_pct), finiteNumber(status?.weekly_limit_pct))

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
    <div className="app-overlay fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-start justify-center p-2 sm:p-4 overflow-y-auto">
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
          <button onClick={onClose} aria-label="Fechar status do bot" className="p-1 rounded hover:bg-slate-800">
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

          {/* Estado operacional e incidentes aparecem SEMPRE que conhecidos —
              inclusive sem leitura de risco (antes a seção toda desaparecia). */}
          <OperationalStateCard state={opState} ageMs={riskQuality.ageMs}>
            {paused && status?.paused_at && (
              <p className="num mt-2 text-[11px] text-slate-500">
                Pausa registrada desde {new Date(status.paused_at).toLocaleString('pt-BR')}
              </p>
            )}
          </OperationalStateCard>
          <P03IncidentCard view={p03} ageMs={p03Quality.ageMs} />

          {status && (
            <>
              <div className="flex items-center gap-2 text-[11px] text-slate-500">
                <Activity className="w-3.5 h-3.5" />
                <span className="break-words">
                  Fontes: <span className="font-mono">/api/risk/status</span>
                  {' e '}
                  <span className="font-mono">/api/execution-incidents/status</span>
                </span>
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
                  {paused && !canResume ? (
                    <span className="text-[11px] text-amber-200 max-w-xs">
                      Retomada não disponível nesta leitura. Consulte o diagnóstico acima.
                    </span>
                  ) : paused ? (
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
                    Sem eventos para exibir nesta janela. Isso não confirma ausência de bloqueios; consulte o estado acima.
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
  pct: number | null | undefined
  limit: number | null | undefined
  trades: number | null | undefined
  progress: number
}) {
  // Ausência NÃO é zero: valor desconhecido aparece como "não confirmado".
  const value = typeof pct === 'number' && Number.isFinite(pct) ? pct : null
  const lim = typeof limit === 'number' && Number.isFinite(limit) ? limit : null
  const n = typeof trades === 'number' && Number.isFinite(trades) ? trades : null
  const danger = value != null && progress >= 70
  const fill = danger ? 'bg-red-500' : progress >= 40 ? 'bg-amber-500' : 'bg-emerald-500'
  return (
    <div className="p-3 rounded-lg border border-slate-700 bg-slate-900/40">
      <div className="flex justify-between items-baseline gap-2">
        <span className="text-[11px] text-slate-400">{label}</span>
        <span className={`num text-sm font-bold ${value == null ? 'text-slate-400' : danger ? 'text-red-300' : 'text-slate-200'}`}>
          {value == null ? 'não confirmado' : `${value.toFixed(2)}%`}
        </span>
      </div>
      <div className="mt-1.5 h-1.5 rounded bg-slate-800 overflow-hidden">
        <div className={`h-full ${fill} transition-all`} style={{ width: `${value == null ? 0 : progress}%` }} />
      </div>
      <div className="num flex justify-between gap-2 mt-1.5 text-[10px] text-slate-500">
        <span>{lim == null ? 'limite não confirmado' : `limite ${lim}%`}</span>
        <span>{n == null ? 'trades não confirmados' : `${n} trades`}</span>
      </div>
    </div>
  )
}
