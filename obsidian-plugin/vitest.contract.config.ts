import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

export default defineConfig({
  resolve: { alias: [
    { find: "obsidian", replacement: fileURLToPath(new URL("./test/obsidian-mock.ts", import.meta.url)) },
    // no WebGL in node: the 知识图谱's Sigma is the recording stand-in (as in vitest.config.ts)
    { find: /^sigma$/, replacement: fileURLToPath(new URL("./src/wiki/graph-page/fake-sigma.ts", import.meta.url)) },
  ] },
  test: { environment: "node", include: ["test/contract/**/*.test.ts"], setupFiles: ["test/contract/setup-window.ts"], testTimeout: 60_000 },
});
