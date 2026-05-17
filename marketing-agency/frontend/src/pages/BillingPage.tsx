import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../services/api'
import { useAuth } from '../context/AuthContext'

interface PlanRow {
  tier: 'free' | 'pro' | 'agency'
  label: string
  price_brl_cents: number
  max_clients: number
  max_posts_per_month: number
  features: { auto_publish: boolean; pdf_report: boolean; voice_scorer: boolean; trends: boolean }
  stripe_configured: boolean
}

function formatBRL(cents: number): string {
  if (cents === 0) return 'Grátis'
  return `R$ ${(cents / 100).toFixed(0)}/mês`
}

export function BillingPage() {
  const { user, refreshUser } = useAuth()
  const navigate = useNavigate()
  const [plans, setPlans] = useState<PlanRow[]>([])
  const [busy, setBusy] = useState<string | null>(null)
  const [err, setErr] = useState('')

  useEffect(() => {
    api.billing.plans().then((r: any) => setPlans(r)).catch(() => {})
    refreshUser().catch(() => {})
  }, [])

  async function upgrade(tier: 'pro' | 'agency') {
    setErr(''); setBusy(tier)
    try {
      const r: any = await api.billing.checkout(tier)
      if (r.url) window.location.href = r.url
    } catch (e: any) {
      try { const body = JSON.parse(e.message); setErr(body.detail || 'Erro') }
      catch { setErr(e.message || 'Erro ao iniciar checkout') }
    } finally { setBusy(null) }
  }

  async function openPortal() {
    setErr(''); setBusy('portal')
    try {
      const r: any = await api.billing.portal()
      if (r.url) window.location.href = r.url
    } catch (e: any) {
      try { const body = JSON.parse(e.message); setErr(body.detail || 'Erro') }
      catch { setErr(e.message || 'Erro ao abrir portal') }
    } finally { setBusy(null) }
  }

  const currentTier = user?.plan?.tier || 'free'
  const trialing = user?.plan?.trialing

  return (
    <div className="p-4 md:p-6 max-w-4xl mx-auto space-y-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <button onClick={() => navigate(-1)} className="text-xs text-gray-500 hover:text-gray-300 mb-2">← voltar</button>
          <h1 className="text-xl font-bold text-white">Planos e cobrança</h1>
          <p className="text-xs text-gray-400 mt-1">
            Plano atual: <span className="text-violet-300 font-semibold">{user?.plan?.label || 'Free'}</span>
            {trialing && user?.plan?.trial_ends_at && (
              <span className="ml-2 text-yellow-400">
                · trial até {new Date(user.plan.trial_ends_at).toLocaleDateString('pt-BR')}
              </span>
            )}
          </p>
        </div>
        {user?.plan?.stripe_customer_id && (
          <button onClick={openPortal} disabled={busy === 'portal'} className="btn-secondary text-xs">
            {busy === 'portal' ? 'Abrindo...' : 'Gerenciar assinatura'}
          </button>
        )}
      </div>

      {err && <div className="card bg-red-900/20 border-red-800/50 text-xs text-red-300">{err}</div>}

      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        {plans.map(p => {
          const isCurrent = p.tier === currentTier
          const isPaid = p.tier !== 'free'
          return (
            <div key={p.tier} className={`card space-y-3 ${
              isCurrent ? 'border-violet-600 bg-violet-900/10' : ''
            }`}>
              <div>
                <p className="text-xs text-gray-400 uppercase">{p.label}</p>
                <p className="text-2xl font-bold text-white mt-1">{formatBRL(p.price_brl_cents)}</p>
                {isCurrent && <p className="text-[10px] text-violet-300 mt-1">PLANO ATUAL</p>}
              </div>
              <ul className="text-xs text-gray-300 space-y-1.5">
                <li>· {p.max_clients} cliente(s)</li>
                <li>· {p.max_posts_per_month} posts/mês</li>
                <li className={p.features.auto_publish ? '' : 'text-gray-600 line-through'}>· Auto-publicação</li>
                <li className={p.features.pdf_report ? '' : 'text-gray-600 line-through'}>· Relatório PDF</li>
                <li className={p.features.voice_scorer ? '' : 'text-gray-600 line-through'}>· Voice scorer</li>
                <li className={p.features.trends ? '' : 'text-gray-600 line-through'}>· Tendências</li>
              </ul>
              {isPaid && !isCurrent && (
                <button onClick={() => upgrade(p.tier as 'pro' | 'agency')}
                  disabled={busy === p.tier || !p.stripe_configured}
                  className="btn-primary w-full text-xs py-2 disabled:opacity-50">
                  {busy === p.tier ? 'Abrindo...' : p.stripe_configured ? `Fazer upgrade para ${p.label}` : 'Em breve'}
                </button>
              )}
              {isPaid && !p.stripe_configured && !isCurrent && (
                <p className="text-[10px] text-gray-500 text-center">Pagamentos sendo configurados</p>
              )}
            </div>
          )
        })}
      </div>

      <div className="card bg-gray-900/30">
        <p className="text-xs text-gray-400">
          Dúvidas? Mande um email pra <a href="mailto:suporte@contentai" className="text-violet-400 hover:text-violet-300">suporte@contentai</a>.
        </p>
      </div>
    </div>
  )
}
