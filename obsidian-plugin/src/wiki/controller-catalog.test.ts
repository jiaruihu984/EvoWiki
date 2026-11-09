import { setUiLanguage, uiLanguage } from "../i18n";
import { describe, expect, it } from "vitest";
import { ApiError, OfflineError } from "../api/errors";
import { WIKI_COPY } from "./copy";
import { firstPage, flush, methods, newGoal, setup, view } from "./controller-test-kit";
import type { WikiController } from "./controller";
import { FakeWikiApi } from "./fake";
import { CONCEPTS, PCA_GOAL } from "./fixtures";
import type { CellBeliefOut, SettingsIO, SettingsPut } from "./schema";
import type { ResultNote } from "./state";

/** Calls made after `n`. */
const since = (api: FakeWikiApi, n: number): string[] => methods(api).slice(n);
const cellOf = (c: WikiController, cell: string): CellBeliefOut | undefined => {
  const k = c.state.k;
  return k.kind === "ready" ? k.value.concepts.flatMap((x) => x.cells).find((x) => x.cell === cell) : undefined;
};
const err = (text: string): ResultNote => ({ text, tone: "error" });
const okNote = (text: string): ResultNote => ({ text, tone: "ok" });

/** p1 (z-score) organized, then p2 (PCA) created and organized; p2 is the current page. */
async function twoPages(c: WikiController): Promise<void> {
  await firstPage(c);
  await newGoal(c, PCA_GOAL);
}
/** 今天's reads (init) include K. */
async function loadK(c: WikiController): Promise<void> {
  await c.init();
  expect(c.state.k.kind).toBe("ready");
}

/** putSettings waits for release() (to observe the controller while a write is in flight). */
class HeldApi extends FakeWikiApi {
  private gate: (() => void) | null = null;
  release(): void { this.gate?.(); }
  override async putSettings(s: SettingsPut): Promise<SettingsIO> {
    await new Promise<void>((r) => { this.gate = r; });
    return super.putSettings(s);
  }
}

/** p1's goal (c1) all 确定会: the server offers 收为参考卡 for it. */
async function shelvable(api: FakeWikiApi, c: WikiController): Promise<void> {
  for (const a of CONCEPTS.c1.aspects) await api.declare(`c1.${a}`, "know");
  await c.openWikiPage("p1");
  expect(view(c).page.can_shelve).toBe(true);
}

describe("收为参考卡 / 恢复成完整页", () => {
  it("收为参考卡: POST collapse → 已收为参考卡 → GET page + catalog (+ K) → the note is synced collapsed", async () => {
    const { api, sync, c } = setup();
    await firstPage(c);
    await loadK(c);
    await shelvable(api, c);
    const runs = sync.runs.length;
    const n = api.calls.length;
    await c.collapse();
    expect(since(api, n)).toEqual(["collapse", "openPage", "catalog", "kGrid"]);
    expect(api.calls[n]?.args).toEqual(["p1"]);
    expect(view(c).status?.text.startsWith(WIKI_COPY.shelveDone)).toBe(true);
    expect(view(c).page.collapsed).toBe(true);
    expect(view(c).page.can_shelve).toBe(false);
    expect(sync.runs).toHaveLength(runs + 1);
    expect(sync.runs[runs]).toEqual(expect.objectContaining({ pageId: "p1", collapsed: true }));
    expect(c.state.catalog.kind === "ready" && c.state.catalog.value.shelf.pages.map((x) => x.page_id)).toEqual(["p1"]);
    expect(c.state.busy).toBeNull();
  });

  it("not offered (can_shelve false): nothing is sent", async () => {
    const { api, c } = setup();
    await firstPage(c);
    expect(view(c).page.can_shelve).toBe(false);
    const n = api.calls.length;
    await c.collapse();
    expect(since(api, n)).toEqual([]);
  });

  it("failure (409): the error on the page's status line, nothing re-read", async () => {
    const { api, c } = setup();
    await firstPage(c);
    await shelvable(api, c);
    const n = api.calls.length;
    api.failNext("collapse", new ApiError(409, "secret"));
    await c.collapse();
    expect(since(api, n)).toEqual(["collapse"]);
    expect(view(c).status).toEqual(err("页面刚被别的操作改过，请再试一次。"));
    expect(view(c).page.collapsed).toBe(false);
  });

  it("恢复成完整页 on a reference card: POST expand → the note is synced full", async () => {
    const { api, c: first } = setup();
    await firstPage(first);
    api.__collapse("p1");
    // A fresh controller on the same server: its first view re-renders the note once (collapsed).
    const { sync, c } = setup({ api });
    await c.init();
    await c.openWikiPage("p1"); await flush();
    expect(view(c).page.collapsed).toBe(true);
    expect(sync.runs.map((r) => r.collapsed)).toEqual([true]);
    const n = api.calls.length;
    await c.expand();
    expect(since(api, n)).toEqual(["expand", "openPage", "catalog", "kGrid"]);
    expect(view(c).status?.text.startsWith(WIKI_COPY.expandDone)).toBe(true);
    expect(view(c).page.collapsed).toBe(false);
    expect(sync.runs).toHaveLength(2);
    expect(sync.runs[1]).toEqual(expect.objectContaining({ pageId: "p1", collapsed: false }));
  });
});

describe("settings", () => {
  it("每天最多: PUT exactly {review_per_day} → 已保存 → GET catalog and today's list", async () => {
    const { api, c } = setup();
    await c.init();
    const n = api.calls.length;
    await c.setReviewPerDay(14);
    expect(since(api, n)).toEqual(["putSettings", "catalog", "reviewToday"]);
    expect(api.calls[n]?.args).toEqual([{ review_per_day: 14 }]);
    expect(c.state.catalog.kind === "ready" && c.state.catalog.value.settings).toEqual({ review_per_day: 14, page_limit_usd: null, language: "zh" });
    expect(c.state.busy).toBeNull();
  });

  it("failure: error text, no GET", async () => {
    const { api, c } = setup();
    await c.init();
    const n = api.calls.length;
    api.failNext("putSettings", new ApiError(422, "secret"));
    await c.setReviewPerDay(3);
    expect(since(api, n)).toEqual(["putSettings"]);
    expect(c.state.review.note).toEqual(err("请求不对，请刷新后再试。"));
  });
});

describe("K", () => {
  it("init GETs K; switching tabs (old k value included) does not read it again", async () => {
    const { api, c } = setup();
    await loadK(c);
    c.setTab("assistant");
    c.setTab("k");
    await flush();
    expect(methods(api).filter((m) => m === "kGrid")).toHaveLength(1);
  });

  it("selectCell is local; the same cell again clears it", () => {
    const { api, c } = setup();
    let notified = 0;
    c.subscribe(() => notified++);
    c.selectCell("c1.term");
    expect(c.state.kSelected).toBe("c1.term");
    c.selectCell("c1.notation");
    expect(c.state.kSelected).toBe("c1.notation");
    c.selectCell("c1.notation");
    expect(c.state.kSelected).toBeNull();
    c.selectCell("c1.term");
    c.selectCell(null);
    expect(c.state.kSelected).toBeNull();
    expect(notified).toBe(5);
    expect(methods(api)).toEqual([]);
  });

  it("declare: POST declare {value} → the cell is replaced → kNote → GET page + catalog + K", async () => {
    const { api, sync, c } = setup();
    await firstPage(c);
    await loadK(c);
    expect(cellOf(c, "c3.notation")?.band).toBe("shaky");
    const runs = sync.runs.length;
    const n = api.calls.length;
    await c.declareCell("c3.notation", "know");
    expect(since(api, n)).toEqual(["declare", "openPage", "catalog", "kGrid"]);
    expect(api.calls[n]?.args).toEqual(["c3.notation", "know"]);
    expect(cellOf(c, "c3.notation")).toEqual(expect.objectContaining({ band: "known", declared: "know" }));
    expect(c.state.kNote).toEqual(okNote("你修改了对“方差·公式 / 记号”的判断"));
    expect(sync.runs).toHaveLength(runs); // nothing on the page changed
    const m = api.calls.length;
    await c.declareCell("c3.notation", null);
    expect(api.calls[m]?.args).toEqual(["c3.notation", null]);
    expect(cellOf(c, "c3.notation")).toEqual(expect.objectContaining({ band: "shaky", declared: null }));
    expect(c.state.busy).toBeNull();
  });

  it("declare: the K cell is the new band straight away, even when the K re-GET fails", async () => {
    const { api, c } = setup();
    await c.init();
    await loadK(c);
    api.failNext("kGrid", new ApiError(500, "x"));
    const n = api.calls.length;
    await c.declareCell("c1.interpret", "know");
    expect(since(api, n)).toEqual(["declare", "catalog", "kGrid"]);
    expect(cellOf(c, "c1.interpret")).toEqual(expect.objectContaining({ band: "known", declared: "know" }));
    // The declare was sent and applied: its line stays, the failed refresh is a second line (final review, Important 1).
    expect(c.state.kNote?.text).toMatch(/^你修改了对/);
    expect(c.state.kNote?.tone).toBe("ok");
    expect(c.state.kNote?.sub).toBe(WIKI_COPY.refreshFailed);
  });

  it("declare: other_pages_stale go to 「待同步」", async () => {
    const { api, sync, c } = setup();
    await twoPages(c); // p2 is current
    await loadK(c);
    const n = api.calls.length;
    await c.declareCell("c4.procedure", "know"); // only p1 uses c4.procedure
    expect(since(api, n)).toEqual(["declare", "openPage", "catalog", "kGrid"]);
    const runs = sync.runs.length;
    await c.openWikiPage("p1");
    expect(sync.runs.length).toBe(runs + 1);
    expect(sync.runs[runs]?.pageId).toBe("p1");
  });

  it("declare failure: error in kNote, no GET", async () => {
    const { api, c } = setup();
    await loadK(c);
    const n = api.calls.length;
    await c.declareCell("c9.term", "know");
    expect(since(api, n)).toEqual(["declare"]);
    expect(c.state.kNote).toEqual(err("找不到这一页。"));
  });
});

describe("guards", () => {
  it("offline: every action sends nothing; the offline text goes to its area", async () => {
    const { api, conn, c } = setup();
    await twoPages(c);
    await loadK(c);
    conn.emit("offline");
    const n = api.calls.length;
    await c.setReviewPerDay(3);
    expect(c.state.review.note).toEqual(err(WIKI_COPY.offline));
    await c.declareCell("c1.term", "know");
    expect(c.state.kNote).toEqual(err(WIKI_COPY.offline));
    expect(since(api, n)).toEqual([]);
  });

  it("unconfigured: needsSettings, nothing sent", async () => {
    const { api, c } = setup({ configured: false });
    await c.declareCell("c1.term", "know");
    expect(c.state.kNote).toEqual(err(WIKI_COPY.needsSettings));
    expect(methods(api)).toEqual([]);
  });

  it("single-flight: while one write runs, every other write sends nothing", async () => {
    const api = new HeldApi();
    const { c } = setup({ api });
    await twoPages(c);
    await loadK(c);
    const n = api.calls.length;
    const running = c.setReviewPerDay(3);
    expect(c.state.busy).toEqual({ what: "settings", abortable: false });
    await c.setReviewPerDay(4);
    await c.declareCell("c1.term", "know");
    await c.know("s1");
    await c.collapse();
    await c.openWikiPage("p1");
    expect(since(api, n)).toEqual([]); // the held PUT records its call once released
    api.release();
    await running;
    expect(since(api, n)).toEqual(["putSettings", "catalog", "reviewToday"]);
    expect(c.state.busy).toBeNull();
  });
});

describe("最近为你改了什么 (RSI changes)", () => {
  const status = (c: WikiController, id: string): string | undefined => {
    const r = c.state.rsi;
    return r.kind === "ready" ? r.value.find((x) => x.patch_id === id)?.status : undefined;
  };
  async function openCatalog(c: WikiController): Promise<void> {
    await c.init();
    expect([c.state.rsi.kind, c.state.k.kind]).toEqual(["ready", "ready"]);
  }

  it("init GETs the changes once (with K); coming back to 今天 re-reads them with the rest of 今天 (once: the TTL), a second visit does not", async () => {
    const { c, api } = setup();
    await c.init();
    expect(c.state.rsi.kind).toBe("ready");
    expect(methods(api).filter((m) => m === "rsiChanges")).toHaveLength(1);
    c.setTab("assistant");
    c.setTab("today");
    await flush();
    expect(methods(api).filter((m) => m === "rsiChanges")).toHaveLength(2);
    c.setTab("assistant");
    c.setTab("today");
    await flush();
    expect(methods(api).filter((m) => m === "rsiChanges")).toHaveLength(2);
  });

  it("a failed first read shows the error; 重试 (reloadRsi) and 重新连接 read it again", async () => {
    const { c, api } = setup();
    api.failNext("rsiChanges", new ApiError(500, "secret detail"));
    await c.init();
    expect(c.state.rsi.kind).toBe("error");
    expect(c.state.rsi).toEqual({ kind: "error", message: WIKI_COPY.failed });
    await c.reloadRsi();
    expect(c.state.rsi.kind).toBe("ready");
    api.failNext("rsiChanges", new ApiError(503, "x"));
    await c.reloadRsi();
    expect(c.state.rsi.kind).toBe("error");
    await c.retryConnection();
    expect(c.state.rsi.kind).toBe("ready");
  });

  it("unconfigured: 今天 shows needsSettings for the list and sends nothing for it", async () => {
    const { c, api } = setup({ configured: false });
    await c.init();
    expect(c.state.rsi).toEqual({ kind: "error", message: WIKI_COPY.needsSettings });
    expect(methods(api)).not.toContain("rsiChanges");
  });

  it("撤销: POST undo → Notice 撤销了… → re-read (now 已撤销); busy while it runs", async () => {
    const { c, api, notices } = setup();
    await openCatalog(c);
    const n = api.calls.length;
    const p = c.undoPatch("pa2");
    expect(c.state.busy).toEqual({ what: "rsi", sid: "pa2", abortable: false });
    await p;
    expect(since(api, n)).toEqual(["undoPatch", "rsiChanges"]);
    expect(notices).toEqual(["已撤销"]);
    expect(c.state.rsiNote).toBeNull();
    expect(status(c, "pa2")).toBe("rolled_back");
    expect(c.state.busy).toBeNull();
  });

  it("409 (busy / not undoable) → one plain sentence, then a re-read; never the server's detail", async () => {
    const { c, api } = setup();
    await openCatalog(c);
    const n = api.calls.length;
    await c.undoPatch("pb3"); // the fake's 409: its rule is not in the spec
    expect(since(api, n)).toEqual(["undoPatch", "rsiChanges"]);
    expect(c.state.rsiNote).toBeNull();                   // a notice: nothing stays on the row
    await c.undoPatch("pb2"); // rolled back: not undoable
    expect(c.state.rsiNote).toBeNull();
    api.failNext("undoPatch", new ApiError(409, "改进正在进行中"));
    await c.undoPatch("pm1");
    expect(c.state.rsiNote).toBeNull();
    expect(status(c, "pm1")).toBe("personal_accepted");
  });

  it("404 → 这条改动已经不在了。 then a re-read; 5xx (the real undo may answer 500) → a plain retry-later sentence, then a re-read", async () => {
    const { c, api } = setup();
    await openCatalog(c);
    let n = api.calls.length;
    await c.undoPatch("gone");
    expect(since(api, n)).toEqual(["undoPatch", "rsiChanges"]);
    expect(c.state.rsiNote).toEqual({ patchId: "gone", note: err(WIKI_COPY.rsiGone) });
    n = api.calls.length;
    api.failNext("undoPatch", new ApiError(500, "DecisionError: patch pa2 cannot be undone"));
    await c.undoPatch("pa2");
    expect(since(api, n)).toEqual(["undoPatch", "rsiChanges"]);
    expect(c.state.rsiNote).toEqual({ patchId: "pa2", note: err(WIKI_COPY.rsiFailed) });
    expect(status(c, "pa2")).toBe("personal_accepted");
  });

  it("a write that went through but whose re-read failed: the Notice says it worked and a list-level line says the list may be stale", async () => {
    const { c, api, notices } = setup();
    await openCatalog(c);
    api.failNext("rsiChanges", new ApiError(500, "x"));
    await c.undoPatch("pa2");
    expect(notices).toEqual(["已撤销"]);
    expect(c.state.rsiNote).toEqual({ patchId: null, note: err(WIKI_COPY.refreshFailed) });
    expect(c.state.rsi.kind).toBe("ready");
  });

  it("offline or unconfigured: nothing sent, the reason under the change; thrown offline: no re-read", async () => {
    const { c, api, conn } = setup();
    await openCatalog(c);
    let n = api.calls.length;
    conn.state = "offline";
    await c.undoPatch("pm1");
    expect(since(api, n)).toEqual([]);
    expect(c.state.rsiNote).toEqual({ patchId: "pm1", note: err(WIKI_COPY.offline) });
    conn.state = "online";
    n = api.calls.length;
    api.failNext("undoPatch", new OfflineError());
    await c.undoPatch("pm1");
    expect(since(api, n)).toEqual(["undoPatch"]);
    expect(c.state.rsiNote).toEqual({ patchId: "pm1", note: err(WIKI_COPY.offline) });
    expect(c.state.busy).toBeNull();
    const u = setup({ configured: false });
    await u.c.undoPatch("pa2");
    expect(methods(u.api)).toEqual([]);
    expect(u.c.state.rsiNote).toEqual({ patchId: "pa2", note: err(WIKI_COPY.needsSettings) });
  });

  it("demo: 撤销 sends nothing", async () => {
    const { c, api } = setup({ demo: true });
    await openCatalog(c);
    const n = api.calls.length;
    await c.undoPatch("pa2");
    expect(since(api, n)).toEqual([]);
  });

  it("single-flight: while another write runs, 撤销 sends nothing; while one runs, 目录 does not re-read the list", async () => {
    const api = new HeldApi();
    const { c } = setup({ api });
    await twoPages(c);
    await openCatalog(c);
    const n = api.calls.length;
    const running = c.setReviewPerDay(3);
    await c.undoPatch("pa2");
    expect(c.state.busy?.what).toBe("settings");
    expect(since(api, n)).toEqual([]); // the held PUT records its call once released
    api.release();
    await running;
    const m = api.calls.length;
    const p = c.undoPatch("pa2");
    c.setTab("assistant");
    c.setTab("today");
    await c.reloadRsi();
    await p;
    expect(since(api, m)).toEqual(["undoPatch", "rsiChanges"]);
  });
});

it("a catalog request predating a confirmed language change cannot reverse it", async () => {
  const { c, api } = setup(); await c.init(); const old = await api.catalog();
  let release!: (catalog: typeof old) => void;
  api.catalog = () => new Promise((resolve) => { release = resolve; });
  const pending = c.reloadCatalog(); await flush();
  expect(await c.setLanguage("en")).toBeNull(); expect(uiLanguage()).toBe("en");
  release(old); await pending; expect(uiLanguage()).toBe("en");
  expect(c.state.catalog.kind === "ready" && c.state.catalog.value.settings.language).toBe("en");
  c.dispose(); setUiLanguage("zh");
});

it("a focus authority read updates language without replacing the current page", async () => {
 const { c, api } = setup(); await firstPage(c); const page = c.state.page;
 await api.putSettings({ language: "en" }); const n = api.calls.length;
 await c.refreshAccountLanguage(); expect(uiLanguage()).toBe("en"); expect(c.state.page).toBe(page);
 expect(methods(api).slice(n)).toEqual(["getSettings"]); c.dispose(); setUiLanguage("zh");
});
