import { useEffect, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { api } from '../services/api'
import type { Product } from '../types'

interface SeqPost {
  id: number
  title: string
  scheduled_at: string
  funnel_stage: string | null
  objective: string
  emotion_used: string | null
  media_url: string | null
  reasoning: string | null
}

interface FormState {
  name: string
  type: string
  price: string
  description: string
  transformation: string
  pains_solved: string
  desires: string
  objections: string
  awareness_stage: string
  funnel_stage: string
  is_primary: boolean
}

const empty: FormState = {
  name: '', type: 'service', price: '', description: '', transformation: '',
  pains_solved: '', desires: '', objections: '',
  awareness_stage: 'problem', funnel_stage: 'middle', is_primary: false,
}

function csv(s: string): string[] {
  return s.split(',').map(x => x.trim()).filter(Boolean)
}

export function ProductsPage() {
  const { clientId } = useParams<{ clientId: string }>()
  const id = Number(clientId)
  const navigate = useNavigate()
  const [items, setItems] = useState<Product[]>([])
  const [showForm, setShowForm] = useState(false)
  const [editing, setEditing] = useState<number | null>(null)
  const [form, setForm] = useState<FormState>(empty)
  const [busy, setBusy] = useState(false)
  const [templates, setTemplates] = useState<Array<{ key: string; label: string; defaults: any }>>([])
  // Sales sequence modal
  const [seqProduct, setSeqProduct] = useState<Product | null>(null)
  const [seqDays, setSeqDays] = useState(7)
  const [seqLaunch, setSeqLaunch] = useState(() => {
    const d = new Date(); d.setDate(d.getDate() + 7)
    return d.toISOString().slice(0, 10)
  })
  const [seqPlatform, setSeqPlatform] = useState('instagram')
  const [seqGenImg, setSeqGenImg] = useState(true)
  const [seqBusy, setSeqBusy] = useState(false)
  const [seqResult, setSeqResult] = useState<{ summary: string; posts: SeqPost[] } | null>(null)
  const [seqErr, setSeqErr] = useState('')

  async function runSequence() {
    if (!seqProduct) return
    setSeqBusy(true); setSeqErr(''); setSeqResult(null)
    try {
      const r: any = await api.strategy.salesSequence(id, {
        product_id: seqProduct.id,
        launch_date: seqLaunch,
        total_days: seqDays,
        platform: seqPlatform,
        generate_images: seqGenImg,
      })
      setSeqResult({ summary: r.strategy_summary || '', posts: r.posts || [] })
    } catch (e: any) {
      setSeqErr(e.message || 'Erro ao gerar sequência')
    } finally { setSeqBusy(false) }
  }

  async function load() {
    const r: any = await api.products.list(id)
    setItems(r)
  }
  useEffect(() => { load() }, [id])
  useEffect(() => {
    api.products.templates().then((r: any) => setTemplates(r)).catch(() => {})
  }, [])

  function applyTemplate(key: string) {
    const tpl = templates.find(t => t.key === key)
    if (!tpl) return
    const d = tpl.defaults || {}
    setForm(prev => ({
      ...prev,
      type: d.type || prev.type,
      awareness_stage: d.awareness_stage || prev.awareness_stage,
      funnel_stage: d.funnel_stage || prev.funnel_stage,
      pains_solved: (d.pains_solved || []).join(', '),
      desires: (d.desires || []).join(', '),
      objections: (d.objections || []).join(', '),
      transformation: d.transformation || prev.transformation,
    }))
  }

  function startEdit(p: Product) {
    setEditing(p.id)
    setForm({
      name: p.name, type: p.type, price: p.price || '', description: p.description || '',
      transformation: p.transformation || '',
      pains_solved: (p.pains_solved || []).join(', '),
      desires: (p.desires || []).join(', '),
      objections: (p.objections || []).join(', '),
      awareness_stage: p.awareness_stage || 'problem',
      funnel_stage: p.funnel_stage || 'middle',
      is_primary: p.is_primary,
    })
    setShowForm(true)
  }

  function reset() { setForm(empty); setEditing(null); setShowForm(false) }

  async function save() {
    setBusy(true)
    try {
      const payload = {
        client_id: id,
        name: form.name, type: form.type, price: form.price || null,
        description: form.description || null,
        transformation: form.transformation || null,
        pains_solved: csv(form.pains_solved),
        desires: csv(form.desires),
        objections: csv(form.objections),
        awareness_stage: form.awareness_stage, funnel_stage: form.funnel_stage,
        is_primary: form.is_primary,
      }
      if (editing) await api.products.update(editing, payload)
      else await api.products.create(payload)
      reset(); await load()
    } catch (e: any) { alert(e.message || 'Erro') }
    finally { setBusy(false) }
  }

  async function remove(pid: number) {
    if (!confirm('Remover produto?')) return
    await api.products.remove(pid); await load()
  }

  return (
    <div className="p-4 md:p-6 space-y-4 max-w-5xl">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-lg md:text-xl font-bold text-white">Produtos & Monetização</h1>
          <p className="text-xs text-gray-400 mt-0.5">A IA conecta cada conteúdo ao produto principal automaticamente</p>
        </div>
        <button onClick={() => { reset(); setShowForm(true) }} className="btn-primary text-xs">+ Novo</button>
      </div>

      {showForm && (
        <div className="card space-y-3">
          <h2 className="text-sm font-semibold text-white">{editing ? 'Editar' : 'Novo produto/oferta'}</h2>
          {!editing && templates.length > 0 && (
            <div className="bg-violet-900/10 border border-violet-800/40 rounded-lg p-2">
              <label className="text-[10px] text-violet-300 font-semibold">PRÉ-PREENCHER COM TEMPLATE</label>
              <select
                className="input text-sm w-full mt-1"
                defaultValue=""
                onChange={e => { if (e.target.value) { applyTemplate(e.target.value); e.target.value = '' } }}
              >
                <option value="">— escolher template —</option>
                {templates.map(t => <option key={t.key} value={t.key}>{t.label}</option>)}
              </select>
              <p className="text-[10px] text-gray-500 mt-1">Preenche dores, desejos, objeções e transformação. Você edita o que quiser depois.</p>
            </div>
          )}
          <div className="grid grid-cols-2 gap-2">
            <input className="input text-sm col-span-2" placeholder="Nome" value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} />
            <select className="input text-sm" value={form.type} onChange={e => setForm({ ...form, type: e.target.value })}>
              <option value="service">Serviço</option>
              <option value="product">Produto</option>
              <option value="course">Curso</option>
              <option value="ebook">E-book</option>
              <option value="mentorship">Mentoria</option>
              <option value="community">Comunidade</option>
            </select>
            <input className="input text-sm" placeholder="Preço" value={form.price} onChange={e => setForm({ ...form, price: e.target.value })} />
          </div>
          <textarea rows={2} className="input text-sm" placeholder="Descrição" value={form.description} onChange={e => setForm({ ...form, description: e.target.value })} />
          <textarea rows={2} className="input text-sm" placeholder="Transformação prometida (depois do produto, a pessoa vira...)" value={form.transformation} onChange={e => setForm({ ...form, transformation: e.target.value })} />
          <input className="input text-sm" placeholder="Dores que resolve (vírgula)" value={form.pains_solved} onChange={e => setForm({ ...form, pains_solved: e.target.value })} />
          <input className="input text-sm" placeholder="Desejos que ativa (vírgula)" value={form.desires} onChange={e => setForm({ ...form, desires: e.target.value })} />
          <input className="input text-sm" placeholder="Objeções comuns (vírgula)" value={form.objections} onChange={e => setForm({ ...form, objections: e.target.value })} />
          <div className="grid grid-cols-2 gap-2">
            <select className="input text-sm" value={form.awareness_stage} onChange={e => setForm({ ...form, awareness_stage: e.target.value })}>
              <option value="unaware">Inconsciente</option>
              <option value="problem">Consciente do problema</option>
              <option value="solution">Consciente da solução</option>
              <option value="product">Consciente do produto</option>
              <option value="most_aware">Pronto pra comprar</option>
            </select>
            <select className="input text-sm" value={form.funnel_stage} onChange={e => setForm({ ...form, funnel_stage: e.target.value })}>
              <option value="top">Topo</option>
              <option value="middle">Meio</option>
              <option value="bottom">Fundo</option>
            </select>
          </div>
          <label className="flex items-center gap-2 text-xs text-gray-300">
            <input type="checkbox" checked={form.is_primary} onChange={e => setForm({ ...form, is_primary: e.target.checked })} />
            Definir como produto principal
          </label>
          <div className="flex gap-2">
            <button onClick={save} disabled={busy || !form.name.trim()} className="btn-primary text-xs">{busy ? 'Salvando...' : 'Salvar'}</button>
            <button onClick={reset} className="btn-secondary text-xs">Cancelar</button>
          </div>
        </div>
      )}

      <div className="space-y-2">
        {items.length === 0 ? (
          <p className="text-xs text-gray-500">Nenhum produto cadastrado</p>
        ) : items.map(p => (
          <div key={p.id} className={`card ${p.is_primary ? 'border-violet-700/60' : ''}`}>
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 flex-wrap">
                  <p className="text-sm font-semibold text-white">{p.name}</p>
                  {p.is_primary && <span className="badge bg-violet-900/40 text-violet-300 border border-violet-700 text-[10px]">PRINCIPAL</span>}
                  <span className="text-[10px] text-gray-500">{p.type}</span>
                  {p.price && <span className="text-[10px] text-gray-400">{p.price}</span>}
                </div>
                {p.transformation && <p className="text-xs text-gray-300 mt-1">{p.transformation}</p>}
                <div className="flex flex-wrap gap-1 mt-2">
                  {(p.pains_solved || []).slice(0, 3).map((x, i) => <span key={i} className="text-[10px] px-1.5 py-0.5 bg-red-900/30 text-red-200 rounded">{x}</span>)}
                </div>
              </div>
              <div className="flex flex-col gap-1 shrink-0 items-end">
                <button onClick={() => { setSeqProduct(p); setSeqResult(null); setSeqErr('') }} className="text-xs text-violet-300 bg-violet-900/30 border border-violet-700/60 rounded px-2 py-0.5">✦ Sequência</button>
                <div className="flex gap-1">
                  <button onClick={() => startEdit(p)} className="text-xs text-violet-400">Editar</button>
                  <button onClick={() => remove(p.id)} className="text-xs text-red-400">×</button>
                </div>
              </div>
            </div>
          </div>
        ))}
      </div>

      {seqProduct && (
        <div className="fixed inset-0 bg-black/70 z-50 flex items-center justify-center p-4 overflow-y-auto" onClick={() => !seqBusy && setSeqProduct(null)}>
          <div className="bg-gray-950 border border-gray-800 rounded-xl p-4 max-w-2xl w-full max-h-[90vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
            <div className="flex items-start justify-between mb-3">
              <div>
                <p className="text-sm font-bold text-white">Sequência de venda</p>
                <p className="text-xs text-gray-400">Produto: {seqProduct.name}</p>
              </div>
              <button onClick={() => !seqBusy && setSeqProduct(null)} className="text-gray-500 text-lg">×</button>
            </div>

            {!seqResult && (
              <div className="space-y-3">
                <p className="text-xs text-gray-400">
                  A IA vai criar uma sequência psicológica de posts (aquecimento → autoridade → quebra de objeção → desejo → oferta) distribuída até a data do lançamento. Cada peça vira ContentPiece + slot no calendário.
                </p>
                <div className="grid grid-cols-2 gap-2">
                  <div>
                    <label className="text-[10px] text-gray-500">Data do lançamento</label>
                    <input type="date" value={seqLaunch} onChange={e => setSeqLaunch(e.target.value)} className="input text-sm w-full" />
                  </div>
                  <div>
                    <label className="text-[10px] text-gray-500">Total de dias</label>
                    <input type="number" min={3} max={30} value={seqDays} onChange={e => setSeqDays(Math.max(3, Math.min(30, Number(e.target.value) || 7)))} className="input text-sm w-full" />
                  </div>
                  <div>
                    <label className="text-[10px] text-gray-500">Plataforma</label>
                    <select value={seqPlatform} onChange={e => setSeqPlatform(e.target.value)} className="input text-sm w-full">
                      <option value="instagram">Instagram</option>
                      <option value="tiktok">TikTok</option>
                      <option value="youtube">YouTube</option>
                      <option value="linkedin">LinkedIn</option>
                    </select>
                  </div>
                  <label className="flex items-center gap-2 text-xs text-gray-300 mt-4">
                    <input type="checkbox" checked={seqGenImg} onChange={e => setSeqGenImg(e.target.checked)} />
                    Gerar imagens
                  </label>
                </div>
                {seqErr && <p className="text-xs text-red-400">{seqErr}</p>}
                <div className="flex gap-2 justify-end">
                  <button onClick={() => setSeqProduct(null)} disabled={seqBusy} className="btn-secondary text-xs">Cancelar</button>
                  <button onClick={runSequence} disabled={seqBusy} className="btn-primary text-xs">
                    {seqBusy ? 'Gerando sequência (pode levar 30-60s)...' : '✦ Gerar Sequência'}
                  </button>
                </div>
              </div>
            )}

            {seqResult && (
              <div className="space-y-3">
                <div className="card bg-violet-900/10 border-violet-800/50">
                  <p className="text-xs text-violet-300 font-semibold mb-1">ESTRATÉGIA</p>
                  <p className="text-xs text-gray-200">{seqResult.summary}</p>
                </div>
                <p className="text-xs text-gray-400">{seqResult.posts.length} peças criadas e agendadas no calendário:</p>
                <div className="space-y-2">
                  {seqResult.posts.map((p, i) => (
                    <div key={p.id} className="card">
                      <div className="flex items-start gap-2">
                        {p.media_url && <img src={p.media_url} alt="" className="w-12 h-12 rounded object-cover shrink-0" />}
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-1.5 flex-wrap">
                            <span className="text-[10px] text-gray-500">#{i + 1}</span>
                            <span className="text-[10px] text-gray-400">{new Date(p.scheduled_at).toLocaleDateString('pt-BR')}</span>
                            {p.funnel_stage && <span className="text-[10px] px-1.5 py-0.5 rounded bg-cyan-900/30 text-cyan-200">{p.funnel_stage}</span>}
                            {p.emotion_used && <span className="text-[10px] px-1.5 py-0.5 rounded bg-orange-900/30 text-orange-200">{p.emotion_used}</span>}
                          </div>
                          <p className="text-sm text-white truncate">{p.title}</p>
                          {p.reasoning && <p className="text-[10px] text-gray-500 mt-0.5">{p.reasoning}</p>}
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
                <div className="flex gap-2 justify-end">
                  <button onClick={() => setSeqProduct(null)} className="btn-secondary text-xs">Fechar</button>
                  <button onClick={() => navigate(`/client/${clientId}/calendar`)} className="btn-primary text-xs">Ver no calendário →</button>
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
