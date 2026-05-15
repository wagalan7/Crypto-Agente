import { useEffect, useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import { api } from '../services/api'
import type { Client, CalendarSlot, MetricsSummary } from '../types'
import { AuthorityScore } from '../components/AuthorityScore'
import { OBJECTIVE_LABELS, OBJECTIVE_COLORS, FORMAT_LABELS } from '../types'

function MetricCard({ label, value, sub }: { label: string; value: string | number; sub?: string }) {
  return (
    <div className="card">
      <p className="text-xs text-gray-400 mb-1">{label}</p>
      <p className="text-2xl font-bold text-white">{value}</p>
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

  useEffect(() => {
    api.clients.get(id).then((c: any) => setClient(c))
    api.calendar.get(id, 7).then((s: any) => setSlots(s))
    api.analytics.summary(id, 30).then((s: any) => setSummary(s))
  }, [id])

  async function refreshScore() {
    const res: any = await api.clients.refreshScore(id)
    setClient(prev => prev ? { ...prev, authority_score: res.authority_score } : prev)
  }

  if (!client) return <div className="p-8 text-gray-400">Carregando...</div>

  const fmt = (n: number) => n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n)

  return (
    <div className="p-6 space-y-6 max-w-5xl">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-xl font-bold text-white">{client.name}</h1>
          <p className="text-sm text-gray-400 mt-0.5">{client.niche} · {client.platforms.join(', ')}</p>
        </div>
        <button onClick={refreshScore} className="btn-secondary text-xs">
          Atualizar Score
        </button>
      </div>

      <div className="flex items-start gap-6">
        <AuthorityScore score={client.authority_score} />
        <div className="flex-1 grid grid-cols-4 gap-3">
          <MetricCard label="Views (30d)" value={summary ? fmt(summary.totals.views) : '—'} />
          <MetricCard label="Compartilhamentos" value={summary ? fmt(summary.totals.shares) : '—'} />
          <MetricCard label="Salvamentos" value={summary ? fmt(summary.totals.saves) : '—'} />
          <MetricCard
            label="Retenção média"
            value={summary ? `${summary.averages.retention_rate}%` : '—'}
            sub={summary ? `${summary.content_count} conteúdos` : undefined}
          />
        </div>
      </div>

      <div className="grid grid-cols-2 gap-6">
        <div className="card">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-sm font-semibold text-white">Próximos 7 dias</h2>
            <Link to={`/client/${id}/calendar`} className="text-xs text-violet-400 hover:text-violet-300">
              Ver calendário →
            </Link>
          </div>
          {slots.length === 0 ? (
            <p className="text-xs text-gray-500">Nenhum slot planejado</p>
          ) : (
            <div className="space-y-2">
              {slots.slice(0, 5).map(slot => {
                const date = new Date(slot.scheduled_at)
                return (
                  <div key={slot.id} className="flex items-center gap-3">
                    <div className="text-center w-10">
                      <p className="text-xs text-gray-400">{date.toLocaleDateString('pt-BR', { weekday: 'short' })}</p>
                      <p className="text-sm font-bold text-white">{date.getDate()}</p>
                    </div>
                    <div className="flex-1 flex items-center gap-2">
                      <span className={`badge border text-xs ${OBJECTIVE_COLORS[slot.objective] || 'bg-gray-700 text-gray-300 border-gray-600'}`}>
                        {OBJECTIVE_LABELS[slot.objective] || slot.objective}
                      </span>
                      <span className="text-xs text-gray-400">{FORMAT_LABELS[slot.format] || slot.format}</span>
                    </div>
                    <span className={`text-xs ${slot.status === 'ready' ? 'text-green-400' : 'text-gray-500'}`}>
                      {slot.status === 'ready' ? 'Pronto' : 'Planejado'}
                    </span>
                  </div>
                )
              })}
            </div>
          )}
        </div>

        <div className="card space-y-3">
          <h2 className="text-sm font-semibold text-white">Atalhos rápidos</h2>
          <div className="space-y-2">
            {[
              { to: `calendar`, label: 'Gerar calendário semanal', color: 'text-violet-400' },
              { to: `agents`, label: 'Criar roteiro de conteúdo', color: 'text-blue-400' },
              { to: `agents`, label: 'Amplificar ideia', color: 'text-green-400' },
              { to: `content`, label: 'Aprovar conteúdo pendente', color: 'text-orange-400' },
              { to: `analytics`, label: 'Ver análise de métricas', color: 'text-cyan-400' },
            ].map(({ to, label, color }) => (
              <Link key={label} to={`/client/${id}/${to}`}
                className="flex items-center gap-2 text-sm text-gray-300 hover:text-white transition-colors group">
                <span className={`text-lg ${color}`}>→</span>
                <span className="group-hover:underline">{label}</span>
              </Link>
            ))}
          </div>
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
