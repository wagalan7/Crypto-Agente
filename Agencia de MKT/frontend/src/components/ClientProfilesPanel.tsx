import { useState, useEffect, useCallback } from 'react'

interface Props {
  authHeaders: Record<string, string>
  onProfilesChange?: () => void
}

interface Profile {
  id: number
  owner: string
  client_name: string
  credentials: Record<string, string>
  created_at: string
}

const CRED_FIELDS = [
  { key: 'fb_page_id',              label: 'Facebook — Page ID',            secret: false },
  { key: 'fb_token',                label: 'Facebook — Access Token',       secret: true  },
  { key: 'fb_ad_account_id',        label: 'Facebook Ads — Ad Account ID',  secret: false },
  { key: 'ig_user_id',              label: 'Instagram — IG Business User ID', secret: false },
  { key: 'ig_token',                label: 'Instagram — Access Token',      secret: true  },
  { key: 'google_developer_token',  label: 'Google Ads — Developer Token',  secret: true  },
  { key: 'google_customer_id',      label: 'Google Ads — Customer ID',      secret: false },
  { key: 'google_refresh_token',    label: 'Google Ads — Refresh Token',    secret: true  },
  { key: 'google_final_url',        label: 'Google Ads — URL de destino',   secret: false },
  { key: 'google_mcc_id',           label: 'Google Ads — MCC ID (opcional)',secret: false },
  { key: 'tiktok_access_token',     label: 'TikTok — Access Token',         secret: true  },
  { key: 'tiktok_advertiser_id',    label: 'TikTok — Advertiser ID',        secret: false },
  { key: 'image_url',               label: 'URL de imagem padrão',          secret: false },
  { key: 'webhook_url',             label: 'Webhook URL',                   secret: false },
]

interface FbPage { id: string; name: string; instagram_business_account_id: string }

export function ClientProfilesPanel({ authHeaders, onProfilesChange }: Props) {
  const [profiles, setProfiles]     = useState<Profile[]>([])
  const [loading, setLoading]       = useState(false)
  const [creating, setCreating]     = useState(false)
  const [editing, setEditing]       = useState<number | null>(null)
  const [newName, setNewName]       = useState('')
  const [draft, setDraft]           = useState<Record<string, string>>({})
  const [saving, setSaving]         = useState(false)
  const [msg, setMsg]               = useState('')
  const [expandedId, setExpandedId] = useState<number | null>(null)
  const [metaBusy, setMetaBusy]     = useState(false)
  const [metaMsg, setMetaMsg]       = useState<string | null>(null)
  const [metaPages, setMetaPages]   = useState<FbPage[]>([])

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const r = await fetch('/client-profiles', { headers: authHeaders })
      if (r.ok) setProfiles(await r.json())
    } catch { /* ignore */ }
    setLoading(false)
  }, [authHeaders])

  useEffect(() => { load() }, [load])

  const flash = (m: string) => { setMsg(m); setTimeout(() => setMsg(''), 3000) }

  const handleCreate = async () => {
    if (!newName.trim()) return
    setSaving(true)
    const filtered = Object.fromEntries(Object.entries(draft).filter(([, v]) => v.trim()))
    const r = await fetch('/client-profiles', {
      method: 'POST', headers: authHeaders,
      body: JSON.stringify({ client_name: newName.trim(), credentials: filtered }),
    })
    if (r.ok) {
      flash('✓ Perfil criado!')
      setCreating(false); setNewName(''); setDraft({})
      load(); onProfilesChange?.()
    } else {
      flash('✗ Erro ao criar perfil')
    }
    setSaving(false)
  }

  const handleUpdate = async (id: number) => {
    setSaving(true)
    const filtered = Object.fromEntries(Object.entries(draft).filter(([, v]) => v.trim()))
    const r = await fetch(`/client-profiles/${id}`, {
      method: 'PUT', headers: authHeaders,
      body: JSON.stringify({ credentials: filtered }),
    })
    if (r.ok) {
      flash('✓ Perfil atualizado!')
      setEditing(null); setDraft({})
      load(); onProfilesChange?.()
    } else {
      flash('✗ Erro ao salvar')
    }
    setSaving(false)
  }

  const handleDelete = async (id: number, name: string) => {
    if (!confirm(`Remover perfil "${name}"?`)) return
    await fetch(`/client-profiles/${id}`, { method: 'DELETE', headers: authHeaders })
    flash('✓ Perfil removido')
    load(); onProfilesChange?.()
  }

  const startEdit = (p: Profile) => {
    setEditing(p.id)
    setDraft({ ...p.credentials })
    setExpandedId(p.id)
    resetMeta()
  }

  const setField = (key: string, value: string) =>
    setDraft(d => ({ ...d, [key]: value }))

  // ── Meta helpers (Facebook/Instagram token & page ID) ──
  const listPages = async () => {
    const token = draft.fb_token || draft.ig_token || ''
    if (!token) { setMetaMsg('Cole o Access Token do Facebook (ou Instagram) antes de listar páginas.'); return }
    setMetaBusy(true); setMetaMsg(null); setMetaPages([])
    try {
      const r = await fetch('/auth/instagram/exchange-token', {
        method: 'POST', headers: authHeaders,
        body: JSON.stringify({ short_token: token }),
      })
      const data = await r.json()
      if (!r.ok) { setMetaMsg(data.detail || 'Erro ao listar páginas'); setMetaBusy(false); return }
      const pages: FbPage[] = data.pages || []
      setMetaPages(pages)
      setMetaMsg(pages.length
        ? `✓ ${pages.length} página(s) encontrada(s). Clique pra preencher.`
        : '⚠ Nenhuma página vinculada a esse token. Verifique permissões "pages_show_list".')
    } catch (e) { setMetaMsg(String(e)) }
    setMetaBusy(false)
  }

  const pickPage = (pg: FbPage) => {
    setDraft(d => ({
      ...d,
      fb_page_id: pg.id,
      ig_user_id: pg.instagram_business_account_id || d.ig_user_id || '',
    }))
    setMetaMsg(`✓ "${pg.name}" selecionada. Agora clique em "obter token PERMANENTE" abaixo.`)
    setMetaPages([])
  }

  const getPermanentToken = async () => {
    const token   = draft.fb_token || draft.ig_token || ''
    const pageId  = draft.fb_page_id || ''
    if (!token)  { setMetaMsg('Cole o Access Token antes.'); return }
    if (!pageId) { setMetaMsg('Preencha o Page ID (ou clique em "Listar minhas páginas" pra escolher).'); return }
    setMetaBusy(true); setMetaMsg(null)
    try {
      const r = await fetch('/auth/instagram/exchange-token', {
        method: 'POST', headers: authHeaders,
        body: JSON.stringify({ short_token: token, page_id: pageId }),
      })
      const data = await r.json()
      if (!r.ok) { setMetaMsg(data.detail || 'Erro'); setMetaBusy(false); return }
      if (data.permanent) {
        setDraft(d => ({
          ...d,
          fb_token:   data.access_token,           // permanent page token (works for both FB & IG)
          ig_token:   data.access_token,
          fb_page_id: data.page_id || d.fb_page_id,
          ig_user_id: data.instagram_business_account_id || d.ig_user_id || '',
        }))
        setMetaMsg(`✓ Token PERMANENTE da página "${data.page_name}" obtido! Não expira. Clique em Salvar.`)
      } else {
        const pgs = (data.pages || []) as FbPage[]
        setMetaPages(pgs)
        setMetaMsg(
          `⚠ Token só renovou pra ${data.expires_days ?? 60} dias (token de usuário).` +
          (pgs.length ? ` Páginas disponíveis listadas abaixo — clique na correta.` : '') +
          (data.warning ? ` ${data.warning}` : '')
        )
      }
    } catch (e) { setMetaMsg(String(e)) }
    setMetaBusy(false)
  }

  // Reset Meta panel state when opening/closing edit/create
  const resetMeta = () => { setMetaMsg(null); setMetaPages([]) }

  const countFilled = (creds: Record<string, string>) =>
    Object.values(creds).filter(v => v).length

  return (
    <div className="bg-gray-900/60 border border-gray-800 rounded-xl overflow-hidden">
      <div className="px-5 py-4 border-b border-gray-800 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-lg">🗂</span>
          <span className="text-sm font-bold text-gray-200 tracking-wide">PERFIS DE CLIENTES</span>
          <span className="text-[10px] bg-gray-800 border border-gray-700 text-gray-400 px-2 py-0.5 rounded-full">
            {profiles.length} perfil{profiles.length !== 1 ? 'is' : ''}
          </span>
        </div>
        <button
          onClick={() => { setCreating(c => !c); setEditing(null); setDraft({}); setNewName(''); resetMeta() }}
          className={`text-[11px] px-3 py-1 rounded-lg border transition-all
            ${creating ? 'border-violet-600 text-violet-300 bg-violet-900/20' : 'border-gray-700 text-gray-400 hover:text-gray-200 hover:border-gray-600'}`}>
          {creating ? '✕ Cancelar' : '+ Novo perfil'}
        </button>
      </div>

      <div className="px-5 py-4 space-y-3">
        {msg && (
          <div className={`px-3 py-2 rounded-lg text-xs border ${msg.startsWith('✓')
            ? 'bg-emerald-900/30 border-emerald-800 text-emerald-400'
            : 'bg-red-900/30 border-red-800 text-red-400'}`}>
            {msg}
          </div>
        )}

        {/* Create form */}
        {creating && (
          <div className="border border-violet-800/50 rounded-xl bg-violet-950/20 p-4 space-y-3">
            <p className="text-[10px] text-violet-400 font-semibold uppercase tracking-widest">Novo perfil de cliente</p>
            <div>
              <label className="block text-[9px] text-gray-500 uppercase tracking-wider mb-1">Nome do cliente *</label>
              <input
                className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-xs text-gray-200
                           placeholder-gray-600 focus:outline-none focus:border-violet-500"
                placeholder="Ex: Maqizi Store, João Silva..."
                value={newName}
                onChange={e => setNewName(e.target.value)}
                autoFocus
              />
            </div>
            <p className="text-[9px] text-gray-500 uppercase tracking-widest">Credenciais (preencha apenas as que usar):</p>

            <MetaHelperPanel
              metaBusy={metaBusy} metaMsg={metaMsg} metaPages={metaPages}
              onList={listPages} onPick={pickPage} onPermanent={getPermanentToken}
            />

            <div className="grid grid-cols-1 gap-2">
              {CRED_FIELDS.map(f => (
                <div key={f.key}>
                  <label className="block text-[9px] text-gray-600 mb-0.5">{f.label}</label>
                  <input
                    type={f.secret ? 'password' : 'text'}
                    className="w-full bg-gray-900 border border-gray-700 rounded-md px-2.5 py-1.5
                               text-[11px] text-gray-200 placeholder-gray-700
                               focus:outline-none focus:border-violet-500"
                    placeholder={f.secret ? '••••••••' : f.label}
                    value={draft[f.key] || ''}
                    onChange={e => setField(f.key, e.target.value)}
                  />
                </div>
              ))}
            </div>
            <button
              onClick={handleCreate}
              disabled={!newName.trim() || saving}
              className="w-full py-2 rounded-lg text-[11px] font-semibold text-white
                bg-gradient-to-r from-violet-700 to-blue-700 hover:from-violet-600 hover:to-blue-600
                disabled:opacity-50 transition-all">
              {saving ? 'Salvando...' : '💾 Criar perfil'}
            </button>
          </div>
        )}

        {/* Profile list */}
        {loading && <p className="text-xs text-gray-600 text-center py-4">Carregando...</p>}
        {!loading && profiles.length === 0 && !creating && (
          <div className="text-center py-8">
            <p className="text-2xl mb-2">🗂</p>
            <p className="text-xs text-gray-500">Nenhum perfil cadastrado ainda.<br />Crie um perfil para cada cliente.</p>
          </div>
        )}

        {profiles.map(p => (
          <div key={p.id} className="border border-gray-700 rounded-xl overflow-hidden">
            {/* Header */}
            <div className="flex items-center justify-between px-4 py-3 bg-gray-800/40">
              <div className="flex items-center gap-2">
                <div className="w-7 h-7 rounded-full bg-violet-900/50 border border-violet-700 flex items-center justify-center text-[11px] font-bold text-violet-300">
                  {p.client_name.charAt(0).toUpperCase()}
                </div>
                <div>
                  <p className="text-[11px] font-semibold text-gray-200">{p.client_name}</p>
                  <p className="text-[9px] text-gray-600">{countFilled(p.credentials)} credencial{countFilled(p.credentials) !== 1 ? 'is' : ''} salva{countFilled(p.credentials) !== 1 ? 's' : ''} · por {p.owner}</p>
                </div>
              </div>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => setExpandedId(expandedId === p.id ? null : p.id)}
                  className="text-[9px] text-gray-500 hover:text-gray-300 transition-colors">
                  {expandedId === p.id ? '▲ fechar' : '▼ ver'}
                </button>
                <button
                  onClick={() => editing === p.id ? (setEditing(null), setDraft({})) : startEdit(p)}
                  className={`text-[9px] px-2 py-1 rounded border transition-colors
                    ${editing === p.id
                      ? 'border-violet-700 text-violet-300 bg-violet-900/20'
                      : 'border-gray-700 text-gray-400 hover:text-gray-200'}`}>
                  {editing === p.id ? 'cancelar' : '✎ editar'}
                </button>
                <button
                  onClick={() => handleDelete(p.id, p.client_name)}
                  className="text-[9px] text-red-600 hover:text-red-400 transition-colors">
                  remover
                </button>
              </div>
            </div>

            {/* Credentials view / edit */}
            {(expandedId === p.id || editing === p.id) && (
              <div className="px-4 py-3 border-t border-gray-700 bg-gray-900/40 space-y-2">
                {editing === p.id ? (
                  <>
                    <MetaHelperPanel
                      metaBusy={metaBusy} metaMsg={metaMsg} metaPages={metaPages}
                      onList={listPages} onPick={pickPage} onPermanent={getPermanentToken}
                    />
                    {CRED_FIELDS.map(f => (
                      <div key={f.key}>
                        <label className="block text-[9px] text-gray-600 mb-0.5">{f.label}</label>
                        <input
                          type={f.secret ? 'password' : 'text'}
                          className="w-full bg-gray-800 border border-gray-700 rounded-md px-2.5 py-1.5
                                     text-[11px] text-gray-200 placeholder-gray-700
                                     focus:outline-none focus:border-violet-500"
                          placeholder={f.secret ? '••••••••' : f.label}
                          value={draft[f.key] || ''}
                          onChange={e => setField(f.key, e.target.value)}
                        />
                      </div>
                    ))}
                    <button
                      onClick={() => handleUpdate(p.id)}
                      disabled={saving}
                      className="w-full py-1.5 rounded-lg text-[11px] font-semibold text-white mt-1
                        bg-gradient-to-r from-violet-700 to-blue-700 hover:from-violet-600 hover:to-blue-600
                        disabled:opacity-50 transition-all">
                      {saving ? 'Salvando...' : '💾 Salvar alterações'}
                    </button>
                  </>
                ) : (
                  <div className="grid grid-cols-2 gap-1.5">
                    {CRED_FIELDS.filter(f => p.credentials[f.key]).map(f => (
                      <div key={f.key} className="bg-gray-800/60 rounded-lg px-2.5 py-1.5">
                        <p className="text-[8px] text-gray-600 uppercase tracking-wider">{f.label.split('—')[1]?.trim() || f.label}</p>
                        <p className="text-[10px] text-gray-300 truncate">
                          {f.secret ? '••••••••' : p.credentials[f.key]}
                        </p>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}

interface MetaHelperProps {
  metaBusy: boolean
  metaMsg: string | null
  metaPages: FbPage[]
  onList: () => void
  onPick: (pg: FbPage) => void
  onPermanent: () => void
}

function MetaHelperPanel({ metaBusy, metaMsg, metaPages, onList, onPick, onPermanent }: MetaHelperProps) {
  return (
    <div className="border border-blue-900/50 bg-blue-950/20 rounded-lg p-3 space-y-2">
      <p className="text-[10px] font-semibold text-blue-300 uppercase tracking-wider">
        🔗 Assistente Facebook / Instagram
      </p>
      <p className="text-[9px] text-gray-500 leading-relaxed">
        1. Cole o Access Token (curto, do Graph API Explorer) no campo abaixo.
        2. Clique <b>Listar páginas</b> pra descobrir o Page ID correto e IG vinculado.
        3. Clique <b>Obter token PERMANENTE</b> — substitui o token curto pelo token da página (não expira).
      </p>
      <div className="flex gap-2 flex-wrap">
        <button
          type="button"
          onClick={onList}
          disabled={metaBusy}
          className="text-[10px] px-3 py-1.5 rounded bg-blue-700 hover:bg-blue-600 disabled:bg-gray-700 text-white transition-colors">
          {metaBusy ? 'aguarde...' : '🔍 Listar páginas'}
        </button>
        <button
          type="button"
          onClick={onPermanent}
          disabled={metaBusy}
          className="text-[10px] px-3 py-1.5 rounded bg-emerald-700 hover:bg-emerald-600 disabled:bg-gray-700 text-white transition-colors">
          {metaBusy ? 'aguarde...' : '🔄 Obter token PERMANENTE'}
        </button>
      </div>
      {metaMsg && (
        <div className={`px-2.5 py-1.5 rounded text-[10px] border whitespace-pre-wrap break-words ${
          metaMsg.startsWith('✓') ? 'bg-emerald-900/30 border-emerald-800 text-emerald-300'
          : metaMsg.startsWith('⚠') ? 'bg-amber-900/30 border-amber-800 text-amber-300'
          : 'bg-red-900/30 border-red-800 text-red-300'
        }`}>
          {metaMsg}
        </div>
      )}
      {metaPages.length > 0 && (
        <div className="space-y-1">
          {metaPages.map(pg => (
            <button
              key={pg.id}
              type="button"
              onClick={() => onPick(pg)}
              className="w-full text-left px-2.5 py-1.5 rounded bg-gray-800 hover:bg-blue-900/40 border border-gray-700 hover:border-blue-700 transition-colors">
              <div className="text-[11px] text-gray-200 font-semibold">{pg.name}</div>
              <div className="text-[9px] text-gray-500 font-mono">Page ID: {pg.id}</div>
              {pg.instagram_business_account_id && (
                <div className="text-[9px] text-pink-400">IG Business ID: {pg.instagram_business_account_id}</div>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
