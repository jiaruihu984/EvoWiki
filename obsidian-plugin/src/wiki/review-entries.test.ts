import { describe, expect, it } from "vitest";
import { pendingCount, pendingEntries, reviewMinutes } from "./review-entries";
import type { ReviewItemOut } from "./schema";
import { INITIAL_WIKI_STATE, type WikiState } from "./state";

const item = (cell: string, over: Partial<ReviewItemOut> = {}): ReviewItemOut => ({
  card_id: `card-${cell}`, cell, cells: [cell], concept_id: "c1", concept_name: "均值", aspect: "procedure", band: "likely", mode: "cue",
  page_id: "p1", page_title: "页一", section_id: "s1", heading: "小节", cue: "线索", answer: "", check: null, recall_now: 0.5,
  days_to_threshold: -1, in_goal: true, exam: false, artifact_id: null, ...over });
function state(o: { items?: ReviewItemOut[]; seen?: string[];
  removed?: string[]; results?: WikiState["review"]["results"] } = {}): WikiState {
  return { ...INITIAL_WIKI_STATE,
    review: { ...INITIAL_WIKI_STATE.review,
      today: { kind: "ready", value: { items: o.items ?? [], due: 0, done_today: 0, limit: 10, confirms: [], page_round_today: 0, streak_days: 0 } },
      seen: o.seen ?? [], removed: o.removed ?? [], results: o.results ?? {} } };
}
describe("reviewMinutes (display estimate, not a backend number)", () => {
  it.each([[1, 1], [2, 2], [3, 3], [5, 5], [10, 10], [0, 1]])("%i cards -> about %i min", (n, m) => {
    expect(reviewMinutes(n)).toBe(m);
  });
});

describe("pendingEntries (one list for the count and the names)", () => {
  it("leaves out cards that are seen, removed or answered; pendingCount is its length", () => {
    const answer = { a: { band: { cell: "a", from: "likely", to: "known" }, grade: "recalled", correct: null, correctIndex: null, choice: null, line: { text: "x", tone: "ok" } } } as never;
    const s = state({ items: [item("a"), item("b"), item("c"), item("d")], seen: ["b"], removed: ["c"], results: answer });
    expect(pendingEntries(s).map((e) => e.key)).toEqual(["d"]);
    expect(pendingCount(s)).toBe(1);
  });
});
