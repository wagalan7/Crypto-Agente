import { NavLink, useParams } from 'react-router-dom'

const NAV = [
  { to: '', label: 'Dashboard', icon: '⬡' },
  { to: 'calendar', label: 'Calendário', icon: '◫' },
  { to: 'content', label: 'Conteúdo', icon: '◈' },
  { to: 'agents', label: 'Agentes IA', icon: '◉' },
  { to: 'analytics', label: 'Analytics', icon: '◎' },
]

export function Sidebar({ clientName }: { clientName?: string }) {
  const { clientId } = useParams<{ clientId: string }>()
  const base = `/client/${clientId}`

  return (
    <aside className="w-52 shrink-0 border-r border-gray-800 flex flex-col min-h-screen bg-gray-950">
      <div className="px-4 py-5 border-b border-gray-800">
        <div className="flex items-center gap-2 mb-1">
          <div className="w-7 h-7 rounded-lg bg-violet-600 flex items-center justify-center text-white font-bold text-xs">
            A
          </div>
          <span className="text-sm font-bold text-white">ContentAI</span>
        </div>
        {clientName && (
          <p className="text-xs text-gray-500 truncate mt-1">{clientName}</p>
        )}
      </div>

      <nav className="flex-1 p-2 space-y-0.5">
        {NAV.map(({ to, label, icon }) => (
          <NavLink
            key={to}
            to={to ? `${base}/${to}` : base}
            end={!to}
            className={({ isActive }) =>
              `nav-link ${isActive ? 'nav-link-active' : 'nav-link-inactive'}`
            }
          >
            <span className="text-base leading-none">{icon}</span>
            <span>{label}</span>
          </NavLink>
        ))}
      </nav>

      <div className="p-3 border-t border-gray-800">
        <NavLink
          to="/"
          className="nav-link nav-link-inactive text-xs"
        >
          <span>←</span>
          <span>Trocar cliente</span>
        </NavLink>
      </div>
    </aside>
  )
}
