import { useEffect, useState, useCallback } from 'react'

/**
 * usePushFocus — captura "foco" vindo de uma notificação push.
 *
 * Dois caminhos:
 * 1. App fechado: SW chama `openWindow('/?focus=BTC&tf=15m&event=tp2')`.
 *    Lemos os query params no mount.
 * 2. App aberto (aba já existente): SW dispara `client.postMessage({type:'push-click', data})`.
 *    Escutamos `navigator.serviceWorker.addEventListener('message', …)`.
 *
 * Retorna o foco atual + `clear()` pra resetar depois de aplicado.
 */
export interface PushFocus {
  symbol: string        // ex: "BTC" (base curto, como o backend envia)
  timeframe: string     // ex: "15m"
  event?: string        // tp1_partial | tp2 | be_plus | lost | expired_tp1 (só em outcome)
}

export function usePushFocus(): {
  focus: PushFocus | null
  clear: () => void
} {
  const [focus, setFocus] = useState<PushFocus | null>(() => {
    // Lê URL params no primeiro render (caso 1)
    if (typeof window === 'undefined') return null
    const params = new URLSearchParams(window.location.search)
    const symbol = params.get('focus')
    const tf = params.get('tf')
    if (!symbol || !tf) return null
    const event = params.get('event') || undefined
    return { symbol, timeframe: tf, event }
  })

  const clear = useCallback(() => {
    setFocus(null)
    // Limpa params da URL pra não disparar foco em refresh
    if (typeof window !== 'undefined' && window.history?.replaceState) {
      const url = new URL(window.location.href)
      url.searchParams.delete('focus')
      url.searchParams.delete('tf')
      url.searchParams.delete('event')
      window.history.replaceState({}, '', url.pathname + (url.search || '') + url.hash)
    }
  }, [])

  useEffect(() => {
    // Caso 2: app aberto, SW manda postMessage
    if (typeof navigator === 'undefined' || !navigator.serviceWorker) return
    const handler = (ev: MessageEvent) => {
      const msg = ev.data
      if (!msg || msg.type !== 'push-click' || !msg.data) return
      const d = msg.data
      // Backend manda symbol completo "BTC/USDT:USDT", usamos só o base
      const sym = typeof d.symbol === 'string' ? d.symbol.split('/')[0] : null
      const tf = typeof d.timeframe === 'string' ? d.timeframe : null
      if (!sym || !tf) return
      setFocus({ symbol: sym, timeframe: tf, event: d.event })
    }
    navigator.serviceWorker.addEventListener('message', handler)
    return () => navigator.serviceWorker.removeEventListener('message', handler)
  }, [])

  return { focus, clear }
}
