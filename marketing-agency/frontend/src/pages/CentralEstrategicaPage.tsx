import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { api } from '../services/api'
import type { Insight, WeeklyBrain } from '../types'

const SEV_COLOR: Record<string, string> = {
  critical: 'bg-red-900/30 border-red-700/60 text-red-200',
  warning: 'bg-yellow-900/30 border-yellow-700/60 text-yellow-200',
  opportunity: 'bg-green-900/30 border-green-700/60 text-green-200',
  info: 'bg-blue-900/20 border-blue-800/50 text-blue-200',
}

const SEV_ICON: Record<string, string> = {
  critical: '⚠',
  warning: '⚡',
  opportunity: '✦',
  info: 'ℹ',
}

export function CentralEstrategicaPage() {
  const { clientId } = useParams<{ clientId: string }>()
  const id = Number(clientId)
  const [wb, setWb] = useState<WeeklyBrain | null>(null)
  const [exists, setExists] = useState<boolean | null>(null)
  const [insights, setInsights] = useState<Insight[]>([])
  const [loadingW, setLoadingW] = useState(false)
  const [loadingI, setLoadingI] = useState(false)
  const [err, setErr] = useState('')

  async function load() {
    const w: any = await api.strategy.weekly(id)
    if (w.exists) { setWb(w); setExists(true) } else setExists(false)
    const ins: any = await api.strategy.insights(id)
    setInsights(ins)
  }

  useEffect(() => { load() }, [id])

  async function regenWeek() {
    setLoadingW(true); setErr('')
    try {
      const r: any = await api.strategy.regenerateWeekly(id)
      setWb(r); setExists(true)
    } catch (e: any) { setErr(e.message || 'Erro') }
    finally { setLoadingW(false) }
  }

  async function regenInsights() {
    setLoadingI(true); setErr('')
    try {
      const r: any = await api.strategy.regenerateInsights(id)
      setInsights(r)
    } catch (e: any) { setErr(e.message || 'Erro') }
    finally { setLoadingI(false) }
  }

  async function dismiss(insightId: number) {
    await api.strategy.dismissInsight(insightId)
    setInsights(prev => prev.filter(i => i.id !== insightId))
  }

  return (
    <div className="p-4 md:p-6 space-y-4 max-w-5xl">
      <div>
        <h1 className="text-lg md:text-xl font-bold text-white">Central Estratégica</h1>
        <p className="text-xs text-gray-400 mt-0.5">Cérebro semanal + insights inteligentes da marca</p>
      </div>

      {err && <div className="card bg-red-900/20 border-red-800/50 text-xs text-red-300">{err}</div>}

      {/* Insights */}
      <section className="space-y-2">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-white">Insights ativos</h2>
          <button onClick={regenInsights} disabled={loadingI} className="text-xs text-violet-400">{loadingI ? 'Pensando...' : '↻ Regenerar'}</button>
        </div>
        {insights.length === 0 ? (
          <p className="text-xs text-gray-500">Nenhum insight ativo. Clique em regenerar.</p>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
            {insights.map(i => (
              <div key={i.id} className={`card border ${SEV_COLOR[i.severity] || SEV_COLOR.info}`}>
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0 flex-1">
                    <p className="text-xs font-semibold flex items-center gap-1.5">
                      <span>{SEV_ICON[i.severity] || 'ℹ'}</span>
                      <span>{i.title}</span>
                      <span className="text-[10px] opacity-60">· {i.kind}</span>
                    </p>
                    <p className="text-xs mt-1 opacity-90">{i.message}</p>
                    {i.evidence && <p className="text-[10px] mt-1 opacity-60">Evidência: {i.evidence}</p>}
                  </div>
                  <button onClick={() => dismiss(i.id)} className="text-xs opacity-60 hover:opacity-100 shrink-0">×</button>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* Weekly brain */}
      <section className="space-y-2">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-white">Cérebro da semana</h2>
          <button onClick={regenWeek} disabled={loadingW} className="text-xs text-violet-400">{loadingW ? 'Pensando...' : '↻ Regenerar'}</button>
        </div>
        {exists === false ? (
          <p className="text-xs text-gray-500">Cérebro semanal ainda não gerado.</p>
        ) : wb ? (
          <div className="space-y-3">
            {wb.focus && (
              <div className="card bg-violet-900/10 border-violet-800/50">
                <p className="text-xs text-violet-400 font-semibold mb-1">FOCO DA SEMANA</p>
                <p className="text-sm text-gray-200">{wb.focus}</p>
              </div>
            )}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
              <ListCard title="Prioridades" items={wb.priorities} color="text-violet-300" />
              <ListCard title="Oportunidades" items={wb.opportunities} color="text-green-300" />
              <ListCard title="Alertas" items={wb.alerts} color="text-yellow-300" />
              <ListCard title="Riscos" items={wb.risks} color="text-red-300" />
            </div>
            {wb.audience_behavior && (
              <div className="card">
                <p className="text-xs text-cyan-400 font-semibold mb-1">COMPORTAMENTO DA AUDIÊNCIA</p>
                <p className="text-xs text-gray-300">{wb.audience_behavior}</p>
              </div>
            )}
            {wb.trends?.length > 0 && (
              <div className="card">
                <p className="text-xs text-pink-400 font-semibold mb-1">TENDÊNCIAS APLICÁVEIS</p>
                <ul className="text-xs text-gray-300 space-y-0.5">
                  {wb.trends.map((t, i) => <li key={i}>· {t}</li>)}
                </ul>
              </div>
            )}
            {wb.emotional_sequence?.length > 0 && (
              <div className="card">
                <p className="text-xs text-orange-400 font-semibold mb-2">SEQUÊNCIA EMOCIONAL DA SEMANA</p>
                <div className="space-y-1.5">
                  {wb.emotional_sequence.map((d, i) => (
                    <div key={i} className="flex items-start gap-2 text-xs">
                      <span className="font-semibold text-gray-200 w-16 shrink-0">{d.day}</span>
                      <span className="px-1.5 py-0.5 rounded bg-orange-900/30 text-orange-200 text-[10px] shrink-0">{d.emotion}</span>
                      <span className="text-gray-400 flex-1">{d.intent} · <span className="text-gray-500">{d.format_suggestion}</span></span>
                    </div>
                  ))}
                </div>
              </div>
            )}
            {wb.generated_at && (
              <p className="text-[10px] text-gray-600">Gerado em {new Date(wb.generated_at).toLocaleString('pt-BR')}</p>
            )}
          </div>
        ) : null}
      </section>
    </div>
  )
}

function ListCard({ title, items, color }: { title: string; items: string[]; color: string }) {
  return (
    <div className="card">
      <p className={`text-xs font-semibold mb-1 ${color}`}>{title.toUpperCase()}</p>
      {(!items || items.length === 0) ? (
        <p className="text-xs text-gray-500">—</p>
      ) : (
        <ul className="text-xs text-gray-300 space-y-0.5">
          {items.map((x, i) => <li key={i}>· {x}</li>)}
        </ul>
      )}
    </div>
  )
}
