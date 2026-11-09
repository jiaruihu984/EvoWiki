/**
 * Review (P4: results on the item; failures in review.note): the in-place round on 今天, self-grades and check answers,
 * the due confirmation questions, 粗心选错了 / 我学会了 / 看不懂, 反馈 on a card.
 */
import { ApiError } from "../../api/errors";
import { WIKI_COPY } from "../copy";
import { confirmErrorText, errorText, followupErrorText, reviewNote } from "../format";
import { followupKey, clip as clipQuote } from "../followup";
import type {
  ReviewTargetOut, CheckFollowupOut, ConfirmAnswerOut, FollowupChoice, FollowupTarget, Grade, ReviewGradeIn, ReviewGradeOut,
  ReviewItemOut, ReviewTodayOut,
} from "../schema";
import { confirmKey, currentEntry, reviewEntries } from "../review-entries";
import { REVIEW, ok, bad, changeMarks } from "./helpers";
import type { WikiCore } from "./core";
import type { Notes } from "./notes";
import type { Reads } from "./reads";

/** The Bot's completion card and the page opener (the facade wires them). */
export interface ReviewHooks {
  botReviewFinished(n: number, ok: number): void;
  closeCelebrate(): void;
  openWikiPage(pageId: string): Promise<void>;
  jumpTo(sid: string): Promise<boolean>;
}

export class Review {
  constructor(private readonly core: WikiCore, private readonly notes: Notes, private readonly reads: Reads, private readonly h: ReviewHooks) {}

  /** 看答案: local only (uncovers the cue; the three self-grades come with it). */
  revealCue(cell: string): void {
    if (this.core.disposed) return;
    const r = this.core.state.review;
    if (r.revealed.includes(cell)) return;
    this.core.setReview({ revealed: [...r.revealed, cell] });
  }

  /**
   * 想起来了 / 有点模糊 / 没想起来 (a 回想 item whose cue was uncovered, not answered yet): one POST grade; the result on
   * the item, done_today + 1; then P5 refresh.
   */
  async gradeReview(cell: string, grade: Grade): Promise<void> {
    const item = this.reviewItem(cell, "cue");
    if (item === null || (item.answer !== "" && !this.core.state.review.revealed.includes(cell))) return;
    await this.sendReview(item, { cell, cells: item.cells, grade, page_id: item.page_id, section_id: item.section_id, artifact_id: item.artifact_id }, null);
  }

  /** 确认题 in the review list (not answered yet): one POST {choice}; the verdict on the item; then P5 refresh. */
  async answerReview(cell: string, choice: number): Promise<void> {
    const item = this.reviewItem(cell, "check");
    if (item === null) return;
    await this.sendReview(item, { cell, choice, page_id: item.page_id, section_id: item.section_id, artifact_id: item.artifact_id }, choice);
  }

  /** 下一张 / 完成: move past an answered card (local; the next unanswered card shows). `cell` is the card's key (a confirmation question's is confirmKey). */
  nextReview(cell: string): void {
    const r = this.core.state.review;
    const done = cell.startsWith("confirm:") ? r.confirmResults[cell.slice("confirm:".length)] !== undefined : r.results[cell] !== undefined;
    if (this.core.disposed || !done || r.seen.includes(cell)) return;
    this.core.setReview({ seen: [...r.seen, cell] });
    this.endIfDone();
  }

  /** 下一步 · 复习 N 张: the review shows in place on 今天 (no new tab). Nothing to do = nothing happens. `inChat`: the chat's card shows it; the sidebar stays on its tab. */
  startReview(inChat = false): void {
    if (this.core.disposed) return;
    if (!inChat) this.core.set({ tab: "today" });
    if (currentEntry(this.core.state) === null) return;
    const r = this.core.state.review;
    const answered = [...Object.keys(r.results), ...Object.keys(r.confirmResults).map(confirmKey)];
    this.core.setReview({ active: true, finished: false, roundDone: false, roundBase: answered, pageRoundDone: null });
  }

  /** 「考考我这页」: the page's cards, due or not, as the round; graded as any card. A failed read says so in the chat's card. */
  async startPageRound(pageId: string, answered: readonly string[] = []): Promise<void> {
    const seq = ++this.reads.reviewSeq;
    let t: ReviewTodayOut;
    try {
      t = await this.core.call(() => this.core.deps.api.reviewPage(pageId));
    } catch (e) {
      if (!this.core.disposed && seq === this.reads.reviewSeq) this.core.setReview({ today: { kind: "error", message: errorText(e) } });
      return;
    }
    if (this.core.disposed || seq !== this.reads.reviewSeq) return;
    t = { ...t, items: t.items.filter((i) => !answered.includes(i.card_id)) };
    this.core.deps.pageRoundMemo?.save(t.items.length > 0 ? { pageId, answered: [...answered] } : null);
    for (const k of Object.keys(this.core.state.followups)) if (k.startsWith("card:")) this.core.clearFollowup(k);
    this.h.closeCelebrate();
    this.core.setReview({ today: { kind: "ready", value: t }, revealed: [], results: {}, confirmResults: {}, seen: [], removed: [],
      active: t.items.length > 0, finished: false, roundDone: false, roundBase: [], barAt: null, note: null, pageRound: pageId, pageRoundBase: t.page_round_today, pageRoundDone: null });
  }

  /** The round a reload cut off goes on, without the cards already answered. */
  async resumePageRound(): Promise<void> {
    const m = this.core.deps.pageRoundMemo?.load() ?? null;
    if (m === null) return;
    // the start's own read of today's list may overtake this one (`reviewSeq`): then read again
    for (let i = 0; i < 3 && this.core.state.review.pageRound === null && this.core.deps.pageRoundMemo?.load() !== null; i++) {
      await this.startPageRound(m.pageId, m.answered);
    }
  }

  /** A page's round is over or ended: today's list again, nothing of the round left on it. */
  private leavePageRound(): void {
    if (this.core.disposed || this.core.state.review.pageRound === null) return;
    this.core.deps.pageRoundMemo?.save(null);
    const base = this.core.state.review.pageRoundBase;
    this.core.setReview({ pageRound: null, revealed: [], results: {}, confirmResults: {}, seen: [], removed: [], barAt: null, note: null });
    void this.reads.loadReview(null).then(() => {
      const t = this.core.state.review.today;
      if (this.core.disposed || t.kind !== "ready") return;
      const n = t.value.page_round_today - base;
      if (n > 0) this.core.setReview({ pageRoundDone: n });
    });
  }

  /**
   * 先不复习了: back to 下一步 with every undone card kept (R4.2: nothing is recorded, 每天最多 is not touched). A card
   * answered but not yet moved past counts as done.
   */
  pauseReview(): void {
    if (this.core.disposed) return;
    const r = this.core.state.review;
    const answered = [...Object.keys(r.results), ...Object.keys(r.confirmResults).map(confirmKey)].filter((c) => !r.seen.includes(c));
    this.core.setReview({ active: false, seen: [...r.seen, ...answered] });
    this.endIfDone();
    this.leavePageRound();
  }

  /** The review is over when no card is left: back to 下一步 with the 复习完了。 line. */
  private endIfDone(): void {
    if (currentEntry(this.core.state) === null && this.core.state.review.today.kind === "ready") {
      const was = this.core.state.review.finished;
      this.core.setReview({ active: false, finished: true, roundDone: true, barAt: null });
      const { n, ok } = this.roundResult();
      if (!was && n > 0) {
        this.h.botReviewFinished(n, ok);
        // streak_days (the done card's 连续学习) counts this round only after a re-read; a failed read keeps the value shown.
        if (this.core.state.review.pageRound === null) void this.reads.loadReview(() => undefined);
      }
      this.leavePageRound();
    }
  }

  /** The cards answered this round (a closed confirmation question has no verdict) and how many were right (check correct / cue not 没想起来 / confirm right). */
  private roundResult(): { n: number; ok: number } {
    const r = this.core.state.review;
    let n = 0;
    let ok = 0;
    for (const e of reviewEntries(this.core.state)) {
      if (r.roundBase.includes(e.key)) continue;
      if (e.kind === "item") {
        const x = r.results[e.key];
        if (x === undefined) continue;
        n++;
        if (x.grade !== null ? x.grade !== "forgot" : x.correct === true) ok++;
      } else {
        const x = r.confirmResults[e.confirm.followup_id];
        if (x === undefined || x.closed === true) continue;
        n++;
        if (x.out.correct === true) ok++;
      }
    }
    return { n, ok };
  }

  // ---- 反馈 on a review card: the assistant fixes the card (its fix_card tool) ----

  /** 反馈 (app.js reviewCard): 助手 opens (a new conversation if this one has questions) with the card quoted; nothing is sent. */
  askAboutCard(cell: string): void {
    const r = this.core.state.review;
    if (this.core.disposed || r.today.kind !== "ready") return;
    const item = r.today.value.items.find((i) => i.cell === cell);
    if (item === undefined) return;
    const question = item.mode === "check" ? item.check?.stem ?? "" : item.cue;
    this.core.deps.askAssistant?.(item.page_id, item.page_title, { text: WIKI_COPY.cardQuote(question), heading: item.heading, kind: "wrong_answer" });
  }



  /** 刷新 (复习): GET today's list (a new list: nothing uncovered / answered); nothing while busy. */
  async reloadReview(): Promise<void> {
    if (this.core.disposed || this.core.state.busy) return;
    this.core.deps.pageRoundMemo?.save(null);
    this.core.setReview({ revealed: [], results: {}, confirmResults: {}, seen: [], removed: [], active: false, finished: false, note: null, pageRound: null, pageRoundDone: null });
    await this.reads.loadReview(null);
  }

  /**
   * 再来一批: the next full batch, whatever 每天最多 says (the server's more=true), reviewed in place. The answers of the last
   * round are forgotten here as in 刷新; nothing left to review just drops the button. A failure is a notice.
   */
  async moreReview(): Promise<void> {
    if (this.core.disposed || this.core.state.busy) return;
    const err = this.core.blocked();
    if (err) { this.core.deps.notice?.(err); return; }
    const seq = ++this.reads.reviewSeq;
    let t: ReviewTodayOut;
    try {
      t = await this.core.call(() => this.core.deps.api.reviewToday(true));
    } catch (e) {
      if (!this.core.disposed) this.core.deps.notice?.(errorText(e));
      return;
    }
    if (this.core.disposed || seq !== this.reads.reviewSeq) return;
    if (t.items.length + t.confirms.length === 0) { this.core.setReview({ today: { kind: "ready", value: { ...t, due: 0 } } }); return; }
    for (const k of Object.keys(this.core.state.followups)) if (k.startsWith("card:")) this.core.clearFollowup(k);
    this.h.closeCelebrate();
    this.core.setReview({ today: { kind: "ready", value: t }, revealed: [], results: {}, confirmResults: {}, seen: [], removed: [],
      active: true, finished: false, roundDone: false, roundBase: [], barAt: null, note: null, pageRoundDone: null });
  }

  /** Today's item for `cell` in `mode`, not answered yet; null otherwise (and while a write is in flight). */
  private reviewItem(cell: string, mode: ReviewItemOut["mode"]): ReviewItemOut | null {
    const r = this.core.state.review;
    if (this.core.disposed || this.core.state.busy || r.today.kind !== "ready" || r.results[cell] !== undefined) return null;
    return r.today.value.items.find((i) => i.cell === cell && i.mode === mode) ?? null;
  }

  /**
   * One review POST (a self-grade when `choice` is null, else a check answer): the result on the item, done_today + 1
   * (a check answer counts too, as the server's reviews_done does); the graded page goes to 「待同步」 when it is not the open page and its content changed (an
   * open one is synced by the refresh); then P5 refresh. A failure: its text in review.note, no result, nothing retried.
   */
  private async sendReview(item: ReviewItemOut, body: ReviewGradeIn, choice: number | null): Promise<void> {
    if (!this.core.beginArea("review", { what: "review-grade", abortable: false })) return;
    const ctx = this.core.currentCtx();
    this.core.setReview({ note: null });
    const inPageRound = this.core.state.review.pageRound !== null;
    let out: ReviewGradeOut;
    try {
      out = await this.core.call(() => this.core.deps.api.gradeReview(inPageRound ? { ...body, page_round: true } : body));
    } catch (e) { this.core.areaFailed("review", e); return; }
    if (this.core.disposed) return;
    if (inPageRound) {
      const memo = this.core.deps.pageRoundMemo;
      const m = memo?.load() ?? null;
      if (m !== null) memo?.save({ ...m, answered: [...m.answered, item.card_id] });
    }
    const changed = this.notes.noteChange(out.change);
    const onShown = ctx !== null && out.page_id === ctx.pageId;
    if (ctx !== null && onShown) this.core.touch(ctx, changeMarks(out.change));
    if (out.page_id !== null && !onShown && changed) this.notes.pendingSync.add(out.page_id);
    // A plain line of what the system did (the card's page is the one graded: page_id is always sent).
    const text = reviewNote({ conceptName: item.concept_name, sectionId: item.section_id, heading: item.heading, band: out.band,
      change: out.change, check: choice !== null, correct: out.correct });
    const r = this.core.state.review;
    const today = r.today.kind === "ready" && !inPageRound
      ? { kind: "ready" as const, value: { ...r.today.value, done_today: r.today.value.done_today + 1 } } : r.today;
    const grade = body.grade ?? null;
    this.core.clearFollowup(`card:${item.card_id}`);
    this.core.answerRefs.set(`card:${item.card_id}`, out.answer_ref);
    const result = { band: out.band, grade, correct: out.correct, correctIndex: out.correct_index, choice, line: ok(text) };
    // In the in-place review every answered card stays on screen with its result line (app.js reviewCard: the result,
    // the follow-ups after a wrong check answer, then 下一张 / 完成); 下一张 moves on.
    this.core.setReview({ today, results: { ...r.results, [item.cell]: result }, fresh: { key: item.cell, at: Date.now() }, note: r.active ? result.line : this.core.state.review.note });
    await this.reads.refresh(ctx, changed && onShown, REVIEW);
  }

  /**
   * One POST followup (slip / learned: one model call on the server, no page touched; unclear: none). Only an idle question
   * (or one whose last try failed) sends. A success shows our sentence and, for 看不懂, the place to review (or the
   * suggestion to learn it) and opens the assistant with the question quoted; a failure is one plain line (never the
   * server's text), the buttons stay and nothing is retried by itself.
   */
  async followup(target: FollowupTarget, choice: FollowupChoice): Promise<void> {
    const key = followupKey(target);
    const cur = this.core.followupView(key);
    if (this.core.disposed || cur.kind !== "idle") return;
    const why = this.core.followupBlockedWhy();
    if (why !== null) { this.core.setFollowup(key, { kind: "idle", error: why }); return; }
    if (choice === "slip" && target.idk) return;
    this.core.setFollowup(key, { kind: "sending", choice });
    const ref = this.core.answerRefs.get(key);
    let out: CheckFollowupOut;
    try {
      out = await this.core.call(() => this.core.deps.api.followup(target.pageId, target.sid, { choice, artifact_id: target.artifactId, ...(ref !== undefined && ref !== "" ? { answer_ref: ref } : {}) }));
    } catch (e) {
      if (this.core.disposed) return;
      if (e instanceof ApiError && e.status === 409 && e.detail === WIKI_COPY.followupAlreadyServer) { this.core.setFollowup(key, { kind: "done", choice, note: WIKI_COPY.followupDone }); return; }
      this.core.setFollowup(key, { kind: "idle", error: followupErrorText(e) });
      return;
    }
    if (this.core.disposed) return;
    // No confirmation sentence (user review): the row shows the chosen button pressed.
    this.core.setFollowup(key, { kind: "done", choice, note: "" });
    if (choice === "unclear" && out.quote !== null) {
      if (this.core.deps.askAssistant) this.core.deps.askAssistant(target.pageId, target.pageTitle, { text: clipQuote(out.quote.text), heading: out.quote.heading, kind: out.quote.kind });
      else this.core.setFollowup(key, { kind: "done", choice, note: WIKI_COPY.followupNoAssistant });
    }
  }

  /** Opens `t.page_id` (read first unless it is the open page) and scrolls to its section; false = it could not be opened. */
  private async openReviewTarget(t: ReviewTargetOut): Promise<boolean> {
    const shown = (): boolean => { const p = this.core.state.page; return p.kind === "open" && p.page.page_id === t.page_id; };
    if (!shown()) await this.h.openWikiPage(t.page_id);
    if (this.core.disposed || !shown()) return false;
    return t.section_id === null ? true : await this.h.jumpTo(t.section_id);
  }

  /**
   * A due confirmation question in the review: one POST {choice} (-1 = 不知道); the verdict on the card (kept until 下一张),
   * then P5 refresh. A failure: its plain text in review.note, nothing retried.
   */
  async answerConfirm(followupId: string, choice: number): Promise<void> {
    const r0 = this.core.state.review;
    const card = r0.today.kind === "ready" ? r0.today.value.confirms.find((c) => c.followup_id === followupId) : undefined;
    if (this.core.disposed || this.core.state.busy || card === undefined || r0.confirmResults[followupId] !== undefined) return;
    if (!this.core.beginArea("review", { what: "review-grade", abortable: false })) return;
    const ctx = this.core.currentCtx();
    this.core.setReview({ note: null });
    let out: ConfirmAnswerOut;
    try {
      out = await this.core.call(() => this.core.deps.api.confirmAnswer(followupId, { choice, artifact_id: card.artifact_id }));
    } catch (e) {
      if (this.core.disposed) return;
      this.core.setAreaNote("review", bad(confirmErrorText(e)));
      // Gone or already answered: the card cannot be answered again, so it is marked done and 下一张 moves on.
      if (e instanceof ApiError && (e.status === 404 || e.status === 409)) {
        const out: ConfirmAnswerOut = { followup_id: followupId, correct: null, correct_index: -2, outcome: "refuted",
          band: { cell: card.cell, from: "none", to: "none" }, reteach: null, signal_id: null };
        this.core.setReview({ confirmResults: { ...this.core.state.review.confirmResults, [followupId]: { choice, out, line: bad(confirmErrorText(e)), closed: true } } });
      }
      this.core.set({ busy: null });
      return;
    }
    if (this.core.disposed) return;
    // No verdict sentence (user review): the options' colours say it.
    const r = this.core.state.review;
    this.core.setReview({ confirmResults: { ...r.confirmResults, [followupId]: { choice, out, line: null } }, fresh: { key: confirmKey(followupId), at: Date.now() }, note: null });
    await this.reads.refresh(ctx, false, REVIEW);
  }

  /** After a refuted 我学会了: 重新读 opens the section of the page that teaches it. */
  async reteach(followupId: string): Promise<void> {
    const res = this.core.state.review.confirmResults[followupId];
    const t = res?.out.reteach ?? null;
    if (this.core.disposed || t === null) return;
    if (!(await this.openReviewTarget(t))) {
      if (!this.core.disposed) this.core.setReview({ note: bad(WIKI_COPY.confirmReteachFailed) });
    }
  }

  /**
   * After a refuted 我学会了: 还没掌握 (like the wrong-answer one) opens 助手 with the question quoted (题目 / 我选的 / 正确答案);
   * nothing is sent.
   */
  confirmUnclear(followupId: string): void {
    const r = this.core.state.review;
    const res = r.confirmResults[followupId];
    const card = r.today.kind === "ready" ? r.today.value.confirms.find((c) => c.followup_id === followupId) : undefined;
    if (this.core.disposed || res === undefined || card === undefined) return;
    const opts = card.check.options;
    const text = WIKI_COPY.wrongQuote(card.check.stem, res.choice === -1 ? WIKI_COPY.idk : opts[res.choice] ?? "", opts[res.out.correct_index] ?? "");
    this.core.deps.askAssistant?.(card.page_id, card.page_title, { text: clipQuote(text), heading: res.out.reteach?.heading ?? null, kind: "wrong_answer" });
  }
}
