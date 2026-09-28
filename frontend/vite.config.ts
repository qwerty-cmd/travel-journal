import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { tanstackRouter } from "@tanstack/router-plugin/vite";
import { VitePWA } from "vite-plugin-pwa";

// SPA mode only — no TanStack Start SSR, since the build target is a Node-free
// Docker image where FastAPI serves static files (spec Section 4).
export default defineConfig({
  plugins: [
    tanstackRouter({ target: "react", autoCodeSplitting: true }),
    react(),
    VitePWA({
      registerType: "autoUpdate",
      workbox: {
        // Offline queue (spec Section 4): captured stops/photos are written to
        // IndexedDB by src/offline/ and synced when connectivity returns. The
        // service worker here only handles asset caching for installability —
        // the queue itself is app-level logic, not a Workbox strategy.
        globPatterns: ["**/*.{js,css,html,svg,png,ico}"],
        // Cold open of any deep link (/t/$slug...) offline renders the
        // precached shell. /api is denylisted so API calls always hit the
        // network — no runtimeCaching for the API, TanStack Query's persisted
        // cache is the offline read path.
        navigateFallback: "index.html",
        navigateFallbackDenylist: [/^\/api\//],
      },
      manifest: {
        name: "Bike Trip Journal",
        short_name: "Bike Trip",
        display: "standalone",
        start_url: "/",
      },
    }),
  ],
  server: {
    proxy: {
      // Local dev: Vite dev server proxies /api to the FastAPI container so
      // there's no CORS config to maintain (matches prod, where the same
      // container serves both).
      "/api": "http://localhost:8000",
    },
  },
});
