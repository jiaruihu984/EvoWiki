import { describe, expect, it, vi } from "vitest";
import type { HttpRequest, JsonTransport, StreamTransport } from "../api/client";
import { ApiError, OfflineError } from "../api/errors";
import { AssistantClient, writeOut } from "./client";
import { ContractError } from "./parse";

const cfg = () => ({ baseUrl: "http://127.0.0.1:8000", userId: "jerry" });
const json = (status: number, body: unknown): JsonTransport => ({ request: vi.fn(() => Promise.resolve({ status, text: JSON.stringify(body) })) });
const sse = (chunks: string[], status = 200): StreamTransport & { reqs: HttpRequest[] } => {
  const reqs: HttpRequest[] = [];
  return { reqs, stream: (req, onChunk) => { reqs.push(req); if (status === 200) chunks.forEach(onChunk); return Promise.resolve({ status, text: status === 200 ? "" : chunks.join("") }); } };
};
const frame = (e: string, d: unknown) => `event: ${e}\ndata: ${JSON.stringify(d)}\n\n`;

describe("streamed frames", () => {
  const done = { type: "done", message_id: "m", tool_calls: 0, llm_calls: 1, cost_usd: null, attachments: [], session_attachments: [], stopped: false };
  it("skips keep-alive comments and reads frames split across chunks and CRLF", async () => {
    const wire = ": ping\n\n" + frame("text_delta", { type: "text_delta", text: "a" }) + ": ping\r\n\r\n" + frame("done", done).replace(/\n/g, "\r\n");
    const chunks = [wire.slice(0, 5), wire.slice(5, 40), wire.slice(40, 41), wire.slice(41)];
    const seen: string[] = [];
    await new AssistantClient(cfg, json(200, {}), sse(chunks)).sendMessage("s1", { text: "q" }, (e) => seen.push(e.type));
    expect(seen).toEqual(["text_delta", "done"]);
  });
  it("data that is not JSON is a ContractError after the transport ends", async () => {
    const c = new AssistantClient(cfg, json(200, {}), sse([frame("text_delta", { type: "text_delta", text: "a" }), "event: done\ndata: {\n\n"]));
    await expect(c.sendMessage("s1", { text: "q" }, () => undefined)).rejects.toThrow(ContractError);
  });
});

describe("AssistantClient", () => {
  it("createSession posts the body with the user header", async () => {
    const http = json(200, { session_id: "s1" });
    const c = new AssistantClient(cfg, http, sse([]));
    await expect(c.createSession({ page_id: "p1" })).resolves.toEqual({ session_id: "s1" });
    expect((http.request as ReturnType<typeof vi.fn>).mock.calls).toEqual([[{ method: "POST", url: "http://127.0.0.1:8000/assistant/sessions",
      headers: { "X-EvoWiki-User": "jerry", "Content-Type": "application/json" }, body: '{"page_id":"p1"}' }]]);
  });
  it("listSessions GETs /assistant/sessions with q (encoded, omitted when empty) and limit, and parses rows", async () => {
    const row = { session_id: "s1", title: "t", page_id: null, page_title: "", note_concept_id: null,
      created_at: "2026-10-14T01:00:00Z", last_activity_at: "2026-10-14T02:00:00Z", message_count: 2 };
    const http = json(200, { sessions: [row] });
    const c = new AssistantClient(cfg, http, sse([]));
    await expect(c.listSessions("特征 值&", 50)).resolves.toEqual([row]);
    await c.listSessions("");
    const calls = (http.request as ReturnType<typeof vi.fn>).mock.calls as [HttpRequest][];
    expect(calls[0]![0]).toMatchObject({ method: "GET", url: "http://127.0.0.1:8000/assistant/sessions?q=%E7%89%B9%E5%BE%81%20%E5%80%BC%26&limit=50", headers: { "X-EvoWiki-User": "jerry" } });
    expect(calls[1]![0].url).toBe("http://127.0.0.1:8000/assistant/sessions?limit=100");
  });
  it("listSessions fails closed: HTTP errors throw ApiError; a malformed row throws ContractError", async () => {
    await expect(new AssistantClient(cfg, json(500, { detail: "x" }), sse([])).listSessions("")).rejects.toBeInstanceOf(ApiError);
    await expect(new AssistantClient(cfg, json(200, { sessions: [{ session_id: "s" }] }), sse([])).listSessions("")).rejects.toBeInstanceOf(ContractError);
    await expect(new AssistantClient(cfg, json(200, { sessions: "no" }), sse([])).listSessions("")).rejects.toBeInstanceOf(ContractError);
  });
  it("sendMessage posts text and selection{text,heading} and streams parsed events in order", async () => {
    const t = sse([frame("text_delta", { type: "text_delta", text: "你" }), ": ping\n\n", frame("done", { type: "done", stopped: false, message_id: "m", tool_calls: 0, llm_calls: 1 })]);
    const got: string[] = [];
    await new AssistantClient(cfg, json(200, {}), t).sendMessage("s/1", { text: "问", selection: { kind: "text", text: "一段", heading: "H" } }, (e) => got.push(e.type));
    expect(got).toEqual(["text_delta", "done"]);
    expect(t.reqs[0]!.url).toBe("http://127.0.0.1:8000/assistant/sessions/s%2F1/messages");
    expect(JSON.parse(t.reqs[0]!.body!)).toEqual({ text: "问", selection: { kind: "text", text: "一段", heading: "H" } });
  });
  it("omits selection when there is none", async () => {
    const t = sse([]);
    await new AssistantClient(cfg, json(200, {}), t).sendMessage("s", { text: "问" }, () => undefined);
    expect(JSON.parse(t.reqs[0]!.body!)).toEqual({ text: "问" });
  });
  it("posts the open note's page (the turn's 当前页), and none when no note is open", async () => {
    const t = sse([]);
    await new AssistantClient(cfg, json(200, {}), t).sendMessage("s", { text: "这页", page_id: "pg_1" }, () => undefined);
    await new AssistantClient(cfg, json(200, {}), t).sendMessage("s", { text: "问", page_id: null }, () => undefined);
    expect(t.reqs.map((r) => JSON.parse(r.body!) as unknown)).toEqual([{ text: "这页", page_id: "pg_1" }, { text: "问" }]);
  });
  it("posts the message's attachments (source ids), and none when there are none", async () => {
    const t = sse([]);
    await new AssistantClient(cfg, json(200, {}), t).sendMessage("s", { text: "问", attachments: ["src_a", "src_b"] }, () => undefined);
    await new AssistantClient(cfg, json(200, {}), t).sendMessage("s", { text: "", attachments: ["src_a"] }, () => undefined);
    await new AssistantClient(cfg, json(200, {}), t).sendMessage("s", { text: "问", attachments: [] }, () => undefined);
    expect(t.reqs.map((r) => JSON.parse(r.body!) as unknown)).toEqual([
      { text: "问", attachments: ["src_a", "src_b"] }, { text: "", attachments: ["src_a"] }, { text: "问" }]);
  });
  it("posts 开始's intent with the form's goal (the server runs new_goal itself; 背景 rides in the text)", async () => {
    const t = sse([]);
    await new AssistantClient(cfg, json(200, {}), t).sendMessage("s", { text: "我要学黑壳虾\n背景：学过高中生物", intent: "learn_goal",
      goal: "我要学黑壳虾" }, () => undefined);
    expect(JSON.parse(t.reqs[0]!.body!)).toEqual({ text: "我要学黑壳虾\n背景：学过高中生物", intent: "learn_goal", goal: "我要学黑壳虾" });
  });
  it("posts the diagnosis's intent and the 摸底 grid's marks (reply_data) as they are", async () => {
    const t = sse([]);
    const c = new AssistantClient(cfg, json(200, {}), t);
    await c.sendMessage("s", { text: "确认", intent: "diagnose", draft_id: "draft_1" }, () => undefined);
    await c.sendMessage("s", { text: "分类与鉴别 掌握", reply_data: { marks: { id: "know" } } }, () => undefined);
    expect(t.reqs.map((r) => JSON.parse(r.body!) as unknown)).toEqual([
      { text: "确认", intent: "diagnose", draft_id: "draft_1" }, { text: "分类与鉴别 掌握", reply_data: { marks: { id: "know" } } }]);
  });
  it("a malformed known event rejects with ContractError after the transport resolves, and stops delivering events", async () => {
    const got: string[] = [];
    const p = new AssistantClient(cfg, json(200, {}), sse([frame("text_delta", { type: "text_delta" }), frame("text_delta", { type: "text_delta", text: "x" })]))
      .sendMessage("s", { text: "q" }, (e) => got.push(e.type));
    await expect(p).rejects.toBeInstanceOf(ContractError);
    expect(got).toEqual([]);
  });
  it("non-2xx → ApiError with the server detail; offline propagates", async () => {
    await expect(new AssistantClient(cfg, json(200, {}), sse(['{"detail":"no session"}'], 404)).sendMessage("s", { text: "q" }, () => undefined))
      .rejects.toMatchObject({ status: 404, detail: "no session" });
    const off: JsonTransport = { request: () => Promise.reject(new OfflineError()) };
    await expect(new AssistantClient(cfg, off, sse([])).decide("p", "accept")).rejects.toBeInstanceOf(OfflineError);
    await expect(new AssistantClient(cfg, json(500, { detail: "x" }), sse([])).getSession("s")).rejects.toBeInstanceOf(ApiError);
  });
  it("decide and getSession hit the contract paths and parse strictly", async () => {
    const http = json(200, { proposal_id: "p 1", status: "rejected" });
    await new AssistantClient(cfg, http, sse([])).decide("p 1", "reject");
    expect((http.request as ReturnType<typeof vi.fn>).mock.calls[0]![0]).toMatchObject({ method: "POST", url: "http://127.0.0.1:8000/assistant/proposals/p%201/reject" });
    await expect(new AssistantClient(cfg, json(200, { session_id: "s" }), sse([])).getSession("s")).rejects.toBeInstanceOf(ContractError);
  });
});

describe("AssistantClient: action cards, writes", () => {
  const row = { action_id: "a 1", kind: "page_split", scope: "new_page", level: "ask", status: "executed", source: "assistant", summary: "s", why: "", content: {},
    page_id: null, detail: "", created_at: "", decided_at: null, expires_at: null, can_undo: true, diff: [], confirm: true, cost_usd: null, new_pages: ["p1"] };
  const calls = (h: JsonTransport): HttpRequest[] => ((h.request as ReturnType<typeof vi.fn>).mock.calls as [HttpRequest][]).map((c) => c[0]);

  it("approve posts the edited draft to /wiki/bot/actions/{id}/approve ({} without one); reject and undo post {}", async () => {
    const http = json(200, row);
    const c = new AssistantClient(cfg, http, sse([]));
    const draft = { pages: [{ key: "q1" }, { title: "新页", isNew: true }] };
    await expect(c.approveAction("a 1", draft)).resolves.toEqual(row);
    await c.approveAction("a 1");
    await c.rejectAction("a 1");
    await c.undoAction("a 1");
    expect(calls(http).map((r) => [r.method, r.url, r.body])).toEqual([
      ["POST", "http://127.0.0.1:8000/wiki/bot/actions/a%201/approve", JSON.stringify({ draft })],
      ["POST", "http://127.0.0.1:8000/wiki/bot/actions/a%201/approve", "{}"],
      ["POST", "http://127.0.0.1:8000/wiki/bot/actions/a%201/reject", "{}"],
      ["POST", "http://127.0.0.1:8000/wiki/bot/actions/a%201/undo", "{}"],
    ]);
  });
  it("listActions GETs /wiki/bot/actions (with ?status=) and checks each row", async () => {
    const http = json(200, [row]);
    const c = new AssistantClient(cfg, http, sse([]));
    await expect(c.listActions("proposed")).resolves.toEqual([row]);
    expect(calls(http)[0]!.url).toBe("http://127.0.0.1:8000/wiki/bot/actions?status=proposed");
    await expect(new AssistantClient(cfg, json(200, [{ action_id: "a" }]), sse([])).listActions()).rejects.toBeInstanceOf(ContractError);
    await expect(new AssistantClient(cfg, json(200, { x: 1 }), sse([])).listActions()).rejects.toBeInstanceOf(ContractError);
  });
  it("writes GETs /wiki/writes?unwritten=true&page_ids= (comma list, encoded) and refuses an unknown state", async () => {
    const w = { page_id: "p1", run_id: "r1", kind: "organize", state: "writing", pass: null, section: 1, of: 4, started_at: null, cost_usd: 0 };
    const http = json(200, [w]);
    const c = new AssistantClient(cfg, http, sse([]));
    await expect(c.writes(["p1", "p2"])).resolves.toEqual([w]);
    expect(calls(http)[0]!.url).toBe("http://127.0.0.1:8000/wiki/writes?unwritten=true&page_ids=p1%2Cp2");
    await expect(new AssistantClient(cfg, json(200, [{ page_id: "p", state: "sleeping" }]), sse([])).writes(["p"])).rejects.toBeInstanceOf(ContractError);
  });
  it("the known-claim confirm uses GET / POST", async () => {
    const k = new AssistantClient(cfg, json(200, { stem: "题", options: ["甲", "乙"] }), sse([]));
    await expect(k.knownItem("a1")).resolves.toEqual({ stem: "题", options: ["甲", "乙"] });
    const ka = new AssistantClient(cfg, json(200, { correct: false, correct_index: 1, undone: true, message: "已收回" }), sse([]));
    await expect(ka.knownAnswer("a1", 0)).resolves.toMatchObject({ undone: true });
  });
  it("a refused click is an ApiError with the server's reason", async () => {
    const c = new AssistantClient(cfg, json(409, { detail: "动作已经是 undone，不能批准" }), sse([]));
    await expect(c.approveAction("a1")).rejects.toMatchObject({ status: 409, detail: "动作已经是 undone，不能批准" });
  });
});


it("requires the current WriteOut task identity contract", () => {
  for (const bad of [
    { page_id: "p", state: "writing" },
    { page_id: "p", state: "writing", run_id: null },
    { page_id: "p", state: "writing", run_id: "" },
    { page_id: "p", state: "done", run_id: 1 },
    { page_id: "p", state: "unwritten" },
    { page_id: "p", state: "unwritten", run_id: "old-run" },
  ]) expect(() => writeOut(bad)).toThrow(ContractError);
  expect(writeOut({ page_id: "p", state: "writing", run_id: "r1" }).run_id).toBe("r1");
  expect(writeOut({ page_id: "p", state: "unwritten", run_id: null }).run_id).toBeNull();
});
