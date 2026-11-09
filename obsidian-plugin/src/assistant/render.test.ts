import { LEDGER_STALE } from "./card-state";
import { describe, expect, it, vi } from "vitest";
import { cardActions, cardRig } from "../../test/card-rig";
import { FAKE_ANSWER } from "./fake-api";
import { type AssistantActions, AssistantRenderer } from "./render";
import { fromOut, type DraftAttachment, type TurnAttachment } from "./attachments";
import { applyEvent, failTurn, newTurn, type ProposalCard, type ToolLine, type Turn } from "./turn";
import type { AssistantState, Conversation, HistoryState } from "./controller";

const actions = (): AssistantActions & Record<string, ReturnType<typeof vi.fn>> => ({
  send: vi.fn(), sendEdit: vi.fn(), setDraft: vi.fn(), removeQuote: vi.fn(), retry: vi.fn(), decide: vi.fn(), undoProposal: vi.fn(), newSession: vi.fn(), reload: vi.fn(),
  toggleHistory: vi.fn(), searchHistory: vi.fn(), reloadHistory: vi.fn(), resume: vi.fn(), openSource: vi.fn(),
  addFiles: vi.fn(), dropLibrary: vi.fn(), removeAttachment: vi.fn(), ...cardActions() }) as never;
const hooks = { renderMarkdown: vi.fn((el: HTMLElement, md: string) => { el.textContent = `MD:${md}`; }), conceptName: (id: string) => (id === "pca" ? "主成分分析" : id) };
const conv = (over: Partial<Conversation> = {}): Conversation => ({ key: "k", target: { kind: "page", pageId: "page_pca", title: "主成分分析" },
  sessionId: "s", turns: [], draft: "", quote: null, busy: false, attachments: [], restore: "none", recap: null, ...over });
const HIST0: HistoryState = { open: false, q: "", status: "loading", items: [], error: "", notice: "", deleting: [] };
const st = (c: Conversation | null, over: Partial<AssistantState> = {}): AssistantState =>
  ({ configured: true, active: c && c.target.kind !== "free" ? c.target : { kind: "none" }, conv: c, focusTick: 0, history: HIST0, writes: new Map(), canAttach: true, ...over });
const q = (r: AssistantRenderer, sel: string) => r.root.querySelector(sel);
const all = (r: AssistantRenderer, sel: string) => [...r.root.querySelectorAll(sel)];
const sendBtn = (r: AssistantRenderer) => q(r, ".ew-cbar .ew-send") as HTMLButtonElement;
const finished = (quote: { text: string; heading: string | null } | null = null) => {
  const t = newTurn("t1", "为什么", quote); for (const e of FAKE_ANSWER) applyEvent(t, e); return t;
};

describe("「帮我复习」's card", () => {
  const reviewTurn = (id: string, ok: boolean): Turn => {
    const t = newTurn(id, "复习", null);
    t.status = "done";
    t.tools = [{ callId: "c", name: "open_review", effect: "read", summary: "", status: ok ? "ok" : "failed", sources: [], label: "", done: "", group: "", groupLabel: "", items: [], refs: [] }];
    return t;
  };
  const draw = (live: boolean, turns: Turn[]) => {
    const a = { ...actions(), reviewCard: () => { const d = document.createElement("div"); d.className = "rv"; return d; }, reviewLive: () => live };
    const r = new AssistantRenderer(a, hooks);
    r.update(st(conv({ turns })));
    return all(r, ".ew-thread .rv").map((e) => e.closest(".ew-amsg")!);
  };
  it("one card: under the newest turn while the round runs, under the turn that opened it afterwards", () => {
    const later = finished();
    later.id = "later";
    const sources = (live: boolean): boolean => {
      const shown = draw(live, [reviewTurn("open", true), later]);
      expect(shown).toHaveLength(1);
      return shown[0]!.textContent.includes("MD:");   // the later turn has an answer (hooks.renderMarkdown); the opener here has none
    };
    expect(sources(true)).toBe(true);
    expect(sources(false)).toBe(false);
  });
  it("no open_review that worked (nothing due): no card", () => {
    expect(draw(true, [reviewTurn("open", false)])).toHaveLength(0);
    expect(draw(false, [finished()])).toHaveLength(0);
  });
});

describe("the demo's DOM (app.js assistantView)", () => {
  it("root .ew-asst > .ew-ahead + .ew-thread + .ew-composer > .ew-cbox > textarea + .ew-cbar(span, 问)", () => {
    const r = new AssistantRenderer(actions(), hooks); r.update(st(conv()));
    expect(r.root.className).toBe("ew-asst");
    expect([...r.root.children].map((c) => c.className)).toEqual(["ew-ahead", "ew-thread", "ew-composer"]);
    const box = q(r, ".ew-composer > .ew-cbox")!;
    expect([...box.children].map((c) => c.tagName === "TEXTAREA" ? "textarea" : c.className).filter((c) => c !== "ew-muted"))
      .toEqual(["ew-atts", "textarea", "ew-cbar"]);
    const ta = q(r, "textarea") as HTMLTextAreaElement;
    expect(ta.rows).toBe(1); expect(ta.placeholder).toBe("这里我不太理解……");
    const bar = q(r, ".ew-cbar")!;
    const [gap, send] = [...bar.children] as HTMLButtonElement[];
    expect(gap!.tagName).toBe("SPAN"); expect(gap!.textContent).toBe(""); expect(bar.querySelector(".ew-ghost")).toBeNull();
    expect(send!.className).toBe("ew-send"); expect(send!.title).toBe(""); expect(send!.getAttribute("aria-label")).toBe("问");
    expect(send!.querySelector("svg path")!.getAttribute("d")).toBe("M12 19V5M5.5 11.5L12 5l6.5 6.5");
    const other = new AssistantRenderer(actions(), hooks);
    expect((q(other, "textarea") as HTMLTextAreaElement).id).not.toBe(ta.id);
  });
  it("no title row, no empty-state hint, no hint line, no counter, no starters: an empty conversation's thread is empty", () => {
    const r = new AssistantRenderer(actions(), hooks); r.update(st(conv()));
    expect(q(r, ".ew-thread")!.children).toHaveLength(0);
    expect(q(r, "label")).toBeNull();
    expect(q(r, ".ew-starters")).toBeNull();
    expect(q(r, ".ew-atitle")!.textContent).toBe("新会话");
  });
  it("a citation in an answer opens its source at its page; found gone it shows as deleted; before the Markdown no raw mark shows", async () => {
    const t = newTurn("t1", "为什么", null);
    t.status = "done"; t.answer = "要除。〔attention p.4｜4｜src_a：「divide each」〕";
    const openCitation = vi.fn(() => Promise.resolve<"gone">("gone"));
    const r = new AssistantRenderer({ ...actions(), openCitation }, {
      ...hooks, renderMarkdown: (el: HTMLElement, md: string) => { el.replaceChildren(...new DOMParser().parseFromString(md, "text/html").body.childNodes); },
    });
    r.update(st(conv({ turns: [t] })));
    const a = q(r, ".ew-answer a.ew-cite") as HTMLElement;
    expect(a.textContent).toBe("attention p.4");
    a.click();
    expect(openCitation).toHaveBeenCalledWith("src_a", 4);
    await Promise.resolve(); await Promise.resolve();
    expect(q(r, ".ew-answer a.ew-cite")).toBeNull();
    expect(q(r, ".ew-answer .ew-gone")!.textContent).toBe("attention p.4（已删除）");

    const placeholder = new AssistantRenderer(actions(), { ...hooks, renderMarkdown: () => undefined });
    placeholder.update(st(conv({ turns: [t] })));
    expect(q(placeholder, ".ew-answer")!.textContent).toBe("要除。（attention p.4）");
  });
  it("a user turn: .ew-umsg > .ew-uquote (the chip's label) + .ew-ubub (the question)", () => {
    const r = new AssistantRenderer(actions(), hooks);
    r.update(st(conv({ turns: [finished({ text: "协方差矩阵\n第二行", heading: "推导" })] })));
    const u = q(r, ".ew-thread > .ew-umsg")!;
    expect([...u.children].map((c) => c.className)).toEqual(["ew-uquote", "ew-ubub"]);
    expect(u.children[0]!.textContent).toBe("引用「协方差矩阵 第二行」");
    expect(u.children[1]!.textContent).toBe("为什么");
    const wrong = "题目：某题\n我选的：A\n正确答案：B";
    r.update(st(conv({ turns: [newTurn("w", "这里我不太理解。", { text: wrong, heading: null, kind: "wrong_answer" })], busy: true })));
    expect(q(r, ".ew-uquote")!.textContent).toBe(wrong);
    // A quote replayed from a stored session has no flag: a heading-less 题目： quote is still a question; others are labels.
    r.update(st(conv({ turns: [newTurn("o", "q", { kind: "wrong_answer", text: wrong, heading: null }), newTurn("p", "q", { text: "一段", heading: null })], busy: true })));
    expect(all(r, ".ew-uquote").map((e) => e.textContent)).toEqual([wrong, "引用「一段」"]);
    r.update(st(conv({ turns: [newTurn("t2", "不带引用", null)], busy: true })));
    expect([...q(r, ".ew-umsg")!.children].map((c) => c.className)).toEqual(["ew-ubub"]);
  });
  it("a finished answer: .ew-amsg > the tool rows, the Markdown, the cited sources, 内容建议 label, .ew-propcard, the rating row .ew-arow (no waiting line, no mastery line)", () => {
    hooks.renderMarkdown.mockClear();
    const r = new AssistantRenderer(actions(), hooks); r.update(st(conv({ turns: [finished()] })));
    const a = q(r, ".ew-thread > .ew-amsg")!;
    expect([...a.children].map((c) => c.className)).toEqual(["ew-trows", "ew-answer", "ew-srcl", "ew-asec", "ew-propcard", "ew-arow"]);
    expect([...a.querySelectorAll(".ew-trow")].map((c) => c.textContent)).toEqual(["联网搜了「PCA 为什么要最大化方差」 · 1 条", "读了《主成分分析》第 2 节"]);
    expect(a.querySelector(".ew-asec")!.textContent).toBe("内容建议");
    expect(a.querySelector(".ew-tools")).toBeNull();
    expect(a.querySelector(".ew-answer")!.textContent).toMatch(/^MD:方差大的方向/);
    expect(hooks.renderMarkdown).toHaveBeenCalledTimes(1);
    // only what it read is cited (the web search's hits are behind its row)
    const links = [...a.querySelectorAll(".ew-srcl > button.ew-srclink")] as HTMLButtonElement[];
    expect(links.map((c) => c.textContent)).toEqual(["主成分分析"]);
    expect(links.every((c) => c.type === "button")).toBe(true);
    expect(a.textContent).not.toContain("看起来你对");
  });
  it("a cited source opens it; one with neither a page nor an http url is inert; each once", () => {
    const t = newTurn("t", "q", null);
    applyEvent(t, { type: "tool_start", call_id: "c", name: "read_page", effect: "read", summary: "方差", label: "正在读《主成分分析》" });
    applyEvent(t, { type: "tool_end", call_id: "c", ok: true, sources: [], label: "读了《主成分分析》", group: "read_page", group_label: "读了 {k} 页", items: ["主成分分析"], refs: [
      { title: "主成分分析", page_id: "page_pca", url: null, segment_id: null }, { title: "一段笔记", segment_id: "seg_1", url: null, page_id: null }, { title: "坏链接", url: "javascript:alert(1)", page_id: null, segment_id: null },
      { title: "主成分分析", page_id: "page_pca", url: null, segment_id: null }] });
    applyEvent(t, { type: "done", message_id: "m", tool_calls: 1, llm_calls: 1, cost_usd: null, attachments: [], session_attachments: [], stopped: false, confirmed: false });
    const act = actions(); const r = new AssistantRenderer(act, hooks); r.update(st(conv({ turns: [t] })));
    expect(q(r, ".ew-tools")).toBeNull();
    const chips = all(r, ".ew-srclink") as HTMLButtonElement[];
    expect(chips.map((c) => [c.textContent, c.disabled])).toEqual([["主成分分析", false], ["一段笔记", true], ["坏链接", true]]);
    chips[0]!.click();
    expect(act.openSource).toHaveBeenCalledWith({ title: "主成分分析", page_id: "page_pca", url: null, segment_id: null });
    expect(q(r, "a")).toBeNull();
  });
  it("server strings are text nodes: markup in a question, quote or streamed text is never parsed", () => {
    const t = newTurn("t", "<b>x</b>", { text: "<img src=x>", heading: "<i>h</i>", kind: "wrong_answer" });
    applyEvent(t, { type: "text_delta", text: "<script>1</script>", at: null });
    const r = new AssistantRenderer(actions(), hooks); r.update(st(conv({ turns: [t], busy: true, quote: { text: "q", heading: "<u>" } })));
    expect(q(r, ".ew-thread b, .ew-thread img, .ew-thread script, .ew-qchip u")).toBeNull();
    expect(q(r, ".ew-ubub")!.textContent).toBe("<b>x</b>");
    expect(q(r, ".ew-qchip span")!.textContent).toBe("引用「q」");
  });
});

describe("streaming", () => {
  it("waiting: .ew-amsg > .ew-tools with a spinner and 正在思考… until the first row", () => {
    const t = newTurn("t1", "q", null);
    const r = new AssistantRenderer(actions(), hooks); r.update(st(conv({ turns: [t], busy: true })));
    const a = q(r, ".ew-amsg")!;
    expect([...a.children].map((c) => c.className)).toEqual(["ew-tools"]);
    expect(q(r, ".ew-tools")!.textContent).toBe("正在思考…");
    expect(q(r, ".ew-tools > i.ew-spin")).not.toBeNull();
  });
  it("a streaming answer is Markdown too, in one element that is rendered again as text arrives; the end flashes nothing", async () => {
    hooks.renderMarkdown.mockClear();
    const t = newTurn("t1", "q", null);
    applyEvent(t, FAKE_ANSWER[0]!);
    const r = new AssistantRenderer(actions(), hooks); r.update(st(conv({ turns: [t], busy: true })));
    expect(q(r, ".ew-trow.ew-run")!.textContent).toBe("正在网上搜索：PCA 为什么要最大化方差");
    vi.useFakeTimers();
    try {
      applyEvent(t, { type: "text_delta", text: "方差**大**", at: null }); r.update(st(conv({ turns: [t], busy: true })));
      expect(q(r, ".ew-tools")).toBeNull();
      const body = q(r, ".ew-amsg .ew-answer")!;
      expect(body.textContent).toBe("MD:方差**大**");
      await vi.advanceTimersByTimeAsync(0);
      applyEvent(t, { type: "text_delta", text: "，第二句", at: null }); r.update(st(conv({ turns: [t], busy: true })));
      applyEvent(t, { type: "text_delta", text: "，第三句", at: null }); r.update(st(conv({ turns: [t], busy: true })));
      expect(hooks.renderMarkdown).toHaveBeenCalledTimes(1);       // at most every STREAM_RENDER_MS
      await vi.advanceTimersByTimeAsync(200);
      expect(q(r, ".ew-amsg .ew-answer")).toBe(body);
      expect(body.textContent).toBe("MD:方差**大**，第二句，第三句");
      expect(hooks.renderMarkdown).toHaveBeenCalledTimes(2);
      t.status = "done"; t.rev++;
      r.update(st(conv({ turns: [t] })));
      expect(q(r, ".ew-amsg .ew-answer")).toBe(body);
    } finally { vi.useRealTimers(); }
  });
  it("a read that failed says nothing while the turn runs, and nothing at all once a later call of that tool worked", () => {
    const line = (callId: string, status: ToolLine["status"], done: string): ToolLine =>
      ({ callId, name: "read_page", effect: "read", summary: "", status, sources: [], label: "正在读页面", done, group: "read_page", groupLabel: "读了 {k} 页", items: [], refs: [] });
    const miss = line("a", "failed", "没读到页面"), hit = line("b", "ok", "读了《度》");
    const shown = (status: Turn["status"], tools: ToolLine[]): string[] => {
      const t = { ...newTurn("t1", "q", null), status, tools };
      const r = new AssistantRenderer(actions(), hooks); r.update(st(conv({ turns: [t] })));
      return all(r, ".ew-trow").map((e) => e.textContent ?? "");
    };
    expect(shown("streaming", [miss])).toEqual([]);
    expect(shown("done", [miss, hit])).toEqual(["读了《度》"]);
    expect(shown("done", [miss])).toEqual(["没读到页面"]);
  });
  it("finished Markdown is rendered once, not again on unrelated updates", () => {
    const t = finished();
    hooks.renderMarkdown.mockClear();
    const r = new AssistantRenderer(actions(), hooks); const c = conv({ turns: [t] });
    r.update(st(c)); r.update(st({ ...c, draft: "x" }));
    expect(hooks.renderMarkdown).toHaveBeenCalledTimes(1);
  });
  it("observations without a real mastery change show no line at all", () => {
    const t = newTurn("t", "q", null);
    applyEvent(t, { type: "observed", observations: 2, changes: [] });
    const r = new AssistantRenderer(actions(), hooks); r.update(st(conv({ turns: [t] })));
    expect(q(r, ".ew-amsg > .ew-muted")).toBeNull();
    expect(r.root.textContent).not.toContain("观察");
  });
});

describe("proposal card (.ew-propcard)", () => {
  it("内容建议 label, the title = what is added, then 添加 (primary) / 暂不添加 calling decide", () => {
    const act = actions(); const r = new AssistantRenderer(act, hooks); r.update(st(conv({ turns: [finished()] })));
    const pc = q(r, ".ew-propcard")!;
    expect(pc.previousElementSibling!.textContent).toBe("内容建议");
    expect(pc.querySelector("b")!.textContent).toBe("在本页添加一节「为什么是方差」");
    expect(pc.querySelector(".ew-muted")).toBeNull();      // the proposal's why is a reason (? tooltip), not a summary
    const [acc, rej] = [...pc.querySelectorAll(".ew-row > button")] as HTMLButtonElement[];
    expect(acc!.className).toBe("ew-btn ew-primary"); expect(acc!.textContent).toBe("添加");
    expect(rej!.className).toBe("ew-btn"); expect(rej!.textContent).toBe("暂不添加");
    acc!.click(); expect(act.decide).toHaveBeenCalledWith("t1", "pr_fake_1", "accept");
    rej!.click(); expect(act.decide).toHaveBeenLastCalledWith("t1", "pr_fake_1", "reject");
  });
  it("sending disables both buttons", () => {
    const t = finished(); t.proposals[0]!.status = "sending"; t.rev++;
    const r = new AssistantRenderer(actions(), hooks); r.update(st(conv({ turns: [t] })));
    for (const b of all(r, ".ew-propcard button") as HTMLButtonElement[]) expect(b.disabled).toBe(true);
    expect(q(r, ".ew-propcard > .ew-muted:last-child")!.textContent).toBe("正在发送…");
  });
  it("buttons stay disabled while the turn that raised the proposal is still streaming (server answers 409)", () => {
    const t = newTurn("t1", "q", null);
    for (const e of FAKE_ANSWER) if (e.type !== "done") applyEvent(t, e);
    const r = new AssistantRenderer(actions(), hooks); r.update(st(conv({ turns: [t], busy: true })));
    const btns = () => all(r, ".ew-propcard button") as HTMLButtonElement[];
    expect(btns()).toHaveLength(2);
    for (const b of btns()) expect(b.disabled).toBe(true);
    applyEvent(t, { type: "done", message_id: "m", tool_calls: 2, llm_calls: 3, cost_usd: null, attachments: [], session_attachments: [], stopped: false, confirmed: false });
    r.update(st(conv({ turns: [t] })));
    for (const b of btns()) expect(b.disabled).toBe(false);
  });
  const card = (status: "failed" | "accepted" | "rejected" | "already_decided", detail: string) => {
    const t = finished(); Object.assign(t.proposals[0]!, { status, detail });
    const r = new AssistantRenderer(actions(), hooks); r.update(st(conv({ turns: [t] })));
    return r;
  };
  it("decided: the buttons give way to the .ew-res line", () => {
    expect(q(card("accepted", ""), ".ew-propcard .ew-res")!.textContent).toBe("已添加到本页");
    expect(q(card("rejected", ""), ".ew-propcard .ew-res")!.textContent).toBe("未添加");
    expect(q(card("already_decided", "accepted"), ".ew-propcard .ew-res")!.textContent).toBe("这条提议已经处理过了。");
    expect(q(card("accepted", ""), ".ew-propcard button")).toBeNull();
  });
  it("failed is final: a Chinese reason shows, no buttons; English or empty detail keeps the generic copy", () => {
    const r = card("failed", "这一段已钉住，不能改写。");
    expect(q(r, ".ew-propcard > .ew-muted:last-child")!.textContent).toBe("没有添加：这一段已钉住，不能改写。");
    expect(q(r, ".ew-propcard button")).toBeNull();
    for (const d of ["", "RuntimeError: boom"]) {
      expect(q(card("failed", d), ".ew-propcard > .ew-muted:last-child")!.textContent).toBe("没有添加，页面没有改动。");
    }
  });
  it("failed because the page moved where it goes: no sentence, a quiet 重试 that asks again in the chat for the page now", () => {
    const t = finished(); Object.assign(t.proposals[0]!, { status: "failed", detail: LEDGER_STALE });
    const a = actions();
    const r = new AssistantRenderer(a, hooks); r.update(st(conv({ turns: [t] })));
    expect(q(r, ".ew-propcard")!.textContent).not.toContain("作废");
    const again = q(r, ".ew-propcard [data-op=redraft]") as HTMLButtonElement;
    expect(again.textContent).toBe("重试");
    again.click();
    expect(a.sendEdit).toHaveBeenCalledWith(expect.stringMatching(/^按现在的页面，重新.*：/));
  });
  it("a failed click uses proposal copy, never the message copy that mentions 重试", () => {
    const t = finished(); t.proposals[0]!.status = "error"; t.proposals[0]!.error = { kind: "offline" }; t.rev++;
    const r = new AssistantRenderer(actions(), hooks); r.update(st(conv({ turns: [t] })));
    const status = q(r, ".ew-propcard > .ew-muted:last-child")!.textContent;
    expect(status).toBe("未连接到服务器");
    expect(status).not.toContain("重试");
  });
});

describe("errors", () => {
  it("a .ew-muted line in the turn with per-code text; 重试 (.ew-btn.ew-link) only when retryable", () => {
    const a = newTurn("a", "q", null); applyEvent(a, { type: "error", code: "llm_unavailable", retryable: true, detail: "", interrupted: false });
    const act = actions(); const r = new AssistantRenderer(act, hooks); r.update(st(conv({ turns: [a] })));
    const line = q(r, ".ew-amsg > .ew-muted")!;
    expect(line.textContent).toContain("请切换模型重试");
    const retry = line.querySelector("button") as HTMLButtonElement;
    expect(retry.className).toBe("ew-btn ew-link"); expect(retry.textContent).toBe("重试");
    retry.click(); expect(act.retry).toHaveBeenCalledWith("a");
    const b = newTurn("b", "q", null); applyEvent(b, { type: "error", code: "budget_exceeded", retryable: false, detail: "今天的花费到了上限 $1.00，这次没有回答。明天 0 点恢复，也可以在设置里调高。", interrupted: false });
    act.openSettings = vi.fn();
    r.update(st(conv({ turns: [b] })));
    expect(q(r, ".ew-amsg > .ew-muted")!.textContent).toBe("今天的花费到了上限 $1.00，这次没有回答。明天 0 点恢复，也可以在设置里调高。 · 设置");
    const settings = q(r, ".ew-amsg > .ew-muted button") as HTMLButtonElement;
    expect(settings.textContent).toBe("设置");
    settings.click(); expect(act.openSettings).toHaveBeenCalled();
  });
  it("offline before any answer: the message stays, no typing dots and no line (the panel's 未连接到服务器 · 重试 says it)", () => {
    // user 2026-10-28: 「未连接到服务器就好了，下面那个删掉」
    const t = newTurn("t", "q", null); failTurn(t, { kind: "offline" });
    const r = new AssistantRenderer(actions(), hooks); r.update(st(conv({ turns: [t] })));
    expect(q(r, ".ew-typing")).toBeNull();
    expect(q(r, ".ew-amsg")).toBeNull();
    expect(r.root.textContent).not.toContain("不会排队");
    expect(r.root.textContent).toContain("q");
  });
});

describe("composer", () => {
  it("the send slot is the answer's stop while it runs: the same button, a square, no text; then the arrow again", () => {
    const act = { ...actions(), stopTurn: vi.fn() }; const r = new AssistantRenderer(act, hooks);
    r.update(st(conv({ busy: true }), { canStop: true }));
    const go = sendBtn(r);
    expect(go.disabled).toBe(false);
    expect(go.getAttribute("aria-label")).toBe("停止");
    expect(go.textContent).toBe("");
    expect(go.querySelector("svg.ew-stopi rect")).not.toBeNull();
    go.click();
    expect(act.stopTurn).toHaveBeenCalledOnce();
    expect(act.send).not.toHaveBeenCalled();
    r.update(st(conv()));
    expect(sendBtn(r)).toBe(go);
    expect(go.getAttribute("aria-label")).toBe("问");
    expect(go.querySelector(".ew-stopi")).toBeNull();
  });
  it("a stopped answer keeps its text, says 已停止 and has no rating row, also with no text at all", () => {
    const r = new AssistantRenderer(actions(), hooks);
    const t = newTurn("a", "q", null);
    applyEvent(t, { type: "text_delta", text: "写到一半", at: null });
    applyEvent(t, { type: "done", message_id: "m", tool_calls: 0, llm_calls: 1, cost_usd: null, attachments: [], session_attachments: [], stopped: true, confirmed: false });
    const empty = newTurn("b", "q", null);
    applyEvent(empty, { type: "done", message_id: "n", tool_calls: 0, llm_calls: 0, cost_usd: null, attachments: [], session_attachments: [], stopped: true, confirmed: false });
    r.update(st(conv({ turns: [t, empty] })));
    expect(r.root.textContent).toContain("写到一半");
    expect(all(r, ".ew-thread .ew-muted").filter((e) => e.textContent === "已停止")).toHaveLength(2);
    expect(all(r, ".ew-copy, .ew-rate")).toHaveLength(0);
  });
  it("Enter sends, Shift+Enter and IME-composing Enter do not", () => {
    const act = actions(); const r = new AssistantRenderer(act, hooks); r.update(st(conv({ draft: "q" })));
    const ta = q(r, "textarea")!;
    ta.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", shiftKey: true }));
    ta.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", isComposing: true }));
    expect(act.send).not.toHaveBeenCalled();
    ta.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", cancelable: true }));
    expect(act.send).toHaveBeenCalledTimes(1);
    sendBtn(r).click(); expect(act.send).toHaveBeenCalledTimes(2);
  });
  it("问 is disabled with no text and no quote, while busy and while a session loads; a quote alone enables it", () => {
    const act = actions(); const r = new AssistantRenderer(act, hooks);
    r.update(st(conv({ draft: "  " }))); expect(sendBtn(r).disabled).toBe(true);
    q(r, "textarea")!.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" }));
    expect(act.send).not.toHaveBeenCalled();
    r.update(st(conv({ draft: "", quote: { text: "一段", heading: null } }))); expect(sendBtn(r).disabled).toBe(false);
    q(r, "textarea")!.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" }));
    expect(act.send).toHaveBeenCalledTimes(1);
    r.update(st(conv({ draft: "q", quote: { text: "一段", heading: null }, busy: true }))); expect(sendBtn(r).disabled).toBe(true);
    r.update(st(conv({ draft: "q", busy: true }))); expect(sendBtn(r).disabled).toBe(true);
    r.update(st(conv({ draft: "q", restore: "loading" }))); expect(sendBtn(r).disabled).toBe(true);
    r.update(st(conv({ draft: "q", quote: { text: "一段", heading: null } }))); expect(sendBtn(r).disabled).toBe(false);
  });
  it("the quote chip (.ew-qchip) sits first in the box: the selected words, cut, and × (去掉引用) calls removeQuote", () => {
    const act = actions(); const r = new AssistantRenderer(act, hooks);
    r.update(st(conv({ quote: { text: "一段很长的文字", heading: "推导" } })));
    const chip = q(r, ".ew-cbox > .ew-qchip")!;
    expect(q(r, ".ew-cbox")!.firstElementChild).toBe(chip);
    expect(chip.querySelector("span")!.textContent).toBe("引用「一段很长的文字」");
    const x = chip.querySelector("button") as HTMLButtonElement;
    expect(x.textContent).toBe("×"); expect(x.title).toBe(""); expect(x.getAttribute("aria-label")).toBe("去掉引用"); expect(x.type).toBe("button");
    x.click(); expect(act.removeQuote).toHaveBeenCalledTimes(1);
    const long = "长".repeat(60);
    r.update(st(conv({ quote: { text: long, heading: null } })));
    expect(q(r, ".ew-qchip span")!.textContent).toBe(`引用「${"长".repeat(40)}…」`);
    expect((q(r, ".ew-qchip span") as HTMLElement).title).toBe(long);
    const wrong = "题目：某题\n我选的：A\n正确答案：B";
    r.update(st(conv({ quote: { text: wrong, heading: "推导", kind: "wrong_answer" } })));
    expect(q(r, ".ew-qchip span")!.textContent).toBe(wrong);
    r.update(st(conv())); expect(q(r, ".ew-qchip")).toBeNull();
  });
  it("over the length limit: 问 disabled and one .ew-muted line; under it, no line", () => {
    const r = new AssistantRenderer(actions(), hooks);
    const line = () => [...q(r, ".ew-cbox")!.children].find((c) => c.className === "ew-muted") as HTMLElement;
    r.update(st(conv({ draft: "x".repeat(4000) })));
    expect(line().hidden).toBe(true); expect(sendBtn(r).disabled).toBe(false);
    r.update(st(conv({ draft: "x".repeat(4001) })));
    expect(line().hidden).toBe(false); expect(line().textContent).toBe("4001/4000"); expect(sendBtn(r).disabled).toBe(true);
  });
  it("a focused textarea keeps what is typed; a cleared draft (after a send) empties it", () => {
    const r = new AssistantRenderer(actions(), hooks); document.body.append(r.root);
    r.update(st(conv({ draft: "abc" })));
    const ta = q(r, "textarea") as HTMLTextAreaElement; expect(ta.value).toBe("abc");
    ta.focus(); ta.value = "abcd";
    r.update(st(conv({ draft: "abc" }))); expect(ta.value).toBe("abcd");
    expect(q(r, "textarea")).toBe(ta);
    r.update(st(conv({ draft: "" }))); expect(ta.value).toBe("");
    r.root.remove();
  });
  it("input forwards the draft and grows the textarea (at most 160 px); a focusTick increase focuses it", () => {
    const act = actions(); const r = new AssistantRenderer(act, hooks); document.body.append(r.root);
    r.update(st(conv()));
    const ta = q(r, "textarea") as HTMLTextAreaElement;
    Object.defineProperty(ta, "scrollHeight", { configurable: true, get: () => 500 });
    ta.value = "hi"; ta.dispatchEvent(new Event("input")); expect(act.setDraft).toHaveBeenCalledWith("hi");
    expect(ta.style.height).toBe("160px");
    ta.blur(); r.update(st(conv(), { focusTick: 1 })); expect(document.activeElement).toBe(ta);
    r.root.remove();
  });
  it("a focus bump skips a hidden panel (another host of the same conversation)", () => {
    const r = new AssistantRenderer(actions(), hooks);
    const host = document.createElement("div"); host.hidden = true; host.append(r.root); document.body.append(host);
    r.update(st(conv()));
    r.update(st(conv(), { focusTick: 1 }));
    expect(document.activeElement).not.toBe(q(r, "textarea"));
    host.remove();
  });
});

describe("status lines (fail closed)", () => {
  const status = (r: AssistantRenderer) => q(r, ".ew-thread > .ew-muted")?.textContent ?? null;
  it("no note: no line, the composer is there (the conversation without a page); not configured: one muted line, composer hidden", () => {
    const r = new AssistantRenderer(actions(), hooks);
    r.update(st(conv({ target: { kind: "free" }, sessionId: null }))); expect(status(r)).toBeNull();
    expect((q(r, ".ew-composer") as HTMLElement).hidden).toBe(false);
    expect(r.root.textContent).not.toContain("打开一篇连到概念的笔记");
    // a plain note (linked to no concept) has the conversation without a page: no line, the composer is there
    r.update(st(conv({ target: { kind: "free" }, sessionId: null }), { active: { kind: "unsupported", title: "日记" } }));
    expect(status(r)).toBeNull(); expect((q(r, ".ew-composer") as HTMLElement).hidden).toBe(false);
    r.update(st(null, { configured: false })); expect(status(r)).toContain("设置");
    r.update(st(conv())); expect(status(r)).toBeNull();
    expect((q(r, ".ew-composer") as HTMLElement).hidden).toBe(false);
  });
  it("restore lines show only while there are no turns (P10); 重新载入 is a link button calling reload", () => {
    const act = actions(); const r = new AssistantRenderer(act, hooks);
    r.update(st(conv({ restore: "loading" }))); expect(status(r)).toContain("正在载入之前的对话");
    r.update(st(conv({ restore: "failed" })));
    const reload = q(r, ".ew-thread > .ew-muted button") as HTMLButtonElement;
    expect(reload.className).toBe("ew-btn ew-link"); expect(reload.textContent).toBe("重试");
    reload.click(); expect(act.reload).toHaveBeenCalledTimes(1);
    r.update(st(conv({ restore: "gone" }))); expect(status(r)).toContain("找不到了");
    r.update(st(conv({ restore: "gone", turns: [finished()] })));
    expect(status(r)).toBeNull();
  });
  it("the recap fold shows only non-empty groups, in order; nothing when empty or null", () => {
    const r = new AssistantRenderer(actions(), hooks);
    r.update(st(conv({ recap: { understood: ["方差"], unclear: [], edits: ["主成分分析"] } })));
    const d = q(r, ".ew-thread details")!;
    expect(d.querySelector("summary")!.textContent).toBe("更早的对话（摘要）");
    expect(d.textContent).toContain("讲懂了的"); expect(d.textContent).not.toContain("还没懂的");
    const t = d.textContent; expect(t.indexOf("讲懂了的")).toBeLessThan(t.indexOf("改过的页面"));
    expect(d.querySelectorAll("li").length).toBe(2);
    r.update(st(conv({ recap: { understood: [], unclear: [], edits: [] } }))); expect(q(r, ".ew-thread details")).toBeNull();
    r.update(st(conv({ recap: null }))); expect(q(r, ".ew-thread details")).toBeNull();
  });
});

describe("the thread follows new content", () => {
  function scrollRig(top: number) {
    const r = new AssistantRenderer(actions(), hooks);
    const el = q(r, ".ew-thread") as HTMLElement;
    const box = { top };
    Object.defineProperty(el, "scrollHeight", { configurable: true, get: () => 1000 });
    Object.defineProperty(el, "clientHeight", { configurable: true, get: () => 200 });
    Object.defineProperty(el, "scrollTop", { configurable: true, get: () => box.top, set: (v: number) => { box.top = v; } });
    return { r, box };
  }
  it("at the bottom, an updated turn keeps the view at the bottom", () => {
    const t = newTurn("t", "q", null);
    const { r, box } = scrollRig(800); r.update(st(conv({ turns: [t], busy: true })));
    box.top = 790; // within 32px of the bottom
    applyEvent(t, { type: "text_delta", text: "更多", at: null }); r.update(st(conv({ turns: [t], busy: true })));
    expect(box.top).toBe(1000);
  });
  it("scrolled up, an updated turn leaves the scroll position alone; a new turn scrolls to it", () => {
    const t = newTurn("t", "q", null);
    const { r, box } = scrollRig(800); r.update(st(conv({ turns: [t], busy: true })));
    box.top = 100;
    applyEvent(t, { type: "text_delta", text: "更多", at: null }); r.update(st(conv({ turns: [t], busy: true })));
    expect(box.top).toBe(100);
    r.update(st(conv({ turns: [t, newTurn("t2", "q2", null)], busy: true })));
    expect(box.top).toBe(1000);
  });
});

describe("header and history view (app.js assistantView / historyView)", () => {
  const NOW = new Date(2026, 9, 14, 12, 0);
  const clockHooks = { ...hooks, now: () => NOW };
  const iso = (d: number, h: number, mi: number) => new Date(2026, 9, d, h, mi).toISOString();
  const row = (id: string, title: string, last: string, page = "pca（2/4）") => ({ session_id: id, title, page_id: null, page_title: page,
    note_concept_id: null, created_at: last, last_activity_at: last, message_count: 2 });
  const open = (over: Partial<HistoryState> = {}): HistoryState => ({ ...HIST0, open: true, status: "ready", ...over });
  const btns = (r: AssistantRenderer) => all(r, ".ew-ahead > .ew-iconbtn") as HTMLButtonElement[];

  it("the header: title, then the clock and + icon buttons with aria-labels (no titles) and aria-pressed", () => {
    const act = actions(); const r = new AssistantRenderer(act, hooks);
    r.update(st(conv()));
    const head = q(r, ".ew-asst > .ew-ahead")!;
    expect([...head.children].map((c) => c.className)).toEqual(["ew-atitle", "ew-iconbtn", "ew-iconbtn"]);
    const [clock, plus] = btns(r);
    expect([clock!.title, clock!.getAttribute("aria-label"), clock!.type]).toEqual(["", "历史对话", "button"]);
    expect([plus!.title, plus!.getAttribute("aria-label")]).toEqual(["", "新会话"]);
    expect(clock!.getAttribute("aria-pressed")).toBe("false"); expect(plus!.hasAttribute("aria-pressed")).toBe(false);
    expect(clock!.querySelector("circle")!.getAttribute("r")).toBe("8.5");
    expect(clock!.querySelector("path")!.getAttribute("d")).toBe("M12 7.5V12l3 2");
    expect(plus!.querySelector("path")!.getAttribute("d")).toBe("M12 5v14M5 12h14");
    clock!.click(); expect(act.toggleHistory).toHaveBeenCalledTimes(1);
    plus!.click(); expect(act.newSession).toHaveBeenCalledTimes(1);
  });
  it("the title: 新会话 without questions, the first question (one ？ stripped, 24 chars) right after sending, 历史对话 while the list is open", () => {
    const r = new AssistantRenderer(actions(), hooks);
    const title = () => q(r, ".ew-atitle")!.textContent;
    r.update(st(conv())); expect(title()).toBe("新会话");
    r.update(st(conv({ turns: [newTurn("t", "特征值为什么叫「特征」？", null)], busy: true }))); expect(title()).toBe("特征值为什么叫「特征」");
    r.update(st(conv({ turns: [newTurn("t", "一二三四五六七八九十一二三四五六七八九十一二三四五六", null)] }))); expect(title()).toHaveLength(24);
    r.update(st(conv(), { history: open() })); expect(title()).toBe("历史对话");
    expect(btns(r)[0]!.getAttribute("aria-pressed")).toBe("true");
  });
  it("the composer bar has no 新会话 button any more: the clip slot, then 问", () => {
    const r = new AssistantRenderer(actions(), hooks); r.update(st(conv()));
    expect([...q(r, ".ew-cbar")!.children].map((c) => c.tagName)).toEqual(["SPAN", "BUTTON"]);
    expect(all(r, ".ew-composer button").map((b) => b.className)).toEqual(["ew-clip", "ew-send"]);
  });
  it("history open: .ew-hist (search + .ew-hlist) replaces the thread and composer; closing brings them back", () => {
    const r = new AssistantRenderer(actions(), hooks);
    r.update(st(conv(), { history: open({ items: [row("a", "t", iso(14, 9, 5))] }) }));
    expect([...r.root.children].map((c) => c.className)).toEqual(["ew-ahead", "ew-hist"]);
    const inp = q(r, ".ew-hist > input") as HTMLInputElement;
    expect([inp.type, inp.placeholder]).toEqual(["text", "搜索对话"]);
    expect(q(r, ".ew-hist > .ew-hlist")).not.toBeNull();
    r.update(st(conv()));
    expect([...r.root.children].map((c) => c.className)).toEqual(["ew-ahead", "ew-thread", "ew-composer"]);
  });
  it("rows are grouped 今天 / 昨天 / 更早 (only non-empty), each a button with title and `page · time`; the current one is .ew-on", () => {
    const r = new AssistantRenderer(actions(), clockHooks);
    const items = [row("a", "特征值为什么叫「特征」", iso(14, 15, 20), "pca（2/4）"), row("b", "协方差为负", iso(13, 21, 40)),
      row("c", "矩阵乘法", iso(11, 8, 0)), row("d", "", iso(14, 9, 0), "")];
    r.update(st(conv({ sessionId: "b" }), { history: open({ items }) }));
    // a row is .ew-hitem > the row button (+ its ⋯ when 删除 is wired)
    const kids = [...q(r, ".ew-hlist")!.children].map((k) => (k.className === "ew-hitem" ? k.firstElementChild! : k));
    expect(kids.map((k) => `${k.className}:${k.children.length ? [...k.children].map((x) => x.textContent).join("|") : k.textContent}`)).toEqual([
      "ew-hgroup:今天", "ew-hrow:特征值为什么叫「特征」|pca（2/4） · 今天 15:20", "ew-hrow:新会话|今天 09:00",
      "ew-hgroup:昨天", "ew-hrow ew-on:协方差为负|pca（2/4） · 昨天 21:40",
      "ew-hgroup:更早", "ew-hrow:矩阵乘法|pca（2/4） · 10月11日"]);
    const rows = all(r, ".ew-hrow") as HTMLButtonElement[];
    expect(rows[0]!.type).toBe("button");
    expect([...rows[0]!.children].map((c) => c.className)).toEqual(["ew-ht", "ew-hs"]);
    r.update(st(conv(), { history: open({ items: [row("c", "x", iso(11, 8, 0))] }) }));
    expect(all(r, ".ew-hgroup").map((g) => g.textContent)).toEqual(["更早"]);
  });
  it("clicking a row resumes that session", () => {
    const act = actions(); const r = new AssistantRenderer(act, clockHooks);
    r.update(st(conv(), { history: open({ items: [row("a", "t", iso(14, 9, 5))] }) }));
    (q(r, ".ew-hrow") as HTMLElement).click();
    expect(act.resume).toHaveBeenCalledWith("a");
  });
  it("empty states: 还没有历史对话 without a query, 没有找到相关的对话 with one", () => {
    const r = new AssistantRenderer(actions(), hooks);
    r.update(st(conv(), { history: open() })); expect(q(r, ".ew-hempty")!.textContent).toBe("还没有历史对话");
    r.update(st(conv(), { history: open({ q: "xx" }) })); expect(q(r, ".ew-hempty")!.textContent).toBe("没有找到相关的对话");
  });
  it("loading shows no empty-state claim; an error shows the failure, the reason and 重试 (never an empty list)", () => {
    const act = actions(); const r = new AssistantRenderer(act, hooks);
    r.update(st(conv(), { history: open({ status: "loading" }) }));
    expect(r.root.textContent).not.toContain("还没有历史对话"); expect(q(r, ".ew-hempty")!.textContent).toBe("正在载入…");
    r.update(st(conv(), { history: open({ status: "error", error: "未连接到服务器" }) }));
    const e = q(r, ".ew-hempty")!;
    expect(e.textContent).toBe("没能读到，重试");
    expect(r.root.textContent).not.toContain("还没有历史对话");
    (e.querySelector("button") as HTMLButtonElement).click(); expect(act.reloadHistory).toHaveBeenCalledTimes(1);
  });
  it("a failed resume shows a notice line above the rows", () => {
    const r = new AssistantRenderer(actions(), clockHooks);
    r.update(st(conv(), { history: open({ notice: "这段对话没能打开：服务器出错了（500）。", items: [row("a", "t", iso(14, 9, 5))] }) }));
    expect([...q(r, ".ew-hlist")!.children].map((c) => c.className)).toEqual(["ew-hempty", "ew-hgroup", "ew-hitem"]);
  });
  it("typing debounces ~200 ms before searchHistory(q); an outside q change updates the box, typing is not overwritten", () => {
    vi.useFakeTimers();
    try {
      const act = actions(); const r = new AssistantRenderer(act, hooks);
      r.update(st(conv(), { history: open() }));
      const inp = q(r, ".ew-hist > input") as HTMLInputElement;
      inp.value = "协"; inp.dispatchEvent(new Event("input"));
      vi.advanceTimersByTime(150); inp.value = "协方"; inp.dispatchEvent(new Event("input"));
      vi.advanceTimersByTime(150); expect(act.searchHistory).not.toHaveBeenCalled();
      vi.advanceTimersByTime(60); expect(act.searchHistory).toHaveBeenCalledTimes(1); expect(act.searchHistory).toHaveBeenCalledWith("协方");
      r.update(st(conv(), { history: open({ q: "协方" }) })); expect(inp.value).toBe("协方");
      inp.value = "协方差";   // typed, not yet sent
      r.update(st(conv(), { history: open({ q: "协方" }) })); expect(inp.value).toBe("协方差");
      r.update(st(conv(), { history: open({ q: "" }) })); expect(inp.value).toBe("");   // cleared from outside (a resume)
    } finally { vi.useRealTimers(); }
  });
  it("the search box takes the focus when the list opens", () => {
    const r = new AssistantRenderer(actions(), hooks); document.body.append(r.root);
    r.update(st(conv()));
    r.update(st(conv(), { history: open() }));
    expect(document.activeElement).toBe(q(r, ".ew-hist > input"));
    r.root.remove();
  });
  it("看改动 is closed, says how much goes, and shows the second paragraph the rewrite dropped; it stays open across redraws; accepted offers 撤销", () => {
    const acts = actions();
    const r = new AssistantRenderer(acts, hooks);
    const t = newTurn("t", "q", null); t.status = "done"; t.answer = "答";
    const card: ProposalCard = { proposalId: "a", status: "open", applied: [], error: null, detail: "", actionId: "", canUndo: false, undoing: false, note: "",
      draft: { kind: "rewrite_section", cue: "", answer: "", page_id: "p", section_id: "s", heading: "谁的朋友最多", cells: [], text: "第一句。第二句。", why: "w" },
      change: { old_heading: "谁的朋友最多", old_body: "第一句。第二句。第三句很长很长。\n\n第二段讲度数的求和。", new_body: "第一句。第二句。" } };
    t.proposals = [card];
    r.update(st(conv({ turns: [t] })));
    const d = () => all(r, ".ew-propdiff")[0] as HTMLDetailsElement;
    expect(d().open).toBe(false);
    expect(d().querySelector("summary")!.textContent).toBe("看改动删 18 字");
    const lines = [...d().querySelectorAll(".ew-dl")];
    expect(lines.map((l) => l.className)).toEqual(["ew-dl ew-mod", "ew-dl ew-del"]);
    expect(lines[0]!.querySelector("del")!.textContent).toBe("第三句很长很长。");
    expect(lines[1]!.textContent).toContain("第二段讲度数的求和。");
    d().open = true; d().dispatchEvent(new Event("toggle"));
    expect(acts.showProposal).toHaveBeenCalledExactlyOnceWith("t", "a");
    t.rev++; card.status = "accepted"; card.actionId = "act"; card.canUndo = true;
    r.update(st(conv({ turns: [t] })));
    expect(d().open).toBe(true);
    d().dispatchEvent(new Event("toggle"));
    expect(acts.showProposal).toHaveBeenCalledTimes(1);
    d().open = false; d().dispatchEvent(new Event("toggle"));
    d().open = true; d().dispatchEvent(new Event("toggle"));
    expect(acts.showProposal).toHaveBeenCalledTimes(2);
    const undo = all(r, ".ew-propres button")[0] as HTMLButtonElement;
    expect(undo.textContent).toBe("撤销");
    undo.click();
    expect(acts.undoProposal).toHaveBeenCalledWith("t", "a");
  });
  it("opening 看改动 uses the real controller's page and section navigation once per user opening", async () => {
    const openPage = vi.fn();
    const rig = cardRig({ results: { openPage, openGoal: vi.fn(), openRules: vi.fn(), openReview: vi.fn() } });
    await rig.ask([{ type: "proposal", proposal_id: "proposal_section", draft: {
      kind: "rewrite_section", cue: "", answer: "", page_id: "another_page", section_id: "actual_section",
      heading: "公式里的符号", cells: [], text: "补充后的解释", why: "解释符号" },
      change: { old_heading: "公式里的符号", old_body: "原有解释", new_body: "补充后的解释" } }]);
    const details = () => rig.q<HTMLDetailsElement>(".ew-propdiff")!;
    expect(openPage).not.toHaveBeenCalled();
    details().open = true; details().dispatchEvent(new Event("toggle"));
    expect(openPage).toHaveBeenCalledExactlyOnceWith("another_page", "actual_section");
    const turn = rig.ctl.state.conv!.turns[0]!;
    turn.rev++;
    rig.r.update(rig.ctl.state);
    expect(details().open).toBe(true);
    details().dispatchEvent(new Event("toggle"));
    expect(openPage).toHaveBeenCalledTimes(1);
    details().open = false; details().dispatchEvent(new Event("toggle"));
    details().open = true; details().dispatchEvent(new Event("toggle"));
    expect(openPage.mock.calls).toEqual([["another_page", "actual_section"], ["another_page", "actual_section"]]);
    rig.dispose();
  });
  it("a restored turn renders the same card as a live one: buttons while open, the result line once decided", () => {
    const r = new AssistantRenderer(actions(), hooks);
    const t = newTurn("t", "q", null); t.status = "done"; t.answer = "答";
    const card = (id: string, status: "open" | "accepted" | "rejected" | "failed" | "undone", detail = "") => ({ proposalId: id, status, applied: [], error: null, detail, change: null, actionId: "", canUndo: false, undoing: false, note: "",
      draft: { kind: "add_section" as const, cue: "", answer: "", page_id: "p", section_id: null, heading: "补充", cells: [], text: "t", why: "w" } });
    t.proposals = [card("a", "accepted"), card("b", "rejected"), card("c", "failed", "页面已经变了"), card("d", "open"), card("e", "undone")];
    r.update(st(conv({ turns: [t] })));
    const cards = all(r, ".ew-propcard");
    expect(cards.map((c) => c.firstElementChild!.textContent)).toEqual(Array(5).fill("在本页添加一节「补充」"));
    expect(cards[0]!.querySelector(".ew-res")!.textContent).toBe("已添加到本页");
    expect(cards[1]!.querySelector(".ew-res")!.textContent).toBe("未添加");
    expect(cards[2]!.textContent).toContain("没有添加：页面已经变了。");
    expect([...cards[3]!.querySelectorAll("button")].map((b) => [b.textContent, b.disabled])).toEqual([["添加", false], ["暂不添加", false]]);
    // accepted, then undone in the ledger (a restored session): 已撤销, no buttons
    expect(cards[4]!.querySelector(".ew-res")!.textContent).toBe("已撤销");
    expect(cards[4]!.querySelector("button")).toBeNull();
  });
});

describe("no recommended-question chips above the composer", () => {
  it("the composer holds only the box, whatever the Bot has suggested", () => {
    const r = new AssistantRenderer(actions(), hooks); r.update(st(conv()));
    expect([...q(r, ".ew-composer")!.children].map((c) => c.className)).toEqual(["ew-cbox"]);
    expect(r.root.querySelector("[aria-label=可以这样问]")).toBeNull();
  });
});

describe("chat attachments (asst.js, shell.html)", () => {
  const draft = (over: Partial<DraftAttachment> = {}): DraftAttachment =>
    ({ localId: "l1", name: "讲义.pdf", kind: "pdf", bytes: 10, pages: 40, status: "ready", ...over });
  const sent = (over: Partial<TurnAttachment> = {}): TurnAttachment => fromOut({ source_id: "src1", title: "讲义.pdf", kind: "pdf", status: "ready",
    deleted: false, uses: ["ask"], bytes: 10, pages: 40, expires_at: null, unreadable_pages: [], chars: null, read: null, importing: false,
    ...over });
  const file = (name: string, type = "") => new File(["x"], name, { type });
  const mk = (att: Partial<TurnAttachment>[] = [], text = "看看", extra: Partial<Conversation> = {}) => {
    const a = actions(); const r = new AssistantRenderer(a, hooks);
    const t = newTurn("t1", text, null, att.map((o) => sent(o as never)));
    r.update(st(conv({ turns: [t], ...extra }))); return { a, r, t };
  };
  const dragEv = (type: string, dt: object) => {
    const e = new Event(type, { cancelable: true, bubbles: true }); Object.defineProperty(e, "dataTransfer", { value: dt }); return e;
  };

  it("the left slot of the bar holds the clip (tooltip 上传附件) and a hidden multi-file input that accepts the source kinds + images", () => {
    const r = new AssistantRenderer(actions(), hooks); r.update(st(conv()));
    const clip = q(r, ".ew-cbar .ew-cleft .ew-clip") as HTMLButtonElement;
    expect(clip.title).toBe(""); expect(clip.getAttribute("aria-label")).toBe("上传附件"); expect(clip.hidden).toBe(false);
    const input = q(r, ".ew-cleft input[type=file]") as HTMLInputElement;
    expect(input.multiple).toBe(true); expect(input.hidden).toBe(true);
    expect(input.accept.split(",")).toEqual(expect.arrayContaining([".pdf", ".docx", ".pptx", ".xlsx", ".md", ".txt", ".png", ".jpg", "image/*"]));
  });
  it("no clip, no paste where the composer takes no files", () => {
    const a = actions(); const r = new AssistantRenderer(a, hooks); r.update(st(conv(), { canAttach: false }));
    expect((q(r, ".ew-clip") as HTMLButtonElement).hidden).toBe(true);
    const ta = q(r, "textarea") as HTMLTextAreaElement;
    const paste = new Event("paste", { cancelable: true }) as never as ClipboardEvent;
    Object.defineProperty(paste, "clipboardData", { value: { files: [file("a.png", "image/png")] } });
    ta.dispatchEvent(paste);
    expect(a.addFiles).not.toHaveBeenCalled(); expect(paste.defaultPrevented).toBe(false);
  });
  it("picking files calls actions.addFiles", () => {
    const a = actions(); const r = new AssistantRenderer(a, hooks); r.update(st(conv()));
    const input = q(r, ".ew-cleft input") as HTMLInputElement;
    const f = file("a.pdf");
    Object.defineProperty(input, "files", { value: [f], configurable: true });
    input.dispatchEvent(new Event("change"));
    expect(a.addFiles).toHaveBeenCalledWith([f]);
  });
  it("paste of files adds them and stops the paste; pasted text is left alone", () => {
    const a = actions(); const r = new AssistantRenderer(a, hooks); r.update(st(conv()));
    const ta = q(r, "textarea") as HTMLTextAreaElement;
    const paste = (files: File[]) => {
      const e = new Event("paste", { cancelable: true, bubbles: true }) as never as ClipboardEvent;
      Object.defineProperty(e, "clipboardData", { value: { files } }); ta.dispatchEvent(e); return e;
    };
    const png = file("image.png", "image/png");
    expect(paste([png]).defaultPrevented).toBe(true);
    expect(a.addFiles).toHaveBeenCalledWith([png]);
    expect(paste([]).defaultPrevented).toBe(false);
    expect(a.addFiles).toHaveBeenCalledTimes(1);
  });
  it("pasted text that would make the message over 4000 characters goes along as a text attachment", () => {
    const a = { ...actions(), addPastedText: vi.fn() }; const r = new AssistantRenderer(a, hooks); r.update(st(conv()));
    const ta = q(r, "textarea") as HTMLTextAreaElement;
    const paste = (text: string) => {
      const e = new Event("paste", { cancelable: true, bubbles: true }) as never as ClipboardEvent;
      Object.defineProperty(e, "clipboardData", { value: { files: [], getData: () => text } }); ta.dispatchEvent(e); return e;
    };
    expect(paste("短的一段").defaultPrevented).toBe(false);
    ta.value = "这段讲了什么？"; ta.setSelectionRange(ta.value.length, ta.value.length);
    const long = "长".repeat(3995);
    expect(paste(long).defaultPrevented).toBe(true);
    expect(a.addPastedText).toHaveBeenCalledWith(long);
  });
  it("dragging files over the panel shows 放到这里; dropping adds them; dragging out clears it", () => {
    const a = actions(); const r = new AssistantRenderer(a, hooks); r.update(st(conv()));
    const f = file("a.pdf");
    r.root.dispatchEvent(dragEv("dragenter", { types: ["Files"] }));
    expect(r.root.classList.contains("ew-drop")).toBe(true);
    expect(r.root.dataset.drop).toBe("放到这里");
    r.root.dispatchEvent(dragEv("dragleave", { types: ["Files"] }));
    expect(r.root.classList.contains("ew-drop")).toBe(false);
    r.root.dispatchEvent(dragEv("dragenter", { types: ["Files"] }));
    r.root.dispatchEvent(dragEv("drop", { types: ["Files"], files: [f], getData: () => "" }));
    expect(a.addFiles).toHaveBeenCalledWith([f]); expect(r.root.classList.contains("ew-drop")).toBe(false);
  });
  it("text dragged from the file list goes to dropLibrary (no overlay); text dropped on the textarea stays the browser's own", () => {
    const a = actions(); const r = new AssistantRenderer(a, hooks); r.update(st(conv()));
    const link = "obsidian://open?vault=v&file=%E8%AE%B2%E4%B9%89.pdf";
    r.root.dispatchEvent(dragEv("dragenter", { types: ["text/plain"] }));
    expect(r.root.classList.contains("ew-drop")).toBe(false);
    r.root.dispatchEvent(dragEv("drop", { types: ["text/plain"], files: [], getData: (t: string) => (t === "text/plain" ? link : "") }));
    expect(a.dropLibrary).toHaveBeenCalledWith(link); expect(a.addFiles).not.toHaveBeenCalled();
    q(r, "textarea")!.dispatchEvent(dragEv("drop", { types: ["text/plain"], files: [], getData: () => "hello" }));
    expect(a.dropLibrary).toHaveBeenCalledTimes(1);
  });

  it("draft cards sit above the textarea: kind tag, name, 正在解析 + bar (no counts), x removes", () => {
    const a = actions(); const r = new AssistantRenderer(a, hooks);
    r.update(st(conv({ attachments: [draft({ status: "reading", progress: { done: 3, total: 12 } })] })));
    const box = q(r, ".ew-cbox")!;
    expect([...box.children][0]!.className).toBe("ew-atts"); expect((q(r, ".ew-atts") as HTMLElement).hidden).toBe(false);
    const card = q(r, ".ew-atts .ew-att")!;
    expect(card.querySelector(".ew-th")!.textContent).toBe("PDF");
    expect(card.querySelector(".ew-an")!.textContent).toBe("讲义.pdf");
    expect(card.querySelector(".ew-as")!.textContent).toBe("正在解析");
    expect((card.querySelector(".ew-rvbar i") as HTMLElement).style.width).toBe("25%");
    expect(card.textContent).not.toMatch(/\d|\//);
    (card.querySelector(".ew-x") as HTMLButtonElement).click();
    expect(a.removeAttachment).toHaveBeenCalledWith("l1");
    expect((card.querySelector(".ew-x") as HTMLButtonElement).getAttribute("aria-label")).toBe("去掉「讲义.pdf」");
  });
  it("the four states: 正在解析 / 可用 (+ pages) / 部分页读不出 · 第 a–b 页 / 失败 · 原因", () => {
    const r = new AssistantRenderer(actions(), hooks);
    r.update(st(conv({ attachments: [draft({ status: "ready" }), draft({ localId: "l2", status: "partial", unreadable: [5, 6, 7, 14] }),
      draft({ localId: "l3", status: "failed", error: "加密，打不开。" }), draft({ localId: "l4", name: "图.png", kind: "image", pages: null })] })));
    const cards = all(r, ".ew-atts .ew-att");
    expect(cards.map((c) => c.querySelector(".ew-as")!.textContent)).toEqual(["可用", "部分页读不出 · 第 5–7、14 页", "失败 · 加密，打不开", "可用"]);
    expect(cards[0]!.querySelector(".ew-ad")!.textContent).toBe("40 页");
    expect(cards[3]!.querySelector(".ew-ad")).toBeNull();
    expect(cards[1]!.classList.contains("ew-warn")).toBe(true); expect(cards[2]!.classList.contains("ew-bad")).toBe(true);
  });
  it("an image draft shows its thumbnail", () => {
    const r = new AssistantRenderer(actions(), hooks);
    r.update(st(conv({ attachments: [draft({ kind: "image", name: "截图.png", thumb: "blob:x" })] })));
    expect((q(r, ".ew-atts .ew-th img") as HTMLImageElement).getAttribute("src")).toBe("blob:x");
  });
  it("send is off while a draft is reading (附件读完才能发 beside the clip), on once ready or failed; a failed attachment alone sends nothing", () => {
    const r = new AssistantRenderer(actions(), hooks);
    r.update(st(conv({ draft: "问", attachments: [draft({ status: "reading" })] })));
    expect(sendBtn(r).disabled).toBe(true);
    expect(q(r, ".ew-cwait")!.textContent).toBe("附件读完才能发"); expect((q(r, ".ew-cwait") as HTMLElement).hidden).toBe(false);
    r.update(st(conv({ draft: "问", attachments: [draft({ status: "failed", error: "x" })] })));
    expect(sendBtn(r).disabled).toBe(false); expect((q(r, ".ew-cwait") as HTMLElement).hidden).toBe(true);
    r.update(st(conv({ draft: "", attachments: [draft({ status: "failed", error: "x" })] })));
    expect(sendBtn(r).disabled).toBe(true);
    r.update(st(conv({ draft: "", attachments: [draft()] })));
    expect(sendBtn(r).disabled).toBe(false);
    r.update(st(conv({ draft: "", attachments: [] }))); expect(sendBtn(r).disabled).toBe(true);
    expect((q(r, ".ew-atts") as HTMLElement).hidden).toBe(true);
  });

  it("a sent message shows its cards above the bubble; images get the big thumbnail; a file-only message has no bubble", () => {
    const { r } = mk([{}, { kind: "image", title: "截图.png", pages: null, source_id: "s2" } as never]);
    const u = q(r, ".ew-umsg")!;
    expect([...u.children].map((c) => c.classList[0])).toEqual(["ew-att", "ew-att", "ew-ubub"]);
    expect(u.querySelectorAll(".ew-th.ew-big")).toHaveLength(1);
    const { r: r2 } = mk([{}], "");
    expect(q(r2, ".ew-umsg .ew-ubub")).toBeNull();
  });
  it("a sent card has no buttons (intent comes from the chat); while an import runs it shows 正在解析 + bar, never the use", () => {
    const { r, t } = mk([{}], "这是往年题，帮我看看");
    const card = q(r, ".ew-att")!;
    expect(card.querySelector(".ew-as")!.textContent).toBe("可用");
    expect(card.querySelector("button")).toBeNull();
    expect(card.querySelector(".ew-ajoin")).toBeNull();
    expect(r.root.textContent).not.toMatch(/加入资料|当真题/);
    t.attachments[0]!.uses = ["ask", "exam"]; t.attachments[0]!.progress = { done: 1, total: 4 }; t.rev++;
    r.update(st(conv({ turns: [t] })));
    const c2 = q(r, ".ew-att")!;
    expect(c2.querySelector(".ew-ajoin span")!.textContent).toBe("正在解析");
    expect((c2.querySelector(".ew-ajoin .ew-rvbar i") as HTMLElement).style.width).toBe("25%");
    expect(c2.querySelector("button")).toBeNull();
    expect(c2.textContent).not.toMatch(/auto|exam|ask|读懂|按这份学|已加入资料/);
  });
  it("N 天后清掉 only within 7 days and not joined; （已清理） for a deleted card, no buttons", () => {
    const soon = new Date(Date.now() + 3 * 86_400_000).toISOString(); const late = new Date(Date.now() + 25 * 86_400_000).toISOString();
    const { r } = mk([{ expires_at: soon } as never, { source_id: "s2", expires_at: late } as never,
      { source_id: "s3", expires_at: soon, uses: ["goal"] } as never, { source_id: "s4", deleted: true } as never]);
    const cards = all(r, ".ew-att");
    expect(cards[0]!.querySelector(".ew-aexp")!.textContent).toBe("3 天后清掉");
    expect(cards[1]!.querySelector(".ew-aexp")).toBeNull();
    expect(cards[2]!.querySelector(".ew-aexp")).toBeNull();
    expect(cards[3]!.querySelector(".ew-as")!.textContent).toBe("（已清理）");
    expect(cards[3]!.querySelector("button")).toBeNull(); expect(cards[3]!.classList.contains("ew-gone")).toBe(true);
  });
  it("failed and partial sent cards say so, with no buttons", () => {
    const { r } = mk([{ status: "failed" }, { source_id: "s2", status: "partial" } as never]);
    const cards = all(r, ".ew-att");
    expect(cards[0]!.querySelector("button")).toBeNull(); expect(cards[0]!.querySelector(".ew-as")!.textContent).toBe("失败");
    expect(cards[1]!.querySelector(".ew-as")!.textContent).toBe("部分页读不出"); expect(cards[1]!.querySelector("button")).toBeNull();
  });
  it("sent and restored cards: 部分页读不出 · 第 a–b 页 and the facts line N 页 · 约 X 万字", () => {
    const { r } = mk([{ status: "partial", unreadable_pages: [5, 6, 7, 14], chars: 12_345 } as never,
      { source_id: "s2", chars: 8_400 } as never, { source_id: "s3", kind: "image", pages: null, chars: null } as never]);
    const cards = all(r, ".ew-att");
    expect(cards[0]!.querySelector(".ew-as")!.textContent).toBe("部分页读不出 · 第 5–7、14 页");
    expect(cards[0]!.querySelector(".ew-ad")!.textContent).toBe("40 页 · 约 1.2 万字");
    expect(cards[1]!.querySelector(".ew-ad")!.textContent).toBe("40 页 · 约 8 千字");
    expect(cards[2]!.querySelector(".ew-ad")).toBeNull();
  });
  it("an import that failed shows its reason on the card", () => {
    const { r, t } = mk([{}]);
    t.attachments[0]!.error = "资料没能开始整理"; t.rev++;
    r.update(st(conv({ turns: [t] })));
    expect(q(r, ".ew-att .ew-aerr")!.textContent).toBe("资料没能开始整理");
    expect(q(r, ".ew-att button")).toBeNull();
  });
  it("errors render like other errors: needs_vision says 这张图片现在看不了; an attachment 409 shows the server's text", () => {
    const t = newTurn("t1", "这张图", null, [sent({ kind: "image" })]);
    failTurn(t, { kind: "event", code: "needs_vision", retryable: false, detail: "x" });
    const r = new AssistantRenderer(actions(), hooks); r.update(st(conv({ turns: [t], busy: false })));
    expect(q(r, ".ew-amsg .ew-muted")!.textContent).toContain("这张图片现在看不了");
    const t2 = newTurn("t2", "q", null, [sent()]); failTurn(t2, { kind: "attachment", detail: "附件还在读，读完再发。" });
    r.update(st(conv({ turns: [t2] })));
    expect(q(r, ".ew-amsg .ew-muted")!.textContent).toContain("附件还在读，读完再发。");
  });
  it("no process text anywhere: nothing about Claude / OCR / how the file was read", () => {
    const { r } = mk([{}, { kind: "image", source_id: "s2" } as never]);
    r.update(st(conv({ attachments: [draft({ status: "reading" })], turns: [] })));
    expect(r.root.textContent).not.toMatch(/Claude|OCR|读法|覆盖|全部读|读了附件/);
  });
});
