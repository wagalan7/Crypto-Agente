import { useState, useEffect, useCallback } from 'react'

interface Props { authHeaders: Record<string, string> }

interface AlertRule {
  id: number; platform: string; metric: string
  condition: string; threshold: number; label: string; active: number; created_at: string
}

interface Notification {
  id: number; message: string; level: string; read: number; created_at: string
}

const METRICS = [
  { value: 'ctr',         label: 'CTR (%)',         unit: '%'  },
  { value: 'avg_cpc',     label: 'CPC Médio (R$)',  unit: 'R$' },
  { value: 'cost',        label: 'Gasto total (R$)', unit: 'R$' },
  { value: 'impressions', label: 'Impressões',       unit: ''   },
  { value: 'clicks',      label: 'Cliques',          unit: ''   },
  { value: 'conversions', label: 'Conversões',       unit: ''   },
]

const CONDITIONS = [
  { value: '<', label: 'Abaixo de (<)' },
  { value: '>', label: 'Acima de (>)'  },
]

function fmtDate(iso: string) {
  try { return new Date(iso).toLocaleString('pt-BR', { day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit' }) }
  catch { return iso }
}

export function AlertsPanel({ authHeaders }: Props) {
  const [rules, setRules]           = useState<AlertRule[]>([])
  const [notifs, setNotifs]         = useState<Notification[]>([])
  const [unread, setUnread]         = useState(0)
  const [tab, setTab]               = useState<'rules' | 'notifications'>('notifications')
  const [loading, setLoading]       = useState(false)
  const [form, setForm]             = useState({ metric: 'ctr', condition: '<', threshold: '1', label: '' })
  const [saving, setSaving]         = useState(false)
  const [error, setError]           = useState('')

  const loadRules = useCallback(async () => {
    const r = await fetch('/alerts', { headers: authHeaders })
    if (r.ok) setRules(await r.json())
  }, [authHeaders])

  const loadNotifs = useCallback(async () => {
    setLoading(true)
    const r = await fetch('/notifications', { headers: authHeaders })
    if (r.ok) {
      const d = await r.json()
      setNotifs(d.notifications || [])
      setUnread(d.unread || 0)
    }
    setLoading(false)
  }, [authHeaders])

  useEffect(() => { loadRules(); loadNotifs() }, [loadRules, loadNotifs])

  const markRead = async () => {
    await fetch('/notifications/read', { method: 'POST', headers: authHeaders })
    setUnread(0)
    setNotifs(n => n.map(x => ({ ...x, read: 1 })))
  }

  const addRule = async () => {
    if (!form.threshold) return
    setSaving(true); setError('')
    const r = await fetch('/alerts', {
      method: 'POST', headers: authHeaders,
      body: JSON.stringify({
        metric: form.metric, condition: form.condition,
        threshold: parseFloat(form.threshold), label: form.label,
      }),
    })
    if (r.ok) { loadRules(); setForm({ metric: 'ctr', condition: '<', threshold: '1', label: '' }) }
    else { const d = await r.json(); setError(d.detail || 'Erro') }
    setSaving(false)
  }

  const deleteRule = async (id: number) => {
    await fetch(`/alerts/${id}`, { method: 'DELETE', headers: authHeaders })
    loadRules()
  }

  const metricLabel = (m: string) => METRICS.find(x => x.value === m)?.label || m

  return (
    <div className="bg-gray-900/60 border border-gray-800 rounded-xl overflow-hidden">
      <div className="px-5 py-4 border-b border-gray-800 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-lg">🔔</span>
          <span className="text-sm font-bold text-gray-200 tracking-wide">ALERTAS DE PERFORMANCE</span>
          {unread > 0 && (
            <span className="text-[10px] bg-red-900/40 border border-red-700 text-red-400 px-2 py-0.5 rounded-full">
              {unread} novo{unread !== 1 ? 's' : ''}
            </span>
          )}
        </div>
        <div className="flex gap-1">
          {(['notifications', 'rules'] as const).map(t => (
            <button key={t} onClick={() => setTab(t)}
              className={`text-[10px] px-3 py-1 rounded-lg border transition-all
                ${tab === t ? 'border-violet-600 bg-violet-900/30 text-violet-300' : 'border-gray-700 text-gray-500 hover:text-gray-300'}`}>
              {t === 'notifications' ? '🔔 Notificações' : '⚙ Regras'}
            </button>
          ))}
        </div>
      </div>

      <div className="px-5 py-4 space-y-4">
        {error && <div className="px-3 py-2 bg-red-900/30 border border-red-800 rounded-lg text-xs text-red-400">{error}</div>}

        {/* Notifications tab */}
        {tab === 'notifications' && (
          <div className="space-y-3">
            <div className="flex justify-between items-center">
              <p className="text-[10px] text-gray-500 uppercase tracking-widest">
                Alertas disparados {unread > 0 ? `· ${unread} não lidos` : ''}
              </p>
              {unread > 0 && (
                <button onClick={markRead} className="text-[10px] text-violet-400 hover:text-violet-300">
                  marcar todos como lido
                </button>
              )}
            </div>
            {loading && <p className="text-xs text-gray-600">Carregando...</p>}
            {!loading && notifs.length === 0 && (
              <div className="text-center py-8">
                <p className="text-2xl mb-2">✅</p>
                <p className="text-xs text-gray-500">Nenhum alerta disparado ainda.<br />Configure regras e o sistema monitorará automaticamente.</p>
              </div>
            )}
            {notifs.map(n => (
              <div key={n.id} className={`flex gap-3 px-3 py-2.5 rounded-lg border transition-all
                ${n.read ? 'border-gray-800 bg-gray-900/30' : 'border-amber-800/60 bg-amber-900/10'}`}>
                <span className="text-base mt-0.5">{n.level === 'critical' ? '🚨' : '⚠'}</span>
                <div className="flex-1 min-w-0">
                  <p className="text-[11px] text-gray-200 leading-relaxed">{n.message}</p>
                  <p className="text-[9px] text-gray-600 mt-0.5">{fmtDate(n.created_at)}</p>
                </div>
                {!n.read && <span className="w-2 h-2 rounded-full bg-amber-400 mt-1.5 shrink-0" />}
              </div>
            ))}
          </div>
        )}

        {/* Rules tab */}
        {tab === 'rules' && (
          <div className="space-y-4">
            {/* Existing rules */}
            {rules.length > 0 && (
              <div className="space-y-2">
                <p className="text-[10px] text-gray-500 uppercase tracking-widest">Regras ativas</p>
                {rules.map(r => (
                  <div key={r.id} className="flex items-center justify-between px-3 py-2 bg-gray-800/40 border border-gray-700 rounded-lg">
                    <div>
                      <p className="text-[11px] text-gray-200">
                        <span className="text-violet-400">{r.label || metricLabel(r.metric)}</span>
                        {' '}{r.condition === '<' ? 'abaixo de' : 'acima de'}{' '}
                        <span className="text-amber-400 font-semibold">{r.threshold}</span>
                      </p>
                      <p className="text-[9px] text-gray-600">{r.platform} · criado {fmtDate(r.created_at)}</p>
                    </div>
                    <button onClick={() => deleteRule(r.id)} className="text-[10px] text-red-600 hover:text-red-400">remover</button>
                  </div>
                ))}
              </div>
            )}

            {/* Add rule form */}
            <div className="bg-gray-800/40 border border-gray-700 rounded-xl p-4 space-y-3">
              <p className="text-[10px] text-gray-400 font-bold uppercase tracking-widest">+ Nova Regra de Alerta</p>
              <p className="text-[9px] text-gray-600">
                O sistema verifica automaticamente a cada hora e envia notificação quando a condição for atingida.<br />
                Para alertas por email, configure SMTP_HOST/SMTP_USER/SMTP_PASS no Railway.
              </p>
              <div>
                <label className="block text-[9px] text-gray-500 mb-0.5 uppercase tracking-wider">Nome do alerta</label>
                <input
                  className="w-full bg-gray-900 border border-gray-700 rounded-md px-2.5 py-1.5 text-xs text-gray-200
                             focus:outline-none focus:border-violet-500"
                  placeholder="Ex: CTR baixo no Google"
                  value={form.label}
                  onChange={e => setForm(f => ({ ...f, label: e.target.value }))}
                />
              </div>
              <div className="grid grid-cols-3 gap-2">
                <div>
                  <label className="block text-[9px] text-gray-500 mb-0.5 uppercase tracking-wider">Métrica</label>
                  <select value={form.metric} onChange={e => setForm(f => ({ ...f, metric: e.target.value }))}
                    className="w-full bg-gray-900 border border-gray-700 rounded-md px-2 py-1.5 text-xs text-gray-200
                               focus:outline-none focus:border-violet-500">
                    {METRICS.map(m => <option key={m.value} value={m.value}>{m.label}</option>)}
                  </select>
                </div>
                <div>
                  <label className="block text-[9px] text-gray-500 mb-0.5 uppercase tracking-wider">Condição</label>
                  <select value={form.condition} onChange={e => setForm(f => ({ ...f, condition: e.target.value }))}
                    className="w-full bg-gray-900 border border-gray-700 rounded-md px-2 py-1.5 text-xs text-gray-200
                               focus:outline-none focus:border-violet-500">
                    {CONDITIONS.map(c => <option key={c.value} value={c.value}>{c.label}</option>)}
                  </select>
                </div>
                <div>
                  <label className="block text-[9px] text-gray-500 mb-0.5 uppercase tracking-wider">Valor</label>
                  <input type="number" step="0.01"
                    className="w-full bg-gray-900 border border-gray-700 rounded-md px-2.5 py-1.5 text-xs text-gray-200
                               focus:outline-none focus:border-violet-500"
                    placeholder="1.5"
                    value={form.threshold}
                    onChange={e => setForm(f => ({ ...f, threshold: e.target.value }))}
                  />
                </div>
              </div>
              <button onClick={addRule} disabled={saving || !form.threshold}
                className="w-full py-1.5 rounded-lg text-xs font-semibold text-white transition-all
                  bg-gradient-to-r from-violet-700 to-blue-700 hover:from-violet-600 hover:to-blue-600
                  disabled:opacity-50">
                {saving ? 'Salvando...' : '+ Criar alerta'}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
