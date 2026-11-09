import { describe, expect, it, vi } from "vitest";
import { protoCatalog, protoGoals, protoK } from "./fixture";
import { StarMapStore, type StarMapData, type StarMapSource } from "./store";

const data = (): StarMapData => ({ k: protoK(), catalog: protoCatalog(), goals: protoGoals() });

function source(loads: (() => Promise<StarMapData>)[], offline = false): StarMapSource & { calls: number } {
  const s = {
    calls: 0, demo: false, isOffline: () => offline,
    load: () => { const f = loads[Math.min(s.calls, loads.length - 1)]!; s.calls++; return f(); },
  };
  return s;
}

describe("StarMapStore", () => {
  it("starts idle, reads only on refresh, then holds a map and a converged layout", async () => {
    const src = source([() => Promise.resolve(data())]);
    const store = new StarMapStore(src);
    expect(store.state.status).toBe("idle");
    expect(src.calls).toBe(0);
    const seen: string[] = [];
    store.subscribe(() => seen.push(store.state.status));
    await store.refresh();
    expect(seen).toEqual(["loading", "ready"]);
    expect(store.state.map!.nodes).toHaveLength(18);
    expect(store.state.goals.map((g) => g.name)).toEqual(["PCA", "基因配对"]);
  });

  it("a failed first read is the error state (offline flag from the source)", async () => {
    const store = new StarMapStore(source([() => Promise.reject(new Error("boom"))], true));
    await store.refresh();
    expect(store.state).toMatchObject({ status: "error", map: null, offline: true });
  });

  it("a failed later read keeps the old map and marks it stale; a retry recovers", async () => {
    const store = new StarMapStore(source([() => Promise.resolve(data()), () => Promise.reject(new Error("x")), () => Promise.resolve(data())]));
    await store.refresh();
    await store.refresh();
    expect(store.state).toMatchObject({ status: "ready", stale: true });
    expect(store.state.map).not.toBeNull();
    await store.refresh();
    expect(store.state.stale).toBe(false);
  });

  it("an older read finishing after a newer one is ignored", async () => {
    let slow!: (d: StarMapData) => void;
    const first = new Promise<StarMapData>((r) => { slow = r; });
    const empty: StarMapData = { k: { concepts: [] }, catalog: null, goals: { goals: [], membership: {} } };
    const store = new StarMapStore(source([() => first, () => Promise.resolve(empty)]));
    const a = store.refresh();
    await store.refresh();
    slow(data());
    await a;
    expect(store.state.map!.nodes).toHaveLength(0);
  });

  it("dispose stops notifications", async () => {
    const store = new StarMapStore(source([() => Promise.resolve(data())]));
    const fn = vi.fn();
    store.subscribe(fn);
    store.dispose();
    await store.refresh();
    expect(fn).not.toHaveBeenCalled();
  });
});
