import { expect, it } from "vitest";
import { FAKE_ANSWER, FakeAssistantApi } from "./fake-api";
import { parseEvent } from "./parse";

it("every scripted fake event passes the strict contract parser unchanged", () => {
  for (const e of FAKE_ANSWER) expect(parseEvent(e.type, JSON.parse(JSON.stringify(e)))).toEqual(e);
  const types = FAKE_ANSWER.map((e) => e.type);
  for (const t of ["text_delta", "tool_start", "tool_end", "proposal", "observed", "done"]) expect(types).toContain(t);
});

it("records calls and consumes scripted replies in order", async () => {
  const f = new FakeAssistantApi();
  f.messageReplies.push({ events: [{ type: "error", code: "tool_limit", retryable: false, detail: "", interrupted: false }] });
  const s = await f.createSession({ note_concept_id: "pca" });
  const got: string[] = [];
  await f.sendMessage(s.session_id, { text: "q" }, (e) => got.push(e.type));
  expect(got).toEqual(["error"]);
  await f.sendMessage(s.session_id, { text: "q2" }, (e) => got.push(e.type)); // default reply after the queue is empty
  expect(got.slice(1)).toEqual(FAKE_ANSWER.map((e) => e.type));
  expect(f.calls.createSession).toEqual([{ note_concept_id: "pca" }]);
  expect(f.calls.sendMessage.map((c) => c.body.text)).toEqual(["q", "q2"]);
  await expect(f.getSession("missing")).rejects.toMatchObject({ status: 404 });
});

it("listSessions records q, answers from sessionList filtered by q, and consumes scripted replies first", async () => {
  const f = new FakeAssistantApi();
  const row = (id: string, title: string) => ({ session_id: id, title, page_id: null, page_title: "页", note_concept_id: null,
    created_at: "2026-10-14T01:00:00Z", last_activity_at: "2026-10-14T01:00:00Z", message_count: 2 });
  f.sessionList = [row("a", "特征值"), row("b", "协方差")];
  expect((await f.listSessions("")).map((r) => r.session_id)).toEqual(["a", "b"]);
  expect((await f.listSessions("协方")).map((r) => r.session_id)).toEqual(["b"]);
  f.listReplies.push({ throws: new Error("down") });
  await expect(f.listSessions("x")).rejects.toThrow("down");
  expect(f.calls.listSessions.map((c) => c.q)).toEqual(["", "协方", "x"]);
});
