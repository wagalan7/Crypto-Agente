import type { Recommendation } from '../types'

export interface KnownSkip { gate: string; reason: string; ts: string }

export function calibratedProbabilities(recommendation: Pick<Recommendation, 'probability_provenance' | 'prob_tp1' | 'prob_tp2'>) {
  const valid = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1
  if (recommendation.probability_provenance?.status !== 'READY' || !valid(recommendation.prob_tp1)) return null
  return {
    tp1: recommendation.prob_tp1,
    tp2: valid(recommendation.prob_tp2) && recommendation.prob_tp2 <= recommendation.prob_tp1 ? recommendation.prob_tp2 : null,
  }
}

export function recommendationPresentation(
  recommendation: Pick<Recommendation, 'origin' | 'bot_verdict' | 'entry_grade'>,
  skip?: KnownSkip,
  nowMs = Date.now(),
) {
  const observation = recommendation.origin === 'observation'
  const timestamp = skip ? Date.parse(skip.ts) : NaN
  const recentSkip = !observation && skip && Number.isFinite(timestamp)
    && nowMs >= timestamp && nowMs - timestamp <= 20 * 60_000 ? skip : undefined
  const verdict = recommendation.bot_verdict
  const qualityBlocked = verdict?.ok === false
  const blocked = recentSkip
    ? { gate: recentSkip.gate, reason: recentSkip.reason, ts: recentSkip.ts, scope: 'LAST_EXECUTION' as const }
    : qualityBlocked
      ? { gate: verdict.blocked_by ?? 'qualidade', reason: verdict.reason ?? 'Critério de qualidade não atendido.', ts: null, scope: 'QUALITY_ONLY' as const }
      : null
  return {
    source: observation ? 'OBSERVAÇÃO · TESTES' : 'RECOMENDAÇÃO · PRD',
    observation,
    blocked,
    quality: qualityBlocked ? 'Critério de qualidade não atendido'
      : verdict?.ok === true ? 'Qualidade parcial atendida'
        : 'Qualidade não confirmada',
    executionConfirmed: false as const,
    status: observation ? 'Somente observação — sem execução neste ambiente.'
      : blocked ? 'Bloqueio conhecido — não é uma entrada liberada.'
        : 'Aguardando avaliação de execução.',
    note: 'Tier e qualidade do setup não confirmam preço atual, saldo, proteção ou autorização do bot.',
  }
}
