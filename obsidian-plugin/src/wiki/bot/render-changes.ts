/**
 * Pieces a ledger / RSI change row shares (个性化 · 活动记录, render-personal.ts; the page's wait bar, page-waits.ts): where a
 * change is, the small 撤销, the "?" of a decided materials draft, and the yes button's words.
 */
import { btn } from "../../ui/ew";
import type { WikiActions } from "../actions";
import { ENGINE_COPY } from "../engine-copy";
import { WIKI_COPY } from "../copy";
import type { ActionOut } from "./bot-schema";
import { appliedTip, REVISE_KIND } from "../revise/rows";

/** Where a ledger action's change is: its page, and the section when known (null: no page). */
export function changeTarget(act: ActionOut | null, a: WikiActions): (() => void) | null {
  const pid = act?.page_id;
  if (act == null || pid == null || pid === "") return null;
  return () => a.goToChange(pid, act.section_id ?? null);
}

/** The small 撤销 (`btn sm`) of a live row. */
export function undoButton(off: boolean, run: () => unknown): HTMLButtonElement {
  const u = btn(() => (WIKI_COPY.rsiUndo), "sm", () => run());
  u.dataset.action = "rsi-undo";
  u.disabled = off;
  return u;
}

/**
 * The "?" of a decided materials draft (its text is the backend's 「X」按《A》更新了 N 节): the backend's reason, then
 * the materials and the citation check (app.js changeRow, copy guide §2). Null for any other row, or when the draft is not
 * loaded (the backend's reason alone then).
 */
export function reviseChangeTip(act: ActionOut | null, why: string | null): string[] | null {
  const rv = act?.revise;
  if (act == null || act.kind !== REVISE_KIND || rv == null || rv.state !== "ready") return null;
  return appliedTip(act, rv, why);
}

/** The yes button's words: the action's own verb when known (a merge says 确认合并), else 确认. */
export function approveText(a: Pick<ActionOut, "kind">): string {
  if (/merge/.test(a.kind)) return ENGINE_COPY.logApproveMerge;
  if (/delete/.test(a.kind)) return ENGINE_COPY.logApproveDelete;
  return ENGINE_COPY.logApprove;
}
