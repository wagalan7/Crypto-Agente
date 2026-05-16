import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { api } from '../services/api'
import type { Product } from '../types'

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
  const [items, setItems] = useState<Product[]>([])
  const [showForm, setShowForm] = useState(false)
  const [editing, setEditing] = useState<number | null>(null)
  const [form, setForm] = useState<FormState>(empty)
  const [busy, setBusy] = useState(false)

  async function load() {
    const r: any = await api.products.list(id)
    setItems(r)
  }
  useEffect(() => { load() }, [id])

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
              <div className="flex gap-1 shrink-0">
                <button onClick={() => startEdit(p)} className="text-xs text-violet-400">Editar</button>
                <button onClick={() => remove(p.id)} className="text-xs text-red-400">×</button>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
