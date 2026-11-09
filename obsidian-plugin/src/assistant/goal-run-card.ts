import { labelNode } from "../i18n/index";
/**
 * The chat diagnosis under a goal confirmed in the chat (goal-run.ts): the quiz card's look (`.check.qz`, one question at a
 * time, options + 不知道), a 跳过 link that plans with the evidence so far, the working line (spinner) while it checks /
 * splits a block, then the page as the path's page card shows it (`.wcard`, writing-fill.ts): filling while it is written
 * (its title a link once the first section is in; the composer offers 停止 meanwhile), full once written. A failure shows
 * its line and 重试; a stopped write 已停止 · 重新写.
 */
import { btn, el } from "../ui/ew";
import { rowProgress } from "../wiki/learn/page-progress";
import { fillLayer } from "../wiki/learn/writing-fill";
import { GOAL_RUN_COPY as T, type GoalRun } from "./goal-run";
import { LEARN_COPY } from "../wiki/learn/copy";

export interface GoalRunHooks {
  pick: (actionId: string, option: number) => void;
  skip: (actionId: string) => void;
  retry: (actionId: string) => void;
  /** 重新写 on a stopped first page. */
  restart?: (actionId: string) => void;
  /** 停止 on the writing card. */
  stop?: (actionId: string) => void;
  showPage: (pageId: string) => void;
  /** 学点新的 named in a line opens this goal there. */
  showGoal?: (actionId: string) => void;
}

/** A line that names 学点新的: the name opens the goal there. */
function learnLine(text: string, go: (() => void) | undefined): HTMLElement {
  const line = el("div", "muted");
  line.append(text);
  if (go !== undefined) { const action = btn(() => LEARN_COPY.title, "link", go); action.dataset.action = "open-learn"; line.append(" · ", action); }
  return line;
}

export function buildGoalRunCard(run: GoalRun, h: GoalRunHooks): HTMLElement | null {
  const v = run.view();
  if (v.kind === "idle") return null;
  const c = el("div", "check qz goalrun");
  c.dataset.goalRun = run.actionId;
  if (v.kind === "working") {
    const w = el("div", "tools");
    w.append(el("i", "spin"), v.text);
    c.append(w);
    return c;
  }
  if (v.kind === "choose") {
    c.append(learnLine(T.choose(v.name), h.showGoal && (() => h.showGoal?.(run.actionId))));
    return c;
  }
  if (v.kind === "read") {
    c.append(learnLine(T.readNext(v.order, v.title), h.showGoal && (() => h.showGoal?.(run.actionId))));
    if (v.uses.length > 0) c.append(el("div", "muted", () => (T.readFirst(v.uses))));
    return c;
  }
  if (v.kind === "failed") {
    c.append(el("div", "muted", v.text), left(btn(() => (T.retry), "", () => h.retry(run.actionId))));
    return c;
  }
  if (v.kind === "stopped") {
    // the writing line, stopped (header.ts writeLine): 已停止 + 重新写
    const w = el("div", "tools");
    const b = btn(() => (T.restart), "xs", () => h.restart?.(run.actionId));
    b.dataset.action = "restart";
    w.append(labelNode(() => (T.stopped)), b);
    c.append(w);
    return c;
  }
  if (v.kind === "writing" || v.kind === "written") {
    c.className = "ew-goalrun";
    const writing = v.kind === "writing";
    const p = writing ? v.progress : null;
    const card = el("div", "wcard");
    card.dataset.state = writing ? "writing" : "written";
    const t = el("div", "t");
    // readable once its first section is written (the note opened with it), and once written
    const readable = v.pageId !== null && (!writing || (p !== null && p.section >= 1));
    if (readable) { const id = v.pageId!; t.append(btn(v.title, "link", () => h.showPage(id))); } else t.append(v.title);
    const s = el("div", "s");
    s.append(el("span", "", () => (writing ? T.writingLine(p) : v.kind === "written" && v.reused === true ? T.reused : T.written)));
    // 停止 at the end of that line while it is written (the note header's 生成中 · 停止), also for a page resumed after a reload
    if (writing && run.stoppable && h.stop) {
      const b = btn(() => (T.stop), "xs", () => h.stop?.(run.actionId));
      b.dataset.action = "stop";
      s.append(b);
    }
    card.append(fillLayer(writing ? "writing" : "written", writing ? (p?.of ? rowProgress({ state: "writing", progress: p }) : null) : 100), t, s);
    c.append(card);
    return c;
  }
  const q = v.round.questions[v.question]!;
  if (v.question === 0 && v.round.round <= 1) c.append(el("div", "h", () => (T.title)));
  c.append(el("div", "rv-src", () => (T.counter(v.question + 1, v.of))), el("div", "stem", q.stem));
  const o = el("div", "opts");
  const opts: [string, number][] = q.options.map((t, i) => [`${"ABCDEFGH"[i] ?? String(i + 1)}. ${t}`, i]);
  if (q.kind === "quiz") opts.push([T.idk, -1]);
  for (const [label, k] of opts) {
    const b = el("button", k === -1 ? "o idk" : "o", label);
    b.type = "button"; b.disabled = v.busy;
    b.onclick = () => h.pick(run.actionId, k);
    o.append(b);
  }
  c.append(o, left(btn(() => (T.skip), "link", () => h.skip(run.actionId))));
  return c;
}

function left(b: HTMLButtonElement): HTMLButtonElement { b.style.alignSelf = "flex-start"; return b; }
