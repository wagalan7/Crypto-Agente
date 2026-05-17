import { NavLink, useParams, useNavigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { NotificationBadge } from './NotificationBadge'

const NAV = [
  { to: '', label: 'Dashboard', icon: '⬡' },
  { to: 'strategy', label: 'Central', icon: '✦' },
  { to: 'persona', label: 'Persona', icon: '☉' },
  { to: 'products', label: 'Produtos', icon: '⊙' },
  { to: 'inspirations', label: 'Inspirações', icon: '✧' },
  { to: 'knowledge', label: 'Base', icon: '☷' },
  { to: 'calendar', label: 'Calendário', icon: '◫' },
  { to: 'content', label: 'Conteúdo', icon: '◈' },
  { to: 'agents', label: 'Agentes', icon: '◉' },
  { to: 'analytics', label: 'Analytics', icon: '◎' },
  { to: 'social', label: 'Social', icon: '◐' },
]

export function Sidebar({ clientName }: { clientName?: string }) {
  const { clientId } = useParams<{ clientId: string }>()
  const navigate = useNavigate()
  const { user, logout } = useAuth()
  const base = `/client/${clientId}`

  function handleLogout() {
    logout()
    navigate('/login', { replace: true })
  }

  return (
    <>
      {/* Desktop sidebar */}
      <aside className="hidden md:flex w-52 shrink-0 border-r border-gray-800 flex-col min-h-screen bg-gray-950">
        <div className="px-4 py-5 border-b border-gray-800">
          <div className="flex items-center justify-between mb-1">
            <div className="flex items-center gap-2">
              <div className="w-7 h-7 rounded-lg bg-violet-600 flex items-center justify-center text-white font-bold text-xs">A</div>
              <span className="text-sm font-bold text-white">ContentAI</span>
            </div>
            {clientId && <NotificationBadge clientId={Number(clientId)} />}
          </div>
          {clientName && <p className="text-xs text-gray-500 truncate mt-1">{clientName}</p>}
        </div>
        <nav className="flex-1 p-2 space-y-0.5">
          {NAV.map(({ to, label, icon }) => (
            <NavLink
              key={to}
              to={to ? `${base}/${to}` : base}
              end={!to}
              className={({ isActive }) => `nav-link ${isActive ? 'nav-link-active' : 'nav-link-inactive'}`}
            >
              <span className="text-base leading-none">{icon}</span>
              <span>{label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="p-3 border-t border-gray-800 space-y-1">
          <NavLink to="/" className="nav-link nav-link-inactive text-xs">
            <span>←</span>
            <span>Trocar cliente</span>
          </NavLink>
          <NavLink to="/billing" className="nav-link nav-link-inactive text-xs">
            <span>💳</span>
            <span>Planos {user?.plan?.trialing ? '(trial)' : ''}</span>
          </NavLink>
          {user && (
            <div className="px-3 py-1">
              <p className="text-[10px] text-gray-500 truncate">{user.email}</p>
              <p className="text-[10px] text-violet-500 capitalize">{user.role} · {user.plan?.label || 'Free'}</p>
            </div>
          )}
          <button onClick={handleLogout} className="nav-link nav-link-inactive text-xs w-full text-left">
            <span>⏻</span><span>Sair</span>
          </button>
        </div>
      </aside>

      {/* Mobile top header */}
      <header className="md:hidden fixed top-0 inset-x-0 z-40 bg-gray-950 border-b border-gray-800 flex items-center justify-between px-4 h-12">
        <div className="flex items-center gap-2">
          <div className="w-6 h-6 rounded-md bg-violet-600 flex items-center justify-center text-white font-bold text-xs">A</div>
          <span className="text-sm font-semibold text-white truncate max-w-[180px]">{clientName || 'ContentAI'}</span>
        </div>
        <div className="flex items-center gap-2">
          {clientId && <NotificationBadge clientId={Number(clientId)} />}
          <button onClick={handleLogout} className="text-xs text-gray-400 border border-gray-700 rounded-md px-2 py-1">
            Sair
          </button>
        </div>
      </header>

      {/* Mobile bottom nav */}
      <nav className="md:hidden fixed bottom-0 inset-x-0 z-40 bg-gray-950 border-t border-gray-800 flex">
        {NAV.map(({ to, label, icon }) => (
          <NavLink
            key={to}
            to={to ? `${base}/${to}` : base}
            end={!to}
            className={({ isActive }) =>
              `flex-1 flex flex-col items-center justify-center py-2 gap-0.5 text-[10px] font-medium transition-colors ${
                isActive ? 'text-violet-400' : 'text-gray-500'
              }`
            }
          >
            <span className="text-lg leading-none">{icon}</span>
            <span>{label}</span>
          </NavLink>
        ))}
      </nav>
    </>
  )
}
