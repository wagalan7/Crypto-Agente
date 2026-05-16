import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { api } from '../services/api'
import type { KnowledgeItem } from '../types'

const SOURCE_LABEL: Record<string, string> = {
  note: '📝 Nota',
  idea: '💡 Ideia',
  book: '📚 Livro',
  concept: '🧠 Conceito',
  reference: '🔗 Referência',
  pdf: '📄 PDF',
  screenshot: '🖼️ Print',
}

export function KnowledgePage() {
  const { clientId } = useParams<{ clientId: string }>()
  const id = Number(clientId)
  const [items, setItems] = useState<KnowledgeItem[]>([])
  const [showForm, setShowForm] = useState(false)
  const [title, setTitle] = useState('')
  const [content, setContent] = useState('')
  const [sourceType, setSourceType] = useState('note')
  const [tags, setTags] = useState('')
  const [busy, setBusy] = useState(false)
  const [filter, setFilter] = useState('')

  async function load() {
    const r: any = await api.knowledge.list(id)
    setItems(r)
  }
  useEffect(() => { load() }, [id])

  async function save() {
    if (!title.trim() || !content.trim()) return
    setBusy(true)
    try {
      await api.knowledge.create({
        client_id: id, title, content, source_type: sourceType,
        tags: tags.split(',').map(t => t.trim()).filter(Boolean),
      })
      setTitle(''); setContent(''); setTags(''); setShowForm(false)
      await load()
    } finally { setBusy(false) }
  }

  async function remove(itemId: number) {
    if (!confirm('Remover item?')) return
    await api.knowledge.remove(itemId); await load()
  }

  const filtered = items.filter(i =>
    !filter ||
    i.title.toLowerCase().includes(filter.toLowerCase()) ||
    i.content.toLowerCase().includes(filter.toLowerCase()) ||
    (i.tags || []).some(t => t.toLowerCase().includes(filter.toLowerCase()))
  )

  return (
    <div className="p-4 md:p-6 space-y-4 max-w-5xl">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-lg md:text-xl font-bold text-white">Base de Conhecimento</h1>
          <p className="text-xs text-gray-400 mt-0.5">Seu capital intelectual — a IA usa em todo conteúdo gerado</p>
        </div>
        <button onClick={() => setShowForm(s => !s)} className="btn-primary text-xs">{showForm ? 'Fechar' : '+ Adicionar'}</button>
      </div>

      {showForm && (
        <div className="card space-y-2">
          <input className="input text-sm" placeholder="Título" value={title} onChange={e => setTitle(e.target.value)} />
          <textarea rows={5} className="input text-sm" placeholder="Conteúdo — anotação, trecho de livro, conceito, ideia..." value={content} onChange={e => setContent(e.target.value)} />
          <div className="grid grid-cols-2 gap-2">
            <select className="input text-sm" value={sourceType} onChange={e => setSourceType(e.target.value)}>
              {Object.entries(SOURCE_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
            <input className="input text-sm" placeholder="Tags (vírgula)" value={tags} onChange={e => setTags(e.target.value)} />
          </div>
          <button onClick={save} disabled={busy || !title.trim() || !content.trim()} className="btn-primary text-xs">{busy ? 'Salvando...' : 'Salvar'}</button>
        </div>
      )}

      <input className="input text-sm" placeholder="Buscar..." value={filter} onChange={e => setFilter(e.target.value)} />

      <div className="space-y-2">
        {filtered.length === 0 ? (
          <p className="text-xs text-gray-500">{items.length === 0 ? 'Base vazia. Adicione anotações, conceitos e referências.' : 'Nenhum item encontrado'}</p>
        ) : filtered.map(it => (
          <div key={it.id} className="card">
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <p className="text-sm font-semibold text-white">{it.title}</p>
                  <span className="text-[10px] text-gray-500">{SOURCE_LABEL[it.source_type] || it.source_type}</span>
                </div>
                <p className="text-xs text-gray-300 mt-1 whitespace-pre-line line-clamp-4">{it.content}</p>
                <div className="flex flex-wrap gap-1 mt-2">
                  {(it.tags || []).map((t, i) => <span key={i} className="text-[10px] px-1.5 py-0.5 bg-gray-800 text-gray-300 rounded">#{t}</span>)}
                </div>
              </div>
              <button onClick={() => remove(it.id)} className="text-xs text-red-400 shrink-0">×</button>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
