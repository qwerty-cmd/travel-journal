import tseslint from "typescript-eslint";
import sonarjs from "eslint-plugin-sonarjs";

export default tseslint.config(
  { ignores: ["dist", "dev-dist", "src/api/gen", "src/routeTree.gen.ts"] },
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: { parser: tseslint.parser },
    plugins: { sonarjs },
    rules: { "sonarjs/cognitive-complexity": ["warn", 15] },
  },
);
