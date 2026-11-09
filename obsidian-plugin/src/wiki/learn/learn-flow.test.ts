import { describe, expect, it, vi } from "vitest";
import { ApiError, OfflineError } from "../../api/errors";
import { WIKI_COPY } from "../copy";
import { LEARN_COPY } from "./copy";
import type { GoalDraftOut, GoalMapOut, PathOut } from "../schema";
import type { GoalApi } from "./goal-api";
import { FakeGoalApi, fakeBlocks, registerBlockGoal } from "./goal-fake";
import { LearnFlow, nodeOf, pageAim, pageHeading, partsOf, type FlowHost } from "./learn-flow";
import { DIAGNOSED, NESTED_MAP, NestedGoalApi } from "./nested-goal-kit";

const flush = async (): Promise<void> => { for (let i = 0; i < 12; i++) await Promise.resolve(); };

function run(api: GoalApi = new FakeGoalApi({ roundMs: 0 }), extra: Partial<FlowHost> = {}) {
  const fails: string[] = [];
  const flow = new LearnFlow(api, { fail: (t) => { fails.push(t); }, ...extra }, "");
  const last = () => flow.state.rounds[flow.state.rounds.length - 1]!;
  const answer = async (picks: number[]): Promise<void> => {
    const r = last();
    r.questions.forEach((q, i) => flow.pick(r, q.index, picks[i]!));
    await flow.submit(r);
  };
  return { flow, fails, last, answer };
}

/** A goal confirmed in the chat (the fake's blocks as its assistant proposed them) opened in the flow, as 确认 brings it in. */
async function opened(flow: LearnFlow, goal: string): Promise<{ draftId: string; mapId: string }> {
  const ids = registerBlockGoal(goal, fakeBlocks(goal));
  await flow.openGoal(ids);
  return ids;
}

/** A draft as the server sends it; the fields a test does not name are empty. */
const out = (o: Partial<GoalDraftOut>): GoalDraftOut => ({
  draft_id: null, page_id: null, path_id: null, map_id: null, title: "", concepts: [], questions: [],
  notes: { from_background: 0, reused_concepts: 0, dropped_questions: 0 }, round: 0, round_title: "", round_why: "", results: [], map: null, decisions: [], ...o });

describe("LearnFlow on the reshaped fake (the real big-goal contract)", () => {
  it("rounds: round_title per round, the next response's results colour the quizzes, the finished map → 学习路径", async () => {
    const { flow, last, answer } = run();
    await opened(flow, "我要学机器学习");
    expect(flow.state.stage).toBe("map");
    expect(last().title).toBe("了解你的基础");
    await answer([0, 0, 0, 0]);
    expect(flow.state.stage).toBe("probe");
    expect(flow.state.rounds[0]!.graded).toEqual({});     // self-reports are never right / wrong
    expect(last().title).toBe("聚焦关键知识");
    await answer([0, 0, 1, -1]);
    expect(flow.state.rounds[1]!.graded).toEqual({ 0: true, 1: true, 2: false, 3: false });
    await answer([0]);
    expect(flow.state.rounds[2]!.graded).toEqual({ 0: true });
    expect(flow.state.stage).toBe("path");
    expect(flow.state.draftId).toBeNull();
    expect(flow.state.rounds.every((r) => !r.open)).toBe(true);
    expect(flow.state.map?.next_region).toBe("pr");
  });

  it("跳过 = answers {skip: true}: every round closes, the map is finished", async () => {
    const api = new FakeGoalApi({ roundMs: 0 });
    const spy = vi.spyOn(api, "answer");
    const { flow } = run(api);
    await opened(flow, "g");
    await flow.skip();
    expect(spy.mock.calls[0]![1]).toEqual({ skip: true });
    expect(flow.state.stage).toBe("path");
    expect(flow.state.rounds[0]!.open).toBe(false);
  });

  it("a finished map_id without a map attached: GET /wiki/maps/{id}", async () => {
    const fake = new FakeGoalApi({ roundMs: 0 });
    let mapOut: GoalMapOut | null = null;
    const getMap = vi.fn(() => Promise.resolve(mapOut!));
    const api: GoalApi = {
      live: false, enterRegion: (m, r) => fake.enterRegion(m, r),
      getPath: (p) => fake.getPath(p), pathNext: (p) => fake.pathNext(p),
      answer: async (d, b) => { const o = await fake.answer(d, b); mapOut = o.map; return { ...o, map: null }; },
      getMap, getDraft: (d) => fake.getDraft(d), editMap: (m, o) => fake.editMap(m, o), editPath: (p, e) => fake.editPath(p, e),
      splitRow: (p, k) => fake.splitRow(p, k), undoSplitRow: (p, k, c) => fake.undoSplitRow(p, k, c), splitNode: (n, m) => fake.splitNode(n, m),
    };
    const { flow } = run(api);
    const ids = await opened(flow, "g");
    await flow.skip();
    expect(getMap).toHaveBeenCalledWith(ids.mapId);
    expect(flow.state.map).toBe(mapOut);
    expect(flow.state.stage).toBe("path");
  });

  it("进入 → the region's path (GET /wiki/paths/{id}); a planned region shows its path again without POST enter", async () => {
    const api = new FakeGoalApi({ roundMs: 0 });
    const enter = vi.spyOn(api, "enterRegion");
    const { flow, answer } = run(api);
    await opened(flow, "g");
    await answer([0, 0, 0, 0]); await answer([0, 0, 1, -1]); await answer([0]);
    await flow.enter("ev");
    const e = flow.state.entered!;
    expect([flow.state.stage, e.region, e.name, e.path?.path_id]).toEqual(["entered", "ev", "模型评估", "fake-path-ev"]);
    expect(e.path!.pages.map((p) => [pageHeading(p), pageAim(p.aim)])).toEqual([["怎么知道模型好不好", "能判断过拟合，并用交叉验证选模型"]]);
    expect(flow.state.map!.nodes.find((n) => n.id === "ev")!.state).toBe("expanded");
    await flow.enter("sl");
    await flow.enter("ev");
    expect(enter.mock.calls.map((c) => c[1])).toEqual(["ev", "sl"]);
    expect(flow.state.entered?.path?.path_id).toBe("fake-path-ev");
    // A known region is entered like any other (known is a state, not a lock).
    await flow.enter("la");
    expect(enter.mock.calls.map((c) => c[1])).toEqual(["ev", "sl", "la"]);
  });

  it("进入 may first ask a round inside the region; its answer finishes with the region's path", async () => {
    const fake = new FakeGoalApi({ roundMs: 0 });
    const { flow, answer } = run(fake);
    await opened(flow, "g");
    await flow.skip();
    const map = flow.state.map!;
    const q = { index: 0, kind: "quiz" as const, target: "op", cell: "gd:procedure", concept_id: "op.gd", concept_name: "梯度下降", aspect: "procedure" as const,
      stem: "学习率太大会怎样？", options: ["来回震荡", "更快收敛"], option_means: null, intent: "mastery" as const };
    const path: PathOut = { path_id: "P", goal: "g · 优化", source: "llm", next_index: 1, prefetch: null,
      pages: [{ index: 1, aim: "学完这页你能：手算两步梯度下降", concepts: [{ concept_id: "op.gd", name: "梯度下降", target: true }, { concept_id: "op.lr", name: "学习率", target: true }], status: "not_started", state: "planned" as const, progress: null, page_id: null, trashed: false, skipped: false, key: "", title: "", abstract: "", sections: [], exam: 0 }], sources: [] };
    vi.spyOn(fake, "enterRegion").mockResolvedValue(out({ draft_id: "D2", round: 1, round_title: "先确认一下优化", round_why: "不显示", questions: [q], map }));
    const ans = vi.spyOn(fake, "answer").mockResolvedValue(out({ path_id: "P", map, results: [{ index: 0, kind: "quiz", choice: 1, answer: 0, correct: false, means: null, intent: "mastery", node_id: "op" }] }));
    vi.spyOn(fake, "getPath").mockResolvedValue(path);
    await flow.enter("op");
    expect([flow.state.stage, flow.state.draftId, flow.state.entered?.name]).toEqual(["entered", "D2", "优化"]);
    expect(flow.state.rounds[flow.state.rounds.length - 1]!.title).toBe("先确认一下优化");
    await answer([1]);
    expect(ans).toHaveBeenCalledWith("D2", { answers: [1] });
    expect(flow.state.rounds[flow.state.rounds.length - 1]!.graded).toEqual({ 0: false });
    expect([flow.state.stage, flow.state.entered?.region, flow.state.entered?.path]).toEqual(["entered", "op", path]);
    expect([pageHeading(path.pages[0]!), pageAim(path.pages[0]!.aim)]).toEqual(["梯度下降、学习率", "手算两步梯度下降"]);
  });

  it("a small goal that finishes straight as a path: 进入「<title>」 with its pages", async () => {
    const path: PathOut = { path_id: "P", goal: "贝叶斯", source: "single", next_index: 1, prefetch: null,
      pages: [{ index: 1, aim: "能用贝叶斯公式更新判断", concepts: [{ concept_id: "c", name: "贝叶斯公式", target: true }], status: "not_started", state: "planned" as const, progress: null, page_id: null, trashed: false, skipped: false, key: "", title: "", abstract: "", sections: [], exam: 0 }], sources: [] };
    const api = { live: true, getDraft: () => Promise.resolve(out({ path_id: "P", title: "贝叶斯定理" })), getPath: vi.fn(() => Promise.resolve(path)) } as unknown as GoalApi;
    const { flow } = run(api);
    await flow.openGoal({ draftId: "D", mapId: null });
    expect([flow.state.stage, flow.state.entered?.name, flow.state.entered?.path]).toEqual(["entered", "贝叶斯定理", path]);
  });

});

describe("LearnFlow fails closed with a plain notice", () => {
  const draft = async (api: FakeGoalApi) => { const r = run(api); await opened(r.flow, "g"); return r; };
  it.each([
    [new ApiError(409, "x"), "这一块已经排好了页面。"],
    [new ApiError(404, "x"), "找不到这个领域了，重新开始试试。"],
    [new ApiError(503, "x"), "请切换模型重试"],
    [new ApiError(429, "x"), WIKI_COPY.budget],
    [new OfflineError(), WIKI_COPY.offline],
  ])("进入 rejected with %s → %s; the path stays usable", async (err, text) => {
    const api = new FakeGoalApi({ roundMs: 0 });
    const { flow, fails } = await draft(api);
    await flow.skip();
    vi.spyOn(api, "enterRegion").mockRejectedValueOnce(err);
    await flow.enter("sl");
    expect(fails).toEqual([text]);
    expect([flow.state.stage, flow.state.busy, flow.state.entered]).toEqual(["path", false, null]);
  });

  it("提交 answered 404 (the draft is gone) → its own line; the round opens again", async () => {
    const api = new FakeGoalApi({ roundMs: 0 });
    const { flow, fails, last } = await draft(api);
    vi.spyOn(api, "answer").mockRejectedValueOnce(new ApiError(404, "x"));
    const r = last();
    r.questions.forEach((q) => flow.pick(r, q.index, 0));
    await flow.submit(r);
    expect(fails).toEqual(["这一轮已经过期了，重新开始试试。"]);
    expect(r.open).toBe(true);
  });

  it("开始 with no assistant → 助手没连上, the form stays", async () => {
    const { flow, fails } = run(new FakeGoalApi());
    await flow.start("g", "");
    expect(fails).toEqual([LEARN_COPY.noAssistant]);
    expect(flow.state.stage).toBe("goal");
  });
});

describe("page lines", () => {
  it("heading = concept names with 「、」; aim without its 「学完这页你能」 lead-in", () => {
    expect(pageHeading({ index: 1, aim: "", concepts: [{ concept_id: "a", name: "期望", target: true }, { concept_id: "b", name: "方差", target: false }], status: "not_started", state: "planned" as const, progress: null, page_id: null, trashed: false, skipped: false, key: "", title: "", abstract: "", sections: [], exam: 0 })).toBe("期望、方差");
    expect(pageAim("学完这页你能：说出期望的意思")).toBe("说出期望的意思");
    expect(pageAim("学完这页你能说出期望的意思")).toBe("说出期望的意思");
    expect(pageAim("能说出期望的意思")).toBe("能说出期望的意思");
  });
});

describe("LearnFlow: chat-first blocks (spec 2026-10-23, app.js startBlocks / blocksAct)", () => {
  type Row = import("../../assistant/goal-chat").GoalChatRow;
  const B5 = [["id", "认识黑壳虾", "它是什么虾"], ["wq", "水质", "水温"], ["keep", "养起来", "开缸"], ["br", "繁殖", "公母比例"], ["sick", "生病和出问题", "常见病"]] as const;
  const rowOf = (o: Partial<Row>): Row => ({ key: "conv1", actionId: "g1", phase: "wait", replaced: false, title: "「黑壳虾」分成 5 块",
    blocks: B5.map(([key, name, scope]) => ({ key, name, scope })), result: null, ...o });
  function chat(api: GoalApi = new FakeGoalApi({ roundMs: 0 })) {
    const sent: string[] = [], learn: { goal: string; background: string }[] = [];
    let finish: (ok: boolean) => void = () => {};
    const r = run(api, { startChat: (text, l) => { sent.push(text); learn.push(l); return { key: "conv1", done: new Promise<boolean>((ok) => { finish = ok; }) }; } });
    return { ...r, sent, learn, finish: (ok: boolean) => finish(ok) };
  }

  it("开始 sends the goal (+ 背景) to the chat; 正在把… until the draft; the draft → stage blocks with its blocks", () => {
    const { flow, sent, learn } = chat();
    void flow.start("我要学黑壳虾", "学过高中生物");
    expect(sent).toEqual(["我要学黑壳虾\n背景：学过高中生物"]);
    expect(learn).toEqual([{ goal: "我要学黑壳虾", background: "学过高中生物" }]);   // sent as intent learn_goal
    expect([flow.state.stage, flow.state.chat, flow.state.goal]).toEqual(["thinking-map", "conv1", "我要学黑壳虾"]);
    flow.goalRow(rowOf({ key: "other" }));                 // another conversation's row changes nothing
    expect(flow.state.stage).toBe("thinking-map");
    flow.goalRow(rowOf({}));
    expect(flow.state.stage).toBe("blocks");
    expect(flow.state.blocks.map((b) => b.name)).toEqual(["认识黑壳虾", "水质", "养起来", "繁殖", "生病和出问题"]);
    // replaced: nothing yet; the new draft takes over
    flow.goalRow(rowOf({ phase: "rejected", replaced: true }));
    expect(flow.state.stage).toBe("blocks");
    flow.goalRow(rowOf({ actionId: "g2", blocks: [{ key: "id", name: "认识黑壳虾", scope: "" }, { key: "keep", name: "养起来", scope: "" }] }));
    expect([flow.state.actionId, flow.state.blocks.length]).toEqual(["g2", 2]);
  });

  it("确认 (executed with the result) → GET /wiki/goals/{draft_id}: 学习路线 + 第 1 轮", async () => {
    const { registerBlockGoal } = await import("./goal-fake");
    const ids = registerBlockGoal("我要学黑壳虾", B5.map(([id, name, scope]) => ({ id, name, scope, from: [id] })));
    const api = new FakeGoalApi({ roundMs: 0 });
    const spy = vi.spyOn(api, "getDraft");
    const { flow, last } = chat(api);
    void flow.start("我要学黑壳虾", "");
    flow.goalRow(rowOf({}));
    flow.goalRow(rowOf({ phase: "live", result: { draftId: ids.draftId, mapId: ids.mapId } }));
    await flush();
    expect(spy).toHaveBeenCalledWith(ids.draftId);
    expect(flow.state.stage).toBe("map");
    expect(last().title).toBe("了解你的基础");
    expect(flow.state.map!.nodes.filter((n) => n.kind === "region").map((n) => n.name)).toEqual(["认识黑壳虾", "水质", "养起来", "繁殖", "生病和出问题"]);
  });

  it("executed without a result: a notice, the blocks stay; 取消 / 撤销 → the goal card again", async () => {
    const { flow, fails } = chat();
    void flow.start("我要学黑壳虾", "");
    flow.goalRow(rowOf({}));
    flow.goalRow(rowOf({ phase: "live", result: null }));
    await flush();
    expect(fails).toEqual(["目标建好了，但没能打开。可以在知识图谱里找到它。"]);
    expect(flow.state.stage).toBe("blocks");
    flow.goalRow(rowOf({ phase: "rejected" }));
    expect([flow.state.stage, flow.state.blocks, flow.state.map]).toEqual(["goal", [], null]);
  });

  it("a turn that failed puts the form back with a notice; a turn with no draft leaves the folded card", async () => {
    const a = chat();
    void a.flow.start("我要学黑壳虾", "");
    a.finish(false); await flush();
    expect([a.flow.state.stage, a.fails]).toEqual(["goal", ["没发出去。可以再点一次开始。"]]);
    const b = chat();
    void b.flow.start("我要学黑壳虾", "");
    b.finish(true); await flush();
    expect(b.flow.state.stage).toBe("blocks");
    const c = run(new FakeGoalApi({ roundMs: 0 }), { startChat: () => null });
    await c.flow.start("我要学黑壳虾", "");
    expect([c.flow.state.stage, c.fails]).toEqual(["goal", ["没发出去。可以再点一次开始。"]]);
  });

  it("学习路径 edits: a move shows at once and is put back when refused; 删除 answers the undo record, 撤销 restores", async () => {
    const api = new FakeGoalApi({ roundMs: 0 });
    const { flow, fails } = run(api);
    await opened(flow, "我要学黑壳虾");
    await flow.skip();
    const names = (): string[] => flow.state.map!.nodes.filter((n) => n.kind === "region" && n.parent === null).map((n) => n.name);
    await flow.moveBlock(1, 0);
    expect(names()).toEqual(["水质与环境", "分类与鉴别", "开缸与日常", "营养与摄食", "生长与繁殖", "疾病与风险"]);
    vi.spyOn(api, "editMap").mockRejectedValueOnce(new ApiError(422, "要列出每一块"));
    await flow.moveBlock(0, 4);
    expect(names()).toEqual(["水质与环境", "分类与鉴别", "开缸与日常", "营养与摄食", "生长与繁殖", "疾病与风险"]);
    expect(fails).toEqual(["要列出每一块"]);
    const undo = await flow.dropBlock("br");
    expect(names()).not.toContain("生长与繁殖");
    expect(undo).toEqual([expect.objectContaining({ op: "insert", index: 4 })]);
    // M2: 撤销 of an older delete sends its inverse op — the block back, the later edits kept
    await flow.renameBlock("wq", "水");
    expect(await flow.addBlock("造景")).toBe(true);
    const sent = vi.spyOn(api, "editMap");
    await flow.undoEdit(undo!);
    expect(sent.mock.calls[sent.mock.calls.length - 1]![1]).toEqual(undo);
    expect(names()).toEqual(["水", "分类与鉴别", "开缸与日常", "营养与摄食", "生长与繁殖", "疾病与风险", "造景"]);
    await flow.undoEdit(undo!);                                   // stale: the server's 409 text is the notice
    expect(fails[fails.length - 1]).toBe("后面又改过，不能这样撤销");
  });

  it("a 学习路径 edit the server refuses (409) shows the server's text", async () => {
    const api = new FakeGoalApi({ roundMs: 0 });
    const { flow, fails } = run(api);
    await opened(flow, "我要学黑壳虾");
    await flow.skip();
    vi.spyOn(api, "editMap").mockRejectedValueOnce(new ApiError(409, "这一块正在诊断，先答完或跳过"));
    expect(await flow.dropBlock("br")).toBeNull();
    expect(fails).toEqual(["这一块正在诊断，先答完或跳过"]);
  });

  it("进入 splits the block (splitting meanwhile), then its pages; page edits, 拆成多页 (parts are not split again) and undo", async () => {
    const api = new FakeGoalApi({ roundMs: 0, splitMs: 5 });
    const { flow } = run(api);
    await opened(flow, "我要学黑壳虾");
    await flow.skip();
    const p = flow.enter("wq");
    expect(flow.state.entered).toMatchObject({ region: "wq", name: "水质与环境", splitting: true });
    await p; await flush();
    const titles = (): string[] => flow.state.entered!.rows.map((r) => r.title);
    expect(titles()).toEqual(["黑壳虾要什么样的水", "氨氮、亚硝酸盐和硝化", "怎么测、怎么调"]);
    expect(flow.state.entered!.rows[0]!.aim).toBe("能说出水温、pH、GH / KH 的合适范围");
    await flow.movePage(2, 0);
    expect(titles()).toEqual(["怎么测、怎么调", "黑壳虾要什么样的水", "氨氮、亚硝酸盐和硝化"]);
    expect(flow.state.entered!.rows[1]!.aim).toBe("能说出水温、pH、GH / KH 的合适范围");   // aims survive an edit
    const key = await flow.removePage(0);
    expect(titles()).toEqual(["黑壳虾要什么样的水", "氨氮、亚硝酸盐和硝化"]);
    await flow.unremovePage(key!);
    expect(titles()).toHaveLength(3);
    const k = flow.state.entered!.rows[1]!.key;
    const out = await flow.splitPage(k);
    expect(out!.n).toBe(2);
    expect(titles()).toEqual(["怎么测、怎么调", "黑壳虾要什么样的水是什么", "黑壳虾要什么样的水怎么做", "氨氮、亚硝酸盐和硝化"]);
    expect(flow.state.entered!.rows.map((r) => r.canSplit)).toEqual([true, false, false, true]);
    await flow.undoSplit(k, out!.undo!);
    expect(titles()).toEqual(["怎么测、怎么调", "黑壳虾要什么样的水", "氨氮、亚硝酸盐和硝化"]);
    expect(await flow.addPage("造景")).toBe(true);
  });
});

describe("a request lost to a backend restart (「为什么点了进入没反应」): never stale, never silent", () => {
  /** The fake, whose answer goes through on the server but whose response is lost (OfflineError) once. */
  function lossy() {
    const api = new FakeGoalApi({ roundMs: 0 });
    const real = api.answer.bind(api);
    let lose = false;
    const spy = vi.spyOn(api, "answer").mockImplementation(async (d, b) => { const o = await real(d, b); if (lose) { lose = false; throw new OfflineError(); } return o; });
    return { api, spy, loseNext: () => { lose = true; } };
  }

  it("the lost 跳过 clears busy and says so; 进入 then reads the draft back, finds it finished and enters", async () => {
    const { api, loseNext } = lossy();
    const enter = vi.spyOn(api, "enterRegion");
    const { flow, fails } = run(api);
    const ids = await opened(flow, "g");
    loseNext();
    await flow.skip();
    expect(flow.state.busy).toBe(false);
    expect(fails).toHaveLength(1);                            // the error line (offline), plain text
    expect(flow.state.stage).toBe("map");                     // what the client knows: the round open again
    await flow.enter("ev");
    expect(flow.state.draftId).toBeNull();                    // GET /wiki/goals/{id}: the server had it finished
    expect(enter).toHaveBeenCalledWith(ids.mapId, "ev");
    expect([flow.state.stage, flow.state.entered?.region]).toEqual(["entered", "ev"]);
    expect(fails).toHaveLength(1);
  });

  it("a lost answer: the next 提交 reads the draft back first and shows the server's next round (the stale answers are not re-sent)", async () => {
    const { api, spy, loseNext } = lossy();
    const { flow, last } = run(api);
    await opened(flow, "g");
    const r1 = last();
    r1.questions.forEach((q) => flow.pick(r1, q.index, 0));
    loseNext();
    await flow.submit(r1);
    expect(flow.state.rounds).toHaveLength(1);
    const calls = spy.mock.calls.length;
    await flow.submit(r1);
    expect(spy.mock.calls.length).toBe(calls);   // nothing re-sent
    expect(flow.state.rounds).toHaveLength(2);
    expect(last().open).toBe(true);
  });

  it("进入 while the diagnosis is really still open, or while a step runs: a plain line, not a no-op", async () => {
    const api = new FakeGoalApi({ roundMs: 0 });
    const { flow, fails } = run(api);
    const ids = await opened(flow, "g");
    await flow.enter("ev");
    expect(fails).toEqual(["正在摸底，先答完或跳过。"]);
    let release!: () => void;
    vi.spyOn(api, "answer").mockImplementation(() => new Promise((ok) => { release = () => ok(out({ map_id: ids.mapId })); }));
    const skipping = flow.skip();
    await flush();
    await flow.enter("ev");
    expect(fails[1]).toBe("上一步还没做完，等它做完再开始。");
    release(); await skipping;
  });

  it("a 409 on 进入 (an earlier 进入 whose answer was lost): the map read back shows the block's pages", async () => {
    const api = new FakeGoalApi({ roundMs: 0 });
    const { flow, fails } = run(api);
    const ids = await opened(flow, "g");
    await flow.skip();
    await api.enterRegion(ids.mapId, "ev");                 // the server planned it; the client never heard
    const planned = { ...flow.state.map!, nodes: flow.state.map!.nodes.map((n) => (n.id === "ev" ? { ...n, path_id: "fake-path-ev" } : n)) };
    vi.spyOn(api, "getMap").mockResolvedValue(planned);
    await flow.enter("ev");
    expect(fails).toEqual([]);
    expect(flow.state.entered?.path?.path_id).toBe("fake-path-ev");
  });
});

describe("on-demand decomposition (spec 2026-10-28 §2.2): entering shows a node's parts, the learner picks", () => {
  async function nested() {
    const api = new NestedGoalApi();
    const r = run(api);
    await r.flow.openGoal({ draftId: null, mapId: NESTED_MAP });
    return { api, ...r };
  }
  const names = (flow: LearnFlow, id: string): [string, string][] => partsOf(flow.state.map, id).map((n) => [n.name, n.enter]);

  it("a block splits one level and only shows its parts; a sub-block does the same, three levels down; nothing is opened by itself", async () => {
    const { api, flow, fails } = await nested();
    expect(flow.state.stage).toBe("path");
    await flow.enter("A");
    expect([flow.state.stage, flow.state.entered?.region, flow.state.entered?.path, flow.state.draftId]).toEqual(["entered", "A", null, null]);
    expect(names(flow, "A")).toEqual([["水的化学", "split"], ["换水", "learn"]]);
    await flow.enter("A1");
    expect(names(flow, "A1")).toEqual([["氮循环", "split"], ["pH", "learn"]]);
    await flow.enter("A1a");
    expect(flow.state.entered?.region).toBe("A1a");
    expect(names(flow, "A1a")).toEqual([["氨", "learn"], ["亚硝酸盐", "learn"]]);
    // the parts are not entered: one POST enter per node the learner picked, none for a part
    expect(api.calls.filter((c) => c.startsWith("enter:"))).toEqual(["enter:A", "enter:A1", "enter:A1a"]);
    // back up: the block already has its parts — shown again (0 model calls on the server: enter "children")
    expect(nodeOf(flow.state.map, "A")?.enter).toBe("children");
    await flow.enter("A");
    expect(flow.state.entered?.region).toBe("A");
    expect(fails).toEqual([]);
  });

  it("while a node is split the entered card says it splits into parts, not pages", async () => {
    const { api, flow } = await nested();
    let release!: () => void;
    const real = api.enterRegion.bind(api);
    vi.spyOn(api, "enterRegion").mockImplementationOnce((m, id) => new Promise((ok) => { release = () => { void real(m, id).then(ok); }; }));
    const entering = flow.enter("A");
    await flush();
    expect(flow.state.entered).toMatchObject({ region: "A", splitting: true, intoParts: true });
    release(); await entering;
    expect(flow.state.entered).toMatchObject({ region: "A", splitting: false });
  });

  it("学这个 on a point: its path (hung on the point's node), or a round over it first that ends with its path", async () => {
    const { api, flow, answer } = await nested();
    await flow.enter("A");
    await flow.enter("A2");
    expect([flow.state.entered?.region, flow.state.entered?.path?.path_id]).toEqual(["A2", "path-A2"]);
    expect(nodeOf(flow.state.map, "A2")?.path_id).toBe("path-A2");
    await flow.enter("A1");
    await flow.enter(DIAGNOSED);
    expect([flow.state.stage, flow.state.draftId, flow.state.entered?.region]).toEqual(["entered", `d-${DIAGNOSED}`, DIAGNOSED]);
    await answer([0]);
    expect([flow.state.draftId, flow.state.entered?.path?.path_id]).toEqual([null, `path-${DIAGNOSED}`]);
    // opened again: its path, no POST enter
    await flow.enter("A2");
    expect(api.calls.filter((c) => c === "enter:A2")).toHaveLength(1);
  });

  it("继续拆细 posts the split with this goal's map; the point becomes a sub-block (进入 shows its parts), nothing entered", async () => {
    const { api, flow, fails } = await nested();
    await flow.enter("A");
    const splitting = flow.splitPart("A2");
    expect(flow.state.entered?.splittingPart).toBe("A2");
    const out = await splitting;
    expect(out?.children).toEqual(["A2-1", "A2-2"]);
    expect(api.calls).toContain(`split:A2:${NESTED_MAP}`);
    expect(flow.state.entered).toMatchObject({ region: "A", splittingPart: null });
    expect(nodeOf(flow.state.map, "A2")?.enter).toBe("children");
    expect(names(flow, "A2")).toEqual([["换水（1）", "learn"], ["换水（2）", "learn"]]);
    expect(api.calls.filter((c) => c.startsWith("enter:"))).toEqual(["enter:A"]);
    expect(fails).toEqual([]);
  });

  it("a failed split (502 twice on the server) leaves the node unexpanded with the plain line", async () => {
    const { api, flow, fails } = await nested();
    vi.spyOn(api, "enterRegion").mockRejectedValueOnce(new ApiError(502, "split failed"));
    await flow.enter("A");
    expect(fails.length).toBe(1);
    expect([flow.state.stage, flow.state.entered]).toEqual(["path", null]);
    expect(partsOf(flow.state.map, "A")).toEqual([]);
  });
});
