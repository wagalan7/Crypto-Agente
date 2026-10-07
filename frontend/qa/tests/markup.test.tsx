// QA Lote 04 — MARKUP dos consumidores (não só a função pura).
// Renderiza os cartões reais com react-dom/server e confere rótulo honesto,
// ausência de botão perigoso e paridade numérica. Fixtures sintéticas.
import test from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import {
  OperationalStateCard, P03IncidentCard, PositionsInventoryCard, SourceResultCard,
} from '../../src/components/status/OperationalCards'
import {
  deriveOperationalState, p03View, readingQuality, emptyReading,
  type P03Facts, type RiskFacts, type SourceReading,
} from '../../src/lib/operationalState'

const NOW = 1_770_000_000_000
const read = <T,>(value: T | null, over: Partial<SourceReading<T>> = {}): SourceReading<T> =>
  ({ state: 'OK', value, lastOkAtMs: NOW - 2_000, errorReason: null, ...over })

const riskOk = (over: Partial<RiskFacts> = {}): RiskFacts =>
  ({ enabled: true, trading_paused: false, pause_manual: false, pause_reason: null, ...over })
const p03Ok = (over: Partial<P03Facts> = {}): P03Facts =>
  ({ ok: true, quarantine_active: false, open_total: 0, manual_required: 0, ...over })

/** Palavras que NUNCA podem aparecer como ação na tela de estado. */
const ACOES_PROIBIDAS = [
  'enable-live', 'promote', 'execute', 'retry-now', 'clear-pause',
  'Retomar agora', 'Reconhecer posição', 'Ativar candidata',
]

function assertSemAcaoPerigosa(html: string) {
  for (const termo of ACOES_PROIBIDAS) {
    assert.ok(!html.includes(termo), `markup não pode oferecer "${termo}"`)
  }
  // Nenhum <button> além dos de diagnóstico/revelação.
  const botoes = html.match(/<button[^>]*>([\s\S]*?)<\/button>/g) ?? []
  for (const b of botoes) {
    assert.ok(/diagn|Ver /i.test(b), `botão inesperado no cartão de estado: ${b.slice(0, 80)}`)
  }
}

test('estado sem confirmação não mostra verde nem promete operação', () => {
  const state = deriveOperationalState({
    risk: { state: 'ERROR', value: null, lastOkAtMs: null, errorReason: 'network' },
    p03: emptyReading<P03Facts>(), nowMs: NOW,
  })
  const html = renderToStaticMarkup(createElement(OperationalStateCard, { state, ageMs: null }))
  assert.ok(html.includes('data-level="UNAVAILABLE"'))
  assert.ok(html.includes('Sem confirmação nesta leitura'))
  assert.ok(html.includes('Nenhuma leitura confirmada ainda'))
  assert.ok(!/Operando|Disponível para operar/.test(html))
  assertSemAcaoPerigosa(html)
})

test('estado bloqueado por P03 mostra causa e só leva ao diagnóstico', () => {
  const state = deriveOperationalState({
    risk: read(riskOk({ trading_paused: true, pause_reason: 'P03-QUARANTINE:UNKNOWN_NO_SL' })),
    p03: read(p03Ok({ open_total: 1, manual_required: 1 })), nowMs: NOW,
  })
  const html = renderToStaticMarkup(createElement(OperationalStateCard, {
    state, ageMs: 2_000, onOpenDiagnosis: () => {}, diagnosisLabel: 'Ver diagnóstico do bloqueio',
  }))
  assert.ok(html.includes('data-level="BLOCKED"'))
  assert.ok(html.includes('Incidente de execução aberto (reconciliador P03)'))
  assert.ok(html.includes('1 incidente exige conferência manual'))
  assert.ok(html.includes('Ver diagnóstico do bloqueio'))
  assert.ok(html.includes('Última leitura confirmada há 2s'))
  assertSemAcaoPerigosa(html)
})

test('leitura antiga é rotulada como anterior, sem autorizar nada', () => {
  const state = deriveOperationalState({
    risk: read(riskOk(), { state: 'ERROR', errorReason: 'HTTP 500', lastOkAtMs: NOW - 120_000 }),
    p03: read(p03Ok()), nowMs: NOW,
  })
  const html = renderToStaticMarkup(createElement(OperationalStateCard, { state, ageMs: 120_000 }))
  assert.ok(html.includes('data-level="STALE"'))
  assert.ok(html.includes('dado de leitura anterior'))
  assert.ok(html.includes('há 2min'))
})

test('P03 sem leitura mostra "não confirmado", nunca zero', () => {
  const html = renderToStaticMarkup(createElement(P03IncidentCard, {
    view: p03View(emptyReading<P03Facts>(), NOW), ageMs: null,
  }))
  assert.ok(html.includes('data-confirmed="false"'))
  assert.ok(html.includes('não confirmado'))
  assert.ok(html.includes('Ausência de'))
  assert.ok(!/>0</.test(html), 'não pode exibir contador 0 sem confirmação')
})

test('P03 confirmado com zero incidentes mostra zero de verdade', () => {
  const html = renderToStaticMarkup(createElement(P03IncidentCard, {
    view: p03View(read(p03Ok()), NOW), ageMs: 1_000,
  }))
  assert.ok(html.includes('data-confirmed="true"'))
  assert.ok(html.includes('data-blocked="false"'))
  assert.ok(html.includes('>0<'))
  assert.ok(html.includes('desligada'))
})

test('resultado dos setups não se apresenta como lucro da conta', () => {
  const html = renderToStaticMarkup(createElement(SourceResultCard, {
    source: 'SETUPS', window: 'hoje (UTC)', primary: '+1.25', unit: '% da banca (setups)',
    secondary: '3 trade(s) resolvido(s)', quality: readingQuality(read({}), NOW, 60_000),
    positive: true,
    note: 'Resultado dos SETUPS recomendados, não lucro líquido da conta.',
  }))
  assert.ok(html.includes('Resultado dos setups'))
  assert.ok(html.includes('hoje (UTC)'))
  assert.ok(html.includes('% da banca (setups)'))
  assert.ok(html.includes('não lucro líquido da conta'))
  assert.ok(!/lucro l[íi]quido da conta:/i.test(html))
})

test('fonte sem leitura mostra indisponível, não zero', () => {
  const html = renderToStaticMarkup(createElement(SourceResultCard, {
    source: 'MANUAL_EXTERNO', window: 'agora', primary: null,
    quality: readingQuality(emptyReading(), NOW, 60_000),
    note: 'Inventário da conta não é publicado por este contrato.',
    unavailableText: 'Posições externas não disponíveis nesta leitura',
  }))
  assert.ok(html.includes('Posições externas não disponíveis nesta leitura'))
  assert.ok(!/0,00|\+0|R\$ ?0/.test(html))
  assert.ok(html.includes('data-quality="MISSING"'))
})

test('posições: erro de leitura não passa como lista vazia', () => {
  const erro = renderToStaticMarkup(createElement(PositionsInventoryCard, {
    quality: readingQuality(emptyReading(), NOW, 60_000), botCount: null, managedManualCount: null,
  }))
  assert.ok(erro.includes('não é lista vazia'))
  assert.ok(erro.includes('data-quality="MISSING"'))

  const vazia = renderToStaticMarkup(createElement(PositionsInventoryCard, {
    quality: readingQuality(read([]), NOW, 60_000), botCount: 0, managedManualCount: 0,
  }))
  assert.ok(vazia.includes('data-quality="CONFIRMED"'))
  assert.ok(vazia.includes('>0<'))
  assert.ok(vazia.includes('Zero registros do bot não significa zero posições na conta'))
})

test('paridade numérica: o cartão não recalcula nem arredonda o que recebe', () => {
  const html = renderToStaticMarkup(createElement(SourceResultCard, {
    source: 'REAL_BOT', window: '30 dias', primary: '-12.3456', unit: 'R',
    quality: readingQuality(read({}), NOW, 60_000), positive: false,
    note: 'Fonte declarada.',
  }))
  assert.ok(html.includes('-12.3456'), 'o valor formatado pelo chamador vai cru para a tela')
})
