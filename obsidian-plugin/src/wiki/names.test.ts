import { describe, expect, it } from "vitest";
import { NameBook } from "./names";
import type { CatalogOut, CellOut, DraftConceptOut, GoalDraftOut, KConceptOut, KGridOut, PageOut } from "./schema";

const card = (page_id: string, title: string) => ({ page_id, title, goal: "", stale: false, done: false, organized: true });
const cell = (concept_id: string, concept_name: string): CellOut =>
  ({ cell: `${concept_id}.term`, concept_id, concept_name, aspect: "term", band: "known", declared: null });

const catalog: CatalogOut = {
  learning: [card("p1", "数据标准化（z-score）")],
  shelf: { pages: [card("p0", "均值复习")] },
  settings: { review_per_day: 10, page_limit_usd: null, language: "zh" }, current_page: null, names: { c1: "z-score 标准化", c2: "均值" }, paths: [], maps: [],
};
const kc: KConceptOut = { id: "c3", name: "方差", role: null, aspects: [], prereqs: [], on_current_page: false, cells: [], notes: [] };
const k: KGridOut = { concepts: [kc] };
const page = (title: string): PageOut => ({
  written_for: "", goal_notes: [], next_step: null, page_id: "p1", title, folder: "", parent_name: "", trashed: false, goal: "", organized: true, source: "organizer", profile: "", assumed: [cell("c4", "标准差")],
  sections: [{ id: "s1", heading: "h", role: "line", cue: "", body: "", folded: false, fold_locked: false, foldable: false,
    pinned: false, changed: null, why: "", cells: [cell("c6", "协方差")], lookups: [], note_body: null, fingerprint: "", artifact_id: null, check: null }],
  diff: null, unresolved_conflicts: [], conflicts: [], budget_exceeded: false,
  collapsed: false, can_shelve: false, shelf_line: "", done: false, llm_calls: 0, writer: null, feedback: null, hand_edit: null, rewriting: false, sources: [],
});
const dc: DraftConceptOut = { id: "c5", name: "PCA 主成分分析", target: true, reused: false, aspects: [], prereqs: [] };
const draft: GoalDraftOut = {
  draft_id: "d1", page_id: null, path_id: null, map_id: null, title: "t",
  round: 1, round_title: "", round_why: "", results: [], map: null, decisions: [],
  concepts: [dc],
  questions: [], notes: { from_background: 0, reused_concepts: 0, dropped_questions: 0 },
};

describe("NameBook", () => {
  it("finds names from every source", () => {
    const b = new NameBook();
    b.addCatalog(catalog); b.addNames(catalog.names); b.addK(k); b.addPage(page("数据标准化（z-score）")); b.addDraft(draft);
    expect(b.page("p1")).toBe("数据标准化（z-score）");
    expect(b.page("p0")).toBe("均值复习");
    expect(b.concept("c1")).toBe("z-score 标准化");
    expect(b.concept("c2")).toBe("均值");
    expect(b.concept("c3")).toBe("方差");
    expect(b.concept("c4")).toBe("标准差");
    expect(b.concept("c6")).toBe("协方差");
    expect(b.concept("c5")).toBe("PCA 主成分分析");
  });
  it("unknown ids are null", () => {
    const b = new NameBook();
    expect(b.page("x")).toBeNull(); expect(b.concept("x")).toBeNull();
  });
  it("catalog and page titles are authoritative: the latest one wins", () => {
    const b = new NameBook();
    b.addCatalog(catalog); b.addPage(page("别的标题"));
    expect(b.page("p1")).toBe("别的标题");
    b.addCatalog(catalog);
    expect(b.page("p1")).toBe("数据标准化（z-score）");
  });
  it("a seeded (draft) title only fills a gap: it never replaces a catalog / page title, and they replace it", () => {
    const b = new NameBook();
    b.addPage(page("学会 z-score 标准化"), false);
    expect(b.page("p1")).toBe("学会 z-score 标准化");
    b.addCatalog(catalog);
    expect(b.page("p1")).toBe("数据标准化（z-score）");
    b.addPage(page("草稿标题"), false);
    expect(b.page("p1")).toBe("数据标准化（z-score）");
    const c = new NameBook();
    c.addPage(page("草稿标题"), false); c.addPage(page("正式标题"));
    expect(c.page("p1")).toBe("正式标题");
  });
  it("an empty catalog / page title does not erase a known name", () => {
    const b = new NameBook();
    b.addCatalog(catalog); b.addPage(page(""));
    expect(b.page("p1")).toBe("数据标准化（z-score）");
  });
  it("empty names do not block later non-empty ones", () => {
    const b = new NameBook();
    b.addK({ concepts: [{ ...kc, id: "c9", name: "" }] });
    expect(b.concept("c9")).toBeNull();
    b.addDraft({ ...draft, concepts: [{ ...dc, id: "c9", name: "相关系数" }] });
    expect(b.concept("c9")).toBe("相关系数");
  });
  it("addNames: a response's names map is authoritative for concepts and pages (replaces what was there)", () => {
    const b = new NameBook();
    b.addCatalog(catalog); b.addK(k);
    b.addNames({ c1: "标准分数", c3: "样本方差", p1: "z-score 入门", c7: "相关系数", p7: "回归" });
    expect([b.concept("c1"), b.concept("c3"), b.concept("c7")]).toEqual(["标准分数", "样本方差", "相关系数"]);
    expect([b.page("p1"), b.page("p7")]).toEqual(["z-score 入门", "回归"]);
    // gap fillers (K, chips, cells) do not replace a served name; a later served name does
    b.addK(k); b.addCatalog(catalog);
    expect(b.concept("c3")).toBe("样本方差");
    expect(b.concept("c1")).toBe("标准分数");
    b.addNames({ c3: "方差" });
    expect(b.concept("c3")).toBe("方差");
    // a later catalog / page title is authoritative too: the latest one wins
    expect(b.page("p1")).toBe("数据标准化（z-score）");
  });
  it("addNames: the server's 未命名… placeholders and empty names never replace a known name", () => {
    const b = new NameBook();
    b.addCatalog(catalog); b.addNames(catalog.names); b.addK(k);
    b.addNames({ c3: "未命名概念", p1: "未命名页面", c2: "" });
    expect([b.concept("c3"), b.page("p1"), b.concept("c2")]).toEqual(["方差", "数据标准化（z-score）", "均值"]);
    b.addNames({ c8: "未命名概念", p8: "未命名页面" });
    expect([b.concept("c8"), b.page("p8")]).toEqual([null, null]);
  });
});
