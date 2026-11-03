/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Development: the API runs on 8000 and every API path is proxied so cookies
// stay same-origin. Production: `vite build` writes dist/, which the Python
// image serves as FRONTEND_DIR; the API is then the same origin.
const API_PATHS = [
  "/auth", "/catalog", "/projects", "/jobs", "/artifacts", "/account", "/profiles", "/scores", "/plans",
  "/arrangements", "/runs", "/feedback", "/candidate-rankings", "/verify", "/render", "/render-and-verify",
  "/plan", "/arrange", "/health", "/ready", "/version", "/legal", "/vendor",
];

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: Object.fromEntries(API_PATHS.map((path) => [path, { target: "http://127.0.0.1:8000", changeOrigin: false }])),
  },
  build: {
    outDir: "dist",
    sourcemap: true,
    // The notation engine is 7 MB of WebAssembly, loaded on demand; nothing
    // in the app's own bundle should come near that.
    chunkSizeWarningLimit: 600,
  },
  test: {
    environment: "jsdom",
    globals: false,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    include: ["src/**/*.test.{ts,tsx}"],
    coverage: { provider: "v8", reporter: ["text", "html"], include: ["src/**/*.{ts,tsx}"], exclude: ["src/api/schema.ts", "src/test/**"] },
  },
});
