/**
 * What the end-of-note block, the line at the top of the page and the 「已更新」 tags show, as pure functions of
 * the controller's state. The renderers (end-block.ts, page-lines.ts) only draw this; the controller owns every change.
 */
import { writeBlockedReason } from "../actions";
import { WIKI_COPY } from "../copy";
import type { FeedbackMarkOut, ReviewFirstOut, StepOut } from "../schema";
import { EMPTY_END, type EndPhase, type PageEnd, type UndoState, type WikiState } from "../state";
import { appliedLine, appliedRevise } from "../revise/rows";

/**
 * The main button, from the server's step once this page is read (PageOut.next_step, wikiapp/progress.py): the next
 * page (start_page / read / writing / failed) → 继续学习; the next block → 下一块：X; the goal done → 这个目标学完了;
 * no goal (null) → 这一页学完了. `step` is what the button follows after 学完.
 */
export type PrimaryButton =
  | { kind: "next"; label: string; step: StepOut } | { kind: "next-block"; label: string; step: StepOut }
  | { kind: "goal-done"; label: string } | { kind: "page-done"; label: string };

export function primaryOf(step: StepOut | null): PrimaryButton {
  if (step === null || step.kind === "none") return { kind: "page-done", label: WIKI_COPY.endPageDone };
  if (step.kind === "goal_done") return { kind: "goal-done", label: WIKI_COPY.endGoalDone };
  if ((step.kind === "enter_block" || step.kind === "diagnosis") && step.region_name !== "") return { kind: "next-block", label: WIKI_COPY.endNextBlock(step.region_name), step };
  return { kind: "next", label: WIKI_COPY.endNext, step };
}

export interface EndModel {
  chips: PageEnd["draft"]["chips"];
  phase: EndPhase;
  /** Why 继续学习 cannot be used now (offline / not set up / another write); null = it can. */
  blocked: string | null;
  primary: PrimaryButton;
  completed: boolean;
  /** 「建议先复习 X」 before the next step (StepOut.review_first); null = none. */
  reviewFirst: ReviewFirstOut | null;
  /** 收为参考卡 is offered (PageOut.can_shelve); `busy`: something else is running, so it cannot be clicked now. */
  shelve: { busy: boolean } | null;
}

/**
 * The block's view-model for `pageId`; the page must be the open, organized one (else null: no block). No block while
 * the page is being written here (its outline makes the streamed copy organized before any section is in) or after
 * its write was stopped (its line reads 已停止 · 重新写): 「这页内容对你有帮助吗？」 and 继续学习 are for a page read to its end.
 */
export function endModel(s: WikiState, pageId: string): EndModel | null {
  const p = s.page;
  if (p.kind !== "open" || p.page.page_id !== pageId || !p.page.organized) return null;
  if (s.busy?.what === "organize" || s.stopped?.pageId === pageId) return null;
  const e = s.pageEnd[pageId] ?? EMPTY_END;
  const blocked = writeBlockedReason(s);
  const shelve = p.page.can_shelve ? { busy: s.busy !== null || blocked !== null } : null;
  const primary = primaryOf(p.page.next_step);
  const completed = p.page.done && (primary.kind === "page-done" || primary.kind === "goal-done");
  return { chips: e.draft.chips, phase: e.phase, blocked, primary, completed, reviewFirst: p.page.next_step?.review_first ?? null, shelve };
}

/** What the strip at the top of the page says (one line at a time); null = nothing. */
export type TopLine =
  | { kind: "changed"; text: string; canUndo: boolean; undo: UndoState }
  /** The hand edit in the note could not go up: the note stays as typed; 恢复 is the way out. */
  | { kind: "hand-failed"; text: string; note: string; conflict?: boolean }
  /** The last hand edit is on the page: 已写入你的改动：… · 撤销. */
  | { kind: "hand-edited"; text: string; canUndo: boolean; undo: UndoState }
  /** A materials draft was confirmed (app.js reviseTop): 本页已按《X》更新 N 节 · 撤销 (the ledger's undo). */
  | { kind: "revised"; text: string; actionId: string }
  /**
   * The page is a reference card (shelved): its card line (PageOut.shelf_line) when it has one, else 这一页收起来了。
   * 恢复成完整页 is offered here (a collapsed page has no ⋯ menus).
   */
  | { kind: "collapsed"; text: string; busy: boolean };

const PREFIXES = [WIKI_COPY.topChanged(""), WIKI_COPY.topChangedLegacy];

/** The summary without a 「本页已更新：」 / 「这一页按你的反馈改了：」 lead (the server's page-level summary carries one; the plan's does not). */
export function changeSummary(m: FeedbackMarkOut): string {
  const t = m.summary.trim();
  const lead = PREFIXES.find((x) => t.startsWith(x));
  return lead === undefined ? t : t.slice(lead.length);
}

/**
 * Organizing uses the note header's progress strip. Otherwise show the feedback result (撤销), a confirmed materials
 * draft (撤销), or the collapsed-page line. Which model wrote the page is never shown.
 */
export function topLine(s: WikiState, pageId: string): TopLine | null {
  const p = s.page;
  if (p.kind !== "open" || p.page.page_id !== pageId || !p.page.organized) return null;
  const e = s.pageEnd[pageId] ?? EMPTY_END;
  // The note header owns the one writing strip, including stop / restart and live progress.
  if (s.busy?.what === "organize") return null;
  if (e.handFailed !== null) return { kind: "hand-failed", text: e.handFailed.text, note: e.handFailed.note, ...(e.handFailed.conflict ? { conflict: true } : {}) };
  const m = p.page.feedback;
  const hand = p.page.hand_edit;
  if (e.handLine && hand !== null && (m === null || new Date(hand.at) > new Date(m.at))) {
    return { kind: "hand-edited", text: hand.summary, canUndo: hand.can_undo, undo: e.undo };
  }
  if (m !== null) return { kind: "changed", text: WIKI_COPY.topChanged(changeSummary(m)), canUndo: m.can_undo, undo: e.undo };
  const rv = appliedRevise(s, pageId);
  if (rv !== null) return { kind: "revised", text: appliedLine(rv.revise), actionId: rv.action_id };
  if (!p.page.collapsed) return null;
  const card = p.page.shelf_line.trim();
  return { kind: "collapsed", text: card === "" ? WIKI_COPY.topCollapsed : card, busy: s.busy !== null };
}

/** Where a page other goals reuse was written and what the goal it came from adds, every goal's with no goal context (PageOut.written_for / goal_notes); null = no goal reuses it. */
export function pageReuse(s: WikiState, pageId: string): { writtenFor: string; notes: { goal: string; note: string }[] } | null {
  const p = s.page;
  if (p.kind !== "open" || p.page.page_id !== pageId || p.page.written_for === "") return null;
  const from = s.pageGoal?.pageId === pageId ? s.pageGoal.mapId : null;
  return { writtenFor: p.page.written_for, notes: p.page.goal_notes.filter((n) => n.note !== "" && (from === null || n.map_id === from)) };
}

/** The sections that carry the last feedback patch in the note (none while the note was not patched): tagged 已更新. */
export function patchedSections(s: WikiState, pageId: string): ReadonlySet<string> {
  const p = s.page;
  if (p.kind !== "open" || p.page.page_id !== pageId) return new Set();
  return new Set(p.page.feedback?.sections ?? []);
}
