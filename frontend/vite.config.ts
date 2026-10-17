import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The browser only ever talks to one origin. In dev, Vite proxies /api/<area>
// to the owning service; in Docker, nginx does the same (frontend/nginx.conf).
const API_PROXY: Record<string, string> = {
  "/api/products": "http://127.0.0.1:8001",
  "/api/auth": "http://127.0.0.1:8002",
  "/api/users": "http://127.0.0.1:8002",
  "/api/orders": "http://127.0.0.1:8003",
};

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: Object.fromEntries(
      Object.entries(API_PROXY).map(([prefix, target]) => [
        prefix,
        { target, changeOrigin: true, rewrite: (path: string) => path.replace(/^\/api/, "") },
      ]),
    ),
  },
  build: { outDir: "dist", sourcemap: false },
});
