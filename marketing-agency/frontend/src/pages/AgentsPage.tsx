import { useEffect, useState } from 'react'
import { useParams, useNavigate, useSearchParams } from 'react-router-dom'
import { api } from '../services/api'
import { AgentStream } from '../components/AgentStream'

const FORMATS = ['reels', 'shorts', 'carousel', 'story', 'post', 'youtube']
const PLATFORMS = ['instagram', 'tiktok', 'youtube', 'linkedin', 'twitter']
const OBJECTIVES = ['attract', 'connect', 'authority', 'sell', 'break_objection']
const OBJECTIVE_LABELS: Record<string, string> = {
  attract: 'Atrair', connect: 'Conectar', authority: 'Autoridade',
  sell: 'Vender', break_objection: 'Quebrar Objeção',
}

type AgentTab = 'auto' | 'strategy' | 'script' | 'trend' | 'design' | 'amplifier' | 'analytics'

const TABS: { id: AgentTab; label: string; icon: string }[] = [
  { id: 'auto', label: 'Auto-Criar', icon: '✦' },
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
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const [tab, setTab] = useState<AgentTab>((params.get('tab') as AgentTab) || 'auto')

  const [autoForm, setAutoForm] = useState({
    site_url: params.get('site_url') || '',
    topic: params.get('topic') || '',
    format: params.get('format') || 'post',
    platform: params.get('platform') || 'instagram',
    objective: params.get('objective') || '',
  })
  const prefillNotice = !!(params.get('topic') || params.get('objective'))
  useEffect(() => {
    if (params.get('tab')) setTab(params.get('tab') as AgentTab)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params.toString()])
  const [prereq, setPrereq] = useState<{ persona: boolean; product: boolean; primary: boolean } | null>(null)
  useEffect(() => {
    (async () => {
      try {
        const [p, prods] = await Promise.allSettled([
          api.persona.get(id),
          api.products.list(id),
        ])
        const personaOk = p.status === 'fulfilled' && !!(p.value as any)?.id
        const list = prods.status === 'fulfilled' ? ((prods.value as any[]) || []) : []
        const productOk = list.some(x => x.is_active)
        const primaryOk = list.some(x => x.is_primary && x.is_active)
        setPrereq({ persona: personaOk, product: productOk, primary: primaryOk })
      } catch {
        setPrereq({ persona: false, product: false, primary: false })
      }
    })()
  }, [id])
  const [autoStatus, setAutoStatus] = useState('')
  const [autoOutput, setAutoOutput] = useState('')
  const [autoResult, setAutoResult] = useState<{ content_id: number; image_url: string; title: string; objective?: string; objective_reasoning?: string; emotion_used?: string; funnel_stage?: string; format_reasoning?: string } | null>(null)
  const [autoRunning, setAutoRunning] = useState(false)
  const [inspirations, setInspirations] = useState<Array<{ id: number; label?: string | null; image_url?: string | null; source_value: string }>>([])
  const [selectedInspirationIds, setSelectedInspirationIds] = useState<number[]>([])

  useEffect(() => {
    api.inspirations.list(id).then((r: any) => setInspirations(r || [])).catch(() => {})
  }, [id])

  function toggleInspiration(insId: number) {
    setSelectedInspirationIds(prev => prev.includes(insId) ? prev.filter(x => x !== insId) : [...prev, insId])
  }

  async function runAuto() {
    setAutoRunning(true)
    setAutoStatus('')
    setAutoOutput('')
    setAutoResult(null)
    try {
      const gen = api.agents.auto(id, autoForm.site_url, autoForm.topic, autoForm.format, autoForm.platform, autoForm.objective, selectedInspirationIds.length ? selectedInspirationIds : undefined)
      for await (const ev of gen) {
        if (ev.type === 'status') setAutoStatus(ev.payload as string)
        else if (ev.type === 'chunk') setAutoOutput(prev => prev + ev.payload)
        else if (ev.type === 'error') setAutoStatus(`Erro: ${ev.payload}`)
        else if (ev.type === 'done') {
          const result = ev.payload as any
          setAutoResult(result)
          setAutoStatus('Pronto! Conteúdo criado e salvo.')
        }
      }
    } catch (e: any) {
      setAutoStatus(`Erro: ${e.message}`)
    } finally {
      setAutoRunning(false)
    }
  }

  async function saveContent(payload: {
    title: string; format: string; platform: string; objective: string;
    script?: string; copy?: string; design_brief?: string; strategic_note?: string;
  }) {
    await api.content.create({ client_id: id, ...payload })
    setTimeout(() => navigate(`/client/${clientId}/content`), 800)
    return payload
  }

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

      {tab === 'auto' && (
        <div className="card space-y-4">
          <div>
            <h2 className="text-sm font-semibold text-white">Auto-Criar Conteúdo</h2>
            <p className="text-xs text-gray-400 mt-0.5">
              Cole o link do seu site/produto. A IA lê, junta com o briefing do cliente, e cria
              hook, roteiro, legenda, briefing visual <span className="text-violet-400">e a imagem</span> — tudo de uma vez.
            </p>
            {prefillNotice && (
              <p className="text-[11px] text-violet-300 mt-1.5 bg-violet-900/20 border border-violet-700/50 rounded px-2 py-1">
                ✦ Pré-preenchido a partir de um insight da Central Estratégica
              </p>
            )}
            {prereq && (!prereq.persona || !prereq.product) && (
              <div className="mt-2 space-y-1.5 bg-yellow-900/15 border border-yellow-800/50 rounded px-2.5 py-2">
                <p className="text-[11px] text-yellow-300 font-semibold">⚠ A IA precisa de contexto pra criar conteúdo afiado</p>
                {!prereq.persona && (
                  <p className="text-[11px] text-gray-300">
                    · Persona ausente —{' '}
                    <button onClick={() => navigate(`/client/${clientId}/persona`)} className="text-violet-300 underline">gerar persona</button>
                  </p>
                )}
                {!prereq.product && (
                  <p className="text-[11px] text-gray-300">
                    · Nenhum produto ativo —{' '}
                    <button onClick={() => navigate(`/client/${clientId}/products`)} className="text-violet-300 underline">cadastrar produto</button>
                  </p>
                )}
                {prereq.product && !prereq.primary && (
                  <p className="text-[11px] text-gray-400">
                    · Sem produto principal definido —{' '}
                    <button onClick={() => navigate(`/client/${clientId}/products`)} className="text-violet-300 underline">marcar 1 como principal</button>
                  </p>
                )}
              </div>
            )}
            {prereq && prereq.persona && prereq.product && prereq.primary && (
              <p className="text-[11px] text-green-400 mt-1.5">✓ Persona, produto e produto principal configurados — IA com contexto completo</p>
            )}
          </div>
          <div className="space-y-3">
            <div>
              <label className="text-xs text-gray-400 mb-1 block">URL do site / página de vendas (opcional)</label>
              <input className="input-field" placeholder="https://meusite.com.br/produto"
                value={autoForm.site_url} onChange={e => setAutoForm(p => ({ ...p, site_url: e.target.value }))} />
            </div>
            <div>
              <label className="text-xs text-gray-400 mb-1 block">Tema (opcional — IA escolhe se vazio)</label>
              <input className="input-field" placeholder="Ex: lançamento da nova linha verão"
                value={autoForm.topic} onChange={e => setAutoForm(p => ({ ...p, topic: e.target.value }))} />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="text-xs text-gray-400 mb-1 block">Formato</label>
                <select className="input-field" value={autoForm.format}
                  onChange={e => setAutoForm(p => ({ ...p, format: e.target.value }))}>
                  {FORMATS.map(f => <option key={f} value={f}>{f}</option>)}
                </select>
              </div>
              <div>
                <label className="text-xs text-gray-400 mb-1 block">Plataforma</label>
                <select className="input-field" value={autoForm.platform}
                  onChange={e => setAutoForm(p => ({ ...p, platform: e.target.value }))}>
                  {PLATFORMS.map(p => <option key={p} value={p}>{p}</option>)}
                </select>
              </div>
            </div>
            <div>
              <label className="text-xs text-gray-400 mb-1.5 block">Objetivo (opcional — IA decide e justifica)</label>
              <div className="flex flex-wrap gap-1.5">
                <button onClick={() => setAutoForm(p => ({ ...p, objective: '' }))}
                  className={`px-2.5 py-1 rounded-lg text-xs font-medium border transition-colors ${
                    !autoForm.objective
                      ? 'bg-violet-600/20 border-violet-500 text-violet-300'
                      : 'bg-gray-800 border-gray-700 text-gray-400'
                  }`}>
                  ✦ IA decide
                </button>
                {OBJECTIVES.map(o => (
                  <button key={o} onClick={() => setAutoForm(p => ({ ...p, objective: o }))}
                    className={`px-2.5 py-1 rounded-lg text-xs font-medium border transition-colors ${
                      autoForm.objective === o
                        ? 'bg-violet-600/20 border-violet-500 text-violet-300'
                        : 'bg-gray-800 border-gray-700 text-gray-400'
                    }`}>
                    {OBJECTIVE_LABELS[o]}
                  </button>
                ))}
              </div>
            </div>
          </div>

          {inspirations.length > 0 && (
            <div>
              <label className="text-xs text-gray-400 mb-1.5 block">
                Referências visuais (opcional — IA replica estética){selectedInspirationIds.length > 0 && <span className="text-violet-400"> · {selectedInspirationIds.length} selecionada(s)</span>}
              </label>
              <div className="flex gap-2 overflow-x-auto pb-1 scrollbar-none">
                {inspirations.map(ins => {
                  const selected = selectedInspirationIds.includes(ins.id)
                  return (
                    <button
                      key={ins.id}
                      type="button"
                      onClick={() => toggleInspiration(ins.id)}
                      className={`shrink-0 w-20 rounded-lg border-2 p-1 text-left transition-colors ${selected ? 'border-violet-500 bg-violet-900/30' : 'border-gray-700 bg-gray-900'}`}
                      title={ins.label || ins.source_value}
                    >
                      {ins.image_url ? (
                        <img src={ins.image_url} alt="" className="w-full h-16 object-cover rounded" />
                      ) : (
                        <div className="w-full h-16 bg-gray-800 rounded flex items-center justify-center text-[10px] text-gray-500">{ins.source_value.slice(0, 20)}</div>
                      )}
                      <p className="text-[9px] text-gray-400 truncate mt-1">{ins.label || 'ref'}</p>
                    </button>
                  )
                })}
              </div>
            </div>
          )}

          <div className="flex items-center justify-between">
            {autoStatus && (
              <p className="text-xs text-gray-400 flex items-center gap-1.5">
                {autoRunning && <span className="w-1.5 h-1.5 rounded-full bg-violet-400 animate-pulse" />}
                {autoStatus}
              </p>
            )}
            <button onClick={runAuto} disabled={autoRunning} className="btn-primary ml-auto">
              {autoRunning ? 'Gerando...' : '✦ Gerar Conteúdo Completo'}
            </button>
          </div>

          {autoOutput && (
            <div className="card max-h-60 overflow-y-auto bg-gray-950">
              <pre className="agent-output text-[11px]">{autoOutput}</pre>
            </div>
          )}

          {autoResult && (
            <div className="card bg-violet-900/10 border-violet-700/40 space-y-3">
              <p className="text-xs text-violet-300 font-semibold">✓ Conteúdo criado e salvo</p>
              <p className="text-sm text-white font-medium">{autoResult.title}</p>
              <img src={autoResult.image_url} alt="" className="rounded-lg w-full max-w-sm" />

              {(autoResult.objective_reasoning || autoResult.emotion_used || autoResult.funnel_stage || autoResult.format_reasoning) && (
                <div className="space-y-2 pt-2 border-t border-violet-800/40">
                  <p className="text-[10px] text-violet-400 font-semibold">POR QUE A IA TOMOU ESSAS DECISÕES</p>
                  {autoResult.objective_reasoning && (
                    <div>
                      <p className="text-[10px] text-violet-300">Objetivo: {autoResult.objective}</p>
                      <p className="text-xs text-gray-300">{autoResult.objective_reasoning}</p>
                    </div>
                  )}
                  <div className="flex flex-wrap gap-1.5">
                    {autoResult.emotion_used && (
                      <span className="text-[10px] px-2 py-0.5 rounded bg-orange-900/30 text-orange-200">
                        Emoção: {autoResult.emotion_used}
                      </span>
                    )}
                    {autoResult.funnel_stage && (
                      <span className="text-[10px] px-2 py-0.5 rounded bg-cyan-900/30 text-cyan-200">
                        Funil: {autoResult.funnel_stage}
                      </span>
                    )}
                  </div>
                  {autoResult.format_reasoning && (
                    <p className="text-xs text-gray-300">{autoResult.format_reasoning}</p>
                  )}
                </div>
              )}

              <div className="flex gap-2">
                <button onClick={() => navigate(`/client/${clientId}/content`)} className="btn-primary text-xs flex-1">
                  Ver na aba Conteúdo
                </button>
              </div>
              <p className="text-[11px] text-gray-500">
                Imagem gerada pelo Pollinations.ai (URL pública). Pronta pra publicar.
              </p>
            </div>
          )}
        </div>
      )}

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
            onSave={(output) => saveContent({
              title: `Estratégia semanal — ${new Date().toLocaleDateString('pt-BR')}`,
              format: 'post', platform: 'instagram', objective: 'authority',
              strategic_note: output,
            })}
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
            onSave={(output) => saveContent({
              title: scriptForm.topic || 'Roteiro sem título',
              format: scriptForm.format, platform: scriptForm.platform, objective: scriptForm.objective,
              script: output,
            })}
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
            onSave={(output) => saveContent({
              title: `Design — ${designForm.topic || 'sem título'}`,
              format: designForm.format, platform: designForm.platform, objective: 'attract',
              design_brief: output,
            })}
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
            onSave={(output) => saveContent({
              title: amplifierInput ? amplifierInput.slice(0, 60) : 'Ideia amplificada',
              format: 'post', platform: 'instagram', objective: 'connect',
              copy: output,
            })}
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
