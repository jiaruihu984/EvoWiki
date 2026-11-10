/**
 * Final-review fixes: a failed refresh GET after a successful write keeps the write's result line (Important 1);
 * 「写入库里」 for an organized page without a note (Important 2) and noteMissing in the note-sync area (Task 6);
 * a stream transport that resolves normally after an abort counts as stopped (Task 7 hardening).
 */
import { describe, expect, it, vi } from "vitest";
import { ApiError, OfflineError } from "../api/errors";
import type { StreamOpts } from "./api";
import { WIKI_COPY } from "./copy";
import { REFRESH_RETRY_MS } from "./controller/reads";
import { firstPage, flush, methods, setup, until, view, type Setup } from "./controller-test-kit";
import { FakeWikiApi } from "./fake";
import type { OrganizeEvent, PageOut } from "./schema";
import { type ResultNote } from "./state";
import { clearEwNotices, ewNotices } from "../../test/ew-notices";

const FAILED = new ApiError(500, "x");
const offline = (): OfflineError => new OfflineError();
const withSub = (n: ResultNote | null | undefined, sub: string): ResultNote => {
  if (!n) throw new Error("no main note");
  return { ...n, sub };
};
/** No note of the open page shows the offline (「…没有发送。」) text. */
function neverOffline(s: Setup): void {
  const v = view(s.c);
  const notes = [v.status, ...Object.values(v.sectionNotes), s.c.state.catalogNote, s.c.state.kNote];
  for (const n of notes) expect(n?.text).not.toBe(WIKI_COPY.offline);
}

describe("Important 1: a failed refresh GET keeps the write's result line", () => {
  it("note progress reads the actual streamed section counts before notifying listeners and clears at the end", async () => {
    const s = setup();
    const ticks: { section: number | null; of: number | null }[] = [];
    const off = s.c.onWriteProgress((p) => {
      if (p.ended) return;
      const progress = s.c.noteWriteProgress(p.pageId)!;
      expect(progress.section).toBe(p.done);
      expect(progress.of).toBe(p.of);
      ticks.push(progress);
    });
    await firstPage(s.c);
    off();
    expect(ticks.length).toBeGreaterThan(1);
    expect(ticks[0]?.section).toBe(0);
    expect(ticks.some((p) => p.section !== null && p.section > 0)).toBe(true);
    expect(s.c.noteWriteProgress("p1")).toBeUndefined();
    s.c.dispose();
  });

  it("background note progress reads each server snapshot without offering local run controls", () => {
    const s = setup();
    const write = { page_id: "background", run_id: "r1", kind: "organize" as const, state: "writing" as const, pass: 2, section: 1, of: 4, started_at: null, cost_usd: 0 };
    const snapshot = vi.spyOn(s.c, "writesSnapshot").mockReturnValue({ at: 1, writes: [write] });
    expect(s.c.noteWriteProgress("background")).toEqual({ pass: 2, section: 1, of: 4 });
    expect(s.c.writeRun("background")).toBeNull();
    snapshot.mockReturnValue({ at: 2, writes: [{ ...write, section: 3 }] });
    expect(s.c.noteWriteProgress("background")?.section).toBe(3);
    snapshot.mockReturnValue({ at: 3, writes: [] });
    expect(s.c.noteWriteProgress("background")).toBeUndefined();
    s.c.dispose();
  });

  it("每天最多: catalog GET offline → 已保存 stays, refreshOffline below it", async () => {
    const ref = setup();
    await firstPage(ref.c);
    await ref.c.setReviewPerDay(5);
    const s = setup();
    await firstPage(s.c);
    s.api.failNext("catalog", offline());
    await s.c.setReviewPerDay(5);
    expect(s.c.state.review.note).toEqual(withSub(ref.c.state.review.note, WIKI_COPY.refreshOffline));
    expect(s.c.state.busy).toBeNull();
    neverOffline(s);
  });

  it("a failed refresh (not offline) is tried once more in the background; when it answers, the refresh line goes", async () => {
    const ref = setup();
    await firstPage(ref.c);
    await ref.c.know("s3");
    const s = setup();
    await firstPage(s.c);
    s.api.failNext("openPage", FAILED);
    vi.useFakeTimers({ toFake: ["setTimeout"] });
    try {
      await s.c.know("s3");
      expect(view(s.c).sectionNotes.s3).toEqual(withSub(view(ref.c).sectionNotes.s3, WIKI_COPY.refreshFailed));
      await vi.advanceTimersByTimeAsync(REFRESH_RETRY_MS);
    } finally {
      vi.useRealTimers();
    }
    await until(() => view(s.c).sectionNotes.s3?.sub === undefined);
    expect(view(s.c).sectionNotes.s3).toEqual(view(ref.c).sectionNotes.s3);
  });

  it("declare: K re-GET fails → kNote keeps the declare line, refreshFailed below", async () => {
    const ref = setup();
    await ref.c.init();
    ref.c.setTab("k");
    await until(() => ref.c.state.k.kind === "ready");
    await ref.c.declareCell("c1.interpret", "know");
    const s = setup();
    await s.c.init();
    s.c.setTab("k");
    await until(() => s.c.state.k.kind === "ready");
    s.api.failNext("kGrid", FAILED);
    await s.c.declareCell("c1.interpret", "know");
    expect(s.c.state.kNote).toEqual(withSub(ref.c.state.kNote, WIKI_COPY.refreshFailed));
  });

  it("know: page GET fails → the section note stays with refreshFailed; status untouched", async () => {
    const ref = setup();
    await firstPage(ref.c);
    await ref.c.know("s3");
    const s = setup();
    await firstPage(s.c);
    const status = view(s.c).status;
    s.api.failNext("openPage", FAILED);
    await s.c.know("s3");
    expect(view(s.c).sectionNotes.s3).toEqual(withSub(view(ref.c).sectionNotes.s3, WIKI_COPY.refreshFailed));
    expect(view(s.c).status).toEqual(status);
    neverOffline(s);
  });

  it("know: catalog GET offline → refreshOffline on the section note; the catalog note is untouched", async () => {
    const ref = setup();
    await firstPage(ref.c);
    await ref.c.know("s3");
    const s = setup();
    await firstPage(s.c);
    const cat = s.c.state.catalogNote;
    s.api.failNext("catalog", offline());
    await s.c.know("s3");
    expect(view(s.c).sectionNotes.s3).toEqual(withSub(view(ref.c).sectionNotes.s3, WIKI_COPY.refreshOffline));
    expect(s.c.state.catalogNote).toEqual(cat);
    neverOffline(s);
  });

  it("done: page GET offline → the done line stays on the status line, refreshOffline below", async () => {
    const ref = setup();
    await firstPage(ref.c);
    await ref.c.done();
    const s = setup();
    await firstPage(s.c);
    s.api.failNext("openPage", offline());
    await s.c.done();
    expect(view(s.c).status).toEqual(withSub(view(ref.c).status, WIKI_COPY.refreshOffline));
    neverOffline(s);
  });

  it("reorganize done: page GET fails → organizeDone stays, refreshFailed below", async () => {
    const s = setup();
    await firstPage(s.c);
    s.api.failNext("openPage", FAILED);
    await s.c.organize();
    const st = view(s.c).status;
    expect(st?.text).toMatch(/^整页组织完成/);
    expect(st?.sub).toBe(WIKI_COPY.refreshFailed);
    expect(st?.tone).toBe("ok");
  });
});

/** sync.find never finds a note. */
function noNote(s: Setup): void {
  s.sync.find = (id: string) => { s.sync.finds.push(id); return Promise.resolve(null); };
}

describe("a page with no note in this vault: opening it writes the note, then opens it", () => {
  /** sync.find finds nothing until a sync.run wrote the note. */
  function lateNote(s: Setup): void {
    s.sync.find = (id: string) => { s.sync.finds.push(id); return Promise.resolve(s.sync.runs.some((r) => r.pageId === id) ? `Wiki/${id}.md` : null); };
  }

  it("one local sync of the page (no server request beyond the GET), then the note is opened", async () => {
    const s = setup();
    await firstPage(s.c);
    s.sync.runs.length = 0;
    lateNote(s);
    s.opened.length = 0;
    const calls = s.api.calls.length;
    await s.c.openWikiPage("p1");
    await flush();
    expect(s.sync.runs.map((r) => r.pageId)).toEqual(["p1"]);
    expect(s.opened).toEqual(["Wiki/p1.md"]);
    expect(view(s.c).noteSync).toEqual({ kind: "done", result: "created" });
    expect(s.api.calls.length).toBe(calls + 1);
  });

  it("a failed write says so in a notice; nothing is opened", async () => {
    const s = setup();
    await firstPage(s.c);
    noNote(s);
    s.opened.length = 0;
    s.sync.throwNext = true;
    await s.c.openWikiPage("p1");
    await flush();
    expect(view(s.c).noteSync).toEqual({ kind: "error" });
    expect(s.opened).toEqual([]);
    expect(s.notices).toContain(WIKI_COPY.noteSyncFailed);
  });

  it("nothing in demo", async () => {
    const d = setup({ demo: true });
    await firstPage(d.c);
    noNote(d);
    await d.c.openWikiPage("p1");
    expect(d.sync.runs).toEqual([]);
  });
});

describe("Task 7 hardening: a stream that resolves normally after abort is a stop", () => {
  it("organize: no error, page re-read", async () => {
    class Quiet extends FakeWikiApi {
      quiet = false;
      override organize(pageId: string, on: (e: OrganizeEvent) => void, o: StreamOpts = {}): Promise<void> {
        if (!this.quiet) return super.organize(pageId, on, o);
        this.calls.push({ method: "organize", args: [pageId] });
        return new Promise((r) => o.signal?.addEventListener("abort", () => r()));
      }
    }
    const api = new Quiet();
    const s = setup({ api });
    await firstPage(s.c);
    api.quiet = true;
    const p = s.c.organize();
    await until(() => s.c.state.busy?.abortable === true);
    await s.c.stop();
    await p;
    expect(view(s.c).status).toBeNull();
    expect(methods(api).slice(-3)).toEqual(["organize", "cancelRun", "openPage"]);
    expect(s.c.state.busy).toBeNull();
  });

});

describe("「停止」 cancels the page's run on the server; leaving the page does not", () => {
  class Held extends FakeWikiApi {
    held = false;
    cancelError: Error | null = null;
    override organize(pageId: string, on: (e: OrganizeEvent) => void, o: StreamOpts = {}): Promise<void> {
      if (!this.held) return super.organize(pageId, on, o);
      this.calls.push({ method: "organize", args: [pageId] });
      return new Promise((r) => o.signal?.addEventListener("abort", () => r()));
    }
    override cancelRun(pageId: string): Promise<void> {
      if (this.cancelError === null) return super.cancelRun(pageId);
      this.calls.push({ method: "cancelRun", args: [pageId] });
      return Promise.reject(this.cancelError);
    }
  }

  it("a cancel that fails keeps following the run and says why; one the server no longer has just stops", async () => {
    clearEwNotices();
    const api = new Held();
    const s = setup({ api });
    await firstPage(s.c);
    api.held = true;
    const p = s.c.organize();
    await until(() => s.c.state.busy?.abortable === true);
    api.cancelError = new ApiError(503, "请切换模型重试");
    await s.c.stop();
    expect(s.c.state.busy?.abortable).toBe(true);
    expect(methods(api).slice(-1)).toEqual(["cancelRun"]);
    expect(ewNotices()).toHaveLength(1);
    api.cancelError = new ApiError(404, "这一页现在没有在写");
    await s.c.stop();
    await p;
    expect(methods(api).slice(-2)).toEqual(["cancelRun", "openPage"]);
    expect(s.c.state.busy).toBeNull();
  });

  it("停止 during a page's first write: the streamed file goes (the page holds nothing yet)", async () => {
    const s = setup({ delayMs: 15 });
    const p = firstPage(s.c);
    await until(() => s.sync.streamed.length > 0 && s.c.state.busy?.abortable === true);
    await s.c.stop();
    await p;
    for (let n = 0; n < 10; n++) await flush();
    expect(methods(s.api)).toContain("cancelRun");
    expect(s.sync.runs[s.sync.runs.length - 1]?.trashed).toBe(true);
  });

  it("停止 → the write reads stopped (writeRun); 重新写 writes the page again and clears it", async () => {
    const api = new Held();
    const s = setup({ api });
    await firstPage(s.c);
    api.held = true;
    const p = s.c.organize();
    await until(() => s.c.state.busy?.abortable === true);
    expect(s.c.writingPage()).toBe("p1");
    expect(s.c.writeRun("p1")).toBe("writing");
    expect(s.c.writeRun("p2")).toBeNull();
    await s.c.stop();
    await p;
    expect(s.c.writeRun("p1")).toBe("stopped");
    expect(s.c.state.stopped?.pageId).toBe("p1");
    api.held = false;
    const before = methods(api).filter((m) => m === "organize").length;
    await s.c.restart("p1");
    expect(methods(api).filter((m) => m === "organize").length).toBe(before + 1);
    expect(s.c.state.stopped).toBeNull();
    expect(s.c.writeRun("p1")).toBeNull();
  });

  it("停止 on a first write, then 重新写: the page is written as a new page again (its note opens)", async () => {
    const s = setup({ delayMs: 15 });
    const p = firstPage(s.c);
    await until(() => s.sync.streamed.length > 0 && s.c.state.busy?.abortable === true);
    const pid = s.c.writingPage()!;
    await s.c.stop();
    await p;
    expect(s.c.writeRun(pid)).toBe("stopped");
    await s.c.restart(pid);
    for (let n = 0; n < 10; n++) await flush();
    expect(s.c.writeRun(pid)).toBeNull();
    expect(s.c.state.page.kind === "open" && s.c.state.page.page.organized).toBe(true);
    expect(s.sync.runs[s.sync.runs.length - 1]?.trashed).not.toBe(true);
  });

  it("closing the view mid-write aborts the stream without cancelling the run", async () => {
    const api = new Held();
    const s = setup({ api });
    await firstPage(s.c);
    api.held = true;
    const p = s.c.organize();
    await until(() => s.c.state.busy?.abortable === true);
    s.c.dispose();
    await p;
    expect(methods(api)).not.toContain("cancelRun");
  });
});

describe("an existing note is re-rendered when its page is first viewed this session", () => {
  it("the first view syncs it once (an older renderer's text is rewritten); later views of the same page do not", async () => {
    const earlier = setup();
    await firstPage(earlier.c);                                   // the page and its note, written in an earlier session
    const s = setup({ api: earlier.api });
    s.sync.find = (id: string) => { s.sync.finds.push(id); return Promise.resolve(`Wiki/${id}.md`); };
    await s.c.openWikiPage("p1"); await flush();
    expect(s.sync.runs.map((r) => r.pageId)).toEqual(["p1"]);
    await s.c.openWikiPage("p1"); await flush();
    expect(s.sync.runs).toHaveLength(1);
  });
});

describe("final review #15: a material deleted since re-syncs an existing note on open", () => {
  const src = { id: "s1", title: "讲义", current_title: "讲义", kind: "pdf" as const, origin: "EvoWiki/资料/讲义.pdf", deleted: false };
  class WithSource extends FakeWikiApi {
    deleted = false;
    current = "讲义";
    override async openPage(pageId: string): Promise<PageOut> {
      return { ...(await super.openPage(pageId)), sources: [{ ...src, current_title: this.current, deleted: this.deleted }] };
    }
  }

  it("opening after the delete syncs the note once; a page without a note gets one", async () => {
    const api = new WithSource();
    const s = setup({ api });
    await firstPage(s.c);
    await s.c.openWikiPage("p1");
    const runs = s.sync.runs.length;
    await s.c.openWikiPage("p1");
    expect(s.sync.runs).toHaveLength(runs);                      // nothing deleted: plain viewing never syncs
    api.deleted = true;
    await s.c.openWikiPage("p1"); await flush();
    expect(s.sync.runs).toHaveLength(runs + 1);
    expect(s.sync.runs[runs]!.sources?.[0]?.deleted).toBe(true);
    await s.c.openWikiPage("p1"); await flush();
    expect(s.sync.runs).toHaveLength(runs + 1);                  // once per change
    noNote(s);
    api.current = "讲义（新）";                                    // another input, still a deleted material
    await s.c.openWikiPage("p1"); await flush();
    expect(s.sync.finds[s.sync.finds.length - 1]).toBe("p1");
    expect(s.sync.runs).toHaveLength(runs + 2);                  // no note in the vault: opening writes it
  });
});

describe("the wiki changed elsewhere (a chat goal, a background write, a merge settling, a path edit, 今天 shown)", () => {
  it("one GET /wiki/progress and one files pass (trashed pages go to the trash) for a burst; nothing before the pause", async () => {
    const s = setup();
    await s.c.init();
    const reads = s.api.progressReads;
    const files = vi.spyOn(s.c, "reconcileFiles");
    vi.useFakeTimers({ toFake: ["setTimeout"] });
    try {
      s.c.wikiChanged(); s.c.wikiChanged(); s.c.wikiChanged();
      expect(s.api.progressReads).toBe(reads);
      await vi.advanceTimersByTimeAsync(400);
    } finally {
      vi.useRealTimers();
    }
    await until(() => s.api.progressReads === reads + 1);
    await flush();
    expect(files).toHaveBeenCalledOnce();
    expect(s.api.progressReads).toBe(reads + 1);
  });
});
