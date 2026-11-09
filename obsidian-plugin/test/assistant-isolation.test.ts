import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import ts from "typescript";
import { expect, it } from "vitest";

// Static guards for the reading assistant (src/assistant/). Non-test sources only: files() skips *.test.ts.
const SRC = join(__dirname, "..", "src");
const files = (d: string): string[] => readdirSync(d).flatMap((f) => {
  const p = join(d, f);
  return statSync(p).isDirectory() ? files(p) : /\.ts$/.test(f) && !/\.test\.ts$/.test(f) ? [p] : [];
});
const rel = (p: string) => relative(SRC, p).replace(/\\/g, "/");
const using = (re: RegExp) => files(SRC).filter((p) => re.test(readFileSync(p, "utf8"))).map(rel).sort();
const ASSISTANT = files(SRC).filter((p) => rel(p).startsWith("assistant/"));

function selectionProperties(path: string): number {
  const source = ts.createSourceFile(path, readFileSync(path, "utf8"), ts.ScriptTarget.Latest, true);
  let count = 0;
  const visit = (node: ts.Node): void => {
    if ((ts.isPropertyAssignment(node) || ts.isShorthandPropertyAssignment(node))
      && (ts.isIdentifier(node.name) || ts.isStringLiteral(node.name)) && node.name.text === "selection") count++;
    ts.forEachChild(node, visit);
  };
  visit(source);
  return count;
}

it("the assistant module never writes the vault, the editor or frontmatter, and never reads the selection itself", () => {
  expect(ASSISTANT.length).toBeGreaterThan(0);
  for (const p of ASSISTANT) {
    const t = ts.createPrinter({ removeComments: true }).printFile(
      ts.createSourceFile(p, readFileSync(p, "utf8"), ts.ScriptTarget.Latest, true));
    // Writes are banned; reads (host.ts's app.vault.cachedRead) are allowed.
    expect(t, rel(p)).not.toMatch(
      /\.process\s*\(|vault\.(create|createBinary|modify|modifyBinary|append|delete|trash|rename|copy|createFolder)\s*\(|createFolder|processFrontMatter|fileManager\.|adapter\.\w+\s*\(/);
    // main.ts owns the editor; the assistant never touches it.
    expect(t, rel(p)).not.toMatch(/\beditor\.|getSelection\s*\(/);
  }
});

it("/assistant/ paths appear only in assistant/client.ts", () => {
  // Relative module paths ("./assistant/…", "../assistant/…") are imports, not server paths, so a preceding "." or
  // word character is not a match; any other "/assistant/" (strings, template literals, comments) is. generated/ is the
  // wire types generated from the pydantic models: their docstrings name the routes, no code there calls them.
  expect(using(/(?<![.\w])\/assistant\//).filter((p) => !p.startsWith("generated/"))).toEqual(["assistant/client.ts"]);
});

it("questions and quotes are sent only by the controller's send/retry path", () => {
  // a fake API that wraps its parent's sendMessage (the demo fixture's paced replies) is the API side, not a sender
  expect(using(/(?<!super)\.sendMessage\s*\(/)).toEqual(["assistant/controller.ts"]);
  // One place builds the quote into a message body (Ruling P4: client.ts only copies it with `out.selection =`).
  expect(ASSISTANT.filter((path) => selectionProperties(path) > 0).map(rel)).toEqual(["assistant/controller.ts"]);
  const ctl = readFileSync(join(SRC, "assistant/controller.ts"), "utf8");
  expect((ctl.match(/\.sendMessage\s*\(/g) ?? []).length).toBe(1);
  expect(selectionProperties(join(SRC, "assistant/controller.ts"))).toBe(1);
});

it("a quote reaches the assistant only from main.ts's editor-menu click", () => {
  // sendQuoted (读完了's taps) is attachQuote plus a send, so it counts too; the controller's own `this.attachQuote`
  // inside sendQuoted is not a second entry point.
  expect(using(/(?<!\bthis)\.(attachQuote|sendQuoted)\s*\(/)).toEqual(["main.ts"]);
});

it("proposal decisions are sent only by the controller", () => {
  // Ruling P6: render.ts and assistant-view.ts call actions/controller.decide; only the controller calls api.decide.
  expect(using(/\bapi\.decide\s*\(/)).toEqual(["assistant/controller.ts"]);
});

it("the fake api is used by no plugin code except the dev-only demo fixture", () => {
  // wiki/fixtures-demo.ts runs only with plugin data `fixture: "demo"` (settings.ts); the assistant never imports it.
  expect(using(/fake-api/).filter((p) => !p.startsWith("assistant/fake-api"))).toEqual(["wiki/fixtures-demo.ts"]);
});

it("the assistant UI makes no not-stored claim (user ruling 2026-10-08)", () => {
  for (const p of ASSISTANT) expect(readFileSync(p, "utf8"), rel(p)).not.toMatch(/不保存|未保存|不会被保存|不会保存/);
});
