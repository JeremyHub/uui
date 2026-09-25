import { dirname } from "node:path";
import { fileURLToPath } from "node:url";
import js from "@eslint/js";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  // frontend/*.js is tsc's output, written from frontend/src.
  { ignores: ["frontend/*.js", "node_modules/", ".venv/"] },
  js.configs.recommended,
  ...tseslint.configs.recommendedTypeChecked,
  {
    languageOptions: {
      globals: globals.browser,
      parserOptions: {
        project: ["./tsconfig.json", "./tsconfig.node.json"],
        // import.meta.dirname is Node 20.11+.
        tsconfigRootDir: dirname(fileURLToPath(import.meta.url)),
      },
    },
    rules: {
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_" }],
      // Hosts and backends implement async interfaces, and some have nothing to await.
      "@typescript-eslint/require-await": "off",
    },
  },
);
