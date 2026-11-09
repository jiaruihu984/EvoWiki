import { describe, expect, it } from "vitest";
import { AbortedError, ApiError, OfflineError } from "../api/errors";
import type { StreamOpts } from "./api";
import { WIKI_COPY } from "./copy";
import { firstPage, flush, methods, newGoal, setup, until, view } from "./controller-test-kit";
import { FakeWikiApi } from "./fake";
import { PCA_GOAL } from "./fixtures";
import type { OrganizeEvent, PageOut, StepOut } from "./schema";
import { noteSyncLine } from "./state";

describe("init / settings", () => {
  it("init reads the catalog, K, today's review list and the changes (one request each), never a page GET", async () => {
    const { api, c } = setup();
    await c.init();
    expect([...methods(api)].sort()).toEqual(["catalog", "kGrid", "memoryConflicts", "reviewToday", "rsiChanges", "teachSpec"]);
    expect(c.state.catalog.kind).toBe("ready");
    expect(c.state.page).toEqual({ kind: "none" });
  });

  it("not configured: init and writes send nothing and say needsSettings", async () => {
    const { api, c } = setup({ configured: false });
    await c.init();
    expect(api.calls).toEqual([]);
    expect(c.state.catalog).toEqual({ kind: "error", message: WIKI_COPY.needsSettings });
    await expect(c.learnWritePage("p1", "第一页", "g")).rejects.toMatchObject({ name: "PageNotWritten", line: WIKI_COPY.needsSettings });
    expect(api.calls).toEqual([]);
  });

  it("retryConnection re-reads 今天 only when a read errored (what already loaded is not read again)", async () => {
    const { api, c } = setup();
    api.failNext("catalog", new OfflineError());
    await c.init();
    expect(c.state.catalog).toEqual({ kind: "error", message: WIKI_COPY.offline });
    await c.retryConnection();
    expect(c.state.catalog.kind).toBe("ready");
    await c.retryConnection();
    expect(methods(api).filter((m) => m === "catalog")).toHaveLength(2);
    expect(methods(api).filter((m) => m === "reviewToday" || m === "kGrid" || m === "rsiChanges")).toHaveLength(3);
  });

  it("offline → back online: every read that failed (今天, the Bot's lists, 参数) is made again by itself; 重试 then reads nothing that loaded", async () => {
    const { api, conn, c } = setup();
    conn.emit("offline");
    for (const m of ["catalog", "kGrid", "reviewToday", "rsiChanges", "engine", "teachSpec", "prompts"] as const) api.failNext(m, new OfflineError());
    await c.init();
    api.failNext("teachSpec", new OfflineError());   // init read it once already (个性化's dot)
    c.setTab("engine");
    await flush();
    expect([c.state.catalog, c.state.k, c.state.review.today, c.state.rsi, c.state.engine, c.state.prefs, c.state.prompts]
      .map((l) => l.kind)).toEqual(["error", "error", "error", "error", "error", "error", "error"]);
    expect(c.state.bot.recent.kind).toBe("idle");   // the Bot's reads are skipped offline (nothing read)
    api.calls.length = 0;
    conn.emit("online");
    await until(() => c.state.engine.kind === "ready" && c.state.bot.settings.kind === "ready" && c.state.rsi.kind === "ready");
    await flush();
    expect(new Set(methods(api))).toEqual(new Set(["catalog", "kGrid", "reviewToday", "rsiChanges", "engine", "teachSpec", "prompts",
      "memoryConflicts", "botActions", "botRecent", "botSettings", "listSources", "rsiSwitch"]));
    expect([c.state.catalog, c.state.k, c.state.review.today, c.state.prefs, c.state.prompts, c.state.bot.recent,
      c.state.bot.settings].map((l) => l.kind)).toEqual(["ready", "ready", "ready", "ready", "ready", "ready", "ready"]);
    api.calls.length = 0;
    await c.retryConnection();
    expect(methods(api)).toEqual([]);
  });

  it("the Bot's today list skipped while offline is read when the connection comes back (and only then)", async () => {
    const { api, conn, c } = setup();
    await c.init();
    conn.emit("offline");
    for (const m of ["catalog", "reviewToday", "rsiChanges"] as const) api.failNext(m, new OfflineError());
    await c.refreshToday(true);
    expect(c.state.bot.today.kind).toBe("idle");
    expect(methods(api)).not.toContain("botSuggestions");
    conn.emit("online");
    await until(() => c.state.bot.today.kind === "ready");
    api.calls.length = 0;
    await c.retryConnection();
    expect(methods(api)).toEqual([]);
  });

  it("retryConnection runs one reload at a time (a second call while one runs joins it)", async () => {
    const { api, c } = setup();
    api.failNext("catalog", new OfflineError());
    await c.init();
    api.calls.length = 0;
    const a = c.retryConnection();
    const b = c.retryConnection();
    expect(b).toBe(a);
    await a;
    expect(methods(api).filter((m) => m === "catalog")).toHaveLength(1);
  });

  it("init loads K once; switching tabs (old values included) reads nothing more", async () => {
    const { api, c } = setup();
    await c.init();
    c.setTab("k");
    c.setTab("guidelines");
    c.setTab("assistant");
    await flush();
    expect(methods(api).filter((m) => m === "kGrid")).toHaveLength(1);
    expect(c.state.k.kind).toBe("ready");
    expect(c.names.concept("c1")).toBe("z-score 标准化");
  });

  it("conn changes reach the state", () => {
    const { conn, c } = setup();
    let n = 0;
    c.subscribe(() => n++);
    conn.emit("offline");
    expect(c.state.conn).toBe("offline");
    expect(n).toBe(1);
  });
});

describe("a goal's page (the chat's goal run writes it)", () => {
  it("full chain: organize → GET page + catalog → sync → open note", async () => {
    const { api, sync, opened, c } = setup();
    await firstPage(c);
    expect(methods(api)).toEqual(["answerDraft", "organize", "openPage", "catalog", "kGrid"]);
    const v = view(c);
    expect(v.page.page_id).toBe("p1");
    expect(v.page.sections).toHaveLength(5);
    for (const sid of ["s1", "s2", "s3", "s4"]) expect(v.page.sections.find((s) => s.id === sid)?.body).not.toBe("");
    expect(v.streaming).toEqual({});
    expect(v.status).toEqual({ text: "整页组织完成", tone: "ok" });
    expect(sync.runs).toHaveLength(1);
    expect(sync.runs[0]?.pageId).toBe("p1");
    expect(sync.finds).toEqual(["p1"]);
    expect(opened).toEqual(["Wiki/p1.md"]);
    expect(v.noteSync).toEqual({ kind: "done", result: "created" });
    expect(c.state.tab).toBe("today");   // opening a page leaves the tab alone
    expect(c.state.busy).toBeNull();
    expect(c.names.page("p1")).not.toBeNull();

    await c.openWikiPage("p1");
    expect(methods(api).slice(5)).toEqual(["openPage"]);
    expect(sync.runs).toHaveLength(1);
    expect(sync.finds).toEqual(["p1", "p1"]);
    expect(opened).toEqual(["Wiki/p1.md", "Wiki/p1.md"]);
  });

  it("a second goal's page: the organize chain", async () => {
    const { api, c, opened } = setup();
    await firstPage(c);
    const n = api.calls.length;
    await newGoal(c, PCA_GOAL);
    expect(methods(api).slice(n)).toEqual(["organize", "openPage", "catalog", "kGrid"]);
    expect(view(c).page.page_id).toBe("p2");
    expect(view(c).page.goal).toBe(PCA_GOAL);
    expect(opened[opened.length - 1]).toBe("Wiki/p2.md");
    // no catalog note from the stream
    expect(c.state.catalogNote).toBeNull();
  });
});

describe("organize stream", () => {
  it("single-flight: while the stream runs, another page write / organize send nothing", async () => {
    const { api, c } = setup({ delayMs: 15 });
    const p = firstPage(c);
    await until(() => c.state.busy?.abortable === true);
    const n = api.calls.length;
    await expect(c.learnWritePage("p1", "第一页", "g")).rejects.toMatchObject({ name: "PageNotWritten" });
    await c.organize();
    expect(api.calls.length).toBe(n);
    await p;
    expect(c.state.busy).toBeNull();
  });

  it("stream error event: localized text + retry, no auto retry, detail never shown", async () => {
    const { api, sync, c } = setup();
    api.failNext("organize", { code: "llm_failed", retryable: true, detail: "secret-detail" });
    await firstPage(c);
    expect(methods(api)).toEqual(["answerDraft", "organize"]);
    const v = view(c);
    expect(v.status).toEqual({ text: "请切换模型重试", tone: "error", retry: { kind: "organize" } });
    expect(v.page.organized).toBe(false);
    expect(JSON.stringify(c.state)).not.toContain("secret-detail");
    expect(sync.runs).toEqual([]);
    expect(c.state.busy).toBeNull();
    await c.organize();
    expect(methods(api)).toEqual(["answerDraft", "organize", "organize", "openPage", "catalog", "kGrid"]);
    expect(view(c).page.organized).toBe(true);
    expect(sync.runs).toHaveLength(1);
  });

  it("organize() also GETs K when K was loaded, and does not open the note", async () => {
    const { api, opened, c } = setup();
    await firstPage(c);
    c.setTab("k");
    await flush();
    const n = api.calls.length;
    await c.organize();
    expect(methods(api).slice(n)).toEqual(["organize", "openPage", "catalog", "kGrid"]);
    expect(opened).toEqual(["Wiki/p1.md"]);
  });

  it("a stream that ends without done/error is a failure (retry, no GET, no sync)", async () => {
    class Silent extends FakeWikiApi {
      silent = false;
      override organize(pageId: string, on: (e: OrganizeEvent) => void, o?: StreamOpts): Promise<void> {
        if (!this.silent) return super.organize(pageId, on, o);
        this.calls.push({ method: "organize", args: [pageId] });
        return Promise.resolve();
      }
    }
    const api = new Silent();
    const { sync, c } = setup({ api });
    await firstPage(c);
    api.silent = true;
    const n = api.calls.length;
    await c.organize();
    expect(methods(api).slice(n)).toEqual(["organize"]);
    expect(view(c).status).toEqual({ text: WIKI_COPY.failed, tone: "error", retry: { kind: "organize" } });
    expect(sync.runs).toHaveLength(1);
  });

  it("stop(): aborts the stream, no error shown, then one page GET", async () => {
    const { api, c } = setup({ delayMs: 15 });
    const p = firstPage(c);
    await until(() => c.state.busy?.abortable === true && c.state.page.kind === "open");
    await c.stop();
    await p;
    expect(methods(api)).toEqual(["answerDraft", "organize", "cancelRun", "openPage"]);
    const v = view(c);
    expect(v.status).toBeNull();
    expect(v.streaming).toEqual({});
    expect(c.state.busy).toBeNull();
  });

  it("dispose aborts the stream and ignores later results", async () => {
    const { api, opened, c } = setup({ delayMs: 15 });
    const p = firstPage(c);
    await until(() => c.state.busy?.abortable === true);
    const before = c.state;
    c.dispose();
    await p;
    expect(c.state).toBe(before);
    expect(methods(api)).toEqual(["answerDraft", "organize"]);
    expect(opened).toEqual([]);
  });

  it("organize with no open page does nothing", async () => {
    const { api, c } = setup();
    await c.organize();
    expect(api.calls).toEqual([]);
  });

  it("offline: organize sends nothing and says offline on the status line", async () => {
    const { api, conn, c } = setup();
    await firstPage(c);
    conn.state = "offline";
    const n = api.calls.length;
    await c.organize();
    expect(api.calls.length).toBe(n);
    expect(view(c).status).toEqual({ text: WIKI_COPY.offline, tone: "error" });
  });
});

describe("open page", () => {
  const blockStep: StepOut = { kind: "enter_block", goal_id: "m1", goal: "化学", map_id: "m1", region_id: "r2",
    region_name: "酸碱", path_id: null, index: null, page_id: null, title: "", draft_id: null, progress: null, failure: null, review_first: null };

  it("continuing a block awaits its restored goal run and preserves the requested region", async () => {
    let release!: () => void;
    const waiting = new Promise<void>((resolve) => { release = resolve; });
    const calls: unknown[] = [];
    const { c } = setup({ continueGoal: async (...args) => { calls.push(args); await waiting; } });
    let finished = false;
    const next = c.followStep(blockStep).then(() => { finished = true; });
    await Promise.resolve();
    expect(calls).toEqual([["m1", "r2"]]);
    expect(finished).toBe(false);
    release();
    await next;
    expect(finished).toBe(true);
  });

  it("a failed continuation reports its error, and offline continuation sends nothing", async () => {
    let calls = 0;
    const { c, notices, conn } = setup({ continueGoal: () => { calls++; return Promise.reject(new Error("无法恢复学习目标")); } });
    await c.followStep(blockStep);
    expect(notices).toEqual([WIKI_COPY.failed]);
    conn.state = "offline";
    await c.followStep(blockStep);
    expect(calls).toBe(1);
    expect(notices[notices.length - 1]).toBe(WIKI_COPY.offline);
  });

  it("continuing while another write is running gives a visible busy reason", async () => {
    const { c, notices } = setup();
    c.state.busy = { what: "organize", abortable: true };
    await c.followStep(blockStep);
    expect(notices).toEqual([WIKI_COPY.reviewBlockedBusy]);
  });

  it("continuing without another page opens 学点新的, including completed server steps", async () => {
    const { c, learned } = setup();
    await c.continueLearning();
    await c.followStep({ ...blockStep, kind: "none" });
    await c.followStep({ ...blockStep, kind: "goal_done" });
    expect(learned).toEqual(["", "", ""]);
  });

  it("switching pages: the older result never lands", async () => {
    const { opened, c } = setup();
    await firstPage(c);
    await newGoal(c, PCA_GOAL);
    opened.length = 0;
    const a = c.openWikiPage("p1");
    const b = c.openWikiPage("p2");
    await Promise.all([a, b]);
    expect(view(c).page.page_id).toBe("p2");
    expect(opened).toEqual(["Wiki/p2.md"]);
  });

  it("a page not written yet: opening writes nothing; with no note of a write under way its 开始生成 tab shows", async () => {
    const { api, sync, c, startPages, opened, notices } = setup();
    const d = api.__goal("g");
    await api.answerDraft(d.draft_id ?? "", { answers: null, skip: true });
    const n = api.calls.length;
    sync.find = (id: string) => { sync.finds.push(id); return Promise.resolve(null); };
    await c.openWikiPage("p1");
    expect(methods(api).slice(n)).toEqual(["openPage", "kGrid"]);       // no organize
    expect(view(c).page.organized).toBe(false);
    expect(c.state.tab).toBe("today");   // opening a page leaves the tab alone
    expect(startPages).toEqual([{ pageId: "p1", title: view(c).page.title, goal: view(c).page.goal }]);
    expect(opened).toEqual([]);
    api.failNext("organize", { code: "llm_failed", retryable: true, detail: "" });
    await c.startPage("p1", "第一页", "g");                               // 开始生成, failed: said, not silent
    expect(notices.length).toBe(1);
    expect(view(c).page.organized).toBe(false);
    await c.startPage("p1", "第一页", "g");                               // 开始生成
    expect(methods(api).slice(n)).toContain("organize");
    expect(view(c).page.organized).toBe(true);
  });

  it("继续学习 on a page not written yet (start_page / failed) writes it; on a page to read it only opens", async () => {
    const { api, c } = setup();
    const d = api.__goal("g");
    await api.answerDraft(d.draft_id ?? "", { answers: null, skip: true });
    const step = { goal_id: null, goal: "g", map_id: null, region_id: null, region_name: "", path_id: null, index: null,
      page_id: "p1", title: "第一页", draft_id: null, progress: null, failure: null, review_first: null };
    let n = api.calls.length;
    await c.followStep({ ...step, kind: "start_page" });
    expect(methods(api).slice(n)).toContain("organize");
    expect(view(c).page.organized).toBe(true);
    n = api.calls.length;
    await c.followStep({ ...step, kind: "read" });
    expect(methods(api).slice(n)).not.toContain("organize");
  });

  it("a failed GET → page error", async () => {
    const { c } = setup();
    await c.openWikiPage("nope");
    expect(c.state.page).toEqual({ kind: "error", pageId: "nope", message: "找不到这一页。" });
  });

  it("GET with a diff syncs (A3-3); the same content again does not", async () => {
    const { api, sync, c } = setup();
    await firstPage(c);
    await newGoal(c, PCA_GOAL);
    expect(sync.runs).toHaveLength(2);
    await api.declare("c1.interpret", "know"); // current page is p2 → p1 becomes stale
    await c.openWikiPage("p1");
    expect(view(c).page.diff).not.toBeNull();
    expect(sync.runs).toHaveLength(3);
    await c.openWikiPage("p1");
    expect(sync.runs).toHaveLength(3);
  });

  it("openCurrentNote: find + open; no note → it is written (the status line keeps its text)", async () => {
    const { sync, opened, c } = setup();
    await firstPage(c);
    c.openCurrentNote();
    await flush();
    expect(opened).toEqual(["Wiki/p1.md", "Wiki/p1.md"]);
    const status = view(c).status;
    sync.find = (id: string) => { sync.finds.push(id); return Promise.resolve(null); };
    const runs = sync.runs.length;
    c.openCurrentNote();
    await flush();
    expect(sync.runs).toHaveLength(runs + 1);   // no note in the vault: it is written
    expect(view(c).status).toEqual(status);
  });
});

describe("note sync", () => {
  it("serial: a slow sync with two more landings runs at most twice, the second with the latest content", async () => {
    class Bumping extends FakeWikiApi {
      n = 0;
      override async openPage(pageId: string): Promise<PageOut> {
        const p = await super.openPage(pageId);
        return { ...p, title: `${p.title}#${++this.n}` };
      }
    }
    const { sync, c } = setup({ api: new Bumping() });
    sync.hold = true;
    const p0 = firstPage(c);
    await until(() => sync.pending === 1);
    expect(c.state.busy).toBeNull();
    const p1 = c.organize();
    await until(() => c.state.busy === null && view(c).page.title.endsWith("#2"));
    const p2 = c.organize();
    await until(() => c.state.busy === null && view(c).page.title.endsWith("#3"));
    expect(view(c).noteSync).toEqual({ kind: "syncing" });
    sync.hold = false;
    sync.release();
    await Promise.all([p0, p1, p2]);
    expect(sync.runs).toHaveLength(2);
    expect(sync.runs[1]?.title).toMatch(/#3$/);
    expect(view(c).noteSync).toEqual({ kind: "done", result: "created" });
  });

  it("user_edited / backup_failed / thrown → status-area lines, no retry", async () => {
    const { sync, c } = setup();
    sync.result = "user_edited";
    await firstPage(c);
    expect(noteSyncLine(view(c).noteSync)).toEqual({ text: WIKI_COPY.noteUserEdited, tone: "error" });
    expect(view(c).status?.text).toBe("整页组织完成");

    const s2 = setup();
    s2.sync.result = "backup_failed";
    await firstPage(s2.c);
    expect(noteSyncLine(view(s2.c).noteSync)?.text).toBe(WIKI_COPY.noteBackupFailed);

    const s3 = setup();
    s3.sync.throwNext = true;
    await firstPage(s3.c);
    expect(view(s3.c).noteSync).toEqual({ kind: "error" });
    expect(noteSyncLine(view(s3.c).noteSync)?.text).toBe(WIKI_COPY.noteSyncFailed);
    expect(s3.sync.runs).toHaveLength(1);
  });

  it("demo (sync null): never syncs, never looks up or opens a note", async () => {
    const { sync, opened, c } = setup({ demo: true });
    await firstPage(c);
    await c.openWikiPage("p1");
    c.openCurrentNote();
    await flush();
    expect(sync.runs).toEqual([]);
    expect(sync.finds).toEqual([]);
    expect(opened).toEqual([]);
    expect(c.state.demo).toBe(true);
    expect(view(c).noteSync).toEqual({ kind: "idle" });
  });
});

describe("fix round 1", () => {
  it("I1: openWikiPage while a stream is in flight does nothing; the stream's results and GETs still land", async () => {
    const { api, sync, c } = setup({ delayMs: 15 });
    await firstPage(c);
    await newGoal(c, PCA_GOAL); // p2 open
    await c.openWikiPage("p1");
    const p = c.organize();
    await until(() => c.state.busy?.abortable === true);
    const n = api.calls.length;
    let notified = 0;
    const off = c.subscribe(() => notified++);
    const before = c.state.page;
    await c.openWikiPage("p1");
    await c.openWikiPage("p2");
    expect(api.calls.length).toBe(n);
    expect(c.state.page).toBe(before);
    off();
    expect(notified).toBe(0);
    const runs = sync.runs.length;
    await p;
    expect(methods(api).slice(n - 1)).toEqual(["organize", "openPage", "catalog", "kGrid"]);
    expect(view(c).page.page_id).toBe("p1");
    expect(view(c).status?.text).toMatch(/^整页组织完成/);
    expect(sync.runs.length).toBe(runs + 1);
  });

  it("M1: stop before the outline + a failed GET ends in page error, not loading", async () => {
    class Hang extends FakeWikiApi {
      override organize(pageId: string, _on: (e: OrganizeEvent) => void, o?: StreamOpts): Promise<void> {
        this.calls.push({ method: "organize", args: [pageId] });
        return new Promise((_r, reject) => o?.signal?.addEventListener("abort", () => reject(new AbortedError())));
      }
    }
    const api = new Hang();
    const { c } = setup({ api });
    const p = firstPage(c);
    await until(() => c.state.busy?.abortable === true);
    expect(c.state.page).toEqual({ kind: "loading", pageId: "p1" });
    api.failNext("openPage", new ApiError(500, "x"));
    await c.stop();
    await p;
    expect(methods(api)).toEqual(["answerDraft", "organize", "cancelRun", "openPage"]);
    expect(c.state.page).toEqual({ kind: "error", pageId: "p1", message: WIKI_COPY.failed });
    expect(c.state.busy).toBeNull();
  });

  it("M2: once the stream has ended, busy is no longer abortable during the follow-up GETs", async () => {
    class HoldGet extends FakeWikiApi {
      hold = false;
      waiting = false;
      release: () => void = () => undefined;
      override async openPage(pageId: string): Promise<PageOut> {
        if (this.hold) {
          this.waiting = true;
          await new Promise<void>((r) => { this.release = r; });
        }
        return super.openPage(pageId);
      }
    }
    const api = new HoldGet();
    const { c } = setup({ api });
    await firstPage(c);
    api.hold = true;
    const p = c.organize();
    await until(() => api.waiting);
    expect(c.state.busy).toEqual({ what: "organize", abortable: false });
    api.release();
    await p;
    expect(c.state.busy).toBeNull();
  });
});
