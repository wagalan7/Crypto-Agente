import { recommendationPresentation, type KnownSkip } from '../lib/recommendationPresentation'
import type { Recommendation } from '../types'

export default function RecommendationExecutionSummary({ recommendation, skip, nowMs }: {
  recommendation: Pick<Recommendation, 'origin' | 'bot_verdict' | 'entry_grade'>
  skip?: KnownSkip
  nowMs?: number
}) {
  const view = recommendationPresentation(recommendation, skip, nowMs)
  return (
    <div data-execution-confirmed="false" className={`mt-2 rounded-lg border px-3 py-2 text-xs leading-relaxed ${
      view.blocked ? 'border-amber-500/30 bg-amber-500/5' : 'border-slate-700/70 bg-slate-950/40'
    }`}>
      <p className={view.blocked ? 'font-semibold text-amber-200' : 'font-semibold text-slate-200'}>{view.status}</p>
      {view.blocked && (
        <p className="mt-1 break-words text-slate-300">
          <span className="text-amber-300">{view.blocked.scope === 'LAST_EXECUTION' ? 'Último bloqueio registrado' : 'Qualidade do setup'}: </span>
          {view.blocked.reason || view.blocked.gate}
          {view.blocked.ts && <span className="ml-1 text-slate-400">({new Date(view.blocked.ts).toLocaleTimeString('pt-BR')}; símbolo e direção, não confirmação deste timeframe)</span>}
        </p>
      )}
      <p className="mt-1 text-slate-400">{view.note}</p>
    </div>
  )
}
