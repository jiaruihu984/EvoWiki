import { describe, expect, it } from "vitest";
import type { GraphOut, PathItemOut } from "../schema";
import { GraphFixture } from "./fixture";
import { actionGoal, cardButtons, movedPath, optimisticPath, parentNames, shareSegments } from "./panel-logic";
import { buildScene, copyId, focusTest, realId } from "./scene";

describe("buildScene", () => {
  it("draws the group members (graph.html's view members), not the containers; draw order follows the groups", () => {
    const S = buildScene(new GraphFixture().graph());
    expect(S.order).toEqual(["vec", "mul", "eig", "pca", "der", "grad", "mean", "var", "cond", "gd", "linreg", "loss", "logreg", "svm", "tree", "nn",
      "reg", "of", "km", copyId("pca", "ul"), "py"]);
    expect(S.nodes.get("pca")).toMatchObject({ g: "la", child: false, parents: ["ul", "la"] });
    expect(S.nodes.get("linreg")!.pre).toEqual([{ id: "mul", phrase: "预测值 = Xw，就是矩阵乘法" }, { id: "gd", phrase: "用梯度下降找到最好的 w" }]);
    expect(S.names.get("la")).toBe("线性代数");
    expect(S.l1.map((g) => g.id)).toEqual(["la", "ca", "pr", "opt", "sl", "ev", "ul", "tools"]);
    expect(S.l2.map((g) => [g.id, g.kids])).toEqual([["A", ["la", "ca", "pr"]], ["B", ["opt", "sl", "ev", "ul"]], ["C", ["tools"]]]);
  });

  it("split children are children of the drawn parent, in its group, drawn right after it, not counted as ring members", () => {
    const S = buildScene(new GraphFixture().split("mul").graph);
    expect(S.order.slice(0, 6)).toEqual(["vec", "mul", "rc", "dim", "comm", "eig"]);
    expect(S.nodes.get("rc")).toMatchObject({ child: true, from: "mul", g: "la" });
    expect(S.l1[0]!.members).toEqual(["vec", "mul", "eig", "pca"]);
  });

  it("a point on two goals is a dot in a block of each: one id, one band, the same in both", () => {
    const fx = new GraphFixture();
    const before = buildScene(fx.graph());
    const dot = copyId("pca", "ul");
    expect(realId(dot)).toBe("pca");
    expect([before.nodes.get("pca")!.g, before.nodes.get(dot)!.g]).toEqual(["la", "ul"]);
    expect(before.nodes.get(dot)!.band).toBe(before.nodes.get("pca")!.band);
    const g = fx.graph(), learned = g.nodes.map((n) => (n.id === "pca" ? { ...n, band: "known" as const } : n));
    const after = buildScene({ ...g, nodes: learned });
    expect([after.nodes.get("pca")!.band, after.nodes.get(dot)!.band]).toEqual(["known", "known"]);
  });

  it("a member of a goal's block stays in its block though its split parent stands elsewhere", () => {
    const node = (id: string, parents: string[] = []): GraphOut["nodes"][number] => ({ id, name: id, definition: "", band: "none", mix: {},
      direct: {}, aggregate: {}, parents, evidence: [], kind: "", x: 0, y: 0, can_split: false });
    const grp = (id: string, level: 1 | 2, members: string[], parent: string | null, map_id: string | null): GraphOut["groups"][number] => ({ id, level, name: id, summary: "", members, parent, x: 0, y: 0, at: null, map_id, copies: {} });
    const S = buildScene({ nodes: [node("p"), node("k", ["p"])], relations: [{ kind: "part_of", src: "k", dst: "p", phrase: "", verified: false, split: true }],
      groups: [grp("G", 2, ["k"], null, "m"), grp("B", 1, ["k"], "G", "m"), grp("c", 1, ["p"], null, null)], goals: [], domains: [] });
    expect(S.nodes.get("k")!.g).toBe("B");
    expect(S.l2.find((x) => x.id === "G")).toMatchObject({ goal: true, mapId: "m", kids: ["B"] });
  });

  it("two groups of one name are one region: the first takes the second's points", () => {
    const group = (id: string, members: string[]): GraphOut["groups"][number] => ({ id, level: 1, name: "黑壳虾", summary: "", members, parent: null, x: 0, y: 0, at: null, map_id: null, copies: {} });
    const base = new GraphFixture().graph(), n = (id: string): GraphOut["nodes"][number] => ({ ...base.nodes[0]!, id, name: id, parents: [] });
    const S = buildScene({ ...base, nodes: [n("a"), n("b")], relations: [], groups: [group("g1", ["a"]), group("g2", ["b"])] });
    expect(S.l1.map((g) => [g.id, g.all])).toEqual([["g1", ["a", "b"]]]);
    expect(S.l2.map((g) => g.id)).toEqual(["g1"]);
    expect(S.nodes.get("b")!.g).toBe("g1");
  });
  it("a leaf outside every group is still drawn; a container with drawn parts is not", () => {
    const g: GraphOut = { relations: [{ kind: "part_of", src: "a", dst: "top", phrase: "", verified: false, split: false }], groups: [], goals: [], domains: [], nodes: [
      { id: "top", name: "上", definition: "", band: "none", mix: {}, direct: {}, aggregate: {}, parents: [], evidence: [], kind: "", x: 0, y: 0, can_split: false },
      { id: "a", name: "甲", definition: "", band: "known", mix: { known: 1 }, direct: { known: 1 }, aggregate: {}, parents: ["top"], evidence: [], kind: "", x: 0.1, y: 0, can_split: false },
    ] };
    const S = buildScene(g);
    expect(S.order).toEqual(["a"]);
    expect(S.nodes.get("a")).toMatchObject({ child: false, g: null });
  });

  it("a topic's parts that are not split out of it are ordinary dots of their own groups (bug 2026-10-22: a column)", () => {
    // regression: the topic 生物化学基础 is a member of its group, and so are its parts; one part sits in another group
    const node = (id: string, parents: string[] = []): GraphOut["nodes"][number] => ({ id, name: id, definition: "", band: "none", mix: {},
      direct: {}, aggregate: {}, parents, evidence: [], kind: "", x: 0, y: 0, can_split: false });
    const part = (src: string, dst: string, split: boolean): GraphOut["relations"][number] => ({ kind: "part_of", src, dst, phrase: "", verified: false, split });
    const g: GraphOut = {
      nodes: [node("hub"), node("p1", ["hub"]), node("p2", ["hub"]), node("p3", ["hub"]), node("k1", ["p3"]), node("k2", ["p3"])],
      relations: [part("p1", "hub", false), part("p2", "hub", false), part("p3", "hub", false), part("k1", "p3", true), part("k2", "p3", true)],
      groups: [{ id: "g1", level: 1, name: "甲", summary: "", members: ["hub", "p1", "p2"], parent: null, x: 0, y: 0, at: null, map_id: null, copies: {} },
        { id: "g2", level: 1, name: "乙", summary: "", members: ["p3", "k1", "k2"], parent: null, x: 0.5, y: 0, at: null, map_id: null, copies: {} }],
      goals: [], domains: [],
    };
    const S = buildScene(g);
    for (const id of ["p1", "p2"]) expect(S.nodes.get(id)).toMatchObject({ child: false, from: null, g: "g1" });
    expect(S.nodes.get("p3")).toMatchObject({ child: false, from: null, g: "g2" });     // stays in its own group
    expect(S.l1[0]!.members).toEqual(["hub", "p1", "p2"]);
    // the parts 看不懂 split out of p3 still grow out of it
    for (const id of ["k1", "k2"]) expect(S.nodes.get(id)).toMatchObject({ child: true, from: "p3", g: "g2" });
  });

  it("a level-1 group with no drawn member is not a group of the scene", () => {
    const g = new GraphFixture().graph();
    const ghost = { id: "ghost", level: 1 as const, name: "空", summary: "", members: [], parent: null, x: 0, y: 0, at: null, map_id: null, copies: {} };
    const S = buildScene({ ...g, groups: [...g.groups, ghost] });
    expect(S.l1.map((x) => x.id)).not.toContain("ghost");
    expect(S.l1).toHaveLength(8);
  });

  it("focusTest: no goal = all; a goal = the dots in its own blocks, a shared point's dot on another goal not", () => {
    const fx = new GraphFixture();
    const graph = fx.split("mul").graph, S = buildScene(graph);
    expect(focusTest(S, null)("py")).toBe(true);
    const math = focusTest(S, graph.goals[1]!);
    expect(["vec", "pca", "rc"].map(math)).toEqual([true, true, true]);
    expect(["py", "linreg", copyId("pca", "ul")].map(math)).toEqual([false, false, false]);
  });
});

describe("panel logic", () => {
  const g = new GraphFixture().graph();
  it("share bar: one segment per band with a share, in known / shaky / gap / none order", () => {
    expect(shareSegments({ known: 0.5, shaky: 0, gap: 0.25, none: 0.25 })).toEqual([{ band: "known", flex: 0.5 }, { band: "gap", flex: 0.25 }, { band: "none", flex: 0.25 }]);
  });
  it("the action goal: the one in focus, else the first", () => {
    expect(actionGoal(g.goals, "map_math")!.map_id).toBe("map_math");
    expect(actionGoal(g.goals, null)!.map_id).toBe("map_ml");
    expect(actionGoal([], null)).toBeNull();
  });
  it("card buttons (graph.html nodeCard conditions)", () => {
    const S = buildScene(g), ml = g.goals[0]!, math = g.goals[1]!;
    // spec 2026-10-21: only 我想学这个, for a node on no path (no 看不懂, no 我会了，跳过)
    expect(cardButtons(S.nodes.get("mul")!, ml)).toEqual({ want: false });
    expect(cardButtons(S.nodes.get("vec")!, ml)).toEqual({ want: true });
    expect(cardButtons(S.nodes.get("vec")!, math)).toEqual({ want: false });
    expect(cardButtons(S.nodes.get("cond")!, null)).toEqual({ want: false });
    const S2 = buildScene(new GraphFixture().split("mul").graph);
    expect(cardButtons(S2.nodes.get("dim")!, ml)).toEqual({ want: false });
    expect(parentNames(S, S.nodes.get("pca")!)).toEqual(["无监督学习", "线性代数"]);
  });
  it("path helpers: move, optimistic remove, Alt + arrows inside the ends", () => {
    const P = g.goals[1]!.path, t = (p: PathItemOut[]): string[] => p.map((x) => x.title);
    expect(t(movedPath(P, 0, 2))).toEqual(["特征值与特征向量", "导数和梯度", "向量和矩阵乘法", "条件概率"]);
    expect(t(optimisticPath(P, { op: "remove", page_key: P[1]!.key })!)).toEqual(["向量和矩阵乘法", "导数和梯度", "条件概率"]);
    expect(optimisticPath(P, { op: "add", request_id: "test-intent", title: "x" })).toBeNull();
    expect(P).toHaveLength(4);
  });
});
