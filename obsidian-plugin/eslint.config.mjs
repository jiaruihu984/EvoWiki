import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["main.js", "node_modules", "test-vault"] },
  ...tseslint.configs.recommendedTypeChecked,
  {
    languageOptions: { parserOptions: { projectService: true, tsconfigRootDir: import.meta.dirname } },
    rules: {
      "@typescript-eslint/no-floating-promises": "error",
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_" }],
      "no-restricted-properties": ["error", { property: "innerHTML", message: "Build DOM with h(); never parse markup." }],
      "no-restricted-globals": ["error", { name: "fetch", message: "Use the injected transports (CORS)." }],
    },
  },
);
