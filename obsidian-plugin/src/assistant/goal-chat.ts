/**
 * 学点新的 → 开始 (chat-first blocks, spec 2026-10-23): the assistant splits the goal into a few big blocks and writes a
 * `goal_new` ledger row (content `{goal, background, title, blocks, pages: [{key, title, covers}], confirm}`); the card in
 * the chat is that row's draft. The 学点新的 view follows the rows of the conversation it started (the demo's
 * toLearn draft / blocks / unplan messages), through `AssistantController.onGoalRow`:
 * - waiting, with blocks → the cloud shows them (edits on the card follow at once);
 * - executed → the goal is open: `result` gives its draft (GET /wiki/goals/{draft_id}) and its map;
 * - rejected (取消; not `replaced`, which only means a newer draft took its place) or undone → back to the goal card.
 */
import type { ActionOut } from "../wiki/bot/bot-schema";
import type { ActionCardState, CardPhase } from "./card-state";

/** The ledger kind of a new goal (assistant tools new_goal). */
export const GOAL_KIND = "goal_new";
/** The ledger's detail on a goal_new row that a newer draft replaced (edit_goal_blocks). */
export const REPLACED = "replaced";

/** One block of the draft as the card shows it (a block the learner added on the card has no key yet). */
export interface GoalBlock { key: string | null; name: string; scope: string; /** 基础 / 核心 / 进阶 / 拓展, "" when none. */ level?: string }
/** What the executed row's result says: the open draft (round 1) and the goal's map. */
export interface GoalResult { draftId: string | null; mapId: string | null }
export interface PendingGoal { row: GoalChatRow; goal: string }
export interface GoalChatRow {
  /** The conversation the card is in (AssistantController's conversation key). */
  key: string;
  actionId: string;
  phase: CardPhase;
  /** Rejected because a newer draft replaced it (the card says 已换成新的分法; the view waits for the new one). */
  replaced: boolean;
  /** The summarized topic; the card's formatted action heading is not a goal name. */
  title: string;
  /** The draft as it stands (the learner's edits on the card included); the confirmed list once executed. */
  blocks: GoalBlock[];
  /** Executed: the goal opened; null when the row (or this read of it) carries no result. */
  result: GoalResult | null;
}

const str = (v: unknown): string | null => (typeof v === "string" && v !== "" ? v : null);

/** The goal the executed row opened: ActionOut's (or a replayed card's) `result` (`draft_id`, `map_id`, …); null: none. */
export function goalResultOf(o: Pick<ActionOut, "result">): GoalResult | null {
  const r = o.result;
  if (r === null || typeof r !== "object") return null;
  const draftId = str(r.draft_id), mapId = str(r.map_id);
  return draftId !== null || mapId !== null ? { draftId, mapId } : null;
}

/** The card's blocks: the editable draft while it waits, else its items. */
export function goalBlocks(c: ActionCardState): GoalBlock[] {
  return c.draft.map((d) => ({ key: d.isNew ? null : d.key, name: d.title, scope: d.covers, level: d.level }));
}
