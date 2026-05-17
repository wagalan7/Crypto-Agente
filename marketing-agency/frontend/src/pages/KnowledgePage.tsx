import { useEffect, useRef, useState } from 'react'
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
  framework: '🧩 Framework',
  study: '🔬 Estudo',
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
  const [expanded, setExpanded] = useState<number | null>(null)
  const [redigestingId, setRedigestingId] = useState<number | null>(null)
  const [uploadingPdf, setUploadingPdf] = useState(false)
  const [uploadMsg, setUploadMsg] = useState('')
  const fileRef = useRef<HTMLInputElement>(null)

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

  async function uploadPdf(file: File) {
    setUploadingPdf(true); setUploadMsg('')
    try {
      await api.knowledge.uploadPdf(id, file, file.name.replace(/\.pdf$/i, ''), tags || undefined)
      setUploadMsg(`✓ "${file.name}" extraído e digerido pela IA`)
      await load()
    } catch (e: any) {
      setUploadMsg(`Erro: ${e.message?.slice(0, 120) || 'falha no upload'}`)
    } finally {
      setUploadingPdf(false)
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  async function redigest(itemId: number) {
    setRedigestingId(itemId)
    try {
      await api.knowledge.redigest(itemId)
      await load()
    } finally { setRedigestingId(null) }
  }

  async function remove(itemId: number) {
    if (!confirm('Remover item?')) return
    await api.knowledge.remove(itemId); await load()
  }

  const filtered = items.filter(i =>
    !filter ||
    i.title.toLowerCase().includes(filter.toLowerCase()) ||
    i.content.toLowerCase().includes(filter.toLowerCase()) ||
    (i.summary || '').toLowerCase().includes(filter.toLowerCase()) ||
    (i.tags || []).some(t => t.toLowerCase().includes(filter.toLowerCase()))
  )

  return (
    <div className="p-4 md:p-6 space-y-4 max-w-5xl">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-lg md:text-xl font-bold text-white">Base de Conhecimento</h1>
          <p className="text-xs text-gray-400 mt-0.5">Seu capital intelectual — a IA digere e reusa em todo conteúdo</p>
        </div>
        <div className="flex gap-1.5">
          <button onClick={() => fileRef.current?.click()} disabled={uploadingPdf} className="text-xs px-3 py-1.5 rounded-md border border-violet-700 bg-violet-900/20 text-violet-300 hover:bg-violet-900/40 disabled:opacity-50">
            {uploadingPdf ? 'Lendo PDF...' : '📄 PDF'}
          </button>
          <button onClick={() => setShowForm(s => !s)} className="btn-primary text-xs">{showForm ? 'Fechar' : '+ Adicionar'}</button>
        </div>
        <input ref={fileRef} type="file" accept=".pdf" hidden onChange={e => { const f = e.target.files?.[0]; if (f) uploadPdf(f) }} />
      </div>

      {uploadMsg && <p className="text-xs text-gray-400">{uploadMsg}</p>}

      {showForm && (
        <div className="card space-y-2">
          <input className="input text-sm" placeholder="Título" value={title} onChange={e => setTitle(e.target.value)} />
          <textarea rows={6} className="input text-sm" placeholder="Conteúdo — anotação, trecho de livro, conceito, framework, estudo..." value={content} onChange={e => setContent(e.target.value)} />
          <div className="grid grid-cols-2 gap-2">
            <select className="input text-sm" value={sourceType} onChange={e => setSourceType(e.target.value)}>
              {Object.entries(SOURCE_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
            <input className="input text-sm" placeholder="Tags (vírgula)" value={tags} onChange={e => setTags(e.target.value)} />
          </div>
          <button onClick={save} disabled={busy || !title.trim() || !content.trim()} className="btn-primary text-xs">{busy ? 'Salvando...' : 'Salvar e digerir'}</button>
          <p className="text-[10px] text-gray-500">Após salvar, a IA cria resumo, ideias-chave e captura sinais de voz automaticamente.</p>
        </div>
      )}

      <input className="input text-sm" placeholder="Buscar..." value={filter} onChange={e => setFilter(e.target.value)} />

      <div className="space-y-2">
        {filtered.length === 0 ? (
          <p className="text-xs text-gray-500">{items.length === 0 ? 'Base vazia. Adicione anotações, PDFs, frameworks ou conceitos.' : 'Nenhum item encontrado'}</p>
        ) : filtered.map(it => {
          const open = expanded === it.id
          const digested = !!(it.summary || (it.key_insights && it.key_insights.length))
          return (
            <div key={it.id} className="card">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 flex-wrap">
                    <p className="text-sm font-semibold text-white">{it.title}</p>
                    <span className="text-[10px] text-gray-500">{SOURCE_LABEL[it.source_type] || it.source_type}</span>
                    {digested && <span className="text-[10px] px-1.5 py-0.5 rounded bg-violet-900/30 text-violet-300">IA digerida</span>}
                    {!!it.use_count && <span className="text-[10px] text-gray-500">usado {it.use_count}x</span>}
                  </div>
                  {it.summary ? (
                    <p className="text-xs text-gray-300 mt-1.5">{it.summary}</p>
                  ) : (
                    <p className="text-xs text-gray-400 mt-1.5 whitespace-pre-line line-clamp-3">{it.content}</p>
                  )}
                  {it.key_insights && it.key_insights.length > 0 && (
                    <div className="mt-2 space-y-0.5">
                      {it.key_insights.slice(0, 5).map((ins, i) => (
                        <p key={i} className="text-[11px] text-gray-400">• {ins}</p>
                      ))}
                    </div>
                  )}
                  {it.voice_signals && it.voice_signals.length > 0 && (
                    <div className="flex flex-wrap gap-1 mt-2">
                      {it.voice_signals.slice(0, 8).map((v, i) => (
                        <span key={i} className="text-[10px] px-1.5 py-0.5 rounded bg-cyan-900/20 text-cyan-300">{v}</span>
                      ))}
                    </div>
                  )}
                  <div className="flex flex-wrap gap-1 mt-2">
                    {(it.tags || []).map((t, i) => <span key={i} className="text-[10px] px-1.5 py-0.5 bg-gray-800 text-gray-300 rounded">#{t}</span>)}
                  </div>
                </div>
                <div className="flex flex-col gap-1 shrink-0">
                  <button onClick={() => setExpanded(open ? null : it.id)} className="text-[10px] text-gray-400 hover:text-violet-300">{open ? 'Recolher' : 'Original'}</button>
                  <button onClick={() => redigest(it.id)} disabled={redigestingId === it.id} className="text-[10px] text-violet-400 hover:text-violet-300 disabled:opacity-50">
                    {redigestingId === it.id ? '...' : 'Redigerir'}
                  </button>
                  <button onClick={() => remove(it.id)} className="text-xs text-red-400">×</button>
                </div>
              </div>
              {open && (
                <div className="mt-3 pt-3 border-t border-gray-800">
                  <p className="text-[10px] text-gray-500 font-semibold mb-1">CONTEÚDO BRUTO</p>
                  <p className="text-xs text-gray-300 whitespace-pre-line max-h-80 overflow-y-auto">{it.content}</p>
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
