import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    // WSL/Windows often misses inotify; polling keeps HMR in sync with disk.
    watch: {
      usePolling: true,
      interval: 500,
    },
    proxy: {
      '/api': 'http://127.0.0.1:8080',
      '/ws': { target: 'ws://127.0.0.1:8080', ws: true },
    },
  },
  // Vite already falls back to index.html for unknown paths in dev;
  // production SPA fallback is handled by FastAPI.
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
