/**
 * The plugin's view of /wiki/bot/*: the wire types generated from bot/*.py (src/generated/bot.ts, by
 * scripts/gen_plugin_types.py) plus the plugin-side names below.
 */
import type { BotSettings, DiffLine, Level, PauseIn, Scope, SuggestionOut as WireSuggestionOut, BotKind } from "../../generated/bot";

export * from "../../generated/bot";

/**
 * SuggestionOut.action as the plugin reads it (the server types it as a bare dict; bot/suggestions.py builds these shapes).
 * `send_to_assistant` / `open_page` `ask`: the text to send; `revise_from_source`: 更新这 N 页 (POST /wiki/sources/{id}/revise).
 */
export type BotAction =
  | { type: "start_practice" } | { type: "open_page"; page_id: string; ask?: string }
  | { type: "open_goal"; path_id: string } | { type: "send_to_assistant"; text: string } | { type: "open_settings" }
  // B5 weekly review: 重新规划 the goal's path / 搁置 the goal
  | { type: "replan_goal"; path_id: string } | { type: "park_goal"; path_id: string }
  | { type: "revise_from_source"; source_id: string; page_ids: string[] };
/** The wire SuggestionOut with `action` read as a BotAction (this local name shadows the generated one). */
export interface SuggestionOut extends Omit<WireSuggestionOut, "action"> { action: BotAction }

export type DiffKind = DiffLine["kind"];
export type PauseUntil = PauseIn["until"];
/** A settings change: only the fields (and the kinds / levels entries) the user touched; the controller merges it onto the latest settings. */
export type SettingsPatch = Partial<Omit<BotSettings, "kinds" | "levels">>
  & { kinds?: Partial<Record<BotKind, boolean>>; levels?: Partial<Record<Scope, Level>> };
