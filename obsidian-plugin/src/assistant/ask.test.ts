// ask_user: the assistant asks one question and the turn ENDS on it (done follows; nothing waits, nothing times out). His
// next message answers it: typed in the composer (the normal send, the composer is not busy) or one of the options the
// model gave (sent as his message, through the same send). The server resumes the paused turn with it, or treats it as a
// new request; either way the question is closed. A reload shows the pending card from the session, still answerable.
import { describe, expect, it, vi } from "vitest";
import { cardActions } from "../../test/card-rig";
import { AssistantController, type AssistantState, type Conversation, type HistoryState } from "./controller";
import { FakeAssistantApi } from "./fake-api";
import { AssistantRenderer, type AssistantActions } from "./render";
import type { AssistantSessionStore, Identity, Target } from "./session-store";
import { applyEvent, newTurn, waitingAsk } from "./turn";
import type { AskEvent, AssistantEvent, SessionOut } from "./types";

const NOTE: Target = { kind: "note", path: "ML/PCA.md", conceptId: "pca", title: "PCA" };
const JERRY: Identity = { server: "http://127.0.0.1:8000", user: "jerry" };
const ASK: AskEvent = { type: "ask", call_id: "c1", kind: "ask_user", payload: { question: "你学过线性代数吗？", options: ["学过", "没学过"] } };
const done = (id: string): AssistantEvent =>
  ({ type: "done", message_id: id, tool_calls: 1, llm_calls: 1, cost_usd: null, attachments: [], session_attachments: [], stopped: false, confirmed: false });
/** The turn that asks: a line, the question, done (the server's turn ends on it). */
const ASKING = { events: [{ type: "text_delta", text: "先问一句。", at: 0 } as AssistantEvent, ASK, done("m_a")] };
const GOES_ON = (text: string) => ({ events: [{ type: "text_delta", text, at: 0 } as AssistantEvent, done("m_b")] });

function rig(api: FakeAssistantApi) {
  const store: AssistantSessionStore = { get: () => null, set: () => Promise.resolve() };
  let n = 0;
  return new AssistantController({ api, store, identity: () => JERRY, newId: () => `t${++n}` });
}

async function asked(api: FakeAssistantApi) {
  api.messageReplies.push(ASKING);
  const ctl = rig(api);
  ctl.show(NOTE); ctl.setDraft("讲讲 PCA");
  await ctl.send();
  return { ctl, c: ctl.state.conv! };
}

describe("ask_user in the controller", () => {
  it("the turn ends on the question: the card waits, the composer is free, nothing else is sent", async () => {
    const api = new FakeAssistantApi();
    const { c } = await asked(api);
    expect(c.busy).toBe(false);
    const t = c.turns[0]!;
    expect(t.status).toBe("done");
    expect(t.thinking).toBe(false);
    expect(waitingAsk(c.turns)).toEqual({ callId: "c1", kind: "ask_user", draftId: null, question: "你学过线性代数吗？", options: ["学过", "没学过"], status: "waiting", block: null, rough: null });
    expect(t.askAt).toBe(5);
    expect(api.calls.sendMessage).toHaveLength(1);
  });

  it("no timeout: however long he takes, nothing happens and the card stays answerable", async () => {
    vi.useFakeTimers();
    try {
      const api = new FakeAssistantApi();
      const { c } = await asked(api);
      await vi.advanceTimersByTimeAsync(60 * 60 * 1000);
      expect(api.calls.sendMessage).toHaveLength(1);
      expect(waitingAsk(c.turns)?.status).toBe("waiting");
      expect(c.turns).toHaveLength(1);
    } finally { vi.useRealTimers(); }
  });

  it("what he types next is his message, sent the normal way; it closes the question", async () => {
    const api = new FakeAssistantApi();
    const { ctl, c } = await asked(api);
    api.messageReplies.push(GOES_ON("那就从向量讲起。"));
    ctl.setDraft("只学过一点矩阵");
    await ctl.send();
    expect(api.calls.sendMessage.map((s) => s.body.text)).toEqual(["讲讲 PCA", "只学过一点矩阵"]);
    expect(api.calls.sendMessage[1]!.sessionId).toBe(api.calls.sendMessage[0]!.sessionId);
    expect(c.turns.map((t) => t.question)).toEqual(["讲讲 PCA", "只学过一点矩阵"]);
    expect(c.turns[0]!.asks[0]!.status).toBe("answered");
    expect(waitingAsk(c.turns)).toBeNull();
    expect(c.turns[1]!.answer).toBe("那就从向量讲起。");
    expect(c.draft).toBe("");
  });

  it("an option is sent as his message through the same send (the composer's draft stays)", async () => {
    const api = new FakeAssistantApi();
    const { ctl, c } = await asked(api);
    ctl.setDraft("还没写完的话");
    await ctl.answerAsk("c9", "学过");                       // not the waiting question: nothing
    expect(api.calls.sendMessage).toHaveLength(1);
    api.messageReplies.push(GOES_ON("好。"));
    await ctl.answerAsk("c1", "没学过");
    expect(api.calls.sendMessage.map((s) => s.body)).toEqual([
      { text: "讲讲 PCA" }, { text: "没学过" }]);
    expect(c.turns[1]!.question).toBe("没学过");
    expect(c.turns[0]!.asks[0]!.status).toBe("answered");
    expect(c.draft).toBe("还没写完的话");
    await ctl.answerAsk("c1", "学过");                       // answered already: nothing
    expect(api.calls.sendMessage).toHaveLength(2);
  });

  it("a reload shows the pending question from the session, still answerable", async () => {
    const api = new FakeAssistantApi();
    const out: SessionOut = { gone_sources: [], session_id: "ses_old", page_id: null, recap: null, decisions: [], proposals: [], actions: [], messages: [
      { message_id: "m1", role: "user", card_at: null, stopped: false, intent: null, draft_id: null, asks: [], text: "讲讲", sources: [], proposals: [], actions: [], selection: null, tools: [], attachments: [], rating: null },
      { message_id: "m2", role: "assistant", card_at: null, stopped: false, intent: null, draft_id: null, text: "好。", sources: [], proposals: [], actions: [], selection: null, tools: [], attachments: [], rating: null,
        asks: [{ call_id: "c1", kind: "ask_user", payload: { question: "学过吗？", options: ["学过", "没有"] }, status: "waiting" }] }] };
    api.sessionOuts.set("ses_old", out);
    const ctl = rig(api);
    ctl.show(NOTE);
    await ctl.resume("ses_old");
    const c = ctl.state.conv!;
    expect(waitingAsk(c.turns)).toEqual({ callId: "c1", kind: "ask_user", draftId: null, question: "学过吗？", options: ["学过", "没有"], status: "waiting", block: null, rough: null });
    expect(c.turns[0]!.askAt).toBe("好。".length);                // the turn ended with it: under the answer, as live
    api.messageReplies.push(GOES_ON("好。"));
    await ctl.answerAsk("c1", "没有");
    expect(api.calls.sendMessage.map((s) => [s.sessionId, s.body.text])).toEqual([["ses_old", "没有"]]);
    expect(c.turns[0]!.asks[0]!.status).toBe("answered");
  });

  it("a restored answered question is no longer answerable", async () => {
    const api = new FakeAssistantApi();
    api.sessionOuts.set("ses_old", { gone_sources: [], session_id: "ses_old", page_id: null, recap: null, decisions: [], proposals: [], actions: [], messages: [
      { message_id: "m1", role: "user", card_at: null, stopped: false, intent: null, draft_id: null, asks: [], text: "讲讲", sources: [], proposals: [], actions: [], selection: null, tools: [], attachments: [], rating: null },
      { message_id: "m2", role: "assistant", card_at: null, stopped: false, intent: null, draft_id: null, text: "好。", sources: [], proposals: [], actions: [], selection: null, tools: [], attachments: [], rating: null,
        asks: [{ call_id: "c1", kind: "ask_user", payload: { question: "学过吗？", options: [] }, status: "answered" }] },
      { message_id: "m3", role: "user", card_at: null, stopped: false, intent: null, draft_id: null, asks: [], text: "没有", sources: [], proposals: [], actions: [], selection: null, tools: [], attachments: [], rating: null },
      { message_id: "m4", role: "assistant", card_at: null, stopped: false, intent: null, draft_id: null, asks: [], text: "那从头讲。", sources: [], proposals: [], actions: [], selection: null, tools: [], attachments: [], rating: null }] });
    const ctl = rig(api);
    ctl.show(NOTE);
    await ctl.resume("ses_old");
    const turns = ctl.state.conv!.turns;
    expect(turns.map((t) => t.question)).toEqual(["讲讲", "没有"]);
    expect(turns[0]!.asks[0]!.status).toBe("answered");
    expect(waitingAsk(turns)).toBeNull();
  });
});

const actions = (): AssistantActions & Record<string, ReturnType<typeof vi.fn>> => ({
  send: vi.fn(), sendEdit: vi.fn(), setDraft: vi.fn(), removeQuote: vi.fn(), retry: vi.fn(), decide: vi.fn(), newSession: vi.fn(), reload: vi.fn(),
  toggleHistory: vi.fn(), searchHistory: vi.fn(), reloadHistory: vi.fn(), resume: vi.fn(), openSource: vi.fn(),
  addFiles: vi.fn(), dropLibrary: vi.fn(), removeAttachment: vi.fn(), answerAsk: vi.fn(), ...cardActions() }) as never;
const hooks = { renderMarkdown: vi.fn(), conceptName: (id: string) => id };
const HIST0: HistoryState = { open: false, q: "", status: "loading", items: [], error: "", notice: "", deleting: [] };
const conv = (over: Partial<Conversation> = {}): Conversation => ({ key: "k", target: NOTE,
  sessionId: "s", turns: [], draft: "", quote: null, busy: false, attachments: [], restore: "none", recap: null, ...over });
const st = (c: Conversation): AssistantState =>
  ({ configured: true, active: NOTE, conv: c, focusTick: 0, history: HIST0, writes: new Map(), canAttach: true });

describe("ask_user in the panel", () => {
  it("the question is the assistant's line, options are reply pills, the composer answers it like any message", () => {
    const t = newTurn("t1", "讲讲", null);
    applyEvent(t, { type: "text_delta", text: "先问一句。", at: 0 });
    applyEvent(t, { ...ASK, payload: { question: "想先看哪种？", options: ["例子", "推导"] } });
    applyEvent(t, done("m_a"));
    const a = actions();
    const r = new AssistantRenderer(a, hooks);
    r.update(st(conv({ turns: [t], draft: "" })));
    const msg = r.root.querySelector(".ew-amsg")!;
    expect([...msg.querySelectorAll("p")].map((p) => p.textContent)).toEqual(["先问一句。", "想先看哪种？"]);
    // the turn ended on it: its 👍 / 👎 (no 复制) sit at the end of the question's own line, no row of their own
    const askRow = msg.querySelector(".ew-arow.ew-ask")!;
    expect([...askRow.children].map((c) => c.className)).toEqual(["ew-askq"]);
    expect([...askRow.firstElementChild!.children].map((c) => c.tagName + "." + c.className)).toEqual(["P.", "DIV.ew-rbtns"]);
    expect([...askRow.querySelectorAll(".ew-ib")].map((b) => b.getAttribute("aria-label"))).toEqual(["评价这个问题", "评价这个问题"]);
    expect(askRow.nextElementSibling!.className).toBe("ew-sugg");
    const pills = [...msg.querySelectorAll(".ew-sugg button")] as HTMLButtonElement[];
    expect(pills.map((b) => b.textContent)).toEqual(["例子", "推导"]);
    expect(msg.querySelectorAll(".ew-arow")).toHaveLength(1);         // no answer row under it (no 复制)
    pills[1]!.click();
    expect(a.answerAsk).toHaveBeenCalledWith("c1", "推导");
    const ta = r.root.querySelector("textarea") as HTMLTextAreaElement;
    expect(ta.placeholder).toBe("回答上面的问题…");
    expect((r.root.querySelector(".ew-cbar .ew-send") as HTMLButtonElement).disabled).toBe(true);   // the normal rule: no text
    r.update(st(conv({ turns: [t], draft: "例子吧" })));
    expect((r.root.querySelector(".ew-cbar .ew-send") as HTMLButtonElement).disabled).toBe(false);
    // answered (his next message went out): no pills, no answer inside the card (it is his own next bubble), no notice
    t.asks[0]!.status = "answered"; t.rev++;
    r.update(st(conv({ turns: [t], draft: "" })));
    expect(r.root.querySelectorAll(".ew-amsg .ew-sugg")).toHaveLength(0);
    expect(r.root.querySelectorAll(".ew-amsg .ew-umsg")).toHaveLength(0);
    expect(r.root.querySelectorAll(".ew-amsg .ew-muted")).toHaveLength(0);
    expect(r.root.querySelectorAll(".ew-amsg .ew-arow")).toHaveLength(1);     // once answered, still on the question's line
    expect(r.root.querySelector(".ew-amsg .ew-arow.ew-ask .ew-askq > p")!.textContent).toBe("想先看哪种？");
    expect(ta.placeholder).toBe("这里我不太理解……");
  });
});
