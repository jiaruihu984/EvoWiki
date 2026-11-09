import { tr } from "../i18n/index";
/**
 * 待确认的记忆 (full-demo app.js `memoriesCard` / `cfTip` / `cfMeta`; approved 2026-10-29): GET MEMORY_CONFLICTS_PATH
 * checked field by field, and each group as the card shows it — every item's text and its "?" (where it is written, then
 * date · source · what it is limited to, then his words, then where he said them). The card only lists; 处理 settles the
 * groups in the assistant (intent resolve_memory_conflicts). The fields mean what memory/conflicts.py says.
 */
import { ContractError } from "../assistant/parse";
import type { MemoryConflictItemOut, MemoryConflictOut } from "../generated/assistant";
import { dayDiff, parseWhen } from "../ui/dates";

export type { MemoryConflictItemOut, MemoryConflictOut };

export const CONFLICT_COPY = {
  get title() { return tr("wiki.memory-conflicts.CONFLICT_COPY.title.1"); },
  groups: (n: number): string => tr("wiki.memory-conflicts.CONFLICT_COPY.groups.2", { v0: n }),
  get lead() { return tr("wiki.memory-conflicts.CONFLICT_COPY.lead.3"); },
  get tipLabel() { return tr("wiki.memory-conflicts.CONFLICT_COPY.tipLabel.4"); },
  get resolve() { return tr("wiki.memory-conflicts.CONFLICT_COPY.resolve.5"); },
  /** The chat 处理 opens (its first message, sent with intent resolve_memory_conflicts; the chat shows no bubble for it). */
  get chatTitle() { return tr("wiki.memory-conflicts.CONFLICT_COPY.chatTitle.6"); },
} as const;

/** app.js CF_SRC. */
const SOURCE: Record<MemoryConflictItemOut["source"], string> = {
  get chat() { return tr("wiki.memory-conflicts.SOURCE.chat.7"); }, get consolidation() { return tr("wiki.memory-conflicts.SOURCE.consolidation.8"); }, get rule_page() { return tr("wiki.memory-conflicts.SOURCE.rule_page.9"); }, get profile() { return tr("wiki.memory-conflicts.SOURCE.profile.10"); },
};
const CATEGORIES = ["rule", "profile", "decision"];
const WHERE_KINDS = ["domain", "page", "goal", "concept"];

type O = Record<string, unknown>;
function obj(x: unknown, c: string): O {
  if (x === null || typeof x !== "object" || Array.isArray(x)) throw new ContractError(`${c} is not an object`);
  return x as O;
}
function str(o: O, k: string, c: string): void {
  if (typeof o[k] !== "string") throw new ContractError(`${c}.${k} must be a string`);
}
function optStr(o: O, k: string, c: string): void {
  if (o[k] !== undefined && o[k] !== null && typeof o[k] !== "string") throw new ContractError(`${c}.${k} must be a string or null`);
}

/** The answer of GET MEMORY_CONFLICTS_PATH (MemoryConflictListOut): its groups. */
export function parseConflicts(x: unknown): MemoryConflictOut[] {
  const o = obj(x, "MemoryConflictListOut");
  if (!Array.isArray(o.conflicts)) throw new ContractError("MemoryConflictListOut.conflicts must be a list");
  for (const g of o.conflicts) {
    const go = obj(g, "MemoryConflictOut");
    for (const k of ["conflict_id", "why", "detected_at"]) str(go, k, "MemoryConflictOut");
    if (!Array.isArray(go.items) || go.items.length < 2) throw new ContractError("MemoryConflictOut.items must list at least two");
    for (const it of go.items) {
      const io = obj(it, "MemoryConflictItemOut");
      str(io, "id", "MemoryConflictItemOut"); str(io, "text", "MemoryConflictItemOut");
      if (!CATEGORIES.includes(io.category as string)) throw new ContractError("MemoryConflictItemOut.category has an unknown value");
      if (!(typeof io.source === "string" && io.source in SOURCE)) throw new ContractError("MemoryConflictItemOut.source has an unknown value");
      for (const k of ["date", "where", "section", "origin_ref", "origin_title", "quote"]) optStr(io, k, "MemoryConflictItemOut");
      if (io.where_kind != null && !WHERE_KINDS.includes(io.where_kind as string)) throw new ContractError("MemoryConflictItemOut.where_kind has an unknown value");
      if (typeof io.origin_deleted !== "boolean") throw new ContractError("MemoryConflictItemOut.origin_deleted must be a boolean");
    }
  }
  return o.conflicts as MemoryConflictOut[];
}

/** app.js dates: 今天, else M月D日 (another year: YYYY年M月D日). */
function day(iso: string | null | undefined, now: Date): string | null {
  const d = iso ? parseWhen(iso) : null;
  if (d === null) return null;
  if (dayDiff(d, now) === 0) return tr("wiki.memory-conflicts..11");
  const md = tr("wiki.memory-conflicts.md.12", { v0: d.getMonth() + 1, v1: d.getDate() });
  return d.getFullYear() === now.getFullYear() ? md : tr("wiki.memory-conflicts..13", { v0: d.getFullYear(), v1: md });
}

/**
 * What an item is limited to, as the demo says it after 只用于: a rule's domain plain (数学), a decision's page by its title in
 * 「」; null = every page (a profile line, a goal or concept the demo has no words for).
 */
export function placeOf(x: MemoryConflictItemOut): { text: string; page: string | null } | null {
  const w = x.where?.trim() ?? "";
  if (w === "") return null;
  if (x.where_kind === "domain") return { text: w, page: null };
  if (x.where_kind === "page") return { text: `「${w}」`, page: w };
  return null;
}

/** app.js cfTip: where it is written, date · source · 只用于…, 「his words」, where he said them. */
export function conflictTip(x: MemoryConflictItemOut, now: Date): string[] {
  const place = placeOf(x);
  const at = x.category === "rule" ? tr("wiki.memory-conflicts.at.14") : x.category === "profile" ? tr("wiki.memory-conflicts.at.15")
    : place?.page ? tr("wiki.memory-conflicts.at.16", { v0: place.page }) : tr("wiki.memory-conflicts.at.17");
  const meta = [day(x.date, now), SOURCE[x.source]].filter((s): s is string => s !== null).join(" · ") + (place ? tr("wiki.memory-conflicts.meta.18", { v0: place.text }) : "");
  // a rule's evidence line is 「你说过「…」」 (memory/conflicts.py): the demo shows his words only
  const words = (x.quote ?? "").trim().replace(/^你说过「(.*)」$/s, "$1");
  const quote = words !== "" ? `「${words}」` : null;
  const from = x.origin_deleted ? tr("wiki.memory-conflicts.from.19") : x.origin_title ? tr("wiki.memory-conflicts.from.20", { v0: x.origin_title })
    : x.source === "rule_page" ? tr("wiki.memory-conflicts.from.21") : x.category === "profile" ? tr("wiki.memory-conflicts.from.22") : tr("wiki.memory-conflicts.from.23");
  return [at, meta, ...(quote ? [quote] : []), from];
}

/** One group as the card draws it. */
export interface ConflictRow { text: string; tip: string[] }
export interface ConflictView { id: string; rows: ConflictRow[] }

export function conflictViews(groups: readonly MemoryConflictOut[], now: Date): ConflictView[] {
  return groups.map((g) => ({ id: g.conflict_id, rows: g.items.map((x) => ({ text: x.text, tip: conflictTip(x, now) })) }));
}
