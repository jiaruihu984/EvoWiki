/**
 * The plugin's view of /wiki/*: the wire types and constant tables generated from the pydantic models
 * (src/generated/wiki.ts and sources.ts, by scripts/gen_plugin_types.py; tests/service/test_plugin_types_fresh.py fails
 * when they are stale), plus the plugin-side names and helpers below. Nothing here is a hand copy of a Python model.
 */
import type { Aspect, Band, CellOut, FeedbackIn, QuestionKind, SectionRole, SettingsIOInput } from "../generated/wiki";
import type { SourceUse } from "../generated/sources";
import { FOLDABLE_ROLE_LIST } from "../generated/wiki";

export * from "../generated/wiki";
export * from "../generated/sources";

export const FOLDABLE_ROLES: ReadonlySet<SectionRole> = new Set<SectionRole>(FOLDABLE_ROLE_LIST);
/** CellOut.declared: 「我会」 / 「我不会」. */
export type Declared = NonNullable<CellOut["declared"]>;
export type FeedbackKind = FeedbackIn["kind"];
/** The self-report question kind as a value (the fake goal API builds questions with it). */
export const SELF_REPORT_KIND = "self_report" satisfies QuestionKind;
/** Share per band, summing to 1; {} = no cells. */
export type BandShares = Partial<Record<Band, number>>;

/**
 * PUT /wiki/settings body as the plugin sends it: the server changes `review_per_day` / `language` / `page_limit_usd` only
 * when the key is present (`model_fields_set`; null page_limit_usd = no cap, sent only by the 用量 row).
 */
export type SettingsPut = SettingsIOInput;

export const cellOf = (conceptId: string, aspect: Aspect): string => `${conceptId}.${aspect}`;
/** Python split_cell: rpartition("."). */
export function splitCell(cell: string): { conceptId: string; aspect: string } {
  const i = cell.lastIndexOf(".");
  return i < 0 ? { conceptId: cell, aspect: "" } : { conceptId: cell.slice(0, i), aspect: cell.slice(i + 1) };
}

// ---- plugin side only (never on the wire) ----
/**
 * How a wrong question is found: a check in a page (page + section), or a review card in check mode (card id, its cell and
 * place). `idk` = answered 不知道 (then 粗心选错了 is not offered); `answerRef` = the answer's `answer_ref`; `pageTitle` names the
 * page for the assistant. The server reads only the page, the section, the choice and the answer_ref.
 */
export type FollowupTarget =
  | { kind: "check"; pageId: string; pageTitle: string; sid: string; idk: boolean; artifactId?: string | null }
  | { kind: "card"; cardId: string; cell: string; pageId: string; pageTitle: string; sid: string; idk: boolean; artifactId?: string | null };

/** POST /wiki/sources (multipart, form fields; no pydantic model): exactly one of a file, a URL or pasted text, plus the optional fields. */
export type SourceUploadIn = (
  | { file: { name: string; data: ArrayBuffer; type?: string }; url?: undefined; text?: undefined }
  | { url: string; file?: undefined; text?: undefined }
  | { text: string; file?: undefined; url?: undefined }
) & { title?: string; origin?: string; use?: SourceUse; goal_id?: string; page_id?: string; session_id?: string };
