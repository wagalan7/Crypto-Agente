import { useState, useEffect, useRef } from 'react'
import type { ReactNode } from 'react'

// ─── Shell de navegação (rail à esquerda no desktop, barra inferior no mobile) ──
// Fica SEMPRE visível num z-index acima dos overlays (z-[60] > modais z-50), então
// os painéis existentes (fixed inset-0) continuam funcionando e o menu nunca some.
// Agrupa tudo em 4 seções: Home · Operar · Aprender · Sistema.

export type SectionKey = 'home' | 'operar' | 'aprender' | 'sistema'

interface NavItem {
  key: string
  icon: string
  label: string
  hint?: string
}

interface NavGroup {
  section: SectionKey
  icon: string
  label: string
  items: NavItem[] // vazio = clique direto (sem flyout)
}

const GROUPS: NavGroup[] = [
  { section: 'home', icon: '🏠', label: 'Home', items: [] },
  {
    section: 'operar', icon: '📈', label: 'Operar', items: [
      { key: 'scanner', icon: '🔍', label: 'Scanner', hint: 'Mercado ao vivo' },
      { key: 'recs', icon: '✨', label: 'Recomendados', hint: 'Varredura automática' },
      { key: 'daily', icon: '📊', label: 'Resultado do dia', hint: 'P&L das recomendações' },
      { key: 'trades', icon: '📋', label: 'Gerenciar trades', hint: 'Posições e ordens' },
      { key: 'coach', icon: '🧘', label: 'Coach PNL', hint: 'Gestão emocional' },
    ],
  },
  {
    section: 'aprender', icon: '🧠', label: 'Aprender', items: [
      { key: 'insights', icon: '🎓', label: 'Insights', hint: 'O que o sistema aprendeu' },
      { key: 'dashboard', icon: '📈', label: 'Dashboard', hint: 'Performance comparativa' },
      { key: 'assert', icon: '🛡️', label: 'Assertividade', hint: 'Confiabilidade do bot' },
    ],
  },
  {
    section: 'sistema', icon: '⚙️', label: 'Sistema', items: [
      { key: 'sweep', icon: '📡', label: 'Sweep', hint: 'Backtest do universo (worker)' },
      { key: 'risco', icon: '🚦', label: 'Risco & circuit breaker', hint: 'Drawdown e trava' },
      { key: 'notif', icon: '🔔', label: 'Notificações', hint: 'Alertas push' },
    ],
  },
]

interface Props {
  active: SectionKey
  onSelect: (key: string) => void
  riscoSlot?: ReactNode
  notifSlot?: ReactNode
}

export default function NavRail({ active, onSelect, riscoSlot, notifSlot }: Props) {
  const [openFlyout, setOpenFlyout] = useState<SectionKey | null>(null)
  const ref = useRef<HTMLDivElement>(null)

  // Fecha o flyout ao clicar fora ou apertar Esc.
  useEffect(() => {
    if (!openFlyout) return
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpenFlyout(null)
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpenFlyout(null) }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDoc)
      document.removeEventListener('keydown', onKey)
    }
  }, [openFlyout])

  const handleTop = (g: NavGroup) => {
    if (g.items.length === 0) {
      onSelect(g.section) // Home = clique direto
      setOpenFlyout(null)
      return
    }
    setOpenFlyout(prev => (prev === g.section ? null : g.section))
  }

  const pick = (key: string) => {
    onSelect(key)
    setOpenFlyout(null)
  }

  // Renderiza o conteúdo de um item de flyout (usa slot p/ risco/notif quando houver).
  const renderItem = (it: NavItem) => {
    if (it.key === 'notif' && notifSlot) {
      return (
        <div key={it.key} className="px-3 py-2 flex items-center gap-2.5">
          <span className="text-base w-5 text-center">{it.icon}</span>
          <div className="flex-1 min-w-0">
            <div className="text-xs font-semibold text-slate-200">{it.label}</div>
            <div className="mt-1">{notifSlot}</div>
          </div>
        </div>
      )
    }
    if (it.key === 'risco' && riscoSlot) {
      return (
        <button
          key={it.key}
          onClick={() => pick(it.key)}
          className="w-full text-left px-3 py-2 flex items-center gap-2.5 hover:bg-slate-800/70 transition-colors"
        >
          <span className="text-base w-5 text-center">{it.icon}</span>
          <div className="flex-1 min-w-0">
            <div className="text-xs font-semibold text-slate-200 flex items-center gap-2">
              {it.label} <span className="scale-90 origin-left">{riscoSlot}</span>
            </div>
            {it.hint && <div className="text-[10px] text-slate-500">{it.hint}</div>}
          </div>
        </button>
      )
    }
    return (
      <button
        key={it.key}
        onClick={() => pick(it.key)}
        className="w-full text-left px-3 py-2 flex items-center gap-2.5 hover:bg-slate-800/70 transition-colors"
      >
        <span className="text-base w-5 text-center">{it.icon}</span>
        <div className="flex-1 min-w-0">
          <div className="text-xs font-semibold text-slate-200">{it.label}</div>
          {it.hint && <div className="text-[10px] text-slate-500">{it.hint}</div>}
        </div>
      </button>
    )
  }

  return (
    <div ref={ref}>
      {/* ── Desktop: rail vertical fixo à esquerda ────────────────────────────── */}
      <nav className="hidden lg:flex fixed top-0 left-0 bottom-0 w-16 z-[60] flex-col items-center gap-1 py-3 bg-[#070b16] border-r border-slate-800/60">
        <img src="/logo.jpg" alt="Crypto Win" className="w-8 h-8 rounded-md object-cover border border-yellow-500/40 shadow-[0_0_8px_rgba(234,179,8,0.25)] mb-2" />
        {GROUPS.map(g => {
          const on = active === g.section
          return (
            <button
              key={g.section}
              onClick={() => handleTop(g)}
              title={g.label}
              className={`relative w-14 py-2 rounded-lg flex flex-col items-center gap-0.5 transition-colors ${
                on ? 'bg-emerald-500/15 text-emerald-300' : 'text-slate-400 hover:bg-slate-800/60 hover:text-slate-200'
              }`}
            >
              {on && <span className="absolute left-0 top-1/2 -translate-y-1/2 w-0.5 h-6 rounded-r bg-emerald-400" />}
              <span className="text-lg leading-none">{g.icon}</span>
              <span className="text-[9px] font-semibold leading-none">{g.label}</span>
            </button>
          )
        })}
      </nav>

      {/* Flyout desktop (à direita do rail) */}
      {openFlyout && (() => {
        const g = GROUPS.find(x => x.section === openFlyout)
        if (!g || g.items.length === 0) return null
        return (
          <div className="hidden lg:block fixed left-16 bottom-3 z-[61] ml-1 w-60 rounded-xl border border-slate-700/70 bg-[#0d1220] shadow-2xl shadow-black/50 overflow-hidden"
               style={{ top: 'auto' }}>
            <div className="px-3 py-2 text-[10px] font-bold uppercase tracking-wider text-slate-500 border-b border-slate-800/60">{g.label}</div>
            <div className="py-1">{g.items.map(renderItem)}</div>
          </div>
        )
      })()}

      {/* ── Mobile: barra inferior fixa ───────────────────────────────────────── */}
      <nav className="lg:hidden fixed bottom-0 left-0 right-0 z-[60] flex items-stretch h-14 bg-[#070b16] border-t border-slate-800/60">
        {GROUPS.map(g => {
          const on = active === g.section
          return (
            <button
              key={g.section}
              onClick={() => handleTop(g)}
              className={`flex-1 flex flex-col items-center justify-center gap-0.5 ${
                on ? 'text-emerald-300' : 'text-slate-400'
              }`}
            >
              <span className="text-lg leading-none">{g.icon}</span>
              <span className="text-[10px] font-semibold leading-none">{g.label}</span>
            </button>
          )
        })}
      </nav>

      {/* Flyout mobile (sheet acima da barra) */}
      {openFlyout && (() => {
        const g = GROUPS.find(x => x.section === openFlyout)
        if (!g || g.items.length === 0) return null
        return (
          <div className="lg:hidden fixed left-0 right-0 bottom-14 z-[61] border-t border-slate-700/70 bg-[#0d1220] shadow-2xl shadow-black/50">
            <div className="px-4 py-2 text-[10px] font-bold uppercase tracking-wider text-slate-500 border-b border-slate-800/60">{g.label}</div>
            <div className="py-1 max-h-[60vh] overflow-y-auto">{g.items.map(renderItem)}</div>
          </div>
        )
      })()}
    </div>
  )
}
