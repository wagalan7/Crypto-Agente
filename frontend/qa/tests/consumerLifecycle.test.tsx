// Lifecycle real usado pelos hooks de Home/Sistema/header. Não há mini-DOM,
// rede, relógio real nem pacote instalado: os controladores recebem as bordas
// do navegador por injeção e são exercitados com EventTarget e timer sintético.
// SSR abaixo verifica o markup REAL dos cartões, não uma cópia da tela.
import test from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import {
  attachOverlayDismiss, startReadingClock, settleReading,
} from '../../src/lib/readingLifecycle'
import {
  deriveOperationalState, readingQuality, DEFAULT_STALE_AFTER_MS,
  type RiskFacts, type P03Facts, type SourceReading,
} from '../../src/lib/operationalState'
import {
  OperationalStateCard, PositionsInventoryCard,
} from '../../src/components/status/OperationalCards'

const NOW = 1_770_000_000_000
const reading = <T,>(value: T): SourceReading<T> => ({
  state: 'OK', value, lastOkAtMs: NOW, errorReason: null,
})
const healthyRisk: RiskFacts = { enabled: true, trading_paused: false, pause_manual: false }
const healthyP03: P03Facts = {
  ok: true, quarantine_active: false, open_total: 0, manual_required: 0,
}

class OverlayDoc extends EventTarget {
  activeElement: { focus?: () => void; isConnected?: boolean } | null = null
  key(key: string) {
    const event = new Event('keydown')
    Object.defineProperty(event, 'key', { value: key })
    this.dispatchEvent(event)
  }
}

class FakeClock {
  time = NOW
  nextId = 0
  timers = new Map<number, { callback: () => void; delay: number; next: number }>()
  cleared: unknown[] = []
  now = () => this.time
  setInterval = (callback: () => void, delay: number): unknown => {
    assert.ok(Number.isFinite(delay) && delay > 0, 'timer precisa de cadência finita')
    const id = ++this.nextId
    this.timers.set(id, { callback, delay, next: this.time + delay })
    return id
  }
  clearInterval = (id: unknown) => {
    this.cleared.push(id)
    this.timers.delete(id as number)
  }
  advance(ms: number) {
    const end = this.time + ms
    for (;;) {
      const due = [...this.timers.values()].map(v => v.next).filter(n => n <= end)
      if (due.length === 0) break
      this.time = Math.min(...due)
      for (const timer of [...this.timers.values()]) {
        if (timer.next !== this.time) continue
        timer.next += timer.delay
        timer.callback()
      }
    }
    this.time = end
  }
}

test('Escape usa callback atual sem reinstalar listener nem deslocar foco', () => {
  const doc = new OverlayDoc()
  let focusCount = 0
  let oldClose = 0
  let newClose = 0
  doc.activeElement = { isConnected: true, focus: () => { focusCount++ } }
  let onClose = () => { oldClose++ }
  const detach = attachOverlayDismiss(() => onClose, doc)

  doc.key('Enter')
  assert.equal(oldClose, 0)
  // Equivale à ref atualizada por rerender: não desmonta o efeito.
  onClose = () => { newClose++ }
  doc.key('Escape')
  assert.equal(oldClose, 0)
  assert.equal(newClose, 1)
  assert.equal(focusCount, 0, 'callback novo/poll não devolve foco ao launcher')

  detach()
  assert.equal(focusCount, 1, 'foco retorna apenas ao desmontar')
  doc.key('Escape')
  assert.equal(newClose, 1, 'listener removido não responde após unmount')
})

test('cleanup do overlay é idempotente e não foca elemento removido', () => {
  const doc = new OverlayDoc()
  let focusCount = 0
  const launcher = { isConnected: true, focus: () => { focusCount++ } }
  doc.activeElement = launcher
  const detach = attachOverlayDismiss(() => () => {}, doc)
  launcher.isConnected = false
  detach()
  detach()
  assert.equal(focusCount, 0)
})

test('overlay sem elemento ativo monta e desmonta sem fabricar foco', () => {
  const doc = new OverlayDoc()
  let closes = 0
  const detach = attachOverlayDismiss(() => () => { closes++ }, doc)
  doc.key('Escape')
  assert.equal(closes, 1)
  detach()
  assert.equal(doc.activeElement, null)
})

test('cleanup repetido restaura o foco só uma vez', () => {
  const doc = new OverlayDoc()
  let focusCount = 0
  doc.activeElement = { isConnected: true, focus: () => { focusCount++ } }
  const detach = attachOverlayDismiss(() => () => {}, doc)
  detach()
  detach()
  assert.equal(focusCount, 1)
})

test('relógio continua avançando enquanto uma requisição nunca resolve', () => {
  const clock = new FakeClock()
  let renderedNow = NOW
  const stop = startReadingClock(n => { renderedNow = n }, clock)
  const never = new Promise<unknown>(() => {})
  let completed = false
  void settleReading(never, clock.now).then(() => { completed = true })

  clock.advance(DEFAULT_STALE_AFTER_MS + 2_000)
  assert.equal(completed, false)
  assert.ok(renderedNow > NOW + DEFAULT_STALE_AFTER_MS)
  const quality = readingQuality(reading(healthyRisk), renderedNow, DEFAULT_STALE_AFTER_MS)
  assert.equal(quality.stale, true)
  const state = deriveOperationalState({
    risk: reading(healthyRisk), p03: reading(healthyP03), nowMs: renderedNow,
  })
  assert.equal(state.level, 'STALE')
  const markup = renderToStaticMarkup(createElement(OperationalStateCard, {
    state, ageMs: quality.ageMs,
  }))
  assert.ok(markup.includes('data-level="STALE"'))
  assert.ok(markup.includes('dado de leitura anterior'))
  assert.ok(!markup.includes('Sem bloqueio de risco/P03 nesta leitura'))
  stop()
})

test('desmontagem remove o relógio e não publica atualização tardia', () => {
  const clock = new FakeClock()
  const readings: number[] = []
  const stop = startReadingClock(n => { readings.push(n) }, clock)
  clock.advance(2_000)
  assert.ok(readings.length > 0)
  stop()
  const countAtUnmount = readings.length
  clock.advance(90_000)
  assert.equal(readings.length, countAtUnmount)
  assert.equal(clock.timers.size, 0)
})

test('fonte rápida conserva instante próprio quando outra fonte atrasa Promise.all', async () => {
  const clock = new FakeClock()
  let releaseSlow!: (value: string) => void
  const fast = settleReading(Promise.resolve('risk'), clock.now)
  const slow = settleReading(new Promise<string>(resolve => { releaseSlow = resolve }), clock.now)
  let allCompleted = false
  const all = Promise.all([fast, slow]).then(values => { allCompleted = true; return values })

  const fastResult = await fast
  assert.equal(fastResult.ok, true)
  if (!fastResult.ok) assert.fail('leitura rápida deveria ter sucesso')
  assert.equal(fastResult.receivedAtMs, NOW)
  assert.equal(allCompleted, false)
  clock.advance(65_000)
  releaseSlow('paper')
  const [first, second] = await all
  assert.ok(first.ok && second.ok)
  if (!first.ok || !second.ok) assert.fail('ambas são fixtures de sucesso')
  assert.equal(first.receivedAtMs, NOW)
  assert.equal(second.receivedAtMs, NOW + 65_000)
  assert.notEqual(first.receivedAtMs, second.receivedAtMs)
  assert.equal(readingQuality({
    state: 'OK', value: healthyRisk, lastOkAtMs: first.receivedAtMs,
  }, clock.now(), DEFAULT_STALE_AFTER_MS).stale, true,
  'fim do lote não renova artificialmente a fonte antiga')
})

test('falha de fonte não recebe timestamp de sucesso nem substitui o erro', async () => {
  const cause = new Error('SYNTHETIC_READING_ERROR')
  const result = await settleReading(Promise.reject(cause), () => NOW)
  assert.equal(result.ok, false)
  if (result.ok) assert.fail('fixture é uma falha')
  assert.equal(result.e, cause)
  assert.equal('receivedAtMs' in result, false)
})

test('zero confirmado antigo conserva o número mas o cartão diz leitura anterior', () => {
  const now = NOW + DEFAULT_STALE_AFTER_MS + 1
  const markup = renderToStaticMarkup(createElement(PositionsInventoryCard, {
    quality: readingQuality(reading([]), now, DEFAULT_STALE_AFTER_MS),
    botCount: 0, managedManualCount: 0,
  }))
  assert.ok(markup.includes('data-quality="STALE"'))
  assert.ok(markup.includes('>0<'), 'zero conhecido permanece visível')
  assert.ok(markup.includes('Última leitura conhecida'))
  assert.ok(!markup.includes('confirmada nesta leitura'))
})
