import { describe, expect, it } from "vitest";
import { ASPECTS, type Band } from "../schema";
import { cell, concept, protoGoals, protoK } from "./fixture";
import { buildStarMap, conceptBand, OTHER_REGION_ID } from "./model";

const cells = (...b: Band[]) => b.map((x, i) => cell("c", ASPECTS[i]!, x));
const none = { goals: [], membership: {} };

describe("conceptBand (R3.1)", () => {
  it("no good cells: shaky beats gap beats none", () => {
    expect(conceptBand(cells("gap", "shaky", "none"))).toBe("shaky");
    expect(conceptBand(cells("gap", "none"))).toBe("gap");
    expect(conceptBand(cells("none", "none", "none", "none", "none", "none"))).toBe("none");
    expect(conceptBand(cells("gap"))).toBe("gap");
    expect(conceptBand([])).toBe("none");
  });
  it("all good: known only when every aspect is known", () => {
    expect(conceptBand(cells("known"))).toBe("known");
    expect(conceptBand(cells("known", "known", "known", "known", "known", "known"))).toBe("known");
    expect(conceptBand(cells("known", "likely"))).toBe("likely");
    expect(conceptBand(cells("likely"))).toBe("likely");
  });
  it("partly good: at least half → likely, below → shaky", () => {
    expect(conceptBand(cells("known", "gap"))).toBe("likely");
    expect(conceptBand(cells("known", "known", "known", "none", "none", "none"))).toBe("likely");
    expect(conceptBand(cells("known", "none", "none", "none", "none", "none"))).toBe("shaky");
    expect(conceptBand(cells("likely", "gap", "gap"))).toBe("shaky");
    expect(conceptBand(cells("likely", "known", "gap"))).toBe("likely");
  });
});

describe("buildStarMap", () => {
  const map = buildStarMap(protoK(), protoGoals());
  it("assigns regions: the first goal, then other", () => {
    expect(map.byId.get("mean")!.regionId).toBe("goal:g-pca");
    expect(map.byId.get("dna")!.regionId).toBe("goal:g-gene");
    expect(map.regions.map((r) => r.name).sort()).toEqual(["PCA", "基因配对"].sort());
    const lone = buildStarMap({ concepts: [concept("z", "孤", ["none"])] }, none);
    expect(lone.byId.get("z")!.regionId).toBe(OTHER_REGION_ID);
    expect(lone.regions[0]!.name).toBe("其他");
  });
  it("a concept's goals", () => {
    expect(map.byId.get("pca")!.goalIds).toEqual(["g-pca"]);
    expect(map.byId.get("pca")!.regionId).toBe("goal:g-pca");
  });
  it("counts aspects and averages mastery per region", () => {
    const n = map.byId.get("var")!;
    expect([n.aspectsKnown, n.aspectsTotal, n.band]).toEqual([4, 6, "likely"]);
    const stats = map.regions.find((r) => r.id === "goal:g-pca")!;
    const avg = stats.nodeIds.reduce((s, id) => s + map.byId.get(id)!.aspectsKnown / map.byId.get(id)!.aspectsTotal, 0) / stats.nodeIds.length;
    expect(stats.averageMastery).toBeCloseTo(avg, 10);
    expect(stats.nodeIds.length).toBeGreaterThan(5);
  });
  it("aspectsTotal follows the concept's own aspect count", () => {
    const m = buildStarMap({ concepts: [concept("a", "A", ["known", "likely", "none"])] }, none);
    expect([m.nodes[0]!.aspectsKnown, m.nodes[0]!.aspectsTotal]).toEqual([2, 3]);
  });
  it("no page ids (the catalog no longer maps concepts to pages); onPage from K", () => {
    expect(map.byId.get("pca")!.pageId).toBeNull();
    expect(map.byId.get("mean")!.pageId).toBeNull();
    expect(map.byId.get("pca")!.onPage).toBe(true);
  });
  it("edges point prerequisite → follower and skip missing prerequisites", () => {
    const m = buildStarMap(
      { concepts: [concept("a", "A", ["known"]), concept("b", "B", ["none"], ["a", "ghost", "b", "a"])] }, none);
    expect(m.edges).toEqual([{ from: "a", to: "b" }]);
    expect(m.byId.get("b")!.prereqs).toEqual(["a"]);
    expect(m.nodes).toHaveLength(2);
  });
  it("a cycle in the input does not crash", () => {
    const m = buildStarMap(
      { concepts: [concept("a", "A", ["none"], ["b"]), concept("b", "B", ["none"], ["a"])] }, none);
    expect(m.edges).toHaveLength(2);
  });
});
