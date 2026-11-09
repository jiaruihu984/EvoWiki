import { describe, expect, it } from "vitest";
import { WIKI_COPY } from "../copy";
import { MAX_NOTE_SUMMARY } from "../schema";
import { editSummary, hunks, MAX_DIFF_LINES, MAX_LINE, sectionLabel, writtenFallback, writtenFrom } from "./edit-diff";
import { normalizeBody, renderWikiBody, wikiBodyLayout } from "./format";
import type { WikiNoteInput, WikiNoteSection } from "./types";

const sec = (id: string, role: WikiNoteSection["role"], heading: string, body: string, o: Partial<WikiNoteSection> = {}): WikiNoteSection =>
  ({ id, role, heading, cue: "", body, folded: false, ...o });

const INPUT: WikiNoteInput = {
  pageId: "p1", title: "z-score", goal: "学会 z-score", diffText: null, collapsed: false,
  sections: [
    sec("s0", "organizer", "", "先说为什么要标准化。\n第二行开篇。"),
    sec("s1", "explain", "公式", "z = (x - μ) / σ\n\n减去均值，再除以标准差。"),
    sec("s2", "explain", "例子", "身高 180，均值 170，标准差 5，z = 2。"),
    sec("s3", "recap", "要点", "z 表示离均值几个标准差。"),
  ],
};

describe("editSummary", () => {
  const layout = writtenFrom(INPUT).layout;
  const lines = (body: string): string[] => normalizeBody(body).split("\n");
  const before = lines(renderWikiBody(INPUT));
  const summary = (edit: (b: string) => string): string => editSummary(before, lines(edit(renderWikiBody(INPUT))), layout);

  it("each changed run under its section's name, with 删 / 加 lines", () => {
    expect(summary((b) => b.replace("身高 180", "身高 190"))).toBe("【例子】\n删：身高 180，均值 170，标准差 5，z = 2。\n加：身高 190，均值 170，标准差 5，z = 2。");
    expect(summary((b) => b.replace("第二行开篇。", "第二行。"))).toBe("【开篇】\n删：第二行开篇。\n加：第二行。");
    expect(summary((b) => b.replace("z 表示离均值", "它表示离均值"))).toContain("【小结：要点】");
  });

  it("each listed line is cut, and the summary is cut and says how many changes were left out", () => {
    const long = "长".repeat(MAX_LINE * 2);
    expect(summary((b) => b.replace("身高 180", long))).toContain("…");
    const many = Array.from({ length: 400 }, (_, i) => `第 ${i} 行`);
    const after = many.map((l, i) => (i % 2 === 0 ? `${l}，改过了，这一句写得长一点让摘要超过上限` : l));
    const got = editSummary(many, after, { sections: [] });
    expect(got.length).toBeLessThanOrEqual(MAX_NOTE_SUMMARY);
    expect(got).toMatch(/还有 \d+ 处改动没列出/);
  });

  it("more than the line cap on one side → no line-by-line diff", () => {
    expect(editSummary(Array.from({ length: MAX_DIFF_LINES + 1 }, () => "x"), ["y"], layout)).toBe(WIKI_COPY.noteEditTooMany(MAX_DIFF_LINES + 1, 1));
  });

  it("writtenFallback (no copy): the current render only when it is exactly what was written (R4.1)", () => {
    const w = writtenFrom(INPUT);
    expect(writtenFallback(INPUT, w.hash)).toEqual(w);
    expect(writtenFallback({ ...INPUT, title: "x", sections: INPUT.sections.slice(1) }, w.hash)).toBeNull();
  });
});

describe("sectionLabel / wikiBodyLayout", () => {
  it("names like the feedback dialog (R3.2)", () => {
    expect(sectionLabel(sec("a", "organizer", "", "x"))).toBe("开篇");
    expect(sectionLabel(sec("a", "line", "均值", "x"))).toBe("一句话：均值");
    expect(sectionLabel(sec("a", "check", "", "x"))).toBe("确认题");
    expect(sectionLabel(sec("a", "explain", "公式", "x", { folded: true }))).toBe("公式（已折起）");
  });

  it("section ranges cover the rendered lines in order, after the page callouts", () => {
    const input = { ...INPUT, collapsed: true };
    const lines = renderWikiBody(input).replace(/\n$/, "").split("\n");
    const l = wikiBodyLayout(input);
    expect(l.sections.map((x) => x.id)).toEqual(["s0", "s1", "s2", "s3"]);
    expect(l.sections[0]!.from).toBe(2);
    expect(lines[l.sections[1]!.from]).toBe("## 公式");
    expect(lines[l.sections[2]!.from]).toBe("## 例子");
    for (let k = 1; k < l.sections.length; k++) expect(l.sections[k]!.from).toBe(l.sections[k - 1]!.to);
  });
});

describe("hunks (jsdiff)", () => {
  const L = (n: number, f: (i: number) => string): string[] => Array.from({ length: n }, (_, i) => f(i));
  it("groups runs of changes with the old line indexes; a pure insertion sits on the line before it", () => {
    expect(hunks(["a", "b", "c", "d"], ["a", "B", "c", "d", "e"])).toEqual([
      { at: [1], del: ["b"], add: ["B"] }, { at: [3], del: [], add: ["e"] },
    ]);
    expect(hunks(["a", "b"], ["a", "x", "b"])).toEqual([{ at: [0], del: [], add: ["x"] }]);
    expect(hunks(["a"], ["a"])).toEqual([]);
  });
  it("3000 lines with scattered edits finish quickly", () => {
    const a = L(MAX_DIFF_LINES, (i) => `line ${i}`), b = a.slice();
    b[100] = "x"; b[1500] = "y"; b.splice(2500, 1);
    const t0 = Date.now();
    const h = hunks(a, b);
    expect(Date.now() - t0).toBeLessThan(2500);
    expect(h.map((x) => x.at)).toEqual([[100], [1500], [2500]]);
  });
  it("past the edit cap the span between the common head and tail is one replaced run", () => {
    const a = L(MAX_DIFF_LINES, (i) => `a${i}`), b = L(MAX_DIFF_LINES, (i) => `b${i}`);
    const h = hunks(["same", ...a, "end"], ["same", ...b, "end"]);
    expect(h).toHaveLength(1);
    expect(h[0]!.del).toEqual(a);
    expect(h[0]!.add).toEqual(b);
  });
});
