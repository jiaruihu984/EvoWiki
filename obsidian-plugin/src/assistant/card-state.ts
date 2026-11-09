import { tr } from "../i18n/index";
/**
 * State of the cards the assistant's tools make (spec 2026-10-15 §2): an action card (a ledger row: a draft to confirm, or a
 * finished action with its pages) and a quiz card. Pure data and functions; action-card.ts / quiz-card.ts draw them, the
 * controller changes them. A card is shown once per conversation, whatever turn re-lists its id (an undo turn).
 */
import { ROW_STATUS } from "./copy";
import type { ActionOut, DiffLine, MadeOut } from "../wiki/bot/bot-schema";
import type { ActionCard, ActionDraft, ActionEvent, ActionItem } from "./types";
import type { Band, WriteOut } from "../wiki/schema";
import { BAND_WORD } from "../wiki/format";

/**
 * One row of the editable draft: a page of the proposal (`key`) or one the learner added (`key` null). `level`: a goal
 * block's level word (基础 / 核心 / 进阶 / 拓展; "" when none, and for an added row).
 */
export interface DraftRow { key: string | null; title: string; covers: string; level: string; isNew: boolean }
/** What the card shows: a draft waiting for 确认, a done action, or the result line of a cancelled / undone / failed one. */
export type CardPhase = "wait" | "live" | "rejected" | "undone" | "failed";
export interface ActionCardState {
  actionId: string; kind: string; title: string; items: ActionItem[]; status: string; confirm: boolean; canUndo: boolean;
  /** goal_new's summarized topic, separate from the action card's formatted heading. */
  goalTitle: string | null;
  /** The ledger's reason for a failed row ("" otherwise). */
  detail: string;
  /** The waiting draft this one replaced (a goal's blocks edited in chat; "" = none): only that card is marked replaced. */
  replaces: string;
  /** The edited page list (only while waiting); `edited` is true once it differs from `items`. */
  draft: DraftRow[]; edited: boolean;
  /** Rule text from the ledger; null until the full proposal has been read. */
  ruleText: string | null;
  /** 改动对比 for a card without a page list (rule, merge); filled from the ledger. */
  diff: DiffLine[];
  /** The request in flight, and the text of the last failed one. */
  busy: "approve" | "reject" | "undo" | "redraft" | null; error: string;
  /** The last request did not go through (offline, a 5xx): the card shows only a quiet 请重试 that sends it again. */
  retry: "approve" | "reject" | "undo" | "redraft" | null;
  /** A materials draft (kind page_revise): where its draft is, from the ledger (null until a ledger row was read). */
  revise: CardRevise | null;
  /** Where the row's result is (ActionOut.page_id / section_id / result.map_id), from the ledger; null until read. */
  at: { pageId: string | null; sectionId: string | null; mapId: string | null } | null;
}

/** 讲解方式 rows: a rule, a preference remembered from the chat. */
/** A group of 待确认的记忆 settled in the chat (memory/conflicts.py RESOLVE_KIND): 查看详情 opens 管理偏好, as a rule's does. */
export const CONFLICT_KIND = "memory_conflict_resolve";
const RULE_KINDS = new Set(["rule", "stated_rule", CONFLICT_KIND]);
/** The RSI log's own rows: their change is what 个性化 · 活动记录 shows. */
const PARAMS_KINDS = new Set(["rsi_patch", "rsi_method"]);

/**
 * 查看详情 opens the RESULT of the action (user 2026-10-28: 「那肯定是看这个操作的结果」), never the Bot log for its own sake:
 * a goal → 学点新的 on it; a rule or preference → 个性化 · 管理偏好; an RSI change → its row in 个性化 · 活动记录; a page it wrote / changed /
 * split / merged / gave an interactive → that page (at the section, when the row names one). Null: nothing to show (no 查看详情).
 */
export type CardResult =
  | { kind: "goal"; actionId: string; mapId: string | null }
  | { kind: "rules" } | { kind: "params" }
  | { kind: "page"; pageId: string; sectionId: string | null };

export function cardResult(c: ActionCardState): CardResult | null {
  if (c.kind === "goal_new") return c.at?.mapId || c.status === "executed" ? { kind: "goal", actionId: c.actionId, mapId: c.at?.mapId ?? null } : null;
  if (RULE_KINDS.has(c.kind)) return { kind: "rules" };
  if (PARAMS_KINDS.has(c.kind)) return { kind: "params" };
  const page = c.at?.pageId || c.revise?.pageId || c.items.find((i) => i.page_id)?.page_id || null;
  // the page the row changed (its section), else the first page it made or lists (a split's first new page)
  if (c.at?.pageId && c.at.sectionId) return { kind: "page", pageId: c.at.pageId, sectionId: c.at.sectionId };
  const made = c.items.find((i) => i.page_id)?.page_id ?? null;
  const id = c.kind === "page_split" || c.kind === "page_merge" || c.kind === "page_new" ? made ?? page : page;
  return id ? { kind: "page", pageId: id, sectionId: null } : null;
}
/**
 * `state`: writing / ready / failed; `n`: the sections it changes; `newer`: the page has a newer draft than this one (the card
 * then keeps one line, 有新的草稿).
 */
/** The ledger's fixed detail of a row refused because its page moved (bot/actions.py STALE_DETAIL). */
export const LEDGER_STALE = "ledger_stale";

/** `stale`: the page changed where the draft goes, it cannot be put in as it is (the card offers 重试, not 确认替换). */
export interface CardRevise { state: "writing" | "ready" | "failed"; pageId: string; n: number; newer: boolean; stale: boolean }

/** The page_revise part of a ledger row as the card keeps it (`newer` is the card's own, set by the controller). */
export function cardRevise(o: ActionOut, newer: boolean): CardRevise | null {
  const r = o.revise;
  if (r == null) return null;
  return { state: r.state, pageId: r.page_id || o.page_id || "", n: r.sections.length, newer, stale: r.stale };
}

export function cardPhase(status: string): CardPhase {
  switch (status) {
    case "proposed": return "wait";
    case "approved": case "executed": return "live";
    case "rejected": return "rejected";
    case "undone": return "undone";
    default: return "failed";   // failed, or a word this plugin does not know: never shown as done
  }
}

/** A merge keeps its pages together (dropping one would make no merge): it shows the diff, not an editable list. */
export const draftable = (c: Pick<ActionCardState, "kind" | "items">): boolean => c.items.length > 0 && !c.kind.endsWith("_merge");

export const rowOf = (i: ActionItem): DraftRow => ({ key: i.key, title: i.title, covers: i.covers ?? "", level: i.level ?? "", isNew: false });
const itemsOf = (x: { items?: ActionItem[] }): ActionItem[] => (x.items ?? []).map((i) => ({ ...i }));

export function newActionCard(e: ActionEvent | ActionCard): ActionCardState {
  const items = itemsOf(e);
  return {
    actionId: e.action_id, kind: e.kind, title: e.title, goalTitle: e.goal_title, items, status: e.status ?? "proposed", confirm: e.confirm ?? false,
    canUndo: e.can_undo ?? false, detail: "detail" in e ? e.detail ?? "" : "", replaces: e.replaces ?? "", draft: items.map(rowOf), edited: false, diff: [], busy: null, error: "",
    retry: null, revise: null, ruleText: null,
    // a card read back with its conversation knows where its change is (ActionCard.page_id / section_id / result)
    at: "section_id" in e && (e.page_id || e.section_id || mapOf(e.result)) ? { pageId: e.page_id ?? null, sectionId: e.section_id ?? null, mapId: mapOf(e.result) } : null,
  };
}

const mapOf = (r: Record<string, unknown> | undefined): string | null => (typeof r?.map_id === "string" && r.map_id !== "" ? r.map_id : null);

/** The same card again (a re-sent `action` event, a replay): the row's new status, title, list; the learner's draft edits survive while it still waits. */
export function updateActionCard(c: ActionCardState, e: ActionEvent | ActionCard): void {
  const items = itemsOf(e);
  const was = cardPhase(c.status);
  c.kind = e.kind; c.title = e.title; c.goalTitle = e.goal_title; c.items = items; c.status = e.status ?? c.status; c.confirm = e.confirm ?? c.confirm;
  c.canUndo = e.can_undo ?? c.canUndo; if ("detail" in e) c.detail = e.detail ?? "";
  if (e.replaces) c.replaces = e.replaces;
  if (cardPhase(c.status) !== "wait" || was !== "wait" || !c.edited) { c.draft = items.map(rowOf); c.edited = false; }
  c.busy = null;
}

/** The ledger row `pages` as card items (service/assistant tools actions.py items_of). */
export function itemsOfContent(content: Record<string, unknown> | undefined): ActionItem[] | null {
  const pages = content?.pages;
  if (!Array.isArray(pages)) return null;
  const out: ActionItem[] = [];
  for (const p of pages) {
    if (p === null || typeof p !== "object") continue;
    const o = p as Record<string, unknown>;
    if (typeof o.key !== "string") continue;
    out.push({ key: o.key, title: typeof o.title === "string" && o.title !== "" ? o.title : o.key,
      page_id: typeof o.page_id === "string" ? o.page_id : null, covers: typeof o.covers === "string" && o.covers !== "" ? o.covers : null, n: null,
      level: typeof o.level === "string" && o.level !== "" ? o.level : null });
  }
  return out.length > 0 ? out : null;
}

/**
 * An executed row's list with the pages it made (actions.py `_made_items`): each item is matched by its draft key (an item that
 * made no page is not listed); a merge lists its one new page with its folder (asst.js mergeFlow `s`).
 */
export function madeItems(kind: string, items: ActionItem[], made: readonly MadeOut[], title: string): ActionItem[] {
  if (made.length === 0) return items;
  if (kind === "page_merge") {
    const m = made[0]!;
    return [{ key: m.page_id, title, page_id: m.page_id, n: m.n, covers: m.folder !== "" ? tr("assistant.card-state.covers.2", { v0: m.folder }) : null, level: null }];
  }
  const byKey = new Map(made.flatMap((m) => (m.key !== null ? [[m.key, m] as const] : [])));
  if (byKey.size === 0) return items;
  return items.flatMap((i) => { const m = byKey.get(i.key); return m ? [{ ...i, page_id: m.page_id, n: m.n }] : []; });
}

/** The ledger only moves forward: proposed -> approved -> executed -> undone, or proposed -> rejected / failed. */
const RANK: Record<string, number> = { proposed: 0, approved: 1, executed: 2, rejected: 3, failed: 3, undone: 3 };
export function forward(from: string, to: string): boolean {
  if (from === to) return false;
  // a draft whose 确认 failed because the page moved, written again (重试 → …/redraft): it waits again
  if (from === "failed" && to === "proposed") return true;
  if (from === "executed") return to === "undone";
  const a = RANK[from], b = RANK[to];
  return a !== undefined && b !== undefined && b > a;
}

/** What the card shows of a row, as a string: equal strings = nothing to redraw. */
export function cardSig(c: ActionCardState): string {
  return JSON.stringify([c.title, c.goalTitle, c.status, c.canUndo, c.detail, c.items.map((i) => [i.key, i.title, i.page_id ?? null, i.n ?? null, i.covers ?? null, i.level ?? null]), c.diff.length, c.revise, c.ruleText]);
}

/** The ledger row after a decision (or as 活动记录 has it now): status, undo, reason (also on a done row: e.g. a merge that stopped), the revised list, the pages made. */
export function applyActionOut(c: ActionCardState, o: ActionOut): void {
  const items = itemsOfContent(o.content) ?? c.items;
  if (o.kind === "rule" && (cardPhase(o.status) !== "wait" || !c.edited)) {
    c.ruleText = typeof o.content.text === "string" ? o.content.text : null;
  }
  c.status = o.status; c.canUndo = o.can_undo; c.confirm = o.confirm; c.detail = o.status === "failed" || o.status === "rejected" || o.status === "executed" ? o.detail : "";
  if (o.diff.length > 0 && !draftable(c)) c.diff = o.diff.map((d) => ({ ...d }));
  const t = o.content?.title;
  c.goalTitle = o.kind === "goal_new" && typeof t === "string" ? t : null;
  c.items = cardPhase(o.status) === "live" ? madeItems(o.kind, items, o.made ?? [], typeof t === "string" && t !== "" ? t : c.title) : items;
  if (cardPhase(o.status) !== "wait") { c.draft = c.items.map(rowOf); c.edited = false; }
  if (o.revise != null) c.revise = cardRevise(o, c.revise?.newer ?? false);
  c.at = { pageId: o.page_id ?? null, sectionId: o.section_id ?? null, mapId: mapOf(o.result) };
  c.busy = null; c.error = ""; c.retry = null;
}

// ---- the draft ----

function markEdited(c: ActionCardState): void {
  c.edited = c.draft.length !== c.items.length || c.draft.some((d, k) => d.isNew || d.key !== c.items[k]?.key || renamed(c, d));
}
/** A row of the proposal whose name the learner changed (学点新的's ⋯ 改名). */
const renamed = (c: ActionCardState, d: DraftRow): boolean =>
  d.key !== null && !d.isNew && d.title !== (c.items.find((i) => i.key === d.key)?.title ?? d.title);
/** ⋯ 改名 on a row: the new name (sent as {key, title}; an added row just takes it). */
export function renameDraft(c: ActionCardState, k: number, title: string): boolean {
  const t = title.trim(), d = c.draft[k];
  if (t === "" || d === undefined || d.title === t) return false;
  d.title = t;
  markEdited(c);
  return true;
}
export function moveDraft(c: ActionCardState, from: number, to: number): boolean {
  if (from === to || from < 0 || to < 0 || from >= c.draft.length || to >= c.draft.length) return false;
  const [x] = c.draft.splice(from, 1);
  c.draft.splice(to, 0, x!);
  markEdited(c);
  return true;
}
export function removeDraft(c: ActionCardState, k: number): boolean {
  if (k < 0 || k >= c.draft.length) return false;
  c.draft.splice(k, 1);
  markEdited(c);
  return true;
}
export function addDraft(c: ActionCardState, title: string): boolean {
  const t = title.trim();
  if (t === "") return false;
  c.draft.push({ key: null, title: t, covers: "", level: "", isNew: true });
  markEdited(c);
  return true;
}
/** The body of approve: the edited list (a page of the proposal by its key, an added one by title), or null when nothing was changed. */
export function draftPayload(c: ActionCardState): ActionDraft | null {
  if (!c.edited) return null;
  if (c.kind === "rule" && c.ruleText !== null) return { text: c.ruleText.trim() };
  return wholeDraft(c);
}
/** The draft as it stands, edited or not (a card edit kept on the row sends the whole list). */
export function wholeDraft(c: ActionCardState): ActionDraft {
  return { pages: c.draft.map((d) => (d.key !== null && !d.isNew ? (renamed(c, d) ? { key: d.key, title: d.title } : { key: d.key })
    : { title: d.title, isNew: true })) };
}
/** A kept card edit came back: the row's list is the card's list now (nothing left to send at 确认). */
export function keptDraft(c: ActionCardState, o: ActionOut): void {
  c.items = itemsOfContent(o.content) ?? c.items;
  c.draft = c.items.map(rowOf); c.edited = false;
}
/** A card edit the server refused: the card goes back to the row's list as last stored. */
export function revertDraft(c: ActionCardState): void {
  c.draft = c.items.map(rowOf); c.edited = false;
}

// ---- live writing status ----

/** The status text of a card row from /wiki/writes: null = nothing to show (no run). `pulse`: the breathing dot goes before the text. */
export function writeLine(w: WriteOut | undefined): { text: string; pulse: boolean; key: string } | null {
  if (w === undefined) return null;
  switch (w.state) {
    case "queued": return { text: tr("assistant.card-state.text.3"), pulse: false, key: "queued" };
    case "planning": case "writing": {
      const of = w.of ?? 0;
      const n = w.state === "writing" && w.section != null && of > 0 ? Math.min(w.section + 1, of) : 0;
      const text = n > 0 ? tr("assistant.card-state.text.4", { v0: n, v1: of }) : tr("assistant.card-state.text.5");
      return { text, pulse: true, key: text };
    }
    case "done": return { text: "", pulse: false, key: "done" };
    case "undone": return { text: tr("assistant.card-state.text.6"), pulse: false, key: "undone" };
    case "failed": return { text: tr("assistant.card-state.text.7"), pulse: false, key: "failed" };
    case "unwritten": return null;
  }
}
/**
 * A page row inside a card (app.js paintWrites under .propcard): 生成中 / 等待中 / ✓ 已完成 / 未生成, no numbers. A page whose
 * status was read and has no run holds its content from before (the demo: not in WRITES), or says `unwritten` (a page a split
 * made: it is written when opened); one not read yet (or whose read failed) shows nothing and keeps the card 进行中 - nothing
 * is claimed that was not seen. `done`: counts toward 已完成 k/n; `busy`: the card is still 进行中.
 */
export function rowWriteLine(w: WriteOut | undefined, known = true): { text: string; pulse: boolean; key: string; done: boolean; busy: boolean } {
  if (w === undefined) {
    return known ? { text: ROW_STATUS.done, pulse: false, key: "done", done: true, busy: false } : { text: "", pulse: false, key: "", done: false, busy: true };
  }
  switch (w.state) {
    case "queued": return { text: ROW_STATUS.queued, pulse: false, key: "queued", done: false, busy: true };
    case "planning": case "writing": return { text: ROW_STATUS.writing, pulse: true, key: "writing", done: false, busy: true };
    case "done": return { text: ROW_STATUS.done, pulse: false, key: "done", done: true, busy: false };
    case "undone": return { text: ROW_STATUS.undone, pulse: false, key: "undone", done: false, busy: false };
    case "failed": return { text: ROW_STATUS.failed, pulse: false, key: "failed", done: false, busy: false };
    case "unwritten": return { text: ROW_STATUS.unwritten, pulse: false, key: "unwritten", done: false, busy: false };
  }
}
export const isWriting = (w: WriteOut | undefined): boolean => w !== undefined && (w.state === "queued" || w.state === "planning" || w.state === "writing");

/** The pages a live card lists (the rows that carry a status). */
export function cardPageIds(c: ActionCardState): string[] {
  return cardPhase(c.status) === "live" ? c.items.flatMap((i) => (i.page_id ? [i.page_id] : [])) : [];
}

// ---- a one-question card (「这个我早会了」's confirm question) ----

export interface QuizItem { stem: string; options: string[]; cell: string }
export interface QuizMastery { node: string; name: string; band: string }
export interface QuizAnswer { correct: boolean; answer: number; answered: number; total: number; correct_count: number; mastery: QuizMastery[] }

export interface QuizState {
  quizId: string; items: QuizItem[];
  /** The question shown; `items.length` = the result. */
  i: number;
  /** After an answer: the option chosen (-1 = 不知道) and the key; `fresh` while the pop / tick should play. */
  pick: number | null; key: number | null; fresh: boolean;
  /** Per question: right / wrong, null = not answered. */
  res: (boolean | null)[];
  busy: boolean; error: string;
  correctCount: number | null; mastery: QuizMastery[] | null;
}

export function newQuiz(quizId: string, items: QuizItem[]): QuizState {
  return { quizId, items: items.map((i) => ({ ...i, options: [...i.options] })), i: 0, pick: null, key: null, fresh: false,
    res: items.map(() => null), busy: false, error: "", correctCount: null, mastery: null };
}

export function quizAnswered(q: QuizState, choice: number, r: QuizAnswer): void {
  q.res[q.i] = r.correct; q.pick = choice; q.key = r.answer; q.fresh = true; q.busy = false; q.error = "";
  if (!q.res.includes(null)) {
    q.correctCount = r.correct_count ?? q.res.filter((x) => x === true).length;
    q.mastery = (r.mastery ?? []).map((m) => ({ ...m }));
  }
}
export function quizNext(q: QuizState): void { q.i = Math.min(q.i + 1, q.items.length); q.pick = null; q.key = null; q.fresh = false; q.error = ""; }

const BAND_COLOR: Record<string, string> = {
  known: "known", 确定会: "known", likely: "likely", 可能会: "likely", shaky: "shaky", 模糊: "shaky", gap: "gap", 不会: "gap", none: "none", 没证据: "none",
};
/** The colour token and the word (app.js BANDW) of a band the server names in Chinese (确定会 / 可能会 / 模糊 / 不会 / 没证据) or by key. */
export function bandOf(band: string): { color: string; word: string } {
  const color = Object.prototype.hasOwnProperty.call(BAND_COLOR, band) ? BAND_COLOR[band]! : "none";
  return { color, word: BAND_WORD[color as Band] };
}
/** Whether the questions on a point were all answered right: the node of a cell is everything before its last dot. */
export function pointRight(q: QuizState, node: string): boolean {
  const own = q.items.map((it, k) => ({ node: it.cell.slice(0, Math.max(0, it.cell.lastIndexOf("."))), k })).filter((x) => x.node === node);
  return own.length > 0 && own.every((x) => q.res[x.k] === true);
}
/** The first wrong question's index, or -1. */
export const firstWrong = (q: QuizState): number => q.res.findIndex((r) => r === false);
