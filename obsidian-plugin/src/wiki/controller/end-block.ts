/**
 * 笔记末尾的「读完了」区块 / 页面顶部一行 (end-block/; drawn by the plugin, never written to the note): the pills, the main
 * button (this page is learned: the next page on its path), the top line's 撤回 of a feedback patch.
 */
import { endModel } from "../end-block/model";
import { isGoalCheck } from "../goal-checks";
import { ApiError } from "../../api/errors";
import { WIKI_COPY } from "../copy";
import { errorText } from "../format";
import { type EndPhase, type Feel, type PageEnd, EMPTY_END } from "../state";
import type { Ctx } from "./helpers";
import type { WikiCore } from "./core";
import type { Notes } from "./notes";
import type { Reads } from "./reads";
import type { HandEditOut, StepOut } from "../schema";

/** The page actions and the learning path this block drives (the facade wires them). */
export interface EndBlockHooks {
  done(): Promise<void>;
  /** Does what a server step says (learn.ts followStep). */
  followStep(step: StepOut): Promise<void>;
}

export class EndBlock {
  constructor(private readonly core: WikiCore, private readonly reads: Reads, private readonly notes: Notes, private readonly h: EndBlockHooks) {}

  /** What the block keeps for `pageId` (chosen chips, the typed text, the checkbox, its line); read-only. */
  endBlockState(pageId: string): PageEnd { return this.core.state.pageEnd[pageId] ?? EMPTY_END; }

  /** Updates one page's block state; `quiet` = no notification (typing must not redraw the block under the cursor). */
  private patchEnd(pageId: string, f: (e: PageEnd) => Partial<PageEnd>, quiet = false): void {
    if (this.core.disposed) return;
    const pageEnd = { ...this.core.state.pageEnd, [pageId]: { ...this.endBlockState(pageId), ...f(this.endBlockState(pageId)) } };
    if (quiet) this.core.state = { ...this.core.state, pageEnd };
    else this.core.set({ pageEnd });
  }

  /**
   * A pill (app.js endBlock): one choice; pressing the pressed one does nothing. 内容太简单 / 理解有困难 are sent to the
   * assistant as the learner's message about this page, in a new conversation (the assistant asks what is wrong and
   * changes the page); 基本合适 records a signal (POST /fit, no model call); a failure takes the pick back and says so.
   */
  pickEndChip(pageId: string, feel: Feel): void {
    if (this.endBlockState(pageId).draft.chips.includes(feel)) return;
    this.patchEnd(pageId, (e) => ({ draft: { ...e.draft, chips: [feel] }, phase: { kind: "idle" } }));
    if (feel === "right") { void this.recordFit(pageId); return; }
    const pg = this.core.state.page;
    const title = pg.kind === "open" && pg.page.page_id === pageId ? pg.page.title : this.core.names.page(pageId) ?? "";
    this.core.deps.assistantSendFresh?.(pageId, title, feel === "shallow" ? WIKI_COPY.endSendShallow : WIKI_COPY.endSendHard);
  }

  private async recordFit(pageId: string): Promise<void> {
    const failed = (text: string): void => this.patchEnd(pageId, (e) => ({ draft: { ...e.draft, chips: [] }, phase: { kind: "error", text } }));
    const blocked = this.core.blocked();
    if (blocked) { failed(blocked); return; }
    try {
      await this.core.call(() => this.core.deps.api.pageFit(pageId));
    } catch (e) {
      if (!this.core.disposed) failed(errorText(e));
    }
  }

  private endLine(pageId: string, phase: EndPhase): void { this.patchEnd(pageId, () => ({ phase })); }

  /**
   * 撤回 (the top line): POST undo of the page's feedback patch, then the page is re-read and the note synced (the
   * patch and the 「按你的反馈改过」 tags go). 409 (the page changed since) says so without a retry; any other failure
   * keeps the button with 撤回没成功，请再试一次。
   */
  async undoPageFeedback(pageId: string): Promise<void> {
    const v = this.core.actionView();
    if (!v || v.page.page_id !== pageId || v.page.feedback === null) return;
    const fid = v.page.feedback.feedback_id;
    const blocked = this.core.blocked();
    const failed = (text: string, retry: boolean): void => this.patchEnd(pageId, () => ({ undo: { kind: "failed", text, retry } }));
    if (blocked) { failed(blocked, true); return; }
    this.core.set({ busy: { what: "undo-feedback", abortable: false } });
    this.patchEnd(pageId, () => ({ undo: { kind: "undoing" } }));
    const ctx: Ctx = { pageId, token: this.core.pageToken };
    try {
      await this.core.call(() => this.core.deps.api.undoFeedback(pageId, fid));
    } catch (err) {
      if (this.core.disposed) return;
      this.core.set({ busy: null });
      if (err instanceof ApiError && err.status === 409) {
        failed(WIKI_COPY.topUndoChanged, false);
        // The marker is stale (the page changed since): re-read, so the top line matches the page.
        if (ctx.token === this.core.pageToken) await this.reads.getAndLand(pageId, ctx.token, false, () => undefined);
      } else failed(WIKI_COPY.topUndoFailed, true);
      return;
    }
    if (this.core.disposed) return;
    // The marker is gone with the patch, whatever the re-read below does.
    this.core.patchView(pageId, (cur) => ({ page: { ...cur.page, feedback: null } }));
    this.patchEnd(pageId, () => ({ undo: { kind: "idle" }, phase: { kind: "idle" } }));
    this.core.deps.notice?.(WIKI_COPY.noticeUndone);
    await this.reads.refresh(ctx, true, { kind: "page", pageId, spot: { kind: "status" } });
  }

  /** 撤销 (the top line) of the last hand edit: its sections as they were, then the note follows the page. */
  async undoHandEdit(pageId: string): Promise<void> {
    const v = this.core.actionView();
    const edit = v?.page.page_id === pageId ? v.page.hand_edit : null;
    if (!edit) return;
    const failed = (text: string, retry: boolean): void => this.patchEnd(pageId, () => ({ undo: { kind: "failed", text, retry } }));
    const blocked = this.core.blocked();
    if (blocked) { failed(blocked, true); return; }
    this.patchEnd(pageId, () => ({ undo: { kind: "undoing" } }));
    const ctx: Ctx = { pageId, token: this.core.pageToken };
    let out: HandEditOut;
    try {
      out = await this.core.call(() => this.core.deps.api.undoHandEdit(pageId, edit.edit_id));
    } catch (err) {
      if (this.core.disposed) return;
      if (err instanceof ApiError && err.status === 409) {
        failed(WIKI_COPY.topUndoChanged, false);
        if (ctx.token === this.core.pageToken) await this.reads.getAndLand(pageId, ctx.token, false, () => undefined);
      } else failed(WIKI_COPY.topUndoFailed, true);
      return;
    }
    if (this.core.disposed) return;
    this.patchEnd(pageId, () => ({ undo: { kind: "idle" }, handLine: false }));
    this.core.deps.notice?.(WIKI_COPY.noticeUndone);
    this.notes.ledgerMoved();
    this.notes.land(out.page, false);
    await this.notes.syncLanded(out.page, true);
  }

  /**
   * 恢复 EvoWiki 的版本 (the line that says a hand edit could not go up): the note is written from the page over `note`, the
   * text the line read; it is backed up first, and a note changed since is left alone.
   */
  async restoreNote(pageId: string, note: string): Promise<void> {
    const v = this.core.actionView();
    if (v?.page.page_id !== pageId) return;
    this.patchEnd(pageId, () => ({ handFailed: null }));
    await this.notes.restoreNote(v.page, note);
  }

  /** 保留我的版本: the hand edit goes over what the server changed in that section. */
  async keepMine(pageId: string): Promise<void> {
    const v = this.core.actionView();
    if (v?.page.page_id === pageId) await this.notes.keepMine(v.page);
  }

  /**
   * The main button: this page is learned. On a path: the path is read fresh, the page marked learned (when it is in
   * 学习中), then the next page is opened (made first when it does not exist yet); the last page ends the goal. Off a
   * path: 这一页我学完了. A failure keeps the page and puts one plain line in the block.
   */
  async finishPage(pageId: string): Promise<void> {
    const v = this.core.actionView();
    if (!v || v.page.page_id !== pageId) return;
    const model = endModel(this.core.state, pageId);
    if (model?.completed && (model.primary.kind !== "goal-done" || v.page.sections.some(isGoalCheck))) return;
    const blocked = this.core.blocked();
    if (blocked) { this.endLine(pageId, { kind: "error", text: blocked }); return; }
    // the step once this page is read, as the server said it with the page (PageOut.next_step)
    let after = endModel(this.core.state, pageId)?.primary ?? null;
    if (!v.page.done || after?.kind === "goal-done") await this.h.done();
    if (this.core.disposed) return;
    const now = this.core.state.page;
    if (now.kind === "open" && now.page.page_id === pageId && now.status?.tone === "error") {
      this.endLine(pageId, { kind: "error", text: WIKI_COPY.endDoneFailed }); return;
    }
    // a page opened while it was written named itself as the step: the page read back knows what follows
    if (after !== null && "step" in after && after.step.page_id === pageId) after = endModel(this.core.state, pageId)?.primary ?? null;
    if (after !== null && (after.kind === "next" || after.kind === "next-block")) { await this.h.followStep(after.step); return; }
    this.endLine(pageId, { kind: "saved", text: WIKI_COPY.endPageDoneLine });
  }

}
