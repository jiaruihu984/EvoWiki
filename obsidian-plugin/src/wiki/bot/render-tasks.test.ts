import { afterEach, describe, expect, it } from "vitest";
import { BOT_SUGGESTIONS } from "../fixtures";
import { renderNext } from "../render-next";
import { btn, fakeActions, fakeCtx } from "../render-test-kit";
import type { ReviewItemOut } from "../schema";
import { INITIAL_WIKI_STATE, type WikiState } from "../state";
import type { ActionOut, SuggestionOut } from "./bot-schema";
import { renderAllDone, taskCards } from "./render-tasks";

afterEach(() => { document.body.replaceChildren(); });

const ready = { kind: "ready" } as const;
const item = (cell: string): ReviewItemOut => ({
  card_id: `card-${cell}`, cell, cells: [cell], concept_id: "c1", concept_name: "均值", aspect: "procedure", band: "likely", mode: "cue",
  page_id: "p1", page_title: "页一", section_id: "s1", heading: `小节${cell}`, cue: `线索${cell}`, answer: "", check: null, recall_now: 0.5, exam: false,
  days_to_threshold: -1, in_goal: true, artifact_id: null });
const sugg = (id: string, kind: string, over: Partial<SuggestionOut> = {}): SuggestionOut => ({
  ...BOT_SUGGESTIONS[0]!, suggestion_id: id, kind, title: `标题${id}`, why: `原因${id}`, ...over });
function st(items: ReviewItemOut[], list: SuggestionOut[] | null, bot: Partial<WikiState["bot"]> = {}): WikiState {
  return { ...INITIAL_WIKI_STATE, configured: true, conn: "online", progress: { kind: "ready" as const, value: { goals: [{ goal_id: "g", kind: "path" as const, goal: "pca", state: "done" as const, blocks: [], path_ids: [], pages_done: 1, pages_total: 1, step: { kind: "goal_done" as const, goal_id: "g", goal: "pca", map_id: null, region_id: null, region_name: "", path_id: null, index: null, page_id: null, title: "", draft_id: null, progress: null, failure: null, review_first: null } }], today: { kind: "none" as const, goal_id: null, goal: "", map_id: null, region_id: null, region_name: "", path_id: null, index: null, page_id: null, title: "", draft_id: null, progress: null, failure: null, review_first: null } } },
    review: { ...INITIAL_WIKI_STATE.review, today: { ...ready, value: { items, due: items.length, done_today: 0, limit: 10, confirms: [], page_round_today: 0, streak_days: 0 } } },
    bot: { ...INITIAL_WIKI_STATE.bot, today: list === null ? { kind: "idle" } : { ...ready, value: list }, ...bot } };
}
const draw = (s: WikiState): { root: HTMLElement; a: ReturnType<typeof fakeActions> } => {
  const a = fakeActions();
  const root = renderNext(s, a, fakeCtx());
  document.body.append(root);
  return { root, a };
};

describe("taskCards / renderTaskCard", () => {
  it("the 复习 switch off: no 今日复习 card", () => {
    const settings = (on: boolean) => ({ kind: "ready", value: { kinds: { review_due: on } } }) as unknown as WikiState["bot"]["settings"];
    expect(taskCards(st([item("a")], [], { settings: settings(true) })).map((c) => c.key)).toEqual(["review"]);
    expect(taskCards(st([item("a")], [], { settings: settings(false) }))).toEqual([]);
  });

  it("review pending + one 补讲 suggestion: two .ew-task cards with exact titles and why; a listed review_due is not a third", () => {
    const { root } = draw(st([item("a"), item("b")], [sugg("r1", "review_due"), sugg("w1", "wrong_answers")]));
    const cards = [...root.querySelectorAll<HTMLElement>(".ew-card.ew-task")];
    expect(cards.map((c) => [c.querySelector(".ew-thead h2")?.textContent, c.querySelector(".ew-thead .ew-faint")?.textContent ?? null,
      c.querySelector(".ew-muted")?.textContent ?? null, c.querySelector(".ew-tipbox")?.textContent ?? null])).toEqual([
      ["今日复习", "2 张 · 约 2 分钟", "均值", null],
      ["标题w1", null, null, "原因w1"]]);
    expect(root.querySelector(".ew-alldone")).toBeNull();
  });

  it("开始 / 不感兴趣 call botStart / botNotInterested with the suggestion id (review: \"review\")", () => {
    const { root, a } = draw(st([item("a")], [sugg("w1", "wrong_answers")]));
    const [rv, w] = [...root.querySelectorAll<HTMLElement>(".ew-task")];
    const start = btn(w!, "[data-action=bot-start]");
    expect([start.className, start.textContent]).toEqual(["ew-btn ew-primary", "开始补讲"]);
    start.click();
    expect(a.botStart).toHaveBeenCalledWith("w1");
    const nope = btn(w!, "[data-action=bot-nope]");
    expect([nope.className, nope.textContent]).toEqual(["ew-btn", "不感兴趣"]);
    expect(btn(rv!, "[data-action=bot-start]").textContent).toBe("开始复习");
    expect(btn(rv!, "[data-action=bot-nope]").textContent).toBe("减少提醒");
    nope.click();
    expect(a.botNotInterested).toHaveBeenCalledWith("w1");
    btn(rv!, "[data-action=bot-nope]").click();
    expect(a.botNotInterested).toHaveBeenLastCalledWith("review");
  });

  it("hidden ids, bot_failing and kinds without a demo label are not drawn; API order is kept; the review card is hidden while it cools down", () => {
    const list = [sugg("g1", "goal_idle"), sugg("h1", "chat"), sugg("f1", "bot_failing"), sugg("s1", "strategy_negative"), sugg("o1", "open_question")];
    expect(taskCards(st([], list, { hidden: ["h1"] })).map((c) => c.key)).toEqual(["g1", "o1"]);
    expect(taskCards(st([item("a")], [], { reviewHidden: true }))).toEqual([]);
    expect(taskCards(st([item("a")], [])).map((c) => c.key)).toEqual(["review"]);
  });

  it("write buttons are off while offline, except the local review card", () => {
    const s = { ...st([item("a")], [sugg("w1", "wrong_answers")]), conn: "offline" as const };
    const { root } = draw(s);
    const [rv, w] = [...root.querySelectorAll<HTMLElement>(".ew-task")];
    expect(btn(rv!, "[data-action=bot-start]").disabled).toBe(false);
    expect(btn(w!, "[data-action=bot-start]").disabled).toBe(true);
  });
});

describe("今天的学习已完成", () => {
  it("no cards and both reads ready: .ew-alldone with the check path and the text; .ew-anim only until allDoneSeen", () => {
    const first = draw(st([], [])).root.querySelector<HTMLElement>(".ew-alldone")!;
    expect(first.className).toBe("ew-alldone ew-anim");
    expect(first.textContent).toBe("今天的学习已完成");
    expect(first.querySelector(".ew-adck svg path")?.getAttribute("d")).toBe("M6 12.5l4 4L18 8.5");
    expect(first.querySelector("b")?.textContent).toBe("今天的学习已完成");
    document.body.replaceChildren();
    const again = draw(st([], [], { allDoneSeen: true })).root.querySelector<HTMLElement>(".ew-alldone")!;
    expect(again.className).toBe("ew-alldone");
    expect(renderAllDone(st([], [], { allDoneSeen: true })).className).toBe("ew-alldone");
  });

  it("not while cards are due and the review card is only cooling down: the count and 开始复习 instead, no 暂无待办", () => {
    const { root, a } = draw(st([item("a"), item("b")], [], { reviewHidden: true }));
    expect(root.querySelector(".ew-alldone")).toBeNull();
    expect(root.querySelector("[data-role=next-empty]")).toBeNull();
    expect(root.querySelector("[data-role=next-due] span")?.textContent).toBe("2 张卡待复习");
    btn(root, "[data-action=review-due]").click();
    expect(a.startReview).toHaveBeenCalledOnce();
  });

  it("not while the Bot list has not been read, and not while a card is drawn", () => {
    expect(draw(st([], null)).root.querySelector(".ew-alldone")).toBeNull();
    document.body.replaceChildren();
    expect(draw(st([], [sugg("g1", "goal_idle")])).root.querySelector(".ew-alldone")).toBeNull();
  });
});

describe("review card why", () => {
  const answered = { a: { band: { cell: "a", from: "likely", to: "known" }, grade: "recalled", correct: null, correctIndex: null, choice: null, line: { text: "x", tone: "ok" } } } as never;
  const withReview = (s: WikiState, patch: Partial<WikiState["review"]>): WikiState => ({ ...s, review: { ...s.review, ...patch } });
  const confirm = { followup_id: "f1", concept_id: "c1", concept_name: "概念甲", page_id: "p1", page_title: "页二", section_id: "s1", check: { stem: "题", options: ["甲", "乙"] } } as never;

  it("title count and why-names use the same pending entries: an answered card is in neither", () => {
    const s = withReview(st([item("a"), item("b")], []), { results: answered });
    const [c] = taskCards(s);
    expect([c!.title, c!.faint]).toEqual(["今日复习", "1 张 · 约 1 分钟"]);
    expect(c!.topic).toBe("均值");
  });

  it("only confirmation questions left after a finished round: 刚才答错的那道题; before a round they are the point names", () => {
    const base = st([], []);
    const v = base.review.today;
    if (v.kind !== "ready") throw new Error("x");
    const s = withReview(base, { today: { kind: "ready", value: { ...v.value, confirms: [confirm] } } });
    expect(taskCards(s)[0]!.topic).toBe("概念甲");
    expect(taskCards(withReview(s, { roundDone: true }))[0]!.topic).toBe("刚才答错的那道题");
    const mixed = withReview(s, { roundDone: true, today: { kind: "ready", value: { ...v.value, items: [item("a")], confirms: [confirm] } } });
    expect(taskCards(mixed)[0]!.topic).toBe("概念甲、均值");
  });

  const rv = sugg("rv1", "source_added", { title: "《线性代数讲义》讲到了 3 页已有的内容",
    why: "《线性代数讲义》讲到了 3 页已有的内容：特征值、对角化、谱定理。要按它更新吗？",
    action: { type: "revise_from_source", source_id: "src1", page_ids: ["p1", "p2", "p3"] } });
  const row = (id: string, state: "writing" | "ready", status: string): ActionOut => ({
    ...({} as ActionOut), action_id: id, status, revise: { state, page_id: id, run_id: null, detail: "", sources: [], sections: [], unchanged: 0, rebuild: false, cite_issues: 0, stale: false, practice: null } });

  it("资料更新 card: page names line, 更新这 3 页 button; after the click 生成中 n/N, then 3 份草稿待确认 + 去确认", () => {
    const { root, a } = draw(st([], [rv]));
    const card = root.querySelector<HTMLElement>(".ew-task")!;
    expect(card.querySelector("h2")?.textContent).toBe("按《线性代数讲义》更新 3 页");
    expect(card.querySelector(".ew-muted")?.textContent).toBe("特征值、对角化、谱定理");
    expect(btn(card, "[data-action=bot-start]").textContent).toBe("更新这 3 页");
    btn(card, "[data-action=bot-start]").click();
    expect(a.botStart).toHaveBeenCalledWith("rv1");
    const batch = { suggestion: rv, actionIds: ["a1", "a2", "a3"], total: 3, loaded: true };
    const mid = draw(st([], [], { revise: { rv1: batch }, actions: { ...ready, value: [row("a1", "ready", "proposed"), row("a2", "writing", "proposed")] } }));
    expect(mid.root.querySelector(".ew-wline")?.textContent).toBe("生成中 1/3");
    const all = draw(st([], [], { revise: { rv1: batch }, actions: { ...ready, value: ["a1", "a2", "a3"].map((i) => row(i, "ready", "proposed")) } }));
    const c = all.root.querySelector<HTMLElement>(".ew-task")!;
    expect(c.querySelector(".ew-wline")?.textContent).toBe("3 份草稿待确认");
    btn(c, "[data-action=bot-confirm]").click();
    expect(all.a.botGoConfirm).toHaveBeenCalledWith("rv1");
  });

  it("the batch card goes once every draft is decided", () => {
    const batch = { suggestion: rv, actionIds: ["a1"], total: 1, loaded: true };
    const s = st([], [], { revise: { rv1: batch }, actions: { ...ready, value: [row("a1", "ready", "approved")] } });
    expect(taskCards(s)).toEqual([]);
  });
});
