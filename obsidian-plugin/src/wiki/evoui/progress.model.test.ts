import { afterEach, expect, it, vi } from "vitest";
import fc from "fast-check";
import { ApiError, OfflineError } from "../../api/errors";
import { setup } from "../controller-test-kit";
import { localBlockProgressStore } from "./progress-store";
import type { BlockProgressData } from "./block-event";
const settle = async (): Promise<void> => { for (let n = 0; n < 15; n++) await Promise.resolve(); };
type Model = { counter: number; wanted: Map<string, number>; hidden: Set<string>; failedStorage: boolean };
function rig() {
  const disk = new Map<string, string>();
  const state = { offline: false, failedStorage: false };
  const store = localBlockProgressStore({ getItem: (key) => disk.get(key) ?? null,
    setItem: (key, value) => { if (state.failedStorage) throw new Error("disk full"); disk.set(key, value); },
    removeItem: (key) => { if (state.failedStorage) throw new Error("disk full"); disk.delete(key); } }, { server: "one", user: "one" });
  const requests: (() => void)[] = [], hidden = new Set<string>();
  const durable = new Map<string, BlockProgressData>();
  let app = setup({ blockProgressStore: store });
  const bind = (): void => {
    vi.spyOn(app.api, "saveBlockProgress").mockImplementation((asset, progress) => new Promise((resolve, reject) => {
      requests.push(() => {
        if (state.offline) { reject(new OfflineError()); return; }
        if (hidden.has(asset)) { reject(new ApiError(404, "source forgotten")); return; }
        const stale = progress.revision <= (durable.get(asset)?.revision ?? 0);
        if (!stale) durable.set(asset, progress.data);
        resolve({ stale });
      });
    }));
    vi.spyOn(app.api, "blockProgress").mockImplementation((asset) => hidden.has(asset) ? Promise.reject(new ApiError(404, "source forgotten"))
      : Promise.resolve({ revision: durable.get(asset)?.revision ?? 0, data: durable.get(asset) ?? null }));
  };
  bind();
  return { get app() { return app; }, state, store, requests, durable, hidden,
    reload: () => { app.c.dispose(); app = setup({ blockProgressStore: store }); bind(); },
    forget: (asset: string) => { hidden.add(asset); durable.delete(asset); void app.c.reconcileBlockVisibility(); },
    conflict: (asset: string) => { durable.set(asset, { revision: (durable.get(asset)?.revision ?? 0) + 100 }); },
    stop: () => app.c.dispose() };
}
type Real = ReturnType<typeof rig>;
const coverage = new Map<string, number>();
let noTarget = 0;
class Command implements fc.AsyncCommand<Model, Real> {
  constructor(readonly kind: "report" | "old-report" | "offline" | "online" | "release" | "reload" | "forget" | "conflict" | "disk-full" | "disk-ok", readonly n: number) {}
  check(): boolean { return true; }
  async run(model: Model, real: Real): Promise<void> {
    coverage.set(this.kind, (coverage.get(this.kind) ?? 0) + 1);
    const asset = this.n % 2 === 0 ? "ia_even" : "ia_odd";
    switch (this.kind) {
      case "report": {
        const revision = ++model.counter;
        real.app.c.saveBlockProgress(asset, { revision, v: 2, state: { answer: this.n }, nodes: {} });
        if (!model.failedStorage) model.wanted.set(asset, revision);
        break;
      }
      case "old-report": real.app.c.saveBlockProgress(asset, { revision: Math.max(0, model.counter - 1) }); break;
      case "offline": real.state.offline = true; break;
      case "online": real.state.offline = false; void real.app.c.flushBlockProgress(); break;
      case "disk-full": model.failedStorage = real.state.failedStorage = true; break;
      case "disk-ok": model.failedStorage = real.state.failedStorage = false; void real.app.c.flushBlockProgress(); break;
      case "reload": real.reload(); break;
      case "forget": model.hidden.add(asset); real.forget(asset); break;
      case "conflict": real.conflict(asset); break;
      case "release": if (real.requests.length === 0) noTarget++; real.requests.shift()?.(); break;
    }
    await settle();
  }
  toString(): string { return `${this.kind}(${this.n})`; }
}
afterEach(() => vi.restoreAllMocks());
it("keeps newest reported progress through 200 offline/reload/storage/visibility/conflict traces", async () => {
  vi.spyOn(console, "warn").mockImplementation(() => undefined);
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  const kinds = ["report", "old-report", "offline", "online", "release", "reload", "forget", "conflict", "disk-full", "disk-ok"] as const;
  await fc.assert(fc.asyncProperty(fc.commands(kinds.map((kind) => fc.integer({ min: 0, max: 5 }).map((n) => new Command(kind, n))), { maxCommands: 50, size: "max" }), async (commands) => {
    const real = rig();
    const model: Model = { counter: 0, wanted: new Map(), hidden: new Set(), failedStorage: false };
    try {
      await fc.asyncModelRun(() => ({ model, real }), commands);
      real.state.offline = real.state.failedStorage = false;
      void real.app.c.flushBlockProgress(); await settle();
      for (let n = 0; n < 200 && real.requests.length > 0; n++) { real.requests.shift()!(); await settle(); }
      expect(real.requests).toHaveLength(0);
      expect(real.store.load()).toEqual([]);
      for (const asset of ["ia_even", "ia_odd"]) {
        const restored = await real.app.c.blockProgress(asset);
        if (model.hidden.has(asset)) expect(restored).toBeNull();
        else {
          expect(restored?.revision ?? 0).toBeGreaterThanOrEqual(model.wanted.get(asset) ?? 0);
          expect(restored?.revision ?? 0).toBeLessThanOrEqual(real.durable.get(asset)?.revision ?? 0);
        }
      }
    } finally { real.stop(); }
  }), { numRuns: 200 });
  for (const kind of kinds) expect(coverage.get(kind)).toBeGreaterThan(0);
  console.info("S7 durable progress command coverage", Object.fromEntries(coverage), { noTarget, filtered: 0 });
}, 20_000);
