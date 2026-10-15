import test from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import ChartPanel from '../../src/components/ChartPanel'

declare const __QA_SRC__: string
const source = () => readFileSync(resolve(__QA_SRC__, 'components/ChartPanel.tsx'), 'utf8')
const html = () => renderToStaticMarkup(createElement(ChartPanel, {
  symbol: 'TEST/USDT:USDT', timeframe: '1h', isMobile: true, onClose: () => {},
}))

test('gráfico: markup real empilha gráfico e análise antes de xl sem duplicar widget', () => {
  const out = html()
  assert.ok(out.includes('flex-col xl:flex-row'))
  assert.ok(out.includes('overflow-y-auto xl:overflow-hidden'))
  assert.ok(out.includes('w-full xl:w-80'))
  assert.ok(out.includes('min-h-[400px]'), 'compatível com minHeight 400 do widget existente')
  assert.equal((out.match(/data-chart-section="canvas"/g) ?? []).length, 1)
  assert.equal((out.match(/data-chart-section="analysis"/g) ?? []).length, 1)
  assert.ok(out.indexOf('data-chart-section="canvas"') < out.indexOf('data-chart-section="analysis"'))
  assert.ok(!out.includes('w-64 '), 'painel de 256px não pode espremê-lo em celular/tablet')
})
test('gráfico: controles móveis são nomeados e tempo gráfico tem rolagem própria', () => {
  const out = html()
  for (const name of ['Voltar ao scanner', 'Fechar análise', 'Atualizar análise', 'Tempo gráfico']) assert.ok(out.includes(`aria-label="${name}"`))
  assert.ok(out.includes('xl:hidden flex'))
  assert.ok(out.includes('overflow-x-auto'))
  assert.ok(out.includes('Situação e plano'))
})
test('gráfico: multi-TF identifica força heurística sem porcentagem de confiança', () => {
  // É um contrato de código: o conteúdo multi-TF chega por efeito assíncrono,
  // não existe no render estático e não foi consultada a API neste teste.
  const src = source()
  assert.ok(src.includes('Força do sinal'))
  assert.ok(src.includes('}/100`'))
  assert.ok(src.includes('Viés técnico, não probabilidade nem autorização do bot.'))
  assert.ok(!src.includes('>Confiança<'))
})
test('gráfico: largura é CSS, não remonta o widget nem altera rota ou callback', () => {
  const src = source()
  assert.equal((src.match(/<TradingViewWidget\s/g) ?? []).length, 1)
  assert.equal((src.match(/<CandleChart\s/g) ?? []).length, 1)
  assert.ok(src.includes('interval={tf}'))
  assert.ok(src.includes('onAddSignalToManager?.(displaySignal)'))
  assert.ok(src.includes('analyze(symbol, tf, withAi)'))
  const refresh = src.split('const runAnalysis = useCallback(() => {')[1].split('}, [')[0]
  assert.ok(refresh.indexOf('setDisplaySignal(null)') < refresh.indexOf('analyze(symbol, tf, withAi)'))
  assert.ok(!src.includes('window.innerWidth'))
  assert.ok(!src.includes('matchMedia('))
})
