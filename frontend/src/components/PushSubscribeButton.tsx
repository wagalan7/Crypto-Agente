import { useEffect, useState, useCallback } from 'react'
import { Bell, BellOff, BellRing } from 'lucide-react'

const BACKEND = import.meta.env.VITE_API_URL ?? 'https://crypto-agente-production.up.railway.app'
const LS_KEY = 'crypto_ai_push_enabled'

type State = 'unsupported' | 'denied' | 'unsubscribed' | 'subscribed' | 'loading'

function urlBase64ToUint8Array(base64String: string): Uint8Array {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4)
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/')
  const raw = atob(base64)
  const arr = new Uint8Array(raw.length)
  for (let i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i)
  return arr
}

export default function PushSubscribeButton() {
  const [state, setState] = useState<State>('loading')
  const [vapidKey, setVapidKey] = useState<string>('')

  const supported =
    typeof window !== 'undefined' &&
    'serviceWorker' in navigator &&
    'PushManager' in window &&
    'Notification' in window

  // Pega VAPID public key + verifica status atual
  useEffect(() => {
    if (!supported) {
      setState('unsupported')
      return
    }
    ;(async () => {
      try {
        const res = await fetch(`${BACKEND}/api/push/vapid-public-key`)
        const data = await res.json()
        if (!data.enabled || !data.public_key) {
          setState('unsupported')
          return
        }
        setVapidKey(data.public_key)

        if (Notification.permission === 'denied') {
          setState('denied')
          return
        }
        const reg = await navigator.serviceWorker.ready
        const sub = await reg.pushManager.getSubscription()
        setState(sub ? 'subscribed' : 'unsubscribed')
      } catch (e) {
        console.warn('Push setup error:', e)
        setState('unsupported')
      }
    })()
  }, [supported])

  const subscribe = useCallback(async () => {
    if (!vapidKey) return
    setState('loading')
    try {
      const perm = await Notification.requestPermission()
      if (perm !== 'granted') {
        setState(perm === 'denied' ? 'denied' : 'unsubscribed')
        return
      }
      const reg = await navigator.serviceWorker.ready
      let sub = await reg.pushManager.getSubscription()
      if (!sub) {
        sub = await reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(vapidKey).buffer as ArrayBuffer,
        })
      }
      const subJson = sub.toJSON() as { endpoint?: string; keys?: Record<string, string> }
      await fetch(`${BACKEND}/api/push/subscribe`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          endpoint: subJson.endpoint,
          keys: subJson.keys,
          user_agent: navigator.userAgent.slice(0, 250),
          filters: { notify_a_plus: true, notify_a: true, notify_b: true },
        }),
      })
      localStorage.setItem(LS_KEY, '1')
      setState('subscribed')
    } catch (e) {
      console.warn('subscribe error:', e)
      setState('unsubscribed')
    }
  }, [vapidKey])

  const unsubscribe = useCallback(async () => {
    setState('loading')
    try {
      const reg = await navigator.serviceWorker.ready
      const sub = await reg.pushManager.getSubscription()
      if (sub) {
        const endpoint = sub.endpoint
        await sub.unsubscribe()
        await fetch(`${BACKEND}/api/push/unsubscribe`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ endpoint }),
        }).catch(() => {})
      }
      localStorage.removeItem(LS_KEY)
      setState('unsubscribed')
    } catch (e) {
      console.warn('unsubscribe error:', e)
    }
  }, [])

  if (state === 'unsupported') return null

  if (state === 'denied') {
    return (
      <button
        title="Notificações bloqueadas no navegador. Habilite manualmente nas configurações do site."
        className="flex items-center gap-1 px-2 py-1 rounded text-xs border border-red-500/40 bg-red-500/10 text-red-300 cursor-not-allowed"
      >
        <BellOff className="w-3.5 h-3.5" />
        <span className="hidden sm:inline">Notif. bloqueadas</span>
      </button>
    )
  }

  if (state === 'subscribed') {
    return (
      <button
        onClick={unsubscribe}
        title="Push ativo — A+ e A. Toque para desativar."
        className="flex items-center gap-1 px-2 py-1 rounded text-xs border border-emerald-500/40 bg-emerald-500/10 text-emerald-300 hover:bg-emerald-500/20"
      >
        <BellRing className="w-3.5 h-3.5" />
        <span className="hidden sm:inline">Push ON</span>
      </button>
    )
  }

  return (
    <button
      onClick={subscribe}
      disabled={state === 'loading'}
      title="Receber push quando aparecer recomendação A+ ou A"
      className="flex items-center gap-1 px-2 py-1 rounded text-xs border border-slate-700 bg-slate-800 hover:bg-slate-700 text-slate-200 disabled:opacity-50"
    >
      <Bell className="w-3.5 h-3.5" />
      <span className="hidden sm:inline">Ativar push</span>
    </button>
  )
}
