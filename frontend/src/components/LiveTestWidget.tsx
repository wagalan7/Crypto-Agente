import { useState, useEffect } from 'react'

const BACKEND = import.meta.env.VITE_API_URL ?? 'https://crypto-agente-production.up.railway.app'

interface LiveTestStatus {
  enabled: boolean
  start_at: string
  deadline_at: string
  target: number
  days: number
  count: number
  remaining: number
  days_left: number
  count_done: boolean
  time_done: boolean
  notified: { count: boolean; time: boolean }
}

/**
 * Widget compacto do "teste do canário a 0.50": mostra o sequencial de
 * auto-trades reais (N/alvo), barra de progresso e dias restantes da janela.
 * Consome /api/live-test/status. Some quando o teste está desabilitado.
 */
export default function LiveTestWidget() {
  const [st, setSt] = useState<LiveTestStatus | null>(null)

  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const res = await fetch(`${BACKEND}/api/live-test/status`)
        if (!res.ok) return
        const j = (await res.json()) as LiveTestStatus
        if (alive) setSt(j)
      } catch {
        /* fail-soft: widget some se a API não responder */
      }
    }
    load()
    const t = setInterval(load, 60_000)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [])

  if (!st || !st.enabled) return null

  const done = st.count_done || st.time_done
  const pct = st.target > 0 ? Math.min(100, (st.count / st.target) * 100) : 0
  const daysLeft = Math.max(0, st.days_left)

  return (
    <div
      className={`mb-3 rounded-lg border p-3 ${
        done
          ? 'border-amber-500/50 bg-amber-950/30'
          : 'border-sky-700/40 bg-sky-950/20'
      }`}
    >
      <div className="flex items-center justify-between mb-2">
        <div className="text-[12px] font-semibold text-slate-200">
          🧪 Teste 0.50 · canário
        </div>
        <div className="text-[11px] font-mono text-slate-300">
          {st.count}/{st.target} auto-trades
        </div>
      </div>

      {/* Barra de progresso */}
      <div className="h-2 w-full rounded-full bg-slate-800 overflow-hidden">
        <div
          className={`h-full rounded-full transition-all ${
            done ? 'bg-amber-400' : 'bg-sky-400'
          }`}
          style={{ width: `${pct}%` }}
        />
      </div>

      <div className="mt-2 flex items-center justify-between text-[10px] text-slate-400">
        {done ? (
          <span className="text-amber-300 font-semibold">
            🏁 Concluído — hora de analisar
          </span>
        ) : (
          <span>
            faltam {st.remaining} · janela {daysLeft.toFixed(1)} dias
          </span>
        )}
        <span className="text-slate-600">alvo {st.target} ou {st.days}d</span>
      </div>
    </div>
  )
}
