import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../services/api'

interface AmplifierHub {
  client_id: number
  knowledge: Array<{
    id: number; title: string; source_type: string;
    summary?: string | null; key_insights?: string[] | null;
    voice_signals?: string[] | null; tags?: string[]; use_count?: number;
    last_used_at?: string | null; created_at?: string | null;
  }>
  inspirations: Array<{
    id: number; label?: string | null; source_type: string;
    image_url?: string | null; analysis?: Record<string, any> | null;
    visual_analysis?: Record<string, any> | null; adapted_brief?: string | null;
  }>
  persona?: {
    id: number; pains: string[]; desires: string[];
    user_refinements?: Array<{ field: string; note?: string; at?: string }>;
    edit_count?: number;
  } | null
  voice_signals: string[]
  totals: { knowledge: number; inspirations: number; voice_signals: number }
}

export function AmplifierPage() {
  const { clientId } = useParams<{ clientId: string }>()
  const id = Number(clientId)
  const base = `/client/${clientId}`
  const [hub, setHub] = useState<AmplifierHub | null>(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')

  async function load() {
    setLoading(true); setErr('')
    try {
      const r = await api.amplifier.get(id) as AmplifierHub
      setHub(r)
    } catch (e: any) {
      setErr(e.message || 'Erro ao carregar Amplificador')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [id])

  return (
    <div className="p-4 md:p-6 space-y-4 max-w-5xl">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-lg md:text-xl font-bold text-white">⚡ Amplificador</h1>
          <p className="text-xs text-gray-400 mt-0.5">
            Tudo que ensina sua IA — base, inspirações, persona, voz. Quanto mais alimenta, mais afiada a IA fica.
          </p>
        </div>
        <button onClick={load} disabled={loading} className="text-xs text-violet-400 hover:text-violet-300">
          {loading ? '...' : '↻'}
        </button>
      </div>

      {err && <div className="card bg-red-900/20 border-red-800/50 text-xs text-red-300">{err}</div>}

      {hub && (
        <div className="grid grid-cols-3 gap-2">
          <Stat label="Base de conhecimento" value={hub.totals.knowledge} to={`${base}/knowledge`} />
          <Stat label="Inspirações" value={hub.totals.inspirations} to={`${base}/inspirations`} />
          <Stat label="Sinais de voz" value={hub.totals.voice_signals} />
        </div>
      )}

      {/* Voice signals — aggregated across knowledge items */}
      {hub && hub.voice_signals.length > 0 && (
        <div className="card bg-cyan-900/10 border-cyan-800/50">
          <p className="text-xs text-cyan-400 font-semibold mb-2">VOZ DO CRIADOR — palavras/expressões que a IA reutiliza</p>
          <div className="flex flex-wrap gap-1.5">
            {hub.voice_signals.slice(0, 40).map((s, i) => (
              <span key={i} className="text-xs px-2 py-0.5 rounded-md bg-cyan-900/30 border border-cyan-800/40 text-cyan-200">{s}</span>
            ))}
          </div>
        </div>
      )}

      {/* Persona refinements */}
      {hub?.persona?.user_refinements && hub.persona.user_refinements.length > 0 && (
        <div className="card bg-violet-900/10 border-violet-800/50">
          <div className="flex items-center justify-between mb-2">
            <p className="text-xs text-violet-400 font-semibold">PERSONA — ajustes manuais ({hub.persona.edit_count || 0})</p>
            <Link to={`${base}/persona`} className="text-[10px] text-violet-300 hover:underline">editar →</Link>
          </div>
          <div className="space-y-1.5">
            {hub.persona.user_refinements.slice(-5).reverse().map((r, i) => (
              <div key={i} className="text-xs text-gray-300">
                <span className="text-violet-300 font-mono">[{r.field}]</span> {r.note || '(sem nota)'}
                {r.at && <span className="text-[10px] text-gray-500 ml-2">{new Date(r.at).toLocaleDateString('pt-BR')}</span>}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Knowledge digest */}
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-white">Base de conhecimento (digerida pela IA)</h2>
          <Link to={`${base}/knowledge`} className="text-xs text-violet-400 hover:underline">Gerenciar →</Link>
        </div>
        {hub && hub.knowledge.length === 0 && (
          <p className="text-xs text-gray-500">
            Sem itens ainda — <Link to={`${base}/knowledge`} className="text-violet-400 hover:underline">adicione notas, PDFs, conceitos</Link>.
          </p>
        )}
        {hub?.knowledge.slice(0, 8).map(k => (
          <div key={k.id} className="card">
            <div className="flex items-start justify-between gap-2">
              <p className="text-sm font-semibold text-white">{k.title}</p>
              <span className="text-[10px] text-gray-500 shrink-0">
                {k.source_type}{k.use_count ? ` · usado ${k.use_count}x` : ''}
              </span>
            </div>
            {k.summary && (
              <p className="text-xs text-gray-300 mt-1.5 line-clamp-3">{k.summary}</p>
            )}
            {k.key_insights && k.key_insights.length > 0 && (
              <div className="mt-2 space-y-0.5">
                {k.key_insights.slice(0, 3).map((ins, i) => (
                  <p key={i} className="text-[11px] text-gray-400">• {ins}</p>
                ))}
              </div>
            )}
            {k.voice_signals && k.voice_signals.length > 0 && (
              <div className="flex flex-wrap gap-1 mt-2">
                {k.voice_signals.slice(0, 6).map((v, i) => (
                  <span key={i} className="text-[10px] px-1.5 py-0.5 rounded bg-cyan-900/20 text-cyan-300">{v}</span>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>

      {/* Inspirations digest */}
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-white">Inspirações visuais</h2>
          <Link to={`${base}/inspirations`} className="text-xs text-violet-400 hover:underline">Gerenciar →</Link>
        </div>
        {hub && hub.inspirations.length === 0 && (
          <p className="text-xs text-gray-500">
            Sem inspirações — <Link to={`${base}/inspirations`} className="text-violet-400 hover:underline">anexe prints, URLs ou imagens</Link>.
          </p>
        )}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
          {hub?.inspirations.slice(0, 6).map(ins => {
            const va = ins.visual_analysis || {}
            const a = ins.analysis || {}
            return (
              <div key={ins.id} className="card flex gap-3">
                {ins.image_url && (
                  <img src={ins.image_url} alt="" className="w-16 h-16 rounded object-cover shrink-0 border border-gray-800" />
                )}
                <div className="min-w-0 flex-1">
                  <p className="text-xs font-semibold text-white truncate">{ins.label || a.hook || '(sem rótulo)'}</p>
                  {va.mood && <p className="text-[10px] text-gray-400 mt-0.5">Mood: {String(va.mood)}</p>}
                  {Array.isArray(va.palette) && va.palette.length > 0 && (
                    <p className="text-[10px] text-gray-400">Paleta: {(va.palette as string[]).slice(0, 4).join(' · ')}</p>
                  )}
                  {ins.adapted_brief && (
                    <p className="text-[11px] text-violet-300 mt-1 line-clamp-2">{ins.adapted_brief}</p>
                  )}
                </div>
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}

function Stat({ label, value, to }: { label: string; value: number; to?: string }) {
  const inner = (
    <div className="card text-center hover:border-violet-700/60 transition-colors">
      <p className="text-2xl font-bold text-white">{value}</p>
      <p className="text-[10px] text-gray-500 uppercase tracking-wide mt-1">{label}</p>
    </div>
  )
  return to ? <Link to={to}>{inner}</Link> : inner
}
