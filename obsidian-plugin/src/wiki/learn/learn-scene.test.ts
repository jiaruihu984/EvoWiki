import { copyId, type Scene, type SceneGroup1, type SceneNode } from "../graph-page/scene";
import { describe, expect, it } from "vitest";
import { zoomTitles } from "../graph-page/sigma-cloud";
import type { GoalMapOut, MapNodeOut } from "../schema";
import { draftMap } from "./learn-flow";
import { learnScene } from "./learn-scene";

describe("学点新的's scene draws actual map nodes", () => {
  it("an unsplit region stays one point regardless of the concepts named in its scope, in draft and confirmed maps", () => {
    const map = draftMap([{ key: "wq", name: "水质与环境", scope: "水温、pH 和 GH / KH、硝化" }, { key: "x", name: "空块", scope: "" }]);
    for (const value of [map, { ...map, map_id: "confirmed-map" }]) {
      const { scene, goalOf } = learnScene(null, value);
      expect(scene.order).toEqual(["wq", "x"]);
      expect([...goalOf.keys()]).toEqual(["wq", "x"]);
      expect(scene.l1.map((g) => [g.id, g.name, g.all.length])).toEqual([["wq", "水质与环境", 1], ["x", "空块", 1]]);
      expect(scene.l1[0]!.summary).toBe("水温、pH 和 GH / KH、硝化");
      expect([...scene.nodes.values()].flatMap((n) => n.pre)).toEqual([]);
    }
  });

  it("a block inside a block stays in its top block's cloud, not a cloud far away of its own", () => {
    const map = draftMap([{ key: "brew", name: "冲煮参数与手法", scope: "" }]);
    const top = map.nodes.find((n) => n.id === "brew")!;
    const parts: MapNodeOut[] = [{ ...top, id: "pour", name: "注水手法", parent: "brew" }, { ...top, id: "temp", name: "水温", kind: "concept", parent: "brew" }];
    const { scene, goalOf } = learnScene(null, { ...map, nodes: [...map.nodes, ...parts] });
    expect(scene.l1.map((g) => g.id)).toEqual(["brew"]);
    expect(scene.nodes.get("pour")!.g).toBe("brew");
    expect(scene.nodes.get("temp")!.g).toBe("brew");
    expect(goalOf.get("pour")).toBe("brew");
  });

  it("the goal's blocks stand under one title, the goal's, as his regions under theirs", () => {
    const map = draftMap([{ key: "wq", name: "水质与环境", scope: "水温、硝化" }, { key: "x", name: "空块", scope: "" }]);
    const { scene } = learnScene(null, map, { places: { wq: [0, 1], x: [1, 0] }, links: [] }, "黑壳虾");
    const goal = scene.l2.find((g) => g.goal === true)!;
    expect([goal.name, goal.kids, goal.at]).toEqual(["黑壳虾", ["wq", "x"], [0.5, 0.5]]);
    expect(scene.l1.map((g) => g.parent)).toEqual([goal.id, goal.id]);
    expect(zoomTitles(scene).titles.filter((t) => t.level === "top").map((t) => t.name)).toEqual(["黑壳虾"]);
  });

  it("actual child nodes grow from their region after splitting", () => {
    const base = draftMap([{ key: "wq", name: "水质与环境", scope: "水温、硝化" }]);
    const pt = (id: string, name: string): MapNodeOut => ({ ...base.nodes[0]!, id, name, kind: "concept", parent: "wq", scope: "", enter: "learn" });
    const map: GoalMapOut = { ...base, nodes: [...base.nodes, pt("c1", "水温"), pt("c2", "硝化")] };
    const { scene } = learnScene(null, map);
    expect(scene.order).toEqual(["wq", "c1", "c2"]);
    expect(scene.nodes.get("c1")!.pre.map((p) => p.id)).toEqual(["wq"]);
  });

  it("the goal's dots stand where the map has this goal's, not on the other goal it shares a point with; the camera frames those only", () => {
    const base0 = draftMap([{ key: "wq", name: "水质", scope: "" }]);
    const map: GoalMapOut = { ...base0, map_id: "fish", nodes: [...base0.nodes, { ...base0.nodes[0]!, id: "ph", name: "pH", kind: "concept", parent: "wq", scope: "", enter: "learn" }] };
    const dot = (id: string, g: string, x: number, y: number): SceneNode => ({ id, name: id, band: "none", kind: "", evidence: [], parents: [], x, y, g, child: false, from: null, pre: [] });
    const blk = (id: string, all: string[], parent: string): SceneGroup1 => ({ id, name: id, summary: "", x: 0, y: 0, parent, at: null, members: all, all });
    const mine = copyId("ph", "block-fish-wq");
    const k: Scene = { order: ["ph", mine], names: new Map(),
      nodes: new Map([["ph", dot("ph", "block-shrimp-wq", 5, 5)], [mine, dot(mine, "block-fish-wq", -3, 2)]]),
      l1: [blk("block-shrimp-wq", ["ph"], "goal-shrimp"), blk("block-fish-wq", [mine], "goal-fish")],
      l2: [{ id: "goal-shrimp", name: "黑壳虾", summary: "", x: 0, y: 0, at: null, kids: ["block-shrimp-wq"], goal: true, mapId: "shrimp" }, { id: "goal-fish", name: "孔雀鱼", summary: "", x: 0, y: 0, at: null, kids: ["block-fish-wq"], goal: true, mapId: "fish" }] };
    const { scene, goalDots } = learnScene(k, map, null, "孔雀鱼");
    const here = copyId("ph", "wq");
    expect([scene.nodes.get(here)!.x, scene.nodes.get(here)!.y]).toEqual([-3, 2]);
    expect(scene.nodes.get("ph")!.x).toBe(5);
    expect(goalDots.has(here)).toBe(true);
    expect(goalDots.has("ph")).toBe(false);
    // the block's own point in the middle of its own dots
    expect(scene.nodes.get("wq")!.x).toBeLessThan(0);
  });

  it("a draft's point taken from a cluster stands round its block once the block has its place: no line across the map", () => {
    const base0 = draftMap([{ key: "wq", name: "水质", scope: "" }]);
    const map: GoalMapOut = { ...base0, nodes: [...base0.nodes, { ...base0.nodes[0]!, id: "ph", name: "pH", kind: "concept", parent: "wq", scope: "", enter: "learn" }] };
    const far: SceneNode = { id: "ph", name: "ph", band: "none", kind: "", evidence: [], parents: [], x: 9, y: 9, g: "cl", child: false, from: null, pre: [] };
    const k: Scene = { order: ["ph"], names: new Map(), nodes: new Map([["ph", far]]), l1: [{ id: "cl", name: "簇", summary: "", x: 0, y: 0, parent: null, at: null, members: ["ph"], all: ["ph"] }], l2: [] };
    const { scene } = learnScene(k, map, { places: { wq: [0, 0] }, links: [] }, "孔雀鱼");
    expect(Math.hypot(scene.nodes.get("ph")!.x, scene.nodes.get("ph")!.y)).toBeLessThan(1);
    expect(Math.hypot(scene.nodes.get("wq")!.x, scene.nodes.get("wq")!.y)).toBeLessThan(1);
  });

  const point = (id: string, g: string, band: SceneNode["band"] = "none"): SceneNode => ({ id, name: id, band, kind: "", evidence: [], parents: [], x: 0, y: 0, g, child: false, from: null, pre: [] });
  const group = (id: string, all: string[], parent: string | null = null): SceneGroup1 => ({ id, name: id + "名", summary: "", x: 0, y: 0, parent, at: null, members: all, all });

  it("what the existing map draws of this goal gives way to its blocks; his other groups stay", () => {
    const base = draftMap([{ key: "wq", name: "水质与环境", scope: "水温" }]);
    const k: Scene = { order: ["wq", "z"], nodes: new Map([["wq", point("wq", "b")], ["z", point("z", "his")]]), names: new Map(), l1: [group("b", ["wq"], "G"), group("his", ["z"])],
      l2: [{ id: "G", name: "目标名", summary: "", x: 0, y: 0, at: null, kids: ["b"], goal: true, mapId: base.map_id }, { id: "his", name: "his名", summary: "", x: 0, y: 0, at: null, kids: ["his"] }] };
    const { scene } = learnScene(k, base, null, "黑壳虾");
    expect(scene.l1.map((g) => g.id)).toEqual(["his", "wq"]);
    expect(scene.l2.map((g) => g.name)).toEqual(["his名", "黑壳虾"]);
    expect(scene.nodes.get("wq")!.g).toBe("wq");
  });

  it("a block another goal also has is a dot in each, of one band; a point of a cluster moves to the goal", () => {
    const base = draftMap([{ key: "wq", name: "水质与环境", scope: "" }, { key: "c", name: "簇里的", scope: "" }]);
    const k: Scene = { order: ["wq", "c", "z"], nodes: new Map([["wq", point("wq", "other", "known")], ["c", point("c", "his")], ["z", point("z", "his")]]), names: new Map(),
      l1: [group("other", ["wq"], "G2"), group("his", ["c", "z"])],
      l2: [{ id: "G2", name: "别的目标", summary: "", x: 0, y: 0, at: null, kids: ["other"], goal: true, mapId: "m2" }] };
    const { scene, goalOf } = learnScene(k, base, null, "黑壳虾");
    const dot = copyId("wq", "wq");
    expect(scene.nodes.get("wq")!.g).toBe("other");
    expect(scene.nodes.get(dot)).toMatchObject({ g: "wq", band: "known" });
    expect([goalOf.get(dot), goalOf.get("wq")]).toEqual(["wq", "wq"]);
    expect(scene.nodes.get("c")!.g).toBe("c");
    expect(scene.l1.find((g) => g.id === "his")!.all).toEqual(["z"]);
  });

  it("splitting grows only actual children and preserves the region's canonical place and the existing map", () => {
    const base = draftMap([{ key: "wq", name: "水质与环境", scope: "水温、硝化" }]);
    const pt = (id: string, name: string): MapNodeOut => ({ ...base.nodes[0]!, id, name, kind: "concept", parent: "wq", scope: "", enter: "learn" });
    const old = { ...point("existing", "his"), x: -4, y: 3 };
    const k: Scene = { order: [old.id], nodes: new Map([[old.id, old]]), names: new Map(), l1: [group("his", [old.id])], l2: [] };
    const places = { places: { wq: [2, 1] as [number, number] }, links: [] };
    const before = learnScene(k, base, places).scene, { scene, goalOf } = learnScene(k, { ...base, nodes: [...base.nodes, pt("c1", "水温"), pt("c2", "硝化"), pt("c3", "溶氧")] }, places);
    expect(scene.nodes.get("c1")!.pre.map((p) => p.id)).toEqual(["wq"]);
    expect(scene.order).toEqual(["existing", "wq", "c1", "c2", "c3"]);
    expect(scene.nodes.get("existing")).toEqual(before.nodes.get("existing"));
    expect(scene.nodes.get("wq")).toEqual(before.nodes.get("wq"));
    expect(scene.l1.find((g) => g.id === "wq")!.at).toEqual([2, 1]);
    expect(k.nodes.get("existing")).toEqual(old);
    expect(goalOf.get("wq")).toBe("wq");
  });
});
