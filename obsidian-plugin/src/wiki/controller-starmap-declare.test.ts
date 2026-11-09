import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/errors";
import { setup } from "./controller-test-kit";

beforeEach(() => { vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] }); });
afterEach(() => { vi.useRealTimers(); });

/** Lets promise chains run (the controller awaits several fake calls per cell). */
async function settle(n = 40): Promise<void> { for (let i = 0; i < n; i++) await Promise.resolve(); }

async function withK() {
  const s = setup();
  await s.c.starMapSource().load();          // proves the fake answers
  await s.c.init();
  await vi.advanceTimersByTimeAsync(50);
  return s;
}

describe("WikiController.declareConcept", () => {
  it("declares every aspect cell of the concept one after the other, then says so once", async () => {
    const s = await withK();
    const grid = await s.api.kGrid();
    const concept = grid.concepts[0]!;
    const cells = concept.cells.filter((x) => concept.aspects.includes(x.aspect)).map((x) => x.cell);
    expect(cells.length).toBeGreaterThan(1);
    s.api.calls.length = 0;
    const p = s.c.declareConcept(concept.id, "dont");
    await vi.advanceTimersByTimeAsync(100);
    const ok = await p;
    expect(ok).toBe(true);
    const declares = s.api.calls.filter((x) => x.method === "declare");
    expect(declares.map((x) => x.args)).toEqual(cells.map((c) => [c, "dont"]));
    expect(s.notices).toEqual([`记下了：${concept.name} · 不会。讲到它的页面会讲细一点。`]);
  });

  it("skips a cell that already carries the same declaration", async () => {
    const s = await withK();
    const concept = (await s.api.kGrid()).concepts[0]!;
    const first = concept.cells[0]!.cell;
    await s.c.declareCell(first, "know");              // the controller's K now carries it
    await vi.advanceTimersByTimeAsync(100);
    s.api.calls.length = 0;
    s.notices.length = 0;
    await s.c.declareConcept(concept.id, "know");
    await vi.advanceTimersByTimeAsync(100);
    const declared = s.api.calls.filter((x) => x.method === "declare").map((x) => x.args[0]);
    expect(declared).not.toContain(first);
    expect(s.notices).toContain(`记下了：${concept.name} · 会。`);
  });

  it("stops at the first failure and says what could not be recorded", async () => {
    const s = await withK();
    const concept = (await s.api.kGrid()).concepts[0]!;
    s.api.failNext("declare", new ApiError(500, "detail that must not show"));
    s.api.calls.length = 0;
    s.notices.length = 0;
    const ok = await s.c.declareConcept(concept.id, "dont");
    expect(ok).toBe(false);
    expect(s.api.calls.filter((x) => x.method === "declare")).toHaveLength(1);
    expect(s.notices).toEqual(["没能记下，什么都没改。"]);
    expect(s.notices.join("")).not.toContain("detail");
  });

  it("quietOk (the 知识图谱 card notices itself): no success notice, failures still say so", async () => {
    const s = await withK();
    const concept = (await s.api.kGrid()).concepts[0]!;
    s.notices.length = 0;
    const p = s.c.declareConcept(concept.id, "dont", { quietOk: true });
    await vi.advanceTimersByTimeAsync(100);
    expect(await p).toBe(true);
    expect(s.notices).toEqual([]);
    s.api.failNext("declare", new ApiError(500, "x"));
    expect(await s.c.declareConcept(concept.id, "know", { quietOk: true })).toBe(false);
    expect(s.notices).toEqual(["没能记下，什么都没改。"]);
  });

  it("a later cell failing says only part was recorded", async () => {
    const s = await withK();
    const concept = (await s.api.kGrid()).concepts[0]!;
    let n = 0;
    const real = s.api.declare.bind(s.api);
    s.api.declare = (cell, v) => (++n === 2 ? Promise.reject(new ApiError(500, "x")) : real(cell, v));
    s.notices.length = 0;
    const ok = await s.c.declareConcept(concept.id, "dont");
    expect(ok).toBe(false);
    expect(n).toBe(2);
    expect(s.notices).toEqual([`只记下了${concept.name}的一部分，其余没能记下。`]);
  });

  it("an unknown concept records nothing", async () => {
    const s = await withK();
    s.api.calls.length = 0;
    expect(await s.c.declareConcept("no-such-concept", "know")).toBe(false);
    expect(s.api.calls.filter((x) => x.method === "declare")).toHaveLength(0);
    expect(s.notices).toEqual(["没能记下，什么都没改。"]);
  });

  it("a concept with no aspect cells says so and records nothing", async () => {
    const s = setup();
    const real = s.api.kGrid.bind(s.api);
    s.api.kGrid = async () => { const g = await real(); return { concepts: g.concepts.map((c, i) => (i === 0 ? { ...c, cells: [] } : c)) }; };
    await s.c.init();
    await vi.advanceTimersByTimeAsync(50);
    const concept = (await real()).concepts[0]!;
    s.api.calls.length = 0;
    s.notices.length = 0;
    expect(await s.c.declareConcept(concept.id, "know")).toBe(false);
    expect(s.api.calls.filter((x) => x.method === "declare")).toHaveLength(0);
    expect(s.notices).toEqual(["没有可以标记的方面。"]);
  });

  it("offline: nothing is sent", async () => {
    const s = await withK();
    const concept = (await s.api.kGrid()).concepts[0]!;
    s.conn.emit("offline");
    s.api.calls.length = 0;
    expect(await s.c.declareConcept(concept.id, "dont")).toBe(false);
    expect(s.api.calls.filter((x) => x.method === "declare")).toHaveLength(0);
  });

  it("re-reads the shared star map store once, at the end", async () => {
    const s = await withK();
    const store = s.c.starMapStore();
    await store.refresh();
    const concept = (await s.api.kGrid()).concepts[0]!;
    s.api.calls.length = 0;
    const p = s.c.declareConcept(concept.id, "dont");
    await vi.advanceTimersByTimeAsync(2000);
    await p;
    expect(s.api.calls.filter((x) => x.method === "kGrid" && x.args.length === 0).length).toBeGreaterThanOrEqual(1);
    expect(store.state.map!.byId.get(concept.id)!.band).toBe("gap");
  });
});

describe("WikiController.starMapStore", () => {
  it("is one store, shared; a changed K re-reads it after a pause, but only once a map was read", async () => {
    const s = await withK();
    const store = s.c.starMapStore();
    expect(s.c.starMapStore()).toBe(store);
    expect(store.state.status).toBe("idle");
    // K changes before any map was read: nothing is read for the maps
    await s.c.refreshToday();
    await vi.advanceTimersByTimeAsync(1000);
    expect(store.state.status).toBe("idle");
    await store.refresh();
    const seen: string[] = [];
    store.subscribe(() => { seen.push(store.state.status); });
    // a K change (a declaration) now makes the maps read again, once
    const concept = (await s.api.kGrid()).concepts[0]!;
    await s.c.declareCell(concept.cells[0]!.cell, "dont");
    await vi.advanceTimersByTimeAsync(1000);
    await settle();
    expect(seen.length).toBeGreaterThan(0);
    expect(store.state.status).toBe("ready");
  });

  it("dispose ends the pause timer and the store", async () => {
    const s = await withK();
    const store = s.c.starMapStore();
    await store.refresh();
    s.c.dispose();
    await store.refresh();
    expect(store.state.status).toBe("ready");     // disposed: a refresh does nothing
  });
});
