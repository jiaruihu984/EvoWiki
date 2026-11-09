import { describe, expect, it } from "vitest";
import { ApiError } from "../api/errors";
import { setup } from "./controller-test-kit";

describe("WikiController.starMapSource", () => {
  it("reads K and the catalog and returns goal membership from the learning paths", async () => {
    const { c, api } = setup();
    const data = await c.starMapSource().load();
    expect(data.k.concepts.length).toBeGreaterThan(0);
    expect(data.catalog).not.toBeNull();
    const methods = api.calls.map((x) => x.method);
    expect(methods).toContain("kGrid");
    expect(methods).toContain("catalog");
    expect(Object.keys(data.goals.membership)).toEqual(data.goals.goals.map((g) => g.id));
  });

  it("a failing K read throws; a failing catalog only drops the grouping", async () => {
    const a = setup();
    a.api.failNext("kGrid", new ApiError(500, "x"));
    await expect(a.c.starMapSource().load()).rejects.toBeTruthy();
    const b = setup();
    b.api.failNext("catalog", new ApiError(500, "x"));
    const d = await b.c.starMapSource().load();
    expect(d.catalog).toBeNull();
    expect(d.goals.goals).toEqual([]);
  });

  it("not configured throws without a request; offline and demo are reported", async () => {
    const a = setup({ configured: false });
    await expect(a.c.starMapSource().load()).rejects.toBeTruthy();
    expect(a.api.calls).toHaveLength(0);
    const b = setup();
    expect(b.c.starMapSource().isOffline()).toBe(false);
    b.conn.emit("offline");
    expect(b.c.starMapSource().isOffline()).toBe(true);
    expect(b.c.starMapSource().demo).toBe(false);
    expect(setup({ demo: true }).c.starMapSource().demo).toBe(true);
  });
});

describe("WikiController.graphSource (知识图谱)", () => {
  it("reads GET /wiki/graph and sends the page's actions to the API", async () => {
    const { c, api } = setup();
    const src = c.graphSource();
    expect(((await src.load()) as { goals: unknown[] }).goals).toHaveLength(2);
    await src.split("mul");
    await src.want("py", "map_math");
    await src.editPath("path_ml", { op: "move", from: 0, to: 1 });
    expect(api.calls.map((x) => [x.method, x.args])).toEqual([
      ["graph", []], ["splitNode", ["mul"]], ["wantNode", ["py", "map_math"]],
      ["editPath", ["path_ml", { op: "move", from: 0, to: 1 }]]]);
  });

  it("sends 拆成多页 (and its undo) to the API", async () => {
    const { c, api } = setup();
    const src = c.graphSource();
    const split = (await src.splitRow("path_ml", "path_ml_1")) as { undo: string };
    await src.undoSplitRow("path_ml", "path_ml_1", split.undo);
    expect(api.calls.map((x) => [x.method, x.args])).toEqual([
      ["splitRow", ["path_ml", "path_ml_1"]], ["undoSplitRow", ["path_ml", "path_ml_1", split.undo]]]);
  });

  it("the rows' live status: subscribing starts the writes watcher, also when 今天 never refreshed", async () => {
    const { c, api } = setup();
    const w = c.graphSource().writes;
    expect(w).toBeDefined();
    const off = w!.subscribe(() => undefined);
    await new Promise((r) => setTimeout(r, 20));
    expect(api.calls.some((x) => x.method === "writes")).toBe(true);
    off(); c.dispose();
  });

  it("not configured throws without a request; offline and demo are reported", async () => {
    const a = setup({ configured: false });
    await expect(a.c.graphSource().load()).rejects.toBeTruthy();
    expect(a.api.calls).toHaveLength(0);
    const b = setup();
    b.conn.emit("offline");
    expect(b.c.graphSource().isOffline()).toBe(true);
    expect(setup({ demo: true }).c.graphSource().demo).toBe(true);
  });
});
