import { describe, expect, it, vi } from "vitest";
import { GraphFixture } from "./fixture";
import { GraphShapeError } from "./parse";
import { GraphStore, type GraphSource } from "./store";

const last = <T>(a: readonly T[]): T => a[a.length - 1]!;

function source(fx = new GraphFixture(), o: { load?: () => Promise<unknown>; offline?: boolean } = {}): GraphSource & { fx: GraphFixture } {
  return {
    fx, demo: false, isOffline: () => o.offline ?? false,
    load: o.load ?? (() => Promise.resolve(fx.graph())),
    split: (id) => Promise.resolve(fx.split(id)),
    want: (id, m) => Promise.resolve(fx.want(id, m)),
    editPath: (p, e) => Promise.resolve(fx.editPath(p, e)),
    splitRow: (p, k) => Promise.resolve(fx.splitRow(p, k)),
    undoSplitRow: (p, k, c) => Promise.resolve(fx.undoSplitRow(p, k, c)),
  };
}

describe("GraphStore", () => {
  it("idle → loading → ready; subscribers hear each change", async () => {
    const store = new GraphStore(source());
    const seen: string[] = [];
    store.subscribe(() => seen.push(store.state.status));
    expect(store.state.status).toBe("idle");
    await store.refresh();
    expect(seen).toEqual(["loading", "ready"]);
    expect(store.state.graph!.goals).toHaveLength(2);
  });

  it("a malformed answer is an error, never drawn", async () => {
    const store = new GraphStore(source(undefined, { load: () => Promise.resolve({ nodes: "x" }) }));
    await store.refresh();
    expect(store.state).toMatchObject({ status: "error", graph: null });
  });

  it("a failed later read keeps the graph (stale, offline as the source says)", async () => {
    let fail = false;
    const fx = new GraphFixture();
    const store = new GraphStore(source(fx, { load: () => (fail ? Promise.reject(new Error("x")) : Promise.resolve(fx.graph())), offline: true }));
    await store.refresh();
    fail = true;
    await store.refresh();
    expect(store.state).toMatchObject({ status: "ready", stale: true, offline: true });
    expect(store.state.graph).not.toBeNull();
  });

  it("an older read that answers after a newer one is dropped", async () => {
    const fx = new GraphFixture();
    let first!: (v: unknown) => void;
    const loads = [() => new Promise<unknown>((r) => { first = r; }), () => Promise.resolve(fx.graph())];
    const store = new GraphStore(source(fx, { load: () => loads.shift()!() }));
    const a = store.refresh();
    await store.refresh();
    const g = store.state.graph;
    first({ nodes: [], relations: [], groups: [], goals: [] });
    await a;
    expect(store.state.graph).toBe(g);
  });

  it("split applies the graph and resolves to the children; want applies the graph", async () => {
    const store = new GraphStore(source());
    await store.refresh();
    await expect(store.split("mul")).resolves.toEqual(["rc", "dim", "comm"]);
    expect(store.state.graph!.nodes.some((n) => n.id === "rc")).toBe(true);
    await store.want("py", "map_math");
    expect(last(store.state.graph!.goals[1]!.path).title).toBe("Python");
  });

  it("editPath replaces that goal's path with the server's; setPath shows a path at once", async () => {
    const store = new GraphStore(source());
    await store.refresh();
    const before = store.state.graph!.goals[0]!.path;
    store.setPath("path_ml", before.slice(1));
    expect(store.state.graph!.goals[0]!.path).toHaveLength(8);
    await store.editPath("path_ml", { op: "remove", index: 0 });
    expect(store.state.graph!.goals[0]!.path.map((p) => p.title)).toEqual(before.slice(1).map((p) => p.title));
    expect(store.state.graph!.goals[1]!.path).toHaveLength(4);
  });

  it("a failed or malformed action rejects and leaves the graph as it was", async () => {
    const src = source();
    const store = new GraphStore({ ...src, split: () => Promise.resolve({ children: ["rc"], graph: { nodes: [] } }) });
    await store.refresh();
    const g = store.state.graph;
    await expect(store.split("mul")).rejects.toBeInstanceOf(GraphShapeError);
    await expect(store.want("py", "nope")).rejects.toBeTruthy();
    expect(store.state.graph).toBe(g);
  });

  it("splitRow shows the new path, reads the graph (the parts grew) and resolves to the undo token; undoSplitRow puts the row back", async () => {
    const fx = new GraphFixture();
    const store = new GraphStore(source(fx));
    await store.refresh();
    const key = store.state.graph!.goals[0]!.path[0]!.key;
    const out = await store.splitRow("path_ml", key);
    expect(out.undo).toMatch(/^split_/);
    expect(store.state.graph!.goals[0]!.path.slice(0, 3).map((p) => p.title)).toEqual(["一行乘一列", "维度要对上", "不满足交换律"]);
    expect(store.state.graph!.nodes.some((n) => n.id === "rc")).toBe(true);
    await store.undoSplitRow("path_ml", key, out.undo!);
    expect(store.state.graph!.goals[0]!.path[0]!.title).toBe("矩阵乘法");
    await expect(store.undoSplitRow("path_ml", key, "gone")).rejects.toBeTruthy();
  });

  it("dispose: no more notifications", async () => {
    const store = new GraphStore(source());
    const fn = vi.fn();
    store.subscribe(fn);
    store.dispose();
    await store.refresh();
    expect(fn).not.toHaveBeenCalled();
  });
});
