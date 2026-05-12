import { useState } from 'react'
import type { AgentName, AgentState } from '../types'
import { AGENTS, STATUS_COLOR, STATUS_LABEL } from '../types'
import { Robot } from './Robot'

interface Props {
  name: AgentName
  state: AgentState
}

export function AgentCard({ name, state }: Props) {
  const meta = AGENTS[name]
  const { status, task, progress, logs, output } = state
  const [expanded, setExpanded] = useState(false)

  const isActive = status === 'thinking' || status === 'generating'
  const isDone   = status === 'completed'
  const isIdle   = status === 'idle'

  const glowColor =
    status === 'thinking'   ? '#60a5fa' :
    status === 'generating' ? '#a855f7' :
    status === 'completed'  ? '#34d399' :
    status === 'error'      ? '#f87171' : '#374151'

  return (
    <div
      className={`relative rounded-xl border overflow-hidden transition-all duration-300
        ${isActive ? 'border-violet-500/50 bg-gray-900/90 agent-active' : ''}
        ${isDone   ? 'border-emerald-500/30 bg-gray-900/70' : ''}
        ${isIdle   ? 'border-gray-800/60 bg-gray-900/30' : ''}
        ${status === 'error' ? 'border-red-500/40 bg-gray-900/70' : ''}
      `}
      style={isActive ? { boxShadow: `0 0 16px ${glowColor}22` } : {}}
    >
      {/* Top color bar */}
      <div className={`h-0.5 bg-gradient-to-r ${meta.color} transition-opacity duration-500 ${isIdle ? 'opacity-15' : 'opacity-100'}`} />

      <div className="p-3">
        {/* Header row */}
        <div className="flex items-center justify-between mb-2">
          <div className="flex items-center gap-1.5">
            <span className={`text-base bg-gradient-to-br ${meta.color} bg-clip-text text-transparent font-bold`}>
              {meta.icon}
            </span>
            <span className="text-[11px] font-bold tracking-widest text-gray-300 uppercase">{meta.label}</span>
          </div>
          <div className="flex items-center gap-1.5">
            {isActive && (
              <div className="flex gap-0.5 items-center h-3">
                <div className="w-1 h-1 rounded-full bg-violet-400 dot-1" />
                <div className="w-1 h-1 rounded-full bg-violet-400 dot-2" />
                <div className="w-1 h-1 rounded-full bg-violet-400 dot-3" />
              </div>
            )}
            <span className={`text-[10px] font-medium ${STATUS_COLOR[status]}`}>
              {STATUS_LABEL[status]}
            </span>
          </div>
        </div>

        {/* Robot + content */}
        <div className="flex gap-3 items-start">
          {/* Robot */}
          <div className={`shrink-0 w-14 h-20 transition-opacity duration-300 ${isIdle ? 'opacity-30' : 'opacity-100'}`}>
            <Robot status={status} accentColor={glowColor} />
          </div>

          {/* Info panel */}
          <div className="flex-1 min-w-0">
            {/* Task */}
            {!isIdle && (
              <p className="text-[10px] text-gray-400 mb-1.5 truncate">{task}</p>
            )}

            {/* Progress bar */}
            {!isIdle && (
              <div className="h-1 bg-gray-800 rounded-full overflow-hidden mb-2">
                <div
                  className={`h-full rounded-full transition-all duration-700 ${isDone ? 'bg-emerald-500' : status === 'error' ? 'bg-red-500' : 'progress-bar'}`}
                  style={{ width: `${progress}%` }}
                />
              </div>
            )}

            {/* Logs */}
            {logs.length > 0 && (
              <div className="space-y-0.5">
                {logs.slice(-3).map((log, i) => (
                  <div key={i} className="log-line flex items-center gap-1">
                    <span className="text-[8px] text-violet-500 shrink-0">▸</span>
                    <span className="text-[10px] text-gray-500 truncate">{log}</span>
                  </div>
                ))}
              </div>
            )}

            {/* Idle state */}
            {isIdle && (
              <p className="text-[10px] text-gray-700 mt-2">aguardando...</p>
            )}
          </div>
        </div>

        {/* Output (expandable) */}
        {isDone && output && (
          <div className="mt-3 border-t border-gray-800 pt-3">
            <div className={`overflow-hidden transition-all duration-300 ${expanded ? 'max-h-96' : 'max-h-10'}`}>
              <div className="agent-output">{output}</div>
            </div>
            <button
              className="mt-1 text-[10px] text-violet-400 hover:text-violet-300 transition-colors"
              onClick={() => setExpanded(e => !e)}
            >
              {expanded ? '▲ recolher' : '▼ ver resultado'}
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
