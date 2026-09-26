import path from "node:path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

// The build lands inside the Python package, so the app (and the .exe) ship
// it as plain static files - no Node needed at runtime.
export default defineConfig({
  base: "./",
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(__dirname, "./src") } },
  build: {
    outDir: "../peribatt/webui",
    emptyOutDir: true,
    chunkSizeWarningLimit: 800,
  },
  server: {
    // `npm run dev` + a running app: point the proxy at the app's port.
    proxy: { "/api": process.env.PERIBATT_API ?? "http://127.0.0.1:8765" },
  },
})
