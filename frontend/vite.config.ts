import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      // Forwards /api/* to the FastAPI backend so the browser makes
      // same-origin requests and no CORS setup is needed in dev.
      // `ws` also carries the terminal sockets: /api/agents/<id>/ws and
      // /api/containers/<id>/shell.
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        ws: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
})
