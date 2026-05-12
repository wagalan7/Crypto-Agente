interface Props {
  status: string
  phase: number
  totalPhases: number
  loading: boolean
  done: boolean
}

const PHASE_LABELS = ['', 'Estratégia', 'Copy · Design · Vídeo', 'Social · Ads · Automação', 'Publicador', 'Analytics']

export function PipelineHeader({ status, phase, totalPhases, loading, done }: Props) {
  const pct = done ? 100 : phase === 0 ? 0 : Math.round((phase / totalPhases) * 100)

  return (
    <div className="bg-gray-900/60 border border-gray-800 rounded-xl p-4">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <div className={`w-2 h-2 rounded-full ${loading ? 'bg-violet-400 animate-pulse' : done ? 'bg-emerald-400' : 'bg-gray-600'}`} />
          <span className="text-xs font-mono text-gray-300">{status || 'Aguardando produto...'}</span>
        </div>
        <span className="text-xs text-gray-500">{pct}%</span>
      </div>

      {/* Phase progress */}
      <div className="flex gap-1 mb-2">
        {PHASE_LABELS.slice(1).map((label, i) => {
          const p = i + 1
          const active = phase === p
          const complete = phase > p || done
          return (
            <div key={p} className="flex-1">
              <div className={`h-1 rounded-full transition-all duration-500
                ${complete ? 'bg-emerald-500' : active ? 'progress-bar' : 'bg-gray-800'}`}
              />
              <div className={`mt-1 text-[9px] text-center truncate transition-colors
                ${active ? 'text-violet-400' : complete ? 'text-emerald-500' : 'text-gray-700'}`}>
                {label}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
