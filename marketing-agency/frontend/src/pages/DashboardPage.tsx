import { useEffect, useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import { api } from '../services/api'
import type { Client, CalendarSlot, MetricsSummary, Insight } from '../types'
import { AuthorityScore } from '../components/AuthorityScore'
import { OnboardingChecklist } from '../components/OnboardingChecklist'
import { ScoreSparkline } from '../components/ScoreSparkline'
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
  const [retro, setRetro] = useState<{
    headline: string
    wins: string[]
    losses: string[]
    themes: string[]
    next_week_priority: string
    mood_score: number
    post_count: number
  } | null>(null)
  const [retroBusy, setRetroBusy] = useState(false)

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

  async function downloadMonthlyReport() {
    const now = new Date()
    const month = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`
    try {
      const blob = await api.clients.monthlyReport(id, month)
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `relatorio-${id}-${month}.pdf`
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
    } catch (e: any) {
      alert('Erro: ' + e.message)
    }
  }

  async function generateRetro() {
    setRetroBusy(true)
    try {
      const r: any = await api.strategy.retrospective(id)
      setRetro(r)
    } catch (e: any) {
      alert('Erro: ' + e.message)
    } finally { setRetroBusy(false) }
  }

  if (!client) return (
    <div className="p-4 md:p-6 space-y-4 max-w-5xl animate-pulse">
      <div className="h-6 w-1/3 bg-gray-800 rounded" />
      <div className="h-3 w-1/2 bg-gray-900 rounded" />
      <div className="flex items-center gap-4">
        <div className="w-20 h-20 rounded-full bg-gray-800" />
        <div className="flex-1 grid grid-cols-2 gap-2">
          {[0, 1, 2, 3].map(i => <div key={i} className="card h-16 bg-gray-900/60" />)}
        </div>
      </div>
      <div className="card h-32 bg-gray-900/60" />
      <div className="card h-40 bg-gray-900/60" />
    </div>
  )

  const fmt = (n: number) => n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n)

  return (
    <div className="p-4 md:p-6 space-y-4 max-w-5xl">
      {/* Header */}
      <div className="flex items-start justify-between">
        <div className="min-w-0">
          <h1 className="text-lg md:text-xl font-bold text-white truncate">{client.name}</h1>
          <p className="text-xs text-gray-400 mt-0.5 truncate">{client.niche} · {client.platforms.join(', ')}</p>
        </div>
        <div className="flex gap-2 shrink-0 ml-2">
          <button onClick={downloadMonthlyReport} className="btn-secondary text-xs" title="Baixar PDF do mês">📄 PDF</button>
          <button onClick={refreshScore} className="btn-secondary text-xs">Score</button>
        </div>
      </div>

      <OnboardingChecklist clientId={id} client={client} />

      {/* Authority + metrics */}
      <div className="flex items-center gap-4">
        <div className="flex flex-col items-center gap-1">
          <AuthorityScore score={client.authority_score} />
          <ScoreSparkline clientId={id} days={30} />
        </div>
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

      {/* Weekly retrospective */}
      <div className="card bg-teal-900/10 border-teal-800/50">
        <div className="flex items-center justify-between mb-2">
          <h2 className="text-sm font-semibold text-white">Revisão semanal</h2>
          <button onClick={generateRetro} disabled={retroBusy} className="text-xs text-teal-400 hover:text-teal-300 disabled:opacity-50">
            {retroBusy ? 'Gerando...' : retro ? '↻ Atualizar' : '✦ Gerar'}
          </button>
        </div>
        {!retro && !retroBusy && (
          <p className="text-xs text-gray-500">Recap dos últimos 7 dias com acertos, falhas e a prioridade da próxima semana.</p>
        )}
        {retro && (
          <div className="space-y-2">
            <div className="flex items-center gap-3">
              <div className={`text-3xl font-bold ${retro.mood_score >= 70 ? 'text-green-400' : retro.mood_score >= 40 ? 'text-yellow-400' : 'text-red-400'}`}>
                {retro.mood_score}
              </div>
              <div className="flex-1 min-w-0">
                <p className="text-xs text-teal-300">{retro.headline}</p>
                <p className="text-[10px] text-gray-500">{retro.post_count} peças nos últimos 7 dias</p>
              </div>
            </div>
            {retro.themes.length > 0 && (
              <div className="flex flex-wrap gap-1">
                {retro.themes.map((t, i) => <span key={i} className="text-[10px] px-1.5 py-0.5 rounded bg-teal-900/40 text-teal-200 border border-teal-800/50">{t}</span>)}
              </div>
            )}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
              {retro.wins.length > 0 && (
                <div>
                  <p className="text-[10px] text-green-400 font-semibold mb-0.5">✓ FUNCIONOU</p>
                  <ul className="text-xs text-gray-300 space-y-0.5">{retro.wins.map((w, i) => <li key={i}>· {w}</li>)}</ul>
                </div>
              )}
              {retro.losses.length > 0 && (
                <div>
                  <p className="text-[10px] text-red-400 font-semibold mb-0.5">✗ FALHOU</p>
                  <ul className="text-xs text-gray-300 space-y-0.5">{retro.losses.map((l, i) => <li key={i}>· {l}</li>)}</ul>
                </div>
              )}
            </div>
            {retro.next_week_priority && (
              <div className="bg-teal-950/40 border border-teal-800/50 rounded p-2">
                <p className="text-[10px] text-teal-300 font-semibold mb-0.5">PRIORIDADE PRÓXIMA SEMANA</p>
                <p className="text-xs text-gray-200">{retro.next_week_priority}</p>
              </div>
            )}
          </div>
        )}
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
