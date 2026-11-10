import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { draftMap } from "../learn/learn-flow";
import { goalCloud, goalScene, regionState } from "../learn/goal-cloud";
import type { GraphCloudHandle } from "./sigma-cloud";

const handles: GraphCloudHandle[] = [];
beforeEach(() => {
  vi.stubGlobal("requestAnimationFrame", () => 1);
  vi.stubGlobal("cancelAnimationFrame", () => undefined);
  vi.stubGlobal("ResizeObserver", class { observe(): void {} disconnect(): void {} });
  vi.stubGlobal("matchMedia", () => ({ matches: true }));
});
afterEach(() => {
  handles.splice(0).forEach((h) => h.destroy());
  document.body.replaceChildren(); vi.restoreAllMocks(); vi.unstubAllGlobals();
});

describe("the shared original learning chart", () => {
  it("uses identical geometry and opacity for mastery and domain backgrounds, changing only their colours", () => {
    // Compare both animated glows at the same instant of their breathing cycle.
    vi.spyOn(performance, "now").mockReturnValue(1000);
    const gradients: { shape: number[]; stops: [number, string][] }[] = [];
    const context = { measureText: (s: string) => ({ width: s.length * 8 }), setTransform: () => undefined, clearRect: () => undefined,
      beginPath: () => undefined, arc: () => undefined, fill: () => undefined,
      createRadialGradient: (...shape: number[]) => {
        const g = { shape, stops: [] as [number, string][] }; gradients.push(g);
        return { addColorStop: (at: number, color: string) => { g.stops.push([at, color]); } };
      } };
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(() => context as unknown as CanvasRenderingContext2D);
    const map = draftMap([{ key: "a", name: "A", scope: "" }, { key: "b", name: "B", scope: "" }]);
    map.nodes[0]!.mix = { gap: 1 }; map.nodes[1]!.mix = { known: 1 };
    const scene = goalScene(null, map, { places: { a: [0, 0], b: [1, 1] }, links: [] }, "Goal", null);
    const draw = (background: "mastery" | "domain") => {
      const root = document.createElement("div");
      for (const [key, value] of Object.entries({ known: "#2f9e5b", likely: "#6cbf84", shaky: "#d99a1e", gap: "#d9483b", none: "#b8b8b8", "tx-1": "#222222", "tx-2": "#5c5c5c", "tx-3": "#8a8a8a", edge: "#d2d2d2", "cloud-bg": "#fafafa" })) root.style.setProperty(`--${key}`, value);
      const box = document.createElement("div"); root.append(box); document.body.append(root);
      Object.defineProperties(box, { clientWidth: { value: 800 }, clientHeight: { value: 600 } });
      const h = goalCloud(box, { root, scene: () => scene, background, enter: () => true, canEnter: () => true, hover: () => undefined });
      handles.push(h); h.setScene(scene.scene); h.frame([...scene.goalDots], "top");
      const at = gradients.length;
      (h.sigma as unknown as { emit(name: string): void }).emit("afterRender");
      return { h, gradients: gradients.slice(at) };
    };
    const first = draw("mastery"), second = draw("domain");
    expect(first.gradients.length).toBeGreaterThan(0);
    expect(first.gradients.map((g) => g.shape)).toEqual(second.gradients.map((g) => g.shape));
    expect(first.gradients.map((g) => g.stops.map(([at, color]) => [at, color.slice(color.lastIndexOf(","))]))).toEqual(second.gradients.map((g) => g.stops.map(([at, color]) => [at, color.slice(color.lastIndexOf(","))])));
    expect(first.gradients.map((g) => g.stops)).not.toEqual(second.gradients.map((g) => g.stops));
    expect(first.h.sigma.getCamera().getState()).toEqual(second.h.sigma.getCamera().getState());
    expect(first.h.graph().export()).toEqual(second.h.graph().export());
  });

  it("keeps EvoWiki goal growth and drift enabled when Windows requests reduced motion", () => {
    expect(matchMedia("(prefers-reduced-motion: reduce)").matches).toBe(true);
    let frame: FrameRequestCallback = () => undefined;
    vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => { frame = cb; return 1; });
    let now = 1000; vi.spyOn(performance, "now").mockImplementation(() => now);
    const root = document.createElement("div"), box = document.createElement("div");
    root.append(box); document.body.append(root);
    Object.defineProperties(box, { offsetWidth: { value: 800 }, clientWidth: { value: 800 }, clientHeight: { value: 600 } });
    const map = draftMap([{ key: "a", name: "A", scope: "" }, { key: "b", name: "B", scope: "" }]);
    const scene = goalScene(null, map, null, "Goal", null);
    const h = goalCloud(box, { root, scene: () => scene, background: "mastery", enter: () => true, canEnter: () => true, hover: () => undefined });
    handles.push(h); h.setScene(scene.scene);
    expect(h.graph().getNodeAttribute("a", "grow")).toBe(0);
    const x = h.graph().getNodeAttribute("a", "x");
    now = 1300; frame(now);
    expect(h.graph().getNodeAttribute("a", "grow")).toBeGreaterThan(0);
    expect(h.graph().getNodeAttribute("a", "grow")).toBeLessThan(1);
    expect(h.graph().getNodeAttribute("a", "x")).not.toBe(x);
  });

  it("uses the first view's self-assessment rules instead of inventing another mastery scale", () => {
    const r = draftMap([{ key: "a", name: "A", scope: "" }]).nodes[0]!;
    expect(regionState(r, { asking: null, marks: { a: "know" }, open: true })).toBe("known");
    expect(regionState(r, { asking: null, marks: { a: "know" }, open: false })).toBe("shaky");
    expect(regionState({ ...r, state: "known" }, null)).toBe("known");
  });
});
