/**
 * 今天's review cards: which are left, which is shown. Where learning goes on is not decided here: the server's one
 * step (GET /wiki/progress, progress.ts) says it.
 */
import type { ConfirmCardOut, ReviewItemOut } from "./schema";
import type { WikiState } from "./state";

/** Display-only estimate (half a minute a card, at least one minute); not a number from the backend. */
export function reviewMinutes(count: number): number {
  return Math.max(1, count); // the demo: one minute per card
}

/** `seen` key of a confirmation card (cells key the others). */
export const confirmKey = (followupId: string): string => `confirm:${followupId}`;

/** One card of the in-place review: a normal item, or a due confirmation question (those come first, they are short). */
export type ReviewEntry =
  | { kind: "item"; key: string; item: ReviewItemOut }
  | { kind: "confirm"; key: string; confirm: ConfirmCardOut };

/** Every card of today's review not removed (the server lists only confirmation questions that are due). */
export function reviewEntries(s: WikiState): ReviewEntry[] {
  const r = s.review;
  if (r.today.kind !== "ready") return [];
  return [
    ...r.today.value.confirms.map((c): ReviewEntry => ({ kind: "confirm", key: confirmKey(c.followup_id), confirm: c })),
    ...r.today.value.items.filter((i) => !r.removed.includes(i.cell)).map((i): ReviewEntry => ({ kind: "item", key: i.cell, item: i })),
  ];
}

const answered = (s: WikiState, e: ReviewEntry): boolean =>
  e.kind === "item" ? s.review.results[e.key] !== undefined : s.review.confirmResults[e.confirm.followup_id] !== undefined;

/** The cards still to do: not removed, not moved past, not answered (confirmation questions count). */
export function pendingEntries(s: WikiState): ReviewEntry[] {
  return reviewEntries(s).filter((e) => !s.review.seen.includes(e.key) && !answered(s, e));
}

/** How many cards are still to do (see pendingEntries). */
export function pendingCount(s: WikiState): number {
  return pendingEntries(s).length;
}

/** The card the in-place review shows: the first one not removed and not moved past (an answered one waits for 下一张). */
export function currentEntry(s: WikiState): ReviewEntry | null {
  return reviewEntries(s).find((e) => !s.review.seen.includes(e.key)) ?? null;
}

/** The normal cards still to do (confirmation questions are counted by pendingCount). */
export function pendingCards(s: WikiState): ReviewItemOut[] {
  return reviewEntries(s).flatMap((e) => (e.kind === "item" && !s.review.seen.includes(e.key) && !answered(s, e) ? [e.item] : []));
}

/** The shown card when it is a normal one (null when none is left or a confirmation question is shown). */
export function currentCard(s: WikiState): ReviewItemOut | null {
  const e = currentEntry(s);
  return e !== null && e.kind === "item" ? e.item : null;
}
