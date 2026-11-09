// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { WIKI_COPY } from "./copy";
import { renderChatReview } from "./render-chat-review";
import { btn, fakeActions, fakeCtx } from "./render-test-kit";
import type { ReviewItemOut, ReviewTodayOut } from "./schema";
import { INITIAL_WIKI_STATE, type ReviewSlot, type WikiState } from "./state";

afterEach(() => { document.body.replaceChildren(); });

const item: ReviewItemOut = {
  card_id: "card-1", cell: "c1.term", cells: ["c1.term", "c1.notation"], concept_id: "c1", concept_name: "度", aspect: "term", band: "likely", mode: "cue",
  page_id: "p1", page_title: "页一", section_id: "s1", heading: "小节", cue: "度是什么", answer: "连了几条边", check: null, recall_now: 0.5,
  days_to_threshold: -1, in_goal: true, exam: false, artifact_id: null };
const list = (items: ReviewItemOut[], done = 0): ReviewTodayOut => ({ items, due: items.length, done_today: done, limit: 10, confirms: [], page_round_today: 0, streak_days: 0 });
const state = (review: Partial<ReviewSlot>): WikiState => ({ ...INITIAL_WIKI_STATE, conn: "online", configured: true, review: { ...INITIAL_WIKI_STATE.review, ...review } });
const ready = (v: ReviewTodayOut) => ({ kind: "ready" as const, value: v });
const draw = (s: WikiState) => {
  const a = fakeActions();
  const root = renderChatReview(s, a, fakeCtx());
  document.body.append(root);
  return { root, a };
};

describe("the review card in the chat is 今天's round", () => {
  it("a running round: the same card, grades through the same action, the answer hidden until 查看答案", () => {
    const { root } = draw(state({ today: ready(list([item])), active: true }));
    expect(root.querySelector("[data-role=review]")!.textContent).toContain("度是什么");
    expect(root.textContent).not.toContain("连了几条边");
    const shown = draw(state({ today: ready(list([item])), active: true, revealed: [item.cell] }));
    btn(shown.root, "[data-action=review-grade][data-value=recalled]").click();
    expect(shown.a.gradeReview).toHaveBeenCalledWith(item.cell, "recalled");
  });
  it("ended before the last card (or a reloaded chat): how many are left, 继续复习 starts the round again", () => {
    const { root, a } = draw(state({ today: ready(list([item])), active: false }));
    expect(root.textContent).toContain(WIKI_COPY.chatReviewLeft(1));
    btn(root, "button").click();
    expect(a.startReview).toHaveBeenCalled();
  });
  it("nothing left: says what was done today, or that nothing is due; never a card", () => {
    expect(draw(state({ today: ready(list([], 3)) })).root.textContent).toBe(WIKI_COPY.chatReviewDone(3));
    expect(draw(state({ today: ready(list([], 0)) })).root.textContent).toBe(WIKI_COPY.chatReviewNone);
  });
  it("a page's round over: this page's count, today's cards only when some are left; never 没有要复习的", () => {
    const none = draw(state({ today: ready(list([])), pageRoundDone: 3 })).root;
    expect(none.textContent).toBe(WIKI_COPY.chatReviewPage(3));
    const some = draw(state({ today: ready(list([item])), pageRoundDone: 3 }));
    expect(some.root.textContent).toContain(WIKI_COPY.chatReviewPage(3));
    expect(some.root.textContent).toContain(WIKI_COPY.chatReviewLeft(1));
    btn(some.root, "button").click();
    expect(some.a.startReview).toHaveBeenCalled();
  });
  it("a reloaded chat counts today's page rounds too: what was reviewed is said, the batch's count stays apart", () => {
    const { root } = draw(state({ today: ready({ ...list([], 1), page_round_today: 3 }) }));
    expect(root.textContent).toBe(WIKI_COPY.chatReviewDone(4));
  });
  it("a failed read says so and offers 重试", () => {
    const { root, a } = draw(state({ today: { kind: "error", message: "没能读到" } }));
    expect(root.textContent).toContain("没能读到");
    btn(root, "button").click();
    expect(a.startReview).toHaveBeenCalled();
  });
});
