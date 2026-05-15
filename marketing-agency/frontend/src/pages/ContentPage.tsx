import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { api } from '../services/api'
import type { ContentPiece } from '../types'
import { STATUS_LABELS, STATUS_COLORS, FORMAT_LABELS, OBJECTIVE_LABELS, OBJECTIVE_COLORS } from '../types'

const STATUSES = ['pending', 'approved', 'recorded', 'published'] as const

export function ContentPage() {
  const { clientId } = useParams<{ clientId: string }>()
  const id = Number(clientId)
  const [contents, setContents] = useState<ContentPiece[]>([])
  const [filter, setFilter] = useState<string>('')
  const [selected, setSelected] = useState<ContentPiece | null>(null)

  async function load() {
    const data: any = await api.content.list(id, filter || undefined)
    setContents(data)
  }

  useEffect(() => { load() }, [id, filter])

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
            {selected.status === 'recorded' && (
              <button onClick={() => setStatus(selected.id, 'published')}
                className="btn-primary w-full py-3 bg-green-600 hover:bg-green-700">
                Marcar como publicado
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
