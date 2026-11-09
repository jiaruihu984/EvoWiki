/**
 * 今天's first block, 待办事项 (app.js `nextBlock`): `.ew-blk` > h2, then
 * - reviewing: the in-place review card (render-review-card.ts);
 * - else the Bot task cards (the local review card first, bot/render-tasks.ts), or, with none left and the reads ready,
 *   the 学习任务全部完成 box.
 * A read that failed while the server answers says so in this block, once (`.ew-muted` 没能读到， + `.ew-btn.ew-link`
 * 重试); offline the block says nothing about it (the sidebar's one 未连接到服务器 line does). Path goals and aims are
 * text nodes.
 */
import { btn, el } from "../ui/ew";
import { withSettingsLink } from "../ui/settings-link";
import { type RenderCtx, type WikiActions, writeBlocked } from "./actions";
import { REVIEW_KEY } from "./bot/bot-controller";
import { renderDoneCard } from "./bot/done-card";
import { renderAllDone, renderTaskCard, taskCards } from "./bot/render-tasks";
import { WIKI_COPY } from "./copy";
import { renderReviewCard } from "./render-review-card";
import { pendingCount } from "./review-entries";
import { allGoalsDone, finished, stepLabel, todayStep } from "./progress";
import { reviewFirstRow } from "./review-first";
import type { StepOut } from "./schema";
import type { WikiState } from "./state";
import { TODAY_COPY } from "./today-copy";

/** A `.ew-muted` line, with `.ew-btn.ew-link` 重试 when `retry` is given. */
export function mutedLine(text: string, role: string, retry?: () => void): HTMLDivElement {
  const d = el("div", "muted");
  d.dataset.role = role;
  if (retry === undefined) { d.textContent = text; return d; }
  const b = btn(() => (WIKI_COPY.retry), "link", retry);
  b.dataset.action = "retry";
  d.append(el("span", "", text), b);
  return d;
}

function dueLine(n: number, a: WikiActions): HTMLDivElement {
  const d = el("div", "muted");
  d.dataset.role = "next-due";
  const b = btn(() => (TODAY_COPY.reviewStart), "link", () => a.startReview());
  b.dataset.action = "review-due";
  d.append(el("span", "", () => (TODAY_COPY.dueHidden(n))), b);
  return d;
}

export function renderNext(s: WikiState, a: WikiActions, ctx: RenderCtx): HTMLDivElement {
  const blk = el("div", "blk");
  blk.dataset.section = "next";
  blk.append(el("h2", "", () => (TODAY_COPY.todoTitle)));
  if (!s.configured) { blk.append(mutedLine(WIKI_COPY.needsSettings, "needs-settings")); return blk; }
  const card = s.review.active ? renderReviewCard(s, a, ctx) : null;
  if (card !== null) { blk.append(card); return blk; }

  // The review-finished card (bot.celebrate) comes before the task cards.
  const done = renderDoneCard(s, a);
  if (done !== null) blk.append(done);
  const cards = taskCards(s);
  const reviewRead = s.review.today.kind;
  const botRead = s.bot.today.kind;
  const reads = [reviewRead, s.catalog.kind, botRead, s.progress.kind];
  const failed = reads.includes("error");
  let hasTasks = done !== null || cards.length > 0;
  // Nothing to do is only said when every read that could have produced a card came back (an empty block would be a false "all done").
  // nothing to review or suggested: where learning goes on (the server's step, GET /wiki/progress), done only when nothing is left
  // (a Bot read not in flight — never made, or failed — does not hold the step back: its failure line says so below)
  const step = cards.length === 0 && reviewRead === "ready" && botRead !== "loading" ? todayStep(s) : null;
  // 今天的学习已完成 only when there were goals and every one is done: no goal at all is nothing to do, not something done
  if (step !== null && !finished(step)) { blk.append(stepCard(step, a, writeBlocked(s))); hasTasks = true; }
  // all done only once the Bot answered too (an unread Bot list could still hold something to do)
  else if (step !== null && botRead === "ready" && pendingCount(s) > 0) { blk.append(dueLine(pendingCount(s), a)); hasTasks = true; }   // the review card is cooling down: cards are still due, so neither done nor empty
  else if (step !== null && botRead === "ready" && allGoalsDone(s)) { blk.append(renderAllDone(s)); hasTasks = true; }
  const off = writeBlocked(s);
  for (const c of cards) blk.append(renderTaskCard(c, a, off && c.key !== REVIEW_KEY));
  // A read that failed says so here, once for the block (nothing offline: the banner says it).
  if (failed && s.conn !== "offline") {
    blk.append(mutedLine(TODAY_COPY.todoLoadFailed, "next-error", () => a.retryConnection()));
  } else if (!failed && !hasTasks && s.conn !== "offline") {
    if (reads.some((kind) => kind === "loading" || kind === "idle")) blk.append(mutedLine(TODAY_COPY.todoLoading, "next-loading"));
    else if (reads.every((kind) => kind === "ready")) blk.append(mutedLine(TODAY_COPY.todoEmpty, "next-empty"));
  }
  const note = s.path.note;
  if (note !== null && note.tone === "error" && s.conn !== "offline") {
    const line = mutedLine("", "path-error");
    line.append(...withSettingsLink(note.text, () => a.openSettings()));
    blk.append(line);
  }
  return blk;
}

/** 下一步 when the Bot has nothing to suggest: the goal, and where it goes on (the page to write or read, the next block). */
function stepCard(step: StepOut, a: WikiActions, off: boolean): HTMLDivElement {
  const card = el("div", "card");
  card.append(el("h2", "", step.goal));
  if (step.title !== "") card.append(el("div", "faint", step.title));
  const hint = step.review_first;
  if (hint !== null) card.append(reviewFirstRow(hint, (choice) => { a.answerReviewFirst(hint, choice); }));
  const go = btn(stepLabel(step), "primary", () => a.continueLearning());
  go.dataset.action = "continue";
  go.disabled = off;
  const acts = el("div", "acts");
  acts.append(go);
  card.append(acts);
  return card;
}
