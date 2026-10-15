import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { createLatestAnalysisRunner, subscribeCurrentTicker } from '../../src/lib/analysisLifecycle'
import type { OHLCVCandle, Ticker, TradeSignal } from '../../src/types'

declare const __QA_SRC__: string

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: Error) => void
  const promise = new Promise<T>((a, b) => { resolve = a; reject = b })
  return { promise, resolve, reject }
}

function analysisFixture() {
  const candles = [deferred<{ data: OHLCVCandle[] }>(), deferred<{ data: OHLCVCandle[] }>()]
  const signals = [deferred<TradeSignal>(), deferred<TradeSignal>()]
  const calls: unknown[][] = []
  let candleCall = 0
  let signalCall = 0
  const state = { loading: false, signal: null as TradeSignal | null, candles: [] as OHLCVCandle[], error: null as string | null, finishes: 0 }
  const runner = createLatestAnalysisRunner({
    getOHLCV: (symbol, tf) => { calls.push(['candles', symbol, tf]); return candles[candleCall++].promise },
    analyze: (symbol, tf, ai) => { calls.push(['signal', symbol, tf, ai]); return signals[signalCall++].promise },
  }, {
    start: () => { state.loading = true; state.signal = null; state.candles = []; state.error = null },
    success: (cs, signal) => { state.candles = cs; state.signal = signal },
    failure: error => { state.error = error },
    finish: () => { state.loading = false; state.finishes++ },
  })
  const finish = (index: number, symbol: string) => {
    candles[index].resolve({ data: [{ timestamp: index } as OHLCVCandle] })
    signals[index].resolve({ symbol } as TradeSignal)
  }
  return { runner, state, calls, finish, candles, signals }
}

test('símbolo antigo terminado por último não troca gráfico/sinal atual', async () => {
  const f = analysisFixture()
  const old = f.runner.analyze('BTC/USDT', '1h', true)
  const current = f.runner.analyze('APE/USDT', '4h', false)
  f.finish(1, 'APE/USDT')
  await current
  const currentCandles = f.state.candles
  f.finish(0, 'BTC/USDT')
  await old
  assert.equal(f.state.signal?.symbol, 'APE/USDT')
  assert.equal(f.state.candles, currentCandles)
  assert.equal(f.state.finishes, 1)
  assert.deepEqual(f.calls, [
    ['candles', 'BTC/USDT', '1h'], ['signal', 'BTC/USDT', '1h', true],
    ['candles', 'APE/USDT', '4h'], ['signal', 'APE/USDT', '4h', false],
  ])
})

test('erro antigo não apaga resultado nem erro da análise atual', async () => {
  const f = analysisFixture()
  const old = f.runner.analyze('BTC/USDT', '1h')
  const current = f.runner.analyze('BTC/USDT', '4h')
  f.finish(1, 'BTC/USDT')
  await current
  f.signals[0].reject(new Error('OLD_ERROR'))
  await old
  assert.equal(f.state.error, null)
  assert.equal(f.state.signal?.symbol, 'BTC/USDT')
  assert.equal(f.state.finishes, 1)
})

test('finalização antiga não encerra indicador de análise nova ainda pendente', async () => {
  const f = analysisFixture()
  const old = f.runner.analyze('BTC/USDT', '1h')
  const current = f.runner.analyze('BTC/USDT', '4h')
  f.finish(0, 'BTC/USDT')
  await old
  assert.equal(f.state.loading, true)
  assert.equal(f.state.signal, null)
  assert.deepEqual(f.state.candles, [])
  f.finish(1, 'BTC/USDT')
  await current
  assert.equal(f.state.loading, false)
})

test('cleanup invalida sucesso, erro e finally sem inventar cancelamento de rede', async () => {
  const f = analysisFixture()
  const pending = f.runner.analyze('BTC/USDT', '1h')
  f.runner.invalidate()
  f.signals[0].reject(new Error('AFTER_UNMOUNT'))
  await pending
  assert.equal(f.state.error, null)
  assert.equal(f.state.finishes, 0)
  assert.equal(f.state.signal, null)
  const remount = f.runner.analyze('APE/USDT', '1h')
  f.finish(1, 'APE/USDT')
  await remount
  assert.equal(f.state.signal?.symbol, 'APE/USDT', 're-montagem StrictMode pode iniciar nova geração')
})

test('requisição atual ainda publica erro e encerra loading normalmente', async () => {
  const f = analysisFixture()
  const current = f.runner.analyze('BTC/USDT', '1h')
  f.signals[0].reject(new Error('CURRENT_ERROR'))
  await current
  assert.equal(f.state.error, 'CURRENT_ERROR')
  assert.equal(f.state.loading, false)
  assert.equal(f.state.finishes, 1)
})

function tickerFixture(symbol = 'BTC/USDT:USDT') {
  let callback!: (message: unknown) => void
  let closed = 0
  const values: Ticker[] = []
  const stop = subscribeCurrentTicker(symbol, ticker => values.push(ticker), (requested, cb) => {
    assert.equal(requested, symbol)
    callback = cb
    return { close: () => { closed++ } }
  })
  const emit = (s: string, last = 50) => callback({ type: 'ticker', data: { symbol: s, last } })
  return { values, stop, emit, callback: (m: unknown) => callback(m), closes: () => closed }
}

test('ticker aceita par equivalente, mas recusa outro ativo e outra quote', () => {
  const f = tickerFixture()
  f.emit('BTCUSDT')
  f.emit('BTC/USDT:USDT')
  f.emit('BTC/USDC:USDC')
  f.emit('APE/USDT:USDT')
  assert.equal(f.values.length, 2)
  assert.equal(f.values[0].last, 50)
})

test('callback do socket antigo é ignorado após cleanup; close é idempotente', () => {
  const f = tickerFixture()
  f.stop()
  f.stop()
  f.emit('BTCUSDT')
  assert.equal(f.values.length, 0)
  assert.equal(f.closes(), 1)
})

test('ticker inválido/ausente não vira preço zero nem atualização válida', () => {
  const f = tickerFixture()
  for (const last of [NaN, Infinity, -Infinity, 0, -1]) f.emit('BTCUSDT', last)
  f.callback(null)
  f.callback({ type: 'ticker', data: { last: 10 } })
  f.callback({ type: 'other', data: { symbol: 'BTCUSDT', last: 10 } })
  assert.deepEqual(f.values, [])
})

test('hooks reais usam controladores testados e escondem preço de símbolo anterior no render', () => {
  const price = readFileSync(join(__QA_SRC__, 'hooks/useLivePrice.ts'), 'utf8')
  const analysis = readFileSync(join(__QA_SRC__, 'hooks/useAnalysis.ts'), 'utf8')
  assert.match(price, /return subscribeCurrentTicker\(symbol/)
  assert.match(price, /reading\?\.symbol === symbol/)
  assert.match(analysis, /createLatestAnalysisRunner\(api/)
  assert.match(analysis, /useEffect\(\(\) => \(\) => runner.current\?\.invalidate\(\)/)
  assert.match(analysis, /setCandles\(\[\]\)/)
  assert.doesNotMatch(analysis, /AbortController/, 'não alega abort sem passar sinal ao fetch')
})
