import { describe, expect, it } from "vitest";
import { AbortedError, ApiError, OfflineError } from "../api/errors";
import { FAKE_ANSWER } from "./fake-api";
import { ContractError } from "./parse";
import { applyEvent, canRetry, cardOpen, classifyError, failTurn, newTurn, resetForRetry, type ProposalCard } from "./turn";

describe("applyEvent", () => {
  it("a later draft of the same section in one turn takes the earlier card's place", () => {
    const t = newTurn("t", "改短", null);
    const draft = (section_id: string, text: string) => ({ kind: "rewrite_section" as const, cue: "", answer: "", page_id: "p1", section_id, heading: "h", cells: [], text, why: "w" });
    applyEvent(t, { type: "proposal", proposal_id: "a", draft: draft("s1", "第一版"), change: null });
    applyEvent(t, { type: "proposal", proposal_id: "other", draft: draft("s2", "别的节"), change: null });
    applyEvent(t, { type: "proposal", proposal_id: "b", draft: draft("s1", "第二版"), change: null });
    expect(t.proposals.map((p) => p.proposalId)).toEqual(["other", "b"]);
  });

  it("folds the fake answer into a finished turn", () => {
    const t = newTurn("t1", "为什么要最大方差", null);
    for (const e of FAKE_ANSWER) applyEvent(t, e);
    expect(t.status).toBe("done");
    expect(t.answer).toBe("方差大的方向，保留的信息最多。\n\n可以把它想成**找一条最能把点分开的直线**。");
    expect(t.tools.map((l) => [l.name, l.status, l.sources.length])).toEqual([["web_search", "ok", 1], ["read_page", "ok", 1]]);
    expect(t.proposals).toMatchObject([{ proposalId: "pr_fake_1", status: "open" }]);
    expect(t.observed?.changes).toHaveLength(1);
    expect(t.rev).toBe(FAKE_ANSWER.length);
  });
  it("status: a long read's line goes on its running row, which is the line shown then", () => {
    const t = newTurn("t", "q", null);
    applyEvent(t, { type: "tool_start", call_id: "c", name: "read_source", effect: "read", summary: "", label: "正在精确读《X》第 7 页" });
    applyEvent(t, { type: "status", text: "正在精确读第 7 页，第一次要等约 2 分钟" });
    expect(t.tools[0]?.label).toBe("正在精确读第 7 页，第一次要等约 2 分钟");
    expect(t.statusLine).toBe("");
  });
  it("status: the latest line replaces the last one; cleared by answer text, by the end of its call, by a card and at done", () => {
    const t = newTurn("t", "q", null);
    expect(t).toMatchObject({ thinking: true, statusLine: "" });
    applyEvent(t, { type: "status", text: "正在搜索：碱基配对" });
    expect(t.statusLine).toBe("正在搜索：碱基配对");
    applyEvent(t, { type: "status", text: "正在读《基因配对》第 2 节" });
    expect(t.statusLine).toBe("正在读《基因配对》第 2 节");
    applyEvent(t, { type: "tool_start", call_id: "c", name: "read_page", effect: "read", summary: "基因配对", label: "" });
    applyEvent(t, { type: "tool_end", call_id: "c", ok: true, sources: [], label: "", group: "", group_label: "", items: [], refs: [] });
    expect(t).toMatchObject({ thinking: true, statusLine: "" });      // the call ended: back to 正在思考…
    applyEvent(t, { type: "status", text: "正在写草稿" });
    applyEvent(t, { type: "proposal", proposal_id: "p", draft: { kind: "add_section", cue: "", answer: "", page_id: "p1", section_id: null, heading: "h", cells: [], text: "x", why: "y" }, change: null });
    expect(t.statusLine).toBe("");
    applyEvent(t, { type: "status", text: "正在网上搜索" });
    applyEvent(t, { type: "text_delta", text: "答", at: 0 });
    expect(t).toMatchObject({ thinking: false, statusLine: "" });
    applyEvent(t, { type: "status", text: "正在网上搜索：z" });      // a step after some text: waiting again, below it
    expect(t).toMatchObject({ thinking: true, statusLine: "正在网上搜索：z" });
    applyEvent(t, { type: "done", message_id: "m", tool_calls: 1, llm_calls: 2, cost_usd: null, attachments: [], session_attachments: [], stopped: false, confirmed: false });
    expect(t).toMatchObject({ thinking: false, statusLine: "" });
  });
  it("tool_end with ok=false marks the line failed; unknown call_id adds a finished line", () => {
    const t = newTurn("t", "q", null);
    applyEvent(t, { type: "tool_start", call_id: "a", name: "fetch_url", effect: "network", summary: "https://x", label: "" });
    applyEvent(t, { type: "tool_end", call_id: "a", ok: false, sources: [], label: "", group: "", group_label: "", items: [], refs: [] });
    applyEvent(t, { type: "tool_end", call_id: "zz", ok: true, sources: [], label: "", group: "", group_label: "", items: [], refs: [] });
    expect(t.tools.map((l) => l.status)).toEqual(["failed", "ok"]);
  });
  it("an error event ends the turn and later events are ignored", () => {
    const t = newTurn("t", "q", null);
    applyEvent(t, { type: "error", code: "budget_exceeded", retryable: false, detail: "", interrupted: false });
    applyEvent(t, { type: "text_delta", text: "late", at: null });
    expect(t).toMatchObject({ status: "error", answer: "", error: { kind: "event", code: "budget_exceeded", retryable: false } });
  });
  it("resetForRetry clears the partial answer and keeps question and quote", () => {
    const t = newTurn("t", "q", { text: "段", heading: null });
    applyEvent(t, { type: "text_delta", text: "半", at: null }); failTurn(t, { kind: "broken" });
    resetForRetry(t);
    expect(t).toMatchObject({ status: "streaming", answer: "", error: null, question: "q", quote: { text: "段" } });
  });
});

describe("classifyError / canRetry", () => {
  it("maps transport and HTTP failures per phase", () => {
    expect(classifyError(new OfflineError(), "message")).toEqual({ kind: "offline" });
    expect(classifyError(new AbortedError(), "message")).toEqual({ kind: "aborted" });
    for (const p of ["create", "message", "decide", "restore"] as const) {
      expect(classifyError(new ApiError(503, "assistant unavailable"), p)).toEqual({ kind: "not_enabled" });
    }
    expect(classifyError(new ApiError(404, "x"), "create")).toEqual({ kind: "unknown_page" });
    expect(classifyError(new ApiError(404, "x"), "message")).toEqual({ kind: "session_gone" });
    expect(classifyError(new ApiError(403, "x"), "message")).toEqual({ kind: "session_gone" });
    expect(classifyError(new ApiError(409, "x"), "message")).toEqual({ kind: "busy" });
    expect(classifyError(new ApiError(404, "x"), "restore")).toEqual({ kind: "session_gone" });
    expect(classifyError(new ApiError(403, "x"), "restore")).toEqual({ kind: "session_gone" });
    expect(classifyError(new ApiError(409, "x"), "restore")).toEqual({ kind: "http", status: 409 });
    expect(classifyError(new ApiError(409, "x"), "decide")).toEqual({ kind: "http", status: 409 });
    expect(classifyError(new ApiError(404, "x"), "decide")).toEqual({ kind: "http", status: 404 });
    expect(classifyError(new ApiError(422, "x"), "message")).toEqual({ kind: "http", status: 422 });
    expect(classifyError(new ContractError("x"), "message")).toEqual({ kind: "broken" });
    expect(classifyError(new Error("malformed"), "message")).toEqual({ kind: "broken" });
  });
  it("retry is offered exactly for retryable failures", () => {
    expect(canRetry({ kind: "event", code: "llm_unavailable", retryable: true })).toBe(true);
    expect(canRetry({ kind: "event", code: "budget_exceeded", retryable: false })).toBe(false);
    for (const k of ["offline", "broken", "session_gone", "busy"] as const) expect(canRetry({ kind: k })).toBe(true);
    for (const k of ["not_enabled", "aborted", "unknown_page"] as const) expect(canRetry({ kind: k })).toBe(false);
    expect(canRetry({ kind: "http", status: 503 })).toBe(true);
    expect(canRetry({ kind: "http", status: 422 })).toBe(false);
  });
});

describe("rev and cards", () => {
  it("each mutation bumps rev exactly once; ignored events do not", () => {
    const t = newTurn("t", "q", null);
    expect(t.rev).toBe(0);
    applyEvent(t, { type: "text_delta", text: "a", at: null }); expect(t.rev).toBe(1);
    failTurn(t, { kind: "offline" }); expect(t.rev).toBe(2);
    applyEvent(t, { type: "text_delta", text: "late", at: null }); expect(t.rev).toBe(2);
    resetForRetry(t); expect(t.rev).toBe(3);
    applyEvent(t, { type: "done", message_id: "m", tool_calls: 0, llm_calls: 1, cost_usd: null, attachments: [], session_attachments: [], stopped: false, confirmed: false }); expect(t.rev).toBe(4);
    applyEvent(t, { type: "observed", observations: 1, changes: [] }); expect(t).toMatchObject({ rev: 4, observed: null });
  });
  it("buttons are enabled for open, failed and error cards only", () => {
    const card = (status: ProposalCard["status"]): ProposalCard =>
      ({ proposalId: "p", draft: { kind: "add_section", cue: "", answer: "", page_id: "pg", heading: "h", text: "x", why: "w", section_id: null, cells: [] }, status, applied: [], error: null, change: null, actionId: "", canUndo: false, undoing: false, note: "", detail: "" });
    expect((["open", "error"] as const).map((s) => cardOpen(card(s)))).toEqual([true, true]);
    expect((["sending", "accepted", "rejected", "already_decided", "failed"] as const).map((s) => cardOpen(card(s)))).toEqual([false, false, false, false, false]);
  });
});
