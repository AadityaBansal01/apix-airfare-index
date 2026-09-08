import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The dashboard is a static export: it reads the JSON written by
// scripts/export_static.py into public/data and never calls the API at runtime.
// That is deliberate (see the module docstring in export_static.py), so there is
// no dev proxy here and nothing to configure for a backend.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173 },
  build: { outDir: 'dist', sourcemap: false },
})
