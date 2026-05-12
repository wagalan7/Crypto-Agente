import { useState, useCallback, useEffect } from 'react'
import { ProductForm } from './components/ProductForm'
import { PixelOffice } from './components/PixelOffice'
import { PipelineHeader } from './components/PipelineHeader'
import { PublishPanel } from './components/PublishPanel'
import { LoginPage } from './components/LoginPage'
import { UsersPanel } from './components/UsersPanel'
import { CampaignHistory } from './components/CampaignHistory'
import { MagaLogo } from './components/MagaLogo'
import { CredentialsPanel } from './components/CredentialsPanel'
import type { AllCreds } from './components/CredentialsPanel'
import { useAuth } from './hooks/useAuth'
import type { ProductInput, AgentState, AgentName, SSEEvent } from './types'
import { AGENTS } from './types'

const AGENT_NAMES = Object.keys(AGENTS) as AgentName[]
const TOTAL_AGENTS = AGENT_NAMES.length   // 10

const emptyAgents = (): Record<AgentName, AgentState> => {
  const entries = AGENT_NAMES.map(n => [n, {
    status: 'idle' as const, task: '', progress: 0, logs: [], output: '',
  }])
  return Object.fromEntries(entries) as Record<AgentName, AgentState>
}

const PHASE_MAP: Record<string, number> = {
  'Fase 1': 1, 'Fase 2': 2, 'Fase 3': 3, 'Fase 4': 4, 'Fase 5': 5, 'Fase 6': 6,
}

export default function App() {
  // ── ALL hooks first ──────────────────────────────────────────
  const { isLoggedIn, login, logout, authHeaders, token } = useAuth()
  const [currentUser, setCurrentUser]   = useState('')
  const [isAdmin, setIsAdmin]           = useState(false)
  const [showUsers, setShowUsers]       = useState(false)
  const [showHistory, setShowHistory]   = useState(false)
  const [showConfig, setShowConfig]     = useState(false)
  const [savedCreds, setSavedCreds]     = useState<AllCreds>({})
  const [allUsers, setAllUsers]         = useState<{ user: string; role: string }[]>([])
  const [loading, setLoading]           = useState(false)
  const [agents, setAgents]             = useState(emptyAgents())
  const [status, setStatus]             = useState('')
  const [phase, setPhase]               = useState(0)
  const [done, setDone]                 = useState(false)
  const [started, setStarted]           = useState(false)
  const [lastInput, setLastInput]       = useState<ProductInput | null>(null)

  // Fetch user info when logged in
  useEffect(() => {
    if (!isLoggedIn || !token) return
    fetch('/auth/me', { headers: { Authorization: `Bearer ${token}` } })
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        if (d) {
          setCurrentUser(d.user)
          setIsAdmin(d.role === 'admin')
        }
      })
      .catch(() => {})
  }, [isLoggedIn, token])

  // Load credentials on login so PublishPanel always has them
  useEffect(() => {
    if (!isLoggedIn || !token) return
    fetch('/credentials', { headers: { Authorization: `Bearer ${token}` } })
      .then(r => r.ok ? r.json() : {})
      .then(setSavedCreds)
      .catch(() => {})
  }, [isLoggedIn, token])

  // Load all users for the grants dropdown (admin only)
  useEffect(() => {
    if (!isAdmin || !token) return
    fetch('/auth/users', { headers: { Authorization: `Bearer ${token}` } })
      .then(r => r.ok ? r.json() : [])
      .then(setAllUsers)
      .catch(() => {})
  }, [isAdmin, token])

  // Auto-save campaign when done
  useEffect(() => {
    if (!done || !lastInput || !currentUser) return
    const result: Record<string, string> = {}
    AGENT_NAMES.forEach(n => { if (agents[n].output) result[n] = agents[n].output })
    fetch('/campaigns', {
      method: 'POST',
      headers: authHeaders,
      body: JSON.stringify({
        produto: lastInput.produto,
        input_data: lastInput,
        result_data: result,
      }),
    }).catch(() => {})
  }, [done])   // intentionally only trigger on done change

  const updateAgent = useCallback((name: AgentName, patch: Partial<AgentState>) =>
    setAgents(prev => ({ ...prev, [name]: { ...prev[name], ...patch } })), [])

  const handleSubmit = useCallback(async (data: ProductInput) => {
    setLoading(true)
    setStarted(true)
    setAgents(emptyAgents())
    setDone(false)
    setPhase(0)
    setStatus('Iniciando pipeline...')
    setLastInput(data)

    try {
      const res = await fetch('/agency/run', {
        method: 'POST',
        headers: authHeaders,
        body: JSON.stringify(data),
      })

      if (res.status === 401) { logout(); return }

      const reader  = res.body!.getReader()
      const decoder = new TextDecoder()
      let buffer    = ''

      while (true) {
        const { done: streamDone, value } = await reader.read()
        if (streamDone) break
        buffer += decoder.decode(value, { stream: true })

        const parts = buffer.split('\n\n')
        buffer = parts.pop() ?? ''

        for (const part of parts) {
          if (!part.startsWith('data: ')) continue
          const ev: SSEEvent = JSON.parse(part.slice(6))

          if (ev.type === 'keepalive') {
            // ignore — just keeps connection alive
          } else if (ev.type === 'status') {
            setStatus(ev.payload)
            const ph = Object.entries(PHASE_MAP).find(([k]) => ev.payload.startsWith(k))
            if (ph) setPhase(ph[1])
          } else if (ev.type === 'agent_event') {
            const { agent, status: s, task, progress, logs } = ev.payload
            updateAgent(agent, { status: s, task, progress, logs })
          } else if (ev.type === 'chunk') {
            const { agent, text } = ev.payload
            setAgents(prev => ({
              ...prev,
              [agent]: { ...prev[agent], output: prev[agent].output + text },
            }))
          } else if (ev.type === 'done') {
            setStatus(ev.payload)
            setDone(true)
            setLoading(false)
            setPhase(6)
            // Debug: log agent output lengths to browser console
            setAgents(prev => {
              const names = Object.keys(prev) as AgentName[]
              console.log('[MagaOne] Agent outputs on done:', Object.fromEntries(
                names.map(n => [n, prev[n].output.length])
              ))
              return prev
            })
          }
        }
      }
    } catch {
      setStatus('Erro de conexão.')
      setLoading(false)
    }
  }, [authHeaders, logout, updateAgent])

  // ── Auth guard ───────────────────────────────────────────────
  if (!isLoggedIn) return <LoginPage onLogin={login} />

  const activeCount = AGENT_NAMES.filter(n =>
    agents[n].status === 'generating' || agents[n].status === 'thinking').length
  const doneCount = AGENT_NAMES.filter(n => agents[n].status === 'completed').length

  return (
    <div className="min-h-screen bg-[#070711]">
      <header className="border-b border-gray-800/60 px-6 py-3 sticky top-0 bg-[#070711]/95 backdrop-blur z-10">
        <div className="max-w-5xl mx-auto flex items-center justify-between">
          {/* Logo + Name */}
          <div className="flex items-center gap-3">
            <MagaLogo size={34} className="rounded-lg" />
            <div>
              <h1 className="text-sm font-bold text-white leading-none">Maga One</h1>
              <p className="text-[10px] text-gray-600 mt-0.5">
                {TOTAL_AGENTS} Agentes · Pipeline Autônomo
              </p>
            </div>
          </div>

          {/* Status + actions */}
          <div className="flex items-center gap-3 text-[11px]">
            {loading && (
              <span className="text-violet-400 flex items-center gap-1">
                <span className="w-1.5 h-1.5 rounded-full bg-violet-400 animate-pulse inline-block" />
                {activeCount} ativo{activeCount !== 1 ? 's' : ''}
              </span>
            )}
            {started && <span className="text-gray-500">{doneCount}/{TOTAL_AGENTS}</span>}
            {done && (
              <span className="text-emerald-400 bg-emerald-900/20 border border-emerald-800 px-2 py-0.5 rounded-full">
                ✓ pronto
              </span>
            )}
            <button
              onClick={() => { setShowHistory(h => !h); setShowUsers(false); setShowConfig(false) }}
              className={`transition-colors ${showHistory ? 'text-violet-400' : 'text-gray-600 hover:text-gray-400'}`}
            >
              histórico
            </button>
            <button
              onClick={() => { setShowConfig(c => !c); setShowHistory(false); setShowUsers(false) }}
              className={`transition-colors ${showConfig ? 'text-violet-400' : 'text-gray-600 hover:text-gray-400'}`}
            >
              credenciais
            </button>
            {isAdmin && (
              <button
                onClick={() => { setShowUsers(s => !s); setShowHistory(false); setShowConfig(false) }}
                className={`transition-colors ${showUsers ? 'text-violet-400' : 'text-gray-600 hover:text-gray-400'}`}
              >
                usuários
              </button>
            )}
            <button onClick={logout} className="text-gray-600 hover:text-gray-400 transition-colors">
              sair
            </button>
          </div>
        </div>
      </header>

      <main className="max-w-5xl mx-auto px-4 py-5 space-y-4">
        {!started ? (
          <ProductForm onSubmit={handleSubmit} loading={loading} />
        ) : (
          <div className="flex items-center justify-between bg-gray-900/40 border border-gray-800 rounded-xl px-4 py-3">
            <span className="text-xs text-gray-400">Pipeline em execução</span>
            {!loading && (
              <button
                onClick={() => { setStarted(false); setAgents(emptyAgents()); setDone(false) }}
                className="text-[11px] text-gray-500 hover:text-gray-300 transition-colors"
              >
                ← novo produto
              </button>
            )}
          </div>
        )}

        {started && (
          <PipelineHeader status={status} phase={phase} totalPhases={6} loading={loading} done={done} />
        )}
        {started && <PixelOffice agents={agents} />}

        {showConfig && (
          <CredentialsPanel
            authHeaders={authHeaders}
            onLoaded={setSavedCreds}
          />
        )}

        {showHistory && (
          <CampaignHistory
            authHeaders={authHeaders}
            isAdmin={isAdmin}
            currentUser={currentUser}
            allUsers={allUsers}
          />
        )}
        {showUsers && isAdmin && (
          <UsersPanel authHeaders={authHeaders} currentUser={currentUser} />
        )}

        {done && (
          <PublishPanel
            publisherOutput={agents.PUBLICADOR.output}
            copyOutput={agents.COPY.output}
            socialOutput={agents.SOCIAL.output}
            designOutput={agents.DESIGN.output}
            adsOutput={agents.ADS.output}
            userBudget={lastInput?.orcamento ?? ''}
            savedCreds={savedCreds}
            authHeaders={authHeaders}
          />
        )}
      </main>
    </div>
  )
}
