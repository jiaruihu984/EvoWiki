/** One question and its streamed answer, folded from assistant events. Pure state; the renderer reads it by `rev`. */
import { AbortedError, ApiError, OfflineError } from "../api/errors";
import { mergeOut, type TurnAttachment } from "./attachments";
import { newActionCard, updateActionCard, type ActionCardState, type QuizState } from "./card-state";
import type { AskEvent, AssistantEvent, ReplyData, DecisionStatus, ErrorCode, Observed, ProposalChange, ProposalDraft, ProposalStateOut, RatingChip, RatingOut, RatingValue, Source, ToolEffect } from "./types";
import { checkBlock, CHECK_KIND, ROUGH_KIND, roughBlocks, type RoughState } from "./diag-cards";

/** A selection carries its semantic kind independently of its displayed text. */
export interface Quote {
  text: string; heading: string | null; kind?: "text" | "wrong_answer";
  /** The section version the text was selected from, as last shown (sent as selection.artifact_id when known). */
  artifactId?: string | null;
}

/** A wrong-answer question: flagged, or (a replayed quote, no flag) a heading-less quote that starts with 题目：. */
export function quoteIsQuestion(q: Quote): boolean {
  return q.kind === "wrong_answer";
}
/**
 * One tool call as a row (assistant/tool_rows.py): `label` while it runs (正在搜索：协方差), `done` once finished (在 Wiki 里
 * 搜了「协方差」 · 5 页); consecutive finished rows of one `group` fold into `groupLabel` ({n} calls, {k} different items);
 * `items`: the result titles behind a click; `refs`: what the answer can cite (listed under it). `sources`: the raw ones.
 */
export const OPEN_REVIEW = "open_review";
export interface ToolLine {
  callId: string; name: string; effect: ToolEffect; summary: string; status: "running" | "ok" | "failed"; sources: Source[];
  label: string; done: string; group: string; groupLabel: string; items: string[]; refs: Source[];
}
/**
 * A failed read the turn may still get past says nothing yet, and nothing at all once a later call of the same tool
 * worked (the model made up a page id, then read the page): only a read that stayed failed is said.
 */
export function missWithheld(t: { status: string; tools: ToolLine[] }, l: ToolLine): boolean {
  if (l.status !== "failed" || (l.effect !== "read" && l.effect !== "network")) return false;
  if (t.status === "streaming") return true;
  return t.tools.slice(t.tools.indexOf(l) + 1).some((x) => x.name === l.name && x.status === "ok");
}
/** The last turn whose open_review call worked: it shows 「帮我复习」's card. */
export function reviewTurnOf<T extends { tools: readonly { name: string; status: string }[] }>(turns: readonly T[]): T | null {
  for (let i = turns.length - 1; i >= 0; i--) if (turns[i]!.tools.some((l) => l.name === OPEN_REVIEW && l.status === "ok")) return turns[i]!;
  return null;
}
/** A finished row's fields from a tool_end / a stored call (each optional: an older server sends none). */
export function rowFields(e: { label?: string; group?: string; group_label?: string; items?: string[]; refs?: Source[] }):
  Pick<ToolLine, "done" | "group" | "groupLabel" | "items" | "refs"> {
  return { done: e.label ?? "", group: e.group ?? "", groupLabel: e.group_label ?? "", items: [...(e.items ?? [])], refs: [...(e.refs ?? [])] };
}
/** A restored card can also be `undone` (ProposalStateOut: accepted, then undone in the ledger). */
export type CardStatus = "open" | "sending" | DecisionStatus | ProposalStateOut["status"] | "error";
export interface ProposalCard {
  proposalId: string; draft: ProposalDraft; status: CardStatus; applied: string[]; error: TurnError | null;
  /** The decision's server detail ("" if none): for `failed`, the reason the page was not changed. */
  detail: string;
  /** What it changes (看改动), read when it was raised; null on a card from before that. */
  change: ProposalChange | null;
  /** An accepted edit's ledger row and whether 撤销 can still take it back; `undoing` while that is out; `note`: why a click changed nothing. */
  actionId: string; canUndo: boolean; undoing: boolean; note: string;
}
export type TurnError =
  | { kind: "event"; code: ErrorCode; retryable: boolean; detail?: string }
  | { kind: "offline" } | { kind: "aborted" } | { kind: "broken" }
  | { kind: "not_enabled" } | { kind: "session_gone" } | { kind: "unknown_page" } | { kind: "busy" }
  /** The server refused the message for one of its attachments (still reading, failed, deleted): `detail` is its text. */
  | { kind: "attachment"; detail: string }
  | { kind: "http"; status: number };
/** The 👍 / 👎 on a finished answer as shown (set at the click; the server's reply, or the last good one on a failure, replaces it). */
export interface Rating { value: RatingValue | null; chips: RatingChip[]; note: string }
export const NO_RATING: Readonly<Rating> = { value: null, chips: [], note: "" };
/** A stored rating (MessageOut.rating / the POST reply) as the row shows it. */
export function ratingOf(r: RatingOut | null | undefined): Rating {
  if (!r || r.value === null) return { ...NO_RATING, chips: [] };
  return { value: r.value, chips: r.value === "down" ? [...(r.chips ?? [])] : [], note: r.value === "down" ? r.note ?? "" : "" };
}
export interface Turn {
  id: string; rev: number; question: string; quote: Quote | null;
  answer: string; tools: ToolLine[]; proposals: ProposalCard[]; observed: Observed | null;
  /** The ledger rows and quizzes the turn's tools made (shown after the proposals, in this order). */
  actions: ActionCardState[]; quizzes: QuizState[];
  status: "streaming" | "done" | "error"; error: TurnError | null;
  /** The answer broke off: the text shown so far stays, marked as cut (an error with `interrupted`). */
  interrupted: boolean;
  /** The server took this message and every resume failed: the turn goes on there, so it is read again, never sent again. */
  lost?: boolean;
  /** He pressed stop: nothing new waits (no thinking line); the server's end of the turn is still to come. */
  stopping: boolean;
  /** The turn ended as stopped (`done` with `stopped`, or the stored message): the text so far is all of it. */
  stopped: boolean;
  /**
   * Waiting for answer text: from the send until the first non-empty text, and again after a tool call starts (the
   * model goes back to work). The renderer shows the waiting line (spinner + `statusLine`, else 正在思考…) while it holds.
   */
  thinking: boolean;
  /**
   * What the assistant is doing right now (the last `status` event, e.g. 正在读《基因配对》第 2 节), shown on the waiting
   * line in place of the previous one; "" = none (the line says 正在思考…). Cleared when answer text arrives and when the
   * call it named ends (its tool_end, or the card it made), so a stale line never stays up while the model thinks again.
   */
  statusLine: string;
  /** When the current run of step lines began (ms; 0 = no line): the waiting line shows the time since. */
  statusSince: number;
  /** When the question was sent (Date.now()); the thinking timer counts from here. */
  sentAt: number;
  /** Restored from the session read: sources of the answer and how many proposals it raised (the cards themselves are in `proposals`). */
  restored: { sources: Source[]; proposalCount: number } | null;
  /** What the question was sent with (cards on the user message); the turn's done brings the server's record of them. */
  attachments: TurnAttachment[];
  /** The answer's own message id (Done.message_id, or the stored assistant message's): what a rating is posted on. */
  answerId: string | null;
  /** The answer's 👍 / 👎. Changing it does not bump `rev` (the rating row repaints in place). */
  rating: Rating;
  /**
   * Where the turn's action cards sit in the answer (asst.js `m.paras` / card / `m.tail`): the answer's length when the
   * first card's `action` event came, so `answer[:cardAt]` shows above the cards and the rest below them. null: every
   * card comes after the whole answer (no card yet, or no text before it: a card the model made before writing anything).
   */
  cardAt: number | null;
  /** 学点新的 → 开始: the goal the message carries (sent as intent learn_goal: the server runs new_goal with it); absent: chat. */
  learn?: LearnGoal;
  /** The message that starts this draft's diagnosis in the chat (sent as intent diagnose; GoalRun's host). */
  diagnose?: string;
  /** 个性化's 待确认的记忆 · 处理: sent as intent resolve_memory_conflicts (the turn reads his open groups first). */
  conflicts?: boolean;
  /** 标记为不会's 重讲这一节, sent as it was left: intent reteach_section (the turn loads the page-edit rule first). */
  reteach?: boolean;
  /** A turn read back from the session: how its message was sent; live turns carry `learn` / `diagnose` / `conflicts`. */
  intent?: "learn_goal" | "diagnose" | "resolve_memory_conflicts";
  /** His 摸底 grid sent with this message (MessageIn.reply_data: it answers the waiting ask_rough). */
  replyData?: ReplyData;
  /** Runs each time this turn's send ends (a 摸底 answer: its card says whether it went up). */
  afterRun?: () => void;
  /** The question the turn ended on (ask_user), in order; `askAt`: the answer's length when the first came (its card sits there). */
  asks: AskState[];
  askAt: number | null;
}
/**
 * A question the assistant asked (ask_user: the `ask` event). The turn ends on it and nothing waits on the server: his
 * next message answers it, typed in the composer or one of its options (sent as his message through the normal send).
 * `waiting` until that message goes out, then `answered` (the card keeps its question, the pills go; his words are the
 * next turn's bubble).
 */
export interface AskState {
  callId: string; kind: string; question: string; options: string[]; status: "waiting" | "answered";
  /** 学点新的's 细问 (ask_check): the block it is about (block_id / block_name); null when the payload names none. */
  block: { id: string; name: string } | null;
  /** 大体摸底 (ask_rough): the grid's blocks and his taps (kept here until 提交 sends them). */
  rough: RoughState | null;
  /** The diagnosis this card belongs to (ask_rough, ask_check); null: another kind of question. */
  draftId: string | null;
}
/** An ask event (or a restored AskCardOut) as the card's state; a payload of another kind shows nothing but its status. */
export function askOf(e: Pick<AskEvent, "call_id" | "kind" | "payload">, status: AskState["status"]): AskState {
  const p = e.payload;
  const question = typeof p.question === "string" ? p.question : "";
  const options = Array.isArray(p.options) ? p.options.filter((o): o is string => typeof o === "string") : [];
  const rough = e.kind === ROUGH_KIND ? { blocks: roughBlocks(p), marks: {}, sent: status === "answered" } : null;
  return { callId: e.call_id, kind: e.kind, question, options, status, block: e.kind === CHECK_KIND ? checkBlock(p) : null, rough,
    draftId: (e.kind === ROUGH_KIND || e.kind === CHECK_KIND) && typeof p.draft_id === "string" ? p.draft_id : null };
}
/** The question the conversation's last turn ended on, still unanswered (his next message answers it), else null. */
export function waitingAsk(turns: Turn[]): AskState | null {
  const t = turns[turns.length - 1];
  if (!t || t.status !== "done") return null;
  return t.asks.find((a) => a.status === "waiting") ?? null;
}
/** A new message went out: every question before it is answered (or dropped: the server treats it as a new request). */
export function closeAsks(turns: Turn[], sent: Turn): void {
  for (const t of turns) {
    if (t === sent) continue;
    let changed = false;
    for (const a of t.asks) if (a.status === "waiting") { a.status = "answered"; changed = true; }
    if (changed) t.rev++;
  }
}

/** The turn is 学点新的's 开始 (sent now, or read back from the session log with that intent). */
export const fromLearn = (t: Pick<Turn, "learn" | "intent">): boolean => t.learn !== undefined || t.intent === "learn_goal";
/** The turn is 待确认的记忆's 处理 (app.js startMemories: the chat opens on the assistant's first group, no bubble of his). */
export const fromConflicts = (t: Pick<Turn, "conflicts" | "intent">): boolean => t.conflicts === true || t.intent === "resolve_memory_conflicts";

/** The goal and background 学点新的's 开始 sends with its first message. */
export interface LearnGoal { goal: string; background: string }

export function newTurn(id: string, question: string, quote: Quote | null, attachments: TurnAttachment[] = []): Turn {
  return {
    id, rev: 0, question, quote, answer: "", tools: [], proposals: [], observed: null, actions: [], quizzes: [],
    status: "streaming", error: null, restored: null, interrupted: false, stopping: false, stopped: false, thinking: true, statusLine: "", statusSince: 0, sentAt: Date.now(), attachments,
    answerId: null, rating: { ...NO_RATING, chips: [] }, cardAt: null, asks: [], askAt: null,
  };
}

/**
 * Folds one event into the turn (mutates, bumps `rev` once). Events after done/error are ignored. A text_delta keeps
 * answer[:at] and appends its text (no `at`: appends); one that leaves the answer as it was (a live piece resent with its
 * committed text) changes nothing, not even `rev`, so the renderer does not rebuild.
 */
export function applyEvent(t: Turn, e: AssistantEvent): void {
  if (t.status !== "streaming") return;
  switch (e.type) {
    case "text_delta": {
      const at = e.at ?? t.answer.length;
      const next = t.answer.slice(0, at) + e.text;
      if (next === t.answer) return;
      t.answer = next;
      if (e.text !== "") { t.thinking = false; t.statusLine = ""; }
      break;
    }
    case "status": {
      // a long read says what it is waiting for (第一次要等约 2 分钟): on its own row, which is the line shown then
      const running = t.tools.filter((l) => l.status === "running" && l.label !== "").pop();
      if (running) running.label = e.text;
      else {
        t.statusLine = e.text;
        if (t.statusSince === 0) t.statusSince = Date.now();
      }
      t.thinking = true;
      break;
    }
    case "tool_start":
      t.tools.push({ callId: e.call_id, name: e.name, effect: e.effect, summary: e.summary ?? "", status: "running", sources: [],
        label: e.label ?? "", ...rowFields({}) });
      t.thinking = true;
      t.statusLine = "";
      t.statusSince = 0;
      break;
    case "tool_end": {
      const status = e.ok ? "ok" : "failed";
      const sources = e.sources ?? [];
      const line = t.tools.find((l) => l.callId === e.call_id);
      t.statusLine = "";
      t.statusSince = 0;
      if (line) Object.assign(line, { status, sources }, rowFields(e));
      // An end without a start stays visible as a finished row; its effect is unknown, so it gets no network badge.
      else t.tools.push({ callId: e.call_id, name: "", effect: "read", summary: "", status, sources, label: "", ...rowFields(e) });
      break;
    }
    case "proposal":
      t.statusLine = "";
      // A later draft of the same section in one turn takes the earlier one's place (assistant/loop.py _replace_proposal).
      if (e.draft.kind === "rewrite_section")
        t.proposals = t.proposals.filter((p) => !(p.status === "open" && p.draft.kind === "rewrite_section"
          && p.draft.page_id === e.draft.page_id && p.draft.section_id === e.draft.section_id));
      t.proposals.push({ proposalId: e.proposal_id, draft: e.draft, status: "open", applied: [], error: null, detail: "", change: e.change, actionId: "", canUndo: false, undoing: false, note: "" });
      break;
    case "action": {
      t.statusLine = "";
      const card = t.actions.find((a) => a.actionId === e.action_id);
      if (card) updateActionCard(card, e);
      else {
        if (t.actions.length === 0 && t.answer.trim() !== "") t.cardAt = t.answer.length;
        t.actions.push(newActionCard(e));
      }
      break;
    }
    case "ask":
      // the turn ends on it (done follows): no spinner, his next message answers it
      if (!t.asks.some((a) => a.callId === e.call_id)) {
        if (t.asks.length === 0) t.askAt = t.answer.length;
        t.asks.push(askOf(e, "waiting"));
      }
      t.thinking = false;
      t.statusLine = "";
      break;
    case "observed":
      t.observed = e;
      break;
    case "done":
      if (e.attachments.length > 0) t.attachments = mergeOut(t.attachments, e.attachments);
      t.answerId = e.message_id;
      t.status = "done";
      t.stopping = false;
      t.stopped = e.stopped === true;
      t.thinking = false;
      t.statusLine = "";
      break;
    case "error":
      t.status = "error";
      t.error = { kind: "event", code: e.code, retryable: e.retryable, ...(e.code === "llm_busy" || e.code === "refused" || e.code === "budget_exceeded" ? { detail: e.detail } : {}) };
      t.interrupted = e.interrupted === true && t.answer !== "";
      t.thinking = false;
      t.statusLine = "";
      break;
  }
  if (t.stopping) { t.thinking = false; t.statusLine = ""; }
  t.rev++;
}

export function failTurn(t: Turn, err: TurnError): void {
  // he stopped it and the stream went (a restart): the server ends it as stopped from its durable request
  if (t.stopping) { t.status = "done"; t.stopping = false; t.stopped = true; t.thinking = false; t.statusLine = ""; t.rev++; return; }
  t.status = "error";
  t.error = err;
  t.thinking = false;
  t.statusLine = "";
  t.rev++;
}

/** Clears everything the failed attempt produced; question and quote stay for the resend. */
export function resetForRetry(t: Turn): void {
  t.answer = "";
  t.tools = [];
  t.proposals = [];
  t.actions = [];
  t.quizzes = [];
  t.observed = null;
  t.answerId = null;
  t.rating = { ...NO_RATING, chips: [] };
  t.cardAt = null;
  t.asks = [];
  t.askAt = null;
  t.error = null;
  t.interrupted = false;
  t.thinking = true;
  t.statusLine = "";
  t.sentAt = Date.now();
  t.status = "streaming";
  t.rev++;
}

export type Phase = "create" | "message" | "decide" | "restore";

export function classifyError(e: unknown, phase: Phase): TurnError {
  if (e instanceof OfflineError) return { kind: "offline" };
  if (e instanceof AbortedError) return { kind: "aborted" };
  if (e instanceof ApiError) {
    // Every assistant route answers 503 while the server has no assistant wired (no model configured).
    if (e.status === 503) return { kind: "not_enabled" };
    if (e.status === 404 && phase === "create") return { kind: "unknown_page" };
    // 409 on a message: another turn or decision holds the session lock. The session is still valid.
    if (e.status === 409 && phase === "message") return { kind: "busy" };
    if ((e.status === 403 || e.status === 404) && (phase === "message" || phase === "restore")) return { kind: "session_gone" };
    return { kind: "http", status: e.status };
  }
  return { kind: "broken" };
}

/**
 * A message 409 is the session lock (`code` session_busy) or, for a message with attachments, an attachment that cannot
 * be sent (`code` attachment_refused; its detail is the text to show). The latter as its own error; null otherwise.
 */
export function attachmentRefusal(e: unknown, hasAttachments: boolean): TurnError | null {
  if (!hasAttachments || !(e instanceof ApiError) || e.status !== 409 || e.code !== "attachment_refused") return null;
  return { kind: "attachment", detail: e.detail.trim() };
}

export function canRetry(err: TurnError): boolean {
  switch (err.kind) {
    case "event": return err.retryable;
    case "offline": case "broken": case "session_gone": case "busy": return true;
    case "http": return err.status >= 500;
    case "not_enabled": case "aborted": case "unknown_page": case "attachment": return false;
  }
}

/**
 * Accept/reject buttons are enabled while the card is open, or after the request itself failed (`error`: nothing was
 * decided, the user can click again). A `failed` decision is final: the ledger decides once.
 */
export function cardOpen(c: ProposalCard): boolean {
  return c.status === "open" || c.status === "error";
}
