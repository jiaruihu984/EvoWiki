import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/errors";
import type { WriteOut } from "./schema";
import type { FileOut, OrganizeEvent, PageOut, SectionOut } from "./schema";
import { IDLE_MS, POLL_MS, WritesWatcher, type WatcherHost } from "./writes-watcher";

const w = (state: WriteOut["state"], over: Partial<WriteOut> = {}): WriteOut =>
  ({ page_id: "p1", run_id: "r1", kind: "organize", state, pass: null, section: null, of: null, started_at: null, cost_usd: 0, ...over });
const file: FileOut = { page_id: "p1", title: "均值", folder: "统计", parent_name: "", trashed: false, organized: false };
const sec = (id: string, role: SectionOut["role"] = "explain"): SectionOut => ({
  id, heading: `标题${id}`, role, cue: "", body: "", folded: false, fold_locked: false, foldable: true, pinned: false, changed: null,
  why: "", cells: [], lookups: [], note_body: null, artifact_id: null, fingerprint: "", check: null,
});

function rig(o: { events?: OrganizeEvent[]; runError?: Error } = {}) {
  const live: { out: WriteOut[] } = { out: [] };
  const streamed: PageOut[] = [];
  const guards: (() => boolean)[] = [];
  const ended: string[] = [];
  const calls = { writes: 0, files: 0, run: 0 };
  const gates: { files: Promise<void> | null; ended: Promise<void> | null } = { files: null, ended: null };
  const dropped: string[] = [];
  const errors: { run: Error[]; runAfter: Error[]; files: Error[] } = { run: [], runAfter: [], files: [] };
  const state = { active: true, endedOk: true, foreground: new Set<string>(), started: null as ReadonlySet<string> | null, fgDone: new Map<string, boolean>() };
  const host: WatcherHost = {
    api: {
      writes: () => { calls.writes++; return Promise.resolve(live.out); },
      files: async () => {
        calls.files++;
        if (gates.files) await gates.files;
        const err = errors.files.shift();
        if (err) throw err;
        return [file];
      },
      runStream: (_id, _runId, on) => {
        calls.run++;
        const err = errors.run.shift();
        if (err) return Promise.reject(err);
        if (o.runError) return Promise.reject(o.runError);
        for (const e of o.events ?? []) on(e);
        const after = errors.runAfter.shift();                // fails after the events landed (a partly streamed page)
        return after ? Promise.reject(after) : Promise.resolve();
      },
    },
    call: (f) => f(),
    active: () => state.active,
    foreground: (id) => state.foreground.has(id),
    foregroundDone: (id) => state.fgDone.get(id) ?? true,
    streamed: (_id, page, valid) => { streamed.push(page); guards.push(valid); },
    ended: async (id) => { ended.push(id); if (gates.ended) await gates.ended; return state.endedOk; },
    drop: (id) => { dropped.push(id); },
    forgetForeground: (id) => { state.fgDone.delete(id); },
    started: (l) => { state.started = l; },
  };
  const watcher = new WritesWatcher(host);
  return { watcher, host, live, streamed, guards, ended, calls, state, gates, dropped, errors };
}
const tick = (ms: number) => vi.advanceTimersByTimeAsync(ms);
afterEach(() => { vi.useRealTimers(); });

describe("WritesWatcher", () => {
  it("retries restart recovery when the first stale-file sync fails", async () => {
    vi.useFakeTimers();
    const r = rig();
    const recover = vi.fn().mockResolvedValueOnce(false).mockResolvedValue(true);
    r.host.started = recover;
    r.watcher.start();
    await tick(0);
    r.watcher.kick();
    await tick(0);
    expect(recover).toHaveBeenCalledTimes(2);
    r.watcher.kick();
    await tick(0);
    expect(recover).toHaveBeenCalledTimes(2);
    r.watcher.stop();
  });

  it("never revives queued partial writes after done then removal from the live list", async () => {
    vi.useFakeTimers();
    const r = rig({ events: [{ event: "done", data: { page_id: "p1", llm_calls: 0, cost_usd: 0, model: "", fallback: false, prefetched: false } }] });
    r.live.out = [w("writing")];
    r.watcher.start();
    await tick(0);
    expect(r.guards).toHaveLength(1);
    expect(r.guards[0]!()).toBe(false);
    r.live.out = [];
    await tick(POLL_MS);
    expect(r.guards[0]!()).toBe(false);
    r.watcher.stop();
  });

  it("follows a new run on the same page even when the previous run finished between polls", async () => {
    vi.useFakeTimers();
    const r = rig({ events: [{ event: "done", data: { page_id: "p1", llm_calls: 0, cost_usd: 0, model: "", fallback: false, prefetched: false } }] });
    r.live.out = [w("writing", { run_id: "r1" })];
    r.watcher.start();
    await tick(0);
    r.live.out = [w("writing", { run_id: "r2" })];
    await tick(POLL_MS);
    expect(r.calls.run).toBe(2);
    r.watcher.stop();
  });

  it("polls GET /wiki/writes without ids: 10 s while nothing is live, 1.1 s while something is, and stops", async () => {
    vi.useFakeTimers();
    const r = rig();
    r.watcher.start();
    await tick(0);
    expect(r.calls.writes).toBe(1);
    await tick(IDLE_MS - 1);
    expect(r.calls.writes).toBe(1);
    await tick(1);
    expect(r.calls.writes).toBe(2);
    r.live.out = [w("queued")];
    await tick(IDLE_MS);
    const n = r.calls.writes;
    await tick(POLL_MS * 3);
    expect(r.calls.writes).toBe(n + 3);
    r.watcher.stop();
    await tick(IDLE_MS * 2);
    expect(r.calls.writes).toBe(n + 3);
  });

  it("queued has no file; planning creates it at once (and reads the page's file data once)", async () => {
    vi.useFakeTimers();
    const r = rig({ events: [] });
    r.live.out = [w("queued")];
    r.watcher.start();
    await tick(0);
    expect(r.streamed).toHaveLength(0);
    r.live.out = [w("planning")];
    await tick(POLL_MS);
    expect(r.calls.files).toBe(1);
    expect(r.streamed).toHaveLength(1);
    expect(r.streamed[0]).toMatchObject({ page_id: "p1", title: "均值", folder: "统计", sections: [] });
    r.watcher.stop();
  });

  it("follows the run stream and hands over the sections as they land", async () => {
    vi.useFakeTimers();
    const events: OrganizeEvent[] = [
      { event: "planning", data: { pass: 1 } },
      { event: "outline", data: { page_id: "p1", source: "organizer", profile: "", assumed: [], sections: [sec("s1"), sec("s2"), sec("c1", "check")], dropped_checks: [] } },
      { event: "section_delta", data: { section_id: "s1", cue: "提示", body: "正文一", heading: null } },
      { event: "section_done", data: { section_id: "s1", cached: false } },
    ];
    const r = rig({ events });
    r.live.out = [w("writing", { section: 1, of: 2 })];
    r.watcher.start();
    await tick(0);
    await tick(0);
    const last = r.streamed[r.streamed.length - 1]!;
    expect(last.sections.map((x) => x.id)).toEqual(["s1", "c1"]);          // written + check sections, in page order
    expect(last.sections[0]!.body).toBe("正文一");
    expect(r.ended).toEqual([]);
    r.watcher.stop();
  });

  it("not the page the user's own stream is writing", async () => {
    vi.useFakeTimers();
    const r = rig();
    r.state.foreground.add("p1");
    r.live.out = [w("writing")];
    r.watcher.start();
    await tick(POLL_MS * 2);
    expect(r.calls.run).toBe(0);
    expect(r.streamed).toHaveLength(0);
    r.watcher.stop();
  });

  it("a write that leaves the live list is ended once (normal sync); a failed read is tried again next tick", async () => {
    vi.useFakeTimers();
    const r = rig({ events: [] });
    r.live.out = [w("writing")];
    r.watcher.start();
    await tick(0);
    r.live.out = [];
    r.state.endedOk = false;
    await tick(POLL_MS);
    expect(r.ended).toEqual(["p1"]);
    await tick(POLL_MS);
    expect(r.ended).toEqual(["p1", "p1"]);
    r.state.endedOk = true;
    await tick(POLL_MS);
    expect(r.ended).toEqual(["p1", "p1", "p1"]);
    await tick(POLL_MS * 3);
    expect(r.ended).toHaveLength(3);
    r.watcher.stop();
  });

  it("a stream that says done ends the page without waiting for the poll, and is not followed again", async () => {
    vi.useFakeTimers();
    const done: OrganizeEvent = { event: "done", data: { page_id: "p1", llm_calls: 1, cost_usd: 0, model: "m", fallback: false, prefetched: false } };
    const r = rig({ events: [done] });
    r.live.out = [w("writing")];                       // the row still says writing a moment after done
    r.watcher.start();
    await tick(POLL_MS * 3);
    expect(r.ended.length).toBeGreaterThan(0);
    expect(r.calls.run).toBe(1);
    r.watcher.stop();
  });

  it("a 404 from the run stream ends the page too (the run is over)", async () => {
    vi.useFakeTimers();
    const r = rig({ runError: new ApiError(404, "no run") });
    r.live.out = [w("writing")];
    r.watcher.start();
    await tick(POLL_MS * 2);
    expect(r.ended).toContain("p1");
    r.watcher.stop();
  });

  it("keeps synchronizing while the document is hidden and supplies the first live set for stale-file recovery", async () => {
    vi.useFakeTimers();
    const hidden = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    const r = rig();
    r.live.out = [w("queued", { page_id: "p9" })];
    r.watcher.start();
    await tick(0);
    expect(r.calls.writes).toBe(1);
    expect([...r.state.started!]).toEqual(["p9"]);
    expect(r.watcher.snapshot()?.writes.map((x) => x.page_id)).toEqual(["p9"]);
    r.watcher.stop();
    hidden.mockRestore();
  });

  it("subscribers hear every read of GET /wiki/writes (the 知识图谱 rows paint from the same snapshot); a throwing one does not stop the poll", async () => {
    vi.useFakeTimers();
    const r = rig();
    const seen: string[][] = [];
    const off = r.watcher.subscribe(() => { seen.push(r.watcher.snapshot()!.writes.map((x) => x.page_id)); });
    r.watcher.subscribe(() => { throw new Error("boom"); });
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    r.live.out = [w("queued", { page_id: "p9" })];
    r.watcher.start();
    await tick(POLL_MS);
    expect(seen.length).toBeGreaterThan(0);
    expect(seen.every((x) => x.length === 1 && x[0] === "p9")).toBe(true);
    r.live.out = [w("queued", { page_id: "p9" }), w("queued", { page_id: "p8" })];
    await tick(POLL_MS);
    expect(seen[seen.length - 1]).toEqual(["p9", "p8"]);
    off();
    const n = seen.length;
    await tick(POLL_MS * 2);
    expect(seen).toHaveLength(n);
    r.watcher.stop();
  });

  it("offline rule: while the server is unreachable nothing is asked and nothing is said; back online a kick reads at once", async () => {
    vi.useFakeTimers();
    const r = rig();
    r.state.active = false;
    r.watcher.start();
    await tick(IDLE_MS * 2);
    expect(r.calls.writes).toBe(0);
    expect(r.streamed).toHaveLength(0);
    expect(r.ended).toHaveLength(0);
    r.state.active = true;
    r.watcher.kick();
    await tick(0);
    expect(r.calls.writes).toBe(1);
    r.watcher.stop();
  });

  it("kick() reads at once", async () => {
    vi.useFakeTimers();
    const r = rig();
    r.watcher.start();
    await tick(0);
    r.watcher.kick();
    await tick(0);
    expect(r.calls.writes).toBe(2);
    r.watcher.stop();
  });

  const doneEvent: OrganizeEvent = { event: "done", data: { page_id: "p1", llm_calls: 1, cost_usd: 0, model: "m", fallback: false, prefetched: false } };

  it("a write is ended ONCE: the stream's done, then leaving the live list, make one end sync", async () => {
    vi.useFakeTimers();
    const r = rig({ events: [doneEvent] });
    r.live.out = [w("writing")];
    r.watcher.start();
    await tick(POLL_MS * 2);
    expect(r.ended).toEqual(["p1"]);
    r.live.out = [];
    await tick(POLL_MS * 3);
    expect(r.ended).toEqual(["p1"]);
    r.watcher.stop();
  });

  it("no end for a kind the watcher never followed, nor for the user's own write", async () => {
    vi.useFakeTimers();
    const r = rig();
    r.live.out = [w("writing", { kind: "feedback" }), w("writing", { page_id: "p2" })];
    r.state.foreground.add("p2");
    r.watcher.start();
    await tick(POLL_MS * 2);
    r.live.out = [];
    await tick(POLL_MS * 3);
    expect(r.ended).toEqual([]);
    expect(r.calls.run).toBe(0);
    r.watcher.stop();
  });

  it("the user opens a page the Bot is writing: the follow stops, its queued syncs are dropped, and the watcher never writes it again", async () => {
    vi.useFakeTimers();
    const events: OrganizeEvent[] = [
      { event: "outline", data: { page_id: "p1", source: "organizer", profile: "", assumed: [], sections: [sec("s1")], dropped_checks: [] } },
    ];
    const r = rig({ events });
    r.live.out = [w("writing")];
    r.watcher.start();
    await tick(0);
    await tick(0);
    const before = r.streamed.length;
    expect(before).toBeGreaterThan(0);
    r.state.foreground.add("p1");                       // the foreground stream starts on the same page
    await tick(POLL_MS * 3);
    expect(r.dropped).toContain("p1");
    expect(r.streamed).toHaveLength(before);            // nothing more from the watcher (no flip back to writing)
    expect(r.calls.run).toBe(1);                        // not followed again, even after the flag clears
    r.state.foreground.delete("p1");
    await tick(POLL_MS * 3);
    expect(r.calls.run).toBe(1);
    r.live.out = [];                                    // the write is over: the foreground's own final sync stands
    await tick(POLL_MS * 3);
    expect(r.ended).toEqual([]);
    expect(r.streamed).toHaveLength(before);
    r.watcher.stop();
  });

  it("a slow end sync (a busy foreground write) does not stall the poll", async () => {
    vi.useFakeTimers();
    const r = rig({ events: [doneEvent] });
    let open = (): void => undefined;
    r.gates.ended = new Promise<void>((res) => { open = res; });
    r.live.out = [w("writing")];
    r.watcher.start();
    await tick(POLL_MS * 2);
    const n = r.calls.writes;
    await tick(POLL_MS * 3);
    expect(r.calls.writes).toBe(n + 3);
    expect(r.ended).toEqual(["p1"]);                    // one in flight, not started again
    open();
    r.watcher.stop();
  });

  it("a stream that fails (not 404) is followed again without writing the empty skeleton a second time", async () => {
    vi.useFakeTimers();
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const r = rig();
    r.errors.run.push(new Error("net"));
    r.live.out = [w("writing")];
    r.watcher.start();
    await tick(POLL_MS * 3);
    expect(r.calls.run).toBeGreaterThan(1);
    expect(r.streamed).toHaveLength(1);
    r.watcher.stop();
  });

  it("a slow files read cannot start a second follow", async () => {
    vi.useFakeTimers();
    const r = rig();
    let open = (): void => undefined;
    r.gates.files = new Promise<void>((res) => { open = res; });
    r.live.out = [w("planning")];
    r.watcher.start();
    await tick(POLL_MS * 4);
    expect(r.calls.files).toBe(1);
    open();
    await tick(0);
    expect(r.calls.run).toBeLessThanOrEqual(1);
    r.watcher.stop();
  });

  it("the user stops their stream (not done) while the run goes on: the watcher takes the page back and the file ends finished", async () => {
    vi.useFakeTimers();
    const r = rig();
    r.live.out = [w("writing")];
    r.state.foreground.add("p1");
    r.watcher.start();
    await tick(POLL_MS * 2);
    expect(r.calls.run).toBe(0);                        // the foreground has it
    r.state.foreground.delete("p1");
    r.state.fgDone.set("p1", false);                    // 停止: the server's run keeps writing
    await tick(POLL_MS * 2);
    expect(r.calls.run).toBeGreaterThan(0);             // followed again
    expect(r.streamed.length).toBeGreaterThan(0);       // the file shows 正在写 again
    r.live.out = [];
    await tick(POLL_MS * 3);
    expect(r.ended).toEqual(["p1"]);                    // and ends with the finished page
    r.watcher.stop();
  });

  it("a stopped foreground stream whose run already left the live list gets one end sync", async () => {
    vi.useFakeTimers();
    const r = rig();
    r.live.out = [w("writing")];
    r.state.foreground.add("p1");
    r.watcher.start();
    await tick(POLL_MS * 2);
    r.state.foreground.delete("p1");
    r.state.fgDone.set("p1", false);
    r.live.out = [];
    await tick(POLL_MS * 3);
    expect(r.ended).toEqual(["p1"]);
    r.watcher.stop();
  });

  it.each([false, true])("remembers a foreground stream that releases after the server finished (done=%s)", async (done) => {
    vi.useFakeTimers();
    const r = rig();
    r.live.out = [w("writing")];
    r.state.foreground.add("p1");
    r.watcher.start();
    await tick(0);
    r.live.out = [];
    await tick(POLL_MS);
    expect(r.ended).toEqual([]);
    r.state.foreground.delete("p1");
    r.state.fgDone.set("p1", done);
    await tick(POLL_MS * 2);
    expect(r.ended).toEqual(done ? [] : ["p1"]);
    await tick(IDLE_MS * 2);
    expect(r.ended).toEqual(done ? [] : ["p1"]);
    r.watcher.stop();
  });

  it("a finished foreground stream (done) is left alone", async () => {
    vi.useFakeTimers();
    const r = rig();
    r.live.out = [w("writing")];
    r.state.foreground.add("p1");
    r.watcher.start();
    await tick(POLL_MS * 2);
    r.state.foreground.delete("p1");
    r.state.fgDone.set("p1", true);
    await tick(POLL_MS * 3);
    r.live.out = [];
    await tick(POLL_MS * 3);
    expect(r.calls.run).toBe(0);
    expect(r.ended).toEqual([]);
    r.watcher.stop();
  });

  it("a follow that failed early (files read) gets no end sync", async () => {
    vi.useFakeTimers();
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const r = rig();
    r.gates.files = Promise.reject(new Error("net"));
    r.gates.files.catch(() => undefined);
    r.live.out = [w("writing")];
    r.watcher.start();
    await tick(POLL_MS * 2);
    r.live.out = [];
    await tick(POLL_MS * 3);
    expect(r.ended).toEqual([]);
    r.watcher.stop();
  });
  it("a partly streamed page whose re-follow fails early still gets its end sync (exactly one)", async () => {
    vi.useFakeTimers();
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const events: OrganizeEvent[] = [
      { event: "outline", data: { page_id: "p1", source: "organizer", profile: "", assumed: [], sections: [sec("s1")], dropped_checks: [] } },
      { event: "section_delta", data: { section_id: "s1", cue: "", body: "半截", heading: null } },
      { event: "section_done", data: { section_id: "s1", cached: false } },
    ];
    const r = rig({ events });
    r.errors.runAfter.push(new Error("net"));            // streams part of the page, then the stream fails (not 404)
    r.live.out = [w("writing")];
    r.watcher.start();
    await tick(0);
    expect(r.streamed.length).toBeGreaterThan(1);       // the file holds part of the page, status writing
    r.errors.files.push(new Error("net"));               // the re-follow fails at its files read
    await tick(POLL_MS);
    expect(r.calls.files).toBe(2);
    r.live.out = [];                                     // the run is over
    await tick(POLL_MS * 3);
    expect(r.ended).toEqual(["p1"]);                     // one normal sync: the status is cleared
    r.watcher.stop();
  });

  it("a stale follow aborted while reading the files does not clear a newer follow's end sync", async () => {
    vi.useFakeTimers();
    const r = rig({ events: [] });
    let open = (): void => undefined;
    r.gates.files = new Promise<void>((res) => { open = res; });
    r.live.out = [w("writing")];
    r.watcher.start();
    await tick(0);                                       // follow A waits on its files read
    r.live.out = [];
    await tick(POLL_MS);                                 // A's write leaves the live list: A is aborted (and ended)
    expect(r.ended).toEqual(["p1"]);
    r.live.out = [w("writing")];
    await tick(POLL_MS);                                 // a new write: follow B waits on its files read too
    expect(r.calls.files).toBe(2);
    open();
    await tick(0);                                       // A returns (aborted), B streams
    r.live.out = [];
    await tick(POLL_MS * 3);
    expect(r.ended).toEqual(["p1", "p1"]);               // B's write is ended too
    r.watcher.stop();
  });

  it("forgets how a foreground stream ended once it is used, or when the page leaves the live list", async () => {
    vi.useFakeTimers();
    const r = rig();
    r.live.out = [w("writing"), w("writing", { page_id: "p2" })];
    r.state.foreground.add("p1");
    r.state.foreground.add("p2");
    r.watcher.start();
    await tick(POLL_MS);
    r.state.foreground.clear();
    r.state.fgDone.set("p1", false);                    // 停止: taken back, so the result is used up
    r.state.fgDone.set("p2", true);
    await tick(POLL_MS);
    expect(r.state.fgDone.has("p1")).toBe(false);
    expect(r.state.fgDone.has("p2")).toBe(true);
    r.live.out = [w("writing")];                        // p2's write is over
    await tick(POLL_MS);
    expect(r.state.fgDone.has("p2")).toBe(false);
    r.watcher.stop();
  });
});
