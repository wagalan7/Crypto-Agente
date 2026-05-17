import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { api } from '../services/api'
import type { Persona } from '../types'

type ListField = 'pains' | 'desires' | 'emotions' | 'insecurities' | 'audience_goals'
type TextField = 'language_patterns' | 'psychological_patterns' | 'audience_profile' | 'evidence'

const LIST_LABELS: Record<ListField, { label: string; head: string; chip: string }> = {
  pains: { label: 'DORES', head: 'text-red-400', chip: 'bg-red-900/30 text-red-200 border-red-800/50' },
  desires: { label: 'DESEJOS', head: 'text-green-400', chip: 'bg-green-900/30 text-green-200 border-green-800/50' },
  emotions: { label: 'EMOÇÕES DOMINANTES', head: 'text-orange-400', chip: 'bg-orange-900/30 text-orange-200 border-orange-800/50' },
  insecurities: { label: 'INSEGURANÇAS', head: 'text-yellow-400', chip: 'bg-yellow-900/30 text-yellow-200 border-yellow-800/50' },
  audience_goals: { label: 'OBJETIVOS DA AUDIÊNCIA', head: 'text-blue-400', chip: 'bg-blue-900/30 text-blue-200 border-blue-800/50' },
}

const TEXT_BLOCK_STYLES: Record<string, { wrap: string; head: string; body: string }> = {
  violet: { wrap: 'bg-violet-900/10 border-violet-800/50', head: 'text-violet-400', body: 'text-gray-200' },
  cyan: { wrap: '', head: 'text-cyan-400', body: 'text-gray-300' },
  pink: { wrap: '', head: 'text-pink-400', body: 'text-gray-300' },
  gray: { wrap: 'bg-gray-900/40', head: 'text-gray-500', body: 'text-gray-400' },
}

export function PersonaPage() {
  const { clientId } = useParams<{ clientId: string }>()
  const id = Number(clientId)
  const [persona, setPersona] = useState<Persona | null>(null)
  const [exists, setExists] = useState<boolean | null>(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState<string>('')
  const [editing, setEditing] = useState<ListField | TextField | null>(null)
  const [editValue, setEditValue] = useState<string>('')
  const [editNote, setEditNote] = useState('')
  const [saving, setSaving] = useState(false)

  async function load() {
    const r: any = await api.persona.get(id)
    if (r.exists) { setPersona(r as Persona); setExists(true) } else { setExists(false) }
  }

  useEffect(() => { load() }, [id])

  async function generate() {
    setLoading(true); setErr('')
    try {
      const r: any = await api.persona.generate(id)
      setPersona(r); setExists(true)
    } catch (e: any) {
      setErr(e.message || 'Erro ao gerar persona')
    } finally { setLoading(false) }
  }

  function startEdit(field: ListField | TextField) {
    if (!persona) return
    setEditing(field)
    setEditNote('')
    if ((field as ListField) in LIST_LABELS) {
      setEditValue((persona as any)[field]?.join(', ') || '')
    } else {
      setEditValue((persona as any)[field] || '')
    }
  }

  async function saveEdit() {
    if (!editing || !persona) return
    setSaving(true); setErr('')
    try {
      const value = (editing in LIST_LABELS)
        ? editValue.split(',').map(s => s.trim()).filter(Boolean)
        : editValue
      const updated: any = await api.persona.update(id, { [editing]: value, note: editNote || undefined })
      setPersona(updated)
      setEditing(null); setEditValue(''); setEditNote('')
    } catch (e: any) {
      setErr(e.message || 'Erro ao salvar')
    } finally { setSaving(false) }
  }

  function renderEditor() {
    if (!editing) return null
    const isList = editing in LIST_LABELS
    return (
      <div className="fixed inset-0 bg-black/70 z-50 flex items-center justify-center p-4" onClick={() => setEditing(null)}>
        <div className="card max-w-lg w-full space-y-3" onClick={e => e.stopPropagation()}>
          <div className="flex items-center justify-between">
            <p className="text-sm font-semibold text-violet-300">Editar: {editing.replace(/_/g, ' ')}</p>
            <button onClick={() => setEditing(null)} className="text-xs text-gray-400">×</button>
          </div>
          {isList ? (
            <input className="input text-sm" value={editValue} onChange={e => setEditValue(e.target.value)} placeholder="Separe por vírgulas" />
          ) : (
            <textarea rows={5} className="input text-sm" value={editValue} onChange={e => setEditValue(e.target.value)} />
          )}
          <input className="input text-xs" value={editNote} onChange={e => setEditNote(e.target.value)} placeholder="Anote por que está editando (opcional — a IA aprende com isso)" />
          {err && <p className="text-xs text-red-400">{err}</p>}
          <div className="flex gap-2 justify-end">
            <button onClick={() => setEditing(null)} className="text-xs px-3 py-1.5 text-gray-400 border border-gray-700 rounded-md">Cancelar</button>
            <button onClick={saveEdit} disabled={saving} className="btn-primary text-xs">{saving ? 'Salvando...' : 'Salvar'}</button>
          </div>
          <p className="text-[10px] text-gray-500">A IA registra essa edição e usa em todos os conteúdos futuros — não vai sobrescrever sua direção.</p>
        </div>
      </div>
    )
  }

  return (
    <div className="p-4 md:p-6 space-y-4 max-w-5xl">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-lg md:text-xl font-bold text-white">Persona Inteligente</h1>
          <p className="text-xs text-gray-400 mt-0.5">A IA sugere → você edita → ela aprende seus ajustes</p>
        </div>
        <button onClick={generate} disabled={loading} className="btn-primary text-xs">
          {loading ? 'Analisando...' : exists ? '↻ Atualizar' : '✦ Gerar Persona'}
        </button>
      </div>

      {err && !editing && <div className="card bg-red-900/20 border-red-800/50 text-xs text-red-300">{err}</div>}

      {exists === false && (
        <div className="card text-center py-10">
          <p className="text-sm text-gray-300 mb-1">Persona ainda não gerada</p>
          <p className="text-xs text-gray-500 mb-4">A IA vai analisar o briefing + conteúdos publicados para mapear sua audiência real.</p>
        </div>
      )}

      {persona && (
        <>
          {persona.audience_profile && (
            <EditableTextBlock
              label="PERFIL"
              color="violet"
              value={persona.audience_profile}
              onEdit={() => startEdit('audience_profile')}
            />
          )}

          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {(Object.keys(LIST_LABELS) as ListField[]).map(field => (
              <EditableChips
                key={field}
                field={field}
                items={(persona as any)[field] || []}
                onEdit={() => startEdit(field)}
              />
            ))}
          </div>

          {persona.language_patterns !== undefined && (
            <EditableTextBlock label="PADRÕES DE LINGUAGEM" color="cyan" value={persona.language_patterns} onEdit={() => startEdit('language_patterns')} />
          )}
          {persona.psychological_patterns !== undefined && (
            <EditableTextBlock label="PADRÕES PSICOLÓGICOS" color="pink" value={persona.psychological_patterns} onEdit={() => startEdit('psychological_patterns')} />
          )}
          {persona.evidence !== undefined && (
            <EditableTextBlock label="EVIDÊNCIAS / JUSTIFICATIVAS" color="gray" value={persona.evidence} onEdit={() => startEdit('evidence')} />
          )}

          {persona.user_refinements && persona.user_refinements.length > 0 && (
            <div className="card bg-violet-900/10 border-violet-800/50">
              <p className="text-xs text-violet-400 font-semibold mb-2">SEUS AJUSTES ({persona.edit_count || 0})</p>
              <div className="space-y-1.5">
                {persona.user_refinements.slice(-6).reverse().map((r, i) => (
                  <div key={i} className="text-xs text-gray-300">
                    <span className="text-violet-300 font-mono">[{r.field}]</span> {r.note || '(sem nota)'}
                  </div>
                ))}
              </div>
            </div>
          )}

          {persona.generated_at && (() => {
            const ageDays = Math.floor((Date.now() - new Date(persona.generated_at!).getTime()) / 86_400_000)
            const stale = ageDays > 60
            return (
              <div className={`text-[10px] flex items-center gap-2 ${stale ? 'text-yellow-300' : 'text-gray-600'}`}>
                <span>Gerada em {new Date(persona.generated_at!).toLocaleString('pt-BR')} · {ageDays}d atrás</span>
                {stale && <span className="px-1.5 py-0.5 rounded bg-yellow-900/40 border border-yellow-800/50">⚠ Pode estar desatualizada — considere regenerar</span>}
              </div>
            )
          })()}
        </>
      )}

      {renderEditor()}
    </div>
  )
}

function EditableChips({ field, items, onEdit }: { field: ListField; items: string[]; onEdit: () => void }) {
  const cfg = LIST_LABELS[field]
  return (
    <div className="card group">
      <div className="flex items-center justify-between mb-2">
        <h3 className={`text-xs font-semibold ${cfg.head}`}>{cfg.label}</h3>
        <button onClick={onEdit} className="text-[10px] text-gray-400 hover:text-violet-300 md:opacity-0 group-hover:opacity-100 transition-opacity">editar</button>
      </div>
      {items.length === 0 ? (
        <p className="text-xs text-gray-500">—</p>
      ) : (
        <div className="flex flex-wrap gap-1.5">
          {items.map((it, i) => (
            <span key={i} className={`text-xs px-2 py-1 rounded-md border ${cfg.chip}`}>{it}</span>
          ))}
        </div>
      )}
    </div>
  )
}

function EditableTextBlock({ label, color, value, onEdit }: { label: string; color: string; value: string; onEdit: () => void }) {
  const s = TEXT_BLOCK_STYLES[color] || TEXT_BLOCK_STYLES.cyan
  return (
    <div className={`card group ${s.wrap}`}>
      <div className="flex items-center justify-between mb-1">
        <p className={`text-xs font-semibold ${s.head}`}>{label}</p>
        <button onClick={onEdit} className="text-[10px] text-gray-400 hover:text-violet-300 md:opacity-0 group-hover:opacity-100 transition-opacity">editar</button>
      </div>
      <p className={`text-sm whitespace-pre-line ${s.body}`}>{value || '—'}</p>
    </div>
  )
}
