// Scripted AssistantApi for tests. The only plugin code that imports it is the dev-only demo fixture
// (src/wiki/fixtures-demo.ts, plugin data `fixture: "demo"`); the assistant itself never does.
import { AbortedError, ApiError } from "../api/errors";
import type { AssistantApi, Decision } from "./client";
import type { ActionOut, MadeOut } from "../wiki/bot/bot-schema";
import type { ActionDraft, AssistantEvent, KnownAnswerOut, KnownItemOut, MessageIn, ProposalDecisionOut, RatingIn, RatingOut, SessionCreateIn, SessionCreateOut,
  SessionOut, SessionSummaryOut } from "./types";
import type { WriteOut } from "../wiki/schema";

/** `lastId`: the SSE id the server gave with the step's last event (a durable turn's resume point). */
export type FakeStep = ({ events: AssistantEvent[] } | { throws: unknown } | { eventsThenThrow: AssistantEvent[]; error: unknown })
  & { lastId?: string };

/** A realistic answer: two tool calls (each a row: running, then done) with sources, streamed text, one proposal, one observation, done. */
export const FAKE_ANSWER: AssistantEvent[] = [
  { type: "tool_start", call_id: "c1", name: "web_search", effect: "network", summary: "PCA 为什么要最大化方差", label: "正在网上搜索：PCA 为什么要最大化方差" },
  { type: "tool_end", call_id: "c1", ok: true, sources: [{ title: "Principal component analysis - Wikipedia", url: "https://en.wikipedia.org/wiki/Principal_component_analysis", page_id: null, segment_id: null }],
    label: "联网搜了「PCA 为什么要最大化方差」 · 1 条", group: "web_search", group_label: "联网搜了 {n} 次", items: ["Principal component analysis - Wikipedia"], refs: [] },
  { type: "tool_start", call_id: "c2", name: "read_page", effect: "read", summary: "主成分分析 · 直观理解", label: "正在读《主成分分析》第 2 节" },
  { type: "tool_end", call_id: "c2", ok: true, sources: [{ title: "主成分分析", page_id: "page_pca", url: null, segment_id: null }],
    label: "读了《主成分分析》第 2 节", group: "read_page", group_label: "读了 {k} 页", items: ["主成分分析"],
    refs: [{ title: "主成分分析", page_id: "page_pca", url: null, segment_id: null }] },
  { type: "text_delta", text: "方差大的方向，", at: null },
  { type: "text_delta", text: "保留的信息最多。", at: null },
  { type: "text_delta", text: "\n\n可以把它想成**找一条最能把点分开的直线**。", at: null },
  { type: "proposal", proposal_id: "pr_fake_1", draft: { kind: "add_section", cue: "", answer: "", page_id: "page_pca", section_id: "s2",
    heading: "为什么是方差", cells: ["pca.mechanism"], text: "方差大的方向保留的信息最多……", why: "你两次问到为什么要最大化方差" }, change: { old_heading: null, old_body: "", new_body: "方差大的方向保留的信息最多……" } },
  { type: "observed", observations: 1, changes: [{ cell: "pca.mechanism", from_band: "likely", to_band: "shaky" }] },
  { type: "done", message_id: "m_fake_1", tool_calls: 2, llm_calls: 3, cost_usd: null, attachments: [], session_attachments: [], stopped: false, confirmed: false },
];

/** Deep copy so callers never share mutable objects with the script (or with each other). */
const clone = <T>(x: T): T => structuredClone(x);
/**
 * Runs `f` synchronously (so sendMessage delivers its events before the promise settles) and turns a throw into a
 * rejection carrying the scripted reason unchanged. Scripts may reject with non-Error reasons on purpose; the cast is
 * type-only, nothing is wrapped.
 */
const settle = <T>(f: () => T): Promise<T> => {
  try { return Promise.resolve(f()); } catch (e: unknown) { const reason = e as Error; return Promise.reject(reason); }
};
const isThrow = (x: unknown): x is { throws: unknown } => typeof x === "object" && x !== null && "throws" in x;

function play(step: FakeStep, onEvent: (e: AssistantEvent) => void, onId?: (id: string) => void): void {
  if ("throws" in step) throw step.throws;
  const events = "eventsThenThrow" in step ? step.eventsThenThrow : step.events;
  for (const e of events) onEvent(clone(e));
  if (step.lastId !== undefined) onId?.(step.lastId);
  if ("eventsThenThrow" in step) throw step.error;
}

export class FakeAssistantApi implements AssistantApi {
  calls: {
    createSession: SessionCreateIn[];
    sendMessage: { sessionId: string; body: MessageIn }[];
    decide: { proposalId: string; decision: Decision }[];
    getSession: string[];
    listSessions: { q: string; limit: number | undefined }[];
  } = { createSession: [], sendMessage: [], decide: [], getSession: [], listSessions: [] };
  /** Consumed in order; default `{ session_id: "ses_fake_<n>" }`. */
  sessionReplies: (SessionCreateOut | { throws: unknown })[] = [];
  /** Consumed in order; default FAKE_ANSWER. */
  messageReplies: FakeStep[] = [];
  /** Consumed in order; default `{ proposal_id, status: accepted|rejected, applied: [] }`. */
  decisionReplies: (ProposalDecisionOut | { throws: unknown })[] = [];
  /** getSession replies by id; a missing id rejects with ApiError(404). */
  sessionOuts = new Map<string, SessionOut | { throws: unknown }>();
  /** listSessions replies, consumed in order; when empty the call answers from `sessionList` filtered by `q`. */
  listReplies: (SessionSummaryOut[] | { throws: unknown })[] = [];
  /** The default listing: substring (case-insensitive) match of q over title and page_title, as the server's first two fields. */
  sessionList: SessionSummaryOut[] = [];
  private sessionCount = 0;

  // ---- the Bot ledger, writes, quizzes (separate from `calls`, whose shape tests compare whole) ----
  /** Every action / quiz / writes call, in order. */
  actionCalls: { op: "approve" | "reject" | "undo" | "redraft" | "list" | "writes" | "knownItem" | "knownAnswer"; id?: string; draft?: ActionDraft;
    status?: string; ids?: string[]; i?: number; choice?: number }[] = [];
  /** Ledger rows by id: approve / reject / undo answer with the row moved to executed / rejected / undone (and `actionNewPages` on approve). */
  actionRows = new Map<string, ActionOut>();
  actionMade = new Map<string, MadeOut[]>();
  /** Consumed in order before the rows above; a `throws` rejects. */
  actionReplies: (ActionOut | { throws: unknown })[] = [];
  /** GET /wiki/writes: consumed in order; when empty the call answers from `writesNow` filtered by the ids. */
  writesReplies: (WriteOut[] | { throws: unknown })[] = [];
  writesNow: WriteOut[] = [];
  knownItems: (KnownItemOut | { throws: unknown })[] = [];
  knownReplies: (KnownAnswerOut | { throws: unknown })[] = [];

  private decided(id: string, status: string, over: Partial<ActionOut> = {}): ActionOut {
    const row = this.actionRows.get(id) ?? { action_id: id, kind: "page_split", scope: "new_page", level: "ask", status: "proposed", source: "assistant" as const,
      summary: "", why: "", content: {}, page_id: null, detail: "", created_at: "2026-10-15T09:00:00Z", decided_at: null, expires_at: null,
      can_undo: false, diff: [], confirm: true, cost_usd: null, made: [], revise: null, result: {}, section_id: null };
    const next: ActionOut = { ...row, status, can_undo: status === "executed", ...over };
    this.actionRows.set(id, next);
    return clone(next);
  }
  private actionReply(id: string, status: string, over: Partial<ActionOut> = {}): ActionOut {
    const r = this.actionReplies.shift();
    if (r !== undefined) { if (isThrow(r)) throw r.throws; this.actionRows.set(id, clone(r)); return clone(r); }
    return this.decided(id, status, over);
  }

  /** POST …/draft (a goal card's kept edits): unset = the card keeps its edits until 确认 (as the demo fixture does). */
  editDraft?: (actionId: string, draft: ActionDraft) => Promise<ActionOut>;

  approveAction(actionId: string, draft?: ActionDraft): Promise<ActionOut> {
    return settle(() => {
      this.actionCalls.push({ op: "approve", id: actionId, ...(draft ? { draft: clone(draft) } : {}) });
      const rule = draft && "text" in draft ? this.actionRows.get(actionId) : undefined;
      return this.actionReply(actionId, "executed", { made: this.actionMade.get(actionId) ?? [],
        ...(rule ? { content: { ...rule.content, text: draft && "text" in draft ? draft.text : rule.content.text } } : {}) });
    });
  }
  /** 重试: the row waits again (status proposed, its draft fitting the page now); `actionReplies` first, like the others. */
  redraftAction(actionId: string): Promise<ActionOut> {
    return settle(() => {
      this.actionCalls.push({ op: "redraft", id: actionId });
      const r = this.actionReplies.shift();
      if (r !== undefined) { if (isThrow(r)) throw r.throws; this.actionRows.set(actionId, clone(r)); return clone(r); }
      const row = this.actionRows.get(actionId);
      return this.decided(actionId, "proposed", { detail: "", decided_at: null, can_undo: false,
        ...(row?.revise ? { revise: { ...row.revise, stale: false } } : {}) });
    });
  }
  rejectAction(actionId: string): Promise<ActionOut> {
    return settle(() => { this.actionCalls.push({ op: "reject", id: actionId }); return this.actionReply(actionId, "rejected"); });
  }
  undoAction(actionId: string): Promise<ActionOut> {
    return settle(() => { this.actionCalls.push({ op: "undo", id: actionId }); return this.actionReply(actionId, "undone", { made: [] }); });
  }
  listActions(status?: string): Promise<ActionOut[]> {
    return settle(() => {
      this.actionCalls.push({ op: "list", ...(status ? { status } : {}) });
      return clone([...this.actionRows.values()].filter((r) => status === undefined || r.status === status));
    });
  }
  writes(pageIds: string[]): Promise<WriteOut[]> {
    return settle(() => {
      this.actionCalls.push({ op: "writes", ids: [...pageIds] });
      const r = this.writesReplies.shift();
      if (r !== undefined) { if (isThrow(r)) throw r.throws; return clone(r); }
      return clone(this.writesNow.filter((w) => pageIds.includes(w.page_id)));
    });
  }
  knownItem(actionId: string): Promise<KnownItemOut> {
    return settle(() => {
      this.actionCalls.push({ op: "knownItem", id: actionId });
      const r = this.knownItems.shift();
      if (r === undefined) throw new ApiError(404, "unknown action");
      if (isThrow(r)) throw r.throws;
      return clone(r);
    });
  }
  knownAnswer(actionId: string, choice: number): Promise<KnownAnswerOut> {
    return settle(() => {
      this.actionCalls.push({ op: "knownAnswer", id: actionId, choice });
      const r = this.knownReplies.shift();
      if (r === undefined) throw new ApiError(404, "unknown action");
      if (isThrow(r)) throw r.throws;
      return clone(r);
    });
  }

  /** Rating posts, in order (kept apart from `calls`, whose shape tests compare whole). */
  rated: { sessionId: string; messageId: string; body: RatingIn }[] = [];
  /** Consumed in order; default: the body echoed back as the stored rating (the note's whitespace collapsed, as the server does). */
  rateReplies: (RatingOut | { throws: unknown })[] = [];

  rate(sessionId: string, messageId: string, body: RatingIn): Promise<RatingOut> {
    return settle(() => {
      this.rated.push({ sessionId, messageId, body: clone(body) });
      const r = this.rateReplies.shift();
      if (r !== undefined) { if (isThrow(r)) throw r.throws; return clone(r); }
      const down = body.value === "down";
      return { message_id: messageId, value: body.value, chips: down ? [...(body.chips ?? [])] : [],
        note: down ? (body.note ?? "").split(/\s+/).filter((w) => w !== "").join(" ") : "" };
    });
  }

  createSession(body: SessionCreateIn): Promise<SessionCreateOut> {
    return settle(() => {
      this.calls.createSession.push(clone(body));
      this.sessionCount += 1;
      const r = this.sessionReplies.shift() ?? { session_id: `ses_fake_${this.sessionCount}` };
      if (isThrow(r)) throw r.throws;
      return clone(r);
    });
  }

  sendMessage(sessionId: string, body: MessageIn, onEvent: (e: AssistantEvent) => void, signal?: AbortSignal,
    onId?: (id: string) => void): Promise<void> {
    return settle(() => {
      this.calls.sendMessage.push({ sessionId, body: clone(body) });
      if (signal?.aborted) throw new AbortedError();
      play(this.messageReplies.shift() ?? { events: FAKE_ANSWER }, onEvent, onId);
    });
  }

  /** Stop calls (kept apart from `calls`, whose shape tests compare whole) and what the next ones answer. */
  stops: string[] = [];
  stopReplies: unknown[] = [];

  stopTurn(sessionId: string): Promise<void> {
    return settle(() => {
      this.stops.push(sessionId);
      const r = this.stopReplies.shift();
      if (r !== undefined) { const reason = r as Error; throw reason; }
    });
  }

  /** Resume calls (kept apart from `calls`, whose shape tests compare whole). */
  resumed: { sessionId: string; lastEventId: string }[] = [];
  /** Consumed in order; default: nothing more, the stream just ends. */
  resumeReplies: FakeStep[] = [];

  resumeMessage(sessionId: string, lastEventId: string, onEvent: (e: AssistantEvent) => void, signal?: AbortSignal,
    onId?: (id: string) => void): Promise<void> {
    return settle(() => {
      this.resumed.push({ sessionId, lastEventId });
      if (signal?.aborted) throw new AbortedError();
      play(this.resumeReplies.shift() ?? { events: [] }, onEvent, onId);
    });
  }

  decide(proposalId: string, decision: Decision): Promise<ProposalDecisionOut> {
    return settle(() => {
      this.calls.decide.push({ proposalId, decision });
      const r = this.decisionReplies.shift()
        ?? { proposal_id: proposalId, status: decision === "accept" ? "accepted" : "rejected", applied: [], detail: "", action_id: decision === "accept" ? `act_${proposalId}` : "", can_undo: decision === "accept" };
      if (isThrow(r)) throw r.throws;
      return clone(r);
    });
  }

  getSession(sessionId: string): Promise<SessionOut> {
    return settle(() => {
      this.calls.getSession.push(sessionId);
      const r = this.sessionOuts.get(sessionId);
      if (r === undefined) throw new ApiError(404, "unknown session");
      if (isThrow(r)) throw r.throws;
      return clone(r);
    });
  }

  listSessions(q: string, limit?: number): Promise<SessionSummaryOut[]> {
    return settle(() => {
      this.calls.listSessions.push({ q, limit });
      const r = this.listReplies.shift();
      if (r !== undefined) { if (isThrow(r)) throw r.throws; return clone(r); }
      const needle = q.trim().toLowerCase();
      return clone(this.sessionList.filter((x) => needle === "" || `${x.title}
${x.page_title}`.toLowerCase().includes(needle)));
    });
  }
}
