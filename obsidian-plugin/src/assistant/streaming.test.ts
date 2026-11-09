/** Streamed answers (spec 2026-10-15): positioned text pieces, the interrupted mark, resume ids, one resume after a break, and no blink at done. */
import { cardActions } from "../../test/card-rig";
import { describe, expect, it, vi } from "vitest";
import { OfflineError } from "../api/errors";
import type { HttpRequest, JsonTransport, StreamTransport } from "../api/client";
import { SseDecoder } from "../api/sse";
import { AssistantClient } from "./client";
import { AssistantController, RESUME_WAITS_MS } from "./controller";
import { FakeAssistantApi } from "./fake-api";
import { ContractError, parseEvent } from "./parse";
import { type AssistantActions, AssistantRenderer } from "./render";
import type { AssistantSessionStore, Target } from "./session-store";
import type { AssistantEvent, Source } from "./types";
import { applyEvent, newTurn } from "./turn";
import type { AssistantState, Conversation, HistoryState } from "./controller";

const HIST0: HistoryState = { open: false, q: "", status: "loading", items: [], error: "", notice: "", deleting: [] };

const NOTE: Target = { kind: "note", path: "ML/PCA.md", conceptId: "pca", title: "PCA" };
const done = { type: "done" as const, message_id: "m", tool_calls: 0, llm_calls: 1, cost_usd: null, attachments: [], session_attachments: [], stopped: false, confirmed: false };

describe("text_delta with a position", () => {
  it("keeps answer[:at] and appends; a resend of the same text changes nothing, not even rev", () => {
    const t = newTurn("t", "q", null);
    applyEvent(t, { type: "text_delta", text: "方差", at: 0 });
    applyEvent(t, { type: "text_delta", text: "大的", at: 2 });
    expect(t.answer).toBe("方差大的");
    const rev = t.rev;
    applyEvent(t, { type: "text_delta", text: "方差大的", at: 0 });         // the committed text, same as shown
    expect(t.rev).toBe(rev);
    applyEvent(t, { type: "text_delta", text: "方差小", at: 0 });           // a corrected generation replaces from 0
    expect(t.answer).toBe("方差小");
    applyEvent(t, { type: "text_delta", text: "！", at: null });                      // no position: appends (old servers)
    expect(t.answer).toBe("方差小！");
    applyEvent(t, { type: "text_delta", text: "", at: 2 });                 // drops uncommitted live text
    expect(t.answer).toBe("方差");
  });
  it("an interrupted error keeps the text and marks it; without text there is nothing to mark", () => {
    const t = newTurn("t", "q", null);
    applyEvent(t, { type: "text_delta", text: "说到一半", at: 0 });
    applyEvent(t, { type: "error", code: "llm_unavailable", retryable: true, interrupted: true, detail: "" });
    expect(t).toMatchObject({ status: "error", answer: "说到一半", interrupted: true });
    const e = newTurn("e", "q", null);
    applyEvent(e, { type: "error", code: "llm_busy", retryable: true, interrupted: true, detail: "" });
    expect(e.interrupted).toBe(false);
  });
  it("parse accepts at (int >= 0 or null) and interrupted (bool), refuses anything else", () => {
    expect(parseEvent("text_delta", { type: "text_delta", text: "a", at: 3 })).toEqual({ type: "text_delta", text: "a", at: 3 });
    expect(parseEvent("text_delta", { type: "text_delta", text: "a", at: null })).not.toBeNull();
    expect(parseEvent("error", { type: "error", code: "internal", retryable: true, interrupted: true })).not.toBeNull();
    for (const bad of [{ type: "text_delta", text: "a", at: -1 }, { type: "text_delta", text: "a", at: 1.5 }, { type: "text_delta", text: "a", at: "1" }])
      expect(() => parseEvent("text_delta", bad)).toThrow(ContractError);
    expect(() => parseEvent("error", { type: "error", code: "internal", retryable: true, interrupted: 1 })).toThrow(ContractError);
  });
});

describe("resume ids", () => {
  const frame = (id: string | null, e: string, d: unknown) => `${id === null ? "" : `id: ${id}\n`}event: ${e}\ndata: ${JSON.stringify(d)}\n\n`;
  it("the decoder reads the SSE id", () => {
    const r = new SseDecoder().push(frame("m1:0", "text_delta", { type: "text_delta", text: "a", at: 0 }) + frame(null, "done", done));
    expect(r.map((f) => f.id)).toEqual(["m1:0", undefined]);
  });
  it("sendMessage reports ids; resumeMessage GETs the events route with the last id", async () => {
    const reqs: HttpRequest[] = [];
    const body = [frame("m1:0", "text_delta", { type: "text_delta", text: "a", at: 0 }), frame("m1:1", "done", done)];
    const sse: StreamTransport = { stream: (req, onChunk) => { reqs.push(req); body.forEach(onChunk); return Promise.resolve({ status: 200, text: "" }); } };
    const http: JsonTransport = { request: vi.fn() };
    const c = new AssistantClient(() => ({ baseUrl: "http://h", userId: "jerry" }), http, sse);
    const ids: string[] = [];
    await c.sendMessage("s/1", { text: "q" }, () => undefined, undefined, (id) => ids.push(id));
    expect(ids).toEqual(["m1:0", "m1:1"]);
    await c.resumeMessage("s/1", "m1:0", () => undefined);
    expect(reqs[1]).toEqual({ method: "GET", url: "http://h/assistant/sessions/s%2F1/events?last_event_id=m1%3A0",
      headers: { "X-EvoWiki-User": "jerry" }, signal: undefined });
  });
});

function rig() {
  const api = new FakeAssistantApi();
  const waits: number[] = [];
  const mem: Record<string, string> = {};
  const store: AssistantSessionStore = { get: (k) => mem[k] ?? null, set: (k, v) => { delete mem[k]; if (v) mem[k] = v; return Promise.resolve(); } };
  let n = 0;
  const ctl = new AssistantController({ api, store, identity: () => ({ server: "http://h", user: "jerry" }), newId: () => `t${++n}`,
    sleep: (ms) => { waits.push(ms); return Promise.resolve(); } });
  return { api, ctl, waits };
}

describe("resume after a broken stream", () => {
  it("a dropped connection resumes from the last id and the answer completes without doubling", async () => {
    const { api, ctl } = rig();
    api.messageReplies.push({ eventsThenThrow: [{ type: "text_delta", text: "方差大", at: 0 }], error: new OfflineError(), lastId: "m1:0" });
    api.resumeReplies.push({ events: [{ type: "text_delta", text: "方差大的方向", at: 0 }, done] });
    ctl.show(NOTE); ctl.setDraft("q"); await ctl.send();
    expect(api.resumed).toEqual([{ sessionId: "ses_fake_1", lastEventId: "m1:0" }]);
    expect(ctl.state.conv!.turns[0]).toMatchObject({ status: "done", answer: "方差大的方向", error: null });
  });
  it("a backend that is not back yet is asked again after each wait (about a minute in all), then the turn fails", async () => {
    const { api, ctl, waits } = rig();
    api.messageReplies.push({ events: [{ type: "text_delta", text: "半", at: 0 }], lastId: "m1:0" });
    for (let i = 0; i < 9; i++) api.resumeReplies.push({ throws: new OfflineError() });
    ctl.show(NOTE); ctl.setDraft("q"); await ctl.send();
    expect(waits).toEqual(RESUME_WAITS_MS);
    expect(waits.reduce((a, b) => a + b, 0)).toBeGreaterThanOrEqual(55_000);
    expect(api.resumed).toHaveLength(RESUME_WAITS_MS.length + 1);
    expect(ctl.state.conv!.turns[0]).toMatchObject({ status: "error", answer: "半", error: { kind: "broken" } });
  });
  it("a backend that comes back after a few tries completes the answer without doubling", async () => {
    const { api, ctl, waits } = rig();
    api.messageReplies.push({ events: [{ type: "text_delta", text: "半", at: 0 }], lastId: "m1:0" });
    api.resumeReplies.push({ throws: new OfflineError() }, { throws: new OfflineError() },
      { events: [{ type: "text_delta", text: "半个答案", at: 0 }, done] });
    ctl.show(NOTE); ctl.setDraft("q"); await ctl.send();
    expect(waits).toEqual([1000, 2000]);
    expect(api.resumed.map((r) => r.lastEventId)).toEqual(["m1:0", "m1:0", "m1:0"]);
    expect(ctl.state.conv!.turns[0]).toMatchObject({ status: "done", answer: "半个答案", error: null });
  });
  it("without a resume id (an inline server) nothing is resumed", async () => {
    const { api, ctl } = rig();
    api.messageReplies.push({ eventsThenThrow: [{ type: "text_delta", text: "半", at: null }], error: new OfflineError() });
    ctl.show(NOTE); ctl.setDraft("q"); await ctl.send();
    expect(api.resumed).toEqual([]);
    expect(ctl.state.conv!.turns[0]!.error).toEqual({ kind: "broken" });
  });
});

describe("rendering a streamed answer", () => {
  const actions = (): AssistantActions => ({ send: vi.fn(), sendEdit: vi.fn(), setDraft: vi.fn(), removeQuote: vi.fn(), retry: vi.fn(), decide: vi.fn(), undoProposal: vi.fn(), addFiles: vi.fn(), dropLibrary: vi.fn(), removeAttachment: vi.fn(),
    newSession: vi.fn(), reload: vi.fn(), toggleHistory: vi.fn(), searchHistory: vi.fn(), reloadHistory: vi.fn(), resume: vi.fn(),
    openSource: vi.fn(), ...cardActions() });
  const conv = (over: Partial<Conversation>): Conversation => ({ key: "k", target: NOTE, sessionId: "s", turns: [], draft: "", quote: null,
    busy: false, attachments: [], restore: "none", recap: null, ...over });
  const st = (c: Conversation): AssistantState => ({ configured: true, active: c.target.kind === "free" ? { kind: "none" } : c.target, conv: c, focusTick: 0, history: HIST0, writes: new Map(), canAttach: true });

  it("at done the plain paragraphs stay until the Markdown replaces them (never empty in between)", () => {
    let later: (() => void) | null = null;
    const hooks = { conceptName: (id: string) => id,
      renderMarkdown: (el: HTMLElement, md: string) => { later = () => { el.replaceChildren(`MD:${md}`); }; } };
    const r = new AssistantRenderer(actions(), hooks);
    const t = newTurn("t", "q", null);
    applyEvent(t, { type: "text_delta", text: "方差**大**", at: 0 });
    r.update(st(conv({ turns: [t], busy: true })));
    expect(r.root.querySelector(".ew-answer")!.textContent).toBe("方差**大**");
    applyEvent(t, done);
    r.update(st(conv({ turns: [t] })));
    expect(r.root.querySelector(".ew-answer")!.textContent).toBe("方差**大**");   // still there while Markdown renders
    later!();
    expect(r.root.querySelector(".ew-answer")!.textContent).toBe("MD:方差**大**");
  });
  it("an interrupted answer stays with its 中断 line above the error", () => {
    const hooks = { conceptName: (id: string) => id, renderMarkdown: vi.fn() };
    const r = new AssistantRenderer(actions(), hooks);
    const t = newTurn("t", "q", null);
    applyEvent(t, { type: "text_delta", text: "说到一半", at: 0 });
    applyEvent(t, { type: "error", code: "llm_unavailable", retryable: true, interrupted: true, detail: "" });
    r.update(st(conv({ turns: [t] })));
    const lines = [...r.root.querySelectorAll(".ew-amsg > *")].map((e) => e.textContent ?? "");
    expect(lines[0]).toBe("说到一半");
    expect(lines[1]).toContain("回答中断");
    expect(lines[1]).toContain("重试");
    expect(r.root.querySelectorAll(".ew-amsg .ew-btn.link, .ew-amsg button").length).toBe(1);   // one 重试 (the error line carries none)
  });
});

describe("the status line (spinner + what the assistant is doing right now)", () => {
  const actions = (): AssistantActions => ({ send: vi.fn(), sendEdit: vi.fn(), setDraft: vi.fn(), removeQuote: vi.fn(), retry: vi.fn(), decide: vi.fn(), undoProposal: vi.fn(), addFiles: vi.fn(), dropLibrary: vi.fn(), removeAttachment: vi.fn(),
    newSession: vi.fn(), reload: vi.fn(), toggleHistory: vi.fn(), searchHistory: vi.fn(), reloadHistory: vi.fn(), resume: vi.fn(),
    openSource: vi.fn(), ...cardActions() });
  const hooks = { conceptName: (id: string) => id, renderMarkdown: vi.fn() };
  const conv = (over: Partial<Conversation>): Conversation => ({ key: "k", target: NOTE, sessionId: "s", turns: [], draft: "", quote: null,
    busy: true, attachments: [], restore: "none", recap: null, ...over });
  const st = (c: Conversation | null): AssistantState => ({ configured: true, active: c && c.target.kind !== "free" ? c.target : { kind: "none" }, conv: c, focusTick: 0, history: HIST0, writes: new Map(), canAttach: true });
  const line = (r: AssistantRenderer) => r.root.querySelector<HTMLElement>(".ew-amsg > .ew-tools");

  const rows = (r: AssistantRenderer) => [...r.root.querySelectorAll<HTMLElement>(".ew-amsg > .ew-trows > .ew-trow")];
  const start = (id: string, name: string, label: string): AssistantEvent => ({ type: "tool_start", call_id: id, name, effect: "read", summary: "", label });
  const end = (id: string, label: string, group: string, groupLabel: string, items: string[], refs: Source[] = []): AssistantEvent =>
    ({ type: "tool_end", call_id: id, ok: true, sources: [], label, group, group_label: groupLabel, items, refs });

  it("a long step's line shows how long the steps have run, across its stages; a quick one shows nothing", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(1_000_000);
      const t = newTurn("t", "q", null);
      const r = new AssistantRenderer(actions(), hooks);
      document.body.append(r.root);
      const paint = () => r.update(st(conv({ turns: [t], busy: true })));
      applyEvent(t, { type: "status", text: "正在看《attention》" }); paint();
      expect(line(r)!.textContent).toBe("正在看《attention》");
      vi.advanceTimersByTime(42_000);
      expect(line(r)!.textContent).toBe("正在看《attention》42 秒");
      applyEvent(t, { type: "status", text: "正在排段落" }); paint();
      vi.advanceTimersByTime(30_000);
      expect(line(r)!.textContent).toBe("正在排段落1 分 12 秒");
      r.root.remove();
    } finally {
      vi.useRealTimers();
    }
  });

  it("each call is one row: the spinner and what it is doing while it runs, what it did once done; 正在思考… only while no row runs", () => {
    const t = newTurn("t", "q", null);
    const r = new AssistantRenderer(actions(), hooks);
    const paint = () => r.update(st(conv({ turns: [t], busy: t.status === "streaming" })));
    paint();
    expect(line(r)!.textContent).toBe("正在思考…");
    expect(line(r)!.querySelector("i.ew-spin")).not.toBeNull();
    applyEvent(t, start("c1", "search_wiki", "正在搜索：碱基配对")); paint();
    expect(rows(r).map((x) => [x.className, x.textContent])).toEqual([["ew-trow ew-run", "正在搜索：碱基配对"]]);
    expect(rows(r)[0]!.querySelector("i.ew-spin")).not.toBeNull();
    expect(line(r)).toBeNull();                                    // a row runs: no 正在思考…
    applyEvent(t, end("c1", "在 Wiki 里搜了「碱基配对」 · 2 页", "search_wiki", "在 Wiki 里搜了 {n} 次", ["基因配对", "DNA 复制"])); paint();
    expect(rows(r).map((x) => x.textContent)).toEqual(["在 Wiki 里搜了「碱基配对」 · 2 页"]);
    expect(rows(r)[0]!.querySelector("i.ew-spin")).toBeNull();
    expect(line(r)!.textContent).toBe("正在思考…");               // the model again
    applyEvent(t, { type: "text_delta", text: "先看定义。", at: 0 }); paint();
    expect(line(r)).toBeNull();
    applyEvent(t, { type: "status", text: "正在写草稿" }); paint();
    expect(line(r)!.textContent).toBe("正在写草稿");              // a drafting step says so on the waiting line, below the text
    expect(r.root.querySelector(".ew-amsg .ew-answer")!.nextElementSibling).toBe(line(r));
    applyEvent(t, { type: "done", message_id: "m", tool_calls: 2, llm_calls: 3, cost_usd: null, attachments: [], session_attachments: [], stopped: false, confirmed: false }); paint();
    expect(line(r)).toBeNull();
    expect(r.root.querySelector(".ew-srcchip")).toBeNull();      // no chips for search hits
  });
  it("consecutive calls of one kind fold into one row (读了 2 页); a click shows the titles, never ids; the cited pages are listed under the answer", () => {
    const t = newTurn("t", "q", null);
    const a = actions();
    const r = new AssistantRenderer(a, hooks);
    const paint = () => r.update(st(conv({ turns: [t], busy: t.status === "streaming" })));
    const P = (title: string, id: string) => ({ title, page_id: id, url: null, segment_id: null });
    for (const e of [start("c1", "search_wiki", "正在搜索：协方差"),
      end("c1", "在 Wiki 里搜了「协方差」 · 2 页", "search_wiki", "在 Wiki 里搜了 {n} 次", ["均值和方差", "协方差矩阵"]),
      start("c2", "read_page", "正在读《均值和方差》第 2 节"), end("c2", "读了《均值和方差》第 2 节", "read_page", "读了 {k} 页", ["均值和方差"], [P("均值和方差", "p1")]),
      start("c3", "read_page", "正在读《协方差矩阵》"), end("c3", "读了《协方差矩阵》", "read_page", "读了 {k} 页", ["协方差矩阵"], [P("协方差矩阵", "p3")]),
      { type: "text_delta", text: "协方差看两个量是不是一起变。", at: null },
      { type: "done", message_id: "m", tool_calls: 3, llm_calls: 2, cost_usd: null, attachments: [], session_attachments: [], stopped: false, confirmed: false }] as AssistantEvent[]) applyEvent(t, e);
    paint();
    expect(rows(r).map((x) => x.textContent)).toEqual(["在 Wiki 里搜了「协方差」 · 2 页", "读了 2 页"]);
    expect(r.root.querySelector(".ew-amsg > .ew-trows")!.nextElementSibling!.classList.contains("ew-answer")).toBe(true);
    expect(r.root.querySelector(".ew-titems")).toBeNull();        // results hidden by default
    rows(r)[0]!.click();
    expect([...r.root.querySelectorAll(".ew-titems li")].map((x) => x.textContent)).toEqual(["均值和方差", "协方差矩阵"]);
    expect(rows(r)[0]!.getAttribute("aria-expanded")).toBe("true");
    rows(r)[1]!.click();                                          // a folded row: each call's line
    expect([...r.root.querySelectorAll(".ew-titems")].pop()!.textContent).toBe("读了《均值和方差》第 2 节读了《协方差矩阵》");
    rows(r)[0]!.click();
    expect(r.root.querySelectorAll(".ew-titems")).toHaveLength(1);
    const src = r.root.querySelector(".ew-amsg > .ew-srcl")!;
    expect([...src.children].map((x) => x.textContent)).toEqual(["来源", "均值和方差", "协方差矩阵"]);
    src.querySelector<HTMLButtonElement>(".ew-srclink")!.click();
    expect(a.openSource).toHaveBeenCalledWith(P("均值和方差", "p1"));
  });
  it("a row never shows tool names, ids or model text: only what the server worded (none: no row, 正在思考…)", () => {
    const t = newTurn("t", "q", null);
    const r = new AssistantRenderer(actions(), hooks);
    applyEvent(t, { type: "tool_start", call_id: "c9", name: "read_review_digest", effect: "network", summary: "period=week", label: "" });
    r.update(st(conv({ turns: [t] })));
    expect(rows(r)).toEqual([]);
    expect(line(r)!.textContent).toBe("正在思考…");
    expect(r.root.textContent).not.toMatch(/read_review_digest|c9|period/);
  });
});
