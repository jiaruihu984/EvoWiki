import { describe, expect, it } from "vitest";
import type { AssistantEvent } from "../assistant/types";
import { createDemoFixture } from "./fixtures-demo";
import { demoFlowOf } from "./fixtures-demo-asst";

const send = async (text: string): Promise<AssistantEvent[]> => {
  const fx = createDemoFixture();
  const ev: AssistantEvent[] = [];
  await fx.assistant.sendMessage("s1", { text }, (e) => ev.push(e));
  return ev;
};

describe("the demo fixture's assistant flows (asst.js replyFor)", () => {
  it("routes the five starters like asst.js", () => {
    expect(["这一页太笼统了，帮我拆解一下", "这个路径不合理，帮我改一下", "考我几道 PCA 的题", "把『协方差』和『方差』那两页合成一页", "以后都先给例子再讲公式", "为什么"]
      .map(demoFlowOf)).toEqual(["split", "path", "review", "merge", "rule", null]);
  });

  it("split: tools, the paragraph, a draft card of three pages; the row also waits in 参数 (等你确认)", async () => {
    const fx = createDemoFixture();
    const ev: AssistantEvent[] = [];
    await fx.assistant.sendMessage("s1", { text: "这一页太笼统了，帮我拆解一下" }, (e) => ev.push(e));
    const card = ev.find((e) => e.type === "action");
    expect(card).toMatchObject({ kind: "page_split", title: "把「特征值与特征向量」拆成 3 页", status: "proposed", confirm: true });
    expect(card?.type === "action" ? card.items?.map((i) => i.title) : []).toEqual(["特征向量", "特征值", "求特征值"]);
    expect((await fx.wiki.botActions("proposed")).find((a) => a.action_id === "act_asst_split")).toMatchObject({ confirm: true, source: "assistant" });
    // 确认 with an edited draft: the pages in the draft's order take path places 2, 3, …
    const out = await fx.assistant.approveAction("act_asst_split", { pages: [{ key: "q3" }, { key: "q1" }, { title: "举个例子再说", isNew: true }] });
    expect(out.status).toBe("executed");
    expect(out.made?.map((m) => [m.key, m.n])).toEqual([["q3", 2], ["q1", 3], ["new2", 4]]);
    expect((await fx.wiki.botActions()).find((a) => a.action_id === "act_asst_split")?.status).toBe("executed");
  });

  it("考考我 opens the review card: open_review's row and one sentence, nothing to answer in the chat", async () => {
    const ev = await send("考考我");
    expect(ev.filter((e) => e.type === "tool_end" && e.ok).length).toBe(1);
    expect(ev.find((e) => e.type === "tool_start")).toMatchObject({ name: "open_review" });
  });

  it("any other question gets the demo's own answer", async () => {
    expect((await send("为什么是 [0, 3]？")).some((e) => e.type === "proposal")).toBe(true);
  });
});

describe("the demo fixture's 学点新的 blocks (app.js startBlocks / blocksReply, #blocks)", () => {
  const text = (ev: AssistantEvent[]): string => ev.flatMap((e) => (e.type === "text_delta" ? [e.text] : [])).join("");
  const card = (ev: AssistantEvent[]): Extract<AssistantEvent, { type: "action" }> | undefined =>
    ev.find((e): e is Extract<AssistantEvent, { type: "action" }> => e.type === "action");

  it("开始 → 6 blocks with levels: framing + caveat, the card, then main line / start points / the one question; 水质和开缸合成一块 → 5 (the old row replaced); 繁殖先不学 → 4; 确认 opens the goal the fake answers; 撤销 closes it", async () => {
    const { FakeGoalApi } = await import("./learn/goal-fake");
    const fx = createDemoFixture();
    const go = async (t: string): Promise<AssistantEvent[]> => { const ev: AssistantEvent[] = []; await fx.assistant.sendMessage("s1", { text: t }, (e) => ev.push(e)); return ev; };
    const e1 = await go("我要学黑壳虾\n背景：学过高中生物，想知道黑壳虾是什么、怎么养。");
    // app.js startBlocks: 搜了网页 / 读了你的背景 / 看了你的 Wiki, then FIRST_REPLY's paras, the card, its tail (stream order)
    expect(e1.filter((e) => e.type === "tool_start").map((e) => (e.type === "tool_start" ? e.name : ""))).toEqual(["web_search", "read_goals", "search_wiki"]);
    expect(e1.filter((e) => e.type === "text_delta" || e.type === "action").map((e) => e.type)).toEqual(["text_delta", "action", "text_delta"]);
    const paras = "这份大纲不是一份养虾教程，而是先把它认清楚，再把它养活，最后能自己判断出了什么问题。\n\n"
      + "先提醒一句：「黑壳虾」是水族店里的俗称，一般指新米虾属（Neocaridina）的一种，常和别的米虾混着卖，所以单开了「分类与鉴别」，放在最前面。";
    const tail = "\n\n主线是 分类与鉴别 → 水质与环境 → 开缸与日常，「营养与摄食」可以和开缸一起学，「生长与繁殖」「疾病与风险」留到虾养稳以后。\n\n"
      + "建议先从「硝化」「开缸」「过水」「喂多少」这四个点开始。\n\n"
      + "你是想自己把虾养好，还是把它当成一种生物来研究？前一种会把操作排前面，后一种会多讲分类和生理。";
    expect(text(e1)).toBe(paras + tail);
    expect(e1.filter((e) => e.type === "text_delta").map((e) => (e.type === "text_delta" ? e.at : -1))).toEqual([null, paras.length]);
    expect([card(e1)!.kind, card(e1)!.title, card(e1)!.items.map((i) => [i.title, i.level])]).toEqual(["goal_new", "为「黑壳虾」拆解学习路径",
      [["分类与鉴别", "基础"], ["水质与环境", "基础"], ["开缸与日常", "核心"], ["营养与摄食", "核心"], ["生长与繁殖", "进阶"], ["疾病与风险", "进阶"]]]);
    expect((fx.assistant.actionRows.get(card(e1)!.action_id)!.content.pages as { level: string }[]).map((x) => x.level)).toEqual(["基础", "基础", "核心", "核心", "进阶", "进阶"]);
    const e2 = await go("水质和开缸合成一块");
    expect(text(e2)).toBe("可以，「水质与环境」并进了「开缸与日常」，原来的水温、pH 和 GH / KH、氨氮和亚硝酸盐都在这一块里，排在前面讲。");
    expect(card(e2)!.items.map((i) => i.title)).toEqual(["分类与鉴别", "开缸与日常", "营养与摄食", "生长与繁殖", "疾病与风险"]);
    expect(card(e2)!.items[1]!.covers).toBe("水温、pH 和 GH / KH、氨氮和亚硝酸盐、硝化、溶氧、缸和密度、过滤和防吸虾、开缸、过水、第一次换水");
    expect(card(e2)!.items[1]!.level).toBe("核心");
    expect(fx.assistant.actionRows.get(card(e1)!.action_id)).toMatchObject({ status: "rejected", detail: "replaced" });
    expect(card(e2)!.replaces).toBe(card(e1)!.action_id);           // the new card names only the row it replaced
    const e3 = await go("繁殖先不学");
    expect(text(e3)).toBe("可以，「生长与繁殖」先不学，以后想学再加回来。");
    const id = card(e3)!.action_id;
    const out = await fx.assistant.approveAction(id);
    expect(out).toMatchObject({ status: "executed", can_undo: true });
    const draftId = out.result.draft_id as string;
    const api = new FakeGoalApi({ roundMs: 0 });
    const r1 = await api.getDraft(draftId);
    expect(r1.map!.nodes.filter((n) => n.kind === "region").map((n) => [n.name, n.level, n.target])).toEqual(
      [["分类与鉴别", "基础", false], ["开缸与日常", "核心", true], ["营养与摄食", "核心", true], ["疾病与风险", "进阶", true]]);
    expect(text(await go("再改一下"))).toBe("已经按这个分法排好了。要改就在学习路径里直接改，或者撤销后重新分。");
    await fx.assistant.undoAction(id);
    await expect(new FakeGoalApi({ roundMs: 0 }).getDraft(draftId)).rejects.toMatchObject({ status: 404 });
  });

  it("the answer to the one question: 想了解 → a new draft with 分类 and 繁殖 first; 自己养 → the order stays, one line, no card", async () => {
    const fx = createDemoFixture();
    const go = async (s: string, t: string): Promise<AssistantEvent[]> => { const ev: AssistantEvent[] = []; await fx.assistant.sendMessage(s, { text: t }, (e) => ev.push(e)); return ev; };
    await go("s1", "我要学黑壳虾");
    const e1 = await go("s1", "想把它当成一种生物来了解");
    expect(text(e1)).toBe("可以，按研究这种生物来排：「分类与鉴别」「生长与繁殖」放到前面，开缸和日常照顾只讲到能看懂原理为止。");
    expect(card(e1)!.items.map((i) => i.title)).toEqual(["分类与鉴别", "生长与繁殖", "水质与环境", "开缸与日常", "营养与摄食", "疾病与风险"]);
    await go("s2", "我要学黑壳虾");
    const e2 = await go("s2", "想自己养");
    expect(text(e2)).toBe("可以，就按自己养来排，顺序不变；分类和生理只讲到能看懂操作为止。");
    expect(card(e2)).toBeUndefined();
    expect(text(await go("s2", "嗯"))).toBe("要改哪一块？比如「水质和开缸合成一块」「繁殖先不学」。");
  });
});

describe("the demo fixture takes 开始's intent", () => {
  it("a learn_goal message gets the blocks draft from its goal field, without 我要学 in the text", async () => {
    const fx = createDemoFixture();
    const ev: AssistantEvent[] = [];
    await fx.assistant.sendMessage("s1", { text: "黑壳虾", intent: "learn_goal", goal: "黑壳虾" }, (e) => ev.push(e));
    expect(ev.some((e) => e.type === "action" && e.kind === "goal_new")).toBe(true);
  });
});
