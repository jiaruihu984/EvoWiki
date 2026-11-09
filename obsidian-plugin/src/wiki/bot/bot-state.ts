/** The Bot's slot of the wiki sidebar state (WikiState.bot). Pure data; BotController owns every transition. */
import type { ActionOut, ActivityOut, BotSettings, SuggestionOut, WhyOut } from "./bot-schema";
import type { WriteOut } from "../schema";
import type { Load } from "../state";

export interface BotSlot {
  /** placement=today, bot_failing filtered out. */
  today: Load<SuggestionOut[]>;
  /** By page id, kind chat only. */
  chips: Record<string, SuggestionOut[]>;
  /** Every status, newest first. */
  actions: Load<ActionOut[]>;
  recent: Load<ActivityOut[]>;
  /** By page id: where the writes of the 最近做了什么 rows are (GET /wiki/writes). A running write is painted in place; this object is replaced only when a write starts, ends or changes state. */
  writes: Record<string, WriteOut>;
  /** By trigger id (or "s:" + suggestion id); the open rows are `whyOpen`. */
  why: Record<string, Load<WhyOut>>;
  whyOpen: string[];
  settings: Load<BotSettings>;
  /** Cards hidden locally after 开始 / 不感兴趣 until the next list read drops them. */
  hidden: string[];
  /** The local review card is cooling down after 减少提醒 (the server's answer, GET /wiki/bot/review-reminder). */
  reviewHidden: boolean;
  /** The review_due suggestion linked to the local review card when 开始 was pressed (it is no longer listed once accepted); `done` goes to it. */
  reviewLinked: string | null;
  /** The review round just finished: the completion card (app.js TODAY.celebrate). null = none. */
  celebrate: { n: number; ok: number; at: number } | null;
  /** app.js BOT.allDoneSeen: the 学习任务全部完成 check has drawn once. */
  allDoneSeen: boolean;
  /** 更新这 N 页 batches by suggestion id: the card stays (the list drops an accepted one) until every draft is decided. */
  revise: Record<string, ReviseBatch>;
}
export interface ReviseBatch { suggestion: SuggestionOut; actionIds: string[]; total: number; loaded: boolean }
/**
 * Where a batch stands (app.js `batchLines`): `done` = drafts no longer being written (ready or failed), `waits` = ready drafts
 * still waiting for 确认替换 (a ledger row still `proposed`). The card stays while `open` (writing, or something waits).
 * A row not listed yet (before the first ledger read) counts as being written.
 */
export function reviseProgress(b: ReviseBatch, acts: readonly ActionOut[]): { done: number; waits: number; open: boolean; firstWait: string | undefined } {
  let done = 0;
  const waiting: string[] = [];
  for (const id of b.actionIds) {
    const a = acts.find((x) => x.action_id === id);
    if (a === undefined) continue;
    const st = a.revise?.state;
    if (st === "writing") continue;
    done++;
    if (st === "ready" && a.status === "proposed") waiting.push(id);
  }
  return { done, waits: waiting.length, open: done < b.actionIds.length || waiting.length > 0, firstWait: waiting[0] };
}

/** The Bot's page drafts no batch of this run knows (the plugin was reloaded since): still being written, or waiting for 确认替换. */
export function orphanRevises(acts: readonly ActionOut[], batches: Readonly<Record<string, ReviseBatch>>): ActionOut[] {
  const known = new Set(Object.values(batches).flatMap((b) => b.actionIds));
  return acts.filter((a) => a.kind === "page_revise" && a.source === "bot" && a.status === "proposed" && !known.has(a.action_id)
    && (a.revise?.state === "writing" || a.revise?.state === "ready"));
}

export const INITIAL_BOT: BotSlot = {
  today: { kind: "idle" }, chips: {}, actions: { kind: "idle" }, recent: { kind: "idle" }, writes: {},
  why: {}, whyOpen: [], settings: { kind: "idle" }, hidden: [], reviewHidden: false, reviewLinked: null, celebrate: null, allDoneSeen: false, revise: {},
};
