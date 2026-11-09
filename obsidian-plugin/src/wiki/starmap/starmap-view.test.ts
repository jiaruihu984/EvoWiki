import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { WorkspaceLeaf } from "obsidian";
import Sortable from "sortablejs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { noticeHost } from "../../ui/ew-notice";
import { sortDrag } from "../../ui/reorder-test-kit";
import EvoWikiPlugin from "../../main";
import { WIKI_COPY } from "../copy";
import { FakeSigma } from "../graph-page/fake-sigma";
import { GraphFixture } from "../graph-page/fixture";
import { levelFor } from "../graph-page/sigma-cloud";
import type { GraphSource } from "../graph-page/store";
import type { WriteOut } from "../schema";
import type { GraphOut, PathEditIn } from "../schema";
import { STAR_COPY } from "./copy";
import { regionWord, STARMAP_VIEW_TYPE, StarMapView, type StarMapHost } from "./starmap-view";

const flush = async (): Promise<void> => { for (let i = 0; i < 8; i++) await Promise.resolve(); };

let frames: FrameRequestCallback[];
let reduced: boolean;
let clock: number;
const cancel = vi.fn();

beforeEach(() => {
  frames = []; cancel.mockClear(); reduced = true; clock = 10_000; FakeSigma.all = [];
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => { frames.push(cb); return frames.length; });
  vi.stubGlobal("cancelAnimationFrame", cancel);
  vi.stubGlobal("matchMedia", () => ({ get matches() { return reduced; } }));
  vi.stubGlobal("ResizeObserver", class { observe(): void {} disconnect(): void {} });
  HTMLElement.prototype.getBoundingClientRect = () => ({ width: 800, height: 600, top: 0, left: 0, right: 800, bottom: 600, x: 0, y: 0, toJSON: () => ({}) });
  vi.spyOn(performance, "now").mockImplementation(() => clock);
});
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); noticeHost().replaceChildren(); document.body.replaceChildren(); });

interface Opts { load?: () => Promise<unknown>; offline?: boolean; demo?: boolean; connection?: StarMapHost["connection"]; fail?: Partial<Record<"split" | "skip" | "want" | "editPath" | "splitRow", boolean>>;
  writes?: GraphSource["writes"]; splitGate?: Promise<void>; waits?: StarMapHost["waits"]; kChanged?: GraphSource["kChanged"] }
function make(o: Opts = {}) {
  const fx = new GraphFixture();
  const calls: [string, ...unknown[]][] = [];
  const maybe = <T>(name: "split" | "skip" | "want" | "editPath" | "splitRow", f: () => T): Promise<T> =>
    (o.fail?.[name] ? Promise.reject(new Error("server says no")) : Promise.resolve().then(f));
  const load = vi.fn(o.load ?? (() => Promise.resolve(fx.graph())));
  const source: GraphSource = {
    demo: o.demo ?? false, isOffline: () => o.offline ?? false, load,
    split: (id) => { calls.push(["split", id]); return maybe("split", () => fx.split(id)); },
    want: (id, m) => { calls.push(["want", id, m]); return maybe("want", () => fx.want(id, m)); },
    editPath: (p, e: PathEditIn) => { calls.push(["editPath", p, e]); return maybe("editPath", () => fx.editPath(p, e)); },
    splitRow: (p, k) => { calls.push(["splitRow", p, k]); return (o.splitGate ?? Promise.resolve()).then(() => maybe("splitRow", () => fx.splitRow(p, k))); },
    undoSplitRow: (p, k, c) => { calls.push(["undoSplitRow", p, k, c]); return Promise.resolve(fx.undoSplitRow(p, k, c)); },
    ...(o.writes ? { writes: o.writes } : {}),
    ...(o.kChanged ? { kChanged: o.kChanged } : {}),
  };
  const openLearn = vi.fn();
  let cssFn: (() => void) | null = null;
  const unsubCss = vi.fn();
  const showPage = vi.fn(), writePage = vi.fn(), openRow = vi.fn();
  const host: StarMapHost = { source, openLearn, showPage, writePage, openRow, onCssChange: (fn) => { cssFn = fn; return unsubCss; }, ...(o.connection ? { connection: o.connection } : {}),
    ...(o.waits ? { waits: o.waits } : {}) };
  const view = new StarMapView({} as WorkspaceLeaf, host);
  document.body.append(view.contentEl);
  const root = (): HTMLElement => view.contentEl.firstElementChild as HTMLElement;
  const q = <T extends Element>(sel: string): T => { const x = view.contentEl.querySelector<T>(sel); if (!x) throw new Error(`no ${sel}`); return x; };
  const all = <T extends Element>(sel: string): T[] => [...view.contentEl.querySelectorAll<T>(sel)];
  const button = (text: string, scope: ParentNode = view.contentEl): HTMLButtonElement => {
    const b = [...scope.querySelectorAll<HTMLButtonElement>("button")].find((x) => x.textContent === text);
    if (!b) throw new Error(`no button ${text}`); return b;
  };
  const state = (): HTMLElement | null => view.contentEl.querySelector<HTMLElement>(".ew-cloudwrap > .ew-mapstate");
  const pickNode = (id: string): void => { all<HTMLButtonElement>("ul.evowiki-sr-only [data-node]").find((b) => b.dataset.node === id)!.click(); };
  const card = (): HTMLElement | null => view.contentEl.querySelector<HTMLElement>("[data-card=node]");
  const goalButtons = (): HTMLButtonElement[] => all<HTMLButtonElement>(".ew-goal");
  const pathTitles = (): string[] => all(".ew-pages li .ew-t").map((t) => t.firstChild!.textContent!);
  const open = async (): Promise<void> => { await view.onOpen(); await flush(); };
  return { view, fx, calls, load, openLearn, showPage, writePage, openRow, root, q, all, button, state, pickNode, card, goalButtons, pathTitles, open, css: () => cssFn?.(), unsubCss };
}
type M = ReturnType<typeof make>;
/** The Sigma the page drew with (jsdom: the fake). */
const sigma = (): FakeSigma => FakeSigma.last;
/** Zoomed in to the block level, where the dots are, and their fade over. */
const atBlocks = async (): Promise<void> => { sigma().camera.ratio = 0.4; sigma().graphToViewport = (p: { x: number; y: number }) => ({ x: p.x * 3000, y: p.y * 3000 }); sigma().emit("afterRender"); await new Promise((r) => setTimeout(r, 300)); };
/** 数学's points (the fixture's map_math). */
const MATH = ["vec", "mul", "eig", "der", "grad", "mean", "var", "cond", "pca"];
/** The camera sits in the middle of these points' box, moved up by the room for a title (framed at block level). */
const framedOn = (ids: string[]): void => {
  const ps = ids.map((id) => sigma().getNodeDisplayData(id)!), xs = ps.map((p) => p.x as number), ys = ps.map((p) => p.y as number);
  expect(sigma().camera.x).toBeCloseTo((Math.min(...xs) + Math.max(...xs)) / 2, 6);
  expect(sigma().camera.y).toBeGreaterThanOrEqual((Math.min(...ys) + Math.max(...ys)) / 2);
};

describe("知识图谱: a wait with no page (page-waits.ts)", () => {
  it("its bar at the top of the panel; redrawn when what it shows changed, not on every sidebar change", async () => {
    let sig = "a";
    let fire: () => void = () => undefined;
    const bars = vi.fn(() => { const b = document.createElement("div"); b.className = "ew-revbar"; b.dataset.wait = sig; return [b]; });
    const m = make({ waits: { bars, sig: () => sig, subscribe: (fn) => { fire = fn; return () => undefined; } } });
    await m.open();
    expect(m.q(".ew-panel").firstElementChild?.className).toBe("ew-revbar");
    const n = bars.mock.calls.length;
    fire();
    expect(bars.mock.calls.length).toBe(n);
    sig = "b";
    fire();
    expect(m.q<HTMLElement>(".ew-panel > .ew-revbar").dataset.wait).toBe("b");
  });
});

describe("知识图谱: follows K", () => {
  it("K read again (an answer went into K): the open map reads the graph again; after closing it does not", async () => {
    let tell: () => void = () => undefined;
    const m = make({ kChanged: (fn) => { tell = fn; return () => { tell = () => undefined; }; } });
    await m.open();
    expect(m.load).toHaveBeenCalledTimes(1);
    tell();
    await flush();
    expect(m.load).toHaveBeenCalledTimes(2);
    await m.view.onClose();
    tell();
    await flush();
    expect(m.load).toHaveBeenCalledTimes(2);
  });
});

describe("知识图谱 = graph.html: layout, copy and states", () => {
  it("div.ew.ew-graph > .ew-app (.ew-panel | .ew-cloudwrap: the Sigma box, tip, ＋ / －, legend) + the hidden node list", async () => {
    const m = make();
    await m.open();
    expect(m.view.getViewType()).toBe(STARMAP_VIEW_TYPE);
    expect(m.view.getDisplayText()).toBe("知识图谱");
    expect(m.view.getIcon()).toBe("network");
    expect(m.root().className).toBe("ew ew-graph");
    expect([...m.root().children].map((c) => c.className)).toEqual(["ew-app", "evowiki-sr-only"]);
    expect([...m.q(".ew-app").children].map((c) => c.className)).toEqual(["ew-panel", "ew-cloudwrap"]);
    expect([...m.q(".ew-cloudwrap").children].map((c) => c.tagName.toLowerCase() + "." + c.className)).toEqual(["div.ew-sigma", "div.ew-tip", "div.ew-zoom", "div.ew-legend"]);
    expect(sigma().container).toBe(m.q(".ew-sigma"));
    expect(m.q(".ew-sigma").hasAttribute("aria-label")).toBe(false);   // Obsidian would show it as a hover tooltip
    const [zin, zout] = m.all<HTMLButtonElement>(".ew-zoom .ew-btns button");
    expect([zin!.className, zin!.textContent, zin!.getAttribute("aria-label")]).toEqual(["ew-btn ew-icon", "＋", "放大"]);
    expect([zout!.textContent, zout!.getAttribute("aria-label")]).toEqual(["－", "缩小"]);
    expect(m.root().querySelector(".ew-lvl")).toBeNull();   // no zoom-level label (spec 2026-10-21)
    expect(m.q(".ew-zoom").children).toHaveLength(1);
    expect(m.all(".ew-legend span").map((s) => s.textContent)).toEqual(["掌握", "熟悉", "不会"]);
    expect(m.all<HTMLElement>(".ew-legend i").map((i) => i.style.background)).toEqual(["var(--known)", "var(--shaky)", "var(--gap)"]);
    expect(m.q<HTMLElement>(".ew-tip").hidden).toBe(true);
    expect(m.root().dataset.state).toBe("ready");
    expect(m.state()).toBeNull();
    // no view switch, no 图谱改了什么 card
    expect(m.view.contentEl.textContent).not.toContain("图谱改了什么");
    expect(m.view.contentEl.querySelector(".ew-seg")).toBeNull();
    await m.view.onClose();
  });

  it("the demo adds （演示） to the title; the ribbon tooltip is EvoWiki 知识图谱", () => {
    expect(make({ demo: true }).view.getDisplayText()).toBe("知识图谱（演示）");
    expect(STAR_COPY.ribbon).toBe("EvoWiki 知识图谱");
  });

  it("loading: one line, the cloud / legend / zoom hidden; then the graph", async () => {
    const fx = new GraphFixture();
    let done!: (v: unknown) => void;
    const m = make({ load: () => new Promise((r) => { done = r; }) });
    await m.view.onOpen();
    expect(m.state()!.textContent).toBe(STAR_COPY.loading);
    expect(m.q<HTMLElement>(".ew-sigma").hidden).toBe(true);
    expect(m.q<HTMLElement>(".ew-legend").hidden).toBe(true);
    expect(m.q<HTMLElement>(".ew-zoom").hidden).toBe(true);
    done(fx.graph());
    await flush();
    expect(m.state()).toBeNull();
    expect(m.q<HTMLElement>(".ew-sigma").hidden).toBe(false);
    await m.view.onClose();
  });

  it("error (also a malformed graph): a plain sentence and 重试, which reads again", async () => {
    let bad = true;
    const fx = new GraphFixture();
    const m = make({ load: () => Promise.resolve(bad ? { nodes: "secret server detail" } : fx.graph()) });
    await m.open();
    expect(m.state()!.textContent).toBe(WIKI_COPY.readFailedShort + "重试");
    expect(m.state()!.getAttribute("role")).toBe("alert");
    expect(m.view.contentEl.textContent).not.toContain("secret");
    bad = false;
    m.button("重试").click();
    await flush();
    expect(m.load).toHaveBeenCalledTimes(2);
    expect(m.state()).toBeNull();
    await m.view.onClose();
  });

  it("offline with nothing read: the one 未连接到服务器 · 重试 line; offline over a kept graph: the same line at the top, no notice", async () => {
    const a = make({ load: () => Promise.reject(new Error("x")), offline: true });
    await a.open();
    expect(a.state()!.textContent).toBe(WIKI_COPY.notConnected + " 重试");
    expect(a.state()!.dataset.role).toBe("offline");
    await a.view.onClose();
    let fail = false;
    const fx = new GraphFixture();
    const b = make({ load: () => (fail ? Promise.reject(new Error("x")) : Promise.resolve(fx.graph())), offline: true });
    await b.open();
    fail = true;
    b.view.refresh(); await flush();
    b.view.refresh(); await flush();
    expect(b.q<HTMLElement>(".ew-sigma").hidden).toBe(false);
    const banners = b.all<HTMLElement>("[data-role=offline]");
    expect(banners).toHaveLength(1);
    expect(banners[0]!.className).toBe("ew-muted ew-mapbanner");
    expect(document.querySelectorAll(".ew-notices .ew-notice")).toHaveLength(0);
    await b.view.onClose();
  });

  it("with the connection: offline shows exactly one banner (its 重试 reconnects, then reads again); back online it goes and the failed read is made again", async () => {
    let offline = true;
    let fire: (() => void) | null = null;
    const retry = vi.fn(() => Promise.resolve());
    let fail = true;
    const fx = new GraphFixture();
    const m = make({ load: () => (fail ? Promise.reject(new Error("x")) : Promise.resolve(fx.graph())), connection: {
      offline: () => offline, onChange: (fn) => { fire = fn; return () => { fire = null; }; }, retry } });
    await m.open();
    expect(m.all("[data-role=offline]")).toHaveLength(1);
    expect(m.view.contentEl.textContent).not.toContain(WIKI_COPY.readFailedShort);
    m.button("重试").click();
    await flush();
    expect(retry).toHaveBeenCalledOnce();
    expect(m.load).toHaveBeenCalledTimes(2);
    expect(m.all("[data-role=offline]")).toHaveLength(1);
    offline = false; fail = false;
    fire!();
    await flush();
    expect(m.all("[data-role=offline]")).toHaveLength(0);
    expect(m.load).toHaveBeenCalledTimes(3);
    expect(m.state()).toBeNull();
    expect(m.q<HTMLElement>(".ew-sigma").hidden).toBe(false);
    await m.view.onClose();
    expect(fire).toBeNull();
  });

  it("empty: the plain line and 去学点新的, which opens the 学点新的 tab", async () => {
    const m = make({ load: () => Promise.resolve({ nodes: [], relations: [], groups: [], goals: [] }) });
    await m.open();
    expect(m.state()!.textContent).toBe(STAR_COPY.empty + " 去学点新的");
    expect(m.root().dataset.state).toBe("empty");
    m.button("去学点新的").click();
    expect(m.openLearn).toHaveBeenCalledTimes(1);
    await m.view.onClose();
  });

  it("reads the graph only when opened; a theme change re-reads the colours; closing unsubscribes and kills Sigma", async () => {
    const m = make();
    expect(m.load).not.toHaveBeenCalled();
    await m.open();
    const spy = vi.spyOn(globalThis, "getComputedStyle");
    m.css();
    expect(spy).toHaveBeenCalled();
    await m.view.onClose();
    expect(m.unsubCss).toHaveBeenCalledTimes(1);
    expect(sigma().killed).toBe(true);
  });
});

describe("the cloud: Sigma.js + graphology (their layout, labels, camera)", () => {
  it("one Sigma node per drawn point (its name, its band colour), a title per group over the canvas; no numbers", async () => {
    const m = make();
    await m.open();
    const g = sigma().graph;
    expect(g.nodes().sort()).toEqual([...m.view["scene"]!.order].sort());
    expect(g.getNodeAttributes("mul")).toMatchObject({ label: "矩阵乘法", color: "", band: "shaky" });   // jsdom has no tokens
    const titles = [...sigma().container.querySelectorAll(".ew-gtitle")].map((e) => e.textContent);
    expect(titles).toEqual(expect.arrayContaining(m.view["scene"]!.l1.map((x) => x.name)));
    expect([...g.mapNodes((_, a) => String(a.label)), ...titles].some((l) => /\d/.test(l))).toBe(false);
    await m.view.onClose();
  });

  it("＋ / － go one level a press (reduced motion: at once); past the coarsest, one camera step; a new graph keeps where the learner put it", async () => {
    const m = make();
    await m.open();
    const h = m.view["canvasH"]!;
    const at = () => h.levels().indexOf(levelFor(h.levels(), sigma().camera.ratio));
    const from = at();
    m.button("＋").click();
    expect(at()).toBe(from + 1);
    m.button("－").click();
    expect(at()).toBe(from);
    const coarsest = sigma().camera.ratio;
    m.button("－").click();
    expect(sigma().camera.ratio).toBeCloseTo(coarsest * 1.6, 6);
    const ratio = sigma().camera.ratio;
    sigma().camera.setState({ x: 0.2, y: 0.7 });                        // a pan
    m.view.refresh(); await flush();
    expect(sigma().camera.getState()).toMatchObject({ x: 0.2, y: 0.7, ratio });
    await m.view.onClose();
  });

  it("Sigma's events: a click on a point selects it, on nothing clears; hover shows the tip; a group's title frames its points", async () => {
    const m = make();
    await m.open();
    await atBlocks();
    sigma().emit("clickNode", { node: "mul" });
    expect(m.view.selectedNode).toBe("mul");
    expect(m.card()!.querySelector("h2")!.textContent).toBe("矩阵乘法熟悉");
    expect(sigma().node("mul")).toMatchObject({ highlighted: true, forceLabel: true });
    sigma().emit("enterNode", { node: "mul" });
    expect(m.q<HTMLElement>(".ew-tip").hidden).toBe(false);
    // selected, its name stands beside it: the tip does not say it again
    expect([...m.q(".ew-tip").children].map((c) => c.textContent)[0]).toBe("向量和点积 → 一行乘一列就是点积");
    sigma().emit("leaveNode", { node: "mul" });
    sigma().emit("clickStage");
    sigma().emit("enterNode", { node: "mul" });
    expect([...m.q(".ew-tip").children].map((c) => c.textContent).slice(0, 2)).toEqual(["矩阵乘法　熟悉", "向量和点积 → 一行乘一列就是点积"]);
    sigma().emit("leaveNode", { node: "mul" });
    expect(m.q<HTMLElement>(".ew-tip").hidden).toBe(true);
    sigma().emit("clickStage", {});
    expect(m.view.selectedNode).toBeNull();
    expect(m.card()).toBeNull();
    sigma().container.querySelector<HTMLElement>(".ew-gtitle-l1")!.click();
    expect(m.view.selectedNode).toBeNull();                              // a title is no point
    expect(sigma().camera.ratio).toBeLessThan(1);                        // framed
    await m.view.onClose();
  });
});

describe("你的目标: share bars without numbers, goal focus dims what the goal does not use", () => {
  it("one button per goal with its name and a bar (known / shaky / gap / none, flex = share), no digits", async () => {
    const m = make();
    await m.open();
    expect(m.q(".ew-panel .ew-card h2").textContent).toBe("你的目标");
    expect(m.goalButtons().map((b) => b.querySelector(".ew-gname")!.textContent)).toEqual(["机器学习", "数学"]);
    expect(m.goalButtons().map((b) => b.title)).toEqual(["", ""]);   // no hover tooltip
    const bar = m.goalButtons()[1]!.querySelector(".ew-bar")!;
    const segs = [...bar.children].map((i) => i as HTMLElement);
    expect(segs.map((i) => i.style.background)).toEqual(["var(--known)", "var(--shaky)", "var(--gap)", "var(--none)"]);
    [4 / 9, 2 / 9, 1 / 9, 2 / 9].forEach((f, k) => { expect(parseFloat(segs[k]!.style.flex)).toBeCloseTo(f, 5); });
    expect(m.q(".ew-panel").textContent).not.toMatch(/\d/);
    await m.view.onClose();
  });

  it("pressing a goal dims the points outside it and lights its path; pressing again clears", async () => {
    const m = make();
    await m.open();
    await atBlocks();
    const drawnPoints = (): string[] => sigma().graph.nodes();
    /** Dimmed: drawn in a colour mixed towards the background. */
    const dimmed = (id: string): boolean => sigma().node(id).color !== sigma().graph.getNodeAttribute(id, "color");
    expect(drawnPoints().every((id) => !dimmed(id))).toBe(true);
    m.goalButtons()[1]!.click();
    expect(m.goalButtons()[1]!.getAttribute("aria-pressed")).toBe("true");
    expect(m.view.focusGoal).toBe("map_math");
    expect(m.pathTitles()).toEqual(["向量和矩阵乘法", "特征值与特征向量", "导数和梯度", "条件概率"]);
    const smallest = Math.min(...drawnPoints().map((id) => sigma().node(id).size as number));
    const lit = drawnPoints().filter((id) => sigma().node(id).size > smallest);
    expect(lit).toHaveLength(6);                                         // the path's points: bigger, so their names win
    expect(drawnPoints().filter((id) => dimmed(id))).toHaveLength(11);   // a point on both goals is one dot here, drawn on its first goal
    framedOn(MATH);                                                      // framed on the goal
    m.goalButtons()[1]!.click();
    expect(m.view.focusGoal).toBeNull();
    expect(m.all(".ew-pages")).toHaveLength(0);
    expect(drawnPoints().every((id) => !dimmed(id) && sigma().node(id).size === sigma().node(drawnPoints()[0]!).size)).toBe(true);
    await m.view.onClose();
  });
});

describe("the node card (graph.html nodeCard)", () => {
  it("only the learner model's state: dot + name + band word, the group names (no 属于), 前置 with each one's dot and band word; no evidence, phrase or kind", async () => {
    const m = make();
    await m.open();
    m.pickNode("pca");
    const card = (): HTMLElement => m.card()!;
    const lines = (): string[] => [...card().querySelectorAll(".ew-ev > span")].map((s) => s.textContent ?? "");
    expect(card().querySelector("h2 .ew-dot")).not.toBeNull();
    expect(card().querySelector("h2.ew-nh")).not.toBeNull();
    expect(lines()).toEqual(["无监督学习、线性代数", "前置", "特征值与特征向量熟悉", "方差掌握"].map((x) => x));
    expect([...card().querySelectorAll(".ew-prow")].map((r) => [r.querySelector(".ew-dot") !== null, r.lastElementChild!.className, r.lastElementChild!.textContent])).toEqual([
      [true, "ew-bandw", "熟悉"], [true, "ew-bandw", "掌握"]]);
    m.pickNode("mul");
    expect(card().querySelector("h2 .ew-bandw")!.textContent).toBe("熟悉");
    expect(lines()[0]).toBe("线性代数");
    expect(card().textContent).not.toMatch(/属于|现在|前置：|一种分类模型|——/);
    await m.view.onClose();
  });

  it("the card has no buttons at all: no 我想学这个, 看不懂 or 我会了，跳过", async () => {
    const m = make();
    await m.open();
    for (const id of ["mul", "vec", "py", "grad"]) {
      m.pickNode(id);
      expect(m.card()!.querySelector("button, .ew-row")).toBeNull();
      expect(m.card()!.textContent).not.toMatch(/我想学这个|看不懂|跳过|证据/);
    }
    await m.view.onClose();
  });

});

/** The ⋯ menu of row i, then its 删除. */
const openMenu = (m: M, i: number): HTMLElement => { m.all<HTMLLIElement>(".ew-pages li")[i]!.querySelector<HTMLButtonElement>("button.ew-more")!.click(); return m.q(".ew-menu"); };
const menuItem = (m: M, label: string): HTMLButtonElement => m.all<HTMLButtonElement>(".ew-menu button").find((b) => b.textContent === label)!;
const removeRow = (m: M, i: number): void => { openMenu(m, i); menuItem(m, "删除").click(); };
describe("学习路径: a row opens its page", () => {
  it("a click on the row, or Enter, opens it; its ⋯ does not", async () => {
    const m = make();
    await m.open();
    m.goalButtons()[1]!.click();
    const li = m.all<HTMLElement>(".ew-pages li")[0]!;
    li.querySelector<HTMLElement>(".ew-t")!.click();
    expect(m.openRow).toHaveBeenCalledTimes(1);
    expect(m.openRow.mock.calls[0]![0]).toMatchObject({ title: m.pathTitles()[0] });
    li.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" }));
    expect(m.openRow).toHaveBeenCalledTimes(2);
    li.querySelector<HTMLElement>("button.ew-more")!.click();
    expect(m.openRow).toHaveBeenCalledTimes(2);
  });
});

describe("学习路径: editable (drag, Alt + ↑ / ↓, ⋯ 删除, 加上), each edit goes to the API", () => {
  async function onMath(o: Opts = {}): Promise<M> {
    const m = make(o);
    await m.open();
    m.goalButtons()[1]!.click();
    return m;
  }
  it("rows: number, title, summary, ⋮⋮ and ⋯ (no ×); draggable, focusable, labelled", async () => {
    const m = await onMath();
    const li = m.all<HTMLLIElement>(".ew-pages li")[0]!;
    expect(li.draggable).toBe(true);
    expect(li.tabIndex).toBe(0);
    expect(li.getAttribute("aria-label")).toBe("第 1 页：向量和矩阵乘法，按住拖动调整顺序，或用 Alt + 上下键");
    expect([...li.children].map((c) => c.className)).toEqual(["ew-n", "", "ew-ops"]);
    expect(li.querySelector(".ew-s")!.textContent).toBe("两个箭头有多同向，两张表怎么相乘");   // the question, never the abstract
    expect(li.querySelector(".ew-grip")!.textContent).toBe("⋮⋮");
    expect(li.querySelectorAll(".ew-ops button")).toHaveLength(1);
    const more = li.querySelector(".ew-ops button.ew-more")!;
    expect([more.textContent, more.getAttribute("aria-label"), more.getAttribute("aria-haspopup"), more.getAttribute("aria-expanded")]).toEqual(["⋯", "「向量和矩阵乘法」的操作", "menu", "false"]);
    expect(m.q<HTMLInputElement>(".ew-card input[type=text]").placeholder).toBe("添加页面，比如：交叉验证");
    await m.view.onClose();
  });

  it("drag page 1 to page 3 (SortableJS): shown at once, sent as move 0 → 2", async () => {
    const m = await onMath();
    sortDrag(m.q<HTMLElement>(".ew-pages"), 0, 2);
    expect(m.pathTitles()).toEqual(["特征值与特征向量", "导数和梯度", "向量和矩阵乘法", "条件概率"]);
    await flush();
    expect(m.calls).toEqual([["editPath", "path_math", { op: "move", from: 0, to: 2 }]]);
    expect(m.pathTitles()).toEqual(["特征值与特征向量", "导数和梯度", "向量和矩阵乘法", "条件概率"]);
    await m.view.onClose();
  });

  it("a file or text dragged in from outside is not taken by the path rows (no dragover accepted, no drop swallowed)", async () => {
    const m = await onMath();
    const lis = m.all<HTMLLIElement>(".ew-pages li");
    const over = new Event("dragover", { bubbles: true, cancelable: true });
    lis[2]!.dispatchEvent(over);
    expect(over.defaultPrevented).toBe(false);
    let reached = false;
    document.addEventListener("drop", () => { reached = true; }, { once: true });
    const drop = new Event("drop", { bubbles: true, cancelable: true });
    lis[2]!.dispatchEvent(drop);
    expect([drop.defaultPrevented, reached]).toEqual([false, true]);
    await flush();
    expect(m.calls).toEqual([]);
    expect(m.all(".ew-sort-ghost, .ew-sort-drag")).toHaveLength(0);
    // the panel drawn again: the old list's instance is gone, the new list has one
    const before = m.q<HTMLElement>(".ew-pages");
    sortDrag(before, 1, 0);
    await flush();
    expect(Sortable.get(before)).toBeFalsy();
    expect(Sortable.get(m.q<HTMLElement>(".ew-pages"))).toBeTruthy();
    await m.view.onClose();
  });

  it("Alt + ↓ / ↑ moves a page and keeps the focus on it; without Alt nothing happens; not past the ends", async () => {
    const m = await onMath();
    const key = (i: number, k: string, alt = true): void => { m.all<HTMLLIElement>(".ew-pages li")[i]!.dispatchEvent(new KeyboardEvent("keydown", { key: k, altKey: alt, bubbles: true })); };
    key(0, "ArrowDown", false);
    key(0, "ArrowUp");
    expect(m.calls).toEqual([]);
    key(1, "ArrowDown");
    await flush();
    await new Promise((r) => setTimeout(r, 0));
    expect(m.calls).toEqual([["editPath", "path_math", { op: "move", from: 1, to: 2 }]]);
    expect(m.pathTitles()[2]).toBe("特征值与特征向量");
    expect(document.activeElement).toBe(m.all(".ew-pages li")[2]);
    key(3, "ArrowDown");
    expect(m.calls).toHaveLength(1);
    await m.view.onClose();
  });

  it("⋯ 删除 removes the page; 加上 adds the typed page (你加的); an empty input sends nothing", async () => {
    const m = await onMath();
    removeRow(m, 3);
    expect(m.pathTitles()).toHaveLength(3);
    await flush();
    m.button("添加").click();
    const inp = m.q<HTMLInputElement>(".ew-card input[type=text]");
    inp.value = "  交叉验证 ";
    m.button("添加").click();
    await flush();
    expect(m.calls).toEqual([["editPath", "path_math", { op: "remove", index: 3 }], ["editPath", "path_math", { op: "add", title: "交叉验证" }]]);
    const last = m.all<HTMLElement>(".ew-pages li").pop()!;
    expect(last.querySelector(".ew-mine")!.textContent).toBe("你添加的");
    expect(last.querySelector(".ew-s")!.textContent).toBe("你加的，会在图谱里找到或新建对应的点");
    await m.view.onClose();
  });

  it("a failed edit puts the path back and says so", async () => {
    const m = await onMath({ fail: { editPath: true } });
    removeRow(m, 0);
    expect(m.pathTitles()).toHaveLength(3);
    await flush();
    expect(m.pathTitles()).toHaveLength(4);
    expect([...document.querySelectorAll(".ew-notice")].map((n) => n.textContent)).toContain("没改成，图谱还是原来的样子。");
    await m.view.onClose();
  });
});

describe("B5: a goal parked for the Bot shows 已搁置 · 恢复 on its path", () => {
  it("parked: the mark and 恢复, which resumes it; not parked or no source support: nothing", async () => {
    const m = make();
    const source = (m.view as unknown as { host: StarMapHost }).host.source;
    const resumed: string[] = [];
    source.parkedGoals = () => Promise.resolve(["path_math"]);
    source.setParked = (p, park) => { resumed.push(`${p}:${String(park)}`); return Promise.resolve(); };
    await m.open();
    m.goalButtons()[1]!.click();
    const mark = m.q<HTMLElement>("[data-parked]");
    expect(mark.textContent).toBe("已搁置");
    expect(mark.title).toBe("");
    m.button("恢复").click();
    await flush();
    expect(resumed).toEqual(["path_math:false"]);
    expect(m.view.contentEl.querySelector("[data-parked]")).toBeNull();
    m.goalButtons()[0]!.click();
    expect(m.view.contentEl.querySelector("[data-parked]")).toBeNull();
    await m.view.onClose();
    const plain = make();
    await plain.open();
    plain.goalButtons()[1]!.click();
    expect(plain.view.contentEl.querySelector("[data-parked]")).toBeNull();
    await plain.view.onClose();
  });
});

describe("keyboard / screen reader: the hidden node list", () => {
  it("one button per drawn node (name，band word), one Tab stop, arrows move, Enter opens the card and focuses it, Escape returns", async () => {
    const m = make();
    await m.open();
    const list = m.all<HTMLButtonElement>("ul.evowiki-sr-only [data-node]");
    expect(list.map((b) => b.dataset.node)).not.toContain("la");
    expect(list.map((b) => b.dataset.node)).toContain("py");
    expect(list.find((b) => b.dataset.node === "mul")!.textContent).toBe("矩阵乘法，熟悉");
    expect(list.find((b) => b.dataset.node === "cond")!.textContent).toBe("条件概率，还没学");
    expect(list.filter((b) => b.tabIndex === 0)).toHaveLength(1);
    list[0]!.focus();
    list[0]!.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
    expect(document.activeElement).toBe(list[1]);
    expect(list[1]!.tabIndex).toBe(0);
    list[1]!.click();
    expect(m.card()).not.toBeNull();
    expect(m.card()!.contains(document.activeElement)).toBe(true);
    (document.activeElement as HTMLElement).dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(m.card()).toBeNull();
    expect(document.activeElement).toBe(list[1]);
    await m.view.onClose();
  });
});

// ---- Task 14: path rows (⋯ menu, live status, 重新写), the new 看不懂, focus, split children ----

/** The graph with page ids on the rows of 我要学数学 (pg0…) and 我要学机器学习 (pm0…); `status` / `trashed` per row index. */
function withPages(g: GraphOut, o: { opened?: number[]; trashed?: number[] } = {}): GraphOut {
  const out = structuredClone(g);
  out.goals.forEach((goal) => goal.path.forEach((p, i) => {
    p.page_id = (goal.map_id === "map_math" ? "pg" : "pm") + i;
    if (goal.map_id === "map_math") { if (o.opened?.includes(i)) p.opened = true; if (o.trashed?.includes(i)) p.trashed = true; }
  }));
  return out;
}
/** A controllable stand-in for the shared watcher's snapshot. */
function fakeWrites() {
  let list: WriteOut[] = [];
  const fns = new Set<() => void>(), ends = new Map<string, WriteOut["state"]>(), looked: string[] = [];
  return { snapshot: () => list, subscribe: (fn: () => void) => { fns.add(fn); return () => { fns.delete(fn); }; },
    /** How a write ended (GET /wiki/writes?page_ids=): not in the live snapshot. */
    lookup: (ids: string[]): Promise<WriteOut[]> => { looked.push(...ids); return Promise.resolve(ids.flatMap((id) => (ends.has(id) ? [{ page_id: id, run_id: "r1", state: ends.get(id)!, kind: "organize", pass: null, section: null, of: null, started_at: null, cost_usd: 0.0 }] : []))); },
    end(id: string, state: WriteOut["state"]): void { ends.set(id, state); }, looked,
    set(next: WriteOut[]): void { list = next; for (const f of [...fns]) f(); }, subs: () => fns.size };
}
const rowStatus = (m: M, i: number): HTMLElement => m.all<HTMLLIElement>(".ew-pages li")[i]!.querySelector<HTMLElement>(".ew-w")!;
const notices = (): string[] => [...document.querySelectorAll(".ew-notice")].map((n) => n.textContent ?? "");
const noticeLink = (text: string): HTMLButtonElement => {
  const n = [...document.querySelectorAll(".ew-notice")].find((x) => x.textContent?.startsWith(text));
  if (!n) throw new Error(`no notice ${text}`);
  return n.querySelector<HTMLButtonElement>("button.ew-lk")!;
};

describe("Task 14: a path row's ⋯ menu", () => {
  async function onMath(o: Opts & { graph?: (g: GraphOut) => GraphOut } = {}): Promise<M> {
    const m: M = make({ load: () => Promise.resolve((o.graph ?? ((g: GraphOut) => g))(m.fx.graph())), ...o });
    await m.open();
    m.goalButtons()[1]!.click();
    return m;
  }

  it("重命名: the title box, 保存 → retitle; 撤销 sends the old title", async () => {
    const m = await onMath();
    const old = m.all(".ew-pages li .ew-t")[0]!.textContent;
    openMenu(m, 0);
    m.all<HTMLButtonElement>(".ew-menu button").find((b) => b.textContent === "重命名")!.click();
    const inp = m.all<HTMLInputElement>(".ew-pages li .ew-edit input")[0]!;
    expect(old).toContain(inp.value);
    inp.value = "新标题";
    m.all<HTMLButtonElement>(".ew-pages li .ew-edit button").find((b) => b.textContent === "保存")!.click();
    await new Promise((r) => { setTimeout(r, 0); });
    await new Promise((r) => { setTimeout(r, 0); });
    expect(m.all(".ew-pages li .ew-t")[0]!.textContent).toContain("新标题");
    const notice = [...document.querySelectorAll(".ew-notices .ew-notice")].pop()!;
    expect(notice.textContent).toContain("改了标题：「新标题」");
    notice.querySelector<HTMLElement>("button")!.click();
    await new Promise((r) => { setTimeout(r, 0); });
    await new Promise((r) => { setTimeout(r, 0); });
    expect(m.all(".ew-pages li .ew-t")[0]!.textContent).toBe(old);
  });

  it("the menu is 重命名 / 拆成多页 / 删除; Esc and a click elsewhere close it, aria-expanded follows", async () => {
    const m = await onMath();
    const menu = openMenu(m, 0);
    expect(menu.getAttribute("role")).toBe("menu");
    expect(m.all<HTMLButtonElement>(".ew-menu button").map((b) => [b.textContent, b.getAttribute("role")])).toEqual([["重命名", "menuitem"], ["拆成多页", "menuitem"], ["删除", "menuitem"]]);
    const more = m.all<HTMLLIElement>(".ew-pages li")[0]!.querySelector(".ew-more")!;
    expect(more.getAttribute("aria-expanded")).toBe("true");
    expect(m.all(".ew-menu")).toHaveLength(1);
    m.root().dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(m.all(".ew-menu")).toHaveLength(0);
    expect(m.all<HTMLLIElement>(".ew-pages li")[0]!.querySelector(".ew-more")!.getAttribute("aria-expanded")).toBe("false");
    expect(document.activeElement).toBe(m.all<HTMLLIElement>(".ew-pages li")[0]!.querySelector(".ew-more"));
    openMenu(m, 1);
    m.q(".ew-pages").dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(m.all(".ew-menu")).toHaveLength(0);
    openMenu(m, 0);
    m.all<HTMLLIElement>(".ew-pages li")[0]!.querySelector<HTMLButtonElement>(".ew-more")!.click();
    expect(m.all(".ew-menu")).toHaveLength(0);
    // ARIA menu: the focus goes to the first item, ArrowDown / Home move it, Esc in the menu goes back to ⋯
    openMenu(m, 0);
    await Promise.resolve();
    expect(document.activeElement?.textContent).toBe("重命名");
    document.activeElement!.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
    expect(document.activeElement?.textContent).toBe("拆成多页");
    document.activeElement!.dispatchEvent(new KeyboardEvent("keydown", { key: "Home", bubbles: true }));
    expect(document.activeElement?.textContent).toBe("重命名");
    document.activeElement!.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(m.all(".ew-menu")).toHaveLength(0);
    expect(document.activeElement).toBe(m.all<HTMLLIElement>(".ew-pages li")[0]!.querySelector(".ew-more"));
    await m.view.onClose();
  });

  it("拆成多页 is offered only for a row with a splittable node whose page was not opened", async () => {
    const m = await onMath({ graph: (g) => withPages(g, { opened: [0] }) });
    openMenu(m, 0); // vec + mul, but the server says its page was opened
    expect(m.all<HTMLButtonElement>(".ew-menu button").map((b) => b.textContent)).toEqual(["重命名", "删除"]);
    openMenu(m, 1); // eig: one node that cannot split
    expect(m.all<HTMLButtonElement>(".ew-menu button").map((b) => b.textContent)).toEqual(["重命名", "删除"]);
    await m.view.onClose();
  });

  it("删除 sends the remove at once and offers 撤销, which sends the unremove (the very row again where it was)", async () => {
    const m = await onMath();
    removeRow(m, 1);
    expect(m.pathTitles()).toEqual(["向量和矩阵乘法", "导数和梯度", "条件概率"]);
    await flush();
    expect(notices()).toContain("路径里删掉了「特征值与特征向量」撤销");
    noticeLink("路径里删掉了").click();
    await flush();
    expect(m.calls).toEqual([["editPath", "path_math", { op: "remove", index: 1 }],
      ["editPath", "path_math", { op: "unremove", page_key: "path_math_2" }]]);
    expect(m.pathTitles()).toEqual(["向量和矩阵乘法", "特征值与特征向量", "导数和梯度", "条件概率"]);
    await m.view.onClose();
  });

  it("a failed 删除 says so and puts the row back without a 撤销 notice", async () => {
    const m = await onMath({ fail: { editPath: true } });
    removeRow(m, 0);
    await flush();
    expect(m.pathTitles()).toHaveLength(4);
    expect(notices()).toEqual(["没改成，图谱还是原来的样子。"]);
    await m.view.onClose();
  });

  it("拆成多页 on a row of several nodes: one row per node, the new rows rise in, 撤销 puts the row back", async () => {
    const m = await onMath();
    expect(m.all(".ew-pages li.ew-fresh")).toHaveLength(0); // the first drawing of a path does not animate
    openMenu(m, 0); menuItem(m, "拆成多页").click();
    expect(m.all(".ew-menu")).toHaveLength(0);
    await flush();
    expect(m.calls[0]).toEqual(["splitRow", "path_math", "path_math_1"]);
    expect(m.pathTitles()).toEqual(["向量和点积", "矩阵乘法", "特征值与特征向量", "导数和梯度", "条件概率"]);
    expect(m.all<HTMLElement>(".ew-pages li.ew-fresh").map((li) => li.querySelector(".ew-t")!.textContent)).toEqual(["向量和点积", "矩阵乘法"]);
    expect(notices()).toContain("「向量和矩阵乘法」拆成了 2 页撤销");
    noticeLink("「向量和矩阵乘法」拆成了").click();
    await flush();
    expect(m.calls[1]![0]).toBe("undoSplitRow");
    expect(m.pathTitles()).toEqual(["向量和矩阵乘法", "特征值与特征向量", "导数和梯度", "条件概率"]);
    await m.view.onClose();
  });

  it("拆成多页 on a one-node row grows the node's parts on the map (linked to the parent) and frames them", async () => {
    const m = make();
    await m.open();
    m.goalButtons()[0]!.click();
    openMenu(m, 0); menuItem(m, "拆成多页").click();
    await flush();
    expect(m.pathTitles().slice(0, 3)).toEqual(["一行乘一列", "维度要对上", "不满足交换律"]);
    for (const k of ["rc", "dim", "comm"]) expect(sigma().graph.hasEdge("mul", k)).toBe(true);   // grown out of 矩阵乘法
    expect(sigma().camera.ratio).toBeLessThan(1);                                                   // and framed
    await m.view.onClose();
  });
});

describe("Task 14: a row's live status (the shared writes watcher)", () => {
  async function onMath(o: Opts = {}) {
    const w = fakeWrites();
    const m: M = make({ load: () => Promise.resolve(withPages(m.fx.graph(), { trashed: [3] })), writes: w, ...o });
    await m.open();
    m.goalButtons()[1]!.click();
    return { m, w };
  }
  const w = (id: string, state: WriteOut["state"], o: Partial<WriteOut> = {}): WriteOut => ({ page_id: id, run_id: "r1", kind: "organize", state, pass: null, section: null, of: null, started_at: null, cost_usd: 0, ...o });

  it("subscribes to the watcher's snapshot (no poll of its own) and unsubscribes on close", async () => {
    const { m, w: fw } = await onMath();
    expect(fw.subs()).toBe(1);
    await m.view.onClose();
    expect(fw.subs()).toBe(0);
  });

  it("queued → 排队写; planning → 正在规划… n 秒 counted from the first planning seen; writing → pulse + 正在写 · n/m 段; done → nothing", async () => {
    const { m, w: fw } = await onMath();
    expect(rowStatus(m, 0).hidden).toBe(true);
    fw.set([w("pg0", "queued")]);
    expect([rowStatus(m, 0).hidden, rowStatus(m, 0).textContent, rowStatus(m, 0).querySelector("i")]).toEqual([false, "排队中", null]);
    fw.set([w("pg0", "planning", { pass: 1 })]);
    expect(rowStatus(m, 0).textContent).toBe("正在规划…");
    expect(rowStatus(m, 0).querySelector("i")).not.toBeNull();
    clock += 47_000;
    fw.set([w("pg0", "planning", { pass: 2 })]); // a later pass does not restart the count
    expect(rowStatus(m, 0).textContent).toBe("正在规划… 47 秒");
    fw.set([w("pg0", "writing", { section: 1, of: 4 })]);
    expect(rowStatus(m, 0).textContent).toBe("生成中 2/4");
    expect(rowStatus(m, 0).querySelector("i")).not.toBeNull();
    fw.set([w("pg0", "done")]);
    expect(rowStatus(m, 0).hidden).toBe(true);
    fw.set([w("pg0", "planning")]);
    expect(rowStatus(m, 0).textContent).toBe("正在规划…"); // a new write counts from its own start
    await m.view.onClose();
  });

  it("only the status span is painted: the row, its menu and an open input are not rebuilt", async () => {
    const { m, w: fw } = await onMath();
    const li = m.all<HTMLLIElement>(".ew-pages li")[0]!;
    const inp = m.q<HTMLInputElement>(".ew-card input[type=text]");
    inp.value = "交叉验"; inp.dispatchEvent(new Event("input"));
    fw.set([w("pg0", "writing", { section: 0, of: 3 })]);
    fw.set([w("pg0", "writing", { section: 1, of: 3 })]);
    expect(m.all<HTMLLIElement>(".ew-pages li")[0]).toBe(li);
    expect(m.q<HTMLInputElement>(".ew-card input[type=text]")).toBe(inp);
    expect(inp.value).toBe("交叉验");
    expect(rowStatus(m, 0).textContent).toBe("生成中 2/3");
    await m.view.onClose();
  });

  it("a row page seen live that ends failed (read with the free lookup, never in the snapshot) → 生成失败 · 重试, which asks for the page again", async () => {
    const { m, w: fw } = await onMath();
    fw.set([w("pg1", "writing", { section: 0, of: 2 })]);
    fw.end("pg1", "failed");
    fw.set([]);
    await flush();
    expect(fw.looked).toEqual(["pg1"]);
    expect(rowStatus(m, 1).textContent).toBe("生成失败 · 重试");
    rowStatus(m, 1).querySelector<HTMLButtonElement>("button")!.click();
    expect(m.writePage).toHaveBeenCalledWith("pg1", "特征值与特征向量", "我要学数学");
    await m.view.onClose();
  });

  it("a page in the trash shows 重新写 (a write request), never opens as a normal page", async () => {
    const { m } = await onMath();
    const st = rowStatus(m, 3);
    expect(st.hidden).toBe(false);
    expect(st.textContent).toBe("重新生成");
    st.querySelector<HTMLButtonElement>("button")!.click();
    expect(m.writePage).toHaveBeenCalledWith("pg3", "条件概率", "我要学数学");
    expect(m.showPage).not.toHaveBeenCalled();
    await m.view.onClose();
  });
});

describe("Task 14: focusing a goal (the goal buttons and the 今天 message)", () => {
  const post = (data: unknown): void => { window.dispatchEvent(new MessageEvent("message", { data })); };
  it("{evowiki: 'focus', goal} selects the goal (clears the selection and the menu), frames it and shows its path", async () => {
    const m = make();
    await m.open();
    m.pickNode("py");
    post({ evowiki: "focus", goal: "map_math" });
    expect(m.view.focusGoal).toBe("map_math");
    expect(m.view.selectedNode).toBeNull();
    framedOn(MATH);
    expect(m.pathTitles()).toHaveLength(4);
    post({ evowiki: "focus", goal: null });
    expect(m.view.focusGoal).toBeNull();
    post({ evowiki: "focus", goal: "nope" });
    expect(m.view.focusGoal).toBeNull();
    await flush();
    expect(m.view.focusGoal).toBeNull();                                 // the re-read does not have it either
    post({ theme: "dark" }); // not ours
    await m.view.onClose();
  });

  it("a focus asked for before the graph arrived is applied when it is there", async () => {
    let done!: (v: unknown) => void;
    const fx = new GraphFixture();
    const m = make({ load: () => new Promise((r) => { done = r; }) });
    await m.view.onOpen();
    post({ evowiki: "focus", goal: "map_math" });
    done(fx.graph());
    await flush();
    expect(m.view.focusGoal).toBe("map_math");
    framedOn(MATH);
    await m.view.onClose();
  });

  it("bug 2026-10-24: a goal made after the page read its graph (今天's 蛋白结合基础) is read again and focused, never another goal", async () => {
    const fx = new GraphFixture();
    const old = fx.graph();
    const fresh: GraphOut = { ...old, goals: [...old.goals, { ...old.goals[1]!, map_id: "map_new", path_id: null, name: "蛋白结合基础", nodes: [], path: [] }] };
    let reads = 0;
    const m = make({ load: () => Promise.resolve(++reads === 1 ? old : fresh) });
    await m.open();
    m.goalButtons()[0]!.click();                                         // another goal was in focus
    post({ evowiki: "focus", goal: "map_new" });
    expect(m.view.focusGoal).toBeNull();                                 // not the old one meanwhile
    await flush();
    expect(m.load).toHaveBeenCalledTimes(2);
    expect(m.view.focusGoal).toBe("map_new");
    // a goal without points or path yet is still listed (and pressed)
    expect(m.goalButtons().map((b) => b.querySelector(".ew-gname")!.textContent)).toContain("蛋白结合基础");
    expect(m.goalButtons().find((b) => b.textContent.includes("蛋白结合基础"))!.getAttribute("aria-pressed")).toBe("true");
    await m.view.onClose();
  });

  it("hovering a primary button keeps it primary (styles.css)", () => {
    const css = readFileSync(join(process.cwd(), "styles.css"), "utf8").replace(/\r\n/g, "\n");
    expect(css).toContain("body .ew-graph .ew-btn.ew-primary:hover:not(:disabled){filter:brightness(1.06);background:var(--accent)}");
    expect(css).not.toContain("ew-primary:hover:not(:disabled){filter:none");
  });
});

// ---- Task 14 fix round 1 ----

describe("Task 14 fixes: lookups, opened, minors", () => {
  it("several pages leaving the live list are looked up in one call", async () => {
    const fx = new GraphFixture();
    const writes = fakeWrites();
    const m: M = make({ load: () => Promise.resolve(withPages(m.fx.graph())), writes });
    void fx;
    await m.open();
    writes.set([{ page_id: "pm0", run_id: "r1", state: "writing", section: 0, of: 2, kind: "organize", pass: null, started_at: null, cost_usd: 0.0 }, { page_id: "pm1", run_id: "r1", state: "queued", kind: "organize", pass: null, section: null, of: null, started_at: null, cost_usd: 0.0 }, { page_id: "pm2", run_id: "r1", state: "queued", kind: "organize", pass: null, section: null, of: null, started_at: null, cost_usd: 0.0 }]);
    writes.set([]);
    await flush();
    expect(writes.looked).toEqual(["pm0", "pm1", "pm2"]);
    await m.view.onClose();
  });

  it("a row shows its `question` and never the writer-only `summary`; a row without a question has no line", async () => {
    const m: M = make({ load: () => Promise.resolve((() => { const g = withPages(m.fx.graph()); g.goals[1]!.path[0]!.summary = "先用拉伸的例子再给公式"; g.goals[1]!.path[1]!.question = ""; return g; })()) });
    await m.open();
    m.goalButtons()[1]!.click();
    const lis = m.all<HTMLLIElement>(".ew-pages li");
    expect(lis[0]!.querySelector(".ew-s")!.textContent).toBe("两个箭头有多同向，两张表怎么相乘");
    expect(lis[0]!.textContent).not.toContain("先用");
    expect(lis[1]!.querySelector(".ew-s")).toBeNull();
    await m.view.onClose();
  });

  it("拆成多页 hides by the server's `opened` of the row's page", async () => {
    const m: M = make({ load: () => Promise.resolve((() => { const g = withPages(m.fx.graph()); g.goals[0]!.path[0]!.opened = true; return g; })()) });
    await m.open();
    m.goalButtons()[0]!.click();
    openMenu(m, 0);
    expect(m.all<HTMLButtonElement>(".ew-menu button").map((b) => b.textContent)).toEqual(["重命名", "删除"]);
    await m.view.onClose();
    const n: M = make({ load: () => Promise.resolve(withPages(n.fx.graph())) });
    await n.open();
    n.goalButtons()[0]!.click();
    openMenu(n, 0);
    expect(n.all<HTMLButtonElement>(".ew-menu button").map((b) => b.textContent)).toEqual(["重命名", "拆成多页", "删除"]);
    await n.view.onClose();
  });
  it("Enter in the bottom input adds the page", async () => {
    const m = make();
    await m.open();
    m.goalButtons()[1]!.click();
    const inp = m.q<HTMLInputElement>(".ew-card input[type=text]");
    inp.value = "交叉验证";
    inp.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" }));
    await flush();
    expect(m.calls).toEqual([["editPath", "path_math", { op: "add", title: "交叉验证" }]]);
    await m.view.onClose();
  });

  it("the split notice counts the rows that replaced the row; the row says it is being split meanwhile (nothing else greyed)", async () => {
    let release!: () => void;
    const m = make({ splitGate: new Promise<void>((r) => { release = r; }) });
    await m.open();
    m.goalButtons()[0]!.click();
    openMenu(m, 0); menuItem(m, "拆成多页").click();
    await flush();
    const st = m.all<HTMLLIElement>(".ew-pages li")[0]!.querySelector<HTMLElement>(".ew-w")!;
    expect([st.hidden, st.textContent, st.querySelector("i") !== null]).toEqual([false, "正在拆成多页…", true]);
    expect(m.all<HTMLButtonElement>(".ew-panel button").some((b) => b.disabled)).toBe(false);
    release();
    await flush();
    expect(notices()).toContain("「矩阵乘法」拆成了 3 页撤销");
    expect(m.all<HTMLLIElement>(".ew-pages li")[0]!.querySelector<HTMLElement>(".ew-w")!.hidden).toBe(true);
    await m.view.onClose();
  });

  it("a focus message from another window is ignored", async () => {
    const m = make();
    await m.open();
    window.dispatchEvent(new MessageEvent("message", { data: { evowiki: "focus", goal: "map_math" }, source: {} as MessageEventSource }));
    expect(m.view.focusGoal).toBeNull();
    window.dispatchEvent(new MessageEvent("message", { data: { evowiki: "focus", goal: "map_math" }, source: window }));
    expect(m.view.focusGoal).toBe("map_math");
    await m.view.onClose();
  });
});

describe("regionWord (学点新的's region tip)", () => {
  it("average SCORE of the bands with evidence: > 0.65 掌握, > 0.3 熟悉, else 不会; nothing without evidence", () => {
    expect(regionWord(["known", "none"])).toBe("掌握");
    expect(regionWord(["likely", "shaky"])).toBe("熟悉");
    expect(regionWord(["shaky", "gap"])).toBe("不会");
    expect(regionWord(["none", "none"])).toBe("");
  });
});

describe("EvoWikiPlugin.openStarMap", () => {
  function plugin(existing: object[]) {
    const p = new EvoWikiPlugin(...([] as unknown as ConstructorParameters<typeof EvoWikiPlugin>));
    const newLeaf = { setViewState: vi.fn(() => Promise.resolve()) };
    const getLeaf = vi.fn(() => newLeaf);
    const revealLeaf = vi.fn(() => Promise.resolve());
    Object.assign(p, { app: { workspace: { getLeavesOfType: vi.fn(() => existing), getLeaf, revealLeaf } } });
    return { p, newLeaf, getLeaf, revealLeaf };
  }
  it("opens a main-area tab once, then focuses it instead of opening a second", async () => {
    const a = plugin([]);
    await a.p.openStarMap();
    expect(a.getLeaf).toHaveBeenCalledWith("tab");
    expect(a.newLeaf.setViewState).toHaveBeenCalledWith({ type: STARMAP_VIEW_TYPE, active: true });
    const existing = {};
    const b = plugin([existing]);
    await b.p.openStarMap();
    expect(b.getLeaf).not.toHaveBeenCalled();
    expect(b.revealLeaf).toHaveBeenCalledWith(existing);
  });
});

describe("materials: § on path rows and the coverage line", () => {
  type Src = { title: string; deleted: boolean; uncovered: string[] };
  const withSources = (sources: Src[], sections: string[] = ["§3.2"]) => {
    const g = new GraphFixture().graph();
    g.goals[0]!.path[0]!.sections = sections;
    g.goals[0]!.sources = sources.map((s, i) => ({ id: `s${i}`, ...s }));
    return g;
  };
  const focus = async (m: M): Promise<void> => { await m.open(); m.goalButtons()[0]!.click(); };

  it("a path row shows its § labels after the title", async () => {
    const m = make({ load: () => Promise.resolve(withSources([], ["§3.2", "§4"])) });
    await focus(m);
    expect(m.all(".ew-pages li")[0]!.querySelector(".ew-sec")?.textContent).toBe("§3.2 §4");
    expect(m.all(".ew-pages li")[1]!.querySelector(".ew-sec")).toBeNull();
  });
  it("真题: a 常考 row (exam > 0) shows the tag after its § and a ? 「有 N 道往年题考到这里」; exam 0 shows none", async () => {
    const g = withSources([], ["§3.2"]);
    g.goals[0]!.path[0]!.exam = 4;
    const m = make({ load: () => Promise.resolve(g) });
    await focus(m);
    const [r0, r1] = m.all(".ew-pages li");
    expect(r0!.querySelector(".ew-sec + .ew-exam")?.textContent).toBe("常考");
    expect(r0!.querySelector(".ew-exam + .ew-qtip .ew-tipbox")!.textContent).toBe("有 4 道往年题考到这里");
    expect(r0!.querySelector(".ew-exam + .ew-qtip .ew-qbtn")!.querySelector(".evowiki-sr-only")?.textContent).toBe("常考");
    expect(r1!.querySelector(".ew-exam")).toBeNull();
  });
  it("a 读懂 row whose title already says its label shows it once (final review #6)", async () => {
    const g = withSources([], ["§摘要", "§2"]);
    g.goals[0]!.path[0]!.title = "《稀疏注意力论文》§摘要";
    const m = make({ load: () => Promise.resolve(g) });
    await focus(m);
    const row = m.all(".ew-pages li")[0]!;
    expect(row.querySelector(".ew-t")!.firstChild?.textContent).toBe("《稀疏注意力论文》§摘要");
    expect(row.querySelector(".ew-sec")?.textContent).toBe("§2");
  });
  it("the last line lists what the material has no page for, and a deleted material by name", async () => {
    const m = make({ load: () => Promise.resolve(withSources([
      { title: "讲义", deleted: false, uncovered: ["前言", "附录 B"] }, { title: "旧笔记", deleted: true, uncovered: [] }])) });
    await focus(m);
    expect(m.q("[data-cover]").textContent).toBe("资料里这几节没讲：前言、附录 B、旧笔记（已删除）");
  });
  it("no line when nothing is uncovered and nothing is deleted", async () => {
    const m = make({ load: () => Promise.resolve(withSources([{ title: "讲义", deleted: false, uncovered: [] }])) });
    await focus(m);
    expect(m.all("[data-cover]")).toHaveLength(0);
  });
});
