// Config de QA do Lote 04. Serve SOMENTE a app com fixtures locais: sem proxy
// para backend, sem ENV de produção, porta própria. Não substitui o
// `vite.config.ts` do projeto (que segue intocado).
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { resolve } from 'node:path'
import { QA_PREVIEW_HEADERS, QA_PREVIEW_ORIGIN } from './isolationPolicy'

const AQUI = resolve(import.meta.dirname ?? '.', '.')

export default defineConfig({
  root: AQUI,
  // Assets do app (logo, ícones) vêm de frontend/public, somente leitura.
  publicDir: resolve(AQUI, '../../public'),
  plugins: [react()],
  define: {
    // Host sentinela local: se algo escapar do mock, não vai para produção.
    'import.meta.env.VITE_API_URL': JSON.stringify(QA_PREVIEW_ORIGIN),
    'import.meta.env.VITE_OBSERVATION_API_URL': JSON.stringify(''),
  },
  server: {
    port: 5199,
    host: '127.0.0.1',
    strictPort: true,
    open: false,
    headers: QA_PREVIEW_HEADERS,
  },
  cacheDir: resolve(AQUI, '../../node_modules/.cache/cw-qa-vite'),
})
