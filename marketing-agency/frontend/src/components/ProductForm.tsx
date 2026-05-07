import { useState } from 'react'
import type { ProductInput } from '../types'

interface Props {
  onSubmit: (data: ProductInput) => void
  loading: boolean
}

const PLATFORMS = ['Instagram', 'TikTok', 'Facebook', 'YouTube', 'LinkedIn', 'Multi-plataforma']
const TONES = ['Profissional', 'Descontraído', 'Urgente', 'Inspirador', 'Educativo', 'Exclusivo']

export function ProductForm({ onSubmit, loading }: Props) {
  const [form, setForm] = useState<ProductInput>({
    produto: '',
    preco: '',
    publico: '',
    objetivo: '',
    plataforma: '',
    tom_de_voz: '',
  })

  const set = (key: keyof ProductInput) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>) =>
    setForm(f => ({ ...f, [key]: e.target.value }))

  const valid = Object.values(form).every(v => v.trim() !== '')

  return (
    <form
      onSubmit={e => { e.preventDefault(); if (valid) onSubmit(form) }}
      className="section-card space-y-4"
    >
      <h2 className="text-base font-bold text-gray-100 mb-1">Informações do Produto</h2>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div>
          <label className="block text-xs text-gray-400 mb-1">PRODUTO</label>
          <input className="input-field" placeholder="Ex: Curso de Tráfego Pago" value={form.produto} onChange={set('produto')} />
        </div>
        <div>
          <label className="block text-xs text-gray-400 mb-1">PREÇO</label>
          <input className="input-field" placeholder="Ex: R$ 497" value={form.preco} onChange={set('preco')} />
        </div>
      </div>

      <div>
        <label className="block text-xs text-gray-400 mb-1">PÚBLICO-ALVO</label>
        <input className="input-field" placeholder="Ex: Empreendedores 25–45 anos que querem vender online" value={form.publico} onChange={set('publico')} />
      </div>

      <div>
        <label className="block text-xs text-gray-400 mb-1">OBJETIVO</label>
        <input className="input-field" placeholder="Ex: 100 vendas em 30 dias" value={form.objetivo} onChange={set('objetivo')} />
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div>
          <label className="block text-xs text-gray-400 mb-1">PLATAFORMA</label>
          <select className="input-field" value={form.plataforma} onChange={set('plataforma')}>
            <option value="">Selecione...</option>
            {PLATFORMS.map(p => <option key={p} value={p}>{p}</option>)}
          </select>
        </div>
        <div>
          <label className="block text-xs text-gray-400 mb-1">TOM DE VOZ</label>
          <select className="input-field" value={form.tom_de_voz} onChange={set('tom_de_voz')}>
            <option value="">Selecione...</option>
            {TONES.map(t => <option key={t} value={t}>{t}</option>)}
          </select>
        </div>
      </div>

      <button type="submit" className="btn-primary" disabled={!valid || loading}>
        {loading ? 'Agência trabalhando...' : 'Ativar Agência de Marketing'}
      </button>
    </form>
  )
}
