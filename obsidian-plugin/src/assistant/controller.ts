import { tr, onLanguageChange } from "../i18n";
/**
 * Reading-assistant conversations, one per note/page and identity. Nothing is sent on show, attach, typing or
 * switching notes; the question (and an attached quote) goes out only from send(). One click, at most one write.
 */
import { AbortedError, ApiError } from "../api/errors";
import type { AssistantApi, Decision } from "./client";
import type { ForgetPendingOut, ModelIn, ModelOut, ModelsOut } from "./types";
import type { ForgetDialog } from "./forget";
import { sessionKey, type ActiveNote, type AssistantSessionStore, type ConvTarget, type Identity, type Target } from "./session-store";
import { type ProposalCard,
  applyEvent, askOf, OPEN_REVIEW, reviewTurnOf, attachmentRefusal, closeAsks, canRetry, cardOpen, classifyError, failTurn, newTurn, ratingOf, resetForRetry, rowFields,
  waitingAsk, type LearnGoal, type Phase, type Quote, type Rating, type Turn,
} from "./turn";
import { ewNotice } from "../ui/ew-notice";
import { ACTION_COPY, ASSISTANT_COPY, attachErrorText, errorText, FORGET_COPY, historyReason, KNOWN_COPY, QUIZ_COPY, RATING_COPY } from "./copy";
import {
  applyProgress, applyUploaded, backToDraft, canSend, failDraft, fromDraft, fromOut, learning, newDraft,
  type AttachmentPort, type DraftAttachment, type TurnAttachment,
} from "./attachments";
import {
  addDraft, applyActionOut, renameDraft, cardPageIds, cardResult, cardRevise, cardPhase, cardSig, draftable, draftPayload, forward, isWriting, keptDraft, moveDraft, newActionCard, newQuiz,
  quizAnswered, quizNext, removeDraft, revertDraft, rowOf, updateActionCard, wholeDraft, type ActionCardState, type QuizState,
  LEDGER_STALE,
} from "./card-state";
import type { ActionOut } from "../wiki/bot/bot-schema";
import { type DraftDecisions, retryable } from "../wiki/revise/decisions";
import { GOAL_KIND, goalBlocks, goalResultOf, REPLACED, type GoalChatRow, type GoalResult, type PendingGoal } from "./goal-chat";
import { CHECK_KIND, roughReady, roughText } from "./diag-cards";
import type { LearnDiag, RoughMark } from "../wiki/learn/learn-view";
import { GoalRun, type FirstPage } from "./goal-run";
import type { GoalApi } from "../wiki/learn/goal-api";
import { hasNewerRevise, inPageDraft, REVISE_KIND } from "../wiki/revise/rows";
import {
  MAX_ATTACHMENTS, MAX_MESSAGE_CHARS, type ActionCard, type AssistantEvent, type MessageIn, type MessageOut,
  type SessionOut, type SessionRecapOut, type SessionSummaryOut,
} from "./types";
import type { StepOut, WriteOut } from "../wiki/schema";
import { sharedWritesFor, type WritesSnapshot } from "../wiki/writes-watcher";

export interface Conversation {
  key: string; target: ConvTarget; sessionId: string | null; turns: Turn[];
  draft: string; quote: Quote | null; busy: boolean;
  /** The quote is his selection in the note right now (follows it, goes when it goes); not set for one he asked for from the menu. */
  quoteLive?: boolean;
  /** 标记为不会 left this in the composer; sent as it is, it is sent as intent reteach_section. */
  reteachDraft?: string;
  /** Settles when the running turn ends (`busy`). */
  running?: Promise<void>;
  /** He stopped the turn and the server has not ended it yet: the composer is free (`busy` false), a message sent now waits for this. */
  ending?: Promise<void> | null;
  /** The composer's attachments (they go with the next message; a new conversation keeps them, as the draft). */
  attachments: DraftAttachment[];
  restore: "none" | "loading" | "failed" | "gone"; recap: SessionRecapOut | null;
}
/**
 * The 历史对话 list (shared by every panel). `status` is loading until the first answer for the current `q`; an error is
 * shown as an error (never as an empty list). `notice` is a failed resume: the current conversation is kept.
 */
export interface HistoryState {
  open: boolean; q: string; status: "loading" | "ready" | "error"; items: SessionSummaryOut[]; error: string; notice: string;
  /** The conversations being deleted (client.ts forgetRequests; they are not in `items`): 「删除中」 rows, never opened.
   *  `title` / `at`: the row's title and last activity when it was last listed here ("" / the request time since a reload). */
  deleting: (ForgetPendingOut & { title: string; at: string })[];
}
export interface AssistantState {
  configured: boolean; active: ActiveNote; conv: Conversation | null; focusTick: number; history: HistoryState;
  /** Moves with 今天's review round: the chat's review card is drawn again. */
  reviewTick?: number;
  /** The composer takes files (an attachments port is wired). */
  canAttach: boolean;
  /** Where the pages of the shown cards are being written (GET /wiki/writes); the renderer paints the status in place. */
  writes: ReadonlyMap<string, WriteOut>;
  /** The pages whose writing status was read and need no further asking: one in it with no entry in `writes` has no run (finished earlier). */
  written?: ReadonlySet<string>;
  /** The composer's model picker: the server's models and the current one; `open` = its menu shows. Absent / null: no picker. */
  models?: ModelsView | null;
  /** The composer offers 停止: a first page this chat asked for is being written (and can be stopped). */
  canStop?: boolean;
  /** 删除对话's dialog (one at a time); null: closed. */
  forget?: ForgetDialog | null;
}
export interface ModelsView { list: readonly ModelOut[]; current: string | null; reasoning: string | null; open: boolean; pending?: boolean }

export interface AssistantDeps {
  api: AssistantApi; store: AssistantSessionStore; identity: () => Identity; newId: () => string;
  /** Called once per accepted proposal with the edited page and the section ids written (E3b wires its note sync). */
  pageEdited?: (pageId: string, applied: string[]) => void;
  /** Before a card changes a page: what he typed in its note and the server has not seen goes up first (false: it could not). */
  handEditFirst?: (pageId: string) => Promise<boolean>;
  /** Chat attachments (upload / progress); without it the composer takes no files. */
  attachments?: AttachmentPort;
  /** A citation in an answer: the original at its PDF page (main.ts: the wiki's 资料). */
  openCitation?: (sourceId: string, page: number) => Promise<"opened" | "gone" | "failed">;
  /** A card's 确认 / 取消 / 撤销 changed a ledger row: 个性化 re-reads it (main.ts wires the wiki controller's reload). */
  ledgerChanged?: () => void;
  /** A confirmed forget finished: reconcile queued evidence against current server visibility. */
  memoryForgotten?: () => void;
  /**
   * An in-page draft's 确认替换 / 取消 is one decision with the page's bar (wiki/revise/decisions.ts): the card claims it
   * there, and follows a decision the bar made (busy, 请重试, decided) at once.
   */
  decisions?: DraftDecisions;
  /** A turn ended: what it wrote about the learner shows in the profile note 「我」 (main.ts wires its sync). */
  turnDone?: () => void;
  /** The wait between resumes of a broken stream (tests); default setTimeout. */
  sleep?: (ms: number) => Promise<void>;
  /**
   * A goal confirmed in the chat goes on in the chat (goal-run.ts): the big-goal routes and 学点新的's 生成第一页. Absent: the
   * chat shows no diagnosis (a goal started from 学点新的's 开始 is driven there either way).
   */
  /**
   * 查看详情's destinations (main.ts): a page (at a section), a goal's run in 学点新的, 个性化 · 管理偏好. Absent: 查看详情 falls
   * back to the action's row in 个性化 · 活动记录.
   */
  results?: { openPage(pageId: string, sectionId: string | null): void; openGoal(actionId: string, mapId: string | null): void; openRules(): void; openReview(pageId: string | null): void };
  goals?: {
    api: () => GoalApi;
    /** 学点新的's 生成第一页; `onPage`: the page's id once known, before its note opens. */
    writeFirstPage: (target: FirstPage, onPage?: (pageId: string) => void) => Promise<void>;
    pageFromGoal?: (pageId: string, mapId: string) => void;
    /**
     * The goals run here (key: the goal_new row's id; value: its draft / map and title): a reloaded chat rebuilds only
     * these, where GET /wiki/progress has them. Absent: nothing comes back after a reload.
     */
    kept?: AssistantSessionStore;
    /** The goal 学点新的 showed last (its goal_new row): after a reload it comes back. */
    lastGoal?: { load(): string | null; save(actionId: string): void };
    /** A chat goal run changed (its goal made, a round, a block entered, a page started or written): 今天 reads GET /wiki/progress again. */
    changed?: () => void;
    /** The page last stopped / 重新写 (main.ts). */
    stoppedWrite?: () => string | null; restartWrite?: (pageId: string) => Promise<void>;
    /** 停止 on the chat's writing card: page `pageId` stopped (the wiki controller's stream, else its server run). */
    stopPage?: (pageId: string) => Promise<boolean>;
  };
  /** The wiki's writes watcher's last read of the live writes (GET /wiki/writes), so the cards do not poll a second time. */
  sharedWrites?: () => WritesSnapshot | null;
}

/** What a chat diagnosis keeps (plugin data goalRuns): where it is, and the goal's ids to rebuild it after a reload. */
interface KeptRun { draftId: string | null; mapId: string | null; sessionId?: string | null; target?: ConvTarget }

/** A material read in 学点新的, sent with its goal to the chat. */
export interface GoalMaterial { sourceId: string; title: string; kind: string; pages: number | null }

/** 待确认的记忆 · 处理's message (its text is the chat's title; the chat shows no bubble for it). */
export let CONFLICTS_MESSAGE = tr("wiki.memory-conflicts.CONFLICT_COPY.chatTitle.6");
onLanguageChange(() => { CONFLICTS_MESSAGE = tr("wiki.memory-conflicts.CONFLICT_COPY.chatTitle.6"); });
/** Typed in the chat while its diagnosis waits for an answer, these skip it as the card's 「跳过，直接开始」 does. */
export const GOAL_SKIP_WORDS = ["跳过", "直接开始", "跳过，直接开始", "跳过,直接开始", "跳过直接开始", "skip", "start now", "skip and start", "skip, start now"];
/** The typed message, as GOAL_SKIP_WORDS lists it: trimmed, one trailing 。/！/!/. dropped. */
export const isGoalSkip = (text: string): boolean => GOAL_SKIP_WORDS.includes(text.trim().replace(/[。！!.]$/, "").trim().toLowerCase());
/** While a visible card's page is queued / writing, /wiki/writes is read this often (ms). */
export const WRITES_POLL_MS = 1100;
const UNWRITTEN_POLL_MS = 10_000;
/** After a failed read of /wiki/writes the next try comes this late (ms). */
const WRITES_RETRY_MS = 5000;
const STOP_RETRY_MS = 300;
/** A broken stream is resumed again after each of these waits (ms; about 60 s in all: a backend restart takes that long to be back). */
export const RESUME_WAITS_MS: readonly number[] = [1000, 2000, 4000, 8000, 15000, 15000, 15000];
/** A page a card just made may have no run for a moment (the request is queued after the commit): it is asked for this many times. */
const WRITES_MISSES = 10;

/**
 * The MessageIn for a turn: the question, plus the quote only if the user attached one to this send, plus its attachments,
 * plus `pageId` (the EvoWiki page whose note is open as he sends, in any conversation: 「这页」 is that page).
 */
function selectionOf(q: Quote): NonNullable<MessageIn["selection"]> {
  return { kind: q.kind ?? "text", text: q.text, ...(q.heading ? { heading: q.heading } : {}), ...(q.artifactId ? { artifact_id: q.artifactId } : {}) };
}

function messageBody(turn: Turn, pageId: string | null): MessageIn {
  const quote = turn.quote;
  const body: MessageIn = !quote ? { text: turn.question }
    : { text: turn.question, selection: selectionOf(quote) };
  if (pageId !== null) body.page_id = pageId;
  if (turn.attachments.length > 0) body.attachments = turn.attachments.map((a) => a.sourceId);
  // 开始's goal: the server runs new_goal with it itself (no model decides whether to)
  if (turn.learn) { body.intent = "learn_goal"; body.goal = turn.learn.goal; }
  if (turn.diagnose) { body.intent = "diagnose"; body.draft_id = turn.diagnose; }
  if (turn.conflicts) body.intent = "resolve_memory_conflicts";
  if (turn.reteach) body.intent = "reteach_section";
  // the 摸底 grid: his marks answer the waiting ask_rough (the text is his readable line)
  if (turn.replyData) body.reply_data = turn.replyData;
  return body;
}

/** A file's bytes (Blob.arrayBuffer where there is one; FileReader otherwise, e.g. older DOM shims). */
function bytesOf(f: Blob): Promise<ArrayBuffer> {
  if (typeof f.arrayBuffer === "function") return f.arrayBuffer();
  return new Promise((ok, fail) => {
    const r = new FileReader();
    r.onload = () => { ok(r.result as ArrayBuffer); };
    r.onerror = () => { fail(r.error ?? new Error("could not read the file")); };
    r.readAsArrayBuffer(f);
  });
}

/** An object URL for an image's thumbnail (none where the environment has no object URLs). */
const thumbOf = (f: File): string | undefined =>
  (f.type.startsWith("image/") && typeof URL.createObjectURL === "function" ? URL.createObjectURL(f) : undefined);

/** Text that does not fit in one message, as a plain-text attachment (the server reads .txt whole). */
const textFile = (text: string, name: string): File => new File([text], name, { type: "text/plain" });

/** The ledger kind of 「这个我早会了」, and the quiz id prefix of its one confirm question (not a chat quiz). */
const KNOWN_KIND = "node_known";
const KNOWN_QUIZ = "known:";

/** The id prefix of the server's 「更早的 N 轮对话已省略」 notice (assistant/session.py fold): a notice, not an answer. */
const OMIT_PREFIX = "omit_";

const FREE: ConvTarget = { kind: "free" };
/** How far back a goal kept without its session is looked for (once: then it is kept). */
const RUN_SEARCH_SESSIONS = 30;
/** The id of a run of a goal started elsewhere (another vault, an earlier install), by its map: it has no chat card. */
const ELSEWHERE_RUN = "map:";
/**
 * The conversation an active note has: its own (note / page), else the one without a page (no note, or a plain note that
 * is linked to no concept: chat always works).
 */
const targetOf = (a: ActiveNote): ConvTarget => (a.kind === "page" || a.kind === "note" ? a : FREE);
const sameActive = (a: ActiveNote, b: ActiveNote): boolean => JSON.stringify(a) === JSON.stringify(b);

export class AssistantController {
  /** Materials the stored answers cite that are deleted or cleaned up by now (GET session), and one a click found gone. */
  private readonly goneSources = new Set<string>();

  private noteGone(ids: readonly string[] | undefined): void { for (const id of ids ?? []) this.goneSources.add(id); }

  citationGone(id: string): boolean { return this.goneSources.has(id); }

  async openCitation(id: string, page: number): Promise<"opened" | "gone" | "failed"> {
    const r = await (this.deps.openCitation?.(id, page) ?? Promise.resolve("failed" as const));
    if (r === "gone") this.goneSources.add(id);
    return r;
  }

  private readonly convs = new Map<string, Conversation>();
  /**
   * Conversations whose turn was still running when the learner opened another one (＋, a history row, 开始): the turn goes
   * on in the background (ChatGPT-like) and lands in its own session; a history row for that session takes it back.
   */
  private readonly background = new Set<Conversation>();
  /** The chat diagnoses of goals confirmed in the chat, by the goal_new row. */
  private readonly goalRuns = new Map<string, GoalRun>();
  /** The goal run last started, restored or continued here (学点新的 shows it). */
  private lastRun: string | null = null;
  /**
   * The first page a chat diagnosis is writing, while the 助手 shows its conversation: that page opening (and being re-reported)
   * keeps the 助手 on the goal's conversation, where the run's 「第一页写好了」 link is. Any other note shown afterwards is
   * followed as usual (and ends the hold).
   */
  /** The EvoWiki page whose note is open (the last show); null: none. Sent with every message (MessageIn.page_id). */
  private viewing: string | null = null;
  private heldPage: string | null = null;
  /**
   * Conversations made with a stored session id that was never read (conv() makes the conversation without a page before
   * any show, e.g. right after a plugin reload): the next show or its history row reads it (it used to stay empty, and
   * its history row did nothing).
   */
  private readonly unread = new WeakSet<Conversation>();
  private readonly subs = new Set<() => void>();
  private readonly abort = new AbortController();
  private active: ActiveNote = { kind: "none" };
  /** A goal's first turn is running (startGoalChat): its run has no id yet, nothing may bring back the goal before it. */
  private goalStarting = 0;
  /** The selection whose live chip he took off or already sent. */
  private selectionDone: string | null = null;
  private reviewTick = 0;
  /** A quote asked about another page than the open note (反馈 on a review card): the panel keeps that conversation
   *  while it re-reports this same note; opening any other note follows it again. */
  private heldFrom: ActiveNote | null = null;
  /** 学点新的 · 开始 before the panel had reported any note: its first report (the note still open behind the 学点新的 tab,
   *  read when the 助手 tab opens) is held too, like heldFrom; any later, different note is followed again. */
  private holdFirst = false;
  private focusTick = 0;
  private history: HistoryState = { open: false, q: "", status: "loading", items: [], error: "", notice: "", deleting: [] };
  /** The conversations listed here, by id (a 「删除中」 row is no longer listed by the server: its title and place come from here). */
  private readonly titles = new Map<string, SessionSummaryOut>();
  private forgetDialog: ForgetDialog | null = null;
  /** Only the newest list request / resume is applied. */
  private listSeq = 0;
  private resumeSeq = 0;
  /** One session create per conversation at a time (a turn and attachments added together share it). */
  private readonly opening = new Map<Conversation, Promise<string>>();
  /** Progress streams: a draft attachment's (by localId) and the import a use from the chat started (by "use:" + source id). */
  private readonly follows = new Map<string, AbortController>();
  /** Restored questions with no answer yet: their user message id (a still running durable turn is followed again by it). */
  private readonly unanswered = new WeakMap<Turn, string>();
  private readonly writes = new Map<string, WriteOut>();
  /** Page ids whose status was read at least once (one read even when nothing is writing). */
  private readonly written = new Set<string>();
  private poll: ReturnType<typeof setTimeout> | null = null;
  /** Pages a card made in this session: asked for until they have a run, then until the run ends. */
  private readonly fresh = new Set<string>();
  private readonly misses = new Map<string, number>();
  /** Whether a panel showing the conversation is on screen (none registered: assumed so). */
  private readonly probes = new Set<() => boolean>();
  /** Card ids whose 改动对比 was asked for. */
  private readonly diffAsked = new Set<string>();
  /** Per answer: the rating the server last confirmed, and the posts in flight (sent one after another, in click order). */
  private readonly rated = new WeakMap<Turn, { good: Rating; chain: Promise<void>; pending: number }>();
  /** 学点新的 follows the goal_new rows (onGoalRow). */
  private readonly goalSubs = new Set<(row: GoalChatRow) => void>();
  /** Per goal draft card: its kept edits in flight, one after another, and how many edits were made (the newest wins). */
  private readonly keeping = new Map<string, { chain: Promise<void>; seq: number }>();

  /** The composer's model picker (the models route, client.ts): null until read (then no picker shows). */
  private models: ModelsOut | null = null;
  private modelsOpen = false;
  /** Only the newest models read / pick is applied. */
  private modelsSeq = 0;
  private modelsLoading = false;
  private modelPicking = false;

  constructor(private readonly deps: AssistantDeps) {
    deps.decisions?.subscribe((id) => { this.followDecision(id); });
  }

  /** In-page drafts this card is deciding itself (its own request is out): the shared decision then changes nothing here. */
  private readonly deciding = new Set<string>();

  /** The page's bar decided (or is deciding) an in-page draft: its card shows the same, at once. */
  private followDecision(actionId: string): void {
    const found = this.cardOf(this.conv(), actionId);
    if (!found || this.deciding.has(actionId) || !inPageDraft(found.card.kind)) return;
    const { card, turn } = found, d = this.deps.decisions!.get(actionId);
    // 重试 also goes for a draft whose 确认 already failed because the page moved
    if (cardPhase(card.status) !== "wait" && d.busy !== "redraft" && d.failed !== "redraft") return;
    card.busy = d.busy; card.retry = d.failed; card.error = "";
    this.changed(turn);
    if (d.settled !== null) this.syncLedger([d.settled]);
  }

  get state(): AssistantState {
    return { configured: this.configured(), active: this.active, conv: this.conv(), focusTick: this.focusTick, history: this.history, reviewTick: this.reviewTick, writes: this.writes, written: new Set([...this.written].filter((id) => !this.pending(id))), canAttach: this.deps.attachments !== undefined,
      models: this.models && this.configured() ? { list: this.models.models, current: this.models.current, reasoning: this.models.reasoning, open: this.modelsOpen, pending: this.modelPicking } : null,
      canStop: this.canStop(), forget: this.forgetDialog };
  }

  /** Reads the picker's models (each open re-reads them: a key topped up or refused since shows as it is now). */
  async loadModels(): Promise<void> {
    const api = this.deps.api;
    if (!this.live() || !api.listModels || this.modelPicking) return;
    const seq = ++this.modelsSeq;
    this.modelsLoading = true;
    try {
      const out = await api.listModels();
      if (seq === this.modelsSeq && this.live()) { this.models = out; this.notify(); }
    } catch {
      if (seq === this.modelsSeq && this.live()) ewNotice(tr("runtime.modelsFailed"));
    } finally {
      if (seq === this.modelsSeq) this.modelsLoading = false;
    }
  }

  /** The picker's menu: open (re-reading the models) or closed; no argument toggles. */
  toggleModels(open?: boolean): void {
    const next = open ?? !this.modelsOpen;
    if (next === this.modelsOpen) return;
    this.modelsOpen = next;
    this.notify();
    if (next) void this.loadModels();
  }

  /** A model or a thinking level picked in the menu: the next turn uses it. A refused pick says why and keeps what was. */
  async pickModel(pick: ModelIn): Promise<void> {
    const api = this.deps.api;
    if (this.modelPicking) return;
    this.modelsOpen = false;
    this.notify();
    const m = this.models;
    const same = (pick.model === undefined || pick.model === null || pick.model === m?.current)
      && (pick.reasoning === undefined || pick.reasoning === null || pick.reasoning === m?.reasoning);
    if (!this.live() || !api.pickModel || same) return;
    const chosen = m?.models.find((x) => x.id === (pick.model ?? m.current));
    if (!chosen?.available || (pick.reasoning != null && !chosen.levels.some((l) => l.reasoning === pick.reasoning))) return;
    const seq = ++this.modelsSeq;
    this.modelPicking = true;
    this.modelsLoading = false;
    this.notify();
    try {
      const out = await api.pickModel(pick);
      if (seq === this.modelsSeq && this.live()) { this.models = out; this.notify(); }
    } catch (e) {
      if (seq === this.modelsSeq && this.live()) ewNotice(e instanceof ApiError && e.status === 409 && e.detail.trim() !== "" ? e.detail : ASSISTANT_COPY.modelPickFailed);
    } finally {
      if (seq === this.modelsSeq) { this.modelPicking = false; this.notify(); }
    }
  }

  subscribe(fn: () => void): () => void {
    this.subs.add(fn);
    return () => { this.subs.delete(fn); };
  }

  /** Every goal_new card the chat shows or changes (学点新的 follows the conversation its 开始 started). */
  onGoalRow(fn: (row: GoalChatRow) => void): () => void {
    this.goalSubs.add(fn);
    return () => { this.goalSubs.delete(fn); };
  }

  /** The unconfirmed goal kept in the free conversation, before a goal run exists. Reads only. */
  async pendingGoal(): Promise<PendingGoal | null> {
    if (!this.live()) return null;
    const key = sessionKey(this.deps.identity(), FREE);
    const c = this.convs.get(key) ?? this.create(key, FREE);
    if (this.unread.has(c)) await this.restoreConv(c);
    else if (c.restore === "loading") await new Promise<void>((resolve) => {
      const stop = this.subscribe(() => { if (c.restore !== "loading") { stop(); resolve(); } });
    });
    if (!this.live() || c.restore !== "none") return null;
    for (const turn of [...c.turns].reverse()) for (const card of [...turn.actions].reverse()) {
      if (card.kind !== GOAL_KIND || cardPhase(card.status) !== "wait") continue;
      const opening = c.turns.find((t) => t.learn !== undefined || t.intent === "learn_goal");
      const goal = opening?.learn?.goal ?? opening?.question ?? turn.question;
      return { goal, row: { key: c.key, actionId: card.actionId, phase: "wait", replaced: false, title: card.goalTitle ?? "", blocks: goalBlocks(card), result: null } };
    }
    return null;
  }

  private goalRow(c: Conversation, card: ActionCardState, out?: ActionOut): void {
    if (out !== undefined) this.deps.goals?.changed?.();
    this.runGoal(c, card, out);
    if (card.kind !== GOAL_KIND || this.goalSubs.size === 0) return;
    const phase = cardPhase(card.status);
    const row: GoalChatRow = { key: c.key, actionId: card.actionId, phase, replaced: phase === "rejected" && card.detail === REPLACED,
      title: card.goalTitle ?? "", blocks: goalBlocks(card), result: out !== undefined ? goalResultOf(out) : null };
    for (const fn of [...this.goalSubs]) {
      try { fn(row); } catch (e) { console.warn("EvoWiki assistant: goal row hook failed", e); }
    }
  }

  /**
   * A goal_new row executed (确认 here or on its page): its diagnosis goes on under the card (goal-run.ts), whichever way the
   * goal was started (学点新的's 开始 sends it here too: one flow). Once per row; never for a row only read back from a
   * stored session (no `out`).
   */
  private runGoal(c: Conversation, card: ActionCardState, out?: ActionOut): void {
    const goals = this.deps.goals;
    if (!goals || card.kind !== GOAL_KIND || out === undefined || cardPhase(card.status) !== "live" || this.goalRuns.has(card.actionId)) return;
    const result = goalResultOf(out);
    if (!this.cardOf(c, card.actionId) || result === null) return;
    const run = this.newGoalRun(card.actionId, result, true);
    this.keepRun(card.actionId, { draftId: result.draftId, mapId: result.mapId, sessionId: c.sessionId, target: c.target });
    void run.start();
  }

  private newGoalRun(id: string, result: GoalResult, auto: boolean): GoalRun {
    const goals = this.deps.goals!;
    const run = new GoalRun({
      api: goals.api(),
      ...(goals.pageFromGoal ? { pageFromGoal: goals.pageFromGoal } : {}),
      // the page it opens keeps the 助手 on the conversation that shows the run
      writeFirstPage: (t, onPage) => goals.writeFirstPage(t, (pageId) => { if (this.runShown(id)) this.heldPage = pageId; onPage?.(pageId); }),
      changed: () => { this.keepMap(id); this.runChanged(id); },
      ...(goals.stoppedWrite ? { stoppedWrite: goals.stoppedWrite } : {}), ...(goals.restartWrite ? { restartWrite: goals.restartWrite } : {}),
      ...(goals.stopPage ? { stopPage: goals.stopPage } : {}),
      diagnose: (draftId, back) => this.diagnoseIn(id, draftId, back === true),
    }, id, result, auto);
    this.goalRuns.set(id, run);
    // the current goal: one the learner drives here (a conversation read back only restores its card)
    if (auto) this.lastRun = id;
    return run;
  }

  /**
   * GoalRun's diagnosis in the chat: one message (intent diagnose, on run `id`'s draft) in the conversation that shows run `id`'s card; its
   * turn asks the questions (ask cards). Resolves when the turn has ended. After a reload no conversation shows the run yet:
   * its own is read back, or a new one takes over (`runChat`); a read that fails rejects (the run's line, 重试 asks again).
   * 确认 clicked while a turn of that conversation still runs (the card shows before its tail ends): it waits for that turn.
   */
  private async diagnoseIn(id: string, draftId: string, back = false): Promise<boolean> {
    const c = await this.runChat(id);
    while (c.busy) await c.running;
    if (!this.live()) return true;
    const card = this.cardOf(c, id);
    if (back) {
      // the grid he left still waits here, his taps on it
      if (waitingAsk(c.turns)?.rough) return true;
    // read back after a reload: the diagnosis already went on under its card, nothing is sent again
    } else if (card !== null && c.turns.slice(c.turns.indexOf(card.turn) + 1).some((t) => t.diagnose === draftId)) return true;
    const turn = newTurn(this.deps.newId(), !back && card !== null ? ASSISTANT_COPY.diagnoseStart : ASSISTANT_COPY.diagnoseResume, null);
    turn.diagnose = draftId;
    c.turns.push(turn);
    await this.run(c, turn);
    if (c.sessionId !== null && this.cardOf(c, id) === null) {
      // a conversation that took over: a reload finds the goal here from now on
      const kept = this.keptRun(id) ?? { draftId, mapId: this.goalRuns.get(id)?.mapId ?? null };
      this.keepRun(id, { ...kept, sessionId: c.sessionId, target: c.target });
    }
    return turn.status !== "done" || waitingAsk(c.turns) !== null;     // false: the turn ended asking nothing
  }

  /** Every conversation's turn that shows run `id` redraws (a run restored for 学点新的 may have none yet). */
  private runChanged(id: string): void {
    this.deps.goals?.changed?.();
    for (const c of [...this.convs.values(), ...this.background]) {
      const f = this.cardOf(c, id);
      if (f) f.turn.rev++;
    }
    this.notify();
  }
  private runShown(id: string): boolean { const c = this.conv(); return c !== null && this.cardOf(c, id) !== null; }

  /** A run that learned its map (its opening diagnosis closed): kept with it, so a reload finds it in GET /wiki/progress. */
  private keepMap(id: string): void {
    const mapId = this.goalRuns.get(id)?.mapId ?? null, kept = this.keptRun(id);
    if (mapId !== null && kept !== null && kept.mapId !== mapId) this.keepRun(id, { ...kept, mapId });
  }

  private keepRun(id: string, kept: KeptRun): void {
    void this.deps.goals?.kept?.set(this.keptKey(id), JSON.stringify(kept))
      .catch((e: unknown) => console.warn("EvoWiki assistant: could not keep the goal run", e));
  }
  private keptRun(id: string): KeptRun | null {
    const raw = this.deps.goals?.kept?.get(this.keptKey(id)) ?? null;
    if (raw === null) return null;
    try { return JSON.parse(raw) as KeptRun; } catch { return null; }
  }

  /**
   * A conversation read back (a plugin reload, a history row): each executed goal_new card gets its run back where the
   * server has it (its result's draft / map, then GET draft / map / path) (GoalRun.restore: the open round, the block entered, the first
   * page writing or its link). It never enters or writes by itself. A run 学点新的 already restored is the same run.
   */
  private restoreGoals(c: Conversation, out: SessionOut): void {
    if (!this.deps.goals) return;
    const stored = new Map((out.actions ?? []).map((a) => [a.action_id, a]));
    for (const turn of c.turns) {
      for (const card of turn.actions) {
        if (card.kind !== GOAL_KIND || cardPhase(card.status) !== "live" || this.goalRuns.has(card.actionId)) continue;
        const a = stored.get(card.actionId);
        const result = a ? goalResultOf({ result: a.result ?? {} }) : null;
        if (result === null) continue;
        void this.newGoalRun(card.actionId, result, false).restore();
      }
    }
  }

  private savedLast: string | null = null;
  /** The goal 学点新的 shows: the run last started or restored here. Null: none yet (after a reload: `resumeGoal`). */
  currentGoal(): GoalRun | null {
    if (this.lastRun !== null && this.lastRun !== this.savedLast) { this.savedLast = this.lastRun; this.deps.goals?.lastGoal?.save(this.lastRun); }
    return this.lastRun !== null ? this.goalRuns.get(this.lastRun) ?? null : null;
  }

  /**
   * After a reload, the goal the learner is on: GET /wiki/progress's `today` names it, and the run kept here for it (this
   * server and user) is restored where the server has it (its conversation is read when it is shown). Its run is the
   * current one from then on; a goal kept nowhere here (started in another vault) gets a run of its own with no chat
   * card. Null: no goal in progress, or progress unreadable (学点新的 then starts a new goal).
   */
  async resumeGoal(): Promise<GoalRun | null> {
    const now = this.currentGoal();
    if (now !== null) return now;
    const api = this.deps.goals?.api();
    const kept = this.deps.goals?.kept;
    if (!api?.progress || !this.live()) return null;
    let today: StepOut;
    try { today = (await api.progress()).today; } catch (e) { console.warn("EvoWiki assistant: could not read the progress", e); return null; }
    if (this.goalStarting > 0) return null;        // he started a new goal meanwhile: the chat stays on it
    const me = this.deps.identity();
    const last = this.deps.goals?.lastGoal?.load() ?? null;
    const mine = (kept?.keys?.() ?? []).flatMap((k) => {
      try { const v = JSON.parse(k) as unknown; return Array.isArray(v) && v[0] === me.server && v[1] === me.user && typeof v[2] === "string" ? [v[2]] : []; }
      catch { return []; }
    }).flatMap((id) => { const r = this.keptRun(id); return r !== null ? [{ id, ...r }] : []; });
    const hit = mine.find((r) => r.id === last)
      ?? mine.find((r) => (r.mapId !== null && r.mapId === today.map_id) || (r.draftId !== null && r.draftId === today.draft_id));
    if (this.currentGoal() !== null) return this.currentGoal();
    if (hit === undefined) {
      // a goal started elsewhere (another vault, an earlier install): its run without a chat card, from the server
      if (today.map_id === null) return null;
      const id = `${ELSEWHERE_RUN}${today.map_id}`;
      const run = this.goalRuns.get(id) ?? this.newGoalRun(id, { draftId: today.draft_id, mapId: today.map_id }, false);
      this.lastRun = id;
      if (run.view().kind === "idle" && run.flow.state.map === null) void run.restore();
      return run;
    }
    const shown = this.goalRuns.get(hit.id);       // its conversation was read back first: that run, already restored
    if (shown) { this.lastRun = hit.id; return shown; }
    const run = this.newGoalRun(hit.id, { draftId: hit.draftId, mapId: hit.mapId }, false);
    this.lastRun = hit.id;
    void run.restore();
    return run;
  }

  /** Open any saved goal's learning path without starting generation. */
  async openGoal(mapId: string): Promise<GoalRun | null> {
    if (!this.live() || !this.deps.goals) return null;
    const existing = [...this.goalRuns.values()].find((run) => run.mapId === mapId);
    const run = existing ?? this.keptRunOf(mapId)
      ?? this.newGoalRun(`${ELSEWHERE_RUN}${mapId}`, { draftId: null, mapId }, false);
    this.lastRun = run.actionId;
    if (existing === undefined || run.flow.state.map === null) await run.restore();
    const view = run.view();
    if (view.kind === "failed" && run.flow.state.map === null) throw new Error(view.text);
    return run;
  }

  /** A goal of map `mapId` kept here (this server and user), rebuilt (not restored: the caller drives it). Null: none. */
  private keptRunOf(mapId: string): GoalRun | null {
    const kept = this.deps.goals?.kept;
    if (!kept?.keys || !this.live()) return null;
    const me = this.deps.identity();
    for (const k of [...kept.keys()].reverse()) {
      let v: unknown;
      try { v = JSON.parse(k); } catch { continue; }
      if (!Array.isArray(v) || v[0] !== me.server || v[1] !== me.user || typeof v[2] !== "string") continue;
      const r = this.keptRun(v[2]);
      if (r !== null && r.mapId === mapId) return this.newGoalRun(v[2], { draftId: r.draftId, mapId }, false);
    }
    return null;
  }

  /**
   * The next block of a goal (a block's last page read, 学点新的's 进入, 今天's 下一块): the run of that map goes on in its
   * conversation — the block entered, its diagnosis in the chat when it needs one, its first page written. False: no run of
   * that map here (the goal was not started in this vault).
   */
  continueGoal(mapId: string, region: string | null = null): boolean {
    const mine = (r: GoalRun | null): boolean => r !== null && (r.flow.state.map?.map_id ?? r.mapId) === mapId;
    const run = [...this.goalRuns.values()].reverse().find(mine) ?? this.keptRunOf(mapId);
    if (!run) return false;
    this.lastRun = run.actionId;
    this.showRun(run.actionId);
    void run.enterBlock(region);
    return true;
  }

  /** The 助手 shows run `id`'s conversation (`runChat`). A read that fails says so in the history panel's line. */
  private showRun(id: string): void {
    void this.runChat(id).catch((e: unknown) => { console.warn("EvoWiki assistant: could not read the goal's conversation", e); });
  }

  /** 学点新的 came to the front on goal run `id`: the 助手 shows that goal's conversation. A goal started elsewhere has none to show. */
  showGoalChat(id: string): void {
    if (this.live() && !id.startsWith(ELSEWHERE_RUN)) this.showRun(id);
  }

  /** He went from 学点新的 back to a note: the 助手 follows the note again (the panel reports it next). */
  followNotes(): void { this.heldFrom = null; this.holdFirst = false; }

  /** Shows target `t`'s conversation, kept while the panel re-reports the note that is open, as startGoalChat does. */
  private showHeld(t: ConvTarget): void {
    const prev = this.active;
    this.holdFirst = false;
    this.show(t.kind === "free" ? { kind: "none" } : t);
    if (prev.kind !== "none") this.heldFrom = prev;
    else this.holdFirst = true;
  }

  /**
   * The conversation of run `id`, shown in the 助手 (the one without a page, or the page's it was confirmed in), so what the
   * goal says next lands where its card is. In memory; else the session kept with the run, read back; else, a run kept
   * before sessions were, found in the history once. No session left (deleted): a new conversation, and it says so: a goal
   * never goes on silently nowhere. A read that fails rejects, and nothing is replaced.
   */
  private async runChat(id: string): Promise<Conversation> {
    const has = (x: Conversation | null): x is Conversation => x !== null && this.cardOf(x, id) !== null;
    const here = [...this.convs.values()].find(has);
    if (here) { if (this.conv() !== here) this.showHeld(here.target); return here; }
    const kept = this.keptRun(id);
    const target = kept?.target ?? FREE;
    const sid = kept?.sessionId ?? await this.sessionOfRun(id);
    if (sid !== null) {
      this.showHeld(target);
      const read = await this.resume(sid);
      if (read === "failed") throw new Error(`goal conversation ${sid} could not be read`);
      const c = this.conv();
      if (read === "ok" && c) {
        if (kept && kept.sessionId !== sid) this.keepRun(id, { ...kept, sessionId: sid, target });
        return c;
      }
    }
    this.showHeld(FREE);
    let c = this.conv();
    if (!c) throw new Error("no conversation to go on in");
    if (c.busy || c.ending) c = this.detach(c);
    const forgot = c.turns.length > 0 || c.sessionId !== null || c.restore === "loading";
    if (forgot) { this.clear(c); await this.save(c.key, null); }
    this.history = { ...this.history, open: false, notice: "" };
    ewNotice(ASSISTANT_COPY.goalChatGone);
    this.notify();
    return c;
  }

  /** The session whose stored cards carry goal row `id` (his latest sessions, newest first); null: none. */
  private async sessionOfRun(id: string): Promise<string | null> {
    // its conversation was found gone earlier, or the goal was started elsewhere (resumeGoal): no card to look for
    if (this.keptRun(id)?.sessionId === null || id.startsWith(ELSEWHERE_RUN)) return null;
    for (const s of await this.deps.api.listSessions("", RUN_SEARCH_SESSIONS)) {
      const out = await this.deps.api.getSession(s.session_id);
      if ((out.actions ?? []).some((a) => a.action_id === id)) return s.session_id;
    }
    return null;
  }

  /**
   * A diagnosis card arrived in a conversation that does not show its goal's run (he said 接着学这个目标 there): that goal
   * is the current one, so its end moves it on (the block entered, the first page written) as under its own card.
   */
  private async followDiagnosis(draftId: string): Promise<void> {
    const goals = this.deps.goals;
    if (!goals || this.currentGoal()?.flow.state.draftId === draftId) return;
    const mapId = (await goals.api().getDraft(draftId)).map_id;
    if (mapId === null || !this.live()) return;
    const mine = (r: GoalRun): boolean => r.mapId === mapId;
    const id = `${ELSEWHERE_RUN}${mapId}`;
    const run = [...this.goalRuns.values()].reverse().find(mine) ?? this.keptRunOf(mapId) ?? this.newGoalRun(id, { draftId, mapId }, false);
    this.lastRun = run.actionId;
    await run.follow(draftId);
  }

  /** A kept chat diagnosis is the server's and the user's (as a session id is): JSON [server, user, goal_new row id]. */
  private keptKey(actionId: string): string {
    const id = this.deps.identity();
    return JSON.stringify([id.server, id.user, actionId]);
  }

  /** Typed 跳过 (GOAL_SKIP_WORDS) while a chat diagnosis of this conversation waits: that run skips; true = taken. */
  private skipTyped(c: Conversation, text: string): boolean {
    if (!isGoalSkip(text)) return false;
    for (let i = c.turns.length - 1; i >= 0; i--) {
      for (const card of [...c.turns[i]!.actions].reverse()) {
        const run = this.goalRuns.get(card.actionId);
        if (run?.waiting) { run.skip(); return true; }
      }
    }
    return false;
  }

  goalRun(actionId: string): GoalRun | undefined { return this.goalRuns.get(actionId); }
  pickGoal(actionId: string, option: number): void { this.goalRuns.get(actionId)?.pick(option); }
  skipGoal(actionId: string): void { this.goalRuns.get(actionId)?.skip(); }
  retryGoal(actionId: string): void { this.goalRuns.get(actionId)?.retry(); }
  restartGoal(actionId: string): void { this.goalRuns.get(actionId)?.restart(); }
  /** 停止 on a goal run's writing card (its page's run cancelled, also after a reload). */
  stopGoal(actionId: string): void { this.goalRuns.get(actionId)?.stop(); }

  /** The answer of this conversation is running and its message went out: the send slot is its stop. */
  private canStop(): boolean {
    const c = this.conv();
    return c !== null && c !== undefined && c.busy && !c.ending && c.turns[c.turns.length - 1]?.status === "streaming";
  }

  /**
   * The send slot's stop: what is shown stays and the composer is free at once. The server ends the turn on the stream
   * `run` is still following, so a card that comes out meanwhile still arrives; a message sent before that end waits for it
   * (`run`). A 404 before the turn has started on the server is asked again until the turn is over.
   */
  async stopTurn(): Promise<void> {
    const c = this.conv();
    if (!c || !this.canStop()) return;
    const turn = c.turns[c.turns.length - 1]!;
    turn.stopping = true; turn.thinking = false; turn.statusLine = ""; turn.rev++;
    c.busy = false; c.ending = c.running ?? null;
    this.notify();
    try {
      const sid = await this.session(c);
      while (turn.status === "streaming" && !this.abort.signal.aborted) {
        try { await this.deps.api.stopTurn(sid); return; } catch (e) {
          if (!(e instanceof ApiError && e.status === 404)) throw e;
        }
        await new Promise((r) => { setTimeout(r, STOP_RETRY_MS); });
      }
    } catch (e) {
      console.warn("EvoWiki assistant: could not stop the turn", e);
      ewNotice(ASSISTANT_COPY.stopFailed);
      turn.stopping = false;
      if (turn.status === "streaming" && c.ending === (c.running ?? null)) { c.busy = true; c.ending = null; }
      turn.rev++;
      this.notify();
    }
  }

  /**
   * 学点新的 → 开始 (app.js startBlocks): the conversation without a page is shown, started afresh when it has questions
   * (the demo's newConv), and `text` goes out as its first message. Returns its key at once (the rows of the turn come
   * before `done` resolves) and the turn (false: it failed; the chat shows why), or null when it cannot send now. `learn`:
   * the form's goal and background, sent with the message as intent learn_goal (the chat shows `text`).
   */
  startGoalChat(text: string, learn: LearnGoal, materials: readonly GoalMaterial[] = []): { key: string; done: Promise<boolean> } | null {
    if (!this.live()) return null;
    // the conversation without a page, kept while the panel re-reports the note that was open (the 学点新的 tab is no note)
    const prev = this.active;
    this.show({ kind: "none" });
    if (prev.kind !== "none") this.heldFrom = prev;
    else this.holdFirst = true;
    let c = this.conv();
    if (!c) return null;
    if (c.busy || c.ending) c = this.detach(c);        // the running turn goes on in the background
    const question = text.trim();
    const goal = learn.goal.trim();
    if (question === "" || (goal === "" && materials.length === 0) || [...question].length > MAX_MESSAGE_CHARS) return null;
    this.history = { ...this.history, open: false, notice: "" };
    // a conversation still being read back (the stored one the free target restores on show) starts anew too: clear()
    // drops its session id, so the late read is discarded (restoreConv checks it)
    const forgot = c.turns.length > 0 || c.sessionId !== null || c.restore === "loading";
    if (forgot) this.clear(c);
    // with a material read in 学点新的: an ordinary message with it attached (learn_goal takes none; the model runs
    // new_goal on it, as when the material is attached here); else learn_goal with the form's goal
    const turn = newTurn(this.deps.newId(), question, null, materials.map((m) => fromOut({ source_id: m.sourceId, title: m.title,
      kind: m.kind, status: "ready", deleted: false, uses: [], bytes: 0, pages: m.pages, expires_at: null, unreadable_pages: [],
      chars: null, read: null, importing: false })));
    if (materials.length === 0) turn.learn = { goal, background: learn.background.trim() };
    c.turns.push(turn);
    this.goalStarting++;
    const done = (async (): Promise<boolean> => {
      try {
        if (forgot && this.deps.store.get(c.key) !== null) await this.save(c.key, null);
        await this.run(c, turn);
        return turn.status !== "error";
      } finally { this.goalStarting--; }
    })();
    return { key: c.key, done };
  }

  /**
   * 个性化 · 待确认的记忆 · 处理 (app.js startMemories): the conversation without a page, started afresh when it has
   * questions, and one message 「处理待确认的记忆」 sent as intent resolve_memory_conflicts (the server reads his open groups
   * and the assistant takes them one at a time; the chat shows no bubble for it). Null when it cannot send now.
   */
  startConflictsChat(): { key: string; done: Promise<boolean> } | null {
    if (!this.live()) return null;
    const prev = this.active;
    this.show({ kind: "none" });
    if (prev.kind !== "none") this.heldFrom = prev;
    else this.holdFirst = true;
    let c = this.conv();
    if (!c) return null;
    if (c.busy || c.ending) c = this.detach(c);
    this.history = { ...this.history, open: false, notice: "" };
    const forgot = c.turns.length > 0 || c.sessionId !== null || c.restore === "loading";
    if (forgot) this.clear(c);
    const turn = newTurn(this.deps.newId(), CONFLICTS_MESSAGE, null);
    turn.conflicts = true;
    c.turns.push(turn);
    const done = (async (): Promise<boolean> => {
      if (forgot && this.deps.store.get(c.key) !== null) await this.save(c.key, null);
      await this.run(c, turn);
      return turn.status !== "error";
    })();
    return { key: c.key, done };
  }

  /** Idempotent for the same key. A conversation seen for the first time with a stored session id starts one read. */
  show(active: ActiveNote): void {
    // the note open now, whichever conversation shows (a held one too): every message names its page
    this.viewing = active.kind === "page" ? active.pageId : null;
    if (this.heldPage !== null && active.kind === "page" && active.pageId === this.heldPage) return;
    if (this.heldPage !== null && (active.kind === "page" || active.kind === "note")) this.heldPage = null;
    if (this.holdFirst && active.kind !== "none") { this.holdFirst = false; this.heldFrom = active; return; }
    if (this.heldFrom !== null && sameActive(this.heldFrom, active)) return;
    this.heldFrom = null;
    if (this.models === null && !this.modelsLoading) void this.loadModels();   // the picker's first read
    const changed = !sameActive(this.active, active);
    this.active = active;
    const t = targetOf(active);
    const key = sessionKey(this.deps.identity(), t);
    const had = this.convs.get(key);
    if (had) {
      had.target = t;
      // made before any show (conv(): the composer's conversation) with a stored session never read: read it now
      if (this.unread.has(had)) void this.restoreConv(had);
      else if (changed) this.notify();
      return;
    }
    const c = this.create(key, t);
    this.notify();
    if (c.sessionId !== null) void this.restoreConv(c);
  }

  private create(key: string, t: ConvTarget): Conversation {
    const c: Conversation = {
      key, target: t, sessionId: this.configured() ? this.deps.store.get(key) : null, turns: [],
      draft: "", quote: null, busy: false, attachments: [], restore: "none", recap: null,
    };
    this.convs.set(key, c);
    if (c.sessionId !== null) this.unread.add(c);
    return c;
  }

  /**
   * The server or user changed: the same note is re-shown under the new identity (its own conversation; at most the
   * one restore read for a stored session id). Called once by main.ts after settings reinit.
   */
  identityChanged(): void {
    for (const r of this.goalRuns.values()) r.dispose();          // another server / user: its rows are not these
    this.goalRuns.clear();
    this.modelsSeq++;
    this.modelsLoading = false;
    this.modelPicking = false;
    this.models = null;
    this.modelsOpen = false;
    const a = this.active;
    this.active = { kind: "none" };
    this.holdFirst = false;
    this.show(a);
  }

  /**
   * Shows the target with the quote attached to its composer. Sends nothing. The conversation goes on: only ＋ starts a new one.
   */
  attachQuote(target: Target, quote: Quote, reteach?: string): void {
    const c = this.showForQuote(target);
    if (!c) return;
    c.quote = quote; c.quoteLive = false;
    if (reteach !== undefined) { c.draft = reteach; c.reteachDraft = reteach; }
    this.focusTick++;
    this.notify();
  }

  /** 今天's review round moved: the chat's card follows (main.ts). */
  reviewChanged(): void { this.reviewTick++; this.notify(); }

  /** A quote from the review card (反馈, 看不懂) while the shown conversation holds the card: it stays in this conversation. False: it holds none. */
  quoteInReview(quote: Quote): boolean {
    const c = this.conv();
    if (!c || reviewTurnOf(c.turns) === null) return false;
    c.quote = quote;
    this.focusTick++;
    this.notify();
    return true;
  }

  /**
   * A selection too long to go as a quote (over MAX_QUOTE_CHARS): shown like attachQuote, but the text goes along as a
   * text attachment (the attachment path reads big text whole), never refused or cut. `draft`: words to put in the box
   * (sent by the learner once the attachment is read). False when this conversation takes no files.
   */
  async attachTextFile(target: Target, text: string, draft = ""): Promise<boolean> {
    if (!this.deps.attachments || !this.live()) return false;
    const c = this.showForQuote(target);
    if (!c) return false;
    if (draft !== "") c.draft = draft;
    this.focusTick++;
    await this.addFiles([textFile(text, ASSISTANT_COPY.selectionFile)]);
    return true;
  }

  /** attachQuote's first half: show the target's conversation, whatever it holds (the demo's askAssistant). */
  private showForQuote(target: Target): Conversation | null {
    const was = this.active;
    this.holdFirst = false;
    this.show(target);
    if (!sameActive(was, target)) this.heldFrom = was;
    const c = this.conv();
    if (!c) return null;
    this.history = { ...this.history, open: false };
    return c;
  }

  /** Pasted text that would not fit in one message: added as a text attachment instead (the typed words stay). */
  async addPastedText(text: string): Promise<void> {
    await this.addFiles([textFile(text, ASSISTANT_COPY.pastedFile)]);
  }

  /** A web link dropped on the panel: into the draft, after what is typed (it is never dropped silently). */
  appendToDraft(text: string): void {
    const c = this.conv();
    const t = text.trim();
    if (!c || t === "") return;
    c.draft = c.draft.trim() === "" ? t : `${c.draft.replace(/\s+$/, "")} ${t}`;
    this.focusTick++;
    this.notify();
  }

  /**
   * A tap that already says what the learner wants (读完了's 理解有困难, app.js sendToAssistant): `text` is sent as the
   * learner's message in the target's conversation. While the conversation cannot send (busy, restoring), the text stays
   * in the box; false = not sent.
   */
  async sendIn(target: Target, text: string): Promise<boolean> {
    const c = this.showForQuote(target);
    if (!c) return false;
    c.draft = text;
    this.focusTick++;
    if (!this.live() || !canSend(c)) { this.notify(); return false; }
    await this.send();
    return true;
  }

  removeQuote(): void {
    const c = this.conv();
    if (!c || !c.quote) return;
    if (c.quoteLive === true) this.selectionDone = c.quote.text;
    c.quote = null; c.quoteLive = false;
    this.notify();
  }

  /**
   * His selection in the open note, whatever conversation the chat is on (a goal's stays on while its first page is read): the chip over the composer, the same as 问陪读助手 (× takes it off, 问 sends it),
   * following the selection. null: nothing selected, a live chip goes; a quote he asked for from the menu or the review card stays.
   */
  selectionChanged(quote: Quote | null): void {
    if (quote === null) this.selectionDone = null;
    const c = this.conv();
    if (!c) return;
    if (c.quote !== null && c.quoteLive !== true) return;
    const next = quote !== null && quote.text !== this.selectionDone ? quote : null;
    if (next?.text === c.quote?.text && next?.heading === c.quote?.heading) return;
    c.quote = next; c.quoteLive = next !== null;
    this.notify();
  }

  /** A page edit asked for by a card (its 重试): sent with intent reteach_section, so the turn has the page-edit rule and tool at once. */
  sendEdit(text: string): void {
    const c = this.conv();
    if (c === null) return;
    c.draft = text; c.reteachDraft = text;
    void this.send();
  }

  setDraft(text: string): void {
    const c = this.conv();
    if (!c) return;
    c.draft = text;
    this.notify();
  }

  async send(): Promise<void> {
    const c = this.conv(); if (!c || !this.live()) return;
    // A question the last turn ended on (ask_user) needs nothing special: his message is the answer (the server resumes
    // that turn with it), or a new request when it is not one.
    if (!canSend(c)) return;
    const quote = c.quote;
    // A quote alone asks the placeholder's question (app.js: send is enabled with a quote and no text). Attachments
    // alone go with no text (the server asks for the user).
    const question = c.draft.trim() || (quote ? ASSISTANT_COPY.quoteOnlyQuestion : "");
    if ([...question].length > MAX_MESSAGE_CHARS) return;
    // 跳过 typed while this conversation's chat diagnosis waits: the card's 「跳过，直接开始」, no message to the model
    if (quote === null && !c.attachments.some((d) => d.status !== "failed") && this.skipTyped(c, question)) { c.draft = ""; this.notify(); return; }
    const attachments = c.attachments.filter((d) => d.status !== "failed").map(fromDraft);
    const reteach = c.reteachDraft === question;
    delete c.reteachDraft;
    if (c.quoteLive === true && quote !== null) this.selectionDone = quote.text;     // still selected in the note: it does not come back
    c.draft = ""; c.quote = null; c.quoteLive = false; c.attachments = [];
    const turn = newTurn(this.deps.newId(), question, quote, attachments);
    if (reteach) turn.reteach = true;
    c.turns.push(turn);
    // What he just typed into the open note reaches the server before the model reads the page (a draft on the old text is refused at 改写).
    if (this.active.kind === "page") await this.deps.handEditFirst?.(this.active.pageId);
    await this.run(c, turn);
  }

  /**
   * Files dropped / picked into the composer: each becomes a card that uploads (use ask, this conversation's session -
   * created first when there is none), then follows its reading until the source is usable. Send stays off meanwhile.
   * Files over MAX_ATTACHMENTS are not added (a notice says so).
   */
  async addFiles(files: File[]): Promise<void> {
    const c = this.conv(); const port = this.deps.attachments;
    if (!c || !port || !this.live() || files.length === 0) return;
    const room = Math.max(0, MAX_ATTACHMENTS - c.attachments.length);
    if (files.length > room) ewNotice(ASSISTANT_COPY.attachTooMany(MAX_ATTACHMENTS));
    const taken = files.slice(0, room).map((f) => ({ f, d: newDraft(this.deps.newId(), f, thumbOf(f)) }));
    if (taken.length === 0) return;
    c.attachments.push(...taken.map((x) => x.d));
    this.notify();
    const who = this.whoKey();
    await Promise.all(taken.map(({ f, d }) => this.upload(c, port, f, d, who)));
  }

  /** The card's x: stops its upload / reading follow and drops it (an uploaded source stays an unused ask material). */
  removeAttachment(localId: string): void {
    const c = this.conv(); if (!c) return;
    const d = c.attachments.find((a) => a.localId === localId);
    if (!d) return;
    this.follows.get(localId)?.abort();
    this.follows.delete(localId);
    this.drop(c, d);
    this.notify();
  }

  private drop(c: Conversation, d: DraftAttachment): void {
    if (d.thumb !== undefined && typeof URL.revokeObjectURL === "function") URL.revokeObjectURL(d.thumb);
    c.attachments = c.attachments.filter((a) => a !== d);
  }

  /** The identity an upload started under: a switched server / user drops it (no request goes out under the new one). */
  private whoKey(): string {
    const id = this.deps.identity();
    return JSON.stringify([id.server, id.user]);
  }

  /**
   * After a turn's done (`outs`: its own cards, then the session's earlier attachments done re-sent after an action):
   * every card of each source takes the server's uses and expiry (a kept file has no 「N 天后清掉」). A source that got its
   * first use beyond ask in this turn (the assistant inferred what the learner wants with it: a goal from it, past exams,
   * kept to learn) follows the import that use started (正在解析 + bar) - only while the server says a run is going
   * (`importing`): an already imported material shows no bar.
   */
  private followNewUses(c: Conversation, kept: ReadonlySet<string>, outs: readonly TurnAttachment[]): void {
    const port = this.deps.attachments;
    if (!port || !this.live()) return;
    for (const out of outs) {
      const cards = c.turns.flatMap((t) => t.attachments.filter((a) => a.sourceId === out.sourceId).map((a) => ({ t, a })));
      if (cards.length === 0) continue;
      for (const { t, a } of cards) { a.uses = out.uses; a.expiresAt = out.expiresAt; a.importing = out.importing; t.rev++; }
      if (out.deleted || !learning(out.uses) || kept.has(out.sourceId) || !out.importing) continue;
      for (const { a } of cards) { a.progress = { done: 0, total: 0 }; a.error = ""; }
      void this.followImport(port, out.sourceId, cards);
    }
    this.notify();
  }

  /** Sources some card of the conversation already shows with a use beyond ask. */
  private keptSources(c: Conversation): Set<string> {
    return new Set(c.turns.flatMap((t) => t.attachments.filter((a) => learning(a.uses)).map((a) => a.sourceId)));
  }

  /** The import a use started, until done / error. A lost stream only drops the bar (the use is recorded). */
  private async followImport(port: AttachmentPort, sourceId: string, cards: { t: Turn; a: TurnAttachment }[]): Promise<void> {
    const id = `use:${sourceId}`;
    this.follows.get(id)?.abort();
    const ctl = new AbortController();
    this.follows.set(id, ctl);
    const mark = (f: (a: TurnAttachment) => void): void => { for (const { t, a } of cards) { f(a); t.rev++; } this.notify(); };
    try {
      await port.follow(sourceId, (p) => {
        if (ctl.signal.aborted) return;
        if (p.kind === "progress") mark((a) => { a.progress = { done: p.done, total: p.total }; });
        else if (p.kind === "done") mark((a) => { a.progress = null; });
        else if (p.kind === "error") mark((a) => { a.progress = null; a.error = p.detail; });
      }, ctl.signal);
    } catch (e) {
      if (!ctl.signal.aborted) console.warn("EvoWiki assistant: lost the import progress of an attachment", e);
    } finally {
      if (this.follows.get(id) === ctl) this.follows.delete(id);
      if (!ctl.signal.aborted && cards.some(({ a }) => a.progress !== null)) mark((a) => { a.progress = null; });
    }
  }

  /** One draft attachment: session (shared create), bytes, upload, then its reading until usable. Fails closed on the card. */
  private async upload(c: Conversation, port: AttachmentPort, f: File, d: DraftAttachment, who: string): Promise<void> {
    const here = (): boolean => c.attachments.includes(d) && this.live() && this.whoKey() === who;
    try {
      const sid = await this.session(c);
      const data = await bytesOf(f);
      if (!here()) return;
      if (!here()) return;
      const out = await port.upload({ name: f.name, data }, sid);
      if (!here()) return;
      // The same content again (a 200 dedupe) is the same source: one card for it.
      if (c.attachments.some((a) => a !== d && a.sourceId === out.id)) { this.drop(c, d); this.notify(); return; }
      applyUploaded(d, out);
      this.notify();
    } catch (e) {
      if (!here()) return;
      failDraft(d, attachErrorText(e));
      this.notify();
      return;
    }
    if (d.status === "reading") await this.followDraft(port, d, here);
  }

  /** Follows a reading attachment until it is usable (ready / partial), fails, or is removed; then stops the stream. */
  private async followDraft(port: AttachmentPort, d: DraftAttachment, here: () => boolean): Promise<void> {
    const ctl = new AbortController();
    this.follows.set(d.localId, ctl);
    try {
      await port.follow(d.sourceId!, (p) => {
        if (ctl.signal.aborted) return;
        if (!here()) { ctl.abort(); return; }
        applyProgress(d, p);
        if (d.status !== "reading") ctl.abort();
        this.notify();
      }, ctl.signal);
    } catch (e) {
      // The upload went through; the reading stream broke (offline, a server error): not read through.
      if (!ctl.signal.aborted && here()) {
        console.warn("EvoWiki assistant: lost the reading progress of an attachment", e);
        failDraft(d, ASSISTANT_COPY.attachLost); this.notify();
      }
      return;
    } finally {
      if (this.follows.get(d.localId) === ctl) this.follows.delete(d.localId);
    }
    // The stream ended with the source still reading: never left spinning (fail closed; the user removes and re-adds).
    if (!ctl.signal.aborted && d.status === "reading" && here()) { failDraft(d, ASSISTANT_COPY.attachLost); this.notify(); }
  }

  /** A fixed question (a page chip): the same guards as send(); the draft and the quote stay. False when a guard refused it. */
  async sendText(text: string): Promise<boolean> {
    const c = this.conv(); if (!c || !this.live() || c.busy || c.restore === "loading") return false;
    const question = text.trim();
    if (question === "" || [...question].length > MAX_MESSAGE_CHARS) return false;
    const turn = newTurn(this.deps.newId(), question, null);
    c.turns.push(turn);
    await this.run(c, turn);
    return true;
  }

  /** Only the last turn, only for a retryable error, never while busy. */
  async retry(turnId: string): Promise<void> {
    const c = this.conv(); if (!c || !this.live() || c.busy || c.restore === "loading") return;
    const turn = c.turns[c.turns.length - 1];
    if (!turn || turn.id !== turnId || turn.status !== "error" || !turn.error || !canRetry(turn.error)) return;
    if (turn.lost) { await this.restoreConv(c); return; }
    resetForRetry(turn);
    await this.run(c, turn);
  }

  /**
   * The connection came back (the panel's 未连接到服务器 · 重试, or a reconnect ping): every conversation whose last
   * message could not be sent (offline) sends it again. Such a turn shows no line of its own (user 2026-10-28:
   * 「未连接到服务器就好了，下面那个删掉」): the panel's status says it, and this is its retry.
   */
  async resendOffline(): Promise<void> {
    if (!this.live()) return;
    const waiting = [...this.convs.values()].flatMap((c) => {
      const turn = c.turns[c.turns.length - 1];
      return !c.busy && c.restore !== "loading" && turn?.status === "error" && (turn.lost || turn.error?.kind === "offline") ? [{ c, turn }] : [];
    });
    await Promise.all(waiting.map(({ c, turn }) => {
      if (turn.lost) return this.restoreConv(c);
      resetForRetry(turn);
      return this.run(c, turn);
    }));
  }

  /** One request per click; ignored while the card is sending or already decided. */
  async decide(turnId: string, proposalId: string, d: Decision): Promise<void> {
    const c = this.conv(); if (!c || !this.live()) return;
    const turn = c.turns.find((t) => t.id === turnId);
    const card = turn?.proposals.find((p) => p.proposalId === proposalId);
    if (!turn || !card || !cardOpen(card)) return;
    card.status = "sending"; card.error = null; card.detail = ""; card.note = ""; turn.rev++;
    this.notify();
    try {
      if (d === "accept" && !(await this.handEditFirst(card))) { card.status = "open"; return; }
      const r = await this.deps.api.decide(proposalId, d);
      card.status = r.status;
      card.applied = r.applied ?? [];
      card.detail = r.detail ?? "";
      card.actionId = r.action_id; card.canUndo = r.can_undo;
      if (r.status === "accepted") {
        this.edited(card.draft.page_id, card.applied);
        // the server voided the other open drafts of this section (AssistantEditExecutor.voided_by): their cards say so now
        for (const t of c.turns) {
          for (const o of t.proposals) {
            if (o !== card && o.status === "open" && o.draft.kind === "rewrite_section" && card.draft.kind === "rewrite_section"
              && o.draft.page_id === card.draft.page_id && o.draft.section_id === card.draft.section_id) {
              o.status = "failed"; o.detail = LEDGER_STALE; t.rev++;
            }
          }
        }
      }
    } catch (e) {
      card.status = "error";
      card.error = classifyError(e, "decide");
    } finally { turn.rev++; this.notify(); }
  }

  /** 撤销 on an accepted proposal card: the ledger row's undo (refused when the page changed since: the card says why). */
  async undoProposal(turnId: string, proposalId: string): Promise<void> {
    const c = this.conv(); if (!c || !this.live()) return;
    const turn = c.turns.find((t) => t.id === turnId);
    const card = turn?.proposals.find((p) => p.proposalId === proposalId);
    if (!turn || !card || !card.canUndo || card.undoing) return;
    card.undoing = true; card.note = ""; turn.rev++;
    this.notify();
    try {
      if (!(await this.handEditFirst(card))) return;
      await this.deps.api.undoAction(card.actionId);
      card.status = "undone"; card.canUndo = false;
      this.edited(card.draft.page_id, card.applied);
      try { this.deps.ledgerChanged?.(); } catch (e) { console.warn("EvoWiki assistant: ledger hook failed", e); }
    } catch (e) {
      card.note = ACTION_COPY.error(e);
    } finally { card.undoing = false; turn.rev++; this.notify(); }
  }

  private async handEditFirst(card: ProposalCard): Promise<boolean> {
    const ok = await (this.deps.handEditFirst?.(card.draft.page_id) ?? true);
    if (!ok) card.note = ASSISTANT_COPY.proposalHandBlocked;
    return ok;
  }

  // ---------------- answer ratings ----------------

  /**
   * 👍 / 👎 (with 👎's reason chip and note) on a finished answer: shown at once, then posted. Posts of one answer go one
   * after another (the server keeps the latest); a failed one puts back the last rating the server confirmed and says so.
   */
  async rate(turnId: string, next: Rating): Promise<void> {
    const c = this.conv(); if (!c || !this.live()) return;
    const turn = c.turns.find((t) => t.id === turnId);
    const sid = c.sessionId;
    if (!turn || turn.status !== "done" || turn.answerId === null || sid === null) return;
    const want: Rating = next.value === "down" ? { value: "down", chips: [...next.chips], note: next.note } : { value: next.value, chips: [], note: "" };
    const st = this.rated.get(turn) ?? { good: turn.rating, chain: Promise.resolve(), pending: 0 };
    this.rated.set(turn, st);
    turn.rating = want;
    st.pending++;
    this.notify();
    const mid = turn.answerId;
    const post = async (): Promise<void> => {
      try {
        const out = await this.deps.api.rate(sid, mid, want);
        st.good = ratingOf(out);
        if (st.pending === 1) turn.rating = st.good;
      } catch (e) {
        console.warn("EvoWiki assistant: could not save the rating", e);
        if (st.pending === 1) turn.rating = st.good;     // a later click still on its way decides what shows
        ewNotice(RATING_COPY.error(e));
      } finally { st.pending--; this.notify(); }
    };
    st.chain = st.chain.then(post);
    await st.chain;
  }

  // ---------------- action cards, quizzes, writing status ----------------

  /** The card with this ledger id in the conversation (the first turn that has it) and its turn. */
  /**
   * 查看详情: opens the action's result (card-state.ts cardResult). False: none to open here (an RSI row: its row in 个性化 · 活动记录, the
   * caller's), or no destinations wired.
   */
  showResult(actionId: string): boolean {
    const r = this.deps.results;
    const found = [...this.convs.values(), ...this.background].map((c) => this.cardOf(c, actionId)).find((x) => x !== null);
    const res = found ? cardResult(found.card) : null;
    if (!r || res === null) return false;
    if (res.kind === "page") { r.openPage(res.pageId, res.sectionId); return true; }
    if (res.kind === "goal") { if (this.goalRuns.has(actionId)) this.lastRun = actionId; r.openGoal(actionId, res.mapId); return true; }
    if (res.kind === "rules") { r.openRules(); return true; }
    return false;
  }

  /** Open the actual section using the same destination as an action result. */
  showProposal(turnId: string, proposalId: string): void {
    const card = this.conv()?.turns.find((t) => t.id === turnId)?.proposals.find((p) => p.proposalId === proposalId);
    if (card) this.deps.results?.openPage(card.draft.page_id, card.draft.section_id);
  }

  private cardOf(c: Conversation | null, actionId: string): { card: ActionCardState; turn: Turn } | null {
    for (const turn of c?.turns ?? []) {
      const card = turn.actions.find((a) => a.actionId === actionId);
      if (card) return { card, turn };
    }
    return null;
  }

  private quizOf(quizId: string): { quiz: QuizState; turn: Turn } | null {
    for (const turn of this.conv()?.turns ?? []) {
      const quiz = turn.quizzes.find((z) => z.quizId === quizId);
      if (quiz) return { quiz, turn };
    }
    return null;
  }

  private changed(turn: Turn): void { turn.rev++; this.notify(); }

  /** 确认: approve the row, with the learner's edited list when it was changed. The card becomes the finished action. */
  async confirmAction(actionId: string): Promise<void> { await this.decideAction(actionId, "approve"); }
  /** Keep keystrokes without rebuilding the focused editor. */
  editRuleText(actionId: string, text: string): void {
    const found = this.cardOf(this.conv(), actionId);
    if (!found || found.card.kind !== "rule" || cardPhase(found.card.status) !== "wait" || found.card.busy !== null || found.card.ruleText === null) return;
    found.card.ruleText = text;
    found.card.edited = true;
  }
  /** 取消: the row is rejected, nothing changes. */
  async cancelAction(actionId: string): Promise<void> { await this.decideAction(actionId, "reject"); }
  /**
   * 重试 on an in-page draft that no longer fits its page (it waits, or its 确认 failed because the page moved): the server
   * writes it again for the same request against the page as it is now (a practice: this row, a new draft, waiting again;
   * a materials draft: a new row, which the ledger read brings). One decision with the page's bar (decisions.ts).
   */
  async redraftAction(actionId: string): Promise<void> {
    const found = this.cardOf(this.conv(), actionId), api = this.deps.api;
    if (!found || !this.live() || api.redraftAction === undefined) return;
    const { card, turn } = found;
    if (card.busy !== null) return;
    const shared = inPageDraft(card.kind) ? this.deps.decisions : undefined;
    if (shared !== undefined && !shared.start(actionId, "redraft")) return;
    this.deciding.add(actionId);
    card.busy = "redraft"; card.error = ""; card.retry = null;
    this.changed(turn);
    try {
      const out = await api.redraftAction(actionId);
      if (out.action_id === actionId) applyActionOut(card, out); else card.busy = null;
      this.deciding.delete(actionId);
      shared?.clear(actionId);
    } catch (e) {
      card.busy = null;
      card.error = ACTION_COPY.error(e);
      card.retry = retryable(e) ? "redraft" : null;
      this.deciding.delete(actionId);
      if (shared !== undefined) { if (card.retry !== null) shared.fail(actionId, "redraft"); else shared.clear(actionId); }
    } finally {
      this.changed(turn);
      try { this.deps.ledgerChanged?.(); } catch (e) { console.warn("EvoWiki assistant: ledger hook failed", e); }
    }
  }

  /** 撤销 of a done action. */
  async undoAction(actionId: string): Promise<void> { await this.decideAction(actionId, "undo"); }

  private async decideAction(actionId: string, op: "approve" | "reject" | "undo"): Promise<void> {
    const found = this.cardOf(this.conv(), actionId);
    if (!found || !this.live()) return;
    const { card, turn } = found;
    const phase = cardPhase(card.status);
    if (card.busy !== null || (op === "undo" ? phase !== "live" : phase !== "wait")) return;
    if (op === "approve" && card.kind === "rule" && !card.ruleText?.trim()) return;
    if (op === "approve" && draftable(card) && card.draft.length === 0) return;
    await this.keeping.get(actionId)?.chain;          // the card's kept edits land first (确认 then sends no list)
    if (card.busy !== null || cardPhase(card.status) !== (op === "undo" ? "live" : "wait")) return;
    // an in-page draft: one decision with the page's bar (claimed there; the bar follows its outcome)
    const dop = op === "undo" ? null : op;
    const shared = dop !== null && inPageDraft(card.kind) ? this.deps.decisions : undefined;
    if (shared !== undefined && dop !== null) {
      if (!shared.start(actionId, dop)) return;
      this.deciding.add(actionId);
    }
    card.busy = op; card.error = ""; card.retry = null;
    this.changed(turn);
    const conv = this.conv();
    let decided: ActionOut | undefined;
    try {
      const api = this.deps.api;
      const sent = op === "approve" ? draftPayload(card) : null, rows = card.draft.map((d) => ({ ...d }));
      const out = op === "approve" ? await api.approveAction(actionId, sent ?? undefined)
        : op === "reject" ? await api.rejectAction(actionId) : await api.undoAction(actionId);
      decided = out;
      applyActionOut(card, out);
      if (shared !== undefined) { this.deciding.delete(actionId); shared.settle(actionId, out); }
      // a goal confirmed with an edited list: the card counts the blocks that were confirmed (app.js blocksAct did)
      if (card.kind === GOAL_KIND && sent !== null && cardPhase(card.status) === "live" && card.items.length !== rows.length) {
        card.items = rows.map((d, k) => ({ key: d.key ?? `new${k}`, title: d.title, page_id: null, covers: d.covers !== "" ? d.covers : null, n: null, level: d.level !== "" ? d.level : null }));
        card.draft = card.items.map(rowOf);
      }
      const now = cardPhase(card.status);
      // an in-page draft (materials / practice) confirmed or undone from the chat: the page's note follows the page (as
      // after any page save)
      const rv = inPageDraft(card.kind) ? card.revise?.pageId || out.revise?.page_id || out.page_id || "" : "";
      if (rv !== "" && (now === "live" || now === "undone")) this.edited(rv, []);
      if (now === "live") for (const id of cardPageIds(card)) this.fresh.add(id);
      if (op === "approve" && now === "live" && card.kind === KNOWN_KIND) void this.askKnown(turn, actionId);
    } catch (e) {
      card.busy = null;
      card.error = ACTION_COPY.error(e);
      // did not go through: only a quiet 请重试 (the card draws it in place of the text)
      card.retry = retryable(e) ? op : null;
      if (shared !== undefined) {
        this.deciding.delete(actionId);
        if (dop !== null && card.retry !== null) shared.fail(actionId, dop); else shared.clear(actionId);
      }
    } finally {
      this.changed(turn);
      if (conv !== null && decided !== undefined) this.goalRow(conv, card, decided);
      try { this.deps.ledgerChanged?.(); } catch (e) { console.warn("EvoWiki assistant: ledger hook failed", e); }
    }
  }

  /**
   * Draft edits change the card at once. A goal's blocks draft keeps each edit on its row as it happens (POST …/draft, so a
   * chat edit — edit_goal_blocks — starts from it); other drafts send their list at 确认.
   */
  moveDraft(actionId: string, from: number, to: number): void {
    const c = this.conv(), f = this.cardOf(c, actionId);
    if (f && cardPhase(f.card.status) === "wait" && f.card.busy === null && moveDraft(f.card, from, to)) this.draftEdited(c!, f.card, f.turn);
  }
  removeDraft(actionId: string, k: number): void {
    const c = this.conv(), f = this.cardOf(c, actionId);
    if (f && cardPhase(f.card.status) === "wait" && f.card.busy === null && removeDraft(f.card, k)) this.draftEdited(c!, f.card, f.turn);
  }
  addDraft(actionId: string, title: string): void {
    const c = this.conv(), f = this.cardOf(c, actionId);
    if (f && cardPhase(f.card.status) === "wait" && f.card.busy === null && addDraft(f.card, title)) this.draftEdited(c!, f.card, f.turn);
  }
  /** 学点新的's ⋯ 改名 on a block of the waiting draft (kept on the row as {key, title}). */
  renameDraft(actionId: string, k: number, title: string): void {
    const c = this.conv(), f = this.cardOf(c, actionId);
    if (f && cardPhase(f.card.status) === "wait" && f.card.busy === null && renameDraft(f.card, k, title)) this.draftEdited(c!, f.card, f.turn);
  }

  private draftEdited(c: Conversation, card: ActionCardState, turn: Turn): void {
    this.changed(turn);
    this.goalRow(c, card);
    const api = this.deps.api;
    if (card.kind !== GOAL_KIND || !api.editDraft || !this.live()) return;
    const k = this.keeping.get(card.actionId) ?? { chain: Promise.resolve(), seq: 0 };
    const seq = ++k.seq;
    this.keeping.set(card.actionId, k);
    k.chain = k.chain.then(async () => {
      if (seq !== k.seq || cardPhase(card.status) !== "wait") return;      // a newer edit sends the newest list
      try {
        const out = await api.editDraft!(card.actionId, wholeDraft(card));
        if (seq === k.seq && cardPhase(card.status) === "wait") keptDraft(card, out);
      } catch (e) {
        // refused (e.g. 已经有这一块了) or the row is no longer waiting: the server's text, and the card as the row is
        ewNotice(e instanceof ApiError && e.status === 409 && e.detail.trim() !== "" ? e.detail : ACTION_COPY.error(e));
        if (cardPhase(card.status) === "wait") revertDraft(card);
        try {
          const row = (await api.listActions()).find((r) => r.action_id === card.actionId);
          if (row !== undefined && card.busy === null) { applyActionOut(card, row); if (cardPhase(card.status) === "wait") revertDraft(card); }
        } catch (err) { console.warn("EvoWiki assistant: could not reload the draft row", err); }
      }
      this.changed(turn);
      this.goalRow(c, card);
    });
  }

  /**
   * The ledger changed elsewhere (个性化 approved, rejected or undid a row): the cards of those rows follow, in place.
   * A draft still waiting keeps the learner's edits.
   */
  syncLedger(rows: readonly ActionOut[]): void {
    const c = this.conv();
    if (!c) return;
    let any = false;
    for (const turn of c.turns) {
      for (const card of turn.actions) {
        const row = rows.find((r) => r.action_id === card.actionId);
        if (!row || card.busy !== null) continue;
        // a materials draft: where its draft is (writing → ready / failed) and whether the page has a newer one
        // (a practice draft: where it is shown in the page, for the card's 在页面里看)
        if (inPageDraft(card.kind) && row.revise != null) {
          const next = cardRevise(row, card.kind === REVISE_KIND && hasNewerRevise(rows, { action_id: row.action_id, page_id: row.revise.page_id || row.page_id }));
          if (JSON.stringify(next) !== JSON.stringify(card.revise)) { card.revise = next; turn.rev++; any = true; }
        }
        // Only forward: a stale row read after a local decision (still `proposed`) never pushes the card back.
        if (row.status !== card.status) {
          if (!forward(card.status, row.status)) continue;
        } else if (cardPhase(row.status) === "wait") {
          if (card.kind === "rule") {
            const before = cardSig(card);
            applyActionOut(card, row);
            if (cardSig(card) !== before) { turn.rev++; any = true; }
          } else if (!draftable(card) && card.diff.length === 0 && row.diff.length > 0) { card.diff = row.diff.map((d) => ({ ...d })); turn.rev++; any = true; }
          continue;
        }
        const before = cardSig(card);
        applyActionOut(card, row);
        if (cardSig(card) === before) continue;
        if (cardPhase(card.status) === "live") for (const id of cardPageIds(card)) this.fresh.add(id);
        turn.rev++; any = true;
        this.goalRow(c, card, row);
      }
    }
    if (any) this.notify();
  }

  /** A waiting card without a page list shows 改动对比 from the ledger (one read per card). */
  private wantDiff(c: Conversation, actionId: string): void {
    const f = this.cardOf(c, actionId);
    if (!f || draftable(f.card) || cardPhase(f.card.status) !== "wait" || f.card.diff.length > 0 || this.diffAsked.has(actionId)) return;
    this.diffAsked.add(actionId);
    void (async () => {
      try {
        const row = (await this.deps.api.listActions("proposed")).find((r) => r.action_id === actionId);
        if (!row || cardPhase(f.card.status) !== "wait" || f.card.busy !== null) return;
        applyActionOut(f.card, row);
        this.changed(f.turn);
      } catch (e) {
        this.diffAsked.delete(actionId);
        if (cardPhase(f.card.status) === "wait") { f.card.error = ACTION_COPY.error(e); this.changed(f.turn); }
      }
    })();
  }

  /**
   * One of the options under the question the last turn ended on: sent as his message, through the normal send (the
   * composer's draft, quote and files stay where they are). The server resumes that turn with it.
   */
  async answerAsk(callId: string, text: string): Promise<void> {
    const c = this.conv(); if (!c || !this.live() || c.busy || c.restore === "loading") return;
    const asked = waitingAsk(c.turns);
    if (asked === null || asked.callId !== callId) return;
    const turn = newTurn(this.deps.newId(), text, null);
    c.turns.push(turn);
    await this.run(c, turn);
    if (asked.kind === CHECK_KIND) this.diagnosisAnswered();
  }

  /** A tap on the 摸底 grid (ask_rough): shown at once, here and in 学点新的 (diag()); sent with 提交. */
  markRough(callId: string, block: string, v: RoughMark): void {
    const c = this.conv(), asked = c ? waitingAsk(c.turns) : null;
    if (!c || asked === null || asked.callId !== callId || asked.rough === null || asked.rough.sent) return;
    asked.rough.marks[block] = v;
    this.changed(c.turns[c.turns.length - 1]!);
  }

  /** 提交 on the grid: his readable line as his message, the marks as its reply_data (they answer the ask_rough). */
  async submitRough(callId: string): Promise<void> {
    const c = this.conv(); if (!c || !this.live() || c.restore === "loading") return;
    while (c.busy) await c.running;      // the turn that asked is still ending: his answer waits for it, never dropped
    if (!this.live()) return;
    const asked = waitingAsk(c.turns);
    if (asked === null || asked.callId !== callId || asked.rough === null || !roughReady(asked.rough)) return;
    asked.rough.sent = true;
    const turn = newTurn(this.deps.newId(), roughText(asked.rough), null);
    turn.replyData = { marks: { ...asked.rough.marks } };
    c.turns.push(turn);
    // 已提交 only once the server answered this message; 重试 and the resend after a reconnect run through here again
    const rough = asked.rough;
    rough.outcome = "sending";
    turn.afterRun = (): void => {
      rough.outcome = turn.status === "done" ? undefined : "failed";
      for (const t of c.turns) t.rev++;
      this.notify();
    };
    await this.run(c, turn);
    this.diagnosisAnswered();
  }

  /**
   * Where the chat's diagnosis is (学点新的's rows and graph): the 摸底 grid's taps while it waits, or the block its 细问
   * asks; null when the conversation waits on neither.
   */
  diag(): LearnDiag | null {
    const c = this.conv();
    if (!c) return null;
    // the conversation's last 摸底 grid: its taps (open) or what he sent (shown until the map has a band for the block)
    const rough = [...c.turns].reverse().flatMap((t) => [...t.asks].reverse()).find((a) => a.rough !== null)?.rough ?? null;
    const asked = waitingAsk(c.turns);
    const asking = asked !== null && asked.rough === null ? asked.block?.id ?? null : null;
    if (rough === null && asking === null) return null;
    return { asking, marks: rough?.marks ?? {}, open: rough !== null && !rough.sent };
  }

  /** An answer of the diagnosis went in: the goal's map is read again, so 学点新的 recolours its blocks. */
  private diagnosisAnswered(): void { this.currentGoal()?.reread(); }

  /** The answer of a 「我早会了」 confirm question; the key shows at once. */
  async answerQuiz(quizId: string, choice: number): Promise<void> {
    const f = this.quizOf(quizId);
    if (!f || !this.live()) return;
    const { quiz, turn } = f;
    if (quiz.busy || quiz.pick !== null || quiz.i >= quiz.items.length) return;
    quiz.busy = true; quiz.error = "";
    this.changed(turn);
    try {
      const known = quizId.startsWith(KNOWN_QUIZ) ? quizId.slice(KNOWN_QUIZ.length) : null;
      if (known !== null) {
        const out = await this.deps.api.knownAnswer(known, choice);
        quizAnswered(quiz, choice, { correct: out.correct, answer: out.correct_index, answered: 1, total: 1,
          correct_count: out.correct ? 1 : 0, mastery: [] });
        if (out.message !== "") ewNotice(out.message);
        // a wrong answer took the claim back: the card and 活动记录 follow the ledger row (now undone)
        if (out.undone) { try { this.deps.ledgerChanged?.(); } catch (err) { console.warn("EvoWiki assistant: ledger hook failed", err); } }
      }
    } catch (e) {
      quiz.busy = false; quiz.error = QUIZ_COPY.error(e);
    } finally { this.changed(turn); }
  }

  /**
   * 「这个我早会了」 confirmed (or a restored one still open): its ONE confirm question as a one-question quiz under the
   * turn (`known:<action id>`; answered through client.knownAnswer, a wrong answer takes the claim back).
   * 404 = nothing to answer (answered already, undone): nothing shown.
   */
  private async askKnown(turn: Turn, actionId: string): Promise<void> {
    const quizId = KNOWN_QUIZ + actionId;
    if (turn.quizzes.some((z) => z.quizId === quizId) || !this.live()) return;
    try {
      const item = await this.deps.api.knownItem(actionId);
      if (turn.quizzes.some((z) => z.quizId === quizId)) return;
      turn.quizzes.push(newQuiz(quizId, [{ stem: item.stem, options: [...item.options], cell: "" }]));
      this.changed(turn);
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) return;
      console.warn("EvoWiki assistant: could not read the confirm question", e);
      const card = turn.actions.find((a) => a.actionId === actionId);
      if (card) { card.error = KNOWN_COPY.itemFailed; this.changed(turn); }
    }
  }

  /** Restored 「我早会了」 cards that are done: their confirm question again, while it is unanswered. */
  private askKnownAgain(c: Conversation): void {
    for (const t of c.turns) for (const a of t.actions) if (a.kind === KNOWN_KIND && cardPhase(a.status) === "live") void this.askKnown(t, a.actionId);
  }

  nextQuiz(quizId: string): void {
    const f = this.quizOf(quizId);
    if (!f || f.quiz.pick === null) return;
    quizNext(f.quiz);
    this.changed(f.turn);
  }

  /** The pages of the shown cards. */
  private pollIds(): string[] {
    const ids = new Set<string>();
    for (const t of this.conv()?.turns ?? []) for (const a of t.actions) for (const id of cardPageIds(a)) ids.add(id);
    return [...ids];
  }

  /**
   * /wiki/writes is read while a visible card has a page that is queued / planning / writing (and once for a page never read);
   * when nothing is, the timer stops. Nothing else re-renders the cards: the renderer paints the status in place.
   */
  private schedulePoll(): void {
    if (this.poll !== null || !this.live()) return;
    const ids = this.pollIds();
    const first = ids.some((id) => !this.written.has(id));
    const moving = ids.some((id) => this.pending(id));
    // a page still 未生成 starts when he opens it, from anywhere: looked at again now and then
    if (!first && !moving && !ids.some((id) => this.writes.get(id)?.state === "unwritten")) return;
    this.poll = setTimeout(() => { this.poll = null; void this.readWrites(); }, first ? 0 : moving ? WRITES_POLL_MS : UNWRITTEN_POLL_MS);
  }

  /** A page still to ask about: queued / planning / writing, or one this session made that has no run yet (a few tries). */
  private pending(id: string): boolean {
    const w = this.writes.get(id);
    if (isWriting(w)) return true;
    return (w === undefined || w.state === "unwritten") && this.fresh.has(id) && (this.misses.get(id) ?? 0) < WRITES_MISSES;
  }

  /** A panel that shows the conversation registers how to tell whether it is on screen; reads wait while none is. Returns the remover. */
  observeVisible(probe: () => boolean): () => void {
    this.probes.add(probe);
    this.schedulePoll();
    return () => { this.probes.delete(probe); };
  }
  private shown(): boolean { return this.probes.size === 0 || [...this.probes].some((p) => p()); }

  private async readWrites(): Promise<void> {
    const ids = this.pollIds();
    if (ids.length === 0 || !this.live()) return;
    // Hidden panel: nothing is asked; a cheap timer looks again until it is on screen.
    if (!this.shown()) { this.poll = setTimeout(() => { this.poll = null; void this.readWrites(); }, WRITES_POLL_MS); return; }
    try {
      const out = sharedWritesFor(this.deps.sharedWrites?.() ?? null, ids) ?? await this.deps.api.writes(ids);
      for (const id of ids) {
        const w = out.find((x) => x.page_id === id);
        if (w) this.writes.set(id, w); else this.writes.delete(id);
        if (w && w.state !== "unwritten") this.misses.delete(id); else this.misses.set(id, (this.misses.get(id) ?? 0) + 1);
        this.written.add(id);
      }
    } catch (e) {
      console.warn("EvoWiki assistant: could not read the writing status", e);
      if (this.live() && this.poll === null) this.poll = setTimeout(() => { this.poll = null; void this.readWrites(); }, WRITES_RETRY_MS);
      return;
    }
    this.notify();
  }

  /** The page-edited hook; a throwing hook is logged and never changes the card. */
  /** The open proposal cards as the server has them now (a card the chat's 「好」 decided, its voided siblings); the page follows. */
  private async syncProposals(c: Conversation): Promise<void> {
    if (c.sessionId === null) return;
    try {
      const fresh = this.turnsFrom(await this.deps.api.getSession(c.sessionId)).flatMap((t) => t.proposals);
      for (const t of c.turns) {
        for (const o of t.proposals) {
          const n = fresh.find((x) => x.proposalId === o.proposalId);
          if (n === undefined || o.status !== "open" || n.status === "open") continue;
          Object.assign(o, { status: n.status, applied: n.applied, detail: n.detail, actionId: n.actionId, canUndo: n.canUndo });
          t.rev++;
          if (n.status === "accepted") this.edited(o.draft.page_id, n.applied);
        }
      }
      this.notify();
    } catch (e) { console.warn("EvoWiki assistant: could not read the cards after a confirm", e); }
  }

  private edited(pageId: string, applied: string[]): void {
    try { this.deps.pageEdited?.(pageId, [...applied]); } catch (e) { console.warn("EvoWiki assistant: page-edited hook failed", e); }
  }

  /**
   * The demo's +: a conversation with questions is forgotten (stored id and turns; the draft and quote stay) and a
   * new one starts; an empty one only closes the history view. No request. The input gets the focus either way.
   */
  async newSession(): Promise<void> {
    const c = this.conv(); if (!c) return;
    this.history = { ...this.history, open: false, notice: "" };
    this.focusTick++;
    if (c.busy || c.ending) { this.detach(c); this.notify(); await this.save(c.key, null); return; }
    if (c.turns.length === 0 && c.sessionId === null) { this.notify(); return; }
    this.clear(c);
    this.notify();
    if (this.deps.store.get(c.key) !== null) await this.save(c.key, null);
  }

  /**
   * A conversation with a running turn makes way for a new one under its key: it goes on in the background (its turn
   * keeps streaming into it; nothing it does writes the stored session id of the key any more), and a fresh conversation
   * takes the key with the composer's draft, quote and uploaded attachments. Returns the fresh one.
   */
  private detach(c: Conversation): Conversation {
    this.convs.delete(c.key);
    this.background.add(c);
    const fresh = this.create(c.key, c.target);
    fresh.sessionId = null;
    fresh.draft = c.draft; fresh.quote = c.quote;
    fresh.attachments = c.attachments.filter((a) => a.sourceId !== undefined && a.status !== "uploading");
    c.draft = ""; c.quote = null; c.attachments = c.attachments.filter((a) => !fresh.attachments.includes(a));
    return fresh;
  }

  /** The conversation is the one shown under its key (not one sent to the background). */
  private isShown(c: Conversation): boolean { return this.convs.get(c.key) === c; }

  private clear(c: Conversation): void {
    c.sessionId = null; c.turns = []; c.restore = "none"; c.recap = null;
    this.focusTick++;
  }

  /** The clock button: opens the list (and reads it) or closes it. */
  toggleHistory(): void {
    if (!this.configured()) return;
    if (this.history.open) { this.history = { ...this.history, open: false, notice: "" }; this.notify(); return; }
    this.history = { ...this.history, open: true, notice: "" };
    void this.loadHistory();
  }

  /** The search box: the server searches titles, page titles and message text. */
  searchHistory(q: string): void {
    if (q === this.history.q) return;
    this.history = { ...this.history, q };
    void this.loadHistory();
  }

  /** (Re)reads the list for the current q; also the 重试 button. */
  async loadHistory(): Promise<void> {
    if (!this.live()) return;
    const seq = ++this.listSeq;
    this.history = { ...this.history, status: "loading", error: "", notice: "" };
    this.notify();
    try {
      const api = this.deps.api;
      const [items, pending] = await Promise.all([api.listSessions(this.history.q.trim()), api.forgetRequests ? api.forgetRequests() : []]);
      if (seq !== this.listSeq) return;
      for (const x of items) this.titles.set(x.session_id, x);
      const deleting = pending.map((p) => {
        const seen = this.titles.get(p.session_id);
        return { ...p, title: seen?.title ?? "", at: seen?.last_activity_at ?? p.requested_at };
      });
      const gone = new Set(pending.map((p) => p.session_id));
      if (this.history.deleting.some((old) => !gone.has(old.session_id) && !items.some((item) => item.session_id === old.session_id))) {
        try { this.deps.memoryForgotten?.(); } catch (error) { console.warn("EvoWiki assistant: forget reconciliation failed", error); }
      }
      this.history = { ...this.history, status: "ready", items: items.filter((x) => !gone.has(x.session_id)), deleting, error: "" };
    } catch (e) {
      if (seq !== this.listSeq) return;
      this.history = { ...this.history, status: "error", items: [], error: historyReason(classifyError(e, "restore")) };
    }
    this.notify();
  }

  /** A history row's ⋯ → 删除: the dialog opens (删除对话 waits for the read) with what the conversation left behind, nothing ticked. */
  async openForget(sessionId: string): Promise<void> {
    const api = this.deps.api;
    if (!this.live() || !api.forgetPreview) return;
    const d: ForgetDialog = { sessionId, title: this.titles.get(sessionId)?.title ?? "", preview: null, busy: false,
      picks: { revokeRuleIds: [], revokeFactKeys: [], deleteSourceIds: [] } };
    this.forgetDialog = d; this.notify();
    try {
      const preview = await api.forgetPreview(sessionId);
      if (this.forgetDialog !== d) return;
      this.forgetDialog = { ...d, preview };
    } catch (e) {
      if (this.forgetDialog !== d) return;
      this.forgetDialog = null;                  // nothing to ask about: the dialog closes and says what to do
      ewNotice(forgetReason(e, FORGET_COPY.previewFailed));
    }
    this.notify();
  }

  /** A checkbox of the dialog: a rule / profile line to revoke too, or a material to delete too. */
  pickForget(kind: "rule" | "fact" | "source", id: string, on: boolean): void {
    const d = this.forgetDialog; if (!d || d.busy) return;
    const k = kind === "rule" ? "revokeRuleIds" : kind === "fact" ? "revokeFactKeys" : "deleteSourceIds";
    const rest = d.picks[k].filter((x) => x !== id);
    this.forgetDialog = { ...d, picks: { ...d.picks, [k]: on ? [...rest, id] : rest } };
    this.notify();
  }

  /** 取消 (or Esc / a click outside): nothing is sent. */
  closeForget(): void {
    if (!this.forgetDialog || this.forgetDialog.busy) return;
    this.forgetDialog = null; this.notify();
  }

  /**
   * 删除对话: one DELETE with the ticked ids. Done (200) → 「已删除」; recorded, not finished (202) → 「删除还没完成，会自动继续」
   * and its row shows 删除中. Either way a conversation showing it here is emptied. A refusal (503, offline) keeps the dialog
   * open (nothing changed) and a notice says what to do.
   */
  async confirmForget(): Promise<void> {
    const d = this.forgetDialog, api = this.deps.api;
    if (!d || d.busy || d.preview === null || !api.deleteSession) return;
    this.forgetDialog = { ...d, busy: true }; this.notify();
    try {
      const out = await api.deleteSession(d.sessionId, d.picks);
      this.forgetDialog = null;
      for (const c of [...this.convs.values(), ...this.background]) {
        if (c.sessionId !== d.sessionId) continue;
        if (this.background.has(c)) { this.background.delete(c); continue; }
        this.clear(c);
        if (this.deps.store.get(c.key) === d.sessionId) await this.save(c.key, null);
      }
      // an answer of another open conversation that quoted the deleted one is gone from the server: read it again
      if (out.state === "committed") for (const c of this.convs.values()) if (c.sessionId !== null && !c.busy) void this.restoreConv(c);
      if (out.state === "committed") {
        try { this.deps.memoryForgotten?.(); } catch (error) { console.warn("EvoWiki assistant: forget reconciliation failed", error); }
      }
      ewNotice(out.state === "committed" ? FORGET_COPY.done : FORGET_COPY.notYet);
      void this.loadHistory();
    } catch (e) {
      if (this.forgetDialog?.sessionId === d.sessionId) this.forgetDialog = { ...d, busy: false };
      ewNotice(forgetReason(e, FORGET_COPY.failed));
    }
    this.notify();
  }

  /**
   * A history row: the current conversation becomes that session (messages, proposal decisions; later questions go to
   * it). A failed read keeps the current conversation and says so in the list.
   */
  async resume(sessionId: string): Promise<"ok" | "gone" | "failed"> {
    let c = this.conv();
    const fail = (reason: string): void => {
      this.history = { ...this.history, notice: `${ASSISTANT_COPY.resumeFailed}：${reason}` };
      this.notify();
    };
    if (!this.live()) return "failed";
    if (!c) { fail(ASSISTANT_COPY.resumeNoNote); return "failed"; }
    if (c.sessionId === sessionId && c.restore === "none" && !this.unread.has(c)) { this.history = { ...this.history, open: false, q: "", notice: "" }; this.notify(); return "ok"; }
    // the session of a conversation sent to the background: that conversation comes back as it is (its turn may still run)
    const cur = c;
    const back = [...this.background].find((b) => b.key === cur.key && b.sessionId === sessionId);
    if (back !== undefined) {
      const composer = cur.busy || cur.ending ? this.detach(cur) : cur;          // the composer's text goes along
      this.background.delete(back);
      this.convs.set(back.key, back);
      back.draft = composer.draft; back.quote = composer.quote; back.attachments = composer.attachments;
      this.history = { ...this.history, open: false, q: "", notice: "" };
      this.notify();
      await this.save(back.key, sessionId);
      return "ok";
    }
    const seq = ++this.resumeSeq;
    this.unread.delete(c);                 // this read is the one: the panel's own first read must not run as well
    this.history = { ...this.history, notice: "" };
    try {
      const out = await this.deps.api.getSession(sessionId);
      if (seq !== this.resumeSeq) return "failed";
      this.noteGone(out.gone_sources);
      if (c.busy || c.ending) c = this.isShown(c) ? this.detach(c) : this.conv() ?? c;
      this.unread.delete(c);
      c.sessionId = sessionId;
      c.turns = this.turnsFrom(out);
      c.recap = out.recap ?? null;
      c.restore = "none";
      this.restoreGoals(c, out);
      this.history = { ...this.history, open: false, q: "", notice: "" };
      this.notify();
      await this.save(c.key, sessionId);
      void this.reattach(c, sessionId);
      this.askKnownAgain(c);
      return "ok";
    } catch (e) {
      if (seq !== this.resumeSeq) return "failed";
      const why = classifyError(e, "restore");
      fail(historyReason(why));
      return why.kind === "session_gone" ? "gone" : "failed";
    }
  }

  /** Re-runs a failed restore (one read). */
  async reload(): Promise<void> {
    const c = this.conv();
    if (!c || !this.live() || c.restore !== "failed" || c.sessionId === null) return;
    await this.restoreConv(c);
  }

  dispose(): void {
    this.modelsSeq++;
    if (this.poll !== null) { clearTimeout(this.poll); this.poll = null; }
    this.abort.abort();
    for (const ctl of this.follows.values()) ctl.abort();
    this.follows.clear();
    for (const run of this.goalRuns.values()) run.dispose();
    this.goalRuns.clear();
    this.subs.clear();
  }

  private configured(): boolean {
    const id = this.deps.identity();
    return id.server !== "" && id.user !== "";
  }

  private live(): boolean { return this.configured() && !this.abort.signal.aborted; }

  private conv(): Conversation | null {
    const t = targetOf(this.active);
    const key = sessionKey(this.deps.identity(), t);
    // No note shown (before any show, too): the conversation without a page is there to type into. Its stored session
    // (the last free chat, e.g. after a plugin reload) is read once, after this call (it may run inside a render).
    const had = this.convs.get(key);
    if (had !== undefined || t.kind !== "free") return had ?? null;
    const c = this.create(key, t);
    if (c.sessionId !== null) queueMicrotask(() => { if (this.unread.has(c)) void this.restoreConv(c); });   // unless a show read it first
    return c;
  }

  private notify(): void {
    for (const fn of [...this.subs]) fn();
    this.schedulePoll();
  }

  /** One create (only when the conversation has no session) + one message. Never retried automatically. */
  private async run(c: Conversation, turn: Turn): Promise<void> {
    c.busy = true;
    let ended = (): void => {};
    const mine = new Promise<void>((r) => { ended = r; });
    c.running = mine;
    closeAsks(c.turns, turn);            // his message answers the question the last turn ended on
    this.notify();
    if (c.ending) { await c.ending; c.ending = null; this.notify(); }   // the turn he stopped is over on the server
    let phase: Phase = "create";
    let sid: string | null = c.sessionId;
    const existing = sid !== null;
    let gone = false;
    let applied = 0;
    try {
      sid = sid ?? await this.session(c);
      phase = "message";
      if (this.abort.signal.aborted) throw new AbortedError();
      await this.follow(c, sid, turn, () => { applied++; });
      if (turn.status === "streaming") failTurn(turn, { kind: "broken" });
      if (turn.error?.kind === "event" && turn.error.code === "llm_busy") ewNotice(errorText(turn.error));
    } catch (e) {
      let err = (phase === "message" ? attachmentRefusal(e, turn.attachments.length > 0) : null) ?? classifyError(e, phase);
      if (err.kind === "attachment") this.backToComposer(c, turn);
      // The message did go out and part of the answer arrived: the answer broke off, it was not "not sent".
      if (err.kind === "offline" && phase === "message" && applied > 0) err = { kind: "broken" };
      if (err.kind === "session_gone" && sid !== null) { gone = true; await this.forget(c, sid); }
      failTurn(turn, err);
    } finally {
      turn.afterRun?.();
      if (c.running === mine) { c.busy = false; c.ending = null; this.background.delete(c); }   // its turn ended: the server has it; a history row reads it back
      ended();
      this.notify();
      if (existing && !gone && sid !== null) await this.touch(c, sid);
    }
    // 不用摸底了 ended the diagnosis on the server: while one is open, each turn reads it again, and the goal goes on
    if (turn.status === "done" && this.currentGoal()?.flow.state.draftId != null) this.diagnosisAnswered();
    const shownDraft = turn.status === "done" ? waitingAsk(c.turns)?.draftId ?? null : null;
    if (shownDraft !== null) void this.followDiagnosis(shownDraft).catch((e: unknown) => { console.warn("EvoWiki assistant: could not read the diagnosis's goal", e); });
  }

  /**
   * A message refused for an attachment: its text, quote and attachments go back to the composer (where nothing else was
   * typed meanwhile) so the user can take the bad one out and send again. The turn stays with the server's reason.
   */
  private backToComposer(c: Conversation, turn: Turn): void {
    if (c.draft === "" && c.quote === null) {
      c.quote = turn.quote;
      c.draft = turn.quote && turn.question === ASSISTANT_COPY.quoteOnlyQuestion ? "" : turn.question;
    }
    const back = turn.attachments.filter((a) => !c.attachments.some((d) => d.sourceId === a.sourceId))
      .map((a) => backToDraft(this.deps.newId(), a));
    c.attachments = [...c.attachments, ...back].slice(0, MAX_ATTACHMENTS);
  }

  /**
   * The message, then - when its stream breaks off (dropped connection, or it ended without done/error) after the server
   * gave a resume id - a resume from that id: the server resends from there and text pieces carry their position, so
   * nothing shown is doubled or lost. A resume that breaks again (the backend restarting is not back yet) is tried again
   * after each wait of RESUME_WAITS_MS; the last break is the caller's error (the turn goes on on the server: reading the
   * session again follows it, `reattach`).
   */
  private async follow(c: Conversation, sid: string, turn: Turn, counted: () => void): Promise<void> {
    const api = this.deps.api;
    let lastId: string | null = null;
    const onEvent = this.sink(c, turn, counted);
    const onId = (id: string): void => { lastId = id; };
    let failure: { error: unknown } | null = null;
    try {
      await api.sendMessage(sid, messageBody(turn, this.viewing), onEvent, this.abort.signal, onId);
    } catch (e) { failure = { error: e }; }
    const broke = failure === null || ["offline", "broken"].includes(classifyError(failure.error, "message").kind);
    if (lastId !== null && api.resumeMessage && turn.status === "streaming" && broke && !this.abort.signal.aborted) {
      const sleep = this.deps.sleep ?? ((ms: number) => new Promise<void>((r) => { setTimeout(r, ms); }));
      for (let i = 0; ; i++) {
        try {
          await api.resumeMessage(sid, lastId, onEvent, this.abort.signal, onId);
          return;
        } catch (e) {
          const again = i < RESUME_WAITS_MS.length && turn.status === "streaming" && !this.abort.signal.aborted
            && ["offline", "broken"].includes(classifyError(e, "message").kind);
          if (!again) { turn.lost = true; throw e; }
        }
        await sleep(RESUME_WAITS_MS[i]!);
        if (this.abort.signal.aborted || !this.live()) return;
      }
    }
    if (failure !== null) throw failure.error;
  }

  /**
   * A goal_new card arrived or changed in a stream. A new draft that names the row it replaced (`replaces`: edit_goal_blocks
   * rejected that row with detail `replaced` on the server; the stream only brings the new card) marks only that card
   * 已换成新的分法 at once — a second new_goal names none and leaves the other waiting cards alone. Then 学点新的 hears of each.
   */
  private goalCard(c: Conversation, card: ActionCardState, isNew: boolean): void {
    if (isNew && cardPhase(card.status) === "wait" && card.replaces !== "") {
      for (const t of c.turns) for (const old of t.actions) {
        if (old === card || old.actionId !== card.replaces || old.kind !== GOAL_KIND || cardPhase(old.status) !== "wait" || old.busy !== null) continue;
        old.status = "rejected"; old.detail = REPLACED; old.draft = old.items.map(rowOf);
        old.edited = false; t.rev++;
        this.goalRow(c, old);
      }
    }
    this.goalRow(c, card);
  }

  /** Folds one turn's stream events into the conversation (the live send, a resume, a re-attach after a reload). */
  private sink(c: Conversation, turn: Turn, counted: () => void): (e: AssistantEvent) => void {
    return (e: AssistantEvent): void => {
      counted();
      // A card is shown once: an undo turn re-lists the id of the card it undid, and that card is updated where it stands.
      const earlier = e.type === "action" ? this.cardOf(c, e.action_id) : null;
      const was = earlier?.card.status ?? null;
      // 「帮我复习」: the review card shows in the chat as this call ends (nothing due ends it not ok); 「考考我这页」 names the page
      if (e.type === "tool_end" && e.ok && turn.tools.find((l) => l.callId === e.call_id)?.name === OPEN_REVIEW) this.deps.results?.openReview(e.refs?.[0]?.page_id ?? null);
      if (e.type === "action" && earlier !== null && earlier.turn !== turn) {
        updateActionCard(earlier.card, e);
        earlier.turn.rev++; turn.rev++;
      } else {
        const kept = e.type === "done" ? this.keptSources(c) : null;
        applyEvent(turn, e);
        if (kept !== null && e.type === "done") {
          this.followNewUses(c, kept, [...turn.attachments, ...(e.session_attachments ?? []).map((a) => fromOut(a))]);
        }
        // 「好」 decided a page edit: its card sits in an earlier turn and no event names it
        if (e.type === "done" && e.confirmed === true) void this.syncProposals(c);
        if (e.type === "done") {
          try { this.deps.turnDone?.(); } catch (err) { console.warn("EvoWiki assistant: turn hook failed", err); }
        }
      }
      if (e.type === "action") {
        this.wantDiff(c, e.action_id);
        const f = this.cardOf(c, e.action_id);
        if (f && f.card.kind === GOAL_KIND) this.goalCard(c, f.card, was === null);
        if (f && e.status === "executed") for (const id of cardPageIds(f.card)) this.fresh.add(id);
        // a tool moved this row (undo in the chat, or an edit done at once): the open note follows the page
        if (f && f.card.status !== was && e.page_id && (e.status === "undone" || e.status === "executed")) this.edited(e.page_id, []);
        // the tool wrote a ledger row (a new card, or a row whose status moved): the same row waits on its page
        // and its count follows; a re-sent row with the same status asks nothing
        if (f && f.card.status !== was) {
          try { this.deps.ledgerChanged?.(); } catch (err) { console.warn("EvoWiki assistant: ledger hook failed", err); }
        }
      }
      this.notify();
    };
  }

  /**
   * After a session read: a last question with no answer may still be running on the server (a durable turn that went on
   * while the plugin reloaded). Its stream is followed again from the start, so 正在思考 and the answer show and the
   * conversation stays busy until it ends. Nothing to follow (the turn is gone, or an inline server: 404, or an empty
   * stream) leaves the turn as it was read; any other failure is that turn's error.
   */
  private async reattach(c: Conversation, sid: string): Promise<void> {
    const api = this.deps.api;
    const turn = c.turns[c.turns.length - 1];
    const mid = turn ? this.unanswered.get(turn) : undefined;
    if (!turn || mid === undefined || !api.resumeMessage || c.busy || !this.live()) return;
    let seen = 0;
    let ended = (): void => {};
    const mine = new Promise<void>((r) => { ended = r; });
    c.busy = true; c.running = mine; turn.status = "streaming"; turn.thinking = true; turn.sentAt = Date.now(); turn.rev++;
    this.notify();
    const settle = (): void => { turn.status = "done"; turn.thinking = false; turn.rev++; };
    try {
      await api.resumeMessage(sid, `${mid}:0`, this.sink(c, turn, () => { seen++; }), this.abort.signal);
      if (turn.status === "streaming") { if (seen === 0) settle(); else failTurn(turn, { kind: "broken" }); }
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) settle();
      else failTurn(turn, classifyError(e, "message"));
    } finally {
      if (c.running === mine) { c.busy = false; c.ending = null; }
      ended();
      this.notify();
    }
  }

  /** The conversation's session id, creating it once (a pending create is shared). */
  private session(c: Conversation): Promise<string> {
    if (c.sessionId !== null) return Promise.resolve(c.sessionId);
    let p = this.opening.get(c);
    if (!p) {
      p = this.open(c).finally(() => { this.opening.delete(c); });
      this.opening.set(c, p);
    }
    return p;
  }

  private async open(c: Conversation): Promise<string> {
    const t = c.target;
    // a page the server no longer has (its note left in the vault, another account's note) must not cost him the chat: the chat goes on without that page
    const r = await this.deps.api.createSession(t.kind === "page" ? { page_id: t.pageId } : t.kind === "note" ? { note_concept_id: t.conceptId } : {})
      .catch((e: unknown) => {
        if (t.kind === "page" && e instanceof ApiError && e.status === 404) return this.deps.api.createSession({});
        throw e;
      });
    c.sessionId = r.session_id;
    // If this write fails the in-memory id is still valid: the turn goes on with it (only the remembering is lost). A
    // conversation sent to the background meanwhile no longer owns the key's stored id.
    if (this.isShown(c)) await this.save(c.key, r.session_id);
    return r.session_id;
  }

  /** A plugin-data write that never throws: a failed save is logged and never freezes or fails a conversation. */
  private async save(key: string, sessionId: string | null): Promise<void> {
    try { await this.deps.store.set(key, sessionId); } catch (e) { console.warn("EvoWiki assistant: could not save the session id", e); }
  }

  /** Marks a stored session as recently used (plugin-data write only, no request); skipped if the stored id changed. */
  private async touch(c: Conversation, sid: string): Promise<void> {
    if (this.isShown(c) && this.deps.store.get(c.key) === sid) await this.save(c.key, sid);
  }

  /** Compare-and-clear: the stored id is removed only if it is still the one that turned out gone. */
  private async forget(c: Conversation, sid: string): Promise<void> {
    if (c.sessionId === sid) c.sessionId = null;
    if (this.isShown(c) && this.deps.store.get(c.key) === sid) await this.save(c.key, null);
  }

  private async restoreConv(c: Conversation): Promise<void> {
    this.unread.delete(c);
    const sid = c.sessionId;
    if (sid === null) return;
    c.restore = "loading"; this.notify();
    try {
      const out = await this.deps.api.getSession(sid);
      if (c.sessionId !== sid) return;
      this.noteGone(out.gone_sources);
      c.turns = this.turnsFrom(out);
      c.recap = out.recap ?? null;
      c.restore = "none";
      this.restoreGoals(c, out);
      for (const t of c.turns) for (const a of t.actions) this.wantDiff(c, a.actionId);
      await this.touch(c, sid);
      void this.reattach(c, sid);
      this.askKnownAgain(c);
    } catch (e) {
      if (c.sessionId !== sid) return;
      if (classifyError(e, "restore").kind === "session_gone") {
        c.restore = "gone";
        await this.forget(c, sid);
      } else c.restore = "failed";
    } finally { this.notify(); }
  }

  /** Each user message with the following assistant message is one finished turn; an unanswered one has an empty answer. */
  private turnsFrom(out: SessionOut): Turn[] {
    const messages: MessageOut[] = out.messages;
    const decided = new Map((out.decisions ?? []).map((d) => [d.proposal_id, d]));
    const drafts = new Map((out.proposals ?? []).map((p) => [p.proposal_id, p]));
    const stored = new Map<string, ActionCard>((out.actions ?? []).map((a) => [a.action_id, a]));
    const shown = new Set<string>();     // an id re-listed by a later (undo) turn is the same card
    const turns: Turn[] = [];
    let open: Turn | null = null;
    for (const m of messages) {
      if (m.role === "user") {
        // The quote this question was sent with, replayed from the session record.
        const q = m.selection;
        const quote: Quote | null = q ? { kind: q.kind, text: q.text, heading: q.heading ?? null } : null;
        open = newTurn(this.deps.newId(), m.text, quote, (m.attachments ?? []).map((a) => fromOut(a)));
        open.status = "done";
        // how it was sent, as the session log stored it (学点新的's 开始 / 确认): its cards show as when it was live
        if (m.intent === "learn_goal" || m.intent === "diagnose" || m.intent === "resolve_memory_conflicts") open.intent = m.intent;
        if (m.draft_id) open.diagnose = m.draft_id;
        this.unanswered.set(open, m.message_id);
        turns.push(open);
        continue;
      }
      // An assistant message with no question before it still shows, under an empty question.
      const t: Turn = open ?? newTurn(this.deps.newId(), "", null);
      if (!open) { t.status = "done"; turns.push(t); }
      this.unanswered.delete(t);
      t.answer = m.text;
      t.stopped = m.stopped === true;
      // The 「更早的 N 轮对话已省略」 notice (omit_<entry>) is no answer: it gets no 👍 / 👎 (the server knows no such answer).
      t.answerId = m.message_id.startsWith(OMIT_PREFIX) ? null : m.message_id;
      t.rating = ratingOf(m.rating);
      // The turn's stored tool calls: the same tool line a live turn showed (finished: ok / failed).
      t.tools = (m.tools ?? []).map((x) => ({ callId: x.call_id, name: x.name, effect: x.effect, summary: x.summary ?? "",
        status: x.ok ? "ok" : "failed", sources: x.sources ?? [], label: "", ...rowFields(x) }));
      const ids = (m.proposals ?? []).filter((id) => decided.get(id)?.detail !== "replaced");
      t.restored = { sources: m.sources ?? [], proposalCount: ids.length };
      // The same card a live stream builds: undecided → open (buttons work), decided → its latest result.
      t.proposals = ids.flatMap((id) => {
        const p = drafts.get(id);
        if (!p) return [];
        const d = decided.get(id);
        return [{ proposalId: id, draft: p.draft, status: d?.status ?? "open", applied: d?.applied ?? [], error: null, detail: d?.detail ?? "", change: p.change, actionId: d?.action_id ?? "", canUndo: d?.can_undo ?? false, undoing: false, note: "" }];
      });
      // The ledger cards and quizzes of the turn, with the ledger's status now.
      t.actions = (m.actions ?? []).flatMap((id) => {
        const a = stored.get(id);
        if (!a || shown.has(id)) return [];
        shown.add(id);
        return [newActionCard(a)];
      });
      // the question the turn ended on, still answerable while no message came after it: the turn ends with it, so it
      // sits under the answer, as it showed live
      t.asks = (m.asks ?? []).map((x) => askOf(x, x.status));
      t.askAt = t.asks.length > 0 ? t.answer.length : null;
      // Where the live stream put the cards in the answer (text → card → text). The stored message carries it as
      // `card_at`; without it the whole answer shows above the cards.
      const at = m.card_at;
      t.cardAt = typeof at === "number" && at > 0 && at < m.text.length && t.actions.length > 0 ? at : null;
      open = null;
    }
    return turns;
  }
}

/** Why a forget call failed, as what to do: the server's own sentence on a 503 (「删除没完成，请再试一次」), else
 *  `fallback` (offline: check the connection). */
function forgetReason(e: unknown, fallback: string): string {
  if (e instanceof ApiError && e.status === 503 && e.detail.trim() !== "") return e.detail.trim();
  if (classifyError(e, "restore").kind === "offline") return FORGET_COPY.offline;
  return fallback;
}
