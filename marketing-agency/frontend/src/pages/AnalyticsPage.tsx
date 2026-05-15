import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { api } from '../services/api'
import type { MetricsSummary, MetricsSnapshot } from '../types'

function Bar({ value, max }: { value: number; max: number }) {
  const pct = max > 0 ? Math.min((value / max) * 100, 100) : 0
  return (
    <div className="h-1.5 bg-gray-800 rounded-full overflow-hidden">
      <div className="h-full bg-violet-500 rounded-full transition-all duration-500" style={{ width: `${pct}%` }} />
    </div>
  )
}

export function AnalyticsPage() {
  const { clientId } = useParams<{ clientId: string }>()
  const id = Number(clientId)
  const [summary, setSummary] = useState<MetricsSummary | null>(null)
  const [metrics, setMetrics] = useState<MetricsSnapshot[]>([])
  const [days, setDays] = useState(30)
  const [adding, setAdding] = useState(false)
  const [addForm, setAddForm] = useState({
    platform: 'instagram', views: '', likes: '', comments: '',
    shares: '', saves: '', reach: '', retention_rate: '', ctr: '',
  })

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
    })
    setAdding(false)
    await load()
  }

  const maxViews = Math.max(...metrics.map(m => m.views), 1)

  return (
    <div className="p-4 md:p-6 space-y-4 max-w-5xl">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold text-white">Analytics</h1>
        <div className="flex gap-1.5">
          {[7, 14, 30].map(d => (
            <button key={d} onClick={() => setDays(d)}
              className={`text-xs px-2.5 py-1.5 rounded-lg border transition-colors ${
                days === d ? 'bg-violet-600/20 border-violet-500 text-violet-300' : 'bg-gray-800 border-gray-700 text-gray-400'
              }`}>
              {d}d
            </button>
          ))}
        </div>
      </div>

      <button onClick={() => setAdding(v => !v)} className="btn-secondary w-full text-sm">
        {adding ? '− Fechar' : '+ Adicionar Métricas'}
      </button>

      {adding && (
        <div className="card space-y-3">
          <h2 className="text-sm font-semibold text-white">Registrar métricas</h2>
          <div className="grid grid-cols-2 gap-2">
            {[
              { key: 'views', label: 'Views' },
              { key: 'likes', label: 'Curtidas' },
              { key: 'comments', label: 'Comentários' },
              { key: 'shares', label: 'Shares' },
              { key: 'saves', label: 'Salvamentos' },
              { key: 'reach', label: 'Alcance' },
              { key: 'retention_rate', label: 'Retenção (%)' },
              { key: 'ctr', label: 'CTR (%)' },
            ].map(({ key, label }) => (
              <div key={key}>
                <label className="text-xs text-gray-400 mb-0.5 block">{label}</label>
                <input type="number" className="input-field py-2 text-sm" placeholder="0"
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
          {/* Totals grid */}
          <div className="grid grid-cols-3 gap-2">
            {[
              { label: 'Views', value: summary.totals.views },
              { label: 'Shares', value: summary.totals.shares },
              { label: 'Salvamentos', value: summary.totals.saves },
              { label: 'Curtidas', value: summary.totals.likes },
              { label: 'Comentários', value: summary.totals.comments },
              { label: 'Conteúdos', value: summary.content_count },
            ].map(({ label, value }) => (
              <div key={label} className="card p-3">
                <p className="text-[10px] text-gray-400 mb-0.5">{label}</p>
                <p className="text-lg font-bold text-white">
                  {value >= 1000 ? `${(value / 1000).toFixed(1)}k` : value}
                </p>
              </div>
            ))}
          </div>

          {/* Averages */}
          <div className="card space-y-3">
            <h2 className="text-sm font-semibold text-white">Médias</h2>
            {[
              { label: 'Retenção média', value: `${summary.averages.retention_rate}%` },
              { label: 'CTR médio', value: `${summary.averages.ctr}%` },
              { label: 'Conversão', value: `${summary.averages.conversion_rate}%` },
            ].map(({ label, value }) => (
              <div key={label} className="flex justify-between items-center">
                <span className="text-xs text-gray-400">{label}</span>
                <span className="text-sm font-bold text-white">{value}</span>
              </div>
            ))}
          </div>

          {/* Engagement bars */}
          <div className="card space-y-3">
            <h2 className="text-sm font-semibold text-white">Engajamento</h2>
            {[
              { label: 'Views', value: summary.totals.views },
              { label: 'Curtidas', value: summary.totals.likes },
              { label: 'Comentários', value: summary.totals.comments },
              { label: 'Shares', value: summary.totals.shares },
              { label: 'Salvamentos', value: summary.totals.saves },
            ].map(({ label, value }) => (
              <div key={label} className="space-y-1">
                <div className="flex justify-between text-xs">
                  <span className="text-gray-400">{label}</span>
                  <span className="text-white">{value.toLocaleString('pt-BR')}</span>
                </div>
                <Bar value={value} max={summary.totals.views} />
              </div>
            ))}
          </div>

          {/* History */}
          {metrics.length > 0 && (
            <div className="card">
              <h2 className="text-sm font-semibold text-white mb-3">Histórico</h2>
              <div className="space-y-2 max-h-64 overflow-y-auto">
                {metrics.map(m => (
                  <div key={m.id} className="flex items-center gap-2 py-1.5 border-b border-gray-800 last:border-0">
                    <div className="w-10 h-4 bg-gray-800 rounded overflow-hidden shrink-0">
                      <div className="h-full bg-violet-600 rounded" style={{ width: `${(m.views / maxViews) * 100}%` }} />
                    </div>
                    <span className="text-xs text-gray-400 shrink-0">{m.platform}</span>
                    <span className="text-xs text-white font-medium">
                      {m.views >= 1000 ? `${(m.views / 1000).toFixed(1)}k` : m.views}v
                    </span>
                    <span className="text-xs text-gray-500">{m.retention_rate}% ret</span>
                    <span className="ml-auto text-xs text-gray-600">
                      {new Date(m.recorded_at).toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit' })}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </>
      ) : (
        <div className="card text-center py-12">
          <p className="text-gray-500 mb-1 text-sm">Nenhuma métrica registrada</p>
          <p className="text-gray-600 text-xs">Adicione métricas para ver análises</p>
        </div>
      )}
    </div>
  )
}
