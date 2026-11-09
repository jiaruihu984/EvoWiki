import { describe, expect, it } from "vitest";
import { ApiError, OfflineError } from "../api/errors";
import { WIKI_COPY } from "./copy";
import {
  ASPECT_LABEL, BAND_LABEL, PATH_STATUS_LABEL, ERROR_TEXT, cellLabel, cellLabelById, changedCount,
  diffNote, draftNotes,
  decideErrorText, errorText, foldTail, noteLine, opLine, opResult, reviewNote, staleTail, streamErrorText,
} from "./format";
import type { Names } from "./names";
import {
  ASPECTS, BANDS, type ChangeOut, type NoteKind,
  type NoteOut,
} from "./schema";

const NAMES: Record<string, string> = { c1: "z-score 标准化", c2: "均值", c3: "方差", c4: "标准差", c5: "PCA" };
const PAGES: Record<string, string> = { p1: "数据标准化（z-score）", p2: "PCA 入门" };
const names: Names = {
  concept: (id) => NAMES[id] ?? null, page: (id) => PAGES[id] ?? null,
};
const noNames: Names = { concept: () => null, page: () => null };
const heading = (sid: string): string | null => ({ s1: "为什么要标准化", s2: "z 的公式", s3: "回顾" } as Record<string, string>)[sid] ?? null;
const change = (p: Partial<ChangeOut> = {}): ChangeOut => ({
  rewritten: [], inserted: [], moved: [], folded: [], unfolded: [], llm_calls: 0, other_pages_stale: [],
  unresolved_conflicts: [], conflicts: [], budget_exceeded: false, ...p,
});
describe("label tables", () => {
  it("exact strings", () => {
    expect(ASPECT_LABEL.mechanism).toEqual({ short: "理", full: "原理 / 推导" });
    expect(BAND_LABEL.none).toBe("还没测过");
    expect(Object.keys(ASPECT_LABEL).sort()).toEqual([...ASPECTS].sort());
    expect(Object.keys(BAND_LABEL).sort()).toEqual([...BANDS].sort());
  });
});

describe("small formatters", () => {
  it("cellLabel / cellLabelById", () => {
    expect(cellLabel("均值", "term")).toBe("均值·术语 / 定义");
    expect(cellLabelById("c2.procedure", names)).toBe("均值·计算过程");
    expect(cellLabelById("cx.term", names)).toBe("未命名概念·术语 / 定义");
    expect(cellLabelById("c2.weird", names)).toBe("均值·weird");
  });
  it("changedCount dedups", () => {
    expect(changedCount(change({ rewritten: ["s1"], inserted: ["s2"], moved: ["s1"], folded: ["s3"], unfolded: ["s3"] }))).toBe(3);
    expect(changedCount(change())).toBe(0);
  });
  it("staleTail", () => {
    expect(staleTail(change())).toBe("");
    expect(staleTail(change({ other_pages_stale: ["p1", "p2"] }))).toBe("另外 2 个页面标记为“待更新”，下次打开时再处理");
  });
  it("foldTail with skip", () => {
    expect(foldTail(change(), heading)).toBe("");
    expect(foldTail(change({ folded: ["s1", "s2"] }), heading)).toBe("折叠了「为什么要标准化」「z 的公式」（你已经会了，全文还在折叠里）");
    expect(foldTail(change({ folded: ["s1", "s2"], unfolded: ["s3"] }), heading, "s1"))
      .toBe("折叠了「z 的公式」（你已经会了，全文还在折叠里），重新展开了「回顾」");
    expect(foldTail(change({ folded: ["s1"] }), heading, "s1")).toBe("");
  });
  it("opResult joins non-empty parts with ；", () => {
    const r = opResult("只重写了「回顾」这一段", change({ rewritten: ["s3"], folded: ["s1"], other_pages_stale: ["p2"], llm_calls: 2 }), heading);
    expect(r).toEqual({
      message: "只重写了「回顾」这一段；折叠了「为什么要标准化」（你已经会了，全文还在折叠里）；另外 1 个页面标记为“待更新”，下次打开时再处理",
      llmCalls: 2, changed: 2,
    });
    expect(opResult("已记下", change(), heading).message).toBe("已记下");
    expect(opResult("x", change({ other_pages_stale: ["p2"] }), heading).message).toBe("x；另外 1 个页面标记为“待更新”，下次打开时再处理");
    expect(opResult("答对了 ✓。", change({ other_pages_stale: ["p2"] }), heading).message).toBe("答对了 ✓；另外 1 个页面标记为“待更新”，下次打开时再处理");
    expect(opResult("答对了 ✓。", change(), heading).message).toBe("答对了 ✓。");
  });
  it("opLine", () => {
    const r = { message: "好了", llmCalls: 1, changed: 2 };
    expect(opLine(r, true)).toBe("好了（改了 2 段）");
    expect(opLine(r, false)).toBe("好了");
    expect(opLine({ ...r, changed: 0 }, true)).toBe("好了");
    expect(opLine({ ...r, message: "" }, true)).toBe("这一页改了 2 段。");
    expect(opLine({ ...r, message: "" }, false)).toBe("");
    // the LLM call count is internal: never shown
    expect(opLine(r, true)).not.toMatch(/LLM|调用/);
  });
});

describe("diffNote", () => {
  it("null or all empty → null", () => {
    expect(diffNote(null, heading, names)).toBeNull();
    expect(diffNote({ cell_changes: [], folded: [], unfolded: [], patched: [] }, heading, names)).toBeNull();
  });
  it("5 cell changes → 4 listed + 等 5 处; other parts", () => {
    const cc = [
      { cell: "c1.term", from: "gap", to: "known" }, { cell: "c2.term", from: "none", to: "likely" },
      { cell: "c3.notation", from: "shaky", to: "known" }, { cell: "c4.procedure", from: "shaky", to: "gap" },
      { cell: "c5.purpose", from: "none", to: "known" },
    ] as const;
    expect(diffNote({ cell_changes: [...cc], folded: ["s1"], unfolded: ["s2"], patched: ["s3", "s2"] }, heading, names)).toBe(
      "自你上次来：你的掌握情况变了：z-score 标准化·术语 / 定义 不会→确定会；均值·术语 / 定义 还没测过→可能会；"
      + "方差·公式 / 记号 模糊→确定会；标准差·计算过程 模糊→不会 等 5 处。"
      + "已折叠「为什么要标准化」，全文还在折叠里。重新展开了「z 的公式」。补写了「回顾」「z 的公式」，其余原样保留。");
  });
  it("only folded", () => {
    expect(diffNote({ cell_changes: [], folded: ["s1"], unfolded: [], patched: [] }, heading, names))
      .toBe("自你上次来：已折叠「为什么要标准化」，全文还在折叠里。");
  });
  it("an unknown concept is 未命名概念, never its id", () => {
    const out = diffNote({ cell_changes: [{ cell: "pg_x1.term", from: "gap", to: "known" }], folded: [], unfolded: [], patched: [] },
      heading, noNames);
    expect(out).toBe("自你上次来：你的掌握情况变了：未命名概念·术语 / 定义 不会→确定会。");
  });
  it("4 cell changes → no 等", () => {
    const cc = Array.from({ length: 4 }, () => ({ cell: "c2.term", from: "gap" as const, to: "known" as const }));
    expect(diffNote({ cell_changes: cc, folded: [], unfolded: [], patched: [] }, heading, names)).toBe(
      "自你上次来：你的掌握情况变了：均值·术语 / 定义 不会→确定会；均值·术语 / 定义 不会→确定会；均值·术语 / 定义 不会→确定会；均值·术语 / 定义 不会→确定会。");
  });
});

describe("unknown section headings (controller ruling: never the section id, as web format.ts)", () => {
  it("one unknown section → 另一页上的一段, unquoted; several → 另一页上的 N 段; known ones first", () => {
    expect(WIKI_COPY.otherPageSection).toBe("另一页上的一段");
    expect(foldTail(change({ folded: ["zz9"] }), heading)).toBe("折叠了另一页上的一段（你已经会了，全文还在折叠里）");
    expect(foldTail(change({ unfolded: ["s1", "zz8", "zz9"] }), heading)).toBe("重新展开了「为什么要标准化」、另一页上的 2 段");
    const d = diffNote({ cell_changes: [], folded: [], unfolded: [], patched: ["zz9"] }, heading, names);
    expect(d).toBe("自你上次来：补写了另一页上的一段，其余原样保留。");
    expect(d).not.toContain("zz9");
  });
});

describe("notes, verdicts, done", () => {
  it("noteLine", () => {
    expect(noteLine({ entry_id: "e", cell: "c1.term", kind: "self", text: "我会算均值", evidence_ref: "", label: null })).toBe("你的描述：“我会算均值”");
    const note = (kind: NoteKind, text: string): NoteOut => ({ entry_id: "e", cell: "c1.term", kind, text, evidence_ref: "", label: null });
    expect(noteLine(note("quiz", "8/3"))).toBe("答了一道题，选了「8/3」");
    expect(noteLine(note("check", "除以标准差"))).toBe("答了一道题，选了「除以标准差」");
    expect(noteLine(note("declare", "know"))).toBe("你的声明：我会");
    expect(noteLine(note("declare", "dont"))).toBe("你的声明：我不会");
    expect(noteLine(note("declare", "clear"))).toBe("你的声明：清除声明");
    expect(noteLine(note("declare", "其他"))).toBe("你的声明：其他");
    expect(noteLine(note("feedback", "太长了"))).toBe("段落反馈：太长了");
    expect(noteLine(note("ask", "为什么"))).toBe("你的提问：为什么");
    expect(noteLine(note("review", "recalled"))).toBe("复习：想起来了");
    expect(noteLine(note("review", "fuzzy"))).toBe("复习：有点模糊");
    expect(noteLine(note("review", "forgot"))).toBe("复习：没想起来");
    expect(noteLine(note("review", "除以标准差"))).toBe("复习：除以标准差");
    // Option text equal to an Object.prototype key prints as-is (own keys only).
    expect(noteLine(note("review", "constructor"))).toBe("复习：constructor");
    expect(noteLine(note("declare", "toString"))).toBe("你的声明：toString");
  });
  it("draftNotes", () => {
    expect(draftNotes({ from_background: 0, reused_concepts: 0, dropped_questions: 0 })).toEqual([]);
    // dropped questions are internal: never mentioned
    expect(draftNotes({ from_background: 2, reused_concepts: 3, dropped_questions: 1 })).toEqual([
      "从你的描述里读出了 2 条信息", "3 个概念在 Wiki 里已有，测过的不再问"]);
  });
  it("reviewNote: only what happened (记下了 for a self-grade, a real tier change, the page's folds and rewrites)", () => {
    const none: ChangeOut = { rewritten: [], inserted: [], moved: [], folded: [], unfolded: [], llm_calls: 3, other_pages_stale: ["p9"],
      unresolved_conflicts: [], conflicts: [], budget_exceeded: false };
    const o = { conceptName: "株", sectionId: "s1", heading: "基因配对", band: { cell: "c1.term", from: "likely" as const, to: "likely" as const },
      change: none, check: false, correct: null };
    expect(reviewNote(o)).toBe("记下了。");
    expect(reviewNote({ ...o, band: { ...o.band, to: "known" } })).toBe("记下了。你对「株」比之前更有把握了。");
    // 记下 only once: the lead says it
    expect(reviewNote({ ...o, band: { ...o.band, to: "gap" } })).toBe("记下了。看起来你对「株」还不太熟。");
    // 没证据 is not a tier: a change from or to it is neither more nor less sure.
    expect(reviewNote({ ...o, band: { ...o.band, from: "none", to: "shaky" } })).toBe("记下了。");
    // A check answer has no verdict sentence (user review): nothing, or only what changed.
    for (const correct of [true, false, null]) expect(reviewNote({ ...o, check: true, correct })).toBe("");
    expect(reviewNote({ ...o, check: true, correct: false, band: { ...o.band, to: "gap" } })).toBe("看起来你对「株」还不太熟。");
    expect(reviewNote({ ...o, change: { ...none, unfolded: ["s1", "s2"], folded: ["s3"], rewritten: ["s2", "s4"], inserted: ["s4", "s5"] } }))
      .toBe("记下了。「基因配对」这一节已经帮你重新展开了。这一页有 1 节重新展开了。这一页有 1 节折起来了。这一页有 3 段按你现在的情况改写了。");
    expect(reviewNote({ ...o, change: { ...none, folded: ["s1"] } })).toBe("记下了。「基因配对」这一节你已经会了，折起来了。");
    expect(reviewNote({ ...o, change: { ...none, unfolded: ["s1"] } })).not.toMatch(/LLM|调用|\d+ 次/);
  });
});

const LIMIT = "这一页的花费到了上限 $0.50，这次没有写完。可以在设置里调高。";

describe("errors", () => {
  it("errorText", () => {
    expect(errorText(new OfflineError())).toBe("未连接到服务器");
    expect(errorText(new ApiError(409, "x"))).toBe("页面刚被别的操作改过，请再试一次。");
    expect(errorText(new ApiError(503, "x"))).toBe("请切换模型重试");
    expect(errorText(new ApiError(503, "请切换模型重试"))).toBe("请切换模型重试");
    expect(errorText(new ApiError(504, "请切换模型重试", "llm_timeout"))).toBe("请切换模型重试");
    expect(errorText(new ApiError(404, "x"))).toBe("找不到这一页。");
    expect(errorText(new ApiError(422, "x"))).toBe("请求不对，请刷新后再试。");
    expect(errorText(new ApiError(500, "x"))).toBe("没能完成，请再试一次。");
    expect(errorText(new ApiError(429, LIMIT, "budget_exceeded"))).toBe(LIMIT);
    expect(errorText(new ApiError(429, "Too Many Requests"))).toBe(WIKI_COPY.budget);
    expect(errorText(new Error("x"))).toBe("没能完成，请再试一次。");
    expect(errorText(new Error("malformed event stream"))).toBe(WIKI_COPY.failed);
    expect(errorText("weird")).toBe(WIKI_COPY.failed);
  });
  it("decideErrorText: a refused ledger decision (409) says the executor's own reason; anything else as errorText", () => {
    expect(decideErrorText(new ApiError(409, "加练习的写作还没有接上", "action_refused"))).toBe("加练习的写作还没有接上");
    expect(decideErrorText(new ApiError(409, "这条改动已撤销，不能撤销", "action_refused"))).toBe("这条改动已撤销，不能撤销");
    expect(decideErrorText(new ApiError(409, "x"))).toBe("页面刚被别的操作改过，请再试一次。");
    expect(decideErrorText(new ApiError(409, '{"detail":"重复"}'))).toBe("页面刚被别的操作改过，请再试一次。");
    expect(decideErrorText(new ApiError(503, "x"))).toBe("请切换模型重试");
    expect(decideErrorText(new OfflineError())).toBe("未连接到服务器");
  });
  it("streamErrorText hides detail", () => {
    expect(streamErrorText({ code: "llm_failed", retryable: true, detail: "secret" })).toBe("请切换模型重试");
    expect(streamErrorText({ code: "conflict", retryable: false, detail: "" })).toBe(ERROR_TEXT.conflict);
    expect(streamErrorText({ code: "budget_exceeded", retryable: false, detail: LIMIT })).toBe(LIMIT);
    expect(streamErrorText({ code: "budget_exceeded", retryable: false, detail: "" })).toBe(WIKI_COPY.budget);
    expect(ERROR_TEXT.budget_exceeded).toBe(WIKI_COPY.budget);
  });
});

describe("WIKI_COPY", () => {
  it("sample strings and A5/A6", () => {
    expect(WIKI_COPY.noteSyncFailed).toBe("没能写入库里的笔记，什么都没改。");
    expect("noteSyncPending" in WIKI_COPY).toBe(false);
    expect("noteSyncReady" in WIKI_COPY).toBe(false);
    expect([WIKI_COPY.unnamedConcept, WIKI_COPY.unnamedPage]).toEqual(["未命名概念", "未命名页面"]);
  });
});

describe("learning path labels", () => {
  it("PATH_STATUS_LABEL", () => {
    expect(PATH_STATUS_LABEL).toEqual({ not_started: "还没开始", learning: "在学", done: "学完" });
  });

});
