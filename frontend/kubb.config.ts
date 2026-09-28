import { defineConfig } from '@kubb/core'
import { pluginClient } from '@kubb/plugin-client'
import { pluginOas } from '@kubb/plugin-oas'
import { pluginReactQuery } from '@kubb/plugin-react-query'
import { pluginTs } from '@kubb/plugin-ts'

// /api/health is a liveness probe, not one of the eight contract operations —
// no UI calls it, so no hook is generated for it.
const exclude = [{ type: 'path' as const, pattern: /^\/api\/health$/ }]

export default defineConfig({
  root: '.',
  input: { path: './openapi.json' },
  // format/lint off: output must not depend on whichever formatter happens to be installed.
  // extension '': extensionless imports, since tsconfig does not allow importing .ts paths.
  output: { path: './src/api/gen', clean: true, format: false, lint: false, extension: { '.ts': '' } },
  plugins: [
    pluginOas({ generators: [] }),
    pluginTs({ output: { path: './types' }, exclude }),
    // importPath: every generated call uses src/api/client.ts (emitted verbatim, resolved from
    // gen/clients/ and gen/hooks/). Kubb v4 types `client` and `bundle` as `never` alongside it.
    pluginClient({
      output: { path: './clients' },
      importPath: '../../client',
      paramsType: 'object',
      exclude,
    }),
    pluginReactQuery({
      output: { path: './hooks' },
      client: { importPath: '../../client' },
      paramsType: 'object',
      suspense: false,
      exclude,
    }),
  ],
})
