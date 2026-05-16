import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../services/api'

interface NotificationsResponse {
  total: number
  pending_approvals: number
  critical: number
  warning: number
  failed_publish: number
  top_alert: { title: string; severity: string } | null
}

export function NotificationBadge({ clientId }: { clientId: number }) {
  const [data, setData] = useState<NotificationsResponse | null>(null)
  const [open, setOpen] = useState(false)

  useEffect(() => {
    let alive = true
    function load() {
      api.strategy.notifications(clientId)
        .then((r: any) => { if (alive) setData(r) })
        .catch(() => {})
    }
    load()
    const t = setInterval(load, 60_000)  // refresh every minute
    return () => { alive = false; clearInterval(t) }
  }, [clientId])

  if (!data || data.total === 0) return null

  const color = data.critical > 0 ? 'bg-red-600' : data.warning > 0 ? 'bg-yellow-600' : 'bg-violet-600'

  return (
    <div className="relative">
      <button
        onClick={() => setOpen(o => !o)}
        className="relative flex items-center justify-center w-7 h-7 rounded-full bg-gray-800 hover:bg-gray-700 transition-colors"
        title={`${data.total} item(s) precisando de atenção`}
      >
        <span className="text-sm">🔔</span>
        <span className={`absolute -top-1 -right-1 text-[9px] font-bold text-white ${color} rounded-full min-w-[16px] h-4 px-1 flex items-center justify-center`}>
          {data.total > 99 ? '99+' : data.total}
        </span>
      </button>

      {open && (
        <div className="absolute right-0 mt-2 w-72 bg-gray-950 border border-gray-800 rounded-lg shadow-xl z-50 p-3 space-y-2">
          <p className="text-xs font-semibold text-white">Precisa da sua atenção</p>
          {data.critical > 0 && (
            <Link to={`/client/${clientId}/strategy`} onClick={() => setOpen(false)}
              className="block text-xs text-red-300 bg-red-900/20 border border-red-800/40 rounded px-2 py-1.5 hover:bg-red-900/30">
              ⚠ {data.critical} alerta(s) crítico(s)
            </Link>
          )}
          {data.warning > 0 && (
            <Link to={`/client/${clientId}/strategy`} onClick={() => setOpen(false)}
              className="block text-xs text-yellow-300 bg-yellow-900/20 border border-yellow-800/40 rounded px-2 py-1.5 hover:bg-yellow-900/30">
              ⚡ {data.warning} aviso(s)
            </Link>
          )}
          {data.pending_approvals > 0 && (
            <Link to={`/client/${clientId}/content`} onClick={() => setOpen(false)}
              className="block text-xs text-violet-300 bg-violet-900/20 border border-violet-800/40 rounded px-2 py-1.5 hover:bg-violet-900/30">
              ◈ {data.pending_approvals} aguardando aprovação
            </Link>
          )}
          {data.failed_publish > 0 && (
            <Link to={`/client/${clientId}/content`} onClick={() => setOpen(false)}
              className="block text-xs text-orange-300 bg-orange-900/20 border border-orange-800/40 rounded px-2 py-1.5 hover:bg-orange-900/30">
              ✗ {data.failed_publish} falha(s) ao publicar
            </Link>
          )}
          {data.top_alert && (
            <div className="text-[11px] text-gray-400 pt-1 border-t border-gray-800">
              <span className="text-gray-500">Mais urgente: </span>{data.top_alert.title}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
