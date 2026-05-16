import { useState } from 'react'
import { api } from '../services/api'
import type { ContentPiece } from '../types'

interface Props {
  contentId: number
  section: 'hook' | 'script' | 'copy' | 'design_brief'
  onUpdated: (c: ContentPiece) => void
}

const PRESETS: Record<string, string[]> = {
  hook: ['Mais provocador', 'Mais curto', 'Comece com pergunta', 'Vulnerabilidade'],
  script: ['Mais vulnerável', 'Mais autoridade', 'Adicione mais tensão', 'Mais curto'],
  copy: ['CTA mais agressivo', 'Mais leve', 'Conte uma micro-história', 'Adicione objeção+resposta'],
  design_brief: ['Mais minimalista', 'Cores quentes', 'Estilo editorial', 'Mais textura'],
}

export function SectionRegenButton({ contentId, section, onUpdated }: Props) {
  const [open, setOpen] = useState(false)
  const [instruction, setInstruction] = useState('')
  const [loading, setLoading] = useState(false)

  async function run(steer?: string) {
    setLoading(true)
    try {
      const updated: any = await api.content.regenerateSection(contentId, section, steer || instruction || undefined)
      onUpdated(updated)
      setOpen(false)
      setInstruction('')
    } catch (e: any) {
      alert('Erro: ' + (e?.message?.slice(0, 200) || 'falha ao regenerar'))
    } finally {
      setLoading(false)
    }
  }

  if (!open) {
    return (
      <button onClick={() => setOpen(true)} className="text-[10px] text-violet-400 hover:text-violet-300 px-1.5 py-0.5 rounded">
        ✎ Regenerar
      </button>
    )
  }

  return (
    <div className="mt-2 bg-violet-950/40 border border-violet-800/50 rounded p-2 space-y-1.5">
      <div className="flex flex-wrap gap-1">
        {PRESETS[section]?.map(p => (
          <button key={p} onClick={() => run(p)} disabled={loading}
            className="text-[10px] px-2 py-0.5 rounded bg-violet-900/40 border border-violet-700/40 text-violet-200 hover:bg-violet-800/60 disabled:opacity-50">
            {p}
          </button>
        ))}
      </div>
      <div className="flex gap-1">
        <input
          type="text"
          value={instruction}
          onChange={e => setInstruction(e.target.value)}
          placeholder="Ou descreva: ex 'tom mais íntimo'"
          className="input-field text-[11px] py-1 flex-1"
          disabled={loading}
        />
        <button onClick={() => run()} disabled={loading} className="text-[11px] px-2 py-1 rounded bg-violet-700 hover:bg-violet-600 text-white disabled:opacity-50">
          {loading ? '...' : 'Gerar'}
        </button>
        <button onClick={() => { setOpen(false); setInstruction('') }} className="text-[11px] px-2 py-1 text-gray-400">×</button>
      </div>
    </div>
  )
}
