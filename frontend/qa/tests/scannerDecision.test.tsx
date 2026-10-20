import test from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { scannerSituation, positivePrice } from '../../src/lib/scannerDecision'
import { SignalPanel } from '../../src/components/SignalPanel/SignalPanel'
import type { TradeSignal } from '../../src/types'

// Dados exclusivamente sintéticos; não consultam preço, conta, API ou probabilidade.
const signal = (over: Partial<TradeSignal> = {}): TradeSignal => ({
  symbol: 'TEST/USDT', timeframe: '1h', direction: 'long', trade_type: 'day_trade', confidence: .8,
  entry: 100, stop_loss: 90, tp1: 110, tp2: 120, tp3: 130, risk_reward: 2,
  patterns: [], indicators: {}, timestamp: 1770000000000, signal_strength: 'forte',
  trade_plan: { entry:100, stop_loss:90, tp1:110, tp2:120, tp3:130, risk_reward:2,
    risk_reward_tp1:1, risk_reward_tp3:3, reasoning_entry:'original',
    reasoning_stop:{price:90,reason:'original stop',source:'test'},
    reasoning_tp1:{price:110,reason:'original TP1',source:'test'},
    reasoning_tp2:{price:120,reason:'original TP2',source:'test'},
    reasoning_tp3:{price:130,reason:'original TP3',source:'test'},quality_warnings:[],
    entry_zone:{bottom:98,top:102,mid:100,type:'limit_pullback',description:'original zone'} }, ...over,
})
const html = (s = signal(), p?: number, action?: () => void) => renderToStaticMarkup(createElement(SignalPanel,{signal:s,livePrice:p,onAddToManager:action}))

test('scanner: cotação ausente/inválida nunca é substituída pela entrada', () => {
  for (const value of [undefined, null, NaN, Infinity, -Infinity, 0, -1, true, '100']) {
    const r = scannerSituation(signal(), value as number)
    assert.equal(r.state,'PRICE_UNKNOWN'); assert.equal(r.price,null)
    assert.equal(positivePrice(value),false)
  }
})
test('scanner: neutro com padrão e força alta não ganha entrada nem recomendação Swing Bom', () => {
  const s=signal({direction:'neutral',confidence:.99,patterns:[{type:'lta',direction:'long',confidence:.9,description:'padrão sintético'}] as TradeSignal['patterns']})
  const out=html(s,105)
  assert.equal(scannerSituation(s,105).state,'NEUTRAL')
  assert.ok(out.includes('Sem viés de compra ou venda'))
  assert.ok(out.includes('Preço acima da zona de entrada'))
  assert.ok(!out.includes('Melhor para:')); assert.ok(!out.includes('✅ Bom'))
})
test('scanner: long fora/na zona, alvo e stop têm leitura própria sem autorização', () => {
  for(const [price,state] of [[97,'OUTSIDE_ZONE'],[103,'OUTSIDE_ZONE'],[98,'IN_ZONE'],[102,'IN_ZONE'],[110,'TARGET_PASSED'],[90,'STOP_PASSED']] as const) {
    const r=scannerSituation(signal(),price); assert.equal(r.state,state)
    assert.equal(r.executionConfirmed,false);assert.equal(r.probability,null)
  }
})
test('scanner: short usa desigualdades espelhadas', () => {
  const s=signal({direction:'short',stop_loss:110,tp1:90,tp2:80,tp3:70})
  for(const [price,state] of [[100,'IN_ZONE'],[103,'OUTSIDE_ZONE'],[90,'TARGET_PASSED'],[110,'STOP_PASSED']] as const) assert.equal(scannerSituation(s,price).state,state)
})
test('scanner: plano inválido não é recalculado nem mutado', () => {
  const s=signal({stop_loss:105});const before=JSON.stringify(s)
  assert.equal(scannerSituation(s,100).state,'PLAN_UNKNOWN');assert.equal(JSON.stringify(s),before)
})
test('scanner: ausência de zona não é interpretada como entrada a mercado', () => {
  const s=signal({trade_plan:null});const r=scannerSituation(s,100)
  assert.equal(r.state,'NO_ZONE');assert.equal(r.probability,null)
  assert.equal(r.executionConfirmed,false)
})
test('scanner: zona explicitamente market permanece só referência', () => {
  const s=signal();s.trade_plan!.entry_zone!.type='market'
  const r=scannerSituation(s,100);assert.equal(r.state,'MARKET_REFERENCE')
  assert.equal(r.executionConfirmed,false)
})
test('scanner: força é /100, separada de probabilidade explicitamente ausente', () => {
  const out=html(signal(),100)
  assert.ok(out.includes('80/100'))
  assert.ok(out.includes('Força do sinal não é probabilidade de lucro.'))
  assert.ok(out.includes('Probabilidade calibrada: não disponível'))
  assert.ok(!out.includes('80% de confiança'))
})
test('scanner: resumo e plano antecedem detalhes fechados, níveis recebidos intactos', () => {
  const out=html(signal(),111)
  assert.ok(out.indexOf('Situação deste ativo') < out.indexOf('PLANO TÉCNICO DE REFERÊNCIA'))
  assert.ok(out.indexOf('PLANO TÉCNICO DE REFERÊNCIA') < out.indexOf('Entender a análise'))
  assert.ok(out.includes('data-situation="TARGET_PASSED"'))
  assert.ok(out.includes('90.0000'));assert.ok(out.includes('110.0000'))
  assert.equal((out.match(/<details/g)??[]).length,2);assert.ok(!out.includes('<details open'))
  assert.ok(out.includes('min-w-0'));assert.ok(out.includes('break-words'))
})
test('scanner: ação existente é declarada registro manual, não ordem do bot', () => {
  const out=html(signal(),100,()=>{})
  assert.ok(out.includes('Registrar plano no gestor manual'))
  assert.ok(out.includes('não libera nem inicia o bot'))
  assert.ok(out.includes('Nenhuma ordem é enviada por abrir o gráfico'))
  assert.equal((out.match(/<button/g)??[]).length,1)
  assert.equal((html(signal({confidence:.7}),100,()=>{}).match(/<button/g)??[]).length,0)
})
test('scanner: força inválida não produz NaN/Infinity no resumo ou barra', () => {
  for(const value of [NaN,Infinity,-1,2]) {
    const s=signal({confidence:value});assert.equal(scannerSituation(s,100).strength,null)
    const out=html(s,100);assert.ok(!out.includes('NaN'));assert.ok(!out.includes('Infinity'))
  }
})
