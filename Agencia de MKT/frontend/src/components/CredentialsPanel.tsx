import { useState, useEffect, useCallback } from 'react'

interface Props {
  authHeaders: Record<string, string>
  onLoaded?: (creds: AllCreds) => void
}

export type AllCreds = Record<string, Record<string, string>>

const PLATFORMS: {
  id: string; label: string; icon: string; color: string
  fields: { key: string; label: string; secret?: boolean }[]
  oauth?: boolean
}[] = [
  {
    id: 'facebook', label: 'Facebook / Facebook Ads', icon: '𝕗', color: 'text-blue-400',
    fields: [
      { key: 'fb_page_id',       label: 'Page ID (ID da Página)' },
      { key: 'fb_token',         label: 'Access Token (precisa de permissão ads_management)', secret: true },
      { key: 'fb_ad_account_id', label: 'Ad Account ID — encontrado em business.facebook.com → Contas de anúncios (ex: 123456789)' },
    ],
  },
  {
    id: 'instagram', label: 'Instagram', icon: '◉', color: 'text-pink-400',
    fields: [
      { key: 'ig_user_id', label: 'IG Business User ID' },
      { key: 'ig_token',   label: 'Access Token (Page)', secret: true },
    ],
  },
  {
    id: 'twitter', label: 'Twitter/X', icon: '✕', color: 'text-sky-400',
    fields: [
      { key: 'tw_api_key',       label: 'API Key' },
      { key: 'tw_api_secret',    label: 'API Secret',    secret: true },
      { key: 'tw_access_token',  label: 'Access Token',  secret: true },
      { key: 'tw_access_secret', label: 'Access Secret', secret: true },
    ],
  },
  {
    id: 'google', label: 'Google Ads', icon: 'G', color: 'text-yellow-400',
    oauth: true,
    fields: [
      { key: 'google_developer_token', label: 'Developer Token', secret: true },
      { key: 'google_customer_id',     label: 'Customer ID da conta de anúncios (sem hífens)' },
      { key: 'google_mcc_id',          label: 'ID da Conta MCC / Gerenciadora (se aplicável, sem hífens)' },
      { key: 'google_refresh_token',   label: 'Refresh Token', secret: true },
    ],
  },
  {
    id: 'tiktok', label: 'TikTok', icon: '♪', color: 'text-rose-400',
    fields: [
      { key: 'tiktok_access_token',  label: 'Access Token',  secret: true },
      { key: 'tiktok_advertiser_id', label: 'Advertiser ID' },
    ],
  },
  {
    id: 'webhook', label: 'Webhook', icon: '⚡', color: 'text-violet-400',
    fields: [
      { key: 'webhook_url', label: 'URL do Webhook' },
    ],
  },
]

export function CredentialsPanel({ authHeaders, onLoaded }: Props) {
  const [creds, setCreds]     = useState<AllCreds>({})
  const [drafts, setDrafts]   = useState<AllCreds>({})
  const [editing, setEditing] = useState<string | null>(null)
  const [saving, setSaving]         = useState<string | null>(null)
  const [success, setSuccess]       = useState<string | null>(null)
  const [error, setError]           = useState<string | null>(null)
  const [googleStatus, setGoogleStatus]     = useState<string | null>(null)
  const [fetchingAccounts, setFetchingAccounts]   = useState(false)
  const [googleAccounts, setGoogleAccounts]       = useState<string[]>([])
  const [igRefreshing, setIgRefreshing]   = useState(false)
  const [igRefreshMsg, setIgRefreshMsg]   = useState<string | null>(null)

  // Check URL params for Google OAuth result
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const g = params.get('google_ads')
    if (g === 'ok') {
      setGoogleStatus('✓ Google Ads conectado com sucesso!')
      window.history.replaceState({}, '', window.location.pathname)
      load()
    } else if (g === 'error') {
      setGoogleStatus('✗ Erro ao conectar: ' + (params.get('msg') || 'desconhecido'))
      window.history.replaceState({}, '', window.location.pathname)
    }
  }, [])

  const load = useCallback(async () => {
    try {
      const res = await fetch('/credentials', { headers: authHeaders })
      if (res.ok) {
        const data: AllCreds = await res.json()
        setCreds(data)
        // Only reset drafts for platforms NOT currently being edited
        setDrafts(prev => {
          const next = { ...data }
          // Keep the draft the user is actively editing
          if (editing && prev[editing]) next[editing] = prev[editing]
          return next
        })
        onLoaded?.(data)
      }
    } catch { /* ignore */ }
  }, [authHeaders, onLoaded, editing])

  useEffect(() => { load() }, [load])

  const startEdit = (platformId: string) => {
    setEditing(platformId)
    // Ensure draft has all fields initialized
    const p = PLATFORMS.find(p => p.id === platformId)!
    const existing = creds[platformId] || {}
    const draft: Record<string, string> = {}
    p.fields.forEach(f => { draft[f.key] = existing[f.key] || '' })
    setDrafts(d => ({ ...d, [platformId]: draft }))
  }

  const handleSave = async (platformId: string) => {
    setSaving(platformId)
    try {
      const res = await fetch('/credentials', {
        method: 'POST',
        headers: authHeaders,
        body: JSON.stringify({ platform: platformId, credentials: drafts[platformId] || {} }),
      })
      if (res.ok) {
        flash('Credenciais salvas ✓')
        setEditing(null)
        load()
      } else {
        flashErr('Erro ao salvar')
      }
    } catch { flashErr('Erro de conexão') }
    setSaving(null)
  }

  const handleDelete = async (platformId: string) => {
    if (!confirm(`Remover todas as credenciais de ${platformId}?`)) return
    await fetch(`/credentials/${platformId}`, { method: 'DELETE', headers: authHeaders })
    flash('Credenciais removidas')
    load()
  }

  const flash    = (m: string) => { setSuccess(m); setTimeout(() => setSuccess(null), 3000) }
  const flashErr = (m: string) => { setError(m);   setTimeout(() => setError(null),   4000) }

  const fetchGoogleAccounts = async () => {
    setFetchingAccounts(true)
    setGoogleAccounts([])
    try {
      const res = await fetch('/auth/google-ads/accounts', { headers: authHeaders })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'Erro')
      setGoogleAccounts(data.customers || [])
      if (!data.customers?.length) flashErr('Nenhuma conta encontrada')
    } catch (e: unknown) {
      flashErr(e instanceof Error ? e.message : 'Erro ao buscar contas')
    }
    setFetchingAccounts(false)
  }

  // Generic refresh: exchanges short token → long, then fetches permanent PAGE token.
  // platform: 'instagram' | 'facebook' — which token field to update.
  const refreshMetaToken = async (platform: 'instagram' | 'facebook') => {
    const tokenKey = platform === 'instagram' ? 'ig_token' : 'fb_token'
    const currentToken = (drafts[platform] || creds[platform] || {})[tokenKey] || ''
    if (!currentToken) { flashErr('Cole o token atual no campo antes de renovar.'); return }

    // Page ID is required for permanent token. Try the platform's own page_id field,
    // falling back to facebook.fb_page_id (Instagram uses the FB page it's linked to).
    const fbPageId =
      (drafts['facebook'] || creds['facebook'] || {})['fb_page_id'] ||
      (drafts['instagram'] || creds['instagram'] || {})['fb_page_id'] || ''
    if (!fbPageId) {
      flashErr('Preencha o Page ID (ID da Página do Facebook) antes de renovar — necessário para obter o token PERMANENTE da página.')
      return
    }

    setIgRefreshing(true); setIgRefreshMsg(null)
    try {
      const res = await fetch('/auth/instagram/exchange-token', {
        method: 'POST', headers: authHeaders,
        body: JSON.stringify({ short_token: currentToken, page_id: fbPageId }),
      })
      const data = await res.json()
      if (!res.ok) { flashErr(data.detail || 'Erro ao renovar token'); setIgRefreshing(false); return }
      const newToken = data.access_token
      setDraftField(platform, tokenKey, newToken)
      // If the response also returned the IG business account ID, prefill it (Instagram only)
      if (platform === 'instagram' && data.instagram_business_account_id) {
        setDraftField('instagram', 'ig_user_id', data.instagram_business_account_id)
      }
      if (data.permanent) {
        setIgRefreshMsg(`✓ Token PERMANENTE da página "${data.page_name}" obtido! Não expira. Clique em Salvar.`)
      } else {
        setIgRefreshMsg(`✓ Token renovado para ~${data.expires_days ?? 60} dias (token de usuário). Clique em Salvar.`)
      }
    } catch (e) { flashErr(String(e)) }
    setIgRefreshing(false)
  }

  const refreshInstagramToken = () => refreshMetaToken('instagram')
  const refreshFacebookToken  = () => refreshMetaToken('facebook')

  const startGoogleOAuth = () => {
    const token = authHeaders['Authorization']?.split(' ')[1] || ''
    if (!token) { flashErr('Faça login novamente antes de conectar o Google Ads'); return }
    window.location.href = `/auth/google-ads/start?token=${encodeURIComponent(token)}`
  }

  const selectGoogleAccount = async (customerId: string) => {
    setDraftField('google', 'google_customer_id', customerId)
    setGoogleAccounts([])
    flash(`Customer ID ${customerId} selecionado — clique em Salvar`)
  }

  const hasCreds = (platformId: string) =>
    Object.values(creds[platformId] || {}).some(v => v)

  const setDraftField = (platformId: string, key: string, value: string) =>
    setDrafts(d => ({ ...d, [platformId]: { ...(d[platformId] || {}), [key]: value } }))

  return (
    <div className="bg-gray-900/60 border border-gray-800 rounded-xl p-5 space-y-4">
      <div className="flex items-center gap-2">
        <div className="w-1.5 h-4 rounded-sm bg-gradient-to-b from-violet-500 to-blue-500" />
        <h2 className="text-sm font-bold text-gray-200 tracking-wide">CREDENCIAIS DE PLATAFORMAS</h2>
      </div>
      <p className="text-[10px] text-gray-600">
        Salvas com segurança no servidor — preenchidas automaticamente ao publicar.
      </p>

      {success      && <div className="px-3 py-2 bg-emerald-900/30 border border-emerald-800 rounded-lg text-xs text-emerald-400">{success}</div>}
      {error        && <div className="px-3 py-2 bg-red-900/30 border border-red-800 rounded-lg text-xs text-red-400">{error}</div>}
      {googleStatus && (
        <div className={`px-3 py-2 rounded-lg text-xs border ${googleStatus.startsWith('✓')
          ? 'bg-emerald-900/30 border-emerald-800 text-emerald-400'
          : 'bg-red-900/30 border-red-800 text-red-400'}`}>
          {googleStatus}
        </div>
      )}

      <div className="space-y-3">
        {PLATFORMS.map(p => {
          const saved = hasCreds(p.id)
          const isEditing = editing === p.id
          const isSaving  = saving === p.id

          return (
            <div key={p.id} className="border border-gray-700 rounded-xl overflow-hidden">
              {/* Header row */}
              <div className="flex items-center justify-between px-4 py-3 bg-gray-800/40">
                <div className="flex items-center gap-2">
                  <span className={`text-base ${p.color}`}>{p.icon}</span>
                  <span className="text-xs font-semibold text-gray-200">{p.label}</span>
                  {saved
                    ? <span className="text-[9px] bg-emerald-900/40 text-emerald-400 border border-emerald-800 px-1.5 py-0.5 rounded-full">✓ configurado</span>
                    : <span className="text-[9px] text-gray-600">não configurado</span>}
                </div>
                <div className="flex items-center gap-2">
                  {saved && !isEditing && (
                    <button onClick={() => handleDelete(p.id)}
                      className="text-[10px] text-red-600 hover:text-red-400 transition-colors">
                      remover
                    </button>
                  )}
                  <button
                    onClick={() => isEditing ? setEditing(null) : startEdit(p.id)}
                    className={`text-[10px] px-2.5 py-1 rounded border transition-colors
                      ${isEditing
                        ? 'text-violet-300 bg-violet-900/30 border-violet-700'
                        : 'text-gray-400 border-gray-700 hover:text-gray-200 hover:border-gray-600'}`}>
                    {isEditing ? '▲ fechar' : saved ? '✎ editar' : '+ adicionar'}
                  </button>
                </div>
              </div>

              {/* Edit form */}
              {isEditing && (
                <div className="px-4 py-4 border-t border-gray-700 bg-gray-900/50 space-y-3">

                  {/* Google OAuth button */}
                  {p.oauth && (
                    <div className="bg-blue-950/30 border border-blue-800/40 rounded-lg p-3">
                      <p className="text-[10px] text-blue-300 font-semibold mb-1">
                        ✦ Conectar via OAuth (recomendado para Refresh Token)
                      </p>
                      <p className="text-[9px] text-gray-500 mb-2">
                        Clique para autorizar o Maga One a acessar sua conta Google Ads.
                        O Refresh Token é gerado e salvo automaticamente.
                      </p>
                      <p className="text-[9px] text-amber-600 mb-2">
                        ⚠ Requer GOOGLE_CLIENT_ID e GOOGLE_CLIENT_SECRET configurados no Railway.
                      </p>
                      <button
                        onClick={startGoogleOAuth}
                        className="px-4 py-1.5 rounded-lg text-[11px] font-semibold
                          bg-gradient-to-r from-blue-700 to-blue-600 text-white
                          hover:from-blue-600 hover:to-blue-500 transition-all">
                        🔗 Conectar Google Ads
                      </button>
                    </div>
                  )}

                  <p className="text-[9px] text-gray-600 uppercase tracking-widest">
                    {p.oauth ? 'Ou preencha manualmente:' : 'Credenciais:'}
                  </p>

                  {/* Meta token refresh message (Facebook & Instagram) */}
                  {(p.id === 'instagram' || p.id === 'facebook') && igRefreshMsg && (
                    <div className="px-3 py-2 bg-emerald-900/30 border border-emerald-800 rounded-lg text-[10px] text-emerald-400">
                      {igRefreshMsg}
                    </div>
                  )}

                  {p.fields.map(f => (
                    <div key={f.key}>
                      <div className="flex items-center justify-between mb-0.5">
                        <label className="text-[9px] text-gray-500 uppercase tracking-wider">{f.label}</label>
                        {/* Instagram token: trocar por token PERMANENTE da página */}
                        {p.id === 'instagram' && f.key === 'ig_token' && (
                          <button
                            onClick={refreshInstagramToken}
                            disabled={igRefreshing}
                            className="text-[9px] text-pink-400 hover:text-pink-300 disabled:text-gray-600 transition-colors flex items-center gap-1">
                            {igRefreshing
                              ? <><span className="w-2 h-2 border border-pink-400/30 border-t-pink-400 rounded-full animate-spin inline-block"/>renovando...</>
                              : '🔄 obter token PERMANENTE da página'}
                          </button>
                        )}
                        {/* Facebook token: trocar por token PERMANENTE da página */}
                        {p.id === 'facebook' && f.key === 'fb_token' && (
                          <button
                            onClick={refreshFacebookToken}
                            disabled={igRefreshing}
                            className="text-[9px] text-blue-400 hover:text-blue-300 disabled:text-gray-600 transition-colors flex items-center gap-1">
                            {igRefreshing
                              ? <><span className="w-2 h-2 border border-blue-400/30 border-t-blue-400 rounded-full animate-spin inline-block"/>renovando...</>
                              : '🔄 obter token PERMANENTE da página'}
                          </button>
                        )}
                        {/* Customer ID: botão buscar automático */}
                        {p.id === 'google' && f.key === 'google_customer_id' && (
                          <button
                            onClick={fetchGoogleAccounts}
                            disabled={fetchingAccounts || !creds.google?.google_refresh_token}
                            className="text-[9px] text-blue-400 hover:text-blue-300 disabled:text-gray-600 transition-colors">
                            {fetchingAccounts ? '⟳ buscando...' : '⟳ buscar automaticamente'}
                          </button>
                        )}
                        {/* Developer Token: link direto */}
                        {p.id === 'google' && f.key === 'google_developer_token' && (
                          <a href="https://ads.google.com/aw/apicenter" target="_blank" rel="noopener noreferrer"
                            className="text-[9px] text-blue-400 hover:text-blue-300 transition-colors">
                            ↗ Google Ads → Central da API
                          </a>
                        )}
                      </div>

                      {/* Dropdown de contas para customer_id */}
                      {p.id === 'google' && f.key === 'google_customer_id' && googleAccounts.length > 0 && (
                        <div className="mb-1.5 bg-gray-800 border border-blue-700 rounded-md overflow-hidden">
                          <p className="text-[9px] text-blue-400 px-2.5 pt-1.5 pb-0.5">Selecione sua conta:</p>
                          {googleAccounts.map(id => (
                            <button key={id} onClick={() => selectGoogleAccount(id)}
                              className="w-full text-left px-2.5 py-1.5 text-[11px] text-gray-200
                                         hover:bg-blue-900/30 transition-colors font-mono">
                              {id}
                            </button>
                          ))}
                        </div>
                      )}

                      <input
                        type={f.secret ? 'password' : 'text'}
                        className="w-full bg-gray-800 border border-gray-700 rounded-md px-2.5 py-1.5
                                   text-[11px] text-gray-200 placeholder-gray-600
                                   focus:outline-none focus:border-violet-500"
                        placeholder={f.secret ? '••••••••' : f.label}
                        value={(drafts[p.id] || {})[f.key] || ''}
                        onChange={e => setDraftField(p.id, f.key, e.target.value)}
                      />
                    </div>
                  ))}

                  <div className="flex gap-2 pt-1">
                    <button
                      onClick={() => handleSave(p.id)}
                      disabled={isSaving}
                      className="flex-1 py-1.5 rounded-lg text-[11px] font-semibold text-white
                        bg-gradient-to-r from-violet-700 to-blue-700 hover:from-violet-600 hover:to-blue-600
                        disabled:opacity-50 transition-all">
                      {isSaving ? 'Salvando...' : '💾 Salvar credenciais'}
                    </button>
                    <button onClick={() => setEditing(null)}
                      className="px-3 py-1.5 rounded-lg text-[11px] text-gray-500
                        border border-gray-700 hover:border-gray-600 hover:text-gray-300 transition-all">
                      Cancelar
                    </button>
                  </div>
                </div>
              )}
            </div>
          )
        })}
      </div>

      {/* Google OAuth setup instructions */}
      <details className="group">
        <summary className="text-[10px] text-gray-600 hover:text-gray-400 cursor-pointer select-none">
          ▸ Como configurar Google OAuth no Railway
        </summary>
        <div className="mt-2 space-y-1 bg-gray-800/40 border border-gray-700 rounded-lg p-3">
          {[
            '1. Acesse console.cloud.google.com → Criar projeto',
            '2. APIs & Services → OAuth consent screen → External → preencha nome',
            '3. Credentials → Create Credentials → OAuth client ID → Web application',
            `4. Authorized redirect URI: ${window.location.origin}/auth/google-ads/callback`,
            '5. Copie Client ID e Client Secret',
            '6. No Railway: Variables → GOOGLE_CLIENT_ID e GOOGLE_CLIENT_SECRET',
          ].map((s, i) => (
            <p key={i} className="text-[10px] text-gray-400">{s}</p>
          ))}
        </div>
      </details>
    </div>
  )
}
