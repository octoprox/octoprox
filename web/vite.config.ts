import { readFileSync } from 'node:fs'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// The release this UI belongs to, from the one place it is declared. The UI
// compares it with the release every API response names (src/utils/appVersion.ts).
const version = readFileSync(new URL('../pyproject.toml', import.meta.url), 'utf8').match(/^version\s*=\s*"([^"]+)"/m)?.[1]
if (!version) throw new Error('No version in ../pyproject.toml')

export default defineConfig({
  plugins: [react(), tailwindcss()],
  define: {
    __OCTOPROX_VERSION__: JSON.stringify(version),
  },
  server: {
    port: 3000,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
