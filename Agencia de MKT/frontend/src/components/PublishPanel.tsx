import { useState, useEffect } from 'react'

interface Credentials {
  fb_page_id: string
  fb_token: string
  ig_user_id: string
  ig_token: string
  tw_api_key: string
  tw_api_secret: string
  tw_access_token: string
  tw_access_secret: string
  webhook_url: string
  image_url: string
  google_developer_token: string
  google_customer_id: string
  google_refresh_token: string
  tiktok_access_token: string
  tiktok_advertiser_id: string
}

interface PublishResult {
  platform: string
  success: boolean
  url?: string
  error?: string
}

interface Props {
  publisherOutput: string
  copyOutput: string
  designOutput: string
  authHeaders: Record<string, string>
}

const PLATFORMS = [
  { id: 'facebook',  label: 'Facebook',    icon: '𝕗', color: 'text-blue-400'   },
  { id: 'instagram', label: 'Instagram',   icon: '◉',  color: 'text-pink-400'   },
  { id: 'twitter',   label: 'Twitter/X',   icon: '✕',  color: 'text-sky-400'    },
  { id: 'google',    label: 'Google Ads',  icon: 'G',  color: 'text-yellow-400' },
  { id: 'tiktok',    label: 'TikTok',      icon: '♪',  color: 'text-rose-400'   },
  { id: 'webhook',   label: 'Webhook',     icon: '⚡',  color: 'text-violet-400' },
]

const CRED_GUIDES: Record<string, { steps: string[]; link: string }> = {
  facebook: {
    steps: [
      '1. Acesse developers.facebook.com → Meus Apps → Criar App',
      '2. Adicione o produto "Páginas"',
      '3. Em Ferramentas → Graph API Explorer → gere token com pages_manage_posts',
      '4. Copie seu Page ID em: facebook.com/[nome-da-pagina]/about',
    ],
    link: 'https://developers.facebook.com/apps',
  },
  instagram: {
    steps: [
      '1. Conta deve ser Business ou Creator no Instagram',
      '2. Conecte ao Facebook Page em Configurações → Conta → Página vinculada',
      '3. No Graph API Explorer: GET /me/accounts → pegue o access_token da página',
      '4. GET /{page-id}?fields=instagram_business_account → pegue o ig_user_id',
      '5. Imagem: precisa ser URL pública acessível (CDN, S3, Cloudinary etc.)',
    ],
    link: 'https://developers.facebook.com/docs/instagram-api/getting-started',
  },
  twitter: {
    steps: [
      '1. Acesse developer.twitter.com → Projects → Create App',
      '2. Em "Keys and Tokens": copie API Key + API Secret',
      '3. Gere Access Token + Access Token Secret (permissão Read+Write)',
    ],
    link: 'https://developer.twitter.com/en/portal/dashboard',
  },
  google: {
    steps: [
      '1. Acesse ads.google.com → Ferramentas → Centro da API',
      '2. Solicite Developer Token (aprovação pode levar dias)',
      '3. Google Cloud Console → OAuth2 → gere Refresh Token com escopo adwords',
      '4. Customer ID: número de 10 dígitos no topo da conta Google Ads',
    ],
    link: 'https://developers.google.com/google-ads/api/docs/get-started/introduction',
  },
  tiktok: {
    steps: [
      '1. Acesse business.tiktok.com → Developer → Create App',
      '2. Solicite permissão de "Ad Management"',
      '3. Gere Access Token com escopo de advertising',
      '4. Advertiser ID: encontrado em ads.tiktok.com na URL do painel',
    ],
    link: 'https://ads.tiktok.com/marketing_api/docs',
  },
}

const EMPTY: Credentials = {
  fb_page_id: '', fb_token: '', ig_user_id: '', ig_token: '',
  tw_api_key: '', tw_api_secret: '', tw_access_token: '', tw_access_secret: '',
  webhook_url: '', image_url: '',
  google_developer_token: '', google_customer_id: '', google_refresh_token: '',
  tiktok_access_token: '', tiktok_advertiser_id: '',
}

/** Extract the AI image prompt from the DESIGN agent output */
function extractImagePrompt(designOutput: string): string {
  if (!designOutput) return ''
  // Look for lines containing PROMPT IA or PROMPT IMAGEM
  const lines = designOutput.split('\n')
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].toUpperCase()
    if (line.includes('PROMPT IA') || line.includes('PROMPT IMAGEM') || line.includes('PROMPT FEED')) {
      // Return this line + next 2 lines as the prompt
      return lines.slice(i, i + 3).join(' ').replace(/PROMPT IA [A-ZÁÉÍÓÚ]*\s*[:|]?\s*/i, '').trim()
    }
  }
  return ''
}

export function PublishPanel({ publisherOutput, copyOutput, designOutput, authHeaders }: Props) {
  const [open, setOpen]             = useState(true)   // auto-open when panel mounts
  const [creds, setCreds]           = useState<Credentials>(() => {
    try { return { ...EMPTY, ...JSON.parse(localStorage.getItem('mkt_creds') || '{}') } }
    catch { return EMPTY }
  })
  const [selected, setSelected]     = useState<Set<string>>(new Set())
  const [publishing, setPublishing] = useState(false)
  const [results, setResults]       = useState<PublishResult[]>([])
  const [text, setText]             = useState('')
  const [showGuide, setShowGuide]   = useState<string | null>(null)

  // Auto-fill text when agency output arrives
  useEffect(() => {
    const output = publisherOutput || copyOutput
    if (output && !text) setText(output)
  }, [publisherOutput, copyOutput])

  // Auto-fill image prompt from design output
  const imagePrompt = extractImagePrompt(designOutput)

  const toggle = (id: string) => setSelected(s => {
    const n = new Set(s)
    n.has(id) ? n.delete(id) : n.add(id)
    return n
  })

  const saveCreds = (patch: Partial<Credentials>) => {
    const updated = { ...creds, ...patch }
    setCreds(updated)
    localStorage.setItem('mkt_creds', JSON.stringify(updated))
  }

  const handlePublish = async () => {
    if (selected.size === 0) return
    setPublishing(true)
    setResults([])
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
        tiktok_access_token: creds.tiktok_access_token,
        tiktok_advertiser_id: creds.tiktok_advertiser_id,
      }
      const res = await fetch('/agency/publish', {
        method: 'POST', headers: authHeaders, body: JSON.stringify(body),
      })
      const data = await res.json()
      setResults(data.results || [])
    } catch (e) {
      setResults([{ platform: 'erro', success: false, error: String(e) }])
    }
    setPublishing(false)
  }

  return (
    <div className="bg-gray-900/60 border border-gray-800 rounded-xl overflow-hidden">
      {/* Header */}
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center justify-between px-5 py-4 hover:bg-gray-800/40 transition-colors"
      >
        <div className="flex items-center gap-2">
          <span className="text-lg">↑</span>
          <span className="text-sm font-bold text-gray-200 tracking-wide">PUBLICAR NAS REDES</span>
          <span className="text-[10px] text-emerald-500 bg-emerald-900/20 border border-emerald-800 px-2 py-0.5 rounded-full">pronto para publicar</span>
        </div>
        <span className="text-gray-500 text-sm">{open ? '▲' : '▼'}</span>
      </button>

      {open && (
        <div className="px-5 pb-5 space-y-4 border-t border-gray-800">
          {/* Text to publish */}
          <div className="mt-4">
            <label className="block text-[10px] text-gray-500 mb-1 tracking-widest uppercase">Texto para publicar</label>
            <textarea
              className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-xs text-gray-200
                         placeholder-gray-600 focus:outline-none focus:border-violet-500 resize-none"
              style={{ height: '10cm' }}
              placeholder="Texto gerado pela agência aparece aqui automaticamente..."
              value={text}
              onChange={e => setText(e.target.value)}
            />
            {!text && (publisherOutput || copyOutput) && (
              <button
                onClick={() => setText(publisherOutput || copyOutput)}
                className="mt-1 text-[10px] text-violet-400 hover:text-violet-300"
              >
                ← usar texto da agência
              </button>
            )}
            {text && (
              <button
                onClick={() => setText(publisherOutput || copyOutput || '')}
                className="mt-1 text-[10px] text-gray-600 hover:text-gray-400"
              >
                ↺ restaurar texto da agência
              </button>
            )}
          </div>

          {/* Image URL (for Instagram) */}
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
                  {' → cole a URL da imagem gerada acima'}
                </p>
              </div>
            )}
          </div>

          {/* Platform selection */}
          <div>
            <p className="text-[10px] text-gray-500 mb-2 tracking-widest uppercase">Plataformas</p>
            <div className="grid grid-cols-3 gap-2">
              {PLATFORMS.map(p => (
                <button
                  key={p.id}
                  onClick={() => toggle(p.id)}
                  className={`flex items-center gap-2 px-3 py-2 rounded-lg border text-xs font-medium transition-all
                    ${selected.has(p.id)
                      ? 'border-violet-500 bg-violet-900/30 text-white'
                      : 'border-gray-700 bg-gray-800/50 text-gray-400 hover:border-gray-600'}`}
                >
                  <span className={p.color}>{p.icon}</span>
                  {p.label}
                  {selected.has(p.id) && <span className="ml-auto text-[8px] text-violet-400">✓</span>}
                </button>
              ))}
            </div>
          </div>

          {/* Credentials accordion per platform */}
          {selected.has('facebook') && (
            <CredentialSection title="Facebook" platformId="facebook"
              showGuide={showGuide} onToggleGuide={setShowGuide}>
              <Field label="Page ID" value={creds.fb_page_id} onChange={v => saveCreds({ fb_page_id: v })} />
              <Field label="Access Token" value={creds.fb_token} onChange={v => saveCreds({ fb_token: v })} secret />
            </CredentialSection>
          )}

          {selected.has('instagram') && (
            <CredentialSection title="Instagram" platformId="instagram"
              showGuide={showGuide} onToggleGuide={setShowGuide}>
              <Field label="IG Business User ID" value={creds.ig_user_id} onChange={v => saveCreds({ ig_user_id: v })} />
              <Field label="Access Token (Page)" value={creds.ig_token} onChange={v => saveCreds({ ig_token: v })} secret />
              <p className="text-[9px] text-yellow-600 mt-1">⚠ Instagram só aceita imagens via URL pública (campo acima)</p>
            </CredentialSection>
          )}

          {selected.has('twitter') && (
            <CredentialSection title="Twitter/X" platformId="twitter"
              showGuide={showGuide} onToggleGuide={setShowGuide}>
              <Field label="API Key"        value={creds.tw_api_key}       onChange={v => saveCreds({ tw_api_key: v })} />
              <Field label="API Secret"     value={creds.tw_api_secret}    onChange={v => saveCreds({ tw_api_secret: v })} secret />
              <Field label="Access Token"   value={creds.tw_access_token}  onChange={v => saveCreds({ tw_access_token: v })} secret />
              <Field label="Access Secret"  value={creds.tw_access_secret} onChange={v => saveCreds({ tw_access_secret: v })} secret />
            </CredentialSection>
          )}

          {selected.has('google') && (
            <CredentialSection title="Google Ads" platformId="google"
              showGuide={showGuide} onToggleGuide={setShowGuide}>
              <Field label="Developer Token"  value={creds.google_developer_token} onChange={v => saveCreds({ google_developer_token: v })} secret />
              <Field label="Customer ID"      value={creds.google_customer_id}      onChange={v => saveCreds({ google_customer_id: v })} />
              <Field label="Refresh Token"    value={creds.google_refresh_token}    onChange={v => saveCreds({ google_refresh_token: v })} secret />
            </CredentialSection>
          )}

          {selected.has('tiktok') && (
            <CredentialSection title="TikTok" platformId="tiktok"
              showGuide={showGuide} onToggleGuide={setShowGuide}>
              <Field label="Access Token"    value={creds.tiktok_access_token}  onChange={v => saveCreds({ tiktok_access_token: v })} secret />
              <Field label="Advertiser ID"   value={creds.tiktok_advertiser_id} onChange={v => saveCreds({ tiktok_advertiser_id: v })} />
            </CredentialSection>
          )}

          {selected.has('webhook') && (
            <CredentialSection title="Webhook" platformId="webhook"
              showGuide={showGuide} onToggleGuide={setShowGuide}>
              <Field label="URL" value={creds.webhook_url} onChange={v => saveCreds({ webhook_url: v })} />
              <p className="text-[9px] text-gray-600 mt-1">Envia POST JSON com {`{text, image_url}`} para qualquer endpoint</p>
            </CredentialSection>
          )}

          {/* Publish button */}
          <button
            onClick={handlePublish}
            disabled={publishing || selected.size === 0 || !text}
            className="w-full py-2.5 rounded-xl text-sm font-semibold transition-all
              bg-gradient-to-r from-emerald-600 to-teal-600 hover:from-emerald-500 hover:to-teal-500
              disabled:from-gray-800 disabled:to-gray-800 disabled:text-gray-600 disabled:cursor-not-allowed
              text-white shadow-lg shadow-emerald-500/20"
          >
            {publishing
              ? <span className="flex items-center justify-center gap-2">
                  <span className="w-3.5 h-3.5 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                  Publicando...
                </span>
              : selected.size === 0
                ? 'Selecione ao menos uma plataforma'
                : !text
                  ? 'Aguardando texto da agência...'
                  : `↑ Publicar em ${selected.size} plataforma${selected.size !== 1 ? 's' : ''}`}
          </button>

          {/* Results */}
          {results.length > 0 && (
            <div className="space-y-2">
              {results.map((r, i) => (
                <div key={i} className={`flex items-center justify-between px-3 py-2 rounded-lg text-xs
                  ${r.success ? 'bg-emerald-900/20 border border-emerald-800' : 'bg-red-900/20 border border-red-800'}`}>
                  <span className="capitalize font-medium">{r.platform}</span>
                  {r.success
                    ? r.url
                      ? <a href={r.url} target="_blank" rel="noopener noreferrer" className="text-emerald-400 hover:underline">ver post ↗</a>
                      : <span className="text-emerald-400">publicado ✓</span>
                    : <span className="text-red-400 truncate max-w-52">{r.error}</span>}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function CredentialSection({
  title, platformId, showGuide, onToggleGuide, children,
}: {
  title: string
  platformId: string
  showGuide: string | null
  onToggleGuide: (id: string | null) => void
  children: React.ReactNode
}) {
  const guide = CRED_GUIDES[platformId]
  const isOpen = showGuide === platformId
  return (
    <div className="bg-gray-800/40 border border-gray-700 rounded-lg p-3 space-y-2">
      <div className="flex items-center justify-between">
        <p className="text-[10px] text-gray-400 font-bold tracking-widest uppercase">{title} — Credenciais</p>
        {guide && (
          <button
            onClick={() => onToggleGuide(isOpen ? null : platformId)}
            className="text-[9px] text-violet-400 hover:text-violet-300 transition-colors"
          >
            {isOpen ? '▲ fechar guia' : '? como obter'}
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
            Abrir painel de developer ↗
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
