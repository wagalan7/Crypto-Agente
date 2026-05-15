import { useState } from 'react'
import { useParams } from 'react-router-dom'
import { api } from '../services/api'
import { AgentStream } from '../components/AgentStream'

const FORMATS = ['reels', 'shorts', 'carousel', 'story', 'post', 'youtube']
const PLATFORMS = ['instagram', 'tiktok', 'youtube', 'linkedin', 'twitter']
const OBJECTIVES = ['attract', 'connect', 'authority', 'sell', 'break_objection']
const OBJECTIVE_LABELS: Record<string, string> = {
  attract: 'Atrair', connect: 'Conectar', authority: 'Autoridade',
  sell: 'Vender', break_objection: 'Quebrar Objeção',
}

type AgentTab = 'strategy' | 'script' | 'trend' | 'design' | 'amplifier' | 'analytics'

const TABS: { id: AgentTab; label: string; icon: string }[] = [
  { id: 'strategy', label: 'Estratégia', icon: '◎' },
  { id: 'script', label: 'Roteiro', icon: '◈' },
  { id: 'trend', label: 'Trends', icon: '◉' },
  { id: 'design', label: 'Design', icon: '◫' },
  { id: 'amplifier', label: 'Amplificador', icon: '⬡' },
  { id: 'analytics', label: 'Analytics', icon: '◧' },
]

export function AgentsPage() {
  const { clientId } = useParams<{ clientId: string }>()
  const id = Number(clientId)
  const [tab, setTab] = useState<AgentTab>('strategy')

  const [scriptForm, setScriptForm] = useState({ topic: '', format: 'reels', platform: 'instagram', objective: 'attract' })
  const [trendInput, setTrendInput] = useState('')
  const [designForm, setDesignForm] = useState({ topic: '', format: 'carousel', platform: 'instagram', references: '' })
  const [amplifierInput, setAmplifierInput] = useState('')
  const [analyticsInput, setAnalyticsInput] = useState('')

  return (
    <div className="p-4 md:p-6 space-y-4 max-w-4xl">
      <h1 className="text-lg font-bold text-white">Agentes de IA</h1>

      {/* Tab scroll horizontal no mobile */}
      <div className="flex gap-1.5 overflow-x-auto pb-1 scrollbar-none -mx-4 px-4 md:mx-0 md:px-0 md:flex-wrap">
        {TABS.map(t => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs font-medium transition-colors border shrink-0 ${
              tab === t.id
                ? 'bg-violet-600/20 border-violet-500 text-violet-300'
                : 'bg-gray-900 border-gray-700 text-gray-400'
            }`}
          >
            {t.icon} {t.label}
          </button>
        ))}
      </div>

      {tab === 'strategy' && (
        <div className="card space-y-4">
          <div>
            <h2 className="text-sm font-semibold text-white">Agente de Estratégia</h2>
            <p className="text-xs text-gray-400 mt-0.5">Mix editorial, calendário e posicionamento baseado no perfil do cliente.</p>
          </div>
          <AgentStream
            label="Gerar Estratégia"
            placeholder="Clique para gerar estratégia personalizada"
            onRun={() => api.agents.strategy(id, 'semanal')}
          />
        </div>
      )}

      {tab === 'script' && (
        <div className="card space-y-4">
          <h2 className="text-sm font-semibold text-white">Agente de Roteiro</h2>
          <div className="space-y-3">
            <div>
              <label className="text-xs text-gray-400 mb-1 block">Tema *</label>
              <input className="input-field" placeholder="Ex: 3 erros que impedem seu crescimento..."
                value={scriptForm.topic} onChange={e => setScriptForm(p => ({ ...p, topic: e.target.value }))} />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="text-xs text-gray-400 mb-1 block">Formato</label>
                <select className="input-field" value={scriptForm.format}
                  onChange={e => setScriptForm(p => ({ ...p, format: e.target.value }))}>
                  {FORMATS.map(f => <option key={f} value={f}>{f}</option>)}
                </select>
              </div>
              <div>
                <label className="text-xs text-gray-400 mb-1 block">Plataforma</label>
                <select className="input-field" value={scriptForm.platform}
                  onChange={e => setScriptForm(p => ({ ...p, platform: e.target.value }))}>
                  {PLATFORMS.map(p => <option key={p} value={p}>{p}</option>)}
                </select>
              </div>
            </div>
            <div>
              <label className="text-xs text-gray-400 mb-1.5 block">Objetivo</label>
              <div className="flex flex-wrap gap-1.5">
                {OBJECTIVES.map(o => (
                  <button key={o} onClick={() => setScriptForm(p => ({ ...p, objective: o }))}
                    className={`px-2.5 py-1 rounded-lg text-xs font-medium border transition-colors ${
                      scriptForm.objective === o
                        ? 'bg-violet-600/20 border-violet-500 text-violet-300'
                        : 'bg-gray-800 border-gray-700 text-gray-400'
                    }`}>
                    {OBJECTIVE_LABELS[o]}
                  </button>
                ))}
              </div>
            </div>
          </div>
          <AgentStream
            label="Gerar Roteiro"
            placeholder="Preencha o tema acima"
            onRun={() => api.agents.script(id, scriptForm.topic, scriptForm.format, scriptForm.platform, scriptForm.objective)}
          />
        </div>
      )}

      {tab === 'trend' && (
        <div className="card space-y-4">
          <div>
            <h2 className="text-sm font-semibold text-white">Agente de Trends</h2>
            <p className="text-xs text-gray-400 mt-0.5">Filtra trends para o posicionamento estratégico do cliente.</p>
          </div>
          <div>
            <label className="text-xs text-gray-400 mb-1 block">Trends que estão circulando</label>
            <textarea
              className="input-field min-h-24 resize-none"
              placeholder="Ex: Challenge do gelo no TikTok, formato POV, vídeos de 'dia na vida'..."
              value={trendInput}
              onChange={e => setTrendInput(e.target.value)}
            />
          </div>
          <AgentStream
            label="Analisar Trends"
            placeholder="Descreva as trends que estão acontecendo"
            onRun={() => api.agents.trend(id, trendInput || 'trends gerais da semana')}
          />
        </div>
      )}

      {tab === 'design' && (
        <div className="card space-y-4">
          <h2 className="text-sm font-semibold text-white">Agente de Design</h2>
          <div className="space-y-3">
            <div>
              <label className="text-xs text-gray-400 mb-1 block">Tema *</label>
              <input className="input-field" placeholder="Ex: 5 hábitos de pessoas ricas"
                value={designForm.topic} onChange={e => setDesignForm(p => ({ ...p, topic: e.target.value }))} />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="text-xs text-gray-400 mb-1 block">Formato</label>
                <select className="input-field" value={designForm.format}
                  onChange={e => setDesignForm(p => ({ ...p, format: e.target.value }))}>
                  {FORMATS.map(f => <option key={f} value={f}>{f}</option>)}
                </select>
              </div>
              <div>
                <label className="text-xs text-gray-400 mb-1 block">Plataforma</label>
                <select className="input-field" value={designForm.platform}
                  onChange={e => setDesignForm(p => ({ ...p, platform: e.target.value }))}>
                  {PLATFORMS.map(p => <option key={p} value={p}>{p}</option>)}
                </select>
              </div>
            </div>
            <div>
              <label className="text-xs text-gray-400 mb-1 block">Referências</label>
              <input className="input-field" placeholder="Ex: @perfil1, estilo minimalista, tons escuros"
                value={designForm.references} onChange={e => setDesignForm(p => ({ ...p, references: e.target.value }))} />
            </div>
          </div>
          <AgentStream
            label="Gerar Briefing Visual"
            placeholder="Preencha o tema acima"
            onRun={() => api.agents.design(id, designForm.topic, designForm.format, designForm.platform, designForm.references)}
          />
        </div>
      )}

      {tab === 'amplifier' && (
        <div className="card space-y-4">
          <div>
            <h2 className="text-sm font-semibold text-white">Amplificador de Ideias</h2>
            <p className="text-xs text-gray-400 mt-0.5">Transforma ideia bruta em conteúdo estratégico de alto impacto.</p>
          </div>
          <div>
            <label className="text-xs text-gray-400 mb-1 block">Sua ideia (pode ser bruta)</label>
            <textarea
              className="input-field min-h-28 resize-none"
              placeholder="Ex: Quero falar que não precisa de muito dinheiro pra começar um negócio..."
              value={amplifierInput}
              onChange={e => setAmplifierInput(e.target.value)}
            />
          </div>
          <AgentStream
            label="Amplificar Ideia"
            placeholder="Digite sua ideia acima"
            onRun={() => api.agents.amplifier(id, amplifierInput || 'ideia geral sobre o nicho')}
          />
        </div>
      )}

      {tab === 'analytics' && (
        <div className="card space-y-4">
          <div>
            <h2 className="text-sm font-semibold text-white">Agente de Analytics</h2>
            <p className="text-xs text-gray-400 mt-0.5">Analisa métricas e gera insights para otimizar sua estratégia.</p>
          </div>
          <div>
            <label className="text-xs text-gray-400 mb-1 block">Dados de métricas</label>
            <textarea
              className="input-field min-h-28 resize-none"
              placeholder="Ex: Reels de segunda: 12k views, 3% retenção, 450 curtidas..."
              value={analyticsInput}
              onChange={e => setAnalyticsInput(e.target.value)}
            />
          </div>
          <AgentStream
            label="Analisar Métricas"
            placeholder="Cole seus dados de métricas acima"
            onRun={() => api.agents.analytics(id, analyticsInput || 'sem dados ainda')}
          />
        </div>
      )}
    </div>
  )
}
