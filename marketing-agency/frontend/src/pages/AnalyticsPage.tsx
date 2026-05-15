import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { api } from '../services/api'
import type { MetricsSummary, MetricsSnapshot } from '../types'
import { AuthorityScore } from '../components/AuthorityScore'

function StatRow({ label, value, max, unit = '' }: { label: string; value: number; max: number; unit?: string }) {
  const pct = max > 0 ? Math.min((value / max) * 100, 100) : 0
  return (
    <div className="space-y-1">
      <div className="flex justify-between text-xs">
        <span className="text-gray-400">{label}</span>
        <span className="text-white font-medium">{value.toLocaleString('pt-BR')}{unit}</span>
      </div>
      <div className="h-1.5 bg-gray-800 rounded-full overflow-hidden">
        <div className="h-full bg-violet-500 rounded-full transition-all duration-500" style={{ width: `${pct}%` }} />
      </div>
    </div>
  )
}

export function AnalyticsPage() {
  const { clientId } = useParams<{ clientId: string }>()
  const id = Number(clientId)
  const [summary, setSummary] = useState<MetricsSummary | null>(null)
  const [metrics, setMetrics] = useState<MetricsSnapshot[]>([])
  const [days, setDays] = useState(30)
  const [addForm, setAddForm] = useState({
    platform: 'instagram', views: '', likes: '', comments: '', shares: '', saves: '',
    reach: '', retention_rate: '', ctr: '', content_id: '',
  })
  const [adding, setAdding] = useState(false)

  async function load() {
    const [s, m] = await Promise.all([
      api.analytics.summary(id, days) as any,
      api.analytics.metrics(id) as any,
    ])
    setSummary(s)
    setMetrics(m)
  }

  useEffect(() => { load() }, [id, days])

  async function submitMetrics() {
    await api.analytics.addMetrics({
      client_id: id,
      platform: addForm.platform,
      views: Number(addForm.views) || 0,
      likes: Number(addForm.likes) || 0,
      comments: Number(addForm.comments) || 0,
      shares: Number(addForm.shares) || 0,
      saves: Number(addForm.saves) || 0,
      reach: Number(addForm.reach) || 0,
      retention_rate: Number(addForm.retention_rate) || 0,
      ctr: Number(addForm.ctr) || 0,
      content_id: addForm.content_id ? Number(addForm.content_id) : undefined,
    })
    setAdding(false)
    await load()
  }

  const maxViews = Math.max(...metrics.map(m => m.views), 1)

  return (
    <div className="p-6 space-y-5 max-w-5xl">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold text-white">Analytics</h1>
        <div className="flex gap-2">
          {[7, 14, 30, 90].map(d => (
            <button key={d} onClick={() => setDays(d)}
              className={`text-xs px-3 py-1.5 rounded-lg border transition-colors ${
                days === d ? 'bg-violet-600/20 border-violet-500 text-violet-300' : 'bg-gray-800 border-gray-700 text-gray-400'
              }`}>
              {d}d
            </button>
          ))}
          <button onClick={() => setAdding(true)} className="btn-primary w-auto px-4 text-xs">
            + Adicionar Métricas
          </button>
        </div>
      </div>

      {adding && (
        <div className="card space-y-3">
          <h2 className="text-sm font-semibold text-white">Registrar métricas de conteúdo</h2>
          <div className="grid grid-cols-4 gap-3">
            {[
              { key: 'views', label: 'Views' },
              { key: 'likes', label: 'Curtidas' },
              { key: 'comments', label: 'Comentários' },
              { key: 'shares', label: 'Compartilhamentos' },
              { key: 'saves', label: 'Salvamentos' },
              { key: 'reach', label: 'Alcance' },
              { key: 'retention_rate', label: 'Retenção (%)' },
              { key: 'ctr', label: 'CTR (%)' },
            ].map(({ key, label }) => (
              <div key={key}>
                <label className="text-xs text-gray-400 mb-1 block">{label}</label>
                <input type="number" className="input-field" placeholder="0"
                  value={(addForm as any)[key]}
                  onChange={e => setAddForm(p => ({ ...p, [key]: e.target.value }))} />
              </div>
            ))}
          </div>
          <div className="flex gap-2">
            <button onClick={submitMetrics} className="btn-primary w-auto px-5">Salvar</button>
            <button onClick={() => setAdding(false)} className="btn-secondary">Cancelar</button>
          </div>
        </div>
      )}

      {summary && summary.content_count > 0 ? (
        <>
          <div className="grid grid-cols-3 gap-4">
            <div className="card col-span-1 flex justify-center">
              <AuthorityScore score={0} />
            </div>
            <div className="card col-span-2 grid grid-cols-3 gap-3">
              {[
                { label: 'Views totais', value: summary.totals.views.toLocaleString('pt-BR') },
                { label: 'Compartilhamentos', value: summary.totals.shares.toLocaleString('pt-BR') },
                { label: 'Salvamentos', value: summary.totals.saves.toLocaleString('pt-BR') },
                { label: 'Comentários', value: summary.totals.comments.toLocaleString('pt-BR') },
                { label: 'Alcance total', value: summary.totals.reach.toLocaleString('pt-BR') },
                { label: 'Conteúdos', value: String(summary.content_count) },
              ].map(({ label, value }) => (
                <div key={label}>
                  <p className="text-xs text-gray-400">{label}</p>
                  <p className="text-xl font-bold text-white">{value}</p>
                </div>
              ))}
            </div>
          </div>

          <div className="grid grid-cols-3 gap-4">
            <div className="card space-y-3">
              <h2 className="text-sm font-semibold text-white">Médias</h2>
              <div className="space-y-4">
                <div>
                  <p className="text-xs text-gray-400 mb-1">Retenção média</p>
                  <div className="flex items-end gap-1">
                    <p className="text-2xl font-bold text-white">{summary.averages.retention_rate}</p>
                    <p className="text-sm text-gray-400 mb-1">%</p>
                  </div>
                </div>
                <div>
                  <p className="text-xs text-gray-400 mb-1">CTR médio</p>
                  <div className="flex items-end gap-1">
                    <p className="text-2xl font-bold text-white">{summary.averages.ctr}</p>
                    <p className="text-sm text-gray-400 mb-1">%</p>
                  </div>
                </div>
                <div>
                  <p className="text-xs text-gray-400 mb-1">Conversão</p>
                  <div className="flex items-end gap-1">
                    <p className="text-2xl font-bold text-white">{summary.averages.conversion_rate}</p>
                    <p className="text-sm text-gray-400 mb-1">%</p>
                  </div>
                </div>
              </div>
            </div>

            <div className="card col-span-2 space-y-3">
              <h2 className="text-sm font-semibold text-white">Distribuição de engajamento</h2>
              <StatRow label="Views" value={summary.totals.views} max={summary.totals.views} />
              <StatRow label="Curtidas" value={summary.totals.likes} max={summary.totals.views} />
              <StatRow label="Comentários" value={summary.totals.comments} max={summary.totals.views} />
              <StatRow label="Compartilhamentos" value={summary.totals.shares} max={summary.totals.views} />
              <StatRow label="Salvamentos" value={summary.totals.saves} max={summary.totals.views} />
            </div>
          </div>

          <div className="card">
            <h2 className="text-sm font-semibold text-white mb-4">Histórico de métricas</h2>
            <div className="space-y-2 max-h-72 overflow-y-auto">
              {metrics.map(m => (
                <div key={m.id} className="flex items-center gap-4 py-2 border-b border-gray-800 last:border-0 text-xs">
                  <div className="w-12">
                    <div className="h-6 bg-gray-800 rounded overflow-hidden">
                      <div
                        className="h-full bg-violet-600 rounded"
                        style={{ width: `${(m.views / maxViews) * 100}%` }}
                      />
                    </div>
                  </div>
                  <span className="text-gray-400">{m.platform}</span>
                  <span className="text-white font-medium">{m.views.toLocaleString('pt-BR')} views</span>
                  <span className="text-gray-400">{m.retention_rate}% ret.</span>
                  <span className="text-gray-400">{m.shares} comp.</span>
                  <span className="text-gray-400">{m.saves} salv.</span>
                  <span className="ml-auto text-gray-600">
                    {new Date(m.recorded_at).toLocaleDateString('pt-BR')}
                  </span>
                </div>
              ))}
            </div>
          </div>
        </>
      ) : (
        <div className="card text-center py-16">
          <p className="text-gray-500 mb-2">Nenhuma métrica registrada</p>
          <p className="text-gray-600 text-xs">Adicione métricas de seus conteúdos para ver análises</p>
        </div>
      )}
    </div>
  )
}
