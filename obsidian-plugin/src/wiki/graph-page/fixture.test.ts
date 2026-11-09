import { describe, expect, it } from "vitest";
import { ApiError } from "../../api/errors";
import { DemoWikiApi } from "../fixtures-demo";
import { FakeWikiApi } from "../fake";
import { bandOfShares, childOffset, CURRENT_GRAPH, DEMO_L1, GraphFixture, MINE_NEW_SUMMARY, ringOffset } from "./fixture";
import { parseGraph, parseNodeAction, parsePathEdit, parseSplit } from "./parse";

const last = <T>(a: readonly T[]): T => a[a.length - 1]!;
const node = (g: ReturnType<GraphFixture["graph"]>, id: string) => g.nodes.find((n) => n.id === id)!;

describe("GraphFixture: graph.html's data on the wire contract", () => {
  it("passes the contract check; every graph.html node, the containers as nodes, prerequisites with their phrases", () => {
    const g = parseGraph(new GraphFixture().graph());
    const ids = g.nodes.map((n) => n.id);
    for (const k of ["vec", "mul", "eig", "der", "grad", "mean", "var", "cond", "py", "gd", "linreg", "loss", "logreg", "svm", "tree", "nn", "reg", "of", "km", "pca"]) expect(ids).toContain(k);
    for (const k of ["la", "ca", "pr", "math", "opt", "sl", "ev", "ul", "ml", "tools"]) expect(ids).toContain(k);
    expect(node(g, "la").parents).toEqual(["math"]);
    expect(node(g, "pca").parents).toEqual(["ul", "la"]);
    expect(g.relations).toContainEqual({ kind: "prerequisite", src: "vec", dst: "mul", phrase: "一行乘一列就是点积", verified: false, split: false });
    expect(g.relations).toContainEqual({ kind: "part_of", src: "la", dst: "math", phrase: "", verified: false, split: false });
    expect(node(g, "svm").kind).toBe("一种分类模型");
    expect(node(g, "mul")).toMatchObject({ band: "shaky", mix: { shaky: 1 }, direct: { shaky: 1 }, aggregate: {}, can_split: true,
      evidence: ["测验答错：2×3 乘 3×4 的结果形状"] });
    expect(node(g, "cond")).toMatchObject({ band: "none", mix: {}, direct: {} });
    expect(node(g, "la").evidence).toEqual(["精髓题答对：奇异值分解在做什么"]);
  });

  it("groups: level 2 = VIEWS.all.l2, level 1 = VIEWS.ml.l1 with names, summaries and at positions", () => {
    const g = new GraphFixture().graph();
    expect(g.groups.filter((x) => x.level === 2).map((x) => [x.id, x.name, x.summary])).toEqual([
      ["A", "数学", "代数、微积分和概率统计"], ["B", "机器学习", "从数据里学出模型，再检验它好不好"], ["C", "工具", "动手写代码"]]);
    const l1 = g.groups.filter((x) => x.level === 1);
    expect(l1.map((x) => x.id)).toEqual(["la", "ca", "pr", "opt", "sl", "ev", "ul", "tools"]);
    expect(l1[0]).toMatchObject({ name: "线性代数", summary: "矩阵和向量的运算", x: -0.95, y: -0.55, members: ["vec", "mul", "eig", "pca"], parent: "A", map_id: "map_math" });
  });

  it("node positions are the demo's ring around the group's at (a lone member sits on it)", () => {
    const g = new GraphFixture().graph();
    expect([node(g, "py").x, node(g, "py").y]).toEqual([-0.15, -0.95]);
    const o = ringOffset("la", DEMO_L1.la![3], 1, "mul");
    expect(node(g, "mul").x).toBeCloseTo(-0.95 + o.dx, 12);
    expect(node(g, "mul").y).toBeCloseTo(-0.55 + o.dy, 12);
  });

  it("goals: the two focus questions with their subsets, shares (no likely key) and paths", () => {
    const g = new GraphFixture().graph();
    expect(g.goals.map((x) => x.goal)).toEqual(["我要学机器学习", "我要学数学"]);
    expect(g.goals.map((x) => x.name)).toEqual(["机器学习", "数学"]);   // the topic: what the rows show
    const math = g.goals[1]!;
    expect(math.nodes).toEqual(["vec", "mul", "eig", "der", "grad", "mean", "var", "cond", "pca"]);
    // known: vec der mean var (4); shaky: mul eig (2); gap: grad (1); none: cond pca (2)
    expect(math.shares).toEqual({ known: 4 / 9, shaky: 2 / 9, gap: 1 / 9, none: 2 / 9 });
    expect(math.path.map((p) => p.title)).toEqual(["向量和矩阵乘法", "特征值与特征向量", "导数和梯度", "条件概率"]);
    expect(g.goals[0]!.nodes).toHaveLength(20);
    expect(g.goals[0]!.path[0]).toEqual({ key: "path_ml_1", title: "矩阵乘法", summary: "", question: "两张表怎么乘出一张新表", node_ids: ["mul"],
      status: "not_started", state: "planned", progress: null, skipped: false, mine: false, page_id: null, trashed: false, skippable: false, opened: false, sections: [], exam: 0 });
  });

  it("split 矩阵乘法: the demo's three children with their bands and evidence, placed where the demo grows them; once only", () => {
    const fx = new GraphFixture();
    const out = parseSplit(fx.split("mul"));
    expect(out.children).toEqual(["rc", "dim", "comm"]);
    const g = out.graph, mul = node(g, "mul");
    expect(node(g, "rc")).toMatchObject({ name: "一行乘一列", band: "known", parents: ["mul"], evidence: ["拆细后单独测：答对"], can_split: false });
    expect(node(g, "dim")).toMatchObject({ name: "维度要对上", band: "gap", evidence: ["拆细后单独测：答错"] });
    expect(node(g, "comm")).toMatchObject({ name: "不满足交换律", band: "none", evidence: [] });
    expect(mul.can_split).toBe(false);
    expect(mul.band).toBe("shaky"); // direct cells weighted 2:1 over the children
    expect(mul.aggregate).toEqual({ known: 1 / 3, gap: 1 / 3, none: 1 / 3 });
    const ang = ringOffset("la", DEMO_L1.la![3], 1, "mul").ang, o = childOffset(ang, 0, 3);
    expect(node(g, "rc").x).toBeCloseTo(mul.x + o.dx, 12);
    expect(g.groups.find((x) => x.id === "la")!.members).toEqual(["vec", "mul", "eig", "pca", "rc", "dim", "comm"]);
    expect(() => fx.split("mul")).toThrow(ApiError);
    expect(() => fx.split("vec")).toThrow(ApiError);
  });

  it("want: a page of the learner's own (mine) at the end; already in the path is a 409", () => {
    const fx = new GraphFixture();
    const out = parseNodeAction(fx.want("py", "map_math"));
    expect(out.path_id).toBe("path_math");
    expect(last(out.path)).toMatchObject({ title: "Python", mine: true });
    const g = parseGraph(fx.graph());
    expect(last(g.goals[1]!.path)).toMatchObject({ title: "Python", question: "你加的", node_ids: ["py"], mine: true });
    // Only the path changes (graph.html; the server's want_node): the goal's subset and its share bar stay.
    expect(g.goals[1]!.nodes).not.toContain("py");
    expect(() => fx.want("py", "map_math")).toThrow(ApiError);
  });

  it("path edits: move, remove, add; out of range / empty is a 422, an unknown path a 404", () => {
    const fx = new GraphFixture();
    let p = parsePathEdit(fx.editPath("path_math", { op: "move", from: 0, to: 2 })).path;
    expect(p.map((x) => x.title)).toEqual(["特征值与特征向量", "导数和梯度", "向量和矩阵乘法", "条件概率"]);
    p = fx.editPath("path_math", { op: "remove", index: 3 }).path;
    expect(p).toHaveLength(3);
    p = fx.editPath("path_math", { op: "add", title: " 交叉验证 " }).path;
    expect(last(p)).toMatchObject({ title: "交叉验证", question: MINE_NEW_SUMMARY, node_ids: [], mine: true });
    expect(fx.graph().goals[1]!.path).toEqual(p);
    // unremove puts the removed row back where it was
    const key = p[1]!.key, n = p.length;
    const gone = fx.editPath("path_math", { op: "remove", index: 1 });
    const back = fx.editPath("path_math", { op: "unremove", page_key: key }).path;
    expect(back.map((r) => r.key).indexOf(key)).toBe(1);
    expect(gone.path).toHaveLength(n - 1);
    expect(back).toHaveLength(n);
    expect(() => fx.editPath("path_math", { op: "unremove", page_key: key })).toThrow(ApiError);
    expect(() => fx.editPath("path_math", { op: "move", from: 0, to: 9 })).toThrow(ApiError);
    expect(() => fx.editPath("path_math", { op: "add", title: "  " })).toThrow(ApiError);
    expect(() => fx.editPath("nope", { op: "remove", index: 0 })).toThrow(ApiError);
  });

  it("bandOfShares: nearest score of the evidence; no evidence is none", () => {
    expect(bandOfShares({})).toBe("none");
    expect(bandOfShares({ none: 1 })).toBe("none");
    expect(bandOfShares({ known: 0.5, none: 0.5 })).toBe("known");
    expect(bandOfShares({ known: 0.5, gap: 0.5 })).toBe("shaky");
  });
});

describe("the fakes serve the graph (FakeWikiApi, DemoWikiApi)", () => {
  it("both answer graph / split / want / editPath from graph.html's data and record the calls", async () => {
    // the dev fixture serves graph.html as it is now (4 goals, one node per row: 13 rows on 我要学机器学习)
    for (const [api, goals, rows] of [[new FakeWikiApi(), 2, 8], [new DemoWikiApi(), 4, 12]] as const) {
      expect(parseGraph(await api.graph()).goals).toHaveLength(goals);
      expect((await api.splitNode("mul")).children).toEqual(["rc", "dim", "comm"]);
      expect(last((await api.wantNode("py", "map_math")).path).mine).toBe(true);
      expect((await api.editPath("path_ml", { op: "remove", index: 0 })).path).toHaveLength(rows);
      expect(api.calls.map((c) => c.method)).toEqual(["graph", "splitNode", "wantNode", "editPath"]);
    }
  });

  it("the usage limit blocks only 看不懂 (the one model call); reads and the other actions go through", async () => {
    const api = new FakeWikiApi();
    api.__budget(true);
    await expect(api.graph()).resolves.toBeTruthy();
    await expect(api.splitNode("mul")).rejects.toMatchObject({ status: 429 });
    await expect(api.editPath("path_ml", { op: "remove", index: 0 })).resolves.toBeTruthy();
  });
});

describe("the dev fixture's rows (Task 16 comparison)", () => {
  it("pageIds: every row has its page (write-all), so the rows can show a writing status", () => {
    const fx = new GraphFixture({ pageIds: true, data: CURRENT_GRAPH });
    expect(fx.graph().goals[0]!.path.map((r) => r.page_id)).toEqual(fx.graph().goals[0]!.path.map((r) => `pg_${r.key}`));
  });
});
