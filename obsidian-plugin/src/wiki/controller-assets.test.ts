/** Task 8 (appendix A·6): after a page's note sync, the interactives in its section bodies are fetched into the vault. */
import { describe, expect, it, vi } from "vitest";
import { ApiError, OfflineError } from "../api/errors";
import { firstPage, flush, setup, until } from "./controller-test-kit";

const recorder = () => ({ retrieveAll: vi.fn((_ids: readonly string[]) => Promise.resolve()) });

describe("asset fetching after note sync", () => {
  for (const result of ["created", "updated", "unchanged"] as const) {
    it(`sync ${result} → the page's assets/<id>.html ids are fetched (the demo interactive of p1 s4)`, async () => {
      const assets = recorder();
      const s = setup({ assets });
      s.sync.result = result;
      await firstPage(s.c);
      await flush();
      expect(s.sync.runs.length).toBeGreaterThan(0);
      expect(assets.retrieveAll).toHaveBeenCalled();
      expect(assets.retrieveAll.mock.calls[0]![0]).toEqual(["demo-z-steps"]);
    });
  }

  it("user_edited, backup_failed or a failed sync → nothing fetched", async () => {
    for (const result of ["user_edited", "backup_failed"] as const) {
      const assets = recorder();
      const s = setup({ assets });
      s.sync.result = result;
      await firstPage(s.c);
      await flush();
      expect(assets.retrieveAll).not.toHaveBeenCalled();
    }
    const assets = recorder();
    const s = setup({ assets });
    s.sync.throwNext = true;
    await firstPage(s.c);
    await flush();
    expect(assets.retrieveAll).not.toHaveBeenCalled();
  });

  it("offline or unconfigured → nothing fetched; a failing fetch is only logged", async () => {
    // The connection drops (or the settings are cleared) while the note is being written: checked after the sync.
    for (const cut of ["offline", "unconfigured"] as const) {
      const rec = recorder();
      let configured = true;
      const s = setup({ assets: rec });
      Object.assign(s.c, { deps: { ...(s.c as unknown as { deps: object }).deps, configured: () => configured } });
      s.sync.hold = true;
      const done = firstPage(s.c);
      await until(() => s.sync.pending > 0);
      if (cut === "offline") s.conn.state = "offline"; else configured = false;
      s.sync.release();
      await done.catch(() => undefined);
      await flush();
      expect(s.sync.runs.length, cut).toBeGreaterThan(0);
      expect(rec.retrieveAll, cut).not.toHaveBeenCalled();
    }

    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const bad = { retrieveAll: vi.fn(() => Promise.reject(new Error("disk"))) };
    const b = setup({ assets: bad });
    await firstPage(b.c);
    await flush();
    expect(bad.retrieveAll).toHaveBeenCalled();
    expect(warn).toHaveBeenCalled();
    warn.mockRestore();
  });
});

describe("evoui block result events", () => {
  const item = (i: number, rev: number, attempt = 1) => ({ type: "item" as const, block: "ia_1", session: "0123456789abcdef", node: "quiz#0",
    kind: "quiz", item: i, attempt, revision: rev, response: 1, correct: true, hintUsed: false, latencyMs: 500 });
  const summary = { type: "summary" as const, block: "ia_1", session: "0123456789abcdef", node: "quiz#0", kind: "quiz", attempt: 1, revision: 5,
    total: 2, right: 2, missed: [], timeMs: 900, hintsUsed: 0 };
  const sent = (s: ReturnType<typeof setup>): unknown[] => s.api.calls.filter((x) => x.method === "blockEvents").map((x) => x.args);

  it("each event is posted as is under its asset; no error line, no busy state", async () => {
    const s = setup();
    s.c.blockEvent("ia_1", item(0, 4));
    await flush();
    expect(sent(s)).toEqual([["ia_1", [item(0, 4)]]]);
    expect(s.c.state.busy).toBeNull();
  });

  it("fail closed: while the server is unreachable events wait (one copy each); they go out together when the connection comes back, oldest first, and are not sent twice", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const s = setup();
    const posts: [string, unknown[]][] = [];
    let down = true;
    vi.spyOn(s.api, "blockEvents").mockImplementation((id, evs) => {
      posts.push([id, [...evs]]);
      return down ? Promise.reject(new OfflineError()) : Promise.resolve({ inK: false });
    });
    s.c.blockEvent("ia_1", item(0, 4));
    s.c.blockEvent("ia_1", item(0, 4));       // the same event again (same key): kept once
    s.c.blockEvent("ia_1", summary);
    await flush();
    expect(posts.length).toBeGreaterThan(0);
    const tried = posts.length;
    down = false;
    s.conn.emit("offline");
    s.conn.emit("online");
    await flush();
    expect(posts.slice(tried)).toEqual([["ia_1", [item(0, 4), summary]]]);
    s.conn.emit("offline");
    s.conn.emit("online");
    await flush();
    expect(posts).toHaveLength(tried + 1);
    expect(s.notices).toEqual([]);
    warn.mockRestore();
  });

  it("a post the server refuses (422) is not retried and says so in a notice", async () => {
    const err = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const s = setup();
    s.api.failNext("blockEvents", new ApiError(422, "块里没有这个交互节点：quiz#9"));
    expect(() => s.c.blockEvent("ia_1", item(0, 4))).not.toThrow();
    await flush();
    expect(s.notices).toEqual(["这次交互结果没能记下：块里没有这个交互节点：quiz#9"]);
    expect(err).toHaveBeenCalled();
    await s.c.flushBlockEvents();
    expect(sent(s)).toHaveLength(1);
    err.mockRestore();
  });

  it("a result that went into K reads K again, and an open 知识图谱 is told; a score-only one is not", async () => {
    const s = setup();
    await s.c.init();
    await flush();
    const told = vi.fn();
    s.c.graphSource().kChanged?.(told);
    const post = vi.spyOn(s.api, "blockEvents").mockResolvedValue({ inK: false });
    s.c.blockEvent("ia_1", item(0, 4));
    await flush();
    expect(told).not.toHaveBeenCalled();
    post.mockResolvedValue({ inK: true });
    s.c.blockEvent("ia_1", item(1, 5));
    await flush();
    expect(told).toHaveBeenCalledTimes(1);
  });

  it("a long backlog goes out at most 200 events per post", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const s = setup();
    s.api.failNext("blockEvents", new OfflineError());
    for (let i = 0; i < 250; i++) s.c.blockEvent("ia_1", item(i, 10 + i));
    await flush();
    await s.c.flushBlockEvents();
    await flush();
    const sizes = sent(s).map((x) => (x as [string, unknown[]])[1].length);
    expect(sizes.slice(-2)).toEqual([200, 50]);
    warn.mockRestore();
  });
});

describe("evoui block progress", () => {
  const prog = (rev: number) => ({ v: 2, revision: rev, state: {}, nodes: { "flashcards#0": { index: rev } } });
  const saves = (s: ReturnType<typeof setup>): unknown[] => s.api.calls.filter((x) => x.method === "saveBlockProgress").map((x) => x.args);

  it("a save is posted as {revision, data}; the stored progress comes back from the server once and is cached", async () => {
    const s = setup();
    s.c.saveBlockProgress("ia_1", prog(3));
    await flush();
    expect(saves(s)).toEqual([["ia_1", { revision: 3, data: prog(3) }]]);
    s.api.blockProgressStore.set("ia_2", { revision: 5, data: prog(5) });
    expect(await s.c.blockProgress("ia_2")).toEqual(prog(5));
    expect(await s.c.blockProgress("ia_2")).toEqual(prog(5));
    expect(s.api.calls.filter((x) => x.method === "blockProgress")).toHaveLength(1);
    expect(await s.c.blockProgress("ia_none")).toBeNull();
  });

  it("fail closed: offline keeps only the newest progress per asset and resends it on reconnect; nothing is shown", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const s = setup();
    s.api.failNext("saveBlockProgress", new OfflineError());
    s.c.saveBlockProgress("ia_1", prog(1));
    await flush();
    s.c.saveBlockProgress("ia_1", prog(2));
    s.c.saveBlockProgress("ia_1", prog(1));      // older: ignored
    await flush();
    expect(await s.c.blockProgress("ia_1")).toEqual(prog(2));    // a reopened block resumes from the unsent one
    s.api.failNext("saveBlockProgress", new OfflineError());
    s.conn.emit("offline");
    s.conn.emit("online");
    await flush();
    s.conn.emit("offline");
    s.conn.emit("online");
    await flush();
    const last = saves(s)[saves(s).length - 1];
    expect(last).toEqual(["ia_1", { revision: 2, data: prog(2) }]);
    const n = saves(s).length;
    s.conn.emit("offline");
    s.conn.emit("online");
    await flush();
    expect(saves(s)).toHaveLength(n);              // recorded: not sent again
    expect(s.notices).toEqual([]);
    warn.mockRestore();
  });

  it("a save the server calls stale (newer progress elsewhere) is not resumed from: the server's is, with one notice", async () => {
    const s = setup();
    s.api.blockProgressStore.set("ia_1", { revision: 10, data: prog(10) });
    s.c.saveBlockProgress("ia_1", prog(1));
    await flush();
    s.c.saveBlockProgress("ia_1", prog(2));
    await flush();
    expect(s.notices).toEqual(["这个练习在别处有更新的进度，没有记下这次的。重新打开这一页会接着那次做。"]);
    expect(await s.c.blockProgress("ia_1")).toEqual(prog(10));
    await s.c.flushBlockProgress();
    expect(saves(s)).toHaveLength(2);
  });

  it("a refused save (422) is dropped with one notice; an unreachable read renders fresh without a notice", async () => {
    const err = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const s = setup();
    s.api.failNext("saveBlockProgress", new ApiError(422, "progress too large"));
    s.c.saveBlockProgress("ia_1", prog(1));
    await flush();
    expect(s.notices).toEqual(["这次交互进度没能记下：progress too large"]);
    await s.c.flushBlockProgress();
    expect(saves(s)).toHaveLength(1);
    s.api.failNext("blockProgress", new OfflineError());
    expect(await s.c.blockProgress("ia_9")).toBeNull();
    expect(s.notices).toHaveLength(1);
    err.mockRestore(); warn.mockRestore();
  });
});


describe("block progress ownership", () => {
  it("does not replace already-saved newer progress with a late older callback", async () => {
    const s = setup();
    const newer = { v: 2, revision: 8, state: { value: "new" }, nodes: {} };
    s.c.saveBlockProgress("ia_1", newer);
    await flush();
    const post = vi.spyOn(s.api, "saveBlockProgress").mockImplementation(() => new Promise(() => undefined));
    s.c.saveBlockProgress("ia_1", { ...newer, revision: 7, state: { value: "old" } });
    expect(await s.c.blockProgress("ia_1")).toEqual(newer);
    expect(post).not.toHaveBeenCalled();
    s.c.dispose();
  });
});


describe("durable interactive result handoff", () => {
  it("replays an offline answer after controller reconstruction with its original event identity", async () => {
    let kept: import("./evoui/event-store").QueuedBlockEvent[] = [];
    const store = { load: () => structuredClone(kept), save: (events: readonly import("./evoui/event-store").QueuedBlockEvent[]) => { kept = structuredClone([...events]); } };
    const first = setup({ blockEventStore: store });
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    vi.spyOn(first.api, "blockEvents").mockRejectedValue(new OfflineError());
    const event = { type: "item" as const, node: "quiz", session: "session-before-reload", revision: 4, attempt: 1, item: 0, response: 1 };
    first.c.blockEvent("ia_1", event);
    await flush();
    first.c.dispose();
    const second = setup({ blockEventStore: store });
    await second.c.flushBlockEvents();
    await flush();
    expect(second.api.calls.filter((call) => call.method === "blockEvents").map((call) => call.args)).toEqual([["ia_1", [event]]]);
    expect(kept).toEqual([]);
    second.c.dispose();
  });
});


it("rejects an answer when its durable enqueue fails", async () => {
  const app = setup({ blockEventStore: { load: () => [], save: () => { throw new Error("disk full"); } } });
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  expect(app.c.blockEvent("ia_1", { type: "item", node: "quiz", revision: 1 })).toBe(false);
  await flush();
  expect(app.api.calls.filter((call) => call.method === "blockEvents")).toEqual([]);
  expect(app.notices).toContain("交互结果未能保存到本机，请检查存储空间后重试。");
  app.c.dispose();
});
it("retains an acknowledged event on disk when clearing its durable copy fails", async () => {
  let kept: import("./evoui/event-store").QueuedBlockEvent[] = [];
  let refuseClear = true;
  const store = { load: () => structuredClone(kept), save: (events: readonly import("./evoui/event-store").QueuedBlockEvent[]) => {
    if (events.length === 0 && refuseClear) throw new Error("clear failed");
    kept = structuredClone([...events]);
  } };
  const app = setup({ blockEventStore: store });
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  const event = { type: "item" as const, node: "quiz", revision: 1, session: "stable-id" };
  expect(app.c.blockEvent("ia_1", event)).toBe(true);
  await flush();
  expect(kept).toEqual([{ assetId: "ia_1", event }]);
  refuseClear = false;
  await app.c.flushBlockEvents();
  expect(kept).toEqual([]);
  app.c.dispose();
});
it("removes a deleted asset's evidence without discarding another asset's offline answer", async () => {
  let kept: import("./evoui/event-store").QueuedBlockEvent[] = [];
  const store = { load: () => structuredClone(kept), save: (events: readonly import("./evoui/event-store").QueuedBlockEvent[]) => { kept = structuredClone([...events]); } };
  const app = setup({ blockEventStore: store });
  vi.spyOn(console, "warn").mockImplementation(() => undefined);
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  vi.spyOn(app.api, "blockEvents").mockImplementation((asset) => Promise.reject(asset === "ia_deleted" ? new ApiError(404, "source gone") : new OfflineError()));
  const event = { type: "item" as const, node: "quiz", revision: 1 };
  app.c.blockEvent("ia_deleted", event);
  app.c.blockEvent("ia_valid", event);
  await flush();
  expect(kept).toEqual([{ assetId: "ia_valid", event }]);
  app.c.dispose();
});


it("replays the newest offline progress after controller reconstruction", async () => {
  let kept: import("./evoui/progress-store").QueuedBlockProgress[] = [];
  const store = { load: () => structuredClone(kept), save: (progress: readonly import("./evoui/progress-store").QueuedBlockProgress[]) => { kept = structuredClone([...progress]); } };
  const first = setup({ blockProgressStore: store });
  vi.spyOn(console, "warn").mockImplementation(() => undefined);
  vi.spyOn(first.api, "saveBlockProgress").mockRejectedValue(new OfflineError());
  const progress = { v: 2, revision: 9, state: { value: "last before closing" }, nodes: {} };
  first.c.saveBlockProgress("ia_1", { ...progress, revision: 8 });
  first.c.saveBlockProgress("ia_1", progress);
  await flush();
  first.c.dispose();
  const second = setup({ blockProgressStore: store });
  await second.c.flushBlockProgress(); await flush();
  expect(second.api.calls.filter((call) => call.method === "saveBlockProgress").map((call) => call.args)).toEqual([["ia_1", { revision: 9, data: progress }]]);
  expect(await second.c.blockProgress("ia_1")).toEqual(progress);
  expect(kept).toEqual([]);
  second.c.dispose();
});


it("does not clear a newer durable progress version when an older request is acknowledged", async () => {
  let kept: import("./evoui/progress-store").QueuedBlockProgress[] = [];
  const store = { load: () => structuredClone(kept), save: (progress: readonly import("./evoui/progress-store").QueuedBlockProgress[]) => { kept = structuredClone([...progress]); } };
  const app = setup({ blockProgressStore: store });
  const releases: ((value: { stale: boolean }) => void)[] = [];
  vi.spyOn(app.api, "saveBlockProgress").mockImplementation(() => new Promise((resolve) => { releases.push(resolve); }));
  app.c.saveBlockProgress("ia_1", { revision: 8 });
  app.c.saveBlockProgress("ia_1", { revision: 9 });
  releases[0]!({ stale: false }); await flush();
  expect(kept).toEqual([{ assetId: "ia_1", progress: { revision: 9 } }]);
  releases[1]!({ stale: false }); await flush();
  expect(kept).toEqual([]);
  app.c.dispose();
});
it("rejects progress when saving it locally fails", async () => {
  const app = setup({ blockProgressStore: { load: () => [], save: () => { throw new Error("disk full"); } } });
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  expect(app.c.saveBlockProgress("ia_1", { revision: 1 })).toBe(false);
  await flush();
  expect(app.api.calls.filter((call) => call.method === "saveBlockProgress")).toEqual([]);
  expect(app.notices).toContain("交互进度未能保存到本机，请检查存储空间后重试。");
  app.c.dispose();
});
it("never restores a stale progress snapshot even when deleting its local copy fails", async () => {
  let kept: import("./evoui/progress-store").QueuedBlockProgress[] = [];
  let cannotClear = true;
  const store = { load: () => structuredClone(kept), save: (progress: readonly import("./evoui/progress-store").QueuedBlockProgress[]) => {
    if (progress.length === 0 && cannotClear) throw new Error("disk unavailable");
    kept = structuredClone([...progress]);
  } };
  const app = setup({ blockProgressStore: store });
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  app.api.blockProgressStore.set("ia_1", { revision: 10, data: { revision: 10 } });
  app.c.saveBlockProgress("ia_1", { revision: 1 }); await flush();
  expect(kept).toHaveLength(1);
  expect(await app.c.blockProgress("ia_1")).toEqual({ revision: 10 });
  cannotClear = false;
  await app.c.flushBlockProgress(); await flush();
  expect(kept).toEqual([]);
  expect(await app.c.blockProgress("ia_1")).toEqual({ revision: 10 });
  app.c.dispose();
});
