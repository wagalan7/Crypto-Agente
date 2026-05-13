import { useState, useEffect, useCallback } from 'react'

interface Props { authHeaders: Record<string, string> }

interface ClientStat {
  username: string
  name: string
  role: string
  campaign_count: number
  last_activity: string | null
}

function fmtDate(iso: string | null) {
  if (!iso) return 'Nunca'
  try { return new Date(iso).toLocaleString('pt-BR', { day: '2-digit', month: '2-digit', year: '2-digit', hour: '2-digit', minute: '2-digit' }) }
  catch { return iso }
}

function timeAgo(iso: string | null) {
  if (!iso) return null
  try {
    const diff = Date.now() - new Date(iso).getTime()
    const d = Math.floor(diff / 86400000)
    const h = Math.floor(diff / 3600000)
    const m = Math.floor(diff / 60000)
    if (d > 0) return `${d}d atrás`
    if (h > 0) return `${h}h atrás`
    if (m > 0) return `${m}m atrás`
    return 'agora'
  } catch { return null }
}

export function ClientsPanel({ authHeaders }: Props) {
  const [clients, setClients] = useState<ClientStat[]>([])
  const [loading, setLoading] = useState(false)
  const [search, setSearch] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    const r = await fetch('/admin/clients', { headers: authHeaders })
    if (r.ok) setClients(await r.json())
    setLoading(false)
  }, [authHeaders])

  useEffect(() => { load() }, [load])

  const filtered = clients.filter(c =>
    c.username.toLowerCase().includes(search.toLowerCase()) ||
    c.name.toLowerCase().includes(search.toLowerCase())
  )

  const totalCampaigns = clients.reduce((s, c) => s + c.campaign_count, 0)
  const activeToday = clients.filter(c => {
    if (!c.last_activity) return false
    return Date.now() - new Date(c.last_activity).getTime() < 86400000
  }).length

  return (
    <div className="bg-gray-900/60 border border-gray-800 rounded-xl overflow-hidden">
      <div className="px-5 py-4 border-b border-gray-800 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-lg">👥</span>
          <span className="text-sm font-bold text-gray-200 tracking-wide">CLIENTES</span>
          <span className="text-[10px] bg-gray-800 border border-gray-700 text-gray-400 px-2 py-0.5 rounded-full">
            {clients.length} usuário{clients.length !== 1 ? 's' : ''}
          </span>
        </div>
        <button onClick={load} className="text-[10px] text-gray-500 hover:text-gray-300 transition-colors">
          ↻ atualizar
        </button>
      </div>

      <div className="px-5 py-4 space-y-4">
        {/* Summary cards */}
        <div className="grid grid-cols-3 gap-3">
          {[
            { label: 'Total de clientes', value: clients.length, icon: '👤' },
            { label: 'Campanhas criadas', value: totalCampaigns, icon: '📋' },
            { label: 'Ativos hoje', value: activeToday, icon: '🟢' },
          ].map(card => (
            <div key={card.label} className="bg-gray-800/40 border border-gray-700 rounded-lg p-3 text-center">
              <p className="text-xl mb-1">{card.icon}</p>
              <p className="text-lg font-bold text-gray-100">{card.value}</p>
              <p className="text-[9px] text-gray-500 uppercase tracking-wider">{card.label}</p>
            </div>
          ))}
        </div>

        {/* Search */}
        <input
          className="w-full bg-gray-900 border border-gray-700 rounded-md px-3 py-1.5 text-xs text-gray-200
                     focus:outline-none focus:border-violet-500 placeholder:text-gray-600"
          placeholder="Buscar por nome ou usuário..."
          value={search}
          onChange={e => setSearch(e.target.value)}
        />

        {/* Table */}
        {loading && <p className="text-xs text-gray-600 text-center py-4">Carregando...</p>}
        {!loading && filtered.length === 0 && (
          <p className="text-xs text-gray-600 text-center py-4">Nenhum cliente encontrado.</p>
        )}
        {!loading && filtered.length > 0 && (
          <div className="space-y-1.5">
            {filtered.map(c => (
              <div key={c.username}
                className="flex items-center justify-between px-3 py-2.5 bg-gray-800/40 border border-gray-700 rounded-lg">
                <div className="flex items-center gap-3">
                  <div className={`w-7 h-7 rounded-full flex items-center justify-center text-[11px] font-bold
                    ${c.role === 'admin' ? 'bg-violet-900/50 border border-violet-700 text-violet-300'
                      : 'bg-gray-700/50 border border-gray-600 text-gray-300'}`}>
                    {(c.name || c.username).charAt(0).toUpperCase()}
                  </div>
                  <div>
                    <p className="text-[11px] text-gray-200 font-medium leading-tight">
                      {c.name || c.username}
                      {c.role === 'admin' && (
                        <span className="ml-1.5 text-[8px] text-violet-400 border border-violet-800 rounded px-1">admin</span>
                      )}
                    </p>
                    <p className="text-[9px] text-gray-600">@{c.username}</p>
                  </div>
                </div>
                <div className="flex items-center gap-4 text-right">
                  <div>
                    <p className="text-[11px] font-semibold text-gray-300">{c.campaign_count}</p>
                    <p className="text-[8px] text-gray-600">campanhas</p>
                  </div>
                  <div>
                    <p className="text-[10px] text-gray-400">{fmtDate(c.last_activity)}</p>
                    {timeAgo(c.last_activity) && (
                      <p className="text-[8px] text-gray-600 text-right">{timeAgo(c.last_activity)}</p>
                    )}
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
