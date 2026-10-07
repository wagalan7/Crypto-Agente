// QA Lote 04 — função PURA de estado operacional.
// Fixtures SINTÉTICAS e rotuladas: nenhum número de produção, nenhuma conta.
import test from 'node:test'
import assert from 'node:assert/strict'

import {
  deriveOperationalState, canOfferResume, p03View, readingQuality, translateReason,
  sanitizeTechnical, finiteNumber, literalBool, countOf, formatAge,
  emptyReading, UNKNOWN_REASON, DEFAULT_STALE_AFTER_MS,
  type P03Facts, type RiskFacts, type SourceReading,
} from '../../src/lib/operationalState'

const NOW = 1_770_000_000_000

function reading<T>(value: T | null, over: Partial<SourceReading<T>> = {}): SourceReading<T> {
  return { state: 'OK', value, lastOkAtMs: NOW - 1_000, errorReason: null, ...over }
}
const riskOk = (over: Partial<RiskFacts> = {}): RiskFacts => ({
  enabled: true, trading_paused: false, pause_reason: null, pause_manual: false,
  daily_dd_pct: -0.5, weekly_dd_pct: -1.25, daily_limit_pct: -3, weekly_limit_pct: -6,
  ...over,
})
const p03Ok = (over: Partial<P03Facts> = {}): P03Facts => ({
  ok: true, quarantine_active: false, open_total: 0, manual_required: 0,
  last_reconciliation_at: '2026-10-07T00:00:00Z', ...over,
})

const state = (risk: SourceReading<RiskFacts>, p03: SourceReading<P03Facts>, extra = {}) =>
  deriveOperationalState({ risk, p03, nowMs: NOW, ...extra })

// ─── Primitivos: ausência nunca vira zero/false ──────────────────────────────

test('primitivos: só valor literal passa', () => {
  assert.equal(finiteNumber(0), 0)
  assert.equal(finiteNumber(NaN), null)
  assert.equal(finiteNumber(Infinity), null)
  assert.equal(finiteNumber('3'), null)
  assert.equal(finiteNumber(null), null)
  assert.equal(literalBool(false), false)
  assert.equal(literalBool('true'), null)
  assert.equal(literalBool(1), null)
  assert.equal(countOf(0), 0)
  assert.equal(countOf(-1), null)
  assert.equal(countOf(1.5), null)
})

test('detalhe técnico: sem segredo, sem stack, uma linha', () => {
  assert.equal(sanitizeTechnical('Bearer abc.def'), 'detalhe omitido (possível credencial)')
  assert.equal(sanitizeTechnical('Traceback (most recent call last):\n  File "x"'),
    'detalhe técnico omitido (stack trace)')
  assert.equal(sanitizeTechnical('erro\n  de   rede'), 'erro de rede')
  assert.equal(sanitizeTechnical('x'.repeat(400))!.length, 160)
  assert.equal(sanitizeTechnical('   '), null)
})

test('motivo desconhecido não é traduzido por adivinhação', () => {
  assert.equal(translateReason('P03-QUARANTINE:abc'),
    'Incidente de execução aberto (reconciliador P03)')
  assert.equal(translateReason('daily drawdown -3.4%'), 'Limite de perda diária atingido')
  assert.equal(translateReason('algo totalmente novo'), UNKNOWN_REASON)
  assert.equal(translateReason(null), UNKNOWN_REASON)
})

// ─── Casos exigidos pelo lote ───────────────────────────────────────────────

test('risco AUSENTE não vira verde', () => {
  const s = state(emptyReading<RiskFacts>(), emptyReading<P03Facts>())
  assert.equal(s.level, 'LOADING')
  assert.equal(s.blocked, false)
  assert.ok(!/dispon/i.test(s.title))
})

test('risco com erro depois de sucesso continua visível como leitura antiga', () => {
  const s = state(
    reading(riskOk(), { state: 'ERROR', errorReason: 'HTTP 502', lastOkAtMs: NOW - 5_000 }),
    reading(p03Ok()),
  )
  assert.equal(s.level, 'STALE')
  assert.equal(s.stale, true)
  assert.equal(s.blocked, false)
  assert.equal(s.lastKnownAtMs, NOW - 5_000)
  assert.ok(s.missing.some(m => /confirmação atual/.test(m)))
})

test('erro sem nenhum valor conhecido é sem confirmação, não zero', () => {
  const s = state(
    { state: 'ERROR', value: null, lastOkAtMs: null, errorReason: 'network' },
    reading(p03Ok()),
  )
  assert.equal(s.level, 'UNAVAILABLE')
  assert.equal(s.lastKnownAtMs, null)
})

test('false explícito + P03 confirmado = SEM BLOQUEIO (nunca "disponível")', () => {
  const s = state(reading(riskOk()), reading(p03Ok()))
  assert.equal(s.level, 'NO_BLOCK_CONFIRMED')
  assert.equal(s.title, 'Sem bloqueio de risco/P03 nesta leitura')
  assert.equal(s.blocked, false)
  assert.ok(s.missing.some(m => /autoridade agregada/.test(m)),
    'precisa nomear o que não foi confirmado')
  assert.notEqual(s.level, 'AVAILABLE')
})

test('zero legítimo e lista vazia confirmada continuam válidos', () => {
  const v = p03View(reading(p03Ok({ open_total: 0, manual_required: 0 })), NOW)
  assert.equal(v.confirmed, true)
  assert.equal(v.openTotal, 0)
  assert.equal(v.blocked, false)
})

test('pausa de risco bloqueia com motivo traduzido', () => {
  const s = state(
    reading(riskOk({ trading_paused: true, pause_reason: 'daily drawdown -3.4%' })),
    reading(p03Ok()),
  )
  assert.equal(s.level, 'BLOCKED')
  assert.equal(s.blocked, true)
  assert.deepEqual(s.reasons, ['Limite de perda diária atingido'])
})

test('pausa manual é nomeada como manual', () => {
  const s = state(
    reading(riskOk({ trading_paused: true, pause_manual: true, pause_reason: 'kill switch' })),
    reading(p03Ok()),
  )
  assert.deepEqual(s.reasons, ['Pausa manual (kill switch)'])
  assert.equal(s.p03Owned, false)
})

test('pausa P03 declara que nem botão nem virada de dia liberam', () => {
  const s = state(
    reading(riskOk({ trading_paused: true, pause_reason: 'P03-QUARANTINE:UNKNOWN_NO_SL' })),
    reading(p03Ok({ open_total: 1, items: [{ state: 'MANUAL_REQUIRED' }] })),
  )
  assert.equal(s.level, 'BLOCKED')
  assert.equal(s.p03Owned, true)
  assert.ok(/reconciliador P03/.test(s.detail))
  assert.ok(s.reasons.some(r => /1 incidente de execução aberto/.test(r)))
})

test('risco SEM pausa + quarentena confirmada = bloqueado (P03 prevalece)', () => {
  const s = state(reading(riskOk()), reading(p03Ok({ quarantine_active: true })))
  assert.equal(s.level, 'BLOCKED')
  assert.ok(s.reasons.includes('Quarentena de execução ativa'))
  assert.ok(s.missing.some(m => /pausa de risco/.test(m)))
})

test('risco SEM pausa + incidente aberto = bloqueado', () => {
  const s = state(reading(riskOk()), reading(p03Ok({ open_total: 2, manual_required: 1 })))
  assert.equal(s.level, 'BLOCKED')
  assert.ok(s.reasons.some(r => /2 incidentes de execução abertos/.test(r)))
  assert.ok(s.reasons.some(r => /1 incidente exige conferência manual/.test(r)))
})

test('P03 ok=false não vira zero incidente nem verde', () => {
  const s = state(reading(riskOk()), reading({ ok: false, error: 'db down' }))
  assert.equal(s.level, 'UNAVAILABLE')
  assert.equal(s.blocked, false)
  assert.ok(s.missing.some(m => /incidentes de execução/.test(m)))
  const v = p03View(reading({ ok: false, error: 'db down' }), NOW)
  assert.equal(v.confirmed, false)
  assert.equal(v.openTotal, null)
  assert.equal(v.blocked, false)
})

test('P03 incoerente (sem contadores) é sem confirmação', () => {
  const v = p03View(reading({ ok: true }), NOW)
  assert.equal(v.confirmed, false)
  assert.equal(v.unconfirmedReason, 'resposta de incidentes incompleta')
})

test('leitura P03 ausente conserva a causa de risco e indica detalhe indisponível', () => {
  const s = state(
    reading(riskOk({ trading_paused: true, pause_reason: 'daily drawdown' })),
    emptyReading<P03Facts>(),
  )
  assert.equal(s.level, 'BLOCKED')
  assert.deepEqual(s.reasons, ['Limite de perda diária atingido'])
  assert.ok(s.missing.some(m => /detalhes de incidentes/.test(m)))
})

test('health saudável + risco pausado continua bloqueado (health não autoriza)', () => {
  // `health` não entra na função: provar que ele NÃO pode virar autorização.
  const s = state(reading(riskOk({ trading_paused: true })), reading(p03Ok()))
  assert.equal(s.level, 'BLOCKED')
})

test('preflight ready + P03 aberto = bloqueado (preflight não entra na decisão)', () => {
  const s = state(reading(riskOk()), reading(p03Ok({ open_total: 1 })))
  assert.equal(s.level, 'BLOCKED')
})

test('autoridade de entrada EXPLÍCITA é o único caminho para disponível', () => {
  const semAutoridade = state(reading(riskOk()), reading(p03Ok()))
  assert.notEqual(semAutoridade.level, 'AVAILABLE')
  const comAutoridade = deriveOperationalState({
    risk: reading(riskOk()), p03: reading(p03Ok()),
    entryAuthority: reading({ confirmed: true, source: 'backend' }),
    nowMs: NOW,
  })
  assert.equal(comAutoridade.level, 'AVAILABLE')
  assert.equal(comAutoridade.title, 'Disponível para operar')
  assert.ok(/não promete/i.test(comAutoridade.detail),
    'mesmo disponível, o texto nega promessa de trade/lucro')
  assert.ok(!/garant/i.test(comAutoridade.detail))
  // `confirmed` não-booleano não autoriza.
  const fake = deriveOperationalState({
    risk: reading(riskOk()), p03: reading(p03Ok()),
    entryAuthority: reading({ confirmed: 'true', source: 'x' }),
    nowMs: NOW,
  })
  assert.notEqual(fake.level, 'AVAILABLE')
})

test('escopo RISK_ONLY (selo do header) nunca promete autoridade', () => {
  const s = deriveOperationalState({
    risk: reading(riskOk()), p03: emptyReading<P03Facts>(),
    scope: 'RISK_ONLY', nowMs: NOW,
  })
  assert.equal(s.level, 'NO_BLOCK_CONFIRMED')
  assert.equal(s.title, 'Risco sem pausa nesta leitura')
  assert.ok(s.missing.some(m => /não lidos neste controle/.test(m)))
})

test('frescor não se renova ao renderizar: só o instante do caller conta', () => {
  const r = reading(riskOk(), { lastOkAtMs: NOW - DEFAULT_STALE_AFTER_MS - 1 })
  const antigo = state(r, reading(p03Ok()))
  assert.equal(antigo.level, 'STALE')
  const mesmaLeituraDepois = deriveOperationalState({
    risk: r, p03: reading(p03Ok()), nowMs: NOW + 600_000,
  })
  assert.equal(mesmaLeituraDepois.level, 'STALE', 'o tempo passando não "melhora" a leitura')
})

test('qualidade da leitura distingue ausente, antigo e confirmado', () => {
  assert.deepEqual(readingQuality(emptyReading(), NOW, 1_000).missing, true)
  assert.equal(readingQuality(reading({}), NOW, 10_000).confirmed, true)
  assert.equal(readingQuality(reading({}, { lastOkAtMs: NOW - 99_999 }), NOW, 10_000).stale, true)
})

test('idade formatada é estável e não inventa valor', () => {
  assert.equal(formatAge(null), '—')
  assert.equal(formatAge(-5), '—')
  assert.equal(formatAge(12_000), 'há 12s')
  assert.equal(formatAge(600_000), 'há 10min')
})

test('causa de validação manual não é um kill switch nem drawdown', () => {
  assert.equal(translateReason('[manual-validation] validação manual pendente (DAILY_READ_UNKNOWN) — resume bloqueado'),
    'Validação da posição manual pendente')
  assert.equal(translateReason('DD diário -3.41% atingiu limite -3%'), 'Limite de perda diária atingido')
  assert.equal(translateReason('DD semanal -6.2% atingiu limite -6%'), 'Limite de perda semanal atingido')
  assert.equal(translateReason('manual accounting unavailable'), UNKNOWN_REASON,
    'a palavra manual isolada não prova um kill switch')
})

test('sinais positivos de P03 prevalecem sobre contadores contraditórios', () => {
  for (const facts of [
    p03Ok({ manual_required: 1 }),
    p03Ok({ retry_pending: 1 }),
    p03Ok({ items: [{ state: 'OPEN' }] }),
    p03Ok({ manual_items: [{ state: 'MANUAL_REQUIRED' }] }),
  ]) {
    const v = p03View(reading(facts), NOW)
    assert.equal(v.confirmed, false, JSON.stringify(facts))
    assert.equal(v.blocked, true, JSON.stringify(facts))
    assert.equal(v.unconfirmedReason, 'resposta de incidentes incoerente')
    assert.equal(state(reading(riskOk()), reading(facts)).level, 'BLOCKED')
  }
})

test('subcontadores e listas incoerentes não confirmam ausência', () => {
  for (const facts of [
    p03Ok({ open_total: 2, manual_required: 3 }),
    p03Ok({ open_total: 2, retry_pending: 3 }),
    p03Ok({ open_total: 2, manual_required: 2, retry_pending: 1 }),
    p03Ok({ manual_required: '0' }),
    p03Ok({ retry_pending: -1 }),
    p03Ok({ items: {} }),
    p03Ok({ manual_items: null }),
  ]) assert.equal(p03View(reading(facts), NOW).confirmed, false, JSON.stringify(facts))
})

test('quarentena e listas positivas aparecem mesmo enquanto risco carrega', () => {
  for (const facts of [
    { ok: false, quarantine_active: true },
    { ok: true, manual_required: 1 },
    { ok: true, items: [{ state: 'OPEN' }] },
  ]) {
    const s = state(emptyReading<RiskFacts>(), reading(facts))
    assert.equal(s.level, 'BLOCKED')
    assert.equal(s.p03Owned, true)
  }
})

test('falha P03 conhecida não volta a parecer primeira leitura carregando', () => {
  const s = state(emptyReading<RiskFacts>(), {
    state: 'ERROR', value: null, lastOkAtMs: null, errorReason: 'P03 unavailable',
  })
  assert.equal(s.level, 'UNAVAILABLE')
})

test('retomada FULL preserva pausa manual e drawdown confirmados sem P03', () => {
  for (const cause of [
    { pause_manual: true, pause_reason: 'Pausa manual via kill switch' },
    { pause_manual: false, pause_reason: 'DD diário -3.4% atingiu limite' },
    { pause_manual: false, pause_reason: 'DD semanal -6.2% atingiu limite' },
  ]) assert.equal(canOfferResume({
    risk: reading(riskOk({ trading_paused: true, ...cause })),
    p03: reading(p03Ok()), nowMs: NOW,
  }), true)
})

test('retomada não aparece em P03, validação manual, causa desconhecida ou leitura antiga', () => {
  const paused = reading(riskOk({ trading_paused: true, pause_manual: true }))
  for (const p03 of [
    reading(p03Ok({ open_total: 1 })),
    reading(p03Ok({ quarantine_active: true })),
    reading(p03Ok({ manual_required: 1 })),
    emptyReading<P03Facts>(),
    reading(p03Ok(), { state: 'ERROR' }),
    reading(p03Ok(), { lastOkAtMs: NOW - DEFAULT_STALE_AFTER_MS - 1 }),
  ]) assert.equal(canOfferResume({ risk: paused, p03, nowMs: NOW }), false)
  for (const reason of ['[manual-validation] pendente', 'P03-QUARANTINE: x', 'algo novo', null]) {
    assert.equal(canOfferResume({
      risk: reading(riskOk({ trading_paused: true, pause_manual: false, pause_reason: reason })),
      p03: reading(p03Ok()), nowMs: NOW,
    }), false)
  }
  assert.equal(canOfferResume({
    risk: { ...paused, state: 'ERROR' }, p03: reading(p03Ok()), nowMs: NOW,
  }), false)
  assert.equal(canOfferResume({
    risk: { ...paused, lastOkAtMs: NOW - DEFAULT_STALE_AFTER_MS - 1 },
    p03: reading(p03Ok()), nowMs: NOW,
  }), false)
})

test('retomada RISK_ONLY exige pausa manual literal e respeita toda causa P03 conhecida', () => {
  const input = {
    risk: reading(riskOk({ trading_paused: true, pause_manual: true })),
    p03: emptyReading<P03Facts>(), nowMs: NOW, scope: 'RISK_ONLY' as const,
  }
  assert.equal(canOfferResume(input), true)
  assert.equal(canOfferResume({ ...input, risk: reading(riskOk({
    trading_paused: true, pause_manual: false, pause_reason: 'DD diário -3.4%',
  })) }), false)
  for (const pause_reason of ['P03-QUARANTINE: x', '[manual-validation] pendente']) {
    assert.equal(canOfferResume({ ...input, risk: reading(riskOk({
      trading_paused: true, pause_manual: true, pause_reason,
    })) }), false)
  }
  assert.equal(canOfferResume({ ...input, p03: reading(p03Ok({ open_total: 1 })) }), false)
  assert.equal(canOfferResume({ ...input, risk: reading(riskOk({ trading_paused: true, pause_manual: 'true' })) }), false)
})
