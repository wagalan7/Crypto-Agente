import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { api } from '../services/api'
import type { ContentPiece } from '../types'
import { STATUS_LABELS, STATUS_COLORS, FORMAT_LABELS, OBJECTIVE_LABELS, OBJECTIVE_COLORS, FUNNEL_STAGE_LABELS } from '../types'

const STATUSES = ['pending', 'approved', 'recorded', 'published'] as const

export function ContentPage() {
  const { clientId } = useParams<{ clientId: string }>()
  const id = Number(clientId)
  const [contents, setContents] = useState<ContentPiece[]>([])
  const [filter, setFilter] = useState<string>('')
  const [selected, setSelected] = useState<ContentPiece | null>(null)
  const [mediaUrl, setMediaUrl] = useState('')
  const [publishing, setPublishing] = useState(false)

  async function load() {
    const data: any = await api.content.list(id, filter || undefined)
    setContents(data)
  }

  useEffect(() => { load() }, [id, filter])

  useEffect(() => {
    if (selected) setMediaUrl(selected.media_url || '')
  }, [selected?.id])

  async function approve(contentId: number) {
    const updated: any = await api.content.approve(contentId)
    setContents(prev => prev.map(c => c.id === contentId ? updated : c))
    if (selected?.id === contentId) setSelected(updated)
  }

  async function setStatus(contentId: number, status: string) {
    const updated: any = await api.content.update(contentId, { status })
    setContents(prev => prev.map(c => c.id === contentId ? updated : c))
    if (selected?.id === contentId) setSelected(updated)
  }

  async function saveMediaUrl() {
    if (!selected) return
    const updated: any = await api.content.update(selected.id, { media_url: mediaUrl || null })
    setContents(prev => prev.map(c => c.id === selected.id ? updated : c))
    setSelected(updated)
  }

  async function publishNow() {
    if (!selected) return
    if (!confirm(`Publicar agora no ${selected.platform}?`)) return
    setPublishing(true)
    try {
      await api.social.publish(selected.id)
      const refreshed: any = await api.content.get(selected.id)
      setContents(prev => prev.map(c => c.id === selected.id ? refreshed : c))
      setSelected(refreshed)
      alert('Publicado com sucesso!')
    } catch (e: any) {
      alert('Erro ao publicar: ' + e.message)
    } finally {
      setPublishing(false)
    }
  }

  // Mobile: show detail as full overlay
  if (selected) {
    return (
      <div className="p-4 md:p-6 max-w-2xl">
        <button onClick={() => setSelected(null)} className="flex items-center gap-1.5 text-sm text-gray-400 mb-4">
          ← Voltar
        </button>
        <div className="space-y-4">
          <div className="flex items-start gap-2 flex-wrap">
            <span className={`badge ${STATUS_COLORS[selected.status]}`}>{STATUS_LABELS[selected.status]}</span>
            <span className="badge bg-gray-800 text-gray-400">{selected.platform}</span>
            <span className="badge bg-gray-800 text-gray-400">{FORMAT_LABELS[selected.format] || selected.format}</span>
            <span className={`badge border ${OBJECTIVE_COLORS[selected.objective] || 'bg-gray-700 text-gray-300 border-gray-600'}`}>
              {OBJECTIVE_LABELS[selected.objective] || selected.objective}
            </span>
          </div>

          <h2 className="text-base font-semibold text-white">{selected.title}</h2>

          {selected.hook && (
            <div className="card">
              <p className="text-xs text-violet-400 font-semibold mb-1">HOOK</p>
              <p className="text-sm text-gray-300">{selected.hook}</p>
            </div>
          )}
          {selected.script && (
            <div className="card">
              <p className="text-xs text-violet-400 font-semibold mb-1">ROTEIRO</p>
              <div className="max-h-60 overflow-y-auto">
                <p className="text-sm text-gray-300 whitespace-pre-wrap">{selected.script}</p>
              </div>
            </div>
          )}
          {selected.copy && (
            <div className="card">
              <p className="text-xs text-violet-400 font-semibold mb-1">COPY</p>
              <p className="text-sm text-gray-300">{selected.copy}</p>
            </div>
          )}
          {selected.design_brief && (
            <div className="card">
              <p className="text-xs text-violet-400 font-semibold mb-1">BRIEFING VISUAL</p>
              <div className="max-h-48 overflow-y-auto">
                <p className="text-sm text-gray-300 whitespace-pre-wrap">{selected.design_brief}</p>
              </div>
            </div>
          )}
          {selected.strategic_note && (
            <div className="card bg-violet-900/10 border-violet-800/50">
              <p className="text-xs text-violet-400 font-semibold mb-1">NOTA ESTRATÉGICA</p>
              <p className="text-sm text-gray-300">{selected.strategic_note}</p>
            </div>
          )}

          {(selected.objective_reasoning || selected.emotion_used || selected.funnel_stage || selected.format_reasoning) && (
            <div className="card bg-indigo-900/10 border-indigo-800/50 space-y-2">
              <p className="text-xs text-indigo-300 font-semibold">JUSTIFICATIVA ESTRATÉGICA</p>
              {selected.objective_reasoning && (
                <div>
                  <p className="text-[10px] text-indigo-400 font-semibold">POR QUE ESSE OBJETIVO</p>
                  <p className="text-xs text-gray-300">{selected.objective_reasoning}</p>
                </div>
              )}
              <div className="flex flex-wrap gap-1.5">
                {selected.emotion_used && (
                  <span className="text-[10px] px-2 py-0.5 rounded bg-orange-900/30 text-orange-200 border border-orange-800/50">
                    Emoção: {selected.emotion_used}
                  </span>
                )}
                {selected.funnel_stage && (
                  <span className="text-[10px] px-2 py-0.5 rounded bg-cyan-900/30 text-cyan-200 border border-cyan-800/50">
                    Funil: {FUNNEL_STAGE_LABELS[selected.funnel_stage] || selected.funnel_stage}
                  </span>
                )}
              </div>
              {selected.format_reasoning && (
                <div>
                  <p className="text-[10px] text-indigo-400 font-semibold">POR QUE ESSE FORMATO</p>
                  <p className="text-xs text-gray-300">{selected.format_reasoning}</p>
                </div>
              )}
            </div>
          )}

          <div className="card">
            <p className="text-xs text-violet-400 font-semibold mb-2">MÍDIA (URL pública)</p>
            <div className="flex gap-2">
              <input
                type="url"
                className="input-field text-xs"
                placeholder="https://... (imagem ou vídeo público)"
                value={mediaUrl}
                onChange={e => setMediaUrl(e.target.value)}
              />
              <button onClick={saveMediaUrl} className="btn-secondary px-3 py-1.5 text-xs shrink-0">Salvar</button>
            </div>
            <p className="text-[11px] text-gray-500 mt-1.5">Obrigatório para publicar no Instagram. URL deve ser pública.</p>
          </div>

          {selected.external_post_id && (
            <div className="card bg-green-900/10 border-green-800/40">
              <p className="text-xs text-green-400 font-semibold mb-1">PUBLICADO</p>
              <p className="text-xs text-gray-400">ID externo: <span className="font-mono">{selected.external_post_id}</span></p>
            </div>
          )}

          {selected.publish_error && (
            <div className="card bg-red-900/10 border-red-800/40">
              <p className="text-xs text-red-400 font-semibold mb-1">ERRO AO PUBLICAR</p>
              <p className="text-xs text-gray-400">{selected.publish_error}</p>
            </div>
          )}

          <div className="space-y-2 pt-1">
            {selected.status === 'pending' && (
              <button onClick={() => approve(selected.id)} className="btn-primary w-full py-3">
                ✓ Aprovar conteúdo
              </button>
            )}
            {selected.status === 'approved' && (
              <button onClick={() => setStatus(selected.id, 'recorded')}
                className="btn-primary w-full py-3 bg-blue-600 hover:bg-blue-700">
                Marcar como gravado
              </button>
            )}
            {(selected.status === 'approved' || selected.status === 'recorded') &&
             (selected.platform === 'instagram' || selected.platform === 'facebook') && (
              <button onClick={publishNow} disabled={publishing}
                className="btn-primary w-full py-3 bg-violet-600 hover:bg-violet-700">
                {publishing ? 'Publicando...' : `📤 Publicar agora no ${selected.platform}`}
              </button>
            )}
            {selected.status === 'recorded' && (
              <button onClick={() => setStatus(selected.id, 'published')}
                className="btn-secondary w-full py-2 text-xs">
                Marcar como publicado (manualmente)
              </button>
            )}
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="p-4 md:p-6 max-w-5xl">
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-lg font-bold text-white">Conteúdo</h1>
      </div>

      {/* Filter pills - horizontal scroll on mobile */}
      <div className="flex gap-1.5 overflow-x-auto pb-2 -mx-4 px-4 md:mx-0 md:px-0 mb-4 scrollbar-none">
        <button onClick={() => setFilter('')}
          className={`px-3 py-1.5 rounded-lg text-xs font-medium border shrink-0 transition-colors ${
            !filter ? 'bg-violet-600/20 border-violet-500 text-violet-300' : 'bg-gray-800 border-gray-700 text-gray-400'
          }`}>
          Todos
        </button>
        {STATUSES.map(s => (
          <button key={s} onClick={() => setFilter(s)}
            className={`px-3 py-1.5 rounded-lg text-xs font-medium border shrink-0 transition-colors ${
              filter === s ? 'bg-violet-600/20 border-violet-500 text-violet-300' : 'bg-gray-800 border-gray-700 text-gray-400'
            }`}>
            {STATUS_LABELS[s]}
          </button>
        ))}
      </div>

      {contents.length === 0 ? (
        <div className="card text-center py-12">
          <p className="text-gray-500 text-sm">Nenhum conteúdo encontrado</p>
          <p className="text-gray-600 text-xs mt-1">Use os Agentes IA para gerar conteúdo</p>
        </div>
      ) : (
        <div className="space-y-2">
          {contents.map(content => (
            <button
              key={content.id}
              onClick={() => setSelected(content)}
              className="card w-full text-left active:border-violet-600 hover:border-violet-700 transition-colors"
            >
              <div className="flex items-start gap-2">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-1.5 mb-1 flex-wrap">
                    <span className={`badge text-[10px] ${STATUS_COLORS[content.status]}`}>
                      {STATUS_LABELS[content.status]}
                    </span>
                    <span className={`badge border text-[10px] ${OBJECTIVE_COLORS[content.objective] || 'bg-gray-700 text-gray-300 border-gray-600'}`}>
                      {OBJECTIVE_LABELS[content.objective] || content.objective}
                    </span>
                  </div>
                  <p className="text-sm font-medium text-white truncate">{content.title}</p>
                  {content.hook && (
                    <p className="text-xs text-gray-400 truncate mt-0.5">{content.hook}</p>
                  )}
                </div>
                <div className="text-right shrink-0 ml-2">
                  <p className="text-xs text-gray-500">{content.platform}</p>
                  <p className="text-xs text-gray-600">{FORMAT_LABELS[content.format] || content.format}</p>
                </div>
              </div>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
