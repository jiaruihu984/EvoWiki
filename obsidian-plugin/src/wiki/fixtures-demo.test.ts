import { describe, expect, it, vi } from "vitest";
import type { AssistantEvent } from "../assistant/types";
import { FakeConn, RecSync } from "./controller-test-kit";
import { WikiController } from "./controller";
import { createDemoFixture, DEMO_ANSWER_EVENTS, DEMO_FEEDBACK_PARAGRAPH, DEMO_IX_ASSET, DEMO_RECOMMENDATIONS, DEMO_SESSION_ID, DemoWikiApi } from "./fixtures-demo";
import { DEMO_IX_HTML } from "./fixtures-demo-ix";
import { reviewEntries } from "./review-entries";
import { todayStep } from "./progress";
import { toWikiNoteInput } from "./note-input";
import { renderWikiNote } from "./note/format";
import { renderLearn } from "./render-learn";
import { fakeActions } from "./render-test-kit";
import { changeRows } from "./bot/change-rows";
import { whyLines } from "./bot/why-text";
import { buildStarMap } from "./starmap";
import { DotTracker } from "./dots";

const names = { page: () => null, concept: () => null, hub: () => null };

function rig() {
  const fx = createDemoFixture();
  const c = new WikiController({ blockEventStore: null, blockProgressStore: null, api: fx.wiki, conn: new FakeConn(), sync: new RecSync(), openNote: () => true,
    configured: () => true, demo: false, recommendations: fx.recommendations });
  return { fx, c };
}

describe("demo fixture through the real controller", () => {
  it("今天: three cards (cue, check, check) with their page · section, next page 3 · 协方差矩阵, the demo's recommendations", async () => {
    const { c } = rig();
    await c.init();
    const s = c.state;
    expect(s.demo).toBe(false);
    const entries = reviewEntries(s);
    expect(entries.map((e) => (e.kind === "item" ? [e.item.mode, e.item.page_title, e.item.heading] : null))).toEqual([
      ["cue", "pca（1/4）", "方差在量什么"], ["check", "pca（1/4）", "协方差"], ["check", "pca（2/4）", "特征值和特征向量"]]);
    const first = entries[0]!;
    expect(first.kind === "item" && first.item.cue).toBe("哪个量可以描述一组数据的离散程度？");
    expect(first.kind === "item" && first.item.answer).toContain("每个数离均值多远");
    expect(todayStep(s)).toMatchObject({ kind: "read", goal: "pca", index: 2, title: "特征值和特征向量" });   // p2 is written, not finished
    expect(s.recommendations).toBe(DEMO_RECOMMENDATIONS);
    expect(s.recommendations!.map((r) => [r.name, r.reason])).toEqual([
      ["机器学习", "已掌握「矩阵乘法」和「方差」"], ["奇异值分解", "已掌握「矩阵乘法」"], ["相关系数", "已掌握「方差」"]]);
  });

  it("想学点新的 draws the override, not the frontier of K; without one the frontier is drawn", async () => {
    const { c } = rig();
    await c.init();
    const shown = (s: typeof c.state) => [...renderLearn(s, fakeActions()).querySelectorAll("button[data-action='recommend']")]
      .map((b) => [b.querySelector("b")?.textContent, b.querySelector("span")?.textContent]);
    expect(shown(c.state)).toEqual([["机器学习", "已掌握「矩阵乘法」和「方差」"], ["奇异值分解", "已掌握「矩阵乘法」"], ["相关系数", "已掌握「方差」"]]);
    expect(shown({ ...c.state, recommendations: null }).map((x) => x[0])).toEqual(["协方差", "特征值与特征向量", "投影"]);
  });

  it("K: the demo's concepts, bands and prerequisites", async () => {
    const { c } = rig();
    await c.init();
    if (c.state.k.kind !== "ready") throw new Error("not loaded");
    const map = buildStarMap(c.state.k.value, { goals: [], membership: {} });
    expect(Object.fromEntries(map.nodes.map((n) => [n.id, n.band]))).toEqual({
      vec: "known", mul: "known", eig: "shaky", mean: "known", var: "known", cov: "shaky", pca: "none", proj: "gap" });
    expect(map.byId.get("pca")!.prereqs).toEqual(["eig", "cov"]);
  });

  it("个性化: ENGINE.log in order (a code change waits for review, the rest applied or undone); PREFS: rules, a pending one, the evidence", async () => {
    const { fx, c } = rig();
    await c.init();
    if (c.state.rsi.kind !== "ready") throw new Error("not loaded");
    expect(c.state.rsi.value.map((r) => [r.status, r.summary])).toEqual(expect.arrayContaining([   // rows dated today at a fixed hour: their order moves with the clock
      ["personal_accepted", "在 pca（2/4）「怎么把它们求出来」末尾补一段：λ = 1 的特征向量怎么求"],
      ["awaiting_user", "等你审阅的代码改动：交互块渲染前规范化"],
      ["personal_accepted", "把「方差」和「协方差」放进主题「统计」"],
      ["personal_accepted", "新增偏好「页面里多配几个能动手的交互组件」"],
      ["personal_accepted", "新增了工具「判断方程式是否配平」：写化学方程式的页面会先自动检查配平，再放进页面"],
      ["rolled_back", "AI 撤回偏好「每页开头先列出要用到的旧知识」"],
      ["personal_accepted", "按你的复习记录调整了记忆保持系数"],
      ["personal_accepted", "新增偏好「提出改进建议时，先找出卡住的步骤」"],
      ["personal_accepted", "新增偏好「公式出现前先说明要解决的问题」"]]));
    expect(c.state.rsi.value.some((r) => r.summary.includes("复习间隔"))).toBe(false);
    const spec = await fx.wiki.teachSpec();
    expect(spec.items.map((i) => [i.item_id, i.origin, i.authority])).toEqual([
      ["u1", "user_chat", "user"], ["u2", "user_chat", "user"], ["a1", "rsi_b", "rsi"], ["a2", "rsi_b", "rsi"], ["d03", "default", "rsi"],
      ["q1", "memory_backfill", "pending"]]);
    expect(spec.evidence.q1).toBe("你在 3 次对话里都要了能算出结果的例子");
    expect(spec.exemplars.map((e) => [e.kind, e.ref])).toEqual([["vault_note", "PCA 读书笔记.md"]]);
  });

  it("个性化 · 待确认的偏好: 设为我的偏好 (take_over), 不采用 remembered, 恢复 brings it back; 采用 the code change", async () => {
    const { fx } = rig();
    expect((await fx.wiki.takeOverSpecItem("q1")).items.find((i) => i.item_id === "q1")?.authority).toBe("user");
    const out = await fx.wiki.deleteSpecItem("q1");
    expect(out.decisions.map((d) => [d.text, d.action])).toEqual([["讲解数学概念时优先使用可计算的小例子", "revoked"]]);
    expect((await fx.wiki.addSpecItem({ text: "讲解数学概念时优先使用可计算的小例子", scope: "any", domain: null })).decisions).toEqual([]);
    expect((await fx.wiki.approvePatch("forgep:demo-1")).status).toBe("personal_accepted");
  });

  it("the page p2 opens as freshNote(); a wrong check answer, 粗心选错了, then the confirmation question is due", async () => {
    const { c } = rig();
    await c.init();
    await c.openWikiPage("p2");
    const p = c.state.page;
    if (p.kind !== "open") throw new Error(p.kind);
    expect(p.page.title).toBe("pca（2/4）");
    expect(p.page.sections.map((s) => [s.id, s.role, s.folded])).toEqual([["intro", "organizer", false], ["mul", "explain", true],
      ["s1", "explain", false], ["s2", "explain", false], ["s2c", "check", false], ["s3", "explain", false]]);
    await c.answerCheck("s2c", 1);
    await c.followup({ kind: "check", pageId: "p2", pageTitle: "pca（2/4）", sid: "s2c", idk: false }, "slip");
    await c.refreshToday(true);
    const today = c.state.review.today;
    if (today.kind !== "ready") throw new Error(today.kind);
    expect(today.value.confirms.map((x) => [x.concept_name, x.page_title, x.check.stem])).toEqual([
      ["特征值与特征向量", "pca（2/4）", "A = [[3, 0], [0, 1]]，下面哪个是 A 的特征向量？"]]);
    expect(reviewEntries(c.state)).toHaveLength(4);
  });
});

describe("demo fixture at any hour", () => {
  it("opened before 9:00, today's fixture rows are never in the future", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    try {
      vi.setSystemTime(new Date(2026, 9, 29, 1, 50));
      const w = new DemoWikiApi();
      await w.pageFeedback("p2", { chips: ["no_example"], text: "例子不够", section_id: null, apply: true });
      const rows = await w.rsiChanges();
      expect(rows.every((r) => Date.parse(r.at) <= Date.now())).toBe(true);
    } finally { vi.useRealTimers(); }
  });
});

describe("demo fixture scripted writes", () => {
  it("page feedback with text: the computable example in 特征值和特征向量 and the 整理建议, no change row (RSI-B retired); 撤回 removes it", async () => {
    const w = new DemoWikiApi();
    const rowsBefore = await w.rsiChanges();
    const specBefore = await w.teachSpec();
    const out = await w.pageFeedback("p2", { chips: ["no_example"], text: "例子不够", section_id: null, apply: true });
    expect(out.summary).toBe("在「特征值和特征向量」里加了一个能算的例子");
    expect(out.reading?.reading).toBe("例子不够，想先看一个能动手算的例子再看定义。");
    expect(out.rsi).toBe("queued");
    const page = await w.openPage("p2");
    expect(page.sections.find((s) => s.id === "s2")!.body).toContain(DEMO_FEEDBACK_PARAGRAPH);
    expect(page.feedback?.summary).toBe(out.summary);
    expect(await w.rsiChanges()).toEqual(rowsBefore);
    expect((await w.teachSpec()).version).toBe(specBefore.version);
    await w.undoFeedback("p2", out.feedback_id!);
    expect((await w.openPage("p2")).sections.find((s) => s.id === "s2")!.body).not.toContain(DEMO_FEEDBACK_PARAGRAPH);
  });

  it("看不懂 on a review card: 去复习 the card's page · section, and the question quoted for the assistant", async () => {
    const w = new DemoWikiApi();
    const g = await w.gradeReview({ cell: "cov.term", choice: 2, page_id: "p1", section_id: "cov" });
    expect(g.correct).toBe(false);
    const f = await w.followup("p1", "cov", { choice: "unclear", answer_ref: g.answer_ref });
    expect(f.review).toMatchObject({ page_title: "pca（1/4）", heading: "协方差" });
    expect(f.quote?.text).toBe("题目：两个量一个变大时另一个通常变小，它们的协方差是？\n我选的：零\n正确答案：负的");
  });

  it("the interactive asset is app.js interactive(), its hash matches the meta", async () => {
    const w = new DemoWikiApi();
    expect(await w.assetHtml(DEMO_IX_ASSET.asset_id)).toBe(DEMO_IX_HTML);
    expect(DEMO_IX_HTML).toContain("转动箭头，找出不被掰弯的方向");
    expect((await w.openPage("p2")).sections.find((s) => s.id === "s1")!.body).toContain("src: assets/demo-ix.html");
  });

  it("the interactive runs on its own: 20° shows 差 …°, 45° lines up (λ = 3.0, v 是特征向量)", () => {
    // The page's box into this test's document, then its script run here (vitest's jsdom runs inserted scripts).
    const parsed = new DOMParser().parseFromString(DEMO_IX_HTML, "text/html");
    const doc = document;
    doc.body.replaceChildren(doc.importNode(parsed.querySelector(".ix")!, true));
    const script = doc.createElement("script");
    script.textContent = parsed.querySelector("script")!.textContent;
    doc.body.append(script);
    // The box and its title are the plugin's embedded-html block (border + `title:` line), so the page draws neither.
    expect(doc.querySelector(".ix > .t")).toBeNull();
    expect(parsed.querySelector("style")!.textContent).not.toMatch(/\.ix\{[^}]*border/);
    expect(parsed.title).toBe(DEMO_IX_ASSET.title);
    expect(doc.getElementById("lt")?.textContent).toBe("v 的方向：20°");
    expect(doc.getElementById("big")?.textContent).toMatch(/^差 \d+°$/);
    expect(doc.querySelectorAll("#svgw svg line")).toHaveLength(4);
    expect(doc.querySelector("#svgw svg line:nth-of-type(4)")?.getAttribute("stroke-dasharray")).toBe("7 5");
    const r = doc.getElementById("ix-angle") as HTMLInputElement;
    r.value = "45";
    r.dispatchEvent(new Event("input"));
    expect(doc.getElementById("big")?.textContent).toBe("λ = 3.0");
    expect(doc.querySelector("#st .ok")?.textContent).toBe("在同一条线上：v 是特征向量");
    expect(doc.querySelector("#svgw svg line:nth-of-type(4)")?.hasAttribute("stroke-dasharray")).toBe(false);
    doc.body.replaceChildren();
  });

  it("the interactive keeps the demo's data palette and reports its height (app.js interactive() after cfdcd31)", () => {
    expect(DEMO_IX_HTML).toContain("看 A v（蓝色）和 v（灰色）是不是在同一条线上");
    expect(DEMO_IX_HTML).not.toContain("紫色");
    expect(DEMO_IX_HTML).toContain(".ix input[type=range]{accent-color:var(--viz-1);width:100%}");
    // A v's line, dot and label in --viz-1; v in --tx-3 / --tx-2; nothing in the monochrome --accent.
    expect(DEMO_IX_HTML.match(/"var\(--viz-1\)"/g)).toHaveLength(3);
    expect(DEMO_IX_HTML).not.toContain("var(--accent)");
    // Its own token values never outrank the host's injected :root values (zero specificity), light and dark.
    expect(DEMO_IX_HTML).toContain(":where(:root){");
    expect(DEMO_IX_HTML).toContain("--viz-1:#2f6fdf");
    expect(DEMO_IX_HTML).toContain("--viz-1:#6ea2ff");
    expect(DEMO_IX_HTML).not.toMatch(/(^|\n):root\s*\{/);
    expect(DEMO_IX_HTML).toContain("parent.postMessage({ evowikiFrameHeight: Math.ceil(document.documentElement.scrollHeight) }, \"*\")");
    expect(DEMO_IX_HTML).toContain("new ResizeObserver(report).observe(document.body)");
  });

  it("the note writer renders both pages (the 你已经懂 callouts and the check block)", async () => {
    const w = new DemoWikiApi();
    const p2 = renderWikiNote(toWikiNoteInput(await w.openPage("p2"), names)).markdown;
    expect(p2).toContain("> [!evowiki-known]- 矩阵乘法：一行乘一列，对应相乘再相加");
    expect(p2).toContain("```evowiki-check\npage: p2\nsection: s2c\n```");
    expect(p2).toContain("## 先看一个变换会做什么");
    const p1 = renderWikiNote(toWikiNoteInput(await w.openPage("p1"), names)).markdown;
    expect(p1).toContain("> [!evowiki-known]- 均值：一组数的「中心」在哪");
  });
});

describe("demo fixture assistant and usage", () => {
  it("freshAsst(): the stored session for p2, the scripted answer, and 加进去 adds the section to p2", async () => {
    const fx = createDemoFixture();
    const sessions: Record<string, string> = {};
    fx.seedSessions(sessions, { server: "http://127.0.0.1:8001", user: "regression" });
    expect(Object.values(sessions)).toEqual([DEMO_SESSION_ID]);
    const s = await fx.assistant.getSession(DEMO_SESSION_ID);
    expect(s.messages.map((m) => [m.role, m.text.slice(0, 12)])).toEqual([["user", "特征值为什么叫「特征」？"], ["assistant", "「特征」在这里是「本身固"]]);
    const events: AssistantEvent[] = [];
    await fx.assistant.sendMessage(DEMO_SESSION_ID, { text: "这里为什么是 [0, 3]？" }, (e) => events.push(e));
    await fx.assistant.sendMessage(DEMO_SESSION_ID, { text: "再问一次" }, (e) => events.push(e));
    expect(events.filter((e) => e.type === "proposal")).toHaveLength(2);
    const answer = events.filter((e) => e.type === "text_delta").map((e) => e.text).join("");
    expect(answer).toContain(String.raw`A=\begin{bmatrix}2&0\\0&3\end{bmatrix}`);
    expect(answer).toContain(String.raw`v=\begin{bmatrix}0\\1\end{bmatrix}`);
    expect(answer).toContain(String.raw`Av=\begin{bmatrix}0\\3\end{bmatrix}=3v`);
    expect(answer).toContain(String.raw`2\times0+0\times1=0`);
    expect(answer).toContain(String.raw`0\times0+3\times1=3`);
    expect(answer).toContain("方向不变，长度变为原来的 **3 倍**");
    const proposal = events.find((e) => e.type === "proposal")!;
    expect(proposal.draft.text).toContain(String.raw`Av=\begin{bmatrix}2&0\\0&3\end{bmatrix}`);
    expect(proposal.change?.new_body).toBe(proposal.draft.text);

    const d = await fx.assistant.decide("pr-demo-1", "accept");
    expect(d.applied).toEqual(["s2b"]);
    expect((await fx.wiki.openPage("p2")).sections.map((x) => x.id)).toContain("s2b");
    expect((await fx.wiki.openPage("p2")).sections.find((x) => x.id === "s2b")?.body).toBe(proposal.draft.text);
  });

  it("a stored session id is kept", () => {
    const fx = createDemoFixture();
    const key = JSON.stringify(["http://127.0.0.1:8001", "regression", "page:p2"]);
    const sessions: Record<string, string> = { [key]: "ses_mine" };
    fx.seedSessions(sessions, { server: "http://127.0.0.1:8001", user: "regression" });
    expect(sessions).toEqual({ [key]: "ses_mine" });
  });

  it("seedSeen: changes from before today were already looked at (app.js boot SEEN.eng), so 个性化 counts 3 like the demo (the preference to confirm, the code change to review, the new tool)", async () => {
    const { fx, c } = rig();
    await c.init();
    await c.loadEngine();
    const keys: string[] = [];
    fx.seedSeen(keys);
    expect(keys).toHaveLength(1);
    expect(new DotTracker({ load: () => keys, save: () => {} }).update(c.state, "today").engine).toBe(3);
    const mine = ["engine@5"];
    fx.seedSeen(mine);
    expect(mine).toEqual(["engine@5"]);
  });

  it("用量: 今天 $0.31 · 本月 $4.12 with 页面 / 助手 / 出题", async () => {
    const u = await createDemoFixture().usage.usage();
    expect([u.today.total_usd, u.month.total_usd]).toEqual([0.31, 4.12]);
    expect(Object.entries(u.today.by_group).map(([g, x]) => [g, x.cost_usd])).toEqual([["page", 0.25], ["assistant", 0.04], ["diagnostic", 0.02]]);
    expect(Object.entries(u.month.by_group).map(([g, x]) => [g, x.cost_usd])).toEqual([["page", 3.4], ["assistant", 0.52], ["diagnostic", 0.2]]);
  });
});

describe("demo fixture in the 助手 tab", () => {
  it("the answer's tool calls: search_wiki, then read_page of pca (2/4); no tools line is shown (copy guide)", () => {
    const calls = DEMO_ANSWER_EVENTS.flatMap((e) => (e.type === "tool_start" ? [[e.name, e.summary ?? ""]] : []));
    expect(calls).toEqual([["search_wiki", expect.any(String)], ["read_page", "pca（2/4）"]]);
  });
  it("the restored conversation carries the same tool calls (GET session's stored tool calls)", async () => {
    const s = await createDemoFixture().assistant.getSession(DEMO_SESSION_ID);
    const answer = s.messages.find((m) => m.role === "assistant")!;
    expect((answer.tools ?? []).map((t) => t.name)).toEqual(["search_wiki", "read_page"]);
  });
});

describe("demo fixture: Bot data (app.js freshBot + ENGINE.log rows 1–2)", () => {
  const NOW = new Date(2026, 9, 15, 16, 0);
  const api = (): DemoWikiApi => new DemoWikiApi({ now: () => NOW });

  it("今天: the review card and the 补讲 card with the demo's titles and whys", async () => {
    const t = await api().botSuggestions("today");
    expect(t.map((s) => [s.kind, s.title, s.why])).toEqual([
      ["review_due", "复习 3 张", "今天到期：方差在量什么、协方差、特征值和特征向量"],
      ["wrong_answers", "投影长度为什么要除以 |b|", "最近三道投影题都忘了除以 |b|"]]);
  });

  it("the completion card's 连续学习 reads 6 天 (doneCard)", async () => {
    expect((await api().reviewToday()).streak_days).toBe(6);
  });

  it("chips per page: p1 has 1, p2 has 2", async () => {
    const a = api();
    expect((await a.botSuggestions("page", "p1")).map((s) => s.title)).toEqual(["方差和标准差有什么区别？"]);
    expect((await a.botSuggestions("page", "p2")).map((s) => s.title)).toEqual(["λ = 0.5 的方向会怎样？", "用 [[2, 1], [1, 2]] 自己求一遍特征向量"]);
  });

  it("最近做了什么: the demo's run titles, newest first; 显示没做的 adds the five skipped reasons", async () => {
    const a = api();
    const runs = await a.botRecent(false);
    expect(runs.map((r) => [r.title, r.suggestion_status])).toEqual([
      ["pca（2/4）的 2 条小建议", "shown"], ["复习 3 张", "shown"], ["补讲：投影长度为什么要除以 |b|", "shown"], ["复习 5 张", "done"],
      ["补讲：协方差为什么会是负的", "not_interested"], ["继续：pca 第 2 页 · 特征值和特征向量", "accepted"]]);
    const skipped = (await a.botRecent(true)).filter((r) => r.result === "skipped");
    expect(skipped.map((r) => [r.title, r.gate_detail])).toEqual([
      ["pca（1/4）的小建议", "额度满：这一页今天已经给过 2 条"],
      ["补讲：协方差为什么会是负的", "你之前点过不感兴趣，冷却到 10月17日"],
      ["复习提醒（同一批卡）", "冷却中：这件事今天已经提过"],
      ["继续：贝叶斯公式（2/3）", "静默：Bot 暂停到今天 8:00"]]);
  });

  it("为什么？ lines of r1, r2, r4 and r6 equal the demo's strings (the demo's 规划 line is gone: no model plans a card)", async () => {
    const a = api();
    expect(whyLines(await a.botWhy({ triggerId: "tr_r1" }))).toMatchObject({
      触发: "规则「复习到期」v1：3 张卡今天到期（方差在量什么、协方差、特征值和特征向量）",
      条件: "通过：Bot 开着，复习开着，今天的卡 1 / 5", 产出: "今天卡「复习 3 张」" });
    expect(whyLines(await a.botWhy({ suggestionId: "sg_wrong" }))).toMatchObject({
      触发: "规则「连续答错」v1：7 天里「投影 × 计算」错了 3 道不同的题（10月9日 1 道，10月12日 2 道），3 道都标了同一个误区「忘了除以 |b|」，按误区归成一件事",
      条件: "通过：补讲开着，今天的卡 2 / 5，这个误区不在冷却中",
      产出: "今天卡「补讲：投影长度为什么要除以 |b|」" });
    expect(whyLines(await a.botWhy({ triggerId: "tr_r4" }))).toMatchObject({
      触发: "规则「陪读建议」v1：你打开了 pca（2/4）；这一页的确认题你上次选了「[0, 2]」", 条件: "通过：陪读建议开着，这一页今天 0 / 2 条",
      产出: "助手里的 2 条小建议" });
    expect(whyLines(await a.botWhy({ triggerId: "tr_r6" }))).toMatchObject({
      产出: "今天卡「补讲：协方差为什么会是负的」", 你的反应: "不感兴趣" });
  });

  it("the two change rows: the Bot's page edit (done, undoable) and the page merge waiting, with their diffs", async () => {
    const rows = await api().botActions();
    const edit = rows.find((r) => r.source === "bot")!;
    expect([edit.status, edit.can_undo, edit.scope, edit.summary, edit.why]).toEqual(["executed", true, "page_body",
      "在 pca（2/4）「怎么把它们求出来」末尾补一段：λ = 1 的特征向量怎么求", "你在助手里两次问「λ = 1 时 v 是哪个」"]);
    expect(edit.diff.map((l) => l.kind)).toEqual(["ctx", "add"]);
    const merge = rows.find((r) => r.kind === "page_merge")!;
    expect([merge.status, merge.scope, merge.summary, merge.why]).toEqual(["proposed", "delete_merge", "把「方差（旧）」并进 pca（1/4）",
      "两页都在讲方差在量什么，旧页 9月20日 以后没打开过"]);
    expect(merge.diff).toEqual([{ kind: "del", text: "页面「方差（旧）」" },
      { kind: "add", text: "pca（1/4）「方差在量什么」末尾加 1 段：旧页里两组成绩的例子" }]);
  });

  it("settings are the demo defaults with chat / idle_write on; reacts, approve and pause stay stateful", async () => {
    const a = api();
    const s = await a.botSettings();
    expect([s.kinds.chat, s.kinds.idle_write, s.today_cap, s.page_cap, s.daily_hour]).toEqual([true, true, 5, 2, 8]);
    await a.botReact("sg_wrong", "not_interested");
    expect((await a.botSuggestions("today")).map((x) => x.suggestion_id)).toEqual(["sg_review"]);
    expect((await a.botWhy({ suggestionId: "sg_wrong" })).reaction?.status).toBe("not_interested");
    expect((await a.botDecide("act_merge1", "approve")).status).toBe("executed");
    expect((await a.botBadge()).pending_actions).toBe(0);
    expect((await a.botPause("forever")).paused).toBe("always");
  });
});

describe("demo fixture: 个性化 · 活动记录 rows (ENGINE.log row 1 and the ledger rows)", () => {
  async function rows(api: DemoWikiApi) {
    const c = new WikiController({ blockEventStore: null, blockProgressStore: null, api, conn: new FakeConn(), sync: new RecSync(), openNote: () => true, configured: () => true, demo: false });
    await c.init();
    c.setTab("engine");
    return c;
  }
  it("the Bot's page edit is a row with its diff, the merge waits; 不感兴趣 adds an act_pref row; approving turns the wait into a change", async () => {
    const api = new DemoWikiApi({ now: () => new Date(2026, 9, 15, 16, 0) });
    let c = await rows(api);
    const keys = (): string[] => changeRows(c.state).map((r) => r.key);
    expect(keys().slice(0, 2)).toEqual(["act:act_merge1", "act:act_edit1"]);
    const edit = changeRows(c.state)[1]!;
    expect(edit.type === "change" && edit.action?.diff?.map((l) => l.kind)).toEqual(["ctx", "add"]);
    await api.botReact("sg_wrong", "not_interested");
    c = await rows(api);
    expect(keys().some((k) => k.startsWith("act:act_pref"))).toBe(true);
    // app.js prefChanged's row text; the backend lists the ledger row's own summary (change_source.change_out).
    const pref = changeRows(c.state).find((r) => r.key.startsWith("act:act_pref"))!;
    expect(pref.type === "change" && [pref.card.change.summary, pref.card.change.why])
      .toEqual(["以后少提醒「投影长度为什么要除以 |b|」", "你点了不感兴趣"]);
    await api.botDecide("act_merge1", "approve");
    c = await rows(api);
    expect(changeRows(c.state).filter((r) => r.key === "act:act_merge1").map((r) => r.type)).toEqual(["change"]);
  });
});
