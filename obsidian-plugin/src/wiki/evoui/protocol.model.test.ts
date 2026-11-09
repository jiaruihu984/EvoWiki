import { afterEach, expect, it, vi } from "vitest";
import fc from "fast-check";
import { OfflineError } from "../../api/errors";
import { setup } from "../controller-test-kit";
import { openBlockFrame, useTransportForTests, type BlockFrame } from "./frame-host";
import type { BlockEvent, BlockProgressData } from "./block-event";
import type { FromFrame, ToFrame } from "./protocol";

const settle = async (): Promise<void> => { for (let n = 0; n < 15; n++) await Promise.resolve(); };
const key = (event: BlockEvent): string => `${String(event.session)}:${String(event.item)}`;
type Frame = { receive: (message: FromFrame) => void; posts: ToFrame[]; handle: BlockFrame; initialized: boolean; alive: boolean; session: string };
function rig() {
  const app = setup();
  const requests: (() => void)[] = [];
  const durableEvents = new Set<string>();
  let revision = 0;
  let durable: BlockProgressData | null = null;
  let offline = false;
  vi.spyOn(app.api, "blockEvents").mockImplementation((_asset, events) => new Promise((resolve, reject) => {
    requests.push(() => { if (offline) { reject(new OfflineError()); return; } for (const event of events) durableEvents.add(key(event)); resolve({ inK: false }); });
  }));
  vi.spyOn(app.api, "saveBlockProgress").mockImplementation((_asset, progress) => new Promise((resolve, reject) => {
    requests.push(() => {
      if (offline) { reject(new OfflineError()); return; }
      const stale = progress.revision <= revision;
      if (!stale) { revision = progress.revision; durable = progress.data; }
      resolve({ stale });
    });
  }));
  vi.spyOn(app.api, "blockProgress").mockImplementation(() => Promise.resolve({ revision, data: durable }));
  const frames: Frame[] = [];
  const replace = (): void => {
    const previous = frames[frames.length - 1];
    if (previous) { previous.alive = false; void previous.handle.stop(); }
    const posts: ToFrame[] = [];
    let receive!: Frame["receive"];
    useTransportForTests((_host, on) => { receive = on; return { start: () => undefined, post: (m) => { posts.push(m); if (m.t === "stop") receive({ t: "stopped", progress: null }); }, remove: () => undefined, connected: () => true, setHeight: () => undefined }; });
    const host = document.createElement("div");
    const handle = openBlockFrame({ host, root: host, source: "model", block: {}, blockId: "asset", progress: durable,
      onStart: () => undefined, onFallback: () => undefined,
      onEvent: (ev) => app.c.blockEvent("asset", ev), onProgress: (p) => app.c.saveBlockProgress("asset", p) });
    frames.push({ handle, receive, posts, initialized: false, alive: true, session: `session${frames.length}` });
  };
  return { app, requests, durableEvents, frames, replace,
    offline: (value: boolean) => { offline = value; },
    conflict: () => { revision += 100; durable = { revision, v: 2, state: {}, nodes: {} }; },
    revision: () => revision,
    dispose: () => { for (const frame of frames) void frame.handle.stop(); app.c.dispose(); },
  };
}
type Real = ReturnType<typeof rig>;
type Model = { expectedEvents: Set<string>; highestUserRevision: number; latestDeliveredRevision: number };
const coverage = new Map<string, number>();
let noTarget = 0;
class Command implements fc.AsyncCommand<Model, Real> {
  constructor(readonly kind: "replace" | "up" | "event" | "late" | "progress" | "old-progress" | "release" | "offline" | "online" | "conflict" | "done" | "destroy", readonly n: number) {}
  check(): boolean { return true; }
  async run(model: Model, real: Real): Promise<void> {
    coverage.set(this.kind, (coverage.get(this.kind) ?? 0) + 1);
    const active = real.frames[real.frames.length - 1];
    if ((["up", "event", "late", "progress", "old-progress", "done", "destroy"].includes(this.kind) && active === undefined)
      || (this.kind === "release" && real.requests.length === 0)) noTarget++;
    switch (this.kind) {
      case "replace": real.replace(); break;
      case "up":
        if (active) { active.receive({ t: "up", boot: active.session }); if (active.alive) active.initialized = true; }
        break;
      case "event":
      case "late": {
        const frame = this.kind === "late" ? real.frames[0] : active;
        if (!frame) break;
        const event: BlockEvent = { type: "item", node: "quiz", session: frame.session, item: this.n, attempt: 1, revision: this.n + 1 };
        frame.receive({ t: "event", ev: event });
        if (frame.alive && frame.initialized) model.expectedEvents.add(key(event));
        break;
      }
      case "progress":
      case "old-progress": {
        const next = this.kind === "progress" ? ++model.highestUserRevision : Math.max(0, model.highestUserRevision - 1);
        active?.receive({ t: "progress", progress: { revision: next, v: 2, state: {}, nodes: {} } });
        if (active?.alive && active.initialized) model.latestDeliveredRevision = Math.max(model.latestDeliveredRevision, next);
        break;
      }
      case "release": real.requests.shift()?.(); break;
      case "offline": real.offline(true); break;
      case "online": real.offline(false); void real.app.c.flushBlockEvents(); void real.app.c.flushBlockProgress(); break;
      case "conflict": real.conflict(); break;
      case "done": {
        if (!active) break;
        const event: BlockEvent = { type: "summary", node: "quiz", session: active.session, revision: ++model.highestUserRevision, attempt: 1, total: 1, right: 1, missed: [] };
        active.receive({ t: "event", ev: event });
        active.receive({ t: "progress", progress: { revision: model.highestUserRevision, v: 2, state: {}, nodes: { quiz: { done: true } } } });
        if (active.alive && active.initialized) model.expectedEvents.add(key(event));
        if (active.alive && active.initialized) model.latestDeliveredRevision = Math.max(model.latestDeliveredRevision, model.highestUserRevision);
        active.alive = false;
        void active.handle.stop();
        break;
      }
      case "destroy":
        if (active) { active.alive = false; void active.handle.stop(); }
        break;
    }
    await settle();
    for (const frame of real.frames) expect(frame.posts.filter((m) => m.t === "init").length).toBeLessThanOrEqual(1);
    for (const event of real.durableEvents) expect(model.expectedEvents.has(event), `unexpected committed event ${event}`).toBe(true);
  }
  toString(): string { return `${this.kind}(${this.n})`; }
}
afterEach(() => { useTransportForTests(null); vi.restoreAllMocks(); });
it("explores 200 frame/outbox/progress traces with controlled network completion", async () => {
  vi.spyOn(console, "warn").mockImplementation(() => undefined);
  const kinds = ["replace", "up", "event", "late", "progress", "old-progress", "release", "offline", "online", "conflict", "done", "destroy"] as const;
  await fc.assert(fc.asyncProperty(fc.commands(kinds.map((kind) => fc.integer({ min: 0, max: 5 }).map((n) => new Command(kind, n))), { maxCommands: 50, size: "max" }), async (commands) => {
    const real = rig();
    const model: Model = { expectedEvents: new Set(), highestUserRevision: 0, latestDeliveredRevision: 0 };
    try {
      await fc.asyncModelRun(() => ({ model, real }), commands);
      real.offline(false);
      void real.app.c.flushBlockEvents(); void real.app.c.flushBlockProgress();
      for (let n = 0; n < 100 && real.requests.length > 0; n++) { real.requests.shift()!(); await settle(); }
      expect(real.requests).toHaveLength(0);
      expect(real.durableEvents).toEqual(model.expectedEvents);
      const saved = await real.app.c.blockProgress("asset");
      // A remote change alone does not invalidate a session cache; every resumed revision must be durable after draining.
      expect(saved?.revision ?? 0).toBeLessThanOrEqual(real.revision());
      expect(saved?.revision ?? 0).toBeGreaterThanOrEqual(model.latestDeliveredRevision);
    } finally { real.dispose(); }
  }), { numRuns: 200 });
  for (const kind of kinds) expect(coverage.get(kind)).toBeGreaterThan(0);
  console.info("S7 admitted command coverage; no precondition filtering", Object.fromEntries(coverage), { noTarget, filtered: 0 });
}, 20_000);
