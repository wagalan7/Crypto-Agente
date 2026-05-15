import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    proxy: {
      '/clients': { target: 'http://localhost:8001', changeOrigin: true },
      '/agents': { target: 'http://localhost:8001', changeOrigin: true },
      '/content': { target: 'http://localhost:8001', changeOrigin: true },
      '/calendar': { target: 'http://localhost:8001', changeOrigin: true },
      '/analytics': { target: 'http://localhost:8001', changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
  },
})
