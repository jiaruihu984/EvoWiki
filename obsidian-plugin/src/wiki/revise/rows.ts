import { tr } from "../../i18n/index";
/**
 * The `page_revise` ledger rows of a page (materials spec 2026-10-20 §7 D) and the words the bar, the top line, the chat
 * card and the 活动记录 row say about them. Pure; no note or DOM here (end-block/model.ts reads it for the top line).
 */
import type { ActionOut, ReviseOut } from "../bot/bot-schema";
import type { WikiState } from "../state";

export const REVISE_KIND = "page_revise";
/**
 * The assistant's practice draft (`add_practice`: a block composed and render-checked when it was proposed). While it waits
 * the backend gives it a `revise` too (bot/revise_schemas.py practice_out: its section plus the block, inline), so the page
 * shows it in the same review mode as a materials draft, the block drawn live.
 */
export const PRACTICE_KIND = "page_practice";
/** A row whose waiting draft is shown inside the page. */
export const inPageDraft = (kind: string): boolean => kind === REVISE_KIND || kind === PRACTICE_KIND;

/** The rows of the ledger as the sidebar last read them (none before the first read). */
function rows(s: WikiState): readonly ActionOut[] {
  return s.bot.actions.kind === "ready" ? s.bot.actions.value : [];
}
const pageOf = (a: ActionOut): string | null => a.revise?.page_id || a.page_id;

/** A ready draft of `pageId` waiting for 确认替换 / 取消 (the newest one); null = the page is not in review mode. */
export function pendingRevise(s: WikiState, pageId: string): (ActionOut & { revise: ReviseOut }) | null {
  for (const a of rows(s)) {
    if (!inPageDraft(a.kind) || a.status !== "proposed" || pageOf(a) !== pageId) continue;
    if (a.revise?.state === "ready") return a as ActionOut & { revise: ReviseOut };
  }
  return null;
}

/** The last decided draft of `pageId` when it was confirmed and can still be undone (the top line's 本页已按…更新); else null. */
export function appliedRevise(s: WikiState, pageId: string): (ActionOut & { revise: ReviseOut }) | null {
  let last: ActionOut | null = null;
  for (const a of rows(s)) {
    if (a.kind !== REVISE_KIND || pageOf(a) !== pageId || a.decided_at === null) continue;
    if (last === null || Date.parse(a.decided_at) > Date.parse(last.decided_at ?? "")) last = a;
  }
  return last !== null && last.status === "executed" && last.can_undo && last.revise ? last as ActionOut & { revise: ReviseOut } : null;
}

/** A newer page_revise row of the same page than `a` (the chat card of `a` then says 有新的草稿). */
export function hasNewerRevise(list: readonly ActionOut[], a: { action_id: string; page_id: string | null; created_at?: string }): boolean {
  const pid = a.page_id;
  if (pid === null) return false;
  const me = list.find((x) => x.action_id === a.action_id);
  const at = Date.parse(me?.created_at ?? a.created_at ?? "");
  return list.some((x) => x.kind === REVISE_KIND && x.action_id !== a.action_id && pageOf(x) === pid && Date.parse(x.created_at) > at);
}

// ---- words ----

/** 《A》 or 《A》《B》 (the materials the draft used; an unnamed one is 资料). */
export function sourceNames(r: Pick<ReviseOut, "sources">): string {
  const t = r.sources.map((x) => x.title.replace(/\.[a-z0-9]{1,5}$/i, "").trim()).filter((x) => x !== "");
  return t.length === 0 ? tr("wiki.revise.rows..1") : t.map((x) => `《${x}》`).join("");
}
/** A practice draft's words: 加了一个交互 / 加了练习题 / 加了记忆卡 (full-demo app.js pracBar). */
const PRACTICE_ADDED = { get interactive() { return tr("wiki.revise.rows.PRACTICE_ADDED.interactive.2"); }, get question() { return tr("wiki.revise.rows.PRACTICE_ADDED.question.3"); }, get card() { return tr("wiki.revise.rows.PRACTICE_ADDED.card.4"); } } as const;
/** The sticky bar's title: 按《X》改了 N 节 (a practice draft: 加了一个交互). */
export const barTitle = (r: ReviseOut): string => r.practice ? PRACTICE_ADDED[r.practice]
  : r.sources.length === 0 ? tr("wiki.revise.scopedTitle", { count: r.sections.length })
    : tr("wiki.revise.rows.barTitle.5", { v0: sourceNames(r), v1: r.sections.length });
/** The top line after 确认替换: 本页已按《X》更新 N 节. */
export const appliedLine = (r: ReviseOut): string => r.sources.length === 0 ? tr("wiki.revise.scopedApplied", { count: r.sections.length })
  : tr("wiki.revise.rows.appliedLine.6", { v0: sourceNames(r), v1: r.sections.length });

/**
 * Who asked for the draft, when the row carries no reason of its own: the user in chat (你让我按…), Bot after a material
 * matched the page (app.js: 《X》讲到了这一页已有的内容), anyone else neutral.
 */
function defaultWhy(source: ActionOut["source"] | undefined, r: ReviseOut): string {
  const names = sourceNames(r);
  if (source === "assistant") return tr("wiki.revise.rows..7", { v0: names });
  if (source === "bot") return tr("wiki.revise.rows..8", { v0: names });
  return tr("wiki.revise.rows..9", { v0: names });
}

/** A draft's reason and details (copy guide §2): the reason (资料重写 / the row's why / who asked), the materials, the citation check. */
function tipLines(a: Pick<ActionOut, "why"> & Partial<Pick<ActionOut, "source">>, r: ReviseOut): string[] {
  const why = r.rebuild ? tr("wiki.revise.rows.why.10")
    : a.why.trim() !== "" ? a.why.trim() : defaultWhy(a.source, r);
  const gone = r.sources.filter((x) => x.deleted).length;
  const cites = r.cite_issues > 0 ? [tr("wiki.revise.rows.cites.11", { v0: r.cite_issues })] : [];
  return [why, ...(r.sources.length > 0 ? [tr("wiki.revise.rows..12", { v0: sourceNames(r), v1: gone > 0 ? tr("wiki.revise.rows..13", { v0: gone }) : "" })] : []), ...cites];
}

/** The circled ? of a waiting draft: the reason first, then the materials, then 确认前这一页不变. */
export const reviseTip = (a: Pick<ActionOut, "why"> & Partial<Pick<ActionOut, "source">>, r: ReviseOut): string[] =>
  r.practice ? [...(a.why.trim() !== "" ? [a.why.trim()] : []), tr("wiki.revise.rows.reviseTip.14")] : [...tipLines(a, r), tr("wiki.revise.rows.reviseTip.15")];

/**
 * The circled ? of a decided draft in 个性化 · 活动记录 (app.js changeRow; the row's text is the backend's 「X」按《A》更新了 N 节):
 * `reason` is the backend's reason for that row (bot/diffs.py revise_label: the same words as tipLines', so the list and the
 * page agree), then the materials and the citation check. No 确认前这一页不变: it is decided.
 */
export function appliedTip(a: Pick<ActionOut, "why"> & Partial<Pick<ActionOut, "source">>, r: ReviseOut, reason: string | null): string[] {
  const lines = tipLines(a, r);
  return reason !== null && reason.trim() !== "" ? [reason.trim(), ...lines.slice(1)] : lines;
}
