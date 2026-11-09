import { labelNode } from "../../i18n/index";
import { tr, onLanguageChange } from "../../i18n/index";
/**
 * 今天's Bot task cards and the 今天的学习已完成 box (app.js `visibleTasks` / `taskTitle` / `taskWhy` / `taskCard` and the
 * `!cards.length` branch of `nextBlock`). The local review card comes first, then the Bot's today list in API order.
 */
import { btn, el } from "../../ui/ew";
import { qTip } from "../q-tip";
import { TODAY_COPY } from "../today-copy";
import type { WikiActions } from "../actions";
import { pendingEntries, reviewMinutes } from "../review-entries";
import type { WikiState } from "../state";
import { REVIEW_KEY } from "./bot-controller";
import { BOT_KIND } from "./bot-copy";
import type { SuggestionOut } from "./bot-schema";
import { orphanRevises, reviseProgress } from "./bot-state";

export interface TaskCardView {
  key: string; title: string;
  /** The right-aligned faint text in the head: the review card's 「N 张 · 约 M 分钟」, a goal card's 「第 k / n 页」 (null: none). */
  faint: string | null;
  /** The review card's topic line (point names; a 资料更新 card's page names); the others show their why only in the "?" tooltip. */
  topic: string | null;
  /** The card's why (the review card has none: its topic line says it). */
  why: string;
  /** The primary button's label (开始复习 / 开始补讲 / 继续学习 / 更新这 N 页, else 开始). */
  go: string;
  suggestion: SuggestionOut | null;
  /** 资料更新 after 更新这 N 页: the batch line (生成中 n/N, then N 份草稿待确认 + 去确认). */
  batch?: { done: number; total: number; waits: number };
}
/**
 * 资料更新 card text. The wire has the trigger's text only: title 「《X》讲到了 N 页已有的内容」, why 「…：页一、页二。要按它更新吗？」;
 * the demo's title is 「按《X》更新 N 页」 and its line is the page names. Anything unexpected is shown as it came.
 */
function reviseTitle(g: SuggestionOut): string {
  const m = /^《(.+?)》讲到了/.exec(g.title);
  return m !== null && g.action?.type === "revise_from_source" ? tr("wiki.bot.render-tasks..1", { v0: m[1]!, v1: g.action.page_ids.length }) : g.title;
}
function reviseWhy(g: SuggestionOut): string {
  const m = /：(.+?)。要按它更新吗？\s*$/.exec(g.why);
  return m !== null ? m[1]! : g.why;
}
/** The button of a 资料更新 card: 更新这 N 页. */
function startLabel(g: SuggestionOut): string {
  return g.action?.type === "revise_from_source" ? tr("wiki.bot.render-tasks..2", { v0: g.action.page_ids.length }) : tr("wiki.bot.render-tasks..3");
}

/** app.js `taskTitle`. */
export let REVIEW_CARD_TITLE = tr("wiki.bot.render-tasks.REVIEW_CARD_TITLE.4");
onLanguageChange(() => { REVIEW_CARD_TITLE = tr("wiki.bot.render-tasks.REVIEW_CARD_TITLE.4"); });
const GO_LABEL: Readonly<Record<string, string>> = { get review_due() { return tr("wiki.bot.render-tasks.GO_LABEL.review_due.5"); }, get wrong_answers() { return tr("wiki.bot.render-tasks.GO_LABEL.wrong_answers.6"); }, get open_question() { return tr("wiki.bot.render-tasks.GO_LABEL.open_question.7"); }, get goal_idle() { return tr("wiki.bot.render-tasks.GO_LABEL.goal_idle.8"); } };
/**
 * The review card's topic line (demo): only confirmation questions left after a finished round = 刚才答错的那道题,
 * else the names of the points the due cards are about (concept_name), joined with 、.
 */
function reviewTopic(s: WikiState): string {
  const pending = pendingEntries(s);
  if (s.review.roundDone && pending.every((e) => e.kind === "confirm")) return TODAY_COPY.reviewRecheck;
  const names = pending.map((e) => (e.kind === "item" ? e.item.concept_name : e.confirm.concept_name));
  return [...new Set(names)].join("、");
}
/** 「第 k / n 页」 of a goal card, from the path catalog (null when the card's path is not listed or has no next page). */
function goalProgress(s: WikiState, g: SuggestionOut): string | null {
  const act = g.action as SuggestionOut["action"] | undefined;
  if (act?.type !== "open_goal" || s.catalog.kind !== "ready") return null;
  const pid = act.path_id;
  const p = s.catalog.value.paths.find((x) => x.path_id === pid);
  return p === undefined || p.next_index === null ? null : TODAY_COPY.goalAt(p.next_index, p.pages);
}
/** Local review card (when cards are due and it is not cooling down), then bot.today minus review_due, hidden, 陪读建议 (chat) and kinds the demo has no label for. */
export function taskCards(s: WikiState): TaskCardView[] {
  const out: TaskCardView[] = [];
  const n = pendingEntries(s).length;
  const switchedOff = s.bot.settings.kind === "ready" && s.bot.settings.value.kinds.review_due === false;
  if (n > 0 && !s.bot.reviewHidden && !switchedOff) {
    out.push({ key: REVIEW_KEY, title: REVIEW_CARD_TITLE, faint: TODAY_COPY.reviewSize(n, reviewMinutes(n)), topic: reviewTopic(s), why: "", go: GO_LABEL.review_due!, suggestion: null });
  }
  if (s.bot.today.kind === "ready") {
    for (const g of s.bot.today.value) {
      if (g.kind === "review_due" || g.kind === "bot_failing" || g.kind === "chat" || s.bot.hidden.includes(g.suggestion_id)) continue;
      if (BOT_KIND[g.kind] === undefined) continue;
      if (g.action?.type === "revise_from_source") {
        if (s.bot.revise[g.suggestion_id] !== undefined) continue;   // shown below with its batch line
        out.push({ key: g.suggestion_id, title: reviseTitle(g), faint: null, topic: reviseWhy(g), why: g.why, go: startLabel(g), suggestion: g });
        continue;
      }
      out.push({ key: g.suggestion_id, title: g.title, faint: g.kind === "goal_idle" ? goalProgress(s, g) : null, topic: null, why: g.why, go: GO_LABEL[g.kind] ?? tr("wiki.bot.render-tasks.go.9"), suggestion: g });
    }
  }
  const acts = s.bot.actions.kind === "ready" ? s.bot.actions.value : [];
  for (const b of Object.values(s.bot.revise)) {
    const p = reviseProgress(b, acts);
    if (p.open) out.push({ key: b.suggestion.suggestion_id, title: reviseTitle(b.suggestion), faint: null, topic: reviseWhy(b.suggestion), why: b.suggestion.why, go: startLabel(b.suggestion), suggestion: b.suggestion, batch: { done: p.done, total: b.total, waits: p.waits } });
  }
  for (const o of orphanRevises(acts, s.bot.revise)) {
    const ready = o.revise?.state === "ready";
    out.push({ key: o.action_id, title: o.summary, faint: null, topic: null, why: "", go: "", suggestion: null,
      batch: { done: ready ? 1 : 0, total: 1, waits: ready ? 1 : 0 } });
  }
  return out;
}

/** app.js `taskCard`: `.ew-card.ew-task`; head = title + faint + "?" (the why); review: topic line; 开始… / 减少提醒 or 不感兴趣. */
export function renderTaskCard(v: TaskCardView, a: WikiActions, off: boolean): HTMLDivElement {
  const card = el("div", "card task");
  card.dataset.key = v.key;
  const head = el("div", "thead");
  head.append(el("h2", "", v.title));
  if (v.faint !== null) head.append(el("span", "faint", v.faint));
  if (v.key !== REVIEW_KEY) head.append(qTip(v.why, () => (TODAY_COPY.whyLabel)));
  card.append(head);
  if (v.topic !== null) card.append(el("div", "muted", v.topic));
  if (v.batch !== undefined) {
    const b = v.batch;
    const line = el("div", "muted wline");
    if (b.done < b.total) { line.append(el("i"), labelNode(() => (tr("wiki.bot.render-tasks..10", { v0: b.done, v1: b.total })))); card.append(line); return card; }
    line.append(labelNode(() => (tr("wiki.bot.render-tasks..11", { v0: b.waits }))));
    card.append(line);
    const go = el("div", "acts");
    const confirm = btn(() => (tr("wiki.bot.render-tasks.confirm.12")), "primary", () => a.botGoConfirm(v.key));
    confirm.dataset.action = "bot-confirm";
    go.append(confirm);
    card.append(go);
    return card;
  }
  const acts = el("div", "acts");
  const start = btn(v.go, "primary", () => a.botStart(v.key));
  start.dataset.action = "bot-start";
  start.disabled = off;
  const nope = btn(() => (v.key === REVIEW_KEY ? TODAY_COPY.reviewLess : TODAY_COPY.notInterested), "", () => a.botNotInterested(v.key));
  nope.dataset.action = "bot-nope";
  nope.disabled = off;
  acts.append(start, nope);
  card.append(acts);
  return card;
}

/** app.js `nextBlock` with no cards: a green box with a check that draws itself the first time (`allDoneSeen` false). */
export function renderAllDone(s: WikiState): HTMLDivElement {
  const d = el("div", "alldone" + (s.bot.allDoneSeen ? "" : " anim"));
  const ck = el("span", "adck");
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", "M6 12.5l4 4L18 8.5");
  svg.append(path);
  ck.append(svg);
  const t = el("div");
  t.append(el("b", "", () => (TODAY_COPY.allDone)));
  d.append(ck, t);
  return d;
}
