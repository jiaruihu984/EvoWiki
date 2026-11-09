import { afterEach, expect, it, vi } from "vitest";
import fc from "fast-check";
import { ApiError, OfflineError } from "../../api/errors";
import { setup } from "../controller-test-kit";
import { localBlockEventStore } from "./event-store";
import type { BlockEvent } from "./block-event";

type Model = { accepted: Set<string>; suppressed: Set<string>; offline: boolean; failedStorage: boolean };
const settle = async (): Promise<void> => { for (let n = 0; n < 15; n++) await Promise.resolve(); };
const id = (asset: string, event: BlockEvent): string => `${asset}:${String(event.item)}`;
function rig() {
  const disk = new Map<string, string>();
  const state = { offline: false, failedStorage: false };
  const store = localBlockEventStore({ getItem: (key) => disk.get(key) ?? null,
    setItem: (key, value) => { if (state.failedStorage) throw new Error("disk full"); disk.set(key, value); },
    removeItem: (key) => { if (state.failedStorage) throw new Error("disk full"); disk.delete(key); } }, { server: "server-one", user: "user-one" });
  const requests: (() => void)[] = [];
  const committed = new Set<string>(), suppressed = new Set<string>();
  let app = setup({ blockEventStore: store });
  const bind = (): void => {
    vi.spyOn(app.api, "blockEvents").mockImplementation((asset, events) => new Promise((resolve, reject) => {
      requests.push(() => {
        if (state.offline) { reject(new OfflineError()); return; }
        if (suppressed.has(asset)) { reject(new ApiError(404, "source forgotten")); return; }
        for (const event of events) committed.add(id(asset, event));
        resolve({ inK: false });
      });
    }));
  };
  bind();
  return { get app() { return app; }, state, store, requests, committed, suppressed,
    reload: () => { app.c.dispose(); app = setup({ blockEventStore: store }); bind(); },
    forget: (asset: string) => { suppressed.add(asset); for (const key of committed) if (key.startsWith(`${asset}:`)) committed.delete(key); void app.c.flushBlockEvents(); },
    stop: () => app.c.dispose() };
}
type Real = ReturnType<typeof rig>;
const coverage = new Map<string, number>();
let noTarget = 0;
class Command implements fc.AsyncCommand<Model, Real> {
  constructor(readonly kind: "answer" | "offline" | "online" | "release" | "reload" | "forget" | "disk-full" | "disk-ok", readonly n: number) {}
  check(): boolean { return true; }
  async run(model: Model, real: Real): Promise<void> {
    coverage.set(this.kind, (coverage.get(this.kind) ?? 0) + 1);
    const asset = this.n % 2 === 0 ? "ia_even" : "ia_odd";
    switch (this.kind) {
      case "answer": {
        const event: BlockEvent = { type: "item", node: "quiz", session: "original-session", item: this.n, attempt: 1, revision: this.n + 1, response: 1 };
        const alreadyDurable = real.store.load().some((entry) => id(entry.assetId, entry.event) === id(asset, event));
        const accepted = !model.failedStorage || alreadyDurable;
        expect(real.app.c.blockEvent(asset, event)).toBe(accepted);
        if (accepted) model.accepted.add(id(asset, event));
        break;
      }
      case "offline": model.offline = real.state.offline = true; break;
      case "online": model.offline = real.state.offline = false; void real.app.c.flushBlockEvents(); break;
      case "disk-full": model.failedStorage = real.state.failedStorage = true; break;
      case "disk-ok": model.failedStorage = real.state.failedStorage = false; void real.app.c.flushBlockEvents(); break;
      case "reload": real.reload(); break;
      case "forget": model.suppressed.add(asset); real.forget(asset); break;
      case "release": if (real.requests.length === 0) noTarget++; real.requests.shift()?.(); break;
    }
    await settle();
    for (const key of real.committed) {
      expect(model.accepted.has(key)).toBe(true);
      expect(model.suppressed.has(key.split(":")[0]!)).toBe(false);
    }
  }
  toString(): string { return `${this.kind}(${this.n})`; }
}
afterEach(() => vi.restoreAllMocks());
it("keeps accepted result identities through 200 offline/reload/storage-fault/forget traces", async () => {
  vi.spyOn(console, "warn").mockImplementation(() => undefined);
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  const kinds = ["answer", "offline", "online", "release", "reload", "forget", "disk-full", "disk-ok"] as const;
  await fc.assert(fc.asyncProperty(fc.commands(kinds.map((kind) => fc.integer({ min: 0, max: 5 }).map((n) => new Command(kind, n))), { maxCommands: 50, size: "max" }), async (commands) => {
    const real = rig();
    const model: Model = { accepted: new Set(), suppressed: new Set(), offline: false, failedStorage: false };
    try {
      await fc.asyncModelRun(() => ({ model, real }), commands);
      real.state.offline = real.state.failedStorage = false;
      void real.app.c.flushBlockEvents();
      await settle();
      for (let n = 0; n < 200 && real.requests.length > 0; n++) { real.requests.shift()!(); await settle(); }
      expect(real.requests).toHaveLength(0);
      expect(real.store.load()).toEqual([]);
      expect(real.committed).toEqual(new Set([...model.accepted].filter((key) => !model.suppressed.has(key.split(":")[0]!))));
    } finally { real.stop(); }
  }), { numRuns: 200 });
  for (const kind of kinds) expect(coverage.get(kind)).toBeGreaterThan(0);
  console.info("S7 durable result command coverage", Object.fromEntries(coverage), { noTarget, filtered: 0 });
}, 20_000);
