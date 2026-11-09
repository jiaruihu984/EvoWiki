/** WikiController's pure helpers, shared types and limits (no state). */
import { isBusyApiError } from "../../api/busy";
import { AbortedError, ApiError, OfflineError } from "../../api/errors";
import { ewNotice } from "../../ui/ew-notice";
import { WIKI_COPY } from "../copy";
import { errorText, streamErrorText } from "../format";
import type { SyncResult, WikiNoteInput } from "../note-port";
import type { ChangeOut, Changed, DiffNoteOut, ErrorEvent, PageOut } from "../schema";
import type { PageView, ResultNote, RetryAction } from "../state";

export type StreamEnd = { kind: "done" } | { kind: "failed"; note: ResultNote } | { kind: "aborted" };
/** A new page's note opens with the first section streamed into it (`pending` until then; `opened` once it did). */
export interface StreamOpen { pending: boolean; opened: boolean }
/** What decides the file's path: folder, title and the parent's name. */
export const placementKey = (i: WikiNoteInput): string => JSON.stringify([i.folder ?? "", i.title, i.parentName ?? null]);

/** `partial`: the page as streamed so far (no asset fetch, no note-edit report; a full sync follows). */
export interface SyncJob { pageId: string; input: WikiNoteInput; json: string; partial: boolean; bg: boolean; valid?: () => boolean; waiters: ((ok: boolean) => void)[] }
/** Where a page action shows its result (P4): under a section, under the question box, or the page status line. */
export type Spot = { kind: "section"; sid: string } | { kind: "status" };
/** What a page sync after an edit elsewhere came to (`syncEdited`). */
export type EditedSync = "done" | "deferred" | "failed";
/**
 * Where a catalog / K / path / review action shows its result (P4): catalogNote, kNote, path.note, review.note or
 * recap.note.
 */
export type Area = "catalog" | "k" | "path" | "review";
/** Where a write's result line is, so a failed refresh GET after it can add its second line (ResultNote.sub). */
export type Target = { kind: "page"; pageId: string; spot: Spot } | { kind: "area"; area: Area };
/** What a refresh GET does when it fails after a successful write; null = a plain load (errors replace the area). */
export type OnFail = ((e: unknown) => void) | null;
/** A write in flight on a page: the page and the page token it started under. */
export interface Ctx { pageId: string; token: number }
/** Builds the page shown before the first GET (new page): `organized` = whether an outline arrived. */
export type Seed = (organized: boolean) => PageOut;

/** The lazy rewrite of an RSI-stale page (one deep rewrite, minutes at most): look with these pauses, then give up silently. */
export const REWRITE_STEP_MS = [4000, 6000, 8000, 10_000, 15_000] as const;
export const REWRITE_MAX_MS = 300_000;

/** The 知识星图 reads at most this many learning paths for its goal grouping. */
export const MAX_STAR_PATHS = 12;
/** A changed K is read into the star maps' store after this pause (a burst of changes reads once). */
export const STAR_REREAD_MS = 400;
export const MIN_DAYS = 1;
export const MAX_DAYS = 90;
export const MIN_REVIEW_PER_DAY = 0;
export const RETRY_ORGANIZE = { kind: "organize" } as const;
export const CATALOG: Target = { kind: "area", area: "catalog" };
export const PATH: Target = { kind: "area", area: "path" };
export const REVIEW: Target = { kind: "area", area: "review" };

/** The second line under a write's result when a refresh GET after it failed (the write itself was sent). */
export const refreshText = (e: unknown): string => (e instanceof OfflineError ? WIKI_COPY.refreshOffline : WIKI_COPY.refreshFailed);
export const isAbort = (e: unknown): boolean => e instanceof AbortedError || (e instanceof Error && e.name === "AbortedError");
export const ok = (text: string): ResultNote => ({ text, tone: "ok" });
export const bad = (text: string): ResultNote => ({ text, tone: "error" });

/** BlockResultsIn.events max_length (wiki_schemas.MAX_BLOCK_EVENTS). */
export const MAX_BLOCK_EVENTS = 200;

/** An exception (not a stream error event) ending a stream: 409 / 503 / 504 are worth a retry button (never 429). */
export function thrownNote(e: unknown, retry: RetryAction = RETRY_ORGANIZE): ResultNote {
  const worth = e instanceof ApiError && (e.status === 409 || e.status === 503 || e.status === 504);
  if (isBusyApiError(e)) ewNotice(errorText(e));      // 「请切换模型重试」
  return worth ? { ...bad(errorText(e)), retry } : bad(errorText(e));
}

/** A stream's error event: a retry button only when retryable, and never for the usage limit (even if flagged retryable). */
export function eventNote(d: ErrorEvent, retry: RetryAction): ResultNote {
  const worth = d.retryable && d.code !== "budget_exceeded";
  if (d.code === "llm_busy") ewNotice(streamErrorText(d));      // 「请切换模型重试」
  return worth ? { ...bad(streamErrorText(d)), retry } : bad(streamErrorText(d));
}

export function emptyPage(pageId: string, title: string, goal: string, organized: boolean): PageOut {
  return {
    page_id: pageId, next_step: null, title, folder: "", parent_name: "", trashed: false, goal, organized, source: null, profile: "", assumed: [], sections: [], diff: null,
    unresolved_conflicts: [], conflicts: [], budget_exceeded: false, collapsed: false, can_shelve: false, written_for: "", goal_notes: [], shelf_line: "", done: false, llm_calls: 0,
    writer: null, feedback: null, hand_edit: null, rewriting: false, sources: [],
  };
}

/** A section's 「改了」 marker: [section id, marker]; applied in order, a later one wins. */
export type Mark = [string, Changed];

/** An action's ChangeOut as markers (web changeMarks): moved counts as rewritten; a later list wins over moved. */
export function changeMarks(c: ChangeOut): Mark[] {
  return [
    ...c.moved.map((sid): Mark => [sid, "rewritten"]),
    ...c.rewritten.map((sid): Mark => [sid, "rewritten"]),
    ...c.inserted.map((sid): Mark => [sid, "inserted"]),
    ...c.unfolded.map((sid): Mark => [sid, "unfolded"]),
    ...c.folded.map((sid): Mark => [sid, "folded"]),
  ];
}

/** The open-time lazy update (PageOut.diff) as markers (web diffMarks). */
export function diffMarks(d: DiffNoteOut | null): Mark[] {
  if (d === null) return [];
  return [
    ...d.patched.map((sid): Mark => [sid, "rewritten"]),
    ...d.unfolded.map((sid): Mark => [sid, "unfolded"]),
    ...d.folded.map((sid): Mark => [sid, "folded"]),
  ];
}

export function withMarks(touched: Record<string, Changed>, marks: Mark[]): Record<string, Changed> {
  if (marks.length === 0) return touched;
  const next = { ...touched };
  for (const [sid, m] of marks) next[sid] = m;
  return next;
}

/** A new visit: no markers except what the landing GET's diff says. */
export function freshView(page: PageOut): PageView {
  return {
    page, streaming: {}, status: null,
    sectionNotes: {}, noteSync: { kind: "idle" },
    touched: withMarks({}, diffMarks(page.diff)), streamWriter: null,
  };
}

/** A note sync that leaves the note matching the page: its interactives are fetched (appendix A·6). */
export const SYNCED: ReadonlySet<SyncResult> = new Set<SyncResult>(["created", "updated", "unchanged"]);

export function without<T>(r: Record<string, T>, key: string): Record<string, T> {
  return Object.fromEntries(Object.entries(r).filter(([k]) => k !== key));
}

/** A quiet re-read of 今天 is skipped when the last one finished less than this long ago. */
export const TODAY_TTL_MS = 30_000;
