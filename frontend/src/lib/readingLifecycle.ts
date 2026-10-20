// Ciclo de vida de apresentação: não consulta API nem autoriza execução.
export interface ReadingClock {
  now(): number
  setInterval(callback: () => void, delay: number): unknown
  clearInterval(handle: unknown): void
}

const browserClock: ReadingClock = {
  now: () => Date.now(),
  setInterval: (callback, delay) => setInterval(callback, delay),
  clearInterval: handle => clearInterval(handle as ReturnType<typeof setInterval>),
}

/** O tempo passa mesmo quando uma requisição fica pendente; nenhum GET novo. */
export function startReadingClock(onTick: (now: number) => void, runtime = browserClock): () => void {
  onTick(runtime.now())
  const handle = runtime.setInterval(() => onTick(runtime.now()), 1_000)
  let stopped = false
  return () => {
    if (stopped) return
    stopped = true
    runtime.clearInterval(handle)
  }
}

export interface OverlayDocument {
  activeElement: { focus?: () => void; isConnected?: boolean } | null
  addEventListener(type: string, listener: EventListener): void
  removeEventListener(type: string, listener: EventListener): void
}

/** Uma inscrição por montagem. Callback atual; foco só retorna ao desmontar. */
export function attachOverlayDismiss(getOnClose: () => () => void, doc: OverlayDocument): () => void {
  const previous = doc.activeElement
  const onKey: EventListener = event => {
    if ((event as KeyboardEvent).key === 'Escape') getOnClose()()
  }
  doc.addEventListener('keydown', onKey)
  let detached = false
  return () => {
    if (detached) return
    detached = true
    doc.removeEventListener('keydown', onKey)
    if (previous?.isConnected !== false && typeof previous?.focus === 'function') previous.focus()
  }
}

export type SettledReading<T> =
  | { ok: true; v: T; receivedAtMs: number }
  | { ok: false; e: unknown }

/** Carimba CADA resposta quando chega, não quando a fonte mais lenta termina. */
export function settleReading<T>(promise: Promise<T>, now = () => Date.now()): Promise<SettledReading<T>> {
  return promise.then(v => ({ ok: true as const, v, receivedAtMs: now() }))
    .catch((e: unknown) => ({ ok: false as const, e }))
}
