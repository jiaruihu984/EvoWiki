import { jsonCall, type ClientConfig, type JsonTransport, type StreamTransport } from "../api/client";
import { apiErrorOf } from "../api/errors";
import { SseDataError, SseDecoder } from "../api/sse";
import type { ActionOut } from "../wiki/bot/bot-schema";
import { ContractError, parseDecisionOut, parseEvent, parseKnownAnswerOut, parseKnownItemOut, parseRatingOut, parseSessionCreateOut,
  parseSessionListOut, parseSessionOut } from "./parse";
import { type ActionDraft, type AssistantEvent, type ModelIn, type ModelsOut, type KnownAnswerOut, type KnownItemOut, type MessageIn, type ProposalDecisionOut,
  type RatingIn, type RatingOut, type SessionCreateIn, type SessionCreateOut, type SessionOut, type SessionSummaryOut } from "./types";
import { WRITE_STATES, type WriteOut } from "../wiki/schema";
import { parseForgetOut, parseForgetPending, parseForgetPreviewOut, type ForgetPicks } from "./forget";
import type { ForgetOut, ForgetPendingOut, ForgetPreviewOut } from "./types";

export type Decision = "accept" | "reject";

export interface AssistantApi {
  createSession(body: SessionCreateIn): Promise<SessionCreateOut>;
  /** `onId`: each event's SSE id (`<message_id>:<n>`, a durable turn's resume point), when the server sends one. */
  sendMessage(sessionId: string, body: MessageIn, onEvent: (e: AssistantEvent) => void, signal?: AbortSignal,
    onId?: (id: string) => void): Promise<void>;
  /** Follows a turn again from the last SSE id seen (GET /assistant/sessions/{id}/events). Optional: fakes may omit it. */
  resumeMessage?(sessionId: string, lastEventId: string, onEvent: (e: AssistantEvent) => void, signal?: AbortSignal,
    onId?: (id: string) => void): Promise<void>;
  decide(proposalId: string, decision: Decision): Promise<ProposalDecisionOut>;
  getSession(sessionId: string): Promise<SessionOut>;
  /** The user's conversations with at least one question, newest activity first; `q` is a server-side search ("" = all). */
  listSessions(q: string, limit?: number): Promise<SessionSummaryOut[]>;
  /** The Bot ledger (/wiki/bot/actions): 确认 (with the edited `draft` when the list was changed), 取消, 撤销. A 409 is an ApiError. */
  approveAction(actionId: string, draft?: ActionDraft): Promise<ActionOut>;
  rejectAction(actionId: string): Promise<ActionOut>;
  undoAction(actionId: string): Promise<ActionOut>;
  /** 重试 on an in-page draft that no longer fits its page (POST …/redraft; see WikiApi.botRedraft). Optional: no 重试 without it. */
  redraftAction?(actionId: string): Promise<ActionOut>;
  /**
   * A waiting draft's card edit kept on its row without approving (POST /wiki/bot/actions/{id}/draft: a goal's blocks, each
   * move / × / 加一块). A refusal is an ApiError (409, its detail the reason). Optional: a fake without it keeps edits local.
   */
  editDraft?(actionId: string, draft: ActionDraft): Promise<ActionOut>;
  /** The ledger rows (newest first), optionally of one status: a card's 改动对比 and its state when 个性化 is not loaded. */
  listActions(status?: string): Promise<ActionOut[]>;
  /** Where each page's write is (GET /wiki/writes); a card polls it while one of its rows is queued / planning / writing. */
  writes(pageIds: string[]): Promise<WriteOut[]>;
  /** 「这个我早会了」's one confirm question and its answer. */
  knownItem(actionId: string): Promise<KnownItemOut>;
  knownAnswer(actionId: string, choice: number): Promise<KnownAnswerOut>;
  /** 👍 / 👎 on an answer (`messageId` = the answer's own message id); `value` null clears it. Returns the stored rating. */
  rate(sessionId: string, messageId: string, body: RatingIn): Promise<RatingOut>;
  /** The composer's model picker (GET /assistant/models). Optional: a fake without it has no picker. */
  listModels?(): Promise<ModelsOut>;
  /** The user's pick for their turns (POST /assistant/model): a concrete model, a reasoning level, or both; a 409 (ApiError,
   *  its detail the reason) when the model cannot be used now. */
  pickModel?(pick: ModelIn): Promise<ModelsOut>;
  /** 删除对话: what the dialog lists (GET …/forget-preview), the delete (DELETE /assistant/sessions/{id}: `committed`, or
   *  `failed` = a 202, finished by the server's retry) and the 「删除中」 sessions (GET /assistant/forget-requests). A 503 is
   *  an ApiError. Optional: a fake without them has no 删除. */
  forgetPreview?(sessionId: string): Promise<ForgetPreviewOut>;
  deleteSession?(sessionId: string, picks: ForgetPicks): Promise<ForgetOut>;
  forgetRequests?(): Promise<ForgetPendingOut[]>;
  /** The chat's stop: the session's running turn ends as stopped on the stream its sender follows. An ApiError 404: none is running. */
  stopTurn(sessionId: string): Promise<void>;
}

const enc = encodeURIComponent;
/** 个性化's 待确认的记忆 (GET, read by the wiki controller with the tab: wiki/api.ts memoryConflicts; settled only in the chat). */
export const MEMORY_CONFLICTS_PATH = "/assistant/memory/conflicts";

/** The ledger fields a card reads; anything else on the row is passed through untouched. */
function actionOut(x: unknown): ActionOut {
  if (x === null || typeof x !== "object" || Array.isArray(x)) throw new ContractError("ActionOut is not an object");
  const o = x as Record<string, unknown>;
  for (const k of ["action_id", "kind", "status", "summary", "detail"]) if (typeof o[k] !== "string") throw new ContractError(`ActionOut.${k} must be a string`);
  if (typeof o.can_undo !== "boolean") throw new ContractError("ActionOut.can_undo must be a boolean");
  if (o.made !== undefined && !(Array.isArray(o.made) && o.made.every((m) => m !== null && typeof m === "object" && typeof (m as { page_id?: unknown }).page_id === "string")))
    throw new ContractError("ActionOut.made must be a list of made pages");
  return x as ActionOut;
}
/** `pass` is a reserved word on the Python side; the wire name stays `pass`. */
export function writeOut(x: unknown): WriteOut {
  if (x === null || typeof x !== "object" || Array.isArray(x)) throw new ContractError("WriteOut is not an object");
  const o = x as Record<string, unknown>;
  if (typeof o.page_id !== "string") throw new ContractError("WriteOut.page_id must be a string");
  if (typeof o.state !== "string" || !(WRITE_STATES as readonly string[]).includes(o.state)) throw new ContractError("WriteOut.state has an unknown value");
  if (o.state === "unwritten") {
    if (o.run_id !== null) throw new ContractError("WriteOut.run_id must be null for an unwritten page");
  } else if (typeof o.run_id !== "string" || o.run_id.length === 0) {
    throw new ContractError("WriteOut.run_id must identify the page's task");
  }
  return x as WriteOut;
}

/** GET /assistant/models, POST /assistant/model (assistant/contracts.py ModelsOut). */
export function parseModelsOut(x: unknown): ModelsOut {
  if (x === null || typeof x !== "object" || Array.isArray(x)) throw new ContractError("ModelsOut is not an object");
  const o = x as Record<string, unknown>;
  if (!Array.isArray(o.models)) throw new ContractError("ModelsOut.models must be a list");
  for (const m of o.models as unknown[]) {
    const r = (m ?? {}) as Record<string, unknown>;
    if (typeof r.id !== "string" || typeof r.name !== "string" || typeof r.provider !== "string" || typeof r.provider_name !== "string" || typeof r.available !== "boolean" || typeof r.reason !== "string")
      throw new ContractError("ModelsOut.models has a malformed model");
    const levels = r.levels;
    if (!Array.isArray(levels) || levels.some((l) => { const v = (l ?? {}) as Record<string, unknown>; return (typeof v.reasoning !== "string" || !["default", "none", "minimal", "low", "medium", "high", "xhigh", "max"].includes(v.reasoning)) || typeof v.name !== "string"; }))
      throw new ContractError("ModelsOut.models has malformed levels");
  }
  if (o.current !== null && typeof o.current !== "string") throw new ContractError("ModelsOut.current must be a string or null");
  if (o.reasoning !== null && (typeof o.reasoning !== "string" || !["default", "none", "minimal", "low", "medium", "high", "xhigh", "max"].includes(o.reasoning))) throw new ContractError("ModelsOut.reasoning has an unknown value");
  return x as ModelsOut;
}

export class AssistantClient implements AssistantApi {
  constructor(
    private readonly config: () => ClientConfig,
    private readonly http: JsonTransport,
    private readonly sse: StreamTransport,
  ) {}

  async createSession(body: SessionCreateIn): Promise<SessionCreateOut> {
    return parseSessionCreateOut(await this.call("POST", "/assistant/sessions", body));
  }

  async getSession(sessionId: string): Promise<SessionOut> {
    return parseSessionOut(await this.call("GET", `/assistant/sessions/${enc(sessionId)}`));
  }

  async listSessions(q: string, limit = 100): Promise<SessionSummaryOut[]> {
    const qs = (q === "" ? "" : `q=${enc(q)}&`) + `limit=${limit}`;
    return parseSessionListOut(await this.call("GET", `/assistant/sessions?${qs}`)).sessions;
  }

  async decide(proposalId: string, decision: Decision): Promise<ProposalDecisionOut> {
    return parseDecisionOut(await this.call("POST", `/assistant/proposals/${enc(proposalId)}/${decision}`, {}));
  }

  async approveAction(actionId: string, draft?: ActionDraft): Promise<ActionOut> {
    return actionOut(await this.call("POST", `/wiki/bot/actions/${enc(actionId)}/approve`, draft ? { draft } : {}));
  }

  async editDraft(actionId: string, draft: ActionDraft): Promise<ActionOut> {
    return actionOut(await this.call("POST", `/wiki/bot/actions/${enc(actionId)}/draft`, { draft }));
  }

  async rejectAction(actionId: string): Promise<ActionOut> {
    return actionOut(await this.call("POST", `/wiki/bot/actions/${enc(actionId)}/reject`, {}));
  }

  async redraftAction(actionId: string): Promise<ActionOut> {
    return actionOut(await this.call("POST", `/wiki/bot/actions/${enc(actionId)}/redraft`, {}));
  }

  async undoAction(actionId: string): Promise<ActionOut> {
    return actionOut(await this.call("POST", `/wiki/bot/actions/${enc(actionId)}/undo`, {}));
  }

  async listActions(status?: string): Promise<ActionOut[]> {
    const r = await this.call("GET", `/wiki/bot/actions${status ? `?status=${enc(status)}` : ""}`);
    if (!Array.isArray(r)) throw new ContractError("actions is not a list");
    return r.map(actionOut);
  }

  async writes(pageIds: string[]): Promise<WriteOut[]> {
    const r = await this.call("GET", `/wiki/writes?unwritten=true&page_ids=${enc(pageIds.join(","))}`);
    if (!Array.isArray(r)) throw new ContractError("writes is not a list");
    return r.map(writeOut);
  }


  async knownItem(actionId: string): Promise<KnownItemOut> {
    return parseKnownItemOut(await this.call("GET", `/assistant/actions/${enc(actionId)}/confirm`));
  }

  async knownAnswer(actionId: string, choice: number): Promise<KnownAnswerOut> {
    return parseKnownAnswerOut(await this.call("POST", `/assistant/actions/${enc(actionId)}/confirm`, { choice }));
  }

  async rate(sessionId: string, messageId: string, body: RatingIn): Promise<RatingOut> {
    // Chips and the note only go with a 👎 (the server refuses chips otherwise and drops the note on a clear).
    const out: RatingIn = body.value === "down" ? { value: "down", chips: [...(body.chips ?? [])], note: body.note ?? "" } : { value: body.value };
    return parseRatingOut(await this.call("POST", `/assistant/sessions/${enc(sessionId)}/messages/${enc(messageId)}/rating`, out));
  }

  async listModels(): Promise<ModelsOut> {
    return parseModelsOut(await this.call("GET", "/assistant/models"));
  }

  async pickModel(pick: ModelIn): Promise<ModelsOut> {
    const body: ModelIn = {};
    if (pick.model) body.model = pick.model;
    if (pick.reasoning) body.reasoning = pick.reasoning;
    return parseModelsOut(await this.call("POST", "/assistant/model", body));
  }

  async forgetPreview(sessionId: string): Promise<ForgetPreviewOut> {
    return parseForgetPreviewOut(await this.call("GET", `/assistant/sessions/${enc(sessionId)}/forget-preview`));
  }

  async deleteSession(sessionId: string, picks: ForgetPicks): Promise<ForgetOut> {
    return parseForgetOut(await this.call("DELETE", `/assistant/sessions/${enc(sessionId)}`, {
      revoke_rule_ids: picks.revokeRuleIds, revoke_fact_keys: picks.revokeFactKeys, delete_source_ids: picks.deleteSourceIds }));
  }

  async stopTurn(sessionId: string): Promise<void> {
    await this.call("POST", `/assistant/sessions/${enc(sessionId)}/stop`, {});
  }

  async forgetRequests(): Promise<ForgetPendingOut[]> {
    return parseForgetPending(await this.call("GET", "/assistant/forget-requests"));
  }

  /** One POST; its events stream through `events`. */
  async sendMessage(sessionId: string, body: MessageIn, onEvent: (e: AssistantEvent) => void, signal?: AbortSignal,
    onId?: (id: string) => void): Promise<void> {
    const out: MessageIn = { text: body.text };
    if (body.selection) {
      const q = body.selection;
      out.selection = { kind: q.kind, text: q.text, ...(q.heading ? { heading: q.heading } : {}), ...(q.artifact_id ? { artifact_id: q.artifact_id } : {}) };
    }
    if (body.attachments && body.attachments.length > 0) out.attachments = [...body.attachments];
    // the page whose note is open as he sends (any conversation): the turn's 当前页
    if (typeof body.page_id === "string" && body.page_id !== "") out.page_id = body.page_id;
    // 学点新的's 开始: the server runs new_goal with the form's goal itself
    if (body.intent === "learn_goal") { out.intent = "learn_goal"; out.goal = body.goal ?? ""; }
    // GoalRun's diagnosis in the chat (its 「确认」)
    if (body.intent === "diagnose") { out.intent = "diagnose"; out.draft_id = body.draft_id ?? null; }
    // 个性化's 待确认的记忆 · 处理: the server reads his open groups first
    if (body.intent === "resolve_memory_conflicts") out.intent = "resolve_memory_conflicts";
    if (body.intent === "reteach_section") out.intent = "reteach_section";
    // the 摸底 grid's marks: they answer the waiting ask_rough
    if (body.reply_data) out.reply_data = { marks: { ...body.reply_data.marks } };
    await this.events("POST", `/assistant/sessions/${enc(sessionId)}/messages`, JSON.stringify(out), onEvent, signal, onId);
  }

  async resumeMessage(sessionId: string, lastEventId: string, onEvent: (e: AssistantEvent) => void, signal?: AbortSignal,
    onId?: (id: string) => void): Promise<void> {
    await this.events("GET", `/assistant/sessions/${enc(sessionId)}/events?last_event_id=${enc(lastEventId)}`, undefined,
      onEvent, signal, onId);
  }

  /** One streamed request. Events are delivered as they arrive; the first malformed frame stops delivery and rejects after the transport resolves. */
  private async events(method: "GET" | "POST", path: string, body: string | undefined, onEvent: (e: AssistantEvent) => void,
    signal: AbortSignal | undefined, onId: ((id: string) => void) | undefined): Promise<void> {
    const dec = new SseDecoder();
    let failure: { error: unknown } | undefined;
    // Runs inside Node's res.on("data"): a throw there would be uncaught, so record it and rethrow later.
    const onChunk = (chunk: string): void => {
      if (failure) return;
      try {
        for (const f of dec.push(chunk)) {
          const e = parseEvent(f.event, f.data);
          if (e) onEvent(e);
          if (f.id !== undefined) onId?.(f.id);
        }
      } catch (e) { failure = { error: e instanceof SseDataError ? new ContractError(e.message) : e }; }
    };
    const res = await this.sse.stream({
      method, url: this.config().baseUrl + path, headers: this.headers(body !== undefined), signal,
      ...(body !== undefined ? { body } : {}),
    }, onChunk);
    if (res.status < 200 || res.status >= 300) throw apiErrorOf(res.status, res.text);
    if (failure) throw failure.error;
  }

  private headers(json: boolean): Record<string, string> {
    const h: Record<string, string> = { "X-EvoWiki-User": this.config().userId };
    if (json) h["Content-Type"] = "application/json";
    return h;
  }

  private call(method: "GET" | "POST" | "DELETE", path: string, body?: unknown): Promise<unknown> {
    return jsonCall<unknown, "GET" | "POST" | "DELETE">(this.http, this.config(), method, path, body,
      (_s, t) => new ContractError(`response is not JSON: ${t.slice(0, 200)}`));
  }
}
