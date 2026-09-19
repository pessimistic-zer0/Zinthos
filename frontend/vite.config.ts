import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The engine (FastAPI) runs on :3000 by default — see backend/engine/config.py.
// Proxying /api through the dev server keeps the browser same-origin, so the engine's
// CORS allowlist (localhost:5173) is a fallback rather than the thing we depend on.
const ENGINE = process.env.SONIC_ENGINE ?? 'http://127.0.0.1:3000'

export default defineConfig({
  plugins: [react()],
  server: {
    // Bind explicitly: Vite otherwise picks IPv6 loopback only on this box, and
    // http://127.0.0.1:5173 then refuses the connection.
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': {
        target: ENGINE,
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ''),
      },
    },
  },
})
