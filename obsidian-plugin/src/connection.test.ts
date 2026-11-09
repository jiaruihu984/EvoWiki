import { describe, expect, it, vi } from "vitest";
import { ApiError, OfflineError } from "./api/errors";
import { BACKOFF_MS, ConnectionMonitor, type Timers } from "./connection";

function fakeTimers() {
  const q: { fn: () => void; ms: number; id: number }[] = []; let n = 0;
  const t: Timers = { set: (fn, ms) => { q.push({ fn, ms, id: ++n }); return n; },
                      clear: (id) => { const i = q.findIndex((x) => x.id === id); if (i >= 0) q.splice(i, 1); } };
  return { t, q, fire: async () => { const x = q.shift(); x?.fn(); await new Promise((r) => setTimeout(r, 0)); return x?.ms; } };
}

describe("ConnectionMonitor", () => {
  it("goes offline on OfflineError and online on any server answer", async () => {
    const { t } = fakeTimers();
    const m = new ConnectionMonitor(async () => {}, t);
    await expect(m.track(Promise.reject(new OfflineError()))).rejects.toBeInstanceOf(OfflineError);
    expect(m.state).toBe("offline");
    await expect(m.track(Promise.reject(new ApiError(404, "x")))).rejects.toBeInstanceOf(ApiError);
    expect(m.state).toBe("online");
  });

  it("re-pings on the fixed schedule and then keeps to the last interval until the server answers", async () => {
    const { t, q, fire } = fakeTimers();
    const ping = vi.fn(() => Promise.reject(new OfflineError()));
    const m = new ConnectionMonitor(ping, t);
    await m.track(Promise.reject(new OfflineError())).catch(() => {});
    const delays: (number | undefined)[] = [];
    for (let i = 0; i < BACKOFF_MS.length + 2; i++) delays.push(await fire());
    expect(delays).toEqual([...BACKOFF_MS, 300_000, 300_000]);
    expect(ping).toHaveBeenCalledTimes(BACKOFF_MS.length + 2);
    expect(m.state).toBe("offline");
    ping.mockResolvedValueOnce(undefined as never);
    await fire();
    expect(m.state).toBe("online");
    expect(q.length).toBe(0);
  });

  it("retryNow probes immediately and a success resets the schedule", async () => {
    const { t, q } = fakeTimers();
    let up = false;
    const m = new ConnectionMonitor(() => (up ? Promise.resolve() : Promise.reject(new OfflineError())), t);
    const seen: string[] = []; m.onChange((s) => seen.push(s));
    await m.track(Promise.reject(new OfflineError())).catch(() => {});
    up = true; await m.retryNow();
    expect(m.state).toBe("online"); expect(q).toHaveLength(0); expect(seen).toEqual(["offline", "online"]);
  });

  it("stop() cancels pending pings", async () => {
    const { t, q } = fakeTimers();
    const m = new ConnectionMonitor(() => Promise.reject(new OfflineError()), t);
    await m.track(Promise.reject(new OfflineError())).catch(() => {});
    m.stop(); expect(q).toHaveLength(0);
  });
});
