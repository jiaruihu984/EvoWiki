import { describe, expect, it, vi } from "vitest";
import type { WikiHttp, WikiHttpRequest } from "../api";
import type { GoalMapOut, MapNodeOut } from "../schema";
import { HttpGoalApi } from "./goal-api";
import { conceptId, FakeGoalApi, fakeBlocks, FixtureGoalApi, registerBlockGoal } from "./goal-fake";

const api = (): FakeGoalApi => new FakeGoalApi({ roundMs: 0 });
const DEMO = "我要学机器学习";
/** A goal confirmed in the fixture's chat with the blocks its assistant proposed. */
const confirmed = (goal: string): { draftId: string; mapId: string } => registerBlockGoal(goal, fakeBlocks(goal));
const node = (m: GoalMapOut | null, id: string): MapNodeOut => {
  const n = m?.nodes.find((x) => x.id === id);
  if (!n) throw new Error(`no node ${id}`);
  return n;
};
describe("FixtureGoalApi (plugin data fixture \"demo\"): the fake answers only its own drafts", () => {
  /** A recording transport: never the real server. */
  const recording = (): WikiHttp & { calls: WikiHttpRequest[] } => {
    const calls: WikiHttpRequest[] = [];
    const draft = { draft_id: "d-live", round: 1, round_title: "", round_why: "", questions: [], results: [], map: null, map_id: null,
      path_id: null, page_id: null, goal: "奇异值分解" };
    return { calls, request: vi.fn((r: WikiHttpRequest) => { calls.push(r); return Promise.resolve({ status: 200, text: JSON.stringify(draft) }); }) };
  };
  const cfg = { baseUrl: "http://h:1", userId: "jerry" };

  it("a draft of the fixture's chat is answered by the scripted fake: nothing is sent; any other goes to the live routes and stays there", async () => {
    const t = recording();
    const router = new FixtureGoalApi(api(), new HttpGoalApi(() => cfg, t));
    expect(router.live).toBe(false);                       // nothing chosen yet
    const fake = await router.getDraft(confirmed(DEMO).draftId);
    expect(fake.draft_id).toMatch(/^fake-draft-/);
    expect(router.live).toBe(false);
    expect(t.calls).toEqual([]);
    const out = await router.getDraft("d-live");
    expect(out.draft_id).toBe("d-live");
    expect(router.live).toBe(true);
    await router.answer("d-live", { skip: true });
    expect(t.calls.map((r) => [r.method, r.url])).toEqual([["GET", "http://h:1/wiki/goals/d-live"], ["POST", "http://h:1/wiki/goals/d-live/answers"]]);
  });
});

describe("the fake reuses the demo K's ids (as the fixed backend does)", () => {
  it("线性代数 is the demo hub's id la; 矩阵乘法 / 向量 / 方差 keep the demo K's concept ids; the rest are <region>.<concept>", async () => {
    expect([conceptId("la", "mul"), conceptId("la", "vec"), conceptId("pr", "var"), conceptId("la", "mat")]).toEqual(["mul", "vec", "var", "la.mat"]);
    const a = new FakeGoalApi({ roundMs: 0 });
    const r1 = await a.getDraft(confirmed(DEMO).draftId);
    expect(r1.map!.nodes.find((n) => n.name === "线性代数")!.id).toBe("la");
    const done = await a.answer(r1.draft_id!, { skip: true });
    await a.enterRegion(done.map_id!, "la");
    const map = await a.getMap(done.map_id!);
    const kids = map.nodes.filter((n) => n.parent === "la");
    expect(kids.map((n) => [n.id, n.prereqs])).toEqual([["la.mat", []], ["mul", ["la.mat"]], ["vec", []]]);
  });

  it("the final map contract: members, reused, regions in prerequisite order each followed by its members, concepts always expanded", async () => {
    const a = new FakeGoalApi({ roundMs: 0 });
    const r1 = await a.getDraft(confirmed(DEMO).draftId);
    const m = r1.map!;
    const regions = m.nodes.filter((n) => n.kind === "region");
    // Each region is followed by exactly its members (concept nodes parented by it), in members order.
    const order: string[] = regions.flatMap((r) => [r.id, ...r.members]);
    expect(m.nodes.map((n) => n.id)).toEqual(order);
    for (const r of regions) for (const id of r.members) expect(node(m, id)).toMatchObject({ kind: "concept", parent: r.id, members: [] });
    expect(node(m, "la").members).toEqual(["la.mat", "mul", "vec"]);
    // 线性代数 is the demo K's existing topic (hub_id la); 矩阵乘法 / 向量 / 方差 exist in the demo K.
    expect(regions.filter((r) => r.reused).map((r) => r.id)).toEqual(["la"]);
    expect(m.nodes.filter((n) => n.kind === "concept" && n.reused).map((n) => n.id)).toEqual(["mul", "vec", "var"]);
    expect(m.nodes.filter((n) => n.kind === "concept").every((n) => n.state === "expanded")).toBe(true);
    // Self-reports target the region (hub) id with the region's name.
    expect(r1.questions.map((q) => [q.target, q.concept_id, q.concept_name])[0]).toEqual(["la", "la", "线性代数"]);
    // collapsed until entered; 进入 answers a path_id and the region is expanded with it set.
    const done = await a.answer(r1.draft_id!, { skip: true });
    const ent = await a.enterRegion(done.map_id!, "pr");
    expect([ent.path_id, node(ent.map, "pr").state, node(ent.map, "pr").path_id, node(ent.map, "ca").state]).toEqual(["fake-path-pr", "expanded", "fake-path-pr", "collapsed"]);
  });
});

describe("the fake's chat-first blocks (big-goal-demo.html 黑壳虾, useBlocks)", () => {
  it("the assistant's first draft is the topic's regions; 黑壳虾 has its own background", async () => {
    const { fakeBlocks, TOPICS } = await import("./goal-fake");
    expect(fakeBlocks("我要学黑壳虾").map((b) => [b.name, b.level])).toEqual([["分类与鉴别", "基础"], ["水质与环境", "基础"], ["开缸与日常", "核心"], ["营养与摄食", "核心"], ["生长与繁殖", "进阶"], ["疾病与风险", "进阶"]]);
    expect(TOPICS["黑壳虾"]!.bg).toBe("学过高中生物，想知道黑壳虾是什么、怎么养。");
  });

  it("confirmed blocks (水质与环境 merged into 开缸与日常; 营养 and 生长 dropped): GET /wiki/goals answers round 1 for them; a merged block carries both parts", async () => {
    const { registerBlockGoal } = await import("./goal-fake");
    const ids = registerBlockGoal("我要学黑壳虾", [
      { id: "id", name: "分类与鉴别", scope: "俗称指哪种虾", level: "基础", from: ["id"] },
      { id: "keep", name: "开缸与日常", scope: "水温、开缸", level: "核心", from: ["wq", "keep"] },
      { id: "sick", name: "疾病与风险", scope: "应激信号", level: "进阶", from: ["sick"] },
    ]);
    const a = api();
    const r1 = await a.getDraft(ids.draftId);
    expect(r1.draft_id).toBe(ids.draftId);
    const regions = r1.map!.nodes.filter((n) => n.kind === "region");
    expect(regions.map((r) => [r.id, r.name, r.level, r.target])).toEqual([["id", "分类与鉴别", "基础", false], ["keep", "开缸与日常", "核心", true], ["sick", "疾病与风险", "进阶", true]]);
    expect(r1.map!.nodes.filter((n) => n.kind === "concept").every((n) => n.level === "")).toBe(true);
    expect(node(r1.map, "keep").members).toEqual(["keep.temp", "keep.ph", "keep.nh3", "keep.cycle", "keep.test", "keep.setup", "keep.acc", "keep.change", "keep.watch"]);
    // the self-reports follow their block, once per block
    expect(r1.questions.map((q) => [q.target, q.stem])).toEqual([["id", "以前了解过黑壳虾吗？"], ["keep", "养鱼时测过水吗？"]]);
    const r2 = await a.answer(ids.draftId, { answers: [0, 0] });
    expect(r2.questions.map((q) => [q.target, q.stem])).toEqual([["id", "「黑壳虾」这个名字是？"], ["keep", "新开的缸第二天就放虾，最可能出什么问题？"]]);
    await expect(api().getDraft("fake-draft-unknown")).rejects.toMatchObject({ status: 404 });
  });

  it("学习路径 edits: reorder / rename / add / drop, and restore takes one back", async () => {
    const a = api();
    const r1 = await a.getDraft(confirmed("我要学黑壳虾").draftId);
    const done = await a.answer(r1.draft_id!, { skip: true });
    const mapId = done.map_id!;
    const tops = (m: GoalMapOut): string[] => m.nodes.filter((n) => n.kind === "region").map((n) => n.name);
    let out = await a.editMap(mapId, [{ op: "reorder", ids: ["wq", "id", "keep", "food", "br", "sick"] }, { op: "rename", id: "wq", name: "水" }]);
    expect(tops(out.map)).toEqual(["水", "分类与鉴别", "开缸与日常", "营养与摄食", "生长与繁殖", "疾病与风险"]);
    out = await a.editMap(mapId, [{ op: "add", name: "造景" }]);
    expect(tops(out.map).slice(-1)).toEqual(["造景"]);
    const dropped = await a.editMap(mapId, [{ op: "drop", id: "br" }]);
    expect(tops(dropped.map)).not.toContain("生长与繁殖");
    const back = await a.editMap(mapId, dropped.undo);
    expect(tops(back.map)).toContain("生长与繁殖");
    await expect(a.editMap(mapId, dropped.undo)).rejects.toMatchObject({ status: 409 });
    await expect(a.editMap(mapId, [{ op: "drop", id: "nope" }])).rejects.toMatchObject({ status: 422 });
  });

  it("a block's pages: move / remove / unremove / add, 拆成多页 (是什么 / 怎么做, not split again) and its undo", async () => {
    const a = api();
    const r1 = await a.getDraft(confirmed("我要学黑壳虾").draftId);
    const done = await a.answer(r1.draft_id!, { skip: true });
    const ent = await a.enterRegion(done.map_id!, "wq");
    const pid = ent.path_id!;
    const titles = (o: { path: { title: string }[] }): string[] => o.path.map((p) => p.title);
    expect(titles(await a.editPath(pid, { op: "move", from: 0, to: 2 }))).toEqual(["氨氮、亚硝酸盐和硝化", "怎么测、怎么调", "黑壳虾要什么样的水"]);
    expect(titles(await a.editPath(pid, { op: "remove", index: 0 }))).toEqual(["怎么测、怎么调", "黑壳虾要什么样的水"]);
    expect(titles(await a.editPath(pid, { op: "unremove", page_key: `${pid}-p2` }))).toEqual(["氨氮、亚硝酸盐和硝化", "怎么测、怎么调", "黑壳虾要什么样的水"]);
    const split = await a.splitRow(pid, `${pid}-p3`);
    expect(titles(split)).toEqual(["氨氮、亚硝酸盐和硝化", "怎么测、怎么调是什么", "怎么测、怎么调怎么做", "黑壳虾要什么样的水"]);
    expect(split.undo).not.toBeNull();
    await expect(a.splitRow(pid, split.path[1]!.key)).rejects.toMatchObject({ status: 422 });
    expect(titles(await a.undoSplitRow(pid, `${pid}-p3`, split.undo!))).toEqual(["氨氮、亚硝酸盐和硝化", "怎么测、怎么调", "黑壳虾要什么样的水"]);
    expect(titles(await a.editPath(pid, { op: "add", title: "造景" })).slice(-1)).toEqual(["造景"]);
  });
});
