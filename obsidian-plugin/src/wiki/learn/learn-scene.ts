/**
 * 学点新的's cloud data (pure): the user's existing map (the 知识图谱's scene, GET /wiki/graph) and the goal map growing
 * into it, as one scene for the 知识图谱 renderer (graph-page/sigma-cloud.ts), so this tab and 知识图谱 draw alike.
 * - the existing map keeps its own groups (his other goals' blocks, the clusters of the points on no goal) and relations;
 * - this goal's blocks are drawn from its map, under its title; what the existing map draws of this goal gives way to them;
 * - a goal region is one point of its own, named as the region; only actual map children grow around it when split;
 *   scope text describes the region and never invents knowledge points;
 * - a point the goal shares with another goal is a dot in each (`copyId`), one band; a point of a cluster moves to the goal;
 * - a new region's prerequisite regions are linked point to point (their first points), so the layout puts them near;
 * - everything stands on the fixed map, the one 知识图谱 draws: his points where the server has them, this goal's dots where the map has this goal's; the goal's blocks where the server would place them
 *   (POST /wiki/graph/places: nothing of his moves), actual children round their region's point; links to their neighbours too;
 * - the goal's own groups stand under one title, the goal's (`goalTitle`), as his other goals stand under theirs.
 */
import { copyId, realId, type Scene, type SceneGroup1, type SceneNode } from "../graph-page/scene";
import type { GoalMapOut, GraphPlacesOut, MapNodeOut } from "../schema";
import { bandOf, conceptsOf, regionsOf } from "./learn-flow";

export interface LearnScene {
  scene: Scene;
  /** Drawn point (its dots too), or a group the goal's region is drawn as → that goal region (a click on it enters the region). */
  goalOf: ReadonlyMap<string, string>;
  /** The dots drawn for this goal (`goalOf` also has a shared point's id, whose first dot can stand on another goal): what the camera frames. */
  goalDots: ReadonlySet<string>;
}

/** The spacing of a block's actual children round its point, in world units (as the server spaces a cloud). */
const BLOCK_SPACING = 0.14, MIN_APART = 0.1;
const NONE: Scene = { order: [], nodes: new Map(), names: new Map(), l1: [], l2: [] };

export function learnScene(k: Scene | null, map: GoalMapOut | null, places: GraphPlacesOut | null = null, goalTitle = ""): LearnScene {
  const base = k ?? NONE;
  // a point split out of one of the goal's points is the goal's: the map does not list it, the graph's parents do
  const own = new Set(map?.nodes.map((n) => n.id) ?? []);
  const ownerOf = (id: string, seen = new Set<string>()): string | null => {
    if (own.has(id)) return id;
    if (seen.has(id)) return null;
    seen.add(id);
    for (const p of base.nodes.get(id)?.parents ?? []) { const o = ownerOf(p, seen); if (o !== null) return o; }
    return null;
  };
  // the goal as the existing map draws it: drawn again here from the goal's map
  const itself = new Set(map === null ? [] : base.l2.filter((g) => g.mapId === map.map_id).flatMap((g) => [g.id, ...g.kids]));
  const moved = new Set(base.l1.filter((g) => itself.has(g.id)).flatMap((g) => g.all));
  const nodes = new Map<string, SceneNode>([...base.nodes].filter(([id]) => !moved.has(id)).map(([id, n]) => [id, { ...n, pre: [...n.pre] }]));
  const l1 = base.l1.filter((g) => !itself.has(g.id)).map((g): SceneGroup1 => ({ ...g, members: [...g.members], all: [...g.all] }));
  const order = base.order.filter((id) => !moved.has(id)), goalOf = new Map<string, string>(), first = new Map<string, string>(), goalDots = new Set<string>();
  const goalId = goalTitle === "" ? null : `~goal~${map?.map_id ?? ""}`, kids: string[] = [];
  const goalGroups = new Set(base.l2.filter((g) => g.goal === true).flatMap((g) => g.kids));
  /** The point in the goal's block `g`, `r` its region: out of a cluster it stood in; a further dot when another goal has it; the dot's key. */
  /** Beside the map until the server says where (a draft's first frame): a row to the right of what he has. */
  const worldX = [...base.nodes.values()].map((n) => n.x), right = worldX.length > 0 ? Math.max(...worldX) + 0.6 : 0;
  const cloudAt = new Map<string, { x: number; y: number; n: number }>();
  // where the map has this goal's dots: a point it shares with another goal has a dot of its own here, not the other goal's
  const ownDot = new Map<string, SceneNode>();
  for (const dot of moved) {
    const n = base.nodes.get(dot);
    if (n === undefined) continue;
    const r = (n.g ?? "").replace(`block-${map?.map_id ?? ""}-`, "");
    ownDot.set(`${realId(dot)}|${r}`, n);
    if (!ownDot.has(realId(dot))) ownDot.set(realId(dot), n);
  }
  // a point the goal takes from elsewhere on the map (a draft, a cluster): round its block once the block has its place, so no line crosses the map
  const onMap = (id: string, r: string): SceneNode | undefined => ownDot.get(`${realId(id)}|${r}`) ?? ownDot.get(realId(id)) ?? (places?.places[r] === undefined ? base.nodes.get(id) : undefined);
  const spot = (id: string, r: string): { x: number; y: number } => {
    const known = onMap(id, r);
    if (known !== undefined && ![...nodes.values()].some((n) => n.x === known.x && n.y === known.y)) return { x: known.x, y: known.y };
    const c = cloudAt.get(r) ?? (() => {
      const stored = places?.places[r] ?? (base.nodes.has(r) ? [base.nodes.get(r)!.x, base.nodes.get(r)!.y] as const : [right + cloudAt.size * 0.7, 0] as const);
      const made = { x: stored[0], y: stored[1], n: 0 }; cloudAt.set(r, made); return made;
    })();
    // round the block's place, clear of every point already standing
    for (;;) {
      const d = c.n === 0 && id === r ? 0 : BLOCK_SPACING * Math.sqrt(c.n), a = c.n * 2.39996, p = { x: c.x + d * Math.cos(a), y: c.y + d * Math.sin(a) };
      c.n += 1;
      if (c.n > 400 || [...nodes.values(), ...base.nodes.values()].every((n) => Math.hypot(n.x - p.x, n.y - p.y) >= MIN_APART)) return p;
    }
  };
  const add = (id: string, name: string, band: SceneNode["band"], g: SceneGroup1, pre: string[], r: string): string => {
    const there = nodes.get(id);
    if (there !== undefined && there.g !== null && !goalGroups.has(there.g) && there.g !== g.id) {
      const from = l1.find((x) => x.id === there.g);
      if (from !== undefined) { from.members = from.members.filter((m) => m !== id); from.all = from.all.filter((m) => m !== id); }
      nodes.delete(id); order.splice(order.indexOf(id), 1);
    }
    const shared = nodes.has(id), key = shared ? copyId(id, g.id) : id;
    const at = shared ? spot(key, r) : spot(id, r);
    nodes.set(key, { id: key, name, band: nodes.get(id)?.band ?? base.nodes.get(id)?.band ?? band, kind: "", evidence: [], parents: [], x: at.x, y: at.y, g: g.id, child: false, from: null,
      pre: pre.map((p) => ({ id: p, phrase: "" })) });
    order.push(key);
    g.members.push(key); g.all.push(key);
    goalOf.set(key, r); goalOf.set(id, r); goalDots.add(key);
    return key;
  };
  if (map !== null) {
    const fresh = new Set<string>();
    // a block inside a block (a part split further) stays in its top block's cloud; only top blocks are clouds of their own
    const regions = regionsOf(map), byId = new Map(regions.map((r) => [r.id, r]));
    const holder = (r: MapNodeOut): MapNodeOut | null => (r.parent !== null ? byId.get(r.parent) : undefined) ?? regions.find((x) => x.members.includes(r.id)) ?? null;
    const topOf = (r: MapNodeOut): MapNodeOut => { let t = r; for (const seen = new Set([r.id]); ;) { const h = holder(t); if (h === null || seen.has(h.id)) return t; seen.add(h.id); t = h; } };
    const cloud = new Map<string, SceneGroup1>();
    for (const r of regions.filter((x) => topOf(x) === x)) {
      const pts = conceptsOf(map, r.id).filter((c) => !byId.has(c.id));
      const g: SceneGroup1 = { id: `block-${map.map_id}-${r.id}`, name: r.name, summary: r.scope, x: 0, y: 0, parent: goalId, at: null, members: [], all: [] };
      l1.push(g); kids.push(g.id); cloud.set(r.id, g); goalOf.set(g.id, r.id);
      // entered: the block's point in the middle of its points, its lines to them a star
      const inside = [...pts, ...regions.filter((x) => topOf(x) === r && x !== r)];
      const mineHere = inside.map((c) => ownDot.get(`${c.id}|${r.id}`) ?? ownDot.get(c.id)).filter((n) => n !== undefined);
      const standing = mineHere.length > 0 ? mineHere : places?.places[r.id] !== undefined ? [] : inside.map((c) => base.nodes.get(c.id)).filter((n) => n !== undefined);
      if (standing.length > 0) cloudAt.set(r.id, { x: standing.reduce((t, n) => t + n.x, 0) / standing.length, y: standing.reduce((t, n) => t + n.y, 0) / standing.length, n: 0 });
      const point = add(r.id, r.name, bandOf(r), g, [], r.id);
      fresh.add(point); first.set(r.id, point);
      for (const c of pts) fresh.add(add(c.id, c.name, bandOf(c), g, [r.id, ...c.prereqs], r.id));
    }
    for (const r of regions.filter((x) => topOf(x) !== x)) {
      const top = topOf(r), g = cloud.get(top.id)!;
      goalOf.set(r.id, top.id);
      if (!g.all.some((d) => realId(d) === r.id)) fresh.add(add(r.id, r.name, bandOf(r), g, r.prereqs, top.id));
      for (const c of conceptsOf(map, r.id).filter((x) => !byId.has(x.id))) {
        if (!g.all.some((d) => realId(d) === c.id)) fresh.add(add(c.id, c.name, bandOf(c), g, [r.id, ...c.prereqs], top.id));
      }
    }
    // the points split out of the goal's points: in the cloud of the block they came from
    for (const dot of moved) {
      const id = realId(dot), owner = ownerOf(id), n = base.nodes.get(dot);
      if (own.has(id) || owner === null || n === undefined) continue;
      const m = map.nodes.find((x) => x.id === owner);
      const at = m === undefined ? null : byId.get(m.id) ?? (m.parent !== null ? byId.get(m.parent) : undefined) ?? regions.find((x) => x.members.includes(m.id)) ?? null;
      if (at === null) continue;
      const top = topOf(at);
      fresh.add(add(id, n.name, n.band, cloud.get(top.id)!, [owner], top.id));
    }
    for (const r of regionsOf(map)) for (const p of r.prereqs) {
      const a = first.get(p), b = first.get(r.id);
      if (a !== undefined && b !== undefined && a !== b && (fresh.has(a) || fresh.has(b))) nodes.get(b)!.pre.push({ id: realId(a), phrase: "" });
    }
  }
  // prerequisites outside the drawing are dropped (an edge needs both ends)
  for (const n of nodes.values()) n.pre = n.pre.filter((p) => nodes.has(p.id));
  // a cluster the goal took all the points of is gone
  const standing = l1.filter((g) => g.all.length > 0 || kids.includes(g.id));
  // where each group goes: the server's places, his groups and the goal's blocks together (similar ones near)
  let l2 = base.l2.filter((g) => !itself.has(g.id)).map((g) => ({ ...g, kids: g.kids.filter((id) => !itself.has(id) && standing.some((x) => x.id === id)) }));
  if (places !== null) {
    l2 = l2.map((g) => ({ ...g, at: places.places[g.id] ?? g.at }));
    for (const g of standing) g.at = places.places[goalOf.get(g.id) ?? g.id] ?? g.at;
    // a block's links to what is nearest it: from its point to the group's first point, or to the other block's
    const entry = (id: string): string | undefined => first.get(id) ?? standing.find((g) => g.id === id)?.all[0] ?? standing.find((g) => g.parent === id)?.all[0];
    for (const [a, b] of places.links) {
      const x = entry(a), y = entry(b);
      if (x !== undefined && y !== undefined && x !== y && !nodes.get(x)!.pre.some((p) => p.id === realId(y)) && !nodes.get(y)!.pre.some((p) => p.id === realId(x))) nodes.get(x)!.pre.push({ id: realId(y), phrase: "" });
    }
  }
  if (goalId !== null && kids.length > 0) {
    // where the goal stands: the middle of its blocks' places
    const at = kids.map((id) => standing.find((g) => g.id === id)?.at).filter((p) => p !== undefined && p !== null);
    l2 = [...l2, { id: goalId, name: goalTitle, summary: "", x: 0, y: 0, kids, goal: true, mapId: map!.map_id,
      at: at.length > 0 ? [at.reduce((t, p) => t + p[0], 0) / at.length, at.reduce((t, p) => t + p[1], 0) / at.length] : null }];
  }
  return {
    scene: { order, nodes, names: new Map([...base.names, ...[...nodes.values()].map((n) => [n.id, n.name] as const)]), l1: standing, l2,
      // the same domains as 知识图谱: the goal drawn here stands in for itself on the map
      domains: (base.domains ?? []).map((d) => ({ ...d, kids: d.kids.map((k) => (goalId !== null && kids.length > 0 && itself.has(k) ? goalId : k)).filter((k) => l2.some((g) => g.id === k) || standing.some((g) => g.id === k && g.parent === null)) })).filter((d) => d.kids.length > 1) },
    goalOf, goalDots,
  };
}
