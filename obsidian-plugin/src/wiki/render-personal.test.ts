import { pendingKeys } from "./dots";
import { pageWaitActions } from "./page-waits";
import { activityItems } from "./personal-rules";
import { describe, expect, it, vi } from "vitest";
import { WIKI_COPY } from "./copy";
import type { ActionOut, ActivityOut } from "./bot/bot-schema";
import { provenanceOf, type RuleKind } from "./fake";
import { demoRsiLog } from "./fixtures-demo";
import { renderPersonal } from "./render-personal";
import { at, btn, btns, fakeActions } from "./render-test-kit";
import type { RsiChangeOut, SpecItem, TeachSpecOut } from "./schema";
import { INITIAL_PERSONAL, INITIAL_WIKI_STATE, type PersonalUi, type WikiState } from "./state";

const NOW = new Date(2026, 9, 28, 15, 0);
const item = (item_id: string, text: string, kind: RuleKind, origin?: SpecItem["origin"]): SpecItem =>
  ({ item_id, text, scope: "any", domain: null, ...provenanceOf(kind), ...(origin ? { origin } : {}), rev: 1, protected: false, helpful: 0, harmful: 0 });
const SPEC: TeachSpecOut = {
  version: 3, stored: true, exemplars: [], params: { recap_max_per_page: 4, analogy_level: 1, fading_level: 1, interactive_bias: 1 },
  items: [item("u1", "先用具体例子解释新概念", "user", "user_chat"), item("d03", "新词第一次出现时先用一句人话解释", "default"),
    item("a1", "公式出现前先说明要解决的问题", "rsi"), item("q1", "讲解数学概念时优先使用可计算的小例子", "pending"),
    item("s1", "一条种子规则", "default", "seed")],
  evidence: { u1: "你说过「例子不够」", q1: "你在 3 次对话里都要了能算出结果的例子" },
  decisions: [{ decision_id: "rd1", rule_id: "x9", text: "每页都配一张图", action: "rejected", via: "ui" },
    { decision_id: "rd2", rule_id: "x8", text: "先讲历史背景", action: "revoked", via: "ui" }],
  max_items: 30, max_exemplars: 3,
};
const state = (o: Partial<WikiState> = {}, p: Partial<PersonalUi> = {}): WikiState => ({
  ...INITIAL_WIKI_STATE, conn: "online", tab: "engine", prefs: { kind: "ready", value: SPEC }, rsi: { kind: "ready", value: [] },
  personal: { ...INITIAL_PERSONAL, rules: true, ...p }, ...o,
});
const fact = { action_id: "f1", kind: "profile_fact", status: "proposed", summary: "记进档案：「我是谁」我是护士", why: "你说的", created_at: "2026-11-04T00:00:00Z", page_id: null } as unknown as ActionOut;
const draw = (s: WikiState, a = fakeActions()) => { const b = document.createElement("div"); renderPersonal(b, s, a, NOW); return { a, b }; };
const tip = (r: Element): string[] => [...r.querySelectorAll(":scope > .ew-qtip .ew-tipbox > span")].map((e) => e.textContent ?? "");
const rows = (box: Element): HTMLElement[] => [...box.querySelectorAll<HTMLElement>(":scope > .ew-pz-it")];

describe("待确认的档案内容", () => {
  it("a waiting fact preserves its supplied summary with 确认 / 取消 in 个性化, and not on 知识图谱", () => {
    const st = state({ bot: { ...INITIAL_WIKI_STATE.bot, actions: { kind: "ready", value: [fact] } } });
    const { a, b } = draw(st);
    const card = b.querySelector<HTMLElement>("#pz-facts")!;
    expect(card.querySelector(".ew-pz-t")?.firstChild?.textContent).toBe(fact.summary);
    card.querySelector<HTMLButtonElement>("[data-action=bot-approve]")!.click();
    card.querySelector<HTMLButtonElement>("[data-action=bot-reject]")!.click();
    expect(vi.mocked(a.botDecide).mock.calls).toEqual([["f1", "approve"], ["f1", "reject"]]);
    expect(pageWaitActions(st, null)).toEqual([]);
  });
});

describe("个性化 · 活动记录's red count", () => {
  it("counts the tools he has not seen with the code changes waiting; none: no count", () => {
    const b = document.createElement("div");
    renderPersonal(b, state(), fakeActions(), NOW, 2);
    expect(b.querySelector("#pz-log .ew-cnt")?.textContent).toBe("2");
    expect(draw(state()).b.querySelector("#pz-log .ew-cnt")).toBeNull();
  });
});

describe("个性化 · 我的学习偏好", () => {
  it("lists the user's rules (我的要求) and EvoWiki's tuned ones (AI 优化); no base, seed or pending rule; the ? is evidence + where it came from", () => {
    const { a, b } = draw(state());
    const box = b.querySelector<HTMLElement>("#pz-rules")!;
    expect(box.className).toBe("ew-pz-box");
    expect(box.querySelector(":scope > .ew-pz-entry[aria-expanded=true] > b")?.textContent).toBe("我的学习偏好");
    expect(rows(box).map((r) => [r.querySelector(".ew-pz-t > b")?.textContent, r.querySelector(".ew-pz-tag")?.textContent])).toEqual([
      ["先用具体例子解释新概念", "我的要求"], ["公式出现前先说明要解决的问题", "AI 优化"]]);
    expect(tip(at(rows(box), 0))).toEqual(["你说过「例子不够」", "在助手里说的"]);
    expect(tip(at(rows(box), 1))).toEqual(["EvoWiki 根据你的学习记录调的"]);
    expect(box.querySelector(".ew-pz-foot > span")?.textContent).toBe("共 2 条");
    btn(box, "[data-action=prefs-manage]").click();
    expect(a.personalView).toHaveBeenCalledWith({ manage: true });
  });

  it("loading and a failed read say so in the box (重试 → loadEngine)", () => {
    expect(draw(state({ prefs: { kind: "loading" } })).b.querySelector("#pz-rules .ew-muted")?.textContent).toBe(WIKI_COPY.loading);
    const { a, b } = draw(state({ prefs: { kind: "error", message: "x" } }));
    btn(b, "[data-action=prefs-reload]").click();
    expect(a.loadEngine).toHaveBeenCalledOnce();
  });
});

describe("个性化 · the folding boxes (app.js prefsBox / logBox / sourcesBox, folded by default)", () => {
  const entries = (b: HTMLElement): [string, string | null, number][] => [...b.querySelectorAll<HTMLElement>(":scope > .ew-pz-box")]
    .map((x) => [x.querySelector(":scope > .ew-pz-entry > b")?.textContent ?? "", x.querySelector(":scope > .ew-pz-entry")?.getAttribute("aria-expanded") ?? null, x.children.length]);

  it("我的学习偏好, 活动记录 and 资料 show only their entry (bold title, ›) until opened, with content or empty", () => {
    const empty = { ...SPEC, items: [], decisions: [] };
    for (const prefs of [SPEC, empty]) {
      const { a, b } = draw(state({ prefs: { kind: "ready", value: prefs }, sources: { kind: "ready", value: [] } }, { rules: false }));
      expect(entries(b).filter(([, open]) => open !== null)).toEqual([["我的学习偏好", "false", 1], ["活动记录", "false", 1], ["资料", "false", 2]]);
      expect([...b.querySelectorAll(".ew-pz-entry > .ew-pz-chev")].map((c) => c.textContent)).toEqual(["›", "›", "›", "›"]);
      b.querySelector<HTMLButtonElement>("#pz-me")!.click();
      expect(a.openProfile).toHaveBeenCalled();
      b.querySelector<HTMLButtonElement>("#pz-rules > .ew-pz-entry")!.click();
      expect(a.personalView).toHaveBeenCalledWith({ rules: true });
    }
  });

  it("opened empty: 还没有偏好 + 共 0 条 / 管理偏好; 活动记录's empty line; 还没有资料", () => {
    const { b } = draw(state({ prefs: { kind: "ready", value: { ...SPEC, items: [], decisions: [] } }, sources: { kind: "ready", value: [] } },
      { rules: true, log: true, sources: true }));
    expect(b.querySelector("#pz-rules > .ew-pz-it.ew-muted")?.textContent).toBe("还没有偏好");
    expect(b.querySelector("#pz-rules > .ew-pz-foot")?.textContent).toBe("共 0 条管理偏好");
    expect(b.querySelector(".ew-pz-log > .ew-pz-it.ew-muted")?.textContent).toBe(WIKI_COPY.rsiEmpty);
    expect(b.querySelector(".ew-src-empty")?.textContent).toBe("还没有资料");
    expect(entries(b).filter(([, open]) => open !== null).map(([, open]) => open)).toEqual(["true", "true", "true"]);
  });
});

describe("个性化 · 待确认的偏好", () => {
  it("only when a rule is pending: ? in the header, the rule as an editable textarea, 设为我的偏好 sends the typed text, 不采用 rejects", () => {
    const none = draw(state({ prefs: { kind: "ready", value: { ...SPEC, items: SPEC.items.filter((i) => i.authority !== "pending") } } })).b;
    expect(none.querySelector("#pz-pending")).toBeNull();
    const { a, b } = draw(state());
    const box = b.querySelector<HTMLElement>("#pz-pending")!;
    expect(box.querySelector(".ew-pz-hd > h2")?.textContent).toBe("待确认的偏好");
    expect(box.querySelector(".ew-pz-hd > .ew-qtip")).not.toBeNull();
    expect(box.querySelector(".ew-pz-hd .ew-pill, .ew-cnt")).toBeNull();
    const ta = box.querySelector<HTMLTextAreaElement>("textarea.ew-pz-edit")!;
    expect(ta.value).toBe("讲解数学概念时优先使用可计算的小例子");
    ta.value = "讲数学时先给能算的小例子";
    ta.dispatchEvent(new Event("input"));
    expect(a.prefDraft).toHaveBeenCalledWith("p:q1", "讲数学时先给能算的小例子");
    btn(box, "[data-action=pref-adopt]").click();
    expect(a.prefAdopt).toHaveBeenCalledWith("q1", "讲数学时先给能算的小例子");
    btn(box, "[data-action=pref-reject]").click();
    expect(a.prefReject).toHaveBeenCalledWith("q1");
    expect(btns(box, ".ew-pz-pill").map((x) => [x.textContent, x.className])).toEqual([
      ["设为我的偏好", "ew-btn ew-pz-pill ew-primary"], ["不采用", "ew-btn ew-pz-pill"]]);
  });

  it("a typed text survives a redraw (personal.drafts); the buttons are off while blocked", () => {
    const { b } = draw(state({ busy: { what: "prefs", abortable: false } }, { drafts: { "p:q1": "我自己的说法" } }));
    expect(b.querySelector<HTMLTextAreaElement>("#pz-pending textarea")!.value).toBe("我自己的说法");
    expect(btns(b, "#pz-pending .ew-pz-pill").every((x) => x.disabled)).toBe(true);
  });
});

// ---- 活动记录 ----
const iso = (daysAgo: number, h = 10): string => new Date(2026, 9, 28 - daysAgo, h).toISOString();
const change = (patch_id: string, o: Partial<RsiChangeOut>): RsiChangeOut => ({
  ...demoRsiLog(NOW).find((c) => c.patch_id === "pb-demo-3")!, patch_id, status: "personal_accepted", can_undo: true, summary: patch_id, ...o });
const run = (o: Partial<ActivityOut>): ActivityOut => ({
  trigger_id: "t1", run_id: null, kind: "review_due", title: "复习 5 张", reason: "到期", at: iso(0, 8), gate_outcome: "pass", gate_reason: null,
  gate_detail: null, result: "suggested", suggestion_title: null, suggestion_status: null, cost_usd: 0, write: null, page_id: null, ...o });
const ledger = (o: Partial<ActionOut> & Pick<ActionOut, "action_id">): ActionOut => ({
  kind: "assistant_edit", scope: "page_body", level: "auto_undo", status: "executed", source: "bot", summary: "s", why: "w", content: {}, page_id: "p2",
  detail: "", created_at: iso(0), decided_at: iso(0), expires_at: null, can_undo: true, diff: [], confirm: false, cost_usd: null, made: [], revise: null,
  result: {}, section_id: "s3", ...o });
const CHANGES: RsiChangeOut[] = [
  change("forgep:c1", { track: "H", status: "awaiting_user", can_undo: false, summary: "等你审阅的代码改动：交互块渲染前规范化", why: "重放 3 节赢 3 节", at: iso(0, 9) }),
  change("act_e1", { track: "G", summary: "在「特征值」补一段", at: iso(0, 11) }),
  change("pb-x", { summary: "新增偏好「先给结论」", at: iso(1) }),
  change("pb-auto", { status: "rolled_back", can_undo: false, outcome: "harmful", summary: "AI 撤回偏好「每页开头先列旧知识」", rule_after: "每页开头先列旧知识", at: iso(1, 9) }),
  change("pa-x", { track: "A", summary: "按你的复习记录调整了记忆保持系数", at: iso(5) }),
  change("pb-undone", { status: "rolled_back", can_undo: false, summary: "新增偏好「多用图」", at: iso(6) }),
];
const RUNS: ActivityOut[] = [
  run({}),
  run({ trigger_id: "t2", kind: "wrong_answers", title: "补讲：投影长度为什么要除以 |b|", page_id: "p1", at: iso(1, 8) }),
  run({ trigger_id: null, run_id: "wr1", kind: "idle_write", title: "创建了「对角化」页面", result: "written", at: iso(0, 7),
    write: { page_id: "p9", title: "对角化", folder: "", goal: null, n: null, asked: null, origin: "idle", action_id: "act_w1", can_undo: true, undone: false,
      opened: false, calls: 3, outline_calls: 1, section_calls: 2 } }),
];
const logState = (p: Partial<PersonalUi> = {}, o: Partial<WikiState> = {}): WikiState => state({
  rsi: { kind: "ready", value: CHANGES },
  bot: { ...INITIAL_WIKI_STATE.bot, recent: { kind: "ready", value: RUNS }, actions: { kind: "ready", value: [ledger({ action_id: "act_e1" }),
    ledger({ action_id: "act_wait", status: "proposed", kind: "page_merge", summary: "合并两页", created_at: new Date().toISOString() })] } },
  ...o }, { log: true, ...p });
const logItems = (b: HTMLElement): HTMLElement[] => [...b.querySelectorAll<HTMLElement>(".ew-pz-log > .ew-pz-it")];
const keys = (b: HTMLElement): string[] => logItems(b).map((r) => r.dataset.chg ?? "");

describe("个性化 · 活动记录", () => {
  it("one entry button, collapsed by default; a click expands it in place", () => {
    const { a, b } = draw(state());
    const ent = b.querySelector<HTMLButtonElement>("button#pz-log.ew-pz-entry")!;
    expect(ent.getAttribute("aria-expanded")).toBe("false");
    expect(ent.querySelector("b")?.textContent).toBe("活动记录");
    expect(b.querySelector(".ew-pz-log")).toBeNull();
    ent.click();
    expect(a.personalView).toHaveBeenCalledWith({ log: true });
  });

  it("a code change waiting for his review: a red count on the title's top right (app.js .pz-ttl .cnt), folded or not; none → no count", () => {
    for (const log of [false, true]) {
      const ttl = draw(logState({ log })).b.querySelector<HTMLElement>("#pz-log > b")!;
      expect([ttl.className, ttl.querySelector(":scope > .ew-cnt")?.textContent]).toEqual(["ew-pz-ttl", "1"]);
    }
    const two = [...CHANGES, change("forgep:c2", { track: "H", status: "awaiting_user", can_undo: false, summary: "另一处代码改动", at: iso(0, 10) })];
    expect(draw(logState({ log: false }, { rsi: { kind: "ready", value: two } })).b.querySelector("#pz-log .ew-cnt")?.textContent).toBe("2");
    const none = CHANGES.filter((c) => c.status !== "awaiting_user");
    const b = draw(logState({ log: false }, { rsi: { kind: "ready", value: none } })).b;
    expect(b.querySelector("#pz-log .ew-cnt")).toBeNull();
    expect(b.querySelector("#pz-log > b")?.textContent).toBe("活动记录");
  });

  it("the code change waiting for review first, then 今天 / 昨天 newest first; older rows under 更早; a wait for a yes / no is not here", () => {
    const { a, b } = draw(logState());
    expect([...b.querySelectorAll(".ew-pz-log > .ew-pz-when")].map((x) => x.textContent)).toEqual(["今天", "昨天"]);
    expect(keys(b)).toEqual(["rsi:forgep:c1", "act:act_e1", "run:t1", "run:wr1", "rsi:pb-x", "rsi:pb-auto", "run:t2"]);
    expect(b.textContent).not.toContain("合并两页");
    const more = btn(b, ".ew-pz-log > button.ew-pz-more");
    expect([more.querySelector("span")?.textContent, more.querySelector(".ew-pz-chev")?.textContent, more.getAttribute("aria-expanded")]).toEqual(["更早", "›", "false"]);
    // the toggle and the timeline are inside the 活动记录 card, under its title row
    expect(b.querySelector(".ew-pz-logbox > #pz-log + .ew-pz-log")).not.toBeNull();
    more.click();
    expect(a.personalView).toHaveBeenCalledWith({ older: true });
    expect(keys(draw(logState({ older: true })).b).slice(-2)).toEqual(["rsi:pa-x", "rsi:pb-undone"]);
  });

  it("which rows get 撤销, which a faint word, which a link: undoable changes 撤销; undone 已撤销; an automatic rollback only struck; Bot work a link", () => {
    const { a, b } = draw(logState({ older: true }));
    const row = (k: string): HTMLElement => logItems(b).find((r) => r.dataset.chg === k)!;
    const words = (k: string): string[] => btns(row(k), "button").filter((x) => !x.classList.contains("ew-qbtn")).map((x) => x.textContent ?? "");
    expect(words("act:act_e1")).toEqual(["在「特征值」补一段", "撤销"]);       // the text goes to the page it changed
    expect(words("rsi:pb-x")).toEqual(["撤销"]);
    expect(words("rsi:pa-x")).toEqual(["撤销"]);                              // the θ_infer tuning: an ordinary undoable change
    expect(words("rsi:pb-undone")).toEqual([]);
    expect(row("rsi:pb-undone").querySelector(".ew-pz-tag")?.textContent).toBe("已撤销");
    expect(row("rsi:pb-undone").querySelector(".ew-pz-t")?.classList.contains("ew-off")).toBe(true);
    expect(row("rsi:pb-auto").querySelector(".ew-pz-tag")).toBeNull();
    expect(row("rsi:pb-auto").querySelector(".ew-pz-t")?.classList.contains("ew-off")).toBe(true);
    expect(words("run:t1")).toEqual(["复习 5 张"]);
    expect(words("run:t2")).toEqual(["补讲：投影长度为什么要除以 |b|"]);
    expect(words("run:wr1")).toEqual(["创建了「对角化」页面", "撤销"]);
    for (const r of logItems(b)) expect(r.querySelector(".ew-qtip")).not.toBeNull();
    btn(row("act:act_e1"), "[data-action=go-change]").click();
    expect(a.goToChange).toHaveBeenCalledWith("p2", "s3");
    btn(row("rsi:pb-x"), "[data-action=rsi-undo]").click();
    expect(a.undoPatch).toHaveBeenCalledWith("pb-x");
    btn(row("run:t1"), "[data-action=run-go]").click();
    expect(a.setTab).toHaveBeenCalledWith("today");
    expect(a.startReview).toHaveBeenCalledOnce();
    btn(row("run:t2"), "[data-action=run-go]").click();
    expect(a.openWikiPage).toHaveBeenCalledWith("p1");
    btn(row("run:wr1"), "[data-action=run-go]").click();
    expect(a.goToChange).toHaveBeenCalledWith("p9", null);
    btn(row("run:wr1"), "[data-action=bot-write-undo]").click();
    expect(a.botDecide).toHaveBeenCalledWith("act_w1", "undo");
  });

  it("the code change waiting for review: its summary, the ? with the details, 采用 / 不要 pills", () => {
    const { a, b } = draw(logState());
    const r = at(logItems(b), 0);
    expect(r.querySelector(".ew-pz-t")?.firstChild?.textContent).toBe("等你审阅的代码改动：交互块渲染前规范化");
    expect(tip(r)).toEqual(["重放 3 节赢 3 节"]);
    expect(btns(r, ".ew-pz-pill").map((x) => x.textContent)).toEqual(["采用", "不要"]);
    btn(r, "[data-action=patch-approve]").click();
    btn(r, "[data-action=patch-discard]").click();
    expect([a.approvePatch.mock.calls, a.discardPatch.mock.calls]).toEqual([[["forgep:c1"]], [["forgep:c1"]]]);
  });

  it("a page written by the Bot is listed once (its run; not its ledger change too)", () => {
    const withWrite = logState({}, { rsi: { kind: "ready", value: [...CHANGES, change("act_w1", { track: "G", summary: "创建了「对角化」页面", at: iso(0, 7) })] } });
    const k = keys(draw(withWrite).b);
    expect([k.includes("run:wr1"), k.includes("act:act_w1")]).toEqual([true, false]);
  });
});

describe("个性化 · 管理偏好", () => {
  const manage = (p: Partial<PersonalUi> = {}, o: Partial<WikiState> = {}) => draw(state(o, { manage: true, ...p }));

  it("‹ 个性化 goes back; each rule with 修改 / 删除; 删除 removes it", () => {
    const { a, b } = manage();
    btn(b, "[data-action=prefs-back]").click();
    expect(a.personalView).toHaveBeenCalledWith({ manage: false });
    const box = b.querySelector<HTMLElement>("#pz-manage")!;
    expect(box.querySelector(".ew-pz-hd > h2")?.textContent).toBe("管理偏好");
    expect(rows(box).map((r) => r.dataset.rule)).toEqual(["u1", "a1"]);
    expect(btns(at(rows(box), 1), ".ew-pz-ops > button").map((x) => x.textContent)).toEqual(["修改", "删除"]);
    btn(at(rows(box), 1), "[data-action=pref-delete]").click();
    expect(a.prefRemove).toHaveBeenCalledWith("a1");
    btn(at(rows(box), 1), "[data-action=pref-edit]").click();
    expect(a.prefDraft).toHaveBeenCalledWith("e:a1", "公式出现前先说明要解决的问题");
    expect(a.personalView).toHaveBeenCalledWith({ edit: "a1" });
  });

  it("修改: an inline textarea with 保存 / 取消", () => {
    const { a, b } = manage({ edit: "a1", drafts: { "e:a1": "公式前先说要解决什么" } });
    const ta = b.querySelector<HTMLTextAreaElement>("#pz-manage textarea.ew-pz-edit")!;
    expect([ta.value, ta.getAttribute("aria-label")]).toEqual(["公式前先说要解决什么", "改这条偏好"]);
    btn(b, "[data-action=pref-save]").click();
    expect(a.prefSave).toHaveBeenCalledWith("a1", "公式前先说要解决什么");
    btn(b, "[data-action=pref-cancel]").click();
    expect(a.personalView).toHaveBeenCalledWith({ edit: null });
  });

  it("没采用和撤回的: the user's decisions (你没采用 / 你删除了), then EvoWiki's automatic rollbacks (AI 撤回); 恢复 adds the text back", () => {
    const { a, b } = manage({}, { rsi: { kind: "ready", value: CHANGES } });
    const box = b.querySelector<HTMLElement>("#pz-history")!;
    expect(box.querySelector(".ew-pz-hd > h2")?.textContent).toBe("没采用和撤回的");
    expect(rows(box).map((r) => [r.querySelector("b")?.textContent, r.querySelector(".ew-pz-tag")?.textContent])).toEqual([
      ["每页都配一张图", "你没采用"], ["先讲历史背景", "你删除了"], ["每页开头先列旧知识", "AI 撤回"]]);
    expect(rows(box).every((r) => r.querySelector(".ew-pz-t.ew-off") !== null && r.querySelector(".ew-qtip") !== null)).toBe(true);
    btn(at(rows(box), 2), "[data-action=pref-restore]").click();
    expect(a.prefRestore).toHaveBeenCalledWith("每页开头先列旧知识");
  });
});

describe("an RSI change waiting for his yes", () => {
  const row = {
    action_id: "act_r", kind: "rsi_method", status: "proposed", created_at: "2026-11-04T00:00:00Z", decided_at: null,
    summary: "以后的页面：例题每一步都写全", why: "试写了 8 段，5 段更好", page_id: null,
    diff: [{ kind: "ctx", text: "运行设置" }, { kind: "add", text: "以后的页面：例题每一步都写全" }, { kind: "add", text: "每节先接上一节。" }],
  } as unknown as ActionOut;
  const state: WikiState = { ...INITIAL_WIKI_STATE, bot: { ...INITIAL_WIKI_STATE.bot, actions: { kind: "ready", value: [row] } } };

  it("waits in 活动记录, counted on the tab", () => {
    expect(activityItems(state).review.map((x) => x.key)).toEqual(["act:act_r"]);
    expect(pendingKeys(state, new Date()).engine).toContain("ask:act_r");
  });
});
