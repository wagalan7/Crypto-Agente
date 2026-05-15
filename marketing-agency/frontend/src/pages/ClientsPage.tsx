import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../services/api'
import type { Client } from '../types'
import { AuthorityScore } from '../components/AuthorityScore'

const PLATFORMS = ['instagram', 'tiktok', 'youtube', 'linkedin', 'twitter']
const GOALS = ['Crescer seguidores', 'Vender produto', 'Gerar leads', 'Construir autoridade', 'Fidelizar audiência']

export function ClientsPage() {
  const [clients, setClients] = useState<Client[]>([])
  const [creating, setCreating] = useState(false)
  const [form, setForm] = useState({
    name: '', niche: '', target_audience: '', tone: '', personality: '', positioning: '',
    goals: [] as string[], platforms: [] as string[],
  })
  const navigate = useNavigate()

  useEffect(() => {
    api.clients.list().then((data: any) => setClients(data))
  }, [])

  async function submit() {
    const client: any = await api.clients.create(form)
    setClients(prev => [client, ...prev])
    setCreating(false)
    setForm({ name: '', niche: '', target_audience: '', tone: '', personality: '', positioning: '', goals: [], platforms: [] })
  }

  function toggleArr(arr: string[], val: string): string[] {
    return arr.includes(val) ? arr.filter(x => x !== val) : [...arr, val]
  }

  return (
    <div className="min-h-screen bg-gray-950">
      <header className="border-b border-gray-800 px-8 py-5">
        <div className="max-w-5xl mx-auto flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-lg bg-violet-600 flex items-center justify-center text-white font-bold text-sm">A</div>
            <div>
              <h1 className="text-base font-bold text-white">ContentAI Agency</h1>
              <p className="text-xs text-gray-500">Plataforma de Autoridade Digital</p>
            </div>
          </div>
          <button onClick={() => setCreating(true)} className="btn-primary w-auto px-5">
            + Novo Cliente
          </button>
        </div>
      </header>

      <main className="max-w-5xl mx-auto px-8 py-8">
        {creating && (
          <div className="card mb-8 space-y-4">
            <h2 className="font-semibold text-white">Novo Cliente</h2>
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="text-xs text-gray-400 mb-1 block">Nome *</label>
                <input className="input-field" placeholder="Nome do cliente" value={form.name}
                  onChange={e => setForm(p => ({ ...p, name: e.target.value }))} />
              </div>
              <div>
                <label className="text-xs text-gray-400 mb-1 block">Nicho</label>
                <input className="input-field" placeholder="Ex: Fitness, Finanças, Marketing..." value={form.niche}
                  onChange={e => setForm(p => ({ ...p, niche: e.target.value }))} />
              </div>
              <div className="col-span-2">
                <label className="text-xs text-gray-400 mb-1 block">Público-alvo</label>
                <input className="input-field" placeholder="Descreva o público ideal" value={form.target_audience}
                  onChange={e => setForm(p => ({ ...p, target_audience: e.target.value }))} />
              </div>
              <div>
                <label className="text-xs text-gray-400 mb-1 block">Tom de voz</label>
                <input className="input-field" placeholder="Ex: Direto, inspiracional, educativo..." value={form.tone}
                  onChange={e => setForm(p => ({ ...p, tone: e.target.value }))} />
              </div>
              <div>
                <label className="text-xs text-gray-400 mb-1 block">Personalidade</label>
                <input className="input-field" placeholder="Ex: Autêntico, polêmico, técnico..." value={form.personality}
                  onChange={e => setForm(p => ({ ...p, personality: e.target.value }))} />
              </div>
              <div className="col-span-2">
                <label className="text-xs text-gray-400 mb-1 block">Posicionamento</label>
                <input className="input-field" placeholder="Diferencial único do cliente" value={form.positioning}
                  onChange={e => setForm(p => ({ ...p, positioning: e.target.value }))} />
              </div>
            </div>

            <div>
              <label className="text-xs text-gray-400 mb-2 block">Plataformas</label>
              <div className="flex flex-wrap gap-2">
                {PLATFORMS.map(p => (
                  <button key={p} onClick={() => setForm(f => ({ ...f, platforms: toggleArr(f.platforms, p) }))}
                    className={`px-3 py-1 rounded-lg text-xs font-medium border transition-colors ${
                      form.platforms.includes(p)
                        ? 'bg-violet-600/20 border-violet-500 text-violet-300'
                        : 'bg-gray-800 border-gray-700 text-gray-400 hover:border-gray-600'
                    }`}>
                    {p}
                  </button>
                ))}
              </div>
            </div>

            <div>
              <label className="text-xs text-gray-400 mb-2 block">Objetivos</label>
              <div className="flex flex-wrap gap-2">
                {GOALS.map(g => (
                  <button key={g} onClick={() => setForm(f => ({ ...f, goals: toggleArr(f.goals, g) }))}
                    className={`px-3 py-1 rounded-lg text-xs font-medium border transition-colors ${
                      form.goals.includes(g)
                        ? 'bg-violet-600/20 border-violet-500 text-violet-300'
                        : 'bg-gray-800 border-gray-700 text-gray-400 hover:border-gray-600'
                    }`}>
                    {g}
                  </button>
                ))}
              </div>
            </div>

            <div className="flex gap-3 pt-2">
              <button onClick={submit} disabled={!form.name} className="btn-primary w-auto px-6">
                Criar Cliente
              </button>
              <button onClick={() => setCreating(false)} className="btn-secondary">Cancelar</button>
            </div>
          </div>
        )}

        {clients.length === 0 ? (
          <div className="text-center py-20">
            <p className="text-gray-500 mb-2">Nenhum cliente cadastrado</p>
            <button onClick={() => setCreating(true)} className="btn-primary w-auto px-6">
              Criar primeiro cliente
            </button>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {clients.map(client => (
              <button
                key={client.id}
                onClick={() => navigate(`/client/${client.id}`)}
                className="card text-left hover:border-violet-700 transition-colors group"
              >
                <div className="flex items-start gap-4">
                  <AuthorityScore score={client.authority_score} />
                  <div className="flex-1 min-w-0">
                    <h3 className="font-semibold text-white group-hover:text-violet-300 transition-colors">
                      {client.name}
                    </h3>
                    {client.niche && <p className="text-xs text-gray-400 mt-0.5">{client.niche}</p>}
                    {client.platforms.length > 0 && (
                      <div className="flex flex-wrap gap-1 mt-2">
                        {client.platforms.map(p => (
                          <span key={p} className="badge bg-gray-800 text-gray-400">{p}</span>
                        ))}
                      </div>
                    )}
                    {client.goals.length > 0 && (
                      <div className="flex flex-wrap gap-1 mt-1">
                        {client.goals.slice(0, 2).map(g => (
                          <span key={g} className="badge bg-violet-900/30 text-violet-400">{g}</span>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              </button>
            ))}
          </div>
        )}
      </main>
    </div>
  )
}
