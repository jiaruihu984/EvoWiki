import { labelNode } from "../i18n/index";
/**
 * The in-place review on 今天 (app.js `reviewCard` / `confirmCard` / `followups`), one `.ew-card` at a time:
 * `.ew-rvtop` (progress bar + the ghost 反馈 button + × 结束复习), `.ew-rv-q`, then
 * - a cue card: the recall cue is the question, `.ew-rv-src` 页面 · 小节; 先试着回忆，再查看答案。 + 查看答案 → `.ew-tipcard
 *   .ew-rv-answer` (the card's short answer, rendered as the note renders it) + 跳到这一节 + `.ew-grades`;
 *   `.ew-grades` 忘了 / 模糊 / 熟悉 → the next card by itself once the grade is recorded (wiring: gradeReview then
 *   nextReview; no result line; 下一张 / 完成 shows only until then);
 * - a check card: `.ew-check` (no frame) holding `.ew-opts` A. … + `.ew-o.ew-idk` 不知道 → right / wrong marks and ✓ 答对了,
 *   after a wrong answer the three follow-ups (`.ew-follow`) → 下一张 / 完成;
 * - a due confirmation question (confirmCard): the same options → right / wrong marks, after a wrong one 还没掌握 / 重新读.
 * 反馈 opens 助手 with the card quoted (the assistant fixes the card); there is no form here.
 * Server and LLM text (page titles, headings, cues, stems, options) goes in as text nodes. The answer stays out of the DOM
 * until 看答案. A failed write is a `.ew-muted` line in the card; a write that cannot run says why the same way.
 */
import { btn, el } from "../ui/ew";
import { isFresh, resumeAnimations } from "../ui/anim";
import { checkSvg, s as svg } from "../ui/svg";
import { type RenderCtx, type WikiActions, writeBlocked } from "./actions";
import { WIKI_COPY } from "./copy";
import { type FollowupHost, renderCheckFollow } from "./followup";
import { confirmKey, currentEntry, type ReviewEntry, reviewEntries } from "./review-entries";
import { noteBlock } from "./note/format";
import { letter } from "./render-common";
import { type ConfirmCardOut, type Grade, type ReviewItemOut } from "./schema";
import type { ResultNote, WikiState } from "./state";

/** Worst to best, as app.js lists them. */
const GRADES: readonly { grade: Grade; label: string }[] = [
  { grade: "forgot", get label() { return WIKI_COPY.gradeForgot; } },
  { grade: "fuzzy", get label() { return WIKI_COPY.gradeFuzzy; } },
  { grade: "recalled", get label() { return WIKI_COPY.gradeRecalled; } },
];

const muted = (text: string, role?: string): HTMLDivElement => {
  const d = el("div", "muted", text);
  if (role !== undefined) d.dataset.role = role;
  return d;
};
const act = <T extends HTMLElement>(e: T, action: string, extra: Record<string, string> = {}): T => {
  e.dataset.action = action;
  for (const [k, v] of Object.entries(extra)) e.setAttribute(`data-${k}`, v);
  return e;
};

/**
 * app.js `okLine`: the check tick + 答对了 (the tick draws itself on the fresh render only).
 * The demo's result sentence after it is not drawn here (user review).
 */
function okLine(fresh: number | null): HTMLDivElement {
  const d = el("div", "okline" + (fresh !== null ? " anim" : ""));
  d.append(checkSvg(), labelNode(() => (WIKI_COPY.reviewRight)));
  if (fresh !== null) resumeAnimations(d, fresh);
  return d;
}

/** How long the right-answer pop and tick animate after the answer was recorded. */
const ANSWER_ANIM_MS = 500;

/** When the answer of card `key` was recorded, if it is still animating (the controller stamps it; renders only read it). */
function freshAt(s: WikiState, key: string): number | null {
  const f = s.review.fresh;
  return f !== null && f.key === key && isFresh(f.at, ANSWER_ANIM_MS) ? f.at : null;
}

const blockedWhy = (s: WikiState): string | null => {
  if (!writeBlocked(s)) return null;
  if (!s.configured) return WIKI_COPY.needsSettings;
  return s.conn === "offline" ? WIKI_COPY.reviewBlockedOffline : WIKI_COPY.reviewBlockedBusy;
};

/** 下一张 / 完成 (primary), shown once the card is answered and not moved past. */
function nextButton(s: WikiState, key: string, rest: boolean, a: WikiActions): HTMLButtonElement | null {
  if (!s.review.active || s.review.seen.includes(key)) return null;
  return act(btn(() => (rest ? WIKI_COPY.reviewNext : WIKI_COPY.reviewNextDone), "primary", () => a.nextReview(key)), "review-next", { cell: key });
}

/** The follow-up row reads its state from the wiki state and acts through the sidebar's actions. */
function followupHost(s: WikiState, a: WikiActions): FollowupHost {
  return {
    followupView: (key) => s.followups[key] ?? { kind: "idle", error: null },
    followup: (t, c) => { a.followup(t, c); },
    followupBlockedWhy: () => (s.demo ? WIKI_COPY.followupDemo : !s.configured ? WIKI_COPY.needsSettings : s.conn === "offline" ? WIKI_COPY.followupOffline : null),
  };
}

/** The options box of a check / confirmation card: `.ew-check` without its frame, `.ew-opts`, the dashed 不知道. */
function options(opts: readonly string[], picked: number | null, correct: number | null, disabled: boolean, pop: number | null,
                 pick: (i: number) => void, action: string, extra: Record<string, string>): HTMLDivElement {
  const box = el("div", "check");
  box.style.cssText = "border:0;padding:0;margin:0";
  const o = el("div", "opts");
  const mark = (b: HTMLButtonElement, i: number): void => {
    if (picked === null) return;
    if (i === correct) { b.classList.add("ew-right"); if (pop !== null) { b.classList.add("ew-pop"); resumeAnimations(b, pop); } }
    else if (i === picked) b.classList.add("ew-wrong");
  };
  opts.forEach((t, i) => {
    const b = act(el("button", "o", `${letter(i)}. ${t}`), action, { ...extra, choice: String(i) });
    b.type = "button";
    b.disabled = disabled || picked !== null;
    mark(b, i);
    b.onclick = () => pick(i);
    o.append(b);
  });
  const idk = act(el("button", "o idk", () => (WIKI_COPY.idk)), action, { ...extra, choice: "-1" });
  idk.type = "button";
  idk.disabled = disabled || picked !== null;
  if (picked === -1) idk.classList.add("ew-wrong");
  idk.onclick = () => pick(-1);
  o.append(idk);
  box.append(o);
  return box;
}

function cueBody(s: WikiState, item: ReviewItemOut, a: WikiActions, rest: boolean, ctx: RenderCtx): HTMLElement[] {
  const cell = item.cell;
  if (!s.review.revealed.includes(cell)) {
    return [muted(WIKI_COPY.reviewThinkFirst), act(btn(() => (WIKI_COPY.reviewReveal), "primary", () => a.revealCue(cell)), "review-reveal", { cell })];
  }
  const r = s.review.results[cell];
  if (r !== undefined) {
    // Self-graded: no result line; the review moves on by itself (wiring), 下一张 shows only until it does.
    const next = nextButton(s, cell, rest, a);
    return next !== null ? [next] : [];
  }
  const g = el("div", "grades");
  for (const x of GRADES) {
    const b = act(btn(x.label, "", () => a.gradeReview(cell, x.grade)), "review-grade", { cell, value: x.grade });
    b.disabled = writeBlocked(s);
    g.append(b);
  }
  const jump = act(el("button", "rv-jump", () => (WIKI_COPY.reviewToSection)), "review-to-section", { cell });
  jump.type = "button";
  jump.onclick = () => a.goToChange(item.page_id, item.section_id);
  const why = blockedWhy(s);
  const tip = el("div", "tipcard rv-answer");
  tip.dataset.role = "answer";
  ctx.renderMarkdown(tip, noteBlock(item.answer));
  return [tip, jump, g, ...(why !== null ? [muted(why, "blocked-why")] : [])];
}

function checkBody(s: WikiState, item: ReviewItemOut, a: WikiActions, rest: boolean): HTMLElement[] {
  const check = item.check;
  if (check === null) return [];
  const cell = item.cell;
  const r = s.review.results[cell];
  const picked = r === undefined ? null : r.choice;
  const right = r?.correct === true;
  const fresh = right ? freshAt(s, cell) : null;
  const out: HTMLElement[] = [options(check.options, picked, r?.correctIndex ?? null, writeBlocked(s), fresh, (i) => a.answerReview(cell, i), "review-answer", { cell })];
  if (r === undefined) {
    const why = blockedWhy(s);
    if (why !== null) out.push(muted(why, "blocked-why"));
    return out;
  }
  if (right) out.push(okLine(fresh));
  // No verdict sentence (user review): the options' colours say it.
  if (r.correct !== true && r.choice !== null) {
    out.push(renderCheckFollow(followupHost(s, a), {
      kind: "card", cardId: item.card_id, cell, pageId: item.page_id, pageTitle: item.page_title, sid: item.section_id, idk: r.choice === -1, artifactId: item.artifact_id,
    }, true));
  }
  const next = nextButton(s, cell, rest, a);
  if (next !== null) out.push(next);
  return out;
}

function confirmBody(s: WikiState, c: ConfirmCardOut, a: WikiActions, rest: boolean): HTMLElement[] {
  const id = c.followup_id;
  const r = s.review.confirmResults[id];
  const closed = r?.closed === true;
  const picked = r === undefined || closed ? null : r.choice;
  const right = r !== undefined && !closed && r.out.correct === true;
  const fresh = right ? freshAt(s, confirmKey(id)) : null;
  const box = options(c.check.options, picked, r?.out.correct_index ?? null, writeBlocked(s) || closed, fresh, (i) => a.answerConfirm(id, i), "confirm-answer", { followup: id });
  if (r === undefined) {
    const why = blockedWhy(s);
    return why !== null ? [box, muted(why, "blocked-why")] : [box];
  }
  const out: HTMLElement[] = [box];
  if (right) out.push(okLine(fresh));
  if (closed && r.line !== null) out.push(muted(r.line.text, "review-error"));
  if (!closed && r.out.outcome === "refuted" && r.out.reteach !== null) {
    const row = el("div", "row");
    row.dataset.role = "reteach";
    row.append(act(btn(() => (WIKI_COPY.confirmUnclear), "", () => a.confirmUnclear(id)), "confirm-unclear", { followup: id }),
      act(btn(() => (WIKI_COPY.confirmReread), "", () => a.reteach(id)), "confirm-reread", { followup: id }));
    out.push(row);
  }
  const next = nextButton(s, confirmKey(id), rest, a);
  if (next !== null) out.push(next);
  return out;
}

/** A failed grade / answer (review.note with an error tone): one muted line in the card. */
const errorNote = (n: ResultNote | null): HTMLElement | null => (n !== null && n.tone === "error" ? muted(n.text, "review-error") : null);

/** Cards moved past or answered, as the bar counts them (app.js `done`). */
const doneCount = (s: WikiState, entries: readonly ReviewEntry[]): number =>
  entries.filter((e) => s.review.seen.includes(e.key) || (e.kind === "item" ? s.review.results[e.key] !== undefined : s.review.confirmResults[e.confirm.followup_id] !== undefined)).length;

/** app.js's `rvbar`: the fill slides from where it was drawn last (`review.barAt`) to the cards done now. */
function progressBar(s: WikiState, entries: readonly ReviewEntry[], a: WikiActions): HTMLDivElement {
  const bar = el("div", "rvbar");
  const fill = el("i");
  bar.append(fill);
  const pct = (k: number): string => `${(100 * k) / entries.length}%`;
  const done = doneCount(s, entries);
  const r = s.review;
  fill.style.width = pct(r.barAt ?? done);
  if (r.barAt !== done) {
    // Written through the action: by the next frame a newer review slot may have replaced `r`.
    window.requestAnimationFrame(() => { fill.style.width = pct(done); a.markBar(done); });
  }
  return bar;
}

const XI = "M6 6l12 12M18 6L6 18";
/** app.js `ib`: a small icon button (aria-label = the label; no title, so no hover tooltip). */
function iconButton(d: string, label: string, fn: () => void): HTMLButtonElement {
  const b = el("button", "ib");
  b.type = "button";
  b.append(svg("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" }, svg("path", { d })));
  b.setAttribute("aria-label", label);
  b.onclick = fn;
  return b;
}

/** The shown card with its controls, or null when no card is left. */
export function renderReviewCard(s: WikiState, a: WikiActions, ctx: RenderCtx): HTMLDivElement | null {
  const entry = currentEntry(s);
  if (entry === null || s.review.today.kind !== "ready") return null;
  const entries = reviewEntries(s);
  const rest = entries.some((e) => e.key !== entry.key && !s.review.seen.includes(e.key));
  const card = el("div", "card");
  card.dataset.role = "review";
  const top = el("div", "rvtop");
  top.append(progressBar(s, entries, a));
  const end = act(iconButton(XI, WIKI_COPY.reviewEnd, () => a.pauseReview()), "review-pause");

  if (entry.kind === "confirm") {
    const c = entry.confirm;
    top.append(end);
    card.append(top, el("div", "rv-q", c.check.stem), ...confirmBody(s, c, a, rest));
    const note = s.review.confirmResults[c.followup_id] === undefined ? errorNote(s.review.note) : null;
    if (note !== null) card.append(note);
    return card;
  }
  const item = entry.item;
  const question = item.mode === "check" ? item.check?.stem ?? "" : item.cue;
  const fb = act(btn(() => (WIKI_COPY.reviewProblem), "ghost", () => a.askAboutCard(item.cell)), "card-fb");
  top.append(fb, end);
  card.append(top, el("div", "rv-q", question));
  if (item.mode === "cue") card.append(el("div", "rv-src", `${item.page_title} · ${item.heading}`), ...cueBody(s, item, a, rest, ctx));
  else card.append(...checkBody(s, item, a, rest));
  const note = errorNote(s.review.note);
  if (note !== null) card.append(note);
  return card;
}
