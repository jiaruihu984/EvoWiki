/**
 * What the 知识图谱 canvas draws, derived once per graph (pure; no time, no size):
 * - drawn nodes: every member of a level-1 group, every node without part_of children (a leaf), and every child of a
 *   drawn node. A node that only contains others (线性代数 when its parts are drawn, 数学) is not a dot: its group
 *   is the picture of it, as in graph.html where group nodes are not members of a view.
 * - a "child" is a drawn node split out of a drawn parent by 看不懂 (its part_of is `split`; graph.html's split
 *   children): it grows out of that parent, is a smaller dot with a dashed link and gets no 我会了 / 我想学 buttons. The
 *   other parts of a drawn node (a topic's, a goal's) are ordinary dots in their own groups (bug 2026-10-22: a topic's
 *   nine parts stood in one column beside it).
 * - draw order = graph.html's layout order: per level-1 group its members, each followed by its children; then the rest.
 * - each node's prerequisites with their phrases (prerequisite relations into it, in relation order).
 * - blocks (far zoom): the level-2 groups; with only one level, every level-1 group is its own block.
 * - a top group's `at`: its place among the top groups (GET /wiki/graph: similar groups near each other), or null.
 * - a goal (level-2 group with `map_id`): its blocks are one picture under its title. A knowledge point keeps one id and one
 *   band and may stand on several goals: it is one dot per block holding it (`copyId`, `realId`), the same colour in each.
 */
import type { Band, GoalOut, GraphOut, GroupOut } from "../schema";

/** A point's further dots, one per other block holding it (no node id has `~`). */
export const copyId = (id: string, group: string): string => `${id}~in~${group}`;
/** The point a dot draws (selection, lit points and hovering speak of points, not dots). */
export const realId = (dot: string): string => { const i = dot.indexOf("~in~"); return i < 0 ? dot : dot.slice(0, i); };

export interface SceneNode {
  /** The dot's key: the point's id, or its `copyId` on a further block. */ id: string; name: string; band: Band; kind: string; evidence: string[]; parents: string[];
  x: number; y: number;
  /** The level-1 group it is drawn in (a child: its parent's), or null. */ g: string | null;
  /** A split child: the drawn parent it grows out of. */ child: boolean; from: string | null;
  pre: { id: string; phrase: string }[];
}
export interface SceneGroup1 {
  id: string; name: string; summary: string; x: number; y: number; parent: string | null; at: [number, number] | null;
  /** Drawn members that are not split children (graph.html's ring members). */ members: string[];
  /** Every drawn node of the group, split children included. */ all: string[];
}
export interface SceneGroup2 { id: string; name: string; summary: string; x: number; y: number; at: [number, number] | null; kids: string[];
  /** A goal, its blocks drawn as one picture under its title (fitRoom). */ goal?: boolean; /** Which goal (GET /wiki/graph). */ mapId?: string }
/** A broad domain over top groups (GET /wiki/graph `domains`). */
export interface SceneDomain { id: string; name: string; summary: string; kids: string[] }
export interface Scene {
  /** Drawn node ids in draw order. */ order: string[];
  nodes: Map<string, SceneNode>;
  /** Every node's name (drawn or not): 属于 and 前置 lines name nodes that may not be dots. */ names: Map<string, string>;
  l1: SceneGroup1[]; l2: SceneGroup2[];
  /** The level above the top groups; absent or empty: none. */ domains?: SceneDomain[];
}

/** Two groups of one name are one region to the reader (a goal's points in two clusters, both named after it): the first takes the second's points. */
function mergeNamesakes(l1: SceneGroup1[], nodes: Map<string, SceneNode>): void {
  const first = new Map<string, SceneGroup1>();
  for (const g of [...l1]) {
    if (g.parent !== null || g.name === "") continue;
    const to = first.get(g.name);
    if (to === undefined) { first.set(g.name, g); continue; }
    to.members.push(...g.members); to.all.push(...g.all);
    for (const id of g.all) nodes.get(id)!.g = to.id;
    l1.splice(l1.indexOf(g), 1);
  }
}
function sameNameOnce(l2: SceneGroup2[], l1: SceneGroup1[]): SceneGroup2[] {
  const first = new Map<string, SceneGroup2>();
  return l2.filter((g) => {
    const to = first.get(g.name);
    if (g.goal === true) return true;
    if (to === undefined || g.name === "") { first.set(g.name, g); return true; }
    to.kids.push(...g.kids);
    for (const k of l1) if (k.parent === g.id) k.parent = to.id;
    return false;
  });
}

/** `oneDot` (知识图谱, every goal in one view): a point shared by goals is one dot, on the first block holding it. */
export function buildScene(graph: GraphOut, oneDot = false): Scene {
  const byId = new Map(graph.nodes.map((n) => [n.id, n]));
  const kids = new Map<string, string[]>();
  const splitOf = new Set<string>();
  for (const r of graph.relations) if (r.kind === "part_of" && byId.has(r.src) && byId.has(r.dst)) {
    kids.set(r.dst, [...(kids.get(r.dst) ?? []), r.src]);
    if (r.split) splitOf.add(`${r.src} ${r.dst}`);
  }
  const l1src = graph.groups.filter((g) => g.level === 1);
  const inL1 = new Set(l1src.flatMap((g) => g.members));
  const drawn = new Set<string>(graph.nodes.filter((n) => inL1.has(n.id) || !kids.has(n.id)).map((n) => n.id));
  for (let grew = true; grew;) {
    grew = false;
    for (const id of [...drawn]) for (const k of kids.get(id) ?? []) if (!drawn.has(k)) { drawn.add(k); grew = true; }
  }
  const pre = new Map<string, { id: string; phrase: string }[]>();
  for (const r of graph.relations) if (r.kind === "prerequisite" && byId.has(r.src)) pre.set(r.dst, [...(pre.get(r.dst) ?? []), { id: r.src, phrase: r.phrase }]);

  // a point on several goals is a dot in a block of each: its first dot has its id, the others `copyId`
  const nodes = new Map<string, SceneNode>();
  const firstIn = new Map<string, string>();
  const keyIn = (id: string, g: string): string => (firstIn.get(id) === g ? id : copyId(id, g));
  const make = (id: string, key: string, g: string | null): SceneNode => {
    const n = byId.get(id)!;
    const parent = n.parents.find((p) => drawn.has(p) && splitOf.has(`${id} ${p}`)) ?? null;
    const from = parent === null ? null : g !== null && inGroup(parent, g) ? keyIn(parent, g) : parent;
    const own = g !== null && key !== id ? l1src.find((x) => x.id === g)?.copies[id] : undefined;
    return { id: key, name: n.name, band: n.band, kind: n.kind, evidence: n.evidence, parents: n.parents, x: own?.[0] ?? n.x, y: own?.[1] ?? n.y,
      g, child: from !== null, from, pre: pre.get(id) ?? [] };
  };
  const members = new Map(l1src.map((g) => [g.id, new Set(g.members)]));
  const inGroup = (id: string, g: string): boolean => members.get(g)?.has(id) === true;
  for (const g of l1src) for (const m of g.members) if (!firstIn.has(m)) firstIn.set(m, g.id);
  for (const g of l1src) for (const m of members.get(g.id)!) if (drawn.has(m) && (!oneDot || firstIn.get(m) === g.id)) nodes.set(keyIn(m, g.id), make(m, keyIn(m, g.id), g.id));
  for (const id of drawn) if (!firstIn.has(id)) nodes.set(id, make(id, id, null));
  // A child stands beside its parent (graph.html: g = the parent's gid), so it is drawn in the parent's level-1 group
  // whatever clustering made of it. A goal's block is not clustering: its members stay in it.
  const blocks = new Set(l1src.filter((g) => g.parent !== null && graph.groups.some((p) => p.id === g.parent && p.map_id != null)).map((g) => g.id));
  for (const n of nodes.values()) if (n.child && !blocks.has(n.g ?? "")) { let p = nodes.get(n.from!); while (p?.child) p = nodes.get(p.from!); n.g = p?.g ?? n.g; }

  const order: string[] = [];
  const placed = new Set<string>();
  const place = (key: string): void => {
    if (placed.has(key) || !nodes.has(key)) return;
    placed.add(key); order.push(key);
    const g = nodes.get(key)!.g;
    for (const k of kids.get(realId(key)) ?? []) { const kk = g !== null && nodes.has(copyId(k, g)) ? copyId(k, g) : k; if (nodes.get(kk)?.from === key) place(kk); }
  };
  const l1: SceneGroup1[] = l1src.flatMap((g): SceneGroup1[] => {
    const keys = g.members.filter((m) => drawn.has(m)).map((m) => keyIn(m, g.id));
    const own = keys.filter((k) => nodes.has(k) && !nodes.get(k)!.child && nodes.get(k)!.g === g.id);
    for (const k of own) place(k);
    const all = [...nodes.values()].filter((n) => n.g === g.id).map((n) => n.id);
    // a group none of whose members is drawn (a container whose parts moved to other groups) has nothing to name
    if (all.length === 0) return [];
    return [{ id: g.id, name: g.name, summary: g.summary, x: g.x, y: g.y, parent: g.parent, at: g.at ?? null, members: own, all }];
  });
  for (const n of graph.nodes) if (nodes.has(n.id) && !nodes.get(n.id)!.child) place(n.id);
  for (const key of nodes.keys()) place(key);

  mergeNamesakes(l1, nodes);
  const l2: SceneGroup2[] = sameNameOnce(graph.groups.filter((g): g is GroupOut & { level: 2 } => g.level === 2).map((g) => ({
    id: g.id, name: g.name, summary: g.summary, x: g.x, y: g.y, at: g.at ?? null, kids: l1.filter((k) => k.parent === g.id).map((k) => k.id),
    ...(g.map_id != null ? { goal: true, mapId: g.map_id } : {}),
  })), l1);
  // One level of groups (a small or loosely connected graph): the far view shows those groups as its blocks, so it is
  // never empty (graph.html always has blocks there).
  if (l2.length === 0) for (const g of l1) l2.push({ id: g.id, name: g.name, summary: g.summary, x: g.x, y: g.y, at: g.at ?? null, kids: [g.id] });
  return { order, nodes, names: new Map(graph.nodes.map((n) => [n.id, n.name])), l1, l2, domains: graph.domains };
}

/** graph.html inFocus: no goal selected → everything; else the dots in the goal's own blocks. */
export function focusTest(scene: Scene, goal: GoalOut | null): (id: string) => boolean {
  if (goal === null) return () => true;
  const blocks = new Set(scene.l2.find((g) => g.mapId === goal.map_id)?.kids ?? []);
  return (id) => blocks.has(scene.nodes.get(id)?.g ?? "");
}
