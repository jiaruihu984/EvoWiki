/**
 * 「帮我复习」's card in the chat: 今天's round itself (renderReviewCard over the same state, the same POST /wiki/review/grade),
 * so a grade given here shows on 今天 and the other way round. Only this card's answers count as a review; what is said in
 * the chat does not.
 */
import { btn, el } from "../ui/ew";
import type { RenderCtx, WikiActions } from "./actions";
import { WIKI_COPY } from "./copy";
import { pendingCount } from "./review-entries";
import { renderReviewCard } from "./render-review-card";
import type { WikiState } from "./state";

export function renderChatReview(s: WikiState, a: WikiActions, ctx: RenderCtx): HTMLElement {
  const box = el("div", "chat-review");
  const today = s.review.today;
  if (today.kind === "error") {
    box.append(el("div", "muted", today.message), btn(() => (WIKI_COPY.retry), "", () => a.startReview()));
    return box;
  }
  if (today.kind !== "ready") { box.append(el("div", "muted", () => (WIKI_COPY.loading))); return box; }
  const card = s.review.active ? renderReviewCard(s, a, ctx) : null;
  if (card !== null) { box.append(card); return box; }
  const left = pendingCount(s), done = s.review.pageRoundDone;
  if (done !== null) box.append(el("div", "muted", () => (WIKI_COPY.chatReviewPage(done))));
  if (left > 0) box.append(el("div", "muted", () => (WIKI_COPY.chatReviewLeft(left))), btn(() => (WIKI_COPY.chatReviewResume), "primary", () => a.startReview()));
  else if (done === null) {
    const n = today.value.done_today + today.value.page_round_today;
    box.append(el("div", "muted", () => (n > 0 ? WIKI_COPY.chatReviewDone(n) : WIKI_COPY.chatReviewNone)));
  }
  return box;
}
