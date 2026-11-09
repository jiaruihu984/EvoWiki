/**
 * The three buttons under a wrong answer (粗心答错 / 现在会了 / 还没掌握), shared by the note's check block and the in-place
 * review card (not by EvoUI quizzes: they have no cell yet): one renderer, renderCheckFollow. The state lives in the wiki controller (by key), so every place
 * draws the same row. After 不知道 there is no 粗心答错. Server text (the page and section names, the concept) goes in as
 * text nodes; copy lives in copy.ts.
 */
import { btn, el } from "../ui/ew";
import { MAX_QUOTE_CHARS } from "../assistant/types";
import { WIKI_COPY } from "./copy";
import { FOLLOWUP_CHOICES, type FollowupChoice, type FollowupTarget } from "./schema";

/** Where one follow-up stands. `idle` is simply absent from the controller's map. */
export type FollowupView =
  | { kind: "idle"; error: string | null }
  | { kind: "sending"; choice: FollowupChoice }
  | { kind: "done"; choice: FollowupChoice; note: string };

/** What a row reads and calls: WikiController satisfies it. */
export interface FollowupHost {
  followupView(key: string): FollowupView;
  followup(target: FollowupTarget, choice: FollowupChoice): unknown;
  /** Why the buttons cannot be used now (offline / not set up / demo), or null. */
  followupBlockedWhy(): string | null;
}

export function followupKey(t: FollowupTarget): string {
  return t.kind === "check" ? `check:${t.pageId}:${t.sid}` : `card:${t.cardId}`;
}

/** The quote for the assistant, within the assistant's own limit. */
export const clip = (text: string): string => [...text].slice(0, MAX_QUOTE_CHARS).join("");

const LABEL: Record<FollowupChoice, string> = {
  get slip() { return WIKI_COPY.followupSlip; }, get learned() { return WIKI_COPY.followupLearned; }, get unclear() { return WIKI_COPY.followupUnclear; },
};

/**
 * app.js followups: `div.ew-follow` holding `div.ew-row` with the three `.ew-btn`s (no 粗心答错 after 不知道). Once a
 * choice is noted (user review) there is no confirmation sentence: the row stays, the chosen button pressed
 * (aria-pressed="true") and the other two disabled. 还没掌握 only hands the question to the assistant (user ruling 2026-10-21):
 * no 去复习 link, no 学这个 suggestion. A line the controller put on a noted row (already chosen, no assistant) is one
 * `.ew-muted` line.
 * Blocked / failed: one `.ew-muted` line under the row (fail closed, in the demo's language).
 * `marks`: the review card's data-* hooks (data-role / data-action / data-choice); the note's card draws none.
 */
export function renderCheckFollow(host: FollowupHost, target: FollowupTarget, marks = false): HTMLElement {
  const key = followupKey(target);
  const v = host.followupView(key);
  const mark = <T extends HTMLElement>(e: T, attr: "role" | "action", value: string): T => {
    if (marks) e.dataset[attr] = value;
    return e;
  };
  const muted = (text: string, role: string): HTMLElement => mark(el("div", "muted", text), "role", role);
  const f = mark(el("div", "follow"), "role", "followup");
  const why = v.kind === "done" ? null : host.followupBlockedWhy();
  const off = why !== null || v.kind === "sending";
  const row = el("div", "row");
  for (const c of FOLLOWUP_CHOICES.filter((x) => !(target.idk && x === "slip"))) {
    const b = mark(btn(() => (LABEL[c]), "", () => { if (host.followupView(key).kind === "idle") void host.followup(target, c); }), "action", "followup");
    if (marks) b.dataset.choice = c;
    if (v.kind === "done") {
      if (c === v.choice) b.setAttribute("aria-pressed", "true");
      else b.disabled = true;
    } else b.disabled = off;
    row.append(b);
  }
  f.append(row);
  if (v.kind === "done") {
    if (v.note !== "") f.append(muted(v.note, "followup-note"));
    return f;
  }
  if (why !== null) f.append(muted(why, "followup-why"));
  else if (v.kind === "idle" && v.error !== null) {
    const e = muted(v.error, "followup-error");
    e.setAttribute("role", "alert");
    f.append(e);
  }
  return f;
}
