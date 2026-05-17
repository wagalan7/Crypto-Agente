import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../services/api'
import { useAuth } from '../context/AuthContext'

type Step = 'welcome' | 'client' | 'first-post' | 'done'

export function OnboardingPage() {
  const { user, refreshUser } = useAuth()
  const navigate = useNavigate()
  const [step, setStep] = useState<Step>('welcome')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  // Step 1: create client
  const [clientName, setClientName] = useState('')
  const [niche, setNiche] = useState('')
  const [platforms, setPlatforms] = useState<string[]>(['instagram'])
  const [createdClientId, setCreatedClientId] = useState<number | null>(null)

  // Step 2: first content
  const [siteUrl, setSiteUrl] = useState('')
  const [topic, setTopic] = useState('')
  const [generating, setGenerating] = useState(false)
  const [progress, setProgress] = useState('')

  function togglePlatform(p: string) {
    setPlatforms(prev => prev.includes(p) ? prev.filter(x => x !== p) : [...prev, p])
  }

  async function createClient() {
    setErr(''); setBusy(true)
    try {
      const c: any = await api.clients.create({
        name: clientName.trim(),
        niche: niche.trim() || null,
        platforms,
        goals: [],
      })
      setCreatedClientId(c.id)
      setStep('first-post')
    } catch (e: any) {
      try { const body = JSON.parse(e.message); setErr(body.detail || 'Erro') }
      catch { setErr(e.message || 'Erro ao criar cliente') }
    } finally { setBusy(false) }
  }

  async function generateFirst() {
    if (!createdClientId) return
    setErr(''); setGenerating(true); setProgress('Iniciando...')
    try {
      const gen = api.agents.auto(
        createdClientId,
        siteUrl.trim(),
        topic.trim() || 'apresentação da marca',
        'reels',
        platforms[0] || 'instagram',
        'autoridade',
      )
      for await (const ev of gen) {
        if (ev.type === 'status') setProgress(ev.payload)
        if (ev.type === 'done') setProgress('✓ Primeiro post gerado!')
      }
      setStep('done')
    } catch (e: any) {
      try { const body = JSON.parse(e.message); setErr(body.detail || 'Erro') }
      catch { setErr(e.message || 'Erro ao gerar') }
    } finally { setGenerating(false) }
  }

  async function finish() {
    setBusy(true)
    try {
      await api.auth.completeOnboarding()
      await refreshUser()
      if (createdClientId) navigate(`/client/${createdClientId}/content`, { replace: true })
      else navigate('/', { replace: true })
    } catch (e: any) {
      navigate('/', { replace: true })
    } finally { setBusy(false) }
  }

  async function skip() {
    setBusy(true)
    try {
      await api.auth.completeOnboarding()
      await refreshUser()
    } catch { /* ignore */ }
    navigate('/', { replace: true })
  }

  return (
    <div className="min-h-screen bg-gray-950 flex items-center justify-center px-4 py-8">
      <div className="w-full max-w-lg space-y-4">
        {/* Progress */}
        <div className="flex items-center gap-2 justify-center">
          {(['welcome', 'client', 'first-post', 'done'] as Step[]).map((s, i) => (
            <div key={s} className={`h-1.5 w-12 rounded-full ${
              (['welcome', 'client', 'first-post', 'done'].indexOf(step) >= i) ? 'bg-violet-500' : 'bg-gray-800'
            }`} />
          ))}
        </div>

        {step === 'welcome' && (
          <div className="card space-y-4 text-center">
            <h1 className="text-2xl font-bold text-white">Bem-vindo, {user?.name || 'criador'} 👋</h1>
            <p className="text-sm text-gray-400">Em 3 passos rápidos você terá seu primeiro post de autoridade pronto.</p>
            <ul className="text-xs text-gray-500 text-left space-y-1.5 max-w-xs mx-auto">
              <li>1. <span className="text-gray-300">Cadastra seu cliente (ou sua marca)</span></li>
              <li>2. <span className="text-gray-300">A IA analisa seu site e gera o 1º post</span></li>
              <li>3. <span className="text-gray-300">Você ajusta e publica</span></li>
            </ul>
            <div className="flex gap-2 pt-2">
              <button onClick={skip} className="btn-secondary text-xs flex-1">Pular</button>
              <button onClick={() => setStep('client')} className="btn-primary text-sm flex-1">Começar →</button>
            </div>
          </div>
        )}

        {step === 'client' && (
          <div className="card space-y-3">
            <div>
              <h2 className="text-lg font-bold text-white">Passo 1: seu primeiro cliente</h2>
              <p className="text-xs text-gray-500 mt-1">Pode ser sua marca pessoal ou de um cliente que você atende.</p>
            </div>
            <div>
              <label className="text-xs text-gray-400 mb-1 block">Nome da marca *</label>
              <input className="input-field" placeholder="Ex: Maria Coach"
                value={clientName} onChange={e => setClientName(e.target.value)} />
            </div>
            <div>
              <label className="text-xs text-gray-400 mb-1 block">Nicho</label>
              <input className="input-field" placeholder="Ex: Coaching de carreira"
                value={niche} onChange={e => setNiche(e.target.value)} />
            </div>
            <div>
              <label className="text-xs text-gray-400 mb-1 block">Plataformas</label>
              <div className="flex gap-2">
                {['instagram', 'facebook', 'tiktok', 'youtube'].map(p => (
                  <button key={p} type="button" onClick={() => togglePlatform(p)}
                    className={`text-xs px-3 py-1.5 rounded-lg border transition-colors ${
                      platforms.includes(p)
                        ? 'bg-violet-700/40 border-violet-600 text-violet-200'
                        : 'bg-gray-800 border-gray-700 text-gray-400'
                    }`}>{p}</button>
                ))}
              </div>
            </div>
            {err && <p className="text-xs text-red-400">{err}</p>}
            <div className="flex gap-2 pt-2">
              <button onClick={() => setStep('welcome')} className="btn-secondary text-xs">Voltar</button>
              <button onClick={createClient} disabled={!clientName.trim() || busy}
                className="btn-primary text-sm flex-1">
                {busy ? 'Criando...' : 'Próximo →'}
              </button>
            </div>
          </div>
        )}

        {step === 'first-post' && (
          <div className="card space-y-3">
            <div>
              <h2 className="text-lg font-bold text-white">Passo 2: gere seu primeiro post</h2>
              <p className="text-xs text-gray-500 mt-1">Cole o site (opcional) — a IA puxa nicho, tom e cria o post.</p>
            </div>
            <div>
              <label className="text-xs text-gray-400 mb-1 block">Site / Linktree (opcional)</label>
              <input className="input-field" placeholder="https://..."
                value={siteUrl} onChange={e => setSiteUrl(e.target.value)} />
            </div>
            <div>
              <label className="text-xs text-gray-400 mb-1 block">Tema do post</label>
              <input className="input-field" placeholder="Ex: por que pivôs de carreira dão errado"
                value={topic} onChange={e => setTopic(e.target.value)} />
            </div>
            {generating && (
              <div className="bg-violet-950/40 border border-violet-800/50 rounded p-2">
                <p className="text-xs text-violet-300">🤖 {progress}</p>
              </div>
            )}
            {err && <p className="text-xs text-red-400">{err}</p>}
            <div className="flex gap-2 pt-2">
              <button onClick={() => setStep('done')} disabled={generating}
                className="btn-secondary text-xs">Pular</button>
              <button onClick={generateFirst} disabled={generating}
                className="btn-primary text-sm flex-1">
                {generating ? 'Gerando...' : '✦ Gerar primeiro post'}
              </button>
            </div>
          </div>
        )}

        {step === 'done' && (
          <div className="card space-y-4 text-center">
            <div className="text-5xl">🎉</div>
            <h2 className="text-xl font-bold text-white">Pronto!</h2>
            <p className="text-sm text-gray-400">Sua conta está configurada. Você tem 7 dias do plano Pro liberados.</p>
            <button onClick={finish} disabled={busy}
              className="btn-primary w-full text-sm font-semibold py-3">
              {busy ? 'Abrindo...' : 'Abrir minha agência →'}
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
