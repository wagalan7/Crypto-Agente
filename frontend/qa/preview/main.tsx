// Entrada de QA do Lote 04. Instala o isolamento ANTES de importar a app —
// se a instalação falhar, a app NÃO é montada (nunca cai para produção).
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import '../../src/index.css'
import { installQaIsolation } from './mock'

const root = document.getElementById('root')!

try {
  installQaIsolation()
} catch (e) {
  root.innerHTML = `<pre style="color:#fca5a5;padding:16px;font:13px ui-monospace">`
    + `[QA] isolamento falhou — app NÃO montada.\n${String(e)}</pre>`
  throw e
}

const { default: App } = await import('../../src/App')

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
