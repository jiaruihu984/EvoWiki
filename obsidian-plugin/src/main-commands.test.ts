import { TFile } from "obsidian";
import { afterEach, describe, expect, it, vi } from "vitest";
import EvoWikiPlugin, { BOT_POLL_MS } from "./main";
import { loadPluginData } from "./settings";
import { HttpGoalApi } from "./wiki/learn/goal-api";
import { FakeGoalApi, FixtureGoalApi } from "./wiki/learn/goal-fake";
import { LearnView } from "./wiki/learn/learn-view";
import { renderLearn } from "./wiki/render-learn";
import type { WikiController } from "./wiki/controller";
import type { KGridOut } from "./wiki/schema";
import { wikiActions } from "./wiki/wiring";
import type { AssistantController } from "./assistant/controller";
import { LEARN_COPY } from "./wiki/learn/copy";

type Cmd = { id: string; name: string; callback?: () => void; checkCallback?: (check: boolean) => boolean };
type Page = { page_id: string; organized: boolean; collapsed: boolean };

const rigs: EvoWikiPlugin[] = [];
// Unload every plugin a test built: its focus listener and 5-minute interval must not outlive it.
afterEach(() => { for (const p of rigs.splice(0)) (p as unknown as { unload(): void }).unload(); });

/** The plugin loaded with a stub wiki controller (the fields the commands read) and an optional active note. */
function rig(opts: { active?: { path: string; pageId: string | null } | null; page?: Page | null; busy?: boolean; conn?: string } = {}) {
  const p = new EvoWikiPlugin(...([] as unknown as ConstructorParameters<typeof EvoWikiPlugin>));
  rigs.push(p);
  const addCommand = vi.fn();
  const addRibbonIcon = vi.fn();
  const active = opts.active === undefined ? { path: "EvoWiki/z-score.md", pageId: "p1" } : opts.active;
  const file = active === null ? null : Object.assign(new TFile(), { path: active.path, extension: "md" });
  Object.assign(p, {
    manifest: { id: "evowiki" }, loadData: () => Promise.resolve({ bootstrapLanguage: "zh" }), saveData: () => Promise.resolve(),
    registerView: vi.fn(), addSettingTab: vi.fn(), addCommand, addRibbonIcon, registerEvent: vi.fn(),
    registerMarkdownCodeBlockProcessor: vi.fn(), registerMarkdownPostProcessor: vi.fn(), registerEditorExtension: vi.fn(),
    app: {
      workspace: { on: vi.fn(() => ({})), onLayoutReady: vi.fn(), getActiveFile: () => file, getLeavesOfType: () => [] },
      metadataCache: { on: vi.fn(() => ({})), resolvedLinks: {},
        getFileCache: () => (active === null ? null : { frontmatter: active.pageId === null ? {} : { evowiki_page_id: active.pageId } }) },
      vault: { on: vi.fn(() => ({})), getMarkdownFiles: () => [] },
    },
  });
  const state = {
    demo: false, configured: true, conn: opts.conn ?? "online", busy: opts.busy ? { what: "x" } : null,
    catalogNote: null as null | { text: string; tone: string },
    page: opts.page === undefined ? { kind: "open", page: { page_id: "p1", organized: true, collapsed: false } }
      : opts.page === null ? { kind: "none" } : { kind: "open", page: opts.page },
  };
  const stub = { refreshAccountLanguage: () => Promise.resolve(), refreshLocale: () => Promise.resolve(),
    state, followPage: vi.fn(() => Promise.resolve()), openFeedback: vi.fn(), organize: vi.fn(() => Promise.resolve()),
    expand: vi.fn(() => { (state.page as { page: Page }).page.collapsed = false; return Promise.resolve(); }),
    openLearnNew: vi.fn(), botPause: vi.fn(() => Promise.resolve()), refreshToday: vi.fn(() => Promise.resolve()), setTab: vi.fn(), personalView: vi.fn(), openManagePrefs: vi.fn(), subscribe: vi.fn(() => () => undefined), dispose: vi.fn(), canReportReading: () => false,
  };
  Object.assign(p, { wiki: stub });
  return { p, stub, addCommand, addRibbonIcon };
}

async function load(r: ReturnType<typeof rig>): Promise<Map<string, Cmd>> {
  await r.p.onload();
  return new Map(r.addCommand.mock.calls.map((c) => [(c[0] as Cmd).id, c[0] as Cmd]));
}

/** app.js COMMANDS, in its order (Obsidian prefixes 「EvoWiki: 」). */
const COMMANDS: [string, string][] = [["initial-setup", "首次设置"],
  ["open-starmap", "打开知识图谱"], ["open-engine", "个性化"], ["open-review", "今天复习"], ["new-goal", "学点新的"],
  ["open-assistant", "打开陪读助手"], ["recent-changes", "活动记录"], ["open-settings", "打开设置"]];

describe("palette commands (T12: app.js COMMANDS)", () => {
  it("exactly the demo's commands, in its order, with its names (no 本周回顾 / 整理 Wiki / 这一页哪里不好… / 重新组织这一页 / 暂停 Bot)", async () => {
    const r = rig();
    await r.p.onload();
    expect(r.addCommand.mock.calls.map((c) => [(c[0] as Cmd).id, (c[0] as Cmd).name])).toEqual(COMMANDS);
  });

  it("个性化 / 活动记录 open the sidebar on 个性化 (活动记录 expanded)", async () => {
    const r = rig({ active: null });
    const cmds = await load(r);
    const openWiki = vi.fn(() => Promise.resolve());
    Object.assign(r.p, { openWiki });
    cmds.get("open-engine")?.callback?.();
    await vi.waitFor(() => expect(r.stub.setTab).toHaveBeenCalledWith("engine"));
    r.stub.setTab.mockClear();
    cmds.get("recent-changes")?.callback?.();
    await vi.waitFor(() => expect(r.stub.setTab).toHaveBeenCalledWith("engine"));
    expect(r.stub.personalView.mock.calls).toEqual([[{ manage: false }], [{ manage: false, log: true }]]);
    expect(openWiki).toHaveBeenCalledTimes(2);
  });

  it("活动记录 scrolls 个性化 to its 活动记录 entry", async () => {
    const r = rig({ active: null });
    const cmds = await load(r);
    Object.assign(r.p, { openWiki: vi.fn(() => Promise.resolve()) });
    const root = document.createElement("div");
    const log = root.appendChild(document.createElement("button"));
    log.id = "pz-log";
    const scrolled: string[] = [];
    log.scrollIntoView = () => { scrolled.push(log.id); };
    Object.assign(r.p.app.workspace, { getLeavesOfType: () => [{ view: { containerEl: root } }] });
    cmds.get("recent-changes")?.callback?.();
    await vi.waitFor(() => expect(scrolled).toEqual(["pz-log"]));
  });

  it("学点新的 opens the 学点新的 tab (no topic), with no note open", async () => {
    const r = rig({ active: null });
    const cmds = await load(r);
    const openLearn = vi.spyOn(r.p, "openLearn").mockResolvedValue();
    cmds.get("new-goal")?.callback?.();
    expect(openLearn).toHaveBeenCalledWith("", true);      // the empty form, not the running goal
    expect(r.stub.openLearnNew).not.toHaveBeenCalled();
  });

  it("the knowledge map (知识图谱) keeps its one ribbon icon (no second one)", async () => {
    const r = rig();
    await r.p.onload();
    expect(r.addRibbonIcon.mock.calls.map((c) => [c[0] as string, c[1] as string])).toEqual([["network", "EvoWiki 知识图谱"]]);
  });
});

describe("学点新的 tab (T11)", () => {
  it("Today and a saved goal card reopen the persistent path when no chat run is in memory", async () => {
    const p = new EvoWikiPlugin(...([] as unknown as ConstructorParameters<typeof EvoWikiPlugin>));
    const showGoal = vi.fn(() => Promise.resolve());
    const view = Object.assign(Object.create(LearnView.prototype) as LearnView, { openTopic: vi.fn(), showGoal });
    const leaf = { view };
    const workspace = { getLeavesOfType: () => [leaf], revealLeaf: vi.fn(() => Promise.resolve()), setActiveLeaf: vi.fn() };
    Object.assign(p, { app: { workspace }, assistant: { goalRun: () => undefined } });
    const internal = p as unknown as { goalProgressHost(): { openGoal(id: string): void }; openGoalResult(actionId: string, mapId: string): Promise<void> };
    internal.goalProgressHost().openGoal("saved-map");
    await vi.waitFor(() => expect(showGoal).toHaveBeenCalledWith("saved-map"));
    await internal.openGoalResult("forgotten-action", "other-map");
    expect(showGoal).toHaveBeenCalledWith("other-map");
    expect(workspace.revealLeaf).toHaveBeenCalledTimes(2);
  });

  it("openLearn opens a main-area tab with the topic, or reveals the open one (the view keeps or restarts its flow)", async () => {
    const p = new EvoWikiPlugin(...([] as unknown as ConstructorParameters<typeof EvoWikiPlugin>));
    const open = vi.fn();
    const view = Object.assign(Object.create(LearnView.prototype) as LearnView, { openTopic: open });
    const leaf = { view: null as unknown, setViewState: vi.fn(() => { leaf.view = view; return Promise.resolve(); }) };
    const leaves: unknown[] = [];
    const workspace = { getLeavesOfType: vi.fn(() => leaves), getLeaf: vi.fn(() => leaf), revealLeaf: vi.fn(() => Promise.resolve()), setActiveLeaf: vi.fn() };
    Object.assign(p, { app: { workspace } });
    await p.openLearn("贝叶斯定理");
    expect(workspace.getLeavesOfType).toHaveBeenCalledWith("evowiki-learn");
    expect(workspace.getLeaf).toHaveBeenCalledWith("tab");
    expect(leaf.setViewState).toHaveBeenCalledWith({ type: "evowiki-learn", active: true });
    expect(open).toHaveBeenCalledWith("贝叶斯定理");
    leaves.push(leaf);
    await p.openLearn("");
    expect(workspace.getLeaf).toHaveBeenCalledTimes(1);
    expect(workspace.revealLeaf).toHaveBeenCalledTimes(2);
    expect(open).toHaveBeenLastCalledWith("");
  });

  it("the goal API: the big-goal routes (HTTP) by default; with plugin data fixture \"demo\" the scripted fake only for the demo goal", async () => {
    const r = rig({ active: null });
    await r.p.onload();
    const api = (p: EvoWikiPlugin): unknown => (p as unknown as { goalApi(): unknown }).goalApi();
    expect(api(r.p)).toBeInstanceOf(HttpGoalApi);
    expect((api(r.p) as HttpGoalApi).live).toBe(true);
    // The old hidden switch is gone: plugin data no longer keeps it.
    expect("goalBackend" in loadPluginData({ goalBackend: "live" })).toBe(false);
    const f = rig({ active: null });
    Object.assign(f.p, { loadData: () => Promise.resolve({ fixture: "demo" }) });
    await f.p.onload();
    const routed = api(f.p) as FixtureGoalApi;
    expect(routed).toBeInstanceOf(FixtureGoalApi);
    expect(routed.fake).toBeInstanceOf(FakeGoalApi);
    expect(routed.http).toBeInstanceOf(HttpGoalApi);                // any other goal: the live routes (goal-fake.test.ts)
  });

  it("a deferred 学点新的 tab is loaded before the topic is handed over", async () => {
    const p = new EvoWikiPlugin(...([] as unknown as ConstructorParameters<typeof EvoWikiPlugin>));
    const open = vi.fn();
    const view = Object.assign(Object.create(LearnView.prototype) as LearnView, { openTopic: open });
    const leaf = { view: {} as unknown, loadIfDeferred: vi.fn(() => { leaf.view = view; return Promise.resolve(); }) };
    const workspace = { getLeavesOfType: vi.fn(() => [leaf]), revealLeaf: vi.fn(() => Promise.resolve()), setActiveLeaf: vi.fn() };
    Object.assign(p, { app: { workspace } });
    await p.openLearn("奇异值分解");
    expect(leaf.loadIfDeferred).toHaveBeenCalledTimes(1);
    expect(open).toHaveBeenCalledWith("奇异值分解");
  });

  it.each(["recommendation", "today"])("%s opens the main-area background form and only its confirmation starts the existing goal chat", async (entry) => {
    vi.stubGlobal("requestAnimationFrame", () => 1);
    vi.stubGlobal("cancelAnimationFrame", vi.fn());
    vi.stubGlobal("ResizeObserver", class { observe(): void {} disconnect(): void {} });
    vi.stubGlobal("matchMedia", () => ({ matches: false }));
    const real = window.getComputedStyle.bind(window);
    vi.stubGlobal("getComputedStyle", (e: Element) => Object.assign(real(e), { getPropertyValue: () => "#888888", fontFamily: "sans-serif" }));
    const rect = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "getBoundingClientRect");
    HTMLElement.prototype.getBoundingClientRect = () => ({ width: 800, height: 600, top: 0, left: 0, right: 800, bottom: 600, x: 0, y: 0, toJSON: () => ({}) });
    try {
      const r = rig({ active: null });
      Object.assign(r.p, { wiki: null, loadData: () => Promise.resolve({ fixture: "demo" }) });
      await r.p.onload();
      const assistant = (r.p as unknown as { assistant: AssistantController }).assistant;
      const start = vi.spyOn(assistant, "startGoalChat").mockReturnValue({ key: "new-goal", done: Promise.resolve(true) });
      Object.assign(r.p, { openAssistant: vi.fn(async () => {}) });
      const factory = (r.p.registerView as unknown as { mock: { calls: [string, (leaf: unknown) => LearnView][] } }).mock.calls
        .find((c) => c[0] === "evowiki-learn")![1];
      const leaf: { view: LearnView | null; setViewState: () => Promise<void> } = {
        view: null, setViewState: async () => { leaf.view = factory(leaf); await leaf.view.onOpen(); } };
      Object.assign(r.p.app.workspace, { getLeaf: () => leaf, revealLeaf: () => Promise.resolve(), setActiveLeaf: () => {}, offref: () => {} });
      const c = (r.p as unknown as { wikiController(): WikiController }).wikiController();
      const k = { kind: "ready" as const, value: { concepts: [] } as unknown as KGridOut };
      const card = renderLearn({ ...c.state, configured: true, conn: "online", k,
        recommendations: [{ conceptId: "svd", name: "奇异值分解", reason: "接着矩阵学", pageId: null }] }, wikiActions(() => c));
      const topic = entry === "today" ? "辅酶q" : "奇异值分解";
      if (entry === "today") {
        const input = card.querySelector<HTMLInputElement>("[data-action=learn-topic]")!;
        input.value = topic; input.dispatchEvent(new Event("input"));
        card.querySelector<HTMLButtonElement>("[data-action=learn-start]")!.click();
      } else card.querySelector<HTMLButtonElement>("[data-action=recommend]")!.click();
      await vi.waitFor(() => expect(leaf.view).not.toBeNull());
      await vi.waitFor(() => expect(leaf.view!.contentEl.querySelector<HTMLInputElement>("input[type=text]")?.value).toBe(topic));
      expect(leaf.view!.state?.state.topic).toBe(topic);
      expect(leaf.view!.state?.state.stage).toBe("goal");
      expect(start).not.toHaveBeenCalled();
      const background = leaf.view!.contentEl.querySelector<HTMLTextAreaElement>("textarea")!;
      background.value = "没有医学基础，希望读懂保健品说明";
      background.dispatchEvent(new Event("input"));
      [...leaf.view!.contentEl.querySelectorAll<HTMLButtonElement>("button")].find((b) => b.textContent === LEARN_COPY.start)!.click();
      await vi.waitFor(() => expect(start).toHaveBeenCalledTimes(1));
      expect(start).toHaveBeenCalledWith(expect.any(String), { goal: topic, background: background.value }, []);
      await leaf.view!.onClose();
      c.dispose();
    } finally {
      vi.unstubAllGlobals();
      if (rect) Object.defineProperty(HTMLElement.prototype, "getBoundingClientRect", rect);
    }
  });
});

describe("Bot pull (Task 5): every 5 minutes and on focus", () => {
  it("BOT_POLL_MS elapsing refreshes once (forced); a window focus refreshes (forced); unload clears both", async () => {
    vi.useFakeTimers();
    try {
      const r = rig();
      const refreshToday = vi.fn(() => Promise.resolve());
      Object.assign(r.stub, { refreshToday });
      await r.p.onload();
      vi.advanceTimersByTime(BOT_POLL_MS - 1);
      expect(refreshToday).not.toHaveBeenCalled();
      vi.advanceTimersByTime(1);
      expect(refreshToday).toHaveBeenCalledTimes(1);
      expect(refreshToday).toHaveBeenLastCalledWith(true);
      window.dispatchEvent(new Event("focus"));
      expect(refreshToday).toHaveBeenCalledTimes(2);
      expect(refreshToday).toHaveBeenLastCalledWith(true);
      (r.p as unknown as { unload(): void }).unload();
      vi.advanceTimersByTime(BOT_POLL_MS * 2);
      window.dispatchEvent(new Event("focus"));
      expect(refreshToday).toHaveBeenCalledTimes(2);
    } finally { vi.useRealTimers(); }
  });

  it("before any sidebar controller exists a tick builds none and calls nothing", async () => {
    vi.useFakeTimers();
    try {
      const r = rig();
      Object.assign(r.p, { wiki: null });
      const make = vi.spyOn(r.p as unknown as { wikiController(): unknown }, "wikiController");
      await r.p.onload();
      make.mockClear();
      vi.advanceTimersByTime(BOT_POLL_MS);
      window.dispatchEvent(new Event("focus"));
      expect(make).not.toHaveBeenCalled();
      expect((r.p as unknown as { wiki: unknown }).wiki).toBeNull();
      (r.p as unknown as { unload(): void }).unload();
    } finally { vi.useRealTimers(); }
  });
});
