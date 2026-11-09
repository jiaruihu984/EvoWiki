import { afterEach, describe, expect, it, vi } from "vitest";
import { GraphFixture } from "../graph-page/fixture";
import { GraphStore, type GraphSource } from "../graph-page/store";
import type { GoalOut, GoalProgressOut, GraphOut, PathItemOut, ProgressOut, StepOut } from "../schema";
import { GoalProgress, progressOf } from "./goal-progress";

afterEach(() => { document.body.replaceChildren(); off.v = false; });
const flush = async (): Promise<void> => { for (let i = 0; i < 8; i++) await Promise.resolve(); };

const row = (key: string, o: Partial<PathItemOut> = {}): PathItemOut => ({
  key, title: "T" + key, summary: "", question: "", node_ids: [], status: "not_started", state: "written", progress: null, skipped: false, mine: false, page_id: "p" + key,
  trashed: false, skippable: false, opened: false, sections: [], exam: 0, ...o,
});

const step = (kind: StepOut["kind"], o: Partial<StepOut> = {}): StepOut => ({ kind, goal_id: null, goal: "", map_id: null, region_id: null,
  region_name: "", path_id: null, index: null, page_id: null, title: "", draft_id: null, progress: null, failure: null, review_first: null, ...o });
/** The server's progress for these goals (wikiapp/progress.py): a path's step is its first row not read. */
function progressFor(goals: GoalOut[], over: Record<string, Partial<GoalProgressOut>> = {}): ProgressOut {
  const out = goals.map((g): GoalProgressOut => {
    const i = g.path.findIndex((r) => r.state !== "read");
    const r = g.path[i];
    const st = g.path.length === 0 ? step("none") : r === undefined ? step("goal_done") : step("read", { goal_id: g.map_id, page_id: r.page_id, title: r.title, index: i + 1 });
    return { goal_id: g.map_id, kind: "map", goal: g.name, state: st.kind === "goal_done" ? "done" : i > 0 ? "learning" : "not_started", blocks: [],
      path_ids: g.path_id === null ? [] : [g.path_id], pages_done: Math.max(0, i < 0 ? g.path.length : i), pages_total: g.path.length, step: st, ...over[g.map_id] };
  });
  return { today: out[0]?.step ?? step("none"), goals: out };
}

const off = { v: false };
function make(graph: () => GraphOut, o: { fail?: boolean; offline?: boolean; over?: Record<string, Partial<GoalProgressOut>> } = {}) {
  let n = 0;
  const source = { demo: false, isOffline: () => o.offline === true, load: () => { n++; return o.fail ? Promise.reject(new Error("x")) : Promise.resolve(graph()); } } as unknown as GraphSource;
  const host = { source: () => source, openGoal: vi.fn(), progress: () => progressFor(graph().goals, o.over), followStep: vi.fn(), offline: () => off.v };
  let t = 1000;
  const gp = new GoalProgress(host, () => t);
  document.body.append(gp.root, gp.line);
  return { gp, host, loads: () => n, tick: (ms: number) => { t += ms; } };
}

describe("progressOf: a graph goal's server progress", () => {
  it("by its map id, or by its lone path", () => {
    const g = new GraphFixture().graph().goals[0]!;
    const p = progressFor([g]);
    expect(progressOf(p, g)?.goal_id).toBe(g.map_id);
    expect(progressOf({ ...p, goals: [{ ...p.goals[0]!, goal_id: "other", path_ids: [g.path_id ?? ""] }] }, g)?.goal_id).toBe("other");
    expect(progressOf(null, g)).toBeUndefined();
  });
});

describe("GoalProgress (app.js goalsBlock)", () => {
  it("div.gls > div.gl > button.glb (aria, .gt, .gbar with flexed segments) + div.gw (第 a / n 页 + button.gnext)", async () => {
    const g = new GraphFixture().graph();
    const m = make(() => g);
    expect(m.loads()).toBe(0);
    m.gp.sync();
    await flush();
    expect(m.gp.root.className).toBe("ew-gls");
    // newest first: the server's order (creation) reversed
    expect([...m.gp.root.querySelectorAll(".ew-gt")].map((e) => e.textContent)).toEqual(g.goals.map((x) => x.name).reverse());   // the topic, not the sentence
    const goal = g.goals[g.goals.length - 1]!;
    const gl = m.gp.root.children[0] as HTMLElement;
    expect(gl.className).toBe("ew-gl");
    const glb = gl.querySelector<HTMLButtonElement>("button.ew-glb")!;
    expect(glb.getAttribute("aria-label")).toBe(`打开「${goal.name}」的学习路径`);
    expect(glb.querySelector(".ew-gt")!.textContent).toBe(goal.name);
    expect(glb.querySelector<HTMLElement>(".ew-gt")!.title).toBe("");   // no hover tooltip
    const segs = [...glb.querySelectorAll<HTMLElement>(".ew-gbar > i")];
    expect(segs.length).toBeGreaterThan(0);
    expect(glb.textContent).toBe(goal.name);          // no numbers
    const gw = gl.querySelector(".ew-gw")!;
    expect(gw.querySelector<HTMLButtonElement>("button.ew-gnext")!.textContent).toBe(goal.path[0]!.title);
    glb.click();
    expect(m.host.openGoal).toHaveBeenCalledWith(goal.map_id);
  });

  it("the row names the goal as progress has it (the map's title), not the graph goal's own name", async () => {
    const g = new GraphFixture().graph();
    const goal = g.goals[g.goals.length - 1]!;
    const m = make(() => g, { over: { [goal.map_id]: { goal: "复杂网络中的渗流" } } });
    m.gp.sync();
    await flush();
    const glb = (m.gp.root.children[0] as HTMLElement).querySelector<HTMLButtonElement>("button.ew-glb")!;
    expect(glb.querySelector(".ew-gt")!.textContent).toBe("复杂网络中的渗流");
    expect(glb.getAttribute("aria-label")).toBe("打开「复杂网络中的渗流」的学习路径");
  });

  it("each row renders the server's step: the page being read (opening it is not finishing it), the next block, 全部学完了", async () => {
    const g = new GraphFixture().graph();
    g.goals = [
      { ...g.goals[0]!, map_id: "a", path_id: "pa", path: [row("1", { status: "done", state: "read" }), row("2", { state: "written" })] },
      { ...g.goals[0]!, map_id: "b", path_id: "pb", path: [row("1", { status: "done", state: "read" })] },
      { ...g.goals[0]!, map_id: "c", path_id: "pc", path: [row("1", { status: "done", state: "read" })] },
    ];
    const m = make(() => g, { over: { b: { step: step("enter_block", { map_id: "b", region_id: "r2", region_name: "水质与环境" }) } } });
    m.gp.sync(); await flush();
    const rows = ([...m.gp.root.children] as HTMLElement[]).reverse();     // shown newest first
    rows[0]!.querySelector<HTMLButtonElement>(".ew-gnext")!.click();
    expect(m.host.followStep).toHaveBeenCalledWith(expect.objectContaining({ kind: "read", page_id: "p2" }));
    expect(rows[0]!.querySelector(".ew-gw")!.textContent).not.toContain(" / ");
    expect(rows[1]!.querySelector(".ew-gnext")!.textContent).toBe("下一块：水质与环境 →");
    rows[1]!.querySelector<HTMLButtonElement>(".ew-gnext")!.click();
    expect(m.host.followStep).toHaveBeenLastCalledWith(expect.objectContaining({ kind: "enter_block", region_id: "r2" }));
    expect(rows[2]!.querySelector(".ew-gw")!.textContent).toBe("全部学完了");
    expect(rows[2]!.querySelector(".ew-gnext")).toBeNull();
  });

  it("reads once when shown, again only when old or stale", async () => {
    const g = new GraphFixture().graph();
    const m = make(() => g);
    m.gp.sync(); await flush(); m.gp.sync(); await flush();
    expect(m.loads()).toBe(1);
    m.tick(30_000); m.gp.sync(); await flush();
    expect(m.loads()).toBe(2);
    m.gp.markStale(); m.gp.sync(); await flush();
    expect(m.loads()).toBe(3);
  });

  it("a failed read online: 没能读到，重试 in this block only (重试 re-reads); offline: nothing at all", async () => {
    const g = new GraphFixture().graph();
    const m = make(() => g, { fail: true });
    m.gp.sync(); await flush();
    expect(m.gp.line.hidden).toBe(false);
    expect(m.gp.line.textContent).toBe("没能读到，重试");
    m.gp.line.querySelector<HTMLButtonElement>("[data-action=retry]")!.click();
    await flush();
    expect(m.loads()).toBe(2);
    document.body.replaceChildren();
    const off = make(() => g, { fail: true, offline: true });
    off.gp.sync(); await flush();
    expect(off.gp.line.hidden).toBe(true);
    expect(off.gp.root.childElementCount).toBe(0);
  });

  it("a goal with no path yet says 还没开始, not 全部学完了", async () => {
    const g = new GraphFixture().graph();
    g.goals = [{ ...g.goals[0]!, path: [] }];
    const m = make(() => g);
    m.gp.sync(); await flush();
    expect(m.gp.root.querySelector(".ew-gw")!.textContent).toBe("还没开始");
  });

  it("offline hides the rows even after a good read; back online shows them again", async () => {
    const g = new GraphFixture().graph();
    const m = make(() => g);
    m.gp.sync(); await flush();
    expect(m.gp.root.childElementCount).toBeGreaterThan(0);
    off.v = true; m.gp.sync();
    expect(m.gp.root.childElementCount).toBe(0);
    off.v = false; m.gp.sync();
    expect(m.gp.root.childElementCount).toBeGreaterThan(0);
  });
});

describe("the rows follow graph changes at once (no 20 s wait)", () => {
  it("an edit on the 知识图谱 page (its announcing store) reads the graph again for 今天", async () => {
    const fx = new GraphFixture();
    const m = make(() => fx.graph());
    m.gp.sync(); await flush();
    expect(m.loads()).toBe(1);
    const pageSource = { demo: false, isOffline: () => false, load: () => Promise.resolve(fx.graph()),
      editPath: (p: string, e: never) => Promise.resolve(fx.editPath(p, e)) } as unknown as GraphSource;
    const page = new GraphStore(pageSource, true);
    await page.refresh();
    await flush();
    expect(m.loads()).toBe(2);                         // the page's first read tells 今天 too
    await page.editPath("path_ml", { op: "remove", index: 0 });
    await flush();
    expect(m.loads()).toBe(3);                         // within the freshness window: no tick() at all
    const quiet = new GraphStore(pageSource);          // a store that does not announce (today's own) never loops
    await quiet.refresh(); await flush();
    expect(m.loads()).toBe(3);
  });

  it("a changed stamp (ledger action applied / undone, a write ended) reads again; the same stamp does not", async () => {
    const fx = new GraphFixture();
    let stamp = "a", fire = (): void => undefined;
    let n = 0;
    const source = { demo: false, isOffline: () => false, load: () => { n++; return Promise.resolve(fx.graph()); } } as unknown as GraphSource;
    const gp = new GoalProgress({ source: () => source, openGoal: vi.fn(), progress: () => null, followStep: vi.fn(), stamp: () => stamp, onChange: (fn) => { fire = fn; return () => undefined; } }, () => 1000);
    document.body.append(gp.root);
    gp.sync(); await flush();
    expect(n).toBe(1);
    fire(); await flush();
    expect(n).toBe(1);
    stamp = "b"; fire(); await flush();
    expect(n).toBe(2);
    fire(); await flush();
    expect(n).toBe(2);
    gp.destroy();
  });
});
