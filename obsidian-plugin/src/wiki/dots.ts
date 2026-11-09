/**
 * The red counts on the sidebar tabs (app.js `pending` / `updateDots` / `SEEN`): a tab that is not shown gets a count of
 * the pending items the user has not seen; showing 今天 marks its items seen (and shows 0). The seen keys persist (plugin
 * data, the newest MAX_SEEN_DOTS) so the same items never raise the count again, also after a restart.
 *
 * Keys (app.js names, plus what makes them one item across days, since they persist):
 * - 今天: `card:<suggestion id>` for each drawn Bot task card, `review:<n>:<day>` for the local review card (n = due cards
 *   + due confirmation questions, day = local date, so tomorrow's review of the same size is new), and ONE
 *   `confirm:<sorted followup ids joined by ,>` for the whole set of due confirmation questions.
 * - 个性化 (tab id "engine"): `pend:<id>` for each preference, memory group and profile fact waiting for him and `code:<patch id>` for each
 *   code change waiting for review (all counted until decided, never seen), and `log:<patch id>@<at ms>` for each shown
 *   change newer than the last time 个性化 was opened (changes apply by themselves: the count means "new changes to look
 *   at"). A ledger action waiting for a yes / no is not counted here: it waits on its page. `tool:<patch id>` for each forged tool
 *   that went live by itself (RsiChangeOut.tool_live): counted here and on 活动记录 until 活动记录 has been open with it shown.
 *
 * "The last time 个性化 was opened" is the newest change time the tab showed then (the backend's clock, so a skewed local
 * clock cannot hide a change); it persists with the seen keys as one `engine@<ms>` entry. It alone decides whether a
 * change is new, so `log:` keys are never stored as seen (the key list holds only 今天's keys, within MAX_SEEN_DOTS).
 * Opening 个性化 clears its changes from the count; a change seen once never counts again while its time stays the same; the
 * same change with a newer time (the backend bumps `at` when it rolls a change back automatically) does.
 */
import { waiting } from "./bot/change-rows";
import { taskCards } from "./bot/render-tasks";
import { pendingFacts } from "./page-waits";
import { reviewEntries } from "./review-entries";
import { changeTime, isCodePatch, newChanges, newestTime } from "./rsi-changes";
import type { Tab, WikiState } from "./state";
import { localDayKey } from "../ui/dates";

export const MAX_SEEN_DOTS = 200;
/** The tabs that can carry a dot (助手 never does). */
export type DotTab = "today" | "engine";
export interface Pending { today: string[]; engine: string[] }
/** The red counts on 今天 / 个性化 (0 = none). */
export interface Counts { today: number; engine: number; log: number }

/** Where the seen keys live (main.ts: plugin data). `save` gets the whole bounded list. */
export interface SeenStore { load(): readonly string[]; save(keys: string[]): void }

const OPENED = "engine@";
const LOG = "engine|log:";
const PROP = "engine|prop:";
/** `log:` keys are new by their own time (already filtered by the last-opened time); `pend:` / `code:` keys stay until decided. */
const isTransient = (k: string): boolean => k.startsWith("log:") || k.startsWith("pend:") || k.startsWith("code:");

const day = localDayKey;

/**
 * app.js `pending()` on the real state. `engineSeen`: the newest change time 个性化 showed when it was last opened (null =
 * never: every shown change is new).
 */
export function pendingKeys(s: WikiState, now: Date, engineSeen: number | null = null): Pending {
  const today: string[] = [];
  const cards = taskCards(s);
  for (const v of cards) today.push(v.suggestion === null ? `review:${reviewEntries(s).length}:${day(now)}` : `card:${v.key}`);
  // what he did himself (a hand edit, a card he decided or a thing he asked for in the chat) is in the list, but nothing new to look at
  const own = new Set(s.bot.actions.kind === "ready" ? s.bot.actions.value.filter((a) => a.kind === "hand_edit" || a.source === "user" || a.source === "assistant").map((a) => a.action_id) : []);
  const fresh = s.rsi.kind === "ready" ? newChanges(s.rsi.value, engineSeen).filter((c) => !own.has(c.patch_id)) : [];
  // waiting for the learner (until decided): preferences to confirm, code changes to review
  const pend = s.prefs.kind === "ready" ? s.prefs.value.items.filter((i) => i.authority === "pending").map((i) => `pend:${i.item_id}`) : [];
  const groups = s.conflicts.kind === "ready" ? s.conflicts.value.map((c) => `pend:${c.id}`) : [];
  const facts = pendingFacts(s, now.getTime()).map((a) => `pend:${a.action_id}`);
  const review = s.rsi.kind === "ready" ? s.rsi.value.filter((c) => isCodePatch(c) && c.status === "awaiting_user").map((c) => `code:${c.patch_id}`) : [];
  // an RSI change waiting for his yes in 活动记录 (it has no page to wait on)
  if (s.bot.actions.kind === "ready") review.push(...s.bot.actions.value.filter((x) => x.kind === "rsi_method" && waiting(x)).map((x) => `ask:${x.action_id}`));
  // Confirmation questions are extra 今天 items (app.js `confirm`), counted only while the local review card is drawn.
  // They are one item for the whole set (the demo pushes a single "confirm"): the sorted ids make the key.
  if (cards.some((v) => v.suggestion === null) && s.review.today.kind === "ready" && s.review.today.value.confirms.length > 0)
    today.push(`confirm:${s.review.today.value.confirms.map((c) => c.followup_id).sort().join(",")}`);
  const tools = s.rsi.kind === "ready" ? s.rsi.value.filter((c) => c.tool_live).map((c) => `tool:${c.patch_id}`) : [];
  const logged = fresh.filter((c) => !c.tool_live && (!isCodePatch(c) || c.status !== "awaiting_user"));
  return { today, engine: [...pend, ...groups, ...facts, ...review, ...tools, ...logged.map((c) => `log:${c.patch_id}@${changeTime(c) ?? ""}`)] };
}

/** Keeps the seen keys (as `<tab>|<key>`) and the time 个性化 was last opened, and works out the counts. One per sidebar view. */
export class DotTracker {
  private seen: string[];
  private engineSeen: number | null = null;
  constructor(private readonly store: SeenStore | null) {
    const all = store === null ? [] : store.load().filter((k) => typeof k === "string");
    for (const k of all) {
      if (!k.startsWith(OPENED)) continue;
      const ms = Number(k.slice(OPENED.length));
      if (Number.isFinite(ms)) this.engineSeen = ms;
    }
    // `engine|log:` keys of older versions are dropped: the last-opened time decides those now (`engine|prop:` is gone with 整理建议).
    this.seen = all.filter((k) => !k.startsWith(OPENED) && !k.startsWith(LOG) && !k.startsWith(PROP)).slice(-MAX_SEEN_DOTS);
  }

  /**
   * app.js `sideTab` + `updateDots`: the shown tab's keys become seen (saved when that changed); 个性化 shown also moves
   * its "last opened" time up to the newest change it shows. Then the counts.
   */
  update(s: WikiState, shown: Tab, now: Date = new Date()): Counts {
    let changed = false;
    if (shown === "engine" && s.rsi.kind === "ready") {
      const top = newestTime(s.rsi.value);
      if (top !== null && (this.engineSeen === null || top > this.engineSeen)) { this.engineSeen = top; changed = true; }
    }
    const p = pendingKeys(s, now, this.engineSeen);
    const logOpen = shown === "engine" && s.personal.log && !s.personal.manage;
    if (shown === "today" || logOpen) {
      const fresh = p[shown].filter((k) => !isTransient(k)).map((k) => `${shown}|${k}`).filter((k) => !this.seen.includes(k));
      if (fresh.length > 0) { this.seen = [...this.seen, ...fresh].slice(-MAX_SEEN_DOTS); changed = true; }
    }
    if (changed) this.store?.save(this.engineSeen === null ? [...this.seen] : [...this.seen, `${OPENED}${this.engineSeen}`]);
    const count = (tab: DotTab): number =>
      tab === "today" && shown === "today" ? 0 : new Set(p[tab].filter((k) => isTransient(k) || !this.seen.includes(`${tab}|${k}`))).size;
    const log = p.engine.filter((k) => k.startsWith("tool:") && !this.seen.includes(`engine|${k}`)).length;
    return { today: count("today"), engine: count("engine"), log };
  }
}
