/**
 * Star map data (pure, no DOM): nodes = concepts, edges = prerequisites, regions = goals / other.
 * Everything here is derived from K (`GET /wiki/k`) and the goal membership the controller gathered.
 */
import { tr } from "../../i18n";
import type { Aspect, Band, CellBeliefOut, KGridOut } from "../schema";

export const OTHER_REGION_ID = "other";
export const otherRegionName = (): string => tr("wiki.usage.GROUP_LABEL.other.6");

export interface GoalMembership { goals: { id: string; name: string }[]; membership: Record<string, string[]> }

export interface StarAspect { aspect: Aspect; band: Band }
export interface StarNode {
  id: string; name: string; band: Band; aspectsKnown: number; aspectsTotal: number;
  /** Prerequisites that exist in the graph (missing ones are dropped). */
  prereqs: string[]; goalIds: string[]; regionId: string; onPage: boolean; pageId: string | null;
  aspects: StarAspect[];
}
export interface StarRegion { id: string; name: string; nodeIds: string[]; averageMastery: number }
/** from = the prerequisite, to = the concept that needs it. */
export interface StarEdge { from: string; to: string }
export interface StarMap { nodes: StarNode[]; byId: Map<string, StarNode>; regions: StarRegion[]; edges: StarEdge[] }

export const isGood = (b: Band): boolean => b === "known" || b === "likely";
export const byCodePoint = (a: string, b: string): number => (a < b ? -1 : a > b ? 1 : 0);

/**
 * R3.1 (plugin-side display rule, not a backend judgement): one band for a whole concept from its own aspects' bands.
 * good = known / likely. None good: any shaky → shaky, else any gap → gap, else none. All good: all known → known, else
 * likely. Some good: at least half → likely, else shaky.
 */
export function conceptBand(cells: readonly CellBeliefOut[]): Band {
  const n = cells.length;
  const good = cells.filter((c) => isGood(c.band)).length;
  if (good === 0) {
    if (cells.some((c) => c.band === "shaky")) return "shaky";
    if (cells.some((c) => c.band === "gap")) return "gap";
    return "none";
  }
  if (good === n) return cells.every((c) => c.band === "known") ? "known" : "likely";
  return good / n >= 0.5 ? "likely" : "shaky";
}

export function buildStarMap(k: KGridOut, goals: GoalMembership): StarMap {
  const ids = new Set(k.concepts.map((c) => c.id));
  const goalSets = goals.goals.map((g) => ({ g, set: new Set(goals.membership[g.id] ?? []) }));

  const regionNames = new Map<string, string>();
  const nodes: StarNode[] = k.concepts.map((c) => {
    const own = new Set<Aspect>(c.aspects);
    const seen = new Set<Aspect>();
    const cells: CellBeliefOut[] = [];
    for (const cell of c.cells) {
      if (cell.concept_id !== c.id || !own.has(cell.aspect) || seen.has(cell.aspect)) continue;
      seen.add(cell.aspect);
      cells.push(cell);
    }
    const prereqs = [...new Set(c.prereqs)].filter((p) => p !== c.id && ids.has(p));
    const goalIds = goalSets.filter((x) => x.set.has(c.id)).map((x) => x.g.id);
    let regionId: string;
    if (goalIds.length > 0) {
      regionId = `goal:${goalIds[0]!}`;
      regionNames.set(regionId, goalSets.find((x) => x.g.id === goalIds[0])!.g.name);
    } else {
      regionId = OTHER_REGION_ID;
      regionNames.set(regionId, otherRegionName());
    }
    return {
      id: c.id, name: c.name, band: conceptBand(cells),
      aspectsKnown: cells.filter((x) => isGood(x.band)).length, aspectsTotal: c.aspects.length,
      prereqs, goalIds, regionId, onPage: c.on_current_page, pageId: null,
      aspects: cells.map((x) => ({ aspect: x.aspect, band: x.band })),
    };
  });

  const byId = new Map(nodes.map((n) => [n.id, n]));
  const edges: StarEdge[] = [];
  for (const n of nodes) for (const p of n.prereqs) edges.push({ from: p, to: n.id });

  const members = new Map<string, StarNode[]>();
  for (const n of nodes) {
    const list = members.get(n.regionId);
    if (list === undefined) members.set(n.regionId, [n]); else list.push(n);
  }
  const regions: StarRegion[] = [...members.keys()].sort(byCodePoint).map((id) => {
    const list = members.get(id)!;
    const avg = list.reduce((s, n) => s + (n.aspectsTotal > 0 ? n.aspectsKnown / n.aspectsTotal : 0), 0) / list.length;
    return { id, name: regionNames.get(id)!, nodeIds: list.map((n) => n.id), averageMastery: avg };
  });
  return { nodes, byId, regions, edges };
}
