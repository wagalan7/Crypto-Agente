import { useState, useEffect, useCallback } from 'react'

const PLATFORM_ICONS: Record<string, { icon: string; color: string }> = {
  facebook:  { icon: '𝕗', color: 'text-blue-400 border-blue-800 bg-blue-900/20' },
  instagram: { icon: '◉', color: 'text-pink-400 border-pink-800 bg-pink-900/20' },
  twitter:   { icon: '✕', color: 'text-sky-400 border-sky-800 bg-sky-900/20' },
  google:    { icon: 'G', color: 'text-yellow-400 border-yellow-800 bg-yellow-900/20' },
  tiktok:    { icon: '♪', color: 'text-rose-400 border-rose-800 bg-rose-900/20' },
  webhook:   { icon: '⚡', color: 'text-violet-400 border-violet-800 bg-violet-900/20' },
}

interface Campaign {
  id: number
  owner: string
  produto: string
  created_at: string
  published_platforms?: string[]
}

interface CampaignDetail {
  id: number
  owner: string
  produto: string
  created_at: string
  input_data: Record<string, string>
  result_data: Record<string, string>
}

interface Grant {
  granted_to: string
  granted_by: string
  granted_at: string
}

interface Props {
  authHeaders: Record<string, string>
  isAdmin: boolean
  currentUser: string
  allUsers: { user: string; role: string }[]
}

export function CampaignHistory({ authHeaders, isAdmin, currentUser, allUsers }: Props) {
  const [campaigns, setCampaigns] = useState<Campaign[]>([])
  const [loading, setLoading]     = useState(false)
  const [expanded, setExpanded]   = useState<number | null>(null)
  const [detail, setDetail]       = useState<CampaignDetail | null>(null)
  const [grants, setGrants]       = useState<Grant[]>([])
  const [grantUser, setGrantUser] = useState('')
  const [msg, setMsg]             = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const r = await fetch('/campaigns', { headers: authHeaders })
      if (r.ok) setCampaigns(await r.json())
    } catch { /* ignore */ }
    setLoading(false)
  }, [authHeaders])


  useEffect(() => { load() }, [load])

  const flash = (m: string) => { setMsg(m); setTimeout(() => setMsg(''), 3000) }

  const openDetail = async (id: number) => {
    if (expanded === id) { setExpanded(null); setDetail(null); return }
    setExpanded(id)
    setDetail(null)
    setGrants([])
    try {
      const r = await fetch(`/campaigns/${id}`, { headers: authHeaders })
      if (r.ok) setDetail(await r.json())
      if (isAdmin) {
        const rg = await fetch(`/campaigns/${id}/grants`, { headers: authHeaders })
        if (rg.ok) setGrants(await rg.json())
      }
    } catch { /* ignore */ }
  }

  const handleGrant = async (campaignId: number) => {
    if (!grantUser.trim()) return
    try {
      const r = await fetch(`/campaigns/${campaignId}/grant`, {
        method: 'POST', headers: authHeaders,
        body: JSON.stringify({ granted_to: grantUser.trim() }),
      })
      if (r.ok) {
        flash(`Acesso liberado para ${grantUser}`)
        setGrantUser('')
        const rg = await fetch(`/campaigns/${campaignId}/grants`, { headers: authHeaders })
        if (rg.ok) setGrants(await rg.json())
      } else {
        const d = await r.json()
        flash(d.detail || 'Erro')
      }
    } catch { flash('Erro de conexão') }
  }

  const handleRevoke = async (campaignId: number, username: string) => {
    try {
      await fetch(`/campaigns/${campaignId}/grant/${encodeURIComponent(username)}`, {
        method: 'DELETE', headers: authHeaders,
      })
      setGrants(g => g.filter(x => x.granted_to !== username))
      flash(`Acesso revogado de ${username}`)
    } catch { /* ignore */ }
  }

  const AGENT_LABELS: Record<string, string> = {
    ESTRATEGIA: '◈ Estratégia', COPY: '✦ Copy', DESIGN: '◉ Design',
    VIDEO: '▶ Vídeo', SOCIAL: '◎ Social', ADS: '◆ Ads',
    AUTOMACAO: '⟳ Automação', PUBLICADOR: '↑ Publicador',
    ANALYTICS: '◐ Analytics', REVISOR: '✔ Revisor',
  }

  return (
    <div className="bg-gray-900/60 border border-gray-800 rounded-xl p-5 space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <div className="w-1.5 h-4 rounded-sm bg-gradient-to-b from-violet-500 to-blue-500" />
          <h2 className="text-sm font-bold text-gray-200 tracking-wide">HISTÓRICO DE CAMPANHAS</h2>
        </div>
        <button onClick={load} className="text-[10px] text-gray-500 hover:text-gray-300 transition-colors">
          ↺ atualizar
        </button>
      </div>

      {msg && (
        <div className="px-3 py-2 bg-violet-900/30 border border-violet-800 rounded-lg text-xs text-violet-300">
          {msg}
        </div>
      )}

      {loading ? (
        <p className="text-xs text-gray-600">Carregando...</p>
      ) : campaigns.length === 0 ? (
        <p className="text-xs text-gray-600">Nenhuma campanha salva ainda.</p>
      ) : (
        <div className="space-y-2">
          {campaigns.map(c => (
            <div key={c.id} className="border border-gray-800 rounded-lg overflow-hidden">
              {/* Row */}
              <button
                onClick={() => openDetail(c.id)}
                className="w-full flex items-center justify-between px-4 py-3 hover:bg-gray-800/40 transition-colors"
              >
                <div className="flex items-center gap-3">
                  <span className="w-6 h-6 rounded-md bg-violet-900/50 border border-violet-800 flex items-center justify-center text-[10px] text-violet-400 font-bold shrink-0">
                    #{c.id}
                  </span>
                  <div className="text-left">
                    <div className="flex items-center gap-2 flex-wrap">
                      <p className="text-xs text-gray-200 font-medium">{c.produto}</p>
                      {(c.published_platforms ?? []).map(p => {
                        const meta = PLATFORM_ICONS[p]
                        if (!meta) return null
                        return (
                          <span key={p} className={`text-[9px] border rounded px-1.5 py-0.5 font-medium ${meta.color}`}>
                            {meta.icon} {p}
                          </span>
                        )
                      })}
                    </div>
                    <p className="text-[10px] text-gray-600 mt-0.5">
                      {isAdmin ? `${c.owner} · ` : ''}{c.created_at}
                    </p>
                  </div>
                </div>
                <span className="text-gray-600 text-xs shrink-0 ml-2">{expanded === c.id ? '▲' : '▼'}</span>
              </button>

              {/* Detail */}
              {expanded === c.id && (
                <div className="border-t border-gray-800 px-4 py-4 space-y-4 bg-gray-900/40">
                  {!detail ? (
                    <p className="text-xs text-gray-600">Carregando detalhes...</p>
                  ) : (
                    <>
                      {/* Input summary */}
                      <div className="grid grid-cols-2 gap-2 text-[10px] text-gray-500">
                        {Object.entries(detail.input_data).filter(([k]) => k !== 'pagina_vendas').map(([k, v]) => (
                          <div key={k}><span className="text-gray-600 uppercase">{k}: </span>{v}</div>
                        ))}
                      </div>

                      {/* Agent outputs — skip internal keys */}
                      <div className="space-y-2">
                        {Object.entries(detail.result_data)
                          .filter(([k]) => !k.startsWith('_'))
                          .map(([agent, output]) => (
                            <AgentOutputBlock key={agent} label={AGENT_LABELS[agent] || agent} output={output} />
                          ))}
                      </div>

                      {/* Admin: manage grants */}
                      {isAdmin && c.owner !== currentUser && (
                        <div className="border-t border-gray-800 pt-3 space-y-2">
                          <p className="text-[10px] text-gray-500 uppercase tracking-widest">Proprietário: {c.owner}</p>
                        </div>
                      )}

                      {isAdmin && (
                        <div className="border-t border-gray-800 pt-3 space-y-3">
                          <p className="text-[10px] text-gray-500 uppercase tracking-widest">Gerenciar Acesso</p>

                          {/* Current grants */}
                          {grants.length > 0 && (
                            <div className="space-y-1">
                              {grants.map(g => (
                                <div key={g.granted_to} className="flex items-center justify-between px-2 py-1.5 bg-gray-800 rounded-md">
                                  <span className="text-[11px] text-gray-300">{g.granted_to}</span>
                                  <button
                                    onClick={() => handleRevoke(c.id, g.granted_to)}
                                    className="text-[9px] text-red-500 hover:text-red-400 transition-colors"
                                  >
                                    revogar
                                  </button>
                                </div>
                              ))}
                            </div>
                          )}

                          {/* Grant to user */}
                          <div className="flex gap-2">
                            <select
                              className="flex-1 bg-gray-800 border border-gray-700 rounded-md px-2.5 py-1.5 text-[11px] text-gray-200 focus:outline-none focus:border-violet-500"
                              value={grantUser}
                              onChange={e => setGrantUser(e.target.value)}
                            >
                              <option value="">Selecionar usuário...</option>
                              {allUsers
                                .filter(u => u.user !== currentUser && u.user !== c.owner)
                                .filter(u => !grants.find(g => g.granted_to === u.user))
                                .map(u => (
                                  <option key={u.user} value={u.user}>{u.user}</option>
                                ))}
                            </select>
                            <button
                              onClick={() => handleGrant(c.id)}
                              disabled={!grantUser}
                              className="px-3 py-1.5 rounded-md text-[11px] font-semibold
                                bg-violet-700 hover:bg-violet-600 disabled:bg-gray-800 disabled:text-gray-600
                                text-white transition-all"
                            >
                              + Liberar
                            </button>
                          </div>
                        </div>
                      )}
                    </>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function AgentOutputBlock({ label, output }: { label: string; output: string }) {
  const [open, setOpen] = useState(false)
  if (!output) return null
  return (
    <div className="border border-gray-800 rounded-md overflow-hidden">
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center justify-between px-3 py-2 hover:bg-gray-800/40 transition-colors"
      >
        <span className="text-[11px] text-gray-400 font-medium">{label}</span>
        <span className="text-[9px] text-gray-600">{open ? '▲' : '▼'}</span>
      </button>
      {open && (
        <div className="px-3 py-2 border-t border-gray-800 bg-gray-900/60">
          <pre className="text-[10px] text-gray-400 whitespace-pre-wrap font-mono leading-relaxed">
            {output}
          </pre>
        </div>
      )}
    </div>
  )
}
