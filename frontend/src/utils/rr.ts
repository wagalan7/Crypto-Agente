// Arredondamento do Risco/Retorno (R:R) pra exibição — menos poluído nos cards
// e no push. Regra do usuário: até X,50 mantém X; a partir de X,51 sobe.
//   4.19 → 4 · 4.50 → 4 · 4.51 → 5
// (round-half-down: a metade exata NÃO sobe; só passa de meio que arredonda pra cima.)
export function roundRR(value: number | string | null | undefined): number {
  const x = typeof value === 'number' ? value : parseFloat(String(value ?? ''))
  if (!isFinite(x)) return 0
  const frac = x - Math.floor(x)
  return frac > 0.5 ? Math.ceil(x) : Math.floor(x)
}

// Pronto pra exibir: "1:4".
export function fmtRR(value: number | string | null | undefined): string {
  return `1:${roundRR(value)}`
}
