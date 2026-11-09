import { tr } from "../i18n/index";
/** Pure helpers of the assistant's 历史对话 list (full-demo/src/asst.js): titles, local-time labels, 今天/昨天/更早 groups. */
import type { SessionSummaryOut } from "./types";
import { dayDiff, formatWhen, parseWhen } from "../ui/dates";

export const TITLE_MAX = 24;
export type HistoryGroup = "today" | "yesterday" | "earlier";
export const HISTORY_GROUPS: readonly HistoryGroup[] = ["today", "yesterday", "earlier"];
export const HISTORY_LABEL = { get today() { return tr("assistant.history.HISTORY_LABEL.today.1"); }, get yesterday() { return tr("assistant.history.HISTORY_LABEL.yesterday.2"); }, get earlier() { return tr("assistant.history.HISTORY_LABEL.earlier.3"); } };

/**
 * The title of a new conversation = the first line of its first question, trimmed, with one trailing ？ / ? / 。 removed,
 * at most 24 characters (as the server does). 学点新的's goal message carries a 背景 line after the goal: the title is the goal.
 */
export function titleOf(question: string): string {
  const first = (question.trim().split("\n", 1)[0] ?? "").trim();
  return [...first.replace(/[？?。]$/, "")].slice(0, TITLE_MAX).join("");
}


export function groupOf(iso: string, now: Date): HistoryGroup {
  const d = parseWhen(iso);
  if (!d) return "earlier";
  const n = dayDiff(d, now);
  return n === 0 ? "today" : n === 1 ? "yesterday" : "earlier";
}


/** The row's second line: `page · time`, or just the time without a page. */
export function rowSub(s: SessionSummaryOut, now: Date): string {
  const when = formatWhen(s.last_activity_at, now, "list");
  return s.page_title ? `${s.page_title} · ${when}` : when;
}
