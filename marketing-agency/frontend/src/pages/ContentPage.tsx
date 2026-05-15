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

  return (
    <div className="p-6 max-w-6xl">
      <div className="flex items-center justify-between mb-5">
        <h1 className="text-lg font-bold text-white">Conteúdo</h1>
        <div className="flex gap-2">
          <button
            onClick={() => setFilter('')}
            className={`text-xs px-3 py-1.5 rounded-lg border transition-colors ${
              !filter ? 'bg-violet-600/20 border-violet-500 text-violet-300' : 'bg-gray-800 border-gray-700 text-gray-400'
            }`}
          >
            Todos
          </button>
          {STATUSES.map(s => (
            <button
              key={s}
              onClick={() => setFilter(s)}
              className={`text-xs px-3 py-1.5 rounded-lg border transition-colors ${
                filter === s ? 'bg-violet-600/20 border-violet-500 text-violet-300' : 'bg-gray-800 border-gray-700 text-gray-400'
              }`}
            >
              {STATUS_LABELS[s]}
            </button>
          ))}
        </div>
      </div>

      <div className="flex gap-5">
        <div className="flex-1 space-y-2">
          {contents.length === 0 ? (
            <div className="card text-center py-12">
              <p className="text-gray-500 text-sm">Nenhum conteúdo encontrado</p>
              <p className="text-gray-600 text-xs mt-1">Use os Agentes IA para gerar conteúdo</p>
            </div>
          ) : (
            contents.map(content => (
              <button
                key={content.id}
                onClick={() => setSelected(content)}
                className={`card w-full text-left hover:border-violet-700 transition-colors ${
                  selected?.id === content.id ? 'border-violet-600' : ''
                }`}
              >
                <div className="flex items-start gap-3">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 mb-1">
                      <span className={`badge ${STATUS_COLORS[content.status]}`}>
                        {STATUS_LABELS[content.status]}
                      </span>
                      <span className={`badge border ${OBJECTIVE_COLORS[content.objective] || 'bg-gray-700 text-gray-300 border-gray-600'}`}>
                        {OBJECTIVE_LABELS[content.objective] || content.objective}
                      </span>
                      <span className="text-xs text-gray-500">{FORMAT_LABELS[content.format] || content.format}</span>
                    </div>
                    <p className="text-sm font-medium text-white truncate">{content.title}</p>
                    {content.hook && (
                      <p className="text-xs text-gray-400 truncate mt-0.5">{content.hook}</p>
                    )}
                  </div>
                  <div className="text-right shrink-0">
                    <p className="text-xs text-gray-500">{content.platform}</p>
                    {content.scheduled_at && (
                      <p className="text-xs text-gray-600 mt-0.5">
                        {new Date(content.scheduled_at).toLocaleDateString('pt-BR')}
                      </p>
                    )}
                  </div>
                </div>
              </button>
            ))
          )}
        </div>

        {selected && (
          <div className="w-80 shrink-0 space-y-3">
            <div className="card space-y-3">
              <div className="flex items-center justify-between">
                <h2 className="text-sm font-semibold text-white truncate">{selected.title}</h2>
                <button onClick={() => setSelected(null)} className="text-gray-500 hover:text-gray-300 text-lg leading-none">&times;</button>
              </div>

              <div className="flex gap-2 flex-wrap">
                <span className={`badge ${STATUS_COLORS[selected.status]}`}>{STATUS_LABELS[selected.status]}</span>
                <span className="badge bg-gray-800 text-gray-400">{selected.platform}</span>
                <span className="badge bg-gray-800 text-gray-400">{FORMAT_LABELS[selected.format] || selected.format}</span>
              </div>

              {selected.hook && (
                <div>
                  <p className="text-xs text-violet-400 font-semibold mb-1">HOOK</p>
                  <p className="text-xs text-gray-300">{selected.hook}</p>
                </div>
              )}

              {selected.script && (
                <div>
                  <p className="text-xs text-violet-400 font-semibold mb-1">ROTEIRO</p>
                  <div className="max-h-40 overflow-y-auto">
                    <p className="text-xs text-gray-300 whitespace-pre-wrap">{selected.script}</p>
                  </div>
                </div>
              )}

              {selected.copy && (
                <div>
                  <p className="text-xs text-violet-400 font-semibold mb-1">COPY</p>
                  <p className="text-xs text-gray-300">{selected.copy}</p>
                </div>
              )}

              {selected.design_brief && (
                <div>
                  <p className="text-xs text-violet-400 font-semibold mb-1">BRIEFING VISUAL</p>
                  <div className="max-h-32 overflow-y-auto">
                    <p className="text-xs text-gray-300 whitespace-pre-wrap">{selected.design_brief}</p>
                  </div>
                </div>
              )}

              {selected.strategic_note && (
                <div>
                  <p className="text-xs text-violet-400 font-semibold mb-1">NOTA ESTRATÉGICA</p>
                  <p className="text-xs text-gray-300">{selected.strategic_note}</p>
                </div>
              )}

              <div className="border-t border-gray-800 pt-3 space-y-2">
                {selected.status === 'pending' && (
                  <button onClick={() => approve(selected.id)} className="btn-primary w-full">
                    Aprovar conteúdo
                  </button>
                )}
                {selected.status === 'approved' && (
                  <button onClick={() => setStatus(selected.id, 'recorded')} className="btn-primary w-full bg-blue-600 hover:bg-blue-700">
                    Marcar como gravado
                  </button>
                )}
                {selected.status === 'recorded' && (
                  <button onClick={() => setStatus(selected.id, 'published')} className="btn-primary w-full bg-green-600 hover:bg-green-700">
                    Marcar como publicado
                  </button>
                )}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
