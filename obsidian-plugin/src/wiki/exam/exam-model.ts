import { tr } from "../../i18n/index";
/**
 * 真题练习 (full demo app.js `examBlock` / `examItem`): what the block at the bottom of a wiki page shows, as plain data.
 * GET /wiki/pages/{id}/exam gives the quiz asset (drawn by the EvoUI renderer, graded by the server) and the questions
 * drawn here one card each: 看答案 → the answer and its explanation + 我答对了 / 没答对; a question without an answer has
 * only 我做完了. A pressed choice stays pressed and the buttons stay off; a question reported earlier today (`done`, the
 * choice unknown here) shows one pressed 「今天做过了」. While a report is out every button of that question is off; a failed
 * report says only 没记上，请重试 and the buttons come back. Nothing to show (no quiz, no question) → no block at all.
 * Also the 常考 tag of a path row (its words: GRAPH_COPY) and the 真题 line of a material card. The other words are here.
 */
import { ApiError } from "../../api/errors";
import { GRAPH_COPY } from "../graph-page/copy";
import type { ExamOpenItemOut, ExamSelfIn, ExamSelfOut, ExamSummaryOut, PageExamOut } from "../schema";

export type ExamResult = ExamSelfIn["result"];

export const EXAM_COPY = {
  get title() { return tr("wiki.exam.exam-model.EXAM_COPY.title.1"); },
  /** The ? by the title: 来自《X》《Y》的往年题. */
  sourcesTip: (titles: readonly string[]) => tr("wiki.exam.exam-model.EXAM_COPY.sourcesTip.2", { v0: titles.map((t) => `《${t}》`).join("") }),
  get sourcesLabel() { return tr("wiki.exam.exam-model.EXAM_COPY.sourcesLabel.3"); },
  number: (n: string) => tr("wiki.exam.exam-model.EXAM_COPY.number.4", { v0: n }),
  get context() { return tr("wiki.exam.exam-model.EXAM_COPY.context.5"); },
  get show() { return tr("wiki.exam.exam-model.EXAM_COPY.show.6"); },
  answer: (a: string) => tr("wiki.exam.exam-model.EXAM_COPY.answer.7", { v0: a }),
  get right() { return tr("wiki.exam.exam-model.EXAM_COPY.right.8"); },
  get wrong() { return tr("wiki.exam.exam-model.EXAM_COPY.wrong.9"); },
  get done() { return tr("wiki.exam.exam-model.EXAM_COPY.done.10"); },
  /** A question reported earlier today (which choice is not known here). */
  get doneToday() { return tr("wiki.exam.exam-model.EXAM_COPY.doneToday.11"); },
  get failed() { return tr("wiki.exam.exam-model.EXAM_COPY.failed.12"); },
  // the material card's 真题 line
  get extracting() { return tr("wiki.exam.exam-model.EXAM_COPY.extracting.13"); },
  extracted: (items: number) => tr("wiki.exam.exam-model.EXAM_COPY.extracted.14", { v0: items }),
  unattached: (n: number) => tr("wiki.exam.exam-model.EXAM_COPY.unattached.15", { v0: n }),
  unreadable: (n: number) => tr("wiki.exam.exam-model.EXAM_COPY.unreadable.16", { v0: n }),
  /** A failed extraction without the server's line. */
  get extractFailed() { return tr("wiki.exam.exam-model.EXAM_COPY.extractFailed.17"); },
  get lineTipLabel() { return tr("wiki.exam.exam-model.EXAM_COPY.lineTipLabel.18"); },
} as const;

/** One question's state in the block (kept per plugin session, so a redraw keeps it). */
export interface ExamItemUi {
  /** 看答案 was pressed. */ shown: boolean;
  /** The choice reported this session (null: none yet). */ pick: ExamResult | null;
  /** The report on its way (its buttons are off). */ sending: ExamResult | null;
  /** The last report failed (没记上，请重试). */ failed: boolean;
}
export const freshItemUi = (): ExamItemUi => ({ shown: false, pick: null, sending: null, failed: false });

export interface ExamButton { result: ExamResult | null; label: string; pressed: boolean; disabled: boolean }
export interface ExamItemView {
  id: string;
  /** 第 7 题 ("" when the paper gives no number). */ heading: string;
  /** 《X》p.12 ("" without a source title). */ cite: string;
  /** The 大题 passage, shown folded. */ context: string | null;
  stem: string;
  /** A keyless choice question's options, 「A. …」. */ options: string[];
  /** 看答案 instead of the answer. */ reveal: boolean;
  answer: string | null; explanation: string | null;
  buttons: ExamButton[];
  error: string | null;
}
export interface ExamView { tip: string | null; assetId: string | null; items: ExamItemView[] }

/** 《X》p.12: the printed page when known, else the PDF page; no page → just 《X》. */
export function citeOf(it: ExamOpenItemOut): string {
  if (it.source_title === "") return "";
  const page = it.printed_page ?? (it.page === null ? null : String(it.page));
  return page === null ? `《${it.source_title}》` : `《${it.source_title}》p.${page}`;
}

const LETTERS = "ABCDEFGH";

function itemView(it: ExamOpenItemOut, ui: ExamItemUi): ExamItemView {
  const busy = ui.sending !== null;
  const reported = ui.pick !== null || it.done;
  const button = (result: ExamResult, label: string): ExamButton =>
    ({ result, label, pressed: ui.pick === result, disabled: busy || reported });
  let buttons: ExamButton[];
  let reveal = false;
  let answer: string | null = null;
  let explanation: string | null = null;
  if (it.has_key && !(ui.shown || reported)) {
    reveal = true;
    buttons = [];
  } else {
    if (it.has_key) { answer = it.answer; explanation = it.explanation; }
    if (it.done && ui.pick === null) buttons = [{ result: null, label: EXAM_COPY.doneToday, pressed: true, disabled: true }];
    else buttons = it.has_key ? [button("right", EXAM_COPY.right), button("wrong", EXAM_COPY.wrong)] : [button("done", EXAM_COPY.done)];
  }
  const options = it.kind === "single" || it.kind === "multi" ? it.options.map((o, i) => `${LETTERS[i] ?? String(i + 1)}. ${o}`) : [];
  return {
    id: it.item_id, heading: it.number === null || it.number.trim() === "" ? "" : EXAM_COPY.number(it.number.trim()),
    cite: citeOf(it), context: it.context !== null && it.context.trim() !== "" ? it.context : null, stem: it.stem, options,
    reveal, answer, explanation: explanation !== null && explanation.trim() !== "" ? explanation : null, buttons,
    error: ui.failed && !busy ? EXAM_COPY.failed : null,
  };
}

/** The block for one page; null when there is nothing to show (no answer yet, no quiz and no question, the material deleted). */
export function examView(out: PageExamOut | null, ui: (itemId: string) => ExamItemUi): ExamView | null {
  if (out === null || (out.asset_id === null && out.open_items.length === 0)) return null;
  const titles = [...new Set(out.sources.filter((t) => t.trim() !== ""))];
  return {
    tip: titles.length > 0 ? EXAM_COPY.sourcesTip(titles) : null,
    assetId: out.asset_id,
    items: out.open_items.map((it) => itemView(it, ui(it.item_id))),
  };
}

/**
 * How a self-report ended: k / score / done → recorded; duplicate and a 409 (a racing report for the same question got
 * there first) → already (the server kept an earlier report, which this one may not match); anything else failed.
 */
export function reportOutcome(r: { ok: true; status: ExamSelfOut["status"] } | { ok: false; error: unknown }): "recorded" | "already" | "failed" {
  if (r.ok) return r.status === "duplicate" ? "already" : "recorded";
  return r.error instanceof ApiError && r.error.status === 409 ? "already" : "failed";
}

/** A path row's 常考 tag: its ? text, or null when the row is not 常考 (the server sends 0 below the threshold). */
export function examTagTip(exam: number | undefined): string | null {
  return exam !== undefined && exam > 0 ? GRAPH_COPY.examTip(exam) : null;
}

/** A material card's 真题 line: 正在找题 / 抽出 N 道题 · M 道没挂上 · K 道读不清 / the failure; null without an exam use. */
export interface ExamLine { text: string; failed: boolean; tip: string[] }
export function examLine(e: ExamSummaryOut | null | undefined): ExamLine | null {
  if (e === null || e === undefined) return null;
  if (e.state === "extracting") return { text: EXAM_COPY.extracting, failed: false, tip: [] };
  if (e.state === "failed") return { text: e.detail !== null && e.detail.trim() !== "" ? e.detail : EXAM_COPY.extractFailed, failed: true, tip: [] };
  const parts: string[] = [EXAM_COPY.extracted(e.items)];
  if (e.unattached > 0) parts.push(EXAM_COPY.unattached(e.unattached));
  if (e.unreadable > 0) parts.push(EXAM_COPY.unreadable(e.unreadable));
  const tip = [...e.unattached_items, ...(e.detail !== null && e.detail.trim() !== "" ? [e.detail] : [])];
  return { text: parts.join(" · "), failed: false, tip };
}
