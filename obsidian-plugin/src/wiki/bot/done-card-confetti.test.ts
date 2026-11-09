// The real canvas-confetti (not mocked) on a fake 2D context: it paints on the card's own canvas, at the 60 fps pace
// whatever the refresh rate, and still under prefers-reduced-motion (user ruling).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { confetti, CONFETTI_DELAY_MS } from "./done-card";

const rect = (r: Partial<DOMRect>): DOMRect => ({ left: 0, top: 0, width: 0, height: 0, right: 0, bottom: 0, x: 0, y: 0, toJSON: () => ({}), ...r });

function ctxStub() {
  const calls = { fill: 0, clearRect: 0 };
  const ctx = {
    clearRect: () => { calls.clearRect++; }, beginPath: () => undefined, moveTo: () => undefined, lineTo: () => undefined, closePath: () => undefined,
    fill: () => { calls.fill++; }, save: () => undefined, restore: () => undefined, translate: () => undefined, rotate: () => undefined,
    scale: () => undefined, arc: () => undefined, ellipse: () => undefined, fillStyle: "",
  };
  return { ctx, calls };
}

let frameTime = 0;
let pending: FrameRequestCallback[] = [];
/** Runs every queued animation frame once, `step` ms after the previous batch. */
const frame = (step: number): void => { frameTime += step; const run = pending; pending = []; run.forEach((f) => f(frameTime)); };

beforeEach(() => {
  vi.useFakeTimers();
  pending = []; // frameTime keeps growing across tests: the library remembers its last frame time
  vi.stubGlobal("requestAnimationFrame", (f: FrameRequestCallback) => pending.push(f));
  vi.stubGlobal("cancelAnimationFrame", () => undefined);
});
afterEach(() => { document.body.replaceChildren(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); });

function card(): { cv: HTMLCanvasElement; ring: HTMLElement; calls: { fill: number; clearRect: number } } {
  const cv = document.createElement("canvas");
  const ring = document.createElement("div");
  document.body.append(cv, ring);
  vi.spyOn(cv, "getBoundingClientRect").mockReturnValue(rect({ width: 300, height: 160 }));
  vi.spyOn(ring, "getBoundingClientRect").mockReturnValue(rect({ left: 100, top: 20, width: 80, height: 80 }));
  const { ctx, calls } = ctxStub();
  vi.spyOn(cv, "getContext").mockReturnValue(ctx as never);
  return { cv, ring, calls };
}

describe("done-card confetti with the real canvas-confetti", () => {
  it("paints 36 pieces on the card's canvas, sized to it, one step per 60 fps tick however fast the screen refreshes", () => {
    vi.stubGlobal("matchMedia", () => ({ matches: false }));
    const { cv, ring, calls } = card();
    confetti(cv, ring);
    vi.advanceTimersByTime(CONFETTI_DELAY_MS);
    expect([cv.width, cv.height]).toEqual([300, 160]);
    frame(16.7);
    expect(calls.fill).toBe(36);
    // a 144 Hz screen calls back every 7 ms: the library skips frames until a 60 fps tick has passed
    const before = calls.fill;
    frame(7); frame(7);
    expect(calls.fill).toBe(before);
    frame(7);
    expect(calls.fill).toBe(before + 36);
  });

  it("still paints under prefers-reduced-motion (celebrations ignore it, user ruling)", () => {
    vi.stubGlobal("matchMedia", (q: string) => ({ matches: q.includes("reduced-motion") }));
    const { cv, ring, calls } = card();
    confetti(cv, ring);
    vi.advanceTimersByTime(CONFETTI_DELAY_MS);
    frame(16.7);
    expect(calls.fill).toBe(36);
  });
});
