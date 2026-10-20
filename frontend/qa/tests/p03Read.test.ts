// QA Lote 04 — leitura P03: cache + single-flight compartilhados.
// Prova a exigência do lote: DUAS telas no mesmo ciclo = UM GET.
// Nenhuma rede real: `globalThis.fetch` é substituído por um contador local.
import test from 'node:test'
import assert from 'node:assert/strict'

import {
  api, __resetExecutionIncidentsCache, EXECUTION_INCIDENTS_TTL_MS,
  executionIncidentsObservedAt,
} from '../../src/services/api'

const PAYLOAD = {
  ok: true, quarantine_active: false, open_total: 0, manual_required: 0,
  last_reconciliation_at: '2026-10-07T00:00:00Z', items: [], manual_items: [],
}

interface Chamada { url: string; method: string }

function instalarFetch(resposta: () => { status: number; body: unknown }) {
  const chamadas: Chamada[] = []
  ;(globalThis as { fetch: unknown }).fetch = async (input: unknown, init?: { method?: string }) => {
    const url = String(input)
    chamadas.push({ url, method: init?.method ?? 'GET' })
    const r = resposta()
    return {
      ok: r.status >= 200 && r.status < 300,
      status: r.status,
      json: async () => r.body,
      text: async () => JSON.stringify(r.body),
    }
  }
  return chamadas
}

test('duas telas simultâneas compartilham UM GET (single-flight)', async () => {
  __resetExecutionIncidentsCache()
  const chamadas = instalarFetch(() => ({ status: 200, body: PAYLOAD }))
  const [a, b] = await Promise.all([
    api.executionIncidentsStatus(),
    api.executionIncidentsStatus(),
  ])
  assert.equal(chamadas.length, 1, 'não pode duplicar o GET do P03')
  assert.deepEqual(a, PAYLOAD)
  assert.equal(a, b, 'as duas telas recebem a MESMA resposta')
  assert.ok(chamadas[0].url.endsWith('/api/execution-incidents/status'))
  assert.equal(chamadas[0].method, 'GET', 'leitura P03 é somente GET')
})

test('dentro da janela o cache evita nova chamada; força refaz', async () => {
  __resetExecutionIncidentsCache()
  const chamadas = instalarFetch(() => ({ status: 200, body: PAYLOAD }))
  await api.executionIncidentsStatus()
  await api.executionIncidentsStatus()
  assert.equal(chamadas.length, 1, 'segunda leitura no mesmo ciclo usa cache')
  await api.executionIncidentsStatus({ maxAgeMs: 0 })
  assert.equal(chamadas.length, 2, 'o botão Atualizar do usuário força leitura nova')
  assert.ok(EXECUTION_INCIDENTS_TTL_MS < 20_000, 'a janela tem de caber no ciclo de 20s da Home')
})

test('falha HTTP propaga erro (o consumidor registra, não inventa zero)', async () => {
  __resetExecutionIncidentsCache()
  instalarFetch(() => ({ status: 503, body: { detail: 'down' } }))
  await assert.rejects(() => api.executionIncidentsStatus(), /API error 503/)
  // Falha não contamina o cache: a próxima tentativa volta à rede.
  const chamadas = instalarFetch(() => ({ status: 200, body: PAYLOAD }))
  const v = await api.executionIncidentsStatus()
  assert.deepEqual(v, PAYLOAD)
  assert.equal(chamadas.length, 1)
})

test('HTTP 200 com ok=false é erro seguro e não envenena o cache', async () => {
  __resetExecutionIncidentsCache()
  const chamadas = instalarFetch(() => ({ status: 200, body: { ok: false, error: 'Bearer private-value' } }))
  await assert.rejects(() => api.executionIncidentsStatus(), err => {
    assert.equal((err as Error).message, 'P03_STATUS_UNCONFIRMED')
    assert.ok(!(err as Error).message.includes('private-value'))
    return true
  })
  await assert.rejects(() => api.executionIncidentsStatus(), /P03_STATUS_UNCONFIRMED/)
  assert.equal(chamadas.length, 2, 'falha de domínio não pode virar cache válido')
  const recuperacao = instalarFetch(() => ({ status: 200, body: PAYLOAD }))
  assert.equal(await api.executionIncidentsStatus(), PAYLOAD)
  assert.equal(recuperacao.length, 1)
})

test('P03 recusa payload que não seja objeto com ok booleano literal', async () => {
  for (const body of [null, [], 'ok', { ok: 'true' }, { ok: 1 }, {}]) {
    __resetExecutionIncidentsCache()
    instalarFetch(() => ({ status: 200, body }))
    await assert.rejects(() => api.executionIncidentsStatus(), /P03_STATUS_UNCONFIRMED/)
  }
})

test('erro após sucesso conserva diagnóstico, mas exige novo GET mesmo dentro do TTL', async () => {
  __resetExecutionIncidentsCache()
  instalarFetch(() => ({ status: 200, body: PAYLOAD }))
  const conhecido = await api.executionIncidentsStatus()
  const receivedAt = executionIncidentsObservedAt(conhecido)
  const falhas = instalarFetch(() => ({ status: 200, body: { ok: false, open_total: 0 } }))
  await assert.rejects(() => api.executionIncidentsStatus({ maxAgeMs: 0 }), /P03_STATUS_UNCONFIRMED/)
  await assert.rejects(() => api.executionIncidentsStatus(), /P03_STATUS_UNCONFIRMED/)
  assert.equal(falhas.length, 2,
    'a segunda tentativa precisa ir à rede; o cache anterior não confirma uma fonte que falhou')
  assert.equal(executionIncidentsObservedAt(conhecido), receivedAt,
    'objeto antigo no consumidor conserva seu instante para diagnóstico')
  assert.deepEqual(conhecido, PAYLOAD, 'a falha não apaga nem reescreve o valor conhecido')
  const novo = { ...PAYLOAD, open_total: 1, items: [{ state: 'OPEN' }] }
  const recuperacao = instalarFetch(() => ({ status: 200, body: novo }))
  assert.equal(await api.executionIncidentsStatus(), novo,
    'somente um novo GET bem-sucedido devolve confirmação')
  assert.equal(recuperacao.length, 1)
})

test('cache conserva o instante real de recebimento e nunca renova o frescor', async () => {
  __resetExecutionIncidentsCache()
  const realNow = Date.now
  let now = 1_770_000_000_000
  Date.now = () => now
  try {
    assert.equal(executionIncidentsObservedAt(null), null)
    assert.equal(executionIncidentsObservedAt({ ok: true }), null)
    const chamadas = instalarFetch(() => ({ status: 200, body: PAYLOAD }))
    const primeiro = await api.executionIncidentsStatus()
    const observedAt = executionIncidentsObservedAt(primeiro)
    assert.equal(observedAt, now)
    now += 5_000
    const segundo = await api.executionIncidentsStatus()
    assert.equal(primeiro, segundo)
    assert.equal(executionIncidentsObservedAt(segundo), observedAt)
    assert.equal(chamadas.length, 1)
    now += 1_000
    instalarFetch(() => ({ status: 200, body: { ok: false } }))
    await assert.rejects(() => api.executionIncidentsStatus({ maxAgeMs: 0 }), /P03_STATUS_UNCONFIRMED/)
    assert.equal(executionIncidentsObservedAt(primeiro), observedAt)
  } finally {
    Date.now = realNow
  }
})

test('recebimento P03 só ganha horário após o corpo chegar, com sinal de timeout', async () => {
  __resetExecutionIncidentsCache()
  const realNow = Date.now
  let now = 1_770_000_000_000
  Date.now = () => now
  try {
    const payload = { ...PAYLOAD }
    let receivedSignal: AbortSignal | undefined
    globalThis.fetch = (async (_input: unknown, init?: RequestInit) => {
      receivedSignal = init?.signal as AbortSignal | undefined
      return { ok: true, json: async () => { now += 2_000; return payload } }
    }) as typeof fetch
    const resposta = await api.executionIncidentsStatus()
    assert.ok(receivedSignal instanceof AbortSignal, 'GET precisa de prazo finito')
    assert.equal(executionIncidentsObservedAt(resposta), now)
  } finally {
    Date.now = realNow
  }
})

test('falha HTTP, JSON, transporte ou timeout também invalida o reuso do cache', async () => {
  const cases: Array<[string, () => Promise<unknown>]> = [
    ['HTTP', async () => ({ ok: false, status: 503 })],
    ['JSON', async () => ({ ok: true, json: async () => { throw new SyntaxError('invalid JSON') } })],
    ['transporte', async () => { throw new TypeError('Failed to fetch') }],
    ['timeout', async () => { throw new DOMException('expired', 'TimeoutError') }],
  ]
  for (const [label, fail] of cases) {
    __resetExecutionIncidentsCache()
    instalarFetch(() => ({ status: 200, body: PAYLOAD }))
    const conhecido = await api.executionIncidentsStatus()
    const receivedAt = executionIncidentsObservedAt(conhecido)
    let calls = 0
    globalThis.fetch = (async () => { calls++; return fail() }) as typeof fetch
    await assert.rejects(() => api.executionIncidentsStatus({ maxAgeMs: 0 }), undefined, label)
    await assert.rejects(() => api.executionIncidentsStatus(), undefined, label)
    assert.equal(calls, 2, `${label}: é obrigatório tentar GET novamente`)
    assert.equal(executionIncidentsObservedAt(conhecido), receivedAt)
    assert.deepEqual(conhecido, PAYLOAD)
  }
})
