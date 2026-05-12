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
    produto: '', preco: '', publico: '', objetivo: '', plataforma: '', tom_de_voz: '', pagina_vendas: '', orcamento: '',
  })

  const set = (key: keyof ProductInput) =>
    (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
      setForm(f => ({ ...f, [key]: e.target.value }))

  const required = ['produto','preco','publico','objetivo','plataforma','tom_de_voz'] as const
  const valid = required.every(k => (form[k] ?? '').trim() !== '')

  return (
    <form onSubmit={e => { e.preventDefault(); if (valid && !loading) onSubmit(form) }}
      className="bg-gray-900/60 border border-gray-800 rounded-xl p-5 space-y-4">

      <div className="flex items-center gap-2 mb-1">
        <div className="w-1.5 h-4 rounded-sm bg-gradient-to-b from-violet-500 to-blue-500" />
        <h2 className="text-sm font-bold text-gray-200 tracking-wide">PRODUTO</h2>
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className="block text-[10px] text-gray-500 mb-1 tracking-widest uppercase">Nome</label>
          <input className="input-field" placeholder="Ex: Curso de Tráfego Pago" value={form.produto} onChange={set('produto')} disabled={loading} />
        </div>
        <div>
          <label className="block text-[10px] text-gray-500 mb-1 tracking-widest uppercase">Preço do produto</label>
          <input className="input-field" placeholder="Ex: R$ 497" value={form.preco} onChange={set('preco')} disabled={loading} />
        </div>
      </div>

      <div>
        <label className="block text-[10px] text-gray-500 mb-1 tracking-widest uppercase">
          Orçamento para anúncios <span className="text-gray-700 normal-case">(opcional)</span>
        </label>
        <input
          className="input-field"
          placeholder="Ex: R$ 50/dia ou R$ 1.500/mês"
          value={form.orcamento ?? ''}
          onChange={set('orcamento')}
          disabled={loading}
        />
      </div>

      <div>
        <label className="block text-[10px] text-gray-500 mb-1 tracking-widest uppercase">Página de Vendas</label>
        <input
          className="input-field"
          placeholder="https://seusite.com/produto (opcional)"
          value={form.pagina_vendas ?? ''}
          onChange={set('pagina_vendas')}
          disabled={loading}
        />
      </div>

      <div>
        <label className="block text-[10px] text-gray-500 mb-1 tracking-widest uppercase">Público-alvo</label>
        <input className="input-field" placeholder="Ex: Empreendedores 25–45 que querem vender online" value={form.publico} onChange={set('publico')} disabled={loading} />
      </div>

      <div>
        <label className="block text-[10px] text-gray-500 mb-1 tracking-widest uppercase">Objetivo</label>
        <input className="input-field" placeholder="Ex: 100 vendas em 30 dias" value={form.objetivo} onChange={set('objetivo')} disabled={loading} />
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className="block text-[10px] text-gray-500 mb-1 tracking-widest uppercase">Plataforma</label>
          <select className="input-field" value={form.plataforma} onChange={set('plataforma')} disabled={loading}>
            <option value="">Selecione...</option>
            {PLATFORMS.map(p => <option key={p}>{p}</option>)}
          </select>
        </div>
        <div>
          <label className="block text-[10px] text-gray-500 mb-1 tracking-widest uppercase">Tom de Voz</label>
          <select className="input-field" value={form.tom_de_voz} onChange={set('tom_de_voz')} disabled={loading}>
            <option value="">Selecione...</option>
            {TONES.map(t => <option key={t}>{t}</option>)}
          </select>
        </div>
      </div>

      <button type="submit" className="btn-run" disabled={!valid || loading}>
        {loading
          ? <span className="flex items-center justify-center gap-2"><span className="w-3.5 h-3.5 border-2 border-white/30 border-t-white rounded-full animate-spin" />Agência operando...</span>
          : '⚡ Ativar Agência'}
      </button>
    </form>
  )
}
