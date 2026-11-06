import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// El panel se sirve desde FastAPI en /admin. Las rutas de API quedan bajo /api/admin.
// El puerto de FastAPI en desarrollo es 18000 por defecto; se puede cambiar con LACUSINE_API_PORT
// (la misma variable que usa docker-compose). Es una variable de proceso, no VITE_*: no llega al navegador.
const apiPort = process.env.LACUSINE_API_PORT ?? "18000";

export default defineConfig({
  base: "/admin/",
  plugins: [react(), tailwindcss()],
  build: { outDir: "dist", emptyOutDir: true, sourcemap: false },
  server: {
    proxy: {
      "/api": { target: `http://127.0.0.1:${apiPort}`, changeOrigin: false },
    },
  },
});
