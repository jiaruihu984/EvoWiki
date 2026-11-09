/**
 * 个性化's lists, worked out from the reads (pure logic; render-personal.ts draws them):
 * - 我的学习偏好: the teaching-spec rules that are the user's (authority user: 我的要求) or that EvoWiki tuned from the
 *   user's records (authority rsi, origin rsi_b / rsi_h: AI 优化). The base rules (origin default / seed) and the pending
 *   ones are not listed. The "?" is the rule's evidence line (when the spec has one), then where it came from.
 * - 待确认的偏好: authority pending (Memory's backfill, rules/authority.py SYSTEM_ADDS).
 * - 没采用和撤回的 (管理偏好): the user's decisions in force on rules no longer in the spec (rejected → 你没采用, revoked →
 *   你删除了), then the track-B rule changes EvoWiki rolled back by itself because they made things worse (AI 撤回).
 * - 活动记录: every shown change (bot/change-rows.ts, waits left out: they wait on their page) and the Bot's activity, newest
 *   first, by local day (今天 / 昨天 / 更早); a code change waiting for review goes first.
 */
import { BOT_KIND } from "./bot/bot-copy";
import type { ActionOut, ActivityOut } from "./bot/bot-schema";
import { changeRows } from "./bot/change-rows";
import { originLine, PERSONAL_COPY as C } from "./personal-copy";
import type { RsiCard } from "./rsi-changes";
import type { RsiChangeOut, SpecItem, TeachSpecOut } from "./schema";
import type { WikiState } from "./state";
import { dayDiff, parseWhen } from "../ui/dates";

export type Own = "user" | "ai";
export interface ShownRule { item: SpecItem; own: Own; why: string[] }

const AI_ORIGINS: ReadonlySet<string> = new Set(["rsi_b", "rsi_h"]);

/** The "?" of a rule: its evidence line (why it was adopted), then where it came from; [] when neither is known. */
export function ruleWhy(spec: TeachSpecOut, item: SpecItem): string[] {
  const ev = spec.evidence[item.item_id]?.trim() ?? "";
  const from = originLine(item.origin);
  return [ev, from ?? ""].filter((x) => x !== "");
}

/** 我的学习偏好, in spec order. */
export function shownRules(spec: TeachSpecOut): ShownRule[] {
  return spec.items.flatMap((item): ShownRule[] => {
    if (item.authority === "user") return [{ item, own: "user", why: ruleWhy(spec, item) }];
    if (item.authority === "rsi" && AI_ORIGINS.has(item.origin)) return [{ item, own: "ai", why: ruleWhy(spec, item) }];
    return [];
  });
}

/** 待确认的偏好, in spec order. */
export function pendingRules(spec: TeachSpecOut): { item: SpecItem; why: string[] }[] {
  return spec.items.filter((i) => i.authority === "pending").map((item) => ({ item, why: ruleWhy(spec, item) }));
}

export interface HistoryRow { key: string; text: string; how: string; why: string[] }

const sameText = (a: string, b: string): boolean => a.replace(/\s+/g, "") === b.replace(/\s+/g, "");

/** A track-B rule change EvoWiki took back by itself (it made things worse). */
const aiRollback = (c: RsiChangeOut): boolean =>
  c.track === "B" && c.layer === 0 && c.status === "rolled_back" && c.outcome === "harmful";

/** 没采用和撤回的: the decisions first (newest first), then the automatic rollbacks; a text in the spec again is left out. */
export function historyRows(spec: TeachSpecOut, rsi: readonly RsiChangeOut[] | null): HistoryRow[] {
  const inSpec = (t: string): boolean => spec.items.some((i) => sameText(i.text, t));
  const out: HistoryRow[] = [];
  for (const d of spec.decisions) {
    if (inSpec(d.text) || out.some((r) => sameText(r.text, d.text))) continue;
    out.push({ key: `d:${d.decision_id}`, text: d.text, how: d.action === "rejected" ? C.howRejected : C.howRevoked, why: [] });
  }
  for (const c of rsi ?? []) {
    if (!aiRollback(c)) continue;
    const text = (c.rule_after || c.summary).trim();
    if (text === "" || inSpec(text) || out.some((r) => sameText(r.text, text))) continue;
    out.push({ key: `rsi:${c.patch_id}`, text, how: C.howAuto, why: [c.why.trim(), c.effect_line.trim()].filter((x) => x !== "") });
  }
  return out;
}

// ---------------- 活动记录 ----------------

export type Day = "today" | "yesterday" | "older";
export type ActivityItem =
  | { type: "change"; key: string; card: RsiCard; action: ActionOut | null; at: string }
  | { type: "wait"; key: string; action: ActionOut; at: string }
  | { type: "run"; key: string; run: ActivityOut; at: string };

const RSI_KINDS = new Set(["rsi_method", "rsi_patch"]);

export function dayOf(at: string, now: Date): Day {
  const d = parseWhen(at);
  if (d === null) return "older";
  const n = dayDiff(d, now);
  return n === 0 ? "today" : n === 1 ? "yesterday" : "older";
}

/** The Bot's rows 活动记录 lists (the kinds it has words for; a skipped run never). */
export function shownRuns(s: WikiState): ActivityOut[] {
  return s.bot.recent.kind === "ready" ? s.bot.recent.value.filter((r) => BOT_KIND[r.kind] !== undefined && r.result !== "skipped") : [];
}

export const runKey = (r: ActivityOut): string => `run:${r.trigger_id ?? r.run_id ?? r.write?.page_id ?? r.at}`;

/**
 * `review`: code changes waiting for the learner's review (采用 / 不要), newest first. `items`: everything else, newest first.
 * A page written by the Bot is its activity row (the ledger's change row of the same write is not listed twice).
 */
export function activityItems(s: WikiState): { review: ActivityItem[]; items: ActivityItem[] } {
  const runs = shownRuns(s);
  const writes = new Set(runs.flatMap((r) => (r.write?.action_id != null ? [r.write.action_id] : [])));
  const review: ActivityItem[] = [];
  const items: ActivityItem[] = [];
  for (const row of changeRows(s)) {
    if (row.type === "wait") {   // an RSI change waiting for his yes has no page and no card elsewhere
      if (RSI_KINDS.has(row.action.kind)) review.push({ type: "wait", key: row.key, action: row.action, at: row.action.created_at });
      continue;
    }
    if (writes.has(row.card.change.patch_id)) continue;
    const it: ActivityItem = { type: "change", key: row.key, card: row.card, action: row.action, at: row.card.change.at };
    (row.card.state === "review" ? review : items).push(it);
  }
  for (const run of runs) items.push({ type: "run", key: runKey(run), run, at: run.at });
  const t = (x: ActivityItem): number => parseWhen(x.at)?.getTime() ?? 0;
  items.sort((a, b) => t(b) - t(a));
  return { review, items };
}

/** The local day of the row a key names (null: not listed), so 查看 can open 更早 first when it is there. */
export function dayOfKey(s: WikiState, key: string, now: Date): Day | null {
  const { review, items } = activityItems(s);
  if (review.some((x) => x.key === key)) return "today";
  const it = items.find((x) => x.key === key);
  return it === undefined ? null : dayOf(it.at, now);
}
