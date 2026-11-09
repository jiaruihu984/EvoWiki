/**
 * 「建议先复习 X」: 先复习 / 跳过, the server's suggestion on a step (StepOut.review_first, wikiapp/review_first.py). Shown at
 * the end of a page and on 今天's goal row. It changes nothing by itself: the step's own button still goes on.
 */
import { btn, el } from "../ui/ew";
import { WIKI_COPY } from "./copy";
import type { ReviewFirstIn, ReviewFirstOut } from "./schema";

export type ReviewFirstChoice = ReviewFirstIn["choice"];

export function reviewFirstRow(hint: ReviewFirstOut, answer: (choice: ReviewFirstChoice) => void): HTMLDivElement {
  const row = el("div", "rfirst");
  const go = el("span", "rfirst-go");
  go.append(btn(() => (WIKI_COPY.reviewFirstGo), "", () => { answer("review"); }), btn(() => (WIKI_COPY.reviewFirstSkip), "", () => { answer("skip"); }));
  row.append(el("span", "", () => (WIKI_COPY.reviewFirst(hint.name))), go);
  return row;
}
