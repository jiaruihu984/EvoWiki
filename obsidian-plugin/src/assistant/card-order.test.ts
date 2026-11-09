import { describe, expect, it, vi } from "vitest";
import { cardActions } from "../../test/card-rig";
import { type AssistantActions, AssistantRenderer } from "./render";
import { applyEvent, newTurn, resetForRetry, type Turn } from "./turn";
import type { AssistantState, Conversation, HistoryState } from "./controller";
import type { AssistantEvent } from "./types";

/**
 * asst.js assistantView: an answer's paragraphs, its action card, then what the reply says after the card (`m.tail`) — the
 * first reply to 学点新的 (framing + caveat, the blocks card, main line / start points / the one question), in stream order.
 */
const actions = (): AssistantActions => ({
  send: vi.fn(), sendEdit: vi.fn(), setDraft: vi.fn(), removeQuote: vi.fn(), retry: vi.fn(), decide: vi.fn(), undoProposal: vi.fn(), newSession: vi.fn(), reload: vi.fn(),
  toggleHistory: vi.fn(), searchHistory: vi.fn(), reloadHistory: vi.fn(), resume: vi.fn(), openSource: vi.fn(),
  addFiles: vi.fn(), dropLibrary: vi.fn(), removeAttachment: vi.fn(), ...cardActions() });
const hooks = { renderMarkdown: vi.fn((el: HTMLElement, md: string) => { el.textContent = `MD:${md}`; }), conceptName: (id: string) => id };
const conv = (turns: Turn[]): Conversation => ({ key: "k", target: { kind: "free" }, sessionId: "s", turns, draft: "", quote: null, busy: false,
  attachments: [], restore: "none", recap: null }) as never;
const HIST0: HistoryState = { open: false, q: "", status: "loading", items: [], error: "", notice: "", deleting: [] };
const st = (c: Conversation): AssistantState =>
  ({ configured: true, active: { kind: "none" }, conv: c, focusTick: 0, history: HIST0, writes: new Map(), canAttach: true }) as never;

const HEAD = "这份大纲不是一份养虾教程。\n\n先提醒一句：「黑壳虾」是俗称。";
const TAIL = "\n\n主线是 分类与鉴别 → 水质与环境。\n\n你是想自己把虾养好，还是把它当成一种生物来研究？";
const ACTION: AssistantEvent = { type: "action", goal_title: "黑壳虾", replaces: "", page_id: null, action_id: "a1", kind: "goal_new", title: "「黑壳虾」分成 2 块", status: "proposed", confirm: true, can_undo: false,
  items: [{ key: "id", title: "分类与鉴别", page_id: null, covers: "俗称指哪种虾", n: null, level: "基础" },
    { key: "wq", title: "水质与环境", page_id: null, covers: "水温", n: null, level: "基础" }] };
const DONE: AssistantEvent = { type: "done", message_id: "m1", tool_calls: 1, llm_calls: 2, cost_usd: null, attachments: [], session_attachments: [], stopped: false, confirmed: false };
const tool = (id: string): AssistantEvent[] => [{ type: "tool_start", call_id: id, name: "new_goal", effect: "propose", summary: "", label: "" }, { type: "tool_end", call_id: id, ok: true, sources: [], label: "", group: "", group_label: "", items: [], refs: [] }];

/** The .ew-amsg's children as short names: answer text (its first line), the section label, the card, the rest by class. */
function shape(r: AssistantRenderer): string[] {
  const a = r.root.querySelector(".ew-amsg")!;
  return [...a.children].map((c) => c.classList.contains("ew-answer") ? `answer:${(c.textContent ?? "").replace(/^MD:/, "").trim().split("\n")[0]}`
    : c.className.split(" ")[0]!.replace(/^ew-/, ""));
}

describe("Turn.cardAt: where the cards sit in the answer", () => {
  it("text → card → text: the answer's length when the first card came", () => {
    const t = newTurn("t", "我要学黑壳虾", null);
    applyEvent(t, { type: "text_delta", text: HEAD, at: null });
    for (const e of tool("c1")) applyEvent(t, e);
    applyEvent(t, ACTION);
    applyEvent(t, { type: "text_delta", text: TAIL, at: HEAD.length });
    expect(t.cardAt).toBe(HEAD.length);
    applyEvent(t, { ...ACTION, status: "rejected" });   // the same card again moves nothing
    expect(t.cardAt).toBe(HEAD.length);
    resetForRetry(t);
    expect(t.cardAt).toBeNull();
  });
  it("a card before any text (or with whitespace only) keeps the old layout: every text above the cards", () => {
    const t = newTurn("t", "q", null);
    applyEvent(t, { type: "text_delta", text: "  ", at: null });
    applyEvent(t, ACTION);
    applyEvent(t, { type: "text_delta", text: "拆好了。", at: null });
    expect(t.cardAt).toBeNull();
  });
});

describe("the thread draws text → card → text in stream order (asst.js m.paras / actCard / m.tail)", () => {
  const stream = (events: AssistantEvent[]): Turn => { const t = newTurn("t", "我要学黑壳虾", null); for (const e of events) applyEvent(t, e); return t; };

  it("live: the paragraphs before the card above it, the rest under it; each part is its own Markdown", () => {
    const t = stream([{ type: "text_delta", text: HEAD, at: null }, ...tool("c1"), ACTION, { type: "text_delta", text: TAIL, at: HEAD.length }, DONE]);
    const r = new AssistantRenderer(actions(), hooks); r.update(st(conv([t])));
    expect(shape(r)).toEqual(["answer:这份大纲不是一份养虾教程。", "asec", "propcard", "answer:主线是 分类与鉴别 → 水质与环境。", "arow"]);
    expect(hooks.renderMarkdown).toHaveBeenCalledWith(expect.anything(), HEAD);
    expect(hooks.renderMarkdown).toHaveBeenCalledWith(expect.anything(), TAIL);
  });

  it("while it streams: plain paragraphs until the first Markdown is ready, and the waiting line sits under the cards (where the next text comes)", () => {
    const t = stream([{ type: "text_delta", text: HEAD, at: null }, { type: "tool_start", call_id: "c1", name: "new_goal", effect: "propose", summary: "", label: "" }, ACTION]);
    const r = new AssistantRenderer(actions(), { ...hooks, renderMarkdown: vi.fn() }); r.update(st(conv([t])));
    expect(shape(r)).toEqual(["answer:这份大纲不是一份养虾教程。先提醒一句：「黑壳虾」是俗称。", "asec", "propcard", "tools"]);
    expect([...r.root.querySelectorAll(".ew-amsg > .ew-answer p")].map((p) => p.textContent)).toEqual(["这份大纲不是一份养虾教程。", "先提醒一句：「黑壳虾」是俗称。"]);
  });

  it("a card with no text before it: the whole answer above the card, as before", () => {
    const t = stream([...tool("c1"), ACTION, { type: "text_delta", text: "拆成了 2 块。", at: null }, DONE]);
    const r = new AssistantRenderer(actions(), hooks); r.update(st(conv([t])));
    expect(shape(r)).toEqual(["answer:拆成了 2 块。", "asec", "propcard", "arow"]);
  });

  it("a replayed turn with cardAt set draws the same order", () => {
    const t = newTurn("t", "我要学黑壳虾", null);
    Object.assign(t, { status: "done", answer: HEAD + TAIL, answerId: "m1", cardAt: HEAD.length });
    t.actions = stream([ACTION]).actions;
    const r = new AssistantRenderer(actions(), hooks); r.update(st(conv([t])));
    expect(shape(r)).toEqual(["answer:这份大纲不是一份养虾教程。", "asec", "propcard", "answer:主线是 分类与鉴别 → 水质与环境。", "arow"]);
  });

  it("the draft rows show each block's level as span.tg after the title (asst.js draftList)", () => {
    const t = stream([{ type: "text_delta", text: HEAD, at: null }, ACTION, DONE]);
    const r = new AssistantRenderer(actions(), hooks); r.update(st(conv([t])));
    const rows = [...r.root.querySelectorAll(".ew-propcard ol.ew-alist.ew-drag > li > div")];
    expect(rows.map((d) => [...d.children].map((c) => `${c.className}:${c.textContent}`))).toEqual([
      ["ew-t:分类与鉴别", "ew-tg:基础", "ew-s:俗称指哪种虾"], ["ew-t:水质与环境", "ew-tg:基础", "ew-s:水温"]]);
  });
});
