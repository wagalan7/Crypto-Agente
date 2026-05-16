import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../services/api'
import type { Client } from '../types'

interface Step {
  key: string
  label: string
  description: string
  done: boolean
  to: string
  cta: string
}

const STORAGE_KEY = (id: number) => `onboarding-dismissed-${id}`

export function OnboardingChecklist({ clientId, client }: { clientId: number; client: Client }) {
  const [steps, setSteps] = useState<Step[] | null>(null)
  const [dismissed, setDismissed] = useState<boolean>(() => localStorage.getItem(STORAGE_KEY(clientId)) === '1')
  const [collapsed, setCollapsed] = useState(false)

  useEffect(() => {
    (async () => {
      const [personaR, productsR, weeklyR, contentR] = await Promise.allSettled([
        api.persona.get(clientId),
        api.products.list(clientId),
        api.strategy.weekly(clientId),
        api.content.list(clientId),
      ])
      const persona = personaR.status === 'fulfilled' ? (personaR.value as any) : null
      const products = productsR.status === 'fulfilled' ? ((productsR.value as any[]) || []) : []
      const weekly = weeklyR.status === 'fulfilled' ? (weeklyR.value as any) : null
      const contents = contentR.status === 'fulfilled' ? ((contentR.value as any[]) || []) : []

      const briefingDone = !!(client.tone && client.target_audience && client.positioning)
      const personaDone = !!persona?.id
      const productDone = products.some((p: any) => p.is_active)
      const primaryDone = products.some((p: any) => p.is_primary && p.is_active)
      const weeklyDone = !!weekly?.exists
      const contentDone = contents.length > 0

      const list: Step[] = [
        { key: 'briefing', label: 'Briefing completo', description: 'Tom, público-alvo e posicionamento', done: briefingDone, to: `/`, cta: 'Editar cliente' },
        { key: 'persona', label: 'Persona gerada', description: 'IA mapeia dores, desejos e emoções da audiência', done: personaDone, to: `/client/${clientId}/persona`, cta: 'Gerar persona' },
        { key: 'product', label: 'Produto cadastrado', description: 'O que você vende — pelo menos 1 ativo', done: productDone, to: `/client/${clientId}/products`, cta: 'Cadastrar produto' },
        { key: 'primary', label: 'Produto principal definido', description: 'Marque 1 como principal pra IA priorizar', done: primaryDone, to: `/client/${clientId}/products`, cta: 'Marcar principal' },
        { key: 'weekly', label: 'Cérebro semanal gerado', description: 'Plano estratégico da semana', done: weeklyDone, to: `/client/${clientId}/strategy`, cta: 'Gerar cérebro' },
        { key: 'content', label: 'Primeiro conteúdo criado', description: 'Use Auto-Criar pra estrear', done: contentDone, to: `/client/${clientId}/agents?tab=auto`, cta: 'Criar conteúdo' },
      ]
      setSteps(list)
    })()
  }, [clientId, client])

  if (!steps) return null
  const completed = steps.filter(s => s.done).length
  const total = steps.length
  const allDone = completed === total
  if (allDone || dismissed) return null

  const next = steps.find(s => !s.done)

  return (
    <div className="card bg-gradient-to-br from-violet-900/20 to-pink-900/15 border-violet-700/50">
      <div className="flex items-start justify-between gap-2 mb-2">
        <div>
          <p className="text-sm font-semibold text-violet-200">✦ Onboarding ({completed}/{total})</p>
          <p className="text-[11px] text-violet-300/70">Completar os passos faz a IA criar conteúdo muito mais afiado</p>
        </div>
        <div className="flex items-center gap-1">
          <button onClick={() => setCollapsed(v => !v)} className="text-xs text-violet-300 px-2">
            {collapsed ? 'Expandir' : 'Recolher'}
          </button>
          <button onClick={() => { localStorage.setItem(STORAGE_KEY(clientId), '1'); setDismissed(true) }} className="text-xs text-violet-400/70 hover:text-violet-200 px-2">×</button>
        </div>
      </div>

      {/* Progress bar */}
      <div className="h-1.5 bg-violet-900/40 rounded-full overflow-hidden mb-3">
        <div className="h-full bg-gradient-to-r from-violet-500 to-pink-500 transition-all" style={{ width: `${(completed / total) * 100}%` }} />
      </div>

      {!collapsed && (
        <ul className="space-y-1.5">
          {steps.map(s => (
            <li key={s.key} className={`flex items-center gap-2.5 px-2 py-1.5 rounded ${s.done ? 'opacity-60' : 'bg-violet-950/30'}`}>
              <span className={`w-5 h-5 rounded-full flex items-center justify-center text-[10px] shrink-0 ${s.done ? 'bg-emerald-700 text-white' : 'bg-violet-900 border border-violet-600 text-violet-300'}`}>
                {s.done ? '✓' : ''}
              </span>
              <div className="flex-1 min-w-0">
                <p className={`text-xs ${s.done ? 'text-gray-400 line-through' : 'text-gray-100 font-medium'}`}>{s.label}</p>
                {!s.done && <p className="text-[10px] text-violet-300/60">{s.description}</p>}
              </div>
              {!s.done && (
                <Link to={s.to} className="text-[11px] px-2 py-0.5 rounded bg-violet-700 hover:bg-violet-600 text-white shrink-0">
                  {s.cta}
                </Link>
              )}
            </li>
          ))}
        </ul>
      )}

      {collapsed && next && (
        <div className="flex items-center justify-between gap-2">
          <p className="text-xs text-gray-300">Próximo: <span className="font-semibold">{next.label}</span></p>
          <Link to={next.to} className="text-[11px] px-2 py-1 rounded bg-violet-700 hover:bg-violet-600 text-white shrink-0">
            {next.cta} →
          </Link>
        </div>
      )}
    </div>
  )
}
