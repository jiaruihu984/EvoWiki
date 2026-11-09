/**
 * Static isolation rules for the personal-wiki module (src/wiki/, excluding E3a's src/wiki/note/). Scans the
 * production .ts files of src/wiki/ and of the controller's modules (src/wiki/controller/); test files and the
 * test-only helpers (*-test-kit.ts) are skipped, and a separate check makes sure no production file imports those
 * helpers (nor the contract tests' generated JSON Schema).
 */
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const WIKI = __dirname;
const SRC = join(WIKI, "..");
const TEST_ONLY = /-test-kit\.ts$/;
const tsIn = (dir: string, prefix: string): string[] => readdirSync(join(WIKI, dir), { withFileTypes: true })
  .filter((e) => e.isFile() && e.name.endsWith(".ts") && !e.name.endsWith(".test.ts")).map((e) => prefix + e.name);
// The controller is a facade (controller.ts) over its modules in controller/: they are held to the same rules.
const all = [...tsIn(".", ""), ...tsIn("controller", "controller/")].sort();
const prod = all.filter((f) => !TEST_ONLY.test(f));
const text = (f: string) => readFileSync(join(WIKI, f), "utf8");
const using = (re: RegExp) => prod.filter((f) => re.test(text(f)));
const count = (s: string, re: RegExp) => (s.match(new RegExp(re.source, "g")) ?? []).length;
/** Drops // line comments and block comments (good enough for our sources: no "//" inside CJK-bearing strings there). */
// CRLF first: `.` does not match "\r", so a `// …` comment on a CRLF line would otherwise survive.
const code = (s: string) => s.replace(/\r\n?/g, "\n").replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:"'`])\/\/.*$/gm, "$1");

describe("wiki isolation", () => {
  it("scans the expected files (sanity)", () => {
    for (const f of ["controller.ts", "controller/core.ts", "controller/notes.ts", "wiring.ts", "api.ts", "transport.ts", "wiki-view.ts", "fake.ts"]) {
      expect(prod).toContain(f);
    }
    expect(prod).not.toContain("controller-test-kit.ts");
    expect(prod).not.toContain("render-test-kit.ts");
    expect(all).toContain("controller-test-kit.ts");
  });

  it("test-only helpers are imported by no production file", () => {
    for (const f of prod) expect(text(f), f).not.toMatch(/from\s+["'][^"']*(-test-kit|wire-schema(\.json)?)["']/);
    const outside = ["main.ts"].map((f) => readFileSync(join(SRC, f), "utf8"));
    for (const s of outside) expect(s).not.toMatch(/test-kit|wire-schema|\/fake["']/);
  });

  it("1. never writes the vault, except the profile note 「我」 (a note he owns and edits): through its owned vault port and folder when missing", () => {
    expect(using(/\b(vault|fileManager|adapter)\s*\.|processFrontMatter|\.process\s*\(|\.modify\s*\(|\.create\s*\(|\.createFolder\s*\(|\.trashFile\s*\(/))
      .toEqual(["profile-backup.ts", "profile-note.ts", "profile-vault.ts", "wiring.ts"]);
    // Nothing is modified, renamed or deleted; no adapter / fileManager.
    expect(using(/fileManager|adapter\s*\.|processFrontMatter|\.modify\s*\(|\.trashFile\s*\(|vault\.(delete|rename|append|modify)/))
      .toEqual([]);
    expect(using(/\.create(Folder)?\s*\(/)).toEqual(["profile-backup.ts", "profile-note.ts", "profile-vault.ts"]);
    const pn = code(text("profile-note.ts"));
    expect(pn).not.toMatch(/\bvault\b|from\s+["']obsidian["']/);
    expect(pn).toMatch(/port\.create\s*\(/);
    expect(pn).toMatch(/port\.replace\s*\(/);
    // the backups touch the vault only through the injected store (notes/backup-store.ts), never directly
    expect(code(text("profile-backup.ts"))).not.toMatch(/\bvault\b|\badapter\b|from\s+["']obsidian["']/);
    const pv = code(text("profile-vault.ts"));
    expect([count(pv, /app\.vault\.process\s*\(/), count(pv, /app\.vault\.create\s*\(/), count(pv, /app\.vault\.createFolder\s*\(/)])
      .toEqual([1, 1, 1]);
  });

  it("2. never reads vault content or the editor; the only .read( are findWikiNote's (wiring.ts) and 「我」's (profile-vault.ts)", () => {
    // code only: doc comments may say "the editor." in prose
    expect(prod.filter((f) => /cachedRead|getSelection|metadataCache|getFileCache|getActiveFile|editor\./.test(code(text(f))))).toEqual([]);
    expect(using(/vault\.read/)).toEqual(["profile-vault.ts"]);
    expect(using(/\.read\s*\(/)).toEqual(["profile-note.ts", "profile-vault.ts", "wiring.ts"]);
    expect(code(text("profile-note.ts"))).toMatch(/port\.read\s*\(/);
    expect(code(text("profile-vault.ts"))).toMatch(/app\.vault\.read\s*\(/);
    const w = code(text("wiring.ts"));
    expect(count(w, /app\.vault\.read\s*\(/)).toBe(0);
    expect(w).toContain("obsidianWikiNoteVault(app)");
  });

  it("3. syncWikiNote is called only from wiring.ts (once); the controller runs it via deps.sync.run", () => {
    expect(using(/\.sync\.run\s*\(/)).toEqual([]); // the controller uses a local `sync` from this.deps.sync
    expect(using(/\bsync\.run\s*\(/)).toEqual(["controller/notes.ts"]);
    expect(using(/syncWikiNote\s*\(/)).toEqual(["wiring.ts"]);
    expect(count(text("wiring.ts"), /syncWikiNote\s*\(/)).toBe(1);
  });

  it("4. no imports of the old sidebar / V / settings / notes / graph / concept (owned profile identity may import settings types; wiring may validate the folder)", () => {
    const re = /from\s+["']\.\.\/(sidebar|adapters|settings|notes|graph|concept)\b[^"']*["']/;
    // controller/ sits one level down: src/ is ../../ there.
    const reDeep = /from\s+["']\.\.\/\.\.\/(sidebar|adapters|settings|notes|graph|concept)\b[^"']*["']/;
    expect(prod.filter((f) => (f.startsWith("controller/") ? reDeep : re).test(text(f)))).toEqual(["profile-path.ts", "wiring.ts"]);
    expect(text("profile-path.ts").split(/\r?\n/).filter((l) => re.test(l))).toEqual(['import type { PluginData } from "../settings";']);
    const imports = text("wiring.ts").split(/\r?\n/).filter((l) => re.test(l));
    expect(imports).toEqual(['import { validateNotesFolder } from "../notes/note-format";']);
  });

  it("5. only api.ts spells /wiki URLs", () => {
    expect(using(/["'`]\/wiki\b/)).toEqual(["api.ts"]);
  });

  it("6. the fake client is constructed only in wiring.ts (demo mode) and the dev-only demo fixture", () => {
    // fixtures-demo.ts hands what the demo does not cover (new goals, 问, guidelines…) to a plain fake.
    expect(using(/new FakeWikiApi\s*\(/)).toEqual(["fixtures-demo.ts", "wiring.ts"]);
  });

  it("7. no CJK literals outside copy.ts / format.ts / fixtures.ts / fake.ts (comments ignored)", () => {
    const strict = prod.filter((f) => /^(controller|wiki-renderer|wiki-view|wiring|actions|state)\.ts$|^render-.*\.ts$|^controller\//.test(f));
    expect(strict).toEqual(expect.arrayContaining(["controller.ts", "controller/core.ts", "controller/page.ts", "render-common.ts",
      "wiki-renderer.ts", "wiki-view.ts", "wiring.ts", "actions.ts", "state.ts"]));
    for (const f of strict) expect(code(text(f)), f).not.toMatch(/[一-鿿]/);
    expect(text("note-port.ts")).not.toMatch(/[一-鿿]/);
  });

  it("9. embed/ (interactives) writes only through AssetFetcher's store: ensureFolder + one create, under assets/", () => {
    const dir = join(WIKI, "embed");
    const files = readdirSync(dir).filter((f) => f.endsWith(".ts") && !f.endsWith(".test.ts")).sort();
    expect(files).toEqual(["assets.ts", "embed-block.ts", "image-block.ts"]);
    const image = code(readFileSync(join(dir, "image-block.ts"), "utf8"));
    expect(image).not.toMatch(/\bvault\b|adapter|\.create\s*\(|\.process\s*\(|\.modify\s*\(|from\s+["']obsidian["']/);
    const block = code(readFileSync(join(dir, "embed-block.ts"), "utf8"));
    expect(block).not.toMatch(/\bvault\b|adapter|\.create\s*\(|\.process\s*\(|\.modify\s*\(|from\s+["']obsidian["']/);
    // allow-scripts only, content only through srcdoc (R8.2).
    expect(block).not.toMatch(/allow-same-origin|allow-top-navigation|allow-popups|allow-forms|getResourcePath/);
    // The host sends only a typed locale to explicitly managed HTML. The gate forwards it only from its parent.
    const host = block.replace(/const GATE_SCRIPT[\s\S]*?;\n/, "");
    expect(count(host, /postMessage/)).toBe(1);
    expect(host).toContain('html.includes("data-evowiki-locale")');
    expect(host).toContain('f.contentWindow?.postMessage({ t: "locale", locale: uiLanguage() }, "*")');
    expect(host).not.toMatch(/\bfetch\s*\(|XMLHttpRequest|WebSocket/);
    expect(block).toContain("e.source===parent&&e.data&&e.data.t==='locale'");
    expect(block).toContain("e.data.locale==='zh'||e.data.locale==='en'");
    // The one message listener is the frame-height handshake: it accepts only messages from its own frame's window.
    expect(count(block, /win\.addEventListener\(\s*["']message/)).toBe(1);
    expect(block).toMatch(/ev\.source !== cur\.contentWindow/);
    const a = code(readFileSync(join(dir, "assets.ts"), "utf8"));
    expect(count(a, /\.create\s*\(/)).toBe(1);
    expect(count(a, /this\.store\.create\(path, html\)/)).toBe(1);
    expect(a).not.toMatch(/\.process\s*\(|\.modify\s*\(|\b(vault|store)\.(delete|trash\w*|rename)\s*\(|adapter/);
    expect(a).toMatch(/AssetStore = Pick<NoteVault, "occupied" \| "ensureFolder" \| "create">/);
  });

  it("10. sources/ (资料) writes the vault only through save-original.ts: NoteVault ensureFolder + one createBinary, never over a file", () => {
    const dir = join(WIKI, "sources");
    const files = readdirSync(dir).filter((f) => f.endsWith(".ts") && !f.endsWith(".test.ts")).sort();
    expect(files).toEqual(["obsidian-sources.ts", "ports.ts", "render-sources.ts", "save-original.ts", "sources-controller.ts", "sources-copy.ts"]);
    for (const f of files) {
      const c = code(readFileSync(join(dir, f), "utf8"));
      expect(c, f).not.toMatch(/vault\.(create|createBinary|createFolder|modify|process|delete|trash|rename|append|copy)\s*\(|adapter|fileManager|processFrontMatter|modifyBinary|trashFile|fetch|requestUrl/);
      if (f !== "save-original.ts") expect(c, f).not.toMatch(/\.createBinary\s*\(|\.ensureFolder\s*\(/);
    }
    const so = code(readFileSync(join(dir, "save-original.ts"), "utf8"));
    expect(count(so, /\.createBinary\s*\(/)).toBe(1);
    expect(count(so, /\.ensureFolder\s*\(/)).toBe(1);
    // The vault is touched on the Obsidian side only by the picker's file list, a read, and opening; the writes go through ObsidianNoteVault.
    const os = code(readFileSync(join(dir, "obsidian-sources.ts"), "utf8"));
    expect(os).toContain("new ObsidianNoteVault(app.vault)");
  });

  it("8. main.ts registers the wiki view once, each wiki command once, and never syncs notes itself", () => {
    const main = readFileSync(join(SRC, "main.ts"), "utf8");
    expect(count(main, /registerView\(WIKI_VIEW_TYPE/)).toBe(1);
    // 打开个人 Wiki is no longer a command (the sidebar opens at start; 今天复习 / 打开陪读助手 open it); never twice either.
    expect(count(main, /id: "open-wiki"/)).toBeLessThanOrEqual(1);
    // The demo wiki's command (打开个人 Wiki（演示数据）) went with the concept-era 显示旧版概念功能 toggle.
    expect(count(main, /id: "open-wiki-demo"/)).toBe(0);
    expect(main).not.toMatch(/syncWikiNote\s*\(/);
    expect(main).not.toMatch(/\.sync\.run\s*\(/);
    expect(main).not.toMatch(/new FakeWikiApi|\bfindWikiNote\s*\(/);
  });
});
