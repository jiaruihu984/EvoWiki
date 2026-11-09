import { afterEach, expect, it, vi } from "vitest";
import fc from "fast-check";
import { POLL_MS, WritesWatcher, type WatcherHost } from "./writes-watcher";
import type { OrganizeEvent, WriteOut } from "./schema";

type Model = { run: string | null; epoch: number; foreground: boolean; finished: boolean };
type Stream = { run: string; on: (e: OrganizeEvent) => void; resolve: () => void };
function rig() {
  const state = { server: null as string | null, epoch: 0, foreground: false, sequence: 0 };
  const streams: Stream[] = [];
  const queued: { run: string; epoch: number; valid: () => boolean }[] = [];
  const host: WatcherHost = {
    api: {
      writes: (): Promise<WriteOut[]> => Promise.resolve(state.server === null ? [] : [{ page_id: "p", run_id: state.server, kind: "organize", state: "writing", pass: null, section: null, of: null, started_at: null, cost_usd: 0 }]),
      files: () => Promise.resolve([{ page_id: "p", title: state.server!, folder: "", parent_name: "", trashed: false, organized: false }]),
      runStream: (_page, run, on) => new Promise<void>((resolve) => { streams.push({ run, on, resolve }); }),
    },
    call: (fn) => fn(), active: () => true, foreground: () => state.foreground,
    foregroundDone: () => false,
    streamed: (_id, page, valid) => { queued.push({ run: page.title, epoch: state.epoch, valid }); },
    ended: () => Promise.resolve(true),
  };
  const watcher = new WritesWatcher(host);
  return { state, streams, queued, watcher };
}
type Real = ReturnType<typeof rig>;
const coverage = new Map<string, number>();
class Command implements fc.AsyncCommand<Model, Real> {
  constructor(readonly kind: "new" | "poll" | "partial" | "finish" | "flush" | "restart" | "foreground" | "background" | "remove") {}
  check(): boolean { return true; }
  async run(model: Model, real: Real): Promise<void> {
    coverage.set(this.kind, (coverage.get(this.kind) ?? 0) + 1);
    const poll = async (): Promise<void> => {
      real.watcher.kick();
      await vi.advanceTimersByTimeAsync(0);
      if (model.run !== real.state.server) model.finished = false;
      model.run = real.state.server;
    };
    switch (this.kind) {
      case "new": real.state.server = `r${++real.state.sequence}`; await poll(); break;
      case "poll": await poll(); break;
      case "remove": real.state.server = null; await poll(); break;
      case "foreground": real.state.foreground = model.foreground = true; await poll(); break;
      case "background": real.state.foreground = model.foreground = false; await poll(); break;
      case "restart":
        real.watcher.stop();
        real.state.epoch = ++model.epoch;
        real.watcher.start();
        await vi.advanceTimersByTimeAsync(0);
        model.run = real.state.server;
        model.finished = false;
        break;
      case "partial":
        // Includes deliberately late callbacks from aborted streams; their transport need not cooperate.
        for (const stream of real.streams) stream.on({ event: "outline", data: { page_id: "p", source: "organizer", profile: "", assumed: [], sections: [], dropped_checks: [] } });
        break;
      case "finish":
        for (const stream of real.streams) {
          stream.on({ event: "done", data: { page_id: "p", llm_calls: 0, cost_usd: 0, model: "", fallback: false, prefetched: false } });
          stream.resolve();
        }
        await vi.advanceTimersByTimeAsync(0);
        if (!model.foreground && real.streams.some((s) => s.run === model.run)) model.finished = true;
        break;
      case "flush":
        for (const job of real.queued.splice(0)) {
          if (!job.valid()) continue;
          expect(job.run).toBe(model.run);
          expect(job.epoch).toBe(model.epoch);
          expect(model.foreground).toBe(false);
          expect(model.finished).toBe(false);
        }
        break;
    }
  }
  toString(): string { return this.kind; }
}
afterEach(() => vi.useRealTimers());
it("keeps page/run ownership across 200 generated command traces, including delayed callbacks and queued commits", async () => {
  vi.useFakeTimers();
  const kinds = ["new", "poll", "partial", "finish", "flush", "restart", "foreground", "background", "remove"] as const;
  await fc.assert(fc.asyncProperty(fc.commands(kinds.map((kind) => fc.constant(new Command(kind))), { maxCommands: 50, size: "max" }), async (commands) => {
    const real = rig();
    real.watcher.start();
    await vi.advanceTimersByTimeAsync(0);
    try {
      await fc.asyncModelRun(() => ({ model: { run: null, epoch: 0, foreground: false, finished: false }, real }), commands);
      await new Command("flush").run({ run: real.state.server, epoch: real.state.epoch, foreground: real.state.foreground, finished: false }, real);
    } finally { real.watcher.stop(); vi.clearAllTimers(); }
  }), { numRuns: 200 });
  for (const kind of kinds) expect(coverage.get(kind)).toBeGreaterThan(0);
  console.info("S1 ownership command coverage (all commands admitted):", Object.fromEntries(coverage), "poll interval", POLL_MS);
});
