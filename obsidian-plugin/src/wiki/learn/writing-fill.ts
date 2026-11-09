/**
 * A page card being written: the whole card fills
 * left → right with the pale green (--pg-fill) to its writing progress; on the fill, 45° white stripes scroll, and its
 * right edge is a small flowing wave (a CSS mask). Written / read / mastered: the full card, plain. Only a card being
 * written moves, driven by requestAnimationFrame (a CSS animation did not move for the user, likely reduced motion on
 * their Windows): ONE loop for every writing card, stopped when none is left on the page.
 */
import { el } from "../../ui/ew";
import type { PageState } from "../schema";

/** Stripes scroll this many px a second (one 32 px cycle ≈ 2.7 s); the wave moves down this many. */
export const STRIPE_PX_S = 12;
export const WAVE_PX_S = 9;
const STRIPE_CYCLE = 32, WAVE_TILE = 48;

/** The fill layer of a page card (`li`): sized and styled for its row's state. */
export function fillLayer(state: PageState, pct: number | null): HTMLElement {
  const f = el("span", "fill");
  f.setAttribute("aria-hidden", "true");
  paintFill(f, state, pct);
  return f;
}

/** Sizes the layer: written = full and plain; writing = p% (+ the wave's width) with stripes and the wave; else none. */
export function paintFill(f: HTMLElement, state: PageState, pct: number | null): void {
  const done = state === "written" || state === "read" || state === "mastered";
  const writing = state === "writing";
  f.dataset.state = done ? "done" : writing ? "writing" : "idle";
  f.dataset.indeterminate = String(writing && pct === null);
  f.style.width = done || (writing && pct === null) ? "100%" : writing ? `calc(${Math.max(0, Math.min(100, pct ?? 0))}% + 4px)` : "0";
  if (writing) { live.add(f); start(); } else live.delete(f);
}

const live = new Set<HTMLElement>();
let frame: number | null = null;

function start(): void {
  if (frame !== null || typeof requestAnimationFrame !== "function") return;
  const t0 = performance.now();
  const tick = (now: number): void => {
    const t = (now - t0) / 1000;
    const x = (t * STRIPE_PX_S) % STRIPE_CYCLE, y = (t * WAVE_PX_S) % WAVE_TILE;
    for (const f of [...live]) {
      if (!f.isConnected || f.dataset.state !== "writing") { live.delete(f); continue; }
      f.style.backgroundPosition = `${x}px 0`;
      const mask = `0 0, 100% ${y}px`;
      f.style.setProperty("mask-position", mask);
      f.style.setProperty("-webkit-mask-position", mask);
    }
    frame = live.size > 0 ? requestAnimationFrame(tick) : null;
  };
  frame = requestAnimationFrame(tick);
}
