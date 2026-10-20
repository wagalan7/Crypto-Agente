// Transportes falsos contabilizados: não abre servidor, socket, DNS ou produção.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { installQaIsolation } from '../preview/mock'
import { QA_PREVIEW_CSP, QA_PREVIEW_HEADERS, QA_PREVIEW_ORIGIN } from '../preview/isolationPolicy'

declare const __QA_SRC__: string

function fakePreview(origin = QA_PREVIEW_ORIGIN, search = '') {
  const native: { input: RequestInfo | URL; init?: RequestInit }[] = []
  let alternativeTransports = 0
  const target = {
    location: { origin, search },
    fetch: async (input: RequestInfo | URL, init?: RequestInit) => {
      native.push({ input, init })
      return new Response('native asset')
    },
    navigator: {
      serviceWorker: { register() { alternativeTransports++ } },
      sendBeacon() { alternativeTransports++; return true },
    },
    WebSocket: class { constructor() { alternativeTransports++ } },
    XMLHttpRequest: class { constructor() { alternativeTransports++ } },
    EventSource: class { constructor() { alternativeTransports++ } },
  } as unknown as typeof window
  installQaIsolation(target)
  return { target, native, alternativeTransports: () => alternativeTransports }
}

test('Request POST sem init é bloqueado antes de qualquer transporte', async () => {
  const { target, native } = fakePreview()
  await assert.rejects(target.fetch(new Request(`${QA_PREVIEW_ORIGIN}/asset`, { method: 'POST' })), /mutação interceptada/)
  assert.equal(native.length, 0)
  assert.equal(target.__QA__.mutations.length, 1)
  assert.equal(target.__QA__.mutations[0].method, 'POST')
})

test('init.method vence Request.method e todos os métodos não-leitura são bloqueados', async () => {
  const { target, native } = fakePreview()
  for (const method of ['POST', 'put', 'patch', 'DELETE', 'OPTIONS', 'CUSTOM']) {
    await assert.rejects(target.fetch(new Request(`${QA_PREVIEW_ORIGIN}/asset`), { method }), /mutação interceptada/)
  }
  assert.equal(native.length, 0)
  assert.equal(target.__QA__.mutations.length, 6)
})

test('override GET explícito em Request POST sem body preserva leitura de asset local', async () => {
  const { target, native } = fakePreview()
  const input = new Request(`${QA_PREVIEW_ORIGIN}/logo.png`, { method: 'POST' })
  await target.fetch(input, { method: 'GET' })
  assert.equal(native.length, 1)
  assert.equal(native[0].input, `${QA_PREVIEW_ORIGIN}/logo.png`)
  assert.equal(native[0].init?.method, 'GET')
  assert.equal(native[0].init?.credentials, 'omit')
  assert.equal(target.__QA__.mutations.length, 0)
})

test('método julgável não muda entre política e transporte, mesmo com getter mutável', async () => {
  const { target, native } = fakePreview()
  let reads = 0
  const init = { get method() { return ++reads === 1 ? 'GET' : 'POST' } }
  await target.fetch('/logo.png', init)
  assert.equal(native.length, 1)
  assert.equal(native[0].init?.method, 'GET')
  assert.equal(native[0].input, `${QA_PREVIEW_ORIGIN}/logo.png`)
})

test('assets GET/HEAD só são delegados no origin exato do preview', async () => {
  const { target, native } = fakePreview()
  await target.fetch('/logo.png')
  await target.fetch(new URL(`${QA_PREVIEW_ORIGIN}/icon.png`), { method: 'HEAD' })
  assert.equal(native.length, 2)
  for (const url of [
    'http://localhost:5199/logo.png', 'http://127.0.0.1:8000/logo.png',
    'https://127.0.0.1:5199/logo.png', 'https://example.test/logo.png',
    'data:text/plain,qa', 'file:///tmp/qa', 'http://[',
  ]) {
    await assert.rejects(target.fetch(url), /externa BLOQUEADA/)
  }
  assert.equal(native.length, 2, 'nenhum origin alternativo escapa, inclusive localhost')
})

test('GET local de API recebe fixture; método mutante nunca recebe nem envia fixture', async () => {
  const { target, native } = fakePreview()
  const response = await target.fetch(`${QA_PREVIEW_ORIGIN}/api/execution-incidents/status`)
  const body = await response.json()
  assert.equal(body.ok, true)
  assert.equal(body.open_total, 0)
  await assert.rejects(target.fetch(new Request(`${QA_PREVIEW_ORIGIN}/api/risk/status`, { method: 'POST' })), /mutação interceptada/)
  await assert.rejects(target.fetch('http://127.0.0.1:8000/api/risk/status'), /externa BLOQUEADA/)
  assert.equal(native.length, 0)
})

test('cenário indisponível retorna erro sintético, nunca transporte nativo', async () => {
  const { target, native } = fakePreview(QA_PREVIEW_ORIGIN, '?cenario=sem-confirmacao')
  const response = await target.fetch('/api/risk/status')
  assert.equal(response.status, 503)
  assert.equal(native.length, 0)
})

test('API não modelada retorna 503 explícito, não sucesso genérico nem transporte', async () => {
  const { target, native } = fakePreview()
  for (const path of ['/api/analyze', '/api/desconhecida', '/api/calibration/outro', '/api/risk/status/outro']) {
    const response = await target.fetch(`${path}?symbol=SYN`)
    assert.equal(response.status, 503)
    assert.match((await response.json()).detail, /endpoint não modelado/)
  }
  assert.equal(native.length, 0)
  assert.equal(target.__QA__.mutations.length, 0)
})

test('cenário desconhecido ou nome do prototype não vira fixture inválida', async () => {
  for (const scenario of ['desconhecido', 'toString', '__proto__']) {
    const { target, native } = fakePreview(QA_PREVIEW_ORIGIN, `?cenario=${scenario}`)
    const response = await target.fetch('/api/risk/status')
    assert.equal((await response.json()).trading_paused, false)
    assert.equal(target.__QA__.scenario, 'sem-bloqueio')
    assert.equal(native.length, 0)
  }
})

test('transportes alternativos são substituídos antes da app: XHR, beacon, SSE, WS e SW', () => {
  const { target, native, alternativeTransports } = fakePreview()
  assert.throws(() => new target.XMLHttpRequest().open('POST', '/api/risk/kill-switch'), /bloqueado/)
  assert.throws(() => new target.EventSource('https://example.test/events'), /bloqueado/)
  assert.equal(target.navigator.sendBeacon(`${QA_PREVIEW_ORIGIN}/api/close`, 'qa'), false)
  new target.WebSocket('wss://example.test/ws')
  assert.equal(target.navigator.serviceWorker, undefined)
  assert.equal(native.length, 0)
  assert.equal(alternativeTransports(), 0)
  assert.equal(target.__QA__.mutations.length, 2)
  assert.equal(target.__QA__.sockets.length, 1)
})

test('estatística separa origin externo de mutação local e não inventa contagem CSP', async () => {
  const { target } = fakePreview()
  await assert.rejects(target.fetch('/api/close', { method: 'POST' }))
  await assert.rejects(target.fetch('https://example.test/read'))
  const summary = target.__QA__.summary()
  assert.equal(summary.externalBlocked, 1)
  assert.equal(summary.mutationsIntercepted, 1)
  assert.equal(summary.directResourcesBoundary, 'CSP do navegador (fora dos contadores JS)')
})

test('instalação fora do origin exclusivo falha fechada; cenários/contadores são por janela', async () => {
  assert.throws(() => fakePreview('https://example.test'), /origin exclusivo/)
  const first = fakePreview()
  const second = fakePreview(QA_PREVIEW_ORIGIN, '?cenario=p03-bloqueado')
  await first.target.fetch('/logo.png')
  const body = await (await second.target.fetch('/api/execution-incidents/status')).json()
  assert.equal(body.open_total, 2)
  assert.equal(second.native.length, 0)
  assert.equal(second.target.__QA__.calls.length, 1)
})

test('CSP efetiva precede scripts e é idêntica no HTML e no header Vite', () => {
  const preview = resolve(__QA_SRC__, '../qa/preview')
  const html = readFileSync(resolve(preview, 'index.html'), 'utf8')
  const config = readFileSync(resolve(preview, 'vite.config.ts'), 'utf8')
  const meta = html.match(/<meta http-equiv="Content-Security-Policy" content="([^"]+)"/)
  assert.equal(meta?.[1], QA_PREVIEW_CSP)
  assert.ok(html.indexOf('Content-Security-Policy') < html.indexOf('<script'))
  assert.equal(QA_PREVIEW_HEADERS['Content-Security-Policy'], QA_PREVIEW_CSP)
  assert.equal(QA_PREVIEW_HEADERS['X-DNS-Prefetch-Control'], 'off')
  assert.match(config, /headers:\s*QA_PREVIEW_HEADERS/)
  for (const directive of ["default-src 'none'", "script-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:", "frame-src 'none'", "worker-src 'none'",
    "object-src 'none'", "form-action 'none'", "base-uri 'self'"]) {
    assert.ok(QA_PREVIEW_CSP.split('; ').includes(directive))
  }
  assert.ok(QA_PREVIEW_CSP.split('; ').includes("connect-src 'self' ws://127.0.0.1:5199"))
  assert.doesNotMatch(QA_PREVIEW_CSP, /https?:\/\/|\*|unsafe-eval/)
})
