import { describe, expect, it } from "vitest";
import { ContractError, parseDecisionOut, parseEvent, parseSessionCreateOut, parseSessionOut } from "./parse";

const draft = { kind: "add_section", page_id: "p1", section_id: null, heading: "直观理解", cells: ["pca.mechanism"], text: "……", why: "你问了为什么" };
const VALID: Record<string, unknown> = {
  text_delta: { type: "text_delta", text: "你好" },
  status: { type: "status", text: "正在读《基因配对》第 2 节" },
  tool_start: { type: "tool_start", call_id: "c1", name: "web_search", effect: "network", summary: "PCA 方差" },
  tool_end: { type: "tool_end", call_id: "c1", ok: true, sources: [{ title: "Wiki", url: "https://x.org", page_id: null, segment_id: null }] },
  proposal: { type: "proposal", proposal_id: "pr1", draft },
  observed: { type: "observed", observations: 2, changes: [{ cell: "pca.mechanism", from_band: "likely", to_band: "shaky" }] },
  done: { type: "done", stopped: false, message_id: "m1", tool_calls: 1, llm_calls: 2, cost_usd: null },
  error: { type: "error", code: "llm_unavailable", retryable: true, detail: "" },
};

describe("parseEvent", () => {
  it("accepts every valid event and returns it unchanged", () => {
    for (const [name, data] of Object.entries(VALID)) expect(parseEvent(name, data), name).toEqual(data);
  });
  it("accepts omitted defaulted fields (summary, sources, changes, cost_usd, detail, section_id, cells)", () => {
    expect(parseEvent("tool_start", { type: "tool_start", call_id: "c", name: "read_k", effect: "read" })).toBeTruthy();
    expect(parseEvent("tool_end", { type: "tool_end", call_id: "c", ok: false })).toBeTruthy();
    expect(parseEvent("observed", { type: "observed", observations: 0 })).toBeTruthy();
    expect(parseEvent("error", { type: "error", code: "internal", retryable: false })).toBeTruthy();
  });
  it("skips unknown event names", () => {
    expect(parseEvent("message", { x: 1 })).toBeNull();
    expect(parseEvent("ping", null)).toBeNull();
    // Object.prototype names are not events: only the table's own keys count.
    for (const n of ["constructor", "toString", "__proto__", "hasOwnProperty"]) expect(parseEvent(n, { type: n }), n).toBeNull();
  });
  it("fails closed on a missing, mistyped or extra field, a bad literal, or a name/type mismatch", () => {
    const bad: [string, unknown][] = [
      ["text_delta", { type: "text_delta" }],
      ["text_delta", { type: "text_delta", text: 3 }],
      ["text_delta", { type: "text_delta", text: "a", extra: 1 }],
      ["status", { type: "status" }],
      ["status", { type: "status", text: 3 }],
      ["status", { type: "status", text: "  " }],
      ["status", { type: "status", text: "正在读", tool: "read_page" }],
      ["status", { type: "text_delta", text: "正在读" }],
      ["tool_start", { type: "tool_start", call_id: "c", name: "x", effect: "write" }],
      ["error", { type: "error", code: "nope", retryable: true }],
      ["done", { type: "text_delta", text: "a" }],
      ["proposal", { type: "proposal", proposal_id: "p", draft: { ...draft, kind: "delete_page" } }],
      ["tool_end", { type: "tool_end", call_id: "c", ok: true, sources: [{ url: "https://x" }] }],
      ["observed", { type: "observed", observations: "2" }],
      ["text_delta", null],
    ];
    for (const [n, d] of bad) expect(() => parseEvent(n, d), JSON.stringify(d)).toThrow(ContractError);
  });
});

describe("response parsers", () => {
  it("parse valid bodies", () => {
    expect(parseSessionCreateOut({ session_id: "s1" })).toEqual({ session_id: "s1" });
    const so = { session_id: "s1", page_id: null, recap: null, messages: [
      { message_id: "m1", role: "user", text: "q", sources: [], proposals: [] },
      { message_id: "m2", role: "assistant", text: "a", sources: [{ title: "T", url: null, page_id: "p", segment_id: null }], proposals: ["pr1"] }] };
    expect(parseSessionOut(so)).toEqual(so);
    expect(parseDecisionOut({ proposal_id: "pr1", status: "accepted", applied: ["s3"], detail: "" })).toMatchObject({ status: "accepted" });
  });
  it("fail closed", () => {
    expect(() => parseSessionCreateOut({})).toThrow(ContractError);
    expect(() => parseSessionOut({ session_id: "s", page_id: null, messages: [{ message_id: "m", role: "system", text: "" }] })).toThrow(ContractError);
    expect(() => parseDecisionOut({ proposal_id: "p", status: "maybe" })).toThrow(ContractError);
  });
  it("SessionOut.decisions: an optional list of ProposalStateOut (accepted, unused by the plugin)", () => {
    const base = { session_id: "s", page_id: null, messages: [] };
    const decisions = [{ proposal_id: "pr1", status: "accepted", applied: ["s3"], detail: "" },
      { proposal_id: "pr2", status: "failed", applied: [], detail: "x" }, { proposal_id: "pr3", status: "rejected" }];
    expect(parseSessionOut({ ...base, decisions })).toEqual({ ...base, decisions });
    expect(parseSessionOut({ ...base, decisions: [] })).toEqual({ ...base, decisions: [] });
    for (const bad of [{}, [{ proposal_id: "p", status: "already_decided" }], [{ proposal_id: 1, status: "accepted" }],
      [{ proposal_id: "p", status: "accepted", extra: 1 }], [{ proposal_id: "p", status: "accepted", applied: [1] }]]) {
      expect(() => parseSessionOut({ ...base, decisions: bad }), JSON.stringify(bad)).toThrow(ContractError);
    }
  });
  it("MessageOut.tools: an optional list of ToolCallOut (the turn's stored tool calls)", () => {
    const so = (m: Record<string, unknown>) => ({ session_id: "s", page_id: null, messages: [{ message_id: "m", role: "assistant", text: "a", ...m }] });
    const tools = [{ call_id: "d1", name: "search_wiki", effect: "read", summary: "特征值", ok: true, sources: [] },
      { call_id: "d2", name: "read_page", effect: "read", ok: false, sources: [{ title: "pca（2/4）", page_id: "p2" }] }];
    expect(parseSessionOut(so({ tools })).messages[0]).toMatchObject({ tools });
    for (const bad of [{}, [{ call_id: "d", name: "x", effect: "write", ok: true }], [{ call_id: "d", name: "x", effect: "read" }],
      [{ call_id: "d", name: "x", effect: "read", ok: true, extra: 1 }], [{ call_id: "d", name: "x", effect: "read", ok: true, sources: [1] }]]) {
      expect(() => parseSessionOut(so({ tools: bad })), JSON.stringify(bad)).toThrow(ContractError);
    }
  });
  it("MessageOut.selection is an optional nullable Selection", () => {
    const so = (m: Record<string, unknown>) => ({ session_id: "s", page_id: null, messages: [{ message_id: "m", role: "user", text: "q", ...m }] });
    for (const quote of [null, { kind: "text", text: "一段" }, { kind: "wrong_answer", text: "一段", heading: null }, { kind: "text", text: "一段", heading: "推导" }]) {
      expect(parseSessionOut(so({ selection: quote })).messages[0]).toMatchObject({ selection: quote });
    }
    for (const bad of ["一段", 3, [], {}, { text: 3 }, { text: "a", heading: 4 }, { text: "a", extra: 1 }]) {
      expect(() => parseSessionOut(so({ selection: bad })), JSON.stringify(bad)).toThrow(ContractError);
    }
  });
});

describe("action events and their stored shapes", () => {
  const item = { key: "q1", title: "特征向量", page_id: null, covers: "先看例子" };
  const action = { type: "action", action_id: "a1", kind: "page_split", title: "拆成 3 页", items: [item], status: "proposed", confirm: true, can_undo: false };
  it("accepts an action event, with the defaulted fields left out", () => {
    expect(parseEvent("action", action)).toEqual(action);
    expect(parseEvent("action", { type: "action", action_id: "a1", kind: "rule", title: "t" })).toBeTruthy();
    // a goal_new draft row carries its block's level word
    const blocks = { ...action, kind: "goal_new", items: [{ ...item, level: "基础" }, { ...item, key: "q2", level: null }] };
    expect(parseEvent("action", blocks)).toEqual(blocks);
    expect(() => parseEvent("action", { ...action, items: [{ ...item, level: 1 }] })).toThrow(ContractError);
  });
  it("refuses unknown fields and wrong types (fail closed)", () => {
    expect(() => parseEvent("action", { ...action, extra: 1 })).toThrow(ContractError);
    expect(() => parseEvent("action", { ...action, items: [{ ...item, key: 3 }] })).toThrow(ContractError);
    expect(() => parseEvent("action", { ...action, confirm: "yes" })).toThrow(ContractError);
  });
  it("a session carries its actions; a message lists their ids", () => {
    const out = { session_id: "s", page_id: null, messages: [{ message_id: "m", role: "assistant", text: "好", actions: ["a1"] }],
      actions: [{ action_id: "a1", kind: "rule", title: "t", items: [], status: "executed", confirm: true, can_undo: true, detail: "" }] };
    expect(parseSessionOut(out)).toEqual(out);
    expect(() => parseSessionOut({ ...out, actions: [{ action_id: "a1" }] })).toThrow(ContractError);
    expect(() => parseSessionOut({ ...out, messages: [{ ...out.messages[0]!, actions: [3] }] })).toThrow(ContractError);
  });
});
