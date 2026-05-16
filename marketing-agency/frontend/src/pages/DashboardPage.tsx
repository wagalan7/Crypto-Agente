import { useEffect, useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import { api } from '../services/api'
import type { Client, CalendarSlot, MetricsSummary, Insight } from '../types'
import { AuthorityScore } from '../components/AuthorityScore'
import { OBJECTIVE_LABELS, OBJECTIVE_COLORS, FORMAT_LABELS } from '../types'

function MetricCard({ label, value, sub }: { label: string; value: string | number; sub?: string }) {
  return (
    <div className="card p-3">
      <p className="text-xs text-gray-400 mb-0.5">{label}</p>
      <p className="text-xl font-bold text-white">{value}</p>
      {sub && <p className="text-xs text-gray-500 mt-0.5">{sub}</p>}
    </div>
  )
}

export function DashboardPage() {
  const { clientId } = useParams<{ clientId: string }>()
  const id = Number(clientId)
  const [client, setClient] = useState<Client | null>(null)
  const [slots, setSlots] = useState<CalendarSlot[]>([])
  const [summary, setSummary] = useState<MetricsSummary | null>(null)
  const [insights, setInsights] = useState<Insight[]>([])

  useEffect(() => {
    api.clients.get(id).then((c: any) => setClient(c))
    api.calendar.get(id, 7).then((s: any) => setSlots(s))
    api.analytics.summary(id, 30).then((s: any) => setSummary(s))
    api.strategy.insights(id).then((s: any) => setInsights(s)).catch(() => {})
  }, [id])

  async function refreshScore() {
    const res: any = await api.clients.refreshScore(id)
    setClient(prev => prev ? { ...prev, authority_score: res.authority_score } : prev)
  }

  if (!client) return <div className="p-6 text-gray-400 text-sm">Carregando...</div>

  const fmt = (n: number) => n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n)

  return (
    <div className="p-4 md:p-6 space-y-4 max-w-5xl">
      {/* Header */}
      <div className="flex items-start justify-between">
        <div className="min-w-0">
          <h1 className="text-lg md:text-xl font-bold text-white truncate">{client.name}</h1>
          <p className="text-xs text-gray-400 mt-0.5 truncate">{client.niche} · {client.platforms.join(', ')}</p>
        </div>
        <button onClick={refreshScore} className="btn-secondary text-xs shrink-0 ml-2">Score</button>
      </div>

      {/* Authority + metrics */}
      <div className="flex items-center gap-4">
        <AuthorityScore score={client.authority_score} />
        <div className="flex-1 grid grid-cols-2 gap-2">
          <MetricCard label="Views (30d)" value={summary ? fmt(summary.totals.views) : '—'} />
          <MetricCard label="Shares" value={summary ? fmt(summary.totals.shares) : '—'} />
          <MetricCard label="Salvamentos" value={summary ? fmt(summary.totals.saves) : '—'} />
          <MetricCard
            label="Retenção"
            value={summary ? `${summary.averages.retention_rate}%` : '—'}
            sub={summary ? `${summary.content_count} conteúdos` : undefined}
          />
        </div>
      </div>

      {/* Insights inteligentes */}
      {insights.length > 0 && (
        <div className="card">
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-sm font-semibold text-white">Insights da IA</h2>
            <Link to={`/client/${id}/strategy`} className="text-xs text-violet-400">Central →</Link>
          </div>
          <div className="space-y-2">
            {insights.slice(0, 4).map(i => {
              const sev = i.severity === 'critical' ? 'bg-red-900/20 border-red-800/50 text-red-200'
                : i.severity === 'warning' ? 'bg-yellow-900/20 border-yellow-800/50 text-yellow-200'
                : i.severity === 'opportunity' ? 'bg-green-900/20 border-green-800/50 text-green-200'
                : 'bg-blue-900/10 border-blue-800/50 text-blue-200'
              const icon = i.severity === 'critical' ? '⚠' : i.severity === 'warning' ? '⚡' : i.severity === 'opportunity' ? '✦' : 'ℹ'
              return (
                <div key={i.id} className={`border rounded-lg px-3 py-2 ${sev}`}>
                  <p className="text-xs font-semibold flex items-center gap-1.5">
                    <span>{icon}</span><span>{i.title}</span>
                    <span className="text-[10px] opacity-60">· {i.kind}</span>
                  </p>
                  <p className="text-xs opacity-90 mt-0.5">{i.message}</p>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* Next 7 days */}
      <div className="card">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-semibold text-white">Próximos 7 dias</h2>
          <Link to={`/client/${id}/calendar`} className="text-xs text-violet-400">Ver →</Link>
        </div>
        {slots.length === 0 ? (
          <p className="text-xs text-gray-500">Nenhum slot planejado</p>
        ) : (
          <div className="space-y-2">
            {slots.slice(0, 5).map(slot => {
              const date = new Date(slot.scheduled_at)
              return (
                <div key={slot.id} className="flex items-center gap-3">
                  <div className="text-center w-9 shrink-0">
                    <p className="text-[10px] text-gray-400">{date.toLocaleDateString('pt-BR', { weekday: 'short' })}</p>
                    <p className="text-sm font-bold text-white">{date.getDate()}</p>
                  </div>
                  <div className="flex-1 flex items-center gap-1.5 min-w-0">
                    <span className={`badge border text-[10px] shrink-0 ${OBJECTIVE_COLORS[slot.objective] || 'bg-gray-700 text-gray-300 border-gray-600'}`}>
                      {OBJECTIVE_LABELS[slot.objective] || slot.objective}
                    </span>
                    <span className="text-xs text-gray-400 truncate">{FORMAT_LABELS[slot.format] || slot.format}</span>
                  </div>
                  <span className={`text-[10px] shrink-0 ${slot.status === 'ready' ? 'text-green-400' : 'text-gray-600'}`}>
                    {slot.status === 'ready' ? '✓' : '○'}
                  </span>
                </div>
              )
            })}
          </div>
        )}
      </div>

      {/* Quick actions */}
      <div className="card">
        <h2 className="text-sm font-semibold text-white mb-3">Ações rápidas</h2>
        <div className="grid grid-cols-1 gap-2">
          {[
            { to: 'calendar', label: 'Gerar calendário semanal', color: 'text-violet-400', bg: 'bg-violet-900/20 border-violet-800/50' },
            { to: 'agents', label: 'Criar roteiro com IA', color: 'text-blue-400', bg: 'bg-blue-900/20 border-blue-800/50' },
            { to: 'agents', label: 'Amplificar ideia', color: 'text-green-400', bg: 'bg-green-900/20 border-green-800/50' },
            { to: 'content', label: 'Aprovar conteúdo pendente', color: 'text-orange-400', bg: 'bg-orange-900/20 border-orange-800/50' },
          ].map(({ to, label, color, bg }) => (
            <Link
              key={label}
              to={`/client/${id}/${to}`}
              className={`flex items-center gap-3 px-3 py-2.5 rounded-lg border transition-colors ${bg}`}
            >
              <span className={`text-base ${color}`}>→</span>
              <span className="text-sm text-gray-200">{label}</span>
            </Link>
          ))}
        </div>
      </div>

      {client.positioning && (
        <div className="card bg-violet-900/10 border-violet-800/50">
          <p className="text-xs text-violet-400 font-semibold mb-1">POSICIONAMENTO</p>
          <p className="text-sm text-gray-300">{client.positioning}</p>
        </div>
      )}
    </div>
  )
}
