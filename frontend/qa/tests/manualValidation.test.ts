import test from 'node:test'
import assert from 'node:assert/strict'
import { deriveOperationalState, canOfferResume, type P03Facts } from '../../src/lib/operationalState'

function input(manual: unknown) {
  const facts: P03Facts = {ok: true, open_total: 0, manual_required: 0,
    quarantine_active: false, items: [], manual_items: [], manual_validation: manual}
  return {nowMs: 1000, risk: {state: 'OK' as const, lastOkAtMs: 1000,
    value: {trading_paused: false}}, p03: {state: 'OK' as const, lastOkAtMs: 1000, value: facts}}
}
test('zero incidentes e risco sem pausa não escondem reconhecimento manual inválido', () => {
  const value = input({ok: true, new_entries_blocked: true,
    reason_code: 'MANUAL_ACK_RECONFIRMATION_REQUIRED'})
  const state = deriveOperationalState(value)
  assert.equal(state.level, 'BLOCKED')
  assert.match(state.title, /validação manual/)
  assert.ok(state.reasons.some(r => r.includes('novo reconhecimento')))
  assert.equal(canOfferResume(value), false)
})
test('diagnóstico indisponível ou booleano textual nunca confirma liberação', () => {
  for (const manual of [{ok: false, new_entries_blocked: null}, {ok: true, new_entries_blocked: 'false'}]) {
    const state = deriveOperationalState(input(manual))
    assert.notEqual(state.level, 'AVAILABLE')
    assert.ok(state.missing.some(x => x.includes('manual')))
  }
})
test('manual validada não equivale a autorização agregada de entrada', () => {
  const state = deriveOperationalState(input({ok: true, new_entries_blocked: false,
    reason_code: 'MANUAL_VALIDATION_CLEAR'}))
  assert.equal(state.level, 'NO_BLOCK_CONFIRMED')
})
