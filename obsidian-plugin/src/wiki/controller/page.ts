/**
 * The open page and its actions (P4: results next to what produced them): open / follow a page, the lazy RSI rewrite
 * watch, 生成这一页 / 重新组织, the section actions (这部分我懂, 再讲深一点 / 换个讲法, 折叠, 钉住, 确认题), 问, 学完了, 恢复成完整页,
 * the 「⋯」 menu's feedback and 问助手, 写入库里 and the jump to a section.
 */
import { ApiError } from "../../api/errors";
import { ewNotice } from "../../ui/ew-notice";
import { graphBus } from "../graph-page/bus";
import { WIKI_COPY } from "../copy";
import { sectionQuote } from "../section-quote";
import { errorText, opLine, opResult } from "../format";
import { findSectionLine } from "../page-toc";
import type { CheckAnswerOut, FeedbackChip, FeedbackOut, PageOut, SectionOut, ShelveOut } from "../schema";
import type { RetryAction } from "../state";
import {
  type Spot, type Ctx, type Seed, REWRITE_STEP_MS, REWRITE_MAX_MS, ok, bad,
  changeMarks, without,
} from "./helpers";
import type { WikiCore } from "./core";
import type { Notes } from "./notes";
import type { Reads } from "./reads";
import type { Streams } from "./streams";

export class PageActions {
  constructor(private readonly core: WikiCore, private readonly notes: Notes, private readonly reads: Reads, private readonly streams: Streams) {}

  /** GET the page; sync if A3 says so; then find and open its note. Never organizes. */
  async openWikiPage(pageId: string): Promise<void> {
    await this.loadPage(pageId, false);
  }

  /** The page is about to open from this goal (a learning path, the chat's run): the next load of another page ends it. */
  pageFromGoal(pageId: string, mapId: string): void {
    this.core.set({ pageGoal: { pageId, mapId } });
  }

  /**
   * A page row of 学点新的: its note, also while a page is being written (loadPage does nothing then, by its ruling): the
   * note on disk opens as it is, and a page with no note yet says so.
   */
  async openPageRow(pageId: string): Promise<void> {
    if (!this.core.state.busy) { await this.openWikiPage(pageId); return; }
    let path: string | null = null;
    try { path = (await this.core.deps.sync?.find(pageId)) ?? null; } catch { path = null; }
    if (path === null || !this.core.deps.openNote(path)) this.core.deps.notice?.(WIKI_COPY.noteNotYet);
  }

  /**
   * A page link in a wiki note's text (note-links.ts; app.js `a.inl[data-open]`): openWikiPage. The click has no line of
   * its own to show a failure on, so a click that cannot open the page says why in a notice (busy, or the GET's error,
   * e.g. a page that does not exist); a page not written yet opens its 开始生成 tab (loadPage).
   */
  async openPageLink(pageId: string): Promise<void> {
    if (this.core.state.busy) { this.core.deps.notice?.(WIKI_COPY.attnBlockedBusy); return; }
    await this.openWikiPage(pageId);
    const s = this.core.state.page;
    if (s.kind === "error" && s.pageId === pageId) this.core.deps.notice?.(s.message);
  }

  /** 开始生成 on a page not written yet (start-page-view.ts): written as a new page; its note opens as it is written. */
  async startPage(pageId: string, title: string, goal: string): Promise<void> {
    if (this.core.disposed) return;
    const err = this.core.state.busy ? WIKI_COPY.attnBlockedBusy : this.core.blocked();
    if (err) { this.core.deps.notice?.(err); return; }
    await this.streams.runCreate(pageId, title, goal);
    // the page has no status area of its own (the note is the page): a failed write says so here
    const s = this.core.state.page;
    if (s.kind === "open" && s.page.page_id === pageId && s.status?.tone === "error") this.core.deps.notice?.(s.status.text);
  }

  /**
   * The wiki follows the active note (main.ts, follow-note.ts): the same GET + sync as openWikiPage, but the tab stays
   * and no note is opened (the user is already on it; a slow GET must never open it over a note they moved to). A newer
   * open or follow bumps pageToken, so an older GET that returns late is dropped.
   */
  async followPage(pageId: string): Promise<void> {
    await this.loadPage(pageId, true);
  }

  private async loadPage(pageId: string, follow: boolean): Promise<void> {
    // Ruling: while any write is in flight, opening a page does nothing (no request, no state change).
    if (this.core.disposed || this.core.state.busy) return;
    const token = ++this.core.pageToken;
    const pageGoal = this.core.state.pageGoal?.pageId === pageId ? this.core.state.pageGoal : null;
    this.core.set({ page: { kind: "loading", pageId }, pageGoal });
    if (!this.core.deps.configured()) {
      this.core.set({ page: { kind: "error", pageId, get message() { return WIKI_COPY.needsSettings; } } });
      return;
    }
    const page = await this.reads.getAndLand(pageId, token, true);
    if (page === null) return;
    // 今天's star map reads K: a read (no LLM), once, not waited for (the bars fill in when it lands);
    // after that every write's refresh re-reads it.
    if (this.core.state.k.kind === "idle") void this.reads.loadK(null);
    await this.notes.syncLanded(page, page.diff !== null || page.llm_calls > 0);
    // A page whose note is not in this vault (another device, a deleted note) is written into it, then opened.
    if (!follow && page.organized && token === this.core.pageToken && !(await this.notes.findAndOpen(pageId, token))) await this.writeNote();
    // Not written yet: the note of a write under way opens; with none, its 开始生成 tab (opening never writes it).
    if (!follow && !page.organized && page.trashed !== true && token === this.core.pageToken && !(await this.notes.findAndOpen(pageId, token))) {
      this.core.deps.showStartPage?.({ pageId, title: page.title, goal: page.goal });
    }
    if (page.rewriting === true) void this.watchRewrite(pageId, page.writer?.at ?? null);
  }

  /**
   * The open started the backend's rewrite of a page an RSI change left out of date (`PageOut.rewriting`); it runs after
   * the response. Look with a growing pause for up to REWRITE_MAX_MS until the page's writer stamp changes, then re-read
   * the page into the view (a refresh: notes kept), sync its note and say once 「这一页按新的讲法更新了」 (no model name).
   * Stops on dispose and when the shown page is no longer this one; a write in flight is waited out; a miss is silent
   * (the next open shows the page as it is, and starts nothing while no rewrite is due).
   */
  private async watchRewrite(pageId: string, stamp: string | null): Promise<void> {
    const here = (): boolean => { const p = this.core.state.page; return p.kind === "open" && p.page.page_id === pageId; };
    for (let i = 0, waited = 0; waited <= REWRITE_MAX_MS; i++) {
      const wait = REWRITE_STEP_MS[Math.min(i, REWRITE_STEP_MS.length - 1)]!;
      await this.core.sleep(wait);
      waited += wait;
      if (this.core.disposed || !here()) return;
      if (this.core.state.busy) continue;
      let now: PageOut;
      try { now = await this.core.call(() => this.core.deps.api.getPage(pageId)); } catch { continue; }   // a background read: nothing is opened
      if (this.core.disposed || !here() || (now.writer?.at ?? null) === stamp) continue;
      if (this.core.state.busy) continue;
      this.notes.land(now, false);
      await this.notes.syncLanded(now, true);
      this.core.deps.notice?.(WIKI_COPY.pageRewritten);
      return;
    }
  }

  /** 生成这一页 / 按当前状态重新组织 / 重试: one POST organize, then GET page + catalog (+ K), sync; no note opening. */
  async organize(): Promise<void> {
    if (this.core.state.busy) return;
    const s = this.core.state.page;
    if (s.kind !== "open") return;
    const pageId = s.page.page_id;
    const token = this.core.pageToken;
    const err = this.core.blocked();
    if (err) { this.core.patchView(pageId, () => ({ status: bad(err) })); return; }
    const seed: Seed = () => s.page;
    this.core.set({ busy: { what: "organize", abortable: true } });
    // Organize-written sections carry no marker: this visit's marks start over.
    this.core.patchView(pageId, () => ({ status: ok(WIKI_COPY.outlining), streaming: {}, touched: {} }));
    // a written page: 重新组织 writes it anew (an earlier run is never replayed); else 生成这一页 / 重试 follow its run
    const end = await this.streams.stream(pageId, token, seed, null, s.page.organized);
    await this.streams.afterStream(pageId, token, end, seed, false);
  }

  /**
   * 「停止」 (only when abortable): cancels the page's write run on the server, then aborts the stream; no error is shown,
   * the page is re-read. Leaving the page does not cancel the run, only this does. A cancel that fails leaves the stream
   * running (the run goes on) and says why; a 404 (the run already ended) just stops the stream. A stopped write is
   * kept in `stopped` (its line reads 已停止 · 重新写) until that page is written again.
   */
  async stop(): Promise<void> {
    const ac = this.core.abort, pageId = this.core.abortPage;
    if (!this.core.state.busy?.abortable || ac === null || pageId === null) return;
    try {
      await this.core.deps.api.cancelRun(pageId);
    } catch (e) {
      if (!(e instanceof ApiError && e.status === 404)) { ewNotice(errorText(e)); return; }
    }
    const s = this.core.state.page;
    const page = s.kind === "open" && s.page.page_id === pageId ? s.page : null;
    this.core.set({ stopped: { pageId, title: page?.title || this.core.names.page(pageId) || WIKI_COPY.unnamedPage, goal: page?.goal ?? "" } });
    ac.abort();
  }

  /**
   * 「停止」 on a page this controller may not stream (the chat's writing card, also after a reload, when the server
   * writes it on its own): its own stream → `stop`; else the page's run is cancelled on the server (404: it already
   * ended) and the page is kept as stopped (已停止 · 重新写, `restart`). False: not stopped (the failure was said).
   */
  async stopPage(pageId: string): Promise<boolean> {
    if (this.writingPage() === pageId) { await this.stop(); return this.core.state.stopped?.pageId === pageId; }
    try {
      await this.core.deps.api.cancelRun(pageId);
    } catch (e) {
      if (!(e instanceof ApiError && e.status === 404)) { ewNotice(errorText(e)); return false; }
    }
    this.core.set({ stopped: { pageId, title: this.core.names.page(pageId) || WIKI_COPY.unnamedPage, goal: "" } });
    return true;
  }

  /**
   * The page's write as a line shows it: "writing" while this controller streams it (「停止」 can end it), "stopped" after
   * 「停止」 until it is written again, else null.
   */
  writeRun(pageId: string): "writing" | "stopped" | null {
    if (this.writingPage() === pageId) return "writing";
    return this.core.state.stopped?.pageId === pageId ? "stopped" : null;
  }

  /** The page whose write this controller streams now (「停止」 can end it), else null. */
  writingPage(): string | null {
    return this.core.state.busy?.abortable === true && this.core.abort !== null ? this.core.abortPage : null;
  }

  /**
   * 重新写 on a stopped write: the page is written again from the start — an organized page as 重新组织 does it, one that
   * held nothing yet as a new page (its note opens with the first section). Nothing while another write runs.
   */
  async restart(pageId: string): Promise<void> {
    const st = this.core.state.stopped;
    if (this.core.disposed || this.core.state.busy || st === null || st.pageId !== pageId) return;
    const err = this.core.blocked();
    if (err) { this.core.deps.notice?.(err); return; }
    const s = this.core.state.page;
    if (s.kind === "open" && s.page.page_id === pageId && s.page.organized) { await this.organize(); return; }
    await this.streams.runCreate(pageId, st.title, st.goal);
  }

  openCurrentNote(): void {
    const s = this.core.state.page;
    if (s.kind !== "open" || this.core.deps.sync === null) return;
    void this.notes.findAndOpen(s.page.page_id, this.core.pageToken).then((ok) => ok ? undefined : this.writeNote());
  }

  /**
   * 本页 小目录: open the page's note (its open tab if there is one) and scroll to where section `sid` starts in it
   * (page-toc.ts findSectionLine, read from the note as it is on disk). Local and read-only; allowed while a write runs.
   * No note → the same 「写入库里」 offer as 打开笔记.
   */
  jumpToSection(sid: string): void { void this.jumpTo(sid); }

  /** jumpToSection's work; true = the note was opened. `frame`: the section also gets the dashed frame. */
  async jumpTo(sid: string, frame: boolean = false): Promise<boolean> {
    const s = this.core.state.page;
    if (this.core.disposed || s.kind !== "open" || this.core.deps.sync === null) return false;
    if (!s.page.sections.some((x) => x.id === sid)) return false;
    const input = this.notes.noteInput(s.page);
    return this.notes.findAndOpen(s.page.page_id, this.core.pageToken, (text) => findSectionLine(text, input, sid), frame);
  }

  /**
   * A 参数 change card (app.js goToChange): open the page the change is in (as a page link does: a refusal says why in a
   * notice), then, when the change knows its section and the page still has it, scroll there and frame it.
   */
  async goToChange(pageId: string, sid: string | null): Promise<void> {
    await this.openPageLink(pageId);
    const s = this.core.state.page;
    if (sid !== null && s.kind === "open" && s.page.page_id === pageId) await this.jumpTo(sid, true);
  }

  /**
   * The 「⋯」 menu's 太浅了 / 有点难 (sid = that section; pinned sections too: feedback never rewrites the text). One
   * POST feedback; the result is a Notice (已提交, or the plain failure, which also goes under the section).
   */
  async feedback(sid: string | null, chips: FeedbackChip[]): Promise<void> {
    const v = this.core.actionView();
    if (!v || (sid !== null && !v.page.sections.some((x) => x.id === sid))) return;
    const spot: Spot = sid === null ? { kind: "status" } : { kind: "section", sid };
    const blocked = this.core.blocked();
    if (blocked) { this.core.put(v.page.page_id, spot, bad(blocked)); this.core.deps.notice?.(blocked); return; }
    const ctx = this.core.begin(v.page.page_id, spot, { what: "feedback", ...(sid !== null ? { sid } : {}), abortable: false });
    if (!ctx) return;
    const err = await this.postFeedback(ctx, { chips, text: "", sectionId: sid, artifactId: sid === null ? undefined : v.page.sections.find((x) => x.id === sid)?.artifact_id });
    if (this.core.disposed) return;
    if (err === null) { this.core.deps.notice?.(WIKI_COPY.feelSaved); return; }
    // The section is gone (422, page re-read): its note would have no place, so the status line gets it.
    if (ctx.token === this.core.pageToken) this.core.put(ctx.pageId, err === WIKI_COPY.feedbackSectionGone ? { kind: "status" } : spot, bad(err));
    this.core.deps.notice?.(err);
  }

  /**
   * One POST /wiki/pages/{id}/feedback under a started write (busy set); clears busy. Null on success, else the plain
   * failure text. A 422 for a section (it is no longer on the page) re-reads the page first; 404 says the page is gone.
   */
  private async postFeedback(ctx: Ctx, b: { chips: FeedbackChip[]; text: string; sectionId: string | null; artifactId?: string | null }): Promise<string | null> {
    try {
      await this.core.call(() => this.core.deps.api.pageFeedback(ctx.pageId, { chips: b.chips, text: b.text, section_id: b.sectionId, apply: false, artifact_id: b.artifactId }));
    } catch (e) {
      if (this.core.disposed) return WIKI_COPY.failed;
      const gone = e instanceof ApiError && e.status === 422 && b.sectionId !== null;
      const msg = e instanceof ApiError && e.status === 404 ? WIKI_COPY.feedbackPageGone
        : gone ? WIKI_COPY.feedbackSectionGone : errorText(e);
      const page = gone && ctx.token === this.core.pageToken ? await this.reads.getAndLand(ctx.pageId, ctx.token, false) : null;
      this.core.set({ busy: null });
      if (page !== null) await this.notes.syncLanded(page, page.diff !== null || page.llm_calls > 0);
      return msg;
    }
    if (!this.core.disposed) this.core.set({ busy: null });
    return null;
  }

  /**
   * An organized page with no note in the vault (opening it found none): one local sync of the current page, bypassing the A3
   * change check and the last-synced dedupe (the user asked); then find + open the note. Not a server request, so
   * allowed offline; nothing while a write is in flight. A failure shows noteSyncFailed and is never retried.
   */
  async writeNote(): Promise<void> {
    const sync = this.core.deps.sync;
    const s = this.core.state.page;
    if (sync === null || this.core.disposed || this.core.state.busy || s.kind !== "open" || !s.page.organized) return;
    if (s.noteSync.kind !== "missing") return;
    const pageId = s.page.page_id;
    const token = this.core.pageToken;
    const input = this.notes.noteInput(s.page);
    await this.notes.enqueueSync(sync, pageId, input, JSON.stringify(input));
    const now = this.core.state.page;
    if (this.core.disposed || token !== this.core.pageToken || now.kind !== "open" || now.page.page_id !== pageId) return;
    if (now.noteSync.kind === "done") await this.notes.findAndOpen(pageId, token);
  }

  // ---------------- section / page actions (P4: results next to what produced them) ----------------

  /** 这部分我懂: one POST feedback {know}; the returned section is written back; then P5 refresh. Pinned → nothing. */
  async know(sid: string): Promise<void> {
    const v = this.core.actionView();
    const sec = v?.page.sections.find((x) => x.id === sid);
    if (!v || !sec || sec.pinned) return;
    const spot: Spot = { kind: "section", sid };
    const ctx = this.core.begin(v.page.page_id, spot, { what: "know", sid, abortable: false });
    if (!ctx) return;
    let out: FeedbackOut;
    try {
      out = await this.core.call(() => this.core.deps.api.feedbackKnow(ctx.pageId, sid, sec.artifact_id));
    } catch (e) { this.core.failed(ctx, spot, e); return; }
    if (this.core.disposed) return;
    this.core.writeBack(ctx.pageId, out.section);
    const changed = this.notes.noteChange(out.change);
    // The ChangeOut first; a folded section it does not list is 已折叠 (the section's own changed is not used).
    const marks = changeMarks(out.change);
    if (out.section.folded && !marks.some(([x]) => x === sid)) marks.push([sid, "folded"]);
    this.core.touch(ctx, marks);
    const hd = this.core.headings(ctx.pageId);
    const h = this.core.headingText(ctx.pageId)(sid);
    const lead = out.section.foldable ? WIKI_COPY.knowFolded(h) : WIKI_COPY.knowShort(h);
    this.core.put(ctx.pageId, spot, ok(opLine(opResult(lead, out.change, hd, sid), true)));
    await this.reads.refresh(ctx, changed, { kind: "page", pageId: ctx.pageId, spot });
  }


  /** 折叠 / 展开全文 (foldable sections only): one POST fold; written back locally, no GET; the note is synced. */
  async toggleFold(sid: string): Promise<void> {
    const v = this.core.actionView();
    const sec = v?.page.sections.find((x) => x.id === sid);
    if (!v || !sec || !sec.foldable) return;
    const spot: Spot = { kind: "section", sid };
    const ctx = this.core.begin(v.page.page_id, spot, { what: "fold", sid, abortable: false });
    if (!ctx) return;
    const folded = !sec.folded;
    let out: SectionOut;
    try {
      out = await this.core.call(() => this.core.deps.api.fold(ctx.pageId, sid, folded));
    } catch (e) { this.core.failed(ctx, spot, e); return; }
    if (this.core.disposed) return;
    this.core.writeBack(ctx.pageId, out);
    const h = this.core.headingText(ctx.pageId)(sid);
    this.core.put(ctx.pageId, spot, ok(out.folded ? WIKI_COPY.folded(h) : WIKI_COPY.unfolded(h)));
    this.core.set({ busy: null });
    const s = this.core.state.page;
    if (s.kind === "open" && s.page.page_id === ctx.pageId) await this.notes.syncLanded(s.page, true);
  }

  /** 钉住 / 取消钉住: one POST pin; written back locally; no GET, no sync. */
  async togglePin(sid: string): Promise<void> {
    const v = this.core.actionView();
    const sec = v?.page.sections.find((x) => x.id === sid);
    if (!v || !sec) return;
    const spot: Spot = { kind: "section", sid };
    const ctx = this.core.begin(v.page.page_id, spot, { what: "pin", sid, abortable: false });
    if (!ctx) return;
    const pinned = !sec.pinned;
    let out: SectionOut;
    try {
      out = await this.core.call(() => this.core.deps.api.pin(ctx.pageId, sid, pinned));
    } catch (e) { this.core.failed(ctx, spot, e); return; }
    if (this.core.disposed) return;
    this.core.writeBack(ctx.pageId, out);
    const h = this.core.headingText(ctx.pageId)(sid);
    this.core.put(ctx.pageId, spot, ok(out.pinned ? WIKI_COPY.pinned(h) : WIKI_COPY.unpinned(h)));
    this.core.set({ busy: null });
  }

  /** 确认题: only an unanswered check; one POST checks/{sid}; the answer is kept locally; then P5 refresh. */
  async answerCheck(sid: string, choice: number): Promise<void> {
    const v = this.core.actionView();
    const sec = v?.page.sections.find((x) => x.id === sid);
    if (!v || !sec || sec.check === null || sec.check.answered !== null) return;
    const spot: Spot = { kind: "section", sid };
    const ctx = this.core.begin(v.page.page_id, spot, { what: "check", sid, abortable: false });
    if (!ctx) return;
    let out: CheckAnswerOut;
    try {
      out = await this.core.call(() => this.core.deps.api.answerCheck(ctx.pageId, sid, choice, sec.check?.artifact_id));
    } catch (e) { this.core.failed(ctx, spot, e); return; }
    if (this.core.disposed) return;
    this.core.clearFollowup(`check:${ctx.pageId}:${sid}`);
    this.core.answerRefs.set(`check:${ctx.pageId}:${sid}`, out.answer_ref);
    this.core.patchView(ctx.pageId, (pv) => ({
      page: { ...pv.page, sections: pv.page.sections.map((x) => (x.id !== sid || x.check === null ? x : {
        ...x, check: { ...x.check, answered: choice, correct_index: out.correct_index, correct: out.correct } })) },
    }));
    const changed = this.notes.noteChange(out.change);
    this.core.touch(ctx, changeMarks(out.change));
    const hd = this.core.headings(ctx.pageId);
    // No verdict sentence (user review): only what changed on the page (folds, rewrites) gets a line.
    const line = opLine(opResult("", out.change, hd), true);
    if (line) this.core.put(ctx.pageId, spot, ok(line));
    else this.core.patchView(ctx.pageId, (pv) => ({ sectionNotes: without(pv.sectionNotes, sid) }));
    await this.reads.refresh(ctx, changed, { kind: "page", pageId: ctx.pageId, spot });
    graphBus.emit();
  }

  /** 这一页我学完了: one POST done → status line; then P5 refresh. */
  async done(): Promise<void> {
    const v = this.core.actionView();
    if (!v) return;
    const spot: Spot = { kind: "status" };
    const ctx = this.core.begin(v.page.page_id, spot, { what: "done", abortable: false });
    if (!ctx) return;
    try {
      await this.core.call(() => this.core.deps.api.done(ctx.pageId));
    } catch (e) { this.core.failed(ctx, spot, e); return; }
    if (this.core.disposed) return;
    this.core.put(ctx.pageId, spot, ok(WIKI_COPY.pageDone));
    await this.reads.refresh(ctx, false, { kind: "page", pageId: ctx.pageId, spot });
  }

  /** 恢复成完整页 (collapsed pages only): one POST expand → status line; then P5 refresh and sync. */
  expand(): Promise<void> { return this.shelve("expand"); }

  /** 收为参考卡 (pages the server offers it for: can_shelve): one POST collapse → status line; then P5 refresh and sync. */
  collapse(): Promise<void> { return this.shelve("collapse"); }

  private async shelve(what: "expand" | "collapse"): Promise<void> {
    const v = this.core.actionView();
    if (!v || (what === "expand" ? !v.page.collapsed : !v.page.can_shelve)) return;
    const spot: Spot = { kind: "status" };
    const ctx = this.core.begin(v.page.page_id, spot, { what, abortable: false });
    if (!ctx) return;
    let out: ShelveOut;
    try {
      out = await this.core.call(() => (what === "expand" ? this.core.deps.api.expand(ctx.pageId) : this.core.deps.api.collapse(ctx.pageId)));
    } catch (e) { this.core.failed(ctx, spot, e); return; }
    if (this.core.disposed) return;
    this.notes.noteChange(out.change);
    this.core.touch(ctx, changeMarks(out.change));
    const lead = what === "expand" ? WIKI_COPY.expandDone : WIKI_COPY.shelveDone;
    this.core.put(ctx.pageId, spot, ok(opLine(opResult(lead, out.change, this.core.headings(ctx.pageId)), true)));
    // The page itself changed (reference card ↔ full page), whatever the section lists say.
    await this.reads.refresh(ctx, true, { kind: "page", pageId: ctx.pageId, spot });
  }

  /** 重试: a new click (same guards as the original action). */
  async retry(r: RetryAction): Promise<void> {
    if (r.kind === "organize") await this.organize();
  }

  /** 「⋯ → 问助手」: the section's heading and opening become the assistant's quote (nothing is sent). */
  askAssistant(sid: string): void {
    const v = this.core.actionView();
    const sec = v?.page.sections.find((x) => x.id === sid);
    if (!v || !sec) return;
    this.core.deps.askAssistant?.(v.page.page_id, v.page.title, sectionQuote(sec));
  }
}
