/**
 * Synthetic graph fixtures retain representative layout topology and numeric ranges:
 * 75 nodes, nested groups, and a separate graph with five split children.
 * Sigma itself uses the jsdom fake (fake-sigma.ts).
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { GraphOut } from "../schema";
import { FakeSigma } from "./fake-sigma";
import { buildScene, type Scene } from "./scene";
import { cloudTitles, fitScale, GraphCloud, levelFor, tipSpot, NEAR_RATIO, PIN_SIZE, glowTint, sceneGraph, zoomTitles, type Colors, type GraphCloudHandle, type ZoomLevel } from "./sigma-cloud";
import { rgb } from "../colors";
import { learnScene } from "../learn/learn-scene";
import { draftMap } from "../learn/learn-flow";

const C: Colors = { known: "#2f9e5b", likely: "#6cbf84", shaky: "#d99a1e", gap: "#d9483b", none: "#b8b8b8", "tx-1": "#222222",
  "tx-2": "#5c5c5c", "tx-3": "#8a8a8a", edge: "#d2d2d2", accent: "#1f1f1f", bg: "#fafafa" };
const load = (f: string): GraphOut => JSON.parse(readFileSync(join(__dirname, f), "utf8")) as GraphOut;

describe.each(["synthetic-75.fixture.json", "synthetic-split.fixture.json"])("sceneGraph on a synthetic wiki (%s)", (file) => {
  const g = load(file), S = buildScene(g), G = sceneGraph(S, C);

  it("every drawn point is a node with its name and its band's colour, sized by degree; every edge joins two of them", () => {
    expect(G.nodes().sort()).toEqual([...S.order].sort());
    for (const id of G.nodes()) expect(G.getNodeAttributes(id)).toMatchObject({ label: S.nodes.get(id)!.name, color: C[S.nodes.get(id)!.band] });
    const hub = G.nodes().sort((a, b) => G.degree(b) - G.degree(a))[0]!, lone = G.nodes().find((id) => G.degree(id) === 0 && !S.nodes.get(id)!.child);
    if (lone) expect(G.getNodeAttribute(hub, "size")).toBeGreaterThan(G.getNodeAttribute(lone, "size"));
    expect(G.size).toBeGreaterThan(0);
  });

  it("titles: one per top group (level-2, or a level-1 group without one), one per level-1 group; each point under one top title", () => {
    const T = cloudTitles(S), tops = T.filter((t) => !t.near);
    const l2 = g.groups.filter((x) => x.level === 2).map((x) => x.name);
    expect(tops.map((t) => t.name)).toEqual(expect.arrayContaining(l2.filter((n) => tops.some((t) => t.name === n))));
    expect(T.filter((t) => t.near).map((t) => t.id).sort()).toEqual(S.l1.map((x) => x.id).sort());
    const under = tops.flatMap((t) => t.points);
    expect(new Set(under).size).toBe(under.length);
  });

});

it("split children are linked to their parent (a child edge)", () => {
  const S = buildScene(load("synthetic-split.fixture.json")), G = sceneGraph(S, C);
  const kids = [...S.nodes.values()].filter((n) => n.child);
  expect(kids.length).toBe(5);
  for (const k of kids) expect(G.getEdgeAttributes(G.edge(k.from!, k.id)!).kind).toBe("child");
});

describe("semantic zoom: one level of the hierarchy per zoom", () => {
  const all: ZoomLevel[] = ["domain", "top", "block", "point"];
  it("the whole map is the coarsest level; finer ones as the camera comes in", () => {
    expect([1, 0.6, 0.3, 0.1].map((r) => levelFor(all, r))).toEqual(["domain", "top", "block", "point"]);
    expect([1, 0.5, 0.1].map((r) => levelFor(["top", "block", "point"], r))).toEqual(["top", "block", "point"]);
    expect([1, 0.1].map((r) => levelFor(["top", "point"], r))).toEqual(["top", "point"]);
  });
  it("a level that does not fit in this box waits, however close the band says", () => {
    expect(levelFor(all, 0.1, undefined, () => 1)).toBe("top");
    expect(levelFor(all, 0.1, undefined, (r) => (r < 0.2 ? 3 : 1))).toBe("point");
    expect(levelFor(all, 0.3, undefined, (r) => (r < 0.2 ? 3 : 1))).toBe("top");
  });
  it("a level fits from the scale where its titles stand clear of each other and of the dots: closer titles need a closer zoom", () => {
    const t = (cx: number, y1 = 0): { w: number; h: number; cx: number; y1: number } => ({ w: 60, h: 20, cx, y1 });
    const r = (): number => 3;
    expect(fitScale([t(0), t(1)], [], r)).toBeLessThanOrEqual(72);
    expect(fitScale([t(0), t(1)], [], r)).toBeGreaterThanOrEqual(50);
    expect(fitScale([t(0), t(0.1)], [], r)).toBeGreaterThan(500);
    // a dot right where the title goes
    expect(fitScale([t(0)], [{ x: 0, y: 0.2 }], r)).toBeGreaterThan(fitScale([t(0)], [], r));
  });
  it("a tip goes under its point, clear of it, or over it at the box's bottom", () => {
    expect(tipSpot({ x: 100, y: 100, r: 30 }, 200, 50, 400, 400)).toEqual({ left: 108, top: 138 });
    expect(tipSpot({ x: 100, y: 380, r: 10 }, 200, 50, 400, 400)).toEqual({ left: 108, top: 312 });
    expect(tipSpot({ x: 390, y: 100, r: 10 }, 200, 50, 400, 400).left).toBe(200);
  });
  it("holds a level a little past its edge, so it does not flicker there", () => {
    expect(levelFor(all, 0.79)).toBe("top");
    expect(levelFor(all, 0.79, "domain")).toBe("domain");
    expect(levelFor(all, 0.6, "domain")).toBe("top");
  });
  it("the scene's levels: a goal's blocks; domains over the groups that merge, the others standing as themselves; no level with nothing of its own", () => {
    const S = buildScene(load("synthetic-split.fixture.json"));
    const two: Scene = { ...S, l1: [{ ...S.l1[0]!, id: "b1", name: "块一", parent: "G", all: ["a"] }, { ...S.l1[0]!, id: "b2", name: "块二", parent: "G", all: ["b"] }, { ...S.l1[0]!, id: "c", name: "簇", parent: null, all: ["c"] }],
      l2: [{ id: "G", name: "目标", summary: "", x: 0, y: 0, at: null, kids: ["b1", "b2"], goal: true }], domains: [] };
    expect(zoomTitles(two).levels).toEqual(["top", "block", "point"]);
    expect(zoomTitles(two).titles.filter((t) => t.level === "top").map((t) => t.name)).toEqual(["目标", "簇"]);
    const d = zoomTitles({ ...two, domains: [{ id: "d", name: "领域", summary: "", kids: ["G"] }] });
    expect(d.levels).toEqual(["domain", "top", "block", "point"]);
    expect(d.titles.filter((t) => t.level === "domain").map((t) => [t.name, t.points])).toEqual([["领域", ["a", "b"]], ["簇", ["c"]]]);
    const flat: Scene = { ...two, l1: [{ ...two.l1[2]! }], l2: [] };
    expect(zoomTitles(flat).levels).toEqual(["top", "point"]);
  });
});

describe("GraphCloud: names on hover, titles over the canvas", () => {
  const made: GraphCloudHandle[] = [];
  afterEach(() => { for (const h of made.splice(0)) h.destroy(); });      // its colour wait must not outlive the test
  const make = (o: { onGroup?: (id: string) => boolean; onHover?: () => void } = {}) => {
    const box = document.createElement("div");
    Object.defineProperties(box, { clientWidth: { value: 100_000 }, clientHeight: { value: 100_000 } });
    const tokens = document.createElement("div");
    const h = GraphCloud(box, { tokens, font: () => "sans-serif", reduced: () => true, ...o });
    made.push(h);
    const S = buildScene(load("synthetic-75.fixture.json"));
    h.setScene(S);
    const sg = FakeSigma.last;
    // the fake maps graph coordinates 1:1 to the viewport, shifted so every point is on screen
    const G = h.graph(), dx = -Math.min(...G.mapNodes((_, a) => a.x)) + 500, dy = -Math.min(...G.mapNodes((_, a) => a.y)) + 500;
    sg.graphToViewport = (p) => ({ x: p.x + dx, y: p.y + dy });
    return { box, h, S, sg, G, shown: () => [...box.querySelectorAll<HTMLElement>(".ew-gtitle")].filter((e) => e.style.display === "block") };
  };

  it("no names by default; hovering a point names it alone, keeps its neighbours and fades the rest", () => {
    const { sg, G } = make();
    expect(G.nodes().every((id) => sg.node(id).label === "")).toBe(true);
    const hub = G.nodes().sort((a, b) => G.degree(b) - G.degree(a))[0]!;
    sg.emit("enterNode", { node: hub });
    expect(G.nodes().filter((id) => sg.node(id).label !== "")).toEqual([hub]);
    for (const id of G.nodes()) {
      const faded = sg.node(id).color !== G.getNodeAttribute(id, "color");
      expect(faded).toBe(id !== hub && !G.areNeighbors(id, hub));
    }
    sg.emit("leaveNode");
    expect(G.nodes().every((id) => sg.node(id).color === G.getNodeAttribute(id, "color"))).toBe(true);
  });

  it("zoomed out the top groups' titles, zoomed in the level-1 ones; no two shown titles overlap", () => {
    const { sg, S, shown } = make();
    const T = cloudTitles(S), names = (near: boolean) => new Set(T.filter((t) => t.near === near).map((t) => t.name));
    sg.emit("afterRender");
    expect(shown().length).toBeGreaterThan(0);
    expect(shown().every((e) => e.classList.contains("ew-gtitle-top") && names(false).has(e.textContent))).toBe(true);
    sg.camera.setState({ ratio: NEAR_RATIO / 2 }); sg.emit("afterRender");
    expect(shown().every((e) => e.classList.contains("ew-gtitle-l1") && names(true).has(e.textContent))).toBe(true);
    const r = shown().map((e) => ({ l: parseFloat(e.style.left), t: parseFloat(e.style.top) }));
    for (let i = 0; i < r.length; i++) for (let j = i + 1; j < r.length; j++) expect(r[i]!.l === r[j]!.l && r[i]!.t === r[j]!.t).toBe(false);
  });

  it("the points float gently around their laid-out places; never with reduced motion", () => {
    const frames: FrameRequestCallback[] = [];
    vi.stubGlobal("requestAnimationFrame", (f: FrameRequestCallback) => { frames.push(f); return frames.length; });
    vi.stubGlobal("cancelAnimationFrame", () => undefined);
    let reduced = false;
    const box = document.createElement("div");
    document.body.append(box);
    Object.defineProperty(box, "offsetWidth", { value: 800 });
    const h = GraphCloud(box, { tokens: document.createElement("div"), font: () => "sans-serif", reduced: () => reduced, small: true });
    h.setScene(buildScene(load("synthetic-75.fixture.json")));
    const G = h.graph(), home = new Map(G.mapNodes((id, a) => [id, { x: a.x, y: a.y }]));
    const span = Math.max(...G.mapNodes((_, a) => a.x)) - Math.min(...G.mapNodes((_, a) => a.x));
    frames[frames.length - 1]!(5000);
    expect(G.nodes().filter((id) => G.getNodeAttribute(id, "x") !== home.get(id)!.x)).toHaveLength(G.order);
    for (const id of G.nodes()) expect(Math.abs(G.getNodeAttribute(id, "x") - home.get(id)!.x)).toBeLessThan(span * 0.05);
    reduced = true;
    const at = G.mapNodes((_, a) => a.x);
    frames[frames.length - 1]!(9000);
    expect(G.mapNodes((_, a) => a.x)).toEqual(at);
    h.destroy(); box.remove(); vi.unstubAllGlobals();
  });

  it("a click on a title goes to onGroup; unhandled, the camera frames its points", () => {
    const onGroup = vi.fn(() => false);
    const { sg, shown } = make({ onGroup });
    sg.emit("afterRender");
    const t = shown()[0]!;
    sg.camera.setState({ x: 9, y: 9 });
    t.click();
    expect(onGroup).toHaveBeenCalledTimes(1);
    expect(sg.camera.x).not.toBe(9);
  });
});

describe("GraphCloud: 学点新的's grow, fade, pulse and pinned names (graphology attributes, Sigma's own layers)", () => {
  const blocks = [{ key: "b1", name: "水质与环境", scope: "" }, { key: "b2", name: "开缸与日常", scope: "" }, { key: "b3", name: "营养与摄食", scope: "" }];
  const setup = () => {
    const frames: FrameRequestCallback[] = [];
    vi.stubGlobal("requestAnimationFrame", (f: FrameRequestCallback) => { frames.push(f); return frames.length; });
    vi.stubGlobal("cancelAnimationFrame", () => undefined);
    let now = 1000;
    vi.spyOn(performance, "now").mockImplementation(() => now);
    const box = document.createElement("div");
    document.body.append(box);
    Object.defineProperties(box, { offsetWidth: { value: 800 }, clientWidth: { value: 800 }, clientHeight: { value: 600 } });
    const goal = new Set(blocks.map((b) => b.key));
    const tokens = document.createElement("div");
    for (const [k, v] of Object.entries(C)) tokens.style.setProperty(k === "bg" ? "--cloud-bg" : `--${k}`, v);
    document.body.append(tokens);
    const h = GraphCloud(box, { tokens, font: () => "sans-serif", reduced: () => false,
      grows: (id) => goal.has(id), labels: "all", pinned: (id) => goal.has(id) });
    const K = buildScene(load("synthetic-75.fixture.json"));
    h.setScene(K);
    const frame = (t: number): void => { now = t; frames[frames.length - 1]!(t + 100_000); };
    const done = (): void => { box.remove(); h.destroy(); vi.unstubAllGlobals(); vi.restoreAllMocks(); };
    return { h, K, box, frame, done, at: (t: number) => { now = t; } };
  };

  it("the goal's new blocks grow in one after another; the existing map is there at once", () => {
    const { h, K, frame, done } = setup();
    h.setScene(learnScene(K, draftMap(blocks)).scene);
    const G = h.graph(), sg = FakeSigma.last;
    expect(K.order.every((id) => G.getNodeAttribute(id, "grow") === undefined)).toBe(true);
    expect(blocks.map((b) => G.getNodeAttribute(b.key, "grow"))).toEqual([0, 0, 0]);
    expect([sg.node("b1").hidden, sg.node("b1").size < 0.1]).toEqual([undefined, true]);     // not yet grown: no size, never hidden
    frame(1000 + 500);
    const g = blocks.map((b) => G.getNodeAttribute(b.key, "grow") ?? 1);
    expect(g[0]).toBeGreaterThan(g[1]!); expect(g[1]).toBeGreaterThan(g[2]!);
    expect(sg.node("b1").size).toBeLessThan(PIN_SIZE);
    frame(1000 + 5000);
    expect(blocks.every((b) => G.getNodeAttribute(b.key, "grow") === undefined)).toBe(true);
    expect(sg.node("b1").size).toBeGreaterThan(sg.node(blocks[1]!.key).size === undefined ? 0 : 0);     // grown: drawn
    done();
  });

  it("a block whose band changed fades from the colour it showed to its band's", () => {
    const { h, K, frame, at, done } = setup();
    const map = draftMap(blocks);
    h.setScene(learnScene(K, map).scene);
    frame(9000);
    const grey = h.graph().getNodeAttribute("b1", "color");
    at(10_000);
    const known = { ...map, nodes: map.nodes.map((n) => (n.id === "b1" ? { ...n, mix: { known: 1 } } : n)) };
    h.setScene(learnScene(K, known).scene);
    const G = h.graph();
    expect(G.getNodeAttribute("b1", "color")).toBe(grey);
    frame(10_000 + 300);
    const mid = G.getNodeAttribute("b1", "color");
    expect(mid).not.toBe(grey);
    frame(10_000 + 2000);
    expect(G.getNodeAttribute("b1", "color")).not.toBe(mid);
    expect(G.getNodeAttribute("b1", "band")).toBe("known");
    done();
  });

  it("the block being asked is highlighted (its ring on Sigma's hover layer); pinned blocks name themselves clear of each other", () => {
    const { h, K, box, frame, done } = setup();
    h.setScene(learnScene(K, draftMap(blocks)).scene);
    frame(9000);
    h.setState({ sel: null, focus: null, lit: new Set(), pulse: new Set(["b2"]) });
    const sg = FakeSigma.last;
    expect(sg.node("b2").highlighted).toBe(true);
    expect(sg.node("b1").highlighted).not.toBe(true);
    expect(sg.node("b1").label).toBe("");          // a pinned block's name is a title, not a canvas label
    expect(K.order.slice(0, 5).every((id) => sg.node(id).label !== "")).toBe(true);
    const x0 = h.graph().getNodeAttribute("b1", "x");
    sg.graphToViewport = (p: { x: number; y: number }) => ({ x: 80 + (p.x - x0) * 200, y: 300 - p.y * 200 });
    sg.emit("afterRender");
    const pins = [...box.querySelectorAll<HTMLElement>(".ew-gpin")].filter((e) => e.style.display === "block");
    expect(pins.map((e) => e.textContent)).toEqual(blocks.map((b) => b.name));
    const r = pins.map((e) => ({ x: parseFloat(e.style.left), y: parseFloat(e.style.top) }));
    for (let i = 0; i < r.length; i++) for (let j = i + 1; j < r.length; j++) expect(r[i]!.x === r[j]!.x && r[i]!.y === r[j]!.y).toBe(false);
    done();
  });
});

describe("the fixed map", () => {
  const scene = (): Scene => buildScene(load("synthetic-split.fixture.json"));
  it("a point stands at its world position: the server's, in any box", () => {
    const S = scene(), G = sceneGraph(S, C);
    for (const id of S.order) expect([G.getNodeAttribute(id, "x"), G.getNodeAttribute(id, "y")]).toEqual([S.nodes.get(id)!.x, S.nodes.get(id)!.y]);
  });
  it("a line across the whole picture is not drawn until hovered; one within a group is", () => {
    const S = scene(), G = sceneGraph(S, C);
    const far = G.filterEdges((_, a) => a.far === true), near = G.filterEdges((_, a) => a.far !== true);
    expect(far.length + near.length).toBe(G.size);
    for (const e of near) expect(G.getNodeAttribute(G.source(e), "top")).toBe(G.getNodeAttribute(G.target(e), "top"));
  });
  it("a group's glow leaves out a colour not read yet; with none read, nothing to paint", () => {
    expect(rgb(glowTint(["#ff0000", ""]), 1)).toBe(rgb([255, 0, 0], 1));
  });
});
