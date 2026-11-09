import { tr } from "../i18n/index";
import { localizedAttribute } from "../i18n/index";
/**
 * A ledger action waiting for a yes / no (merge pages, delete, add a paragraph…) shows where it applies (app.js
 * `pageWaits`): on its page, as a slim bar like a materials draft's (`div.ew-revbar`): its summary, the "?" (why) and
 * 确认… / 取消. One with no page shows the same bar at the top of the 知识图谱 page. A materials draft and a practice draft
 * are not listed: they wait on their page in review mode (revise/overlay.ts); a profile fact waits in 个性化. The chat's
 * card decides the same row.
 */
import { btn, el } from "../ui/ew";
import { type WikiActions, writeBlocked } from "./actions";
import type { ActionOut } from "./bot/bot-schema";
import { waiting } from "./bot/change-rows";
import { approveText } from "./bot/render-changes";
import { ENGINE_COPY } from "./engine-copy";
import { PERSONAL_COPY as C } from "./personal-copy";
import { qTip } from "./q-tip";
import { PRACTICE_KIND, REVISE_KIND } from "./revise/rows";
import type { WikiState } from "./state";

/** A materials draft, or a practice draft shown in the page: decided in the page's review mode, not by a wait bar. */
const inPage = (a: ActionOut): boolean => a.kind === REVISE_KIND || (a.kind === PRACTICE_KIND && a.revise?.state === "ready");
/** A fact he said about himself, waiting: decided in 个性化 (render-personal.ts), next to 待确认的记忆. */
const FACT_KIND = "profile_fact";
const EDIT_KIND = "assistant_edit";

/** The profile facts waiting for a yes / no, newest first. */
export function pendingFacts(s: WikiState, now: number = Date.now()): ActionOut[] {
  if (s.bot.actions.kind !== "ready") return [];
  return s.bot.actions.value.filter((a) => a.kind === FACT_KIND && waiting(a, now))
    .sort((x, y) => Date.parse(y.created_at) - Date.parse(x.created_at));
}
/** A fact's words: its section and text, without the action's 「记进档案：」. */
export const factText = (a: ActionOut): string => a.summary.trim();

/** The waits of one page (`pageId` null: the ones with no page, shown on 知识图谱), newest first. */
export function pageWaitActions(s: WikiState, pageId: string | null, now: number = Date.now()): ActionOut[] {
  if (s.bot.actions.kind !== "ready") return [];
  const page = (a: ActionOut): string | null => (a.page_id == null || a.page_id === "" ? null : a.page_id);
  return s.bot.actions.value
    .filter((a) => waiting(a, now) && !inPage(a) && a.kind !== FACT_KIND && page(a) === pageId)
    .sort((x, y) => Date.parse(y.created_at) - Date.parse(x.created_at));
}

/** What a wait says it will do. A page edit's bar sits on its page: the section, as still to happen. */
function waitText(act: ActionOut): string {
  const c = act.content as { kind?: unknown; heading?: unknown };
  if (act.kind !== EDIT_KIND || typeof c.heading !== "string" || c.heading === "") return act.summary.trim();
  return c.kind === "add_section" ? tr("wiki.page-waits..1", { v0: c.heading }) : tr("wiki.page-waits..2", { v0: c.heading });
}

/** One wait's bar. `off`: its buttons are disabled (a write is out, offline, not set up, the demo). */
export function waitBar(act: ActionOut, off: boolean, decide: (id: string, op: "approve" | "reject") => void): HTMLDivElement {
  const b = el("div", "revbar");
  b.setAttribute("role", "region");
  localizedAttribute(b, "aria-label", () => (C.waitAria));
  b.dataset.wait = act.action_id;
  b.append(el("span", "rb-t", waitText(act)));
  const why = act.why.trim();
  if (why !== "") b.append(qTip(why, () => (C.waitWhy)));
  const acts = el("span", "rb-acts");
  const ok = btn(approveText(act), "primary sm", () => decide(act.action_id, "approve"));
  ok.dataset.action = "bot-approve";
  ok.disabled = off;
  acts.append(ok);
  // B3 refuses a reject on an approved row (a 429 left it there): only the yes button (retry) is offered.
  if (act.status !== "approved") {
    const no = btn(() => (ENGINE_COPY.logReject), "sm", () => decide(act.action_id, "reject"));
    no.dataset.action = "bot-reject";
    no.disabled = off;
    acts.append(no);
  }
  b.append(acts);
  return b;
}

/** The bars of one page (or of 知识图谱 when `pageId` is null). */
export function waitBars(s: WikiState, pageId: string | null, a: Pick<WikiActions, "botDecide">): HTMLDivElement[] {
  const off = writeBlocked(s) || s.demo;
  return pageWaitActions(s, pageId).map((act) => waitBar(act, off, (id, op) => a.botDecide(id, op)));
}

/** What the bars of `pageId` show (a view redraws them only when it changed). */
export function waitSig(s: WikiState, pageId: string | null): string {
  const acts = pageWaitActions(s, pageId);
  return JSON.stringify([acts.map((x) => [x.action_id, x.status, x.summary, x.why]), acts.length > 0 && (writeBlocked(s) || s.demo)]);
}
