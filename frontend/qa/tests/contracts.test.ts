// QA Lote 04 — contratos de PARIDADE que o markup estático não alcança.
// Limite declarado: aqui a verificação é sobre o código-fonte das ações
// mutáveis já existentes (rota, método, confirmação) — é prova de que o lote
// NÃO as alterou, não prova de execução. O comportamento delas continua
// coberto pelos testes de backend dos lotes anteriores.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

// Injetado pelo runner (`qa/run-tests.mjs`): raiz real de `frontend/src`.
declare const __QA_SRC__: string
const ler = (rel: string) => readFileSync(resolve(__QA_SRC__, rel), 'utf8')

test('kill switch mantém rota, método e confirmação', () => {
  const home = ler('components/HomeCockpit.tsx')
  assert.ok(home.includes('/api/risk/kill-switch?paused=false'))
  assert.ok(home.includes("method: 'POST'"))
  assert.ok(home.includes('window.confirm('), 'a confirmação não pode ser removida')

  const badge = ler('components/RiskStatusBadge.tsx')
  assert.ok(badge.includes('/api/risk/kill-switch?paused=${next}'))
  assert.ok(badge.includes('confirm('))

  const painel = ler('components/StatusPanel.tsx')
  assert.ok(painel.includes('/api/risk/kill-switch?paused=${next}'))
  assert.ok(painel.includes('confirmKill'), 'confirmação 2-step do painel preservada')
})

test('nenhuma ação mutável nova e nenhum segredo no frontend', () => {
  const arquivos = [
    'App.tsx', 'services/api.ts', 'lib/operationalState.ts',
    'components/HomeCockpit.tsx', 'components/StatusPanel.tsx',
    'components/RiskStatusBadge.tsx', 'components/status/OperationalCards.tsx',
    'components/DashboardPanel.tsx',
  ].map(ler).join('\n')
  for (const proibido of [
    'enable-live', 'execution-incidents/resolve', 'retry-now', 'clear-pause',
    '/api/strategy/p05/experiments', 'manual-positions/ack', 'admin/manual-positions',
    'X-Admin-Token', 'ADMIN_API_TOKEN',
  ]) {
    assert.ok(!arquivos.includes(proibido), `frontend não pode conter "${proibido}"`)
  }
})

test('leitura P03 entrou UMA vez, por método tipado e só GET', () => {
  const apiSrc = ler('services/api.ts')
  const ocorrencias = apiSrc.split('/execution-incidents/status').length - 1
  assert.equal(ocorrencias, 1, 'um único ponto de chamada do endpoint P03')
  assert.ok(apiSrc.includes('export interface ExecutionIncidentsStatus'))
  // Consumidores usam o método compartilhado, nunca `fetch` direto do endpoint
  // (mencionar a rota como RÓTULO de fonte na tela é permitido).
  for (const rel of ['components/HomeCockpit.tsx', 'components/StatusPanel.tsx']) {
    const src = ler(rel)
    assert.ok(src.includes('api.executionIncidentsStatus('), rel)
    assert.ok(!/fetch\([^)]*execution-incidents/.test(src),
      `${rel} não deve fazer fetch direto do endpoint`)
    assert.ok(!/BACKEND\}\/api\/execution-incidents/.test(src), rel)
  }
  // O selo do header não faz leitura própria (nenhuma chamada por card).
  assert.ok(!ler('components/RiskStatusBadge.tsx').includes('executionIncidentsStatus'))
})

test('preflight não entrou no polling do frontend', () => {
  const todos = ['App.tsx', 'services/api.ts', 'components/HomeCockpit.tsx',
    'components/StatusPanel.tsx', 'components/RiskStatusBadge.tsx'].map(ler).join('\n')
  assert.ok(!todos.includes('/api/live/preflight'))
})

test('ordenação do scanner: chave preservada, rótulo corrigido', () => {
  const app = ler('App.tsx')
  assert.ok(app.includes("case 'rr':       return b.confidence - a.confidence"),
    'o algoritmo de ordenação não muda')
  assert.ok(app.includes("{ key: 'rr',       label: 'Força do sinal↓' }"))
  assert.ok(!app.includes("label: 'R/R↓'"), 'rótulo enganoso removido')
})

test('estado operacional é decidido num único módulo', () => {
  // Nenhum consumidor reimplementa a regra "pausado ? ... : verde".
  for (const rel of ['components/HomeCockpit.tsx', 'components/StatusPanel.tsx',
    'components/RiskStatusBadge.tsx']) {
    const src = ler(rel)
    assert.ok(src.includes('deriveOperationalState('), `${rel} precisa usar a função compartilhada`)
    assert.ok(!src.includes("? 'Pausado' : 'Operando'"), `${rel} não pode decidir sozinho`)
    assert.ok(!src.includes("paused ? 'PAUSADO' : 'OK'"), rel)
  }
})

test('overlays reservam a área da navegação', () => {
  const overlays = [
    'components/HomeCockpit.tsx', 'components/StatusPanel.tsx',
    'components/RecommendationsPanel.tsx', 'components/DailyPnLPanel.tsx',
    'components/DashboardPanel.tsx', 'components/AssertivenessPanel.tsx',
    'components/InsightsPanel.tsx', 'components/SweepPanel.tsx',
    'components/TradeManager.tsx', 'components/ChartModal.tsx',
  ]
  for (const rel of overlays) {
    assert.ok(ler(rel).includes('app-overlay'), `${rel} sem reserva do rail/barra`)
  }
  const css = ler('index.css')
  assert.ok(css.includes('env(safe-area-inset-bottom'), 'safe-area inferior obrigatória')
  assert.ok(css.includes('prefers-reduced-motion'))
  assert.ok(css.includes('focus-visible'))
})
