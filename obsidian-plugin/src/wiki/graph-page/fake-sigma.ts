/**
 * Tests only: a stand-in for Sigma in jsdom (no WebGL). It keeps the graph, the settings and the event handlers, runs
 * the reducers when asked (what Sigma would draw) and has a camera that takes its states at once. vitest.config.ts
 * aliases "sigma" to this file for every test.
 */
import type Graph from "graphology";
import type { Attributes } from "graphology-types";

type Handler = (e: Record<string, unknown>) => void;
interface CameraState { x: number; y: number; ratio: number; angle: number }

export class FakeCamera {
  x = 0.5; y = 0.5; ratio = 1; angle = 0;
  getState(): CameraState { return { x: this.x, y: this.y, ratio: this.ratio, angle: this.angle }; }
  setState(s: Partial<CameraState>): void { Object.assign(this, s); }
  animate(s: Partial<CameraState>): Promise<void> { Object.assign(this, s); return Promise.resolve(); }
  on(): this { return this; }
}

export class FakeSigma {
  static all: FakeSigma[] = [];
  static get last(): FakeSigma { return FakeSigma.all[FakeSigma.all.length - 1]!; }
  readonly handlers = new Map<string, Handler[]>();
  readonly camera = new FakeCamera();
  refreshes = 0; resizes = 0; killed = false;

  constructor(public graph: Graph, public container: HTMLElement, public settings: Record<string, unknown>) { FakeSigma.all.push(this); }

  on(ev: string, fn: Handler): this { this.handlers.set(ev, [...(this.handlers.get(ev) ?? []), fn]); return this; }
  emit(ev: string, payload: Record<string, unknown> = {}): void { for (const fn of this.handlers.get(ev) ?? []) fn(payload); }
  getCamera(): FakeCamera { return this.camera; }
  setGraph(g: Graph): void { this.graph = g; this.refreshes++; }
  refresh(): this { this.refreshes++; return this; }
  resize(): this { this.resizes++; return this; }
  setCustomBBox(b: unknown): this { this.bbox = b; return this; }
  bbox: unknown = null;
  kill(): void { this.killed = true; }
  /** Like Sigma's: in the framed graph, the graph's box scaled into 0..1 (its larger side) and centred. */
  getNodeDisplayData(id: string): Attributes | undefined {
    if (!this.graph.hasNode(id)) return undefined;
    const xs = this.graph.mapNodes((_, a) => a.x as number), ys = this.graph.mapNodes((_, a) => a.y as number);
    const x0 = Math.min(...xs), y0 = Math.min(...ys), w = Math.max(Math.max(...xs) - x0, Math.max(...ys) - y0) || 1;
    const a = this.node(id);
    return { ...a, x: ((a.x as number) - x0) / w, y: ((a.y as number) - y0) / w };
  }
  framedGraphToViewport(p: { x: number; y: number }): { x: number; y: number } { return { x: p.x, y: p.y }; }
  graphToViewport(p: { x: number; y: number }): { x: number; y: number } { return { x: p.x, y: p.y }; }
  /** What Sigma would draw for this node / edge: its attributes through the reducer. */
  node(id: string): Attributes {
    const r = this.settings.nodeReducer as ((id: string, a: Attributes) => Attributes) | undefined;
    const a = this.graph.getNodeAttributes(id);
    return r ? r(id, a) : a;
  }
  edge(src: string, dst: string): Attributes {
    const e = this.graph.edge(src, dst)!;
    const r = this.settings.edgeReducer as ((e: string, a: Attributes) => Attributes) | undefined;
    const a = this.graph.getEdgeAttributes(e);
    return r ? r(e, a) : a;
  }
}

export default FakeSigma;
