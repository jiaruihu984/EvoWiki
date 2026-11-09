/**
 * The cloud's animations as numbers (pure; sigma-cloud.ts writes them into graphology attributes every frame), after the
 * approved grow demo (full-demo/grow.html, big-goal-demo.html's graph):
 * - grow: a new point scales up from nothing over GROW_MS (cubic ease-out), the goal's blocks one after another
 *   (GROW_STAGGER apart), so they grow into the existing map;
 * - fade: a point whose band changed eases from the colour it showed to its band's over FADE_MS;
 * - pulse: the block being asked has a ring that breathes (radius and opacity, a period of about 1.6 s);
 * - labels: a name placed where it covers no other name or title (above, below, right, left of its point, in that order).
 * Reduced motion: everything at its end at once, the ring still.
 */

export const GROW_MS = 900, GROW_STAGGER = 160, FADE_MS = 700;
/** The asking ring: its radius (px, around the dot) and how fast it breathes (ms per radian). */
export const PULSE_R = 13, PULSE_AMP = 3, PULSE_RATE = 260;

const clamp01 = (x: number): number => Math.min(1, Math.max(0, x));
export const ease = (x: number): number => 1 - Math.pow(1 - clamp01(x), 3);

/** How far a point born at `born` has grown at `t` (0..1); reduced motion: 1. */
export function grownAt(born: number, t: number, reduced: boolean): number {
  return reduced ? 1 : ease((t - born) / GROW_MS);
}

const hexRgb = (h: string): [number, number, number] => {
  let s = h.trim().replace("#", "");
  if (s.length === 3) s = [...s].map((c) => c + c).join("");
  const n = parseInt(s.slice(0, 6), 16);
  return Number.isNaN(n) ? [0, 0, 0] : [(n >> 16) & 255, (n >> 8) & 255, n & 255];
};
const rgbHex = (c: readonly number[]): string => "#" + c.map((v) => Math.round(v).toString(16).padStart(2, "0")).join("");

/** The colour between `from` and `to` at share `p` (0..1). */
export function mixColor(from: string, to: string, p: number): string {
  const a = hexRgb(from), b = hexRgb(to), q = clamp01(p);
  return rgbHex(a.map((v, i) => v + (b[i]! - v) * q));
}

/** A fade begun at `at`: its colour at `t` and whether it has ended. */
export function fadeAt(from: string, to: string, at: number, t: number, reduced: boolean): { color: string; done: boolean } {
  const p = reduced ? 1 : ease((t - at) / FADE_MS);
  return { color: p >= 1 ? to : mixColor(from, to, p), done: p >= 1 };
}

/** The asking ring at `t`: its radius and opacity; reduced motion: still. */
export function pulseRing(t: number, reduced: boolean): { r: number; alpha: number } {
  if (reduced) return { r: PULSE_R, alpha: 0.8 };
  const k = Math.sin(t / PULSE_RATE);
  return { r: PULSE_R + PULSE_AMP * k, alpha: 0.65 + 0.35 * k };
}

export interface Box { x0: number; y0: number; x1: number; y1: number }
const overlap = (a: Box, b: Box): number =>
  Math.max(0, Math.min(a.x1, b.x1) - Math.max(a.x0, b.x0)) * Math.max(0, Math.min(a.y1, b.y1) - Math.max(a.y0, b.y0));

/**
 * Where a name of size w × h goes beside its point at (x, y) with radius r: the first of above, below, right, left that
 * covers nothing in `taken` and stays in the W × H box; when every place covers something, the one covering least.
 * Returns the label's box (top-left origin).
 */
export function placeLabel(x: number, y: number, r: number, w: number, h: number, taken: readonly Box[], W: number, H: number): Box {
  const gap = 4;
  const at = (x0: number, y0: number): Box => ({ x0, y0, x1: x0 + w, y1: y0 + h });
  const options = [
    at(x - w / 2, y - r - gap - h),      // above
    at(x - w / 2, y + r + gap),          // below
    at(x + r + gap, y - h / 2),          // right
    at(x - r - gap - w, y - h / 2),      // left
    // the four corners, and a line further up and down: more places before two names have to touch
    at(x + r + gap, y - r - gap - h), at(x - r - gap - w, y - r - gap - h), at(x + r + gap, y + r + gap), at(x - r - gap - w, y + r + gap),
    at(x - w / 2, y - r - gap - 2 * h), at(x - w / 2, y + r + gap + h),
  ];
  let best = options[0]!, least = Infinity;
  for (const b of options) {
    const out = (b.x0 < 0 ? -b.x0 : 0) + (b.x1 > W ? b.x1 - W : 0) + (b.y0 < 0 ? -b.y0 : 0) + (b.y1 > H ? b.y1 - H : 0);
    const cost = taken.reduce((s, t) => s + overlap(b, t), 0) + out * h;
    if (cost === 0) return b;
    if (cost < least) { least = cost; best = b; }
  }
  return best;
}

export const boxesOverlap = (a: Box, b: Box): boolean => overlap(a, b) > 0;

/** A new place keeps this much more clear than the one a name has, so a floating name does not jump back and forth. */
const SLACK = 2;
/**
 * A point's quiet name (学点新的): under the dot, else above, right, left: the first place clear of `taken` and inside the
 * W × H box, trying the place it had (`was`, an index of those) first; null when none is free.
 */
export function clearLabel(x: number, y: number, r: number, w: number, h: number, taken: readonly Box[], W: number, H: number, was: number): (Box & { spot: number }) | null {
  const gap = 2;
  const spots: Box[] = [
    { x0: x - w / 2, y0: y + r + gap, x1: x + w / 2, y1: y + r + gap + h },
    { x0: x - w / 2, y0: y - r - gap - h, x1: x + w / 2, y1: y - r - gap },
    { x0: x + r + gap, y0: y - h / 2, x1: x + r + gap + w, y1: y + h / 2 },
    { x0: x - r - gap - w, y0: y - h / 2, x1: x - r - gap, y1: y + h / 2 },
  ];
  const free = (b: Box, pad: number): boolean => b.x0 >= 0 && b.y0 >= 0 && b.x1 <= W && b.y1 <= H
    && !taken.some((t) => overlap({ x0: b.x0 - pad, y0: b.y0 - pad, x1: b.x1 + pad, y1: b.y1 + pad }, t) > 0);
  const had = spots[was];
  if (had !== undefined && free(had, 0)) return { ...had, spot: was };
  for (let i = 0; i < spots.length; i++) if (free(spots[i]!, SLACK)) return { ...spots[i]!, spot: i };
  return null;
}
