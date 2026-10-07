// QA Lote 04 — leitura P03: cache + single-flight compartilhados.
// Prova a exigência do lote: DUAS telas no mesmo ciclo = UM GET.
// Nenhuma rede real: `globalThis.fetch` é substituído por um contador local.
import test from 'node:test'
import assert from 'node:assert/strict'

import { api, __resetExecutionIncidentsCache, EXECUTION_INCIDENTS_TTL_MS } from '../../src/services/api'

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
