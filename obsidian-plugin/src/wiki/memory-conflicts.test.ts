import { afterEach, describe, expect, it, vi } from "vitest";
import { cardRig } from "../../test/card-rig";
import { ContractError } from "../assistant/parse";
import type { ActionEvent, AskEvent } from "../assistant/types";
import { setup } from "./controller-test-kit";
import { conflictTip, conflictViews, parseConflicts, type MemoryConflictOut } from "./memory-conflicts";
import { renderPersonal } from "./render-personal";
import { btn, fakeActions } from "./render-test-kit";
import { INITIAL_WIKI_STATE, type WikiState } from "./state";

const NOW = new Date(2026, 9, 29, 15, 0);
/** full-demo app.js CONFLICT_DEMO, on the server's fields (memory/conflicts.py library). */
const GROUPS: MemoryConflictOut[] = [
  { conflict_id: "k1", why: "一个说先举例子，一个说直接给公式", detected_at: "2026-10-29T03:00:00Z", items: [
    { id: "u3", category: "rule", text: "以后讲公式先举例子", date: "2026-10-10T02:00:00Z", source: "chat", where: "数学", where_kind: "domain", section: null, origin_ref: "s1/m1", origin_title: "方差怎么理解", origin_deleted: false, quote: "以后讲公式先举例子，不然看不懂" },
    { id: "u5", category: "rule", text: "以后直接给公式，别举例了", date: "2026-10-29T02:00:00Z", source: "consolidation", where: "数学", where_kind: "domain", section: null, origin_ref: "deleted:s2", origin_title: null, origin_deleted: true, quote: "例子太多了，直接给公式就行" }] },
  { conflict_id: "k2", why: "一个说先用数字算，这一页定了用字母推导", detected_at: "2026-10-29T03:00:00Z", items: [
    { id: "u6", category: "rule", text: "新概念先用具体数字算一遍", date: "2026-10-08T02:00:00Z", source: "rule_page", where: null, where_kind: null, section: null, origin_ref: null, origin_title: null, origin_deleted: false, quote: null },
    { id: "m7", category: "decision", text: "这一页讲方差用字母推导", date: "2026-10-29T01:00:00Z", source: "chat", where: "均值和方差", where_kind: "page", section: null, origin_ref: "s3/m2", origin_title: null, origin_deleted: false, quote: "这页别代数字了，直接用字母推" }] },
  { conflict_id: "k3", why: "每天只有半小时，规则却要每页写长", detected_at: "2026-10-29T03:00:00Z", items: [
    { id: "我是谁:x", category: "profile", text: "我每天只有半小时", date: null, source: "profile", where: null, where_kind: null, section: "我是谁", origin_ref: null, origin_title: null, origin_deleted: false, quote: "我每天只有半小时" },
    { id: "u7", category: "rule", text: "每页都写长一点，讲透", date: "2026-10-12T02:00:00Z", source: "chat", where: null, where_kind: null, section: null, origin_ref: "s1/m3", origin_title: "方差怎么理解", origin_deleted: false, quote: "每页都写长一点，讲透" }] },
];

afterEach(() => { document.body.replaceChildren(); });

describe("待确认的记忆 · GET /assistant/memory/conflicts checks", () => {
  it("passes the server's answer through", () => {
    expect(parseConflicts({ conflicts: GROUPS })).toEqual(GROUPS);
    expect(parseConflicts({ conflicts: [] })).toEqual([]);
  });

  it.each([
    ["not an object", []],
    ["no list", { conflicts: null }],
    ["one item", { conflicts: [{ ...GROUPS[0], items: [GROUPS[0]!.items[0]] }] }],
    ["unknown category", { conflicts: [{ ...GROUPS[0], items: [{ ...GROUPS[0]!.items[0], category: "tag" }, GROUPS[0]!.items[1]] }] }],
    ["unknown source", { conflicts: [{ ...GROUPS[0], items: [{ ...GROUPS[0]!.items[0], source: "bot" }, GROUPS[0]!.items[1]] }] }],
    ["a number for a quote", { conflicts: [{ ...GROUPS[0], items: [{ ...GROUPS[0]!.items[0], quote: 3 }, GROUPS[0]!.items[1]] }] }],
    ["no why", { conflicts: [{ ...GROUPS[0], why: undefined }] }],
    ["unknown where_kind", { conflicts: [{ ...GROUPS[0], items: [{ ...GROUPS[0]!.items[0], where_kind: "topic" }, GROUPS[0]!.items[1]] }] }],
    ["no origin_deleted", { conflicts: [{ ...GROUPS[0], items: [{ ...GROUPS[0]!.items[0], origin_deleted: undefined }, GROUPS[0]!.items[1]] }] }],
  ])("fails closed on %s", (_, x) => {
    expect(() => parseConflicts(x)).toThrow(ContractError);
  });

  it("the ? (app.js cfTip): where it is written, date · source · 只用于…, his words, where he said them", () => {
    const [k1, k2, k3] = GROUPS;
    expect(conflictTip(k1!.items[0]!, NOW)).toEqual(["这条写在你的规则里", "10月10日 · 聊天里说的 · 只用于数学", "「以后讲公式先举例子，不然看不懂」", "在「方差怎么理解」里说的"]);
    expect(conflictTip(k1!.items[1]!, NOW)[1]).toBe("今天 · 从对话里整理的 · 只用于数学");
    expect(conflictTip(k1!.items[1]!, NOW)[3]).toBe("原对话已删除");
    // a rule's evidence line as the server gives it: his words only
    expect(conflictTip({ ...k1!.items[1]!, quote: "你说过「例子太多了」" }, NOW)[2]).toBe("「例子太多了」");
    expect(conflictTip(k2!.items[0]!, NOW)).toEqual(["这条写在你的规则里", "10月8日 · 规则页手写的", "在规则页里写的"]);
    expect(conflictTip(k2!.items[1]!, NOW)).toEqual(["这条是「均值和方差」这一页的决定", "今天 · 聊天里说的 · 只用于「均值和方差」", "「这页别代数字了，直接用字母推」", "在助手里说的"]);
    expect(conflictTip(k3!.items[0]!, NOW)).toEqual(["这条写在你的档案里", "学习者档案里写的", "「我每天只有半小时」", "在学习者档案里"]);
  });
});

const state = (o: Partial<WikiState>): WikiState => ({ ...INITIAL_WIKI_STATE, conn: "online", tab: "engine", rsi: { kind: "ready", value: [] }, ...o });
const draw = (s: WikiState, a = fakeActions()) => { const b = document.createElement("div"); renderPersonal(b, s, a, NOW); return { a, b }; };

describe("待确认的记忆 · the card (app.js memoriesCard)", () => {
  it("on top of 个性化: title + N 组, the line, every group's items with their ?, one 处理; 待确认的偏好 and 我的学习偏好 under it", () => {
    const { a, b } = draw(state({ conflicts: { kind: "ready", value: conflictViews(GROUPS, NOW) } }));
    const card = b.querySelector<HTMLElement>("#pz-conflicts")!;
    expect(b.firstElementChild).toBe(card);
    expect(card.className).toBe("ew-card ew-task");
    expect([...card.children].map((c) => c.className)).toEqual(["ew-thead", "ew-muted", "ew-pz-mem", "ew-acts"]);
    expect([...card.querySelectorAll(".ew-thead > *")].map((e) => e.textContent)).toEqual(["待确认的记忆", "3 组"]);
    expect(card.querySelector(":scope > .ew-muted")!.textContent).toBe("发现几条可能不一致的记忆。确认后，EvoWiki 才能更准确地遵循你的偏好。");
    const groups = [...card.querySelectorAll(".ew-pz-mem > .ew-pz-grp")];
    expect(groups.map((g) => [...g.querySelectorAll(":scope > .ew-pz-it > .ew-pz-t")].map((t) => t.textContent))).toEqual([
      ["以后讲公式先举例子", "以后直接给公式，别举例了"], ["新概念先用具体数字算一遍", "这一页讲方差用字母推导"], ["我每天只有半小时", "每页都写长一点，讲透"]]);
    // no category tags, no group headers (user 2026-10-29)
    expect(card.querySelector(".ew-pz-tag, .ew-pz-when")).toBeNull();
    const q = groups[0]!.querySelector(".ew-qtip")!;
    expect(q.querySelector("button .evowiki-sr-only")!.textContent).toBe("日期、来源和原话");
    expect([...q.querySelectorAll(".ew-tipbox > span")].map((e) => e.textContent)[0]).toBe("这条写在你的规则里");
    const go = card.querySelectorAll(".ew-acts > button");
    expect([...go].map((x) => [x.textContent, x.className])).toEqual([["处理", "ew-btn ew-primary"]]);
    btn(card, "[data-action=conflicts-resolve]").click();
    expect(a.resolveConflicts).toHaveBeenCalledOnce();
  });

  it("0 groups, not read yet, or still loading: no card", () => {
    for (const conflicts of [{ kind: "ready", value: [] }, { kind: "idle" }, { kind: "loading" }] as WikiState["conflicts"][]) {
      expect(draw(state({ conflicts })).b.querySelector("#pz-conflicts")).toBeNull();
    }
  });

  it("a failed read: the title and 没能读到，重试; offline no card (the sidebar's banner says it)", () => {
    const { a, b } = draw(state({ conflicts: { kind: "error", message: "x" } }));
    const card = b.querySelector<HTMLElement>("#pz-conflicts")!;
    expect(card.textContent).toContain("待确认的记忆");
    expect(card.querySelector("[data-role=conflicts-failed]")!.textContent).toContain("没能读到");
    btn(card, "[data-action=conflicts-reload]").click();
    expect(a.reloadConflicts).toHaveBeenCalledOnce();
    expect(draw(state({ conn: "offline", conflicts: { kind: "error", message: "x" } })).b.querySelector("#pz-conflicts")).toBeNull();
  });

  it("处理 is off while offline", () => {
    const { b } = draw(state({ conn: "offline", conflicts: { kind: "ready", value: conflictViews(GROUPS, NOW) } }));
    expect(btn(b, "[data-action=conflicts-resolve]").disabled).toBe(true);
  });
});

describe("待确认的记忆 · the controller", () => {
  it("showing 个性化 reads the groups; settled in the chat, the next read leaves the card out", async () => {
    const { c, api } = setup();
    api.__setConflicts(GROUPS);
    await c.loadEngine();
    const s = c.state.conflicts;
    if (s.kind !== "ready") throw new Error(s.kind);
    expect(s.value.map((g) => g.id)).toEqual(["k1", "k2", "k3"]);
    api.__setConflicts([]);
    await c.loadEngine();
    expect(c.state.conflicts).toEqual({ kind: "ready", value: [] });
  });

  it("处理 asks main.ts for the chat (deps.resolveMemoryConflicts)", () => {
    const go = vi.fn();
    const { c } = setup({ resolveMemoryConflicts: go });
    c.resolveConflicts();
    expect(go).toHaveBeenCalledOnce();
  });
});

describe("待确认的记忆 · 处理 in the 助手 (app.js startMemories / cfAnswer)", () => {
  const ASK: AskEvent = { type: "ask", call_id: "c1", kind: "ask_user",
    payload: { question: "你想保留哪条？也可以直接告诉我怎么改。", options: ["保留「以后讲公式先举例子」", "保留「以后直接给公式，别举例了」", "两条都保留"] } };
  const KEPT: ActionEvent = { type: "action", goal_title: null, page_id: null, action_id: "act9", kind: "memory_conflict_resolve", title: "保留「以后讲公式先举例子」，撤下另外 1 条",
    items: [], status: "executed", confirm: false, can_undo: true, replaces: "" };

  it("a new chat without a page, its message sent with intent resolve_memory_conflicts and no bubble; the title says what it is", async () => {
    const rig = cardRig();
    await rig.ask([], "之前的问题");
    rig.api.messageReplies.push({ events: [{ type: "text_delta", text: "一个说先举例子，一个说直接给公式，两条现在都在用。", at: null }, ASK,
      { type: "done", message_id: "m9", tool_calls: 1, llm_calls: 1, cost_usd: null, attachments: [], session_attachments: [], stopped: false, confirmed: false }] });
    const chat = rig.ctl.startConflictsChat()!;
    expect(rig.ctl.state.conv!.target).toEqual({ kind: "free" });
    expect(await chat.done).toBe(true);
    expect(rig.api.calls.sendMessage.slice(-1)[0]!.body).toEqual({ text: "处理待确认的记忆", intent: "resolve_memory_conflicts" });
    expect(rig.ctl.state.conv!.turns).toHaveLength(1);
    expect(rig.q(".ew-ubub")).toBeNull();
    expect(rig.q(".ew-umsg")).toBeNull();
    expect(rig.ctl.state.conv!.turns[0]!.asks.map((q) => q.options)).toEqual([ASK.payload.options]);
    rig.dispose();
  });

  it("the settled group's card: 已保留「…」 with 已完成, 查看详情 / 撤销, no 内容建议 label", async () => {
    const rig = cardRig();
    await rig.ask([KEPT], "保留「以后讲公式先举例子」");
    const card = rig.q(".ew-amsg > .ew-propcard")!;
    expect(card.querySelector(".ew-thead b")!.textContent).toBe("已保留「以后讲公式先举例子」");
    expect(card.querySelector(".ew-thead .ew-chip")!.textContent).toBe("已完成");
    expect([...card.querySelectorAll(".ew-aacts button")].map((b) => b.textContent)).toEqual(["查看详情", "撤销"]);
    expect(rig.q(".ew-amsg > .ew-asec")).toBeNull();
    rig.dispose();
  });

  it("the result comes before the next group's question; the reads between groups draw no tool line", async () => {
    const rig = cardRig();
    await rig.ask([KEPT,
      { type: "text_delta", text: "保留「以后讲公式先举例子」，撤下另外 1 条，可以撤销。\n\n每天只有半小时，规则却要每页写长", at: null },
      { type: "tool_start", call_id: "r2", name: "read_memory_conflicts", effect: "read", summary: "", label: "正在看待确认的记忆" },
      { type: "tool_end", call_id: "r2", ok: true, sources: [], label: "看了待确认的记忆", group: "", group_label: "", items: [], refs: [] },
      { ...ASK, call_id: "c2" }], "保留「以后讲公式先举例子」");
    const msgs = rig.all(".ew-amsg"), msg = msgs[msgs.length - 1]!;
    const card = msg.querySelector(":scope > .ew-propcard")!, q = [...msg.children].find((p) => p.textContent?.includes("你想保留哪条"))!;
    // the card came before any text of the turn: first, then the next group's lines, then its question
    const kids = [...msg.children], at = (k: string): number => kids.findIndex((c) => c.textContent?.includes(k));
    expect(kids.indexOf(card)).toBe(0);
    expect(at("每天只有半小时")).toBeGreaterThan(0);
    expect(at("每天只有半小时")).toBeLessThan(kids.indexOf(q));
    expect(card.compareDocumentPosition(q) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(msg.querySelectorAll(".ew-propcard")).toHaveLength(1);
    expect(msg.querySelector(".ew-trows")).toBeNull();
    rig.dispose();
  });
});
