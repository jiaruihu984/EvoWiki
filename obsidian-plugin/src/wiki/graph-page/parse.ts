/**
 * Runtime checks of the node-graph wire shapes (plan §4). Fail closed: anything that is not exactly the contract
 * (a missing field, a wrong type, an unknown band / kind / status, an id that names no node) throws, so the page shows
 * its error state instead of drawing a half-understood graph. Returns the same value, typed.
 */
import {
  BANDS, PATH_PAGE_STATUSES, RELATION_KINDS, SHARE_BANDS,
  type BandShares, type GoalOut, type GraphNodeOut, type GraphOut, type GroupOut, type PathEditOut, type PathItemOut,
  type RelationOut, type SplitOut,
} from "../schema";

export class GraphShapeError extends Error {
  constructor(where: string) { super(`EvoWiki graph: unexpected shape at ${where}`); this.name = "GraphShapeError"; }
}

type Obj = Record<string, unknown>;
const fail = (where: string): never => { throw new GraphShapeError(where); };
const obj = (x: unknown, where: string): Obj => (typeof x === "object" && x !== null && !Array.isArray(x) ? (x as Obj) : fail(where));
const arr = (x: unknown, where: string): unknown[] => (Array.isArray(x) ? x : fail(where));
const str = (x: unknown, where: string): string => (typeof x === "string" ? x : fail(where));
const num = (x: unknown, where: string): number => (typeof x === "number" && Number.isFinite(x) ? x : fail(where));
const bool = (x: unknown, where: string): boolean => (typeof x === "boolean" ? x : fail(where));
const nullable = <T>(x: unknown, f: (v: unknown, w: string) => T, where: string): T | null => (x === null ? null : f(x, where));
const oneOf = <T extends string>(x: unknown, set: readonly T[], where: string): T =>
  (typeof x === "string" && (set as readonly string[]).includes(x) ? (x as T) : fail(where));
const strs = (x: unknown, where: string): string[] => arr(x, where).map((v, i) => str(v, `${where}[${i}]`));

function shares(x: unknown, where: string): BandShares {
  const o = obj(x, where);
  for (const [k, v] of Object.entries(o)) { oneOf(k, BANDS, `${where}.${k}`); if (num(v, `${where}.${k}`) < 0) fail(`${where}.${k}`); }
  return o;
}

function node(x: unknown, w: string): GraphNodeOut {
  const o = obj(x, w);
  str(o.id, `${w}.id`); str(o.name, `${w}.name`); str(o.definition, `${w}.definition`); oneOf(o.band, BANDS, `${w}.band`);
  shares(o.mix, `${w}.mix`); shares(o.direct, `${w}.direct`); shares(o.aggregate, `${w}.aggregate`);
  strs(o.parents, `${w}.parents`); strs(o.evidence, `${w}.evidence`); str(o.kind, `${w}.kind`);
  num(o.x, `${w}.x`); num(o.y, `${w}.y`); bool(o.can_split, `${w}.can_split`);
  return o as unknown as GraphNodeOut;
}

function relation(x: unknown, w: string): RelationOut {
  const o = obj(x, w);
  oneOf(o.kind, RELATION_KINDS, `${w}.kind`); str(o.src, `${w}.src`); str(o.dst, `${w}.dst`); str(o.phrase, `${w}.phrase`);
  bool(o.verified, `${w}.verified`); bool(o.split, `${w}.split`);
  return o as unknown as RelationOut;
}

function group(x: unknown, w: string): GroupOut {
  const o = obj(x, w);
  str(o.id, `${w}.id`); if (o.level !== 1 && o.level !== 2) fail(`${w}.level`);
  str(o.name, `${w}.name`); str(o.summary, `${w}.summary`); strs(o.members, `${w}.members`);
  nullable(o.parent, str, `${w}.parent`); num(o.x, `${w}.x`); num(o.y, `${w}.y`);
  if (o.at !== null && !(Array.isArray(o.at) && o.at.length === 2 && o.at.every((v) => typeof v === "number" && Number.isFinite(v)))) fail(`${w}.at`);
  return o as unknown as GroupOut;
}

function pathItem(x: unknown, w: string): PathItemOut {
  const o = obj(x, w);
  str(o.key, `${w}.key`); str(o.title, `${w}.title`); str(o.summary, `${w}.summary`); str(o.question, `${w}.question`); strs(o.node_ids, `${w}.node_ids`);
  oneOf(o.status, PATH_PAGE_STATUSES, `${w}.status`); bool(o.skipped, `${w}.skipped`); bool(o.mine, `${w}.mine`);
  nullable(o.page_id, str, `${w}.page_id`);
  if (o.trashed !== undefined) bool(o.trashed, `${w}.trashed`);
  if (o.opened !== undefined) bool(o.opened, `${w}.opened`);
  if (o.sections === undefined) o.sections = [];
  strs(o.sections, `${w}.sections`);
  return o as unknown as PathItemOut;
}

function goal(x: unknown, w: string): GoalOut {
  const o = obj(x, w);
  str(o.map_id, `${w}.map_id`); str(o.goal, `${w}.goal`); str(o.name, `${w}.name`); strs(o.nodes, `${w}.nodes`);
  const s = obj(o.shares, `${w}.shares`);
  for (const b of SHARE_BANDS) if (num(s[b], `${w}.shares.${b}`) < 0) fail(`${w}.shares.${b}`);
  nullable(o.path_id, str, `${w}.path_id`);
  arr(o.path, `${w}.path`).forEach((p, i) => pathItem(p, `${w}.path[${i}]`));
  if (o.paths === undefined) o.paths = [];
  arr(o.paths, `${w}.paths`).forEach((p, i) => {
    const q = obj(p, `${w}.paths[${i}]`);
    str(q.path_id, `${w}.paths[${i}].path_id`); str(q.name, `${w}.paths[${i}].name`);
    arr(q.path, `${w}.paths[${i}].path`).forEach((r, j) => pathItem(r, `${w}.paths[${i}].path[${j}]`));
  });
  if (o.sources === undefined) o.sources = [];
  arr(o.sources, `${w}.sources`).forEach((p, i) => {
    const q = obj(p, `${w}.sources[${i}]`);
    str(q.id, `${w}.sources[${i}].id`); str(q.title, `${w}.sources[${i}].title`); bool(q.deleted, `${w}.sources[${i}].deleted`);
    strs(q.uncovered, `${w}.sources[${i}].uncovered`);
  });
  return o as unknown as GoalOut;
}

/** GET /wiki/graph (and every action that answers with the graph). Ids must name nodes / groups that exist. */
export function parseGraph(x: unknown): GraphOut {
  const o = obj(x, "graph");
  const nodes = arr(o.nodes, "nodes").map((n, i) => node(n, `nodes[${i}]`));
  const relations = arr(o.relations, "relations").map((r, i) => relation(r, `relations[${i}]`));
  const groups = arr(o.groups, "groups").map((g, i) => group(g, `groups[${i}]`));
  const goals = arr(o.goals, "goals").map((g, i) => goal(g, `goals[${i}]`));
  const ids = new Set<string>();
  nodes.forEach((n, i) => { if (n.id === "" || ids.has(n.id)) fail(`nodes[${i}].id`); ids.add(n.id); });
  const known = (id: string, where: string): void => { if (!ids.has(id)) fail(where); };
  nodes.forEach((n, i) => n.parents.forEach((p, j) => known(p, `nodes[${i}].parents[${j}]`)));
  relations.forEach((r, i) => { known(r.src, `relations[${i}].src`); known(r.dst, `relations[${i}].dst`); });
  const gids = new Set(groups.map((g) => g.id));
  groups.forEach((g, i) => {
    g.members.forEach((m, j) => known(m, `groups[${i}].members[${j}]`));
    if (g.parent !== null && !gids.has(g.parent)) fail(`groups[${i}].parent`);
  });
  goals.forEach((g, i) => {
    g.nodes.forEach((m, j) => known(m, `goals[${i}].nodes[${j}]`));
    g.path.forEach((p, j) => p.node_ids.forEach((m, k) => known(m, `goals[${i}].path[${j}].node_ids[${k}]`)));
  });
  return o as unknown as GraphOut;
}

/** POST /wiki/nodes/{id}/split: the children must be nodes of the graph that comes with them. */
export function parseSplit(x: unknown): SplitOut {
  const o = obj(x, "split");
  const children = strs(o.children, "children");
  const graph = parseGraph(o.graph);
  const ids = new Set(graph.nodes.map((n) => n.id));
  children.forEach((c, i) => { if (!ids.has(c)) fail(`children[${i}]`); });
  return o as unknown as SplitOut;
}

/**
 * POST /wiki/nodes/{id}/want (我想学这个): the goal's path as shown now (`path_id` null and
 * `path` [] without a goal or when the goal has no path). The graph itself is read again afterwards.
 */
export interface NodeActionOut { ok: boolean; path_id: string | null; path: PathItemOut[] }

export function parseNodeAction(x: unknown): NodeActionOut {
  const o = obj(x, "node action");
  if (typeof o.ok !== "boolean") fail("ok");
  nullable(o.path_id, str, "path_id");
  arr(o.path, "path").forEach((p, i) => pathItem(p, `path[${i}]`));
  return o as unknown as NodeActionOut;
}

/** POST /wiki/paths/{path_id}/edits. */
export function parsePathEdit(x: unknown): PathEditOut {
  const o = obj(x, "edit");
  arr(o.path, "path").forEach((p, i) => pathItem(p, `path[${i}]`));
  return o as unknown as PathEditOut;
}
