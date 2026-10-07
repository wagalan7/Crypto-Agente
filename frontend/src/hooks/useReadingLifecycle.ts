import { useEffect, useRef, useState } from 'react'
import { attachOverlayDismiss, startReadingClock } from '../lib/readingLifecycle'

export function useReadingClock(): number {
  const [nowMs, setNowMs] = useState(() => Date.now())
  useEffect(() => startReadingClock(setNowMs), [])
  return nowMs
}

export function useOverlayDismiss(onClose: () => void): void {
  const latestClose = useRef(onClose)
  latestClose.current = onClose
  useEffect(() => attachOverlayDismiss(() => latestClose.current, document), [])
}
