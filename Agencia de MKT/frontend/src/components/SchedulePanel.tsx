import { useState, useEffect, useCallback } from 'react'

interface Props {
  authHeaders: Record<string, string>
}

interface ScheduledPost {
  id: number
  owner: string
  text: string
  platforms: string[]
  scheduled_at: string
  status: 'pending' | 'publishing' | 'published' | 'failed' | 'cancelled'
  result?: { results?: { platform: string; success: boolean; error?: string; url?: string }[] } | null
  created_at: string
}

const STATUS_STYLE: Record<string, string> = {
  pending:    'text-blue-400 bg-blue-900/20 border-blue-800',
  publishing: 'text-violet-400 bg-violet-900/20 border-violet-800',
  published:  'text-emerald-400 bg-emerald-900/20 border-emerald-800',
  failed:     'text-red-400 bg-red-900/20 border-red-800',
  cancelled:  'text-gray-500 bg-gray-800/20 border-gray-700',
}

const STATUS_LABEL: Record<string, string> = {
  pending:    '⏳ Aguardando',
  publishing: '⟳ Publicando',
  published:  '✓ Publicado',
  failed:     '✗ Falhou',
  cancelled:  '✕ Cancelado',
}

const PLATFORM_ICON: Record<string, string> = {
  facebook: '𝕗', instagram: '◉', twitter: '✕', google: 'G', tiktok: '♪', webhook: '⚡',
}

function fmtDate(iso: string) {
  try {
    return new Date(iso).toLocaleString('pt-BR', {
      day: '2-digit', month: '2-digit', year: 'numeric',
      hour: '2-digit', minute: '2-digit',
    })
  } catch { return iso }
}

export function SchedulePanel({ authHeaders }: Props) {
  const [posts, setPosts]     = useState<ScheduledPost[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError]     = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const r = await fetch('/schedule', { headers: authHeaders })
      if (r.ok) setPosts(await r.json())
    } catch { /* ignore */ }
    setLoading(false)
  }, [authHeaders])

  useEffect(() => { load() }, [load])

  const cancel = async (id: number) => {
    if (!confirm('Cancelar agendamento?')) return
    const r = await fetch(`/schedule/${id}`, { method: 'DELETE', headers: authHeaders })
    if (r.ok) load()
    else setError('Não foi possível cancelar.')
  }

  const pending   = posts.filter(p => p.status === 'pending' || p.status === 'publishing')
  const completed = posts.filter(p => p.status === 'published' || p.status === 'failed' || p.status === 'cancelled')

  return (
    <div className="bg-gray-900/60 border border-gray-800 rounded-xl overflow-hidden">
      <div className="px-5 py-4 border-b border-gray-800 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-lg">📅</span>
          <span className="text-sm font-bold text-gray-200 tracking-wide">POSTS AGENDADOS</span>
          {pending.length > 0 && (
            <span className="text-[10px] bg-blue-900/30 border border-blue-800 text-blue-400 px-2 py-0.5 rounded-full">
              {pending.length} pendente{pending.length !== 1 ? 's' : ''}
            </span>
          )}
        </div>
        <button onClick={load} disabled={loading}
          className="text-[10px] text-gray-500 hover:text-gray-300 transition-colors">
          {loading ? '⟳ atualizando...' : '⟳ atualizar'}
        </button>
      </div>

      <div className="px-5 py-4 space-y-3">
        {error && (
          <div className="px-3 py-2 bg-red-900/30 border border-red-800 rounded-lg text-xs text-red-400">{error}</div>
        )}

        {posts.length === 0 && !loading && (
          <div className="text-center py-8">
            <p className="text-3xl mb-2">📅</p>
            <p className="text-xs text-gray-500">Nenhum post agendado ainda.<br />Use "Agendar para depois" ao publicar.</p>
          </div>
        )}

        {pending.length > 0 && (
          <div className="space-y-2">
            <p className="text-[10px] text-gray-500 uppercase tracking-widest">Próximos</p>
            {pending.map(p => (
              <PostRow key={p.id} post={p} onCancel={cancel} />
            ))}
          </div>
        )}

        {completed.length > 0 && (
          <div className="space-y-2">
            <p className="text-[10px] text-gray-500 uppercase tracking-widest mt-2">Histórico</p>
            {completed.slice(0, 20).map(p => (
              <PostRow key={p.id} post={p} onCancel={cancel} />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function PostRow({ post, onCancel }: { post: ScheduledPost; onCancel: (id: number) => void }) {
  const [expanded, setExpanded] = useState(false)

  return (
    <div className="border border-gray-700 rounded-lg overflow-hidden">
      <div className="flex items-center justify-between px-3 py-2.5 bg-gray-800/40">
        <div className="flex items-center gap-2 min-w-0">
          <div className="flex gap-0.5">
            {post.platforms.map(p => (
              <span key={p} className="text-[11px] text-gray-400">{PLATFORM_ICON[p] || p}</span>
            ))}
          </div>
          <span className="text-[11px] text-gray-300 truncate max-w-[200px]">
            {post.text.slice(0, 60)}{post.text.length > 60 ? '…' : ''}
          </span>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <span className="text-[10px] text-gray-500">{fmtDate(post.scheduled_at)}</span>
          <span className={`text-[9px] px-1.5 py-0.5 rounded border ${STATUS_STYLE[post.status] || ''}`}>
            {STATUS_LABEL[post.status] || post.status}
          </span>
          {post.status === 'pending' && (
            <button onClick={() => onCancel(post.id)}
              className="text-[9px] text-red-600 hover:text-red-400 transition-colors">
              cancelar
            </button>
          )}
          {post.result && (
            <button onClick={() => setExpanded(e => !e)}
              className="text-[9px] text-gray-500 hover:text-gray-300">
              {expanded ? '▲' : '▼'}
            </button>
          )}
        </div>
      </div>
      {expanded && post.result?.results && (
        <div className="px-3 py-2 border-t border-gray-700 space-y-1">
          {post.result.results.map((r, i) => (
            <div key={i} className="flex items-center gap-2 text-[10px]">
              <span className={r.success ? 'text-emerald-400' : 'text-red-400'}>{r.success ? '✓' : '✗'}</span>
              <span className="text-gray-400 capitalize">{r.platform}</span>
              {r.success && r.url && <a href={r.url} target="_blank" rel="noopener noreferrer" className="text-violet-400 hover:underline">ver post ↗</a>}
              {!r.success && <span className="text-red-400 truncate max-w-xs">{r.error}</span>}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
