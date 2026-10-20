import { useState, useEffect, useCallback } from 'react'
import { Shield, ShieldAlert, ShieldQuestion } from 'lucide-react'
import {
  deriveOperationalState, emptyReading, readingQuality, formatAge,
  DEFAULT_STALE_AFTER_MS, finiteNumber, canOfferResume,
  type SourceReading,
} from '../lib/operationalState'
import { useReadingClock } from '../hooks/useReadingLifecycle'

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
}

interface BadgeProps {
  /** Quando fornecido, clique chama onOpen() em vez de abrir o modal interno. */
  onOpen?: () => void
}

/**
 * RiskStatusBadge — mostra estado do circuit breaker no header.
 *
 * - Sem pausa de risco confirmada não significa autoridade de entrada.
 * - Bloqueio conhecido permanece visível mesmo com leitura antiga.
 * - Clique: se `onOpen` fornecido, delega (ex: abrir StatusPanel completo);
 *   senão abre modal inline com kill switch rápido.
 *
 * Faz poll leve a cada 30s. Erro e envelhecimento ficam explícitos.
 */
export default function RiskStatusBadge({ onOpen }: BadgeProps = {}) {
  // Leitura COM qualidade: erro depois de sucesso continua visível e o dado
  // antigo é rotulado como tal (antes o erro era silenciado — Lote 04).
  const [reading, setReading] = useState<SourceReading<RiskStatus>>(emptyReading)
  const [busy, setBusy] = useState(false)
  const [showPanel, setShowPanel] = useState(false)
  const tick = useReadingClock()
  const status = reading.value

  const load = useCallback(async () => {
    try {
      const res = await fetch(`${BACKEND}/api/risk/status`, { signal: AbortSignal.timeout(10_000) })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const json = (await res.json()) as RiskStatus
      if (json.enabled === false) {
        setReading(prev => ({ ...prev, state: 'ERROR', errorReason: 'risco desabilitado no backend' }))
        return
      }
      setReading({ state: 'OK', value: json, lastOkAtMs: Date.now(), errorReason: null })
    } catch (e) {
      setReading(prev => ({
        ...prev, state: 'ERROR',
        errorReason: e instanceof Error ? e.message : 'leitura falhou',
      }))
    }
  }, [])

  useEffect(() => {
    load()
    const id = setInterval(load, 30_000)
    return () => clearInterval(id)
  }, [load])

  const toggle = async (next: boolean) => {
    if (!next && !canOfferResume({ risk: reading, p03: emptyReading(), scope: 'RISK_ONLY', nowMs: Date.now() })) return
    setBusy(true)
    try {
      const url = `${BACKEND}/api/risk/kill-switch?paused=${next}`
      const res = await fetch(url, { method: 'POST' })
      if (res.ok) {
        const json = (await res.json()) as RiskStatus
        setReading({ state: 'OK', value: json, lastOkAtMs: Date.now(), errorReason: null })
      }
    } catch {
      /* idem */
    } finally {
      setBusy(false)
      setShowPanel(false)
    }
  }

  // MESMA função pura da Home/Sistema, com escopo DECLARADO: este controle não
  // lê incidentes de execução (nenhuma chamada por card), então nunca afirma
  // "disponível" — no máximo "risco sem pausa".
  const state = deriveOperationalState({
    risk: reading, p03: emptyReading(), scope: 'RISK_ONLY',
    nowMs: tick, staleAfterMs: DEFAULT_STALE_AFTER_MS,
  })
  const quality = readingQuality(reading, tick, DEFAULT_STALE_AFTER_MS)
  if (state.level === 'LOADING') return null

  const paused = status?.trading_paused === true
  const canResume = canOfferResume({ risk: reading, p03: emptyReading(), scope: 'RISK_ONLY', nowMs: tick })
  const Icon = state.blocked ? ShieldAlert
    : state.level === 'NO_BLOCK_CONFIRMED' ? Shield
    : ShieldQuestion
  const cls = state.blocked
    ? 'border-red-500/60 text-red-200 bg-red-500/15'
    : state.level === 'NO_BLOCK_CONFIRMED'
      ? 'border-sky-500/40 text-sky-200 bg-sky-500/10'
      : 'border-amber-500/50 text-amber-200 bg-amber-500/10'
  const shortLabel = state.blocked ? 'BLOQUEADO'
    : state.level === 'NO_BLOCK_CONFIRMED' ? 'SEM PAUSA'
    : state.level === 'STALE' ? 'LEITURA ANTIGA'
    : 'SEM CONFIRMAÇÃO'
  const titleText = [
    `${state.title}.`,
    state.reasons.length > 0 ? `Motivo: ${state.reasons.join('; ')}.` : '',
    state.missing.length > 0 ? `Não confirmado: ${state.missing.join('; ')}.` : '',
    `Última leitura confirmada ${formatAge(quality.ageMs)}.`,
  ].filter(Boolean).join(' ')

  return (
    <>
      <button
        onClick={() => (onOpen ? onOpen() : setShowPanel(true))}
        aria-label={`Risco: ${state.title}`}
        data-testid="risk-badge"
        data-level={state.level}
        className={`flex items-center gap-1 px-2 py-1 border rounded text-xs font-bold ${cls}`}
        title={titleText}
      >
        <Icon className="w-3.5 h-3.5" />
        <span className="hidden sm:inline">{shortLabel}</span>
      </button>

      {showPanel && (
        <div className="app-overlay fixed inset-0 bg-black/80 backdrop-blur-sm z-50 flex items-center justify-center p-4">
          <div className="w-full max-w-sm bg-[#0a0e1a] border border-slate-700 rounded-xl p-4">
            <div className="flex items-center gap-2 mb-3">
              <Icon className={`w-5 h-5 ${state.blocked ? 'text-red-400' : state.level === 'NO_BLOCK_CONFIRMED' ? 'text-sky-300' : 'text-amber-300'}`} />
              <h3 className="text-sm font-bold text-white">Circuit breaker · {state.title}</h3>
            </div>

            <p className="text-[11.5px] text-slate-300 mb-2 leading-snug">{state.detail}</p>
            {state.reasons.length > 0 && (
              <ul className="text-xs text-red-200 mb-3 p-2 rounded bg-red-500/10 border border-red-500/30 space-y-1">
                {state.reasons.map((r, i) => <li key={i} className="break-words">{r}</li>)}
              </ul>
            )}
            {state.missing.length > 0 && (
              <p className="text-[11px] text-slate-400 mb-3 leading-snug break-words">
                Não confirmado nesta leitura: {state.missing.join(' · ')}
              </p>
            )}

            <div className="num text-xs space-y-1 mb-4">
              <Row label="DD dia" value={finiteNumber(status?.daily_dd_pct)} limit={finiteNumber(status?.daily_limit_pct)} />
              <Row label="DD semana" value={finiteNumber(status?.weekly_dd_pct)} limit={finiteNumber(status?.weekly_limit_pct)} />
              <Row label="Trades dia" value={finiteNumber(status?.daily_trades)} />
              <Row label="Trades semana" value={finiteNumber(status?.weekly_trades)} />
              <div className="flex justify-between text-slate-500">
                <span>Última leitura confirmada</span>
                <span>{formatAge(quality.ageMs)}</span>
              </div>
            </div>

            <div className="flex gap-2">
              {paused && !canResume ? (
                <p className="flex-1 text-xs text-amber-200">
                  Retomada não disponível nesta leitura. Consulte o diagnóstico de risco e execução.
                </p>
              ) : paused ? (
                <button
                  disabled={busy}
                  onClick={() => toggle(false)}
                  className="flex-1 px-3 py-2 rounded bg-emerald-600/20 border border-emerald-500/50 text-emerald-300 text-xs font-bold disabled:opacity-50"
                >
                  ▶ Retomar trading
                </button>
              ) : (
                <button
                  disabled={busy}
                  onClick={() => {
                    if (confirm('🛑 Pausar todas as novas recomendações?\n\nTrades em andamento continuam normais — só blokeia push de NOVAS recs.')) {
                      toggle(true)
                    }
                  }}
                  className="flex-1 px-3 py-2 rounded bg-red-600/20 border border-red-500/50 text-red-300 text-xs font-bold disabled:opacity-50"
                >
                  🛑 Kill switch
                </button>
              )}
              <button
                onClick={() => setShowPanel(false)}
                className="px-3 py-2 rounded bg-slate-800 border border-slate-700 text-slate-300 text-xs"
              >
                Fechar
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  )
}

/** Linha de métrica: valor ausente aparece como "não confirmado", não como 0. */
function Row({ label, value, limit }: { label: string; value: number | null; limit?: number | null }) {
  const breached = value != null && limit != null && value <= limit
  return (
    <div className="flex justify-between gap-2">
      <span className="text-slate-500">{label}</span>
      <span className={value == null ? 'text-slate-400' : breached ? 'text-red-300' : 'text-slate-200'}>
        {value == null ? 'não confirmado' : `${value.toFixed(2)}`}
        {limit != null && value != null && ` / limite ${limit}`}
      </span>
    </div>
  )
}
