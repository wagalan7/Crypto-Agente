import type { TradeSignal } from '../types'

/** Leitura visual de preço/plano. Não consulta gates nem autoriza entrada. */
export type ScannerSituation = {
  state: 'PRICE_UNKNOWN' | 'PLAN_UNKNOWN' | 'NEUTRAL' | 'STOP_PASSED' | 'TARGET_PASSED' | 'OUTSIDE_ZONE' | 'IN_ZONE' | 'MARKET_REFERENCE' | 'NO_ZONE'
  title: string
  detail: string
  tone: 'muted' | 'attention'
  price: number | null
  zone: { bottom: number; top: number } | null
  priceVsZone: string
  strength: number | null
  executionConfirmed: false
  probability: null
}

export function positivePrice(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value > 0
}

export function scannerSituation(signal: TradeSignal, livePrice?: number): ScannerSituation {
  const price = positivePrice(livePrice) ? livePrice : null
  const strength = typeof signal.confidence === 'number' && Number.isFinite(signal.confidence)
    && signal.confidence >= 0 && signal.confidence <= 1 ? Math.round(signal.confidence * 100) : null
  const rawZone = signal.trade_plan?.entry_zone
  const zone = rawZone && rawZone.type !== 'market' && positivePrice(rawZone.bottom)
    && positivePrice(rawZone.top) && rawZone.bottom <= rawZone.top
    ? { bottom: rawZone.bottom, top: rawZone.top } : null
  const priceVsZone = price == null ? 'Preço atual não confirmado'
    : !zone ? 'Sem zona limite válida para comparar'
    : price < zone.bottom ? 'Preço abaixo da zona de entrada'
    : price > zone.top ? 'Preço acima da zona de entrada'
    : 'Preço dentro da zona de entrada'
  const base = { price, zone, priceVsZone, strength, executionConfirmed: false as const, probability: null }
  const result = (state: ScannerSituation['state'], title: string, detail: string, tone: ScannerSituation['tone'] = 'attention'): ScannerSituation =>
    ({ ...base, state, title, detail, tone })

  // `signal.entry` é um nível do plano, nunca substituto de cotação atual.
  if (price == null) return result('PRICE_UNKNOWN', 'Preço atual não confirmado', 'Não é possível comparar a cotação com a entrada. O plano abaixo não representa o preço atual.', 'muted')
  if (signal.direction !== 'long' && signal.direction !== 'short') return result('NEUTRAL', 'Sem viés de compra ou venda', 'O scanner está neutro. Os níveis são referências técnicas, não uma indicação de entrada.', 'muted')
  const long = signal.direction === 'long'
  if (!positivePrice(signal.entry) || !positivePrice(signal.stop_loss) || !positivePrice(signal.tp1)
    || (long ? signal.stop_loss >= signal.entry || signal.tp1 <= signal.entry : signal.stop_loss <= signal.entry || signal.tp1 >= signal.entry)
    || (rawZone?.type !== undefined && rawZone.type !== 'market' && !zone)) {
    return result('PLAN_UNKNOWN', 'Plano precisa de conferência', 'Há níveis ausentes ou incompatíveis com o lado indicado. Não foi recalculado nenhum stop ou alvo.')
  }
  if (long ? price <= signal.stop_loss : price >= signal.stop_loss) return result('STOP_PASSED', 'Preço além do stop apresentado', 'A cotação está no stop ou além dele. Esses níveis não descrevem uma nova entrada validada.')
  if (long ? price >= signal.tp1 : price <= signal.tp1) return result('TARGET_PASSED', 'Preço já alcançou o Alvo 1', 'Não confunda esse alvo do plano com um ganho realizado. A entrada original não deve ser tratada como uma nova oportunidade confirmada.')
  if (zone && (price < zone.bottom || price > zone.top)) return result('OUTSIDE_ZONE', 'Preço fora da zona de entrada', 'O preço ainda não está na região do plano. A volta à zona, por si só, não confirma uma operação.')
  if (zone) return result('IN_ZONE', 'Preço na zona · falta confirmação', 'Estar na zona não confirma os gates do bot, saldo, proteção ou probabilidade. Esta análise não autoriza uma entrada.', 'muted')
  if (rawZone?.type === 'market') return result('MARKET_REFERENCE', 'Referência a mercado · não é autorização', 'O plano não define uma zona limite. A execução e os gates do bot não estão confirmados nesta tela.', 'muted')
  return result('NO_ZONE', 'Plano sem zona limite · falta confirmação', 'Não foi publicada uma zona limite. A ausência da zona não prova que a entrada seja a mercado nem que os gates do bot estejam liberados.', 'muted')
}
