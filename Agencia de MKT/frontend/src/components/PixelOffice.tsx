import type { AgentName, AgentState } from '../types'
import { AGENTS, STATUS_LABEL, STATUS_COLOR } from '../types'
import { PixelCharacter } from './PixelCharacter'
import { useState } from 'react'

interface Props {
  agents: Record<AgentName, AgentState>
}

const CHAR_STYLES: Record<AgentName, { shirt: string; hair: string }> = {
  ESTRATEGIA: { shirt: '#7c3aed', hair: '#1e1b4b' },
  COPY:        { shirt: '#2563eb', hair: '#1e3a5f' },
  DESIGN:      { shirt: '#db2777', hair: '#500724' },
  VIDEO:       { shirt: '#dc2626', hair: '#450a0a' },
  SOCIAL:      { shirt: '#0891b2', hair: '#083344' },
  ADS:         { shirt: '#d97706', hair: '#451a03' },
  AUTOMACAO:   { shirt: '#ea580c', hair: '#431407' },
  PUBLICADOR:  { shirt: '#16a34a', hair: '#052e16' },
  ANALYTICS:   { shirt: '#0d9488', hair: '#042f2e' },
  REVISOR:     { shirt: '#65a30d', hair: '#1a2e05' },
}

const SCREEN_CONTENT: Record<string, string> = {
  idle:       'bg-gray-900',
  thinking:   'bg-blue-950',
  generating: 'bg-violet-950',
  publishing: 'bg-orange-950',
  completed:  'bg-emerald-950',
  error:      'bg-red-950',
}

function WorkStation({ name, state }: { name: AgentName; state: AgentState }) {
  const meta  = AGENTS[name]
  const chars = CHAR_STYLES[name]
  const [expanded, setExpanded] = useState(false)

  const isActive = state.status === 'thinking' || state.status === 'generating'
  const isDone   = state.status === 'completed'
  const isIdle   = state.status === 'idle'

  return (
    <div className="flex flex-col items-center gap-1 select-none">
      {/* Desk + character container */}
      <div className="relative w-28">
        {/* Active glow */}
        {isActive && (
          <div className="absolute inset-0 rounded-xl blur-md opacity-40 -z-10"
            style={{ background: `radial-gradient(circle, ${chars.shirt}, transparent)` }} />
        )}

        {/* Monitor */}
        <div className="relative mx-auto w-20 h-14 rounded-t-lg border-2 border-gray-600 bg-gray-800 overflow-hidden mb-0.5">
          {/* Screen */}
          <div className={`absolute inset-1 rounded ${SCREEN_CONTENT[state.status]} transition-colors duration-500`}>
            {/* Screen content */}
            {state.status === 'generating' && (
              <div className="p-1 space-y-0.5 overflow-hidden h-full">
                {[...Array(4)].map((_, i) => (
                  <div key={i} className="h-1 rounded-full bg-violet-500/60"
                    style={{ width: `${[80,60,90,50][i]}%`, animation: `chest-scroll ${0.6 + i * 0.15}s linear infinite` }} />
                ))}
              </div>
            )}
            {state.status === 'thinking' && (
              <div className="flex items-center justify-center h-full">
                <span className="text-blue-300 text-xs font-mono" style={{ animation: 'pc-think 1s ease-in-out infinite' }}>?</span>
              </div>
            )}
            {isDone && (
              <div className="flex items-center justify-center h-full">
                <span className="text-emerald-400 text-sm">✓</span>
              </div>
            )}
            {state.status === 'error' && (
              <div className="flex items-center justify-center h-full">
                <span className="text-red-400 text-xs">!</span>
              </div>
            )}
            {isIdle && (
              <div className="flex items-center justify-center h-full opacity-20">
                <div className="w-8 h-0.5 bg-gray-500 rounded" />
              </div>
            )}
          </div>
          {/* Monitor glow when active */}
          {isActive && (
            <div className="absolute inset-0 rounded pointer-events-none"
              style={{ boxShadow: `0 0 8px ${chars.shirt}88 inset` }} />
          )}
        </div>
        {/* Monitor stand */}
        <div className="mx-auto w-3 h-2 bg-gray-600 rounded-b" />

        {/* Desk */}
        <div className="w-full h-5 rounded-t-sm"
          style={{ background: 'linear-gradient(180deg, #92400e 0%, #78350f 100%)', border: '1px solid #b45309' }} />
        <div className="w-full h-2 rounded-b"
          style={{ background: '#451a03', border: '1px solid #78350f' }} />

        {/* Character */}
        <div className="absolute bottom-7 left-1/2 -translate-x-1/2 w-12 h-16">
          <PixelCharacter status={state.status} shirtColor={chars.shirt} hairColor={chars.hair} />
        </div>

        {/* Chair */}
        <div className="relative z-10 mx-auto mt-0.5 w-16 h-3 rounded-full"
          style={{ background: '#374151', border: '1px solid #4b5563' }} />
        <div className="mx-auto w-2 h-4" style={{ background: '#374151' }} />
        <div className="mx-auto w-10 h-2 rounded-full" style={{ background: '#1f2937' }} />
      </div>

      {/* Name tag */}
      <div className="flex flex-col items-center gap-0.5 w-full px-1">
        <div className={`flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-bold
          bg-gray-900/80 border ${isActive ? 'border-violet-500/50' : isDone ? 'border-emerald-500/30' : 'border-gray-800'}`}>
          <span className={`w-1.5 h-1.5 rounded-full ${
            isActive ? 'bg-violet-400 animate-pulse' :
            isDone   ? 'bg-emerald-400' :
            state.status === 'error' ? 'bg-red-400' : 'bg-gray-600'}`} />
          <span className="text-gray-300">{meta.label}</span>
        </div>
        {!isIdle && (
          <p className={`text-[9px] text-center truncate w-full px-1 ${STATUS_COLOR[state.status]}`}>
            {state.status === 'completed' ? '✓ concluído' : state.task || STATUS_LABEL[state.status]}
          </p>
        )}
        {/* Progress mini bar */}
        {!isIdle && state.progress > 0 && state.progress < 100 && (
          <div className="w-full h-0.5 bg-gray-800 rounded-full overflow-hidden">
            <div className="h-full rounded-full progress-bar" style={{ width: `${state.progress}%` }} />
          </div>
        )}
      </div>

      {/* Output expand (done only) */}
      {isDone && state.output && (
        <div className="w-full">
          <button
            onClick={() => setExpanded(e => !e)}
            className="w-full text-[9px] text-violet-400 hover:text-violet-300 bg-gray-900/60 border border-gray-800 rounded px-2 py-0.5 transition-colors"
          >
            {expanded ? '▲ fechar' : '▼ resultado'}
          </button>
          {expanded && (
            <div className="mt-1 p-2 bg-gray-900/80 border border-gray-800 rounded-lg max-h-48 overflow-y-auto w-56 absolute z-20 agent-output text-[10px]">
              {state.output}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

const PHASES: { label: string; agents: AgentName[] }[] = [
  { label: 'Fase 1 — Estratégia',              agents: ['ESTRATEGIA'] },
  { label: 'Fase 2 — Copy · Design · Vídeo',   agents: ['COPY', 'DESIGN', 'VIDEO'] },
  { label: 'Fase 3 — Social · Ads · Automação', agents: ['SOCIAL', 'ADS', 'AUTOMACAO'] },
  { label: 'Fase 4 — Publicador',               agents: ['PUBLICADOR'] },
  { label: 'Fase 5 — Analytics',                agents: ['ANALYTICS'] },
  { label: 'Fase 6 — Revisão Final',            agents: ['REVISOR'] },
]

export function PixelOffice({ agents }: Props) {
  const totalDone = Object.values(agents).filter(a => a.status === 'completed').length
  const totalActive = Object.values(agents).filter(a => a.status === 'thinking' || a.status === 'generating').length

  return (
    <div className="rounded-2xl border border-gray-800 overflow-hidden"
      style={{ background: 'linear-gradient(180deg, #0f0f1a 0%, #0a0a14 100%)' }}>

      {/* Office roof / header */}
      <div className="px-4 py-3 border-b border-gray-800/60 flex items-center justify-between"
        style={{ background: 'linear-gradient(90deg, #1a0a2e 0%, #0a1628 100%)' }}>
        <div className="flex items-center gap-2">
          <div className="flex gap-1">
            <div className="w-2.5 h-2.5 rounded-full bg-red-500/80" />
            <div className="w-2.5 h-2.5 rounded-full bg-yellow-500/80" />
            <div className="w-2.5 h-2.5 rounded-full bg-emerald-500/80" />
          </div>
          <span className="text-[11px] text-gray-400 font-mono">agencia.mkt — pipeline ativo</span>
        </div>
        <div className="flex items-center gap-3 text-[10px]">
          {totalActive > 0 && (
            <span className="text-violet-400 flex items-center gap-1">
              <span className="w-1.5 h-1.5 rounded-full bg-violet-400 animate-pulse inline-block" />
              {totalActive} trabalhando
            </span>
          )}
          <span className="text-gray-500">{totalDone}/9 concluídos</span>
        </div>
      </div>

      {/* Office floor */}
      <div className="p-5 space-y-8"
        style={{
          backgroundImage: 'repeating-linear-gradient(0deg,transparent,transparent 39px,#1a1a2e 39px,#1a1a2e 40px), repeating-linear-gradient(90deg,transparent,transparent 39px,#1a1a2e 39px,#1a1a2e 40px)',
          backgroundSize: '40px 40px',
        }}>

        {PHASES.map(({ label, agents: names }) => {
          const phaseActive = names.some(n => agents[n].status === 'thinking' || agents[n].status === 'generating')
          const phaseDone   = names.every(n => agents[n].status === 'completed')

          return (
            <div key={label}>
              {/* Phase label */}
              <div className="flex items-center gap-2 mb-4">
                <div className={`h-px flex-1 ${phaseActive ? 'bg-violet-500/40' : phaseDone ? 'bg-emerald-500/30' : 'bg-gray-800'}`} />
                <span className={`text-[9px] font-bold tracking-widest uppercase px-2 ${
                  phaseActive ? 'text-violet-400' : phaseDone ? 'text-emerald-400' : 'text-gray-700'}`}>
                  {label}
                </span>
                <div className={`h-px flex-1 ${phaseActive ? 'bg-violet-500/40' : phaseDone ? 'bg-emerald-500/30' : 'bg-gray-800'}`} />
              </div>

              {/* Workstations row */}
              <div className={`flex gap-4 justify-center flex-wrap`}>
                {names.map(name => (
                  <div key={name} className="relative">
                    <WorkStation name={name} state={agents[name]} />
                  </div>
                ))}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
