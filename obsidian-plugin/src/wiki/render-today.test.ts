import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WIKI_COPY } from "./copy";
import { RSI_CHANGES } from "./fixtures";
import { renderEngine } from "./render-engine";
import { btn, btns, fakeActions, fakeCtx } from "./render-test-kit";
import { renderToday } from "./render-today";
import type { CatalogOut, GoalProgressOut, ReviewItemOut, StepOut } from "./schema";
import { buildStarMap, frontier } from "./starmap";
import { protoK } from "./starmap/fixture";
import { INITIAL_WIKI_STATE, type ReviewSlot, type WikiState } from "./state";

afterEach(() => { document.body.replaceChildren(); vi.useRealTimers(); });

const ready = { kind: "ready" } as const;
const draw = (s: WikiState): { root: HTMLElement; a: ReturnType<typeof fakeActions> } => {
  const a = fakeActions();
  const root = renderToday(s, a, fakeCtx());
  document.body.append(root);
  return { root, a };
};
const err = { kind: "error", message: "x" } as const;
const loaded = { ...INITIAL_WIKI_STATE, conn: "online" as const, catalog: { ...ready, value: { paths: [], maps: [], learning: [], current_page: null } as never } };

describe("todayView: the body and its blocks in order", () => {
  it("an .ew-side-body (full height, border-box) with 待办事项, 想学点新的？ and 知识图谱; never 需要你看的 (user review: 个性化 has it)", () => {
    const { root } = draw(loaded);
    expect(root.className).toBe("ew-side-body");
    expect(root.style.height).toBe("100%");
    expect(root.style.boxSizing).toBe("border-box");
    expect([...root.children].map((c) => (c as HTMLElement).dataset.section)).toEqual(["next", "learn", "map"]);
    const waiting = RSI_CHANGES.filter((c) => c.patch_id === "pb4");
    const pending = [{ proposal_id: "pr1", confirm_reason: "needs_user", proposal: { op: "collapse" } }];
    const w = draw({ ...loaded, rsi: { kind: "ready", value: waiting },
      catalog: { ...ready, value: { paths: [], maps: [], learning: [], current_page: null, pending } as never } }).root;
    expect([...w.children].map((c) => (c as HTMLElement).dataset.section)).toEqual(["next", "learn", "map"]);
    expect(w.querySelector(".ew-attn, .ew-prop, textarea[data-action^=attn]")).toBeNull();
  });

  it("no 最近改了 line, no reading / skeleton lines (the old 今天 chrome is gone)", () => {
    const live = RSI_CHANGES.filter((c) => c.patch_id === "pb3");
    const { root } = draw({ ...INITIAL_WIKI_STATE, rsi: { kind: "ready", value: live } });
    expect(root.textContent).not.toContain("最近改了");
    expect(root.textContent).not.toContain(WIKI_COPY.todayReading);
    expect(root.querySelector("[class*=evowiki]")).toBeNull();
  });

  it("知识图谱: .ew-blk > .ew-row (space-between) > h2 (15px, margin 0), then the cloud's persistent nodes", () => {
    const mini = document.createElement("div");
    const a = fakeActions();
    const root = renderToday(loaded, a, { ...fakeCtx(), cloud: { root: mini, sync: () => undefined } });
    const blk = root.querySelector<HTMLElement>("[data-section=map]")!;
    expect(blk.className).toBe("ew-blk");
    const row = blk.firstElementChild as HTMLElement;
    expect(row.className).toBe("ew-row");
    expect(row.style.justifyContent).toBe("space-between");
    const h2 = row.querySelector("h2")!;
    expect(h2.textContent).toBe("知识图谱");
    expect(h2.getAttribute("style")).toBe("font-size:15px;margin:0");
    expect([...blk.children].slice(1)).toEqual([mini]);
  });

  it("the goal rows follow the thumbnail inside the same block", () => {
    const mini = document.createElement("div"), gr = document.createElement("div"), gl = document.createElement("div");
    const root = renderToday(loaded, fakeActions(), { ...fakeCtx(), cloud: { root: mini, sync: () => undefined, goals: { root: gr, line: gl } } });
    const blk = root.querySelector<HTMLElement>("[data-section=map]")!;
    expect([...blk.children].slice(1)).toEqual([mini]);
    expect(blk.nextElementSibling).toBe(gr);          // a sibling after the map block (demo: b.append(mm); b.append(goalsBlock()))
    expect(gr.nextElementSibling).toBe(gl);
  });

  it("demo: a .ew-muted line at the top of the body; offline is no line in the body (the sidebar header has the one banner)", () => {
    const { root } = draw({ ...loaded, conn: "offline", demo: true });
    const first = root.children[0] as HTMLElement;
    expect(first.className).toBe("ew-muted");
    expect(first.textContent).toBe(WIKI_COPY.demoBanner);
    expect(root.querySelector("[data-role=offline]")).toBeNull();
    expect(root.textContent).not.toContain(WIKI_COPY.notConnected);
  });

  it("个性化: .ew-side-body.ew-pz (the demo engineView), full height", () => {
    const b = renderEngine(loaded, fakeActions());
    expect(b.className).toBe("ew-side-body ew-pz");
    expect(b.style.height).toBe("100%");
  });
});

// ---- 待办事项 and the in-place review ----
const item = (cell: string, over: Partial<ReviewItemOut> = {}): ReviewItemOut => ({
  card_id: `card-${cell}`, cell, cells: [cell], concept_id: "c1", concept_name: "均值", aspect: "procedure", band: "likely", mode: "cue",
  page_id: "p1", page_title: "页一", section_id: "s1", heading: `小节${cell}`, cue: `线索${cell}`, answer: `正文${cell}`, check: null, recall_now: 0.5,
  days_to_threshold: -1, in_goal: true, exam: false, artifact_id: null, ...over });
const check = (cell: string): ReviewItemOut => item(cell, { mode: "check", cue: "", check: { stem: `题目${cell}`, options: ["甲", "乙", "丙"] } });
const pcaCard = { path_id: "pca", goal: "主成分分析", pages: 4, done: 1, next_index: 2, map_id: null };
function today(items: ReviewItemOut[], o: { review?: Partial<ReviewSlot>; paths?: CatalogOut["paths"]; plan?: boolean } & Partial<Omit<WikiState, "review">> = {}): WikiState {
  const { review, paths, plan, ...rest } = o;
  return { ...loaded, catalog: { ...ready, value: { paths: paths ?? [], maps: [], learning: [], current_page: null } as never },
    pathPlans: plan ? { pca: { path_id: "pca", goal: "主成分分析", source: "llm", next_index: 2, prefetch: null, sources: [],
      pages: [{ index: 2, aim: "特征值与特征向量", concepts: [], status: "not_started", state: "planned" as const, progress: null, page_id: null, trashed: false, skipped: false, key: "", title: "", abstract: "", sections: [], exam: 0 }] } } : {},
    review: { ...INITIAL_WIKI_STATE.review, today: { ...ready, value: { items, due: items.length, done_today: 0, limit: 10, confirms: [], page_round_today: 0, streak_days: 0 } }, ...review },
    // the server's step (GET /wiki/progress): the first path with a next page, else none
    progress: { kind: "ready", value: { goals: (paths ?? []).map((c) => goalOf(c)), today: stepOf((paths ?? []).find((p) => p.next_index !== null)) } },
    ...rest };
}
function goalOf(card: CatalogOut["paths"][number]): GoalProgressOut {
  const step = stepOf(card.next_index === null ? undefined : card);
  return { goal_id: card.path_id, kind: "path", goal: card.goal, state: card.next_index === null ? "done" : "learning", blocks: [], path_ids: [card.path_id],
    pages_done: card.done, pages_total: card.pages, step: card.next_index === null ? { ...step, kind: "goal_done" } : step };
}
function stepOf(card: CatalogOut["paths"][number] | undefined): StepOut {
  const base = { goal_id: card?.path_id ?? null, goal: card?.goal ?? "", map_id: null, region_id: null, region_name: "", path_id: card?.path_id ?? null,
    page_id: null, draft_id: null, progress: null, failure: null, review_first: null };
  return card === undefined ? { ...base, kind: "none", index: null, title: "" } : { ...base, kind: "start_page", index: card.next_index, title: "特征值与特征向量" };
}
const next = (root: HTMLElement): HTMLElement => root.querySelector<HTMLElement>("[data-section=next]")!;

const botReady = (value: never[] = []): Partial<WikiState> => ({ bot: { ...INITIAL_WIKI_STATE.bot, today: { ...ready, value } } });

describe("待办事项 (nextBlock)", () => {
  it("a .ew-blk with h2 待办事项; cards to review: one .ew-card.ew-task (复习 N 张 · 大约 N 分钟, the point names), no 继续 bigbtn", () => {
    const { root, a } = draw(today([item("a"), item("b"), item("c")], { paths: [pcaCard], plan: true, ...botReady() }));
    const sec = next(root);
    expect(sec.className).toBe("ew-blk");
    expect(sec.querySelector("h2")?.textContent).toBe("待办事项");
    const c = sec.querySelector<HTMLElement>(".ew-card.ew-task")!;
    expect(c.querySelector("h2")?.textContent).toBe("今日复习");
    expect(c.querySelector(".ew-thead .ew-faint")?.textContent).toBe("3 张 · 约 3 分钟");
    expect(c.querySelector(".ew-muted")?.textContent).toBe("均值");
    expect(sec.querySelector("button.ew-bigbtn, [data-role=then]")).toBeNull();
    btn(c, "[data-action=bot-start]").click();
    expect(a.botStart).toHaveBeenCalledWith("review");
  });

  it("no cards but a path with a next page: where learning goes on (the goal, 继续学习), never 今天的学习已完成 (D05)", () => {
    const { root, a } = draw(today([], { paths: [pcaCard], plan: true, review: { finished: true }, ...botReady() }));
    expect(next(root).querySelector(".ew-alldone")).toBeNull();
    expect(next(root).querySelector(".ew-card h2")!.textContent).toBe("主成分分析");
    next(root).querySelector<HTMLButtonElement>("button[data-action=continue]")!.click();
    expect(a.continueLearning).toHaveBeenCalledOnce();
  });

  it("no goal at all: nothing to do is not something done — no 今天的学习已完成 box (D05)", () => {
    const { root } = draw(today([], { paths: [], review: { finished: true }, ...botReady() }));
    expect(next(root).querySelector(".ew-alldone, button[data-action=continue]")).toBeNull();
    expect(next(root).querySelector("[data-role=next-empty]")?.textContent).toBe("暂无待办");
  });

  it("no cards and every goal done: the 今天的学习已完成 box", () => {
    const { root } = draw(today([], { paths: [{ ...pcaCard, next_index: null }], plan: true, review: { finished: true }, ...botReady() }));
    expect(next(root).querySelector(".ew-alldone")?.textContent).toBe("今天的学习已完成");
    expect(next(root).querySelector("button, [data-role=review-done]")).toBeNull();
  });

  it("the Bot list not read yet: no all-done box (an unread list is not an empty one)", () => {
    const { root } = draw(today([]));
    expect(next(root).querySelector(".ew-alldone, button")).toBeNull();
    expect(next(root).querySelector("[data-role=next-loading]")?.textContent).toBe("正在加载待办…");
    expect(next(root).querySelector("[data-role=next-empty]")).toBeNull();
  });

  it("a failed read (server reachable) says so once in this block (.ew-muted 没能读到， + .ew-btn.ew-link 重试); never the all-done box", () => {
    const s = today([], { paths: [pcaCard], ...botReady() });
    const { root, a } = draw({ ...s, review: { ...s.review, today: err } });
    const sec = next(root);
    const line = sec.querySelector<HTMLElement>("[data-role=next-error]")!;
    expect(line.className).toBe("ew-muted");
    expect(line.textContent).toBe("没能读到，重试");
    const retry = btn(line, "button");
    expect([retry.className, retry.textContent]).toEqual(["ew-btn ew-link", "重试"]);
    retry.click();
    expect(a.retryConnection).toHaveBeenCalledTimes(1);
    expect(sec.querySelector(".ew-alldone")).toBeNull();
    const cat = draw({ ...today([item("a")], botReady()), catalog: err }).root;
    expect(next(cat).querySelectorAll("[data-role=next-error]")).toHaveLength(1);
    const bot = draw({ ...today([]), bot: { ...INITIAL_WIKI_STATE.bot, today: err } }).root;
    expect(next(bot).querySelectorAll("[data-role=next-error]")).toHaveLength(1);
    expect(next(bot).querySelector(".ew-alldone")).toBeNull();
    // Three reads failed: still one line.
    const all3 = draw({ ...today([]), catalog: err, review: { ...s.review, today: err }, bot: { ...INITIAL_WIKI_STATE.bot, today: err } }).root;
    expect(next(all3).querySelectorAll("[data-role=next-error]")).toHaveLength(1);
    // Offline: the block says nothing about it (the sidebar banner does).
    const off = draw({ ...s, conn: "offline", review: { ...s.review, today: err }, catalog: err }).root;
    expect(next(off).querySelector("[data-role=next-error]")).toBeNull();
    expect(next(off).querySelector(".ew-alldone")).toBeNull();
  });

  it("not set up: the settings sentence in the block", () => {
    const { root } = draw({ ...INITIAL_WIKI_STATE, configured: false });
    expect(next(root).querySelector("[data-role=needs-settings]")?.textContent).toBe(WIKI_COPY.needsSettings);
  });
});

describe("in-place review (reviewCard)", () => {
  const active = { active: true };
  const card = (root: HTMLElement): HTMLElement => root.querySelector<HTMLElement>(".ew-card[data-role=review]")!;
  it("a .ew-card: .ew-rvtop (bar + 反馈 + ×), .ew-rv-q = the recall cue, .ew-rv-src 页面 · 小节; the answer hidden: 先试着回忆，再查看答案。 + primary 查看答案", () => {
    const { root, a } = draw(today([item("a"), item("b")], { review: active }));
    const c = card(root);
    expect(c.className).toBe("ew-card");
    expect(c.querySelector(".ew-rv-src")?.textContent).toBe("页一 · 小节a");
    expect([...c.querySelectorAll(".ew-rvtop > *")].map((x) => x.className)).toEqual(["ew-rvbar", "ew-btn ew-ghost", "ew-ib"]);
    expect(c.querySelector(".ew-rv-q")?.textContent).toBe("线索a");
    expect(c.textContent).not.toContain("正文a");
    expect(c.querySelector(".ew-muted")?.textContent).toBe("先试着回忆，再查看答案。");
    const r = btn(c, "[data-action=review-reveal]");
    expect([r.className, r.textContent]).toEqual(["ew-btn ew-primary", "查看答案"]);
    r.click();
    expect(a.revealCue).toHaveBeenCalledWith("a");
  });

  it("after 看答案: .ew-tipcard (the card's answer, as Markdown), 跳到这一节, .ew-grades 忘了 / 模糊 / 熟悉 → gradeReview", () => {
    const { root, a } = draw(today([item("a")], { review: { ...active, revealed: ["a"] } }));
    expect(root.querySelector(".ew-tipcard")?.textContent).toBe("正文a");
    btn(root, "[data-action=review-to-section]").click();
    expect(a.goToChange).toHaveBeenCalledWith("p1", "s1");
    expect(root.querySelector(".ew-rv-jump + .ew-grades")).not.toBeNull();
    const grades = btns(root, ".ew-grades > button");
    expect(grades.map((g) => g.textContent)).toEqual(["忘了", "模糊", "熟悉"]);
    expect(grades.every((g) => g.className === "ew-btn")).toBe(true);
    grades[1]!.click();
    expect(a.gradeReview).toHaveBeenCalledWith("a", "fuzzy");
  });

  it("a graded cue: no result line, just primary 下一张 / 完成 (no follow-ups)", () => {
    const line = { text: "记下了。你对「均值」比之前更有把握了。", tone: "ok" } as const;
    const result = { band: { cell: "a", from: "likely", to: "known" }, grade: "recalled", correct: null, correctIndex: null, choice: null, line } as const;
    const { root, a } = draw(today([item("a"), item("b")], { review: { ...active, revealed: ["a"], results: { a: result } } }));
    expect(root.querySelector(".ew-res, .ew-okline")).toBeNull();
    expect(root.querySelector(".ew-grades")).toBeNull();
    expect(root.querySelector("[data-role=followup]")).toBeNull();
    const n = btn(root, "[data-action=review-next]");
    expect([n.className, n.textContent]).toEqual(["ew-btn ew-primary", "下一张"]);
    n.click();
    expect(a.nextReview).toHaveBeenCalledWith("a");
    const last = draw(today([item("a")], { review: { ...active, revealed: ["a"], results: { a: result } } })).root;
    expect(btn(last, "[data-action=review-next]").textContent).toBe("完成本轮复习");
  });

  it("a check card: .ew-check (style border:0;padding:0;margin:0) > .ew-opts with A. … and the dashed .ew-idk 不知道", () => {
    const { root, a } = draw(today([check("k")], { review: active }));
    expect(root.querySelector(".ew-rv-q")?.textContent).toBe("题目k");
    const box = root.querySelector<HTMLElement>(".ew-check")!;
    expect(box.getAttribute("style")).toBe("border: 0px; padding: 0px; margin: 0px;");
    const opts = btns(box, ".ew-opts > button");
    expect(opts.map((o) => o.textContent)).toEqual(["A. 甲", "B. 乙", "C. 丙", "不知道"]);
    expect(opts.map((o) => o.className)).toEqual(["ew-o", "ew-o", "ew-o", "ew-o ew-idk"]);
    opts[2]!.click();
    expect(a.answerReview).toHaveBeenCalledWith("k", 2);
    opts[3]!.click();
    expect(a.answerReview).toHaveBeenLastCalledWith("k", -1);
  });

  it("an answered check: options off with ew-right / ew-wrong, no result sentence (user review), follow-ups after a wrong one, then 下一张", () => {
    const res = (choice: number, correct: boolean) => ({ k: { band: { cell: "k", from: "likely", to: "shaky" }, grade: null, correct, correctIndex: 0, choice,
      line: { text: "x", tone: "ok" } } }) as never;
    const wrong = draw(today([check("k")], { review: { ...active, results: res(2, false) } })).root;
    const opts = btns(wrong, ".ew-opts > button");
    expect(opts.every((o) => o.disabled)).toBe(true);
    expect(opts.map((o) => o.className)).toEqual(["ew-o ew-right", "ew-o", "ew-o ew-wrong", "ew-o ew-idk"]);
    expect(wrong.querySelector(".ew-res")).toBeNull();
    const f = wrong.querySelector<HTMLElement>("[data-role=followup]")!;
    expect(f.className).toBe("ew-follow");
    expect(btns(f, ".ew-row > button").map((b) => b.textContent)).toEqual(["粗心答错", "现在会了", "还没掌握"]);
    expect(f.nextElementSibling?.getAttribute("data-action")).toBe("review-next");
    const idk = draw(today([check("k")], { review: { ...active, results: res(-1, false) } })).root;
    expect(idk.querySelector(".ew-res")).toBeNull();
    expect(btns(idk, "[data-role=followup] .ew-row > button").map((b) => b.textContent)).toEqual(["现在会了", "还没掌握"]);
    expect(btns(idk, ".ew-opts > button")[3]!.className).toBe("ew-o ew-idk ew-wrong");
    const right = draw(today([check("k")], { review: { ...active, results: res(0, true) } })).root;
    expect(right.querySelector(".ew-res")).toBeNull();
    expect(btns(right, ".ew-opts > button")[0]!.className).toBe("ew-o ew-right");
    expect(right.querySelector("[data-role=followup]")).toBeNull();
  });

  it("follow-ups once noted: no sentence, the row stays (chosen aria-pressed, others disabled); 看不懂 adds no link", () => {
    const res = { k: { band: { cell: "k", from: "likely", to: "shaky" }, grade: null, correct: false, correctIndex: 0, choice: 1, line: { text: "x", tone: "ok" } } } as never;
    const unclear = { kind: "done", choice: "unclear", note: "" } as const;
    const { root } = draw(today([check("k")], { review: { ...active, results: res }, followups: { "card:card-k": unclear } }));
    const f = root.querySelector<HTMLElement>("[data-role=followup]")!;
    expect(f.querySelector(".ew-res, .ew-muted")).toBeNull();
    const row = btns(f, ".ew-row > button");
    expect(row.map((b) => [b.textContent, b.getAttribute("aria-pressed"), b.disabled])).toEqual([
      ["粗心答错", null, true], ["现在会了", null, true], ["还没掌握", "true", false]]);
    expect(f.querySelector("button.ew-lk")).toBeNull();      // 看不懂 only goes to the assistant: no 去复习 link
    const slip = { kind: "done", choice: "slip", note: "" } as const;
    const s2 = draw(today([check("k")], { review: { ...active, results: res }, followups: { "card:card-k": slip } })).root;
    expect(s2.querySelector("[data-role=followup] .ew-res, [data-role=followup] .ew-muted")).toBeNull();
    expect(s2.querySelector("[data-role=followup] [aria-pressed=true]")?.textContent).toBe("粗心答错");
    expect(s2.querySelector("[data-role=followup] button.ew-lk")).toBeNull();
  });

  it("removed cards leave the count; 反馈 (a ghost text button) and × (结束复习, an icon button) in .ew-rvtop", () => {
    const { root, a } = draw(today([item("a"), item("b")], { review: { ...active, removed: ["a"] } }));
    expect(root.querySelector(".ew-rv-q")?.textContent).toBe("线索b");
    const pause = btn(root, "[data-action=review-pause]");
    const row = pause.parentElement!;
    expect(row.className).toBe("ew-rvtop");
    expect([...row.querySelectorAll("button")].map((b) => [b.className, b.title, b.getAttribute("aria-label")])).toEqual([
      ["ew-btn ew-ghost", "", null], ["ew-ib", "", "结束复习"]]);
    pause.click();
    expect(a.pauseReview).toHaveBeenCalledTimes(1);
  });

  it("offline or busy: grades and options are disabled and a .ew-muted line says why", () => {
    const off = draw(today([item("a")], { review: { ...active, revealed: ["a"] }, conn: "offline" }));
    expect(btns(off.root, "[data-action=review-grade]").every((g) => g.disabled)).toBe(true);
    expect(off.root.querySelector("[data-role=blocked-why]")?.textContent).toBe(WIKI_COPY.reviewBlockedOffline);
    const busy = draw(today([check("k")], { review: active, busy: { what: "review-grade", abortable: false } }));
    expect(btns(busy.root, "[data-action=review-answer]").every((g) => g.disabled)).toBe(true);
    expect(busy.root.querySelector("[data-role=blocked-why]")?.textContent).toBe(WIKI_COPY.reviewBlockedBusy);
    const ok = draw(today([item("a")], { review: { ...active, revealed: ["a"] } }));
    expect(ok.root.querySelector("[data-role=blocked-why]")).toBeNull();
  });

  it("a failed grade: its plain line (.ew-muted) in the card", () => {
    const { root } = draw(today([item("a")], { review: { ...active, note: { text: "没能完成，请再试一次。", tone: "error" } } }));
    expect(root.querySelector("[data-role=review-error]")?.textContent).toBe("没能完成，请再试一次。");
  });
});

describe("反馈 on a review card", () => {
  it("a small ghost text button before × 结束复习; a click hands the card to the assistant (no box in the card)", () => {
    const { root, a } = draw(today([item("a")], { review: { active: true } }));
    const b = btn(root, "[data-action=card-fb]");
    expect([b.className, b.textContent]).toEqual(["ew-btn ew-ghost", "反馈"]);
    expect(b.nextElementSibling?.getAttribute("aria-label")).toBe("结束复习");
    b.click();
    expect(a.askAboutCard).toHaveBeenCalledWith("a");
    expect(root.querySelector("textarea")).toBeNull();
  });
});

// ---- 想学点新的？ ----
const withK = (over: Partial<WikiState> = {}): WikiState => ({ ...loaded, k: { kind: "ready", value: protoK() }, ...over });
const learn = (root: HTMLElement): HTMLElement => root.querySelector<HTMLElement>("[data-section=learn]")!;

describe("想学点新的？ (learnCard)", () => {
  it("the learning card has a title without a subtitle and an explicit start button", () => {
    const { root } = draw(withK());
    const c = learn(root);
    expect(c.className).toBe("ew-card ew-feature");
    const hd = c.querySelector<HTMLElement>(":scope > .ew-lhead")!;
    expect([...hd.children].map((x) => [x.tagName, x.className])).toEqual([["H2", ""]]);
    expect(hd.querySelector("h2")?.textContent).toBe("你想学什么？");
    expect(hd.querySelector(".ew-lquote")).toBeNull();
    const row = c.querySelector<HTMLElement>(".ew-row")!;
    expect(row.style.flexWrap).toBe("nowrap");
    expect(row.querySelector("input")?.placeholder).toBe("贝叶斯定理、傅里叶变换");
    const start = btn(row, "[data-action=learn-start]");
    expect([start.className, start.textContent, start.disabled]).toEqual(["ew-btn ew-primary", "开始学习", false]);
    expect(c.textContent).not.toContain("从你会的地方往外走一步");
  });

  it("typing is kept (local); 开始 and Enter open the 学点新的 flow with the topic", () => {
    const { root, a } = draw(withK());
    const input = learn(root).querySelector("input")!;
    input.value = " 贝叶斯定理 ";
    input.dispatchEvent(new Event("input"));
    expect(a.setLearnTopic).toHaveBeenCalledWith(" 贝叶斯定理 ");
    btn(learn(root), "[data-action=learn-start]").click();
    expect(a.openLearnNew).toHaveBeenCalledWith("贝叶斯定理");
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" }));
    expect(a.openLearnNew).toHaveBeenCalledTimes(2);
    expect(draw(withK({ learnTopic: "傅里叶变换" })).root.querySelector<HTMLInputElement>("[data-section=learn] input")?.value).toBe("傅里叶变换");
  });

  it(".ew-rec: the frontier (at most 3), each <b>name</b><span>reason</span>, → openLearnNew(name)", () => {
    const want = frontier(buildStarMap(protoK(), { goals: [], membership: {} }), { max: 3 });
    expect(want.length).toBeGreaterThan(0);
    const { root, a } = draw(withK());
    const rows = btns(learn(root), ".ew-rec > button");
    expect(rows.map((r) => [r.children[0]?.tagName, r.children[0]?.textContent, r.children[1]?.tagName, r.children[1]?.textContent]))
      .toEqual(want.map((r) => ["B", r.name, "SPAN", r.reason]));
    rows[0]!.click();
    expect(a.openLearnNew).toHaveBeenCalledWith(want[0]!.name);
  });

  it("K failed: a .ew-muted line with 重试 in place of the recommendations; K not read yet: nothing", () => {
    const { root, a } = draw(withK({ k: err }));
    const line = learn(root).querySelector<HTMLElement>("[data-role=learn-error]")!;
    expect(line.className).toBe("ew-muted");
    btn(line, "button").click();
    expect(a.retryConnection).toHaveBeenCalledOnce();
    expect(learn(draw(withK({ k: { kind: "loading" } })).root).querySelector(".ew-rec, [data-role=learn-error]")).toBeNull();
  });

  it("offline / not set up: 开始 and the rows are off, the reason is said except offline (the banner); a page being written holds nothing up", () => {
    const off = draw(withK({ conn: "offline" }));
    expect(btn(learn(off.root), "[data-action=learn-start]").disabled).toBe(true);
    expect(btns(learn(off.root), ".ew-rec > button").every((b) => b.disabled)).toBe(true);
    expect(learn(off.root).querySelector("[data-role=blocked]")).toBeNull();
    const unset = draw(withK({ configured: false }));
    expect(learn(unset.root).querySelector("[data-role=blocked]")?.textContent).toBe(WIKI_COPY.needsSettings);
    const busy = draw(withK({ busy: { what: "organize", abortable: true } }));
    expect(btn(learn(busy.root), "[data-action=learn-start]").disabled).toBe(false);
    expect(learn(busy.root).querySelector("[data-role=blocked]")).toBeNull();
  });
});

describe("review polish: progress bar, right-answer pop, okline", () => {
  // The 500 ms answer window reads Date.now(): frozen so a slow run cannot close it mid-test.
  beforeEach(() => { vi.useFakeTimers({ toFake: ["Date"] }); vi.setSystemTime(new Date(2026, 9, 15, 9, 0)); });
  const rv = (root: HTMLElement): HTMLElement => root.querySelector<HTMLElement>(".ew-card[data-role=review]")!;
  const res = (cell: string, choice: number, correct: boolean) => ({ [cell]: { band: { cell, from: "likely", to: "shaky" }, grade: null, correct, correctIndex: 0, choice,
    line: { text: "x", tone: "ok" } } }) as never;
  const cueRes = (cell: string, grade: string) => ({ [cell]: { band: { cell, from: "likely", to: "known" }, grade, correct: null, correctIndex: null, choice: null,
    line: { text: "记下了。", tone: "ok" } } }) as never;
  const frames = (): Promise<void> => new Promise((r) => { requestAnimationFrame(() => { requestAnimationFrame(() => { r(); }); }); });

  it("the bar is the first child, .ew-rvbar > i; 1 of 3 answered = 33.33%; it starts at the old width and slides on the next frame", async () => {
    const s = today([check("a"), check("b"), check("c")], { review: { active: true, results: res("a", 0, true), barAt: 0 } });
    const { root, a } = draw(s);
    const c = rv(root);
    expect(c.firstElementChild?.className).toBe("ew-rvtop");
    expect(c.firstElementChild?.firstElementChild?.className).toBe("ew-rvbar");
    const fill = c.querySelector<HTMLElement>(".ew-rvbar > i")!;
    expect(fill.style.width).toBe("0%");
    expect(a.markBar).not.toHaveBeenCalled();
    await frames();
    expect(parseFloat(fill.style.width)).toBeCloseTo(33.333, 2);
    // The new position goes through the action, so it lands on the review slot that is current by then (not the one drawn from).
    expect(a.markBar).toHaveBeenCalledWith(1);
    expect(s.review.barAt).toBe(0);
  });

  it("a fresh round (barAt null) draws the bar straight at the current width; seen cards count as done", () => {
    const { root } = draw(today([check("a"), check("b")], { review: { active: true, seen: ["a"] } }));
    expect(parseFloat(rv(root).querySelector<HTMLElement>(".ew-rvbar > i")!.style.width)).toBeCloseTo(50, 5);
  });

  it("a right check answer: ew-pop and .ew-okline.ew-anim (tick + 答对了) while the stamp is under 500 ms old, also on a re-render; plain after", () => {
    vi.useFakeTimers();
    const s = today([check("k")], { review: { active: true, results: res("k", 0, true), fresh: { key: "k", at: Date.now() } } });
    for (let pass = 0; pass < 2; pass++) {
      const c = rv(draw(s).root);
      expect(btns(c, ".ew-opts > button")[0]!.className).toBe("ew-o ew-right ew-pop");
      const ok = c.querySelector<HTMLElement>(".ew-okline")!;
      expect(ok.className).toBe("ew-okline ew-anim");
      expect(ok.textContent).toBe("答对了");
      expect(ok.querySelector("svg path")?.getAttribute("d")).toBe("M5 12.5l4.5 4.5L19 7.5");
      expect(c.querySelector(".ew-res")).toBeNull();
    }
    vi.advanceTimersByTime(500);
    const late = rv(draw(s).root);
    expect(btns(late, ".ew-opts > button")[0]!.className).toBe("ew-o ew-right");
    expect(late.querySelector(".ew-okline")?.className).toBe("ew-okline");
    const other = rv(draw({ ...s, review: { ...s.review, fresh: { key: "zz", at: Date.now() } } }).root);
    expect(other.querySelector(".ew-pop")).toBeNull();
  });

  it("a fresh node's CSS animations are moved to the elapsed time (when the browser has getAnimations)", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    const an = { currentTime: 0 };
    Object.defineProperty(HTMLElement.prototype, "getAnimations", { configurable: true, value: () => [an] });
    try {
      vi.setSystemTime(10_000);
      draw(today([check("k")], { review: { active: true, results: res("k", 0, true), fresh: { key: "k", at: 9_800 } } }));
      await Promise.resolve();
      expect(an.currentTime).toBe(200);
    } finally {
      delete (HTMLElement.prototype as { getAnimations?: unknown }).getAnimations;
    }
  });

  it("a wrong check answer or 不知道: no okline, no pop", () => {
    const wrong = rv(draw(today([check("k")], { review: { active: true, results: res("k", 2, false) } })).root);
    expect(wrong.querySelector(".ew-okline")).toBeNull();
    expect(wrong.querySelector(".ew-pop")).toBeNull();
  });

  it("a self-graded cue has neither okline nor result line", () => {
    const good = rv(draw(today([item("a")], { review: { active: true, revealed: ["a"], results: cueRes("a", "recalled"), fresh: { key: "a", at: Date.now() } } })).root);
    expect(good.querySelector(".ew-okline, .ew-res")).toBeNull();
    const forgot = rv(draw(today([item("a")], { review: { active: true, revealed: ["a"], results: cueRes("a", "forgot") } })).root);
    expect(forgot.querySelector(".ew-okline")).toBeNull();
  });

  it("a right confirmation answer gets pop + okline; a wrong one does not", () => {
    const conf = { followup_id: "f1", concept_id: "c1", concept_name: "概念", page_id: "p1", page_title: "页一", section_id: "s1",
      check: { stem: "确认题", options: ["甲", "乙"] } } as never;
    const mk = (correct: boolean): HTMLElement => {
      const s = today([], { review: { active: true } });
      const v = s.review.today;
      if (v.kind !== "ready") throw new Error("x");
      s.review.today = { kind: "ready", value: { ...v.value, confirms: [conf] } };
      s.review.fresh = { key: "confirm:f1", at: Date.now() };
      s.review.confirmResults = { f1: { choice: correct ? 0 : 1, line: null,
        out: { followup_id: "f1", correct, correct_index: 0, outcome: correct ? "confirmed" : "refuted", band: {}, reteach: null, signal_id: null } } } as never;
      return rv(draw(s).root);
    };
    const right = mk(true);
    expect(btns(right, ".ew-opts > button")[0]!.className).toBe("ew-o ew-right ew-pop");
    expect(right.querySelector(".ew-okline.ew-anim")?.textContent).toBe("答对了");
    expect(mk(false).querySelector(".ew-okline")).toBeNull();
  });
});
