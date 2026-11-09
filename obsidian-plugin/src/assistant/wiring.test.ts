import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, expect, it, vi } from "vitest";
import EvoWikiPlugin from "../main";
import { AssistantClient } from "./client";
import type { AssistantController } from "./controller";

const MAIN = readFileSync(join(__dirname, "..", "main.ts"), "utf8");

afterEach(() => { vi.restoreAllMocks(); });

it("main registers the assistant view, its command and one editor-menu item", () => {
  expect(MAIN).toContain("this.registerView(ASSISTANT_VIEW_TYPE,");
  expect(MAIN).toContain('id: "open-assistant", name: ASSISTANT_COPY.commandOpen');
  expect((MAIN.match(/ASSISTANT_COPY\.menuAsk/g) ?? []).length).toBe(1);
  // two explicit-click entry points: the editor menu and the section 「⋯ → 问助手」 (Task 7)
  expect((MAIN.match(/\.attachQuote\(/g) ?? []).length).toBe(2);
  expect(MAIN).toContain("this.assistant?.dispose()");
});

it("settings reinit tells the assistant the identity changed, once, at the end", () => {
  const start = MAIN.indexOf("reinit: async () => {");
  const reinit = MAIN.slice(start, MAIN.indexOf("\n    });", start));
  expect((MAIN.match(/\.identityChanged\(\)/g) ?? []).length).toBe(1);
  expect(reinit.trimEnd()).toMatch(/this\.assistant\.identityChanged\(\);[^\n]*\s*\},$/);
});

it("the Bot's chips and 问助手 send through sendToAssistant (sendText, no draft), and the panel gets the chip source", () => {
  const send = MAIN.slice(MAIN.indexOf("assistantSend:"), MAIN.indexOf("askAssistant:"));
  expect(send).toContain("sendToAssistant(this.assistant, text, page,");
  expect(MAIN).toContain("botChip(s.suggestion_id)");
});

type Item = { title: string; click: () => void };
type MenuFn = (m: unknown, e: unknown, i: unknown) => void;

async function loaded(settings: Record<string, unknown> = {}) {
  vi.spyOn(AssistantClient.prototype, "listModels").mockResolvedValue({ models: [], current: null, reasoning: null });
  const p = new EvoWikiPlugin(...([] as unknown as ConstructorParameters<typeof EvoWikiPlugin>));
  const menus: MenuFn[] = [];
  const active = { path: "ML/PCA.md", basename: "PCA", extension: "md" };
  const state = { cache: { frontmatter: { evowiki: { concept_id: "pca" } }, headings: [{ heading: "推导", position: { start: { line: 0 } } }] } as unknown };
  const getRightLeaf = vi.fn(() => null);
  const getLeavesOfType = vi.fn((_t: string) => [] as unknown[]);
  const registerView = vi.fn(); const addCommand = vi.fn();
  Object.assign(p, {
    manifest: { id: "evowiki" },
    loadData: () => Promise.resolve({ bootstrapLanguage: "zh", settings: { serverUrl: "http://127.0.0.1:8000", userId: "jerry", ...settings } }),
    saveData: () => Promise.resolve(),
    registerView, addSettingTab: vi.fn(), addCommand, registerEvent: vi.fn(), registerMarkdownCodeBlockProcessor: vi.fn(), registerMarkdownPostProcessor: vi.fn(), registerEditorExtension: vi.fn(),
    app: { workspace: { on: (n: string, fn: MenuFn) => { if (n === "editor-menu") menus.push(fn); return {}; }, onLayoutReady: vi.fn(),
      getActiveFile: () => active, getLeavesOfType, getRightLeaf },
      metadataCache: { on: () => ({}), resolvedLinks: {}, getFileCache: () => state.cache },
      vault: { on: () => ({}), getMarkdownFiles: () => [] } },
  });
  await p.onload();
  const items: Item[] = [];
  const menu = { addItem: (cb: (i: unknown) => void) => { const it = { title: "", click: () => undefined as void,
    setTitle(t: string) { it.title = t; return it; }, setIcon() { return it; }, setDisabled() { return it; },
    onClick(fn: () => void) { it.click = fn; return it; } }; cb(it); items.push(it); } };
  const editor = (text: string, value = "# PCA") => ({ getSelection: () => text, getValue: () => value, getCursor: () => ({ line: 0, ch: 0 }) });
  const ask = (text: string, file: unknown = active, value?: string): Item[] => {
    items.length = 0;
    for (const fn of menus) fn(menu, editor(text, value), { file });
    return items.filter((i) => i.title === "问陪读助手");
  };
  const assistant = (p as unknown as { assistant: AssistantController }).assistant;
  const wikiTab = (): string | undefined => (p as unknown as { wiki: { state: { tab: string } } | null }).wiki?.state.tab;
  return { p, ask, state, assistant, getRightLeaf, getLeavesOfType, registerView, addCommand, wikiTab, menus };
}

it("the menu item is offered only for a concept-linked or personal-wiki note on the active editor, and sends nothing itself", async () => {
  const create = vi.spyOn(AssistantClient.prototype, "createSession");
  const message = vi.spyOn(AssistantClient.prototype, "sendMessage");
  const read = vi.spyOn(AssistantClient.prototype, "getSession");
  const { ask, state, assistant, getRightLeaf, getLeavesOfType, registerView, addCommand, wikiTab } = await loaded();
  expect(registerView).toHaveBeenCalledWith("evowiki-assistant", expect.any(Function));
  expect(addCommand).toHaveBeenCalledWith(expect.objectContaining({ id: "open-assistant", name: "打开陪读助手" }));

  const items = ask("协方差");
  expect(items).toHaveLength(1);
  items[0]!.click();
  await new Promise((r) => setTimeout(r, 0));
  expect(assistant.state.conv?.target).toEqual({ kind: "note", path: "ML/PCA.md", conceptId: "pca", title: "PCA" });
  expect(assistant.state.conv?.quote).toEqual({ kind: "text", text: "协方差", heading: "推导" });
  expect(getRightLeaf).toHaveBeenCalled(); // the wiki sidebar is opened…
  expect(wikiTab()).toBe("assistant");     // …on its 助手 tab
  expect(getLeavesOfType).toHaveBeenCalledWith("evowiki-wiki");
  expect(getLeavesOfType).not.toHaveBeenCalledWith("evowiki-assistant"); // the old view is never opened
  expect(create).not.toHaveBeenCalled();
  expect(message).not.toHaveBeenCalled();
  expect(read).not.toHaveBeenCalled();

  // blank selection → no item
  expect(ask("   ")).toHaveLength(0);
  // a file other than the active one (popover/embed) → no item
  expect(ask("协方差", { path: "Other.md", basename: "Other", extension: "md" })).toHaveLength(0);
  // a file-less editor → no item
  expect(ask("协方差", null)).toHaveLength(0);
  // unsupported note → no item
  state.cache = {};
  expect(ask("协方差")).toHaveLength(0);
});

it("a personal-wiki note gets the item; the setting off removes it", async () => {
  const { renderWikiNote } = await import("../wiki/note");
  const wiki = renderWikiNote({ pageId: "page_pca", title: "主成分分析", goal: "学 PCA", diffText: null,
    collapsed: false, sections: [{ id: "s1", heading: "直观理解", role: "explain", cue: "方差", body: "正文", folded: false }] }).markdown;
  const on = await loaded();
  on.state.cache = {};
  expect(on.ask("方差", undefined, wiki)).toHaveLength(1);
  const off = await loaded({ allowSelectionContext: false });
  expect(off.ask("协方差")).toHaveLength(0);
});

it("an over-long selection is not refused: it goes along as a text attachment (no quote, no notice)", async () => {
  const { clearEwNotices, ewNotices } = await import("../../test/ew-notices");
  clearEwNotices();
  const { ask, assistant } = await loaded();
  const items = ask("字".repeat(4001));
  expect(items).toHaveLength(1);
  items[0]!.click();
  for (let i = 0; i < 5; i++) await new Promise((r) => setTimeout(r, 0));
  expect(ewNotices()).toEqual([]);
  expect(assistant.state.conv?.quote ?? null).toBeNull();
  expect(assistant.state.conv?.attachments.map((a) => a.name)).toEqual(["选中的文字.txt"]);
});

it("the menu item is not offered while the server or the user id is empty", async () => {
  expect((await loaded({ serverUrl: "" })).ask("协方差")).toHaveLength(0);
  expect((await loaded({ userId: "" })).ask("协方差")).toHaveLength(0);
  expect((await loaded()).ask("协方差")).toHaveLength(1);
});

it("the 打开陪读助手 command opens the wiki sidebar on its 助手 tab, never the old assistant view", async () => {
  const { addCommand, getLeavesOfType, getRightLeaf, wikiTab } = await loaded();
  const cmd = addCommand.mock.calls.map((c) => c[0] as { id: string; callback: () => void }).find((c) => c.id === "open-assistant");
  if (!cmd) throw new Error("no open-assistant command");
  cmd.callback();
  await new Promise((r) => setTimeout(r, 0));
  expect(getRightLeaf).toHaveBeenCalled();
  expect(wikiTab()).toBe("assistant");
  expect(getLeavesOfType).not.toHaveBeenCalledWith("evowiki-assistant");
});

it("main mounts the assistant panel in the wiki view (the one shared controller)", () => {
  expect(MAIN).toContain("(v) => new AssistantPanel(this.assistant, assistantHost, v, {");
  expect((MAIN.match(/new AssistantController\(/g) ?? []).length).toBe(1);
});

it("the 今天复习 command opens the wiki sidebar on its 今天 tab", async () => {
  const { addCommand, getRightLeaf, wikiTab } = await loaded();
  const cmd = addCommand.mock.calls.map((c) => c[0] as { id: string; name: string; callback: () => void }).find((c) => c.id === "open-review");
  if (!cmd) throw new Error("no open-review command");
  expect(cmd.name).toBe("今天复习");
  cmd.callback();
  await new Promise((r) => setTimeout(r, 0));
  expect(getRightLeaf).toHaveBeenCalled();
  expect(wikiTab()).toBe("today");
});
