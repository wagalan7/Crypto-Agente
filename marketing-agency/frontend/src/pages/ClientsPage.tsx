import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../services/api'
import type { Client } from '../types'
import { AuthorityScore } from '../components/AuthorityScore'
import { useAuth } from '../context/AuthContext'

const PLATFORMS = ['instagram', 'tiktok', 'youtube', 'linkedin', 'twitter']
const GOALS = ['Crescer seguidores', 'Vender produto', 'Gerar leads', 'Construir autoridade', 'Fidelizar audiência']

type SimpleUser = { id: number; email: string; name?: string; role: string }

function AccessModal({ client, users, onClose }: { client: Client; users: SimpleUser[]; onClose: () => void }) {
  const [granted, setGranted] = useState<number[]>([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState<number | null>(null)

  useEffect(() => {
    api.auth.listAccess(client.id)
      .then((data: any) => setGranted(data.map((g: any) => g.user_id)))
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [client.id])

  async function toggle(uid: number) {
    setSaving(uid)
    try {
      if (granted.includes(uid)) {
        await api.auth.revokeAccess(uid, client.id)
        setGranted(prev => prev.filter(x => x !== uid))
      } else {
        await api.auth.grantAccess(uid, client.id)
        setGranted(prev => [...prev, uid])
      }
    } catch (e: any) {
      alert('Erro: ' + e.message)
    } finally {
      setSaving(null)
    }
  }

  return (
    <div className="fixed inset-0 z-50 bg-black/70 flex items-center justify-center p-4" onClick={onClose}>
      <div className="card max-w-md w-full space-y-4" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold text-white">Acesso ao cliente: {client.name}</h3>
          <button onClick={onClose} className="text-gray-400 hover:text-white text-lg leading-none">×</button>
        </div>
        {loading ? (
          <p className="text-xs text-gray-500">Carregando...</p>
        ) : users.length === 0 ? (
          <p className="text-xs text-gray-500">Nenhum outro usuário cadastrado.</p>
        ) : (
          <div className="space-y-2">
            {users.map(u => (
              <div key={u.id} className="flex items-center justify-between p-2 rounded-lg bg-gray-800 border border-gray-700">
                <div className="min-w-0">
                  <p className="text-sm text-white truncate">{u.name || u.email}</p>
                  <p className="text-[11px] text-gray-500 truncate">{u.email} · <span className="capitalize text-violet-400">{u.role}</span></p>
                </div>
                <button
                  onClick={() => toggle(u.id)}
                  disabled={saving === u.id}
                  className={`text-xs px-3 py-1.5 rounded-lg font-medium border transition-colors shrink-0 ml-2 ${
                    granted.includes(u.id)
                      ? 'bg-green-900/30 border-green-700 text-green-400'
                      : 'bg-gray-700 border-gray-600 text-gray-300'
                  }`}
                >
                  {saving === u.id ? '...' : granted.includes(u.id) ? '✓ Acesso ativo' : 'Conceder'}
                </button>
              </div>
            ))}
          </div>
        )}
        <p className="text-[11px] text-gray-500">
          Usuários com acesso podem fazer tudo: rodar agentes, criar/aprovar/publicar conteúdo, configurar redes sociais.
        </p>
      </div>
    </div>
  )
}

export function ClientsPage() {
  const { user, logout } = useAuth()
  const [clients, setClients] = useState<Client[]>([])
  const [creating, setCreating] = useState(false)
  const [users, setUsers] = useState<SimpleUser[]>([])
  const [shareWith, setShareWith] = useState<number[]>([])
  const [managingClient, setManagingClient] = useState<Client | null>(null)
  const [form, setForm] = useState({
    name: '', niche: '', target_audience: '', tone: '', personality: '', positioning: '',
    goals: [] as string[], platforms: [] as string[],
  })
  const navigate = useNavigate()
  const isMaster = user?.role === 'master'

  useEffect(() => {
    api.clients.list().then((data: any) => setClients(data))
    if (isMaster) {
      api.auth.users().then((data: any) => setUsers(data)).catch(() => {})
    }
  }, [isMaster])

  async function submit() {
    const client: any = await api.clients.create(form)
    // Grant access to selected users (parallel — best effort)
    if (shareWith.length > 0) {
      await Promise.all(
        shareWith.map(uid =>
          api.auth.grantAccess(uid, client.id).catch(() => {})
        )
      )
    }
    setClients(prev => [client, ...prev])
    setCreating(false)
    setShareWith([])
    setForm({ name: '', niche: '', target_audience: '', tone: '', personality: '', positioning: '', goals: [], platforms: [] })
  }

  function toggleShare(uid: number) {
    setShareWith(prev => prev.includes(uid) ? prev.filter(x => x !== uid) : [...prev, uid])
  }

  function toggleArr(arr: string[], val: string): string[] {
    return arr.includes(val) ? arr.filter(x => x !== val) : [...arr, val]
  }

  return (
    <div className="min-h-screen bg-gray-950">
      <header className="border-b border-gray-800 px-4 md:px-8 py-4">
        <div className="max-w-5xl mx-auto flex items-center justify-between">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-lg bg-violet-600 flex items-center justify-center text-white font-bold text-sm">A</div>
            <div>
              <h1 className="text-sm md:text-base font-bold text-white">ContentAI Agency</h1>
              <p className="text-xs text-gray-500 hidden md:block">Plataforma de Autoridade Digital</p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            {user && <span className="text-xs text-gray-500 hidden md:block">{user.name} · <span className="text-violet-400 capitalize">{user.role}</span></span>}
            <button onClick={() => setCreating(true)} className="btn-primary w-auto px-4 py-2 text-xs md:text-sm">
              + Novo Cliente
            </button>
            <button onClick={() => { logout(); navigate('/login', { replace: true }) }} className="btn-secondary px-3 py-2 text-xs">
              Sair
            </button>
          </div>
        </div>
      </header>

      <main className="max-w-5xl mx-auto px-4 md:px-8 py-6">
        {creating && (
          <div className="card mb-6 space-y-4">
            <h2 className="font-semibold text-white">Novo Cliente</h2>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              <div>
                <label className="text-xs text-gray-400 mb-1 block">Nome *</label>
                <input className="input-field" placeholder="Nome do cliente" value={form.name}
                  onChange={e => setForm(p => ({ ...p, name: e.target.value }))} />
              </div>
              <div>
                <label className="text-xs text-gray-400 mb-1 block">Nicho</label>
                <input className="input-field" placeholder="Ex: Fitness, Finanças..." value={form.niche}
                  onChange={e => setForm(p => ({ ...p, niche: e.target.value }))} />
              </div>
              <div className="md:col-span-2">
                <label className="text-xs text-gray-400 mb-1 block">Público-alvo</label>
                <input className="input-field" placeholder="Descreva o público ideal" value={form.target_audience}
                  onChange={e => setForm(p => ({ ...p, target_audience: e.target.value }))} />
              </div>
              <div>
                <label className="text-xs text-gray-400 mb-1 block">Tom de voz</label>
                <input className="input-field" placeholder="Ex: Direto, inspiracional..." value={form.tone}
                  onChange={e => setForm(p => ({ ...p, tone: e.target.value }))} />
              </div>
              <div>
                <label className="text-xs text-gray-400 mb-1 block">Personalidade</label>
                <input className="input-field" placeholder="Ex: Autêntico, polêmico..." value={form.personality}
                  onChange={e => setForm(p => ({ ...p, personality: e.target.value }))} />
              </div>
              <div className="md:col-span-2">
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
                    className={`px-3 py-1.5 rounded-lg text-xs font-medium border transition-colors ${
                      form.platforms.includes(p)
                        ? 'bg-violet-600/20 border-violet-500 text-violet-300'
                        : 'bg-gray-800 border-gray-700 text-gray-400'
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
                    className={`px-3 py-1.5 rounded-lg text-xs font-medium border transition-colors ${
                      form.goals.includes(g)
                        ? 'bg-violet-600/20 border-violet-500 text-violet-300'
                        : 'bg-gray-800 border-gray-700 text-gray-400'
                    }`}>
                    {g}
                  </button>
                ))}
              </div>
            </div>

            {isMaster && users.length > 0 && (
              <div>
                <label className="text-xs text-gray-400 mb-2 block">
                  Compartilhar acesso com (mesmas permissões que você)
                </label>
                <div className="flex flex-wrap gap-2">
                  {users.map(u => (
                    <button key={u.id} onClick={() => toggleShare(u.id)}
                      className={`px-3 py-1.5 rounded-lg text-xs font-medium border transition-colors ${
                        shareWith.includes(u.id)
                          ? 'bg-violet-600/20 border-violet-500 text-violet-300'
                          : 'bg-gray-800 border-gray-700 text-gray-400'
                      }`}>
                      {shareWith.includes(u.id) ? '✓ ' : ''}{u.name || u.email}
                    </button>
                  ))}
                </div>
                <p className="text-[11px] text-gray-500 mt-1.5">
                  Usuários selecionados terão acesso total ao cliente (agentes, conteúdo, redes sociais, analytics).
                </p>
              </div>
            )}

            <div className="flex gap-2 pt-1">
              <button onClick={submit} disabled={!form.name} className="btn-primary w-auto px-5">Criar</button>
              <button onClick={() => setCreating(false)} className="btn-secondary">Cancelar</button>
            </div>
          </div>
        )}

        {clients.length === 0 ? (
          <div className="text-center py-20">
            <p className="text-gray-500 mb-3">Nenhum cliente cadastrado</p>
            <button onClick={() => setCreating(true)} className="btn-primary w-auto px-6">
              Criar primeiro cliente
            </button>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {clients.map(client => (
              <div
                key={client.id}
                className="card hover:border-violet-700 active:border-violet-500 transition-colors group relative"
              >
                <button
                  onClick={() => navigate(`/client/${client.id}`)}
                  className="text-left w-full"
                >
                  <div className="flex items-center gap-3">
                    <AuthorityScore score={client.authority_score} />
                    <div className="flex-1 min-w-0 pr-8">
                      <h3 className="font-semibold text-white group-hover:text-violet-300 transition-colors truncate">
                        {client.name}
                      </h3>
                      {client.niche && <p className="text-xs text-gray-400 mt-0.5 truncate">{client.niche}</p>}
                      {client.platforms.length > 0 && (
                        <div className="flex flex-wrap gap-1 mt-2">
                          {client.platforms.map(p => (
                            <span key={p} className="badge bg-gray-800 text-gray-400">{p}</span>
                          ))}
                        </div>
                      )}
                    </div>
                  </div>
                </button>
                {isMaster && (
                  <button
                    onClick={(e) => { e.stopPropagation(); setManagingClient(client) }}
                    className="absolute top-3 right-3 text-xs text-gray-500 hover:text-violet-400 border border-gray-700 rounded-md px-2 py-1"
                    title="Gerenciar acesso"
                  >
                    👥
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
      </main>

      {managingClient && (
        <AccessModal
          client={managingClient}
          users={users}
          onClose={() => setManagingClient(null)}
        />
      )}
    </div>
  )
}
