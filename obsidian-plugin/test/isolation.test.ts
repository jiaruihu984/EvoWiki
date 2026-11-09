import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { expect, it } from "vitest";

const SRC = join(__dirname, "..", "src");
const files = (d: string): string[] => readdirSync(d).flatMap((f) => {
  const p = join(d, f);
  return statSync(p).isDirectory() ? files(p) : /\.ts$/.test(f) && !/\.test\.ts$/.test(f) ? [p] : [];
});
const rel = (p: string) => relative(SRC, p).replace(/\\/g, "/");
const using = (re: RegExp) => files(SRC).filter((p) => re.test(readFileSync(p, "utf8"))).map(rel).sort();

it("no V wire fields or /view endpoints anywhere (the concept-era personal view is retired)", () => {
  const V = /\/view\/|primary_hub|perspective_segment_id|also_belongs_to|axis_kind|\bmode\b\s*[:=]|\brole_anchor\b/;
  // wiki/ has its own isolation test, and `mode` there is the review mode (ReviewItemOut.mode), so wiki/ is exempt from the
  // `mode` alternative only; every other V token still applies to it.
  const V_NO_MODE = /\/view\/|primary_hub|perspective_segment_id|also_belongs_to|axis_kind|\brole_anchor\b/;
  // generated/ holds the wire types generated from the pydantic models: contracts.py's AttachmentRead `mode` (how a chat
  // attachment was read: full / toc / image …) and ReviewItemOut.mode, not V's mode: exempt from the `mode` alternative only,
  // as wiki/ is.
  const noMode = (p: string) => p.startsWith("wiki/") || p.startsWith("generated/");
  const hit = (p: string) => (noMode(p) ? V_NO_MODE : V).test(readFileSync(join(SRC, p), "utf8"));
  expect(using(V).filter(hit)).toEqual([]);
  // `@codemirror/view` (the editor package, section-menu-view.ts) is not the endpoint.
  expect(using(/(?<!@codemirror)\/view[?"`]/)).toEqual([]);
});

it("no concept-era server calls are left (page views, events, asks, quiz answers, sessions, decisions, K reads)", () => {
  expect(using(/\.(pageView|postEvent|answerProbe|openSession|endSession|decideProposal|selfReport|knowledgeDiff|conceptContent|neighbourhood)\s*\(/))
    .toEqual([]);
  expect(using(/["'`]\/(concepts|events|sessions|quiz\/answers|ask|me\/knowledge|render-plans|content|notes|demo\/view)\b/)).toEqual([]);
});

it("the only plugin-side user creation is the account check's POST /users (api/client.ts)", () => {
  expect(using(/["'`]\/users\b/)).toEqual(["api/client.ts"]);
  expect(using(/\.createUser\s*\(/)).toEqual(["account.ts", "main.ts"]);
});

it("frontmatter is never written (the concept-era link / unlink is retired)", () => {
  expect(using(/processFrontMatter\s*\(/)).toEqual([]);
});

it("vault writes, exact per API: note-service.ts (ObsidianNoteVault) → create, createBinary, createFolder, process; wiki/sources/save-original.ts → createBinary (via BinaryVault, into <notes>/资料 only); wiki/note/sync.ts → process via ObsidianNoteVault, trashFile / vault.trash and renameFile (Task 13); wiki/profile-vault.ts → process / create / createFolder (the profile note 「我」, via ProfileNote's port); notes/backup-store.ts → for its backups under .evowiki/backups, adapter.remove of one file and adapter.rmdir(…, false) of an empty folder (profile-backup.ts names them); nothing else anywhere", () => {
  expect(using(/\.process\s*\(/)).toEqual(["notes/note-service.ts", "wiki/note/sync.ts", "wiki/profile-vault.ts"]);
  expect(using(/vault\.create\s*\(/)).toEqual(["notes/note-service.ts", "wiki/profile-vault.ts"]);
  expect(using(/\.createFolder\s*\(/)).toEqual(["notes/note-service.ts", "wiki/profile-vault.ts"]);
    // Binary writes go through ObsidianNoteVault: imported originals or verified image assets.
  expect(using(/vault\.createBinary\s*\(/)).toEqual(["notes/note-service.ts"]);
    expect(using(/\.createBinary\s*\(/)).toEqual(["notes/note-service.ts", "wiki/embed/assets.ts", "wiki/sources/save-original.ts"]);
  expect(using(/vault\.(modify|append|delete|rename|copy)\s*\(/)).toEqual([]);
  // Task 13 (assistant-actions): a trashed wiki page's unedited note goes to Obsidian's trash and a title / folder change
  // moves the note; both only in wiki/note/sync.ts (fileManager.trashFile, falling back to vault.trash; fileManager.renameFile).
  expect(using(/vault\.trash\s*\(/)).toEqual(["wiki/note/sync.ts"]);
  expect(using(/\.trashFile\s*\(/)).toEqual(["wiki/note/sync.ts"]);
  // The profile path resolver uses main's controlled rename port, covered by profile-path.test.ts.
  expect(using(/\.renameFile\s*\(/)).toEqual(["main.ts", "wiki/note/sync.ts"]);
  expect(using(/adapter\.(write|writeBinary|mkdir|rename|append|copy|trashSystem|trashLocal)\s*\(/)).toEqual([]);
  expect(using(/\.(modify|modifyBinary|appendBinary|writeBinary|mkdir)\s*\(/)).toEqual([]);
  // the profile note's backups (profile-backup.ts): one file removed, a folder only while empty, never recursive
  expect(using(/adapter\.(remove|rmdir)\s*\(/)).toEqual([]);
  expect(using(/(?<![\w.])a\.remove\s*\(/)).toEqual(["notes/backup-store.ts"]);
  expect(readFileSync(join(SRC, "notes/backup-store.ts"), "utf8")).toMatch(/promises\.rmdir\(.*getFullPath\(p\)\)/);
  expect(readFileSync(join(SRC, "notes/backup-store.ts"), "utf8")).not.toMatch(/rmdir\([^)]*true/);
});

it("no editor-based writes and no distinctive write/delete/rename calls on any receiver (T10-I2)", () => {
  expect(using(/\beditor\.(replaceSelection|replaceRange|setValue|setLine|transaction)\s*\(/)).toEqual([]);
  // Prefix-free: these names are unambiguous writes whatever the receiver is called. Ambiguous names
  // (append/create/delete/copy/rename) stay in the vault.-prefixed check above, because a bare
  // `.append(` matches DOM calls.
  expect(using(/\.(modify|modifyBinary|appendBinary|writeBinary|mkdir)\s*\(/)).toEqual([]);
  expect(using(/\.rmdir\s*\(/)).toEqual(["notes/backup-store.ts"]);
});

it("no fetch/XMLHttpRequest (CORS) and no innerHTML (server text is never markup)", () => {
  // evoui/math.ts reads MathJax's fonts from Obsidian's own app:// origin, which requestUrl cannot open
  expect(using(/\bfetch\s*\(|XMLHttpRequest|innerHTML|insertAdjacentHTML/).filter((f) => f !== "wiki/evoui/math.ts")).toEqual([]);
});

it("event literals of the concept era stay banned in the plugin", () => {
  // wiki/prompt-view.ts, wiki/start-page-view.ts: `el("div", "scroll")` is a class name, not an event.
  const views = ["wiki/prompt-view.ts", "wiki/start-page-view.ts"];
  expect(using(/"(dwell|scroll|note_created|note_edited|page_view|view_suggestion_\w+)"/).filter((p) => !views.includes(p))).toEqual([]);
});

it("no code translates between mode and role values", () => {
  for (const p of files(SRC)) {
    const s = readFileSync(p, "utf8");
    expect(/instructional|compressed/.test(s) && /\blearn\b|\bsupport\b|\bbridge\b/.test(s), rel(p)).toBe(false);
  }
});

it("the editor selection is read only in main.ts (the 问陪读助手 quote, sent only on an explicit click)", () => {
  expect(using(/getSelection\s*\(/)).toEqual(["main.ts"]);
});

it(".evowiki/** is never an assistant note (A3)", () => {
  // The guard lives in assistant/target.ts (resolveActiveNote, the active note) and assistant/host.ts (the note text
  // read); main.ts resolves the editor-menu target only through resolveActiveNote. (main.ts's own checks went with the
  // 样板页 file menu in the chat-first audit.)
  for (const f of ["assistant/target.ts", "assistant/host.ts"]) {
    expect(readFileSync(join(SRC, f), "utf8"), f).toMatch(/isInternalPath\s*\(/);
  }
  const main = readFileSync(join(SRC, "main.ts"), "utf8");
  expect(main).toMatch(/resolveActiveNote\s*\(/);
});

it("personal-wiki notes never call an LLM path or the network (E3a)", () => {
  for (const p of files(SRC).filter((x) => rel(x).startsWith("wiki/note/"))) {
    expect(readFileSync(p, "utf8"), rel(p)).not.toMatch(/\.ask\s*\(|\/ask|client_context|answerProbe|requestUrl|\bfetch\b/);
  }
});
