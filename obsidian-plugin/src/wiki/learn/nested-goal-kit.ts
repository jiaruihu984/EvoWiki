/**
 * Test kit: a goal map split on demand (spec 2026-10-28 §2.2) behind the GoalApi contract, for the 学点新的 / goal-run
 * tests. Entering a node by its `enter`: "split" adds its parts (collapsed) under it and answers map_id + the map;
 * "children" only answers the map; "learn" opens the point — `pH` asks a round first, every other point answers its path
 * at once (hung on that point's node). 继续拆细 (splitNode) gives a point two parts on this map. `calls` records every
 * route, in order. No model, no timers.
 */
import { ApiError } from "../../api/errors";
import type { GoalDraftOut, GoalMapOut, MapNodeOut, PathOut, SplitOut } from "../schema";
import type { GoalAnswer, GoalApi } from "./goal-api";

/** The tree as the model would split it, one level per entering: the parts of each non-atomic node. */
const TREE: Record<string, { name: string; atomic: boolean; parts?: string[] }> = {
  A: { name: "水质", atomic: false, parts: ["A1", "A2"] },
  B: { name: "喂养", atomic: false, parts: ["B1"] },
  A1: { name: "水的化学", atomic: false, parts: ["A1a", "A1b"] },
  A2: { name: "换水", atomic: true },
  A1a: { name: "氮循环", atomic: false, parts: ["A1a1", "A1a2"] },
  A1b: { name: "pH", atomic: true },
  A1a1: { name: "氨", atomic: true },
  A1a2: { name: "亚硝酸盐", atomic: true },
  B1: { name: "饵料", atomic: true },
};

export const NESTED_MAP = "m-nested";
/** The point whose opening asks a round first (the draft's id). */
export const DIAGNOSED = "A1b";

const emptyOut = (o: Partial<GoalDraftOut>): GoalDraftOut => ({ draft_id: null, page_id: null, path_id: null, map_id: null, title: "黑壳虾",
  concepts: [], questions: [], notes: { from_background: 0, reused_concepts: 0, dropped_questions: 0 }, round: 0, round_title: "", round_why: "",
  results: [], map: null, decisions: [], ...o });

export class NestedGoalApi implements GoalApi {
  readonly live = false;
  readonly calls: string[] = [];
  private nodes: MapNodeOut[];
  private readonly atomic = new Map<string, boolean>(Object.entries(TREE).map(([id, t]) => [id, t.atomic]));
  private readonly names = new Map<string, string>(Object.entries(TREE).map(([id, t]) => [id, t.name]));
  private readonly paths = new Set<string>();

  constructor() { this.nodes = ["A", "B"].map((id) => this.node(id, null)); }

  private node(id: string, parent: string | null): MapNodeOut {
    const atomic = this.atomic.get(id) ?? null;
    return { id, kind: atomic === false ? "region" : "concept", name: this.names.get(id) ?? id, parent, scope: "", level: "", prereqs: [], target: false,
      state: "collapsed", path_id: null, page_id: null, reused: false, members: [], atomic, enter: "split", mix: {}, mastery: null, counts: {} };
  }

  /** The map as GET /wiki/maps answers it: `enter` read from the parts on the map and `atomic`, as the server computes it. */
  map(): GoalMapOut {
    const nodes = this.nodes.map((n): MapNodeOut => {
      const atomic = this.atomic.get(n.id) ?? null;
      const enter = this.nodes.some((c) => c.parent === n.id) ? "children" : atomic === false || (atomic === null && n.parent === null) ? "split" : "learn";
      return { ...n, atomic, enter, kind: atomic === false || enter === "children" ? "region" : n.kind };
    });
    return { map_id: NESTED_MAP, goal: "黑壳虾", title: "黑壳虾", next_region: null, nodes: structuredClone(nodes), reading: null };
  }

  private at(id: string): MapNodeOut {
    const n = this.nodes.find((x) => x.id === id);
    if (!n) throw new ApiError(404, "no such node");
    return n;
  }
  private addParts(id: string, parts: string[]): void {
    for (const p of parts) if (!this.nodes.some((x) => x.id === p)) this.nodes.push(this.node(p, id));
  }
  private planned(id: string): GoalDraftOut {
    const n = this.at(id);
    n.path_id = `path-${id}`; this.paths.add(n.path_id);
    return emptyOut({ path_id: n.path_id, map: this.map() });
  }

  getDraft(draftId: string): Promise<GoalDraftOut> { this.calls.push(`draft:${draftId}`); return Promise.resolve(emptyOut({ map_id: NESTED_MAP, map: this.map() })); }
  getMap(mapId: string): Promise<GoalMapOut> {
    this.calls.push(`map:${mapId}`);
    return mapId === NESTED_MAP ? Promise.resolve(this.map()) : Promise.reject(new ApiError(404, "no such map"));
  }

  enterRegion(mapId: string, id: string): Promise<GoalDraftOut> {
    this.calls.push(`enter:${id}`);
    if (mapId !== NESTED_MAP) return Promise.reject(new ApiError(404, "no such map"));
    const n = this.map().nodes.find((x) => x.id === id);
    if (!n) return Promise.reject(new ApiError(404, "no such node"));
    if (n.enter === "learn") {
      if (n.path_id !== null) return Promise.resolve(emptyOut({ path_id: n.path_id, map: this.map() }));
      if (id === DIAGNOSED) {
        return Promise.resolve(emptyOut({ draft_id: `d-${id}`, round: 1, round_title: "先确认一下", map: this.map(),
          questions: [{ index: 0, kind: "quiz", target: id, cell: `${id}:procedure`, concept_id: id, concept_name: n.name, aspect: "procedure",
            stem: "pH 多少合适？", options: ["7.5", "5"], option_means: null, intent: "mastery" }] }));
      }
      return Promise.resolve(this.planned(id));
    }
    if (n.enter === "split") this.addParts(id, TREE[id]?.parts ?? []);
    this.at(id).state = "expanded";
    return Promise.resolve(emptyOut({ map_id: NESTED_MAP, map: this.map() }));
  }

  answer(draftId: string, _body: GoalAnswer): Promise<GoalDraftOut> {
    this.calls.push(`answer:${draftId}`);
    return Promise.resolve(this.planned(draftId.replace(/^d-/, "")));
  }

  splitNode(nodeId: string, mapId: string): Promise<SplitOut> {
    this.calls.push(`split:${nodeId}:${mapId}`);
    const parts = [`${nodeId}-1`, `${nodeId}-2`];
    parts.forEach((p, i) => { this.names.set(p, `${this.names.get(nodeId) ?? nodeId}（${i + 1}）`); this.atomic.set(p, true); });
    this.atomic.set(nodeId, false);
    this.addParts(nodeId, parts);
    return Promise.resolve({ children: parts, graph: { nodes: [], relations: [], groups: [], goals: [], domains: [] }, kept: "" });
  }

  getPath(pathId: string): Promise<PathOut> {
    this.calls.push(`path:${pathId}`);
    if (!this.paths.has(pathId)) return Promise.reject(new ApiError(404, "no such path"));
    const id = pathId.replace(/^path-/, ""), name = this.names.get(id) ?? id;
    return Promise.resolve({ path_id: pathId, goal: `黑壳虾 · ${name}`, source: "llm", next_index: 1, prefetch: null, sources: [],
      pages: [{ index: 1, aim: "", status: "not_started", state: "planned", progress: null, page_id: null, trashed: false, skipped: false, key: `${pathId}-k1`,
        title: name, abstract: "", sections: [], exam: 0, concepts: [{ concept_id: id, name, target: true }] }] });
  }
  pathNext(pathId: string): ReturnType<GoalApi["pathNext"]> {
    this.calls.push(`next:${pathId}`);
    return Promise.resolve({ path_id: pathId, index: 1, page_id: `page-${pathId}`, prefetched: false });
  }
  editMap(): ReturnType<GoalApi["editMap"]> { return Promise.reject(new ApiError(404, "not here")); }
  editPath(): ReturnType<GoalApi["editPath"]> { return Promise.reject(new ApiError(404, "not here")); }
  splitRow(): ReturnType<GoalApi["splitRow"]> { return Promise.reject(new ApiError(404, "not here")); }
  undoSplitRow(): ReturnType<GoalApi["undoSplitRow"]> { return Promise.reject(new ApiError(404, "not here")); }
}
