import { setUiLanguage } from "./i18n";
import { HttpWikiClient } from "./wiki/api";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { MarkdownView, Notice, TFile } from "obsidian";
import { describe, expect, it, vi } from "vitest";
import { clearEwNotices, ewNotices } from "../test/ew-notices";
import { ApiError } from "./api/errors";
import { COPY } from "./copy";
import EvoWikiPlugin from "./main";
import { WikiController } from "./wiki/controller";
import { SETTINGS_FIXTURE, UsageClient } from "./wiki/usage";

it("exports the plugin class", () => {
  expect(typeof EvoWikiPlugin).toBe("function");
});

it("onunload disposes the account check and stops the connection monitor", () => {
  const p = new EvoWikiPlugin(...([] as unknown as ConstructorParameters<typeof EvoWikiPlugin>));
  const dispose = vi.fn(); const stop = vi.fn();
  const budgetSetup = new AbortController();
  Object.assign(p, { account: { dispose }, conn: { stop }, budgetSetup });
  p.onunload();
  expect(dispose).toHaveBeenCalledTimes(1);
  expect(stop).toHaveBeenCalledTimes(1);
  expect(budgetSetup.signal.aborted).toBe(true);
});

it("the account check: GET /me on layout ready; an unknown user is registered once with Obsidian's language; a failed registration is a notice", async () => {
  const p = new EvoWikiPlugin(...([] as unknown as ConstructorParameters<typeof EvoWikiPlugin>));
  const onLayoutReady = vi.fn();
  Object.assign(p, {
    manifest: { id: "evowiki" }, loadData: () => Promise.resolve({ settings: { serverUrl: "http://x", userId: "jerry" } }), saveData: () => Promise.resolve(),
    registerView: vi.fn(), addSettingTab: vi.fn(), addCommand: vi.fn(), registerEvent: vi.fn(), registerMarkdownCodeBlockProcessor: vi.fn(), registerMarkdownPostProcessor: vi.fn(), registerEditorExtension: vi.fn(),
    registerDomEvent: vi.fn(), registerInterval: vi.fn(), addRibbonIcon: vi.fn(),
    app: { workspace: { getLeavesOfType: () => [], on: () => ({}), onLayoutReady, getActiveFile: () => null, getRightLeaf: () => null },
      metadataCache: { on: () => ({}), resolvedLinks: {}, getFileCache: () => null }, vault: { on: () => ({}), getMarkdownFiles: () => [], getAbstractFileByPath: () => null } },
  });
  await p.onload();
  const resend = vi.spyOn((p as unknown as { assistant: { resendOffline(): Promise<void> } }).assistant,
    "resendOffline").mockResolvedValue();
  const me = vi.fn(() => Promise.reject(new ApiError(404, "unknown user")));
  const createUser = vi.fn(() => Promise.reject(new ApiError(422, "bad")));
  const checkServer = vi.fn(() => Promise.resolve());
  Object.assign((p as unknown as { client: object }).client, { me, createUser, checkServer });
  clearEwNotices();
  for (const c of onLayoutReady.mock.calls) (c[0] as () => void)();
  await vi.waitFor(() => expect(p.account.state.kind).toBe("error"));
  expect(me).toHaveBeenCalledOnce();
  expect(checkServer).toHaveBeenCalledOnce();
  expect(createUser.mock.calls).toEqual([[{ user_id: "jerry", survey: { math: "none", ml: "none", dl: "none" }, lang: "en" }]]);
  expect(ewNotices()).toEqual([COPY.registerFailed("jerry", "bad")]);
  // The connection probe is GET /me too (any HTTP answer = online).
  await (p as unknown as { conn: { retryNow(): Promise<void> } }).conn.retryNow();
  expect(me).toHaveBeenCalledTimes(2);
  expect(createUser).toHaveBeenCalledOnce();                  // a probe alone never registers again
  // 重新连接 / 重试: the failed check runs once more (one more registration attempt, one more notice).
  await (p as unknown as { reconnect(): Promise<void> }).reconnect();
  expect(createUser).toHaveBeenCalledTimes(2);
  expect(ewNotices()).toEqual([COPY.registerFailed("jerry", "bad"), COPY.registerFailed("jerry", "bad")]);
  expect(resend).not.toHaveBeenCalled();
  const client = (p as unknown as { client: object }).client;
  Object.assign(client, { me: () => Promise.resolve({ user_id: "jerry", background: "beginner", condition: "main", lang: "en" }),
    checkServer: () => Promise.reject(new ApiError(409, "请升级插件", "protocol_mismatch")) });
  await p.account.init();
  expect(p.account.state.kind).toBe("error");
  expect(resend).not.toHaveBeenCalled();
  Object.assign(client, { checkServer: () => Promise.resolve() });
  const settings = vi.spyOn(UsageClient.prototype, "settings").mockResolvedValue(SETTINGS_FIXTURE);
  const language = vi.spyOn(HttpWikiClient.prototype, "getSettings").mockResolvedValue({ language: "en", review_per_day: 20, page_limit_usd: null });
  try {
    await p.account.init();
    expect(p.account.state.kind).toBe("ready");
    expect(resend).toHaveBeenCalledOnce();
  } finally { settings.mockRestore(); language.mockRestore(); }
});

it("the Bot pull: the window-focus refresh runs at most once per 30 s; the interval is not throttled", async () => {
  vi.useFakeTimers({ toFake: ["Date"] });
  try {
    vi.setSystemTime(new Date(2026, 9, 15, 9, 0, 0));
    const p = new EvoWikiPlugin(...([] as unknown as ConstructorParameters<typeof EvoWikiPlugin>));
    const dom: Record<string, () => void> = {};
    const tick: (() => void)[] = [];
    Object.assign(p, {
      manifest: { id: "evowiki" }, loadData: () => Promise.resolve(null), saveData: () => Promise.resolve(),
      registerView: vi.fn(), addSettingTab: vi.fn(), addCommand: vi.fn(), registerEvent: vi.fn(), registerMarkdownCodeBlockProcessor: vi.fn(), registerMarkdownPostProcessor: vi.fn(), registerEditorExtension: vi.fn(),
      registerDomEvent: (_t: unknown, name: string, fn: () => void) => { dom[name] = fn; },
      registerInterval: (id: number) => id,
      app: { workspace: { getLeavesOfType: () => [], on: () => ({}), onLayoutReady: vi.fn(), getActiveFile: () => null },
        metadataCache: { on: () => ({}), resolvedLinks: {}, getFileCache: () => null }, vault: { on: () => ({}), getMarkdownFiles: () => [], getAbstractFileByPath: () => null } },
    });
    const setInterval = vi.spyOn(window, "setInterval").mockImplementation(((f: () => void) => { tick.push(f); return 1; }) as never);
    await p.onload();
    setInterval.mockRestore();
    const refresh = vi.fn(() => Promise.resolve());
    const language = vi.fn(() => Promise.resolve());
    Object.assign(p, { wiki: { state: { catalog: { kind: "ready", value: { settings: { language: "zh" } } } }, refreshToday: refresh, refreshAccountLanguage: language, refreshLocale: () => Promise.resolve() }, wikiDemo: false });
    // A wizard language PUT updates global UI first; reconcile an already-loaded catalog too.
    setUiLanguage("zh"); language.mockClear(); setUiLanguage("en");
    expect(language).toHaveBeenCalledOnce(); setUiLanguage("zh"); language.mockClear();
    expect(dom.focus).toBeDefined();
    dom.focus!(); dom.focus!(); dom.focus!();
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(language).toHaveBeenCalledTimes(3);
    vi.setSystemTime(new Date(2026, 9, 15, 9, 0, 29));
    dom.focus!();
    expect(refresh).toHaveBeenCalledTimes(1);
    tick[0]!(); tick[0]!();                       // the 5-minute interval is not throttled by it
    expect(refresh).toHaveBeenCalledTimes(3);
    vi.setSystemTime(new Date(2026, 9, 15, 9, 0, 31));
    dom.focus!();
    expect(refresh).toHaveBeenCalledTimes(4);
  } finally { vi.useRealTimers(); }
});

it("static: main.ts never calls openLinkText (it creates unresolved targets)", () => {
  expect(readFileSync(join(__dirname, "main.ts"), "utf8")).not.toMatch(/\.openLinkText\s*\(/);
});

it("static: no concept-era wiring is left in main.ts", () => {
  const src = readFileSync(join(__dirname, "main.ts"), "utf8");
  for (const gone of ["showLegacy", "SidebarController", "GraphController", "NoteService", "SessionManager", "open-sidebar", "open-graph", "open-wiki-demo"]) {
    expect(src, gone).not.toContain(gone);
  }
});

// ---- Personal wiki (obsidian-wiki-sidebar Task 11) ----

describe("personal wiki wiring", () => {
  function loadRig(stored: unknown = null) {
    const p = new EvoWikiPlugin(...([] as unknown as ConstructorParameters<typeof EvoWikiPlugin>));
    const registerView = vi.fn(); const addCommand = vi.fn(); const removeCommand = vi.fn();
    Object.assign(p, {
      manifest: { id: "evowiki" }, loadData: () => Promise.resolve({ ...(stored as Record<string, unknown> ?? {}), bootstrapLanguage: "zh" }), saveData: () => Promise.resolve(),
      registerView, addSettingTab: vi.fn(), addCommand, removeCommand, registerEvent: vi.fn(), registerMarkdownCodeBlockProcessor: vi.fn(), registerMarkdownPostProcessor: vi.fn(), registerEditorExtension: vi.fn(),
      app: { workspace: { getLeavesOfType: () => [], on: vi.fn(() => ({})), onLayoutReady: vi.fn(), getActiveFile: () => null },
        metadataCache: { on: vi.fn(() => ({})), resolvedLinks: {}, getFileCache: () => null },
        vault: { on: vi.fn(() => ({})), getMarkdownFiles: () => [], getAbstractFileByPath: () => null } },
    });
    return { p, registerView, addCommand, removeCommand };
  }

  type WikiPriv = { wiki: { state: { demo: boolean }; dispose(): void } | null; wikiDemo: boolean;
    openWiki(demo: boolean): Promise<void>; openIfExists(p: string, at?: (text: string) => number | null): boolean };

  it.each([
    { name: "real", stored: null, demo: false, blocked: true },
    { name: "demo", stored: null, demo: true, blocked: false },
    { name: "fixture", stored: { fixture: "demo" }, demo: false, blocked: false },
  ])("only the real wiki displays account errors ($name)", async ({ stored, demo, blocked }) => {
    const r = loadRig(stored);
    await r.p.onload();
    const account = { state: { kind: "error", message: "请先确认预算" }, subscribe: vi.fn(() => () => {}) };
    Object.assign(r.p, { account, wikiDemo: demo });
    const factory = r.registerView.mock.calls.find((call) => call[0] === "evowiki-wiki")![1] as
      (leaf: unknown) => { onOpen(): Promise<void>; onClose(): Promise<void>; contentEl: HTMLElement };
    const view = factory({});
    Object.assign(view, { miniCloud: null, assistant: null });
    await view.onOpen();
    expect(view.contentEl.querySelector("[data-role=account-status]")?.textContent ?? null)
      .toBe(blocked ? "请先确认预算 重试" : null);
    const slot = view.contentEl.querySelector(".ew-side")!.children[1] as HTMLElement;
    expect(slot.hidden).toBe(blocked);
    expect(account.subscribe).toHaveBeenCalledTimes(blocked ? 1 : 0);
    await view.onClose();
  });

  it("openWiki: switching real ↔ demo detaches the open wiki leaf and disposes the old controller; same mode reuses it", async () => {
    const r = loadRig();
    await r.p.onload();
    const leaves: { detach: ReturnType<typeof vi.fn>; setViewState: ReturnType<typeof vi.fn> }[] = [];
    const getLeavesOfType = vi.fn((t: string) => (t === "evowiki-wiki" ? leaves : []));
    const right = () => { const l = { detach: vi.fn(() => { leaves.splice(leaves.indexOf(l), 1); }),
      setViewState: vi.fn(() => { leaves.push(l); return Promise.resolve(); }) }; return l; };
    const getRightLeaf = vi.fn(right); const revealLeaf = vi.fn(() => Promise.resolve());
    Object.assign((r.p as unknown as { app: { workspace: object } }).app.workspace, { getLeavesOfType, getRightLeaf, revealLeaf });
    const priv = r.p as unknown as WikiPriv;

    await priv.openWiki(true);
    expect(getRightLeaf).toHaveBeenCalledTimes(1);
    expect(leaves).toHaveLength(1);
    const first = leaves[0]!;
    expect(first.setViewState).toHaveBeenCalledWith({ type: "evowiki-wiki", active: true });
    // the view factory builds the controller lazily; emulate it
    const demoCtrl = (r.p as unknown as { wikiController(): WikiPriv["wiki"] }).wikiController()!;
    expect(demoCtrl.state.demo).toBe(true);
    const dispose = vi.spyOn(demoCtrl, "dispose");

    await priv.openWiki(true); // same mode: reveal the existing leaf, keep the controller
    expect(getRightLeaf).toHaveBeenCalledTimes(1);
    expect(first.detach).not.toHaveBeenCalled();
    expect(priv.wiki).toBe(demoCtrl);
    expect(revealLeaf).toHaveBeenCalledTimes(2);

    await priv.openWiki(false); // switch: close, dispose, reopen on the real client
    expect(first.detach).toHaveBeenCalledOnce();
    expect(dispose).toHaveBeenCalledOnce();
    expect(priv.wikiDemo).toBe(false);
    expect(leaves).toHaveLength(1);
    expect((r.p as unknown as { wikiController(): WikiPriv["wiki"] }).wikiController()!.state.demo).toBe(false);
  });

  it("registers the evowiki-check processor once; blocks follow the real wiki controller (never the demo) until unloaded", async () => {
    const r = loadRig();
    await r.p.onload();
    const reg = (r.p as unknown as { registerMarkdownCodeBlockProcessor: ReturnType<typeof vi.fn> }).registerMarkdownCodeBlockProcessor;
    expect(reg.mock.calls.map((c) => c[0] as unknown)).toEqual(["evowiki-check"]);
    const processor = reg.mock.calls[0]![1] as (src: string, el: HTMLElement, ctx: { sourcePath: string; addChild(c: unknown): void }) => void;
    const other = document.createElement("div");
    processor("page: p1\nsection: s5\n", other, { sourcePath: "EvoWiki/bob/Wiki/P.md", addChild: vi.fn() });
    expect(other.childElementCount).toBe(0);                      // another user's note: no block
    const el = document.createElement("div");
    const addChild = vi.fn();
    processor("page: p1\nsection: s5\n", el, { sourcePath: "EvoWiki/me/Wiki/P.md", addChild });
    expect(el.textContent).toBe("在这里答题");
    expect(addChild).toHaveBeenCalledOnce();
    const child = addChild.mock.calls[0]![0] as { unload(): void };
    const priv = r.p as unknown as WikiPriv & { wikiController(): WikiPriv["wiki"] };
    priv.wikiController(); // the real controller, no page open yet
    expect(el.textContent).toBe("在这里答题");
    priv.wiki!.dispose(); priv.wiki = null; priv.wikiDemo = true;
    priv.wikiController(); // the demo controller is never offered to note blocks
    expect(el.textContent).toBe("在这里答题");
    child.unload();
    priv.wiki!.dispose(); priv.wiki = null; priv.wikiDemo = false;
    priv.wikiController();
    expect(el.textContent).toBe("在这里答题"); // unloaded: no longer redrawn
  });

  /** Runs every onLayoutReady callback registered so far (startup plugin loading is over). */
  const layoutReady = (p: unknown) => {
    const fn = (p as { app: { workspace: { onLayoutReady: ReturnType<typeof vi.fn> } } }).app.workspace.onLayoutReady;
    for (const c of fn.mock.calls) (c[0] as () => void)();
  };
  it("the old-layout move failing on load is caught: a notice says so, notes go on (no unhandled rejection)", async () => {
    const r = loadRig({ settings: { serverUrl: "http://x", userId: "me", notesFolder: "EvoWiki" } });
    Object.assign((r.p as unknown as { app: { vault: object } }).app.vault, { getAbstractFileByPath: () => { throw new Error("vault busy"); } });
    await r.p.onload();
    clearEwNotices();
    layoutReady(r.p);
    await (r.p as unknown as { folderReady: Promise<void> }).folderReady;
    expect(ewNotices()).toEqual(["EvoWiki 没能挪进「EvoWiki/me」，还在原处。"]);
  });

  const regOf = (p: unknown) => (p as { registerMarkdownCodeBlockProcessor: ReturnType<typeof vi.fn> }).registerMarkdownCodeBlockProcessor;

  it("embedded-html: registered only once the layout is ready, so a user's plugin loaded at startup registers it first", async () => {
    const r = loadRig();
    await r.p.onload();
    expect(regOf(r.p).mock.calls.map((c) => c[0] as unknown)).toEqual(["evowiki-check"]);
    layoutReady(r.p);
    expect(regOf(r.p).mock.calls.map((c) => c[0] as unknown)).toEqual(["evowiki-check", "embedded-html", "evoui", "evowiki-image"]);
  });

  it("embedded-html: already registered by another plugin → caught, one console.info, no warning or Notice", async () => {
    const r = loadRig();
    const reg = vi.fn((lang: string) => { if (lang === "embedded-html") throw new Error("already registered"); });
    Object.assign(r.p, { registerMarkdownCodeBlockProcessor: reg });
    const info = vi.spyOn(console, "info").mockImplementation(() => undefined);
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const shown = ewNotices().length;
    await r.p.onload();
    expect(() => layoutReady(r.p)).not.toThrow();
    expect(reg.mock.calls.map((c) => c[0])).toEqual(["evowiki-check", "embedded-html", "evoui", "evowiki-image"]);
    expect(info).toHaveBeenCalledOnce();
    expect(warn).not.toHaveBeenCalled();
    expect(ewNotices().length).toBe(shown);
    info.mockRestore(); warn.mockRestore();
  });

  it("evoui: another plugin already has the language → caught, one console.info, embedded-html still registered", async () => {
    const r = loadRig();
    const reg = vi.fn((lang: string) => { if (lang === "evoui") throw new Error("already registered"); });
    Object.assign(r.p, { registerMarkdownCodeBlockProcessor: reg });
    const info = vi.spyOn(console, "info").mockImplementation(() => undefined);
    await r.p.onload();
    expect(() => layoutReady(r.p)).not.toThrow();
    expect(reg.mock.calls.map((c) => c[0])).toEqual(["evowiki-check", "embedded-html", "evoui", "evowiki-image"]);
    expect(info).toHaveBeenCalledOnce();
    info.mockRestore();
  });

  it("embedded-html: the vault file (found ignoring case, like the fetch check) in a sandboxed iframe; 取回 only for the real, online wiki", async () => {
    const r = loadRig({ settings: { serverUrl: "http://x", userId: "me", notesFolder: "EvoWiki" } });
    const file = Object.assign(new TFile(), { path: "EvoWiki/me/Wiki/Assets/A1.html", name: "A1.html" });  // differs in case from the block's src
    const tree = (files: TFile[]) => ({ children: [{ name: "EvoWiki", children: [{ name: "me", children: [{ name: "Wiki", children: [{ name: "Assets", children: files }] }] }] }] });
    let exists = false;
    const on = vi.fn(() => ({}));
    Object.assign(r.p.app, { workspace: { getLeavesOfType: () => [], on, onLayoutReady: vi.fn(), getActiveFile: () => null },
      vault: { on: vi.fn(() => ({})), getMarkdownFiles: () => [], getAbstractFileByPath: (p: string) => (exists && p === file.path ? file : null),
        getFiles: () => { throw new Error("no whole-vault scan"); }, getRoot: () => tree(exists ? [file] : []), cachedRead: vi.fn(() => Promise.resolve("<p>hi</p>")) } });
    await r.p.onload();
    layoutReady(r.p);
    const processor = regOf(r.p).mock.calls[1]![1] as (src: string, el: HTMLElement, ctx: { sourcePath: string; addChild(c: unknown): void }) => void;
    const mount = () => {
      const el = document.createElement("div");
      processor("type: embedded-html\nid: z-1\nsrc: assets/a1.html\ntitle: T\nheight: 400\n", el, { sourcePath: "EvoWiki/me/Wiki/P.md", addChild: vi.fn() });
      return el;
    };
    Object.assign(r.p, { conn: { state: "online" } });
    const el = mount();
    await vi.waitFor(() => expect(el.querySelector("button.evowiki-embed-fetch")).not.toBeNull());
    expect(el.textContent).toContain("这个交互还没取回。");
    exists = true;
    (r.p as unknown as { embedHub: { assetWritten(id: string): void } }).embedHub.assetWritten("a1"); // what the fetcher reports
    await vi.waitFor(() => expect(el.querySelector(".evowiki-evoui-run")).not.toBeNull());   // the server does not answer here
    expect(el.querySelector("iframe")).toBeNull();
    el.querySelector<HTMLButtonElement>(".evowiki-evoui-run")!.click();
    expect(el.querySelector("iframe")!.getAttribute("sandbox")).toBe("allow-scripts");
    expect(on.mock.calls.map((c) => (c as unknown as [string])[0])).toContain("css-change");
    exists = false;
    (r.p as unknown as WikiPriv).wikiDemo = true;
    const demo = mount();
    await vi.waitFor(() => expect(demo.textContent).toContain("这个交互还没取回。"));
    expect(demo.querySelector("button")).toBeNull();
    (r.p as unknown as WikiPriv).wikiDemo = false;
    Object.assign(r.p, { conn: { state: "offline" } });
    const off = mount();
    await vi.waitFor(() => expect(off.textContent).toContain("这个交互还没取回。"));
    expect(off.querySelector("button")).toBeNull();
  });

  it("the wiki view's 重新连接 probes the connection (conn.retryNow) before the controller retry; the demo skips the probe", async () => {
    const r = loadRig();
    await r.p.onload();
    const factory = r.registerView.mock.calls.find((c) => c[0] === "evowiki-wiki")![1] as (leaf: unknown) => unknown;
    const order: string[] = [];
    const retryNow = vi.fn(() => { order.push("retryNow"); return Promise.resolve(); });
    Object.assign(r.p, { conn: { retryNow, state: "offline", track: <T>(p: Promise<T>) => p, onChange: () => () => undefined } });
    const view = factory({}) as { actions: { retryConnection(): void } };
    const ctrl = (r.p as unknown as { wiki: { retryConnection(): Promise<void> } }).wiki;
    const retry = vi.spyOn(ctrl, "retryConnection").mockImplementation(() => { order.push("controller"); return Promise.resolve(); });
    view.actions.retryConnection();
    await vi.waitFor(() => expect(retry).toHaveBeenCalledOnce());
    expect(order).toEqual(["retryNow", "controller"]);
    (r.p as unknown as WikiPriv).wikiDemo = true;
    view.actions.retryConnection();
    await vi.waitFor(() => expect(retry).toHaveBeenCalledTimes(2));
    expect(retryNow).toHaveBeenCalledOnce();
  });

  it("openIfExists opens only an existing TFile and says whether it did; an open tab of the note is brought forward", async () => {
    const p = new EvoWikiPlugin(...([] as unknown as ConstructorParameters<typeof EvoWikiPlugin>));
    const openFile = vi.fn(() => Promise.resolve()); const getLeaf = vi.fn(() => ({ openFile })); const openLinkText = vi.fn();
    const setActiveLeaf = vi.fn(), revealLeaf = vi.fn(() => Promise.resolve());
    const f = Object.assign(new TFile(), { path: "EvoWiki/Wiki/A.md", basename: "A" });
    let leaves: { view: MarkdownView }[] = [];
    Object.assign(p, { app: { workspace: { getLeaf, openLinkText, setActiveLeaf, revealLeaf, getActiveViewOfType: () => null,
      getLeavesOfType: (t: string) => (t === "markdown" ? leaves : []), getActiveFile: () => f },
      vault: { getAbstractFileByPath: (x: string) => (x === f.path ? f : x === "Dir" ? { path: "Dir" } : null) } } });
    const priv = p as unknown as WikiPriv;
    // getActiveFile() names the note (the last one shown) while another tab is in front: it is opened, not skipped
    expect(priv.openIfExists("EvoWiki/Wiki/A.md")).toBe(true);
    expect(priv.openIfExists("Gone.md")).toBe(false);
    expect(priv.openIfExists("Dir")).toBe(false);
    expect(openFile.mock.calls).toEqual([[f, { active: true }]]);
    expect(getLeaf).toHaveBeenCalledWith(false);
    expect(openLinkText).not.toHaveBeenCalled();
    // the note open in a tab behind 学点新的: that tab is activated and revealed, nothing opened again
    const tab = { view: Object.assign(Object.create(MarkdownView.prototype) as MarkdownView, { file: f }) };
    leaves = [tab];
    expect(priv.openIfExists("EvoWiki/Wiki/A.md")).toBe(true);
    expect(setActiveLeaf).toHaveBeenCalledWith(tab, { focus: true });
    expect(revealLeaf).toHaveBeenCalledWith(tab);
    expect(openFile).toHaveBeenCalledOnce();
    // an open that fails is said, never silent
    leaves = [];
    openFile.mockImplementationOnce(() => Promise.reject(new Error("x")));
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    expect(priv.openIfExists("EvoWiki/Wiki/A.md")).toBe(true);
    await Promise.resolve(); await Promise.resolve();
    expect(warn).toHaveBeenCalled();
    expect(document.body.textContent).toContain("没能打开笔记「A」");
    warn.mockRestore();
  });

  it("openIfExists with a locator (本页 jump): reads the note, reuses its open tab and scrolls there; else opens it at the line", async () => {
    const p = new EvoWikiPlugin(...([] as unknown as ConstructorParameters<typeof EvoWikiPlugin>));
    const f = Object.assign(new TFile(), { path: "EvoWiki/me/Wiki/A.md" });
    const openFile = vi.fn(() => Promise.resolve()); const getLeaf = vi.fn(() => ({ openFile }));
    const setActiveLeaf = vi.fn();
    /** A MarkdownView (instanceof holds) showing `file`, recording the ephemeral states set on it. */
    const mdView = (file: TFile): MarkdownView & { eStates: unknown[] } => {
      const eStates: unknown[] = [];
      return Object.assign(Object.create(MarkdownView.prototype) as MarkdownView,
        { file, eStates, setEphemeralState: (s: unknown) => { eStates.push(s); } });
    };
    const shown = mdView(f);
    const other = mdView(Object.assign(new TFile(), { path: "B.md" }));
    let leaves: { view: MarkdownView }[] = [{ view: other }, { view: shown }];
    const cachedRead = vi.fn(() => Promise.resolve(["a", "b", "TARGET", ""].join("\n")));
    Object.assign(p, { app: { workspace: { getLeaf, setActiveLeaf, getLeavesOfType: (t: string) => (t === "markdown" ? leaves : []),
      getActiveFile: () => f }, vault: { cachedRead, getAbstractFileByPath: (x: string) => (x === f.path ? f : null) } } });
    const priv = p as unknown as WikiPriv;
    const at = (text: string): number | null => { const i = text.split("\n").indexOf("TARGET"); return i < 0 ? null : i; };
    expect(priv.openIfExists(f.path, at)).toBe(true);
    await vi.waitFor(() => expect(shown.eStates).toEqual([{ line: 2 }]));
    expect(setActiveLeaf).toHaveBeenCalledWith(leaves[1], { focus: true });
    expect(openFile).not.toHaveBeenCalled();
    expect(other.eStates).toEqual([]);
    leaves = [{ view: other }];
    expect(priv.openIfExists(f.path, at)).toBe(true);
    await vi.waitFor(() => expect(openFile).toHaveBeenCalledWith(f, { active: true, eState: { line: 2 } }));
    expect(getLeaf).toHaveBeenCalledWith(false);
    expect(priv.openIfExists(f.path, () => null)).toBe(true);
    await vi.waitFor(() => expect(openFile).toHaveBeenLastCalledWith(f, { active: true, eState: undefined }));
    expect(priv.openIfExists("Gone.md", at)).toBe(false);
  });

  it("registers the four wiki processors plus managed note hints and their editor extensions", async () => {
    const r = loadRig();
    await r.p.onload();
    const p = r.p as unknown as { registerMarkdownPostProcessor: ReturnType<typeof vi.fn>; registerEditorExtension: ReturnType<typeof vi.fn> };
    expect(p.registerMarkdownPostProcessor).toHaveBeenCalledTimes(5);
    expect(p.registerEditorExtension).toHaveBeenCalledTimes(4);
  });

  it("the wiki follows the active EvoWiki note: builds the real controller if needed, follows only real file changes, never the demo", async () => {
    const r = loadRig();
    const fm: Record<string, unknown> = {};
    Object.assign((r.p as unknown as { app: object }).app, { metadataCache: { on: vi.fn(() => ({})), resolvedLinks: {},
      getFileCache: (f: TFile) => ({ frontmatter: fm[f.path] }) } });
    await r.p.onload();
    const on = (r.p as unknown as { app: { workspace: { on: ReturnType<typeof vi.fn> } } }).app.workspace.on;
    const handler = (name: string) => on.mock.calls.find((c) => c[0] === name)![1] as (x: unknown) => void;
    const file = (path: string) => Object.assign(new TFile(), { path, extension: "md" });
    const a = file("EvoWiki/me/Wiki/A.md"), b = file("EvoWiki/me/Wiki/B.md"), out = file("Notes/C.md");
    fm[a.path] = { evowiki_page_id: "p2" }; fm[b.path] = { evowiki_page_id: "p3" }; fm[out.path] = { evowiki_page_id: "p4" };
    const leaf = (f: TFile) => ({ view: Object.assign(Object.create(MarkdownView.prototype) as object, { file: f }) });
    const follow = vi.spyOn(WikiController.prototype, "followPage").mockResolvedValue(undefined);
    const openPage = vi.spyOn(WikiController.prototype, "openWikiPage");
    const priv = r.p as unknown as WikiPriv;
    try {
      handler("file-open")(out); // not a wiki note: no controller is built for it
      expect(priv.wiki).toBeNull();
      handler("file-open")(a); // a wiki note: the real controller is built and follows
      expect(priv.wiki).not.toBeNull();
      expect(priv.wiki!.state.demo).toBe(false);
      expect(follow.mock.calls).toEqual([["p2"]]);
      handler("active-leaf-change")(leaf(a)); // the same note again (refocus): nothing
      handler("active-leaf-change")({ view: {} }); // not a Markdown view
      expect(follow).toHaveBeenCalledOnce();
      handler("active-leaf-change")(leaf(b)); // a tab switch to another wiki note
      handler("file-open")(b); // the same switch reported twice: once
      expect(follow.mock.calls).toEqual([["p2"], ["p3"]]);
      handler("file-open")(out);
      handler("file-open")(a); // back to A after another note: follows again
      expect(follow.mock.calls).toEqual([["p2"], ["p3"], ["p2"]]);
      expect(openPage).not.toHaveBeenCalled(); // never the note-opening path
      priv.wiki!.dispose(); priv.wiki = null; priv.wikiDemo = true;
      handler("file-open")(b); // demo mode: never follows, nothing is built
      expect(priv.wiki).toBeNull();
      expect(follow).toHaveBeenCalledTimes(3);
    } finally {
      follow.mockRestore(); openPage.mockRestore();
    }
  });

  /** loadRig plus wiki notes with frontmatter and the workspace event handlers (follow tests). */
  async function followRig() {
    const r = loadRig();
    const fm: Record<string, unknown> = {};
    const leaves: { detach: ReturnType<typeof vi.fn> }[] = [];
    Object.assign((r.p as unknown as { app: object }).app, { metadataCache: { on: vi.fn(() => ({})), resolvedLinks: {},
      getFileCache: (f: TFile) => ({ frontmatter: fm[f.path] }) } });
    await r.p.onload();
    const ws = (r.p as unknown as { app: { workspace: Record<string, unknown> & { on: ReturnType<typeof vi.fn> } } }).app.workspace;
    Object.assign(ws, {
      getLeavesOfType: vi.fn((t: string) => (t === "evowiki-wiki" ? leaves : [])),
      getRightLeaf: vi.fn(() => { const l = { detach: vi.fn(() => { leaves.splice(leaves.indexOf(l), 1); }),
        setViewState: vi.fn(() => { leaves.push(l); return Promise.resolve(); }) }; return l; }),
      revealLeaf: vi.fn(() => Promise.resolve()),
    });
    const handler = (name: string) => ws.on.mock.calls.find((c) => c[0] === name)![1] as (x: unknown) => void;
    const file = (path: string, pageId: string | null) => {
      const f = Object.assign(new TFile(), { path, extension: "md" });
      fm[path] = pageId === null ? {} : { evowiki_page_id: pageId };
      return f;
    };
    const leaf = (f: TFile) => ({ view: Object.assign(Object.create(MarkdownView.prototype) as object, { file: f }) });
    const priv = r.p as unknown as { wiki: WikiController | null; wikiDemo: boolean; openWiki(demo: boolean): Promise<void>;
      wikiController(): WikiController };
    /** The controller's own state setter (private): what a write sets while it runs. */
    const setState = (c: WikiController, patch: object) => (c as unknown as { set(p: object): void }).set(patch);
    const busy = (on: boolean) => setState(priv.wiki!, { busy: on ? { what: "organize", abortable: true } : null });
    return { r, handler, file, leaf, priv, busy, leaves, setState };
  }

  it("tab rules (app.js boot / openTab): on start the sidebar opens on 今天 without focus; another Markdown note → 助手; 参数 only by click", async () => {
    const { r, handler, file, leaf, priv, leaves } = await followRig();
    const ws = (r.p as unknown as { app: { workspace: Record<string, unknown> } }).app.workspace;
    const a = file("Notes/A.md", null), b = file("Notes/B.md", null);
    const noteLeaf = leaf(a);
    Object.assign(noteLeaf.view, { leaf: noteLeaf });
    const setActiveLeaf = vi.fn();
    Object.assign(ws, { getActiveViewOfType: () => noteLeaf.view, setActiveLeaf, activeLeaf: noteLeaf });
    handler("file-open")(a);                       // layout restore before start-up is over: nothing
    layoutReady(r.p);
    await vi.waitFor(() => expect(leaves).toHaveLength(1));
    const set = (leaves[0] as unknown as { setViewState: ReturnType<typeof vi.fn> }).setViewState;
    expect(set).toHaveBeenCalledWith({ type: "evowiki-wiki", active: false });
    expect(priv.wiki!.state.tab).toBe("today");
    handler("active-leaf-change")(noteLeaf);       // focus back to the same note: stays on 今天
    expect(priv.wiki!.state.tab).toBe("today");
    priv.wiki!.setTab("engine");
    handler("file-open")(b);                       // another note: 助手
    expect(priv.wiki!.state.tab).toBe("assistant");
  });

  it("a wiki note opened while a write runs is followed once the write ends (final review I4)", async () => {
    const { handler, file, leaf, busy } = await followRig();
    const a = file("EvoWiki/me/Wiki/A.md", "p2"), b = file("EvoWiki/me/Wiki/B.md", "p3"), c = file("EvoWiki/me/Wiki/C.md", "p4");
    const out = file("Notes/X.md", null);
    const follow = vi.spyOn(WikiController.prototype, "followPage").mockResolvedValue(undefined);
    try {
      handler("file-open")(a);
      expect(follow.mock.calls).toEqual([["p2"]]);
      busy(true);                                   // e.g. 重新组织这一页 is running
      handler("active-leaf-change")(leaf(b));       // switched to B meanwhile: not followed yet
      handler("file-open")(b);
      expect(follow).toHaveBeenCalledOnce();
      busy(false);                                  // the write ends: B is followed now, once
      expect(follow.mock.calls).toEqual([["p2"], ["p3"]]);
      handler("active-leaf-change")(leaf(b));       // B again: already followed
      busy(true); busy(false);
      expect(follow).toHaveBeenCalledTimes(2);
      // A wiki note left for another note before the write ends is not followed afterwards.
      busy(true);
      handler("file-open")(c);
      handler("file-open")(out);
      busy(false);
      expect(follow).toHaveBeenCalledTimes(2);
    } finally {
      follow.mockRestore();
    }
  });

  it("the hand edit's leave trigger: leaving an EvoWiki note checks it 2 s later; coming back first cancels; never the demo", async () => {
    const { handler, file, leaf, priv } = await followRig();
    const a = file("EvoWiki/me/Wiki/A.md", "p2"), b = file("EvoWiki/me/Wiki/B.md", "p3"), out = file("Notes/C.md", null);
    const follow = vi.spyOn(WikiController.prototype, "followPage").mockResolvedValue(undefined);
    const left = vi.spyOn(WikiController.prototype, "noteLeft").mockResolvedValue(undefined);
    vi.useFakeTimers();
    try {
      handler("file-open")(a);
      handler("active-leaf-change")(leaf(a));          // the same note again (refocus): no leave
      handler("file-open")(out);                       // left A
      vi.advanceTimersByTime(1999);
      expect(left).not.toHaveBeenCalled();
      vi.advanceTimersByTime(1);
      expect(left.mock.calls).toEqual([["EvoWiki/me/Wiki/A.md", "p2"]]);
      handler("file-open")(b);                         // a plain note left: nothing due
      handler("file-open")(a);                         // left B …
      vi.advanceTimersByTime(1000);
      handler("file-open")(b);                         // … and back to B within 2 s: canceled (A is now due)
      vi.advanceTimersByTime(2000);
      expect(left.mock.calls).toEqual([["EvoWiki/me/Wiki/A.md", "p2"], ["EvoWiki/me/Wiki/A.md", "p2"]]);
      priv.wikiDemo = true;
      handler("file-open")(out);                       // left B in demo mode: nothing sent
      vi.advanceTimersByTime(2000);
      expect(left).toHaveBeenCalledTimes(2);
      priv.wikiDemo = false;
      handler("file-open")(a);
      handler("file-open")(out);                       // a pending leave is canceled on unload
      (priv as unknown as { onunload(): void }).onunload();
      vi.advanceTimersByTime(5000);
      expect(left).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
      follow.mockRestore(); left.mockRestore();
    }
  });

  it("an identity change drops a leave report still due (never sent to the new server / user)", async () => {
    const { handler, file, priv } = await followRig();
    const a = file("EvoWiki/me/Wiki/A.md", "p2"), out = file("Notes/C.md", null);
    const follow = vi.spyOn(WikiController.prototype, "followPage").mockResolvedValue(undefined);
    const left = vi.spyOn(WikiController.prototype, "noteLeft").mockResolvedValue(undefined);
    vi.useFakeTimers();
    try {
      handler("file-open")(a);
      handler("file-open")(out);                       // left A: a report is due in 2 s
      (priv as unknown as { resetWiki(): void }).resetWiki(); // new server / user id
      vi.advanceTimersByTime(5000);
      expect(left).not.toHaveBeenCalled();
      handler("file-open")(a);
      (priv as unknown as { resetWiki(): void }).resetWiki(); // while in A: the watched note is forgotten too
      handler("file-open")(out);
      vi.advanceTimersByTime(5000);
      expect(left).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
      follow.mockRestore(); left.mockRestore();
    }
  });

  it("a wiki note opened while the demo is open is followed when 打开个人 Wiki leaves the demo", async () => {
    const { handler, file, priv } = await followRig();
    const a = file("EvoWiki/me/Wiki/A.md", "p2");
    const follow = vi.spyOn(WikiController.prototype, "followPage").mockResolvedValue(undefined);
    try {
      await priv.openWiki(true);
      priv.wikiController();                       // the view factory builds the demo controller
      handler("file-open")(a);
      expect(follow).not.toHaveBeenCalled();
      await priv.openWiki(false);
      expect(priv.wikiDemo).toBe(false);
      expect(follow.mock.calls).toEqual([["p2"]]);
      expect(priv.wiki!.state.demo).toBe(false);
      handler("file-open")(a);                     // the same note again: already followed
      expect(follow).toHaveBeenCalledOnce();
    } finally {
      follow.mockRestore();
    }
  });

  it("closing the demo wiki's view leaves demo mode; closing the real one changes nothing (final review M8)", async () => {
    const { r, handler, file, leaf, priv } = await followRig();
    const factory = r.registerView.mock.calls.find((c) => c[0] === "evowiki-wiki")![1] as (l: unknown) => { onClose(): Promise<void> };
    const a = file("EvoWiki/me/Wiki/A.md", "p2");
    const follow = vi.spyOn(WikiController.prototype, "followPage").mockResolvedValue(undefined);
    try {
      priv.wikiDemo = true;
      const demoView = factory({});
      const demo = priv.wiki!;
      expect(demo.state.demo).toBe(true);
      const dispose = vi.spyOn(demo, "dispose");
      handler("file-open")(a);                     // demo: not followed
      await demoView.onClose();
      expect(dispose).toHaveBeenCalledOnce();
      expect(priv.wiki).toBeNull();
      expect(priv.wikiDemo).toBe(false);
      expect(r.p.reviewLimitHost().state().demo).toBe(false); // the settings row now uses the real wiki
      handler("active-leaf-change")(leaf(a));      // back in the note: followed on the real wiki
      expect(follow.mock.calls).toEqual([["p2"]]);
      const real = priv.wiki!;
      await factory({}).onClose();
      expect(priv.wiki).toBe(real);
    } finally {
      follow.mockRestore();
    }
  });

  it("a new server / user id drops the wiki controller, so the settings rows never show or save the old user's data (final review I1)", async () => {
    const { r, priv, leaves, setState } = await followRig();
    const accountInit = vi.fn(() => Promise.resolve());
    Object.assign(r.p, { account: { state: { kind: "loading" }, init: accountInit, dispose: vi.fn() } });
    await priv.openWiki(false);
    const old = priv.wikiController();
    setState(old, { catalog: { kind: "ready", catalog: { pages: [], settings: { review_per_day: 7, page_limit_usd: null, language: "zh" } } } });
    const dispose = vi.spyOn(old, "dispose");
    const init = vi.spyOn(WikiController.prototype, "init").mockResolvedValue(undefined);
    const oldTeach = r.p.teachHost();
    try {
      expect(leaves).toHaveLength(1);
      const oldLeaf = leaves[0];
      await r.p.updateSettings({ userId: "bob" });
      expect(leaves).not.toContain(oldLeaf);       // the wiki leaf (bound to the old controller) is closed
      expect(accountInit).toHaveBeenCalledOnce();  // GET /me again, for bob
      expect(dispose).toHaveBeenCalledOnce();
      expect(priv.wiki).not.toBe(old);
      const review = r.p.reviewLimitHost();
      expect(review.state().catalog.kind).toBe("idle"); // not the old user's settings
      review.load();
      expect(init).toHaveBeenCalledOnce();         // read again, for bob
      const fresh = priv.wiki!;
      expect(fresh).not.toBe(old);
      // 讲解方式: the section's next draw talks to the new controller's client, never the old one's.
      const teach = r.p.teachHost();
      expect(teach.api).toBe(fresh.teachPort().api);
      expect(teach.api).not.toBe(oldTeach.api);
    } finally {
      init.mockRestore();
    }
  });

  it("the reading assistant's pageEdited (an accepted 加入页面) goes to the real wiki controller; the demo does nothing", async () => {
    const r = loadRig();
    await r.p.onload();
    const priv = r.p as unknown as WikiPriv & { assistant: { deps: { pageEdited?: (pageId: string, applied: string[]) => void } };
      wikiController(): { pageEditedElsewhere(pageId: string): Promise<void> } };
    const hook = priv.assistant.deps.pageEdited;
    expect(hook).toBeTypeOf("function");
    // no wiki view opened yet: the real controller is built on first use
    expect(priv.wiki).toBeNull();
    hook!("p1", ["s1"]); // unconfigured settings: the controller sends nothing
    expect(priv.wiki).not.toBeNull();
    const real = priv.wikiController();
    expect(real).toBe(priv.wiki);
    expect(priv.wikiDemo).toBe(false);
    const edited = vi.spyOn(real, "pageEditedElsewhere").mockResolvedValue(undefined);
    hook!("p2", ["s2"]);
    expect(edited.mock.calls).toEqual([["p2"]]);
    // demo wiki: nothing is called and no controller is built or replaced
    priv.wiki = null;
    priv.wikiDemo = true;
    hook!("p3", []);
    expect(priv.wiki).toBeNull();
    expect(edited).toHaveBeenCalledOnce();
  });

  it("onunload disposes the wiki controller when there is one", () => {
    const p = new EvoWikiPlugin(...([] as unknown as ConstructorParameters<typeof EvoWikiPlugin>));
    const wikiDispose = vi.fn();
    Object.assign(p, { account: { dispose: vi.fn() }, conn: { stop: vi.fn() },
      wiki: { dispose: wikiDispose, canReportReading: () => false } });
    p.onunload();
    expect(wikiDispose).toHaveBeenCalledOnce();
  });
});

describe("the file menu: no 设为 EvoWiki 样板页 and no 交给 EvoWiki (both are said in the assistant)", () => {
  async function rig() {
    const p = new EvoWikiPlugin(...([] as unknown as ConstructorParameters<typeof EvoWikiPlugin>));
    const on = vi.fn((_name: string, _fn: unknown) => ({}));
    const cachedRead = vi.fn((f: TFile) => Promise.resolve(f.path === "Notes/写法.md" ? "---\na: 1\n---\n先举例。" : "OTHER"));
    const addCommand = vi.fn();
    Object.assign(p, {
      manifest: { id: "evowiki" }, loadData: () => Promise.resolve(null), saveData: () => Promise.resolve(),
      registerView: vi.fn(), addSettingTab: vi.fn(), addCommand, registerEvent: vi.fn(), registerMarkdownCodeBlockProcessor: vi.fn(),
      registerMarkdownPostProcessor: vi.fn(), registerEditorExtension: vi.fn(),
      app: { workspace: { getLeavesOfType: () => [], on, onLayoutReady: vi.fn(), getActiveFile: () => null },
        metadataCache: { on: vi.fn(() => ({})), resolvedLinks: {}, getFileCache: () => null },
        vault: { on: vi.fn(() => ({})), getMarkdownFiles: () => [], cachedRead, getAbstractFileByPath: () => null } },
    });
    await p.onload();
    const handlers = on.mock.calls.filter((c) => c[0] === "file-menu").map((c) => c[1] as (m: unknown, f: unknown) => void);
    const menuFor = (f: unknown) => {
      const items: { title: string; click: (() => void) | null }[] = [];
      const menu = { addItem: (cb: (i: object) => void) => {
        const it = { title: "", click: null as (() => void) | null };
        const api = { setTitle: (t: string) => { it.title = t; return api; }, setIcon: () => api,
          onClick: (fn: () => void) => { it.click = fn; return api; } };
        cb(api); items.push(it);
      } };
      for (const h of handlers) h(menu, f);
      return items;
    };
    const file = (path: string, extension = "md") =>
      Object.assign(new TFile(), { path, extension, basename: path.split("/").pop()!.replace(/\.[^.]*$/, "") });
    return { p, menuFor, file, cachedRead, addCommand };
  }

  it("no EvoWiki entry for a note or a material file; nothing is read; no concept-era command", async () => {
    const { menuFor, file, cachedRead, addCommand } = await rig();
    expect(menuFor(file("Notes/写法.md"))).toEqual([]);
    expect(menuFor(file("资料/讲义.pdf", "pdf"))).toEqual([]);
    expect(cachedRead).not.toHaveBeenCalled();
    expect(addCommand.mock.calls.map((c) => (c[0] as { id: string }).id)).not.toContain("open-sidebar");
  });
});

it("a data.json that cannot be read stops onload: no default server, nothing saved, a notice that stays", async () => {
  const p = new EvoWikiPlugin({} as never, {} as never);
  const saveData = vi.fn();
  const registerView = vi.fn();
  Object.assign(p, { manifest: { id: "evowiki", dir: ".obsidian/plugins/evowiki" }, saveData,
    loadData: () => Promise.reject(new SyntaxError("broken")), registerView });
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  await p.onload();
  expect((Notice as unknown as { last: { duration?: number } }).last.duration).toBe(0);
  expect(registerView).not.toHaveBeenCalled();
  expect(saveData).not.toHaveBeenCalled();
  expect(() => p.onunload()).not.toThrow();
});
