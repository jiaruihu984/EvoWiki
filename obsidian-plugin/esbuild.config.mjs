import { createHash } from "node:crypto";
import { builtinModules } from "node:module";
import esbuild from "esbuild";

// The EvoUI frame's script (src/wiki/evoui/frame/entry.ts) as text plus its CSP hash: frame-host.ts writes it into the
// frame's srcdoc, and nothing of it runs in the main window.
const evouiFrame = {
  name: "evoui-frame",
  setup(build) {
    build.onResolve({ filter: /^virtual:evoui-frame$/ }, (args) => ({ path: args.path, namespace: "evoui-frame" }));
    build.onLoad({ filter: /.*/, namespace: "evoui-frame" }, async () => {
      const out = await esbuild.build({ entryPoints: ["src/wiki/evoui/frame/entry.ts"], bundle: true, write: false, format: "iife", target: "es2021", minify: true, charset: "ascii", legalComments: "none" });
      const code = out.outputFiles[0].text.replace(/<(!--|\/?script)/gi, "\\x3c$1").trimEnd();
      const hash = `sha256-${createHash("sha256").update(code).digest("base64")}`;
      return { contents: `export default ${JSON.stringify({ code, hash })};`, loader: "js" };
    });
  },
};

const prod = process.argv[2] === "production";
const ctx = await esbuild.context({
  entryPoints: ["src/main.ts"],
  bundle: true,
  external: ["obsidian", "electron", "@codemirror/state", "@codemirror/view", ...builtinModules, ...builtinModules.map((m) => `node:${m}`)],
  format: "cjs",
  platform: "node",
  target: "es2021",
  sourcemap: prod ? false : "inline",
  treeShaking: true,
  outfile: "main.js",
  logLevel: "info",
  plugins: [evouiFrame],
});
if (prod) { await ctx.rebuild(); await ctx.dispose(); } else { await ctx.watch(); }
