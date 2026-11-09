import { tr } from "../i18n/index";
/**
 * Chinese descriptions of structured results (labels, op results, errors).
 * The backend returns only structure; LLM-written text is passed through verbatim.
 */
import { BUSY_FALLBACK, TIMEOUT_TEXT, busyText, isBusyApiError, isTimeoutApiError } from "../api/busy";
import { ApiError, OfflineError } from "../api/errors";
import { WIKI_COPY } from "./copy";
import type { Names } from "./names";
import {
  ASPECTS, splitCell,
  type Aspect, type Band, type CellChangeOut, type ChangeOut, type DiffNoteOut, type DraftNotesOut,
  type ErrorCode, type ErrorEvent, type Grade, type NoteKind, type NoteOut, type PathPageStatus,
} from "./schema";

export const ASPECT_LABEL: Record<Aspect, { short: string; full: string }> = {
  get term() { return { short: tr("wiki.format.ASPECT_LABEL.term.short.1"), full: tr("wiki.format.ASPECT_LABEL.term.full.2") }; }, get purpose() { return { short: tr("wiki.format.ASPECT_LABEL.purpose.short.3"), full: tr("wiki.format.ASPECT_LABEL.purpose.full.4") }; },
  get notation() { return { short: tr("wiki.format.ASPECT_LABEL.notation.short.5"), full: tr("wiki.format.ASPECT_LABEL.notation.full.6") }; }, get procedure() { return { short: tr("wiki.format.ASPECT_LABEL.procedure.short.7"), full: tr("wiki.format.ASPECT_LABEL.procedure.full.8") }; },
  get interpret() { return { short: tr("wiki.format.ASPECT_LABEL.interpret.short.9"), full: tr("wiki.format.ASPECT_LABEL.interpret.full.10") }; }, get mechanism() { return { short: tr("wiki.format.ASPECT_LABEL.mechanism.short.11"), full: tr("wiki.format.ASPECT_LABEL.mechanism.full.12") }; } };
/** What the learner reads for a band: K's three levels (guardrails.py BAND_WORD). */
export const BAND_WORD: Record<Band, string> = { get known() { return tr("wiki.format.BAND_WORD.known.13"); }, get likely() { return tr("wiki.format.BAND_WORD.likely.14"); }, get shaky() { return tr("wiki.format.BAND_WORD.shaky.15"); }, get gap() { return tr("wiki.format.BAND_WORD.gap.16"); }, get none() { return tr("wiki.format.BAND_WORD.none.17"); } };
export const BAND_LABEL: Record<Band, string> = { get known() { return tr("wiki.format.BAND_LABEL.known.18"); }, get likely() { return tr("wiki.format.BAND_LABEL.likely.19"); }, get shaky() { return tr("wiki.format.BAND_LABEL.shaky.20"); }, get gap() { return tr("wiki.format.BAND_LABEL.gap.21"); }, get none() { return tr("wiki.format.BAND_LABEL.none.22"); } };
export const NOTE_KIND_LABEL: Record<NoteKind, string> = { get quiz() { return tr("wiki.format.NOTE_KIND_LABEL.quiz.23"); }, get check() { return tr("wiki.format.NOTE_KIND_LABEL.check.24"); }, get self() { return tr("wiki.format.NOTE_KIND_LABEL.self.25"); }, get feedback() { return tr("wiki.format.NOTE_KIND_LABEL.feedback.26"); }, get ask() { return tr("wiki.format.NOTE_KIND_LABEL.ask.27"); }, get declare() { return tr("wiki.format.NOTE_KIND_LABEL.declare.28"); }, get review() { return tr("wiki.format.NOTE_KIND_LABEL.review.29"); } };
export const GRADE_LABEL: Record<Grade, string> = { get recalled() { return tr("wiki.format.GRADE_LABEL.recalled.30"); }, get fuzzy() { return tr("wiki.format.GRADE_LABEL.fuzzy.31"); }, get forgot() { return tr("wiki.format.GRADE_LABEL.forgot.32"); } };
export const PATH_STATUS_LABEL: Record<PathPageStatus, string> = { get not_started() { return tr("wiki.format.PATH_STATUS_LABEL.not_started.33"); }, get learning() { return tr("wiki.format.PATH_STATUS_LABEL.learning.34"); }, get done() { return tr("wiki.format.PATH_STATUS_LABEL.done.35"); } };
export const ERROR_TEXT: Record<ErrorCode, string> = {
  get llm_unavailable() { return tr("wiki.format.ERROR_TEXT.llm_unavailable.36"); }, get llm_busy() { return BUSY_FALLBACK; }, get llm_timeout() { return TIMEOUT_TEXT; }, get llm_failed() { return tr("wiki.format.ERROR_TEXT.llm_failed.37"); },
  get conflict() { return tr("wiki.format.ERROR_TEXT.conflict.38"); }, get not_found() { return tr("wiki.format.ERROR_TEXT.not_found.39"); }, get invalid() { return tr("wiki.format.ERROR_TEXT.invalid.40"); },
  get budget_exceeded() { return WIKI_COPY.budget; }, get internal() { return tr("wiki.format.ERROR_TEXT.internal.41"); } };

export interface OpResult { message: string; llmCalls: number; changed: number }

/** Section id → its heading on the page; null when the page has no such section (web Heading). */
export type Heading = (sid: string) => string | null;

/** Known headings quoted 「…」; unknown ones as 另一页上的一段 / 另一页上的 N 段, unquoted, never the id (web quoted). */
const headingList = (sids: string[], heading: Heading): string => {
  const known: string[] = [];
  let other = 0;
  for (const sid of sids) {
    const h = heading(sid);
    if (h === null) other++; else known.push(`「${h}」`);
  }
  const rest = other === 0 ? "" : other === 1 ? WIKI_COPY.otherPageSection : WIKI_COPY.otherPageSections(other);
  return [known.join(""), rest].filter(Boolean).join("、");
};
export function cellLabel(conceptName: string, aspect: Aspect): string {
  return `${conceptName}·${ASPECT_LABEL[aspect].full}`;
}

export function cellLabelById(cell: string, names: Names): string {
  const { conceptId, aspect } = splitCell(cell);
  const name = names.concept(conceptId) ?? WIKI_COPY.unnamedConcept;
  const known = (ASPECTS as readonly string[]).includes(aspect);
  return `${name}·${known ? ASPECT_LABEL[aspect as Aspect].full : aspect}`;
}

export function changedCount(c: ChangeOut): number {
  return new Set([...c.rewritten, ...c.inserted, ...c.moved, ...c.folded, ...c.unfolded]).size;
}

export function staleTail(c: ChangeOut): string {
  const n = c.other_pages_stale.length;
  return n === 0 ? "" : tr("wiki.format..42", { v0: n });
}

export function foldTail(c: ChangeOut, heading: Heading, skip?: string): string {
  const keep = (sids: string[]) => sids.filter((sid) => sid !== skip);
  const folded = keep(c.folded);
  const unfolded = keep(c.unfolded);
  const parts: string[] = [];
  if (folded.length) parts.push(tr("wiki.format..43", { v0: headingList(folded, heading) }));
  if (unfolded.length) parts.push(tr("wiki.format..44", { v0: headingList(unfolded, heading) }));
  return parts.join("，");
}

export function opResult(lead: string, c: ChangeOut, heading: Heading, skip?: string): OpResult {
  const tails = [foldTail(c, heading, skip), staleTail(c)].filter(Boolean);
  // A lead that ends a sentence (答对了 ✓。) drops its 。 when more follows, so the line never reads 「。；」.
  const message = [tails.length ? lead.replace(/。$/, "") : lead, ...tails].filter(Boolean).join("；");
  return { message, llmCalls: c.llm_calls, changed: changedCount(c) };
}

/** The result line: the message, plus how many sections changed when asked for and any did. Never the LLM call count. */
export function opLine(r: OpResult, withChanged: boolean): string {
  if (!withChanged || r.changed === 0) return r.message;
  return r.message ? r.message + WIKI_COPY.changedTail(r.changed) : WIKI_COPY.changedAlone(r.changed);
}

export function diffNote(d: DiffNoteOut | null, heading: Heading, names: Names): string | null {
  if (d === null) return null;
  const parts: string[] = [];
  if (d.cell_changes.length) {
    const shown = d.cell_changes.slice(0, 4)
      .map((c) => `${cellLabelById(c.cell, names)} ${BAND_LABEL[c.from]}→${BAND_LABEL[c.to]}`).join("；");
    const more = d.cell_changes.length > 4 ? tr("wiki.format.more.45", { v0: d.cell_changes.length }) : "";
    parts.push(tr("wiki.format..46", { v0: shown, v1: more }));
  }
  if (d.folded.length) parts.push(tr("wiki.format..47", { v0: headingList(d.folded, heading) }));
  if (d.unfolded.length) parts.push(tr("wiki.format..48", { v0: headingList(d.unfolded, heading) }));
  if (d.patched.length) parts.push(tr("wiki.format..49", { v0: headingList(d.patched, heading) }));
  return parts.length ? tr("wiki.format..50", { v0: parts.join("。") }) : null;
}

/** The backend's raw values on a "declare" note. */
const DECLARE_TEXT: Partial<Record<string, string>> = {
  get know() { return WIKI_COPY.declareKnow; }, get dont() { return WIKI_COPY.declareDont; }, get clear() { return WIKI_COPY.declareClear; } };

/** An own key of `table` only (Object.hasOwn semantics; the ES2021 lib has no Object.hasOwn): "constructor" is not a label. */
function ownLabel(table: Partial<Record<string, string>>, key: string): string | undefined {
  return Object.prototype.hasOwnProperty.call(table, key) ? table[key] : undefined;
}

export function noteLine(n: NoteOut): string {
  switch (n.kind) {
    case "self": return tr("wiki.format..51", { v0: n.text });
    // The backend writes the chosen option's text verbatim; for 不知道 it writes the question's stem instead, so the
    // line then reads 选了「<stem>」 (same as the web; a joint backend / web wording fix is in the carry-over).
    case "quiz": case "check": return tr("wiki.format..52", { v0: n.text });
    case "declare": return `${NOTE_KIND_LABEL.declare}：${ownLabel(DECLARE_TEXT, n.text) ?? n.text}`;
    // A self-graded review writes the grade; a check-mode review writes the option text (or the stem for 不知道).
    case "review": return `${NOTE_KIND_LABEL.review}：${ownLabel(GRADE_LABEL, n.text) ?? n.text}`;
    default: return `${NOTE_KIND_LABEL[n.kind]}：${n.text}`;
  }
}

export function draftNotes(n: DraftNotesOut): string[] {
  const out: string[] = [];
  if (n.from_background > 0) out.push(tr("wiki.format..53", { v0: n.from_background }));
  if (n.reused_concepts > 0) out.push(tr("wiki.format..54", { v0: n.reused_concepts }));
  return out;
}

/** A review result: `概念·层面 旧档位→新档位`, then the change tails, as an op line with the changed count. */
/** Band order for "more / less sure": 没证据 is not a tier, so a change from or to it is neither. */
const TIER: Partial<Record<Band, number>> = { gap: 0, shaky: 1, likely: 2, known: 3 };

/**
 * The plain line under a graded review card: what the system actually did, nothing more. A check answer leads with
 * its verdict, a self-grade with 记下了; then a real tier change of the cell (更有把握 / 还不太熟), then what happened
 * to the card's page (its own section by heading; other sections by count). No promise about when it comes back:
 * the schedule follows the recall curve, not the grade.
 */
export function reviewNote(o: {
  conceptName: string; sectionId: string; heading: string; band: CellChangeOut; change: ChangeOut; check: boolean; correct: boolean | null;
}): string {
  const C = WIKI_COPY;
  // A check answer has no verdict sentence (user review: the options' colours say it); a self-grade leads with 记下了。
  const parts: string[] = o.check ? [] : [C.reviewNoted];
  const from = TIER[o.band.from];
  const to = TIER[o.band.to];
  if (from !== undefined && to !== undefined && to !== from) {
    parts.push(to > from ? C.reviewSurer(o.conceptName) : C.reviewLessSure(o.conceptName));
  }
  const c = o.change;
  const others = (xs: string[]): number => xs.filter((x) => x !== o.sectionId).length;
  if (c.unfolded.includes(o.sectionId)) parts.push(C.reviewUnfoldedHere(o.heading));
  if (others(c.unfolded) > 0) parts.push(C.reviewUnfolded(others(c.unfolded)));
  if (c.folded.includes(o.sectionId)) parts.push(C.reviewFoldedHere(o.heading));
  if (others(c.folded) > 0) parts.push(C.reviewFolded(others(c.folded)));
  const rewrote = new Set([...c.rewritten, ...c.inserted]).size;
  if (rewrote > 0) parts.push(C.reviewRewrote(rewrote));
  return parts.join("");
}

export function errorText(e: unknown): string {
  if (e instanceof OfflineError) return WIKI_COPY.offline;
  if (isBusyApiError(e)) return busyText();
  if (isTimeoutApiError(e)) return TIMEOUT_TEXT;
  if (e instanceof ApiError) {
    switch (e.status) {
      case 409: return ERROR_TEXT.conflict;
      case 429: return e.code === "budget_exceeded" && e.detail.trim() ? e.detail : WIKI_COPY.budget;
      case 503: return ERROR_TEXT.llm_unavailable;
      case 404: return ERROR_TEXT.not_found;
      case 422: return ERROR_TEXT.invalid;
      default: return WIKI_COPY.failed;
    }
  }
  return WIKI_COPY.failed;
}

/**
 * A refused ledger decision (批准 / 不用 / 撤销): a 409 carries the executor's own plain Chinese reason (bot/actions.py
 * ActionRefused → NotDecidable, e.g. 这条改动已撤销，不能撤销), which is what really happened; anything else as errorText.
 */
export function decideErrorText(e: unknown): string {
  if (e instanceof ApiError && e.status === 409) {
    const d = e.detail.trim();
    if (e.code !== null && d !== "" && !/^[[{]/.test(d)) return d;
  }
  return errorText(e);
}

/** A failed 粗心选错了 / 我学会了 / 看不懂: one plain line per status (the server's text is never shown). */
export function followupErrorText(e: unknown): string {
  const C = WIKI_COPY;
  if (e instanceof ApiError) {
    switch (e.status) {
      case 404: return C.followupNotFound;
      case 409: return C.followupRetry;
      case 422: return C.followupInvalid;
      case 502: return C.followupNoQuestion;
      case 503: return C.followupNoModel;
      default: break;
    }
  }
  return e instanceof ApiError && e.status === 429 || e instanceof OfflineError ? errorText(e) : C.followupFailed;
}

/** A failed answer to a confirmation question. */
export function confirmErrorText(e: unknown): string {
  const C = WIKI_COPY;
  if (e instanceof ApiError) {
    switch (e.status) {
      case 404: return C.confirmNotFound;
      case 409: return C.confirmAnswered;
      case 422: return C.confirmInvalid;
      default: break;
    }
  }
  return errorText(e);
}

/** ERROR_TEXT[code]; the detail is shown only for llm_busy and budget_exceeded (it names the limit). */
export function streamErrorText(e: ErrorEvent): string {
  if (e.code === "llm_busy") return busyText();
  if (e.code === "budget_exceeded" && e.detail.trim() !== "") return e.detail;
  return (ERROR_TEXT as Partial<Record<string, string>>)[e.code] ?? WIKI_COPY.failed;
}
