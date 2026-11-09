import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/errors";
import type { ReadingEntry } from "./bot/bot-schema";
import { FLUSH_MS, IDLE_MS, MAX_ENTRIES, ReadingReporter, SEND_TIMEOUT_MS, TICK_MS } from "./reading-reporter";

afterEach(() => { vi.useRealTimers(); });

function make(o: { page?: string | null; attentive?: boolean; canSend?: boolean; start?: number } = {}) {
  let t = o.start ?? new Date(2026, 9, 15, 12, 0).getTime();
  const env = {
    page: o.page === undefined ? "pg_1" : o.page, attentive: o.attentive ?? true, canSend: o.canSend ?? true,
    fail: null as Error | null, hang: false, sent: [] as { id: string; entries: ReadingEntry[] }[], n: 0,
  };
  const r = new ReadingReporter({
    now: () => t, currentPage: () => env.page, attentive: () => env.attentive, canSend: () => env.canSend,
    send: (id, entries) => {
      if (env.hang) return new Promise(() => undefined);
      if (env.fail) return Promise.reject(env.fail);
      env.sent.push({ id, entries }); return Promise.resolve();
    },
    newBatchId: () => `b-${++env.n}`,
  });
  const advance = (ms: number, act = true): void => {
    for (let k = 0; k < ms / TICK_MS; k++) { t += TICK_MS; if (act) r.activity(); r.tick(); }
  };
  return { r, env, advance, at: () => t };
}
const flush = async (): Promise<void> => { for (let i = 0; i < 5; i++) await Promise.resolve(); };
const total = (sent: { entries: ReadingEntry[] }[]): number => sent.flatMap((b) => b.entries).reduce((s, e) => s + e.seconds, 0);

describe("ReadingReporter", () => {
  it("counts only while a wiki page is active, attentive and recently used; batches every minute", async () => {
    const m = make();
    m.r.tick();
    m.advance(FLUSH_MS);
    await flush();
    expect(m.env.sent).toHaveLength(1);
    expect(m.env.sent[0]!.entries.map((e) => [e.page_id, e.seconds])).toEqual([["pg_1", 60]]);
  });

  it("stops counting after the idle cutoff, without focus, or off a wiki page", async () => {
    const m = make();
    m.r.tick(); m.r.activity();
    m.advance(IDLE_MS + FLUSH_MS, false);               // no input: only the first 90 s count
    await flush();
    await m.r.flush();
    expect(total(m.env.sent)).toBe(IDLE_MS / 1000);
    const n = make({ attentive: false });
    n.r.tick(); n.advance(FLUSH_MS); await n.r.flush();
    expect(n.env.sent).toEqual([]);
    const p = make({ page: null });
    p.r.tick(); p.advance(FLUSH_MS); await p.r.flush();
    expect(p.env.sent).toEqual([]);
  });

  it("a failed batch (network, 5xx, 408, 429) is resent with the same id; nothing is sent while it cannot be", async () => {
    for (const fail of [new Error("offline"), new ApiError(503, "busy"), new ApiError(429, "slow"), new ApiError(408, "t")]) {
      const m = make();
      m.r.tick();
      m.env.fail = fail;
      m.advance(FLUSH_MS);
      await flush();
      expect(m.env.sent).toEqual([]);
      expect(m.r.unsent.queue.map((b) => b.id)).toEqual(["b-1"]);
      m.env.fail = null;
      await m.r.flush();
      expect(m.env.sent.map((b) => b.id)).toEqual(["b-1"]);
    }
    const off = make({ canSend: false });
    off.r.tick(); off.advance(FLUSH_MS); await off.r.flush();
    expect(off.env.sent).toEqual([]);
    expect(off.r.unsent.counted.get("pg_1")).toBe(FLUSH_MS);   // kept for when it can send
  });

  it("any other 4xx: the batch is dropped, the next ones still go", async () => {
    const m = make();
    m.r.tick();
    m.env.fail = new ApiError(422, "bad entries");
    m.advance(FLUSH_MS);
    await flush();
    expect(m.r.unsent.queue).toEqual([]);
    m.env.fail = null;
    m.advance(FLUSH_MS);
    await flush();
    expect(m.env.sent.map((b) => b.id)).toEqual(["b-2"]);
  });

  it("long stretches are split into entries of at most 600 s; at most 200 entries a batch", async () => {
    const m = make({ canSend: false });
    m.r.tick();
    m.advance(15 * 60_000);
    m.env.canSend = true;
    await m.r.flush();
    expect(m.env.sent[0]!.entries.map((e) => e.seconds)).toEqual([600, 300]);
    const many = make({ canSend: false });
    many.r.tick();
    for (let i = 0; i < MAX_ENTRIES + 5; i++) { many.env.page = `pg_${i}`; many.advance(TICK_MS); }
    many.env.canSend = true;
    await many.r.flush();
    expect(many.env.sent.map((b) => b.entries.length)).toEqual([MAX_ENTRIES, 5]);
  });

  it("entries are stamped per local day: reading across midnight gives one entry for each day", async () => {
    const m = make({ canSend: false, start: new Date(2026, 9, 15, 23, 59, 0).getTime() });
    m.r.tick();
    m.advance(120_000);
    m.env.canSend = true;
    await m.r.flush();
    const days = m.env.sent[0]!.entries.map((e) => new Date(e.at).getDate());
    expect(days).toEqual([15, 16]);
    expect(total(m.env.sent)).toBe(120);
  });

  it("a send that never answers times out, so later flushes are not blocked", async () => {
    vi.useFakeTimers();
    const m = make();
    m.r.tick();
    m.env.hang = true;
    m.advance(FLUSH_MS);
    await vi.advanceTimersByTimeAsync(SEND_TIMEOUT_MS + 1);
    expect(m.r.unsent.queue.map((b) => b.id)).toEqual(["b-1"]);
    m.env.hang = false;
    await m.r.flush();
    expect(m.env.sent.map((b) => b.id)).toEqual(["b-1"]);
  });
});
