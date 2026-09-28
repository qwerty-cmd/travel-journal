import { defineConfig } from "vitest/config";

// Deliberately separate from vite.config.ts: inheriting its plugins would run
// tanstackRouter() and regenerate src/routeTree.gen.ts on every test run.
// esbuild handles TSX via tsconfig's "jsx": "react-jsx", so no React plugin.
export default defineConfig({
  test: {
    environment: "jsdom",
  },
});
