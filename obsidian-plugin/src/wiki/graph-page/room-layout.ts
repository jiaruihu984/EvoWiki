/**
 * Where 学点新的's groups go (pure, in the box's pixels; the plugin's sigma-cloud.ts and the demo's big-goal-demo.html both
 * use it): no title ever covers a title or a point of another group, whatever the box. noverlap only keeps circles apart
 * and a title is a wide box, so each group is measured (its title's real size above its cloud of points) and laid out in
 * rows: his regions in the top and bottom corners (`at` outside the unit box, as the grow demo), the goal's blocks in the
 * rows between; the spare height goes evenly between the rows, so the picture fills the box. The largest point spacing that
 * fits is taken; when nothing fits the rows run past the box (`height` says how far) and the caller shrinks the picture.
 */

import { removeOverlaps, Rectangle } from "webcola/dist/src/rectangle";

export interface RoomGroup {
  id: string;
  /** "" : no title. */ title: string;
  /** The title's font size. */ titlePx: number;
  points: number;
  at: [number, number] | null;
}
export interface RoomPlace { x: number; y: number }
export interface RoomLayout {
  /** Each group's middle (its first point), from the box's top left. */ places: Map<string, RoomPlace>;
  /** The distance unit of `sunflower`. */ spacing: number;
  /** How tall the rows came out (at most the box's height when they fit). */ height: number;
}

/** A title's padding and the room under it to its cloud; a dot's radius; the gaps between groups; the point spacings tried. */
export const TITLE_PAD = 14, TITLE_GAP = 12, DOT_R = 8, GROUP_GAP_X = 16, GROUP_GAP_Y = 12;
const SPACINGS = [30, 26, 22, 20, 18, 16, 14, 12, 10, 8, 6];

/** A title's width at `px` (no canvas: pure): CJK a full square, the rest about 0.62 of it, with its padding. */
export const titleWidth = (s: string, px: number): number => [...s].reduce((w, ch) => w + (ch.codePointAt(0)! > 0x2e7f ? px : px * 0.62), 0) + TITLE_PAD;
export const titleHeight = (px: number): number => Math.ceil(px * 1.3) + 4;

/** The i-th point of a cloud round its middle (i = 0 is the middle; even spacing, no two near each other). */
export function sunflower(i: number, spacing: number): RoomPlace {
  const r = spacing * Math.sqrt(i), a = i * 2.39996;
  return { x: r * Math.cos(a), y: r * Math.sin(a) };
}
/** How far a group's cloud reaches from its middle. */
export const cloudRadius = (points: number, spacing: number): number => spacing * Math.sqrt(Math.max(0, points - 1)) + DOT_R;

interface Box { g: RoomGroup; w: number; h: number; lead: number; r: number }

export function packGroups(groups: readonly RoomGroup[], W: number, H: number): RoomLayout {
  const outer = (g: RoomGroup): boolean => g.at !== null && Math.max(Math.abs(g.at[0]), Math.abs(g.at[1])) >= 1;
  const byX = (a: RoomGroup, b: RoomGroup): number => (a.at?.[0] ?? 0) - (b.at?.[0] ?? 0);
  const tops = groups.filter((g) => outer(g) && g.at![1] < 0).sort(byX), bottoms = groups.filter((g) => outer(g) && g.at![1] >= 0).sort(byX);
  const middle = groups.filter((g) => !outer(g));
  const box = (g: RoomGroup, s: number): Box => {
    const r = cloudRadius(g.points, s), lead = g.title === "" ? 0 : titleHeight(g.titlePx) + TITLE_GAP;
    return { g, r, lead, w: Math.max(2 * r, g.title === "" ? 0 : titleWidth(g.title, g.titlePx)) + GROUP_GAP_X, h: lead + 2 * r + GROUP_GAP_Y };
  };
  const shelves = (gs: readonly RoomGroup[], s: number): Box[][] => {
    const rows: Box[][] = [];
    let row: Box[] = [], used = 0;
    for (const g of gs) {
      const b = box(g, s);
      if (row.length > 0 && used + b.w > W) { rows.push(row); row = []; used = 0; }
      row.push(b); used += b.w;
    }
    if (row.length > 0) rows.push(row);
    return rows;
  };
  const tall = (rows: readonly Box[][]): number => rows.reduce((t, r) => t + Math.max(...r.map((b) => b.h)), 0);
  // the corners on rows of their own, at the largest spacing that fits; when none fits, everything one row after another
  const plans: ((s: number) => Box[][])[] = [
    (s) => [...shelves(tops, s), ...shelves(middle, s), ...shelves(bottoms, s)],
    (s) => shelves([...tops, ...middle, ...bottoms], s),
  ];
  let rows: Box[][] = [], spacing = SPACINGS[SPACINGS.length - 1]!;
  search: for (const plan of plans) for (const s of SPACINGS) {
    const laid = plan(s);
    if (tall(laid) <= H) { rows = laid; spacing = s; break search; }
  }
  if (rows.length === 0) rows = plans[1]!(spacing);

  const places = new Map<string, RoomPlace>();
  const used = tall(rows), free = Math.max(0, H - used) / (rows.length + 1);
  let y = free;
  rows.forEach((row, n) => {
    const h = Math.max(...row.map((b) => b.h)), sum = row.reduce((t, b) => t + b.w, 0);
    const corners = row.every((b) => outer(b.g));
    // his regions to the edges; the blocks spaced evenly; every other one a little lower, within the spare height
    const gap = corners ? (row.length > 1 ? (W - sum) / (row.length - 1) : 0) : (W - sum) / (row.length + 1);
    let x = corners ? (row.length === 1 && (row[0]!.g.at?.[0] ?? 0) > 0 ? W - sum : 0) : gap;
    row.forEach((b, i) => {
      const drop = !corners && i % 2 === 1 && n > 0 ? Math.min(free * 0.35, h - b.h + free * 0.35, 14) : 0;
      places.set(b.g.id, { x: x + b.w / 2, y: y + b.lead + b.r + drop });
      x += b.w + Math.max(0, gap);
    });
    y += h + free;
  });
  return { places, spacing, height: used + free * (rows.length + 1) };
}

/** A group as `fillBox` moves it: its middle, and what it takes round that middle at `unit` pixels a unit (a title stays its size in pixels). */
export interface RoomCloud { x: number; y: number; size(unit: number): { x0: number; x1: number; y0: number; y1: number } }
export interface RoomFill { unit: number; x0: number; x1: number; y0: number; y1: number; /** Stretch along each axis, round the picture's middle. */ fx: number; fy: number }
/** The groups start this much tighter than their boxes allow, so only the push apart decides the gaps (no room left between them); how much of the box the picture fills. */
export const ROOM_DENSITY = 2, ROOM_INNER_DENSITY = 2, ROOM_FILL = 1;
const FILL_ROUNDS = 16, UNIT_MIN = 0.01, UNIT_MAX = 64;

type Bounds = Pick<RoomFill, "x0" | "x1" | "y0" | "y1">;
/** The clouds into a w × h picture round the origin, keeping where they stand relative to each other. */
function spread(all: readonly RoomCloud[], w: number, h: number): void {
  const xs = all.map((c) => c.x), ys = all.map((c) => c.y);
  const rx = Math.max(...xs) - Math.min(...xs) || 1, ry = Math.max(...ys) - Math.min(...ys) || 1;
  const mx = (Math.max(...xs) + Math.min(...xs)) / 2, my = (Math.max(...ys) + Math.min(...ys)) / 2;
  for (const c of all) { c.x = ((c.x - mx) / rx) * w; c.y = ((c.y - my) / ry) * h; }
}
/** webcola's removeOverlaps (VPSC: the least movement, so what was near stays near); the picture's bounds after. */
function separate(all: readonly RoomCloud[], unit: number): Bounds {
  const rects = all.map((c) => { const b = c.size(unit); return new Rectangle(c.x + b.x0, c.x + b.x1, c.y + b.y0, c.y + b.y1); });
  const before = rects.map((q) => ({ x: q.cx(), y: q.cy() }));
  removeOverlaps(rects);
  rects.forEach((q, i) => { all[i]!.x += q.cx() - before[i]!.x; all[i]!.y += q.cy() - before[i]!.y; });
  return { x0: Math.min(...rects.map((q) => q.x)), x1: Math.max(...rects.map((q) => q.X)), y0: Math.min(...rects.map((q) => q.y)), y1: Math.max(...rects.map((q) => q.Y)) };
}
const area = (all: readonly RoomCloud[], unit: number): number => all.reduce((t, c) => { const b = c.size(unit); return t + (b.x1 - b.x0) * (b.y1 - b.y0); }, 0);
/** A goal's blocks as one tight picture round the origin, none touching: each `from` is where it stands by meaning. */
export function packTogether(all: readonly (RoomCloud & { from: [number, number] })[], unit: number, aspect = 1): void {
  // in rows that fill the picture: top to bottom and left to right as they stand by meaning, so one block far from the rest
  // cannot squeeze the others into a column
  const side = Math.sqrt(area(all, unit) / ROOM_INNER_DENSITY), w = side * Math.sqrt(aspect), h = side / Math.sqrt(aspect);
  const n = all.length, cols = Math.max(1, Math.min(n, Math.round(Math.sqrt(n * aspect)))), rows = Math.ceil(n / cols);
  const byY = [...all].sort((a, b) => a.from[1] - b.from[1] || a.from[0] - b.from[0]);
  for (let r = 0; r < rows; r++) {
    const row = byY.slice(r * cols, (r + 1) * cols).sort((a, b) => a.from[0] - b.from[0]);
    row.forEach((c, i) => { c.x = ((i + 0.5) / row.length - 0.5) * w; c.y = ((r + 0.5) / rows - 0.5) * h; });
  }
  separate(all, unit);
}
/** The clouds in rows `width` wide, top to bottom and left to right as they stand, each row's spare width between its clouds. */
function shelves(all: readonly RoomCloud[], unit: number, width: number): Bounds {
  const sized = [...all].sort((a, b) => a.y - b.y || a.x - b.x).map((c) => ({ c, b: c.size(unit) }));
  const rows: (typeof sized)[] = [];
  let row: typeof sized = [], used = 0;
  for (const it of sized) {
    const w = it.b.x1 - it.b.x0;
    if (row.length > 0 && used + w > width) { rows.push(row); row = []; used = 0; }
    row.push(it); used += w;
  }
  if (row.length > 0) rows.push(row);
  let y = 0, x1 = 0;
  for (const r of rows) {
    const sum = r.reduce((t, it) => t + it.b.x1 - it.b.x0, 0), gap = Math.max(0, width - sum) / (r.length + 1), h = Math.max(...r.map((it) => it.b.y1 - it.b.y0));
    let x = gap;
    for (const { c, b } of r) { c.x = x - b.x0; c.y = y - b.y0; x += b.x1 - b.x0 + gap; }
    x1 = Math.max(x1, x);
    y += h;
  }
  return { x0: 0, x1: Math.max(width, x1), y0: 0, y1: y };
}
/**
 * The groups placed in a W × H box (user 2026-11-03: 「没有文字的时候有必要分这么开吗」, then 「留白太多」): tight in the box's
 * shape, pushed just clear of each other, and the whole picture scaled up to fill the box, so the clouds take the room and
 * not the gaps between them. A title keeps its size in pixels, so each round lays the groups out again at the new scale.
 */
export function fillBox(all: readonly RoomCloud[], W: number, H: number): RoomFill {
  const aspect = H / W, from = all.map((c) => [c.x, c.y] as const);
  const place = (u: number): Bounds => {
    all.forEach((c, i) => { c.x = from[i]![0]; c.y = from[i]![1]; });
    const w = Math.sqrt(area(all, u) / ROOM_DENSITY / aspect);
    spread(all, w, w * aspect);
    return separate(all, u);
  };
  // a larger unit takes more pixels (the points scale, a title does not): the largest that fits, by halving, and the
  // placement kept is that unit's own, so no title is squeezed onto another after it was placed
  const fits = (u: number): boolean => { const b = place(u); return (b.x1 - b.x0) * u <= ROOM_FILL * W && (b.y1 - b.y0) * u <= ROOM_FILL * H; };
  let unit = UNIT_MIN, high = UNIT_MAX, fitted = false;
  for (let round = 0; round < FILL_ROUNDS; round++) {
    const mid = Math.sqrt(unit * high);
    if (fits(mid)) { unit = mid; fitted = true; } else high = mid;
  }
  // a narrow box, wide titles: pushed apart sideways they never fit, whatever the unit. Then in rows, top to bottom as they stood
  if (!fitted) {
    const shelved = (u: number): Bounds => { all.forEach((c, i) => { c.x = from[i]![0]; c.y = from[i]![1]; }); return shelves(all, u, W / u); };
    const inBox = (u: number): boolean => { const b = shelved(u); return (b.y1 - b.y0) * u <= H; };
    let low = UNIT_MIN, top = UNIT_MAX;
    for (let round = 0; round < FILL_ROUNDS; round++) {
      const mid = Math.sqrt(low * top);
      if (inBox(mid)) low = mid; else top = mid;
    }
    const U = shelved(low);
    return { unit: low, ...U, fx: Math.max(1, W / low / (U.x1 - U.x0)), fy: Math.max(1, H / low / (U.y1 - U.y0)) };
  }
  const U = place(unit);
  // the axis with room left: the whole picture stretched along it by the caller (points too, so the room is shared and not one gap)
  return { unit, ...U, fx: Math.max(1, W / unit / (U.x1 - U.x0)), fy: Math.max(1, H / unit / (U.y1 - U.y0)) };
}

const seeded = (key: string): (() => number) => {
  let h = 2166136261;
  for (let i = 0; i < key.length; i++) h = Math.imul(h ^ key.charCodeAt(i), 16777619);
  return () => { h = (h + 0x6d2b79f5) | 0; let t = Math.imul(h ^ (h >>> 15), 1 | h); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
};
/** n points with no relation among them, round a cloud's middle: each cloud its own shape, stretched and turned by its key (the same key, the same shape). */
export function cloudShape(key: string, n: number, spacing: number): RoomPlace[] {
  const r = seeded(key), turn = r() * 2 * Math.PI, squash = 0.55 + r() * 0.45, reach = spacing * Math.sqrt(n + 1);
  return Array.from({ length: n }, (_, i) => {
    const a = ((i + (r() - 0.5) * 0.75) / n) * 2 * Math.PI, d = reach * (0.45 + r() * 0.75);
    const x = d * Math.cos(a), y = d * Math.sin(a) * squash;
    return { x: x * Math.cos(turn) - y * Math.sin(turn), y: x * Math.sin(turn) + y * Math.cos(turn) };
  });
}
