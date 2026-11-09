import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

export default defineConfig({
  resolve: { alias: [
    { find: "obsidian", replacement: fileURLToPath(new URL("./test/obsidian-mock.ts", import.meta.url)) },
    // the frame's script as text comes from the build (esbuild.config.mjs); jsdom runs no iframe, tests use frame/inline-transport.ts
    { find: "virtual:evoui-frame", replacement: fileURLToPath(new URL("./test/evoui-frame-stub.ts", import.meta.url)) },
    // jsdom has no WebGL: the 知识图谱's Sigma is the recording stand-in in every test
    { find: /^sigma$/, replacement: fileURLToPath(new URL("./src/wiki/graph-page/fake-sigma.ts", import.meta.url)) },
  ] },
  // Each jsdom worker is expensive; 31 competing workers on a 32-thread machine
  // made the manually driven animation tests exceed their timeout only in the full suite.
  test: { maxWorkers: 4, environment: "jsdom", include: ["src/**/*.test.ts", "test/**/*.test.ts"], exclude: ["test/contract/**"] },
});
