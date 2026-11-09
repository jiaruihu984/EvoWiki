import { describe, expect, it } from "vitest";
import type { GraphOut } from "../schema";
import { GraphFixture } from "./fixture";
import { GraphShapeError, parseGraph, parsePathEdit, parseSplit } from "./parse";

const good = (): GraphOut => new GraphFixture().graph();
type Loose = Record<string, unknown>;
/** The graph as plain records, so a test can break any field. */
interface Raw { nodes: Loose[]; relations: Loose[]; groups: (Loose & { members: string[] })[]; goals: (Loose & { shares: Loose; path: Loose[] })[] }
const broken = (f: (g: Raw) => void): unknown => { const g = structuredClone(good()) as unknown as Raw; f(g); return g; };

describe("parseGraph (fail closed on anything but the wire contract)", () => {
  it("accepts the contract and returns the same value", () => {
    const g = good();
    expect(parseGraph(g)).toBe(g);
  });
  it("accepts a recommended relation and a verified prerequisite (relation layers 2026-10-14)", () => {
    const g = new GraphFixture().graph();
    const [a, b] = g.nodes;
    g.relations.push({ kind: "recommended", src: a!.id, dst: b!.id, phrase: "先看它更好懂", verified: false, split: false },
      { kind: "prerequisite", src: b!.id, dst: a!.id, phrase: "", verified: true, split: false });
    expect(parseGraph(g)).toBe(g);
  });
  it.each<[string, (g: Raw) => void]>([
    ["not an object", (g) => { (g as unknown as Loose).nodes = null; }],
    ["a node without x", (g) => { delete g.nodes[0]!.x; }],
    ["a non-finite y", (g) => { g.nodes[0]!.y = Number.NaN; }],
    ["an unknown band", (g) => { g.nodes[0]!.band = "great"; }],
    ["an unknown band in a mix", (g) => { g.nodes[0]!.mix = { great: 1 }; }],
    ["a negative share", (g) => { g.nodes[0]!.direct = { known: -1 }; }],
    ["a duplicate node id", (g) => { g.nodes[1]!.id = g.nodes[0]!.id; }],
    ["a parent that is no node", (g) => { g.nodes[0]!.parents = ["ghost"]; }],
    ["an unknown relation kind", (g) => { g.relations[0]!.kind = "causes"; }],
    ["a relation to no node", (g) => { g.relations[0]!.dst = "ghost"; }],
    ["a relation without verified", (g) => { delete g.relations[0]!.verified; }],
    ["verified not a boolean", (g) => { g.relations[0]!.verified = "yes"; }],
    ["a group level 3", (g) => { g.groups[0]!.level = 3; }],
    ["a group member that is no node", (g) => { g.groups[0]!.members.push("ghost"); }],
    ["a group parent that is no group", (g) => { g.groups[g.groups.length - 1]!.parent = "Z"; }],
    ["a goal share missing", (g) => { delete g.goals[0]!.shares.none; }],
    ["a goal without its topic name", (g) => { delete g.goals[0]!.name; }],
    ["a path row without its question", (g) => { delete g.goals[0]!.path[0]!.question; }],
    ["a path status unknown", (g) => { g.goals[0]!.path[0]!.status = "maybe"; }],
    ["a path node that is no node", (g) => { g.goals[0]!.path[0]!.node_ids = ["ghost"]; }],
    ["can_split not a boolean", (g) => { g.nodes[0]!.can_split = "yes"; }],
  ])("refuses %s", (_, f) => {
    expect(() => parseGraph(broken(f))).toThrow(GraphShapeError);
  });
});

describe("parseSplit / parsePathEdit", () => {
  it("split: the children must be nodes of the graph that comes with them", () => {
    const fx = new GraphFixture();
    const out = fx.split("mul");
    expect(parseSplit(out)).toBe(out);
    expect(() => parseSplit({ ...out, children: ["ghost"] })).toThrow(GraphShapeError);
  });
  it("path edit: a path of items", () => {
    expect(parsePathEdit({ path: good().goals[0]!.path }).path).toHaveLength(9);
    expect(() => parsePathEdit({ path: [{ key: "k" }] })).toThrow(GraphShapeError);
    expect(() => parsePathEdit({})).toThrow(GraphShapeError);
    const row = { ...good().goals[0]!.path[0]!, trashed: "yes" };
    expect(() => parsePathEdit({ path: [row] })).toThrow(GraphShapeError);
  });
});
