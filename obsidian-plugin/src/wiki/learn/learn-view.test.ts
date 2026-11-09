import type { WorkspaceLeaf } from "obsidian";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../api/errors";
import { noticeHost } from "../../ui/ew-notice";
import { sortDrag } from "../../ui/reorder-test-kit";
import { FakeSigma } from "../graph-page/fake-sigma";
import { GraphFixture } from "../graph-page/fixture";
import type { GraphSource } from "../graph-page/store";
import { FakeGoalApi, fakeBlocks, registerBlockGoal } from "./goal-fake";
import type { GoalApi } from "./goal-api";
import { LEARN_VIEW_TYPE, LearnView, type LearnHost, type LearnSources } from "./learn-view";
import { LearnFlow } from "./learn-flow";
import { GoalRun } from "../../assistant/goal-run";
import { DIAGNOSED, NESTED_MAP, NestedGoalApi } from "./nested-goal-kit";
import { ReadingGoalApi } from "./reading-goal-kit";
import { LEARN_COPY } from "./copy";
import { ewNotice } from "../../ui/ew-notice";
import type { GraphPlacesOut, PathPageOut, ProgressOut, SectionEditIn, SourceDetail, SourceOut } from "../schema";
import { PAGE_PROGRESS_POLL_MS } from "./page-progress";
import { SourceBlocked, SourceRefused, type SourceUpdate } from "../sources/sources-controller";
import { WIKI_COPY } from "../copy";
import { SOURCES_COPY } from "../sources/sources-copy";

const TOKENS: Record<string, string> = {
  "--known": "#2f9e5b", "--likely": "#6cbf84", "--shaky": "#d99a1e", "--gap": "#d9483b", "--none": "#b8b8b8",
  "--tx-1": "#222222", "--tx-2": "#5c5c5c", "--tx-3": "#8a8a8a", "--edge": "#d2d2d2", "--accent": "#1f1f1f",
};
const flush = async (): Promise<void> => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
let frames: FrameRequestCallback[];
let reduced = false;
let scrolled: Element[] = [];

beforeEach(() => {
  frames = []; reduced = false; scrolled = [];
  noticeHost().replaceChildren();
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => { frames.push(cb); return frames.length; });
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
  vi.stubGlobal("matchMedia", () => ({ get matches() { return reduced; } }));
  vi.stubGlobal("ResizeObserver", class { observe(): void {} disconnect(): void {} });
  const real = window.getComputedStyle.bind(window);
  vi.stubGlobal("getComputedStyle", (e: Element) => Object.assign(real(e), { getPropertyValue: (n: string) => TOKENS[n] ?? "", fontFamily: "sans-serif" }));
  HTMLElement.prototype.getBoundingClientRect = () => ({ width: 800, height: 600, top: 0, left: 0, right: 800, bottom: 600, x: 0, y: 0, toJSON: () => ({}) });
  HTMLElement.prototype.scrollIntoView = function (this: Element) { scrolled.push(this); };
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); document.body.replaceChildren(); });

/**
 * `run`: the chat drives the goal (one flow): the fake assistant's run is a LearnFlow on the same API (driven by the test
 * through its own routes, as the chat's goal run does), and the view shows it; 进入 asks the run to enter the block.
 */
function make(o: { api?: GoalApi; writeFirstPage?: LearnHost["writeFirstPage"]; graph?: GraphSource; configured?: () => boolean; sources?: LearnSources;
  writeProgress?: LearnHost["writeProgress"]; run?: boolean; start?: NonNullable<LearnHost["assistant"]>["start"];
  progress?: () => ProgressOut | null; onProgress?: LearnHost["onProgress"]; openPage?: LearnHost["openPage"] } = {}) {
  let cssFn: (() => void) | null = null;
  const apis: GoalApi[] = [];
  const runApi = o.api ?? new FakeGoalApi({ roundMs: 0 });
  const runFlow = o.run ? new LearnFlow(runApi, { fail: (t) => { ewNotice(t); } }, "") : null;
  const entered: (string | null)[] = [];
  const run = runFlow === null ? null : { actionId: "g1", flow: runFlow,
    enterBlock: (r: string | null) => { entered.push(r); return r !== null ? runFlow.enter(r) : Promise.resolve(); } };
  const host: LearnHost = {
    api: () => { const a = o.api ?? new FakeGoalApi({ roundMs: 0 }); apis.push(a); return a; },
    onCssChange: (fn) => { cssFn = fn; return () => { cssFn = null; }; },
    ...(o.writeFirstPage ? { writeFirstPage: o.writeFirstPage } : {}),
    ...(o.graph ? { graph: () => o.graph! } : {}),
    ...(o.configured ? { configured: o.configured } : {}),
    ...(o.sources ? { sources: o.sources } : {}),
    ...(o.writeProgress ? { writeProgress: o.writeProgress } : {}),
    ...(o.progress ? { progress: o.progress } : {}),
    ...(o.onProgress ? { onProgress: o.onProgress } : {}),
    ...(o.openPage ? { openPage: o.openPage } : {}),
    ...(o.run || o.start ? { assistant: { start: o.start ?? (() => null), onGoalRow: () => () => {}, run: () => run } } : {}),
  };
  const view = new LearnView({} as WorkspaceLeaf, host);
  document.body.append(view.contentEl);
  const panel = (): HTMLElement => view.contentEl.querySelector<HTMLElement>(".ew-panel")!;
  const cards = (): HTMLElement[] => [...panel().children] as HTMLElement[];
  const heads = (): string[] => cards().map((c) => c.querySelector("h2")?.textContent ?? `(${c.className}) ${c.textContent ?? ""}`);
  const button = (text: string): HTMLButtonElement => {
    const b = [...panel().querySelectorAll<HTMLButtonElement>("button")].filter((x) => x.textContent === text).pop();
    if (!b) throw new Error(`no button ${text}`);
    return b;
  };
  const round = (): HTMLElement => cards().filter((c) => c.querySelector(".ew-q")).pop()!;
  /** Picks option `i` of every question of the last round (−1 = the last option, 不知道 for a quiz). */
  const pickAll = (picks: number[]): void => {
    round().querySelectorAll(".ew-q").forEach((q, qi) => {
      const radios = [...q.querySelectorAll<HTMLInputElement>("input[type=radio]")];
      const r = picks[qi]! < 0 ? radios[radios.length - 1]! : radios[picks[qi]!]!;
      r.checked = true; r.dispatchEvent(new Event("change"));
    });
  };
  return { view, apis, panel, cards, heads, button, round, pickAll, css: () => cssFn?.(), hasCss: () => cssFn !== null, runFlow, entered };
}
/** A goal confirmed in the chat (the fake's blocks as its assistant proposed them), opened by the chat's run as 确认 does. */
async function opened(m: ReturnType<typeof make>, goal = "我要学机器学习"): Promise<void> {
  await m.runFlow!.openGoal(registerBlockGoal(goal, fakeBlocks(goal))); await flush();
}
/** The chat's run through the opening diagnosis to the 学习路径 (the run's own routes; the view only shows it). */
async function toPath(m: ReturnType<typeof make>, r2: number[] = [0, 0, 1, -1], goal = "我要学机器学习"): Promise<void> {
  const f = m.runFlow!;
  await opened(m, goal);
  const answer = async (picks: number[]): Promise<void> => {
    const r = f.state.rounds[f.state.rounds.length - 1]!;
    r.questions.forEach((q, qi) => { const p = picks[qi]!; f.pick(r, q.index, p >= 0 ? p : q.kind === "quiz" ? -1 : q.options.length - 1); });
    await f.submit(r); await flush();
  };
  await answer([0, 0, 0, 0]);
  await answer(r2);
  if (f.state.draftId !== null) await answer([0]);
}

describe("学点新的 view (big-goal-demo.html as build.py transforms it)", () => {
  it("the DOM: div.ew.ew-learn > .ew-app > .ew-panel + .ew-cloudwrap (the cloud box, the empty line, hidden .ew-tip, legend 掌握/熟悉/不会)", async () => {
    const m = make();
    await m.view.onOpen();
    expect(m.view.getViewType()).toBe(LEARN_VIEW_TYPE);
    expect(LEARN_VIEW_TYPE).toBe("evowiki-learn");
    expect(m.view.getDisplayText()).toBe("学点新的");
    expect(m.view.getIcon()).toBe("sparkles");
    const root = m.view.contentEl.firstElementChild as HTMLElement;
    expect(root.className).toBe("ew ew-learn");
    const app = root.firstElementChild as HTMLElement;
    expect(app.className).toBe("ew-app");
    expect([...app.children].map((c) => c.className)).toEqual(["ew-panel", "ew-cloudwrap"]);
    const wrap = app.children[1]!;
    expect([...wrap.children].map((c) => `${c.tagName.toLowerCase()}.${c.className}`)).toEqual(["div.ew-sigma", "div.ew-cloud-empty", "div.ew-tip", "div.ew-legend"]);
    expect(wrap.querySelector(".ew-sigma")!.hasAttribute("aria-label")).toBe(false);   // Obsidian would show it as a hover tooltip
    expect((wrap.children[2] as HTMLElement).hidden).toBe(true);
    expect([...wrap.querySelectorAll(".ew-legend span")].map((s) => [s.textContent, (s.firstElementChild as HTMLElement).style.background]))
      .toEqual([["掌握", "var(--known)"], ["熟悉", "var(--shaky)"], ["不会", "var(--gap)"]]);
  });

  it("the goal card: 新的目标, 想学什么 (the demo goal on the fake), 你的背景 (empty), 开始; a topic is prefilled instead", async () => {
    const m = make();
    await m.view.onOpen();
    const c = m.cards();
    expect(c.length).toBe(1);
    expect(c[0]!.className).toBe("ew-card");
    const labels = [...c[0]!.querySelectorAll("label.ew-field")];
    expect(labels.map((l) => l.firstChild?.textContent)).toEqual(["想学什么", "你的背景"]);
    expect(labels[0]!.querySelector<HTMLInputElement>("input[type=text]")!.value).toBe("我要学机器学习");
    expect(labels[1]!.querySelector("textarea")!.value).toBe("");
    expect(m.button("开始").className).toBe("ew-btn ew-primary");
    m.view.openTopic("贝叶斯定理");
    expect(m.panel().querySelector<HTMLInputElement>("input[type=text]")!.value).toBe("贝叶斯定理");
  });

  it("goal + 背景 over the message's limit (as the 助手 shows it): n/4000 under the form and 开始 waits; nothing is cut or sent (D28)", async () => {
    const sent: string[] = [];
    const m = make({ start: (text) => { sent.push(text); return null; } });
    await m.view.onOpen();
    const ta = m.panel().querySelector("textarea")!;
    const over = (): HTMLElement => m.panel().querySelector<HTMLElement>("[data-role=over]")!;
    ta.value = "长".repeat(2500); ta.dispatchEvent(new Event("input"));       // 2,500 背景: fine
    expect(over().hidden).toBe(true);
    expect(m.button("开始").disabled).toBe(false);
    ta.value = "长".repeat(3995); ta.dispatchEvent(new Event("input"));       // 我要学机器学习 + 背景：… > 4000
    expect(over().hidden).toBe(false);
    expect(over().textContent).toBe(`${[..."我要学机器学习\n背景："].length + 3995}/4000`);
    expect(m.button("开始").disabled).toBe(true);
    m.button("开始").click();
    expect(sent).toEqual([]);
    ta.value = "长".repeat(3000); ta.dispatchEvent(new Event("input"));
    expect(over().hidden).toBe(true);
    expect(m.button("开始").disabled).toBe(false);
  });

  it("学习路径: regions in prerequisite order; the state is the dot and the word after the name; one button, also on a known block", async () => {
    const m = make({ run: true });
    await m.view.onOpen();
    await toPath(m);
    expect(m.heads().slice(-1)).toEqual(["学习路径"]);
    expect(m.panel().querySelectorAll(".ew-q").length).toBe(0);           // the diagnosis is in the chat
    const rows = [...m.cards().pop()!.querySelectorAll("ol.ew-elist li")];
    expect(rows.map((li) => [li.querySelector(".ew-rname")!.textContent, li.getAttribute("aria-label")!.split("，")[1],
      li.children[2]!.className, li.children[2]!.textContent]))
      .toEqual([
        ["线性代数", "掌握", "ew-act", "开始学习"],      // known is a state: it can still be learned
        ["概率论", "熟悉", "ew-act", "开始学习"],        // mostly known, not every cell: not 掌握
        ["微积分", "熟悉", "ew-act", "开始学习"],
        ["优化", "还没问", "ew-act", "开始学习"],
        ["监督学习", "熟悉", "ew-act", "开始学习"],
        ["模型评估", "不会", "ew-act", "开始学习"],
      ]);
    // the dot carries the same state; ⋮⋮ / ⋯ are in the last cell (shown on hover by the CSS)
    expect(rows.map((li) => (li.querySelector(".ew-dot") as HTMLElement).style.background)).toEqual(
      ["var(--known)", "var(--shaky)", "var(--shaky)", "var(--none)", "var(--shaky)", "var(--gap)"]);
    expect(rows.every((li) => li.children[3]!.className === "ew-ops")).toBe(true);
  });

  it("开始学习 opens the block under its row: its pages with 学完能：, 添加页面 last, its button 收起; scrolled into view", async () => {
    const m = make({ run: true });
    await m.view.onOpen();
    await toPath(m);
    m.button("开始学习").click(); await flush();   // the last 开始学习 = 模型评估: the run enters it
    expect(m.entered).toEqual(["ev"]);
    expect(m.heads().slice(-1)).toEqual(["学习路径"]);                  // no card of its own
    const box = m.panel().querySelector<HTMLElement>("[data-entered]")!;
    expect(box.dataset.entered).toBe("ev");
    const row = box.closest("li.ew-nest")!.previousElementSibling!;
    expect([row.classList.contains("ew-cur"), row.querySelector(".ew-go")!.textContent]).toEqual([true, "收起"]);
    const li = box.querySelector("ol.ew-elist.ew-pg li")!;
    expect([...li.children].map((c) => c.className)).toEqual(["ew-fill", "ew-n", "", "ew-act", "ew-ops"]);   // the writing card's fill first
    expect(li.querySelector(".ew-t")!.textContent).toBe("怎么知道模型好不好");
    expect(li.querySelector(".ew-scope")!.textContent).toBe("学完能：能判断过拟合，并用交叉验证选模型");
    expect(li.querySelector(".ew-go")!.textContent).toBe("开始生成");
    expect(box.lastElementChild!.querySelector("input")!.placeholder).toBe("添加页面");
    expect((scrolled[scrolled.length - 1] as HTMLElement).dataset.entered).toBe("ev");
    row.querySelector<HTMLButtonElement>(".ew-go")!.click();           // 收起: the list folds, nothing is asked again
    expect(m.panel().querySelector("[data-entered]")).toBeNull();
    expect(m.entered).toEqual(["ev"]);
    m.panel().querySelector<HTMLLIElement>("li.ew-cur .ew-go")!.click();
    expect(m.panel().querySelector("[data-entered]")).not.toBeNull();
    expect(m.entered).toEqual(["ev"]);
  });

  it("a page row's one button: 开始生成 writes that row (its key), its progress while written, then 打开", async () => {
    const fake = new FakeGoalApi({ roundMs: 0 });
    const realPath = fake.getPath.bind(fake);
    let row: Partial<PathPageOut> | null = null;
    vi.spyOn(fake, "getPath").mockImplementation(async (id) => {
      const p = await realPath(id);
      return row === null ? p : { ...p, pages: p.pages.map((x, i) => (i === 0 ? { ...x, ...row } : x)) };
    });
    const write = vi.fn((_t: Parameters<NonNullable<LearnHost["writeFirstPage"]>>[0]) => Promise.resolve());
    const opened: string[] = [];
    let emit = (_p: { pageId: string; done: number; of: number; ended: boolean; ok: boolean }): void => {};
    const m = make({ api: fake, run: true, writeFirstPage: write, openPage: (id: string) => { opened.push(id); },
      writeProgress: (fn) => { emit = fn; return () => {}; } });
    await m.view.onOpen();
    await toPath(m);
    m.button("开始学习").click(); await flush();
    const go = (): HTMLElement => m.panel().querySelector<HTMLElement>("ol.ew-pg > li .ew-act > *")!;
    expect(go().textContent).toBe("开始生成");
    // The shared writer can start before the path row learns its page id.
    emit({ pageId: "pg_pending", done: 0, of: 4, ended: false, ok: false });
    expect((go() as HTMLButtonElement).disabled).toBe(true);
    go().click();
    expect(write).not.toHaveBeenCalled();
    emit({ pageId: "pg_pending", done: 0, of: 4, ended: true, ok: false });
    expect((go() as HTMLButtonElement).disabled).toBe(false);
    const key = m.runFlow!.state.entered!.rows[0]!.key;
    go().click(); await flush();
    expect(write.mock.calls[0]![0]).toMatchObject({ pathId: "fake-path-ev", pageId: null, key });
    row = { page_id: "pg_1", state: "writing", progress: { section: 1, of: 4, pass: 1 } };
    await m.runFlow!.readPath(); await flush();
    expect([go().tagName, go().textContent]).toEqual(["SPAN", "25%"]);
    row = { page_id: "pg_1", state: "written", progress: null };
    await m.runFlow!.readPath(); await flush();
    expect(go().textContent).toBe("打开");
    go().click();
    m.panel().querySelector<HTMLElement>("ol.ew-pg > li .ew-rowhit")!.click();  // the row's text opens it too
    expect(opened).toEqual(["pg_1", "pg_1"]);
    row = { page_id: null, state: "planned", progress: null };
    await m.runFlow!.readPath(); await flush();
    emit({ pageId: "pg_pending", done: 0, of: 4, ended: false, ok: false });
    expect((go() as HTMLButtonElement).disabled).toBe(true);
    await m.view.onClose();
    await m.view.onOpen(); await flush();
    expect((go() as HTMLButtonElement).disabled).toBe(false);
  });

  it("openTopic(topic): a different topic starts a fresh flow; the same topic or none keeps the running one", async () => {
    const m = make({ run: true });
    await m.view.onOpen();
    await toPath(m);                                           // the goal the chat drives is shown
    m.view.openTopic("");
    expect(m.heads()).toContain("学习路径");
    m.view.openTopic("傅里叶变换");                               // a new topic: a new goal's form
    expect(m.cards().length).toBe(1);
    expect(m.panel().querySelector<HTMLInputElement>("input[type=text]")!.value).toBe("傅里叶变换");
    // open before onOpen: remembered.
    const n = make();
    n.view.openTopic("贝叶斯定理");
    await n.view.onOpen();
    expect(n.panel().querySelector<HTMLInputElement>("input[type=text]")!.value).toBe("贝叶斯定理");
  });

  it("新建目标: the goal card's button and newGoal() give the empty form, not the running goal; a form he started stays", async () => {
    const m = make({ run: true });
    await m.view.onOpen();
    await toPath(m);
    expect(m.heads()).toContain("学习路径");
    m.button("新建目标").click();
    expect(m.cards().length).toBe(1);
    const box = m.panel().querySelector<HTMLInputElement>("input[type=text]")!;
    box.value = "傅里叶"; box.dispatchEvent(new Event("input"));
    m.view.newGoal();
    expect(m.panel().querySelector<HTMLInputElement>("input[type=text]")!.value).toBe("傅里叶");
    // before the tab has drawn: remembered, and the running goal does not take its place
    const n = make({ run: true });
    n.view.newGoal();
    await n.view.onOpen();
    expect(n.heads()).not.toContain("学习路径");
  });

  it("重命名 on a page row: the box, 保存 → the server's title, the note told, 撤销 sends the old title", async () => {
    const fake = new FakeGoalApi({ roundMs: 0 });
    const edit = vi.spyOn(fake, "editPath");
    const m = make({ api: fake, run: true });
    await m.view.onOpen();
    await toPath(m);
    m.button("开始学习").click(); await flush();
    const first = (): HTMLElement => m.panel().querySelector<HTMLElement>("ol.ew-pg > li")!;
    const old = m.runFlow!.state.entered!.rows[0]!.title, key = m.runFlow!.state.entered!.rows[0]!.key;
    first().querySelector<HTMLElement>(".ew-more")!.click();
    [...first().querySelectorAll<HTMLElement>(".ew-menu button")].find((b) => b.textContent === "重命名")!.click();
    const inp = first().querySelector<HTMLInputElement>(".ew-edit input")!;
    expect(inp.value).toBe(old);
    inp.value = "新标题";
    [...first().querySelectorAll<HTMLElement>("button")].find((b) => b.textContent === "保存")!.click();
    await flush();
    expect(edit).toHaveBeenLastCalledWith(expect.anything(), { op: "retitle", page_key: key, title: "新标题" });
    expect(m.runFlow!.state.entered!.rows[0]!.title).toBe("新标题");
    const notice = [...document.querySelectorAll(".ew-notices .ew-notice")].pop()!;
    expect(notice.textContent).toContain("改了标题：「新标题」");
    notice.querySelector<HTMLElement>("button")!.click();
    await flush();
    expect(edit).toHaveBeenLastCalledWith(expect.anything(), { op: "retitle", page_key: key, title: old });
  });

  it("fail closed: 开始 with no assistant stays on the form with 助手没连上", async () => {
    const m = make();
    await m.view.onOpen();
    m.button("开始").click(); await flush();
    expect(m.cards().length).toBe(1);
    expect(m.cards()[0]!.className).toBe("ew-card");
    expect([...document.querySelectorAll(".ew-notices .ew-notice span")].map((x) => x.textContent)).toEqual([LEARN_COPY.noAssistant]);
  });

  it("fail closed: 进入 answered 409 → the plain notice, the 学习路径 buttons stay usable", async () => {
    const api = new FakeGoalApi({ roundMs: 0 });
    const m = make({ api, run: true });
    await m.view.onOpen();
    await toPath(m);
    vi.spyOn(api, "enterRegion").mockRejectedValueOnce(new ApiError(409, "x"));
    m.button("开始学习").click(); await flush();
    expect([...document.querySelectorAll(".ew-notices .ew-notice span")].map((x) => x.textContent)).toEqual(["这一块已经排好了页面。"]);
    expect(m.heads().slice(-1)).toEqual(["学习路径"]);
    expect(m.button("开始学习").disabled).toBe(false);
    m.button("开始学习").click(); await flush();
    expect(m.panel().querySelector<HTMLElement>("[data-entered]")!.dataset.entered).toBe("ev");
  });

});

describe("Obsidian's View lifecycle", () => {
  it("LearnView does not override View.open (Obsidian calls open(containerEl) to load the view)", () => {
    expect(Object.prototype.hasOwnProperty.call(LearnView.prototype, "open")).toBe(false);
  });
});

describe("学点新的 uses the chosen topic (user review)", () => {
  it("switches a planned path through parallel fresh reads and two renders instead of three serial reads and renders", async () => {
    const api = new NestedGoalApi();
    await api.enterRegion(NESTED_MAP, "A"); await api.enterRegion(NESTED_MAP, "A2");
    const run = new GoalRun({ api, writeFirstPage: () => Promise.resolve(), changed: () => {} }, "nav", { draftId: null, mapId: NESTED_MAP }, false);
    await run.start(); await run.flow.enter("A");
    const m = make({ api, start: () => null }); await m.view.onOpen(); m.view.showRun(run);
    vi.useFakeTimers();
    const requests: { name: string; start: number; end?: number }[] = [];
    const delayed = <T,>(name: string, read: () => Promise<T>): Promise<T> => {
      const request = { name, start: Date.now() } as (typeof requests)[number]; requests.push(request);
      return new Promise<T>((resolve, reject) => { setTimeout(() => { request.end = Date.now(); void read().then(resolve, reject); }, 40); });
    };
    const map = api.getMap.bind(api), path = api.getPath.bind(api);
    vi.spyOn(api, "getMap").mockImplementation((id) => delayed("map", () => map(id)));
    vi.spyOn(api, "getPath").mockImplementation((id) => delayed("path", () => path(id).then((out) => ({ ...out,
      pages: out.pages.map((p) => ({ ...p, page_id: "existing-page", state: "written" as const })),
    }))));
    Object.assign(api, { progress: () => delayed("progress", () => Promise.resolve({ goals: [], today: {} } as unknown as ProgressOut)) });
    const render = vi.spyOn(m.view, "render");
    const start = Date.now();
    m.panel().querySelector<HTMLButtonElement>('li[data-part="A2"] .ew-go')!.click();
    await vi.advanceTimersByTimeAsync(200);
    const elapsed = Math.max(...requests.map((r) => r.end!)) - start;
    expect(run.flow.state.entered?.region).toBe("A2");
    expect(requests.map((r) => r.name)).toEqual(["map", "progress", "path"]);
    expect(requests[0]!.start).toBe(requests[1]!.start);
    expect(elapsed).toBe(80);
    expect(render.mock.calls.length).toBe(2);
    await m.view.onClose(); run.dispose();
  });

  it("restores the workspace's saved map into the same path view after closing and restarting", async () => {
    const api = new FakeGoalApi({ roundMs: 0 });
    const source = make({ api, run: true });
    await opened(source, "我要学黑壳虾");
    const mapId = source.runFlow!.state.map!.map_id;
    const openGoal = vi.fn(() => Promise.resolve({ actionId: "saved-goal", flow: source.runFlow!, enterBlock: () => Promise.resolve() }));
    const start = vi.fn(() => null);
    const host: LearnHost = { api: () => api, onCssChange: () => () => {}, assistant: {
      start, onGoalRow: () => () => {}, run: () => null, openGoal,
    } };
    const view = new LearnView({} as WorkspaceLeaf, host);
    await view.setState({ mapId }, {} as import("obsidian").ViewStateResult);
    await view.onOpen();
    expect(openGoal).toHaveBeenCalledWith(mapId);
    expect(view.state).toBe(source.runFlow);
    expect(view.getState()).toEqual({ mapId });
    expect(view.contentEl.textContent).toContain("学习路径");
    const saved = view.getState(); await view.onClose();
    const reopened = new LearnView({} as WorkspaceLeaf, host);
    await reopened.setState(saved, {} as import("obsidian").ViewStateResult);
    await reopened.onOpen();
    expect(openGoal).toHaveBeenCalledTimes(2);
    expect(reopened.getState()).toEqual(saved);
    expect(start).not.toHaveBeenCalled();
    await reopened.onClose(); source.runFlow!.dispose();
  });

  it("a topic given before the tab has drawn (openLearn on a new tab): the input holds it, never the fake's 我要学机器学习", async () => {
    const m = make();
    m.view.openTopic("奇异值分解");
    await m.view.onOpen();
    expect(m.panel().querySelector<HTMLInputElement>("input[type=text]")!.value).toBe("奇异值分解");
    // Another topic later: a fresh flow with that topic.
    m.view.openTopic("傅里叶变换");
    expect(m.panel().querySelector<HTMLInputElement>("input[type=text]")!.value).toBe("傅里叶变换");
  });

});

describe("学点新的's cloud: the existing map (知识图谱's graph) and the goal map on the 知识图谱 renderer (Sigma.js)", () => {
  /** GET /wiki/graph as the 知识图谱 page reads it (the demo graph); loads() counts the reads. */
  const demoGraph = (fail = false): GraphSource & { loads: () => number } => {
    let n = 0;
    const src = { demo: true, isOffline: () => false, load: () => { n++; return fail ? Promise.reject(new Error("down")) : Promise.resolve(new GraphFixture().graph()); } };
    return Object.assign(src as unknown as GraphSource, { loads: () => n });
  };
  const sigma = (): FakeSigma => FakeSigma.last;
  /** Zoomed in to the block level, where the dots are. */
  const atBlocks = async (): Promise<void> => { sigma().camera.ratio = 0.4; sigma().graphToViewport = (p: { x: number; y: number }) => ({ x: p.x * 3000, y: p.y * 3000 }); sigma().emit("afterRender"); await new Promise((r) => setTimeout(r, 300)); };
  const points = (): string[] => sigma().graph.nodes();
  /** The top titles over the canvas (one per top group). */
  const titles = (): string[] => [...sigma().container.querySelectorAll(".ew-gtitle")].map((e) => e.textContent);
  const title = (id: string): HTMLElement => sigma().container.querySelector<HTMLElement>(`.ew-gtitle[data-group="${id}"]`)!;
  const empty = (m: { view: LearnView }): HTMLElement => m.view.contentEl.querySelector<HTMLElement>(".ew-cloud-empty")!;
  /** The camera sits in the middle of these points' box. */
  const framedOn = (ids: string[]): void => {
    const ps = ids.map((id) => sigma().getNodeDisplayData(id)!), xs = ps.map((p) => p.x as number), ys = ps.map((p) => p.y as number);
    expect(sigma().camera.x).toBeCloseTo((Math.min(...xs) + Math.max(...xs)) / 2, 6);
    expect(sigma().camera.y).toBeCloseTo((Math.min(...ys) + Math.max(...ys)) / 2, 6);
  };

  it("default: the whole existing map in its own groups (read when the tab opens), no catch-all group, no empty line", async () => {
    const graph = demoGraph();
    const m = make({ graph });
    await m.view.onOpen();
    expect(graph.loads()).toBe(1);
    await flush();
    expect(points().sort()).toEqual([...m.view.existing!.order].sort());
    expect(titles()).toEqual(expect.arrayContaining(m.view.existing!.l1.map((g) => g.name)));
    expect(titles()).not.toContain("其他");
    expect(empty(m).hidden).toBe(true);
    expect(sigma().camera.ratio).toBe(1);
  });

  it("waits for canonical places before drawing new goal blocks, then draws them at those places", async () => {
    let resolve!: (places: GraphPlacesOut) => void;
    const graphPlaces = vi.fn((_blocks: { id: string; name: string; scope: string }[]) => new Promise<GraphPlacesOut>((done) => { resolve = done; }));
    const api = Object.assign(new FakeGoalApi({ roundMs: 0 }), { graphPlaces });
    const m = make({ api, graph: demoGraph(), run: true });
    await m.view.onOpen(); await flush();
    const before = points().sort();
    await opened(m);
    expect(graphPlaces).toHaveBeenCalledTimes(1);
    expect(points().sort()).toEqual(before);
    expect(m.view.cloudScene!.goalDots.size).toBe(0);
    const blocks = graphPlaces.mock.calls[0]![0];
    const places = Object.fromEntries(blocks.map((b, i) => [b.id, [i % 2, Math.floor(i / 2)] as [number, number]]));
    resolve({ places, links: [] }); await flush();
    expect(m.view.cloudScene!.goalDots.size).toBeGreaterThan(0);
    for (const [id, at] of Object.entries(places)) {
      expect(m.view.cloudScene!.scene.l1.find((g) => g.id === id)?.at).toEqual(at);
    }
    expect(graphPlaces).toHaveBeenCalledTimes(1);
    await m.view.onClose();
  });

  it("ignores an older goal's placement response while the new goal is waiting", async () => {
    const pending: { blocks: { id: string }[]; resolve: (p: GraphPlacesOut) => void }[] = [];
    const api = Object.assign(new FakeGoalApi({ roundMs: 0 }), {
      graphPlaces: (blocks: { id: string }[]) => new Promise<GraphPlacesOut>((resolve) => { pending.push({ blocks, resolve }); }),
    });
    const m = make({ api, graph: demoGraph(), run: true });
    await m.view.onOpen(); await flush();
    await opened(m, "我要学机器学习");
    await opened(m, "我要学黑壳虾");
    expect(pending).toHaveLength(2);
    pending[0]!.resolve({ places: Object.fromEntries(pending[0]!.blocks.map((b) => [b.id, [99, 99] as [number, number]])), links: [] });
    await flush();
    expect(m.view.cloudScene!.goalDots.size).toBe(0);
    pending[1]!.resolve({ places: Object.fromEntries(pending[1]!.blocks.map((b, i) => [b.id, [i % 2, Math.floor(i / 2)] as [number, number]])), links: [] });
    await flush();
    expect(m.view.cloudScene!.scene.l2.find((g) => g.id === `~goal~${m.runFlow!.state.map!.map_id}`)?.name).toBe("黑壳虾");
    expect(m.view.cloudScene!.goalDots.size).toBeGreaterThan(0);
    expect(m.view.cloudScene!.scene.l1.every((g) => g.at?.[0] !== 99)).toBe(true);
    await m.view.onClose();
  });

  it("a failed canonical layout keeps the existing graph and offers a real retry", async () => {
    const graphPlaces = vi.fn<(blocks: { id: string; name: string; scope: string }[]) => Promise<GraphPlacesOut>>()
      .mockRejectedValueOnce(new Error("layout unavailable"))
      .mockImplementation((blocks) => Promise.resolve({ places: Object.fromEntries(blocks.map((b, i) => [b.id, [i % 2, Math.floor(i / 2)]])), links: [] }));
    const m = make({ api: Object.assign(new FakeGoalApi({ roundMs: 0 }), { graphPlaces }), graph: demoGraph(), run: true });
    await m.view.onOpen(); await flush();
    const before = points().sort();
    await opened(m); await flush();
    expect(points().sort()).toEqual(before);
    const notice = noticeHost().lastElementChild!;
    expect(notice.textContent).toContain(LEARN_COPY.graphPlacesFailed);
    notice.querySelector<HTMLButtonElement>("button")!.click(); await flush();
    expect(graphPlaces).toHaveBeenCalledTimes(2);
    expect(m.view.cloudScene!.goalDots.size).toBeGreaterThan(0);
    await m.view.onClose();
  });

  it("no existing map — no graph, a failed read, or the wiki not set up: only 「加入你的第一个知识点，让知识自然生长」", async () => {
    for (const o of [{}, { graph: demoGraph(true) }, { graph: demoGraph(), configured: () => false }]) {
      const m = make(o);
      await m.view.onOpen();
      await flush();
      if (o.graph && "configured" in o) expect(o.graph.loads()).toBe(0);
      expect(points()).toEqual([]);
      expect(empty(m).hidden).toBe(false);
      expect(empty(m).textContent).toBe("加入你的第一个知识点，让知识自然生长");
      await m.view.onClose();
    }
  });

  it("「我要学机器学习」 grows into the existing map: his other points stay in their groups; the goal stands under its title; the whole picture shows", async () => {
    const m = make({ graph: demoGraph(), run: true });
    await m.view.onOpen();
    await flush();
    const before = new Map(points().map((id) => [id, sigma().graph.getNodeAttribute(id, "group")]));
    await opened(m);
    expect(m.view.cloudScene!.scene.l2.find((g) => g.goal === true)?.name).not.toContain("分成");
    const goalPts = [...m.view.cloudScene!.goalOf.keys()].filter((id) => sigma().graph.hasNode(id));
    expect(goalPts.length).toBeGreaterThan(0);
    for (const [id, g] of before) expect(sigma().graph.getNodeAttribute(id, "group")).toBe(g);
    expect(m.view.cloudScene!.scene.l1.filter((g) => g.all.includes("pr"))).toHaveLength(1);   // a region of new points: its own group
    expect(sigma().camera.ratio).toBeLessThanOrEqual(1);                                        // the camera goes to the goal; the map stays
  });

  it("the goal overview keeps its goal title and shared wash; entering a region reveals block detail", async () => {
    const m = make({ graph: demoGraph(), run: true });
    await m.view.onOpen(); await flush();
    const frame = vi.spyOn(m.view.cloudHandle!, "frame");
    await opened(m);
    expect(frame).toHaveBeenLastCalledWith(expect.any(Array), "top");
    await toPath(m);
    m.button("开始学习").click(); await flush();
    expect(frame).toHaveBeenLastCalledWith(expect.any(Array), "block");
    await m.view.onClose();
  });

  it("进入 lights the region being learned (its points bigger) and moves nothing", async () => {
    reduced = true;
    const m = make({ graph: demoGraph(), run: true });
    await m.view.onOpen();
    await flush();
    await toPath(m);
    await atBlocks();
    const at = (): number[] => points().map((id) => sigma().graph.getNodeAttribute(id, "x") as number);
    const before = at();
    const size = (id: string): number => sigma().node(id).size as number;
    const was = new Map(points().map((id) => [id, size(id)]));
    const grown = (id: string): boolean => was.has(id) && size(id) > was.get(id)!;
    m.button("开始学习").click(); await flush();   // the last 开始学习 = 模型评估
    sigma().camera.ratio = 0.4;                     // sizes compared at one zoom
    const lit = points().filter(grown);
    expect(lit.length).toBeGreaterThan(0);
    expect(lit.every((id) => m.view.cloudScene!.goalOf.get(id) === "ev")).toBe(true);
    expect(at()).toEqual(before);
  });

  it("a click on a goal region's title enters it (path stage); on an existing group's it only frames it", async () => {
    reduced = true;
    const m = make({ graph: demoGraph(), run: true });
    await m.view.onOpen();
    await flush();
    await toPath(m);
    const goalGroup = [...m.view.cloudScene!.goalOf].find(([id, r]) => r === "ev" && title(id) !== null)![0];
    title(goalGroup).click();
    await flush();
    expect(m.view.state!.state.entered?.region).toBe("ev");
    const other = m.view.cloudScene!.scene.l1.find((g) => !m.view.cloudScene!.goalOf.has(g.id))!;
    sigma().camera.setState({ x: 0.5, y: 0.5, ratio: 1 });
    title(other.id).click();
    expect(m.view.state!.state.entered?.region).toBe("ev");
    framedOn(other.all);
  });

  it("hover tips: an existing group / point the 知识图谱 words and related points; a goal region its name, word and scope", async () => {
    const m = make({ graph: demoGraph(), run: true });
    await m.view.onOpen();
    await flush();
    const tip = m.view.contentEl.querySelector<HTMLElement>(".ew-tip")!;
    const g = m.view.existing!.l1[0]!;
    title(g.id).dispatchEvent(new MouseEvent("mouseenter"));
    expect(tip.hidden).toBe(false);
    expect(tip.querySelector("b")!.textContent).toBe(g.name + "　");
    await atBlocks();
    sigma().emit("enterNode", { node: "mul" });
    expect(tip.textContent).toMatch(/^矩阵乘法　/);
    expect(tip.textContent).toContain("相关：");
    sigma().emit("leaveNode", { node: "mul" });
    expect(tip.hidden).toBe(true);
    await opened(m);
    const pr = [...m.view.cloudScene!.goalOf].find(([id, r]) => r === "pr" && title(id) !== null)![0];
    title(pr).dispatchEvent(new MouseEvent("mouseenter"));
    expect(tip.querySelector("b")!.textContent).toBe("概率论　");
    expect(tip.lastElementChild!.textContent).toBe("条件概率、贝叶斯公式、期望和方差、正态分布、最大似然");
  });

  it("a different topic goes back to the whole existing map", async () => {
    const m = make({ graph: demoGraph(), run: true });
    await m.view.onOpen();
    await flush();
    await opened(m);
    m.view.openTopic("贝叶斯定理");
    expect(m.view.cloudScene!.goalOf.size).toBe(0);
    expect(sigma().camera.getState()).toMatchObject({ x: 0.5, y: 0.5, ratio: 1 });
  });
});


/** LearnHost.sources on scripted answers: upload resolves (or rejects) when told; follow hands its callback out. */
function fakeSources(o: { fail?: Error; editFail?: Error } = {}) {
  const uploads: File[] = [];
  const edits: SectionEditIn[][] = [];
  let resolveUpload: ((s: SourceOut) => void) | null = null;
  let update: ((u: SourceUpdate) => void) | null = null;
  let stopped = 0;
  const out = (id: string, status: SourceOut["status"]): SourceOut => ({ id, title: "讲义", kind: "pdf", pages: 24, status, error: null, error_code: null, can_retry: false,
    unreadable_pages: [], progress: null, note: null, origin: null, created_at: "2026-10-21T00:00:00Z", deleted: false, uses: [] });
  const sec = (id: string, title: string, depth: number, page: number) => ({ id, parent_id: null, title, depth, page_from: page, page_to: page,
    tokens: 1, read_by: "text" as const, summary: null });
  let sections = [sec("c1", "第 1 章 绪论", 0, 1), sec("c2", "1.1 什么是机器学习", 1, 3), sec("c3", "第 2 章 线性代数回顾", 2, 12)];
  const detail = (): SourceDetail => ({ ...out("src1", "ready"), sections: sections.map((x) => ({ ...x })), page_offset: null, glossary: [], glossary_coverage: null, exam: null });
  const port: LearnSources = {
    upload: (f) => { uploads.push(f); return o.fail ? Promise.reject(o.fail) : new Promise((r) => { resolveUpload = r; }); },
    follow: (_id, on) => { update = on; return () => { stopped++; update = null; }; },
    detail: () => Promise.resolve(detail()),
    editSections: (_id, e) => {
      edits.push(e);
      if (o.editFail) return Promise.reject(o.editFail);
      sections = sections.map((x) => {
        const d = e.find((y) => y.id === x.id);
        return d ? { ...x, ...(d.title !== undefined && d.title !== null ? { title: d.title } : {}),
          ...(d.depth !== undefined && d.depth !== null ? { depth: d.depth } : {}) } : x;
      });
      return Promise.resolve(detail());
    },
  };
  return {
    port, uploads, edits, stopped: () => stopped,
    uploaded: (status: SourceOut["status"] = "reading") => { resolveUpload?.(out("src1", status)); },
    push: (u: SourceUpdate) => { update?.(u); },
  };
}
const pdf = (name = "讲义.pdf"): File => new File([new Uint8Array([37, 80, 68, 70])], name, { type: "application/pdf" });
/** 上传附件 picks `f` (the hidden file input's change). */
function pick(m: ReturnType<typeof make>, f: File): void {
  const input = m.panel().querySelector<HTMLInputElement>("input[type=file]")!;
  Object.defineProperty(input, "files", { value: [f], configurable: true });
  input.dispatchEvent(new Event("change"));
}
const notices = (): string[] => [...document.querySelectorAll(".ew-notices .ew-notice")].map((n) => n.textContent ?? "");

describe("学点新的 · 上传附件 (full-demo goalCard, user rulings 2026-10-21)", () => {
  it("📎 上传附件 → the material card: name, 正在解析, ✕; 开始 disabled until the material is read; then a 目录 link", async () => {
    const src = fakeSources();
    const m = make({ sources: src.port });
    await m.view.onOpen();
    const at = m.button("📎 上传附件");
    expect(at.className).toBe("ew-attach");
    pick(m, pdf()); await flush();
    expect(src.uploads.map((f) => f.name)).toEqual(["讲义.pdf"]);
    const mat = (): HTMLElement => m.panel().querySelector<HTMLElement>(".ew-mat")!;
    expect([...mat().querySelectorAll(".ew-mt > *")].map((x) => [x.className, x.textContent])).toEqual([["ew-mn", "讲义.pdf"], ["ew-ms", "正在解析"], ["ew-x", "✕"]]);
    expect(m.panel().querySelector(".ew-attach")).toBeNull();
    expect(m.button("开始").disabled).toBe(true);
    src.uploaded(); await flush();
    src.push({ status: "reading", progress: { stage: "toc", done: 1, total: 3, message: "x" }, error: null }); await flush();
    expect(mat().querySelector(".ew-ms")!.textContent).toBe("正在解析");
    expect(mat().textContent).not.toMatch(/1\/3|页/);
    expect(m.button("开始").disabled).toBe(true);
    src.push({ status: "ready", progress: null, error: null }); await flush();
    expect(mat().querySelector(".ew-ms")!.textContent).toBe("");
    expect(m.button("目录").className).toBe("ew-btn ew-link");
    expect(m.button("开始").disabled).toBe(false);
    // ✕, 目录, 开始: one 开始, no 按这份学 / 帮我读懂它
    expect([...m.panel().querySelectorAll("button")].map((b) => b.textContent)).toEqual(["✕", "目录", "开始"]);
  });

  it("typing while the material is read keeps the goal field (only the material card and 开始 redraw)", async () => {
    const src = fakeSources();
    const m = make({ sources: src.port });
    await m.view.onOpen();
    pick(m, pdf()); await flush(); src.uploaded(); await flush();
    const gi = m.panel().querySelector<HTMLInputElement>("input[type=text]")!;
    gi.value = "帮我读懂这篇"; gi.dispatchEvent(new Event("input"));
    src.push({ status: "ready", progress: null, error: null }); await flush();
    expect(m.panel().querySelector("input[type=text]")).toBe(gi);
  });

  it("开始 with a goal and a read material sends the goal to the chat with the material attached", async () => {
    const api = new FakeGoalApi({ roundMs: 0 });
    const src = fakeSources();
    const starts: unknown[][] = [];
    const m = make({ api, sources: src.port, start: (...a) => { starts.push(a); return { key: "c1", done: Promise.resolve(true) }; } });
    await m.view.onOpen();
    const gi = m.panel().querySelector<HTMLInputElement>("input[type=text]")!;
    gi.value = "帮我读懂这篇"; gi.dispatchEvent(new Event("input"));
    pick(m, pdf()); await flush(); src.uploaded("ready"); await flush();
    expect(m.button("开始").disabled).toBe(false);
    m.button("开始").click(); await flush();
    expect(starts.map((x) => [x[0], x[1], (x[2] as { sourceId: string }[]).map((y) => y.sourceId)])).toEqual([["帮我读懂这篇", { goal: "帮我读懂这篇", background: "" }, ["src1"]]]);
    expect(m.cards()[0]!.querySelector(".ew-faint")!.textContent).toBe("📎 讲义.pdf");
  });

  it("only a material, no goal: the thinking line names the material, never empty quotes (final review #7)", async () => {
    const api = new FakeGoalApi({ roundMs: 0 });
    const src = fakeSources();
    const m = make({ api, sources: src.port, start: () => ({ key: "c1", done: new Promise<boolean>(() => {}) }) });
    await m.view.onOpen();
    const gi = m.panel().querySelector<HTMLInputElement>("input[type=text]")!;
    gi.value = "  "; gi.dispatchEvent(new Event("input"));
    pick(m, pdf()); await flush(); src.uploaded("ready"); await flush();
    m.button("开始").click(); await flush();
    expect(m.panel().querySelector(".ew-thinking")!.textContent).toBe("正在把「按《讲义.pdf》学」分成几大块…");
  });

  it("a goal without a map: the path's rows show their § labels and the coverage line sits under the path (final review #2)", async () => {
    const api = new FakeGoalApi({ roundMs: 0 });
    const flat = { draft_id: null, page_id: null, path_id: "P1", map_id: null, title: "按《讲义》学", concepts: [],
      notes: { from_background: 0, reused_concepts: 0, dropped_questions: 0 }, round: 0, round_title: "", round_why: "", map: null,
      decisions: [], results: [], questions: [] };
    vi.spyOn(api, "getDraft").mockResolvedValue(flat);
    vi.spyOn(api, "getPath").mockResolvedValue({ path_id: "P1", goal: "按《讲义》学", source: "llm", next_index: 1, prefetch: null,
      sources: [{ id: "src1", title: "讲义", deleted: false, uncovered: ["前言", "附录 B"] }],
      pages: [
        { index: 1, aim: "会加", status: "not_started", state: "planned" as const, progress: null, page_id: null, trashed: false, skipped: false, key: "", title: "向量加法", abstract: "", sections: ["§1.1"], exam: 0,
          concepts: [{ concept_id: "c1", name: "向量加法", target: true }] },
        { index: 2, aim: "会读", status: "not_started", state: "planned" as const, progress: null, page_id: null, trashed: false, skipped: false, key: "", title: "《讲义》§摘要", abstract: "", sections: ["§摘要"], exam: 5,
          concepts: [{ concept_id: "c2", name: "《讲义》§摘要", target: true }] }] });
    const src = fakeSources();
    const m = make({ api, sources: src.port, run: true });
    await m.view.onOpen();
    await m.runFlow!.openGoal({ draftId: "d1", mapId: null }); await flush();
    const card = m.cards().pop()!;
    const rows = [...card.querySelectorAll("ol.ew-elist.ew-pg li")];
    expect(rows.map((li) => li.querySelector(".ew-sec")?.textContent ?? null)).toEqual(["§1.1", null]);   // 读懂 rows say it once
    expect([rows[0]!.querySelector(".ew-t")!.textContent, rows[0]!.querySelector(".ew-scope")!.textContent]).toEqual(["向量加法§1.1", "学完能：会加"]);
    // 真题: a 常考 row (exam > 0) gets the tag and its ?
    expect(rows.map((li) => li.querySelector(".ew-exam")?.textContent ?? null)).toEqual([null, "常考"]);
    expect(rows[1]!.querySelector(".ew-exam + .ew-qtip .ew-tipbox")!.textContent).toBe("有 5 道往年题考到这里");
    expect(card.querySelector("[data-cover]")!.textContent).toBe("资料里这几节没讲：前言、附录 B");
  });

  it("✕ removes the material: back to the plain card with 📎 上传附件; the progress stream stops", async () => {
    const src = fakeSources();
    const m = make({ sources: src.port });
    await m.view.onOpen();
    pick(m, pdf()); await flush(); src.uploaded(); await flush();
    m.button("✕").click(); await flush();
    expect(m.panel().querySelector(".ew-mat")).toBeNull();
    expect(m.button("📎 上传附件")).toBeTruthy();
    expect(m.button("开始").disabled).toBe(false);
    expect(src.stopped()).toBe(1);
  });

  it("✕ while the upload is still running: the answer that comes later is dropped", async () => {
    const src = fakeSources();
    const m = make({ sources: src.port });
    await m.view.onOpen();
    pick(m, pdf()); await flush();
    m.button("✕").click(); await flush();
    src.uploaded(); await flush();
    expect(m.panel().querySelector(".ew-mat")).toBeNull();
    src.push({ status: "ready", progress: null, error: null }); await flush();
    expect(m.panel().querySelector(".ew-mat")).toBeNull();
  });

  it("a file dropped on the card is the same as 📎 上传附件 (dashed while dragged over)", async () => {
    const src = fakeSources();
    const m = make({ sources: src.port });
    await m.view.onOpen();
    const card = m.cards()[0]!;
    const drag = (type: string, files: File[] = []): void => {
      const e = new Event(type, { bubbles: true, cancelable: true });
      Object.defineProperty(e, "dataTransfer", { value: { files } });
      card.dispatchEvent(e);
    };
    drag("dragover");
    expect(card.classList.contains("ew-dragover")).toBe(true);
    drag("drop", [pdf("notes.pdf")]); await flush();
    expect(src.uploads.map((f) => f.name)).toEqual(["notes.pdf"]);
    expect(m.panel().querySelector(".ew-mn")!.textContent).toBe("notes.pdf");
    expect(m.cards()[0]!.classList.contains("ew-dragover")).toBe(false);
  });

  it("fail closed: a refused upload says 「这个文件打不开」 and the card is plain again; a failed read too", async () => {
    const bad = fakeSources({ fail: new SourceRefused("不支持这种文件") });
    const m = make({ sources: bad.port });
    await m.view.onOpen();
    pick(m, pdf()); await flush();
    expect(m.panel().querySelector(".ew-mat")).toBeNull();
    expect(notices()).toEqual(["这个文件打不开"]);
    noticeHost().replaceChildren();
    const src = fakeSources();
    const m2 = make({ sources: src.port });
    await m2.view.onOpen();
    pick(m2, pdf()); await flush(); src.uploaded(); await flush();
    src.push({ status: "failed", progress: null, error: "读不出来" }); await flush();
    expect(m2.panel().querySelector(".ew-mat")).toBeNull();
    expect(m2.button("开始").disabled).toBe(false);
    expect(notices()).toEqual(["这个文件打不开"]);
  });

  it("目录: the sections (title, p.N, ← →, indented by level); a click renames inline, ← → relevel; 收起 folds", async () => {
    const src = fakeSources();
    const m = make({ sources: src.port });
    await m.view.onOpen();
    pick(m, pdf()); await flush(); src.uploaded("ready"); await flush();
    expect(m.button("目录").getAttribute("aria-expanded")).toBe("false");
    m.button("目录").click(); await flush();
    const rows = (): HTMLElement[] => [...m.panel().querySelectorAll<HTMLElement>(".ew-toc li")];
    expect(rows().map((li) => [li.style.paddingLeft, li.querySelector(".ew-tn")?.textContent, li.querySelector(".ew-pg")?.textContent]))
      .toEqual([["0px", "第 1 章 绪论", "p.1"], ["16px", "1.1 什么是机器学习", "p.3"], ["32px", "第 2 章 线性代数回顾", "p.12"]]);
    const lv = (i: number): HTMLButtonElement[] => [...rows()[i]!.querySelectorAll<HTMLButtonElement>(".ew-lv")];
    expect(lv(0).map((b) => [b.textContent, b.disabled])).toEqual([["←", true], ["→", false]]);
    expect(lv(2).map((b) => [b.textContent, b.disabled])).toEqual([["←", false], ["→", true]]);
    expect(lv(0)[1]!.getAttribute("aria-label")).toBe("降一级：第 1 章 绪论");
    expect(m.button("收起").getAttribute("aria-expanded")).toBe("true");
    // → on 第 1 章: depth 1, sent to the server
    lv(0)[1]!.click(); await flush();
    expect(src.edits).toEqual([[{ id: "c1", depth: 1 }]]);
    expect(rows()[0]!.style.paddingLeft).toBe("16px");
    // rename inline: click the title, type, Enter
    rows()[1]!.querySelector<HTMLButtonElement>(".ew-tn")!.click(); await flush();
    const inp = rows()[1]!.querySelector<HTMLInputElement>("input[type=text]")!;
    expect(inp.getAttribute("aria-label")).toBe("这一节的名字");
    inp.value = "  1.1 机器学习是什么 ";
    inp.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" })); await flush();
    expect(src.edits[1]).toEqual([{ id: "c2", title: "1.1 机器学习是什么" }]);
    expect(rows()[1]!.querySelector(".ew-tn")!.textContent).toBe("1.1 机器学习是什么");
    // Escape keeps the old name, nothing sent
    rows()[2]!.querySelector<HTMLButtonElement>(".ew-tn")!.click(); await flush();
    const inp2 = rows()[2]!.querySelector<HTMLInputElement>("input[type=text]")!;
    inp2.value = "别的"; inp2.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" })); await flush();
    expect(src.edits.length).toBe(2);
    expect(rows()[2]!.querySelector(".ew-tn")!.textContent).toBe("第 2 章 线性代数回顾");
    m.button("收起").click(); await flush();
    expect(m.panel().querySelector(".ew-toc")).toBeNull();
    expect(m.button("目录").getAttribute("aria-expanded")).toBe("false");
    expect(m.button("开始").disabled).toBe(false);
  });
});

describe("学点新的 · 上传附件: the right line for each failure (review fix 1)", () => {
  it("offline / busy: the plugin's own line for that state, never 「这个文件打不开」", async () => {
    const src = fakeSources({ fail: new SourceBlocked(WIKI_COPY.offline) });
    const m = make({ sources: src.port });
    await m.view.onOpen();
    pick(m, pdf()); await flush();
    expect(m.panel().querySelector(".ew-mat")).toBeNull();
    expect(notices()).toEqual([WIKI_COPY.offline]);
  });

  it("a lost progress stream says the plugin's stream-lost line", async () => {
    const src = fakeSources();
    const m = make({ sources: src.port });
    await m.view.onOpen();
    pick(m, pdf()); await flush(); src.uploaded(); await flush();
    src.push({ status: "failed", progress: null, error: SOURCES_COPY.streamLost }); await flush();
    expect(notices()).toEqual([SOURCES_COPY.streamLost]);
  });

  it("目录 edits never show an internal message: an unknown error gets the shared failure line", async () => {
    const src = fakeSources({ editFail: new Error("disposed") });
    const m = make({ sources: src.port });
    await m.view.onOpen();
    pick(m, pdf()); await flush(); src.uploaded("ready"); await flush();
    m.button("目录").click(); await flush();
    m.panel().querySelectorAll<HTMLButtonElement>(".ew-toc li .ew-lv")[1]!.click(); await flush();
    expect(notices()).toEqual([WIKI_COPY.failed]);
    expect(m.panel().querySelector<HTMLElement>(".ew-toc li")!.style.paddingLeft).toBe("0px"); // undone
  });

  it("开始 sits in the demo's #goal-foot (flex column, gap 8px)", async () => {
    const m = make({ sources: fakeSources().port });
    await m.view.onOpen();
    const foot = m.button("开始").parentElement!;
    expect(foot.id).toBe("ew-goal-foot");
    expect([foot.style.display, foot.style.flexDirection, foot.style.gap]).toEqual(["flex", "column", "8px"]);
  });
});

describe("学点新的 · chat-first blocks (big-goal-demo.html start → blocks, pathCard, enterCard)", () => {
  type Row = import("../../assistant/goal-chat").GoalChatRow;
  function chatHost(api: GoalApi = new FakeGoalApi({ roundMs: 0 })) {
    let on: ((r: Row) => void) | null = null;
    const runs: { g1?: import("./learn-view").LearnRun } = {};
    const sent: string[] = [], learn: { goal: string; background: string }[] = [];
    const host: LearnHost = {
      api: () => api, onCssChange: () => () => {},
      assistant: { start: (t, l) => { sent.push(t); learn.push(l); return { key: "c1", done: Promise.resolve(true) }; }, onGoalRow: (fn) => { on = fn; return () => { on = null; }; },
        run: (id) => (id === "g1" ? runs.g1 ?? null : null) },
    };
    const view = new LearnView({} as WorkspaceLeaf, host);
    document.body.append(view.contentEl);
    const panel = (): HTMLElement => view.contentEl.querySelector<HTMLElement>(".ew-panel")!;
    const cards = (): HTMLElement[] => [...panel().children] as HTMLElement[];
    const button = (text: string, root: ParentNode = panel()): HTMLButtonElement => [...root.querySelectorAll<HTMLButtonElement>("button")].filter((b) => b.textContent === text)[0]!;
    return { view, host, sent, learn, panel, cards, button, row: (r: Row) => on?.(r), runs, api };
  }
  const row = (o: Partial<Row>): Row => ({ key: "c1", actionId: "g1", phase: "wait", replaced: false, title: "", result: null,
    blocks: [{ key: "id", name: "认识黑壳虾", scope: "它是什么虾" }, { key: "wq", name: "水质", scope: "水温" }], ...o });

  it("uses the intake title 梯度下降 for both the proposal and the confirmed map while retaining the learner's words", async () => {
    const goal = "我想学会梯度下降，从导数基础开始，最后能手算一次参数更新";
    const title = "梯度下降";
    const api = new FakeGoalApi({ roundMs: 0 });
    const getDraft = api.getDraft.bind(api);
    vi.spyOn(api, "getDraft").mockImplementation(async (id) => {
      const out = await getDraft(id);
      return { ...out, title, map: out.map === null ? null : { ...out.map, title } };
    });
    const m = chatHost(api);
    const blocks = [{ key: "der", name: "导数基础", scope: "导数、变化率" }, { key: "gd", name: "手算一次参数更新", scope: "梯度、学习率" }];
    m.host.assistant!.pending = () => Promise.resolve({ row: row({ title, blocks }), goal });
    await m.view.onOpen(); await flush();
    const shownTitle = (): string | undefined => m.view.cloudScene!.scene.l2.find((g) => g.goal)?.name;
    expect(shownTitle()).toBe(title);
    expect(m.view.state!.state.goal).toBe(goal);
    const result = registerBlockGoal(goal, blocks.map((b) => ({ ...b, id: b.key, from: [] })));
    const flow = new LearnFlow(api, { fail: () => {} }, goal);
    await flow.openGoal(result); await flush();
    m.runs.g1 = { actionId: "g1", flow, enterBlock: (r) => flow.enter(r ?? "der") };
    m.row(row({ title, blocks, phase: "live", result })); await flush();
    expect(m.view.state).toBe(flow);
    expect(shownTitle()).toBe(title);
    expect(flow.state.map).toMatchObject({ goal, title });
    await m.view.onClose();
  });

  it("reload projects the saved proposal and follows edits and cancellation without sending another goal", async () => {
    const m = chatHost();
    const show = vi.fn();
    m.host.assistant!.pending = () => Promise.resolve({ row: row({ title: "黑壳虾分成两块" }), goal: "我要学黑壳虾" });
    m.host.assistant!.showGoal = show;
    const resume = vi.fn(() => Promise.resolve(null));
    m.host.assistant!.resume = resume;
    await m.view.onOpen(); await flush();
    expect(m.view.state!.state).toMatchObject({ stage: "blocks", chat: "c1", actionId: "g1" });
    expect(m.panel().textContent).toContain("认识黑壳虾");
    expect(m.view.state!.state.goal).toBe("我要学黑壳虾");
    expect(show).toHaveBeenCalledWith("g1");
    expect(m.sent).toEqual([]);
    expect(resume).not.toHaveBeenCalled();
    m.row(row({ blocks: [{ key: "wq", name: "改过的水质", scope: "水温" }] }));
    expect(m.panel().textContent).toContain("改过的水质");
    m.row(row({ phase: "rejected" }));
    expect(m.view.state!.state.stage).toBe("goal");
    expect(m.panel().querySelector<HTMLInputElement>("input[type=text]")!.value).toBe("我要学黑壳虾");
    await m.view.onClose();
  });

  it("a delayed saved proposal cannot replace a new goal the learner started meanwhile", async () => {
    const m = chatHost();
    let release!: (r: import("../../assistant/goal-chat").PendingGoal) => void;
    m.host.assistant!.pending = () => new Promise((resolve) => { release = resolve; });
    const show = vi.fn();
    m.host.assistant!.showGoal = show;
    await m.view.onOpen();
    m.view.openTopic("新的学习目标"); await flush();
    release({ row: row({}), goal: "旧的学习目标" }); await flush();
    expect(m.panel().querySelector<HTMLInputElement>("input[type=text]")!.value).toBe("新的学习目标");
    expect(show).not.toHaveBeenCalled();
    expect(m.sent).toEqual([]);
    await m.view.onClose();
  });

  it("a delayed saved proposal leaves a form the learner is typing intact", async () => {
    const m = chatHost();
    let release!: (r: import("../../assistant/goal-chat").PendingGoal) => void;
    m.host.assistant!.pending = () => new Promise((resolve) => { release = resolve; });
    await m.view.onOpen();
    const input = m.panel().querySelector<HTMLInputElement>("input[type=text]")!;
    input.value = "正在填写的新目标"; input.dispatchEvent(new Event("input"));
    release({ row: row({}), goal: "旧目标" }); await flush();
    expect(m.panel().querySelector<HTMLInputElement>("input[type=text]")!.value).toBe("正在填写的新目标");
    expect(m.view.state!.state.stage).toBe("goal");
    await m.view.onClose();
  });

  it("开始: the goal card folds, 正在把「黑壳虾」分成几大块…, then only the folded card while the blocks are talked over; 取消 → the form with the text", async () => {
    const m = chatHost();
    await m.view.onOpen();
    m.view.openTopic("我要学黑壳虾");
    const gi = m.panel().querySelector<HTMLInputElement>("input[type=text]")!;
    expect(m.panel().querySelector("textarea")!.value).toBe("学过高中生物，想知道黑壳虾是什么、怎么养。");   // the fake: the demo topic's background
    gi.value = "我要学黑壳虾"; gi.dispatchEvent(new Event("input"));
    m.button("开始").click();
    expect(m.sent).toEqual(["我要学黑壳虾\n背景：学过高中生物，想知道黑壳虾是什么、怎么养。"]);
    expect(m.learn).toEqual([{ goal: "我要学黑壳虾", background: "学过高中生物，想知道黑壳虾是什么、怎么养。" }]);   // intent learn_goal
    expect(m.cards().map((c) => c.className)).toEqual(["ew-card ew-done", "ew-thinking"]);
    expect(m.cards()[1]!.textContent).toBe("正在把「黑壳虾」分成几大块…");
    m.row(row({}));
    expect(m.cards().map((c) => c.className)).toEqual(["ew-card ew-done", "ew-card"]);           // the blocks: 学点新的's list
    expect(m.cards()[0]!.querySelector(".ew-goaltext")!.textContent).toBe("我要学黑壳虾");
    expect(m.view.state!.state.stage).toBe("blocks");
    m.row(row({ phase: "rejected" }));
    expect(m.panel().querySelector<HTMLInputElement>("input[type=text]")!.value).toBe("我要学黑壳虾");
  });

  it("the chat's draft is 学点新的's list: 还没问 rows, ⋯ 改名 / 删除, drag, 添加学习主题 … 添加, 确认 / 取消 — each the chat card's call", async () => {
    const m = chatHost();
    const calls: string[] = [];
    const host = (m.view as unknown as { host: LearnHost }).host;
    host.assistant!.draft = {
      move: (id, f, t) => calls.push(`move:${id}:${f}:${t}`), remove: (id, k) => calls.push(`remove:${id}:${k}`),
      add: (id, t) => calls.push(`add:${id}:${t}`), rename: (id, k, t) => calls.push(`rename:${id}:${k}:${t}`),
      confirm: (id) => { calls.push(`confirm:${id}`); return Promise.resolve(); }, cancel: (id) => { calls.push(`cancel:${id}`); return Promise.resolve(); },
    };
    await m.view.onOpen();
    m.view.openTopic("我要学黑壳虾");
    m.button("开始").click(); await flush();
    m.row(row({ blocks: [{ key: "id", name: "认识黑壳虾", scope: "它是什么虾", level: "基础" }, { key: "wq", name: "水质", scope: "水温", level: "" }] }));
    const card = m.cards()[1]!;
    expect(card.querySelector("h2")!.textContent).toBe("学习路径");
    const lis = [...card.querySelectorAll<HTMLLIElement>("ol.ew-elist > li")];
    expect(lis.map((li) => [li.querySelector(".ew-rname")!.firstChild!.textContent, li.querySelector(".ew-rname")!.textContent, li.querySelector(".ew-act")!.textContent]))
      .toEqual([["认识黑壳虾", "认识黑壳虾基础", ""], ["水质", "水质", ""]]);
    sortDrag(card.querySelector<HTMLElement>("ol.ew-elist")!, 0, 1); await flush();
    lis[1]!.querySelector<HTMLButtonElement>(".ew-more")!.click();
    [...m.panel().querySelectorAll<HTMLButtonElement>(".ew-menu button")].find((b) => b.textContent === "删除")!.click(); await flush();
    m.panel().querySelectorAll<HTMLButtonElement>(".ew-more")[0]!.click();
    [...m.panel().querySelectorAll<HTMLButtonElement>(".ew-menu button")].find((b) => b.textContent === "改名")!.click();
    const inp = m.panel().querySelector<HTMLInputElement>(".ew-edit input")!;
    inp.value = "认识它"; m.button("保存").click();
    const add = m.panel().querySelector(".ew-row.ew-add")!;
    const ai = add.querySelector<HTMLInputElement>("input")!;
    ai.value = "造景"; ai.dispatchEvent(new Event("input")); add.querySelector<HTMLButtonElement>("button")!.click(); await flush();
    m.button("确认").click(); m.button("取消").click(); await flush();
    expect(calls).toEqual(["move:g1:0:1", "remove:g1:1", "rename:g1:0:认识它", "add:g1:造景", "confirm:g1", "cancel:g1"]);
  });

  it("the chat's diagnosis colours the rows: a 摸底 tap at once, the block being asked 正在问 (its dot pulsing)", async () => {
    const m = make({ run: true });
    let diag: { asking: string | null; marks: Record<string, "know" | "partly" | "dont"> } = { asking: null, marks: { op: "dont" } };
    const host = (m.view as unknown as { host: LearnHost }).host;
    let tell = (): void => {};
    host.assistant = { ...(host.assistant ?? { start: () => null, onGoalRow: () => () => {}, run: () => null }), diag: () => diag, onDiag: (fn) => { tell = fn; return () => {}; } };
    await m.view.onOpen();
    await toPath(m);
    const li = (name: string): Element => [...m.cards().pop()!.querySelectorAll("ol.ew-elist > li")]
      .find((li) => li.querySelector(".ew-rname")!.firstChild!.textContent === name)!;
    expect((li("优化").querySelector(".ew-dot") as HTMLElement).style.background).toBe("var(--gap)");
    expect(li("优化").classList.contains("ew-cur")).toBe(false);
    diag = { asking: "op", marks: {} }; tell();
    expect(li("优化").querySelector(".ew-rname")!.textContent).toBe("优化");   // no word: the row is framed like the block being learned
    expect(li("优化").classList.contains("ew-cur")).toBe(true);
    expect(li("优化").querySelector(".ew-dot.ew-asking")).not.toBeNull();
    expect(m.view.cloudHandle).not.toBeNull();
  });

  it("确认 in the chat: the view shows the chat's run from then on (one flow): its 学习路径 and the block it enters, no rounds here", async () => {
    const m = chatHost();
    await m.view.onOpen();
    m.view.openTopic("我要学黑壳虾");
    m.button("开始").click(); await flush();
    m.row(row({}));
    const flow = new LearnFlow(m.api, { fail: () => {} }, "我要学黑壳虾");
    await flow.openGoal(registerBlockGoal("我要学黑壳虾", fakeBlocks("我要学黑壳虾"))); await flow.skip(); await flush();
    m.runs.g1 = { actionId: "g1", flow, enterBlock: (r) => flow.enter(r ?? "id") };
    m.row(row({ phase: "live", result: { draftId: "x", mapId: "y" } }));
    expect(m.view.state).toBe(flow);
    expect(m.cards().map((c) => c.querySelector("h2")?.textContent)).toEqual(["新的目标", "学习路径"]);
    expect(m.panel().querySelectorAll(".ew-q").length).toBe(0);
    await flow.enter("wq"); await flush();
    expect(m.panel().querySelector<HTMLElement>("[data-entered]")!.dataset.entered).toBe("wq");
    await m.view.onClose();
    expect(flow.state.entered?.region).toBe("wq");                    // the run's flow outlives the tab
  });

  it("reopened (closed, or after a reload): the goal the chat drives comes back as the server has it (U4)", async () => {
    const m = chatHost();
    const flow = new LearnFlow(m.api, { fail: () => {} }, "我要学黑壳虾");
    await flow.openGoal(registerBlockGoal("我要学黑壳虾", fakeBlocks("我要学黑壳虾"))); await flow.skip(); await flow.enter("wq"); await flush();
    m.runs.g1 = { actionId: "g1", flow, enterBlock: () => Promise.resolve() };
    const host = (m.view as unknown as { host: LearnHost }).host;
    host.assistant!.run = () => m.runs.g1!;
    await m.view.onOpen();
    expect(m.cards().map((c) => c.querySelector("h2")?.textContent)).toEqual(["新的目标", "学习路径"]);
    expect(m.panel().querySelector<HTMLElement>("[data-entered]")!.dataset.entered).toBe("wq");
  });

  it("学习路径 = ol.elist: dot, name + level (span.tg) + scope, 进入, ⋮⋮ / ⋯ (改名 / 删除), 加一块; 删除 → notice with 撤销; 改名 → 保存 / 取消", async () => {
    const api = new FakeGoalApi({ roundMs: 0 });
    const m = make({ api, run: true });
    await m.view.onOpen();
    await opened(m, "我要学黑壳虾");
    await m.runFlow!.skip(); await flush();
    const card = m.cards().pop()!;
    expect(card.querySelector("h2")!.textContent).toBe("学习路径");
    const lis = [...card.querySelectorAll<HTMLLIElement>("ol.ew-elist > li")];
    expect(lis.map((li) => [li.querySelector(".ew-rname")!.firstChild!.textContent, li.querySelector(".ew-rname .ew-tg")!.textContent])).toEqual(
      [["分类与鉴别", "基础"], ["水质与环境", "基础"], ["开缸与日常", "核心"], ["营养与摄食", "核心"], ["生长与繁殖", "进阶"], ["疾病与风险", "进阶"]]);
    expect([...lis[0]!.children].map((c) => c.className)).toEqual(["ew-dot", "ew-rowhit", "ew-act", "ew-ops"]);
    expect([lis[1]!.draggable, lis[1]!.tabIndex, lis[1]!.getAttribute("aria-label")]).toEqual([true, 0, "第 2 块：水质与环境，还没问，按住拖动调整顺序，或用 Alt + 上下键"]);
    expect([...lis[0]!.querySelector(".ew-ops")!.children].map((c) => c.textContent)).toEqual(["⋮⋮", "⋯"]);
    const more = lis[3]!.querySelector<HTMLButtonElement>(".ew-more")!;
    expect([more.getAttribute("aria-label"), more.getAttribute("aria-haspopup"), more.getAttribute("aria-expanded")]).toEqual(["「营养与摄食」的操作", "menu", "false"]);
    more.click();
    const menu = m.cards().pop()!.querySelectorAll("ol.ew-elist > li")[3]!.querySelector(".ew-menu")!;
    expect([...menu.querySelectorAll("button")].map((b) => b.textContent)).toEqual(["改名", "删除"]);
    (menu.querySelectorAll("button")[1] as HTMLButtonElement).click(); await flush();
    const names = (): string[] => [...m.cards().pop()!.querySelectorAll("ol.ew-elist > li .ew-rname")].map((x) => x.firstChild?.textContent ?? "");
    expect(names()).not.toContain("营养与摄食");
    const n = document.querySelector(".ew-notices .ew-notice")!;
    expect([n.querySelector("span")!.textContent, n.querySelector(".ew-lk")!.textContent]).toEqual(["学习路径里删掉了「营养与摄食」", "撤销"]);
    (n.querySelector(".ew-lk") as HTMLButtonElement).click(); await flush();
    expect(names()).toContain("营养与摄食");
    // drag (SortableJS) the first block to the third place: the flow's order changes and the list is drawn from it
    const before = names();
    sortDrag(m.cards().pop()!.querySelector<HTMLElement>("ol.ew-elist")!, 0, 2); await flush();
    expect(names()).toEqual([before[1], before[2], before[0], ...before.slice(3)]);
    // Alt + ↑ moves it back up by one
    m.cards().pop()!.querySelectorAll<HTMLLIElement>("ol.ew-elist > li")[2]!
      .dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowUp", altKey: true, bubbles: true, cancelable: true })); await flush();
    expect(names()).toEqual([before[1], before[0], before[2], ...before.slice(3)]);
    m.cards().pop()!.querySelectorAll<HTMLLIElement>("ol.ew-elist > li")[1]!
      .dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowUp", altKey: true, bubbles: true, cancelable: true })); await flush();
    expect(names()).toEqual(before);
    // the ⋯ menu's keys: the focus goes to 改名, ArrowDown to 删除; Esc closes it and puts the focus back on ⋯
    m.cards().pop()!.querySelectorAll<HTMLButtonElement>("ol.ew-elist > li .ew-more")[2]!.click(); await flush();
    expect(document.activeElement?.textContent).toBe("改名");
    document.activeElement!.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
    expect(document.activeElement?.textContent).toBe("删除");
    document.activeElement!.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(m.cards().pop()!.querySelector(".ew-menu")).toBeNull();
    expect(document.activeElement).toBe(m.cards().pop()!.querySelectorAll("ol.ew-elist > li .ew-more")[2]);
    // 改名
    m.cards().pop()!.querySelectorAll<HTMLButtonElement>("ol.ew-elist > li .ew-more")[1]!.click();
    m.cards().pop()!.querySelector<HTMLButtonElement>(".ew-menu button")!.click();
    const box = m.cards().pop()!.querySelector(".ew-edit")!;
    expect([...box.querySelectorAll("button")].map((b) => b.textContent)).toEqual(["保存", "取消"]);
    const inp = box.querySelector<HTMLInputElement>("input")!;
    expect([inp.value, inp.getAttribute("aria-label")]).toEqual(["水质与环境", "名字"]);
    inp.value = "水"; (box.querySelector("button") as HTMLButtonElement).click(); await flush();
    expect(names()[1]).toBe("水");
    // 加一块
    const add = m.cards().pop()!.querySelector(".ew-row.ew-add")!;
    const ai = add.querySelector<HTMLInputElement>("input")!;
    expect([ai.placeholder, ai.getAttribute("aria-label"), add.querySelector("button")!.textContent]).toEqual(["添加学习主题", "添加学习主题", "添加"]);
    ai.value = "造景"; ai.dispatchEvent(new Event("input")); (add.querySelector("button") as HTMLButtonElement).click(); await flush();
    expect(names().slice(-1)).toEqual(["造景"]);
  });

  it("进入: 正在把「水质与环境」拆成页… while it is split, then ol.elist.pg (number, title, 学完能, ⋯ 拆成多页 / 删除), 加一页", async () => {
    vi.useFakeTimers();
    const api = new FakeGoalApi({ roundMs: 0, splitMs: 900 });
    const m = make({ api, run: true });
    await m.view.onOpen();
    await opened(m, "我要学黑壳虾");
    await m.runFlow!.skip(); await flush();
    m.cards().pop()!.querySelectorAll<HTMLButtonElement>("ol.ew-elist > li")[1]!.querySelector<HTMLButtonElement>(".ew-go")!.click(); await flush();
    let card = m.cards().pop()!;
    expect(card.querySelector("[data-entered] .ew-thinking")!.textContent).toBe("正在把「水质与环境」拆成页…");
    await vi.advanceTimersByTimeAsync(900); await flush();
    card = m.cards().pop()!;
    expect([...card.querySelector("[data-entered]")!.children].map((c) => c.tagName.toLowerCase() + "." + c.className))
      .toEqual(["ol.ew-elist ew-pg", "div.ew-row ew-add"]);
    const lis = [...card.querySelectorAll("ol.ew-elist.ew-pg > li")];
    expect(lis.map((li) => [li.querySelector(".ew-n")!.textContent, li.querySelector(".ew-t")!.textContent])).toEqual([["1", "黑壳虾要什么样的水"], ["2", "氨氮、亚硝酸盐和硝化"], ["3", "怎么测、怎么调"]]);
    lis[0]!.querySelector<HTMLButtonElement>(".ew-more")!.click();
    const items = [...m.cards().pop()!.querySelectorAll(".ew-menu button")];
    expect(items.map((b) => b.textContent)).toEqual(["重命名", "拆成多页", "删除"]);
    (items[1] as HTMLButtonElement).click(); await flush();
    expect([...m.cards().pop()!.querySelectorAll("ol.ew-pg > li .ew-t")].map((x) => x.textContent).slice(0, 2)).toEqual(["黑壳虾要什么样的水是什么", "黑壳虾要什么样的水怎么做"]);
    expect([...document.querySelectorAll(".ew-notices .ew-notice span")].map((x) => x.textContent)).toContain("「黑壳虾要什么样的水」拆成了 2 页");
    const pi = m.cards().pop()!.querySelector<HTMLInputElement>(".ew-row.ew-add input")!;
    expect(pi.placeholder).toBe("添加页面");
  });
});

describe("a block's page cards fill with their pages' writing, from the rows' server state (the writing card)", () => {
  /** The run at 模型评估's pages; `row`: the first row's state / progress the server says now (getPath). */
  async function entered(o: { writeProgress?: LearnHost["writeProgress"] } = {}) {
    const fake = new FakeGoalApi({ roundMs: 0 });
    const realPath = fake.getPath.bind(fake);
    const st: { row: Partial<PathPageOut> | null } = { row: null };
    vi.spyOn(fake, "getPath").mockImplementation(async (id) => {
      const p = await realPath(id);
      return st.row === null ? p : { ...p, pages: p.pages.map((x, i) => (i === 0 ? { ...x, page_id: "pg_first", ...st.row } : x)) };
    });
    const m = make({ api: fake, run: true, ...(o.writeProgress ? { writeProgress: o.writeProgress } : {}) });
    await m.view.onOpen();
    await toPath(m);
    m.button("开始学习").click(); await flush();
    const fill = (): HTMLElement => m.cards().pop()!.querySelector<HTMLElement>("ol.ew-elist.ew-pg > li > .ew-fill")!;
    return { m, st, fill };
  }

  it("a page written in the background: the path read again fills its card (writing 4px → 1/3 → full and plain), no /wiki/writes read", async () => {
    vi.useFakeTimers();
    const { m, st, fill } = await entered();
    expect([fill().dataset.state, fill().style.width]).toEqual(["idle", "0px"]);
    st.row = { state: "writing", progress: { section: 0, of: 3, pass: 1 } };
    await m.runFlow!.readPath(); await flush();               // the run made the page (its path read again)
    expect([fill().dataset.state, fill().style.width, fill().dataset.pg]).toEqual(["writing", "calc(0% + 4px)", "pg_first"]);
    st.row = { state: "writing", progress: { section: 1, of: 3, pass: 1 } };
    await vi.advanceTimersByTimeAsync(PAGE_PROGRESS_POLL_MS); await flush();
    expect(fill().style.width).toBe("calc(33% + 4px)");
    st.row = { state: "written", progress: null };
    await vi.advanceTimersByTimeAsync(PAGE_PROGRESS_POLL_MS); await flush();
    expect([fill().dataset.state, fill().style.width]).toEqual(["done", "100%"]);
  });

  it("a page the chat's run writes here: its stream names it, and its steps fill 4px → 1/3 → 2/3 → full", async () => {
    let emit: ((p: { pageId: string; done: number; of: number; ended: boolean; ok: boolean }) => void) | null = null;
    const { st, fill } = await entered({ writeProgress: (fn) => { emit = fn; return () => { emit = null; }; } });
    const later = vi.spyOn(Date, "now").mockReturnValue(Date.now() + 60_000);    // past the watch after 开始学习
    await new Promise((r) => setTimeout(r, PAGE_PROGRESS_POLL_MS + 50));
    later.mockRestore();
    vi.useFakeTimers();
    vi.setSystemTime(Date.now() + 60_000);
    st.row = { state: "planned", progress: null };
    const widths: string[] = [];
    for (const [done, ended, ok] of [[0, false, false], [1, false, false], [2, false, false], [3, true, true]] as const) {
      emit!({ pageId: "pg_first", done, of: 3, ended, ok });
      await vi.advanceTimersByTimeAsync(PAGE_PROGRESS_POLL_MS);
      widths.push(fill().style.width);
    }
    expect(fill().dataset.pg).toBe("pg_first");
    expect(widths).toEqual(["calc(0% + 4px)", "calc(33% + 4px)", "calc(67% + 4px)", "100%"]);
  });
});

describe("学点新的 · entering splits on demand (spec 2026-10-28 §2.2): a node's parts are rows, the learner picks", () => {
  async function nested(progress?: () => ProgressOut | null) {
    const api = new NestedGoalApi();
    const m = make({ api, run: true, ...(progress ? { progress } : {}) });
    await m.view.onOpen();
    await m.runFlow!.openGoal({ draftId: null, mapId: NESTED_MAP }); await flush();
    return { api, ...m };
  }
  const opened = (m: ReturnType<typeof make>): HTMLElement => m.panel().querySelector<HTMLElement>("[data-entered]")!;
  const parts = (m: ReturnType<typeof make>): string[][] => [...opened(m).querySelectorAll<HTMLElement>(":scope > ol.ew-parts > li:not(.ew-nest)")]
    .map((li) => [li.querySelector(".ew-rname")!.textContent ?? "", ...[...li.querySelectorAll("button")].map((b) => b.textContent ?? "")]);
  const part = (m: ReturnType<typeof make>, id: string, text: string): HTMLButtonElement =>
    [...m.panel().querySelectorAll<HTMLButtonElement>(`li[data-part="${id}"] button`)].find((b) => b.textContent === text)!;
  const block = (m: ReturnType<typeof make>, k: number): HTMLButtonElement =>
    m.cards()[1]!.querySelectorAll<HTMLLIElement>("ol.ew-elist > li")[k]!.querySelector<HTMLButtonElement>(".ew-go")!;
  const splitFurther = async (m: ReturnType<typeof make>, id: string): Promise<void> => {
    part(m, id, "⋯").click();
    [...m.panel().querySelectorAll<HTMLButtonElement>(".ew-menu button")].find((b) => b.textContent === "继续拆细")!.click();
    await flush();
  };

  it("a block's parts open under its row, each part the same row (开始学习, ⋯ 继续拆细 on a point), three levels down, each 收起", async () => {
    const m = await nested();
    block(m, 0).click(); await flush();
    expect(m.entered).toEqual(["A"]);
    expect(opened(m).dataset.entered).toBe("A");
    expect(parts(m)).toEqual([["水的化学", "开始学习"], ["换水", "开始学习", "⋯"]]);
    part(m, "A1", "开始学习").click(); await flush();
    expect(opened(m).dataset.entered).toBe("A1");
    expect(part(m, "A1", "收起")).toBeTruthy();
    expect(parts(m)).toEqual([["氮循环", "开始学习"], ["pH", "开始学习", "⋯"]]);
    part(m, "A1a", "开始学习").click(); await flush();
    expect(opened(m).dataset.entered).toBe("A1a");
    expect(parts(m)).toEqual([["氨", "开始学习", "⋯"], ["亚硝酸盐", "开始学习", "⋯"]]);
    // only the nodes the learner picked were entered: no part was entered or opened by itself
    expect(m.api.calls.filter((c) => c.startsWith("enter:"))).toEqual(["enter:A", "enter:A1", "enter:A1a"]);
    part(m, "A1a", "收起").click();                       // folded: the level above stays, nothing is asked
    expect(m.panel().querySelector("[data-entered]")).toBeNull();
    expect(part(m, "A1", "收起")).toBeTruthy();
    part(m, "A1a", "继续学习").click();
    expect(opened(m).dataset.entered).toBe("A1a");
    expect(m.api.calls.filter((c) => c.startsWith("enter:")).length).toBe(3);
  });

  it("开始学习 on a point: its one page of the same name is the point's row (no level of its own); 继续学习 after; its diagnosis first when it needs one", async () => {
    const m = await nested();
    block(m, 0).click(); await flush();
    part(m, "A2", "开始学习").click(); await flush();
    expect(m.entered).toEqual(["A", "A2"]);
    expect(opened(m).dataset.entered).toBe("A2");
    expect(opened(m).querySelector("ol.ew-pg")).toBeNull();
    expect(opened(m).querySelector("input")!.placeholder).toBe("添加页面");
    expect(part(m, "A2", "开始生成")).toBeTruthy();
    part(m, "A1", "开始学习").click(); await flush();
    expect(part(m, "A2", "继续学习")).toBeTruthy();
    part(m, DIAGNOSED, "开始学习").click(); await flush();
    expect(m.runFlow!.state.draftId).toBe(`d-${DIAGNOSED}`);
  });

  it("⋯ 继续拆细 posts the split with the goal's map; the point's row becomes a sub-block, nothing entered", async () => {
    const m = await nested();
    block(m, 0).click(); await flush();
    await splitFurther(m, "A2");
    expect(m.api.calls).toContain(`split:A2:${NESTED_MAP}`);
    expect(parts(m)).toEqual([["水的化学", "开始学习"], ["换水", "开始学习"]]);
    expect([...document.querySelectorAll(".ew-notices .ew-notice span")].map((x) => x.textContent)).toContain("「换水」拆成了 2 部分");
    expect(m.api.calls.filter((c) => c.startsWith("enter:"))).toEqual(["enter:A"]);
    part(m, "A2", "开始学习").click(); await flush();
    expect(parts(m)).toEqual([["换水（1）", "开始学习", "⋯"], ["换水（2）", "开始学习", "⋯"]]);
  });

  it("the block entered is the highlighted row, its button 收起 (GET /wiki/progress tags are not shown); an older map's concept is an openable point", async () => {
    let progress: ProgressOut | null = null;
    const m = await nested(() => progress);
    block(m, 0).click(); await flush();
    progress = { today: { kind: "none" } as ProgressOut["today"], goals: [{ goal_id: NESTED_MAP, kind: "map", goal: "黑壳虾", state: "learning",
      blocks: [{ region_id: "A", name: "水质", level: "", state: "choosing", path_id: null, pages_done: 0, pages_total: 0, draft_id: null }],
      path_ids: [], pages_done: 0, pages_total: 0, step: { kind: "none" } as ProgressOut["today"] }] };
    m.view.render();
    const row = m.cards()[1]!.querySelector<HTMLLIElement>("ol.ew-elist > li")!;
    expect([row.className, row.querySelector(".ew-rname")!.firstChild!.textContent, row.querySelector(".ew-act")!.textContent]).toEqual(["ew-cur", "水质", "收起"]);
    // an older map: a region entered before, its concept leaves always `expanded`, not judged (atomic null): 学这个 · 继续拆细
    const old = m.runFlow!.state.map!;
    const map = { ...old, nodes: [...old.nodes.filter((n) => n.id === "B").map((n) => ({ ...n, state: "expanded" as const, enter: "children" as const })),
      { ...old.nodes.find((n) => n.id === "B")!, id: "c-old", name: "老知识点", parent: "B", kind: "concept" as const, state: "expanded" as const,
        atomic: null, enter: "learn" as const }] };
    vi.spyOn(m.api, "getMap").mockResolvedValue(map);
    vi.spyOn(m.api, "enterRegion").mockResolvedValue({ ...(await m.api.getDraft("x")), map_id: NESTED_MAP, map });
    await m.runFlow!.refreshMap(); await flush();
    m.cards()[1]!.querySelector<HTMLButtonElement>("ol.ew-elist > li .ew-go")!.click(); await flush();
    expect(parts(m)).toEqual([["老知识点", "开始学习", "⋯"]]);
  });
});

describe("学点新的 · 读懂这一篇 (reading-mode spec §5, §8.1 进度和展示): 先补 and 按原文读", () => {
  async function reading(o: { openPage?: LearnHost["openPage"]; writeFirstPage?: LearnHost["writeFirstPage"] } = {}) {
    const api = new ReadingGoalApi();
    const m = make({ api, run: true, ...o });
    await m.view.onOpen();
    await m.runFlow!.openGoal({ draftId: null, mapId: NESTED_MAP }); await flush();
    return { api, ...m };
  }
  const readCard = (m: ReturnType<typeof make>): HTMLElement => m.panel().querySelector<HTMLElement>("[data-reading]")!;
  const unitRow = (m: ReturnType<typeof make>, id: string): HTMLElement => readCard(m).querySelector<HTMLElement>(`li[data-unit="${id}"]`)!;
  const rowButtons = (li: HTMLElement): string[] => [...li.querySelectorAll(":scope > button, :scope > span.ew-chip")].map((b) => b.textContent ?? "");

  it("two blocks: 先补 (the map's prerequisite rows as ever, none primary) and 按原文读 (the units in order: title, location, about, status)", async () => {
    const m = await reading();
    expect(m.heads()).toEqual(["新的目标", LEARN_COPY.prereqTitle, LEARN_COPY.readTitle]);
    const pre = m.cards()[1]!;
    expect([...pre.querySelectorAll("ol.ew-elist > li .ew-rname")].map((x) => x.firstChild!.textContent)).toEqual(["水质", "喂养"]);
    // the block rows of the diagnosis redesign: 开始学习 is the row's quiet .ew-go, never a primary button
    expect([...pre.querySelectorAll("ol.ew-elist > li .ew-go")].map((b) => b.textContent)).toEqual(["开始学习", "开始学习"]);
    expect(pre.querySelector(".ew-primary")).toBeNull();
    const rows = [...readCard(m).querySelectorAll<HTMLElement>("ol.ew-units > li")];
    expect(rows.map((li) => li.dataset.unit)).toEqual(["ru_1", "ru_2", "ru_3", "ru_4"]);
    expect(rows.map((li) => li.querySelector(".ew-rname")!.textContent)).toEqual(["要解决什么问题§1 p.1", "自注意力§2 p.2", "实验§3 p.4", "结论§4 p.5"]);
    expect(rows[1]!.querySelector(".ew-scope")!.textContent).toBe("每个词看所有词");
    expect(rows.map(rowButtons)).toEqual([[LEARN_COPY.unitDone], [LEARN_COPY.readUnit], [LEARN_COPY.unitSkipped], [LEARN_COPY.readUnit]]);
    expect(rows[2]!.classList.contains("ew-skipped")).toBe(true);
  });

  it("the progress line: 读到第 N 段 · 共 M 段, skipped units not counted", async () => {
    const m = await reading();
    expect(readCard(m).querySelector("[data-role=progress]")!.textContent).toBe("读到第 1 段 · 共 3 段");
    expect(LEARN_COPY.readProgress(0, 8)).toBe("共 8 段");
    expect(LEARN_COPY.readProgress(3, 8)).toBe("读到第 3 段 · 共 8 段");
    expect(LEARN_COPY.readProgress(8, 8)).toBe("读完了 · 共 8 段");
  });

  it("a read unit shows its points' K colour, never 已掌握; a unit not read is grey", async () => {
    const m = await reading();
    const done = unitRow(m, "ru_1");
    expect(done.textContent).not.toContain("已掌握");
    expect(done.querySelector<HTMLElement>(".ew-dot")!.style.background).toBe("rgb(47, 158, 91)");    // --known
    expect(unitRow(m, "ru_4").querySelector<HTMLElement>(".ew-dot")!.style.background).toBe("rgb(184, 184, 184)");   // --none
    expect(m.panel().textContent).not.toContain("已掌握");
  });

  it("下一步: the first unit not read and not skipped is the primary 读这一段", async () => {
    const m = await reading();
    const primary = [...readCard(m).querySelectorAll<HTMLButtonElement>(".ew-btn.ew-primary")];
    expect(primary.map((b) => b.closest("li")!.dataset.unit)).toEqual(["ru_2"]);
  });

  it("a unit whose uses are not learned: 先补：X chips and 先去学 / 直接读 (never a block); 直接读 starts and writes its page", async () => {
    const write = vi.fn((_t: unknown) => Promise.resolve());
    const m = await reading({ writeFirstPage: write });
    unitRow(m, "ru_2").querySelector<HTMLButtonElement>(".ew-btn")!.click(); await flush();
    expect(m.api.calls.filter((c) => c.startsWith("open:"))).toEqual(["open:ru_2:false"]);   // looked at, not started
    const row = unitRow(m, "ru_2");
    expect([...row.querySelectorAll<HTMLButtonElement>("[data-use]")].map((b) => b.textContent)).toEqual(["先补：水质", "先补：softmax"]);
    const hint = row.querySelector<HTMLElement>("[data-hint]")!;
    expect(hint.firstChild!.textContent).toBe("这一段用到 水质、softmax，你还没学：");
    expect([...hint.querySelectorAll("button")].map((b) => b.textContent)).toEqual([LEARN_COPY.learnFirst, LEARN_COPY.readAnyway]);
    expect(write).not.toHaveBeenCalled();
    [...hint.querySelectorAll<HTMLButtonElement>("button")].find((b) => b.textContent === LEARN_COPY.readAnyway)!.click(); await flush();
    expect(m.api.calls.filter((c) => c.startsWith("open:"))).toEqual(["open:ru_2:false", "open:ru_2:true"]);
    expect(write).toHaveBeenCalledWith({ pathId: null, pageId: "pg-ru_2", title: "自注意力", goal: "黑壳虾" });
    expect(rowButtons(unitRow(m, "ru_2"))).toEqual([LEARN_COPY.continueUnit]);
    expect(unitRow(m, "ru_2").querySelector("[data-hint]")).toBeNull();
  });

  it("先补 chips go to the point's page, else enter its node on the map", async () => {
    const opened: string[] = [];
    const m = await reading({ openPage: (id) => { opened.push(id); } });
    unitRow(m, "ru_2").querySelector<HTMLButtonElement>(".ew-btn")!.click(); await flush();
    unitRow(m, "ru_2").querySelector<HTMLButtonElement>("[data-use=sm]")!.click(); await flush();
    expect(opened).toEqual(["pg-softmax"]);
    unitRow(m, "ru_2").querySelector<HTMLButtonElement>("[data-use=A]")!.click(); await flush();
    expect(m.entered).toEqual(["A"]);
    expect(m.api.calls.filter((c) => c.startsWith("open:"))).toEqual(["open:ru_2:false"]);   // a chip never reads the unit
  });

  it("a unit whose uses are known reads at once; a read unit opens its page", async () => {
    const opened: string[] = [];
    const write = vi.fn((_t: unknown) => Promise.resolve());
    const m = await reading({ openPage: (id) => { opened.push(id); }, writeFirstPage: write });
    unitRow(m, "ru_4").click(); await flush();
    expect(m.api.calls.filter((c) => c.startsWith("open:"))).toEqual(["open:ru_4:true"]);
    expect(write).toHaveBeenCalledWith(expect.objectContaining({ pageId: "pg-ru_4" }));
    unitRow(m, "ru_1").click(); await flush();
    expect(opened).toEqual(["pg-r1"]);
    unitRow(m, "ru_3").click(); await flush();                                // 不读: nothing
    expect(m.api.calls.filter((c) => c.startsWith("open:ru_3"))).toEqual([]);
  });
});
