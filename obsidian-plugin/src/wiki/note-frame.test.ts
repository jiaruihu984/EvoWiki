import { describe, expect, it } from "vitest";
import { sectionEnd } from "./note-frame";
import { WIKI_END } from "./note/format";

describe("sectionEnd (the lines a change card's jump frames)", () => {
  const lines = ["# 页", "", "## 一", "文字", "### 一.1", "更多", "", "## 二", "```", "## 不是标题", "```", "尾", "", WIKI_END, "我的笔记"];
  it("a heading's section runs to the next heading of the same or a higher level, trailing blank lines left out", () => {
    expect(sectionEnd(lines, 2)).toBe(5);
    expect(sectionEnd(lines, 4)).toBe(5);
  });
  it("a heading inside a code fence is not one; the generated block's end stops the last section", () => {
    expect(sectionEnd(lines, 7)).toBe(11);
  });
  it("a section that is not a heading (a check block) stops at the next heading of any level", () => {
    expect(sectionEnd(["```evowiki-check", "section: s1", "```", "", "#### x"], 0)).toBe(2);
  });
  it("a four-backtick fence wrapping ``` lines stays code until its own closer (CommonMark): a # line inside ends nothing", () => {
    const l = ["## 一", "````md", "```", "# 代码里的标题", "```", "````", "尾", "## 二"];
    expect(sectionEnd(l, 0)).toBe(6);
    expect(sectionEnd(["## 一", "~~~", "```", "## x", "~~~", "y", "## 二"], 0)).toBe(5);
  });
});
