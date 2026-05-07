import { useEffect, useRef } from 'react'
import type { SectionKey } from '../types'
import { SECTION_META } from '../types'

interface Props {
  sectionKey: SectionKey
  text: string
  active: boolean
  done: boolean
}

export function AgentSection({ sectionKey, text, active, done }: Props) {
  const meta = SECTION_META[sectionKey]
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (active && ref.current) {
      ref.current.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }
  }, [active])

  if (!text && !active) return null

  return (
    <div ref={ref} className={`section-card border ${meta.color.split(' ')[2]} transition-all`}>
      <div className="flex items-center justify-between mb-3">
        <span className={`section-label text-xs border ${meta.color}`}>
          {meta.label}
        </span>
        <span className="text-xs text-gray-500">{meta.agent}</span>
        {done && <span className="text-xs text-green-400">Concluído</span>}
      </div>
      <div className={`agent-output${active && !done ? ' blinking-cursor' : ''}`}>
        {text || ' '}
      </div>
    </div>
  )
}
