import { labelNode } from "../i18n/index";
import { localizedAttribute, localizedText } from "../i18n/index";
/**
 * DOM for the reading-assistant panel = the demo's 助手 (full-demo/src/app.js `assistantView`): `.ew-asst` >
 * `.ew-ahead` (title + the clock and + icon buttons) then either `.ew-thread` (user turns `.ew-umsg`, answers `.ew-amsg`)
 * + `.ew-composer` (`.ew-cbox`: quote chip, textarea, `.ew-cbar` with the round 问 button) or the history view `.ew-hist`
 * (search box + `.ew-hlist` of `.ew-hgroup` headers and `.ew-hrow` buttons). Server strings are text nodes or go through hooks.renderMarkdown. Turn elements
 * are cached by id and rebuilt only when their rev changes, so a finished answer's Markdown is rendered once per rev.
 * The textarea is persistent and never overwrites what is being typed. The root needs an ancestor with class `ew`.
 */
import { bindCitationLabels, citationsToMarkdown, plainCitations } from "./citations";
import { btn, el } from "../ui/ew";
import { withSettingsLink } from "../ui/settings-link";
import { tr } from "../i18n";
import { openMenuAt } from "../ui/floating";
import { s } from "../ui/svg";
import type { AssistantState, Conversation, HistoryState } from "./controller";
import { canSend, expiresInDays, learning, type DraftAttachment, type TurnAttachment } from "./attachments";
import { buildActionCard, isNoteLine, paintWrites, type ActionCardHooks } from "./action-card";
import { CONFLICT_KIND, cardPhase, LEDGER_STALE } from "./card-state";
/** The read 处理待确认的记忆 makes between groups: no tool line (app.js cfNextMsg). */
const CONFLICTS_READ = "read_memory_conflicts";
import { GOAL_KIND } from "./goal-chat";
import { REVISE_COPY } from "../wiki/revise/copy";
import { ACTION_COPY, ASSISTANT_COPY as C, FORGET_COPY as F, attFacts, decidedFailedText, draftStatus, errorText, proposalErrorText, turnStatus, type AttTone } from "./copy";
import { buildQuizCard } from "./quiz-card";
import { buildGoalRunCard } from "./goal-run-card";
import { GOAL_RUN_COPY, type GoalRun } from "./goal-run";
import { groupOf, HISTORY_GROUPS, HISTORY_LABEL, rowSub, titleOf } from "./history";
import { usedLine, usedTip, type ForgetDialog } from "./forget";
import { parseWhen } from "../ui/dates";
import { type AskState, canRetry, fromConflicts, fromLearn, cardOpen, missWithheld, type ProposalCard, type Quote, quoteIsQuestion, type Rating, reviewTurnOf, type ToolLine, type Turn, waitingAsk } from "./turn";
import { changeView } from "./proposal-diff";
import { RatingRow } from "./rating-row";
import { buildCheckCard, buildRoughCard, CHECK_KIND } from "./diag-cards";
import { MAX_MESSAGE_CHARS, type ForgetMaterialOut, type ModelIn, type ProposalChange, type RoughMark, type SessionSummaryOut, type Source } from "./types";
import { EXT_KIND } from "../wiki/sources/sources-copy";
import { qTip } from "../wiki/q-tip";

/** Property signatures (not methods) so callers and tests may pass the functions around unbound. */
export interface AssistantActions {
  send: () => void; sendEdit: (text: string) => void; setDraft: (text: string) => void; removeQuote: () => void; retry: (turnId: string) => void;
  decide: (turnId: string, proposalId: string, d: "accept" | "reject") => void;
  undoProposal: (turnId: string, proposalId: string) => void; newSession: () => void; reload: () => void;
  toggleHistory: () => void; searchHistory: (q: string) => void; reloadHistory: () => void; resume: (sessionId: string) => void;
  /** A source chip: opens that wiki page (page_id) or that web page (http(s) url). */
  openSource: (src: Source) => void;
  /** A citation in an answer: the original at its PDF page; "gone": the material is deleted (it told him). */
  openCitation?: (sourceId: string, page: number) => Promise<"opened" | "gone" | "failed">;
  /** A cited material deleted or cleaned up by now. */
  citationGone?: (sourceId: string) => boolean;
  /** Files picked, dropped or pasted into the composer. */
  addFiles: (files: File[]) => void;
  /** Pasted text that would make the message too long for one message: it goes along as a text attachment instead. */
  addPastedText?: (text: string) => void;
  /** Text dropped from Obsidian's file explorer (a library file's link or path); the host reads the file. */
  dropLibrary: (text: string) => void;
  removeAttachment: (localId: string) => void;
  /** The action card's controls (asst.js actCard): 确认 / 取消 / 撤销, the draft's edits, a page title, 在个性化里看. */
  confirmAction: (actionId: string) => void; cancelAction: (actionId: string) => void; undoAction: (actionId: string) => void;
  editRuleText?: (actionId: string, text: string) => void;
  showProposal?: (turnId: string, proposalId: string) => void;
  /** 重试 on an in-page draft that no longer fits its page (written again for the page as it is now); absent: no 重试. */
  redraftAction?: (actionId: string) => void;
  moveDraft: (actionId: string, from: number, to: number) => void; removeDraft: (actionId: string, k: number) => void;
  addDraft: (actionId: string, title: string) => void;
  showPage: (pageId: string) => void; showInParams: (actionId: string) => void;
  /** 查看详情: the action's result (card-state.ts cardResult); absent: showInParams. */
  showResult?: (actionId: string) => void;
  /** 草稿在页面里 ›: the page in review mode at its first mark (absent: showPage). */
  showRevise?: (pageId: string) => void;
  /** The quiz card: an option (-1 = 不知道), 下一题 / 看结果. */
  answerQuiz: (quizId: string, choice: number) => void; nextQuiz: (quizId: string) => void;
  /** 「帮我复习」's card (absent, or null: not drawn). */
  reviewCard?: () => HTMLElement | null;
  /** The review round is running now. */
  reviewLive?: () => boolean;
  /** One of the options under the question the running turn waits on (ask_user), sent as his answer. Absent: no buttons. */
  answerAsk?: (callId: string, text: string) => void;
  /** The 摸底 grid (ask_rough): a tap, and 提交 (his line + its marks as reply_data). Absent: the grid is read-only. */
  markRough?: (callId: string, block: string, v: RoughMark) => void;
  submitRough?: (callId: string) => void;
  /** The chat diagnosis under a goal confirmed in the chat (goal-run.ts): its run, an option, 跳过, 重试. */
  goalRun?: (actionId: string) => GoalRun | undefined;
  pickGoal?: (actionId: string, option: number) => void; skipGoal?: (actionId: string) => void; retryGoal?: (actionId: string) => void;
  /** 重新写 and 停止 on the writing card of a first page (goal-run.ts). */
  restartGoal?: (actionId: string) => void; stopGoal?: (actionId: string) => void;
  /** The send slot while the answer runs: stops the answer. */
  stopTurn?: () => void;
  /** The rating row's 👍 / 👎 / reason chip / note: the answer's next rating. */
  rate: (turnId: string, next: Rating) => void;
  /** The composer's model picker: its menu open / closed (no argument: toggle), a model picked. Absent: no picker. */
  toggleModels?: (open?: boolean) => void;
  openSettings?: () => void;
  pickModel?: (pick: ModelIn) => void;
  /** 删除对话: a history row's ⋯ → 删除 opens the dialog; a checkbox; 取消; 删除对话. Absent: rows have no ⋯. */
  openForget?: (sessionId: string) => void;
  pickForget?: (kind: "rule" | "fact" | "source", id: string, on: boolean) => void;
  closeForget?: () => void; confirmForget?: () => void;
}
/** The Bot-settings fields the chips need (structural: the assistant package takes no wiki code, only the chip type). */
export interface ChipSettings { kinds: Partial<Record<string, boolean>>; page_cap: number }

/** renderMarkdown replaces el's children with the rendered Markdown (possibly later: el keeps its placeholder until then). */
export interface RenderHooks {
  renderMarkdown(el: HTMLElement, md: string): void | Promise<void>; conceptName(id: string): string;
  /** The clock for the history list's times and groups (tests inject one). */
  now?: () => Date;
}
/** The search box waits this long after the last keystroke before asking the server. */
export const SEARCH_DEBOUNCE_MS = 200;

/** The transcript follows new content while the reader is within this many px of its bottom. */
const FOLLOW_PX = 32;
/** A streaming answer's Markdown is rendered again at most this often. */
const STREAM_RENDER_MS = 120;
/** A part of an answer still streaming: its element, the text so far and what was last rendered. */
interface LiveAnswer { body: HTMLElement; text: string; rendered: string; at: number; busy: boolean; timer: ReturnType<typeof setTimeout> | undefined }
/** app.js grow(): the textarea grows with its text up to this height. */
const GROW_MAX_PX = 160;
const HTTP_URL = /^https?:\/\//i;
const codePoints = (s: string): number => [...s].length;
/** Per-renderer input ids, unique even if two panels are open. */
let inputSeq = 0;

/** What the file picker offers: the kinds the backend reads (the same extensions as the sources page), plus any image. */
const ACCEPT = [...Object.keys(EXT_KIND).map((e) => `.${e}`), "image/*"].join(",");

/** The demo's CLIP_ICON (asst.js). */
const clipIcon = (): SVGElement => s("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" },
  s("path", { d: "M20 11.5l-8 8a5 5 0 01-7-7l8.5-8.5a3.3 3.3 0 014.7 4.7L9.5 17.2a1.7 1.7 0 01-2.4-2.4L15 7" }));

/** asst.js CHEV_ICON (the picker's ▾) and TICK_ICON (the current model). */
const chevIcon = (): SVGElement => s("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" }, s("path", { d: "M7 10l5 5 5-5" }));
const tickIcon = (): SVGElement => s("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" }, s("path", { d: "M5 12.5l4.5 4.5L19 7.5" }));

/** app.js HIST_ICON (a clock) and NEW_ICON (a plus). */
const histIcon = (): SVGElement => s("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" },
  s("circle", { cx: 12, cy: 12, r: 8.5 }), s("path", { d: "M12 7.5V12l3 2" }));
const newIcon = (): SVGElement => s("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" },
  s("path", { d: "M12 5v14M5 12h14" }));

/** asst.js TRASH_ICON (the dialog's title) and UP_ICON (a section's fold chevron). */
const trashIcon = (): SVGElement => s("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" },
  s("path", { d: "M4 7h16" }), s("path", { d: "M9 7V4.5h6V7" }), s("path", { d: "M6 7l1 13h10l1-13" }), s("path", { d: "M10 11v5.5M14 11v5.5" }));
const upIcon = (): SVGElement => s("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" }, s("path", { d: "M6 15l6-6 6 6" }));

/** app.js SEND_ICON (an up arrow), built as an SVG node (no markup is parsed). */
const sendIcon = (): SVGElement => s("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" },
  s("path", { d: "M12 19V5M5.5 11.5L12 5l6.5 6.5" }));

/** The send slot while the answer runs: Codex's composer stop, a rounded square in a 20 box (asst.js STOP_ICON). */
const stopIcon = (): SVGElement => s("svg", { class: "ew-stopi", viewBox: "0 0 20 20", "aria-hidden": "true" },
  s("rect", { x: "4.5", y: "4.5", width: "11", height: "11", rx: "1.25" }));

interface CachedTurn { key: string; els: HTMLElement[]; run?: { home: Element; box: HTMLElement } }

/** Whole seconds since `since` (Date.now() ms); never negative. */

export class AssistantRenderer {
  readonly root: HTMLElement;
  private readonly thread = el("div", "thread");
  private readonly status = el("div", "muted");
  private readonly summary = el("div", "muted");
  private readonly composer = el("div", "composer");
  private readonly box = el("div", "cbox");
  private readonly textarea: HTMLTextAreaElement;
  private readonly over = el("div", "muted");
  private readonly sendBtn: HTMLButtonElement;
  /** The send slot is the answer's stop (AssistantState.canStop). */
  private stopSlot = false;
  private readonly clipBtn: HTMLButtonElement;
  private readonly fileInput: HTMLInputElement;
  private readonly wait = el("span", "cwait");
  /** asst.js modelPicker: the current model's name + ▾, and its menu while open. Rebuilt when its key changes. */
  private readonly mpick = el("span", "mpickw");
  private mpickKey: string | null = null;
  private modelProvider: string | null = null;
  /** Stops placing the open model menu (floating-ui); `mpickFocus`: the next open came from the button, so its first item gets the focus. */
  private mpickStop: (() => void) | null = null;
  /** The history list as last drawn (redrawn only when it changes, so an open ⋯ menu survives other updates). */
  private histKey = "";
  /** A history row's open ⋯ menu. */
  private rowMenu: { sid: string; more: HTMLElement; menu: HTMLElement; stop: () => void; off: (e: Event) => void } | null = null;
  /** 删除对话's dialog on document.body (only the panel on screen draws it), and what it was drawn from. */
  private forgetEl: HTMLElement | null = null;
  private forgetKey = "";
  private forgetYes: HTMLButtonElement | null = null;
  private forgetEsc: ((e: KeyboardEvent) => void) | null = null;
  private mpickFocus = false;
  private readonly closeModels = (): void => this.actions.toggleModels?.(false);
  private readonly escModels = (e: KeyboardEvent): void => { if (e.key === "Escape") this.actions.toggleModels?.(false); };
  private readonly atts = el("div", "atts");
  private attsKey: string | null = null;
  /** The proposal cards whose 看改动 is open. */
  private readonly openDiffs = new Set<string>();
  private chip: HTMLElement | null = null;
  private readonly cache = new Map<string, CachedTurn>();
  /** The turn that draws 「帮我复习」's card. */
  private reviewTurn: string | null = null;
  private reviewTick = 0;
  /** The tool rows opened by a click (`turn id|first call id`). */
  private readonly openRows = new Set<string>();
  private lastConv: Conversation | null = null;
  /** Each finished answer's rating row, kept across rebuilds of its turn (repainted in place, never rebuilt). */
  private readonly rows = new Map<string, RatingRow>();
  private readonly liveAnswers = new Map<string, LiveAnswer>();
  private summaryText: string | null = null;
  private statusKey: string | null = null;
  private chipKey: string | null = null;
  private convKey: string | null = null;
  private focusTick = 0;
  /** After a draft edit redraws the card: which control gets the focus back (a row, or the 加一页 box). */
  private focusReq: { actionId: string; k: number | "add" | "rule"; start?: number; end?: number } | null = null;
  /** The card whose 加一页 box was just emptied by an add (its redraw must not bring the text back). */
  private added: string | null = null;
  private readonly cardHooks: ActionCardHooks = {
    confirm: (id) => this.actions.confirmAction(id), cancel: (id) => this.actions.cancelAction(id), undo: (id) => this.actions.undoAction(id),
    editRuleText: (id, text) => this.actions.editRuleText?.(id, text),
    move: (id, from, to, refocus) => { if (refocus) this.focusReq = { actionId: id, k: to }; this.actions.moveDraft(id, from, to); },
    remove: (id, k) => this.actions.removeDraft(id, k),
    add: (id, title) => { this.focusReq = { actionId: id, k: "add" }; this.added = id; this.actions.addDraft(id, title); this.added = null; },
    showPage: (pid) => this.actions.showPage(pid), showInParams: (id) => this.actions.showInParams(id),
    showResult: (id) => (this.actions.showResult ?? this.actions.showInParams)(id),
    showRevise: (pid) => (this.actions.showRevise ?? this.actions.showPage)(pid),
    ...(this.actions.redraftAction ? { redraft: (id: string) => this.actions.redraftAction!(id) } : {}),
  };
  /** The page the assistant is on (the active note's), for the materials draft card. */
  private activePage: string | null = null;
  private readonly head = el("div", "ahead");
  private readonly titleEl = el("div", "atitle");
  private readonly histBtn: HTMLButtonElement;
  private readonly hist = el("div", "hist");
  private readonly search: HTMLInputElement;
  private readonly hlist = el("div", "hlist");
  private searchTimer: ReturnType<typeof setTimeout> | null = null;
  private seenQ = "";
  private wasOpen = false;

  constructor(private readonly actions: AssistantActions, private readonly hooks: RenderHooks) {
    const ta = el("textarea");
    ta.id = `ew-asst-input-${++inputSeq}`;
    ta.rows = 1;
    localizedAttribute(ta, "placeholder", () => (C.placeholder));
    ta.oninput = () => { this.grow(); this.actions.setDraft(ta.value); };
    ta.onkeydown = (e) => this.onKey(e);
    ta.onpaste = (e) => this.onPaste(e);
    this.textarea = ta;
    this.over.hidden = true;
    const hb = el("button", "iconbtn");
    hb.type = "button"; localizedAttribute(hb, "aria-label", () => (C.historyTitle));
    hb.setAttribute("aria-pressed", "false");
    hb.append(histIcon());
    hb.onclick = () => this.actions.toggleHistory();
    this.histBtn = hb;
    const nb = el("button", "iconbtn");
    nb.type = "button"; localizedAttribute(nb, "aria-label", () => (C.newSession));
    nb.append(newIcon());
    nb.onclick = () => this.actions.newSession();
    this.head.append(this.titleEl, hb, nb);
    const sq = el("input");
    sq.type = "text"; sq.id = `${ta.id}-hist-q`; localizedAttribute(sq, "placeholder", () => (C.searchHistory));
    sq.oninput = () => {
      if (this.searchTimer !== null) clearTimeout(this.searchTimer);
      this.searchTimer = setTimeout(() => { this.searchTimer = null; this.actions.searchHistory(sq.value); }, SEARCH_DEBOUNCE_MS);
    };
    this.search = sq;
    this.hist.append(sq, this.hlist);
    const go = el("button", "send");
    go.type = "button"; localizedAttribute(go, "aria-label", () => (C.send));
    go.append(sendIcon());
    go.onclick = () => { if (this.stopSlot) this.actions.stopTurn?.(); else this.actions.send(); };
    this.sendBtn = go;
    // The empty left slot of the bar: 📎 (and, while a file is being read, why send is off).
    const input = el("input");
    input.type = "file"; input.multiple = true; input.accept = ACCEPT; input.hidden = true;
    input.onchange = () => { const files = [...(input.files ?? [])]; input.value = ""; if (files.length > 0) this.actions.addFiles(files); };
    this.fileInput = input;
    const clip = el("button", "clip");
    clip.type = "button"; localizedAttribute(clip, "aria-label", () => (C.attachTitle));
    clip.append(clipIcon());
    clip.onclick = () => input.click();
    this.clipBtn = clip;
    this.wait.hidden = true;
    const left = el("span", "cleft");
    this.mpick.hidden = true;
    left.append(clip, this.mpick, this.wait, input);
    const cbar = el("div", "cbar");
    cbar.append(left, go);
    this.atts.hidden = true;
    this.box.append(this.atts, ta, this.over, cbar);
    this.composer.append(this.box);
    this.root = el("div", "asst");
    this.root.append(this.head, this.thread, this.composer);
    this.dropTarget(this.root);
  }

  update(s: AssistantState): void {
    const c = s.conv;
    this.activePage = s.active.kind === "page" ? s.active.pageId : null;
    this.renderHead(s, c);
    this.renderStatus(s, c);
    this.renderSummary(c);
    this.lastConv = c;
    this.reviewTick = s.reviewTick ?? 0;
    this.renderThread(c);
    paintWrites(this.thread, s.writes, s.written);
    this.applyFocus();
    this.renderComposer(s, c);
    this.renderForget(s);
  }
  languageChanged(): void {
    this.mpickKey = null; this.histKey = ""; this.statusKey = null; this.chipKey = null;
  }

  /** A redrawn turn keeps what was typed in a draft's 加一页 box (and its focus). */
  private keepAddBoxes(old: HTMLElement[], fresh: HTMLElement[]): void {
    const rules = (els: HTMLElement[]): Map<string, HTMLTextAreaElement> => new Map(els.flatMap((e) => [...e.querySelectorAll<HTMLTextAreaElement>(".ew-rule-draft")])
      .map((input) => [input.closest<HTMLElement>("[data-action]")?.dataset.action ?? "", input]));
    const previous = rules(old);
    for (const [id, now] of rules(fresh)) {
      const before = previous.get(id);
      if (!before || now.disabled) continue;
      before.disabled = now.disabled;
      if (before.ownerDocument.activeElement === before) this.focusReq = { actionId: id, k: "rule", start: before.selectionStart, end: before.selectionEnd };
      if (before.value !== now.value) before.value = now.value;
      before.oninput = now.oninput;
      now.replaceWith(before);
    }
    const box = (els: HTMLElement[]): Map<string, HTMLInputElement> => new Map(els.flatMap((e) => [...e.querySelectorAll<HTMLElement>(".ew-propcard[data-action]")])
      .flatMap((c) => { const i = c.querySelector<HTMLInputElement>(".ew-aadd input"); return i ? [[c.dataset.action ?? "", i] as const] : []; }));
    const was = box(old);
    for (const [id, now] of box(fresh)) {
      const before = was.get(id);
      if (!before || before.value === "" || id === this.added) continue;
      now.value = before.value;
      if (before.ownerDocument.activeElement === before && this.focusReq === null) this.focusReq = { actionId: id, k: "add" };
    }
  }

  /** Gives the focus back to the control a draft edit was made with (the redraw replaced it). */
  private applyFocus(): void {
    const req = this.focusReq;
    if (req === null) return;
    this.focusReq = null;
    const card = [...this.thread.querySelectorAll<HTMLElement>(".ew-propcard[data-action]")].find((x) => x.dataset.action === req.actionId);
    const target = req.k === "add" ? card?.querySelector<HTMLElement>(".ew-aadd input")
      : req.k === "rule" ? card?.querySelector<HTMLTextAreaElement>(".ew-rule-draft")
      : card?.querySelector<HTMLElement>(`.ew-alist.ew-drag li[data-k="${req.k}"]`);
    target?.focus();
    if (req.k === "rule" && target instanceof HTMLTextAreaElement) target.setSelectionRange(req.start ?? 0, req.end ?? 0);
  }

  /** Header (title, clock toggle) and which body shows: the conversation or the history list. */
  private renderHead(s: AssistantState, c: Conversation | null): void {
    const h = s.history;
    const open = h.open && s.configured;
    const first = c?.turns[0];
    localizedText(this.titleEl, () => (open ? C.historyTitle : (first ? titleOf(first.question) : "") || C.newTitle));
    this.histBtn.setAttribute("aria-pressed", String(open));
    this.histBtn.disabled = !s.configured;
    const body: HTMLElement[] = open ? [this.hist] : [this.thread, this.composer];
    const cur = [...this.root.children].slice(1);
    if (cur.length !== body.length || cur.some((e, i) => e !== body[i])) this.root.replaceChildren(this.head, ...body);
    if (open) this.renderHistory(s, h);
    if (open && !this.wasOpen && visible(this.search)) this.search.focus();
    this.wasOpen = open;
  }

  /** app.js historyView: rows grouped 今天 / 昨天 / 更早, or one empty / loading / error line (never an empty list on failure). */
  private renderHistory(s: AssistantState, h: HistoryState): void {
    // The box only follows the controller when the query changed from outside (a resume clears it), never while typing.
    if (h.q !== this.seenQ) { this.seenQ = h.q; if (this.search.value !== h.q) this.search.value = h.q; }
    const now = this.hooks.now?.() ?? new Date();
    const cur = s.conv?.sessionId ?? null;
    // 「删除中」 rows only on the whole list (the server's search does not know them)
    const deleting = h.q.trim() ? [] : h.deleting;
    const key = JSON.stringify([h.q, h.notice, h.status, h.error, h.items, deleting, cur, now.toDateString()]);
    if (key === this.histKey) return;
    this.histKey = key;
    this.closeRowMenu(false);
    const out: HTMLElement[] = [];
    if (h.notice) out.push(el("div", "hempty", h.notice));
    if (h.status === "loading") out.push(el("div", "hempty", () => (C.historyLoading)));
    else if (h.status === "error") {
      const e = el("div", "hempty", () => (C.historyFailed));
      e.append(btn(() => (C.retry), "link", () => this.actions.reloadHistory()));
      out.push(e);
    } else if (h.items.length === 0 && deleting.length === 0) out.push(el("div", "hempty", () => (h.q.trim() ? C.historyNoHits : C.historyEmpty)));
    else {
      type Entry = { at: string; row?: SessionSummaryOut; del?: HistoryState["deleting"][number] };
      const time = (x: Entry): number => parseWhen(x.at)?.getTime() ?? 0;
      const entries: Entry[] = [...h.items.map((r) => ({ at: r.last_activity_at, row: r })), ...deleting.map((d) => ({ at: d.at, del: d }))]
        .sort((a, b) => time(b) - time(a));
      for (const g of HISTORY_GROUPS) {
        const rows = entries.filter((x) => groupOf(x.at, now) === g);
        if (rows.length === 0) continue;
        out.push(el("div", "hgroup", () => HISTORY_LABEL[g]));
        for (const x of rows) out.push(x.row ? this.historyRow(x.row, x.row.session_id === cur, now) : this.deletingRow(x.del!));
      }
    }
    this.hlist.replaceChildren(...out);
  }

  /** asst.js historyView's row: `div.hitem` > the row button and its ⋯ (删除). */
  private historyRow(r: SessionSummaryOut, on: boolean, now: Date): HTMLElement {
    const it = el("div", "hitem");
    const b = el("button", `hrow${on ? " on" : ""}`);
    b.type = "button";
    b.append(el("span", "ht", () => (r.title || C.newTitle)), el("span", "hs", rowSub(r, now)));
    b.onclick = () => this.actions.resume(r.session_id);
    it.append(b);
    if (!this.actions.openForget) return it;
    const more = el("button", "more", "⋯");
    more.type = "button";
    localizedAttribute(more, "aria-label", () => (F.more)); more.setAttribute("aria-haspopup", "menu"); more.setAttribute("aria-expanded", "false");
    more.onclick = (e) => { e.stopPropagation(); this.toggleRowMenu(r.session_id, more); };
    it.append(more);
    return it;
  }

  /** A conversation being deleted: greyed, 「删除中」 (or 「删除没完成，会自动重试」 after a failed try), never opened. */
  private deletingRow(d: HistoryState["deleting"][number]): HTMLElement {
    const it = el("div", "hitem");
    const b = el("button", "hrow del");
    b.type = "button"; b.disabled = true;
    const st = el("span", "hs");
    if (d.state === "failed") st.append(labelNode(() => (F.retrying)));
    else { const w = el("span", "ws"); w.append(el("i"), labelNode(() => (F.deleting))); st.append(w); }
    b.append(el("span", "ht", () => (d.title || F.untitled)), st);
    it.append(b);
    return it;
  }

  /** asst.js convMenu: one item, 删除, under the ⋯ (ui/floating.ts openMenuAt; Esc / Tab back on the ⋯, a click elsewhere closes). */
  private toggleRowMenu(sid: string, more: HTMLElement): void {
    const was = this.rowMenu?.sid === sid;
    this.closeRowMenu(false);
    if (was) return;
    more.setAttribute("aria-expanded", "true");
    const menu = el("div", "menu");
    menu.setAttribute("role", "menu");
    menu.onclick = (e) => e.stopPropagation();
    const item = el("button", "", () => (F.menu));
    item.type = "button"; item.setAttribute("role", "menuitem");
    item.onclick = () => { this.closeRowMenu(false); this.actions.openForget?.(sid); };
    menu.append(item);
    more.parentElement?.append(menu);
    const stop = openMenuAt(more, menu, (refocus) => this.closeRowMenu(refocus), true, { placement: "bottom-start", gap: 4, overhang: (w) => w - 190 });
    const off = (): void => this.closeRowMenu(false);
    this.rowMenu = { sid, more, menu, stop, off };
    more.ownerDocument.addEventListener("click", off);
  }

  private closeRowMenu(refocus: boolean): void {
    const m = this.rowMenu;
    if (!m) return;
    this.rowMenu = null;
    m.stop(); m.menu.remove();
    m.more.setAttribute("aria-expanded", "false");
    m.more.ownerDocument.removeEventListener("click", m.off);
    if (refocus) m.more.focus();
  }

  /**
   * asst.js forgetDialog (the user's mockup of 2026-10-28) on document.body: the trash title, the conversation's title, what
   * stays; 「同时删除已保存的学习偏好与背景」 (rules as 学习偏好, profile lines as 学习背景) and 「同时删除这段对话用过的资料」
   * (a material used elsewhere says where, the names behind ?), each folding, nothing ticked; 取消 and the red 删除对话
   * (waits for the read and for its own answer). Esc and a click outside are 取消. Only the panel on screen draws it.
   */
  private renderForget(s: AssistantState): void {
    const d = s.forget ?? null;
    const show = d !== null && visible(this.root);
    const key = show ? JSON.stringify([d.sessionId, d.title, d.preview]) : "";
    if (key !== this.forgetKey) {
      this.forgetKey = key;
      this.forgetEl?.remove(); this.forgetEl = null; this.forgetYes = null;
      if (this.forgetEsc) { this.root.ownerDocument.removeEventListener("keydown", this.forgetEsc, true); this.forgetEsc = null; }
      if (show) this.buildForget(d, s.history.items);
    }
    if (show && this.forgetEl) {
      this.forgetYes!.disabled = d.preview === null || d.busy;
      this.forgetEl.querySelectorAll<HTMLInputElement>("input[type=checkbox]").forEach((x) => { x.disabled = d.busy; });
    }
  }

  private buildForget(d: ForgetDialog, listed: readonly SessionSummaryOut[]): void {
    const doc = this.root.ownerDocument;
    const close = (): void => this.actions.closeForget?.();
    const layer = el("div", "layer");
    layer.className = "ew ew-layer";
    const scrim = el("div", "scrim");
    scrim.onclick = (e) => { if (e.target === scrim) close(); };
    const m = el("div", "modal fg");
    m.setAttribute("role", "dialog"); m.setAttribute("aria-modal", "true"); localizedAttribute(m, "aria-label", () => (F.title));
    const head = el("div", "fg-head"), ic = el("span", "fg-ic");
    ic.append(trashIcon());
    head.append(ic, el("h2", "", () => (F.title)));
    const desc = el("div", "fg-desc");
    desc.append(labelNode(() => (F.desc)), el("br"), labelNode(() => (F.keep)), el("b", "", () => (F.keepBold)));
    m.append(head, el("div", "fg-title", () => (d.title || F.untitled)), desc);
    const p = d.preview;
    const row = (text: string, kind: string | HTMLElement, pick: "rule" | "fact" | "source", id: string, on: boolean): HTMLElement => {
      const l = el("label", "fg-item"), cb = el("input");
      cb.type = "checkbox"; cb.checked = on;
      cb.onchange = () => this.actions.pickForget?.(pick, id, cb.checked);
      const t = el("div");
      t.append(el("div", "fg-it", text), typeof kind === "string" ? el("div", "fg-ik", kind) : kind);
      l.append(cb, t);
      return l;
    };
    const section = (title: string, rows: HTMLElement[]): void => {
      const sh = el("button", "fg-sh");
      sh.type = "button"; sh.setAttribute("aria-expanded", "true");
      const t = el("div");
      t.append(el("div", "fg-st", title), el("div", "fg-ss", () => (F.sectionSub)));
      const ch = el("span", "fg-ch");
      ch.append(upIcon());
      sh.append(t, ch);
      const box = el("div", "fg-box");
      box.append(...rows);
      sh.onclick = () => { const open = sh.getAttribute("aria-expanded") !== "true"; sh.setAttribute("aria-expanded", String(open)); box.hidden = !open; };
      const sec = el("div", "fg-sec");
      sec.append(sh, box);
      m.append(el("hr", "fg-hr"), sec);
    };
    if (p && (p.rules.length > 0 || p.facts.length > 0)) {
      section(F.prefs, [
        ...p.rules.map((r) => row(r.text, F.rule, "rule", r.item_id, d.picks.revokeRuleIds.includes(r.item_id))),
        ...p.facts.map((f) => row(f.text, F.fact, "fact", f.key, d.picks.revokeFactKeys.includes(f.key))),
      ]);
    }
    if (p && p.materials.length > 0) {
      const title = (id: string): string | null => listed.find((x) => x.session_id === id)?.title || null;
      section(F.materials, p.materials.map((x: ForgetMaterialOut) => {
        const on = d.picks.deleteSourceIds.includes(x.source_id);
        if (!x.in_use_elsewhere) return row(x.title, F.material, "source", x.source_id, on);
        const k = el("div", "fg-ik fg-used");
        k.append(labelNode(() => (F.usedElsewhere(usedLine(x.used_by)))), qTip(usedTip(x, title), () => (F.usedTipLabel)));
        return row(x.title, k, "source", x.source_id, on);
      }));
    }
    const ops = el("div", "fg-ops");
    const yes = btn(() => (F.confirm), "fg-yes", () => this.actions.confirmForget?.());
    ops.append(btn(() => (F.cancel), "fg-no", close), yes);
    m.append(ops);
    scrim.append(m);
    layer.append(scrim);
    doc.body.append(layer);
    this.forgetEl = layer; this.forgetYes = yes;
    this.forgetEsc = (e: KeyboardEvent) => { if (e.key === "Escape" && !e.defaultPrevented && !layer.querySelector(".ew-qtip.ew-open")) { e.stopPropagation(); close(); } };
    doc.addEventListener("keydown", this.forgetEsc, true);
  }

  /** app.js grow(): height follows the text, at most 160 px. */
  private grow(): void {
    const ta = this.textarea;
    ta.style.height = "auto";
    ta.style.height = `${Math.min(ta.scrollHeight, GROW_MAX_PX)}px`;
  }

  private onKey(e: KeyboardEvent): void {
    if (e.key !== "Enter" || e.shiftKey || e.isComposing) return;
    e.preventDefault();
    if (!this.sendBtn.disabled) this.actions.send();
  }

  /**
   * Pasted files / images (a screenshot) become attachments. Pasted text is left to the textarea, unless the message
   * would then be over MAX_MESSAGE_CHARS: then it goes along as a text attachment (never refused, never cut).
   */
  private onPaste(e: ClipboardEvent): void {
    if (this.clipBtn.hidden) return;
    const files = [...(e.clipboardData?.files ?? [])];
    if (files.length > 0) {
      e.preventDefault();
      this.actions.addFiles(files);
      return;
    }
    const text = e.clipboardData?.getData?.("text/plain") ?? "";
    const ta = this.textarea;
    const kept = ta.value.length - Math.max(0, (ta.selectionEnd ?? 0) - (ta.selectionStart ?? 0));
    if (!this.actions.addPastedText || text.trim() === "" || kept + [...text].length <= MAX_MESSAGE_CHARS) return;
    e.preventDefault();
    this.actions.addPastedText(text);
  }

  /**
   * Files dragged over the panel show 「放到这里」 and are added on drop. Text dragged from Obsidian's file explorer (a
   * library file) goes to the host to read; text dropped on the textarea stays the browser's own.
   */
  private dropTarget(w: HTMLElement): void {
    let depth = 0;
    const hasFiles = (e: DragEvent): boolean => [...(e.dataTransfer?.types ?? [])].includes("Files");
    const wanted = (e: DragEvent): boolean => {
      if (this.clipBtn.hidden) return false;
      if (hasFiles(e)) return true;
      const types = [...(e.dataTransfer?.types ?? [])];
      return e.target !== this.textarea && (types.includes("text/plain") || types.includes("text/uri-list"));
    };
    const off = (): void => { depth = 0; w.classList.remove("ew-drop"); };
    w.ondragenter = (e) => {
      if (!wanted(e)) return;
      e.preventDefault(); depth++;
      if (hasFiles(e)) w.classList.add("ew-drop");
    };
    w.ondragover = (e) => { if (wanted(e)) e.preventDefault(); };
    w.ondragleave = () => { if (--depth <= 0) off(); };
    w.ondrop = (e) => {
      if (!wanted(e)) return;
      e.preventDefault(); off();
      const dt = e.dataTransfer;
      const files = [...(dt?.files ?? [])];
      if (files.length > 0) { this.actions.addFiles(files); return; }
      const text = dt?.getData("text/uri-list") || dt?.getData("text/plain") || "";
      if (text.trim() !== "") this.actions.dropLibrary(text);
    };
    // The overlay's words are copy (CSS reads them from here), not CSS content.
    w.dataset.drop = C.dropHere;
  }

  /**
   * Why nothing can be asked here (not set up, a stored conversation loading / lost): one muted line. No note open, or a
   * plain note, is no line: the conversation without a page is asked as usual.
   */
  private renderStatus(s: AssistantState, c: Conversation | null): void {
    let text = "";
    let reload = false;
    if (!s.configured) text = C.needsSettings;
    else if (c && c.turns.length === 0) {
      // Ruling P10: restore notices only while the conversation has no turns.
      if (c.restore === "loading") text = C.restoring;
      else if (c.restore === "failed") { text = C.restoreFailed; reload = true; }
      else if (c.restore === "gone") text = C.restoreGone;
    }
    const key = `${text}|${reload ? 1 : 0}`;
    if (key === this.statusKey) return;
    this.statusKey = key;
    this.status.replaceChildren(text);
    if (reload) this.status.append(btn(() => (C.reload), "link", () => this.actions.reload()));
  }

  private renderSummary(c: Conversation | null): void {
    const r = c?.recap ?? null;
    const groups: [string, string[]][] = r ? [[C.recapUnderstood, r.understood], [C.recapUnclear, r.unclear], [C.recapEdits, r.edits]] : [];
    const shown = groups.filter(([, items]) => items.length > 0);
    const key = shown.length > 0 ? JSON.stringify(shown) : "";
    if (key === this.summaryText) return;
    this.summaryText = key;
    if (shown.length === 0) { this.summary.replaceChildren(); return; }
    const det = el("details");
    det.append(el("summary", null, () => (C.summary)));
    for (const [label, items] of shown) {
      const ul = el("ul", "titems");
      for (const it of items) ul.append(el("li", "", it));
      det.append(el("div", null, label), ul);
    }
    this.summary.replaceChildren(det);
  }

  private renderThread(c: Conversation | null): void {
    const list = c ? c.turns : [];
    const box = this.thread;
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight <= FOLLOW_PX;
    const seen = new Set<string>();
    const els: HTMLElement[] = [];
    if (this.summaryText) els.push(this.summary);
    if (this.status.childNodes.length > 0 && this.status.textContent !== "") els.push(this.status);
    let newTurn = false;
    // app.js lastAi: the latest answer (one still being written does not count) always shows its rating row.
    let last = -1;
    list.forEach((t, i) => { if (t.status !== "streaming") last = i; });
    // one review card: while the round runs it stays under the newest turn (he talks about a card, then goes on with it);
    // after that, under the last turn that opened the round
    this.reviewTurn = reviewTurnOf(list)?.id ?? null;
    if (this.reviewTurn !== null && this.actions.reviewLive?.() === true) this.reviewTurn = list[list.length - 1]!.id;
    const runs: NonNullable<CachedTurn["run"]>[] = [];
    list.forEach((t, i) => {
      const showRetry = t.error !== null && canRetry(t.error) && i === list.length - 1 && !(c?.busy ?? false);
      // a materials draft card says 打开这一页 only when another page is open: its turn follows the active page
      const key = `${t.rev}|${showRetry ? 1 : 0}|${[...this.openRows].filter((k) => k.startsWith(`${t.id}|`)).join(",")}|${t.actions.some((x) => x.kind === "page_revise") ? this.activePage ?? "" : ""}|${t.id === this.reviewTurn ? `r${this.reviewTick}` : ""}`;
      let cached = this.cache.get(t.id);
      if (!cached) newTurn = true;
      if (!cached || cached.key !== key) {
        const old = cached?.els;
        cached = { key, els: this.buildTurn(t, showRetry) };
        if (old) this.keepAddBoxes(old, cached.els);
        const box = cached.els.flatMap((e) => [...e.querySelectorAll<HTMLElement>(".ew-goalrun")]).pop();
        if (box !== undefined) cached.run = { home: box.parentElement!, box };
        this.cache.set(t.id, cached);
      }
      if (cached.run) runs.push(cached.run);
      seen.add(t.id);
      const row = this.rows.get(t.id);
      if (row && (rated(t) || askRated(t))) {
        // a question he has answered folds its 👎 reasons away (the pressed thumb stays)
        row.paint(t.rating, !askRated(t) || t.asks[t.asks.length - 1]!.status === "waiting");
        cached.els[1]?.classList.toggle("ew-last", i === last);
      }
      els.push(...cached.els);
    });
    // the latest run's step reads where he is looking, under the turns that came after its goal's card
    const latest = runs.pop();
    for (const r of runs) if (r.box.parentElement !== r.home) r.home.append(r.box);
    if (latest !== undefined) {
      if (this.cache.get(list[list.length - 1]!.id)?.run === latest) { if (latest.box.parentElement !== latest.home) latest.home.append(latest.box); } else els.push(latest.box);
    }
    for (const id of [...this.cache.keys()]) if (!seen.has(id)) this.cache.delete(id);
    for (const [k, live] of this.liveAnswers) if (!seen.has(k.split("|")[0]!)) { clearTimeout(live.timer); this.liveAnswers.delete(k); }
    for (const id of [...this.rows.keys()]) if (!seen.has(id)) this.rows.delete(id);
    const current = [...box.children];
    if (current.length !== els.length || current.some((e, i) => e !== els[i])) {
      box.replaceChildren(...els);
      if (atBottom || newTurn) box.scrollTop = box.scrollHeight;
    }
  }

  /**
   * asst.js rowsEl (as Codex / Claude Code show an agent turn): each read / search call is one quiet row, the spinner and
   * 正在… while it runs, then what was done; consecutive finished rows of one group fold into one (读了 3 页); a row with
   * results opens on a click to their titles (a folded row: each call's line). Null: no rows.
   */
  private toolRows(t: Turn): HTMLElement | null {
    // only what the server worded (a call from an older server has no label: no row, never its raw summary)
    // 处理待确认的记忆 reads the groups between questions (app.js cfNextMsg: no tool line)
    const lines = t.tools.filter((l) => (l.status === "running" ? l.label : l.done) !== "" && l.name !== CONFLICTS_READ && !missWithheld(t, l));
    if (lines.length === 0) return null;
    const groups: ToolLine[][] = [];
    for (const l of lines) {
      const g = groups[groups.length - 1], prev = g?.[g.length - 1];
      if (g && prev && l.group !== "" && l.group === prev.group && l.status !== "running" && prev.status !== "running") g.push(l);
      else groups.push([l]);
    }
    const box = el("div", "trows");
    for (const g of groups) {
      const l0 = g[0]!;
      if (l0.status === "running") {
        const d = el("div", "trow run");
        d.append(el("i", "spin"), el("span", "", l0.label));
        box.append(d);
        continue;
      }
      const many = g.length > 1;
      const items = many ? g.map((l) => l.done) : l0.items;
      const label = many && l0.groupLabel !== ""
        ? l0.groupLabel.replace("{n}", String(g.length)).replace("{k}", String(new Set(g.flatMap((l) => l.items)).size))
        : many ? g.map((l) => l.done).join("；") : l0.done;
      if (items.length === 0) { box.append(el("div", "trow", label)); continue; }
      const key = `${t.id}|${l0.callId}`, open = this.openRows.has(key);
      const b = el("button", "trow");
      b.type = "button";
      b.setAttribute("aria-expanded", String(open));
      b.dataset.row = l0.callId;
      const chev = el("span", "chev");
      chev.append(chevIcon());
      b.append(el("span", "", label), chev);
      b.onclick = () => {
        if (this.openRows.has(key)) this.openRows.delete(key); else this.openRows.add(key);
        this.renderThread(this.lastConv);
      };
      box.append(b);
      if (open) {
        const ul = el("ul", "titems");
        for (const it of items) ul.append(el("li", "", it));
        box.append(ul);
      }
    }
    return box;
  }

  /** app.js: `.ew-umsg` (quote above the bubble), then `.ew-amsg` (tools, paragraphs, sources, proposal cards). */
  private buildTurn(t: Turn, showRetry: boolean): HTMLElement[] {
    const u = el("div", "umsg");
    if (t.quote) { const uq = el("div", "uquote", quoteLabel(t.quote)); uq.title = t.quote.text; u.append(uq); }
    for (const att of t.attachments) u.append(this.sentCard(att));
    // 待确认的记忆 · 处理: the chat opens on the assistant's first group (app.js startMemories), no bubble of his
    if (t.question !== "" && !fromConflicts(t)) u.append(el("div", "ubub", t.question));
    const a = el("div", "amsg");
    const thinking = t.status === "streaming" && t.thinking;
    // asst.js: the paragraphs, the cards, then what the reply says after its card (`m.tail`). The text the stream sent
    // before the first action card shows above it; the rest under the cards (Turn.cardAt).
    // A question it asked mid-turn (ask_user) sits where the answer stood when it was asked (Turn.askAt): the text before
    // it, the question and his reply, then the rest. Cards that came before the question show after the whole answer.
    const askAt = t.asks.length > 0 ? Math.min(t.askAt ?? 0, t.answer.length) : 0;
    const split = t.cardAt !== null && t.actions.length > 0 && (t.cardAt ?? 0) >= askAt;
    const at = split ? Math.min(t.cardAt ?? 0, t.answer.length) : t.answer.length;
    const head = t.answer.slice(askAt, at), tail = t.answer.slice(at);
    const textEnd = (): void => {
      if (t.stopped || t.stopping) a.append(el("div", "muted", () => (C.stopped)));
      if (t.answer && t.interrupted) {
        const cut = el("div", "muted", () => (C.interrupted));
        if (showRetry) cut.append(" ", btn(() => (C.retry), "link", () => this.actions.retry(t.id)));
        a.append(cut);
      }
      // While the model works with no row running: one line, the spinner and 正在思考… (or what a drafting step is doing:
      // 正在写草稿). A running read / search is its own row above (asst.js turn).
      if (thinking && !t.tools.some((l) => l.status === "running" && l.label !== "")) {
        const tl = el("div", "tools");
        tl.append(el("i", "spin"), labelNode(() => (t.statusLine || C.thinking)));
        if (t.statusLine) tl.append(elapsedSpan(t.statusSince));
        a.append(tl);
      }
    };
    const rows = this.toolRows(t);
    if (rows !== null) a.append(rows);
    // a settled group of 待确认的记忆 shows where it came, before the next group and its question (app.js cfAnswer: the
    // result, then the next one): the text the turn had written by then, the card, the rest up to the question
    const settled = t.asks.length > 0 ? t.actions.filter((c) => c.kind === CONFLICT_KIND) : [];
    const settledAt = settled.length > 0 ? Math.min(t.cardAt ?? 0, askAt) : askAt;
    if (settledAt > 0) a.append(this.answerBody(t, 0, t.answer.slice(0, settledAt)));
    for (const card of settled) a.append(buildActionCard(card, this.cardHooks, this.activePage));
    if (askAt > settledAt) a.append(this.answerBody(t, settledAt, t.answer.slice(settledAt, askAt)));
    for (const q of t.asks) a.append(...this.askBlock(q, t));
    if (head !== "") a.append(this.answerBody(t, askAt, head));
    if (!split) textEnd();
    // asst.js: what the answer read (pages, materials, web pages it opened), under it like ChatGPT's sources; search hits are not.
    const sources = turnSources(t);
    if (sources.length > 0 && t.status !== "streaming") {
      const sr = el("div", "srcl");
      sr.append(el("span", "srcl-h", () => (C.sourcesHead)));
      for (const src of sources) {
        const link = el("button", "srclink", src.title);
        link.type = "button";
        if (openable(src)) link.onclick = () => this.actions.openSource(src);
        else link.disabled = true;
        sr.append(link);
      }
      a.append(sr);
    }
    // The server refuses a decision (409) while the turn that raised the proposal is still streaming.
    for (const card of t.proposals) a.append(...this.buildCard(t.id, card, t.status !== "streaming"));
    // asst.js: the ledger card (actCard), then the quiz (quizCard).
    // a recorded preference is a quiet line of its own (no 内容建议 label)
    for (const card of t.actions) {
      // 学点新的 → 开始: its blocks are 学点新的's list (edited and confirmed there), so the chat shows no blocks card
      // (asst.js startBlocks: one line); a draft that failed still says so here
      if (inLearn(t, card) || settled.includes(card)) continue;
      // a settled group of 待确认的记忆 is titled with what was kept (app.js cfAnswer: no 内容建议 label)
      // 内容建议 is what still waits for him: a change that took effect at once is not a suggestion
      if (card.confirm && !isNoteLine(card) && card.kind !== CONFLICT_KIND) a.append(el("div", "asec", () => (C.proposalSection)));
      a.append(buildActionCard(card, this.cardHooks, this.activePage));
    }
    if (split) {
      if (tail.trim() !== "") a.append(this.answerBody(t, at, tail));
      textEnd();
    }
    for (const quiz of t.quizzes) {
      a.append(buildQuizCard(quiz, { answer: (id, k) => this.actions.answerQuiz(id, k), next: (id) => this.actions.nextQuiz(id) }));
      quiz.fresh = false;     // the pop and the tick play once, on the draw right after the answer
    }
    if (t.id === this.reviewTurn) {
      const card = this.actions.reviewCard?.() ?? null;
      if (card !== null) a.append(card);
    }
    // a goal confirmed in the chat: its diagnosis, the block entered, the first page (goal-run.ts)
    for (const card of t.actions) {
      const run = this.actions.goalRun?.(card.actionId);
      // 学点新的's goal: its progress (the block entered, the page written) is shown there, not in a card here; only a
      // failure (重试) or a stopped write (重新写) is said in the chat
      const v = run !== undefined && fromLearn(t) ? run.view().kind : null;
      if (v !== null && v !== "failed" && v !== "stopped") {
        // nothing was asked (零基础): the chat still says where he starts, once a block is entered
        const name = run?.enteredName ?? null;
        if (run?.askedInChat === false && name !== null && (v === "writing" || v === "written")) a.append(el("p", null, () => (GOAL_RUN_COPY.startsAt(name))));
        continue;
      }
      const box = run ? buildGoalRunCard(run, { pick: (id, k) => this.actions.pickGoal?.(id, k), skip: (id) => this.actions.skipGoal?.(id),
        retry: (id) => this.actions.retryGoal?.(id), restart: (id) => this.actions.restartGoal?.(id), stop: (id) => this.actions.stopGoal?.(id),
        showPage: (pid) => this.actions.showPage(pid), showGoal: (id) => (this.actions.showResult ?? this.actions.showInParams)(id) }) : null;
      if (box) a.append(box);
    }
    // a message that could not be sent (offline) has no line of its own: the panel's 未连接到服务器 · 重试 says it and
    // sends it again once connected (AssistantController.resendOffline)
    if (t.error && t.error.kind !== "offline") {
      const modelAction = t.error?.kind === "not_enabled" || (t.error?.kind === "event" && ["llm_unavailable", "llm_busy", "llm_timeout"].includes(t.error.code));
      const line = this.errorLine(errorText(t.error), modelAction ? "switch-model" : t.error?.kind === "event" && t.error.code === "budget_exceeded" ? "open-settings" : null);
      if (showRetry && !t.interrupted) line.append(" ", btn(() => (C.retry), "link", () => this.actions.retry(t.id)));
      a.append(line);
    }
    // app.js rateRow: last, under a finished answer (复制 / 👍 / 👎).
    if (rated(t)) {
      let row = this.rows.get(t.id);
      if (!row) {
        row = new RatingRow(() => plainCitations(t.answer), (next) => this.actions.rate(t.id, next));
        this.rows.set(t.id, row);
      }
      a.append(row.root);
    }
    // 待确认的记忆 · 处理 sent no words of his to show: only the answer
    if (u.childNodes.length === 0 && fromConflicts(t)) return [a];
    return a.childNodes.length > 0 ? [u, a] : [u];
  }

  /**
   * Below, a part of the answer (`start`: where it starts in the turn's text) is Markdown, rendered by the host, also
   * while it streams: one element per part, rendered again at most every STREAM_RENDER_MS. The plain paragraphs stay
   * until the first Markdown is ready, so the answer never blinks empty.
   */
  /**
   * A question the assistant asked, as the chat shows talk: its words as the assistant's own line and, while his next
   * message has not gone out, the options it gave (if any) as reply pills. His answer is his next message (its own bubble).
   * The question the finished turn ended on carries the turn's 👍 / 👎 at the end of its line (app.js `rateRow(m, lead)`).
   */
  private askBlock(q: AskState, t: Turn): HTMLElement[] {
    // 学点新的's diagnosis: the 摸底 grid, then 细问 cards (diag-cards.ts, the approved demo's)
    if (q.rough !== null) {
      const live = q.status === "waiting" && this.actions.markRough !== undefined;
      return [buildRoughCard(q.rough, { mark: (id, v) => this.actions.markRough?.(q.callId, id, v), submit: () => this.actions.submitRough?.(q.callId) }, live)];
    }
    if (q.kind === CHECK_KIND && q.block !== null) {
      const waiting = q.status === "waiting" && this.actions.answerAsk !== undefined;
      let rating: HTMLElement | null = null;
      if (askRated(t) && q === t.asks[t.asks.length - 1]) {
        let row = this.rows.get(t.id);
        if (!row) {
          // 👎 → 我说过不会 on the question still waiting: the block is 不会 (answered 不知道), the next question follows
          row = new RatingRow(() => q.question, (next) => {
            this.actions.rate(t.id, next);
            if (next.value === "down" && next.chips.includes(SAID_DONT) && q.status === "waiting") this.actions.answerAsk?.(q.callId, dontOption(q.options));
          }, el("span"));
          this.rows.set(t.id, row);
        }
        rating = row.root;
      }
      return [buildCheckCard({ blockName: q.block.name, question: q.question, options: q.options, waiting },
        (o) => this.actions.answerAsk?.(q.callId, o), rating)];
    }
    const out: HTMLElement[] = [];
    if (q.question !== "") {
      const line = el("p", null, q.question);
      if (askRated(t) && q === t.asks[t.asks.length - 1]) {
        let row = this.rows.get(t.id);
        if (!row) {
          row = new RatingRow(() => q.question, (next) => this.actions.rate(t.id, next), line);
          this.rows.set(t.id, row);
        }
        out.push(row.root);
      } else out.push(line);
    }
    if (q.status === "waiting" && q.options.length > 0 && this.actions.answerAsk) {
      const pills = el("div", "sugg");
      for (const o of q.options) {      // plain pills, as app.js askBlock (`.sugg button`, no .btn)
        const b = el("button", null, o); b.type = "button"; b.onclick = () => this.actions.answerAsk?.(q.callId, o);
        pills.append(b);
      }
      out.push(pills);
    }
    return out;
  }

  private answerBody(t: Turn, start: number, text: string): HTMLElement {
    const key = `${t.id}|${start}`;
    const live = this.liveAnswers.get(key);
    if (t.status !== "streaming" && live) {     // the same element: the end of the stream does not flash raw text
      clearTimeout(live.timer);
      this.liveAnswers.delete(key);
      void this.renderMarkdown(live.body, this.cited(text));
      return live.body;
    }
    if (live) { live.text = text; this.renderLive(key, live); return live.body; }
    const body = el("div", "answer");
    body.addEventListener("click", (e) => { this.citationClick(e); });
    for (const line of plainCitations(text).split(/\n+/)) if (line.trim() !== "") body.append(el("p", null, line));
    if (t.status === "done") void this.renderMarkdown(body, this.cited(text));
    if (t.status === "streaming") {
      const fresh: LiveAnswer = { body, text, rendered: "", at: 0, busy: false, timer: undefined };
      this.liveAnswers.set(key, fresh);
      this.renderLive(key, fresh);
    }
    return body;
  }

  /** One render at a time, at most every STREAM_RENDER_MS; a thread that was following the end keeps following it. */
  private renderLive(key: string, live: LiveAnswer): void {
    if (live.busy || live.timer !== undefined || live.text === live.rendered) return;
    const wait = live.at + STREAM_RENDER_MS - Date.now();
    if (wait > 0) {
      live.timer = setTimeout(() => { live.timer = undefined; this.renderLive(key, live); }, wait);
      return;
    }
    const box = this.thread;
    const following = box.scrollHeight - box.scrollTop - box.clientHeight <= FOLLOW_PX;
    live.rendered = live.text; live.at = Date.now(); live.busy = true;
    const after = (): void => {
      live.busy = false;
      if (this.liveAnswers.get(key) !== live) return;
      if (following) box.scrollTop = box.scrollHeight;
      this.renderLive(key, live);
    };
    void Promise.resolve(this.renderMarkdown(live.body, this.cited(live.text))).then(after, after);
  }

  private cited(text: string): string {
    return citationsToMarkdown(text, (id) => this.actions.citationGone?.(id) ?? false);
  }
  private renderMarkdown(body: HTMLElement, markdown: string): void | Promise<void> {
    const result = this.hooks.renderMarkdown(body, markdown);
    if (result) return result.then(() => { bindCitationLabels(body); });
    bindCitationLabels(body);
  }

  /** A citation in a rendered answer: opens the original at its page; a material deleted meanwhile shows as deleted at once. */
  private citationClick(e: Event): void {
    const a = e.target instanceof Element ? e.target.closest<HTMLElement>("a.ew-cite[data-source]") : null;
    if (a === null) return;
    e.preventDefault();
    const open = this.actions.openCitation;
    if (open === undefined) return;
    void open(a.dataset.source ?? "", Number(a.dataset.page)).then((r) => {
      if (r !== "gone") return;
      const gone = el("span", "cite gone", () => (`${a.textContent ?? ""}${C.citeDeleted}`));
      gone.title = a.title;
      a.replaceWith(gone);
    });
  }

  /**
   * A card on a sent message (asst.js msgAtt): thumbnail and state, then 正在解析 + bar while an import the chat started runs
   * and 「N 天后清掉」 while nothing keeps it. No buttons: what to do with the file comes from the chat.
   */
  private sentCard(a: TurnAttachment): HTMLElement {
    const [line, tone] = turnStatus(a);
    const c = el("div", `att m${a.deleted ? " gone" : tone === "err" ? " bad" : tone === "warn" ? " warn" : ""}`);
    const top = el("div", "atop");
    top.append(attThumb(a.kind, a.thumb, !a.deleted && a.kind === "image"), attText(a.title, line, tone, a.deleted ? "" : attFacts(a)));
    c.append(top);
    if (a.deleted || a.status === "failed" || a.status === "reading" || a.status === "uploading") return c;
    if (a.progress !== null) {
      const j = el("div", "ajoin");
      j.append(el("span", null, () => (C.attReading)), progressBar(a.progress));
      c.append(j);
    }
    const days = learning(a.uses) ? null : expiresInDays(a.expiresAt, Date.now());
    if (days !== null) c.append(el("div", "aexp", () => (C.attExpires(days))));
    if (a.error !== "") c.append(el("div", "aerr", a.error));
    return c;
  }

  /** The draft cards above the textarea (asst.js attCard): rebuilt only when what they show changes. */
  private renderAttachments(list: DraftAttachment[]): void {
    const key = JSON.stringify(list.map((d) => [d.localId, d.name, d.status, d.progress ?? null, d.error ?? "", d.pages, d.unreadable ?? null, d.thumb ?? ""]));
    if (key === this.attsKey) return;
    this.attsKey = key;
    this.atts.hidden = list.length === 0;
    this.atts.replaceChildren(...list.map((d) => this.draftCard(d)));
  }

  private draftCard(d: DraftAttachment): HTMLElement {
    const [line, tone] = draftStatus(d);
    const working = d.status === "uploading" || d.status === "reading";
    const c = el("div", `att${tone === "err" ? " bad" : tone === "warn" ? " warn" : ""}`);
    c.append(attThumb(d.kind, d.thumb, false));
    const b = attText(d.name, line, tone, working || d.status === "failed" ? "" : attFacts(d));
    if (working) b.append(progressBar(d.progress ?? { done: 0, total: 0 }));
    c.append(b);
    const x = el("button", "x", "×");
    x.type = "button"; localizedAttribute(x, "aria-label", () => (C.attachRemoveNamed(d.name)));
    x.onclick = () => this.actions.removeAttachment(d.localId);
    c.append(x);
    return c;
  }

  /** app.js `.propcard` (内容建议): the section label, the title = what is added, then the verbs while undecided, else the result. */
  private buildCard(turnId: string, card: ProposalCard, turnEnded: boolean): HTMLElement[] {
    const kind = card.draft.kind;
    const label = el("div", "asec", () => (C.proposalSection));
    const pc = el("div", "propcard");
    pc.append(el("b", null, () => (C.proposalAsk(kind, card.draft.heading))));
    if (card.change) pc.append(this.changeEl(turnId, card.proposalId, card.change, card.draft.heading));
    if (card.status === "accepted" || card.status === "rejected" || card.status === "already_decided" || card.status === "undone") {
      const status = card.status;
      const res = el("div", "res propres", () => (C.decided(kind)[status]));
      if (card.status === "accepted" && card.canUndo) {
        const u = btn(() => (ACTION_COPY.undo), "link", () => this.actions.undoProposal(turnId, card.proposalId));
        u.disabled = card.undoing;
        u.dataset.op = "undo";
        res.append(u);
      }
      pc.append(res);
      if (card.note !== "") pc.append(el("div", "muted", card.note));
      return [label, pc];
    }
    // The ledger decides once: a failed apply is final (a second click would only answer already_decided).
    if (card.status === "failed") {
      if (card.detail === LEDGER_STALE) {
        // the page moved where it goes (the section it rewrites was edited since): nothing was written, and a quiet 重试
        // asks for it again in the chat, for the page as it is now
        pc.append(el("div", "res", () => (C.proposalStale(kind))));
        const row = el("div", "row");
        const again = btn(() => (REVISE_COPY.redraft), "link", () => { this.actions.sendEdit(C.proposalAgain(kind, card.draft.heading, card.draft.why)); });
        again.classList.add("ew-retry");
        again.dataset.op = "redraft";
        row.append(again);
        pc.append(row);
      } else pc.append(el("div", "muted", decidedFailedText(card.detail)));
      return [label, pc];
    }
    const open = cardOpen(card) && turnEnded;
    const row = el("div", "row");
    const acc = btn(() => (C.accept(kind)), "primary", () => this.actions.decide(turnId, card.proposalId, "accept"));
    const rej = btn(() => (C.reject(kind)), null, () => this.actions.decide(turnId, card.proposalId, "reject"));
    acc.disabled = !open; rej.disabled = !open;
    row.append(acc, rej);
    pc.append(row);
    const status = card.status === "sending" ? C.sending
      : card.status === "error" ? (card.error ? proposalErrorText(card.error) : "") : card.note;
    if (status) pc.append(el("div", "muted", status));
    return [label, pc];
  }

  /** 看改动: closed unless he opened it (`openDiffs`: a card is drawn again on every change of its turn), its size on the line. */
  private changeEl(turnId: string, proposalId: string, change: ProposalChange, heading: string): HTMLElement {
    const view = changeView(change);
    const d = el("details", "propdiff");
    d.open = this.openDiffs.has(proposalId);
    d.ontoggle = () => {
      if (d.open) {
        const opened = !this.openDiffs.has(proposalId);
        this.openDiffs.add(proposalId);
        if (opened) this.actions.showProposal?.(turnId, proposalId);
      } else this.openDiffs.delete(proposalId);
    };
    const s = el("summary", null, () => (C.proposalDiff));
    const size = C.proposalDiffSize(view.removed, view.added);
    if (size !== "") s.append(el("span", "faint", size));
    const body = el("div", "diff");
    localizedAttribute(body, "aria-label", () => (ACTION_COPY.diffLabel));
    const line = (kind: string, mark: string, content: HTMLElement): void => {
      const l = el("div", "dl " + kind);
      l.append(el("span", "mk", mark), content);
      body.append(l);
    };
    if (change.old_heading != null && change.old_heading !== heading) {
      line("del", "−", el("span", null, () => (C.proposalHeading(change.old_heading!))));
      line("add", "+", el("span", null, () => (C.proposalHeading(heading))));
    }
    for (const ln of view.lines) {
      if (ln.kind !== "mod") { line(ln.kind, ln.kind === "add" ? "+" : ln.kind === "del" ? "−" : "…", el("span", null, ln.text)); continue; }
      const t = el("span", "trk");
      for (const [k, text] of ln.ops) t.append(k === "=" ? text : el(k === "-" ? "del" : "ins", null, text));
      line("mod", "~", t);
    }
    d.append(s, body);
    return d;
  }

  /** A turn's error line; 「切换模型」 in it opens the picker (asst.js errLine), when there is one. */
  private errorLine(text: string, action: "switch-model" | "open-settings" | null): HTMLElement {
    const line = el("div", "muted");
    if (action !== "switch-model" || !this.actions.toggleModels) { line.append(...withSettingsLink(text, this.actions.openSettings, action === "open-settings")); return line; }
    const link = el("button", "elink", () => (C.switchModel));
    link.type = "button";
    link.onclick = (e) => { e.stopPropagation(); this.actions.toggleModels?.(true); };
    link.dataset.action = action;
    line.append(el("span", "", text), " · ", link);
    return line;
  }

  /** Concrete model selection: browse a provider, choose its model, then the reasoning levels it supports.
   *  Reuses the composer's floating menu and its keyboard navigation. */
  private renderModels(m: AssistantState["models"]): void {
    if (!m?.open) this.modelProvider = null;
    const key = m ? JSON.stringify([m.list, m.current, m.reasoning, m.open, m.pending, this.modelProvider]) : "";
    if (key === this.mpickKey) return;
    this.mpickKey = key;
    const doc = this.root.ownerDocument;
    doc.removeEventListener("click", this.closeModels);
    doc.removeEventListener("keydown", this.escModels);
    this.mpickStop?.(); this.mpickStop = null;
    this.mpick.replaceChildren();
    const cur = m ? m.list.find((x) => x.id === m.current) : undefined;
    this.mpick.hidden = !m || !this.actions.toggleModels;
    if (!m) return;
    const lv = cur?.levels.find((x) => x.reasoning === m.reasoning);
    const label = cur ? (lv ? C.modelLabel(cur.name, lv.name) : cur.name) : tr("runtime.chooseModel");
    const b = el("button", "mpick");
    b.type = "button";
    b.disabled = !!m.pending;
    b.setAttribute("aria-haspopup", "menu");
    b.setAttribute("aria-expanded", String(m.open));
    localizedAttribute(b, "aria-label", () => (C.modelAria(label)));
    b.append(el("span", "", label), chevIcon());
    b.onclick = (e) => { e.stopPropagation(); this.mpickFocus = !m.open; this.actions.toggleModels?.(); };
    this.mpick.append(b);
    if (!m.open) return;
    const menu = el("div", "mmenu");
    menu.setAttribute("role", "menu");
    menu.onclick = (e) => e.stopPropagation();
    this.mpickStop = openMenuAt(b, menu, (refocus) => {
      this.actions.toggleModels?.(false);
      if (refocus) this.mpick.querySelector<HTMLElement>(".ew-mpick")?.focus();
    }, this.mpickFocus, { placement: "top-start", gap: 6 });
    this.mpickFocus = false;
    const provider = this.modelProvider ?? cur?.provider ?? m.list[0]?.provider;
    const row = (name: string, selected: boolean, disabled: boolean, action: () => void, reason = ""): void => {
      const wrap = el("div", "mrow");
      const it = el("button", "mitem");
      it.type = "button";
      it.setAttribute("role", "menuitemradio");
      it.setAttribute("aria-checked", String(selected));
      it.disabled = disabled || !!m.pending;
      it.append(el("span", "", name));
      if (selected) it.append(tickIcon());
      it.onclick = action;
      wrap.append(it);
      if (disabled && reason) wrap.append(qTip(reason, () => (C.modelOffLabel)));
      menu.append(wrap);
    };
    const section = (name: string): void => { const title = el("div", "muted", name); title.setAttribute("role", "presentation"); menu.append(title); };
    section(tr("runtime.provider"));
    for (const id of new Set(m.list.map((x) => x.provider))) {
      const models = m.list.filter((x) => x.provider === id);
      const available = models.some((x) => x.available);
      row(models[0]!.provider_name, id === provider, !available, () => {
        this.modelProvider = id;
        this.mpickFocus = true;
        this.renderModels(m);
      }, models.find((x) => x.reason)?.reason);
    }
    menu.append(el("hr")); section(tr("runtime.model"));
    for (const x of m.list.filter((x) => x.provider === provider)) {
      row(x.name, x.id === cur?.id, !x.available, () => { this.actions.pickModel?.({ model: x.id }); this.textarea.focus(); }, x.reason);
    }
    if (cur && cur.provider === provider && cur.levels.length > 0) {
    menu.append(el("hr")); section(tr("runtime.reasoning"));
      for (const l of cur.levels) row(l.name, l.reasoning === m.reasoning, false, () => {
        this.actions.pickModel?.({ reasoning: l.reasoning }); this.textarea.focus();
      });
    }
    this.mpick.append(menu);
    doc.addEventListener("click", this.closeModels);
    doc.addEventListener("keydown", this.escModels);
  }

  private renderComposer(s: AssistantState, c: Conversation | null): void {
    const live = s.configured && c !== null;
    this.composer.hidden = !live;
    if (!c || !live) return;
    this.renderModels(s.models ?? null);
    // Quote chip, rebuilt only when the quote changes.
    const quote = c.quote;
    const chipKey = quote ? JSON.stringify([quote.heading, quote.text, quoteIsQuestion(quote)]) : "";
    if (chipKey !== this.chipKey) {
      this.chipKey = chipKey;
      this.chip?.remove();
      this.chip = null;
      if (quote) {
        const q = el("div", "qchip");
        const x = el("button", null, "×");
        x.type = "button"; localizedAttribute(x, "aria-label", () => (C.removeQuote));
        x.onclick = () => this.actions.removeQuote();
        const label = el("span", null, quoteLabel(quote)); label.title = quote.text;
        q.append(label, x);
        this.chip = q;
        this.box.prepend(q);
      }
    }
    // Never overwrite what is being typed: write only when unfocused, after a send (draft ""), or on a note switch.
    const switched = c.key !== this.convKey;
    this.convKey = c.key;
    const ta = this.textarea;
    if (ta.value !== c.draft && (switched || c.draft === "" || ta.ownerDocument.activeElement !== ta)) {
      ta.value = c.draft;
      this.grow();
    }
    const n = codePoints(c.draft);
    const tooLong = n > MAX_MESSAGE_CHARS;
    localizedText(this.over, () => (tooLong ? C.tooLong(n) : ""));
    this.over.hidden = !tooLong;
    // app.js: disabled with no text and no quote (a quote alone asks the controller's quote-only question).
    const stopSlot = s.canStop === true;
    if (stopSlot !== this.stopSlot) {
      this.stopSlot = stopSlot;
      this.sendBtn.replaceChildren(stopSlot ? stopIcon() : sendIcon());
      localizedAttribute(this.sendBtn, "aria-label", () => (stopSlot ? C.stop : C.send));
    }
    this.sendBtn.disabled = !stopSlot && !canSend(c);
    // 📎 only when the composer takes files; while one is being read the reason send is off sits beside it.
    this.clipBtn.hidden = !s.canAttach;
    this.renderAttachments(s.canAttach ? c.attachments : []);
    this.wait.hidden = !c.attachments.some((d) => d.status === "uploading" || d.status === "reading");
    localizedText(this.wait, () => (this.wait.hidden ? "" : C.attachWait));
    // The last turn ended on a question (ask_user): what he sends answers it (the composer is the normal one).
    localizedAttribute(ta, "placeholder", () => (waitingAsk(c.turns) !== null ? C.askPlaceholder : c.target.kind === "free" ? C.placeholderNoPage : C.placeholder));
    // Only a panel the reader can see takes the focus (another open host of the same conversation may be hidden).
    if (s.focusTick > this.focusTick && visible(ta)) ta.focus();
    this.focusTick = s.focusTick;
  }
}

/** The card's picture: a thumbnail for an image we hold, else the kind's tag (asst.js attThumb). */
/** How long the step lines of a long tool have been running, ticking while the line is on screen; nothing for a quick step. */
export function elapsedText(ms: number): string {
  const s = Math.floor(ms / 1000);
  if (s < 3) return "";
  return s < 60 ? tr("runtime.elapsedSeconds", { seconds: s }) : tr("runtime.elapsedMinutes", { minutes: Math.floor(s / 60), seconds: s % 60 });
}

function elapsedSpan(since: number): HTMLElement {
  const span = el("span", "elapsed", elapsedText(Date.now() - since));
  const timer = window.setInterval(() => {
    if (span.isConnected) span.textContent = elapsedText(Date.now() - since);
    else clearInterval(timer);
  }, 1000);
  return span;
}

function attThumb(kind: string, thumb: string | undefined, big: boolean): HTMLElement {
  const isImg = kind === "image";
  const t = el("span", `th${isImg ? " img" : ""}${big ? " big" : ""}`);
  if (isImg && thumb !== undefined) {
    const im = el("img"); im.src = thumb; im.alt = "";
    t.append(im);
  } else if (!isImg) t.textContent = kind.toUpperCase().slice(0, 4);
  return t;
}

/** Name, state line and the file's own facts (asst.js attText). */
function attText(name: string, line: string, tone: AttTone, facts: string): HTMLElement {
  const b = el("div", "ab");
  b.append(el("div", "an", name), el("div", `as${tone ? " " + tone : ""}`, line));
  if (facts !== "") b.append(el("div", "ad", facts));
  return b;
}

/** The thin progress bar (no counts shown); an unknown total shows a short stub. */
function progressBar(p: { done: number; total: number }): HTMLElement {
  const b = el("div", "rvbar");
  const i = el("i");
  i.style.width = `${p.total > 0 ? Math.min(100, Math.round((p.done / p.total) * 100)) : 8}%`;
  b.append(i);
  return b;
}

/** A finished answer with text and its message id gets the rating row. */
/**
 * A goal's blocks card of 学点新的's 开始: not drawn in the chat unless its draft failed. A draft still waiting in a turn read
 * back (a reload: `t.learn` is gone and 学点新的 follows no conversation any more) has only the chat to be seen in.
 */
function inLearn(t: Turn, card: { kind: string; status: string }): boolean {
  if (!fromLearn(t) || card.kind !== GOAL_KIND) return false;
  const phase = cardPhase(card.status);
  return phase !== "failed" && (phase !== "wait" || t.learn !== undefined);
}

/** 👎 → 我说过不会 on a 细问 (QUESTION_CHIPS[1]). */
const SAID_DONT = "我说过不会";
/** The option that says 不会 on a 细问: 不知道 on a quiz, else the last (a self-report runs from 会 to 不会). */
function dontOption(options: readonly string[]): string {
  return options.find((option) => /^(?:不知道|I (?:do not|don't) know|Not sure)$/i.test(option)) ?? options[options.length - 1] ?? GOAL_RUN_COPY.idk;
}

function rated(t: Turn): boolean {
  // a turn that ended on a question (ask_user) gets none: it is no answer yet (app.js `m.ask && !m.tail`)
  return t.status === "done" && !t.stopped && t.answer.trim() !== "" && t.answerId !== null && t.asks.length === 0;
}

/** A finished turn that ended on a question: rated on the question's own line (the same rating of its message). */
function askRated(t: Turn): boolean {
  return t.status === "done" && t.answerId !== null && t.asks.length > 0;
}

/** In the document and not hidden (checkVisibility where the engine has it: display:none ancestors, hidden leaves). */
export function visible(e: HTMLElement): boolean {
  if (!e.isConnected || e.closest("[hidden]") !== null) return false;
  return typeof e.checkVisibility !== "function" || e.checkVisibility();
}

/** A wrong-answer question shows its own text, any other quote the selected words (cut; the whole on hover). */
function quoteLabel(q: Quote): string {
  return quoteIsQuestion(q) ? q.text : C.quoteText(q.text, q.heading);
}

/** A chip opens a wiki page (page_id) or an http(s) page; anything else (a segment, a javascript: url) is inert. */
function openable(src: Source): boolean {
  return (src.page_id ?? "") !== "" || HTTP_URL.test(src.url ?? "");
}

/** What the answer can cite: the refs of its finished tool calls (the pages, materials and web pages read); each once, in order. */
function turnSources(t: Turn): Source[] {
  const all = t.tools.filter((l) => l.status !== "running").flatMap((l) => l.refs);
  const seen = new Set<string>();
  return all.filter((src) => {
    const key = JSON.stringify([src.page_id ?? "", src.url ?? "", src.page_id || src.url ? "" : src.title]);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}
