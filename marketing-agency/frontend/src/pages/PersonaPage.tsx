import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { api } from '../services/api'
import type { Persona } from '../types'

function Chips({ items, color }: { items: string[]; color: string }) {
  if (!items || items.length === 0) return <p className="text-xs text-gray-500">—</p>
  return (
    <div className="flex flex-wrap gap-1.5">
      {items.map((it, i) => (
        <span key={i} className={`text-xs px-2 py-1 rounded-md border ${color}`}>{it}</span>
      ))}
    </div>
  )
}

export function PersonaPage() {
  const { clientId } = useParams<{ clientId: string }>()
  const id = Number(clientId)
  const [persona, setPersona] = useState<Persona | null>(null)
  const [exists, setExists] = useState<boolean | null>(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState<string>('')

  async function load() {
    const r: any = await api.persona.get(id)
    if (r.exists) { setPersona(r as Persona); setExists(true) } else { setExists(false) }
  }

  useEffect(() => { load() }, [id])

  async function generate() {
    setLoading(true); setErr('')
    try {
      const r: any = await api.persona.generate(id)
      setPersona(r); setExists(true)
    } catch (e: any) {
      setErr(e.message || 'Erro ao gerar persona')
    } finally { setLoading(false) }
  }

  return (
    <div className="p-4 md:p-6 space-y-4 max-w-5xl">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-lg md:text-xl font-bold text-white">Persona Inteligente</h1>
          <p className="text-xs text-gray-400 mt-0.5">Análise psicológica e linguística da audiência</p>
        </div>
        <button onClick={generate} disabled={loading} className="btn-primary text-xs">
          {loading ? 'Analisando...' : exists ? '↻ Atualizar' : '✦ Gerar Persona'}
        </button>
      </div>

      {err && <div className="card bg-red-900/20 border-red-800/50 text-xs text-red-300">{err}</div>}

      {exists === false && (
        <div className="card text-center py-10">
          <p className="text-sm text-gray-300 mb-1">Persona ainda não gerada</p>
          <p className="text-xs text-gray-500 mb-4">A IA vai analisar o briefing + conteúdos publicados para mapear sua audiência real.</p>
        </div>
      )}

      {persona && (
        <>
          {persona.audience_profile && (
            <div className="card bg-violet-900/10 border-violet-800/50">
              <p className="text-xs text-violet-400 font-semibold mb-1">PERFIL</p>
              <p className="text-sm text-gray-200 whitespace-pre-line">{persona.audience_profile}</p>
            </div>
          )}

          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <div className="card">
              <h3 className="text-xs font-semibold text-red-400 mb-2">DORES</h3>
              <Chips items={persona.pains} color="bg-red-900/30 text-red-200 border-red-800/50" />
            </div>
            <div className="card">
              <h3 className="text-xs font-semibold text-green-400 mb-2">DESEJOS</h3>
              <Chips items={persona.desires} color="bg-green-900/30 text-green-200 border-green-800/50" />
            </div>
            <div className="card">
              <h3 className="text-xs font-semibold text-orange-400 mb-2">EMOÇÕES DOMINANTES</h3>
              <Chips items={persona.emotions} color="bg-orange-900/30 text-orange-200 border-orange-800/50" />
            </div>
            <div className="card">
              <h3 className="text-xs font-semibold text-yellow-400 mb-2">INSEGURANÇAS</h3>
              <Chips items={persona.insecurities} color="bg-yellow-900/30 text-yellow-200 border-yellow-800/50" />
            </div>
            <div className="card md:col-span-2">
              <h3 className="text-xs font-semibold text-blue-400 mb-2">OBJETIVOS DA AUDIÊNCIA</h3>
              <Chips items={persona.audience_goals} color="bg-blue-900/30 text-blue-200 border-blue-800/50" />
            </div>
          </div>

          {persona.language_patterns && (
            <div className="card">
              <p className="text-xs text-cyan-400 font-semibold mb-1">PADRÕES DE LINGUAGEM</p>
              <p className="text-sm text-gray-300">{persona.language_patterns}</p>
            </div>
          )}
          {persona.psychological_patterns && (
            <div className="card">
              <p className="text-xs text-pink-400 font-semibold mb-1">PADRÕES PSICOLÓGICOS</p>
              <p className="text-sm text-gray-300">{persona.psychological_patterns}</p>
            </div>
          )}
          {persona.evidence && (
            <div className="card bg-gray-900/40">
              <p className="text-xs text-gray-500 font-semibold mb-1">EVIDÊNCIAS / JUSTIFICATIVAS</p>
              <p className="text-xs text-gray-400 whitespace-pre-line">{persona.evidence}</p>
            </div>
          )}
          {persona.generated_at && (() => {
            const ageDays = Math.floor((Date.now() - new Date(persona.generated_at!).getTime()) / 86_400_000)
            const stale = ageDays > 60
            return (
              <div className={`text-[10px] flex items-center gap-2 ${stale ? 'text-yellow-300' : 'text-gray-600'}`}>
                <span>Gerada em {new Date(persona.generated_at!).toLocaleString('pt-BR')} · {ageDays}d atrás</span>
                {stale && <span className="px-1.5 py-0.5 rounded bg-yellow-900/40 border border-yellow-800/50">⚠ Pode estar desatualizada — considere regenerar</span>}
              </div>
            )
          })()}
        </>
      )}
    </div>
  )
}
