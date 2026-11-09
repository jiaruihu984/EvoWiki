/**
 * The ledger actions that wait for a yes / no (shown on their page: page-waits.ts), then the RSI changes shown by
 * rsi-changes.ts (个性化 · 活动记录). A ledger change is joined to its action by `patch_id === action_id` (B3
 * LedgerChangeSource), which gives it its page. Pure logic.
 */
import { type RsiCard, rsiCard } from "../rsi-changes";
import type { RsiChangeOut } from "../schema";
import type { Load, WikiState } from "../state";
import type { ActionOut } from "./bot-schema";

export type ChangeRow =
  | { type: "wait"; key: string; action: ActionOut }
  | { type: "change"; key: string; card: RsiCard; action: ActionOut | null };

/** The row's `data-chg` key (and 查看's target): `act:<action id>` for a ledger change, `rsi:<patch id>` otherwise. */
export function changeKey(c: RsiChangeOut, action: ActionOut | null): string {
  return action !== null || c.track === "G" ? `act:${c.patch_id}` : `rsi:${c.patch_id}`;
}

/** store/bot.py APPROVED_STALE. */
const APPROVED_STALE_MS = 2 * 60_000;
/**
 * Still waits for a yes (B3 count_pending): `proposed`, or `approved` for more than 2 minutes (a 429 / crash mid-approve left it; approving
 * again finishes it). Age by decided_at, else created_at.
 */
export const waiting = (a: ActionOut, now: number = Date.now()): boolean => {
  if (a.status === "proposed") return true;
  const at = Date.parse(a.decided_at ?? a.created_at);
  return a.status === "approved" && !Number.isNaN(at) && at < now - APPROVED_STALE_MS;
};
const loaded = <T,>(l: Load<T[]>): T[] => (l.kind === "ready" ? l.value : []);
const time = (a: ActionOut): number => { const t = Date.parse(a.created_at); return Number.isNaN(t) ? 0 : t; };

/** Waits first (newest first), then the shown changes in the backend's order. */
export function changeRows(s: WikiState): ChangeRow[] {
  const actions = loaded(s.bot.actions);
  const byId = new Map(actions.map((a) => [a.action_id, a] as const));
  const waits = actions.filter((a) => waiting(a)).sort((x, y) => time(y) - time(x));
  const out: ChangeRow[] = waits.map((a) => ({ type: "wait", key: `act:${a.action_id}`, action: a }));
  if (s.rsi.kind !== "ready") return out;
  for (const c of s.rsi.value) {
    const card = rsiCard(c);
    if (card === null) continue;
    const action = byId.get(c.patch_id) ?? null;
    if (action !== null && waiting(action)) continue;
    out.push({ type: "change", key: changeKey(c, action), card, action });
  }
  return out;
}
