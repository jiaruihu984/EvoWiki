import { describe, expect, it } from "vitest";
import { renderWikiNote } from "./note/format";
import type { WikiNoteInput, WikiNoteSection } from "./note-port";
import { findSectionLine } from "./page-toc";

const sec = (o: Partial<WikiNoteSection> & { id: string }): WikiNoteSection =>
  ({ heading: o.id, role: "explain", cue: "", body: `${o.id} 的正文`, folded: false, ...o });
const INPUT: WikiNoteInput = {
  pageId: "p1", title: "T", goal: "g", diffText: "这次改了一段",
  collapsed: false,
  sections: [
    sec({ id: "s1", role: "organizer", body: "\n开篇第一行\n第二行" }),
    sec({ id: "s2", heading: "同名" }),
    sec({ id: "s3", heading: "折起的", cue: "一句提示", folded: true }),
    sec({ id: "s4", role: "check", body: "" }),
    sec({ id: "s5", heading: "同名", body: "## 正文里的小标题\n同名\n内容" }),
    sec({ id: "s6", role: "line", heading: "一行", body: "就一句" }),
    sec({ id: "s7", role: "recap", heading: "回顾", body: "回顾内容" }),
  ],
};
const lineOf = (text: string, sid: string, input = INPUT): string | null => {
  const n = findSectionLine(text, input, sid);
  return n === null ? null : text.split("\n")[n]!;
};

describe("findSectionLine", () => {
  const note = renderWikiNote(INPUT).markdown;

  it("finds each section's first line in a freshly written note", () => {
    expect(lineOf(note, "s1")).toBe("开篇第一行");
    expect(lineOf(note, "s2")).toBe("## 同名");
    expect(lineOf(note, "s3")).toBe("> [!evowiki-known]- 一句提示");
    expect(lineOf(note, "s4")).toBe("```evowiki-check");
    expect(lineOf(note, "s6")).toBe("- **一行**：就一句");
    expect(lineOf(note, "s7")).toBe("## 回顾");
    // the second 「## 同名」, not the first, and not the body line 「同名」
    const lines = note.split("\n");
    const s5 = findSectionLine(note, INPUT, "s5")!;
    expect(lines[s5]).toBe("## 同名");
    expect(s5).toBeGreaterThan(findSectionLine(note, INPUT, "s2")!);
    expect(lines[s5 + 2]).toBe("## 正文里的小标题");
  });

  it("follows the text when the note is shifted (lines added above, CRLF, a fold sign flipped)", () => {
    const shifted = note.replace("%% evowiki:start %%\n", "%% evowiki:start %%\n多出来的一行\n\n");
    expect(lineOf(shifted, "s2")).toBe("## 同名");
    expect(lineOf(shifted, "s5")).toBe("## 同名");
    expect(findSectionLine(shifted, INPUT, "s5")).toBe(findSectionLine(note, INPUT, "s5")! + 2);
    const flipped = note.replace("[!evowiki-known]-", "[!evowiki-known]+");
    expect(lineOf(flipped, "s3")).toBe("> [!evowiki-known]+ 一句提示");
    const crlf = note.replace(/\n/g, "\r\n");
    expect(findSectionLine(crlf, INPUT, "s3")).toBe(findSectionLine(note, INPUT, "s3"));
  });

  it("a note written from an older page (section now folded in the input) still finds the current line, or null", () => {
    const later: WikiNoteInput = { ...INPUT, sections: INPUT.sections.map((x) => (x.id === "s2" ? { ...x, folded: true, cue: "新提示" } : x)) };
    expect(findSectionLine(note, later, "s2")).toBeNull();          // the callout is not in the old note
    expect(lineOf(note, "s3", later)).toBe("> [!evowiki-known]- 一句提示");
  });

  it("null: no block, an unknown section, nothing to match, a heading only after END", () => {
    expect(findSectionLine("# 普通笔记\n## 同名\n", INPUT, "s2")).toBeNull();
    expect(findSectionLine(note, INPUT, "nope")).toBeNull();
    const gone = note.replace("## 同名\n", "## 改过的标题\n").replace("## 同名\n", "## 也改了\n");
    expect(findSectionLine(gone, INPUT, "s5")).toBeNull();
    const noCheck = note.replace("section: s4", "section: other");
    expect(findSectionLine(noCheck, INPUT, "s4")).toBeNull();
    const afterEnd = "%% evowiki:start %%\n%% evowiki:end %%\n## 同名\n";
    expect(findSectionLine(afterEnd, INPUT, "s2")).toBeNull();
  });
});
