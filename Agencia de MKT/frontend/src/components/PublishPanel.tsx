import { useState, useEffect } from 'react'
import type { AllCreds } from './CredentialsPanel'
import { MetricsPanel } from './MetricsPanel'

interface Credentials {
  fb_page_id: string; fb_token: string
  ig_user_id: string; ig_token: string
  tw_api_key: string; tw_api_secret: string; tw_access_token: string; tw_access_secret: string
  webhook_url: string; image_url: string
  google_developer_token: string; google_customer_id: string; google_refresh_token: string
  google_mcc_id: string; google_final_url: string
  tiktok_access_token: string; tiktok_advertiser_id: string
}

interface PublishResult {
  platform: string; success: boolean; post_id?: string; url?: string; error?: string
}

interface Props {
  publisherOutput: string
  copyOutput: string
  socialOutput: string
  designOutput: string
  adsOutput: string
  userBudget: string
  savedCreds: AllCreds
  authHeaders: Record<string, string>
}

const PLATFORMS = [
  { id: 'facebook',  label: 'Facebook',   icon: '𝕗', color: 'text-blue-400'   },
  { id: 'instagram', label: 'Instagram',  icon: '◉',  color: 'text-pink-400'   },
  { id: 'twitter',   label: 'Twitter/X',  icon: '✕',  color: 'text-sky-400'    },
  { id: 'google',    label: 'Google Ads', icon: 'G',  color: 'text-yellow-400' },
  { id: 'tiktok',    label: 'TikTok',     icon: '♪',  color: 'text-rose-400'   },
  { id: 'webhook',   label: 'Webhook',    icon: '⚡',  color: 'text-violet-400' },
]

const CRED_GUIDES: Record<string, { steps: string[]; link: string }> = {
  facebook: {
    steps: [
      '1. Acesse developers.facebook.com → Meus Apps → Criar App',
      '2. Adicione o produto "Páginas"',
      '3. Graph API Explorer → gere token com pages_manage_posts',
      '4. Page ID: facebook.com/[nome-pagina]/about',
    ],
    link: 'https://developers.facebook.com/apps',
  },
  instagram: {
    steps: [
      '1. Conta deve ser Business/Creator no Instagram',
      '2. Conecte ao Facebook Page em Configurações → Conta',
      '3. Graph API Explorer: GET /me/accounts → pegue access_token da página',
      '4. GET /{page-id}?fields=instagram_business_account → ig_user_id',
      '5. Imagem: precisa ser URL pública (Cloudinary, S3, etc.)',
    ],
    link: 'https://developers.facebook.com/docs/instagram-api/getting-started',
  },
  twitter: {
    steps: [
      '1. developer.twitter.com → Projects → Create App',
      '2. Keys and Tokens: copie API Key + API Secret',
      '3. Gere Access Token + Access Secret (permissão Read+Write)',
    ],
    link: 'https://developer.twitter.com/en/portal/dashboard',
  },
  google: {
    steps: [
      '1. ads.google.com → Ferramentas → Centro da API → solicite Developer Token',
      '2. Google Cloud Console → OAuth2 → gere Refresh Token (escopo adwords)',
      '3. Customer ID: número 10 dígitos no topo da conta Google Ads',
    ],
    link: 'https://developers.google.com/google-ads/api/docs/get-started/introduction',
  },
  tiktok: {
    steps: [
      '1. business.tiktok.com → Developer → Create App',
      '2. Solicite permissão de "Ad Management"',
      '3. Gere Access Token com escopo advertising',
      '4. Advertiser ID: encontrado na URL do painel ads.tiktok.com',
    ],
    link: 'https://ads.tiktok.com/marketing_api/docs',
  },
}

// Budget ranges per platform (R$/day) shown as guidance
const BUDGET_DEFAULTS: Record<string, { min: number; max: number; currency: string }> = {
  facebook:  { min: 15,  max: 150,  currency: 'R$/dia' },
  instagram: { min: 15,  max: 150,  currency: 'R$/dia' },
  twitter:   { min: 20,  max: 200,  currency: 'R$/dia' },
  google:    { min: 30,  max: 500,  currency: 'R$/dia' },
  tiktok:    { min: 50,  max: 300,  currency: 'R$/dia' },
}

const EMPTY: Credentials = {
  fb_page_id: '', fb_token: '', ig_user_id: '', ig_token: '',
  tw_api_key: '', tw_api_secret: '', tw_access_token: '', tw_access_secret: '',
  webhook_url: '', image_url: '',
  google_developer_token: '', google_customer_id: '', google_refresh_token: '',
  google_mcc_id: '', google_final_url: '',
  tiktok_access_token: '', tiktok_advertiser_id: '',
}

function extractImagePrompt(designOutput: string): string {
  if (!designOutput) return ''
  const lines = designOutput.split('\n')
  for (let i = 0; i < lines.length; i++) {
    const up = lines[i].toUpperCase()
    if (up.includes('PROMPT IA') || up.includes('PROMPT IMAGEM') || up.includes('PROMPT FEED')) {
      return lines.slice(i, i + 3).join(' ').replace(/PROMPT IA [A-ZÁÉÍÓÚ]*\s*[:|]?\s*/i, '').trim()
    }
  }
  return ''
}

function extractBudgetFromAds(adsOutput: string): Record<string, string> {
  if (!adsOutput) return {}
  const result: Record<string, string> = {}
  const lines = adsOutput.toLowerCase().split('\n')
  const platformMap: Record<string, string[]> = {
    facebook:  ['facebook', 'meta', 'fb'],
    instagram: ['instagram', 'ig', 'insta'],
    twitter:   ['twitter', 'x.com', 'tweet'],
    google:    ['google', 'google ads', 'adwords', 'search'],
    tiktok:    ['tiktok', 'tik tok'],
  }
  for (const line of lines) {
    if (!line.includes('r$') && !line.includes('budget') && !line.includes('orçamento') && !line.includes('verba')) continue
    for (const [platform, keywords] of Object.entries(platformMap)) {
      if (keywords.some(k => line.includes(k))) {
        const match = line.match(/r\$\s*[\d.,]+(?:\s*[-–]\s*r\$\s*[\d.,]+)?/i)
        if (match) {
          result[platform] = match[0].replace(/r\$/gi, 'R$').trim()
          break
        }
      }
    }
  }
  return result
}

export function PublishPanel({ publisherOutput, copyOutput, socialOutput, designOutput, adsOutput, userBudget, savedCreds, authHeaders }: Props) {
  const [open, setOpen]             = useState(true)
  const [budgetConfirmed, setBudget] = useState(false)
  const [creds, setCreds]           = useState<Credentials>(EMPTY)

  // Pre-fill credentials from server when savedCreds loads
  useEffect(() => {
    const merged: Partial<Credentials> = {}
    Object.values(savedCreds).forEach(platformCreds => {
      Object.assign(merged, platformCreds)
    })
    if (Object.keys(merged).length > 0) {
      setCreds(prev => ({ ...prev, ...merged }))
    }
  }, [savedCreds])
  const [selected, setSelected]         = useState<Set<string>>(new Set())
  const [publishing, setPublishing]     = useState(false)
  const [results, setResults]           = useState<PublishResult[]>([])
  const [validationErrors, setValidationErrors] = useState<string[]>([])
  const [customText, setCustomText]     = useState<string | null>(null)
  const [showGuide, setShowGuide]       = useState<string | null>(null)
  const [customBudgets, setCustomBudgets] = useState<Record<string, string>>(() => {
    // Pre-fill all platform budgets with the form value
    if (!userBudget) return {}
    return Object.fromEntries(PLATFORMS.map(p => [p.id, userBudget]))
  })
  const [publishedPosts, setPublishedPosts] = useState<{
    platform: string; post_id: string; token: string; bearer_token?: string; url?: string
  }[]>([])

  // Derived: show customText if user typed something, else fall back to agent output
  // Use || (not ??) so empty strings "" also fall through to the next option
  const agentText = publisherOutput || copyOutput || socialOutput || ''
  const agentSource = publisherOutput ? 'Publicador' : copyOutput ? 'Copy' : socialOutput ? 'Social' : null
  const text = customText !== null ? customText : agentText

  const imagePrompt  = extractImagePrompt(designOutput)
  const budgetFromAI = extractBudgetFromAds(adsOutput)

  const toggle = (id: string) => setSelected(s => {
    const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n
  })

  const saveCreds = (patch: Partial<Credentials>) => {
    const updated = { ...creds, ...patch }
    setCreds(updated)
    localStorage.setItem('mkt_creds', JSON.stringify(updated))
  }

  const validateCredentials = (): string[] => {
    const errors: string[] = []
    if (selected.has('facebook')) {
      if (!creds.fb_page_id) errors.push('Facebook: Page ID não preenchido')
      if (!creds.fb_token)   errors.push('Facebook: Access Token não preenchido')
    }
    if (selected.has('instagram')) {
      if (!creds.ig_user_id) errors.push('Instagram: IG Business User ID não preenchido')
      if (!creds.ig_token)   errors.push('Instagram: Access Token não preenchido')
      if (!creds.image_url)  errors.push('Instagram: URL da imagem é obrigatória')
    }
    if (selected.has('twitter')) {
      if (!creds.tw_api_key)       errors.push('Twitter/X: API Key não preenchida')
      if (!creds.tw_api_secret)    errors.push('Twitter/X: API Secret não preenchido')
      if (!creds.tw_access_token)  errors.push('Twitter/X: Access Token não preenchido')
      if (!creds.tw_access_secret) errors.push('Twitter/X: Access Secret não preenchido')
    }
    if (selected.has('google')) {
      if (!creds.google_developer_token) errors.push('Google Ads: Developer Token não preenchido')
      if (!creds.google_customer_id)     errors.push('Google Ads: Customer ID não preenchido')
      if (!creds.google_refresh_token)   errors.push('Google Ads: Refresh Token não preenchido (conecte via OAuth)')
      if (!creds.google_final_url)       errors.push('Google Ads: URL de destino do anúncio não preenchida')
    }
    if (selected.has('tiktok')) {
      if (!creds.tiktok_access_token)  errors.push('TikTok: Access Token não preenchido')
      if (!creds.tiktok_advertiser_id) errors.push('TikTok: Advertiser ID não preenchido')
    }
    if (selected.has('webhook')) {
      if (!creds.webhook_url) errors.push('Webhook: URL não preenchida')
    }
    return errors
  }

  const handlePublish = async () => {
    const errors = validateCredentials()
    if (errors.length > 0) {
      setValidationErrors(errors)
      return
    }
    setValidationErrors([])
    if (selected.size === 0 || !budgetConfirmed) return
    setPublishing(true)
    setResults([])
    setPublishedPosts([])
    try {
      const body = {
        text: text || publisherOutput || copyOutput,
        image_url: creds.image_url || undefined,
        platforms: [...selected],
        fb_page_id: creds.fb_page_id, fb_token: creds.fb_token,
        ig_user_id: creds.ig_user_id, ig_token: creds.ig_token,
        tw_api_key: creds.tw_api_key, tw_api_secret: creds.tw_api_secret,
        tw_access_token: creds.tw_access_token, tw_access_secret: creds.tw_access_secret,
        webhook_url: creds.webhook_url,
        google_developer_token: creds.google_developer_token,
        google_customer_id: creds.google_customer_id,
        google_refresh_token: creds.google_refresh_token,
        google_mcc_id: creds.google_mcc_id,
        google_final_url: creds.google_final_url,
        google_budget: customBudgets['google'] || userBudget || '20',
        tiktok_access_token: creds.tiktok_access_token,
        tiktok_advertiser_id: creds.tiktok_advertiser_id,
      }
      const res = await fetch('/agency/publish', {
        method: 'POST', headers: authHeaders, body: JSON.stringify(body),
      })
      const data = await res.json()
      const apiResults: PublishResult[] = data.results || []
      setResults(apiResults)

      // Build posts list for metrics tracking (successful posts with IDs)
      const postsForMetrics = apiResults
        .filter(r => r.success && r.post_id)
        .map(r => ({
          platform: r.platform,
          post_id: r.post_id!,
          token: r.platform === 'facebook' ? creds.fb_token
               : r.platform === 'instagram' ? creds.ig_token
               : r.platform === 'twitter'   ? creds.tw_api_key
               : '',
          bearer_token: r.platform === 'twitter' ? creds.tw_api_key : undefined,
          url: r.url,
        }))
      if (postsForMetrics.length > 0) setPublishedPosts(postsForMetrics)
    } catch (e) {
      setResults([{ platform: 'erro', success: false, error: String(e) }])
    }
    setPublishing(false)
  }

  const [scheduleAt, setScheduleAt]     = useState('')
  const [scheduling, setScheduling]     = useState(false)
  const [scheduleResult, setScheduleResult] = useState<string | null>(null)

  const handleSchedule = async () => {
    const errors = validateCredentials()
    if (errors.length > 0) { setValidationErrors(errors); return }
    if (!scheduleAt) return
    setValidationErrors([])
    setScheduling(true)
    setScheduleResult(null)
    try {
      const body = {
        text: text || publisherOutput || copyOutput,
        image_url: creds.image_url || undefined,
        platforms: [...selected].filter(p => p !== 'google'),
        scheduled_at: scheduleAt,
        fb_page_id: creds.fb_page_id, fb_token: creds.fb_token,
        ig_user_id: creds.ig_user_id, ig_token: creds.ig_token,
        tw_api_key: creds.tw_api_key, tw_api_secret: creds.tw_api_secret,
        tw_access_token: creds.tw_access_token, tw_access_secret: creds.tw_access_secret,
        webhook_url: creds.webhook_url,
      }
      const res = await fetch('/schedule', { method: 'POST', headers: authHeaders, body: JSON.stringify(body) })
      const data = await res.json()
      if (res.ok) {
        setScheduleResult(`✓ Agendado para ${new Date(scheduleAt).toLocaleString('pt-BR')}`)
        setScheduleAt('')
      } else {
        setScheduleResult(`✗ ${data.detail || 'Erro ao agendar'}`)
      }
    } catch (e) {
      setScheduleResult(`✗ ${String(e)}`)
    }
    setScheduling(false)
  }

  const selectedPlatformsWithBudget = [...selected].filter(p => p in BUDGET_DEFAULTS)

  return (
    <div className="bg-gray-900/60 border border-gray-800 rounded-xl overflow-hidden">
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center justify-between px-5 py-4 hover:bg-gray-800/40 transition-colors"
      >
        <div className="flex items-center gap-2">
          <span className="text-lg">↑</span>
          <span className="text-sm font-bold text-gray-200 tracking-wide">PUBLICAR NAS REDES</span>
          <span className="text-[10px] text-emerald-500 bg-emerald-900/20 border border-emerald-800 px-2 py-0.5 rounded-full">
            pronto para publicar
          </span>
        </div>
        <span className="text-gray-500 text-sm">{open ? '▲' : '▼'}</span>
      </button>

      {open && (
        <div className="px-5 pb-5 space-y-5 border-t border-gray-800">

          {/* ── Texto ── */}
          <div className="mt-4">
            <div className="flex items-center justify-between mb-1">
              <label className="block text-[10px] text-gray-500 tracking-widest uppercase">Texto para publicar</label>
              {agentSource
                ? <span className="text-[9px] text-emerald-500">✓ Agente {agentSource} · {agentText.length} chars</span>
                : <span className="text-[9px] text-amber-500">⚠ aguardando agência…</span>
              }
            </div>
            <textarea
              className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-xs text-gray-200
                         placeholder-gray-600 focus:outline-none focus:border-violet-500 resize-none"
              style={{ height: '10cm' }}
              placeholder="Texto gerado pela agência aparece aqui automaticamente..."
              value={text}
              onChange={e => setCustomText(e.target.value)}
            />
            {customText !== null && (
              <button onClick={() => setCustomText(null)}
                className="mt-1 text-[10px] text-gray-600 hover:text-gray-400">
                ↺ restaurar texto da agência
              </button>
            )}
          </div>

          {/* ── Imagem ── */}
          <div>
            <label className="block text-[10px] text-gray-500 mb-1 tracking-widest uppercase">
              URL da Imagem <span className="text-pink-500">· obrigatório para Instagram</span>
            </label>
            <input
              className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-xs text-gray-200
                         placeholder-gray-600 focus:outline-none focus:border-violet-500"
              placeholder="https://meusite.com/imagem.jpg"
              value={creds.image_url}
              onChange={e => saveCreds({ image_url: e.target.value })}
            />
            {imagePrompt && (
              <div className="mt-2 bg-gray-800/60 border border-gray-700 rounded-lg p-2.5">
                <p className="text-[9px] text-gray-500 uppercase tracking-widest mb-1">Prompt IA gerado pelo Agente Design</p>
                <p className="text-[11px] text-gray-300 leading-relaxed">{imagePrompt}</p>
                <p className="text-[9px] text-gray-600 mt-1.5">
                  Use em{' '}
                  <a href="https://www.midjourney.com" target="_blank" rel="noopener noreferrer" className="text-violet-400 hover:underline">Midjourney</a>
                  {', '}
                  <a href="https://openai.com/dall-e-3" target="_blank" rel="noopener noreferrer" className="text-violet-400 hover:underline">DALL-E</a>
                  {' ou '}
                  <a href="https://stability.ai" target="_blank" rel="noopener noreferrer" className="text-violet-400 hover:underline">Stable Diffusion</a>
                  {' → cole a URL acima'}
                </p>
              </div>
            )}
          </div>

          {/* ── Aviso credenciais ── */}
          {Object.keys(savedCreds).length === 0 && (
            <div className="flex items-center gap-2 bg-gray-800/40 border border-gray-700 rounded-lg px-3 py-2">
              <span className="text-amber-500 text-xs">⚠</span>
              <p className="text-[11px] text-gray-400">
                Nenhuma credencial salva. Acesse{' '}
                <span className="text-violet-400 font-semibold">credenciais</span>{' '}
                no menu para salvar suas chaves de plataforma.
              </p>
            </div>
          )}

          {/* ── Plataformas ── */}
          <div>
            <p className="text-[10px] text-gray-500 mb-2 tracking-widest uppercase">Plataformas</p>
            <div className="grid grid-cols-3 gap-2">
              {PLATFORMS.map(p => (
                <button key={p.id} onClick={() => toggle(p.id)}
                  className={`flex items-center gap-2 px-3 py-2 rounded-lg border text-xs font-medium transition-all
                    ${selected.has(p.id)
                      ? 'border-violet-500 bg-violet-900/30 text-white'
                      : 'border-gray-700 bg-gray-800/50 text-gray-400 hover:border-gray-600'}`}>
                  <span className={p.color}>{p.icon}</span>
                  {p.label}
                  {selected.has(p.id) && <span className="ml-auto text-[8px] text-violet-400">✓</span>}
                </button>
              ))}
            </div>
          </div>

          {/* ── Credenciais por plataforma ── */}
          {selected.has('facebook') && (
            <CredentialSection title="Facebook" platformId="facebook" showGuide={showGuide} onToggleGuide={setShowGuide}>
              <Field label="Page ID"      value={creds.fb_page_id} onChange={v => saveCreds({ fb_page_id: v })} />
              <Field label="Access Token" value={creds.fb_token}   onChange={v => saveCreds({ fb_token: v })} secret />
            </CredentialSection>
          )}
          {selected.has('instagram') && (
            <CredentialSection title="Instagram" platformId="instagram" showGuide={showGuide} onToggleGuide={setShowGuide}>
              <Field label="IG Business User ID" value={creds.ig_user_id} onChange={v => saveCreds({ ig_user_id: v })} />
              <Field label="Access Token (Page)" value={creds.ig_token}   onChange={v => saveCreds({ ig_token: v })} secret />
              <p className="text-[9px] text-yellow-600 mt-1">⚠ Requer URL pública de imagem (campo acima)</p>
            </CredentialSection>
          )}
          {selected.has('twitter') && (
            <CredentialSection title="Twitter/X" platformId="twitter" showGuide={showGuide} onToggleGuide={setShowGuide}>
              <Field label="API Key"       value={creds.tw_api_key}       onChange={v => saveCreds({ tw_api_key: v })} />
              <Field label="API Secret"    value={creds.tw_api_secret}    onChange={v => saveCreds({ tw_api_secret: v })} secret />
              <Field label="Access Token"  value={creds.tw_access_token}  onChange={v => saveCreds({ tw_access_token: v })} secret />
              <Field label="Access Secret" value={creds.tw_access_secret} onChange={v => saveCreds({ tw_access_secret: v })} secret />
            </CredentialSection>
          )}
          {selected.has('google') && (
            <CredentialSection title="Google Ads" platformId="google" showGuide={showGuide} onToggleGuide={setShowGuide}>
              <Field label="Developer Token" value={creds.google_developer_token} onChange={v => saveCreds({ google_developer_token: v })} secret />
              <Field label="Customer ID (conta de anúncios, sem hífens)" value={creds.google_customer_id} onChange={v => saveCreds({ google_customer_id: v })} />
              <Field label="ID da Conta MCC / Gerenciadora (sem hífens, se aplicável)" value={creds.google_mcc_id} onChange={v => saveCreds({ google_mcc_id: v })} />
              <Field label="Refresh Token"   value={creds.google_refresh_token}    onChange={v => saveCreds({ google_refresh_token: v })} secret />
              <Field label="URL de destino do anúncio (site do produto)" value={creds.google_final_url} onChange={v => saveCreds({ google_final_url: v })} />
              <p className="text-[9px] text-amber-600 mt-1">⚠ Campanha criada em status PAUSADA — ative manualmente no Google Ads após revisar.</p>
            </CredentialSection>
          )}
          {selected.has('tiktok') && (
            <CredentialSection title="TikTok" platformId="tiktok" showGuide={showGuide} onToggleGuide={setShowGuide}>
              <Field label="Access Token"  value={creds.tiktok_access_token}  onChange={v => saveCreds({ tiktok_access_token: v })} secret />
              <Field label="Advertiser ID" value={creds.tiktok_advertiser_id} onChange={v => saveCreds({ tiktok_advertiser_id: v })} />
            </CredentialSection>
          )}
          {selected.has('webhook') && (
            <CredentialSection title="Webhook" platformId="webhook" showGuide={showGuide} onToggleGuide={setShowGuide}>
              <Field label="URL" value={creds.webhook_url} onChange={v => saveCreds({ webhook_url: v })} />
              <p className="text-[9px] text-gray-600 mt-1">Envia POST JSON com {`{text, image_url}`} para qualquer endpoint</p>
            </CredentialSection>
          )}

          {/* ── Resumo de Orçamento ── */}
          {selectedPlatformsWithBudget.length > 0 && (
            <div className="bg-amber-950/30 border border-amber-800/50 rounded-xl p-4 space-y-3">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <span className="text-amber-400 text-base">💰</span>
                  <p className="text-xs font-bold text-amber-300 tracking-wide uppercase">
                    Orçamento por Plataforma
                  </p>
                </div>
                {userBudget && (
                  <span className="text-[10px] text-amber-500 bg-amber-900/30 border border-amber-800/50 px-2 py-0.5 rounded-full">
                    Total disponível: {userBudget}
                  </span>
                )}
              </div>
              <div className="grid grid-cols-2 gap-2">
                {selectedPlatformsWithBudget.map(pid => {
                  const def   = BUDGET_DEFAULTS[pid]
                  const ai    = budgetFromAI[pid]
                  const label = PLATFORMS.find(p => p.id === pid)?.label || pid
                  const suggestion = ai || `R$ ${def.min}–${def.max}`
                  const customVal = customBudgets[pid] ?? ''
                  return (
                    <div key={pid} className="bg-gray-900/60 border border-gray-800 rounded-lg px-3 py-2.5 space-y-1.5">
                      <p className="text-[10px] text-gray-500 uppercase tracking-widest">{label}</p>
                      <input
                        className="w-full bg-gray-800 border border-gray-700 rounded-md px-2 py-1 text-xs text-amber-300
                                   font-semibold placeholder-gray-600 focus:outline-none focus:border-amber-600"
                        placeholder={suggestion}
                        value={customVal}
                        onChange={e => setCustomBudgets(b => ({ ...b, [pid]: e.target.value }))}
                      />
                      <p className="text-[9px] text-gray-600">
                        {customVal === userBudget && userBudget ? 'do formulário · edite se quiser'
                          : customVal && customVal !== userBudget ? 'valor personalizado'
                          : ai ? 'sugerido pelo Agente Ads'
                          : 'estimativa de mercado'}
                      </p>
                      {ai && (
                        <p className="text-[9px] text-blue-500">
                          Agente sugere: {ai}
                          {ai !== customVal && (
                            <button
                              onClick={() => setCustomBudgets(b => ({ ...b, [pid]: ai }))}
                              className="ml-2 underline hover:text-blue-400">usar</button>
                          )}
                        </p>
                      )}
                      {/* URL de destino exclusivo para Google Ads */}
                      {pid === 'google' && (
                        <div className="pt-1 border-t border-gray-700/50 mt-1">
                          <label className="block text-[9px] text-gray-500 mb-1 uppercase tracking-wider">
                            URL de destino do anúncio <span className="text-red-500">*</span>
                          </label>
                          <input
                            type="text"
                            className="w-full bg-gray-900 border border-gray-700 rounded-md px-2.5 py-1.5
                                       text-[11px] text-gray-200 placeholder-gray-600
                                       focus:outline-none focus:border-violet-500"
                            placeholder="https://seusite.com.br/produto"
                            value={creds.google_final_url}
                            onChange={e => saveCreds({ google_final_url: e.target.value })}
                          />
                          <p className="text-[9px] text-gray-600 mt-0.5">Site para onde o anúncio vai direcionar</p>
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
              <div className="flex items-start gap-2 mt-1">
                <input
                  type="checkbox"
                  id="budget-confirm"
                  checked={budgetConfirmed}
                  onChange={e => setBudget(e.target.checked)}
                  className="mt-0.5 accent-violet-500"
                />
                <label htmlFor="budget-confirm" className="text-[11px] text-gray-400 cursor-pointer leading-relaxed">
                  Confirmo que estou ciente do orçamento acima e autorizo a publicação da campanha.
                </label>
              </div>
            </div>
          )}

          {/* ── Agendar ── */}
          <div className="bg-gray-800/40 border border-gray-700 rounded-xl p-3 space-y-2">
            <p className="text-[10px] text-gray-400 font-bold tracking-widest uppercase">📅 Agendar para depois</p>
            <p className="text-[9px] text-gray-600">Publica automaticamente na data e hora escolhidas. Google Ads não suporta agendamento.</p>
            <div className="flex gap-2">
              <input
                type="datetime-local"
                value={scheduleAt}
                onChange={e => setScheduleAt(e.target.value)}
                className="flex-1 bg-gray-900 border border-gray-700 rounded-lg px-3 py-1.5 text-xs text-gray-200
                           focus:outline-none focus:border-violet-500"
              />
              <button
                onClick={handleSchedule}
                disabled={scheduling || !scheduleAt || selected.size === 0 || !text}
                className="px-4 py-1.5 rounded-lg text-xs font-semibold text-white
                  bg-gradient-to-r from-blue-700 to-violet-700 hover:from-blue-600 hover:to-violet-600
                  disabled:opacity-40 disabled:cursor-not-allowed transition-all">
                {scheduling ? '...' : 'Agendar'}
              </button>
            </div>
            {scheduleResult && (
              <p className={`text-[11px] ${scheduleResult.startsWith('✓') ? 'text-emerald-400' : 'text-red-400'}`}>
                {scheduleResult}
              </p>
            )}
          </div>

          {/* ── Publicar ── */}
          <button
            onClick={handlePublish}
            disabled={publishing || selected.size === 0 || !text || (selectedPlatformsWithBudget.length > 0 && !budgetConfirmed)}
            className="w-full py-2.5 rounded-xl text-sm font-semibold transition-all
              bg-gradient-to-r from-emerald-600 to-teal-600 hover:from-emerald-500 hover:to-teal-500
              disabled:from-gray-800 disabled:to-gray-800 disabled:text-gray-600 disabled:cursor-not-allowed
              text-white shadow-lg shadow-emerald-500/20"
          >
            {publishing ? (
              <span className="flex items-center justify-center gap-2">
                <span className="w-3.5 h-3.5 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                Publicando...
              </span>
            ) : selected.size === 0 ? 'Selecione ao menos uma plataforma'
              : !text ? 'Aguardando texto da agência...'
              : (selectedPlatformsWithBudget.length > 0 && !budgetConfirmed) ? '⚠ Confirme o orçamento para publicar'
              : `↑ Publicar em ${selected.size} plataforma${selected.size !== 1 ? 's' : ''}`}
          </button>

          {/* ── Erros de validação ── */}
          {validationErrors.length > 0 && (
            <div className="bg-red-950/30 border border-red-800/60 rounded-xl p-4 space-y-1.5">
              <p className="text-xs font-bold text-red-400 flex items-center gap-1.5">
                <span>🚨</span> Corrija as credenciais antes de publicar:
              </p>
              {validationErrors.map((e, i) => (
                <p key={i} className="text-[11px] text-red-300 flex items-center gap-1.5">
                  <span className="text-red-600">▸</span> {e}
                </p>
              ))}
              <p className="text-[10px] text-gray-600 pt-1">
                Acesse <span className="text-violet-400">credenciais</span> no menu para preencher.
              </p>
            </div>
          )}

          {/* ── Resultados de publicação ── */}
          {results.length > 0 && (
            <div className="space-y-2">
              {results.map((r, i) => (
                <div key={i} className={`flex items-center justify-between px-3 py-2 rounded-lg text-xs
                  ${r.success ? 'bg-emerald-900/20 border border-emerald-800' : 'bg-red-900/20 border border-red-800'}`}>
                  <div className="flex items-center gap-2">
                    <span className={r.success ? 'text-emerald-400' : 'text-red-400'}>
                      {r.success ? '✓' : '✗'}
                    </span>
                    <span className="capitalize font-medium">{r.platform}</span>
                    {!r.success && (
                      <span className="text-red-400 truncate max-w-64">{r.error}</span>
                    )}
                  </div>
                  {r.success && r.url && (
                    <a href={r.url} target="_blank" rel="noopener noreferrer"
                      className="text-emerald-400 hover:underline">ver post ↗</a>
                  )}
                </div>
              ))}
            </div>
          )}

          {/* ── Métricas em tempo real ── */}
          {publishedPosts.length > 0 && (
            <MetricsPanel posts={publishedPosts} authHeaders={authHeaders} />
          )}
        </div>
      )}
    </div>
  )
}

function CredentialSection({ title, platformId, showGuide, onToggleGuide, children }: {
  title: string; platformId: string; showGuide: string | null
  onToggleGuide: (id: string | null) => void; children: React.ReactNode
}) {
  const guide  = CRED_GUIDES[platformId]
  const isOpen = showGuide === platformId
  return (
    <div className="bg-gray-800/40 border border-gray-700 rounded-lg p-3 space-y-2">
      <div className="flex items-center justify-between">
        <p className="text-[10px] text-gray-400 font-bold tracking-widest uppercase">{title} — Credenciais</p>
        {guide && (
          <button onClick={() => onToggleGuide(isOpen ? null : platformId)}
            className="text-[9px] text-violet-400 hover:text-violet-300 transition-colors">
            {isOpen ? '▲ fechar' : '? como obter'}
          </button>
        )}
      </div>
      {guide && isOpen && (
        <div className="bg-gray-900/60 border border-gray-700 rounded-md p-2.5 space-y-1">
          {guide.steps.map((s, i) => (
            <p key={i} className="text-[10px] text-gray-400 leading-relaxed">{s}</p>
          ))}
          <a href={guide.link} target="_blank" rel="noopener noreferrer"
            className="inline-block mt-1 text-[9px] text-violet-400 hover:underline">
            Abrir painel developer ↗
          </a>
        </div>
      )}
      {children}
    </div>
  )
}

function Field({ label, value, onChange, secret }: {
  label: string; value: string; onChange: (v: string) => void; secret?: boolean
}) {
  return (
    <div>
      <label className="block text-[9px] text-gray-600 mb-0.5 uppercase tracking-wider">{label}</label>
      <input
        type={secret ? 'password' : 'text'}
        className="w-full bg-gray-900 border border-gray-700 rounded-md px-2.5 py-1.5 text-[11px] text-gray-200
                   placeholder-gray-700 focus:outline-none focus:border-violet-500"
        value={value}
        onChange={e => onChange(e.target.value)}
        placeholder={secret ? '••••••••' : label}
      />
    </div>
  )
}
