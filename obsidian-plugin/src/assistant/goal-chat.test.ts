import { afterEach, describe, expect, it, vi } from "vitest";
import { OfflineError } from "../api/errors";
import { cardRig, ID, PAGE } from "../../test/card-rig";
import { AssistantController, type AssistantDeps } from "./controller";
import { FakeAssistantApi } from "./fake-api";
import { sessionKey, type AssistantSessionStore } from "./session-store";

const sessionKeyOf = (): string => sessionKey(ID, PAGE);
import type { ActionOut } from "../wiki/bot/bot-schema";
import type { GoalChatRow } from "./goal-chat";
import type { FirstPage } from "./goal-run";
import { goalResultOf } from "./goal-chat";
import type { ActionEvent, SessionOut } from "./types";

/** The goal_new row new_goal writes for 「我要学黑壳虾」 (content pages = the blocks). */
const blocks = (id: string, names: [string, string, string][]): ActionEvent => ({
  type: "action", goal_title: "黑壳虾", replaces: "", page_id: null, action_id: id, kind: "goal_new", title: `「黑壳虾」分成 ${names.length} 块`, status: "proposed", confirm: true, can_undo: false,
  items: names.map(([key, title, covers]) => ({ key, title, covers, page_id: null, n: null, level: null })),
});
const FIVE = blocks("g1", [["id", "认识黑壳虾", "它是什么虾、身体结构和蜕皮、怎么分公母"], ["wq", "水质", "水温、pH 和硬度"], ["keep", "养起来", "开缸、喂什么喂多少"],
  ["br", "繁殖", "公母比例、抱卵和孵化"], ["sick", "生病和出问题", "常见病"]]);
// edit_goal_blocks's new draft names the row it replaced
const FOUR = { ...blocks("g2", [["id", "认识黑壳虾", "它是什么虾"], ["keep", "养起来", "水温、开缸"], ["br", "繁殖", "公母比例"], ["sick", "生病和出问题", "常见病"]]),
  replaces: "g1" };
const row = (id: string, status: string, over: Partial<ActionOut> = {}): ActionOut => ({
  action_id: id, kind: "goal_new", scope: "goal", level: "ask", status, source: "assistant", summary: "", why: "", content: {}, page_id: null, detail: "",
  created_at: "2026-10-23T09:00:00Z", decided_at: null, expires_at: null, can_undo: status === "executed", confirm: true, cost_usd: null, diff: [], made: [],
  revise: null, result: {}, section_id: null, ...over });

afterEach(() => { document.body.replaceChildren(); });

describe("the goal_new card (asst.js draftList a.unit = 块, app.js blocksAct)", () => {
  it("keeps the intake topic separate from the decorated action heading in pending and live goal rows", async () => {
    const rig = cardRig();
    const rows: GoalChatRow[] = [];
    rig.ctl.onGoalRow((r) => rows.push(r));
    await rig.ask([{ ...FIVE, title: "「梯度下降」分成5块", goal_title: "梯度下降" }], "我想学会梯度下降，从导数基础开始");
    expect(rows.slice(-1)[0]).toMatchObject({ phase: "wait", title: "梯度下降" });
    expect(rig.q(".ew-amsg > .ew-propcard b")!.textContent).toBe("「梯度下降」分成5块");
    rig.api.actionReplies.push(row("g1", "executed", { content: { title: "梯度下降", pages: [] }, result: { draft_id: "dr1", map_id: "map1", open: true } }));
    await rig.ctl.confirmAction("g1");
    expect(rows.slice(-1)[0]).toMatchObject({ phase: "live", title: "梯度下降" });
    rig.dispose();
  });
  it("a draft of blocks: 「黑壳虾」分成 5 块, rows 第 n 块, × 去掉「…」, 加一块, 确认 / 取消 (no sub line)", async () => {
    const rig = cardRig();
    await rig.ask([FIVE]);
    const card = rig.q(".ew-amsg > .ew-propcard")!;
    expect([...card.children].map((c) => c.tagName.toLowerCase() + "." + c.className)).toEqual(["b.", "div.ew-draft", "div.ew-row"]);
    expect(card.querySelector("b")!.textContent).toBe("「黑壳虾」分成 5 块");
    const li = rig.all(".ew-alist.ew-drag > li");
    expect(li[1]!.getAttribute("aria-label")).toBe("第 2 块：水质，拖动或用 Alt + 上下键调整顺序");
    // × has no native title tooltip (master ui-polish-1023), only its aria-label
    const x0 = li[0]!.querySelector<HTMLButtonElement>(".ew-x")!;
    expect([x0.title, x0.getAttribute("aria-label")]).toEqual(["", "去掉「认识黑壳虾」"]);
    const inp = rig.q<HTMLInputElement>(".ew-aadd input")!;
    expect([inp.placeholder, inp.getAttribute("aria-label")]).toEqual(["添加学习主题", "添加学习主题"]);
    expect([...card.querySelectorAll(":scope > .ew-row button")].map((b) => b.textContent)).toEqual(["确认", "取消"]);
    // the title follows the draft as it is edited
    li[1]!.querySelector<HTMLButtonElement>(".ew-x")!.click();
    expect(rig.q(".ew-amsg > .ew-propcard b")!.textContent).toBe("「黑壳虾」分成 4 块");
    rig.dispose();
  });

  it.each(["center", "sidebar"])("%s restores the pending goal from its saved conversation with no new message or action", async (first) => {
    const session: SessionOut = { gone_sources: [], session_id: "ses_saved", page_id: null, recap: null, decisions: [], proposals: [],
      actions: [{ ...FIVE, title: "「梯度下降」分成5块", goal_title: "梯度下降", items: FIVE.items.map((item, i) => ({ ...item, title: ["导数基础", "偏导数", "损失函数", "梯度与下降方向", "手算一次参数更新"][i]!, covers: null })), result: {}, detail: "", section_id: null }], messages: [
        { message_id: "m1", role: "user", card_at: null, stopped: false, intent: "learn_goal", draft_id: null, text: "我想学会梯度下降，从导数基础开始", sources: [], proposals: [], actions: [], selection: null, tools: [], attachments: [], rating: null, asks: [] },
        { message_id: "m2", role: "assistant", card_at: null, stopped: false, intent: null, draft_id: null, text: "分成五块", sources: [], proposals: [], actions: ["g1"], selection: null, tools: [], attachments: [], rating: null, asks: [] }] };
    const api = new FakeAssistantApi();
    api.sessionOuts.set("ses_saved", session);
    const free = sessionKey(ID, { kind: "free" });
    const rig = cardRig({ api, store: { get: (key) => key === free ? "ses_saved" : null, set: () => Promise.resolve() } });
    if (first === "sidebar") rig.ctl.show({ kind: "none" });
    const pending = await rig.ctl.pendingGoal();
    expect(pending).toMatchObject({ goal: "我想学会梯度下降，从导数基础开始", row: { key: free, actionId: "g1", phase: "wait", title: "梯度下降", blocks: [{ name: "导数基础" }, {}, {}, {}, {}] } });
    expect(api.calls.getSession).toEqual(["ses_saved"]);
    expect(api.actionCalls).toEqual([]);
    expect(rig.ctl.goalRun("g1")).toBeUndefined();
    expect((await rig.ctl.pendingGoal())?.row.actionId).toBe("g1");
    expect(api.calls.getSession).toEqual(["ses_saved"]);
    rig.dispose();
  });

  it("a newer draft in the chat: the old card says 已换成新的分法 at once; 学点新的 hears replaced, then the new draft", async () => {
    const rig = cardRig();
    const rows: GoalChatRow[] = [];
    rig.ctl.onGoalRow((r) => rows.push(r));
    await rig.ask([FIVE]);
    await rig.ask([FOUR], "水质和养起来合成一块");
    const cards = rig.all(".ew-amsg > .ew-propcard");
    expect(cards.map((c) => [c.querySelector("b")!.textContent, c.querySelector(".ew-res")?.textContent ?? null]))
      .toEqual([["「黑壳虾」分成 5 块", "已换成新的分法"], ["「黑壳虾」分成 4 块", null]]);
    expect(rows.map((r) => [r.actionId, r.phase, r.replaced, r.blocks.length])).toEqual([["g1", "wait", false, 5], ["g1", "rejected", true, 5], ["g2", "wait", false, 4]]);
    expect(rows[2]!.blocks[1]).toEqual({ key: "keep", name: "养起来", scope: "水温、开缸", level: "" });
    rig.dispose();
  });

  it("a second new_goal (it names no replaced row) leaves the other waiting goal card alone", async () => {
    const rig = cardRig();
    await rig.ask([FIVE]);
    await rig.ask([blocks("g3", [["a", "面团", "揉面"], ["b", "烘烤", "温度"]])], "我还想学做面包");
    const cards = rig.all(".ew-amsg > .ew-propcard");
    expect(cards.map((c) => c.querySelector(".ew-res")?.textContent ?? null)).toEqual([null, null]);
    expect([...cards[0]!.querySelectorAll(":scope > .ew-row button")].map((b) => b.textContent)).toEqual(["确认", "取消"]);
    rig.dispose();
  });

  it("card edits of a goal draft are kept on its row as they happen (POST …/draft); 确认 then sends no list", async () => {
    const rig = cardRig();
    const sent: { id: string; draft: unknown }[] = [];
    rig.api.editDraft = (id, draft) => {
      if (!("pages" in draft)) throw new Error("Expected a page-list draft");
      sent.push({ id, draft: structuredClone(draft) });
      const pages = draft.pages.map((p, k) => ("key" in p && p.key ? { key: p.key, title: `t${k}` } : { key: `new${k}`, title: (p as { title: string }).title }));
      return Promise.resolve(row(id, "proposed", { content: { pages } }));
    };
    await rig.ask([FIVE]);
    rig.ctl.removeDraft("g1", 3);
    rig.ctl.addDraft("g1", "造景");
    for (let i = 0; i < 5; i++) await new Promise((r) => setTimeout(r, 0));
    expect(sent.map((x) => x.id)).toEqual(["g1"]);            // the newest list only (the first was superseded in the queue)
    expect(sent[0]!.draft).toEqual({ pages: [{ key: "id" }, { key: "wq" }, { key: "keep" }, { key: "sick" }, { title: "造景", isNew: true }] });
    await rig.ctl.confirmAction("g1");
    const approve = rig.api.actionCalls.find((c) => c.op === "approve")!;
    expect(approve.draft).toBeUndefined();
    rig.dispose();
  });

  it("学点新的's 改名 of a block is kept on the row as {key, title} (its key, level and covers stay on the server)", async () => {
    const rig = cardRig();
    const sent: unknown[] = [];
    rig.api.editDraft = (id, draft) => { sent.push(structuredClone(draft)); return Promise.resolve(row(id, "proposed", {})); };
    await rig.ask([FIVE]);
    rig.ctl.renameDraft("g1", 1, "水和环境");
    rig.ctl.renameDraft("g1", 1, "  ");                         // an empty name changes nothing
    for (let i = 0; i < 5; i++) await new Promise((r) => setTimeout(r, 0));
    expect(sent).toHaveLength(1);
    expect((sent[0] as { pages: unknown[] }).pages).toEqual([{ key: "id" }, { key: "wq", title: "水和环境" }, { key: "keep" }, { key: "br" }, { key: "sick" }]);
    rig.dispose();
  });

  it("a refused card edit shows the server's text and the card goes back to the row as stored", async () => {
    const { ApiError } = await import("../api/errors");
    const { clearEwNotices, ewNotices } = await import("../../test/ew-notices");
    clearEwNotices();
    const rig = cardRig();
    rig.api.editDraft = () => Promise.reject(new ApiError(409, "已经有这一块了"));
    await rig.ask([FIVE]);
    rig.ctl.addDraft("g1", "水质");
    for (let i = 0; i < 5; i++) await new Promise((r) => setTimeout(r, 0));
    expect(ewNotices()).toEqual(["已经有这一块了"]);
    expect(rig.all(".ew-alist.ew-drag > li")).toHaveLength(5);
    expect(rig.api.actionCalls.some((c) => c.op === "list")).toBe(true);
    clearEwNotices();
    rig.dispose();
  });

  it("the ledger's own replaced row (a reloaded session / 参数) says the same", async () => {
    const rig = cardRig();
    await rig.ask([FIVE]);
    rig.ctl.syncLedger([row("g1", "rejected", { detail: "replaced" })]);
    expect(rig.q(".ew-amsg > .ew-propcard .ew-res")!.textContent).toBe("已换成新的分法");
    rig.dispose();
  });

  it("确认 with an edited list: the edited draft goes with approve; the done card 已把「黑壳虾」分成 3 块 + 已完成, no list, 查看详情 / 撤销; the row's result reaches 学点新的", async () => {
    const rig = cardRig();
    const rows: GoalChatRow[] = [];
    rig.ctl.onGoalRow((r) => rows.push(r));
    await rig.ask([FIVE]);
    rig.ctl.removeDraft("g1", 3); rig.ctl.removeDraft("g1", 1);
    expect(rows.slice(-1)[0]!.blocks.map((b) => b.name)).toEqual(["认识黑壳虾", "养起来", "生病和出问题"]);
    rig.api.actionReplies.push(row("g1", "executed", { content: { pages: [] }, result: { draft_id: "dr1", map_id: "map1", open: true } }));
    await rig.ctl.confirmAction("g1");
    expect(rig.api.actionCalls[0]).toEqual({ op: "approve", id: "g1", draft: { pages: [{ key: "id" }, { key: "keep" }, { key: "sick" }] } });
    const card = rig.q(".ew-amsg > .ew-propcard")!;
    expect(card.querySelector(".ew-thead b")!.textContent).toBe("已把「黑壳虾」分成 3 块");
    expect(card.querySelector(".ew-thead .ew-chip")!.textContent).toBe("已完成");
    expect(card.querySelector(".ew-alist")).toBeNull();
    expect([...card.querySelectorAll(".ew-aacts button")].map((b) => b.textContent)).toEqual(["查看详情", "撤销"]);
    expect(rows.slice(-1)[0]).toMatchObject({ actionId: "g1", phase: "live", result: { draftId: "dr1", mapId: "map1" } });
    rig.dispose();
  });

  it("取消 → rejected (not replaced): 学点新的 hears it", async () => {
    const rig = cardRig();
    const rows: GoalChatRow[] = [];
    rig.ctl.onGoalRow((r) => rows.push(r));
    await rig.ask([FIVE]);
    await rig.ctl.cancelAction("g1");
    expect(rows.slice(-1)[0]).toMatchObject({ actionId: "g1", phase: "rejected", replaced: false });
    expect(rig.q(".ew-amsg > .ew-propcard .ew-res")!.textContent).toBe("未做更改");
    rig.dispose();
  });

  it("startGoalChat: the conversation without a page, afresh, the text as its first message (intent learn_goal with the form's goal); its key at once; held against the note re-reported", async () => {
    const rig = cardRig();                                   // shows PAGE
    await rig.ask([], "之前的问题");
    const chat = rig.ctl.startGoalChat("我要学黑壳虾\n背景：学过高中生物", { goal: "我要学黑壳虾", background: "学过高中生物" })!;
    expect(chat.key).toBe(rig.ctl.state.conv!.key);
    expect(rig.ctl.state.conv!.target).toEqual({ kind: "free" });
    expect(await chat.done).toBe(true);
    expect(rig.api.calls.sendMessage.slice(-1)[0]!.body).toEqual({ text: "我要学黑壳虾\n背景：学过高中生物", intent: "learn_goal", goal: "我要学黑壳虾" });
    expect(rig.ctl.state.conv!.turns.map((t) => t.question)).toEqual(["我要学黑壳虾\n背景：学过高中生物"]);
    rig.ctl.show({ kind: "page", pageId: "page_pca", title: "主成分分析" });          // the panel re-reports the open note
    expect(rig.ctl.state.conv!.target).toEqual({ kind: "free" });
    rig.dispose();
  });

  it("startGoalChat while the free conversation is still being restored (right after start): starts anew, the late read is dropped", async () => {
    const api = new FakeAssistantApi();
    let release!: () => void;
    const gate = new Promise<void>((r) => { release = r; });
    const real = api.getSession.bind(api);
    api.getSession = async (id: string) => { await gate; return real(id); };
    api.sessionOuts.set("s-old", { gone_sources: [], session_id: "s-old", page_id: null, note_concept_id: null, messages: [
      { message_id: "m0", role: "user", text: "以前的问题", tool_calls: [], sources: [], created_at: "2026-10-22T09:00:00Z" }] } as unknown as SessionOut);
    const saved: (string | null)[] = [];
    const store: AssistantSessionStore = { get: (k) => (k.includes("free") ? "s-old" : null), set: (_k, v) => { saved.push(v); return Promise.resolve(); } };
    let n = 0;
    const ctl = new AssistantController({ api, store, identity: () => ID, newId: () => `t${++n}` });
    api.messageReplies.push({ events: [FIVE, { type: "done", message_id: "m1", tool_calls: 1, llm_calls: 1, cost_usd: null, attachments: [], session_attachments: [], stopped: false, confirmed: false }] });
    const Q = "我要学黑壳虾\n背景：学过高中生物";
    const chat = ctl.startGoalChat(Q, { goal: "我要学黑壳虾", background: "学过高中生物" });
    expect(chat).not.toBeNull();
    expect(ctl.state.conv!.turns.map((t) => t.question)).toEqual([Q]);
    expect(await chat!.done).toBe(true);
    release(); await new Promise((r) => setTimeout(r, 0));
    expect(ctl.state.conv!.turns.map((t) => t.question)).toEqual([Q]);
    expect(saved).toContain(null);                       // the stored session id was forgotten
    expect(api.calls.sendMessage).toHaveLength(1);
    ctl.dispose();
  });

  it("startGoalChat before the panel reported any note: its first report (the note behind 学点新的) keeps the goal chat; another note is followed", async () => {
    const api = new FakeAssistantApi();
    const store: AssistantSessionStore = { get: () => null, set: () => Promise.resolve() };
    let n = 0;
    const ctl = new AssistantController({ api, store, identity: () => ID, newId: () => `t${++n}` });
    api.messageReplies.push({ events: [FIVE, { type: "done", message_id: "m1", tool_calls: 1, llm_calls: 1, cost_usd: null, attachments: [], session_attachments: [], stopped: false, confirmed: false }] });
    const chat = ctl.startGoalChat("我要学黑壳虾", { goal: "我要学黑壳虾", background: "" })!;
    expect(await chat.done).toBe(true);
    ctl.show({ kind: "page", pageId: "p2", title: "pca（2/4）" });       // the 助手 tab opens: the panel's first report
    expect(ctl.state.conv!.target).toEqual({ kind: "free" });
    expect(ctl.state.conv!.turns.map((t) => t.question)).toEqual(["我要学黑壳虾"]);
    ctl.show({ kind: "page", pageId: "p2", title: "pca（2/4）" });       // re-reported: still held
    expect(ctl.state.conv!.target).toEqual({ kind: "free" });
    ctl.show({ kind: "page", pageId: "page_pca", title: "主成分分析" });  // the learner opens another note
    expect(ctl.state.conv!.target).toEqual({ kind: "page", pageId: "page_pca", title: "主成分分析" });
    ctl.dispose();
  });

  it("goalResultOf: the row's result only (ids in content are not read); null without either id", () => {
    expect(goalResultOf({ ...row("a", "executed"), result: { draft_id: "d", map_id: null } })).toEqual({ draftId: "d", mapId: null });
    expect(goalResultOf(row("a", "executed", { content: { map_id: "m" } }))).toBeNull();
    expect(goalResultOf(row("a", "executed"))).toBeNull();
  });
});

describe("a goal confirmed in the chat goes on in the chat (D3a)", () => {
  async function goalsOf() {
    const { FakeGoalApi, fakeBlocks, registerBlockGoal } = await import("../wiki/learn/goal-fake");
    const api = new FakeGoalApi({ roundMs: 0, splitMs: 0 });
    const out = await api.getDraft(registerBlockGoal("我要学黑壳虾", fakeBlocks("我要学黑壳虾")).draftId);   // confirmed in the chat
    const goals: NonNullable<AssistantDeps["goals"]> = { api: () => api, writeFirstPage: () => Promise.resolve() };
    return { api, out, goals };
  }
  const settle = async (): Promise<void> => { for (let i = 0; i < 60; i++) await new Promise((r) => setImmediate(r)); await new Promise((r) => setTimeout(r, 0)); };

  it("确认 of a chat goal: the diagnosis shows under the card, one question at a time, with 跳过", async () => {
    const { out, goals: g } = await goalsOf();
    const changed = vi.fn();                       // 今天 reads /wiki/progress again (the goal exists, its round shows)
    const goals = { ...g, changed };
    const rig = cardRig({ goals });
    await rig.ask([FIVE], "帮我整理这一篇");
    rig.api.actionReplies.push(row("g1", "executed", { result: { draft_id: out.draft_id, map_id: out.map_id } }));
    await rig.ctl.confirmAction("g1");
    await settle();
    const run = rig.q(".ew-goalrun")!;
    expect(run).not.toBeNull();
    expect(run.querySelector(".ew-rv-src")!.textContent).toMatch(/^第 1 \/ \d+ 题$/);
    expect([...run.querySelectorAll("button")].map((b) => b.textContent)).toContain("跳过，直接开始");
    expect(changed).toHaveBeenCalled();
    rig.dispose();
  });

  it("确认 while a turn of that conversation still runs: the diagnosis starts when it ends, it is not dropped", async () => {
    const { api, out, goals } = await goalsOf();
    const real = api.getDraft.bind(api);
    vi.spyOn(api, "getDraft").mockImplementation(async (id: string) => ({ ...(await real(id)), questions: [] }));
    const rig = cardRig({ goals });
    await rig.ask([FIVE], "帮我整理这一篇");
    const send = rig.api.sendMessage.bind(rig.api);
    let release = (): void => {};
    const held = new Promise<void>((r) => { release = r; });
    vi.spyOn(rig.api, "sendMessage").mockImplementationOnce(async (...args) => { await held; return send(...args); });
    rig.ctl.setDraft("还在问");
    const talking = rig.ctl.send();
    await settle();
    rig.api.actionReplies.push(row("g1", "executed", { result: { draft_id: out.draft_id, map_id: out.map_id } }));
    await rig.ctl.confirmAction("g1");
    await settle();
    const intents = (): (string | undefined)[] => rig.api.calls.sendMessage.map((c) => c.body.intent);
    expect(intents()).not.toContain("diagnose");
    release();
    await talking;
    await settle();
    expect(intents()).toContain("diagnose");
    rig.dispose();
  });

  it("two goals open in one conversation: each 确认 sends its own card's draft, not the newest", async () => {
    const { goals } = await goalsOf();
    const api = goals.api();
    const { fakeBlocks, registerBlockGoal } = await import("../wiki/learn/goal-fake");
    const older = await api.getDraft(registerBlockGoal("我要学孔雀鱼", fakeBlocks("我要学孔雀鱼")).draftId);
    const newer = await api.getDraft(registerBlockGoal("我要学斗鱼", fakeBlocks("我要学斗鱼")).draftId);
    expect(older.draft_id).not.toBe(newer.draft_id);
    const real = api.getDraft.bind(api);
    vi.spyOn(api, "getDraft").mockImplementation(async (id: string) => ({ ...(await real(id)), questions: [] }));
    const rig = cardRig({ goals });
    await rig.ask([blocks("gA", [["a", "认识孔雀鱼", "它是什么鱼"]])], "我要学孔雀鱼");
    await rig.ask([blocks("gB", [["b", "认识斗鱼", "它是什么鱼"]])], "我要学斗鱼");
    const sentDrafts = (): (string | null | undefined)[] =>
      rig.api.calls.sendMessage.filter((c) => c.body.intent === "diagnose").map((c) => c.body.draft_id);
    rig.api.actionReplies.push(row("gA", "executed", { result: { draft_id: older.draft_id, map_id: older.map_id } }));
    await rig.ctl.confirmAction("gA");
    await settle();
    expect(sentDrafts()).toEqual([older.draft_id]);
    rig.api.actionReplies.push(row("gB", "executed", { result: { draft_id: newer.draft_id, map_id: newer.map_id } }));
    await rig.ctl.confirmAction("gB");
    await settle();
    expect(sentDrafts()).toEqual([older.draft_id, newer.draft_id]);
    rig.dispose();
  });

  it("while its first page is written its card offers 停止 (the wiki's stop); after it the card reads 已停止 · 重新写", async () => {
    const { api, out } = await goalsOf();
    Object.defineProperty(api, "live", { value: true });
    const readPath = api.getPath.bind(api);                     // the path's first page has a page id once its write began
    vi.spyOn(api, "getPath").mockImplementation(async (id: string) => {
      const p = await readPath(id);
      return wiki.made ? { ...p, pages: p.pages.map((x, i) => (i === 0 ? { ...x, page_id: "pg1" } : x)) } : p;
    });
    // the wiki controller, as main.ts wires it: a write that runs until 停止
    const wiki = { made: false, writing: null as string | null, stopped: null as string | null, end: () => {}, restarted: [] as string[] };
    const goals = {
      api: () => api,
      writeFirstPage: (_t: unknown, onPage?: (id: string) => void) => new Promise<void>((r) => { wiki.made = true; wiki.writing = "pg1"; onPage?.("pg1"); wiki.end = r; }),
      stopPage: vi.fn(() => { wiki.stopped = wiki.writing; wiki.writing = null; wiki.end(); return Promise.resolve(true); }),
      stoppedWrite: () => wiki.stopped,
      restartWrite: (id: string) => { wiki.restarted.push(id); wiki.stopped = null; return Promise.resolve(); },
    };
    const rig = cardRig({ goals });
    await rig.ask([FIVE], "帮我整理这一篇");
    rig.api.actionReplies.push(row("g1", "executed", { result: { draft_id: out.draft_id, map_id: out.map_id } }));
    await rig.ctl.confirmAction("g1");
    await settle();
    rig.ctl.skipGoal("g1");
    await settle(); await settle();
    expect(wiki.writing).toBe("pg1");
    expect(rig.q(".ew-goalrun [data-action=stop]")).not.toBeNull();         // the page write's own stop, on its card
    rig.ctl.stopGoal("g1");
    await settle();
    expect(goals.stopPage).toHaveBeenCalledWith("pg1");
    const run = rig.q(".ew-goalrun")!;
    expect(run.textContent).toBe("已停止重新写");
    run.querySelector<HTMLButtonElement>("[data-action=restart]")!.click();
    await settle();
    expect(wiki.restarted).toEqual(["pg1"]);
    expect(rig.q(".ew-goalrun .ew-s")!.textContent).toBe("页面已生成");
    rig.dispose();
  });

  it("开始's goal goes on in the chat too (one flow): no blocks card and no run card there (学点新的 shows both)", async () => {
    const { out, goals } = await goalsOf();
    const rig = cardRig({ goals });
    rig.api.messageReplies.push({ events: [FIVE, { type: "done", message_id: "m1", tool_calls: 1, llm_calls: 1, cost_usd: null, attachments: [], session_attachments: [], stopped: false, confirmed: false }] });
    const chat = rig.ctl.startGoalChat("我要学黑壳虾", { goal: "我要学黑壳虾", background: "" })!;
    await chat.done;
    rig.api.actionReplies.push(row("g1", "executed", { result: { draft_id: out.draft_id, map_id: out.map_id } }));
    await rig.ctl.confirmAction("g1");
    await settle();
    expect(rig.ctl.goalRun("g1")).toBeDefined();
    expect(rig.q(".ew-goalrun")).toBeNull();
    expect(rig.q(".ew-propcard")).toBeNull();
    rig.dispose();
  });

  /** A store over a plain record (the plugin's data): what main.ts gives as `store` / `goals.kept`. */
  const memStore = (rec: Record<string, string> = {}): AssistantSessionStore & { rec: Record<string, string> } => ({
    rec, get: (k) => rec[k] ?? null, set: (k, v) => { if (v === null) delete rec[k]; else rec[k] = v; return Promise.resolve(); } });

  async function confirmed(goals: Awaited<ReturnType<typeof goalsOf>>["goals"] & { kept?: AssistantSessionStore },
    out: Awaited<ReturnType<typeof goalsOf>>["out"], extra: Parameters<typeof cardRig>[0] = {}) {
    const rig = cardRig({ goals, ...extra });
    await rig.ask([FIVE], "帮我整理这一篇");
    rig.api.actionReplies.push(row("g1", "executed", { result: { draft_id: out.draft_id, map_id: out.map_id } }));
    await rig.ctl.confirmAction("g1");
    await settle();
    return rig;
  }

  it("after 确认 the goal is the goal's own name, not the card's title 「黑壳虾」分成 5 块 (the cloud's title, a page's goal)", async () => {
    const { out, goals } = await goalsOf();
    const rig = await confirmed(goals, out);
    const { topic, goal } = rig.ctl.goalRun("g1")!.flow.state;
    expect(topic).toBe("");
    expect(goal).not.toBe("");
    expect(goal).not.toContain("分成");
    rig.dispose();
  });

  it("typing 跳过 while the diagnosis waits skips it as the card's control does: no message to the model", async () => {
    const { api, out, goals } = await goalsOf();
    const skip = vi.spyOn(api, "answer");
    const rig = await confirmed(goals, out);
    const sent = rig.api.calls.sendMessage.length;
    rig.ctl.setDraft("跳过。");
    await rig.ctl.send(); await settle();
    expect(rig.api.calls.sendMessage.length).toBe(sent);
    expect(skip).toHaveBeenCalledWith(out.draft_id, { skip: true });
    expect(rig.ctl.state.conv!.draft).toBe("");
    expect(rig.ctl.state.conv!.turns).toHaveLength(1);              // no turn of its own: the card says what happened
    // nothing waits any more: 跳过 is an ordinary message again; other words never skip
    expect(rig.ctl.goalRun("g1")!.waiting).toBe(false);
    rig.ctl.setDraft("直接开始");
    await rig.ctl.send();
    expect(rig.api.calls.sendMessage.length).toBe(sent + 1);
    rig.dispose();
  });

  it("free chat while the diagnosis waits is unchanged: any other text goes to the model, the round stays", async () => {
    const { api, out, goals } = await goalsOf();
    const skip = vi.spyOn(api, "answer");
    const rig = await confirmed(goals, out);
    const sent = rig.api.calls.sendMessage.length;
    rig.ctl.setDraft("跳过这个问题之前，先问一下 pH 是什么");
    await rig.ctl.send(); await settle();
    expect(rig.api.calls.sendMessage.length).toBe(sent + 1);
    expect(skip).not.toHaveBeenCalled();
    expect(rig.ctl.goalRun("g1")!.waiting).toBe(true);
    rig.dispose();
  });

  it("after a plugin reload the waiting diagnosis comes back under its card, kept here or not (rebuilt from the server: D03)", async () => {
    const { out, goals } = await goalsOf();
    const progress = memStore();
    const first = await confirmed({ ...goals, kept: progress }, out);
    expect(Object.keys(progress.rec)).toEqual([JSON.stringify([ID.server, ID.user, "g1"])]);
    expect(JSON.parse(Object.values(progress.rec)[0]!)).toMatchObject({ draftId: out.draft_id });
    first.dispose();
    // a new controller (the reload) reads the stored session back: the card carries the row's result
    const card = { action_id: "g1", kind: "goal_new", goal_title: "黑壳虾", title: "「黑壳虾」分成 5 块", status: "executed", confirm: true, can_undo: true, detail: "", replaces: "",
      result: { draft_id: out.draft_id, map_id: out.map_id }, items: FIVE.items, page_id: null, section_id: null };
    const session: SessionOut = { gone_sources: [], session_id: "ses_g", page_id: null, recap: null, decisions: [], proposals: [], actions: [card], messages: [
      { message_id: "m1", role: "user", card_at: null, stopped: false, intent: null, draft_id: null, text: "帮我整理这一篇", sources: [], proposals: [], actions: [], selection: null, tools: [], attachments: [], rating: null, asks: [] },
      { message_id: "m2", role: "assistant", card_at: null, stopped: false, intent: null, draft_id: null, text: "分成 5 块", sources: [], proposals: [], actions: ["g1"], selection: null, tools: [], attachments: [], rating: null, asks: [] }] };
    for (const [kept, shown] of [[progress, true], [memStore(), true]] as const) {
      const api = new FakeAssistantApi();
      api.sessionOuts.set("ses_g", session);
      const rig = cardRig({ goals: { ...goals, kept }, api, store: memStore({ [sessionKeyOf()]: "ses_g" }) });
      await settle();
      expect(rig.q(".ew-goalrun") !== null).toBe(shown);
      if (shown) expect(rig.q(".ew-goalrun .ew-rv-src")!.textContent).toMatch(/^第 1 \/ \d+ 题$/);
      rig.dispose();
    }
  });

  it("after a reload, a conversation 学点新的 started (its message stored with intent learn_goal) shows its waiting draft in the chat: nothing else shows it now", async () => {
    const card = { action_id: "g1", kind: "goal_new", goal_title: "黑壳虾", title: "「黑壳虾」分成 5 块", status: "proposed", confirm: true, can_undo: false, detail: "", replaces: "",
      result: {}, items: FIVE.items, page_id: null, section_id: null };
    const msgs = (intent: "learn_goal" | null): SessionOut => ({ gone_sources: [], session_id: "ses_l", page_id: null, recap: null, decisions: [], proposals: [], actions: [card], messages: [
      { draft_id: null, message_id: "m1", role: "user", card_at: null, stopped: false, intent, text: "我要学黑壳虾", sources: [], proposals: [], actions: [], selection: null, tools: [], attachments: [], rating: null, asks: [] },
      { message_id: "m2", role: "assistant", card_at: null, stopped: false, intent: null, draft_id: null, text: "已分成 5 个学习主题，可以在左侧修改，改好后点「确认」。", sources: [], proposals: [], actions: ["g1"], selection: null, tools: [], attachments: [], rating: null, asks: [] }] });
    for (const [intent, shown] of [["learn_goal", true], [null, true]] as const) {
      const api = new FakeAssistantApi();
      api.sessionOuts.set("ses_l", msgs(intent));
      const rig = cardRig({ api, store: memStore({ [sessionKeyOf()]: "ses_l" }) });
      await settle();
      expect(rig.q(".ew-amsg")).not.toBeNull();
      expect(rig.q(".ew-propcard") !== null).toBe(shown);
      rig.dispose();
    }
  });

  it("a run that learns its map (its opening diagnosis closed) is kept with it: a reload finds it in GET /wiki/progress", async () => {
    const { out, goals } = await goalsOf();
    const kept = memStore();
    const rig = await confirmed({ ...goals, kept }, out);
    rig.ctl.skipGoal("g1"); await settle();
    const mapId = rig.ctl.goalRun("g1")!.mapId;
    expect(mapId).not.toBeNull();
    expect(JSON.parse(Object.values(kept.rec)[0]!)).toMatchObject({ mapId });
    rig.dispose();
  });

  it("after a reload 学点新的 resumes the goal GET /wiki/progress names (today), not just the newest kept one", async () => {
    const { api, goals } = await goalsOf();
    Object.defineProperty(api, "live", { value: true });
    const kept = memStore({
      [JSON.stringify([ID.server, ID.user, "g_a"])]: JSON.stringify({ draftId: null, mapId: "m_a", title: "目标甲" }),
      [JSON.stringify([ID.server, ID.user, "g_b"])]: JSON.stringify({ draftId: null, mapId: "m_b", title: "目标乙" }),
    });
    const today = { kind: "enter_block", goal_id: "m_a", goal: "目标甲", map_id: "m_a", region_id: "r1", region_name: "", path_id: null, index: null,
      page_id: null, title: "", draft_id: null, progress: null, failure: null, review_first: null } as const;
    const progress = vi.fn(() => Promise.resolve({ today, goals: [] }));
    Object.assign(api, { progress });
    const rig = cardRig({ goals: { ...goals, kept: Object.assign(kept, { keys: () => Object.keys(kept.rec) }) } });
    expect(rig.ctl.currentGoal()).toBeNull();                      // nothing driven here yet
    const run = await rig.ctl.resumeGoal();
    expect(run?.actionId).toBe("g_a");
    expect(rig.ctl.currentGoal()).toBe(run);
    expect(await rig.ctl.resumeGoal()).toBe(run);                  // the current one from then on
    rig.dispose();
  });

  it("after a reload with no goal kept here (started elsewhere) 学点新的 still resumes the goal progress names, cardless", async () => {
    const { api, goals } = await goalsOf();
    Object.defineProperty(api, "live", { value: true });
    const today = { kind: "read", goal_id: "m_x", goal: "复杂网络中的渗流", map_id: "m_x", region_id: "r1", region_name: "", path_id: null, index: 1,
      page_id: "pg1", title: "节点与边", draft_id: null, progress: null, failure: null, review_first: null } as const;
    Object.assign(api, { progress: vi.fn(() => Promise.resolve({ today, goals: [] })) });
    const rig = cardRig({ goals: { ...goals, kept: Object.assign(memStore(), { keys: () => [] }) } });
    const run = await rig.ctl.resumeGoal();
    expect(run?.actionId).toBe("map:m_x");
    expect(run?.mapId).toBe("m_x");
    expect(rig.ctl.currentGoal()).toBe(run);
    rig.dispose();
  });

  it("opens a specifically selected saved goal after restart, independent of today's goal, without generation", async () => {
    const { api, goals } = await goalsOf();
    const getMap = vi.spyOn(api, "getMap").mockImplementation((mapId) => Promise.resolve({
      map_id: mapId, goal: `Goal ${mapId}`, title: mapId, next_region: null, reading: null, nodes: [],
    }));
    const enter = vi.spyOn(api, "enterRegion"), next = vi.spyOn(api, "pathNext");
    const write = vi.fn(() => Promise.resolve());
    for (let restart = 0; restart < 2; restart++) {
      const rig = cardRig({ goals: { ...goals, writeFirstPage: write } });
      const selected = await rig.ctl.openGoal("saved-map");
      expect(selected?.flow.state.map?.map_id).toBe("saved-map");
      expect(rig.ctl.currentGoal()).toBe(selected);
      expect(await rig.ctl.openGoal("saved-map")).toBe(selected);
      expect((await rig.ctl.openGoal("other-map"))?.mapId).toBe("other-map");
      expect(await rig.ctl.openGoal("saved-map")).toBe(selected);
      expect(rig.api.calls.sendMessage).toHaveLength(0);
      rig.dispose();
    }
    expect(getMap).toHaveBeenCalledWith("saved-map");
    expect(enter).not.toHaveBeenCalled(); expect(next).not.toHaveBeenCalled(); expect(write).not.toHaveBeenCalled();
  });

  it("a goal's next block (a block's last page, 今天): the 助手 goes back to the goal's conversation and stays while the page is open", async () => {
    const { out, goals } = await goalsOf();
    const rig = await confirmed(goals, out);
    const key = rig.ctl.state.conv!.key;
    rig.ctl.skipGoal("g1"); await settle();                                      // the diagnosis ends: the goal has its map
    const mapId = rig.ctl.goalRun("g1")!.mapId!;
    rig.ctl.show({ kind: "page", pageId: "pg_last", title: "分公母" });          // the block's last page is read
    expect(rig.ctl.state.conv!.key).not.toBe(key);
    expect(rig.ctl.continueGoal(mapId)).toBe(true);
    expect(rig.ctl.state.conv!.key).toBe(key);
    rig.ctl.show({ kind: "page", pageId: "pg_last", title: "分公母" });          // the panel re-reports the open page
    expect(rig.ctl.state.conv!.key).toBe(key);
    expect(rig.ctl.continueGoal("map-other")).toBe(false);
    rig.dispose();
  });

  describe("after a reload the goal knows its conversation (user decision 13)", () => {
    const KEY = JSON.stringify([ID.server, ID.user, "g1"]);
    const MOLINO = { kind: "page", pageId: "pg_grinder", title: "磨豆机" } as const;
    const storedGoal = (out: { draft_id: string | null; map_id: string | null }): SessionOut => ({
      gone_sources: [], session_id: "ses_g", page_id: null, recap: null, decisions: [], proposals: [],
      actions: [{ action_id: "g1", kind: "goal_new", goal_title: "手冲咖啡", title: "「手冲咖啡」分成 5 块", status: "executed", confirm: true, can_undo: true, detail: "", replaces: "",
        result: { draft_id: out.draft_id, map_id: out.map_id }, items: FIVE.items, page_id: null, section_id: null }],
      messages: [
        { message_id: "m1", role: "user", card_at: null, stopped: false, intent: null, draft_id: null, text: "我要学手冲咖啡", sources: [], proposals: [], actions: [], selection: null, tools: [], attachments: [], rating: null, asks: [] },
        { message_id: "m2", role: "assistant", card_at: null, stopped: false, intent: null, draft_id: null, text: "分成 5 块", sources: [], proposals: [], actions: ["g1"], selection: null, tools: [], attachments: [], rating: null, asks: [] }] });

    /** The reload: a new controller over what was stored, 磨豆机 the open note, the goal in GET /wiki/progress. */
    async function reloaded(kept: Record<string, unknown>, api: FakeAssistantApi) {
      const { api: goalApi, out, goals } = await goalsOf();
      const rec = { [KEY]: JSON.stringify({ draftId: out.draft_id, mapId: "m1", ...kept }) };
      Object.assign(goalApi, { progress: () => Promise.resolve({ today: { kind: "read", map_id: "m1", draft_id: null }, goals: [] }) });
      const rig = cardRig({ goals: { ...goals, kept: { ...memStore(rec), keys: () => Object.keys(rec) } }, api, store: memStore() });
      rig.ctl.show(MOLINO);
      await settle();
      return { rig, out };
    }

    it("confirming a goal keeps its session with it", async () => {
      const { out, goals } = await goalsOf();
      const kept = memStore();
      const rig = await confirmed({ ...goals, kept }, out);
      expect(JSON.parse(Object.values(kept.rec)[0]!)).toMatchObject({ sessionId: rig.ctl.state.conv!.sessionId, target: rig.ctl.state.conv!.target });
      rig.dispose();
    });

    it("opening the goal: the 助手 is that conversation, not the empty chat of the note that is open; re-reported, it stays", async () => {
      const api = new FakeAssistantApi();
      const { out } = await goalsOf();
      api.sessionOuts.set("ses_g", storedGoal(out));
      const { rig } = await reloaded({ sessionId: "ses_g", target: { kind: "free" } }, api);
      expect(rig.ctl.state.conv!.turns.length).toBe(0);            // 磨豆机's own, as before opening the goal
      const run = await rig.ctl.resumeGoal();
      expect(rig.ctl.state.conv!.turns.length).toBe(0);            // a tab rebuilt behind the note leaves his chat alone
      rig.ctl.showGoalChat(run!.actionId); await settle();          // 学点新的 in front
      expect(rig.ctl.state.conv!.sessionId).toBe("ses_g");
      expect(rig.ctl.state.conv!.turns.length).toBe(1);
      rig.ctl.show(MOLINO);
      expect(rig.ctl.state.conv!.sessionId).toBe("ses_g");
      rig.ctl.followNotes();                                        // back to the note: its own chat
      rig.ctl.show(MOLINO);
      expect(rig.ctl.state.conv!.sessionId).not.toBe("ses_g");
      rig.dispose();
    });

    it("a run kept before sessions were: found in the history once, then kept", async () => {
      const api = new FakeAssistantApi();
      const { out } = await goalsOf();
      api.sessionOuts.set("ses_g", storedGoal(out));
      api.sessionOuts.set("ses_other", { ...storedGoal(out), session_id: "ses_other", actions: [] });
      api.sessionList = [
        { session_id: "ses_other", title: "别的", page_id: null, page_title: "", note_concept_id: null, created_at: "2026-11-04T00:00:00Z", last_activity_at: "2026-11-04T00:00:00Z", message_count: 2 },
        { session_id: "ses_g", title: "手冲咖啡", page_id: null, page_title: "", note_concept_id: null, created_at: "2026-11-03T00:00:00Z", last_activity_at: "2026-11-03T00:00:00Z", message_count: 2 }];
      const { rig } = await reloaded({}, api);
      rig.ctl.showGoalChat((await rig.ctl.resumeGoal())!.actionId); await settle();
      expect(rig.ctl.state.conv!.sessionId).toBe("ses_g");
      rig.dispose();
    });

    it("its conversation deleted: a diagnosis goes out in a new conversation and that is kept, never nowhere", async () => {
      const api = new FakeAssistantApi();
      const { rig } = await reloaded({ sessionId: "ses_deleted", target: { kind: "free" } }, api);       // getSession: 404
      await rig.ctl.resumeGoal(); await settle();
      const before = api.calls.sendMessage.length;
      await (rig.ctl as unknown as { diagnoseIn(id: string, draft: string): Promise<void> }).diagnoseIn("g1", "dr_block");
      await settle();
      expect(api.calls.sendMessage.length).toBe(before + 1);
      expect(rig.ctl.state.conv!.sessionId).not.toBe("ses_deleted");
      expect(rig.ctl.state.conv!.turns[0]!.diagnose).toBe("dr_block");
      expect(rig.ctl.state.conv!.turns[0]!.question).toBe("继续摸底");    // no card here that he could have confirmed
      rig.dispose();
    });

    it("its conversation unreadable (offline): nothing is replaced, nothing sent, the run gets the failure", async () => {
      const api = new FakeAssistantApi();
      api.sessionOuts.set("ses_g", { throws: new OfflineError() });
      const { rig } = await reloaded({ sessionId: "ses_g", target: { kind: "free" } }, api);
      await expect((rig.ctl as unknown as { diagnoseIn(id: string, draft: string): Promise<void> }).diagnoseIn("g1", "dr_block")).rejects.toThrow();
      expect(api.calls.sendMessage.length).toBe(0);
      rig.dispose();
    });
  });

  it("the first page opening keeps the 助手 on the goal's conversation, where its link is; another note is followed", async () => {
    const { api, out, goals } = await goalsOf();
    Object.defineProperty(api, "live", { value: true });          // 生成第一页 goes through the wiki controller (writeFirstPage)
    let opened: ((id: string) => void) | null = null;
    const write = (_t: FirstPage, onPage?: (id: string) => void): Promise<void> => { opened = onPage ?? null; onPage?.("pg_first"); return Promise.resolve(); };
    const rig = await confirmed({ ...goals, writeFirstPage: write }, out);
    const key = rig.ctl.state.conv!.key;
    rig.ctl.skipGoal("g1"); await settle();
    expect(opened).not.toBeNull();
    rig.ctl.show({ kind: "page", pageId: "pg_first", title: "水温和 pH" });
    rig.ctl.show({ kind: "page", pageId: "pg_first", title: "水温和 pH（已改名）" });       // re-reported (renamed): still held
    expect(rig.ctl.state.conv!.key).toBe(key);
    rig.ctl.show({ kind: "page", pageId: "pg_other", title: "别的页" });
    expect(rig.ctl.state.conv!.key).not.toBe(key);
    rig.ctl.show({ kind: "page", pageId: "pg_first", title: "水温和 pH" });               // the hold ended: its own conversation
    expect(rig.ctl.state.conv!.key).not.toBe(key);
    rig.dispose();
  });
});
