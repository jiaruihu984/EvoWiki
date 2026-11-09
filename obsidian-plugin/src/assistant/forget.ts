import { tr } from "../i18n/index";
/**
 * 删除对话 (full-demo/src/asst.js forgetDialog; the user's mockup of 2026-10-28): the contract checks of the three forget
 * calls and the pure helpers of the dialog and the 「删除中」 rows. The fields mean what memory/forget.py says.
 */
import { ContractError } from "./parse";
import type { ForgetMaterialOut, ForgetOut, ForgetPendingOut, ForgetPreviewOut } from "./types";

type O = Record<string, unknown>;
function obj(x: unknown, c: string): O {
  if (x === null || typeof x !== "object" || Array.isArray(x)) throw new ContractError(`${c} is not an object`);
  return x as O;
}
function list(o: O, k: string, c: string): unknown[] {
  if (!Array.isArray(o[k])) throw new ContractError(`${c}.${k} must be a list`);
  return o[k] as unknown[];
}
function strs(o: O, keys: readonly string[], c: string): void {
  for (const k of keys) if (typeof o[k] !== "string") throw new ContractError(`${c}.${k} must be a string`);
}

/** client.ts forgetPreview. */
export function parseForgetPreviewOut(x: unknown): ForgetPreviewOut {
  const o = obj(x, "ForgetPreviewOut");
  list(o, "rules", "ForgetPreviewOut").forEach((r) => strs(obj(r, "ForgetRuleOut"), ["item_id", "text", "authority"], "ForgetRuleOut"));
  list(o, "facts", "ForgetPreviewOut").forEach((r) => strs(obj(r, "ForgetFactOut"), ["key", "section", "text"], "ForgetFactOut"));
  list(o, "tainted_sessions", "ForgetPreviewOut").forEach((r) => {
    const s = obj(r, "ForgetSessionOut"); strs(s, ["session_id", "title"], "ForgetSessionOut");
    if (!Number.isInteger(s.turns)) throw new ContractError("ForgetSessionOut.turns must be an integer");
  });
  list(o, "materials", "ForgetPreviewOut").forEach((r) => {
    const m = obj(r, "ForgetMaterialOut"); strs(m, ["source_id", "title"], "ForgetMaterialOut");
    if (typeof m.in_use_elsewhere !== "boolean") throw new ContractError("ForgetMaterialOut.in_use_elsewhere must be a boolean");
    if (!list(m, "used_by", "ForgetMaterialOut").every((u) => typeof u === "string")) throw new ContractError("ForgetMaterialOut.used_by must be strings");
  });
  obj(o.memory_counts, "ForgetPreviewOut.memory_counts");
  strs(o, ["note"], "ForgetPreviewOut");
  return o as unknown as ForgetPreviewOut;
}

/** client.ts deleteSession: 200 committed, 202 failed (recorded, finished by a retry). */
export function parseForgetOut(x: unknown): ForgetOut {
  const o = obj(x, "ForgetOut");
  if (o.state !== "committed" && o.state !== "failed") throw new ContractError("ForgetOut.state has an unknown value");
  strs(o, ["detail"], "ForgetOut");
  return o as unknown as ForgetOut;
}

const PENDING_STATES = ["requested", "deleting", "failed"];
/** client.ts forgetRequests. */
export function parseForgetPending(x: unknown): ForgetPendingOut[] {
  const o = obj(x, "ForgetPendingListOut");
  const rows = list(o, "sessions", "ForgetPendingListOut");
  rows.forEach((r) => {
    const p = obj(r, "ForgetPendingOut"); strs(p, ["session_id", "requested_at"], "ForgetPendingOut");
    if (!PENDING_STATES.includes(p.state as string)) throw new ContractError("ForgetPendingOut.state has an unknown value");
  });
  return rows as ForgetPendingOut[];
}

/** What the learner ticked in the dialog: the body of client.ts deleteSession. */
export interface ForgetPicks { revokeRuleIds: string[]; revokeFactKeys: string[]; deleteSourceIds: string[] }

/** The dialog while it is open (one at a time). `preview` null: still being read (删除对话 waits). `busy`: 删除对话 was
 *  pressed and the answer is not back. */
export interface ForgetDialog {
  sessionId: string; title: string; preview: ForgetPreviewOut | null; picks: ForgetPicks; busy: boolean;
}

const usedKinds = (): readonly (readonly [string, string, string])[] => [
  ["session", tr("assistant.forget.USED_KIND.1"), tr("assistant.forget.USED_KIND.2")], ["page", tr("assistant.forget.USED_KIND.3"), tr("assistant.forget.USED_KIND.4")], ["goal", tr("assistant.forget.USED_KIND.5"), tr("assistant.forget.USED_KIND.6")], ["node", tr("assistant.forget.USED_KIND.7"), tr("assistant.forget.USED_KIND.8")],
];

/** `used_by` (session:<id> / page:<id> / goal:<id> / node:<id>) as 「1 段对话、1 个页面」. */
export function usedLine(used: readonly string[]): string {
  return usedKinds().map(([k, n]) => [used.filter((u) => u.startsWith(k + ":")).length, n] as const)
    .filter(([x]) => x > 0).map(([x, n]) => `${x} ${n}`).join("、");
}

/** The ? tip of a material used elsewhere: 「引用此内容的对话」 (「…的地方」 when not only conversations), then each
 *  conversation's title, the rest counted. */
export function usedTip(m: ForgetMaterialOut, sessionTitle: (id: string) => string | null): string[] {
  const named: string[] = [], unnamed: string[] = [];
  for (const u of m.used_by) {
    const t = u.startsWith("session:") ? sessionTitle(u.slice("session:".length)) : null;
    if (t) named.push(t); else unnamed.push(u);
  }
  const rest = usedLine(unnamed);
  const head = m.used_by.every((u) => u.startsWith("session:")) ? tr("assistant.forget.head.9") : tr("assistant.forget.head.10");
  return [head, ...named, ...(rest ? [rest] : [])];
}
