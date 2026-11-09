/**
 * The block at the end of a wiki note = app.js endBlock: `div.ew-endb` with h3 这页内容对你有帮助吗？, the three pills
 * (内容太简单 / 基本合适 / 理解有困难, aria-pressed, one choice — the controller's pickEndChip: 内容太简单 / 理解有困难 are sent
 * to the assistant), an hr, then the action row: 收为参考卡 (a `.ew-lk`, only while the server offers it: PageOut.can_shelve)
 * and the primary round 继续学习 → (pushed right). No form: what is wrong is said in the
 * assistant. Only a failure or a blocked write says anything in the block. Drawn by the plugin in Reading view and in
 * Live Preview (end-block-view.ts), never written to the note file. It builds its DOM once and `update()` only changes
 * what the state changed. The state lives in the controller, per page.
 */
import { btn, el } from "../../ui/ew";
import { WIKI_COPY } from "../copy";
import { FEELS, type Feel, type WikiState } from "../state";
import { reviewFirstRow, type ReviewFirstChoice } from "../review-first";
import type { ReviewFirstOut } from "../schema";
import { endModel } from "./model";
import { renderCheckBlock } from "../check-block";
import { isGoalCheck, isMasteredCheck } from "../goal-checks";
import { tr } from "../../i18n/index";

/** What the block calls: WikiController satisfies it. */
export interface EndSource {
  readonly state: WikiState;
  subscribe(fn: () => void): () => void;
  answerCheck(sid: string, choice: number): unknown;
  pickEndChip(pageId: string, feel: Feel): unknown;
  finishPage(pageId: string): unknown;
  answerReviewFirst(hint: ReviewFirstOut, choice: ReviewFirstChoice): unknown;
  undoPageFeedback(pageId: string): unknown;
  undoHandEdit(pageId: string): unknown;
  restoreNote(pageId: string, note: string): unknown;
  keepMine(pageId: string): unknown;
  /** 恢复成完整页 (the open page, collapsed pages only). */
  expand(): unknown;
  /** 收为参考卡 (the open page, when PageOut.can_shelve). */
  collapse(): unknown;
  /** A materials draft (page_revise row): 确认替换 / 取消 / 撤销; the note follows the page after approve and undo. */
  reviseDecide(actionId: string, op: "approve" | "reject" | "undo"): unknown;
}
/** The plugin's current wiki controller (null before the wiki is used, and for the demo) and its change notifications. */
export interface EndHub {
  readonly current: EndSource | null; subscribe(fn: () => void): () => void;
  /** Whether a note (its vault path) is the current user's (CheckBlockHub.owns); absent = every note. */
  owns?(notePath: string | null): boolean;
}

const CHIP_LABEL: Record<Feel, string> = { get shallow() { return WIKI_COPY.feelShallow; }, get right() { return WIKI_COPY.feelRight; }, get hard() { return WIKI_COPY.feelHard; } };

/** `el`: the `div.ew.ew-inline` host; it holds `div.ew-endb` while the page is the open, organized one, nothing otherwise. */
export interface EndBlock { el: HTMLElement; update(): void }

/** Builds the block for `pageId`; `update()` redraws it from the hub's controller. */
export function createEndBlock(pageId: string, hub: EndHub): EndBlock {
  const host = el("div");
  host.className = "ew ew-inline evowiki-end-host";
  host.dataset.page = pageId;
  const b = el("div", "endb");
  const chips = el("div", "chips");
  const pills = FEELS.map((f) => {
    const p = el("button", "pill", () => (CHIP_LABEL[f]));
    p.type = "button";
    p.setAttribute("aria-pressed", "false");
    p.onclick = () => { hub.current?.pickEndChip(pageId, f); };
    return p;
  });
  chips.append(...pills);
  const next = btn("", "primary round", () => { void hub.current?.finishPage(pageId); });
  next.style.marginLeft = "auto";
  const completed = el("span", "end-completed", "✓");
  completed.setAttribute("role", "status");
  const shelve = el("button", "lk", () => (WIKI_COPY.shelvePage));
  shelve.type = "button";
  shelve.onclick = () => { void hub.current?.collapse(); };
  const act = el("div", "act");
  act.append(next);
  // No hint line and no "saved" line (copy guide §3): this one is only for what went wrong or what could not be done.
  const saved = el("div", "hint");
  const checks = el("div", "goal-checks");
  const startChecks = btn(() => tr("wiki.goal.startChecks"), "primary start-goal-checks", () => { void hub.current?.finishPage(pageId); });
  saved.setAttribute("role", "status");
  let hintRow: HTMLElement | null = null;
  let hintFor = "";
  b.append(el("h3", "", () => (WIKI_COPY.endTitle)), chips, el("hr"), act);

  function update(): void {
    const src = hub.current;
    const m = src === null ? null : endModel(src.state, pageId);
    if (m === null) { host.replaceChildren(); return; }
    if (b.parentElement !== host) host.replaceChildren(b);
    pills.forEach((p, i) => { p.setAttribute("aria-pressed", String(m.chips.includes(FEELS[i]!))); });
    const hint = m.reviewFirst;
    if ((hint?.node_id ?? "") !== hintFor) {
      hintRow?.remove(); hintRow = null; hintFor = hint?.node_id ?? "";
      if (hint !== null) {
        hintRow = reviewFirstRow(hint, (choice) => { void hub.current?.answerReviewFirst(hint, choice); });
        b.insertBefore(hintRow, act);
      }
    }
    next.textContent = m.primary.label;
    next.dataset.kind = m.primary.kind;
    next.disabled = m.blocked !== null;
    if (m.completed) {
      next.replaceWith(completed);
      completed.setAttribute("aria-label", m.primary.label);
      completed.title = m.primary.label;
    } else if (next.parentElement !== act) {
      completed.replaceWith(next);
    }
    if (m.shelve === null) shelve.remove();
    else {
      shelve.disabled = m.shelve.busy;
      if (shelve.parentElement !== act) act.insertBefore(shelve, m.completed ? completed : next);
    }
    const line = m.phase.kind === "error" ? m.phase.text : m.blocked ?? (m.phase.kind === "saved" && !m.completed ? m.phase.text : null);
    saved.textContent = line ?? "";
    if (line !== null) b.append(saved); else saved.remove();
    const page = src!.state.page;
    const questions = page.kind === "open" && page.page.done ? page.page.sections.filter(isGoalCheck) : [];
    if (m.completed && m.primary.kind === "goal-done" && questions.length === 0) {
      startChecks.disabled = m.blocked !== null;
      b.append(startChecks);
    } else startChecks.remove();
    const visibleQuestions = questions.filter((section) => !isMasteredCheck(section) && !section.folded && !(page.kind === "open" && page.page.collapsed && !section.pinned));
    checks.replaceChildren(...visibleQuestions.map((section) => renderCheckBlock(
      `page: ${pageId}\nsection: ${section.id}`, src,
    )));
    if (visibleQuestions.length > 0) b.append(checks); else checks.remove();
  }
  update();
  return { el: host, update };
}

/** Puts the block into `parent` (before `before`) and keeps it current while `hub` changes. Returns the cleanup. */
export function mountEndBlock(parent: HTMLElement, pageId: string, hub: EndHub, before: Node | null = null): () => void {
  const block = createEndBlock(pageId, hub);
  parent.insertBefore(block.el, before);
  const unsub = hub.subscribe(() => { block.update(); });
  return () => { unsub(); block.el.remove(); };
}
