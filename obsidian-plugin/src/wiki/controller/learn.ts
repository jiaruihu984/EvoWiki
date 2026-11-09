/**
 * 学点新的 and the learning path: a chat goal's first page (生成第一页), 下一页 / 学完 on a path (P4: results in
 * path.note) and 下一步 · 继续.
 */
import { WIKI_COPY } from "../copy";
import { errorText } from "../format";
import type { PathNextOut, PathOut, PathPageDoneOut } from "../schema";
import { todayStep } from "../progress";
import type { ReviewFirstChoice } from "../review-first";
import type { ReviewFirstOut } from "../schema";
import type { StepOut } from "../schema";
import { PATH, ok, bad } from "./helpers";
import type { WikiCore } from "./core";
import type { Notes } from "./notes";
import type { Reads } from "./reads";
import type { Streams } from "./streams";

/** A page that was asked to be written and was not (the stream failed or stopped, the path could not be read). */
export class PageNotWritten extends Error {
  constructor(readonly line: string) { super(line); this.name = "PageNotWritten"; }
}

export class Learning {
  constructor(private readonly core: WikiCore, private readonly notes: Notes, private readonly reads: Reads, private readonly streams: Streams,
    private readonly openPage: (pageId: string) => Promise<void>) {}

  // ---------------- goal ----------------

  /** Local only; does not notify (keeps focus in the box). */
  setLearnTopic(text: string): void {
    if (this.core.disposed) return;
    this.core.state = { ...this.core.state, learnTopic: text };
  }

  /** 想学点新的 · 开始 / a recommendation / 学这个: the 学点新的 tab with the topic (deps.learnNew; absent = nothing, tests). */
  openLearnNew(topic: string): void {
    if (this.core.disposed) return;
    this.core.deps.learnNew?.(topic);
  }

  /**
   * A goal's first page (the chat's goal run): the region's path is opened and its first page written through the existing
   * 下一页 chain (POST paths/{id}/next, organize, sync, open the note) — what continueLearning does. `onPage`: the page's id
   * as soon as POST next names it, before the note opens (the 助手 keeps the goal's conversation for that page). Resolves
   * only when the page was written; anything else (busy, the path unreadable, the write failed) rejects with
   * PageNotWritten and its plain line (the run shows it with 重试).
   */
  async learnFirstPage(pathId: string, onPage?: (pageId: string) => void, openNote = true, key?: string): Promise<void> {
    if (this.core.disposed) throw new PageNotWritten(WIKI_COPY.failed);
    if (this.core.state.busy) throw new PageNotWritten(WIKI_COPY.reviewBlockedBusy);
    if (this.core.state.path.pathId !== pathId || this.core.state.path.view.kind !== "ready") await this.openPath(pathId);
    const view = this.core.state.path.view;
    if (view.kind !== "ready") throw new PageNotWritten(view.kind === "error" ? view.message : WIKI_COPY.failed);
    let made: string | null = null;
    await this.pathNext((id) => { made = id; onPage?.(id); }, openNote, key);
    this.mustBeWritten(made);
  }

  /** A goal's single page, or a first page made but never written (重试): the new-page chain on it (organize, sync, open). */
  async learnWritePage(pageId: string, title: string, goal: string, onPage?: (pageId: string) => void, openNote = true): Promise<void> {
    const err = this.core.disposed ? WIKI_COPY.failed : this.core.state.busy ? WIKI_COPY.reviewBlockedBusy : this.core.blocked();
    if (err) throw new PageNotWritten(err);
    onPage?.(pageId);
    await this.streams.runCreate(pageId, title, goal, openNote);
    this.mustBeWritten(pageId);
  }

  /** The organize stream of `pageId` ended written, or PageNotWritten with the path's / page's own line. */
  private mustBeWritten(pageId: string | null): void {
    if (pageId !== null && this.notes.foregroundResult.get(pageId) === true) return;
    const note = this.core.state.path.note;
    throw new PageNotWritten(note !== null && note.tone === "error" ? note.text : WIKI_COPY.failed);
  }

  // ---------------- learning path (P4: results in path.note) ----------------

  /** Shows a path (GET it); null = back to the path list (local). Nothing while a write is in flight. */
  async openPath(id: string | null): Promise<void> {
    if (this.core.disposed || this.core.state.busy) return;
    if (id === null) {
      this.core.set({ path: { pathId: null, view: { kind: "idle" }, note: null } });
      return;
    }
    this.core.set({ path: { pathId: id, view: { kind: "loading" }, note: null } });
    await this.reads.loadPath(id, null);
  }

  /**
   * 下一页: one POST next (the server creates the page, or claims the one it wrote ahead); then the new-page chain; then
   * one GET of the path. A failed POST: its text in path.note, nothing retried or re-read.
   * Written ahead (single writer): the chain is the same whatever the prefetch's state — POST organize replays a ready
   * run at once (and consumes it, so a later 重新组织 writes anew), attaches to a writing one, and writes a failed / absent
   * one as usual; the sections stream into the note and it opens with the first.
   */
  async pathNext(onPage?: (pageId: string) => void, openNote = true, key?: string): Promise<void> {
    if (this.core.disposed || this.core.state.busy) return;
    const ps = this.core.state.path;
    const pathId = ps.pathId;
    if (pathId === null || ps.view.kind !== "ready" || (key === undefined && ps.view.value.next_index === null)) return;
    const path: PathOut = ps.view.value;
    const err = this.core.blocked();
    if (err) { this.core.setPath({ note: bad(err) }); return; }
    this.reads.pathEpoch++;
    this.core.set({ busy: { what: "path-next", abortable: false }, path: { ...ps, note: null } });
    let out: PathNextOut;
    try {
      out = await this.core.call(() => this.core.deps.api.pathNext(pathId, key));
    } catch (e) {
      if (this.core.disposed) return;
      this.core.setPath({ note: bad(errorText(e)) });
      this.core.set({ busy: null });
      return;
    }
    if (this.core.disposed) return;
    onPage?.(out.page_id);
    // The page now exists: the shown path says so straight away (the GET below confirms), so even if that GET fails
    // the 下一页 button names the next page and never re-starts this one.
    this.patchPathAfterNext(out);
    // 第 i/n 页 counts the shown order (`out.index` is the stored position, only an id once the learner reorders)
    const shownAt = path.pages.findIndex((p) => p.index === out.index) + 1;
    const rowTitle = path.pages.find((p) => p.index === out.index)?.title ?? "";
    await this.streams.runCreate(out.page_id, rowTitle !== "" ? rowTitle : WIKI_COPY.pathPageTitle(path.goal, shownAt > 0 ? shownAt : out.index, path.pages.length), path.goal, openNote);
    // The page exists whatever the stream did (failed / stopped too): its status is now 在学.
    if (!this.core.disposed) await this.reads.loadPath(pathId, this.core.refreshFail(PATH));
  }

  /** 学完 (a page in 学习中 only): one POST → the path from the response, the done line in path.note; then P5 refresh. */
  async pathDone(index: number): Promise<void> {
    const ps = this.core.state.path;
    const pathId = ps.pathId;
    if (pathId === null || ps.view.kind !== "ready") return;
    if (ps.view.value.pages.find((p) => p.index === index)?.status !== "learning") return;
    if (!this.core.beginArea("path", { what: "path-done", abortable: false })) return;
    this.reads.pathEpoch++;
    const ctx = this.core.currentCtx();
    let out: PathPageDoneOut;
    try {
      out = await this.core.call(() => this.core.deps.api.pathPageDone(pathId, index));
    } catch (e) { this.core.areaFailed("path", e); return; }
    if (this.core.disposed) return;
    if (this.core.state.path.pathId === pathId) {
      this.core.names.addPath(out.path);
      this.core.setPath({ view: { kind: "ready", value: out.path } });
    }
    this.core.setAreaNote("path", ok(WIKI_COPY.pageDone));
    await this.reads.refresh(ctx, false, PATH);
  }

  /** 先复习 / 跳过 on 「建议先复习 X」: closes it on the server; 先复习 then asks about the point in a new chat turn (writes no page). */
  async answerReviewFirst(hint: ReviewFirstOut, choice: ReviewFirstChoice): Promise<void> {
    const ctx = this.core.currentCtx();
    try {
      await this.core.call(() => this.core.deps.api.reviewFirst({ node_id: hint.node_id, choice }));
    } catch (e) { this.core.deps.notice?.(errorText(e)); return; }
    if (this.core.disposed) return;
    if (choice === "review") {
      const text = WIKI_COPY.reviewFirstAsk(hint.name);
      if (hint.page_id !== null) this.core.deps.assistantSendFresh?.(hint.page_id, hint.page_title, text);
      else this.core.deps.assistantSend?.(text);
    }
    await this.reads.refresh(ctx, false, PATH);
  }

  /** 下一步 · 继续: the server's step for 今天 (GET /wiki/progress), followed. */
  async continueLearning(): Promise<void> {
    const step = todayStep(this.core.state);
    if (step !== null) await this.followStep(step);
  }

  /**
   * Does what a step says (wikiapp/progress.py StepOut): a page to read or being written opens; a page not written yet or
   * whose write failed is written (opening a page never writes it); a row with no page is made by 下一页 on its path; a
   * block to enter or a diagnosis goes on in the chat's run of that goal (continueGoal). Nothing for goal_done / none.
   */
  async followStep(step: StepOut): Promise<void> {
    if (this.core.disposed || this.core.state.busy) return;
    if (step.page_id !== null && (step.kind === "read" || step.kind === "writing")) {
      if (step.map_id !== null) this.core.set({ pageGoal: { pageId: step.page_id, mapId: step.map_id } });
      await this.openPage(step.page_id);
      return;
    }
    if (step.page_id !== null && (step.kind === "failed" || step.kind === "start_page")) {
      const err = this.core.blocked();
      if (err) { this.core.deps.notice?.(err); return; }
      // step.goal names the goal, not the page's 目标: the seed shows none until the page lands
      await this.streams.runCreate(step.page_id, step.title, "");
      return;
    }
    if (step.kind === "start_page" && step.path_id !== null) {
      const ps = this.core.state.path;
      if (ps.pathId !== step.path_id || ps.view.kind !== "ready") await this.openPath(step.path_id);
      if (this.core.disposed || this.core.state.path.view.kind !== "ready") return;
      await this.pathNext();
      return;
    }
    if ((step.kind === "enter_block" || step.kind === "diagnosis") && step.map_id !== null) {
      this.core.deps.continueGoal?.(step.map_id, step.region_id);
    }
  }

  /** After POST next: page out.index is 在学 with its page id; next_index = the next 还没开始 page, or null. */
  private patchPathAfterNext(out: PathNextOut): void {
    const ps = this.core.state.path;
    if (ps.pathId !== out.path_id || ps.view.kind !== "ready") return;
    const pages = ps.view.value.pages.map((pg) => (pg.index !== out.index ? pg
      : { ...pg, status: "learning" as const, page_id: out.page_id }));
    const next = pages.find((pg) => pg.status === "not_started")?.index ?? null;
    this.core.setPath({ view: { kind: "ready", value: { ...ps.view.value, pages, next_index: next, prefetch: null } } });
  }
}
