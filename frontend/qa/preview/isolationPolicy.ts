// Somente o preview sintético importa esta política. Não altera o app publicado.
export const QA_PREVIEW_ORIGIN = 'http://127.0.0.1:5199'

// O header chega antes de qualquer script; o meta repete a proteção no HTML.
// Vite/React precisam dos scripts/estilos locais e do WS local de HMR. Recursos
// externos (incluindo scripts, imagens, frames e workers) nunca são autorizados.
export const QA_PREVIEW_CSP = [
  "default-src 'none'",
  "script-src 'self' 'unsafe-inline'",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob:",
  "font-src 'self' data:",
  "connect-src 'self' ws://127.0.0.1:5199",
  "object-src 'none'",
  "frame-src 'none'",
  "worker-src 'none'",
  "form-action 'none'",
  "base-uri 'self'",
].join('; ')

export const QA_PREVIEW_HEADERS = {
  'Content-Security-Policy': QA_PREVIEW_CSP,
  'X-DNS-Prefetch-Control': 'off',
  'Referrer-Policy': 'no-referrer',
}
