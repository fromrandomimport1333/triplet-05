import path from "node:path";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The built screen is served by the Python timeline server from src/timeline/static/ (committed,
// so the demo laptop needs no Node). `npm run dev` proxies the data routes to a running timeline.
export default defineConfig({
  plugins: [react()],
  base: "/static/",
  resolve: { alias: { "@": path.resolve(__dirname, "src") } },
  build: { outDir: "../src/timeline/static", emptyOutDir: true, sourcemap: false },
  server: {
    port: 5173,
    proxy: { "/events": "http://127.0.0.1:8091", "/api": "http://127.0.0.1:8091" },
  },
});
