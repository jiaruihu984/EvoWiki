import { describe, expect, it } from "vitest";
import { ApiError, OfflineError } from "../api/errors";
import { WIKI_COPY } from "./copy";
import type { WikiController } from "./controller";
import { firstPage, flush, methods, newGoal, setup, until, view } from "./controller-test-kit";
import type { PathOut } from "./schema";

const BIG = "学统计推断";
async function toPath(c: WikiController): Promise<void> {
  await newGoal(c, BIG);
}
const pathOf = (c: WikiController): PathOut => {
  const v = c.state.path.view;
  if (v.kind !== "ready") throw new Error(`path is ${v.kind}`);
  return v.value;
};

describe("learning path", () => {
  it("下一页: one POST next, then the new-page chain, then one GET path", async () => {
    const { c, api, sync, opened } = setup();
    await toPath(c);
    api.calls.length = 0;
    await c.pathNext();
    expect(methods(api)).toEqual(["pathNext", "organize", "openPage", "catalog", "kGrid", "getPath"]);
    expect(sync.runs).toHaveLength(1);
    expect(opened).toHaveLength(1);
    expect(c.state.tab).toBe("today");
    expect(view(c).page.organized).toBe(true);
    expect(c.state.busy).toBeNull();
    const p = pathOf(c);
    expect(p.pages[0]?.status).toBe("learning");
    expect(view(c).page.page_id).toBe(p.pages[0]?.page_id);
  });

  it("下一页 uses the path page title before the GET", async () => {
    const { c, api } = setup({ delayMs: 1 });
    await toPath(c);
    const p = pathOf(c);
    const run = c.pathNext();
    await until(() => methods(api).includes("organize") && c.state.page.kind === "open");
    expect(view(c).page.title).toBe(WIKI_COPY.pathPageTitle(p.goal, 1, p.pages.length));
    expect(view(c).page.goal).toBe(p.goal);
    await run;
  });

  it("下一页 with K loaded: kGrid after catalog, then getPath", async () => {
    const { c, api } = setup();
    await toPath(c);
    await c.init();
    expect(c.state.k.kind).toBe("ready");
    api.calls.length = 0;
    await c.pathNext();
    expect(methods(api)).toEqual(["pathNext", "organize", "openPage", "catalog", "kGrid", "getPath"]);
  });

  it("下一页 failure: one request, the reason in path.note, nothing retried", async () => {
    const { c, api } = setup();
    await toPath(c);
    api.calls.length = 0;
    api.failNext("pathNext", new ApiError(409, "这条路径的页都已经开始了"));
    await c.pathNext();
    expect(methods(api)).toEqual(["pathNext"]);
    expect(c.state.path.note?.tone).toBe("error");
    expect(c.state.busy).toBeNull();
    expect(c.state.tab).toBe("today");   // no tab of its own: the path shows in 今天
  });

  it("下一页 whose organize stream fails: afterStream as usual, then one GET path (the page is now in learning)", async () => {
    const { c, api } = setup();
    await toPath(c);
    api.calls.length = 0;
    api.failNext("organize", { code: "llm_failed", retryable: true, detail: "" });
    await c.pathNext();
    expect(methods(api)).toEqual(["pathNext", "organize", "getPath"]);
    expect(view(c).status?.tone).toBe("error");
    expect(c.state.busy).toBeNull();
    expect(pathOf(c).pages[0]?.status).toBe("learning");
  });

  it("下一页 when no page is left (next_index null) sends nothing", async () => {
    const { c, api } = setup();
    await toPath(c);
    const n = pathOf(c).pages.length;
    for (let i = 0; i < n; i++) {
      await c.openPath(c.state.path.pathId);
      await c.pathNext();
    }
    await c.openPath(c.state.path.pathId);
    expect(pathOf(c).next_index).toBeNull();
    api.calls.length = 0;
    await c.pathNext();
    expect(methods(api)).toEqual([]);
  });

  it("a failed GET path after 下一页 adds a second line to path.note only", async () => {
    const { c, api } = setup();
    await toPath(c);
    api.calls.length = 0;
    api.failNext("getPath", new OfflineError());
    await c.pathNext();
    expect(methods(api)).toEqual(["pathNext", "organize", "openPage", "catalog", "kGrid", "getPath"]);
    expect(c.state.path.view.kind).toBe("ready"); // the old path stays
    expect(c.state.path.note).toEqual({ text: WIKI_COPY.refreshOffline, tone: "error" });
  });

  it("offline: 下一页 / 学完 send nothing", async () => {
    const { c, api, conn } = setup();
    await toPath(c);
    await c.pathNext();
    await c.openPath(c.state.path.pathId);
    conn.emit("offline");
    api.calls.length = 0;
    await c.pathNext();
    expect(methods(api)).toEqual([]);
    expect(c.state.path.note).toEqual({ text: WIKI_COPY.offline, tone: "error" });
    await c.pathDone(1);
    expect(methods(api)).toEqual([]);
    expect(c.state.path.note).toEqual({ text: WIKI_COPY.offline, tone: "error" });
    expect(c.state.busy).toBeNull();
  });

  it("学完: one POST, the path from the response, then P5 GETs", async () => {
    const { c, api } = setup();
    await toPath(c);
    await c.pathNext();
    api.calls.length = 0;
    await c.pathDone(1);
    expect(methods(api)).toEqual(["pathPageDone", "openPage", "catalog", "kGrid"]);
    expect(pathOf(c).pages[0]?.status).toBe("done");
    expect(c.state.path.note?.tone).toBe("ok");
    expect(c.state.path.note?.text).toBeTruthy();
    expect(c.state.busy).toBeNull();
  });

  it("学完 failure: the reason in path.note, nothing re-read", async () => {
    const { c, api } = setup();
    await toPath(c);
    await c.pathNext();
    api.calls.length = 0;
    api.failNext("pathPageDone", new ApiError(503, "x"));
    await c.pathDone(1);
    expect(methods(api)).toEqual(["pathPageDone"]);
    expect(c.state.path.note?.tone).toBe("error");
    expect(pathOf(c).pages[0]?.status).toBe("learning");
    expect(c.state.busy).toBeNull();
  });

  it("学完 on a page not started sends nothing", async () => {
    const { c, api } = setup();
    await toPath(c);
    await c.pathNext();
    api.calls.length = 0;
    await c.pathDone(2);
    await c.pathDone(99);
    expect(methods(api)).toEqual([]);
    expect(c.state.busy).toBeNull();
  });

  it("openPath while busy does nothing; openPath(null) is local", async () => {
    const { c, api } = setup();
    await toPath(c);
    const pathId = c.state.path.pathId;
    const run = c.pathNext();
    expect(c.state.busy).not.toBeNull();
    await c.openPath(null);
    expect(c.state.path.pathId).toBe(pathId);
    await run;
    api.calls.length = 0;
    await c.openPath(null);
    expect(methods(api).filter((m) => m !== "rsiChanges")).toEqual([]);
    expect(c.state.tab).toBe("today");   // no tab of its own: the path shows in 今天
    expect(c.state.path).toEqual({ pathId: null, view: { kind: "idle" }, note: null });
    c.setTab("path");
    expect(methods(api).filter((m) => m !== "rsiChanges")).toEqual([]);
    await c.openPath(pathId);
    expect(methods(api).filter((m) => m !== "rsiChanges")).toEqual(["getPath"]);
    expect(c.state.path.view.kind).toBe("ready");
  });

  it("openPath: a failed GET shows its text; unconfigured shows needsSettings and sends nothing", async () => {
    const { c, api } = setup();
    api.failNext("getPath", new ApiError(404, "x"));
    await c.openPath("nope");
    expect(c.state.path.view.kind).toBe("error");
    const u = setup({ configured: false });
    await u.c.openPath("path1");
    expect(u.c.state.path.view).toEqual({ kind: "error", message: WIKI_COPY.needsSettings });
    expect(methods(u.api)).toEqual([]);
  });

  it("reloadCatalog: one GET; a failure keeps the catalog and says the reload failed", async () => {
    const { c, api } = setup();
    await c.init();
    api.calls.length = 0;
    await c.reloadCatalog();
    expect(methods(api)).toEqual(["catalog"]);
    api.failNext("catalog", new ApiError(500, "x"));
    await c.reloadCatalog();
    expect(methods(api)).toEqual(["catalog", "catalog"]);
    expect(c.state.catalog.kind).toBe("ready");
    expect(c.state.catalogNote).toEqual({ text: WIKI_COPY.reloadFailed(WIKI_COPY.failed), tone: "error" });
  });

  it("reloadCatalog while busy or unconfigured sends nothing", async () => {
    const { c, api } = setup();
    await toPath(c);
    api.calls.length = 0;
    const run = c.pathNext();
    await c.reloadCatalog();
    await run;
    expect(methods(api)).toEqual(["pathNext", "organize", "openPage", "catalog", "kGrid", "getPath"]); // no extra catalog
    const u = setup({ configured: false });
    await u.c.reloadCatalog();
    expect(methods(u.api)).toEqual([]);
    expect(u.c.state.catalogNote).toEqual({ text: WIKI_COPY.needsSettings, tone: "error" });
  });
});

describe("loadPath after a newer write", () => {
  it("下一页's follow-up GET path is dropped when 学完 started after it was sent", async () => {
    const { c, api } = setup();
    await toPath(c);
    await c.pathNext(); // page 1: 学习中
    expect(pathOf(c).pages[0]?.status).toBe("learning");
    // Hold the next GET path (its answer is computed when it is sent: page 1 still 学习中).
    let open = (): void => undefined;
    const held = new Promise<void>((r) => { open = r; });
    const orig = api.getPath.bind(api);
    let hold = true;
    api.getPath = async (id: string) => {
      const out = await orig(id);
      if (hold) { hold = false; await held; }
      return out;
    };
    const gets = (): number => methods(api).filter((m) => m === "getPath").length;
    const before = gets();
    const run = c.pathNext(); // page 2; then the held GET path
    await until(() => gets() === before + 1 && c.state.busy === null);
    await c.pathDone(1); // a newer write: its response says page 1 学完了
    expect(pathOf(c).pages[0]?.status).toBe("done");
    open();
    await run;
    expect(pathOf(c).pages[0]?.status).toBe("done"); // the older GET did not land
    expect(pathOf(c).pages[1]?.status).toBe("learning");
  });

  it("a plain path load still lands when an unrelated write started meanwhile (no endless loading)", async () => {
    const { c, api } = setup();
    await firstPage(c);
    await toPath(c);
    const pathId = c.state.path.pathId;
    let open = (): void => undefined;
    const held = new Promise<void>((r) => { open = r; });
    const orig = api.getPath.bind(api);
    api.getPath = async (id: string) => { const out = await orig(id); await held; return out; };
    const load = c.openPath(pathId);
    expect(c.state.path.view.kind).toBe("loading");
    const write = c.setReviewPerDay(5);
    open();
    await Promise.all([load, write]);
    expect(c.state.path.view.kind).toBe("ready");
  });
});

describe("final review fixes (path)", () => {
  it("下一页 whose follow-up GET path fails: the shown path is already patched from the POST (button names page 2)", async () => {
    const { c, api } = setup();
    await toPath(c);
    api.failNext("getPath", new OfflineError());
    await c.pathNext();
    const p = pathOf(c);
    expect(p.pages[0]?.status).toBe("learning");
    expect(p.pages[0]?.page_id).toBe(view(c).page.page_id);
    expect(p.next_index).toBe(2);
    expect(c.state.path.note).toEqual({ text: WIKI_COPY.refreshOffline, tone: "error" });
  });

  it("the last page's 下一页 with a failed GET path: no page left (next_index null)", async () => {
    const { c, api } = setup();
    await toPath(c);
    const n = pathOf(c).pages.length;
    for (let i = 1; i < n; i++) {
      await c.openPath(c.state.path.pathId);
      await c.pathNext();
    }
    await c.openPath(c.state.path.pathId);
    expect(pathOf(c).next_index).toBe(n);
    api.failNext("getPath", new ApiError(500, "x"));
    await c.pathNext();
    expect(pathOf(c).next_index).toBeNull();
    expect(pathOf(c).pages.every((pg) => pg.status === "learning")).toBe(true);
  });

  it("an unrelated write during 下一页's follow-up GET path no longer drops it", async () => {
    const { c, api } = setup();
    await firstPage(c);
    await toPath(c);
    let open = (): void => undefined;
    const held = new Promise<void>((r) => { open = r; });
    const orig = api.getPath.bind(api);
    let hold = true;
    // The held answer is marked (source fallback) so its landing is visible.
    api.getPath = async (id: string) => {
      const out = await orig(id);
      if (!hold) return out;
      hold = false;
      await held;
      return { ...out, source: "fallback" as const };
    };
    const gets = (): number => methods(api).filter((m) => m === "getPath").length;
    const before = gets();
    const run = c.pathNext();
    await until(() => gets() === before + 1 && c.state.busy === null);
    await c.setReviewPerDay(5); // a settings write: not a path write
    open();
    await run;
    expect(pathOf(c).source).toBe("fallback");
  });

  it("学完, then switching tabs reads only what 今天 shows; the path is re-read by openPath", async () => {
    const { c, api } = setup();
    await toPath(c);
    await c.pathNext();
    const id = c.state.path.pathId ?? "";
    await c.done();
    expect(pathOf(c).pages[0]?.status).toBe("learning"); // not re-read yet
    api.calls.length = 0;
    c.setTab("assistant");
    expect(methods(api)).toEqual([]);
    c.setTab("path");                                    // a legacy value: lands on 今天 and re-reads what it shows
    await flush();
    // (+ files: the vault has no note yet, so the first reconcile writes every page's note once)
    expect([...new Set(methods(api))].sort()).toEqual(["botActions", "botReviewReminder", "botSettings", "botSuggestions", "catalog", "files", "getPath", "reviewToday", "rsiChanges"]);
    api.calls.length = 0;
    await c.openPath(id);
    expect(methods(api)).toEqual(["getPath"]);
    expect(pathOf(c).pages.some((pg) => pg.status === "learning")).toBe(false); // no 学完 button left
  });

  it("switching tabs while a write is in flight sends nothing", async () => {
    const { c, api } = setup({ delayMs: 1 });
    await toPath(c);
    const run = c.pathNext();
    await until(() => methods(api).includes("organize"));
    api.calls.length = 0;
    c.setTab("path");
    expect(methods(api)).toEqual([]);
    await run;
  });
});
