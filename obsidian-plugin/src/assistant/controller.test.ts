import { describe, expect, it, vi } from "vitest";
import { AbortedError, ApiError, OfflineError } from "../api/errors";
import { AssistantController } from "./controller";
import { FakeAssistantApi } from "./fake-api";
import { sessionKey, type AssistantSessionStore, type Identity, type Target } from "./session-store";
import { MAX_ATTACHMENTS, type SessionOut, type SessionSummaryOut } from "./types";
import type { AttachmentPort, AttachProgress } from "./attachments";
import type { SourceOut, WriteOut } from "../wiki/schema";
/** A stored call's row fields when the test does not care about them. */
const NO_ROW = { label: "", group: "", group_label: "", items: [], refs: [] };
/** The same as the restored turn's row (no label: no row is drawn). */
const ROW0 = { done: "", group: "", groupLabel: "", items: [], refs: [] };

const PAGE: Target = { kind: "page", pageId: "page_pca", title: "主成分分析" };
const NOTE: Target = { kind: "note", path: "ML/PCA.md", conceptId: "pca", title: "PCA" };
const JERRY: Identity = { server: "http://127.0.0.1:8000", user: "jerry" };
const EMPTY = { createSession: [], sendMessage: [], decide: [], getSession: [], listSessions: [] };

function rig(stored: Record<string, string> = {}, start: Identity = JERRY, pageEdited?: (pageId: string, applied: string[]) => void) {
  const api = new FakeAssistantApi(); const mem = { ...stored };
  const writes: [string, string | null][] = [];
  const store: AssistantSessionStore = {
    get: (k) => mem[k] ?? null,
    set: (k, v) => { writes.push([k, v]); delete mem[k]; if (v) mem[k] = v; return Promise.resolve(); },
  };
  let n = 0;
  const who = { id: start };
  const ctl = new AssistantController({ api, store, identity: () => who.id, newId: () => `t${++n}`, pageEdited });
  return { api, mem, writes, who, ctl };
}
const flush = () => new Promise((r) => setTimeout(r, 0));

const RESTORED: SessionOut = {
  gone_sources: [], session_id: "ses_old", page_id: null, recap: { understood: ["方差"], unclear: [], edits: [] },
  messages: [
    { message_id: "m1", role: "user", card_at: null, stopped: false, intent: null, draft_id: null, asks: [], text: "为什么最大方差", sources: [], proposals: [], actions: [], selection: null, tools: [], attachments: [], rating: null },
    { message_id: "m2", role: "assistant", card_at: null, stopped: false, intent: null, draft_id: null, asks: [], text: "因为信息多", sources: [{ title: "主成分分析", page_id: "page_pca", url: null, segment_id: null }], proposals: ["pr_a", "pr_b"], actions: [], selection: null, tools: [], attachments: [], rating: null },
    { message_id: "m3", role: "user", card_at: null, stopped: false, intent: null, draft_id: null, asks: [], text: "还有吗", sources: [], proposals: [], actions: [], selection: null, tools: [], attachments: [], rating: null },
  ], decisions: [], proposals: [], actions: [],
};

describe("1. configuration", () => {
  it("is not configured without a server or a user, and send is then a no-op", async () => {
    for (const id of [{ server: "", user: "jerry" }, { server: "http://x", user: "" }]) {
      const { api, ctl } = rig({}, id);
      ctl.show(NOTE); ctl.setDraft("q");
      expect(ctl.state.configured).toBe(false);
      await ctl.send();
      expect(api.calls).toEqual(EMPTY);
      expect(ctl.state.conv?.turns ?? []).toEqual([]);
    }
    expect(rig().ctl.state.configured).toBe(true);
  });
});

describe("2. conversations per target and identity", () => {
  it("switching notes and back keeps turns, draft and quote", async () => {
    const { ctl } = rig();
    ctl.show(NOTE); ctl.setDraft("q"); await ctl.send();
    ctl.setDraft("草稿"); ctl.show(NOTE);
    ctl.state.conv!.quote = { text: "一段", heading: null };
    ctl.show(PAGE);
    expect(ctl.state.conv?.target).toEqual(PAGE);
    expect(ctl.state.conv?.turns).toEqual([]);
    expect(ctl.state.conv?.draft).toBe("");
    ctl.show(NOTE);
    expect(ctl.state.conv).toMatchObject({ draft: "草稿", quote: { text: "一段", heading: null } });
    expect(ctl.state.conv?.turns).toHaveLength(1);
  });
  it("a different identity yields a different conversation", async () => {
    const { ctl, who } = rig();
    ctl.show(NOTE); ctl.setDraft("q"); await ctl.send();
    const first = ctl.state.conv!;
    who.id = { server: JERRY.server, user: "alice" };
    ctl.show(NOTE);
    expect(ctl.state.conv).not.toBe(first);
    expect(ctl.state.conv?.turns).toEqual([]);
    expect(ctl.state.conv?.key).toBe(sessionKey(who.id, NOTE));
  });
  it("no note, or a plain note: the conversation without a page (nothing read)", async () => {
    const { api, ctl } = rig();
    ctl.show({ kind: "none" });
    expect(ctl.state.conv?.target).toEqual({ kind: "free" });
    expect(api.calls).toEqual(EMPTY);
    ctl.setDraft("随便问一句");
    await ctl.send();
    expect(api.calls.createSession).toEqual([{}]);
    expect(api.calls.sendMessage).toHaveLength(1);
    ctl.show({ kind: "unsupported", title: "x" });
    expect(ctl.state.active).toEqual({ kind: "unsupported", title: "x" });
    expect(ctl.state.conv?.target).toEqual({ kind: "free" });
    expect(ctl.state.conv?.turns).toHaveLength(1);           // the same free conversation: chat always works
  });
  it("no note, with a stored free session (after a plugin reload): that conversation is read and shown", async () => {
    const free = sessionKey(JERRY, { kind: "free" });
    const { api, ctl } = rig({ [free]: "ses_free" });
    api.sessionOuts.set("ses_free", RESTORED);
    expect(ctl.state.conv?.target).toEqual({ kind: "free" });   // the panel drew before any note was shown
    ctl.show({ kind: "none" }); await flush();
    expect(api.calls.getSession).toEqual(["ses_free"]);
    expect(ctl.state.conv?.sessionId).toBe("ses_free");
    expect(ctl.state.conv?.turns.length).toBeGreaterThan(0);
  });
});

describe("3. restore", () => {
  const key = sessionKey(JERRY, NOTE);
  it("a stored id issues exactly one getSession and builds turns from messages", async () => {
    const { api, ctl } = rig({ [key]: "ses_old" });
    api.sessionOuts.set("ses_old", RESTORED);
    ctl.show(NOTE); ctl.show(NOTE);
    expect(ctl.state.conv?.restore).toBe("loading");
    await flush();
    ctl.show(PAGE); ctl.show(NOTE);
    expect(api.calls.getSession).toEqual(["ses_old"]);
    const c = ctl.state.conv!;
    expect(c).toMatchObject({ restore: "none", sessionId: "ses_old", recap: { understood: ["方差"], unclear: [], edits: [] } });
    expect(c.turns.map((t) => [t.question, t.answer, t.status, t.restored])).toEqual([
      ["为什么最大方差", "因为信息多", "done", { sources: [{ title: "主成分分析", page_id: "page_pca", url: null, segment_id: null }], proposalCount: 2 }],
      ["还有吗", "", "done", null],
    ]);
  });
  it("a last question with no answer is followed again from its start: the running turn's answer shows, busy meanwhile", async () => {
    const { api, ctl } = rig({ [key]: "ses_old" });
    api.sessionOuts.set("ses_old", RESTORED);
    api.resumeReplies.push({ events: [{ type: "text_delta", text: "还有一点", at: 0 }, { type: "done", message_id: "m4", tool_calls: 0, llm_calls: 1, cost_usd: null, attachments: [], session_attachments: [], stopped: false, confirmed: false }] });
    ctl.show(NOTE);
    await flush(); await flush();
    expect(api.resumed).toEqual([{ sessionId: "ses_old", lastEventId: "m3:0" }]);
    const last = ctl.state.conv!.turns[1]!;
    expect(last).toMatchObject({ question: "还有吗", answer: "还有一点", status: "done", answerId: "m4" });
    expect(ctl.state.conv!.busy).toBe(false);
  });
  it("nothing to follow (404: no such turn) leaves the restored question as it was read", async () => {
    const { api, ctl } = rig({ [key]: "ses_old" });
    api.sessionOuts.set("ses_old", RESTORED);
    api.resumeReplies.push({ throws: new ApiError(404, "no turn") });
    ctl.show(NOTE);
    await flush(); await flush();
    expect(ctl.state.conv!.turns[1]).toMatchObject({ answer: "", status: "done", error: null });
    expect(ctl.state.conv!.busy).toBe(false);
  });
  it("the 「更早的对话已省略」 notice gets no rating row (no answer id)", async () => {
    const { api, ctl } = rig({ [key]: "ses_old" });
    api.sessionOuts.set("ses_old", { ...RESTORED, messages: [{ message_id: "omit_e1", role: "assistant", card_at: null, stopped: false, intent: null, draft_id: null, asks: [], text: "更早的 3 轮对话已省略", sources: [], proposals: [], actions: [], selection: null, tools: [], attachments: [], rating: null },
      ...RESTORED.messages.slice(0, 2)] });
    ctl.show(NOTE);
    await flush();
    expect(ctl.state.conv!.turns.map((t) => t.answerId)).toEqual([null, "m2"]);
  });
  it("an assistant message's stored tool calls become the restored turn's tool line (finished)", async () => {
    const { api, ctl } = rig({ [key]: "ses_old" });
    api.sessionOuts.set("ses_old", { ...RESTORED, messages: [RESTORED.messages[0]!, { ...RESTORED.messages[1]!, tools: [
      { call_id: "d1", name: "search_wiki", effect: "read", summary: "方差", ok: true, sources: [], ...NO_ROW },
      { call_id: "d2", name: "fetch_url", effect: "network", summary: "https://w/z", ok: false, sources: [], ...NO_ROW }] }] });
    ctl.show(NOTE);
    await flush();
    expect(ctl.state.conv!.turns[0]!.tools).toEqual([
      { callId: "d1", name: "search_wiki", effect: "read", summary: "方差", status: "ok", sources: [], label: "", ...ROW0 },
      { callId: "d2", name: "fetch_url", effect: "network", summary: "https://w/z", status: "failed", sources: [], label: "", ...ROW0 }]);
  });
  it("a user message's stored quote becomes that turn's quote", async () => {
    const { api, ctl } = rig({ [key]: "ses_old" });
    api.sessionOuts.set("ses_old", { ...RESTORED, messages: [
      { message_id: "m1", role: "user", card_at: null, stopped: false, intent: null, draft_id: null, asks: [], text: "这是什么", selection: { kind: "text", text: "协方差矩阵", heading: "推导", artifact_id: null }, sources: [], proposals: [], actions: [], tools: [], attachments: [], rating: null },
      { message_id: "m2", role: "assistant", card_at: null, stopped: false, intent: null, draft_id: null, asks: [], text: "答", sources: [], proposals: [], actions: [], selection: null, tools: [], attachments: [], rating: null },
      { message_id: "m3", role: "user", card_at: null, stopped: false, intent: null, draft_id: null, asks: [], text: "还有吗", selection: { kind: "text", text: "一段", heading: null, artifact_id: null }, sources: [], proposals: [], actions: [], tools: [], attachments: [], rating: null },
      { message_id: "m4", role: "user", card_at: null, stopped: false, intent: null, draft_id: null, asks: [], text: "没引用", selection: null, sources: [], proposals: [], actions: [], tools: [], attachments: [], rating: null },
    ] });
    ctl.show(NOTE); await flush();
    expect(ctl.state.conv!.turns.map((t) => t.quote)).toEqual([
      { kind: "text", text: "协方差矩阵", heading: "推导" }, { kind: "text", text: "一段", heading: null }, null]);
  });
  it.each([404, 403])("%i compare-and-clears the stored id and marks the conversation gone", async (status) => {
    const { api, mem, ctl } = rig({ [key]: "ses_old" });
    api.sessionOuts.set("ses_old", { throws: new ApiError(status, "gone") });
    ctl.show(NOTE); await flush();
    expect(ctl.state.conv).toMatchObject({ restore: "gone", sessionId: null });
    expect(mem).toEqual({});
  });
  it("a gone restore does not clear a stored id that changed meanwhile", async () => {
    const { api, mem, ctl } = rig({ [key]: "ses_old" });
    api.sessionOuts.set("ses_old", { throws: new ApiError(404, "gone") });
    ctl.show(NOTE);
    mem[key] = "ses_newer";
    await flush();
    expect(mem[key]).toBe("ses_newer");
    expect(ctl.state.conv?.restore).toBe("gone");
  });
  it("other errors → failed; reload re-runs one GET", async () => {
    const { api, ctl } = rig({ [key]: "ses_old" });
    api.sessionOuts.set("ses_old", { throws: new OfflineError() });
    ctl.show(NOTE); await flush();
    expect(ctl.state.conv).toMatchObject({ restore: "failed", sessionId: "ses_old" });
    api.sessionOuts.set("ses_old", RESTORED);
    await ctl.reload();
    expect(api.calls.getSession).toHaveLength(2);
    expect(ctl.state.conv?.restore).toBe("none");
    expect(ctl.state.conv?.turns).toHaveLength(2);
  });
  it("show for none/unsupported, or a target without a stored id, makes no request", () => {
    const { api, ctl } = rig();
    ctl.show({ kind: "none" }); ctl.show({ kind: "unsupported", title: "x" }); ctl.show(NOTE); ctl.show(PAGE);
    expect(api.calls).toEqual(EMPTY);
  });
  it("send is a no-op while restoring", async () => {
    const { api, ctl } = rig({ [key]: "ses_old" });
    api.sessionOuts.set("ses_old", RESTORED);
    ctl.show(NOTE); ctl.setDraft("q");
    const p = ctl.send();
    await p; await flush();
    expect(api.calls.sendMessage).toHaveLength(0);
    expect(ctl.state.conv?.draft).toBe("q");
  });
});

describe("4. the selection is sent only when the user clicks 问", () => {
  it("attaching a quote, typing and switching notes send nothing", () => {
    const { api, ctl } = rig();
    ctl.attachQuote(NOTE, { text: "协方差矩阵的特征向量", heading: "推导" });
    ctl.setDraft("这句什么意思");
    ctl.show(PAGE); ctl.show(NOTE);
    expect(api.calls).toEqual(EMPTY);
    expect(ctl.state.conv?.quote).toEqual({ text: "协方差矩阵的特征向量", heading: "推导" });
  });
  it("what is selected in the note shows as the quote: it follows the selection, × keeps it off, sending keeps it off, a menu quote stays", async () => {
    const { ctl } = rig();
    ctl.show(NOTE);
    const one = { text: "第一句", heading: "推导" }, two = { text: "第二句", heading: "推导" };
    ctl.selectionChanged(one);
    expect(ctl.state.conv?.quote).toEqual(one);
    ctl.selectionChanged(two);
    expect(ctl.state.conv?.quote).toEqual(two);
    ctl.removeQuote();
    ctl.selectionChanged(two);
    expect(ctl.state.conv?.quote).toBeNull();
    ctl.selectionChanged(null);
    ctl.selectionChanged(two);
    expect(ctl.state.conv?.quote).toEqual(two);
    ctl.selectionChanged(null);
    expect(ctl.state.conv?.quote).toBeNull();
    ctl.selectionChanged(one);
    ctl.setDraft("这句什么意思");
    await ctl.send();
    expect(ctl.state.conv?.turns.slice(-1)[0]?.quote).toEqual(one);
    expect(ctl.state.conv?.quote).toBeNull();
    ctl.selectionChanged(one);
    expect(ctl.state.conv?.quote).toBeNull();
    ctl.attachQuote(NOTE, two);
    ctl.selectionChanged(null);
    expect(ctl.state.conv?.quote).toEqual(two);
  });
  it("the 重讲这一节 标记为不会 leaves goes out as reteach_section, unless he changes the words", async () => {
    const { ctl } = rig();
    ctl.attachQuote(NOTE, { text: "一段", heading: "推导" }, "重讲这一节");
    expect(ctl.state.conv?.draft).toBe("重讲这一节");
    await ctl.send();
    expect(ctl.state.conv?.turns.slice(-1)[0]?.reteach).toBe(true);
    ctl.attachQuote(NOTE, { text: "一段", heading: "推导" }, "重讲这一节");
    ctl.setDraft("这句什么意思");
    await ctl.send();
    expect(ctl.state.conv?.turns.slice(-1)[0]?.reteach).toBeUndefined();
  });
  it("attachQuote shows the target and bumps focusTick", () => {
    const { ctl } = rig();
    ctl.show(PAGE);
    const before = ctl.state.focusTick;
    ctl.attachQuote(NOTE, { text: "一段", heading: null });
    expect(ctl.state.active).toEqual(NOTE);
    expect(ctl.state.focusTick).toBe(before + 1);
  });
  it("a quote on a conversation still being read back stays on it: the read lands, the quote is kept", async () => {
    const { api, ctl } = rig({ [sessionKey(JERRY, NOTE)]: "ses_old" });
    api.sessionOuts.set("ses_old", RESTORED);
    ctl.show(NOTE);
    ctl.attachQuote(NOTE, { text: "一段", heading: null });
    await flush();
    expect(ctl.state.conv?.sessionId).toBe("ses_old");
    expect(ctl.state.conv?.turns.length).toBeGreaterThan(0);
    expect(ctl.state.conv?.quote?.text).toBe("一段");
  });
  it("a quote about another page (反馈 on a review card) stays shown while the panel re-reports the open note", () => {
    const { ctl } = rig();
    ctl.show(PAGE);
    ctl.attachQuote(NOTE, { text: "这张卡片：x", heading: null, kind: "wrong_answer" });
    ctl.show(PAGE);
    expect(ctl.state.active).toEqual(NOTE);
    expect(ctl.state.conv?.quote?.text).toBe("这张卡片：x");
    const other: Target = { kind: "page", pageId: "page_other", title: "别的页" };
    ctl.show(other);
    expect(ctl.state.active).toEqual(other);
    ctl.show(PAGE);
    expect(ctl.state.active).toEqual(PAGE);
  });
});

describe("5. send", () => {
  it("send carries the quote once; the next send does not", async () => {
    const { api, mem, ctl } = rig();
    ctl.attachQuote(NOTE, { text: "一段", heading: "推导" }); ctl.setDraft("什么意思");
    await ctl.send();
    expect(api.calls.createSession).toEqual([{ note_concept_id: "pca" }]);
    expect(api.calls.sendMessage[0]!.body).toEqual({ text: "什么意思", selection: { kind: "text", text: "一段", heading: "推导" } });
    expect(mem).toEqual({ [sessionKey(JERRY, NOTE)]: "ses_fake_1" });
    const c = ctl.state.conv!;
    expect(c).toMatchObject({ draft: "", quote: null, busy: false, sessionId: "ses_fake_1" });
    expect(c.turns[0]).toMatchObject({ question: "什么意思", status: "done", quote: { text: "一段", heading: "推导" } });
  });
  it("a quote with a blank draft asks 这里我不太理解。 with the quote (a real, non-blank question)", async () => {
    const { api, ctl } = rig();
    ctl.attachQuote(PAGE, { text: "题目：x\n我选的：a\n正确答案：b", heading: null, kind: "wrong_answer" }); ctl.setDraft("  ");
    await ctl.send();
    expect(api.calls.sendMessage[0]!.body).toEqual({ text: "这里我不太理解。", selection: { kind: "wrong_answer", text: "题目：x\n我选的：a\n正确答案：b" }, page_id: "page_pca" });
    expect(ctl.state.conv!.turns[0]).toMatchObject({ question: "这里我不太理解。", quote: { kind: "wrong_answer" } });
    expect(ctl.state.conv).toMatchObject({ draft: "", quote: null });
  });
  it("a quote without heading sends only its text", async () => {
    const { api, ctl } = rig();
    ctl.attachQuote(PAGE, { text: "一段", heading: null }); ctl.setDraft("q");
    await ctl.send();
    expect(api.calls.sendMessage[0]!.body).toEqual({ text: "q", selection: { kind: "text", text: "一段" }, page_id: "page_pca" });
  });
  it("blank, over-long, busy → no-op", async () => {
    const { api, ctl } = rig();
    ctl.show(NOTE);
    ctl.setDraft("   "); await ctl.send();
    ctl.setDraft("字".repeat(4001)); await ctl.send();
    expect(api.calls).toEqual(EMPTY);
    ctl.setDraft("字".repeat(4000));
    const p = ctl.send();
    expect(ctl.state.conv?.busy).toBe(true);
    ctl.setDraft("again"); const p2 = ctl.send();
    await p; await p2;
    expect(api.calls.sendMessage).toHaveLength(1);
    expect(ctl.state.conv?.draft).toBe("again");
  });
  it("busy is true while streaming and subscribers are notified", async () => {
    const { ctl } = rig();
    const seen: boolean[] = [];
    const off = ctl.subscribe(() => seen.push(ctl.state.conv?.busy ?? false));
    ctl.show(NOTE); ctl.setDraft("q"); await ctl.send();
    off();
    expect(seen).toContain(true);
    expect(seen[seen.length - 1]).toBe(false);
  });
  it("a stream that ends without done is broken and retryable", async () => {
    const { api, ctl } = rig();
    api.messageReplies.push({ events: [{ type: "text_delta", text: "半", at: null }] });
    ctl.show(NOTE); ctl.setDraft("q"); await ctl.send();
    expect(ctl.state.conv!.turns[0]!.error).toEqual({ kind: "broken" });
  });
});

describe("6. quote lifetime", () => {
  it("a removed quote is not sent", async () => {
    const { api, ctl } = rig();
    ctl.attachQuote(PAGE, { text: "一段", heading: null }); ctl.removeQuote(); ctl.setDraft("q");
    await ctl.send();
    expect(api.calls.sendMessage[0]!.body).toEqual({ text: "q", page_id: "page_pca" });
    expect(api.calls.createSession).toEqual([{ page_id: "page_pca" }]);
  });
  it("the second send after a quoted one has no selection and reuses the session", async () => {
    const { api, ctl } = rig();
    ctl.attachQuote(NOTE, { text: "一段", heading: "推导" }); ctl.setDraft("什么意思"); await ctl.send();
    ctl.setDraft("再说说"); await ctl.send();
    expect(api.calls.sendMessage[1]!.body).toEqual({ text: "再说说" });
    expect(api.calls.sendMessage[1]!.sessionId).toBe("ses_fake_1");
    expect(api.calls.createSession).toHaveLength(1);
  });
});

describe("7. errors", () => {
  it("createSession 503 → not_enabled, nothing stored", async () => {
    const { api, mem, ctl } = rig();
    api.sessionReplies.push({ throws: new ApiError(503, "assistant unavailable") });
    ctl.show(NOTE); ctl.setDraft("q"); await ctl.send();
    expect(ctl.state.conv!.turns[0]!.error).toEqual({ kind: "not_enabled" });
    expect(mem).toEqual({});
    expect(api.calls.createSession).toHaveLength(1); expect(api.calls.sendMessage).toHaveLength(0);
  });
  it("a page the server no longer has: the chat goes on without the page", async () => {
    const { api, ctl } = rig();
    api.sessionReplies.push({ throws: new ApiError(404, "unknown page") });
    ctl.show({ kind: "page", pageId: "gone", title: "x" } as never); ctl.setDraft("q"); await ctl.send();
    expect(api.calls.createSession).toEqual([{ page_id: "gone" }, {}]);
    expect(ctl.state.conv!.turns[0]!.error).toBeNull();
  });

  it("createSession 404 → unknown_page (not retryable), nothing stored", async () => {
    const { api, mem, ctl } = rig();
    api.sessionReplies.push({ throws: new ApiError(404, "unknown page") });
    ctl.show(NOTE); ctl.setDraft("q"); await ctl.send();
    const t = ctl.state.conv!.turns[0]!;
    expect(t.error).toEqual({ kind: "unknown_page" });
    expect(mem).toEqual({});
    await ctl.retry(t.id);
    expect(api.calls.createSession).toHaveLength(1); expect(api.calls.sendMessage).toHaveLength(0);
  });
  it("message 409 → busy: the session is kept (stored id and in memory), retry resends on it", async () => {
    const { api, mem, ctl } = rig();
    ctl.show(NOTE); ctl.setDraft("q"); await ctl.send();
    api.messageReplies.push({ throws: new ApiError(409, "session busy") });
    ctl.setDraft("q2"); await ctl.send();
    const t = ctl.state.conv!.turns[1]!;
    expect(t.error).toEqual({ kind: "busy" });
    expect(ctl.state.conv?.sessionId).toBe("ses_fake_1");
    expect(mem).toEqual({ [sessionKey(JERRY, NOTE)]: "ses_fake_1" });
    await ctl.retry(t.id);
    expect(api.calls.createSession).toHaveLength(1);
    expect(api.calls.sendMessage).toHaveLength(3);
    expect(t.status).toBe("done");
  });
  it("offline: one attempt, no queue, retry is a new click", async () => {
    const { api, ctl } = rig();
    api.sessionReplies.push({ throws: new OfflineError() });
    ctl.show(NOTE); ctl.setDraft("q"); await ctl.send();
    const t = ctl.state.conv!.turns[0]!;
    expect(t.error).toEqual({ kind: "offline" });
    expect(api.calls.createSession).toHaveLength(1); expect(api.calls.sendMessage).toHaveLength(0);
    await flush();
    expect(api.calls.createSession).toHaveLength(1);
    await ctl.retry(t.id);
    expect(api.calls.createSession).toHaveLength(2); expect(api.calls.sendMessage).toHaveLength(1);
    expect(t.status).toBe("done");
  });
  it.each([404, 403])("session gone (%i) on send: stored id cleared, retry opens a new session", async (status) => {
    const { api, mem, ctl } = rig();
    ctl.show(NOTE); ctl.setDraft("q"); await ctl.send();
    api.messageReplies.push({ throws: new ApiError(status, "ended") });
    ctl.setDraft("q2"); await ctl.send();
    const t = ctl.state.conv!.turns[1]!;
    expect(t.error).toEqual({ kind: "session_gone" });
    expect(ctl.state.conv?.sessionId).toBeNull();
    expect(Object.keys(mem)).toHaveLength(0);
    expect(api.calls.sendMessage).toHaveLength(2);
    await ctl.retry(t.id);
    expect(api.calls.createSession).toHaveLength(2);
    expect(api.calls.sendMessage).toHaveLength(3);
    expect(mem).toEqual({ [sessionKey(JERRY, NOTE)]: "ses_fake_2" });
  });
  it("session gone does not clear a stored id that was replaced meanwhile", async () => {
    const { api, mem, ctl } = rig();
    ctl.show(NOTE); ctl.setDraft("q"); await ctl.send();
    const key = sessionKey(JERRY, NOTE);
    mem[key] = "ses_other_window";
    api.messageReplies.push({ throws: new ApiError(404, "ended") });
    ctl.setDraft("q2"); await ctl.send();
    expect(mem[key]).toBe("ses_other_window");
  });
  it("offline after part of the answer arrived → broken (回答中途断了, retryable), one sendMessage only", async () => {
    const { api, ctl } = rig();
    api.messageReplies.push({ eventsThenThrow: [{ type: "text_delta", text: "半", at: null }], error: new OfflineError() });
    ctl.show(NOTE); ctl.setDraft("q"); await ctl.send(); await flush();
    expect(ctl.state.conv!.turns[0]!.error).toEqual({ kind: "broken" });
    expect(api.calls.sendMessage).toHaveLength(1);
  });
  it("offline on the message before any event → offline", async () => {
    const { api, ctl } = rig();
    api.messageReplies.push({ throws: new OfflineError() });
    ctl.show(NOTE); ctl.setDraft("q"); await ctl.send();
    expect(ctl.state.conv!.turns[0]!.error).toEqual({ kind: "offline" });
  });
});

describe("8. retry", () => {
  it("back online: a message that could not be sent goes out again (the panel's 重试 / a reconnect), once", async () => {
    const { api, ctl } = rig();
    api.messageReplies.push({ throws: new OfflineError() });
    ctl.show(NOTE); ctl.setDraft("还没发出去"); await ctl.send();
    expect(ctl.state.conv!.turns[0]!.error).toEqual({ kind: "offline" });
    await ctl.resendOffline();
    expect(api.calls.sendMessage.map((c) => c.body.text)).toEqual(["还没发出去", "还没发出去"]);
    expect(ctl.state.conv!.turns).toHaveLength(1);
    expect(ctl.state.conv!.turns[0]!.status).toBe("done");
    await ctl.resendOffline();                                    // nothing waits any more
    expect(api.calls.sendMessage).toHaveLength(2);
  });
  it("only the last turn with a retryable error can be retried", async () => {
    const { api, ctl } = rig();
    api.messageReplies.push({ throws: new OfflineError() });
    ctl.show(NOTE); ctl.setDraft("q1"); await ctl.send();
    const first = ctl.state.conv!.turns[0]!;
    ctl.setDraft("q2"); await ctl.send();
    await ctl.retry(first.id);
    expect(api.calls.sendMessage).toHaveLength(2);
    await ctl.retry(ctl.state.conv!.turns[1]!.id);   // done, not an error
    expect(api.calls.sendMessage).toHaveLength(2);
  });
  it("a non-retryable error is not retried", async () => {
    const { api, ctl } = rig();
    api.messageReplies.push({ events: [{ type: "error", code: "budget_exceeded", retryable: false, detail: "", interrupted: false }] });
    ctl.show(NOTE); ctl.setDraft("q"); await ctl.send();
    await ctl.retry(ctl.state.conv!.turns[0]!.id);
    expect(api.calls.sendMessage).toHaveLength(1);
  });
  it("retry resets the turn, sends one message on the existing session and is not busy-concurrent", async () => {
    const { api, ctl } = rig();
    api.messageReplies.push({ eventsThenThrow: [{ type: "text_delta", text: "半", at: null }], error: new ApiError(502, "bad gateway") });
    ctl.show(NOTE); ctl.setDraft("q"); await ctl.send();
    const t = ctl.state.conv!.turns[0]!;
    expect(t.error).toEqual({ kind: "http", status: 502 });
    const p1 = ctl.retry(t.id); const p2 = ctl.retry(t.id);
    await p1; await p2;
    expect(api.calls.createSession).toHaveLength(1);
    expect(api.calls.sendMessage).toHaveLength(2);
    expect(api.calls.sendMessage[1]!.sessionId).toBe("ses_fake_1");
    expect(t).toMatchObject({ status: "done", error: null });
    expect(t.answer.startsWith("半")).toBe(false);
  });
  it("retry resends the turn's own quote", async () => {
    const { api, ctl } = rig();
    api.messageReplies.push({ throws: new OfflineError() });
    ctl.attachQuote(NOTE, { text: "一段", heading: null }); ctl.setDraft("q"); await ctl.send();
    await ctl.retry(ctl.state.conv!.turns[0]!.id);
    expect(api.calls.sendMessage[1]!.body).toEqual({ text: "q", selection: { kind: "text", text: "一段" } });
  });
});

describe("9. proposal clicks", () => {
  it("each click sends one request; a click while sending is ignored; final states ignore clicks", async () => {
    const { api, ctl } = rig();
    ctl.show(PAGE); ctl.setDraft("q"); await ctl.send();
    const turn = ctl.state.conv!.turns[0]!;
    const p1 = ctl.decide(turn.id, "pr_fake_1", "accept");
    expect(turn.proposals[0]!.status).toBe("sending");
    void ctl.decide(turn.id, "pr_fake_1", "accept");
    await p1;
    expect(api.calls.decide).toEqual([{ proposalId: "pr_fake_1", decision: "accept" }]);
    expect(turn.proposals[0]!.status).toBe("accepted");
    await ctl.decide(turn.id, "pr_fake_1", "reject");
    expect(api.calls.decide).toHaveLength(1);
  });
  it.each(["rejected", "already_decided", "failed"] as const)("the result status %s lands on the card", async (status) => {
    const { api, ctl } = rig();
    ctl.show(PAGE); ctl.setDraft("q"); await ctl.send();
    const turn = ctl.state.conv!.turns[0]!;
    api.decisionReplies.push({ proposal_id: "pr_fake_1", status, applied: status === "failed" ? [] : ["s2"], detail: "", action_id: "", can_undo: false });
    await ctl.decide(turn.id, "pr_fake_1", "reject");
    expect(turn.proposals[0]!.status).toBe(status);
    await ctl.decide(turn.id, "pr_fake_1", "reject");
    expect(api.calls.decide).toHaveLength(1);              // every result is final (the ledger decides once)
  });
  it("failed keeps the server detail on the card; other results keep it empty", async () => {
    const { api, ctl } = rig();
    ctl.show(PAGE); ctl.setDraft("q"); await ctl.send();
    const turn = ctl.state.conv!.turns[0]!;
    const card = turn.proposals[0]!;
    expect(card.detail).toBe("");
    api.decisionReplies.push({ proposal_id: "pr_fake_1", status: "failed", detail: "这一段已钉住，不能改写", applied: [], action_id: "", can_undo: false });
    await ctl.decide(turn.id, "pr_fake_1", "accept");
    expect(card).toMatchObject({ status: "failed", detail: "这一段已钉住，不能改写" });
    // failed is final: a second click sends nothing
    await ctl.decide(turn.id, "pr_fake_1", "reject");
    expect(card).toMatchObject({ status: "failed" });
    expect(api.calls.decide).toHaveLength(1);
  });
  it("an undo the chat ran calls pageEdited for the row's page: the open note follows", async () => {
    const edited: string[] = [];
    const { api, ctl } = rig({}, JERRY, (p) => { edited.push(p); });
    api.messageReplies.push({ events: [
      { type: "action", goal_title: null, replaces: "", page_id: "page_pca", action_id: "a1", kind: "assistant_edit", title: "改了一段", status: "undone", confirm: false, can_undo: false, items: [] },
      { type: "done", message_id: "m1", tool_calls: 1, llm_calls: 1 },
    ] as never });
    ctl.show(PAGE); ctl.setDraft("撤销"); await ctl.send();
    expect(edited).toEqual(["page_pca"]);
  });
  it("撤销 on an accepted card undoes its ledger row, the note follows; refused, the card says why and stays accepted", async () => {
    const edited: string[] = [];
    const { api, ctl } = rig({}, JERRY, (p) => { edited.push(p); });
    ctl.show(PAGE); ctl.setDraft("q"); await ctl.send();
    const turn = ctl.state.conv!.turns[0]!;
    await ctl.decide(turn.id, "pr_fake_1", "accept");
    const card = turn.proposals[0]!;
    expect(card).toMatchObject({ status: "accepted", actionId: "act_pr_fake_1", canUndo: true });
    api.actionReplies.push({ throws: new ApiError(409, "页面在这次修改之后又改过了，不能撤销，避免覆盖新的内容") });
    await ctl.undoProposal(turn.id, "pr_fake_1");
    expect(card).toMatchObject({ status: "accepted", canUndo: true, note: "没改成：页面在这次修改之后又改过了，不能撤销，避免覆盖新的内容。" });
    expect(edited).toEqual(["page_pca"]);
    await ctl.undoProposal(turn.id, "pr_fake_1");
    expect(card).toMatchObject({ status: "undone", canUndo: false, note: "" });
    expect(api.actionCalls.filter((c) => c.op === "undo").map((c) => c.id)).toEqual(["act_pr_fake_1", "act_pr_fake_1"]);
    expect(edited).toEqual(["page_pca", "page_pca"]);
  });
  it("a hand edit that could not go up first: nothing is sent, the card stays open and says so", async () => {
    const { api, ctl } = rig();
    (ctl as unknown as { deps: { handEditFirst: () => Promise<boolean> } }).deps.handEditFirst = () => Promise.resolve(false);
    ctl.show(PAGE); ctl.setDraft("q"); await ctl.send();
    const turn = ctl.state.conv!.turns[0]!;
    await ctl.decide(turn.id, "pr_fake_1", "accept");
    expect(api.calls.decide).toEqual([]);
    expect(turn.proposals[0]).toMatchObject({ status: "open", note: "这一页的手改没能同步，页面没有改动" });
  });
  it("accepted calls pageEdited once with the draft's page and the applied sections", async () => {
    const edited: [string, string[]][] = [];
    const { api, ctl } = rig({}, JERRY, (p, a) => { edited.push([p, a]); });
    ctl.show(PAGE); ctl.setDraft("q"); await ctl.send();
    const turn = ctl.state.conv!.turns[0]!;
    api.decisionReplies.push({ proposal_id: "pr_fake_1", status: "accepted", applied: ["s2", "s9"], detail: "", action_id: "", can_undo: false });
    await ctl.decide(turn.id, "pr_fake_1", "accept");
    expect(edited).toEqual([["page_pca", ["s2", "s9"]]]);
    await ctl.decide(turn.id, "pr_fake_1", "accept");
    expect(edited).toHaveLength(1);
  });
  it.each([
    ["rejected", { proposal_id: "pr_fake_1", status: "rejected", applied: [] as string[], detail: "", action_id: "", can_undo: false }],
    ["already_decided", { proposal_id: "pr_fake_1", status: "already_decided", applied: [] as string[], detail: "accepted", action_id: "", can_undo: false }],
    ["failed", { proposal_id: "pr_fake_1", status: "failed", applied: [] as string[], detail: "提议的正文是空的", action_id: "", can_undo: false }],
    ["error", { throws: new OfflineError() }],
  ] as const)("%s never calls pageEdited", async (_n, reply) => {
    const edited: unknown[] = [];
    const { api, ctl } = rig({}, JERRY, (...a) => { edited.push(a); });
    ctl.show(PAGE); ctl.setDraft("q"); await ctl.send();
    api.decisionReplies.push(reply);
    await ctl.decide(ctl.state.conv!.turns[0]!.id, "pr_fake_1", "accept");
    expect(edited).toEqual([]);
  });
  it("a throwing pageEdited hook does not break the card", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const { ctl } = rig({}, JERRY, () => { throw new Error("sync broke"); });
    ctl.show(PAGE); ctl.setDraft("q"); await ctl.send();
    const turn = ctl.state.conv!.turns[0]!;
    await ctl.decide(turn.id, "pr_fake_1", "accept");
    expect(turn.proposals[0]).toMatchObject({ status: "accepted", error: null });
    expect(warn).toHaveBeenCalledTimes(1);
    warn.mockRestore();
  });
  it("a failed decision re-enables the buttons for a new click, never resends by itself", async () => {
    const { api, ctl } = rig();
    ctl.show(PAGE); ctl.setDraft("q"); await ctl.send();
    const turn = ctl.state.conv!.turns[0]!;
    api.decisionReplies.push({ throws: new OfflineError() });
    await ctl.decide(turn.id, "pr_fake_1", "reject");
    expect(turn.proposals[0]).toMatchObject({ status: "error", error: { kind: "offline" } });
    await flush();
    expect(api.calls.decide).toHaveLength(1);
    await ctl.decide(turn.id, "pr_fake_1", "reject");
    expect(api.calls.decide).toHaveLength(2);
    expect(turn.proposals[0]).toMatchObject({ status: "rejected", error: null });
  });
  it("an unknown turn or proposal sends nothing", async () => {
    const { api, ctl } = rig();
    ctl.show(PAGE); ctl.setDraft("q"); await ctl.send();
    await ctl.decide("nope", "pr_fake_1", "accept");
    await ctl.decide(ctl.state.conv!.turns[0]!.id, "pr_nope", "accept");
    expect(api.calls.decide).toHaveLength(0);
  });
});

describe("10. new session", () => {
  it("clears the stored id and turns, keeps draft and quote, sends nothing", async () => {
    const { api, mem, ctl } = rig();
    ctl.show(NOTE); ctl.setDraft("q"); await ctl.send();
    ctl.setDraft("下一个"); ctl.attachQuote(NOTE, { text: "一段", heading: null });
    const before = structuredClone(api.calls);
    await ctl.newSession();
    expect(api.calls).toEqual(before);
    expect(mem).toEqual({});
    expect(ctl.state.conv).toMatchObject({ sessionId: null, turns: [], restore: "none", draft: "下一个", quote: { text: "一段", heading: null } });
  });
  /** sendMessage that delivers the text, then waits for `release()` before done (a turn still running). */
  function gated(api: FakeAssistantApi): () => void {
    let release!: () => void;
    const gate = new Promise<void>((r) => { release = r; });
    const real = api.sendMessage.bind(api);
    api.sendMessage = async (sid, body, onEvent) => {
      api.sendMessage = real;                                  // only the first message is held
      api.calls.sendMessage.push({ sessionId: sid, body: structuredClone(body) });
      onEvent({ type: "text_delta", text: "正在想", at: null });
      await gate;
      onEvent({ type: "done", message_id: "m_bg", tool_calls: 0, llm_calls: 1, cost_usd: null, attachments: [], session_attachments: [], stopped: false, confirmed: false });
    };
    return () => release();
  }
  it("while a turn runs: a new conversation opens at once (draft and quote go with it); the turn goes on in the background, its session is not the stored one", async () => {
    const { api, mem, ctl } = rig();
    const release = gated(api);
    ctl.show(NOTE); ctl.setDraft("q");
    const p = ctl.send();
    await flush();
    const running = ctl.state.conv!;
    expect(running.busy).toBe(true);
    ctl.setDraft("下一个");
    await ctl.newSession();
    const fresh = ctl.state.conv!;
    expect(fresh).not.toBe(running);
    expect(fresh).toMatchObject({ sessionId: null, turns: [], busy: false, draft: "下一个" });
    expect(mem).toEqual({});                                  // the key forgot the running session
    await ctl.send();                                         // the new conversation can ask right away
    expect(ctl.state.conv!.turns.map((t) => t.question)).toEqual(["下一个"]);
    release(); await p;
    expect(running.turns[0]).toMatchObject({ status: "done", answer: "正在想" });   // it landed in its own conversation
    expect(ctl.state.conv!.turns.map((t) => t.question)).toEqual(["下一个"]);
    expect(Object.values(mem)).toEqual(["ses_fake_2"]);       // the shown conversation's session is the stored one
  });
  it("every message names the open page, in any conversation (a new one, one read back from 历史对话)", async () => {
    // user report 2026-10-28: a free chat from 历史对话 sent no page, so 「这页没有组件」 could not be read
    const { api, ctl } = rig();
    ctl.show(NOTE); ctl.setDraft("先问一个"); await ctl.send();          // a concept note: no page open
    const old = ctl.state.conv!.sessionId!;
    ctl.show(PAGE); await ctl.newSession(); ctl.setDraft("这页没有组件"); await ctl.send();
    await ctl.resume(old); await flush(); ctl.setDraft("还是说这页"); await ctl.send();
    expect(api.calls.sendMessage.map((c) => c.body.page_id ?? null)).toEqual([null, "page_pca", "page_pca"]);
  });
  it("while a turn runs: its session from the history list takes the running conversation back, still streaming", async () => {
    const { api, ctl } = rig();
    const release = gated(api);
    ctl.show(NOTE); ctl.setDraft("q");
    const p = ctl.send();
    await flush();
    const running = ctl.state.conv!;
    await ctl.newSession();
    await ctl.resume(running.sessionId!);
    expect(ctl.state.conv).toBe(running);
    expect(ctl.state.conv!.busy).toBe(true);
    expect(api.calls.getSession).toHaveLength(0);             // no read: it is the live one
    release(); await p;
    expect(ctl.state.conv!.turns[0]!.status).toBe("done");
  });
});

describe("11. dispose", () => {
  it("aborts an in-flight stream; the turn ends aborted", async () => {
    const { api, ctl } = rig();
    api.sendMessage = (sessionId, body, onEvent, signal) => {
      api.calls.sendMessage.push({ sessionId, body });
      onEvent({ type: "text_delta", text: "半", at: null });
      return new Promise((_, reject) => signal?.addEventListener("abort", () => reject(new AbortedError())));
    };
    ctl.show(NOTE); ctl.setDraft("q");
    const p = ctl.send();
    await flush();
    expect(ctl.state.conv?.busy).toBe(true);
    ctl.dispose();
    await p;
    expect(ctl.state.conv!.turns[0]).toMatchObject({ status: "error", error: { kind: "aborted" }, answer: "半" });
    expect(ctl.state.conv?.busy).toBe(false);
  });
  it("after dispose nothing is sent", async () => {
    const { api, ctl } = rig();
    ctl.show(NOTE); ctl.setDraft("q");
    const p = ctl.send();   // createSession is already under way
    ctl.dispose();
    await p;
    expect(api.calls.sendMessage).toHaveLength(0);
    expect(ctl.state.conv!.turns[0]!.error).toEqual({ kind: "aborted" });
    ctl.setDraft("q2"); await ctl.send();
    expect(api.calls.createSession).toHaveLength(1);
  });
});

describe("12. store writes use the key captured when the operation started", () => {
  it("an identity change during createSession stores under the old key", async () => {
    const { api, mem, who, ctl } = rig();
    const create = api.createSession.bind(api);
    api.createSession = (body) => { who.id = { server: JERRY.server, user: "alice" }; return create(body); };
    ctl.show(NOTE); ctl.setDraft("q"); await ctl.send();
    expect(mem).toEqual({ [sessionKey(JERRY, NOTE)]: "ses_fake_1" });
  });
  it("an identity change mid-stream clears under the old key on session_gone", async () => {
    const { api, mem, who, ctl } = rig();
    ctl.show(NOTE); ctl.setDraft("q"); await ctl.send();
    const aliceKey = sessionKey({ server: JERRY.server, user: "alice" }, NOTE);
    mem[aliceKey] = "ses_fake_1";
    const send = api.sendMessage.bind(api);
    api.sendMessage = (sid, body, onEvent, signal) => { who.id = { server: JERRY.server, user: "alice" }; return send(sid, body, onEvent, signal); };
    api.messageReplies.push({ throws: new ApiError(404, "ended") });
    ctl.setDraft("q2"); await ctl.send();
    expect(mem).toEqual({ [aliceKey]: "ses_fake_1" });
  });
});

describe("recency: a stored session in use moves to most recent (local write only)", () => {
  const key = sessionKey(JERRY, NOTE);
  const other = sessionKey(JERRY, PAGE);
  it("restoring a stored session re-appends its key, with no extra API call", async () => {
    const { api, mem, writes, ctl } = rig({ [key]: "ses_old", [other]: "ses_page" });
    api.sessionOuts.set("ses_old", RESTORED);
    ctl.show(NOTE); await flush();
    expect(Object.keys(mem)).toEqual([other, key]);
    expect(writes).toEqual([[key, "ses_old"]]);
    expect(api.calls).toEqual({ ...EMPTY, getSession: ["ses_old"] });
  });
  it("a failed or gone restore does not refresh", async () => {
    const { api, writes, ctl } = rig({ [key]: "ses_old" });
    api.sessionOuts.set("ses_old", { throws: new OfflineError() });
    ctl.show(NOTE); await flush();
    expect(writes).toEqual([]);
  });
  it("sending on an existing session re-appends its key, with no extra API call", async () => {
    const { api, mem, writes, ctl } = rig();
    ctl.show(NOTE); ctl.setDraft("q"); await ctl.send();
    mem[other] = "ses_page";
    expect(Object.keys(mem)).toEqual([key, other]);
    ctl.setDraft("q2"); await ctl.send();
    expect(Object.keys(mem)).toEqual([other, key]);
    expect(mem[key]).toBe("ses_fake_1");
    expect(writes).toEqual([[key, "ses_fake_1"], [key, "ses_fake_1"]]);
    expect(api.calls.createSession).toHaveLength(1);
    expect(api.calls.sendMessage).toHaveLength(2);
    expect(api.calls.getSession).toHaveLength(0);
    expect(api.calls.decide).toHaveLength(0);
  });
  it("a session_gone send does not refresh; a replaced stored id is not overwritten", async () => {
    const { api, mem, writes, ctl } = rig();
    ctl.show(NOTE); ctl.setDraft("q"); await ctl.send();
    api.messageReplies.push({ throws: new ApiError(404, "ended") });
    ctl.setDraft("q2"); await ctl.send();
    expect(writes).toEqual([[key, "ses_fake_1"], [key, null]]);
    await ctl.retry(ctl.state.conv!.turns[1]!.id);
    mem[key] = "ses_other_window";
    ctl.setDraft("q3"); await ctl.send();
    expect(mem[key]).toBe("ses_other_window");
  });
  it("an identity change mid-stream refreshes under the old key", async () => {
    const { api, mem, who, ctl } = rig();
    ctl.show(NOTE); ctl.setDraft("q"); await ctl.send();
    mem[other] = "ses_page";
    const send = api.sendMessage.bind(api);
    api.sendMessage = (sid, body, onEvent, signal) => { who.id = { server: JERRY.server, user: "alice" }; return send(sid, body, onEvent, signal); };
    ctl.setDraft("q2"); await ctl.send();
    expect(Object.keys(mem)).toEqual([other, key]);
  });
});

describe("13. identity change", () => {
  it("re-shows the active note under the new identity: configured, a conversation, a notify", async () => {
    const { api, ctl, who } = rig({ [sessionKey(JERRY, NOTE)]: "ses_old" }, { server: "", user: "" });
    ctl.show(NOTE);
    expect(ctl.state.configured).toBe(false);
    let notified = 0; ctl.subscribe(() => { notified++; });
    who.id = JERRY;
    expect(ctl.state.conv).toBeNull(); // the old map has no entry under the new key yet
    api.sessionOuts.set("ses_old", RESTORED);
    ctl.identityChanged();
    expect(ctl.state.configured).toBe(true);
    expect(ctl.state.conv?.key).toBe(sessionKey(JERRY, NOTE));
    expect(notified).toBeGreaterThan(0);
    await flush();
    expect(api.calls.getSession).toEqual(["ses_old"]); // the one allowed restore read
    expect(ctl.state.conv?.turns).toHaveLength(2);
    ctl.setDraft("q"); await ctl.send();
    expect(api.calls.sendMessage).toHaveLength(1);
  });
  it("with no note active it only notifies (the conversation without a page, nothing read)", () => {
    const { api, ctl } = rig();
    let notified = 0; ctl.subscribe(() => { notified++; });
    ctl.identityChanged();
    expect(notified).toBe(1);
    expect(ctl.state.conv?.target).toEqual({ kind: "free" });
    expect(api.calls).toEqual(EMPTY);
  });
});

describe("14. a failing session store never freezes a conversation", () => {
  function failing(stored: Record<string, string> = {}) {
    const api = new FakeAssistantApi(); let n = 0;
    const store: AssistantSessionStore = {
      get: (k) => stored[k] ?? null,
      set: () => Promise.reject(new Error("disk full")),
    };
    const ctl = new AssistantController({ api, store, identity: () => JERRY, newId: () => `t${++n}` });
    return { api, ctl };
  }
  it("send completes and busy is reset when saving the new session id fails; the session is kept for the turn", async () => {
    const { api, ctl } = failing();
    ctl.show(NOTE); ctl.setDraft("q"); await ctl.send();
    expect(ctl.state.conv!.turns[0]!.status).toBe("done");
    expect(ctl.state.conv!.busy).toBe(false);
    expect(ctl.state.conv!.sessionId).toBe("ses_fake_1");
    expect(api.calls.sendMessage).toHaveLength(1);
  });
  it("send completes when touching a stored session fails", async () => {
    const { api, ctl } = failing({ [sessionKey(JERRY, NOTE)]: "ses_old" });
    api.sessionOuts.set("ses_old", RESTORED);
    ctl.show(NOTE); await flush();
    expect(ctl.state.conv!.restore).toBe("none");
    ctl.setDraft("q"); await ctl.send();
    expect(ctl.state.conv!.turns[ctl.state.conv!.turns.length - 1]!.status).toBe("done");
    expect(ctl.state.conv!.busy).toBe(false);
  });
  it("session gone on send still fails the turn and resets busy when forgetting fails", async () => {
    const { api, ctl } = failing({ [sessionKey(JERRY, NOTE)]: "ses_old" });
    api.sessionOuts.set("ses_old", RESTORED);
    ctl.show(NOTE); await flush();
    api.messageReplies.push({ throws: new ApiError(404, "ended") });
    ctl.setDraft("q"); await ctl.send();
    expect(ctl.state.conv!.turns[ctl.state.conv!.turns.length - 1]!.error).toEqual({ kind: "session_gone" });
    expect(ctl.state.conv!.busy).toBe(false);
  });
  it("a restore that finds the session gone ends \"gone\" even when forgetting fails", async () => {
    const { ctl } = failing({ [sessionKey(JERRY, NOTE)]: "ses_missing" });
    ctl.show(NOTE); await flush(); await flush();
    expect(ctl.state.conv!.restore).toBe("gone");
  });
  it("newSession clears the conversation even when the store write fails", async () => {
    const { api, ctl } = failing({ [sessionKey(JERRY, NOTE)]: "ses_old" });
    api.sessionOuts.set("ses_old", RESTORED);
    ctl.show(NOTE); await flush();
    expect(ctl.state.conv!.turns).toHaveLength(2);
    await ctl.newSession();
    expect(ctl.state.conv).toMatchObject({ sessionId: null, turns: [], restore: "none" });
  });
});

const ROW = (id: string, title: string, over: Partial<SessionSummaryOut> = {}): SessionSummaryOut => ({
  session_id: id, title, page_id: null, page_title: "pca（2/4）", note_concept_id: null,
  created_at: "2026-10-14T01:00:00Z", last_activity_at: "2026-10-14T02:00:00Z", message_count: 2, ...over });

describe("11. history (历史对话)", () => {
  const key = sessionKey(JERRY, NOTE);
  it("opening reads the list once (loading first), closing does not read", async () => {
    const { api, ctl } = rig();
    api.sessionList = [ROW("a", "特征值")];
    ctl.show(NOTE);
    ctl.toggleHistory();
    expect(ctl.state.history).toMatchObject({ open: true, status: "loading", items: [] });
    await flush();
    expect(ctl.state.history).toMatchObject({ open: true, status: "ready" });
    expect(ctl.state.history.items.map((r) => r.session_id)).toEqual(["a"]);
    ctl.toggleHistory();
    expect(ctl.state.history.open).toBe(false);
    expect(api.calls.listSessions).toEqual([{ q: "", limit: undefined }]);
  });
  it("a failed list is an error state with the reason, not an empty list; loadHistory retries", async () => {
    const { api, ctl } = rig();
    api.listReplies.push({ throws: new OfflineError() });
    ctl.show(NOTE); ctl.toggleHistory(); await flush();
    expect(ctl.state.history).toMatchObject({ status: "error", items: [], error: "未连接到服务器" });
    api.sessionList = [ROW("a", "t")];
    await ctl.loadHistory();
    expect(ctl.state.history).toMatchObject({ status: "ready", error: "" });
    expect(ctl.state.history.items).toHaveLength(1);
  });
  it("searching asks the server with the trimmed q; only the newest answer is applied", async () => {
    const { api, ctl } = rig();
    api.sessionList = [ROW("a", "特征值"), ROW("b", "协方差")];
    ctl.show(NOTE); ctl.toggleHistory(); await flush();
    ctl.searchHistory("协方"); ctl.searchHistory("协方差 ");
    await flush();
    expect(api.calls.listSessions.map((c) => c.q)).toEqual(["", "协方", "协方差"]);
    expect(ctl.state.history.items.map((r) => r.session_id)).toEqual(["b"]);
    expect(ctl.state.history.q).toBe("协方差 ");
  });
  it("resume loads the session into the current conversation: messages, decisions, store points at it", async () => {
    const { api, mem, ctl } = rig();
    ctl.show(NOTE);
    const draft = (h: string) => ({ kind: "add_section" as const, cue: "", answer: "", page_id: "page_pca", section_id: null, heading: h, cells: [], text: "正文", why: "w" });
    api.sessionOuts.set("ses_old", { ...RESTORED,
      proposals: [{ proposal_id: "pr_a", draft: draft("A"), change: null }, { proposal_id: "pr_b", draft: draft("B"), change: null }],
      decisions: [{ proposal_id: "pr_a", status: "accepted", applied: ["s2"], detail: "", action_id: "", can_undo: false }, { proposal_id: "pr_b", status: "failed", detail: "页面已变", applied: [], action_id: "", can_undo: false }] });
    api.sessionList = [ROW("ses_old", "为什么最大方差")];
    ctl.toggleHistory(); await flush();
    ctl.searchHistory("x");
    const tick = ctl.state.focusTick;
    await ctl.resume("ses_old");
    expect(ctl.state.focusTick).toBe(tick);   // the demo does not focus the composer on resume
    const c = ctl.state.conv!;
    expect(c).toMatchObject({ sessionId: "ses_old", recap: { understood: ["方差"], unclear: [], edits: [] }, restore: "none" });
    expect(c.turns.map((t) => t.question)).toEqual(["为什么最大方差", "还有吗"]);
    expect(c.turns[0]!.proposals.map((p) => [p.proposalId, p.draft.heading, p.status, p.applied, p.detail])).toEqual([
      ["pr_a", "A", "accepted", ["s2"], ""], ["pr_b", "B", "failed", [], "页面已变"]]);
    expect(ctl.state.history).toMatchObject({ open: false, q: "", notice: "" });
    expect(mem[key]).toBe("ses_old");
    // Later questions go to the resumed session, with no createSession.
    ctl.setDraft("接着问"); await ctl.send();
    expect(api.calls.createSession).toEqual([]);
    expect(api.calls.sendMessage[0]!.sessionId).toBe("ses_old");
  });
  it("an undecided restored proposal is an open card whose accept still posts the decision by id", async () => {
    const { api, ctl } = rig();
    ctl.show(NOTE);
    api.sessionOuts.set("ses_old", { ...RESTORED, proposals: [{ proposal_id: "pr_a", draft: { kind: "add_section", cue: "", answer: "", page_id: "page_pca", heading: "A", text: "t", why: "w", section_id: null, cells: [] }, change: null }] });
    await ctl.resume("ses_old");
    const t = ctl.state.conv!.turns[0]!;
    expect(t.proposals.map((p) => p.status)).toEqual(["open"]);
    await ctl.decide(t.id, "pr_a", "accept");
    expect(api.calls.decide).toEqual([{ proposalId: "pr_a", decision: "accept" }]);
    expect(t.proposals[0]!.status).toBe("accepted");
  });
  it("a failed resume keeps the current conversation and says why; so does a busy one", async () => {
    const { api, ctl } = rig();
    ctl.show(NOTE); ctl.setDraft("q"); await ctl.send();
    api.sessionOuts.set("bad", { throws: new ApiError(500, "boom") });
    ctl.toggleHistory(); await flush();
    await ctl.resume("bad");
    expect(ctl.state.conv).toMatchObject({ sessionId: "ses_fake_1" });
    expect(ctl.state.conv!.turns).toHaveLength(1);
    expect(ctl.state.history.open).toBe(true);
    expect(ctl.state.history.notice).toContain("这段对话没能打开");
    expect(ctl.state.history.notice).toContain("没能读到");
    await ctl.resume("missing");
    expect(ctl.state.history.notice).toContain("找不到");
  });
  it("resuming the current session just closes the list", async () => {
    const { api, ctl } = rig();
    ctl.show(NOTE); ctl.setDraft("q"); await ctl.send();
    ctl.toggleHistory(); await flush();
    await ctl.resume("ses_fake_1");
    expect(api.calls.getSession).toEqual([]);
    expect(ctl.state.history.open).toBe(false);
  });
  it("新会话 with questions starts a new conversation (store cleared); on an empty one it only closes the list and creates nothing", async () => {
    const { api, mem, ctl } = rig();
    ctl.show(NOTE);
    ctl.toggleHistory(); await flush();
    const tick = ctl.state.focusTick;
    await ctl.newSession();   // empty: closes the history view only
    expect(ctl.state.history.open).toBe(false);
    expect(ctl.state.focusTick).toBeGreaterThan(tick);
    expect(api.calls.createSession).toEqual([]);
    ctl.setDraft("q"); await ctl.send();
    expect(mem[key]).toBe("ses_fake_1");
    await ctl.newSession();
    expect(ctl.state.conv).toMatchObject({ sessionId: null, turns: [] });
    expect(mem[key]).toBeUndefined();
    await ctl.newSession();   // empty again: no further store writes, no requests
    expect(api.calls.createSession).toHaveLength(1);
  });
  it("askAssistant (attachQuote) stays in the conversation it finds and closes the list: only 新会话 starts another", async () => {
    const { api, mem, ctl } = rig();
    ctl.show(NOTE); ctl.setDraft("q"); await ctl.send();
    const sid = mem[key];
    ctl.toggleHistory(); await flush();
    ctl.attachQuote(NOTE, { text: "一段", heading: "推导" });
    expect(ctl.state.conv).toMatchObject({ sessionId: sid, quote: { text: "一段", heading: "推导" } });
    expect(ctl.state.conv!.turns).toHaveLength(1);
    expect(ctl.state.history.open).toBe(false);
    ctl.setDraft("这句什么意思"); await ctl.send();
    expect(api.calls.createSession).toHaveLength(1);
    expect(ctl.state.conv!.turns.map((t) => t.question)).toEqual(["q", "这句什么意思"]);
    expect(api.calls.sendMessage[1]!.body).toMatchObject({ text: "这句什么意思" });
  });
});

describe("15. sendText (a page chip)", () => {
  it("sends the text as a user turn, leaving the draft and the quote alone", async () => {
    const { api, ctl } = rig();
    ctl.show(PAGE); ctl.setDraft("草稿"); ctl.attachQuote(PAGE, { text: "一段", heading: null });
    expect(await ctl.sendText("特征值是什么？")).toBe(true);
    expect(api.calls.sendMessage[0]!.body).toEqual({ text: "特征值是什么？", page_id: "page_pca" });
    expect(ctl.state.conv).toMatchObject({ draft: "草稿", quote: { text: "一段", heading: null } });
    expect(ctl.state.conv!.turns.map((t) => t.question)).toEqual(["特征值是什么？"]);
  });
  it("ignored while busy, when blank, over-long or unconfigured", async () => {
    const { api, ctl } = rig();
    ctl.show(PAGE);
    expect(await ctl.sendText("  ")).toBe(false); expect(await ctl.sendText("字".repeat(4001))).toBe(false);
    expect(api.calls).toEqual(EMPTY);
    const p = ctl.sendText("第一个");
    expect(ctl.state.conv?.busy).toBe(true);
    expect(await ctl.sendText("第二个")).toBe(false); expect(await p).toBe(true);
    expect(api.calls.sendMessage).toHaveLength(1);
    const off = rig({}, { server: "", user: "" });
    off.ctl.show(PAGE); await off.ctl.sendText("q");
    expect(off.api.calls).toEqual(EMPTY);
  });
});

// ---------------------------------------------------------------- chat attachments (materials phase 2, T8)

/** A scripted AttachmentPort: uploads answer from `replies` (default: a reading source); follows stay open until fed. */
class FakePort implements AttachmentPort {
  uploads: { name: string; sessionId: string; bytes: number }[] = [];
  replies: (SourceOut | { throws: Error })[] = [];
  follows = new Map<string, { on: (p: AttachProgress) => void; end: () => void; fail: (e: Error) => void; signal: AbortSignal }>();
  private n = 0;
  upload(file: { name: string; data: ArrayBuffer }, sessionId: string): Promise<SourceOut> {
    this.uploads.push({ name: file.name, sessionId, bytes: file.data.byteLength });
    const r = this.replies.shift() ?? srcOut({ id: `src_${++this.n}`, title: file.name });
    return "throws" in r ? Promise.reject(r.throws) : Promise.resolve(r);
  }
  follow(id: string, on: (p: AttachProgress) => void, signal: AbortSignal): Promise<void> {
    return new Promise((end, fail) => {
      this.follows.set(id, { on, end: () => end(), fail, signal });
      signal.addEventListener("abort", () => fail(new AbortedError()));
    });
  }
}
const srcOut = (over: Partial<SourceOut> = {}): SourceOut => ({
  id: "src_1", title: "讲义.pdf", kind: "pdf", pages: 3, status: "reading", error: null, error_code: null, unreadable_pages: [],
  progress: { stage: "parse", done: 0, total: 3, message: "" }, note: null, origin: null, created_at: "2026-10-21T00:00:00Z",
  deleted: false, uses: [], can_retry: false, ...over,
});
const file = (name = "讲义.pdf", type = "application/pdf") => new File(["%PDF-1.4"], name, { type });

function attachRig(stored: Record<string, string> = {}, outs: SessionOut[] = []) {
  const r = rig(stored);
  for (const o of outs) r.api.sessionOuts.set(o.session_id, o);
  const port = new FakePort();
  let n = 0;
  const ctl = new AssistantController({
    api: r.api, store: { get: (k) => r.mem[k] ?? null, set: (k, v) => { delete r.mem[k]; if (v) r.mem[k] = v; return Promise.resolve(); } },
    identity: () => JERRY, newId: () => `a${++n}`, attachments: port,
  });
  ctl.show(PAGE);
  return { ...r, ctl, port, conv: () => ctl.state.conv! };
}

describe("12. chat attachments", () => {
  it("no session yet: one create, then the upload (use ask via the port) with that session; usable → send carries the id", async () => {
    const { ctl, api, port, conv } = attachRig();
    const adding = ctl.addFiles([file()]);
    expect(conv().attachments.map((a) => a.status)).toEqual(["uploading"]);
    await flush(); await flush();
    expect(api.calls.createSession).toEqual([{ page_id: "page_pca" }]);
    expect(port.uploads).toEqual([{ name: "讲义.pdf", sessionId: "ses_fake_1", bytes: 8 }]);
    expect(conv().attachments[0]).toMatchObject({ status: "reading", sourceId: "src_1" });
    // Reading: send is refused.
    ctl.setDraft("这份讲义讲什么"); await ctl.send();
    expect(api.calls.sendMessage).toEqual([]);
    const f = port.follows.get("src_1")!;
    f.on({ kind: "progress", done: 2, total: 3 });
    expect(conv().attachments[0]!.progress).toMatchObject({ done: 2, total: 3 });
    f.on({ kind: "usable", status: "ready" });
    await adding;
    expect(f.signal.aborted).toBe(true);                 // the stream is not followed past usable
    expect(conv().attachments[0]).toMatchObject({ status: "ready" });
    await ctl.send();
    expect(api.calls.createSession).toHaveLength(1);
    expect(api.calls.sendMessage).toEqual([{ sessionId: "ses_fake_1", body: { text: "这份讲义讲什么", attachments: ["src_1"], page_id: "page_pca" } }]);
    expect(conv().attachments).toEqual([]);
    expect(conv().turns[0]!.attachments.map((a) => [a.sourceId, a.title, a.progress])).toEqual([["src_1", "讲义.pdf", null]]);
  });

  it("text too long for one message (a long selection, a long paste) goes along as a whole text attachment", async () => {
    const { ctl, api, port, conv } = attachRig();
    port.replies = [srcOut({ id: "s1", title: "选中的文字.txt", kind: "text", status: "ready", progress: null }),
      srcOut({ id: "s2", title: "粘贴的文字.txt", kind: "text", status: "ready", progress: null })];
    const long = "字".repeat(5000);
    expect(await ctl.attachTextFile(PAGE, long, "理解有困难")).toBe(true);
    expect(port.uploads[0]).toMatchObject({ name: "选中的文字.txt", bytes: new TextEncoder().encode(long).length });
    expect(conv()).toMatchObject({ quote: null, draft: "理解有困难" });
    await ctl.addPastedText("贴" + long);
    expect(port.uploads[1]!.name).toBe("粘贴的文字.txt");
    await ctl.send();
    expect(api.calls.sendMessage[0]!.body).toEqual({ text: "理解有困难", attachments: ["s1", "s2"], page_id: "page_pca" });
  });

  it("a dropped web link goes into the draft after what is typed", () => {
    const { ctl, conv } = attachRig();
    ctl.appendToDraft("https://example.com/a");
    expect(conv().draft).toBe("https://example.com/a");
    ctl.setDraft("看看这个 "); ctl.appendToDraft("https://example.com/b\n");
    expect(conv().draft).toBe("看看这个 https://example.com/b");
  });

  it("two files added together share one session create", async () => {
    const { ctl, api, port } = attachRig();
    port.replies = [srcOut({ id: "s1", status: "ready", progress: null }), srcOut({ id: "s2", status: "partial", progress: null })];
    await ctl.addFiles([file("a.pdf"), file("b.pdf")]);
    expect(api.calls.createSession).toHaveLength(1);
    expect(port.uploads.map((u) => u.sessionId)).toEqual(["ses_fake_1", "ses_fake_1"]);
  });

  it("attachments alone can be sent (empty text); a stored session is reused without a create", async () => {
    const { ctl, api, port } = attachRig({ [sessionKey(JERRY, PAGE)]: "ses_old" },
      [{ gone_sources: [], session_id: "ses_old", page_id: "page_pca", messages: [], recap: null, decisions: [], proposals: [], actions: [] }]);
    await flush();
    port.replies = [srcOut({ status: "ready", progress: null })];
    await ctl.addFiles([file()]);
    expect(port.uploads[0]!.sessionId).toBe("ses_old");
    await ctl.send();
    expect(api.calls.createSession).toEqual([]);
    expect(api.calls.sendMessage).toEqual([{ sessionId: "ses_old", body: { text: "", attachments: ["src_1"], page_id: "page_pca" } }]);
  });

  it("a failed reading shows the reason; the words go without it and its card leaves the composer", async () => {
    const { ctl, api, port, conv } = attachRig();
    const adding = ctl.addFiles([file()]);
    await flush(); await flush();
    port.follows.get("src_1")!.on({ kind: "error", detail: "读不出这份 PDF" });
    port.follows.get("src_1")!.end();
    await adding;
    expect(conv().attachments[0]).toMatchObject({ status: "failed", error: "读不出这份 PDF" });
    ctl.setDraft("问"); await ctl.send();
    expect(api.calls.sendMessage.map((c) => c.body)).toEqual([{ text: "问", page_id: "page_pca" }]);
    expect(conv().attachments).toEqual([]);
  });

  it("an upload refused by the server fails the card with the server's Chinese reason; offline says so", async () => {
    const { ctl, port, conv } = attachRig();
    port.replies = [{ throws: new ApiError(415, "不支持这种文件", "unsupported_file_type") }, { throws: new OfflineError() }, { throws: new ApiError(500, "boom") }];
    await ctl.addFiles([file("a.exe"), file("b.pdf"), file("c.pdf")]);
    expect(conv().attachments.map((a) => [a.status, a.error])).toEqual([
      ["failed", "不支持这种文件"], ["failed", "未连接到服务器"], ["failed", "没传上去。"]]);
  });

  it("a stream that ends while the source is still reading fails the card (never left spinning)", async () => {
    const { ctl, port, conv } = attachRig();
    const adding = ctl.addFiles([file()]);
    await flush(); await flush();
    port.follows.get("src_1")!.end();
    await adding;
    expect(conv().attachments[0]).toMatchObject({ status: "failed", error: "附件没读完，去掉它再试。" });
  });

  it("removing a card mid-reading stops its follow; the card does not come back", async () => {
    const { ctl, port, conv } = attachRig();
    const adding = ctl.addFiles([file()]);
    await flush(); await flush();
    const f = port.follows.get("src_1")!;
    ctl.removeAttachment(conv().attachments[0]!.localId);
    await adding;
    expect(f.signal.aborted).toBe(true);
    expect(conv().attachments).toEqual([]);
  });

  it("more than MAX_ATTACHMENTS files (the backend's runaway guard): the extra ones are not added and a notice says so", async () => {
    const { clearEwNotices, ewNotices } = await import("../../test/ew-notices");
    clearEwNotices();
    const { ctl, port, conv } = attachRig();
    const n = MAX_ATTACHMENTS + 1;
    port.replies = Array.from({ length: n }, (_, i) => srcOut({ id: `s${i}`, status: "ready", progress: null }));
    await ctl.addFiles(Array.from({ length: n }, (_, i) => file(`f${i}.pdf`)));
    expect(conv().attachments).toHaveLength(MAX_ATTACHMENTS);
    expect(ewNotices()).toEqual([`一次最多带 ${MAX_ATTACHMENTS} 个附件，多的没有添加。`]);
    clearEwNotices();
  });

  it("a use the chat added (done's uses go beyond ask): every card of that source follows the import (正在解析 + bar)", async () => {
    const { ctl, api, port, conv } = attachRig();
    port.replies = [srcOut({ status: "ready", progress: null })];
    await ctl.addFiles([file()]);
    const A = { source_id: "src_1", title: "讲义.pdf", kind: "pdf", status: "ready", deleted: false, uses: ["ask"], bytes: 8, pages: 3,
      expires_at: "2026-11-20T00:00:00Z", unreadable_pages: [], chars: null, importing: false,
      read: { mode: "full" as const, pages_total: null, pages_read: null, sections: null, tokens: null, unseen_pages: [] } };
    const done = (uses: string[], importing = uses.length > 1) => ({ events: [{ type: "text_delta" as const, text: "好", at: null },
      { type: "done" as const, message_id: "m1", tool_calls: 0, llm_calls: 1, cost_usd: null, attachments: [{ ...A, uses, importing }],
        session_attachments: [], stopped: false, confirmed: false }] });
    api.messageReplies = [done(["ask"])];
    await ctl.send();                                      // only asked: nothing follows
    expect(port.follows.has("src_1")).toBe(false);
    port.replies = [srcOut({ status: "ready", progress: null })];
    await ctl.addFiles([file()]);
    ctl.setDraft("这是往年题");
    api.messageReplies = [done(["ask", "exam"])];
    await ctl.send();
    const cards = () => conv().turns.map((t) => t.attachments[0]!);
    expect(cards().map((a) => [a.uses, a.progress])).toEqual([
      [["ask", "exam"], { done: 0, total: 0 }], [["ask", "exam"], { done: 0, total: 0 }]]);
    const f = port.follows.get("src_1")!;
    f.on({ kind: "progress", done: 1, total: 4 });
    expect(cards().every((a) => a.progress?.done === 1)).toBe(true);
    f.on({ kind: "done", status: "ready" });
    f.end();
    await flush();
    expect(cards().every((a) => a.progress === null)).toBe(true);
    // a later done that still shows the use starts nothing again
    port.follows.clear();
    port.replies = [srcOut({ status: "ready", progress: null })];
    await ctl.addFiles([file()]);
    api.messageReplies = [done(["ask", "exam"])];
    await ctl.send();
    expect(port.follows.has("src_1")).toBe(false);
  });

  it("an already imported material that gets a use: the cards take the uses (no expiry) but show no bar", async () => {
    const { ctl, api, port, conv } = attachRig();
    port.replies = [srcOut({ status: "ready", progress: null })];
    await ctl.addFiles([file()]);
    api.messageReplies = [{ events: [{ type: "done", stopped: false, confirmed: false, message_id: "m1", tool_calls: 0, llm_calls: 1, attachments: [{
      source_id: "src_1", title: "讲义.pdf", kind: "pdf", status: "ready", deleted: false, uses: ["ask", "learn"], bytes: 8,
      pages: 3, expires_at: null, read: { mode: "full", pages_total: null, pages_read: null, sections: null, tokens: null, unseen_pages: [] }, importing: false, unreadable_pages: [], chars: null }], cost_usd: null, session_attachments: [] }] }];
    ctl.setDraft("先收进资料"); await ctl.send();
    expect(port.follows.has("src_1")).toBe(false);
    expect(conv().turns[0]!.attachments[0]).toMatchObject({ uses: ["ask", "learn"], expiresAt: null, progress: null });
  });

  it("「刚才那份是往年题」: done's session_attachments update the earlier cards and follow their import", async () => {
    const { ctl, api, port, conv } = attachRig();
    port.replies = [srcOut({ status: "ready", progress: null })];
    await ctl.addFiles([file()]);
    const A = { source_id: "src_1", title: "讲义.pdf", kind: "pdf", status: "ready", deleted: false, uses: ["ask"], bytes: 8, pages: 3,
      expires_at: "2026-11-20T00:00:00Z", unreadable_pages: [], chars: null, importing: false,
      read: { mode: "full" as const, pages_total: null, pages_read: null, sections: null, tokens: null, unseen_pages: [] } };
    api.messageReplies = [
      { events: [{ type: "done", message_id: "m1", tool_calls: 0, llm_calls: 1, attachments: [A], cost_usd: null, session_attachments: [], stopped: false, confirmed: false }] },
      { events: [{ type: "done", stopped: false, confirmed: false, message_id: "m2", tool_calls: 1, llm_calls: 2, cost_usd: null, attachments: [],
        session_attachments: [{ ...A, uses: ["ask", "exam"], expires_at: null, read: null, importing: true }] }] }];
    await ctl.send();
    ctl.setDraft("刚才那份是往年题"); await ctl.send();
    expect(conv().turns[0]!.attachments[0]).toMatchObject({ uses: ["ask", "exam"], expiresAt: null, progress: { done: 0, total: 0 } });
    expect(conv().turns[1]!.attachments).toEqual([]);
    port.follows.get("src_1")!.on({ kind: "done", status: "ready" });
    port.follows.get("src_1")!.end();
    await flush();
    expect(conv().turns[0]!.attachments[0]!.progress).toBeNull();
  });

  it("the import's error shows on the card", async () => {
    const { ctl, api, port, conv } = attachRig();
    port.replies = [srcOut({ status: "ready", progress: null })];
    await ctl.addFiles([file()]);
    api.messageReplies = [{ events: [{ type: "done", stopped: false, confirmed: false, message_id: "m1", tool_calls: 0, llm_calls: 1, attachments: [{
      source_id: "src_1", title: "讲义.pdf", kind: "pdf", status: "ready", deleted: false, uses: ["ask", "learn"], bytes: 8,
      pages: 3, expires_at: null, read: { mode: "full", pages_total: null, pages_read: null, sections: null, tokens: null, unseen_pages: [] }, importing: true, unreadable_pages: [], chars: null }], cost_usd: null, session_attachments: [] }] }];
    ctl.setDraft("先收进资料"); await ctl.send();
    const f = port.follows.get("src_1")!;
    f.on({ kind: "error", detail: "资料没能开始整理" });
    f.end();
    await flush();
    expect(conv().turns[0]!.attachments[0]).toMatchObject({ progress: null, error: "资料没能开始整理" });
  });

  it("done's attachments replace the sent cards; a restored user message has its attachments", async () => {
    const { ctl, api, port, conv } = attachRig();
    port.replies = [srcOut({ status: "ready", progress: null })];
    await ctl.addFiles([file()]);
    const A = { source_id: "src_1", title: "讲义.pdf", kind: "pdf", status: "ready", deleted: false, uses: ["ask"], bytes: 8, pages: 3,
      expires_at: "2026-11-20T00:00:00Z", unreadable_pages: [], chars: null, importing: false,
      read: { mode: "full" as const, pages_total: null, pages_read: null, sections: null, tokens: null, unseen_pages: [] } };
    api.messageReplies = [{ events: [{ type: "text_delta", text: "好", at: null },
      { type: "done", message_id: "m1", tool_calls: 0, llm_calls: 1, attachments: [A], cost_usd: null, session_attachments: [], stopped: false, confirmed: false }] }];
    await ctl.send();
    expect(conv().turns[0]!.attachments[0]).toMatchObject({ expiresAt: "2026-11-20T00:00:00Z", pages: 3 });

    const b = attachRig({ [sessionKey(JERRY, PAGE)]: "ses_r" }, [{ gone_sources: [], session_id: "ses_r", page_id: "page_pca", recap: null, decisions: [], proposals: [],
      actions: [], messages: [
      { message_id: "u1", role: "user", card_at: null, stopped: false, intent: null, draft_id: null, asks: [], text: "", attachments: [{ ...A, deleted: true, expires_at: null }], sources: [], proposals: [], actions: [], selection: null, tools: [], rating: null },
      { message_id: "a1", role: "assistant", card_at: null, stopped: false, intent: null, draft_id: null, asks: [], text: "这是讲义", sources: [], proposals: [], actions: [], selection: null, tools: [], attachments: [], rating: null }] }]);
    await flush(); await flush();
    expect(b.conv().turns[0]!.attachments).toEqual([expect.objectContaining({ sourceId: "src_1", deleted: true, progress: null })]);
  });

  it("retry resends the same attachments", async () => {
    const { ctl, api, port } = attachRig();
    port.replies = [srcOut({ status: "ready", progress: null })];
    await ctl.addFiles([file()]);
    api.messageReplies = [{ throws: new OfflineError() }];
    await ctl.send();
    const t = ctl.state.conv!.turns[0]!;
    await ctl.retry(t.id);
    expect(api.calls.sendMessage.map((c) => c.body)).toEqual([{ text: "", attachments: ["src_1"], page_id: "page_pca" }, { text: "", attachments: ["src_1"], page_id: "page_pca" }]);
  });

  it("a reading stream that breaks (offline) says the attachment was not read through, not that it did not upload", async () => {
    const { ctl, port, conv } = attachRig();
    const adding = ctl.addFiles([file()]);
    await flush(); await flush();
    const f = port.follows.get("src_1")!;
    // The port's promise rejects as a dropped connection would.
    f.fail(new OfflineError());
    await adding;
    expect(conv().attachments[0]).toMatchObject({ status: "failed", error: "附件没读完，去掉它再试。" });
  });

  it("removing a card while it is still uploading: the upload's answer is dropped, nothing is followed", async () => {
    const { ctl, port, conv } = attachRig();
    let answer: (o: SourceOut) => void = () => undefined;
    port.upload = (fl, sid) => { port.uploads.push({ name: fl.name, sessionId: sid, bytes: fl.data.byteLength });
      return new Promise((r) => { answer = r; }); };
    const adding = ctl.addFiles([file()]);
    await flush(); await flush();
    expect(port.uploads).toHaveLength(1);
    expect(conv().attachments[0]!.status).toBe("uploading");
    ctl.removeAttachment(conv().attachments[0]!.localId);
    expect(conv().attachments).toEqual([]);
    answer(srcOut());
    await adding;
    expect(conv().attachments).toEqual([]);
    expect(port.follows.size).toBe(0);
  });

  it("the same file twice (a 200 dedupe: same source id) keeps one card", async () => {
    const { ctl, api, port, conv } = attachRig();
    port.replies = [srcOut({ status: "ready", progress: null }), srcOut({ status: "ready", progress: null })];
    await ctl.addFiles([file()]);
    await ctl.addFiles([file()]);
    expect(conv().attachments.map((a) => a.sourceId)).toEqual(["src_1"]);
    await ctl.send();
    expect(api.calls.sendMessage[0]!.body).toEqual({ text: "", attachments: ["src_1"], page_id: "page_pca" });
  });

  it("a switched server / user drops an upload in flight: no upload goes out under the new identity", async () => {
    const r = rig();
    const port = new FakePort();
    const who = { id: JERRY };
    const ctl = new AssistantController({ api: r.api, store: { get: () => null, set: () => Promise.resolve() },
      identity: () => who.id, newId: () => "x", attachments: port });
    ctl.show(PAGE);
    const adding = ctl.addFiles([file()]);
    who.id = { server: JERRY.server, user: "alice" };
    await adding;
    expect(port.uploads).toEqual([]);
  });

  it("409 on a send with attachments: the server's reason, not retryable; text, quote and attachments back in the composer", async () => {
    const { ctl, api, port, conv } = attachRig();
    port.replies = [srcOut({ status: "ready", progress: null }), srcOut({ id: "src_2", title: "图.png", kind: "image", status: "partial", progress: null })];
    await ctl.addFiles([file(), file("图.png", "image/png")]);
    api.messageReplies = [{ throws: new ApiError(409, "附件已经删除了，去掉它再发", "attachment_refused") }];
    ctl.setDraft("这两份讲什么"); await ctl.send();
    const t = conv().turns[0]!;
    expect(t.error).toEqual({ kind: "attachment", detail: "附件已经删除了，去掉它再发" });
    expect(conv().draft).toBe("这两份讲什么");
    expect(conv().attachments.map((a) => [a.sourceId, a.status])).toEqual([["src_1", "ready"], ["src_2", "partial"]]);
    await ctl.retry(t.id);
    expect(api.calls.sendMessage).toHaveLength(1);              // not retryable: no loop
    // The user takes the bad one out and sends again.
    ctl.removeAttachment(conv().attachments[0]!.localId);
    await ctl.send();
    expect(api.calls.sendMessage[1]!.body).toEqual({ text: "这两份讲什么", attachments: ["src_2"], page_id: "page_pca" });
  });

  it("a 409 busy (code session_busy, Chinese detail) with attachments stays busy and retryable", async () => {
    const { ctl, api, port, conv } = attachRig();
    port.replies = [srcOut({ status: "ready", progress: null })];
    await ctl.addFiles([file()]);
    api.messageReplies = [{ throws: new ApiError(409, "上一轮还没结束", "session_busy") }];
    await ctl.send();
    expect(conv().turns[0]!.error).toEqual({ kind: "busy" });
    expect(conv().attachments).toEqual([]);
  });

  it("without a port the composer takes no files", async () => {
    const { ctl } = rig();
    ctl.show(PAGE);
    await ctl.addFiles([file()]);
    expect(ctl.state.conv!.attachments).toEqual([]);
  });
});

describe("action cards in a stored session (replay with the live ledger status)", () => {
  const STORED: SessionOut = {
    gone_sources: [], session_id: "ses_cards", page_id: "page_pca",
    messages: [
      { message_id: "m1", role: "user", card_at: null, stopped: false, intent: null, draft_id: null, asks: [], text: "拆一下", sources: [], proposals: [], actions: [], selection: null, tools: [], attachments: [], rating: null },
      { message_id: "m2", role: "assistant", card_at: null, stopped: false, intent: null, draft_id: null, asks: [], text: "拆好了草稿", actions: ["a1"], sources: [], proposals: [], selection: null, tools: [], attachments: [], rating: null },
      { message_id: "m3", role: "user", card_at: null, stopped: false, intent: null, draft_id: null, asks: [], text: "考我", sources: [], proposals: [], actions: [], selection: null, tools: [], attachments: [], rating: null },
      { message_id: "m4", role: "assistant", card_at: null, stopped: false, intent: null, draft_id: null, asks: [], text: "三道题", sources: [], proposals: [], actions: [], selection: null, tools: [], attachments: [], rating: null },
      { message_id: "m5", role: "user", card_at: null, stopped: false, intent: null, draft_id: null, asks: [], text: "撤销刚才那个", sources: [], proposals: [], actions: [], selection: null, tools: [], attachments: [], rating: null },
      { message_id: "m6", role: "assistant", card_at: null, stopped: false, intent: null, draft_id: null, asks: [], text: "撤销了", actions: ["a1"], sources: [], proposals: [], selection: null, tools: [], attachments: [], rating: null },
    ],
    actions: [{ action_id: "a1", goal_title: null, kind: "page_split", title: "拆成 2 页", status: "executed", confirm: true, can_undo: true, detail: "", replaces: "", result: {}, page_id: null, section_id: null,
      items: [{ key: "k1", title: "甲", page_id: "pg1", covers: null, n: null, level: null }, { key: "k2", title: "乙", page_id: "pg2", covers: null, n: null, level: null }] }], recap: null, decisions: [], proposals: [],
  };
  const restored = async () => {
    const r = rig({ [sessionKey(JERRY, PAGE)]: "ses_cards" });
    r.api.sessionOuts.set("ses_cards", STORED);
    r.ctl.show(PAGE); await flush();
    return r;
  };

  it("each message gets its cards; an id re-listed by a later (undo) message is the same card, shown once, with the ledger's status", async () => {
    const { ctl } = await restored();
    const turns = ctl.state.conv!.turns;
    expect(turns.map((t) => t.actions.length)).toEqual([1, 0, 0]);
    const card = turns[0]!.actions[0]!;
    expect([card.actionId, card.status, card.canUndo, card.items.map((i) => i.page_id)]).toEqual(["a1", "executed", true, ["pg1", "pg2"]]);
    ctl.dispose();
  });

  it("the pages of a restored done card get their writing status (one read)", async () => {
    const { api, ctl } = await restored();
    await flush();
    expect(api.actionCalls.filter((c) => c.op === "writes")).toEqual([{ op: "writes", ids: ["pg1", "pg2"] }]);
    ctl.dispose();
  });

  it("the wiki watcher's fresh read is used when it has every card page; a stale one or a missing page is asked for", async () => {
    const run = async (snap: { at: number; writes: WriteOut[] } | null) => {
      const api = new FakeAssistantApi();
      api.sessionOuts.set("ses_cards", STORED);
      const ctl = new AssistantController({ api, store: { get: () => "ses_cards", set: () => Promise.resolve() }, identity: () => JERRY,
        newId: () => "t", sharedWrites: () => snap });
      ctl.show(PAGE); await flush(); await flush();
      const asked = api.actionCalls.filter((c) => c.op === "writes").length;
      ctl.dispose();
      return { asked };
    };
    const w = (id: string): WriteOut => ({ page_id: id, run_id: "r1", kind: "organize", state: "writing", pass: null, section: null, of: null, started_at: null, cost_usd: 0 });
    expect((await run({ at: Date.now(), writes: [w("pg1"), w("pg2"), w("other")] })).asked).toBe(0);
    expect((await run({ at: Date.now() - 60_000, writes: [w("pg1"), w("pg2")] })).asked).toBe(1);
    expect((await run({ at: Date.now(), writes: [w("pg1")] })).asked).toBe(1);
  });

  it("an undo turn's re-listed card updates the earlier card in place (no second card)", async () => {
    const { api, ctl } = await restored();
    ctl.setDraft("再来一次");
    api.messageReplies.push({ events: [
      { type: "action", goal_title: null, replaces: "", page_id: null, action_id: "a1", kind: "page_split", title: "拆成 2 页", status: "undone", confirm: true, can_undo: false, items: [] },
      { type: "done", message_id: "m7", tool_calls: 1, llm_calls: 1, cost_usd: null, attachments: [], session_attachments: [], stopped: false, confirmed: false }] });
    await ctl.send();
    const turns = ctl.state.conv!.turns;
    expect(turns.flatMap((t) => t.actions).map((a) => [a.actionId, a.status])).toEqual([["a1", "undone"]]);
    expect(turns[turns.length - 1]!.actions).toHaveLength(0);
    ctl.dispose();
  });

  it("an unknown card id in a message is skipped, not guessed", async () => {
    const r = rig({ [sessionKey(JERRY, PAGE)]: "ses_cards" });
    r.api.sessionOuts.set("ses_cards", { ...STORED, actions: [] });
    r.ctl.show(PAGE); await flush();
    expect(r.ctl.state.conv!.turns.flatMap((t) => t.actions)).toEqual([]);
    r.ctl.dispose();
  });

  it("confirm / cancel / undo are ignored in the wrong state and while one is in flight", async () => {
    const { api, ctl } = await restored();
    await ctl.confirmAction("a1");                 // already done
    await ctl.cancelAction("a1");
    expect(api.actionCalls.filter((c) => ["approve", "reject"].includes(c.op))).toEqual([]);
    const first = ctl.undoAction("a1"); const second = ctl.undoAction("a1");
    await Promise.all([first, second]);
    expect(api.actionCalls.filter((c) => c.op === "undo")).toHaveLength(1);
    await ctl.undoAction("a1");                    // now undone
    expect(api.actionCalls.filter((c) => c.op === "undo")).toHaveLength(1);
    ctl.dispose();
  });

  it("the ledger hook runs after a decision", async () => {
    const api = new FakeAssistantApi();
    api.sessionOuts.set("ses_cards", STORED);
    const hook = vi.fn();
    const ctl = new AssistantController({ api, store: { get: () => "ses_cards", set: () => Promise.resolve() }, identity: () => JERRY, newId: () => "t", ledgerChanged: hook });
    ctl.show(PAGE); await flush();
    await ctl.undoAction("a1");
    expect(hook).toHaveBeenCalledTimes(1);
    ctl.dispose();
  });

  it("the ledger hook runs when a turn's tool writes a ledger row (参数 lists the same row, 等你确认)", async () => {
    const api = new FakeAssistantApi();
    const hook = vi.fn();
    const ctl = new AssistantController({ api, store: { get: () => null, set: () => Promise.resolve() }, identity: () => JERRY, newId: () => "t", ledgerChanged: hook });
    ctl.show(PAGE); await flush();
    api.messageReplies.push({ events: [
      { type: "text_delta", text: "拆成两页。", at: null },
      { type: "action", goal_title: null, replaces: "", page_id: null, action_id: "a9", kind: "page_split", title: "拆成 2 页", status: "proposed", confirm: true, can_undo: false,
        items: [{ key: "k1", title: "一", page_id: null, covers: null, n: null, level: null }, { key: "k2", title: "二", page_id: null, covers: null, n: null, level: null }] },
      // the same row re-sent with the same status: no second read
      { type: "action", goal_title: null, replaces: "", page_id: null, action_id: "a9", kind: "page_split", title: "拆成 2 页", status: "proposed", confirm: true, can_undo: false,
        items: [{ key: "k1", title: "一", page_id: null, covers: null, n: null, level: null }, { key: "k2", title: "二", page_id: null, covers: null, n: null, level: null }] },
      { type: "done", message_id: "m9", tool_calls: 1, llm_calls: 1, cost_usd: null, attachments: [], session_attachments: [], stopped: false, confirmed: false }] });
    ctl.setDraft("这一页太笼统了");
    await ctl.send();
    expect(hook).toHaveBeenCalledTimes(1);
    ctl.dispose();
  });
});

describe("the conversation without a page made before any show (a plugin reload)", () => {
  it("its stored session is read on the first show, and its history row is not a no-op", async () => {
    const key = sessionKey(JERRY, { kind: "free" });
    for (const via of ["show", "history"] as const) {
      const { api, ctl } = rig({ [key]: "ses_old" });
      api.sessionOuts.set("ses_old", RESTORED);
      expect(ctl.state.conv?.sessionId).toBe("ses_old");          // the composer's conversation: made, not read
      expect(api.calls.getSession).toEqual([]);
      if (via === "show") ctl.show({ kind: "none" }); else await ctl.resume("ses_old");
      await flush(); await flush();
      expect(api.calls.getSession).toEqual(["ses_old"]);
      expect(ctl.state.conv!.turns.map((t) => t.question)).toEqual(["为什么最大方差", "还有吗"]);
    }
  });
});
