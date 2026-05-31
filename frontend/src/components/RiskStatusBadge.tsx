import { useState, useEffect, useCallback } from 'react'
import { Shield, ShieldAlert } from 'lucide-react'

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
 * - Verde "ativo" quando trading_paused=false
 * - Vermelho piscando "pausado" quando true
 * - Clique: se `onOpen` fornecido, delega (ex: abrir StatusPanel completo);
 *   senão abre modal inline com kill switch rápido.
 *
 * Faz poll leve a cada 30s. Não bloqueia UI — silencia erros.
 */
export default function RiskStatusBadge({ onOpen }: BadgeProps = {}) {
  const [status, setStatus] = useState<RiskStatus | null>(null)
  const [busy, setBusy] = useState(false)
  const [showPanel, setShowPanel] = useState(false)

  const load = useCallback(async () => {
    try {
      const res = await fetch(`${BACKEND}/api/risk/status`)
      if (!res.ok) return
      const json = (await res.json()) as RiskStatus
      if (json.enabled !== false) setStatus(json)
    } catch {
      /* silencia — não é fatal pra UI */
    }
  }, [])

  useEffect(() => {
    load()
    const id = setInterval(load, 30_000)
    return () => clearInterval(id)
  }, [load])

  const toggle = async (next: boolean) => {
    setBusy(true)
    try {
      const url = `${BACKEND}/api/risk/kill-switch?paused=${next}`
      const res = await fetch(url, { method: 'POST' })
      if (res.ok) {
        const json = (await res.json()) as RiskStatus
        setStatus(json)
      }
    } catch {
      /* idem */
    } finally {
      setBusy(false)
      setShowPanel(false)
    }
  }

  if (!status) return null

  const paused = status.trading_paused
  const Icon = paused ? ShieldAlert : Shield
  const cls = paused
    ? 'border-red-500/60 text-red-300 bg-red-500/15 animate-pulse'
    : 'border-emerald-500/40 text-emerald-300 bg-emerald-500/10'

  return (
    <>
      <button
        onClick={() => (onOpen ? onOpen() : setShowPanel(true))}
        className={`flex items-center gap-1 px-2 py-1 border rounded text-xs font-bold ${cls}`}
        title={
          paused
            ? `🛑 PAUSADO · ${status.pause_reason ?? ''}`
            : `Ativo · DD dia ${status.daily_dd_pct.toFixed(2)}% / sem ${status.weekly_dd_pct.toFixed(2)}%`
        }
      >
        <Icon className="w-3.5 h-3.5" />
        <span className="hidden sm:inline">{paused ? 'PAUSADO' : 'OK'}</span>
      </button>

      {showPanel && (
        <div className="fixed inset-0 bg-black/80 backdrop-blur-sm z-50 flex items-center justify-center p-4">
          <div className="w-full max-w-sm bg-[#0a0e1a] border border-slate-700 rounded-xl p-4">
            <div className="flex items-center gap-2 mb-3">
              <Icon className={`w-5 h-5 ${paused ? 'text-red-400' : 'text-emerald-400'}`} />
              <h3 className="text-sm font-bold text-white">
                Circuit Breaker · {paused ? 'PAUSADO' : 'ATIVO'}
              </h3>
            </div>

            {paused && status.pause_reason && (
              <p className="text-xs text-red-300 mb-3 p-2 rounded bg-red-500/10 border border-red-500/30">
                {status.pause_reason}
                {status.pause_manual && <span className="text-slate-400"> · manual</span>}
              </p>
            )}

            <div className="text-xs space-y-1 mb-4 font-mono">
              <div className="flex justify-between">
                <span className="text-slate-500">DD dia</span>
                <span className={status.daily_dd_pct <= status.daily_limit_pct ? 'text-red-300' : 'text-slate-200'}>
                  {status.daily_dd_pct.toFixed(2)}% / limite {status.daily_limit_pct}%
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-slate-500">DD semana</span>
                <span className={status.weekly_dd_pct <= status.weekly_limit_pct ? 'text-red-300' : 'text-slate-200'}>
                  {status.weekly_dd_pct.toFixed(2)}% / limite {status.weekly_limit_pct}%
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-slate-500">Trades dia</span>
                <span className="text-slate-200">{status.daily_trades}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-slate-500">Trades semana</span>
                <span className="text-slate-200">{status.weekly_trades}</span>
              </div>
            </div>

            <div className="flex gap-2">
              {paused ? (
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
