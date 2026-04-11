import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// https://vitejs.dev/config/
//
// The dev server proxies /api and /healthz to the FastAPI backend so the
// browser can talk to `/api/...` without CORS ceremony and without hard-
// coding the backend host. `VITE_API_BASE` lets you override the target
// (e.g. to point at a running Pi on the local network).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.VITE_API_BASE || "http://localhost:8080",
        changeOrigin: true,
      },
      "/healthz": {
        target: process.env.VITE_API_BASE || "http://localhost:8080",
        changeOrigin: true,
      },
    },
  },
});
