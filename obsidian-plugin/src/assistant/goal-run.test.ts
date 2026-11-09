import { afterEach, describe, expect, it, vi } from "vitest";
import { FakeGoalApi, fakeBlocks, registerBlockGoal } from "../wiki/learn/goal-fake";
import type { GoalApi } from "../wiki/learn/goal-api";
import type { GoalDraftOut, GoalMapOut, MapNodeOut, PathOut, ProgressOut, StepOut, WriteOut } from "../wiki/schema";
import { GoalRun, GOAL_RUN_COPY, WRITING_POLL_MS } from "./goal-run";
import { buildGoalRunCard } from "./goal-run-card";
import { DIAGNOSED, NESTED_MAP, NestedGoalApi } from "../wiki/learn/nested-goal-kit";
import { READING_ID, ReadingGoalApi } from "../wiki/learn/reading-goal-kit";

const flush = async (n = 30): Promise<void> => { for (let i = 0; i < n; i++) await new Promise((r) => setImmediate(r)); await new Promise((r) => setTimeout(r, 0)); };

/** A goal confirmed in the fixture's chat (its blocks as the assistant proposed them), read as the run reads it. */
async function confirmedGoal(api: FakeGoalApi): Promise<{ draftId: string | null; mapId: string | null }> {
  const out = await api.getDraft(registerBlockGoal("我要学黑壳虾", fakeBlocks("我要学黑壳虾")).draftId);
  return { draftId: out.draft_id, mapId: out.map_id };
}

describe("the diagnosis asked in the chat (stage 3 T11)", () => {
  it("a round with no question starts one diagnose turn, then reads the draft again and goes on", async () => {
    const api = new FakeGoalApi({ roundMs: 0, splitMs: 0 });
    const result = await confirmedGoal(api);
    const real = api.getDraft.bind(api);
    let asked = false;
    // the server's round: the pick only (no question) until the chat has asked it; then the goal's map
    vi.spyOn(api, "getDraft").mockImplementation(async (id: string) => {
      const out = await real(id);
      return asked ? { ...out, draft_id: null, questions: [], map_id: result.mapId } : { ...out, questions: [] };
    });
    const diagnose = vi.fn(() => { asked = true; return Promise.resolve(); });
    const run = new GoalRun({ api, writeFirstPage: () => Promise.resolve(), changed: () => {}, diagnose }, "g1", result, true);
    await run.start(); await flush(60);
    expect(diagnose).toHaveBeenCalledTimes(1);
    expect(diagnose).toHaveBeenCalledWith(result.draftId);
    expect(run.flow.state.draftId).toBeNull();          // read again after the chat turn: the diagnosis ended
    run.dispose();
  });
});

describe("a goal confirmed in the chat goes on in the chat (goal-run.ts)", () => {
  it("shows the diagnosis one question at a time; 跳过 plans, enters the next block and writes the first page, then links it", async () => {
    const api = new FakeGoalApi({ roundMs: 0, splitMs: 0 });
    const result = await confirmedGoal(api);
    const changed = vi.fn(), write = vi.fn((_t: unknown) => Promise.resolve());
    const next = vi.spyOn(api, "pathNext");
    const run = new GoalRun({ api, writeFirstPage: write, changed }, "g1", result, true);
    await run.start(); await flush();
    const v = run.view();
    expect(v.kind).toBe("round");
    if (v.kind !== "round") return;
    expect(v.question).toBe(0);
    const card = buildGoalRunCard(run, { pick: () => {}, skip: () => {}, retry: () => {}, showPage: () => {} })!;
    expect(card.querySelector(".ew-stem")!.textContent).toBe(v.round.questions[0]!.stem);
    expect(card.textContent).toContain(GOAL_RUN_COPY.skip);
    run.pick(0);                                                   // the next question of the same round
    const v2 = run.view();
    expect(v2.kind === "round" && v2.question).toBe(1);
    run.skip(); await flush(60);
    // the opening diagnosis ended: the map's next block was entered and its path planned; the first page written (the fake
    // API is not live: pathNext), and the card links it
    expect(run.flow.state.entered).not.toBeNull();
    expect(next).toHaveBeenCalledTimes(1);
    expect(write).not.toHaveBeenCalled();
    expect(run.view().kind).toBe("written");
    run.dispose();
  });

  it("a live API writes the first page through 学点新的's 生成第一页; no page afterwards is a failure with 重试", async () => {
    const api = new FakeGoalApi({ roundMs: 0, splitMs: 0 });
    Object.defineProperty(api, "live", { value: true });
    const result = await confirmedGoal(api);
    const write = vi.fn((_t: unknown) => Promise.resolve());
    const run = new GoalRun({ api, writeFirstPage: write, changed: () => {} }, "g1", result, true);
    await run.start(); await flush();
    run.skip(); await flush(60);
    expect(write).toHaveBeenCalledTimes(1);
    expect(typeof (write.mock.calls[0]![0] as { pathId: unknown }).pathId).toBe("string");
    // the fake path has no written page: fail closed, not a link to nothing
    expect(run.view()).toEqual({ kind: "failed", text: GOAL_RUN_COPY.notWritten });
    // 重试 writes it again: the write the entering started is the step 重试 runs (entering finished first and must not
    // drop it — the real-Obsidian 重试 that did nothing)
    run.retry(); await flush(60);
    expect(write).toHaveBeenCalledTimes(2);
    run.dispose();
  });

  it("停止 while the first page is written: 已停止 · 重新写 on the card; 重新写 writes it again, then links it", async () => {
    const api = new FakeGoalApi({ roundMs: 0, splitMs: 0 });
    Object.defineProperty(api, "live", { value: true });
    const result = await confirmedGoal(api);
    let made = false;
    const readPath = api.getPath.bind(api);                     // the path's first page has a page id once its write began
    vi.spyOn(api, "getPath").mockImplementation(async (id: string) => {
      const out = await readPath(id);
      return made ? { ...out, pages: out.pages.map((x, i) => (i === 0 ? { ...x, page_id: "pg1" } : x)) } : out;
    });
    let stopped: string | null = null;
    let release: () => void = () => {};
    const write = vi.fn(() => new Promise<void>((r) => { made = true; release = r; }));
    const restartWrite = vi.fn((_id: string) => { stopped = null; return Promise.resolve(); });
    const run = new GoalRun({ api, writeFirstPage: write, stoppedWrite: () => stopped, restartWrite, changed: () => {} }, "g1", result, true);
    await run.start(); await flush();
    run.skip(); await flush(60);
    expect(run.writingPage).toBe(true);
    stopped = "pg1"; release(); await flush();                  // 停止: the wiki controller ended the write
    expect(run.writingPage).toBe(false);
    expect(run.view()).toMatchObject({ kind: "stopped", pageId: "pg1" });
    (run as unknown as { advance(): void }).advance();         // any later change of the flow drives it forward again
    await flush(30);
    expect(write).toHaveBeenCalledTimes(1);                     // a stopped page is never written again by itself
    expect(run.writingPage).toBe(false);
    const h = { pick: () => {}, skip: () => {}, retry: () => {}, restart: vi.fn((_id: string) => run.restart()), showPage: () => {} };
    const card = buildGoalRunCard(run, h)!;
    expect(card.textContent).toBe(`${GOAL_RUN_COPY.stopped}${GOAL_RUN_COPY.restart}`);
    (card.querySelector("[data-action=restart]") as HTMLButtonElement).click();
    expect(h.restart).toHaveBeenCalledWith("g1");
    await flush();
    expect(restartWrite).toHaveBeenCalledWith("pg1");
    expect(run.view()).toMatchObject({ kind: "written", pageId: "pg1" });
    run.dispose();
  });

  it("停止 that ends the write with PageNotWritten (the live controller): still 已停止 · 重新写, not 没写成", async () => {
    const api = new FakeGoalApi({ roundMs: 0, splitMs: 0 });
    Object.defineProperty(api, "live", { value: true });
    const result = await confirmedGoal(api);
    let made = false;
    const readPath = api.getPath.bind(api);
    vi.spyOn(api, "getPath").mockImplementation(async (id: string) => {
      const out = await readPath(id);
      return made ? { ...out, pages: out.pages.map((x, i) => (i === 0 ? { ...x, page_id: "pg1" } : x)) } : out;
    });
    let stopped: string | null = null;
    let fail: (e: Error) => void = () => {};
    const write = vi.fn(() => new Promise<void>((_r, j) => { made = true; fail = j; }));
    const run = new GoalRun({ api, writeFirstPage: write, stoppedWrite: () => stopped, changed: () => {} }, "g1", result, true);
    await run.start(); await flush();
    run.skip(); await flush(60);
    stopped = "pg1"; fail(Object.assign(new Error("没写成"), { name: "PageNotWritten" })); await flush();
    expect(run.view()).toMatchObject({ kind: "stopped", pageId: "pg1" });
    run.dispose();
  });

  it("a run not driven forward (auto off) never enters or writes by itself (the learner's answer or 跳过 drives it: below)", async () => {
    const api = new FakeGoalApi({ roundMs: 0, splitMs: 0 });
    const result = await confirmedGoal(api);
    const enter = vi.spyOn(api, "enterRegion");
    const run = new GoalRun({ api, writeFirstPage: () => Promise.resolve(), changed: () => {} }, "g1", result, false);
    await run.start(); await flush(60);
    expect(run.view().kind).toBe("round");
    expect(enter).not.toHaveBeenCalled();
    run.dispose();
  });
});

// ---- a reloaded chat: the run rebuilt where the server has it (GoalRun.restore) ----

const DRAFT0 = { draft_id: null, page_id: null, path_id: null, map_id: "m1", title: "黑壳虾", concepts: [], questions: [],
  notes: { from_background: 0, reused_concepts: 0, dropped_questions: 0 }, round: 0, round_title: "", round_why: "", results: [], map: null, decisions: [] };
const QUESTION = { index: 0, kind: "quiz", target: "c1", cell: "c1.a", concept_id: "c1", concept_name: "水质", aspect: null, stem: "pH 多少合适？",
  options: ["6", "7.5"], option_means: [], intent: "quiz" };
const regionNode = (over: Partial<MapNodeOut>): MapNodeOut => ({ id: "r1", kind: "region", name: "水质", parent: null, scope: "", level: "", prereqs: [],
  target: false, state: "collapsed", path_id: null, page_id: null, reused: false, members: [], atomic: false, enter: "split", mix: {}, mastery: null, counts: {}, ...over });
const mapOf = (r: Partial<MapNodeOut>): GoalMapOut => ({ map_id: "m1", goal: "黑壳虾", title: "黑壳虾", next_region: null, nodes: [regionNode(r)], reading: null });
const pathOf = (pageId: string | null): PathOut => ({ path_id: "p1", goal: "黑壳虾 · 水质", source: "llm", next_index: 1, prefetch: null, sources: [],
  pages: [{ index: 1, aim: "", status: "not_started", state: "planned" as const, progress: null, page_id: pageId, trashed: false, skipped: false, key: "k1", title: "水温和 pH", abstract: "",
    sections: [], exam: 0, concepts: [] }] });

/** GET /wiki/progress with goal m1 at `step` (today's too). */
const stepOf = (over: Partial<StepOut>): StepOut => ({ kind: "none", goal_id: "m1", goal: "黑壳虾", map_id: "m1", region_id: null, region_name: "",
  path_id: null, index: null, page_id: null, title: "", draft_id: null, progress: null, failure: null, review_first: null, ...over });
const progressOf = (step: StepOut): ProgressOut => ({ today: step, goals: [{ goal_id: "m1", kind: "map", goal: "黑壳虾", state: "learning", blocks: [],
  path_ids: [], pages_done: 0, pages_total: 1, step }] });

/** A live goal API whose opening diagnosis is closed: the map has block r1 as `region`, the path `path`, progress at `step`. */
function closedGoal(region: Partial<MapNodeOut>, path: PathOut | null, step: StepOut, enter?: GoalDraftOut) {
  const api = {
    live: true,
    getDraft: vi.fn(() => Promise.resolve({ ...DRAFT0, map: mapOf(region) } as GoalDraftOut)),
    getMap: vi.fn(() => Promise.resolve(mapOf(region))),
    getPath: vi.fn(() => (path ? Promise.resolve(path) : Promise.reject(new Error("no path")))),
    enterRegion: vi.fn(() => (enter ? Promise.resolve(enter) : Promise.reject(new Error("not expected")))),
    progress: vi.fn(() => Promise.resolve(progressOf(step))),
    pathNext: vi.fn(), answer: vi.fn(), editMap: vi.fn(), editPath: vi.fn(), splitRow: vi.fn(), undoSplitRow: vi.fn(),
  };
  return { ...api, as: api as unknown as GoalApi };
}
const RESULT = { draftId: "dr0", mapId: "m1" };
const PAGE_STEP = { region_id: "r1", region_name: "水质", path_id: "p1", index: 1, title: "水温和 pH" };

describe("a reloaded chat resumes where GET /wiki/progress has the goal (GoalRun.restore)", () => {
  afterEach(() => { vi.useRealTimers(); });

  it("the goal's diagnosis still open: its round, and nothing entered until the learner answers; then it drives on", async () => {
    const api = new FakeGoalApi({ roundMs: 0, splitMs: 0 });
    const result = await confirmedGoal(api);
    const enter = vi.spyOn(api, "enterRegion"), next = vi.spyOn(api, "pathNext");
    const run = new GoalRun({ api, writeFirstPage: () => Promise.resolve(), changed: () => {} }, "g1", result, false);
    await run.restore(); await flush();
    expect(run.view().kind).toBe("round");
    expect(run.waiting).toBe(true);
    expect(enter).not.toHaveBeenCalled();
    run.skip(); await flush(60);                                  // the learner skips here: the run goes on as a fresh one
    expect(enter).toHaveBeenCalledTimes(1);
    expect(next).toHaveBeenCalledTimes(1);
    expect(run.view().kind).toBe("written");
    run.dispose();
  });

  it("a restored run asked for one block enters that block, not the next one by itself", async () => {
    const map: GoalMapOut = { ...mapOf({}), nodes: [regionNode({ id: "r1", enter: "children", state: "expanded" }), regionNode({ id: "r2", name: "器具", enter: "children", state: "expanded" })] };
    const api = closedGoal({}, null, stepOf({ kind: "enter_block", region_id: "r1" }), { ...DRAFT0, draft_id: null, map_id: "m1", map });
    api.getMap.mockImplementation(() => Promise.resolve(map));
    api.getDraft.mockImplementation(() => Promise.resolve({ ...DRAFT0, map } as GoalDraftOut));
    const run = new GoalRun({ api: api.as, writeFirstPage: () => Promise.resolve(), changed: () => {} }, "g1", RESULT, false);
    await run.restore(); await flush();
    await run.enterBlock("r2"); await flush();
    expect(api.enterRegion.mock.calls).toEqual([["m1", "r2"]]);
    expect(run.view().kind).not.toBe("failed");
    run.dispose();
  });

  it("a block's diagnosis open (progress: diagnosis in r1): that round is read back, nothing is entered", async () => {
    const open = { ...DRAFT0, draft_id: "dr1", map_id: null, round: 1, questions: [QUESTION] };
    const api = closedGoal({ state: "expanded" }, null, stepOf({ kind: "diagnosis", region_id: "r1", draft_id: "dr1" }));
    api.getDraft.mockImplementation((id?: string) => Promise.resolve((id === "dr1" ? open : { ...DRAFT0, map: mapOf({ state: "expanded" }) }) as GoalDraftOut));
    const run = new GoalRun({ api: api.as, writeFirstPage: () => Promise.resolve(), changed: () => {} }, "g1", RESULT, false);
    await run.restore(); await flush();
    expect(api.enterRegion).not.toHaveBeenCalled();
    const v = run.view();
    expect(v.kind === "round" && v.round.questions[0]!.stem).toBe("pH 多少合适？");
  });

  it("he came back to a goal whose opening diagnosis is open: the chat goes back to it, nothing is entered", async () => {
    const api = closedGoal({ state: "expanded" }, null, { ...stepOf({ kind: "diagnosis", draft_id: "dr0" }), region_id: null });
    api.getDraft.mockImplementation(() => Promise.resolve({ ...DRAFT0, draft_id: "dr0", round: 1, questions: [], map: mapOf({ state: "expanded" }) } as GoalDraftOut));
    const diagnose = vi.fn(() => Promise.resolve(true));
    const run = new GoalRun({ api: api.as, writeFirstPage: () => Promise.resolve(), changed: () => {}, diagnose }, "g1", RESULT, false);
    await run.enterBlock(null); await flush();
    expect(diagnose.mock.calls).toEqual([["dr0", true]]);
    expect(api.enterRegion).not.toHaveBeenCalled();
    run.dispose();
  });

  it("a turn elsewhere moves nothing while the diagnosis stays open; its end moves the goal on", async () => {
    const open = { ...DRAFT0, draft_id: "dr0", round: 1, questions: [], map: mapOf({ state: "expanded" }) } as GoalDraftOut;
    const closed = { ...DRAFT0, map: mapOf({ state: "expanded" }) } as GoalDraftOut;
    const api = closedGoal({ state: "expanded" }, null, stepOf({ kind: "diagnosis", draft_id: "dr0" }));
    let draft = open;
    api.getDraft.mockImplementation(() => Promise.resolve(draft));
    const diagnose = vi.fn(() => Promise.resolve(true));
    const run = new GoalRun({ api: api.as, writeFirstPage: () => Promise.resolve(), changed: () => {}, diagnose }, "g1", RESULT, false);
    await run.follow("dr0"); await flush();
    run.reread(); await flush();
    expect(diagnose).not.toHaveBeenCalled();
    expect(api.enterRegion).not.toHaveBeenCalled();
    draft = closed;
    run.reread(); await flush();
    expect(run.flow.state.draftId).toBeNull();
    expect(diagnose).not.toHaveBeenCalled();
    run.dispose();
  });

  it("the page to read: its link; a page being written: its writing card, read again until progress says written", async () => {
    const done = new GoalRun({ api: closedGoal({ state: "expanded", path_id: "p1" }, pathOf("pg1"), stepOf({ ...PAGE_STEP, kind: "read", page_id: "pg1" })).as,
      writeFirstPage: () => Promise.resolve(), changed: () => {} }, "g1", RESULT, false);
    await done.restore(); await flush();
    expect(done.view()).toEqual({ kind: "written", pageId: "pg1", title: "水温和 pH" });
    // a goal confirmed before its opening diagnosis closed: only its draft kept; the draft names the map
    const viaDraft = new GoalRun({ api: closedGoal({ state: "expanded", path_id: "p1" }, pathOf("pg1"), stepOf({ ...PAGE_STEP, kind: "read", page_id: "pg1" })).as,
      writeFirstPage: () => Promise.resolve(), changed: () => {} }, "g1", { draftId: "dr0", mapId: null }, false);
    await viaDraft.restore(); await flush();
    expect(viaDraft.view()).toEqual({ kind: "written", pageId: "pg1", title: "水温和 pH" });

    vi.useFakeTimers();
    const writing = stepOf({ ...PAGE_STEP, kind: "writing", page_id: "pg1", progress: { section: 1, of: 4, pass: null } });
    const api = closedGoal({ state: "expanded", path_id: "p1" }, pathOf("pg1"), writing);
    const changed = vi.fn();
    const run = new GoalRun({ api: api.as, writeFirstPage: () => Promise.resolve(), changed }, "g1", RESULT, false);
    await run.restore();
    expect(run.view()).toEqual({ kind: "writing", pageId: "pg1", title: "水温和 pH", progress: { section: 1, of: 4, pass: null } });
    expect(api.enterRegion).not.toHaveBeenCalled();                // a planned block is only read again
    api.progress.mockResolvedValueOnce(progressOf({ ...writing, progress: { section: 3, of: 4, pass: null } }));
    await vi.advanceTimersByTimeAsync(WRITING_POLL_MS + 10);
    expect(run.view()).toMatchObject({ kind: "writing", progress: { section: 3 } });
    api.progress.mockResolvedValue(progressOf({ ...writing, kind: "read", progress: null }));
    await vi.advanceTimersByTimeAsync(WRITING_POLL_MS + 10);
    expect(run.view()).toEqual({ kind: "written", pageId: "pg1", title: "水温和 pH" });
    run.dispose();
  });

  it("a page resumed while the server writes it: 停止 on its card cancels the run, then 已停止 · 重新写", async () => {
    const writing = stepOf({ ...PAGE_STEP, kind: "writing", page_id: "pg1", progress: { section: 1, of: 3, pass: null } });
    const api = closedGoal({ state: "expanded", path_id: "p1" }, pathOf("pg1"), writing);
    const stopPage = vi.fn((_id: string) => Promise.resolve(true));
    const run = new GoalRun({ api: api.as, writeFirstPage: () => Promise.resolve(), changed: () => {}, stopPage }, "g1", RESULT, false);
    await run.restore(); await flush();
    const h = { pick: () => {}, skip: () => {}, retry: () => {}, showPage: () => {}, stop: vi.fn((_id: string) => run.stop()) };
    const stop = buildGoalRunCard(run, h)!.querySelector<HTMLButtonElement>("[data-action=stop]")!;
    expect(stop.textContent).toBe(GOAL_RUN_COPY.stop);
    stop.click(); await flush();
    expect(stopPage).toHaveBeenCalledWith("pg1");
    expect(run.view()).toEqual({ kind: "stopped", pageId: "pg1", title: "水温和 pH" });
    expect(buildGoalRunCard(run, h)!.querySelector("[data-action=restart]")).not.toBeNull();
    // a stop the server refused: still writing, its card unchanged
    const api2 = closedGoal({ state: "expanded", path_id: "p1" }, pathOf("pg1"), writing);
    const run2 = new GoalRun({ api: api2.as, writeFirstPage: () => Promise.resolve(), changed: () => {}, stopPage: () => Promise.resolve(false) }, "g1", RESULT, false);
    await run2.restore(); await flush();
    run2.stop(); await flush();
    expect(run2.view().kind).toBe("writing");
    run.dispose(); run2.dispose();
  });

  it("a write that failed, or the first page never begun: the failure line; 重试 writes it (the learner's click)", async () => {
    for (const step of [stepOf({ ...PAGE_STEP, kind: "start_page" }), stepOf({ ...PAGE_STEP, kind: "failed", page_id: "pg9", failure: "x" })]) {
      const api = closedGoal({ state: "expanded", path_id: "p1" }, pathOf(step.page_id), step);
      const write = vi.fn(() => { api.getPath.mockResolvedValue(pathOf("pg9")); return Promise.resolve(); });
      const run = new GoalRun({ api: api.as, writeFirstPage: write, changed: () => {} }, "g1", RESULT, false);
      await run.restore(); await flush();
      expect(run.view()).toEqual({ kind: "failed", text: GOAL_RUN_COPY.notWritten });
      expect(write).not.toHaveBeenCalled();
      run.retry(); await flush();
      expect(write).toHaveBeenCalledTimes(1);
      expect(run.view()).toEqual({ kind: "written", pageId: "pg9", title: "水温和 pH" });
      run.dispose();
    }
  });

  it("a block not entered yet, the goal done, or the goal not in progress: nothing shows, nothing is entered", async () => {
    for (const step of [stepOf({ kind: "enter_block", region_id: "r1" }), stepOf({ kind: "goal_done" }), stepOf({ goal_id: "other", map_id: "other" })]) {
      const api = closedGoal({ state: "collapsed" }, null, step);
      if (step.goal_id === "other") api.progress.mockResolvedValue({ today: step, goals: [] });
      const run = new GoalRun({ api: api.as, writeFirstPage: () => Promise.resolve(), changed: () => {} }, "g1", RESULT, false);
      await run.restore(); await flush();
      expect(run.view().kind).toBe("idle");
      expect(api.enterRegion).not.toHaveBeenCalled();
      run.dispose();
    }
  });

  it("progress unreadable: the failure line (fail closed); 重试 reads it again", async () => {
    const api = closedGoal({ state: "expanded", path_id: "p1" }, pathOf("pg1"), stepOf({ ...PAGE_STEP, kind: "read", page_id: "pg1" }));
    api.progress.mockRejectedValueOnce(new Error("offline"));
    const run = new GoalRun({ api: api.as, writeFirstPage: () => Promise.resolve(), changed: () => {} }, "g1", RESULT, false);
    await run.restore(); await flush();
    expect(run.view().kind).toBe("failed");
    expect(api.getMap).not.toHaveBeenCalled();
    run.retry(); await flush();
    expect(run.view()).toEqual({ kind: "written", pageId: "pg1", title: "水温和 pH" });
    run.dispose();
  });
});

describe("the page written from the chat shows its writing card (one source: GET /wiki/progress)", () => {
  afterEach(() => { vi.useRealTimers(); });

  it("fills with the server's progress while written; its title links once the first section is in; written: the full card", async () => {
    vi.useFakeTimers();
    const step = stepOf({ ...PAGE_STEP, kind: "writing", page_id: "pg1", progress: { section: 0, of: 4, pass: null } });
    const api = closedGoal({ state: "expanded", path_id: "p1" }, pathOf(null), stepOf({ ...PAGE_STEP, kind: "start_page" }));
    let release: () => void = () => {};
    const write = vi.fn((_t: unknown, onPage?: (id: string) => void) => new Promise<void>((r) => { onPage?.("pg1"); release = r; }));
    const run = new GoalRun({ api: api.as, writeFirstPage: write, changed: () => {} }, "g1", RESULT, false);
    await run.restore(); await vi.advanceTimersByTimeAsync(0);     // the first page never begun: 重试 writes it
    api.progress.mockResolvedValue(progressOf(step));
    run.retry(); await vi.advanceTimersByTimeAsync(0);
    const shown = (): HTMLElement => buildGoalRunCard(run, { pick: () => {}, skip: () => {}, retry: () => {}, showPage: () => {} })!;
    expect(run.view()).toMatchObject({ kind: "writing", pageId: "pg1", progress: null });
    expect(shown().className).toBe("ew-goalrun");
    expect(shown().querySelectorAll(".ew-wcard")).toHaveLength(1);
    expect(shown().querySelector(".ew-check, .ew-qz")).toBeNull();
    expect(shown().querySelector<HTMLElement>(".ew-fill")!.dataset.indeterminate).toBe("true");
    expect(shown().querySelector(".ew-s")!.textContent).toBe("正在生成");
    expect(shown().querySelector(".ew-wcard .ew-t button")).toBeNull();     // not readable yet
    expect(shown().querySelector(".ew-wcard .ew-s")!.textContent).toBe(GOAL_RUN_COPY.writingLine(null));
    api.progress.mockResolvedValue(progressOf({ ...step, progress: { section: 2, of: 4, pass: null } }));
    await vi.advanceTimersByTimeAsync(WRITING_POLL_MS + 10);
    const card = shown().querySelector<HTMLElement>(".ew-wcard")!;
    expect(card.querySelector(".ew-t button")!.textContent).toBe("水温和 pH");
    expect(card.querySelector(".ew-s")!.textContent).toBe("已写好 2 / 4 节，可以先读");
    expect(card.querySelector<HTMLElement>(".ew-fill")!.style.width).toBe("calc(50% + 4px)");
    release(); await vi.advanceTimersByTimeAsync(0);
    expect(run.view()).toEqual({ kind: "written", pageId: "pg1", title: "水温和 pH" });
    const full = shown().querySelector<HTMLElement>(".ew-wcard")!;
    expect(full.querySelector<HTMLElement>(".ew-fill")!.dataset.state).toBe("done");
    expect(full.querySelector(".ew-s")!.textContent).toBe(GOAL_RUN_COPY.written);
    run.dispose();
  });
});
describe("a first page whose write failed is not written (U1)", () => {
  const writeOut = (state: WriteOut["state"], section: number | null = null, of: number | null = null): WriteOut => ({
    page_id: "pg_made", run_id: "run1", kind: "organize", state, section, of, pass: null, started_at: null, cost_usd: 0,
  });

  it("a lost POST-next response recovers the created page and durable completion without writing again", async () => {
    const api = closedGoal({ state: "expanded", path_id: "p1" }, pathOf(null), stepOf({ ...PAGE_STEP, kind: "start_page" }));
    const writes = vi.fn(() => Promise.resolve([writeOut("done")]));
    api.as.writes = writes;
    const write = vi.fn(() => {
      api.getPath.mockResolvedValue(pathOf("pg_made"));
      return Promise.reject(Object.assign(new Error("response lost"), { name: "PageNotWritten" }));
    });
    const run = new GoalRun({ api: api.as, writeFirstPage: write, changed: () => {} }, "g1", RESULT, false);
    await run.restore(); await flush();
    run.retry(); await flush();
    expect(run.view()).toEqual({ kind: "written", pageId: "pg_made", title: "水温和 pH" });
    expect(writes).toHaveBeenCalledWith(["pg_made"]);
    run.retry(); await flush();
    expect(write).toHaveBeenCalledTimes(1);
    run.dispose();
  });

  it("a disconnected completion follows real per-page sections and done despite a stale failed goal step", async () => {
    vi.useFakeTimers();
    const api = closedGoal({ state: "expanded", path_id: "p1" }, pathOf(null), stepOf({ ...PAGE_STEP, kind: "start_page" }));
    const writes = vi.fn(() => Promise.resolve([writeOut("planning")]));
    api.as.writes = writes;
    const write = vi.fn((_target: unknown, onPage?: (id: string) => void) => {
      onPage?.("pg_made");
      return Promise.reject(Object.assign(new Error("stream lost"), { name: "PageNotWritten" }));
    });
    const run = new GoalRun({ api: api.as, writeFirstPage: write, changed: () => {} }, "g1", RESULT, false);
    await run.restore();
    run.retry(); await vi.advanceTimersByTimeAsync(0);
    expect(run.view()).toMatchObject({ kind: "writing", progress: { section: 0, of: null } });
    api.progress.mockResolvedValue(progressOf(stepOf({ ...PAGE_STEP, kind: "failed", page_id: "pg_made" })));
    writes.mockResolvedValue([writeOut("writing", 2, 4)]);
    await vi.advanceTimersByTimeAsync(WRITING_POLL_MS);
    expect(run.view()).toMatchObject({ kind: "writing", progress: { section: 2, of: 4 } });
    writes.mockResolvedValue([writeOut("done", 4, 4)]);
    await vi.advanceTimersByTimeAsync(WRITING_POLL_MS);
    expect(run.view()).toMatchObject({ kind: "written", pageId: "pg_made" });
    expect(write).toHaveBeenCalledTimes(1);
    run.dispose(); vi.useRealTimers();
  });

  it("the controller's PageNotWritten is the failure line; 重试 writes the page already made, not a second one", async () => {
    const api = closedGoal({ state: "expanded", path_id: "p1" }, pathOf(null), stepOf({ ...PAGE_STEP, kind: "start_page" }));
    api.as.writes = vi.fn(() => Promise.resolve([writeOut("failed")]));
    const notWritten = Object.assign(new Error("暂时不能用"), { name: "PageNotWritten" });
    const write = vi.fn().mockImplementationOnce(() => { api.getPath.mockResolvedValue(pathOf("pg_made")); return Promise.reject(notWritten); })
      .mockResolvedValue(undefined);
    const run = new GoalRun({ api: api.as, writeFirstPage: write, changed: () => {} }, "g1", RESULT, false);
    await run.restore(); await flush();
    run.retry(); await flush();                                   // the never-written path: its first write fails
    expect(run.view()).toEqual({ kind: "failed", text: GOAL_RUN_COPY.notWritten });
    run.retry(); await flush();
    expect(write.mock.calls[1]![0]).toMatchObject({ pathId: null, pageId: "pg_made" });
    expect(run.view()).toEqual({ kind: "written", pageId: "pg_made", title: "水温和 pH" });
  });
});

describe("a block entered from the chat only shows its parts (spec 2026-10-28): the learner picks, the run goes on from the pick", () => {
  it("after the opening diagnosis the first block is entered and the run stops there (no part entered, nothing written)", async () => {
    const api = new NestedGoalApi();
    const write = vi.fn((_t: unknown) => Promise.resolve());
    const run = new GoalRun({ api, writeFirstPage: write, changed: () => {} }, "g1", { draftId: null, mapId: NESTED_MAP }, true);
    await run.start(); await flush();
    expect(api.calls.filter((c) => c.startsWith("enter:"))).toEqual(["enter:A"]);
    expect(run.flow.state.entered?.region).toBe("A");
    expect(run.view()).toEqual({ kind: "choose", name: "水质" });
    expect(buildGoalRunCard(run, { pick: () => {}, skip: () => {}, retry: () => {}, showPage: () => {} })!.textContent).toBe(GOAL_RUN_COPY.choose("水质"));
    expect(api.calls.some((c) => c.startsWith("next:"))).toBe(false);
    // the map naming an entered node that waits for a pick (or a part at any depth): entering it again only shows its parts
    await run.enterBlock(null); await flush();
    expect(run.view().kind).toBe("choose");
    run.dispose();
  });

  it("a point picked: its path's first page is written; a point that needs a diagnosis asks it here first", async () => {
    const api = new NestedGoalApi();
    const run = new GoalRun({ api, writeFirstPage: () => Promise.resolve(), changed: () => {} }, "g1", { draftId: null, mapId: NESTED_MAP }, true);
    await run.start(); await flush();
    await run.enterBlock("A2"); await flush();
    expect(api.calls).toContain("next:path-A2");
    expect(run.view()).toMatchObject({ kind: "written", title: "换水" });
    await run.enterBlock("A1"); await flush();
    expect(run.view()).toEqual({ kind: "choose", name: "水的化学" });
    await run.enterBlock(DIAGNOSED); await flush();
    const v = run.view();
    expect(v.kind === "round" && v.round.questions[0]!.stem).toBe("pH 多少合适？");
    run.pick(0); await flush(60);
    expect(api.calls).toContain(`next:path-${DIAGNOSED}`);
    expect(run.view()).toMatchObject({ kind: "written", title: "pH" });
    run.dispose();
  });
});

describe("a 读懂 goal in the chat (reading-mode spec §5): 下一步 is the first unit not read, with the 先补 hint", () => {
  it("after the opening diagnosis no block is entered: the card says which unit to read next, in 学点新的", async () => {
    const api = new ReadingGoalApi();
    const run = new GoalRun({ api, writeFirstPage: () => Promise.resolve(), changed: () => {} }, "g1", { draftId: null, mapId: NESTED_MAP }, true);
    await run.start(); await flush();
    expect(api.calls.filter((c) => c.startsWith("enter:") || c.startsWith("open:"))).toEqual([]);
    expect(run.view()).toEqual({ kind: "read", order: 2, title: "自注意力", uses: [] });
    const hooks = { pick: () => {}, skip: () => {}, retry: () => {}, showPage: () => {} };
    expect(buildGoalRunCard(run, hooks)!.textContent).toBe(GOAL_RUN_COPY.readNext(2, "自注意力"));
    await run.enterBlock(null); await flush();                         // 下一块 on a 读懂 goal: still the unit, nothing entered
    expect(api.calls.filter((c) => c.startsWith("enter:"))).toEqual([]);
    // the unit looked at meanwhile (学点新的): the points it uses that are not learned show beside it
    await api.openUnit(READING_ID, "ru_2", false);
    await run.flow.refreshMap(); await flush();
    expect(run.view()).toEqual({ kind: "read", order: 2, title: "自注意力", uses: ["水质", "softmax"] });
    expect([...buildGoalRunCard(run, hooks)!.children].map((x) => x.textContent))
      .toEqual([GOAL_RUN_COPY.readNext(2, "自注意力"), GOAL_RUN_COPY.readFirst(["水质", "softmax"])]);
    // unit 2 read: the next is unit 4 (3 is skipped)
    api.units[1] = { ...api.units[1]!, status: "done", page_id: "pg-ru_2" };
    await run.flow.refreshMap(); await flush();
    expect(run.view()).toMatchObject({ kind: "read", order: 4, title: "结论" });
    run.dispose();
  });
});
