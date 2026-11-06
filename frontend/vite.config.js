import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The dashboard talks to the FastAPI backend on 8080. Proxying in dev keeps
// the browser on one origin, so there is no CORS configuration to get wrong
// and the production build (served by FastAPI itself) uses the same paths.
// Override when 8080 is taken:  VITE_API_TARGET=http://127.0.0.1:8099 npm run dev
const API_TARGET = process.env.VITE_API_TARGET || 'http://127.0.0.1:8080'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: Object.fromEntries(
      ['/health', '/schema', '/query', '/healing'].map((path) => [
        path,
        { target: API_TARGET, changeOrigin: true },
      ])
    ),
  },
  build: { outDir: 'dist', emptyOutDir: true },
})
