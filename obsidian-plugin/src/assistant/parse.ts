import { ATTACHMENT_READ_MODES, CONTRACT_FIELDS, DECISION_STATUSES, ERROR_CODES, MESSAGE_ROLES, PROPOSAL_KINDS, PROPOSAL_STATE_STATUSES, RATING_CHIPS, RATING_VALUES, TOOL_EFFECTS,
  type AssistantEvent, type CellChange, type ProposalDecisionOut, type ProposalDraft, type SessionCreateOut,
  type SessionListOut, type SessionOut, type Source, type MessageOut, type KnownItemOut, type KnownAnswerOut, type RatingOut } from "./types";

/** The server sent something the shared contract does not allow. Never guessed around (fail closed). */
export class ContractError extends Error {
  constructor(msg: string) { super(`assistant contract: ${msg}`); this.name = "ContractError"; }
}

type O = Record<string, unknown>;
function record(x: unknown, cls: keyof typeof CONTRACT_FIELDS): O {
  if (x === null || typeof x !== "object" || Array.isArray(x)) throw new ContractError(`${cls} is not an object`);
  const allowed: readonly string[] = CONTRACT_FIELDS[cls];
  for (const k of Object.keys(x)) if (!allowed.includes(k)) throw new ContractError(`${cls} has unknown field ${k}`);
  return x as O;
}
const str = (o: O, k: string, cls: string): string => {
  if (typeof o[k] !== "string") throw new ContractError(`${cls}.${k} must be a string`); return o[k];
};
const optStr = (o: O, k: string, cls: string): void => {
  if (o[k] !== undefined && o[k] !== null && typeof o[k] !== "string") throw new ContractError(`${cls}.${k} must be a string or null`);
};
const int = (o: O, k: string, cls: string): void => {
  if (typeof o[k] !== "number" || !Number.isInteger(o[k])) throw new ContractError(`${cls}.${k} must be an integer`);
};
const bool = (o: O, k: string, cls: string): void => { if (typeof o[k] !== "boolean") throw new ContractError(`${cls}.${k} must be a boolean`); };
const oneOf = (o: O, k: string, vals: readonly string[], cls: string): void => {
  if (typeof o[k] !== "string" || !vals.includes(o[k])) throw new ContractError(`${cls}.${k} has an unknown value`);
};
const optArr = (o: O, k: string, cls: string, each: (x: unknown) => void): void => {
  if (o[k] === undefined) return;
  if (!Array.isArray(o[k])) throw new ContractError(`${cls}.${k} must be a list`);
  (o[k] as unknown[]).forEach(each);
};
const strItem = (cls: string) => (x: unknown) => { if (typeof x !== "string") throw new ContractError(`${cls} item must be a string`); };
const ASK_STATUSES = ["waiting", "answered"] as const;
/** A question the turn asked the learner (AskEvent / AskCardOut): its kind's payload stays as sent (the card reads it). */
function askBody(o: O, c: string): void {
  str(o, "call_id", c); str(o, "kind", c);
  if (o.payload === null || typeof o.payload !== "object" || Array.isArray(o.payload)) throw new ContractError(`${c}.payload must be an object`);
}

/** A finished tool row's fields (ToolEnd, ToolCallOut; assistant/tool_rows.py): each optional (an older server sends none). */
function toolRow(o: O, c: string): void {
  for (const k of ["label", "group", "group_label"]) if (o[k] !== undefined) str(o, k, c);
  optArr(o, "items", c, strItem(c)); optArr(o, "refs", c, source);
}
function source(x: unknown): Source {
  const o = record(x, "Source"); str(o, "title", "Source");
  for (const k of ["url", "page_id", "segment_id"]) optStr(o, k, "Source");
  return o as unknown as Source;
}
function draft(x: unknown): ProposalDraft {
  const o = record(x, "ProposalDraft"); const c = "ProposalDraft";
  oneOf(o, "kind", PROPOSAL_KINDS, c); str(o, "page_id", c); optStr(o, "section_id", c);
  str(o, "heading", c); str(o, "text", c); str(o, "why", c); optArr(o, "cells", c, strItem(c));
  return o as unknown as ProposalDraft;
}
/** A Selection (the quote a user message was sent with): text, optional nullable heading. */
function quoteRecord(x: unknown): void {
  const o = record(x, "Selection"); str(o, "text", "Selection"); optStr(o, "heading", "Selection");
  if (o.kind !== "text" && o.kind !== "wrong_answer") throw new ContractError("Selection.kind has an unknown value");
}
const optInt = (o: O, k: string, cls: string): void => { if (o[k] !== undefined && o[k] !== null) int(o, k, cls); };
/** An AttachmentOut (a chat attachment on its user message, or on a turn's done). */
function attachment(x: unknown): void {
  const o = record(x, "AttachmentOut"); const c = "AttachmentOut";
  for (const k of ["source_id", "title", "kind", "status"]) str(o, k, c);
  bool(o, "deleted", c); optArr(o, "uses", c, strItem(c));
  if (o.bytes !== undefined) int(o, "bytes", c);
  optInt(o, "pages", c); optStr(o, "expires_at", c); optInt(o, "chars", c);
  if (o.importing !== undefined) bool(o, "importing", c);
  optArr(o, "unreadable_pages", c, (p) => { if (typeof p !== "number" || !Number.isInteger(p)) throw new ContractError(`${c}.unreadable_pages item must be an integer`); });
  if (o.read !== undefined && o.read !== null) {
    const r = record(o.read, "AttachmentRead"); const rc = "AttachmentRead";
    oneOf(r, "mode", ATTACHMENT_READ_MODES, rc);
    for (const k of ["pages_total", "pages_read", "sections", "tokens"]) optInt(r, k, rc);
    optArr(r, "unseen_pages", rc, (p) => { if (typeof p !== "number" || !Number.isInteger(p)) throw new ContractError(`${rc}.unseen_pages item must be an integer`); });
  }
}
function change(x: unknown): CellChange {
  const o = record(x, "CellChange"); for (const k of ["cell", "from_band", "to_band"]) str(o, k, "CellChange");
  return o as unknown as CellChange;
}

function actionItem(x: unknown): void {
  const o = record(x, "ActionItem"); str(o, "key", "ActionItem"); str(o, "title", "ActionItem");
  optStr(o, "page_id", "ActionItem"); optStr(o, "covers", "ActionItem"); optStr(o, "level", "ActionItem");
  if (o.n !== undefined && o.n !== null) int(o, "n", "ActionItem");
}
/** The shared fields of an ActionEvent and an ActionCard. */
function actionBody(o: O, c: string): void {
  str(o, "action_id", c); str(o, "kind", c); str(o, "title", c);
  optArr(o, "items", c, actionItem);
  if (o.status !== undefined) str(o, "status", c);
  if (o.confirm !== undefined) bool(o, "confirm", c);
  if (o.can_undo !== undefined) bool(o, "can_undo", c);
}
const EVENTS: Record<string, (o: unknown) => AssistantEvent> = {
  text_delta: (x) => { const o = record(x, "TextDelta"); str(o, "text", "TextDelta");
    if (o.at !== undefined && o.at !== null) { int(o, "at", "TextDelta"); if ((o.at as number) < 0) throw new ContractError("TextDelta.at must be >= 0"); }
    return o as never; },
  status: (x) => { const o = record(x, "StatusLine"); str(o, "text", "StatusLine");
    if ((o.text as string).trim() === "") throw new ContractError("StatusLine.text must not be empty"); return o as never; },
  tool_start: (x) => { const o = record(x, "ToolStart"); const c = "ToolStart";
    str(o, "call_id", c); str(o, "name", c); oneOf(o, "effect", TOOL_EFFECTS, c);
    if (o.summary !== undefined) str(o, "summary", c); if (o.label !== undefined) str(o, "label", c); return o as never; },
  tool_end: (x) => { const o = record(x, "ToolEnd"); str(o, "call_id", "ToolEnd"); bool(o, "ok", "ToolEnd");
    optArr(o, "sources", "ToolEnd", source); toolRow(o, "ToolEnd"); return o as never; },
  proposal: (x) => { const o = record(x, "ProposalEvent"); str(o, "proposal_id", "ProposalEvent"); draft(o.draft); return o as never; },
  action: (x) => { const o = record(x, "ActionEvent"); actionBody(o, "ActionEvent"); return o as never; },
  ask: (x) => { const o = record(x, "AskEvent"); askBody(o, "AskEvent"); return o as never; },
  observed: (x) => { const o = record(x, "Observed"); int(o, "observations", "Observed"); optArr(o, "changes", "Observed", change); return o as never; },
  done: (x) => { const o = record(x, "Done"); const c = "Done"; str(o, "message_id", c); int(o, "tool_calls", c); int(o, "llm_calls", c);
    if (o.cost_usd !== undefined && o.cost_usd !== null && typeof o.cost_usd !== "number") throw new ContractError("Done.cost_usd must be a number or null");
    optArr(o, "attachments", c, attachment); optArr(o, "session_attachments", c, attachment);
    if (o.stopped !== undefined) bool(o, "stopped", c); return o as never; },
  error: (x) => { const o = record(x, "ErrorEv"); oneOf(o, "code", ERROR_CODES, "ErrorEv"); bool(o, "retryable", "ErrorEv");
    if (o.detail !== undefined) str(o, "detail", "ErrorEv");
    if (o.interrupted !== undefined) bool(o, "interrupted", "ErrorEv"); return o as never; },
};

/** null for an event name the contract does not define (skipped); throws ContractError for a malformed known event. */
export function parseEvent(name: string, data: unknown): AssistantEvent | null {
  // Own keys only: a plain object literal would otherwise resolve "constructor", "toString", "__proto__" … from Object.prototype.
  if (!Object.prototype.hasOwnProperty.call(EVENTS, name)) return null;
  const p = EVENTS[name];
  if (!p) return null;
  const ev = p(data);
  if (ev.type !== name) throw new ContractError(`event ${name} carries type ${String(ev.type)}`);
  return ev;
}

export function parseSessionCreateOut(x: unknown): SessionCreateOut {
  const o = record(x, "SessionCreateOut"); str(o, "session_id", "SessionCreateOut"); return o as unknown as SessionCreateOut;
}
function toolCall(x: unknown): void {
  const o = record(x, "ToolCallOut"); const c = "ToolCallOut";
  str(o, "call_id", c); str(o, "name", c); oneOf(o, "effect", TOOL_EFFECTS, c);
  if (o.summary !== undefined) str(o, "summary", c); bool(o, "ok", c); optArr(o, "sources", c, source); toolRow(o, c);
}
/** An answer's rating (a MessageOut's `rating`, or the POST .../rating response). */
export function parseRatingOut(x: unknown): RatingOut {
  const o = record(x, "RatingOut"); const c = "RatingOut";
  str(o, "message_id", c);
  if (o.value !== null) oneOf(o, "value", RATING_VALUES, c);
  optArr(o, "chips", c, (k) => { if (typeof k !== "string" || !(RATING_CHIPS as readonly string[]).includes(k)) throw new ContractError(`${c}.chips has an unknown value`); });
  if (o.note !== undefined) str(o, "note", c);
  return o as unknown as RatingOut;
}
function message(x: unknown): MessageOut {
  const o = record(x, "MessageOut"); const c = "MessageOut";
  str(o, "message_id", c); oneOf(o, "role", MESSAGE_ROLES, c); str(o, "text", c);
  optArr(o, "sources", c, source); optArr(o, "proposals", c, strItem(c));
  optArr(o, "actions", c, strItem(c));
  if (o.selection !== undefined && o.selection !== null) quoteRecord(o.selection);
  optArr(o, "tools", c, toolCall);
  optArr(o, "attachments", c, attachment);
  if (o.rating !== undefined && o.rating !== null) parseRatingOut(o.rating);
  optArr(o, "asks", c, (a) => { const k = record(a, "AskCardOut"); askBody(k, "AskCardOut"); oneOf(k, "status", ASK_STATUSES, "AskCardOut"); });
  return o as unknown as MessageOut;
}
function decision(x: unknown): void {
  const o = record(x, "ProposalStateOut"); const c = "ProposalStateOut";
  str(o, "proposal_id", c); oneOf(o, "status", PROPOSAL_STATE_STATUSES, c);
  optArr(o, "applied", c, strItem(c)); if (o.detail !== undefined) str(o, "detail", c);
}
function proposalOut(x: unknown): void {
  const o = record(x, "ProposalOut"); str(o, "proposal_id", "ProposalOut"); draft(o.draft);
}
export function parseSessionOut(x: unknown): SessionOut {
  const o = record(x, "SessionOut"); const c = "SessionOut";
  str(o, "session_id", c); optStr(o, "page_id", c);
  if (o.recap != null) { const r = o.recap as O; for (const k of ["understood", "unclear", "edits"]) optArr(r, k, "SessionRecapOut", (v) => { if (typeof v !== "string") throw new ContractError(`SessionRecapOut.${k} must hold strings`); }); }
  if (o.summary !== undefined) str(o, "summary", c);
  if (!Array.isArray(o.messages)) throw new ContractError("SessionOut.messages must be a list");
  o.messages.forEach(message);
  optArr(o, "decisions", c, decision);
  optArr(o, "proposals", c, proposalOut);
  optArr(o, "actions", c, (a) => { const r = record(a, "ActionCard"); actionBody(r, "ActionCard"); if (r.detail !== undefined) str(r, "detail", "ActionCard"); });

  return o as unknown as SessionOut;
}
export function parseDecisionOut(x: unknown): ProposalDecisionOut {
  const o = record(x, "ProposalDecisionOut"); const c = "ProposalDecisionOut";
  str(o, "proposal_id", c); oneOf(o, "status", DECISION_STATUSES, c);
  optArr(o, "applied", c, strItem(c)); if (o.detail !== undefined) str(o, "detail", c);
  return o as unknown as ProposalDecisionOut;
}

export function parseSessionListOut(x: unknown): SessionListOut {
  const o = record(x, "SessionListOut");
  if (!Array.isArray(o.sessions)) throw new ContractError("SessionListOut.sessions must be a list");
  o.sessions.forEach((r) => {
    const c = "SessionSummaryOut"; const row = record(r, c);
    for (const k of ["session_id", "title", "page_title", "created_at", "last_activity_at"]) str(row, k, c);
    for (const k of ["page_id", "note_concept_id"]) { if (!(k in row)) throw new ContractError(`${c}.${k} is required`); optStr(row, k, c); }
    int(row, "message_count", c);
  });
  return o as unknown as SessionListOut;
}

export function parseKnownItemOut(x: unknown): KnownItemOut {
  const o = record(x, "KnownItemOut"); str(o, "stem", "KnownItemOut");
  if (!Array.isArray(o.options)) throw new ContractError("KnownItemOut.options must be a list");
  o.options.forEach(strItem("KnownItemOut.options"));
  return o as unknown as KnownItemOut;
}
export function parseKnownAnswerOut(x: unknown): KnownAnswerOut {
  const o = record(x, "KnownAnswerOut"); const c = "KnownAnswerOut";
  bool(o, "correct", c); int(o, "correct_index", c); bool(o, "undone", c); str(o, "message", c);
  return o as unknown as KnownAnswerOut;
}
