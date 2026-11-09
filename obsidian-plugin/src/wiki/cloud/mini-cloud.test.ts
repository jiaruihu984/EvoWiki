import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GraphFixture } from "../graph-page/fixture";
import { FakeSigma } from "../graph-page/fake-sigma";
import { buildScene } from "../graph-page/scene";
import type { GraphSource } from "../graph-page/store";
import { MiniCloud } from "./mini-cloud";

let observers: { cb: () => void; disconnect: ReturnType<typeof vi.fn> }[] = [];
beforeEach(() => {
  observers = []; FakeSigma.all = [];
  vi.stubGlobal("requestAnimationFrame", () => 1);
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
  vi.stubGlobal("matchMedia", () => ({ matches: true }));
  vi.stubGlobal("ResizeObserver", class {
    disconnect = vi.fn();
    constructor(public cb: () => void) { observers.push(this); }
    observe(): void {}
  });
});
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); document.body.replaceChildren(); });

const flush = async (): Promise<void> => { for (let i = 0; i < 8; i++) await Promise.resolve(); };

function source(o: { fail?: boolean; offline?: boolean; graph?: () => unknown } = {}) {
  const fx = new GraphFixture();
  const s = { loads: 0, demo: false, isOffline: () => o.offline === true,
    load: () => { s.loads++; return o.fail ? Promise.reject(new Error("x")) : Promise.resolve(o.graph ? o.graph() : fx.graph()); } };
  return s as typeof s & GraphSource;
}
function make(src: GraphSource, extra: { configured?: () => boolean; open?: () => void } = {}) {
  const open = vi.fn(extra.open);
  const css: (() => void)[] = [];
  const m = new MiniCloud({ openStarMap: open, onCssChange: (fn) => { css.push(fn); return () => undefined; },
    goals: { source: () => src, openGoal: vi.fn(), progress: () => null, followStep: vi.fn(), ...(extra.configured ? { configured: extra.configured } : {}) } });
  return { m, open, css };
}

describe("MiniCloud (今天's thumbnail = a live miniature of the 知识图谱 page)", () => {
  it("a .ew-mini (no aria-label: no hover tooltip) and the cloud's box; click → the big map; the data is the page's graph (GET /wiki/graph), read once when shown", async () => {
    const src = source();
    const { m, open, css } = make(src);
    expect(m.root.className).toBe("ew-mini");
    expect(m.root.hasAttribute("aria-label")).toBe(false);   // Obsidian would show it as a hover tooltip
    expect(m.root.firstElementChild).toBe(m.canvas);
    expect(m.canvas.className).toBe("ew-sigma");
    expect(FakeSigma.last.container).toBe(m.canvas);
    expect(src.loads).toBe(0);                       // not read before it is on screen
    m.sync();
    expect(src.loads).toBe(0);
    document.body.append(m.root, m.goals.root);
    m.sync();
    await flush();
    expect(src.loads).toBe(1);
    expect(m.drawn).toEqual(new GraphFixture().graph());     // the same nodes, groups, goals the page draws
    expect(FakeSigma.last.graph.nodes().sort()).toEqual([...buildScene(m.drawn!).order].sort());
    m.sync();
    expect(src.loads).toBe(1);                       // read once
    m.root.click();
    expect(open).toHaveBeenCalledWith(null);          // the thumbnail opens the unfocused map
    expect(css).toHaveLength(1);
    m.destroy();
    expect(observers[0]!.disconnect).toHaveBeenCalled();
    expect(FakeSigma.last.killed).toBe(true);
  });

  it("the box getting a size (placed in 今天, the sidebar resized) resizes Sigma", async () => {
    const { m } = make(source());
    document.body.append(m.root, m.goals.root);
    m.sync();
    await flush();
    const before = FakeSigma.last.resizes;
    observers[0]!.cb();
    expect(FakeSigma.last.resizes).toBe(before + 1);
    m.destroy();
  });

  it("the page's own picture without any text (app.js: \"scaled down, without text\"), no gestures, no handlers", async () => {
    const { m } = make(source());
    document.body.append(m.root, m.goals.root);
    m.sync();
    await flush();
    const sg = FakeSigma.last;
    expect(sg.settings).toMatchObject({ renderLabels: false, enableCameraZooming: false, enableCameraPanning: false });
    expect(sg.handlers.size).toBe(0);                                  // no click, hover or stage handlers
    expect(m.canvas.style.pointerEvents).toBe("none");                 // the box around it takes the click
    m.destroy();
  });

  it("always the whole graph: a new graph puts the camera back on Sigma's fit", async () => {
    const { m } = make(source());                                    // every read is a new graph object
    document.body.append(m.root, m.goals.root);
    m.sync();
    await flush();
    FakeSigma.last.camera.setState({ x: 0.1, ratio: 0.3 });
    await m.goals.graphStore.refresh();
    expect(FakeSigma.last.camera.getState()).toMatchObject({ x: 0.5, y: 0.5, ratio: 1 });
    m.destroy();
  });

  it("follows the graph: a new graph read (the next sync after it is old, or the store refreshed) is what is drawn", async () => {
    let g = new GraphFixture().graph();
    const src = source({ graph: () => g });
    const { m } = make(src);
    document.body.append(m.root, m.goals.root);
    m.sync();
    await flush();
    const first = m.drawn!;
    g = structuredClone(g);
    g.nodes[0]!.name = "改了名字";
    await m.goals.graphStore.refresh();
    await flush();
    expect(m.drawn).not.toBe(first);
    expect(m.drawn!.nodes[0]!.name).toBe("改了名字");
    m.destroy();
  });

  it("a failed read draws nothing; the one 没能读到，重试 line is the goal rows' (no second one here); an unset wiki reads nothing", async () => {
    const src = source({ fail: true });
    const { m } = make(src);
    document.body.append(m.root, m.goals.root, m.goals.line);
    m.sync();
    await flush();
    expect(m.drawn).toBeNull();
    expect(m.goals.line.textContent).toBe("没能读到，重试");
    expect("line" in m).toBe(false);
    const idle = source();
    const unset = make(idle, { configured: () => false });
    document.body.append(unset.m.root);
    unset.m.sync();
    await flush();
    expect(idle.loads).toBe(0);
  });

  it("offline: nothing is drawn and no line is written (the sidebar's one banner says it)", async () => {
    const src = source({ fail: true, offline: true });
    const { m } = make(src);
    document.body.append(m.root, m.goals.root, m.goals.line);
    m.sync();
    await flush();
    expect(m.drawn).toBeNull();
    expect(m.goals.line.textContent).toBe("");
  });

  it("follows a rebuilt source (the wiki controller was replaced)", async () => {
    let src = source();
    const m = new MiniCloud({ openStarMap: vi.fn(), onCssChange: () => () => undefined, goals: { source: () => src, openGoal: vi.fn(), progress: () => null, followStep: vi.fn() } });
    document.body.append(m.root, m.goals.root);
    m.sync();
    await flush();
    const src2 = source();
    src = src2;
    m.sync();
    await flush();
    expect(src2.loads).toBe(1);
    expect(m.drawn).not.toBeNull();
    m.destroy();
  });

  it("shares one graph read with the goal rows", async () => {
    const src = source();
    const { m } = make(src);
    document.body.append(m.root, m.goals.root);
    m.sync(); await flush(); m.sync(); await flush();
    expect(src.loads).toBe(1);
    expect(m.goals.root.querySelectorAll(".ew-gl").length).toBe(new GraphFixture().graph().goals.length);
    m.destroy();
  });
});
