import { useState, useEffect, useCallback, useRef } from 'react'

interface PostRef {
  platform: string
  post_id: string
  token: string
  bearer_token?: string
  url?: string
}

interface PlatformMetrics {
  platform: string
  post_id: string
  impressions?: number
  reach?: number
  likes?: number
  comments?: number
  shares?: number
  clicks?: number
  saves?: number
  engagements?: number
  url?: string
  error?: string
}

interface Props {
  posts: PostRef[]
  authHeaders: Record<string, string>
}

const PLATFORM_LABELS: Record<string, { label: string; icon: string; color: string }> = {
  facebook:  { label: 'Facebook',  icon: '𝕗', color: 'text-blue-400'  },
  instagram: { label: 'Instagram', icon: '◉', color: 'text-pink-400'  },
  twitter:   { label: 'Twitter/X', icon: '✕', color: 'text-sky-400'   },
}

const METRIC_LABELS: Record<string, { label: string; icon: string }> = {
  impressions: { label: 'Impressões',  icon: '👁' },
  reach:       { label: 'Alcance',     icon: '📡' },
  likes:       { label: 'Curtidas',    icon: '❤' },
  comments:    { label: 'Comentários', icon: '💬' },
  shares:      { label: 'Compartilhamentos', icon: '🔁' },
  clicks:      { label: 'Cliques',     icon: '🖱' },
  saves:       { label: 'Salvamentos', icon: '🔖' },
  engagements: { label: 'Engajamento total', icon: '⚡' },
}

function fmt(n?: number): string {
  if (n === undefined || n === null) return '—'
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M'
  if (n >= 1_000)     return (n / 1_000).toFixed(1) + 'K'
  return String(n)
}

const REFRESH_INTERVAL = 60_000 // 60 seconds

export function MetricsPanel({ posts, authHeaders }: Props) {
  const [metrics, setMetrics]     = useState<PlatformMetrics[]>([])
  const [loading, setLoading]     = useState(false)
  const [lastUpdate, setLastUpdate] = useState<Date | null>(null)
  const [countdown, setCountdown] = useState(REFRESH_INTERVAL / 1000)
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const supportedPosts = posts.filter(p => ['facebook', 'instagram', 'twitter'].includes(p.platform))

  const fetchMetrics = useCallback(async () => {
    if (!supportedPosts.length) return
    setLoading(true)
    try {
      const res = await fetch('/metrics/fetch', {
        method: 'POST',
        headers: authHeaders,
        body: JSON.stringify({ posts: supportedPosts }),
      })
      if (res.ok) {
        const data = await res.json()
        setMetrics(data.metrics || [])
        setLastUpdate(new Date())
        setCountdown(REFRESH_INTERVAL / 1000)
      }
    } catch { /* ignore */ }
    setLoading(false)
  }, [supportedPosts, authHeaders])

  // Initial fetch + auto-refresh
  useEffect(() => {
    fetchMetrics()
    timerRef.current = setInterval(fetchMetrics, REFRESH_INTERVAL)
    return () => { if (timerRef.current) clearInterval(timerRef.current) }
  }, [fetchMetrics])

  // Countdown display
  useEffect(() => {
    const tick = setInterval(() => setCountdown(c => Math.max(0, c - 1)), 1000)
    return () => clearInterval(tick)
  }, [lastUpdate])

  if (!supportedPosts.length) return null

  return (
    <div className="bg-gray-900/60 border border-gray-800 rounded-xl overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between px-5 py-4 border-b border-gray-800">
        <div className="flex items-center gap-2">
          <span className="text-base">📊</span>
          <span className="text-sm font-bold text-gray-200 tracking-wide">MÉTRICAS EM TEMPO REAL</span>
          {loading && (
            <span className="w-3 h-3 border-2 border-violet-400/30 border-t-violet-400 rounded-full animate-spin" />
          )}
        </div>
        <div className="flex items-center gap-3">
          {lastUpdate && (
            <span className="text-[10px] text-gray-600">
              atualiza em {countdown}s
            </span>
          )}
          <button
            onClick={fetchMetrics}
            disabled={loading}
            className="text-[10px] text-violet-400 hover:text-violet-300 transition-colors disabled:opacity-50"
          >
            ↺ atualizar agora
          </button>
        </div>
      </div>

      <div className="p-5 space-y-4">
        {metrics.length === 0 && !loading && (
          <p className="text-xs text-gray-600 text-center py-4">
            Aguardando dados das plataformas...
          </p>
        )}

        {metrics.map(m => {
          const meta = PLATFORM_LABELS[m.platform]
          if (!meta) return null

          const metricKeys = ['impressions', 'reach', 'likes', 'comments', 'shares', 'clicks', 'saves', 'engagements'] as const
          const hasData    = metricKeys.some(k => m[k] !== undefined && m[k] !== null)

          return (
            <div key={m.platform} className="border border-gray-800 rounded-xl overflow-hidden">
              {/* Platform header */}
              <div className="flex items-center justify-between px-4 py-3 bg-gray-800/40">
                <div className="flex items-center gap-2">
                  <span className={`text-base ${meta.color}`}>{meta.icon}</span>
                  <span className="text-xs font-semibold text-gray-200">{meta.label}</span>
                  {m.error
                    ? <span className="text-[9px] text-red-400 bg-red-900/20 border border-red-800 px-1.5 py-0.5 rounded-full">erro</span>
                    : hasData
                      ? <span className="text-[9px] text-emerald-400 bg-emerald-900/20 border border-emerald-800 px-1.5 py-0.5 rounded-full">● ao vivo</span>
                      : <span className="text-[9px] text-gray-600">aguardando...</span>}
                </div>
                {m.url && (
                  <a href={m.url} target="_blank" rel="noopener noreferrer"
                    className="text-[10px] text-violet-400 hover:text-violet-300 transition-colors">
                    ver post ↗
                  </a>
                )}
              </div>

              {/* Error */}
              {m.error && (
                <div className="px-4 py-3 bg-red-950/20">
                  <p className="text-[11px] text-red-400">{m.error}</p>
                  <p className="text-[10px] text-gray-600 mt-0.5">
                    Nota: algumas métricas só ficam disponíveis após algumas horas da publicação.
                  </p>
                </div>
              )}

              {/* Metrics grid */}
              {!m.error && (
                <div className="grid grid-cols-4 gap-px bg-gray-800/30 border-t border-gray-800">
                  {metricKeys.map(key => {
                    const val  = m[key]
                    const meta = METRIC_LABELS[key]
                    if (val === undefined || val === null) return null
                    return (
                      <div key={key} className="bg-gray-900/60 px-3 py-3 text-center">
                        <p className="text-base">{meta.icon}</p>
                        <p className="text-lg font-bold text-white mt-0.5">{fmt(val)}</p>
                        <p className="text-[9px] text-gray-500 mt-0.5 leading-tight">{meta.label}</p>
                      </div>
                    )
                  })}
                </div>
              )}

              {/* Awaiting data message */}
              {!m.error && !hasData && (
                <div className="px-4 py-3 border-t border-gray-800">
                  <p className="text-[11px] text-gray-500 text-center">
                    Métricas chegam em alguns minutos após publicação · atualizando automaticamente
                  </p>
                </div>
              )}
            </div>
          )
        })}

        <p className="text-[9px] text-gray-700 text-center">
          Dados via API oficial · atualização automática a cada 60 segundos
        </p>
      </div>
    </div>
  )
}
