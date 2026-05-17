import { useEffect, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'
import { api } from '../services/api'
import type { Inspiration } from '../types'

export function InspirationsPage() {
  const { clientId } = useParams<{ clientId: string }>()
  const id = Number(clientId)
  const [items, setItems] = useState<Inspiration[]>([])
  const [sourceType, setSourceType] = useState<'url' | 'text'>('url')
  const [value, setValue] = useState('')
  const [label, setLabel] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [expanded, setExpanded] = useState<number | null>(null)
  const [uploading, setUploading] = useState(false)
  const [uploadMsg, setUploadMsg] = useState('')
  const fileRef = useRef<HTMLInputElement>(null)

  async function load() {
    const r: any = await api.inspirations.list(id)
    setItems(r)
  }

  useEffect(() => { load() }, [id])

  async function create() {
    if (!value.trim()) return
    setBusy(true); setErr('')
    try {
      await api.inspirations.create({ client_id: id, source_type: sourceType, source_value: value, label: label || undefined })
      setValue(''); setLabel('')
      await load()
    } catch (e: any) {
      setErr(e.message || 'Erro')
    } finally { setBusy(false) }
  }

  async function uploadImage(file: File) {
    setUploading(true); setUploadMsg('')
    try {
      await api.inspirations.uploadImage(id, file, label || undefined)
      setUploadMsg(`✓ Imagem analisada — composição, paleta e mood capturados`)
      setLabel('')
      await load()
    } catch (e: any) {
      setUploadMsg(`Erro: ${e.message?.slice(0, 120) || 'falha no upload'}`)
    } finally {
      setUploading(false)
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  async function remove(itemId: number) {
    if (!confirm('Remover esta referência?')) return
    await api.inspirations.remove(itemId)
    await load()
  }

  return (
    <div className="p-4 md:p-6 space-y-4 max-w-5xl">
      <div>
        <h1 className="text-lg md:text-xl font-bold text-white">Inspirações</h1>
        <p className="text-xs text-gray-400 mt-0.5">Cole URL, texto ou faça upload de uma imagem — a IA disseca hook, estética, composição e adapta pra sua marca</p>
      </div>

      <div className="card space-y-3">
        <div className="flex gap-2 flex-wrap">
          <button onClick={() => setSourceType('url')} className={`text-xs px-3 py-1.5 rounded-md border ${sourceType === 'url' ? 'bg-violet-900/40 border-violet-700 text-violet-200' : 'border-gray-700 text-gray-400'}`}>URL</button>
          <button onClick={() => setSourceType('text')} className={`text-xs px-3 py-1.5 rounded-md border ${sourceType === 'text' ? 'bg-violet-900/40 border-violet-700 text-violet-200' : 'border-gray-700 text-gray-400'}`}>Texto / Hook</button>
          <button onClick={() => fileRef.current?.click()} disabled={uploading} className="text-xs px-3 py-1.5 rounded-md border border-violet-700 bg-violet-900/20 text-violet-300 hover:bg-violet-900/40 disabled:opacity-50 ml-auto">
            {uploading ? 'Analisando imagem...' : '🖼️ Upload imagem'}
          </button>
          <input ref={fileRef} type="file" accept="image/*" hidden onChange={e => { const f = e.target.files?.[0]; if (f) uploadImage(f) }} />
        </div>
        {sourceType === 'url' ? (
          <input value={value} onChange={e => setValue(e.target.value)} placeholder="https://instagram.com/p/... ou site" className="input text-sm" />
        ) : (
          <textarea value={value} onChange={e => setValue(e.target.value)} placeholder="Cole o hook, copy ou descreva o que viu" rows={4} className="input text-sm" />
        )}
        <input value={label} onChange={e => setLabel(e.target.value)} placeholder="Rótulo (opcional)" className="input text-sm" />
        {err && <p className="text-xs text-red-400">{err}</p>}
        {uploadMsg && <p className="text-xs text-gray-400">{uploadMsg}</p>}
        <button onClick={create} disabled={busy || !value.trim()} className="btn-primary text-xs">
          {busy ? 'Analisando...' : '✦ Analisar e adaptar'}
        </button>
      </div>

      <div className="space-y-2">
        {items.length === 0 ? (
          <p className="text-xs text-gray-500">Nenhuma inspiração ainda</p>
        ) : items.map(item => {
          const a = item.analysis as any
          const v = item.visual_analysis as any
          const open = expanded === item.id
          return (
            <div key={item.id} className="card">
              <div className="flex items-start gap-3">
                {item.image_url && (
                  <img src={item.image_url} alt="" className="w-16 h-16 rounded object-cover shrink-0 border border-gray-800" />
                )}
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-semibold text-white truncate">{item.label || a?.hook || '(sem rótulo)'}</p>
                  <p className="text-[10px] text-gray-500 truncate">{item.source_type} · {(item.source_value || '').slice(0, 80)}</p>
                  {v?.mood && (
                    <p className="text-[10px] text-gray-400 mt-1">Mood: {v.mood}{Array.isArray(v.palette) && ` · Paleta: ${v.palette.slice(0, 3).join(', ')}`}</p>
                  )}
                </div>
                <div className="flex flex-col gap-1 shrink-0">
                  <button onClick={() => setExpanded(open ? null : item.id)} className="text-xs text-violet-400">{open ? 'Recolher' : 'Ver análise'}</button>
                  <button onClick={() => remove(item.id)} className="text-xs text-red-400">×</button>
                </div>
              </div>
              {open && (a || v) && (
                <div className="mt-3 space-y-2 text-xs border-t border-gray-800 pt-3">
                  {v?.composition && <Field label="COMPOSIÇÃO" value={v.composition} />}
                  {v?.mood && <Field label="MOOD" value={v.mood} />}
                  {Array.isArray(v?.palette) && v.palette.length > 0 && <Field label="PALETA" value={v.palette.join(' · ')} />}
                  {v?.identity && <Field label="IDENTIDADE VISUAL" value={v.identity} />}
                  {a?.hook && <Field label="HOOK" value={a.hook} />}
                  {a?.narrative && <Field label="NARRATIVA" value={a.narrative} />}
                  {a?.cta && <Field label="CTA" value={a.cta} />}
                  {a?.dominant_emotion && <Field label="EMOÇÃO DOMINANTE" value={a.dominant_emotion} />}
                  {a?.structure && <Field label="ESTRUTURA" value={a.structure} />}
                  {a?.retention_factors && <Field label="POR QUE PRENDE" value={Array.isArray(a.retention_factors) ? a.retention_factors.join(' · ') : a.retention_factors} />}
                  {a?.why_it_works && <Field label="POR QUE FUNCIONA" value={a.why_it_works} />}
                  {item.adapted_brief && (
                    <div className="card bg-violet-900/10 border-violet-800/50 mt-2">
                      <p className="text-xs text-violet-400 font-semibold mb-1">BRIEFING ADAPTADO PRA SUA MARCA</p>
                      <p className="text-xs text-gray-200 whitespace-pre-line">{item.adapted_brief}</p>
                    </div>
                  )}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-[10px] text-gray-500 font-semibold">{label}</p>
      <p className="text-xs text-gray-300">{value}</p>
    </div>
  )
}
