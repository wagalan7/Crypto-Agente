import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { calibratedProbabilities, recommendationPresentation } from '../../src/lib/recommendationPresentation'
import RecommendationExecutionSummary from '../../src/components/RecommendationExecutionSummary'
import type { Recommendation } from '../../src/types'

declare const __QA_SRC__: string
const NOW = 1_790_000_000_000
const base = { origin: 'bot', bot_verdict: { ok: true, blocked_by: null, reason: null } } as const

test('tier/entry_grade e qualidade parcial nunca viram autorização', () => {
  for (const entry_grade of ['good', 'manual', 'avoid', null] as const) {
    const view = recommendationPresentation({ ...base, entry_grade })
    assert.equal(view.executionConfirmed, false)
    assert.equal(view.source, 'RECOMENDAÇÃO · PRD')
    assert.equal(view.quality, 'Qualidade parcial atendida')
    assert.equal(view.status, 'Aguardando avaliação de execução.')
  }
})

test('qualidade ausente, nula ou bool inválido não aparece aprovada', () => {
  for (const bot_verdict of [undefined, null, { ok: 'true' }, { ok: 1 }]) {
    const view = recommendationPresentation({ origin: 'bot', bot_verdict } as Recommendation)
    assert.equal(view.quality, 'Qualidade não confirmada')
    assert.equal(view.executionConfirmed, false)
  }
})

test('bloqueio recente aparece como último registro com escopo e motivo visível', () => {
  const skip = { gate: 'proximity', reason: 'Preço esticado; aguardar aproximação da zona.', ts: new Date(NOW - 60_000).toISOString() }
  const view = recommendationPresentation(base, skip, NOW)
  assert.equal(view.blocked?.scope, 'LAST_EXECUTION')
  const html = renderToStaticMarkup(createElement(RecommendationExecutionSummary, { recommendation: base, skip, nowMs: NOW }))
  assert.ok(html.includes('Último bloqueio registrado'))
  assert.ok(html.includes(skip.reason), 'motivo não fica escondido só em tooltip')
  assert.ok(html.includes('não confirmação deste timeframe'))
  assert.ok(html.includes('data-execution-confirmed="false"'))
  assert.ok(!html.includes('<button'), 'resumo não libera nem executa nada')
})

test('registro velho/futuro/inválido não representa bloqueio atual nem libera entrada', () => {
  for (const ts of [new Date(NOW - 20 * 60_000 - 1).toISOString(), new Date(NOW + 1).toISOString(), 'invalid']) {
    const view = recommendationPresentation(base, { gate: 'old', reason: 'OLD', ts }, NOW)
    assert.equal(view.blocked, null)
    assert.equal(view.executionConfirmed, false)
    assert.equal(view.status, 'Aguardando avaliação de execução.')
  }
})

test('falha de qualidade é separada de recusa de execução', () => {
  const view = recommendationPresentation({ bot_verdict: { ok: false, blocked_by: 'rr-gate', reason: 'R:R insuficiente' } })
  assert.equal(view.blocked?.scope, 'QUALITY_ONLY')
  assert.equal(view.blocked?.reason, 'R:R insuficiente')
  assert.equal(view.quality, 'Critério de qualidade não atendido')
})

test('observação mantém origem sem utilizar skip do PRD como autoridade', () => {
  const view = recommendationPresentation({ ...base, origin: 'observation' }, {
    gate: 'balance', reason: 'saldo', ts: new Date(NOW).toISOString(),
  }, NOW)
  assert.equal(view.source, 'OBSERVAÇÃO · TESTES')
  assert.equal(view.blocked, null)
  assert.equal(view.status, 'Somente observação — sem execução neste ambiente.')
})

test('consumidor real não promete entrada pelo tier/grade nem inventa TP1 ausente', () => {
  const source = readFileSync(join(__QA_SRC__, 'components/RecommendationsPanel.tsx'), 'utf8')
  assert.ok(source.includes('<RecommendationExecutionSummary recommendation={r} skip={skip} />'))
  for (const label of ['Bom pra entrar', 'Dá pra entrar', "'🤖 BOT OPERA'", 'Só aprovados pelo bot']) assert.ok(!source.includes(label), label)
  assert.ok(!source.includes("r.tier === 'A+' || r.tier === 'A') ? 'good'"))
  assert.ok(!source.includes('fmt(r.signal?.tp1 ?? r.entry)'))
})

test('scanner informa viés sem mudar chaves/fórmulas de filtro e ordenação', () => {
  const source = readFileSync(join(__QA_SRC__, 'App.tsx'), 'utf8')
  assert.ok(source.includes('Viés não é ordem.'))
  assert.ok(source.includes('VIÉS DE ALTA'))
  assert.ok(source.includes('VIÉS DE BAIXA'))
  assert.match(source, /case 'rr':\s+return b.confidence - a.confidence/)
  assert.match(source, /if \(filter === 'long'\) return a.direction === 'long'/)
  assert.match(source, /if \(filter === 'short'\) return a.direction === 'short'/)
  assert.ok(source.includes('aria-label="Ordenar pares"'))
})

test('histórico só é apresentado como calibrado com READY e valores finitos na unidade certa', () => {
  const ready = { status: 'READY' }
  assert.deepEqual(calibratedProbabilities({ probability_provenance: ready, prob_tp1: 0, prob_tp2: 0 }), { tp1: 0, tp2: 0 })
  assert.deepEqual(calibratedProbabilities({ probability_provenance: ready, prob_tp1: 0.7, prob_tp2: 0.5 }), { tp1: 0.7, tp2: 0.5 })
  assert.equal(calibratedProbabilities({ prob_tp1: 0.8 }), null)
  assert.equal(calibratedProbabilities({ probability_provenance: { status: 'INSUFFICIENT_BIN_EVIDENCE' }, prob_tp1: 0.8 }), null)
  for (const prob_tp1 of [NaN, Infinity, -1, 1.01, true, '0.7']) {
    assert.equal(calibratedProbabilities({ probability_provenance: ready, prob_tp1 } as Recommendation), null)
  }
  assert.equal(calibratedProbabilities({ probability_provenance: ready, prob_tp1: 0.6, prob_tp2: 0.7 })?.tp2, null)
})
