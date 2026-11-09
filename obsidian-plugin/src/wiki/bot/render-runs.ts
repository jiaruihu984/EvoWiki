/**
 * One Bot activity row of 个性化 · 活动记录 (app.js `logItem` for a run): `div.ew-pz-it` of `div.ew-pz-t` (the title, a
 * `button.ew-pz-go` link when it has somewhere to go: the page it wrote, the page its card is about, 今天's review for a
 * review run), the "?" tooltip (qTip: the reason, then the execution details, then when · reaction; the chain is read on its
 * first ask) and, for a page write that can be taken back, a small 撤销. A write taken back is struck (`.ew-off`) with a
 * faint 已撤销. A running write has its live status line under the title (painted in place, paint-writes.ts).
 * B5's quiet weekly review shows its outcome line under the title.
 */
import { writeLine } from "../../assistant/card-state";
import { btn, el } from "../../ui/ew";
import type { WikiActions } from "../actions";
import { qTip } from "../q-tip";
import type { WikiState } from "../state";
import { RUNS_COPY as C } from "./bot-copy";
import type { ActivityOut, WhyOut } from "./bot-schema";
import { fillWline } from "./paint-writes";
import { runTipLines, writeTipLines } from "./why-text";
import { formatWhen } from "../../ui/dates";

/** Demo `when`: 今天 8:00 / 昨天 8:00 / 10月12日 8:00, local time (a naive ISO time is UTC, as the server writes it). */
export const whenText = (iso: string, now: Date = new Date()): string => formatWhen(iso, now, "stamp");

const titleOf = (a: ActivityOut): string => a.title !== "" ? a.title : a.suggestion_title ?? a.reason;

/** The data-wrun / data-wcost key of a page-write row. */
export const writeWhyKey = (run: ActivityOut): string => `w:${run.run_id ?? run.write?.page_id ?? ""}`;

/** The "?" tooltip of a run; the first ask reads the chain (hover, focus or click) so the details can show. */
function runTip(s: WikiState, a: WikiActions, key: string | null, lines: (why: WhyOut | null) => string[]): HTMLElement {
  const w = key === null ? undefined : s.bot.why[key];
  const why = w !== undefined && w.kind === "ready" ? w.value : null;
  let asked = false;   // once: botToggleWhy toggles
  const onOpen = key !== null && (w === undefined || w.kind === "idle") ? () => { if (!asked) { asked = true; a.botToggleWhy(key); } } : undefined;
  return qTip(lines(why), () => (C.why), onOpen);
}

/** Where a click on the run's title goes: the page it wrote, 今天's review, the page its card is about; null: plain text. */
function runTarget(run: ActivityOut, a: WikiActions): (() => void) | null {
  const w = run.write;
  if (w != null) return w.undone ? null : () => a.goToChange(w.page_id, null);
  if (run.kind === "review_due") return () => { a.setTab("today"); a.startReview(); };
  const pid = run.page_id;
  return pid != null && pid !== "" ? () => a.openWikiPage(pid) : null;
}

/** The title: a link when it goes somewhere (app.js `pz-go`), else plain text. */
function title(text: string, go: (() => void) | null): HTMLElement {
  if (go === null) return el("span", "", text);
  const b = el("button", "pz-go", text);
  b.type = "button";
  b.dataset.action = "run-go";
  b.onclick = () => go();
  return b;
}

export function runItem(s: WikiState, run: ActivityOut, a: WikiActions, key: string): HTMLDivElement {
  const it = el("div", "pz-it");
  it.dataset.chg = key;
  const t = el("div", "pz-t");
  it.append(t);
  const w = run.write;
  if (w == null) {
    t.append(title(titleOf(run), runTarget(run, a)));
    // the title is the whole line: a weekly review's outcome is its title, the details are behind the ?
    it.append(runTip(s, a, run.trigger_id, (why) => runTipLines(run, why, whenText(run.at))));
    return it;
  }
  const live = s.bot.writes[w.page_id];
  // The demo shows the running line only once the write has begun (a queued page has no row, no line).
  const running = live !== undefined ? live.state === "planning" || live.state === "writing" : run.result === "writing";
  const cost = running && live?.cost_usd !== undefined ? live.cost_usd : run.cost_usd;
  t.append(title(running ? C.writeRunning(w.title) : C.writeDone(w.title), runTarget(run, a)));
  if (running) {
    const l = el("div", "muted wline");
    l.dataset.wrun = w.page_id;
    fillWline(l, live);
    l.dataset.state = writeLine(live)?.key ?? "";
    t.append(l);
  }
  it.append(qTip(writeTipLines(run, cost, running, whenText(run.at)), () => (C.why)));
  if (w.undone) {
    t.classList.add("ew-off");
    it.append(el("span", "pz-tag", () => (C.writeUndonePill)));
  } else if (w.can_undo && w.action_id !== null && !running) {
    const id = w.action_id;
    const u = btn(() => (C.undo), "sm", () => a.botDecide(id, "undo"));
    u.dataset.action = "bot-write-undo";
    it.append(u);
  }
  return it;
}
