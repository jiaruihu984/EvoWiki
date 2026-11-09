import { describe, expect, it } from "vitest";
import { boxesOverlap, clearLabel, fadeAt, FADE_MS, grownAt, GROW_MS, mixColor, placeLabel, pulseRing, PULSE_R, type Box } from "./cloud-anim";

describe("cloud-anim", () => {
  it("a point grows from nothing to full over GROW_MS (eased), at once with reduced motion", () => {
    expect(grownAt(1000, 1000, false)).toBe(0);
    const mid = grownAt(1000, 1000 + GROW_MS / 2, false);
    expect(mid).toBeGreaterThan(0.5); expect(mid).toBeLessThan(1);
    expect(grownAt(1000, 1000 + GROW_MS, false)).toBe(1);
    expect(grownAt(1000, 999, true)).toBe(1);
  });

  it("a band change fades from the old colour to the new one over FADE_MS", () => {
    expect(mixColor("#000000", "#ffffff", 0.5)).toBe("#808080");
    expect(fadeAt("#b8b8b8", "#2f9e5b", 0, 0, false)).toEqual({ color: "#b8b8b8", done: false });
    const mid = fadeAt("#b8b8b8", "#2f9e5b", 0, FADE_MS / 3, false);
    expect(mid.done).toBe(false); expect(mid.color).not.toBe("#b8b8b8"); expect(mid.color).not.toBe("#2f9e5b");
    expect(fadeAt("#b8b8b8", "#2f9e5b", 0, FADE_MS, false)).toEqual({ color: "#2f9e5b", done: true });
    expect(fadeAt("#b8b8b8", "#2f9e5b", 0, 0, true)).toEqual({ color: "#2f9e5b", done: true });
  });

  it("the asking ring breathes; reduced motion keeps it still", () => {
    const a = pulseRing(0, false), b = pulseRing(400, false);
    expect(a.r).not.toBe(b.r);
    expect(pulseRing(0, true)).toEqual(pulseRing(400, true));
    expect(pulseRing(123, true).r).toBe(PULSE_R);
  });

  it("a name goes above its point, else below / right / left, never over another name when there is room", () => {
    const above = placeLabel(100, 100, 6, 40, 16, [], 400, 400);
    expect(above.y1).toBeLessThanOrEqual(100 - 6);
    const taken: Box[] = [above];
    const second = placeLabel(100, 100, 6, 40, 16, taken, 400, 400);
    expect(second.y0).toBeGreaterThanOrEqual(100 + 6);
    // four labels on one point: each place used once, none overlapping
    const boxes: Box[] = [];
    for (let i = 0; i < 4; i++) boxes.push(placeLabel(200, 200, 6, 40, 16, boxes, 400, 400));
    for (let i = 0; i < 4; i++) for (let j = i + 1; j < 4; j++) {
      const a = boxes[i]!, b = boxes[j]!;
      expect(a.x1 <= b.x0 || b.x1 <= a.x0 || a.y1 <= b.y0 || b.y1 <= a.y0).toBe(true);
    }
  });

  it("a name at the top edge goes below rather than out of the box", () => {
    const b = placeLabel(100, 5, 6, 40, 16, [], 400, 400);
    expect(b.y0).toBeGreaterThanOrEqual(0);
  });
});

describe("clearLabel: a point's quiet name goes only where it covers nothing", () => {
  const W = 400, H = 300;
  it("under the dot, centred, when that is free", () => {
    expect(clearLabel(100, 100, 4, 60, 15, [], W, H, 0)).toEqual({ x0: 70, y0: 106, x1: 130, y1: 121, spot: 0 });
  });
  it("above, then right, then left when the others are taken", () => {
    const under: Box = { x0: 99, y0: 108, x1: 101, y1: 120 }, above: Box = { x0: 99, y0: 80, x1: 101, y1: 92 };
    expect(clearLabel(100, 100, 4, 60, 15, [under], W, H, 0)?.spot).toBe(1);
    expect(clearLabel(100, 100, 4, 60, 15, [under, above], W, H, 0)?.spot).toBe(2);
  });
  it("none free, or only outside the box: no place at all", () => {
    const all: Box = { x0: 0, y0: 0, x1: W, y1: H };
    expect(clearLabel(100, 100, 4, 60, 15, [all], W, H, 0)).toBeNull();
    expect(clearLabel(5, 5, 4, 600, 15, [], W, H, 0)).toBeNull();
  });
  it("a name keeps the place it has while that is free, even one a new name would keep clear of (the slack)", () => {
    const near: Box = { x0: 70, y0: 121.5, x1: 130, y1: 130 };  // 0.5 px under the name's place: free, but inside the slack
    expect(clearLabel(100, 100, 4, 60, 15, [near], W, H, 0)?.spot).toBe(0);
    expect(clearLabel(100, 100, 4, 60, 15, [near], W, H, 1)?.spot).toBe(1);
    const has = clearLabel(100, 100, 4, 60, 15, [near], W, H, 2);
    expect(has?.spot).toBe(2);
  });
  it("two names never share a box", () => {
    const taken: Box[] = [];
    for (let i = 0; i < 12; i++) {
      const b = clearLabel(100 + i * 6, 100, 3, 60, 15, taken, W, H, 0);
      if (b !== null) { expect(taken.some((t) => boxesOverlap(t, b))).toBe(false); taken.push(b); }
    }
    expect(taken.length).toBeGreaterThan(1);
  });
});
