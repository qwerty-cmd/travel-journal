// Generates src/routeTree.gen.ts when it is missing, so `npm test` works on a
// fresh clone. The file is gitignored (and .dockerignored) because the
// tanstackRouter() Vite plugin regenerates it on every dev/build run, but
// vitest.config.ts deliberately does not load that plugin. Uses the same
// generator the plugin runs, with the same options as vite.config.ts.
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { Generator, getConfig } from "@tanstack/router-generator";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");

if (!existsSync(resolve(root, "src/routeTree.gen.ts"))) {
  const config = getConfig({ target: "react", autoCodeSplitting: true }, root);
  await new Generator({ config, root }).run();
}
