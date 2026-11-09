/**
 * The 知识图谱 cloud (and 今天's thumbnail, and 学点新的) on Sigma.js 3 + graphology: the user's pick of 2026-10-28
 * (陪读助手's mock-ups graphcmp/sigma-fa2.html and sigma.html: 「sigma吧还是，移上去再显示具体的点吧，大体的分类加一个标题」,
 * 「还是要带上熟悉/不熟悉这种颜色的」). The libraries do the work:
 * - the map is fixed: world coordinates from the server (GET /wiki/graph: each node's x, y; `wikiapp.graph_layout`), the same in all three views
 *   whatever the box. A box only sets the camera: the map fitted to it, and how big a dot can be without touching the next. When the data
 *   changes the points glide to where the server now has them;
 * - points coloured by band (掌握 / 熟悉 / 不会 / grey; a point on two goals is a dot in each, both its band), one size; a line across the whole picture only on hover;
 * - no point names by default: hovering a point keeps its neighbours and fades the rest; the views' tip says its name and related points, or, where its
 *   name stands beside it (the finest level, a clicked point), only what is related;
 * - titles: an HTML layer over the canvas placed with graphToViewport after every render, on the page's background so
 *   no line crosses them; zoomed out one per top group (level-2, or a level-1 group without one), zoomed in the
 *   level-1 groups; a title that would cover another is left out. A title is clickable (the views' onGroup);
 * - semantic zoom on 知识图谱 and 学点新的, which differ only in their background (`semantic`; 2026-11-04 「不同缩放程度看的是不同的层级」): each zoom shows one level of the hierarchy, its titles
 *   and its pastel regions: domains zoomed all the way out, then goals and the clusters of what is on no goal, then blocks, then the
 *   points alone with their names, on the plain page. A level with nothing of its own is skipped. A level comes in when it fits, in pixels a world
 *   unit (`levelNeeds`, the scale at which a map shows a layer: a narrow box reaches it closer in, on the same map): most of its titles have room and
 *   its dots can be seen. A title with no room waits for a closer zoom (per label, as DataMapPlot), it never covers another. Adjacent levels crossfade; at rest there is one;
 * - territories (「得看得出来这块的疆域」「模糊的也行」, 2026-10-28; user 2026-11-04 「这个挺好看的呀」): on the 知识图谱 page a faint blurred
 *   wash in one cool colour per group, at the titles' level, so no wash reads as a band; 学点新的 a round glow in each group's
 *   average colour (`washByBand`);
 * - the points float gently (「让他轻轻飘动」, 2026-10-28): each drifts on its own slow ellipse around its laid-out place, a
 *   fraction of the spacing between points, ~30 frames a second; paused while the box is hidden, never with reduced motion;
 * - pan / zoom / pinch / picking: Sigma's camera and captors. zIndex stays off (it dropped part of a 2.7k graph).
 * - 学点新的 (the approved grow demo, cloud-anim.ts): the goal's new points grow in (`grows`), a point whose band changed
 *   fades to its new colour, the block being asked pulses (`CloudState.pulse`): graphology attributes (`grow`, `color`)
 *   interpolated every frame and a ring drawn on Sigma's hover layer, never a canvas of our own. `labels: "all"` (user
 *   ruling, as the 知识图谱: 「移上去再显示具体的点，大体的分类加一个标题」): the whole view shows the points and the titles
 *   only, each group measured and placed by room-layout.ts in the box's own pixels; a point's name shows with the hover on it
 *   or on a point related to it, and zoomed in, each only where it covers nothing (cloud-anim clearLabel). `pinned` points
 *   (the goal's blocks) get a bold name in the title layer, above their cloud. When the box changes size the picture is laid
 *   out for it again and fitted.
 * The full demo's graph.html bundles this very file (full-demo/src/cloud/build.mjs), so demo and plugin draw alike.
 */
import Graph from "graphology";
import Sigma from "sigma";
import type { Settings } from "sigma/settings";
import type { NodeDisplayData, PartialButFor } from "sigma/types";
import { hex, mix, readColors, rgb, type CloudColors, type RGB } from "../colors";
import { type Box, boxesOverlap, clearLabel, fadeAt, GROW_STAGGER, grownAt, pulseRing } from "./cloud-anim";
import { TITLE_GAP } from "./room-layout";
import { realId, type Scene } from "./scene";

/** A lit point (the goal's path, the region being learned) grows by this. */
export const LIT_GROW = 2;
/** A pinned point (学点新的's block) is at least this big (the grow demo's block dot). */
export const PIN_SIZE = 7;
/** Framing a goal or a split stops this close (Sigma camera ratio; 1 = the whole graph): a few points keep their surroundings. */
export const FRAME_MIN = 0.25;
/** Zoomed in past this camera ratio, the level-1 titles replace the top groups' ones. */
export const NEAR_RATIO = 0.45;
/** A point's drift in pixels at the usual scale, as a share of the spacing between points; its period range (s), frames per second. */
export const FLOAT_AMP = 0.03, FLOAT_PERIOD: [number, number] = [7, 13], FLOAT_FPS = 30;
export const ROOM_DRIFT = 1.5, ROOM_PADDING = 14;
/** A goal's title stands this far over its blocks' own titles (学点新的, where both show). */
export const GOAL_LIFT = 24;
/** Two dots side by side take at most this share of the distance between them, so none touch however small the box; the rest is for their drift. */
const DOT_SCALE = 130, DOT_ROOM = 0.9, DOT_MIN = 0.8, DOT_SMALL_MIN = 1.8, GLIDE_MS = 600, DOTS_FADE_MS = 250;
/** How far a territory's wash reaches round each point, in world units. */
const WASH_REACH = 0.32, WASH_ROOM = 1.6;
/** Framing with a level: pixels kept at the sides, over the highest point (its title) and under the lowest (the legend); the closest it goes. */
const FRAME_SIDE = 40, FRAME_TOP = 60, FRAME_BOTTOM = 50, FRAME_LEAST = 0.05, MIN_RATIO = 0.03, FIT_MARGIN = 1.1;
const GROW_TOTAL_MS = 1200, TITLE_INSET = 8, TIP_GAP = 8, FRAME_MS = 500;
const CLICK_SLOP = 4, COLOR_WAIT_MS = 200, COLOR_TRIES = 50, SHOWN_SETTLE_MS = 250;
/** A map with more points than this is not measured pair by pair: `place_missing` keeps at least MAP_MIN_GAP between them. */
const GAP_MEASURED_UP_TO = 1500, MAP_MIN_GAP = 0.1;
export const ROOM_DOT = 5.5, ROOM_LINE = 1.2, GLOW_MID = 0.3, GLOW_OUT = 0.11, GLOW_BREATH = 0.07, GLOW_BACK = 0.25;
/** Territory washes: their colours (cool, none like a band), opacity, and blur as a share of the spacing between points. */
export const WASH = ["#4e79a7", "#76b7b2", "#b07aa1", "#9c755f", "#a0cbe8", "#d4a6c8", "#79706e", "#499894"], WASH_ALPHA = 0.16, WASH_BLUR = 0.45;

export interface NodeAttrs {
  x: number; y: number; size: number; color: string; label: string;
  band: string; group: string | null; top: string;
  /** Growing in (0..1; absent = grown). */ grow?: number;
}
/** pre: a prerequisite; part: part_of (a point and the drawn point it is part of); child: a split child and its parent. */
export interface EdgeAttrs { size: number; color: string; kind: "pre" | "part" | "child"; /** layout only */ weight?: number; /** A line between two clouds: drawn only with the hover on an end. */ far?: boolean }

/** A title over the cloud: a top group's (zoomed out) or a level-1 group's (zoomed in), and the points it names. `lift`: pixels
 *  more over them (a goal's, over its blocks' titles); `small`: at the level-1 size though zoomed out (a block inside a goal). */
export interface Title { id: string; name: string; near: boolean; points: string[]; lift?: number; small?: boolean; /** Semantic zoom: the level it belongs to. */ level?: ZoomLevel }

/** Semantic zoom's levels, coarse to fine. */
export type ZoomLevel = "domain" | "top" | "block" | "point";
/** The whole map (ratio 1) shows the coarsest level; each finer one starts at these camera ratios. A level holds on LEVEL_HOLD past its edge, so the view does not flicker there. */
const BANDS: Record<number, number[]> = { 2: [0.4], 3: [0.7, 0.3], 4: [0.8, 0.45, 0.2] };
export const LEVEL_HOLD = 1.06;
const CROSSFADE_MS = 250, WHEEL_STEP_MS = 180;

/** The level for this ratio among those the scene has; `now` holds on past its edges. `finest`: the finest index that fits at a ratio (a box too small for a level holds the coarser one). */
export function levelFor(levels: readonly ZoomLevel[], ratio: number, now?: ZoomLevel, finest?: (ratio: number) => number): ZoomLevel {
  const edges = BANDS[levels.length] ?? [];
  const at = (r: number): ZoomLevel => levels[Math.min(edges.filter((e) => r < e).length, finest?.(r) ?? Infinity, levels.length - 1)]!;
  const want = at(ratio);
  if (now !== undefined && want !== now && (at(ratio * LEVEL_HOLD) === now || at(ratio / LEVEL_HOLD) === now)) return now;
  return want;
}
/** From this level dots are drawn: the coarser ones are their titles and regions only. */
export const dotsShow = (l: ZoomLevel): boolean => l === "block" || l === "point";

/** Radius in pixels a level's dots must have to be shown; the room round a point its name needs; the share of a level's titles that must have room. */
export const DOT_SEE = 3, NAME_ROOM = 44, TITLES_FIT = 0.95, SPARSE_GAP_PX = 12;
const SCALE_MIN = 4, SCALE_MAX = 6000, SCALE_STEP = 1.2, FIT_DOTS = 800, LEVEL_STEP = 1.6;

export interface FitTitle { w: number; h: number; cx: number; y1: number }
/** How many of these titles have room at `s` pixels a world unit, each over its highest point, clear of the titles before it and of the dots. */
function titlesClear(ts: readonly FitTitle[], dots: readonly { x: number; y: number }[], s: number, r: number): number {
  const taken: Box[] = [];
  const gap = Math.max(TITLE_GAP, r + ROOM_DRIFT + 3);
  for (const t of ts) {
    const b: Box = { x0: t.cx * s - t.w / 2, x1: t.cx * s + t.w / 2, y0: -t.y1 * s - gap - t.h, y1: -t.y1 * s - gap };
    if (taken.some((o) => boxesOverlap(o, b))) continue;
    if (dots.some((d) => { const x = d.x * s, y = -d.y * s; return x + r > b.x0 && x - r < b.x1 && y + r > b.y0 && y - r < b.y1; })) continue;
    taken.push(b);
  }
  return taken.length;
}
/** The scale (pixels a world unit, whatever the box) from which a level with these titles fits: from there on, closer in, TITLES_FIT of them have room. A label's active range (Been et al., dynamic map labeling), kept per level. */
export function fitScale(ts: readonly FitTitle[], dots: readonly { x: number; y: number }[], radius: (s: number) => number): number {
  const need = Math.ceil(ts.length * TITLES_FIT);
  const few = dots.length > FIT_DOTS ? dots.filter((_, i) => i % Math.ceil(dots.length / FIT_DOTS) === 0) : dots;
  let s = SCALE_MAX;
  while (s / SCALE_STEP >= SCALE_MIN && titlesClear(ts, few, s / SCALE_STEP, radius(s / SCALE_STEP)) >= need) s /= SCALE_STEP;
  return s;
}

/** Each level's titles (semantic zoom), and the levels the scene has. */
export function zoomTitles(S: Scene): { titles: Title[]; levels: ZoomLevel[] } {
  const tops = cloudTitles(S).filter((t) => !t.near && t.small !== true).map((t): Title => ({ id: t.id, name: t.name, near: false, points: t.points, level: "top" }));
  const blocks = S.l1.filter((g) => g.name !== "").map((g): Title => ({ id: g.id, name: g.name, near: false, small: true, points: g.all, level: "block" }));
  const inDomain = new Set((S.domains ?? []).flatMap((d) => d.kids));
  const domains: Title[] = (S.domains ?? []).length === 0 ? [] : [
    ...(S.domains ?? []).map((d): Title => ({ id: d.id, name: d.name, near: false, level: "domain", points: tops.filter((t) => d.kids.includes(t.id)).flatMap((t) => t.points) })),
    ...tops.filter((t) => !inDomain.has(t.id)).map((t): Title => ({ ...t, level: "domain" })),
  ];
  // the blocks are a level of their own only where a top group holds more than one
  const finer = blocks.length > tops.length;
  return { titles: [...domains, ...tops, ...(finer ? blocks : [])], levels: [...(domains.length > 0 ? ["domain" as const] : []), "top", ...(finer ? ["block" as const] : []), "point"] };
}

/** The colours plus the cloud's background (dimmed things are mixed towards it). */
export type Colors = CloudColors & { bg: string };
export function readCloudColors(from: Element): Colors {
  return { ...readColors(from), bg: getComputedStyle(from).getPropertyValue("--cloud-bg").trim() || "#fafafa" };
}

const rng = (seed: number) => (): number => { seed = (seed + 0x6d2b79f5) | 0; let t = Math.imul(seed ^ (seed >>> 15), 1 | seed); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
const mixHex = (a: string, b: string, t: number): string => "#" + mix(hex(a), hex(b), t).map((v) => v.toString(16).padStart(2, "0")).join("");
const FREE = "~";

/** The top group a level-1 group stands in: its level-2 group when the scene has it, else itself. */
function topOf(S: Scene): (g: string | null) => string {
  const l2 = new Set(S.l2.map((g) => g.id)), parent = new Map(S.l1.map((g) => [g.id, g.parent]));
  return (g) => { if (g === null) return FREE; const p = parent.get(g) ?? null; return p !== null && l2.has(p) ? p : g; };
}

/** Its titles: one per top group (zoomed out), one per level-1 group (zoomed in); each with the points it names. */
export function cloudTitles(S: Scene): Title[] {
  const top = topOf(S), tops = new Map<string, { name: string; points: string[] }>(), goals = new Set(S.l2.filter((g) => g.goal === true).map((g) => g.id));
  for (const g of S.l2) tops.set(g.id, { name: g.name, points: [] });
  for (const g of S.l1) if (!tops.has(top(g.id))) tops.set(g.id, { name: g.name, points: [] });
  for (const g of S.l1) tops.get(top(g.id))?.points.push(...g.all);
  return [
    ...[...tops].filter(([, t]) => t.points.length > 0).map(([id, t]): Title => ({ id, name: t.name, near: false, points: t.points, ...(goals.has(id) ? { lift: GOAL_LIFT } : {}) })),
    ...S.l1.filter((g) => goals.has(g.parent ?? "") && g.name !== "").map((g): Title => ({ id: g.id, name: g.name, near: false, points: g.all, small: true })),
    ...S.l1.map((g): Title => ({ id: g.id, name: g.name, near: true, points: g.all })),
  ];
}

/** A group's glow: its points' average colour, leaving out a colour not read yet (a token still empty is NaN: nothing to paint). */
export function glowTint(colors: readonly string[]): RGB {
  const read = colors.map(hex).filter((c) => !c.some(Number.isNaN));
  return [0, 1, 2].map((i) => Math.round(read.reduce((t, c) => t + c[i]!, 0) / read.length)) as RGB;
}

/** The scene as a graphology graph at its world positions. */
export function sceneGraph(S: Scene, C: Colors): Graph<NodeAttrs, EdgeAttrs> {
  const G = new Graph<NodeAttrs, EdgeAttrs>({ type: "directed", multi: false });
  const top = topOf(S);
  for (const id of S.order) {
    const n = S.nodes.get(id)!;
    G.addNode(id, { x: n.x, y: n.y, label: n.name, band: n.band, group: n.g, top: top(n.g), color: C[n.band] || C.none, size: ROOM_DOT });
  }
  // a relation joins two points: between the dots of the same goal when both ends have one there
  const dots = new Map<string, string[]>();
  for (const id of S.order) dots.set(realId(id), [...(dots.get(realId(id)) ?? []), id]);
  const dotBy = (point: string, from: string): string | undefined => { const ds = dots.get(point); return ds?.find((d) => G.getNodeAttribute(d, "top") === G.getNodeAttribute(from, "top")) ?? ds?.[0]; };
  for (const n of S.nodes.values()) {
    for (const q of n.pre) { const p = dotBy(q.id, n.id); if (p !== undefined && p !== n.id && realId(p) !== realId(n.id) && !G.hasEdge(p, n.id) && !G.hasEdge(n.id, p)) G.addEdge(p, n.id, { kind: "pre", size: 1, color: C.edge }); }
    if (n.child && n.from !== null && G.hasNode(n.from) && !G.hasEdge(n.from, n.id)) G.addEdge(n.from, n.id, { kind: "child", size: 1, color: C.edge });
  }
  for (const n of S.nodes.values()) for (const q of n.parents) {
    const p = dotBy(q, n.id);
    if (p !== undefined && realId(p) !== realId(n.id) && !G.hasEdge(p, n.id) && !G.hasEdge(n.id, p)) G.addEdge(p, n.id, { kind: "part", size: 1, color: C.edge });
  }
  // a line between two clouds says little and crosses the picture (「穿过全图的长线」): it shows on hover
  const goals = new Set(S.l2.filter((g) => g.goal === true).map((g) => g.id));
  const cloud = (id: string): string => { const a = G.getNodeAttributes(id); return goals.has(a.top) ? a.group ?? id : a.top === FREE ? id : a.top; };
  G.forEachEdge((e, _, a, b) => { G.setEdgeAttribute(e, "far", cloud(a) !== cloud(b)); });
  return G;
}

/** The middle of the points' distances to their nearest neighbour, in world units (what a name has round a point). */
export function nearestMedian(ps: readonly { x: number; y: number }[]): number {
  const few = ps.length > GAP_MEASURED_UP_TO ? ps.filter((_, i) => i % Math.ceil(ps.length / GAP_MEASURED_UP_TO) === 0) : ps;
  const near = few.map((a) => few.reduce((m, b) => (a === b ? m : Math.min(m, Math.hypot(a.x - b.x, a.y - b.y))), Infinity)).filter(Number.isFinite).sort((a, b) => a - b);
  return near[near.length >> 1] ?? MAP_MIN_GAP;
}

/** The nearest two dots' distance in world units (what the dots' size must keep clear of). */
function nearestGap(G: Graph<NodeAttrs, EdgeAttrs>): number {
  if (G.order > GAP_MEASURED_UP_TO) return MAP_MIN_GAP;
  const ps = G.mapNodes((_, a) => [a.x, a.y] as const);
  let gap = Infinity;
  ps.forEach((a, i) => { for (let k = i + 1; k < ps.length; k++) { const b = ps[k]!, d = Math.hypot(a[0] - b[0], a[1] - b[1]); if (d > 0 && d < gap) gap = d; } });
  // two points that close are one pair's trouble: not worth shrinking every dot of the map for
  return gap === Infinity ? 1 : Math.max(gap, MAP_MIN_GAP * 0.8);
}

export interface CloudState {
  sel: string | null;
  /** The focused goal's points (null: no goal). */ focus: ReadonlySet<string> | null;
  /** Its path's points (bigger). */ lit: ReadonlySet<string>;
  /** The points being asked (学点新的's 正在问): a breathing ring. */ pulse?: ReadonlySet<string>;
}
/** A hovered point (`near`: its neighbours' ids) or title (a group id; `near` empty), at viewport x, y. */
export interface Hover { kind: "node" | "group"; id: string; x: number; y: number; near: string[]; /** A point: its radius in pixels, for the tip to keep off it. */ r?: number; /** A point whose name stands beside it: the tip does not repeat it. */ named?: boolean; /** A block: its title and cloud, in the box's pixels (with the names the hover brings up); its tip goes beside them. */ around?: Box }
export interface GraphCloudOpts {
  /** The element the colour tokens are read from (the `.ew` root). */ tokens: Element;
  font(): string;
  reduced(): boolean;
  onSelect?(id: string | null): void;
  onHover?(h: Hover | null): void;
  /** A click on a title; true when handled (学点新的 enters the region), else the camera frames the group. */
  onGroup?(id: string): boolean;
  /** 今天's thumbnail: the same groups fitted into the box, no names, no titles, no gestures (the box takes the click). */ small?: boolean;
  /** Points that grow in when a scene first has them (学点新的: the goal's). Absent: none. */ grows?(id: string): boolean;
  /** "all": every point's name is written (学点新的); default: the hovered and the clicked point's only. */ labels?: "hover" | "all";
  /** Points whose name is a bold title over the canvas, placed clear of the others (学点新的: the goal's blocks). */ pinned?(id: string): boolean;
  /** Whether a click on this point does something (the pointer cursor). Absent: every point. */ canClick?(id: string): boolean;
  /** Each group's territory wash in its points' average colour (学点新的: the grow demo's glow), not a fixed cool hue. */ washByBand?: boolean;
  /** One level of the hierarchy per zoom (知识图谱 and 学点新的; the thumbnail is the whole map, no titles). */ semantic?: boolean;
}
/** Where a hover tip goes in its W × H box: under the point, clear of it, or over it when there is no room under. */
export function tipSpot(h: Pick<Hover, "x" | "y" | "r">, tw: number, th: number, W: number, H: number): { left: number; top: number } {
  const gap = (h.r ?? 0) + TIP_GAP, under = h.y + gap, over = h.y - gap - th;
  return { left: Math.max(0, Math.min(h.x + TIP_GAP, W - tw)), top: under + th > H && over >= 0 ? over : under };
}
/** A name written on the canvas in the last frame, in the box's pixels. */
export interface ShownName { text: string; x0: number; y0: number; x1: number; y1: number }
export interface GraphCloudHandle {
  /** The names on the canvas now (for the harness: the titles are elements). */ shownNames(): ShownName[];
  setScene(S: Scene | null): void;
  setState(s: CloudState): void;
  recolor(): void;
  zoomIn(): void;
  zoomOut(): void;
  /** Frames these points (a goal, a split node and its children); `level`: at a zoom showing that level where it fits; `close`: closer in until it does. */ frame(ids: readonly string[], level?: ZoomLevel, close?: boolean): void;
  /** The whole graph (Sigma's first view). */ fit(): void;
  destroy(): void;
  /** The level shown now (semantic zoom). */ zoomLevel(): ZoomLevel;
  /** The levels the scene has, coarse to fine. */ levels(): ZoomLevel[];
  /** A point's world position (without its float). */ worldOf(id: string): { x: number; y: number } | null;
  /** For tests and the harness. */ readonly sigma: Sigma<NodeAttrs, EdgeAttrs>;
  graph(): Graph<NodeAttrs, EdgeAttrs>;
}

type Display = PartialButFor<NodeDisplayData, "x" | "y" | "size" | "label" | "color"> & { key?: string; grow?: number };

export function GraphCloud(container: HTMLElement, o: GraphCloudOpts): GraphCloudHandle {
  let C = readCloudColors(o.tokens);
  let state: CloudState = { sel: null, focus: null, lit: new Set() };
  let hov: string | null = null;
  let G = new Graph<NodeAttrs, EdgeAttrs>();
  let gapWorld = 1, mapW = 1, mapH = 1, zoom: ZoomLevel = "top", levelScale = 1, hand = false;
  /** Pixels a world unit takes at the whole map: Sigma's own matrix for this box and that camera (the live one lags a resize). */
  const wholeMapScale = (): number => {
    const width = container.clientWidth, height = container.clientHeight, o = { cameraState: { x: 0.5, y: 0.5, ratio: 1, angle: 0 }, viewportDimensions: { width, height } };
    if (width <= 0 || height <= 0) return 1;
    const p0 = sigma.framedGraphToViewport({ x: 0.5, y: 0.5 }, o), p1 = sigma.framedGraphToViewport({ x: 0.6, y: 0.5 }, o);
    return (Math.hypot(p1.x - p0.x, p1.y - p0.y) * 10) / Math.max(mapW, mapH);
  };
  /** A dot's radius in pixels now: its usual one, bigger zoomed in, never so big it would touch its nearest neighbour. */
  // from the numbers, not Sigma's matrices: a reducer can run before they are up to date (a box just shown)
  const dotPx = (): number => dotAt(wholeMapScale() / sigma.getCamera().ratio);
  /** A dot's radius at `s` pixels a world unit, whatever the box: bigger closer in, and clear of its nearest neighbour even for the two biggest dots a view can have (a block's point grown as lit, a lit point, a plain one). */
  function dotAt(s: number): number {
    const biggest = o.small === true ? ROOM_DOT : (o.pinned !== undefined ? PIN_SIZE : ROOM_DOT) + LIT_GROW;
    // the thumbnail shows the whole map in a small box: seen comes before clear of each other (its points may touch)
    return Math.max(o.small === true ? DOT_SMALL_MIN : DOT_MIN, Math.min(ROOM_DOT * Math.max(1, Math.sqrt(s / DOT_SCALE)), DOT_ROOM * gapWorld * s * ROOM_DOT / (2 * biggest)));
  }
  /** From what scale each level of the scene fits (set with the scene: the titles' sizes and the map do not change with the box). */
  let needs = new Map<ZoomLevel, number>(), settleUntil = 0, nearWorld = 0;
  const levelNeeds = (S: Scene): Map<ZoomLevel, number> => {
    const levels = zoomTitles(S).levels, out = new Map<ZoomLevel, number>(), dots = [...home0.values()];
    const seeDots = (DOT_SEE * 2 * ((o.pinned !== undefined ? PIN_SIZE : ROOM_DOT) + LIT_GROW)) / (DOT_ROOM * gapWorld * ROOM_DOT);
    const near = nearestMedian(dots);
    nearWorld = near;
    let floor = 0;
    levels.forEach((l, i) => {
      // the coarsest is always there: it is what the whole map shows
      if (i === 0) { out.set(l, 0); return; }
      const own = titles.filter((t) => t.level === l).map((t) => ({ w: t.w, h: t.h, cx: t.cx, y1: t.y1 }));
      const n = l === "point" ? NAME_ROOM / near : own.length > 0 ? fitScale(own, dots, dotAt) : 0;
      // each level has a stretch of zoom of its own, however soon the next would fit
      floor = Math.max(floor * LEVEL_STEP, seeDots, n);
      out.set(l, floor);
    });
    return out;
  };
  /** The finest level (index) that fits at a camera ratio in this box. */
  const finestFit = (ratio: number): number => {
    const s = wholeMapScale() / ratio, levels = scene === null ? [] : zoomTitles(scene).levels;
    // no box yet: nothing to fit
    if (container.clientWidth <= 0 || container.clientHeight <= 0) return levels.length - 1;
    let at = 0;
    levels.forEach((l, i) => { if (s >= (needs.get(l) ?? 0)) at = i; });
    return at;
  };
  /** A map so sparse its dots stand clear of each other in the whole view shows them from there: the first look is never only colour. */
  const dotsNow = (l: ZoomLevel): boolean => dotsShow(l) || (o.semantic === true && nearWorld * wholeMapScale() >= SPARSE_GAP_PX);
  /** A press of ＋ / － goes one level of the hierarchy (a level can span many camera steps on a small map); at the ends, one camera step. */
  const stepRatio = (ratio: number, k: number): number => {
    if (o.semantic !== true || scene === null) return ratio * k;
    const levels = zoomTitles(scene).levels, level = (r: number) => levelFor(levels, r, undefined, finestFit);
    const from = level(ratio);
    let r = ratio * k;
    for (let i = 0; i < 40 && r >= MIN_RATIO && r <= 3; i++, r *= k > 1 ? 1.12 : 1 / 1.12) if (level(r) !== from) return r * (k > 1 ? 1.08 : 1 / 1.08);
    return ratio * k;
  };
  /** What a dot's nominal size multiplies by, so it is drawn at dotPx whatever Sigma's own scale. */
  const dotK = (): number => (dotPx() / ROOM_DOT) * Math.sqrt(sigma.getCamera().ratio);
  const font = (px: number, w: number): string => `${w} ${px}px ${o.font()}`;
  // labels "all": what this frame's names must not cover (each dot, last frame's titles, the names written so far); each
  // name's width, and the place it found (it stays there while that is free, so a floating name does not jump)
  let frameBoxes: Box[] = [], titleBoxes: Box[] = [], dotBoxes: Box[] = [], frameNames: ShownName[] = [];
  const widths = new Map<string, number>(), spots = new Map<string, number>();
  // the names the hovered point brings up: its neighbours; for a block, the points of its group too
  const related = (a: string, b: string): boolean => {
    if (!G.hasNode(a) || !G.hasNode(b)) return false;
    if (G.areNeighbors(a, b) || realId(a) === realId(b)) return true;
    const g = G.getNodeAttribute(a, "group");
    return o.pinned?.(a) === true && g !== null && g === G.getNodeAttribute(b, "group");
  };
  // a name on the page's background, so no line crosses it
  const drawLabel = (ctx: CanvasRenderingContext2D, d: Display): void => {
    if (!d.label) return;
    const finest = o.semantic === true && zoom === "point";
    if ((o.labels === "all" || finest) && d.key !== hov) {
      // a point's name: zoomed in, or related to the point hovered; quiet, under the dot when that place is free, else
      // beside it; with no free place it is not written
      if (!(finest || sigma.getCamera().ratio < NEAR_RATIO || (hov !== null && related(hov, d.key ?? "")))) return;
      ctx.font = font(11.5, 400);
      let w = widths.get(d.label);
      if (w === undefined) { w = ctx.measureText(d.label).width; widths.set(d.label, w); }
      const key = d.key ?? d.label;
      const box = clearLabel(d.x, d.y, d.size, w + 2, 15, frameBoxes, container.clientWidth, container.clientHeight, spots.get(key) ?? 0);
      if (box === null) return;
      spots.set(key, box.spot);
      frameBoxes.push(box);
      frameNames.push({ text: d.label, x0: box.x0, y0: box.y0, x1: box.x1, y1: box.y1 });
      ctx.fillStyle = C["tx-2"]; ctx.textAlign = "left"; ctx.fillText(d.label, box.x0 + 1, box.y1 - 3.5);
      return;
    }
    ctx.font = font(12, 500);
    const w = ctx.measureText(d.label).width, x = d.x + d.size + 3;
    frameNames.push({ text: d.label, x0: x - 2, y0: d.y - 9, x1: x + w + 2, y1: d.y + 9 });
    ctx.fillStyle = C.bg; ctx.fillRect(x - 2, d.y - 9, w + 4, 18);
    ctx.fillStyle = C["tx-1"]; ctx.textAlign = "left"; ctx.fillText(d.label, x, d.y + 4);
  };
  const namedNow = (id: string): boolean => zoom === "point" || realId(id) === state.sel;
  const drawHover = (ctx: CanvasRenderingContext2D, d: Display): void => {
    if (o.semantic === true && !dotsNow(zoom)) return;
    // the block being asked: an amber ring that breathes (Sigma's hover layer, redrawn every frame while one is asked)
    if (d.key !== undefined && state.pulse?.has(realId(d.key)) === true && (state.focus === null || state.focus.has(d.key))) {
      const ring = pulseRing(performance.now(), o.reduced());
      ctx.save(); ctx.globalAlpha = ring.alpha * (d.grow ?? 1);
      ctx.beginPath(); ctx.arc(d.x, d.y, d.size + ring.r - 7, 0, Math.PI * 2); ctx.strokeStyle = C.shaky; ctx.lineWidth = 2; ctx.stroke();
      ctx.restore();
      if (d.key !== hov) return;
    }
    ctx.beginPath(); ctx.arc(d.x, d.y, d.size + 2.5, 0, Math.PI * 2); ctx.strokeStyle = C["tx-2"]; ctx.lineWidth = 1.5; ctx.stroke();
    // the tip says the name; beside the dot only where the name stands anyway
    if (o.semantic !== true ? o.pinned?.(d.key ?? "") !== true : namedNow(d.key ?? "")) drawLabel(ctx, d);
  };
  const settings: Partial<Settings<NodeAttrs, EdgeAttrs>> = {
    renderLabels: o.small !== true, renderEdgeLabels: false,
    labelRenderedSizeThreshold: 0,
    // every name (学点新的): Sigma's label grid passes them all; drawLabel keeps them apart by their real boxes
    ...(o.labels === "all" || o.semantic === true ? { labelDensity: 1000, labelGridCellSize: 48 } : {}),
    minCameraRatio: MIN_RATIO, maxCameraRatio: 1.5, stagePadding: ROOM_PADDING,
    // a hidden tab or a box not yet on the page has no size; the observer below resizes when it gets one
    allowInvalidContainer: true,
    defaultDrawNodeLabel: drawLabel,
    defaultDrawNodeHover: drawHover,
    // the thumbnail: no gestures
    ...(o.small ? { enableCameraZooming: false, enableCameraPanning: false } : {}),
    nodeReducer: (id, a) => {
      const r: Partial<NodeDisplayData> & Record<string, unknown> = { ...a };
      // names only for the hovered and the clicked point (every name with labels "all"; a pinned one is a title instead)
      r.label = o.semantic === true ? (namedNow(id) ? a.label : "") : o.pinned?.(id) === true ? "" : o.labels === "all" || id === hov || realId(id) === state.sel ? a.label : "";
      const g = a.grow ?? 1, k = dotK();
      r.size = a.size * k;
      if (o.pinned?.(id) === true) r.size = Math.max(a.size, PIN_SIZE) * k;      // a block: the grow demo's 7 px dot
      // never `hidden` while it grows: Sigma's partial refresh of a node turning visible again drops it from its buffers
      if (g < 1) { r.size = Math.max(0.01, r.size * g); if (g < 0.6) r.label = ""; }
      // a point on two goals: the ring and the lit size on the dot of the goal in focus
      const here = state.focus === null || state.focus.has(id);
      if (here && state.pulse?.has(realId(id)) === true) r.highlighted = true;
      if (state.focus !== null && !state.focus.has(id)) r.color = mixHex(a.color, C.bg, 0.82);
      if (here && (state.lit.has(id) || state.lit.has(realId(id)))) r.size = r.size + LIT_GROW * k;
      if (hov !== null && id !== hov && !related(hov, id)) r.color = mixHex(r.color as string, C.bg, 0.85);
      if (realId(id) === state.sel) { r.highlighted = true; r.forceLabel = true; }
      // a coarse level is its titles and regions: the dots fade out (kept drawn: Sigma drops what turns visible again)
      if (o.semantic === true) {
        const f = dotsFade();
        if (f < 1) { const [cr, cg, cb] = hex(r.color as string); r.color = rgb([cr, cg, cb], f); if (f === 0) { r.size = 0.01; r.label = ""; } }
      }
      return r;
    },
    edgeReducer: (e, a) => {
      const s = G.source(e), t = G.target(e);
      const on = (hov !== null && (s === hov || t === hov)) || (state.sel !== null && (realId(s) === state.sel || realId(t) === state.sel))
        || (state.lit.has(realId(s)) && state.lit.has(realId(t)));
      const out = (hov !== null && !on) || (state.focus !== null && (!state.focus.has(s) || !state.focus.has(t)));
      // an edge shows once both its ends have mostly grown
      // a line belongs to the dots' levels: gone with the level, not after its fade (the float's updates never touch the edges)
      const hidden = (o.semantic === true && !dotsNow(zoom)) || (G.getNodeAttribute(s, "grow") ?? 1) < 0.6 || (G.getNodeAttribute(t, "grow") ?? 1) < 0.6 || (a.far === true && !(hov !== null && (s === hov || t === hov)) && !(state.sel !== null && (realId(s) === state.sel || realId(t) === state.sel)));
      return { ...a, hidden, color: on ? C["tx-3"] : out ? mixHex(C.edge, C.bg, 0.7) : o.small ? mixHex(C.edge, C.bg, 0.5) : C.edge, size: (on ? 1.6 : o.small ? 0.6 : ROOM_LINE) * Math.sqrt(Math.min(1, sigma.getCamera().ratio)) };
    },
  };
  const sigma = new Sigma<NodeAttrs, EdgeAttrs>(G, container, settings);
  if (!o.small) sigma.on("beforeRender", () => { frameNames = []; });
  if (o.labels === "all" || o.semantic === true) sigma.on("beforeRender", () => {
    frameBoxes = [...titleBoxes];
    G.forEachNode((id, a) => {
      // each dot as drawn (the camera scales it; a block is at least PIN_SIZE)
      const v = sigma.graphToViewport(a), r = dotPx() * ((o.pinned?.(id) === true ? Math.max(a.size, PIN_SIZE) : a.size) / ROOM_DOT) * (a.grow ?? 1);
      frameBoxes.push({ x0: v.x - r, y0: v.y - r, x1: v.x + r, y1: v.y + r });
    });
    dotBoxes = frameBoxes.slice(titleBoxes.length);
  });

  // the titles: HTML over the canvas, placed after every render
  let titles: (Title & { el: HTMLElement; w: number; h: number; natural: number; line: number; wrapped: boolean; wrapW: number; measured: boolean; cx: number; x0: number; x1: number; y0: number; y1: number })[] = [];
  const layer = o.small ? null : container.appendChild(document.createElement("div"));
  if (layer) layer.className = "ew-gtitles";
  const measure = document.createElement("canvas").getContext("2d");
  /** The wheel zooms and a press and drag pans over a title as over the rest of the picture (Sigma listens on its own canvas); `click` only for a press that stayed put. */
  const gesturesThrough = (el: HTMLElement, click: () => void): void => {
    const canvas = (): HTMLElement | null => container.querySelector(".sigma-mouse");
    let from: { x: number; y: number } | null = null;
    el.addEventListener("wheel", (e) => { e.preventDefault(); canvas()?.dispatchEvent(new WheelEvent("wheel", e)); }, { passive: false });
    el.addEventListener("mousedown", (e) => { from = { x: e.clientX, y: e.clientY }; canvas()?.dispatchEvent(new MouseEvent("mousedown", e)); });
    el.addEventListener("click", (e) => {
      if (from === null || Math.hypot(e.clientX - from.x, e.clientY - from.y) <= CLICK_SLOP) click();
      from = null;
    });
  };
  const makeTitles = (S: Scene | null): void => {
    layer?.replaceChildren(); titles = []; pins = [];
    if (!layer || S === null) return;
    for (const t of o.semantic === true ? zoomTitles(S).titles : cloudTitles(S)) {
      const pts = t.points.filter((id) => G.hasNode(id)).map((id) => G.getNodeAttributes(id));
      if (pts.length === 0 || t.name === "") continue;      // a group with no name (学点新的's block placeholders) has no title
      const el = layer.appendChild(document.createElement("div"));
      el.className = "ew-gtitle " + (t.near || t.small === true ? "ew-gtitle-l1" : "ew-gtitle-top");
      el.textContent = t.name; el.dataset.group = t.id;
      if (t.level !== undefined) { el.dataset.level = t.level; if (!o.reduced()) el.style.transition = `opacity ${CROSSFADE_MS}ms, visibility ${CROSSFADE_MS}ms`; }
      const px = t.near || t.small === true ? 13 : 16;
      if (measure) measure.font = font(px, 700);
      const w = (measure?.measureText(t.name).width ?? t.name.length * px) + 12;
      el.addEventListener("mouseenter", () => { const b = el.getBoundingClientRect(), c = container.getBoundingClientRect(); o.onHover?.({ kind: "group", id: t.id, x: b.left - c.left, y: b.bottom - c.top, near: [] }); });
      el.addEventListener("mouseleave", () => { o.onHover?.(null); });
      gesturesThrough(el, () => { if (o.onGroup?.(t.id) !== true) handle.frame(t.points); });
      titles.push({ ...t, el, w, h: px + 6, natural: w, line: px + 6, wrapped: false, wrapW: 0, measured: false, cx: pts.reduce((s, p) => s + p.x, 0) / pts.length, x0: Math.min(...pts.map((p) => p.x)), x1: Math.max(...pts.map((p) => p.x)),
        y0: Math.min(...pts.map((p) => p.y)), y1: Math.max(...pts.map((p) => p.y)) });
    }
    // pinned names (学点新的's blocks): bold, beside their point, clear of every other name and title
    if (o.pinned && o.semantic !== true) G.forEachNode((id, a) => {
      if (o.pinned?.(id) !== true || !a.label) return;
      const el = layer.appendChild(document.createElement("div"));
      el.className = "ew-gtitle ew-gpin"; el.textContent = a.label; el.dataset.node = id;
      if (measure) measure.font = font(13, 600);
      const w = (measure?.measureText(a.label).width ?? a.label.length * 13) + 4;
      el.addEventListener("mouseenter", () => {
        hov = id; sigma.refresh({ skipIndexation: true });
        const b = el.getBoundingClientRect(), c = container.getBoundingClientRect();
        container.style.cursor = o.canClick?.(id) === false ? "" : "pointer";
        el.style.cursor = container.style.cursor;
        const pts = G.filterNodes((m) => m === id || related(id, m)).map((m) => sigma.graphToViewport(G.getNodeAttributes(m)));
        o.onHover?.({ kind: "node", id: realId(id), x: b.left - c.left, y: b.bottom - c.top, near: [...new Set(G.neighbors(id).map(realId))],
          around: { x0: Math.min(b.left - c.left, ...pts.map((q) => q.x - 60)), y0: b.top - c.top, x1: Math.max(b.right - c.left, ...pts.map((q) => q.x + 60)), y1: Math.max(b.bottom - c.top, ...pts.map((q) => q.y + 26)) } });
      });
      el.addEventListener("mouseleave", () => { hov = null; sigma.refresh({ skipIndexation: true }); container.style.cursor = ""; o.onHover?.(null); });
      gesturesThrough(el, () => { o.onSelect?.(realId(id)); });
      // above its cloud (a block with no points of its own: above its dot)
      const own = a.group === null ? [a] : G.filterNodes((_, b) => b.group === a.group).map((m) => G.getNodeAttributes(m));
      const ys = own.map((b) => b.y);
      pins.push({ id, el, w, h: 19, measured: false, y0: Math.min(...ys), y1: Math.max(...ys), x: a.x });
    });
  };
  let pins: { id: string; el: HTMLElement; w: number; h: number; measured: boolean; x: number; y0: number; y1: number }[] = [];
  /** How far a title stands over its highest point: TITLE_GAP, more when zooming in has made the dots bigger than that. */
  const titleGap = (): number => {
    return Math.max(TITLE_GAP, (dotPx() * (o.pinned !== undefined ? PIN_SIZE + LIT_GROW : ROOM_DOT + LIT_GROW)) / ROOM_DOT + ROOM_DRIFT + 3);
  };
  /** A block's name centred above its cloud, over the highest point (as the group titles). */
  const above = (p: { w: number; h: number; x: number; y0: number; y1: number }, s: Sigma<NodeAttrs, EdgeAttrs>): Box => {
    const a = s.graphToViewport({ x: p.x, y: p.y0 }), b = s.graphToViewport({ x: p.x, y: p.y1 }), top = Math.min(a.y, b.y) - titleGap();
    return { x0: a.x - p.w / 2, y0: top - p.h, x1: a.x + p.w / 2, y1: top };
  };
  /** The pinned names, each where it covers no other; their boxes, for the titles to keep clear of. */
  const placePins = (W: number, H: number): Box[] => {
    const boxes: Box[] = [];
    for (const p of pins) if (!p.measured && p.el.style.display === "block" && p.el.offsetWidth > 0) { p.w = p.el.offsetWidth; p.h = p.el.offsetHeight; p.measured = true; }
    const at = pins.map((p) => {
      const a = G.hasNode(p.id) ? G.getNodeAttributes(p.id) : null;
      const v = a === null ? null : sigma.graphToViewport(a);
      const d = sigma.getNodeDisplayData(p.id);
      const r = d !== undefined && typeof (sigma as { scaleSize?: (s: number) => number }).scaleSize === "function"
        ? (sigma as unknown as { scaleSize: (s: number) => number }).scaleSize(d.size) : 7;
      return { p, a, v, r };
    });
    // every pinned dot is taken first: a name never covers another block's point
    for (const { v, r } of at) if (v !== null) boxes.push({ x0: v.x - r, y0: v.y - r, x1: v.x + r, y1: v.y + r });
    const names: Box[] = [];
    for (const { p, a, v } of at) {
      const g = a?.grow ?? 1;
      if (a === null || v === null || g < 0.05 || v.x < -p.w || v.x > W + p.w || v.y < -p.h || v.y > H + p.h) { p.el.style.display = "none"; continue; }
      let b = above(p, sigma);
      // never cut by the box's edge
      { const dx = Math.max(0, -b.x0) - Math.max(0, b.x1 - W); b = { ...b, x0: b.x0 + dx, x1: b.x1 + dx }; }
      // no place clear of another block's name (a very small box): this one waits for a closer zoom
      if (names.some((n) => boxesOverlap(n, b)) || dotBoxes.some((d) => boxesOverlap(d, b))) { p.el.style.display = "none"; continue; }
      boxes.push(b); names.push(b);
      const dim = hov !== null && hov !== p.id && !G.areNeighbors(hov, p.id);
      Object.assign(p.el.style, { display: "block", left: `${b.x0}px`, top: `${b.y0}px`, transform: "none", opacity: String((dim ? 0.3 : 1) * g),
        color: C["tx-1"], fontFamily: o.font() });
    }
    return boxes;
  };
  /** A semantic-zoom title's box at this camera: over its highest point. */
  const titleBox = (t: { w: number; h: number; cx: number; y0: number; y1: number }): Box => {
    const a = sigma.graphToViewport({ x: t.cx, y: t.y0 }), b = sigma.graphToViewport({ x: t.cx, y: t.y1 }), top = Math.min(a.y, b.y) - titleGap();
    return { x0: a.x - t.w / 2, y0: top - t.h, x1: a.x + t.w / 2, y1: top };
  };
  const setZoom = (l: ZoomLevel): void => {
    if (l === zoom) return;
    if (dotsNow(l) !== dotsNow(zoom)) { dotsFrom = dotsFade(); dotsAt = performance.now(); }
    zoomFrom = zoom; zoomAt = performance.now(); zoom = l;
    // a hovered dot gone with its level takes its tip along, and has it back with the level (Sigma still has it hovered: no new enter comes)
    if (hov !== null) { if (dotsNow(l)) tellHover(hov); else o.onHover?.(null); }
    sigma.refresh({ skipIndexation: true });
  };
  /** How much of the dots shows now: they fade in at the block level, out above it. */
  const dotsFade = (): number => {
    const to = dotsNow(zoom) ? 1 : 0, t = o.reduced() ? 1 : Math.min(1, (performance.now() - dotsAt) / DOTS_FADE_MS);
    return dotsFrom + (to - dotsFrom) * t;
  };
  const placeTitles = (): void => {
    if (!layer) return;
    const near = sigma.getCamera().getState().ratio < NEAR_RATIO;
    const W = container.clientWidth, H = container.clientHeight;
    const taken: [number, number, number, number][] = placePins(W, H).map((b) => [b.x0, b.y0, b.x1, b.y1]);
    // what stands fixed over the picture (the legend, ＋ / －): no title or name goes under it
    if (o.semantic === true) {
      const c = container.getBoundingClientRect();
      for (const e of container.parentElement?.querySelectorAll(":scope > .ew-legend > span, :scope > .ew-zoom") ?? []) { const r = e.getBoundingClientRect(); taken.push([r.left - c.left, r.top - c.top, r.right - c.left, r.bottom - c.top]); }
    }
    if (o.semantic === true && scene !== null) setZoom(levelFor(zoomTitles(scene).levels, sigma.getCamera().getState().ratio * levelScale, performance.now() < settleUntil ? undefined : zoom, (r) => finestFit(r / levelScale)));
    // a title wider than the box wraps inside it
    for (const t of titles) if (t.level !== undefined) {
      const wrap = t.natural > W - TITLE_INSET;
      if (wrap === t.wrapped && (!wrap || t.wrapW === W)) continue;
      t.wrapped = wrap; t.wrapW = W; t.measured = false;
      t.w = wrap ? W - TITLE_INSET : t.natural; t.h = wrap ? t.line * Math.ceil(t.natural / (W - TITLE_INSET)) : t.line;
      Object.assign(t.el.style, { whiteSpace: wrap ? "normal" : "", width: wrap ? `${W - TITLE_INSET}px` : "", boxSizing: wrap ? "border-box" : "", textAlign: wrap ? "center" : "" });
    }
    // the canvas estimate of a title's size gives way to its rendered size once it has been shown (read all at once)
    for (const t of titles) if (!t.measured && t.el.style.display === "block" && t.el.offsetWidth > 0) {
      t.w = t.el.offsetWidth; t.h = t.el.offsetHeight; t.measured = true;
    }
    const inFocus = (t: { points: string[] }): boolean => state.focus === null || t.points.some((id) => state.focus!.has(id));
    // the goal in focus first: its titles take their places, the others give way
    for (const t of state.focus === null ? titles : [...titles].sort((a, b) => Number(inFocus(b)) - Number(inFocus(a)))) {
      // zoomed in on the goal, the other goals' blocks say nothing
      const show = t.level !== undefined ? t.level === zoom && !(dotsNow(zoom) && !inFocus(t)) : t.near === near;
      const a = sigma.graphToViewport({ x: t.cx, y: t.y0 }), b = sigma.graphToViewport({ x: t.cx, y: t.y1 });
      if (t.level !== undefined) {
        // kept in the layout and moving with the picture, so a level fades instead of snapping
        let at = titleBox(t);
        // a level with no dots is its regions and titles: a region in view keeps its title, inside the box, or the view would be blank
        if (show && !dotsNow(zoom)) {
          const c0 = sigma.graphToViewport({ x: t.x0, y: t.y1 }), c1 = sigma.graphToViewport({ x: t.x1, y: t.y0 });
          if (c0.x < W && c1.x > 0 && c0.y < H && c1.y > 0) {
            const dx = Math.max(0, -at.x0) - Math.max(0, at.x1 - W), dy = Math.max(0, -at.y0) - Math.max(0, at.y1 - H);
            at = { x0: at.x0 + dx, x1: at.x1 + dx, y0: at.y0 + dy, y1: at.y1 + dy };
          }
        }
        const blocked = (q: Box): boolean => q.x0 < 0 || q.x1 > W || q.y0 < 0 || q.y1 > H || taken.some((r) => q.x0 < r[2] && q.x1 > r[0] && q.y0 < r[3] && q.y1 > r[1]) || (dotsNow(zoom) && dotBoxes.some((d) => boxesOverlap(d, q)));
        // its cloud in view: over it, pulled into the box, or under it, before it waits for a closer zoom
        if (show && blocked(at)) {
          const c0 = sigma.graphToViewport({ x: t.x0, y: t.y1 }), c1 = sigma.graphToViewport({ x: t.x1, y: t.y0 });
          if (c0.x < W && c1.x > 0 && c0.y < H && c1.y > 0) {
            const inX = (q: Box): Box => { const dx = Math.max(0, -q.x0) - Math.max(0, q.x1 - W); return { ...q, x0: q.x0 + dx, x1: q.x1 + dx }; };
            const down = sigma.graphToViewport({ x: t.cx, y: t.y0 }).y + titleGap(), under = { x0: at.x0, x1: at.x1, y0: down, y1: down + t.h };
            // just under what stands in its way (the ＋ / － over a wide title at the top of a narrow box)
            const past = (q: Box): Box => { const r = taken.find((o) => q.x0 < o[2] && q.x1 > o[0] && q.y0 < o[3] && q.y1 > o[1]); return r === undefined ? q : { ...q, y0: r[3] + 2, y1: r[3] + 2 + (q.y1 - q.y0) }; };
            at = [inX(at), under, inX(under), past(inX(at))].find((q) => !blocked(q)) ?? at;
          }
        }
        const on = show && !blocked(at);
        if (on) taken.push([at.x0, at.y0, at.x1, at.y1]);
        const dim = hov !== null || (state.focus !== null && !t.points.some((id) => state.focus!.has(id)));
        Object.assign(t.el.style, { display: "block", left: `${at.x0 + t.w / 2}px`, top: `${at.y1}px`, opacity: on ? (dim ? "0.3" : "1") : "0", visibility: on ? "visible" : "hidden",
          background: "transparent", color: C["tx-1"], fontFamily: o.font(), pointerEvents: on ? "" : "none" });
        continue;
      }
      const top = Math.min(a.y, b.y) - titleGap() - (t.lift ?? 0), L = a.x - t.w / 2, T = top - t.h;
      const clash = taken.some((r) => L < r[2] && L + t.w > r[0] && T < r[3] && T + t.h > r[1]) || dotBoxes.some((d) => L < d.x1 && L + t.w > d.x0 && T < d.y1 && T + t.h > d.y0);
      if (!show || clash || a.x < -t.w || a.x > W + t.w || top < 0 || top > H + t.h) { t.el.style.display = "none"; continue; }
      taken.push([L, T, L + t.w, T + t.h]);
      const dim = hov !== null || (state.focus !== null && !t.points.some((id) => state.focus!.has(id)));
      Object.assign(t.el.style, { display: "block", left: `${a.x}px`, top: `${top}px`, opacity: dim ? "0.3" : "1",
        background: "transparent", color: C["tx-1"], fontFamily: o.font() });
    }
    titleBoxes = taken.map(([x0, y0, x1, y1]) => ({ x0, y0, x1, y1 }));
  };
  if (layer) sigma.on("afterRender", placeTitles);

  // territories: each point paints a disc in its group's colour on an off-screen canvas, which is laid behind the
  // canvases blurred and faint (where groups meet, the later one's colour wins: a border, softened by the blur)
  const wash = o.small ? null : container.insertBefore(document.createElement("canvas"), container.firstChild);
  const ink = wash ? document.createElement("canvas") : null;
  if (wash) wash.className = "ew-gwash";
  let hue = new Map<ZoomLevel, Map<string, string>>(), domainOf = new Map<string, string>(), unit = 0, zoomFrom: ZoomLevel | null = null, zoomAt = 0, dotsFrom = 0, dotsAt = -Infinity;
  const paintWash = (): void => {
    const wc = wash?.getContext("2d"), ic = ink?.getContext("2d");
    if (!wash || !ink || !wc || !ic) return;
    const W = container.clientWidth, H = container.clientHeight, dpr = window.devicePixelRatio || 1;
    for (const c of [wash, ink]) if (c.width !== Math.round(W * dpr) || c.height !== Math.round(H * dpr)) { c.width = Math.round(W * dpr); c.height = Math.round(H * dpr); }
    wc.setTransform(1, 0, 0, 1, 0, 0); wc.clearRect(0, 0, wash.width, wash.height);
    if (W === 0 || H === 0 || G.order === 0) return;
    const levelKey = (l: ZoomLevel, a: NodeAttrs): string | null => (l === "point" ? null : l === "domain" ? domainOf.get(a.top) ?? a.top : l === "top" ? a.top : a.group ?? a.top);
    if (o.washByBand) {
      // big-goal-demo.html: each group of the level a round glow in its points' average colour, breathing a little
      const glowLevel = (l: ZoomLevel, share: number): void => {
        const groups = new Map<string, { x: number; y: number; n: number; colors: string[]; ps: { x: number; y: number }[]; mine: boolean }>();
        G.forEachNode((_, a) => {
          const key = levelKey(l, a);
          if (key === null) return;
          const p = sigma.graphToViewport(a), g = groups.get(key) ?? { x: 0, y: 0, n: 0, colors: [], ps: [], mine: false };
          g.x += p.x; g.y += p.y; g.n++; g.colors.push(G.getNodeAttribute(_, "color")); g.ps.push(p); g.mine ||= state.focus === null || state.focus.has(_); groups.set(key, g);
        });
        const now = o.reduced() ? null : performance.now();
        wc.setTransform(dpr, 0, 0, dpr, 0, 0);
        for (const [key, g] of groups) {
          const x = g.x / g.n, y = g.y / g.n, far = Math.max(...g.ps.map((p) => Math.hypot(p.x - x, p.y - y)));
          const phase = [...key].reduce((t, ch) => t + ch.charCodeAt(0), 0);
          const rr = Math.max(46, far * 1.6 + 30) * (now === null ? 1 : 1 + GLOW_BREATH * Math.sin(now / 1900 + phase));
          const tint = glowTint(g.colors), glow = wc.createRadialGradient(x, y, 0, x, y, rr);
          const strength = share * (g.mine ? 1 : GLOW_BACK);
          glow.addColorStop(0, rgb(tint, GLOW_MID * strength)); glow.addColorStop(0.6, rgb(tint, GLOW_OUT * strength)); glow.addColorStop(1, rgb(tint, 0));
          wc.fillStyle = glow; wc.beginPath(); wc.arc(x, y, rr, 0, Math.PI * 2); wc.fill();
        }
      };
      const t = zoomFrom === null || o.reduced() ? 1 : (performance.now() - zoomAt) / CROSSFADE_MS;
      if (t >= 1) { zoomFrom = null; glowLevel(zoom, 1); return; }
      glowLevel(zoomFrom!, 1 - t);
      glowLevel(zoom, t);
      return;
    }
    const a0 = sigma.graphToViewport({ x: 0, y: 0 }), a1 = sigma.graphToViewport({ x: unit, y: 0 });
    const r = Math.min(80, Math.max(8, Math.hypot(a1.x - a0.x, a1.y - a0.y) * 0.9));
    /** One level's regions at this share of the wash's strength. */
    const paintLevel = (l: ZoomLevel, share: number): void => {
      // the finest level is points far apart: a territory would only be a smudge behind each dot
      if (l === "point") return;
      const colors = hue.get(l);
      ic.setTransform(dpr, 0, 0, dpr, 0, 0); ic.clearRect(0, 0, W, H);
      G.forEachNode((_, a) => {
        const c = colors?.get(levelKey(l, a) ?? "");
        if (c === undefined) return;
        const p = sigma.graphToViewport(a);
        if (p.x < -r || p.y < -r || p.x > W + r || p.y > H + r) return;
        ic.fillStyle = c; ic.beginPath(); ic.arc(p.x, p.y, r, 0, Math.PI * 2); ic.fill();
      });
      wc.globalAlpha = WASH_ALPHA * share; wc.filter = `blur(${Math.round(r * WASH_BLUR * dpr)}px)`;
      wc.drawImage(ink, 0, 0);
      wc.filter = "none"; wc.globalAlpha = 1;
    };
    const t = zoomFrom === null || o.reduced() ? 1 : (performance.now() - zoomAt) / CROSSFADE_MS;
    if (t >= 1) { zoomFrom = null; paintLevel(zoom, 1); return; }
    paintLevel(zoomFrom!, 1 - t);
    paintLevel(zoom, t);
  };
  if (wash) sigma.on("afterRender", paintWash);
  // the dots' size follows the camera (nodeReducer)
  sigma.getCamera().on("updated", () => { sigma.refresh({ skipIndexation: true }); });

  let scene: Scene | null = null;
  // floating: every point on its own slow ellipse around its laid-out place (seeded, so a graph floats the same way)
  let home = new Map<string, { x: number; y: number; w: number; p: number; q: number }>(), home0 = new Map<string, { x: number; y: number }>(), amp = 0, raf = 0, last = -Infinity;
  // when the data moved the map: each point glides from where it stood
  let glide: { from: Map<string, { x: number; y: number }>; at: number } | null = null;
  const settle = (): void => {
    const r = rng(23);
    gapWorld = nearestGap(G);
    amp = FLOAT_AMP * gapWorld;
    unit = WASH_REACH;
    if (G.order > 0) {
      // the map's own box: fixed, so the camera at ratio 1 is the whole map, whatever the box's shape. It holds the territories
      // too, blur included, not only the points; the thumbnail has none
      const xs = [...home0.values()].map((p) => p.x), ys = [...home0.values()].map((p) => p.y), pad = o.small === true ? 0.15 : WASH_REACH * WASH_ROOM;
      mapW = Math.max(...xs) - Math.min(...xs) + 2 * pad; mapH = Math.max(...ys) - Math.min(...ys) + 2 * pad;
      sigma.setCustomBBox({ x: [Math.min(...xs) - pad, Math.max(...xs) + pad], y: [Math.min(...ys) - pad, Math.max(...ys) + pad] });
    } else sigma.setCustomBBox(null);
    home = new Map([...home0].map(([id, p]) => [id, { x: p.x, y: p.y, w: (2 * Math.PI) / (FLOAT_PERIOD[0] + r() * (FLOAT_PERIOD[1] - FLOAT_PERIOD[0])) / 1000,
      p: r() * 2 * Math.PI, q: r() * 2 * Math.PI }]));
  };
  // grow / fade (cloud-anim.ts): when each growing point was born, each fading point's colour and start
  const born = new Map<string, number>();
  const fades = new Map<string, { from: string; at: number }>();
  /** One frame of the grow / fade animations at `t` (graphology attributes); false when none is running. */
  const animate = (t: number): boolean => {
    if (born.size === 0 && fades.size === 0) return false;
    const reduced = o.reduced();
    // one update of the whole graph per frame (a full refresh), never one partial refresh per point
    G.updateEachNodeAttributes((id, a) => {
      const b = born.get(id), f = fades.get(id);
      if (b === undefined && f === undefined) return a;
      const next = { ...a };
      if (b !== undefined) {
        const g = grownAt(b, t, reduced);
        if (g >= 1) { born.delete(id); delete next.grow; } else next.grow = g;
      }
      if (f !== undefined) {
        const step = fadeAt(f.from, C[a.band as keyof CloudColors] || C.none, f.at, t, reduced);
        next.color = step.color;
        if (step.done) fades.delete(id);
      }
      return next;
    });
    for (const id of [...born.keys(), ...fades.keys()]) if (!G.hasNode(id)) { born.delete(id); fades.delete(id); }
    return true;
  };
  const drift = (t: number): void => {
    raf = requestAnimationFrame(drift);
    if (t - last < 1000 / FLOAT_FPS || document.hidden || !container.isConnected || container.offsetWidth === 0) return;
    last = t;
    const growing = born.size;
    const moving = animate(performance.now());
    // the last point grown: its edges show now (Sigma's update of the nodes leaves the edges as they were)
    if (growing > 0 && born.size === 0) sigma.refresh();
    if (o.reduced() || amp === 0) {
      // nothing floats; a breathing ring is still redrawn
      if (!moving && (state.pulse?.size ?? 0) > 0 && !o.reduced()) sigma.refresh({ skipIndexation: true });
      return;
    }
    const gt = glide === null ? 1 : Math.min(1, (performance.now() - glide.at) / GLIDE_MS), ease = 1 - (1 - gt) ** 3;
    G.updateEachNodeAttributes((id, a) => {
      const h = home.get(id);
      if (h === undefined) return a;
      const from = gt < 1 ? glide?.from.get(id) : undefined;
      const hx = from === undefined ? h.x : from.x + (h.x - from.x) * ease, hy = from === undefined ? h.y : from.y + (h.y - from.y) * ease;
      return { ...a, x: hx + amp * Math.sin(t * h.w + h.p), y: hy + amp * Math.cos(t * h.w * 0.8 + h.q) };
    }, moving ? undefined : { attributes: ["x", "y"] });
    if (gt >= 1) glide = null;
  };
  if (typeof requestAnimationFrame === "function") raf = requestAnimationFrame(drift);

  // Sigma only follows the window's size: a pane resized, a tab shown again or the thumbnail's box placed resize it here
  let wasShown = container.clientWidth > 0 && container.clientHeight > 0, showing = 0;
  let framed: { ids: readonly string[]; level: ZoomLevel; close: boolean } | null = null, framedBox = [container.clientWidth, container.clientHeight];
  const ro = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(() => {
    sigma.resize();
    // A tab restored in the background (Obsidian opened, the plugin reloaded): when it comes to the front Chromium can paint
    // the titles and leave every canvas empty, with a small missing-image box in the corner, though the canvases hold their
    // picture. Taking them out of the layout once, when the box has settled, makes it show them.
    const shown = container.clientWidth > 0 && container.clientHeight > 0;
    if (shown && (!wasShown || showing !== 0)) {
      window.clearTimeout(showing);
      showing = window.setTimeout(() => {
        showing = 0;
        const canvases = [...container.querySelectorAll("canvas")];
        for (const c of canvases) c.style.display = "none";
        void container.offsetHeight;
        for (const c of canvases) c.style.display = "";
        sigma.refresh();
      }, SHOWN_SETTLE_MS);
    }
    // what the camera was sent to see stays in view in the new box (the map itself does not change)
    if (shown && framed !== null && (container.clientWidth !== framedBox[0] || container.clientHeight !== framedBox[1])) handle.frame(framed.ids, framed.level, framed.close);
    framedBox = [container.clientWidth, container.clientHeight];
    wasShown = shown;
    sigma.refresh();
  });
  ro?.observe(container);
  if (o.small) container.style.pointerEvents = "none";
  else {
    // a dot faded out at a coarse level is not there to click or hover
    const live = (): boolean => o.semantic !== true || dotsNow(zoom);
    sigma.on("clickNode", ({ node }) => { if (live()) o.onSelect?.(realId(node)); });
    sigma.on("clickStage", () => { o.onSelect?.(null); });
    // the user's own hand on the camera: a change of box leaves it where he put it
    for (const ev of ["downStage", "wheelStage", "downNode", "wheelNode"] as const) sigma.on(ev, () => { framed = null; hand = true; });
    sigma.on("enterNode", ({ node }) => {
      if (!live()) return;
      hov = node; sigma.refresh({ skipIndexation: true });
      container.style.cursor = o.canClick?.(node) === false ? "" : "pointer";
      tellHover(node);
    });
    sigma.on("leaveNode", () => { hov = null; sigma.refresh({ skipIndexation: true }); container.style.cursor = ""; o.onHover?.(null); });
  }

  /** The views' tip for the hovered point, where it stands now. */
  function tellHover(node: string): void {
    const d = sigma.getNodeDisplayData(node); if (!d) return;
    const p = sigma.framedGraphToViewport(d);
    const px = (sigma as unknown as { scaleSize?: (s: number) => number }).scaleSize?.(d.size) ?? d.size;
    o.onHover?.({ kind: "node", id: realId(node), x: p.x, y: p.y, r: px, named: o.semantic === true && namedNow(node), near: [...new Set(G.neighbors(node).map(realId))] });
  }
  /** Reduced motion: at once (Sigma's animate with duration 0 yields NaN). */
  const go = (to: { x?: number; y?: number; ratio?: number }, ms: number): void => {
    const cam = sigma.getCamera(), next = { ...cam.getState(), ...to };
    if (o.reduced()) cam.setState(next); else void cam.animate(next, { duration: ms });
  };
  // The plugin's stylesheet can arrive after the view opens (Obsidian starting, the plugin reloaded): the colour tokens are
  // still empty, every dot would stay Sigma's default grey and no group would have its glow. Read them again until they are there.
  let colorWait = 0, colorTries = 0;
  const awaitColors = (): void => {
    if (colorWait !== 0 || C.none !== "" || colorTries >= COLOR_TRIES) return;
    colorWait = window.setTimeout(() => {
      colorWait = 0; colorTries++;
      if (!o.tokens.isConnected) return;       // the view is gone: no stylesheet will reach it
      handle.recolor();
      sigma.refresh();
      awaitColors();
    }, COLOR_WAIT_MS);
  };
  const zoomIn = (): void => {
      framed = null; hand = true;
      const cam = sigma.getCamera(), ratio = Math.max(MIN_RATIO, stepRatio(cam.ratio, 1 / 1.6));
      // closer in with no point left in view is an empty picture: the camera goes to the nearest point
      let near: NodeDisplayData | null = null, best = Infinity;
      G.forEachNode((id) => {
        const d = sigma.getNodeDisplayData(id);
        if (d === undefined) return;
        const far = Math.max(Math.abs(d.x - cam.x), Math.abs(d.y - cam.y));
        if (far < best) { best = far; near = d; }
      });
      const to = near as NodeDisplayData | null;
      go(to !== null && best > ratio * 0.4 ? { x: to.x, y: to.y, ratio } : { ratio }, 250);
  };
  const zoomOut = (): void => { framed = null; hand = true; go({ ratio: Math.min(3, stepRatio(sigma.getCamera().ratio, 1.6)) }, 250); };
  // one notch of the wheel, one level, as ＋ / －; a trackpad's run of small events is one step
  let wheelAt = 0;
  if (o.semantic === true) container.addEventListener("wheel", (e) => {
    e.preventDefault(); e.stopPropagation();
    const now = performance.now();
    if (now - wheelAt < WHEEL_STEP_MS || e.deltaY === 0) return;
    wheelAt = now;
    if (e.deltaY < 0) zoomIn(); else zoomOut();
  }, { capture: true, passive: false });
  const handle: GraphCloudHandle = {
    sigma,
    graph: () => G,
    shownNames: () => [...frameNames],
    zoomLevel: () => zoom,
    levels: () => (scene === null ? [] : zoomTitles(scene).levels),
    worldOf: (id) => home0.get(id) ?? null,
    setScene(S) {
      const fresh = G.order === 0;
      C = readCloudColors(o.tokens);
      hov = null;
      const was = G, now = performance.now();
      scene = S;
      G = S === null ? new Graph() : sceneGraph(S, C);
      home0 = new Map(G.mapNodes((id, a) => [id, { x: a.x, y: a.y }]));
      // where the server moved a point (the map changed with the data): it glides there from where it stood
      const moved = new Map<string, { x: number; y: number }>();
      G.forEachNode((id, a) => { const h = home.get(id); if (was.hasNode(id) && h !== undefined && Math.hypot(h.x - a.x, h.y - a.y) > 1e-6) moved.set(id, { x: h.x, y: h.y }); });
      glide = moved.size > 0 && !o.reduced() ? { from: moved, at: now } : null;
      // a goal's point new to the cloud grows in (one after another); one whose band changed fades from what it showed
      // one after another, the whole of it in about a second however many there are
      let k = 0;
      const growing = G.filterNodes((id) => !was.hasNode(id) && o.grows?.(id) === true).length, stagger = Math.min(GROW_STAGGER, GROW_TOTAL_MS / Math.max(1, growing));
      G.forEachNode((id, a) => {
        if (!was.hasNode(id)) {
          if (o.grows?.(id) === true && !o.reduced()) { born.set(id, now + k++ * stagger); G.setNodeAttribute(id, "grow", 0); }
          return;
        }
        const g = was.getNodeAttribute(id, "grow");
        if (g !== undefined) G.setNodeAttribute(id, "grow", g);
        const shown = was.getNodeAttribute(id, "color");
        if (shown !== a.color && !o.reduced()) { fades.set(id, { from: shown, at: now }); G.setNodeAttribute(id, "color", shown); }
      });
      settle();
      sigma.setGraph(G);
      makeTitles(S);
      needs = S !== null && o.semantic === true ? levelNeeds(S) : new Map<ZoomLevel, number>();
      // one colour per group at each level, in title order
      hue = new Map(); domainOf = new Map((S?.domains ?? []).flatMap((d) => d.kids.map((k) => [k, d.id] as const)));
      if (S !== null && o.semantic === true) for (const l of ["domain", "top", "block"] as const) hue.set(l, new Map(zoomTitles(S).titles.filter((t) => t.level === l).map((t, i) => [t.id, WASH[i % WASH.length]!])));
      // the blocks are not a level of their own: zoomed in, the wash stays the top groups'
      if (S !== null && o.semantic === true && hue.get("block")!.size === 0) hue.set("block", new Map(S.l1.map((g, i) => [g.id, WASH[i % WASH.length]!])));
      // the first view settles in from a little further out
      if (fresh) {
        sigma.getCamera().setState({ x: 0.5, y: 0.5, ratio: 1, angle: 0 });
      }
      placeTitles();
      awaitColors();
    },
    setState(s) { state = s; sigma.refresh({ skipIndexation: true }); },
    recolor() {
      C = readCloudColors(o.tokens);
      fades.clear();
      G.forEachNode((id, a) => { G.setNodeAttribute(id, "color", C[a.band as keyof CloudColors] || C.none); });
      G.forEachEdge((e) => { G.setEdgeAttribute(e, "color", C.edge); });
    },
    zoomIn,
    zoomOut,
    frame(ids, level, close) {
      // the user's own hand on the camera: only his next goal moves it again
      if (level !== undefined && hand) return;
      framed = level === undefined ? null : { ids, level, close: close === true };
      const ps = ids.map((id) => sigma.getNodeDisplayData(id)).filter((d): d is NodeDisplayData => d !== undefined);
      if (!ps.length) return;
      const xs = ps.map((p) => p.x), ys = ps.map((p) => p.y);
      const mid = { x: (Math.min(...xs) + Math.max(...xs)) / 2, y: (Math.min(...ys) + Math.max(...ys)) / 2 };
      if (level === undefined) {
        const span = Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys), 0.05);
        levelScale = 1;
        go({ ...mid, ratio: Math.min(1, Math.max(FRAME_MIN, span * 1.5)) }, 500);
        return;
      }
      // all of them in the box, with room for a title over them and the legend under them
      const world = ids.map((id) => home0.get(id)).filter((p) => p !== undefined);
      const perWorld = wholeMapScale();
      const W = container.clientWidth - 2 * FRAME_SIDE, H = container.clientHeight - FRAME_TOP - FRAME_BOTTOM;
      const needW = Math.max(...world.map((p) => p.x)) - Math.min(...world.map((p) => p.x)), needH = Math.max(...world.map((p) => p.y)) - Math.min(...world.map((p) => p.y));
      const whole = W > 0 && H > 0 && perWorld > 0 ? Math.max(FRAME_LEAST, Math.min(1, Math.max((needW * perWorld) / W, (needH * perWorld) / H))) : 1;
      // the level comes first (user 2026-11-04): a box too narrow for the whole goal at this level shows part of it, the rest by dragging. `close`: just where the
      // level comes in, the points in the middle and their neighbours round them (no closer: a block alone would fill the picture)
      const need = (needs.get(level) ?? 0) * FIT_MARGIN, atLevel = W > 0 && H > 0 && perWorld > 0 && need > 0 ?Math.max(MIN_RATIO, Math.min(1, perWorld / need)) : whole;
      const ratio = close === true || (perWorld > 0 && (needs.get(level) ?? 0) > perWorld / whole) ? atLevel : whole;
      // part of the goal in view: its first block whole, not the gap between two
      if (close !== true && ratio < whole) {
        const g = G.getNodeAttribute(ids[0]!, "group"), own = ids.filter((id) => G.getNodeAttribute(id, "group") === g).map((id) => sigma.getNodeDisplayData(id)).filter((d): d is NodeDisplayData => d !== undefined);
        const ox = own.map((d) => d.x), oy = own.map((d) => d.y);
        mid.x = (Math.min(...ox) + Math.max(...ox)) / 2; mid.y = (Math.min(...oy) + Math.max(...oy)) / 2;
      }
      // what is framed shows at this level, however much of the map it takes: the bands count from here (they are the map's otherwise)
      const levels = scene === null ? [] : zoomTitles(scene).levels, at = levels.indexOf(level), edges = BANDS[levels.length] ?? [];
      levelScale = at < 0 ? 1 : Math.sqrt((at === 0 ? 1 : edges[at - 1]!) * (edges[at] ?? 0.03)) / ratio;
      // sigma's y runs up in its own frame: the top's title room moves the middle down by half the difference
      const shift = ((FRAME_TOP - FRAME_BOTTOM) / 2) * (ratio / Math.max(1, Math.max(container.clientWidth, container.clientHeight)));
      // where the camera lands decides the level: no holding on to the one it left
      settleUntil = performance.now() + FRAME_MS + 200;
      go({ x: mid.x, y: mid.y + shift, ratio }, FRAME_MS);
    },
    fit() { framed = null; hand = false; levelScale = 1; sigma.getCamera().setState({ x: 0.5, y: 0.5, ratio: 1, angle: 0 }); },
    destroy() { if (raf) cancelAnimationFrame(raf); window.clearTimeout(showing); window.clearTimeout(colorWait); ro?.disconnect(); layer?.remove(); wash?.remove(); sigma.kill(); },
  };
  return handle;
}
