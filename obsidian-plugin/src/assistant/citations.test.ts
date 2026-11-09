import { describe, expect, it } from "vitest";
import { citationsToMarkdown, plainCitations } from "./citations";

const OK = "要除。〔attention p.4｜4｜src_a：「divide each by $\\sqrt{d_k}$ *now*」〕";
const none = () => false;

describe("an answer's citations", () => {
  it("a checked one links to its source and PDF page, shows the printed page, and hovers its quote with nothing Markdown can act on", () => {
    const md = citationsToMarkdown("〔讲义 p.2｜12｜src_b：「a*b*$c$」〕", none);
    expect(md).toContain('data-source="src&#95;b"');
    expect(md).toContain('data-page="12"');
    expect(md).toContain(">讲义 p.2</a>");
    expect(md).not.toMatch(/[*$]/);
    expect(new DOMParser().parseFromString(md, "text/html").querySelector("a")?.title).toBe("a*b*$c$");
  });

  it("one this turn did not read says so and still opens the page", () => {
    const text = "〔讲义 p.2｜12｜此前读取｜src_b：「片段」〕";
    const md = citationsToMarkdown(text, none);
    expect(md).toContain('data-page="12"');
    expect(md).toContain(">讲义 p.2（之前读的）</a>");
    expect(plainCitations(text)).toBe("（讲义 p.2（之前读的））");
  });

  it("a failed one says so and is no link, with or without a quote", () => {
    const md = citationsToMarkdown("〔attention p.9｜未核对：「编的」〕和〔attention p.9｜未核对〕", none);
    expect(md).not.toContain("<a");
    expect(md.match(/attention p\.9：这一处没有在原文核对到/g)).toHaveLength(2);
  });

  it("a material without pages is text with no page", () => {
    const md = citationsToMarkdown("〔笔记｜src_m：「一句话」〕", none);
    expect(md).not.toContain("<a");
    expect(md).toContain(">笔记</span>");
  });

  it("a deleted material says so and is no link", () => {
    const md = citationsToMarkdown(OK, (id) => id === "src_a");
    expect(md).not.toContain("<a");
    expect(md).toContain("attention p.4（已删除）");
  });

  it("other brackets stay", () => {
    expect(citationsToMarkdown("〔注〕和〔2〕", none)).toBe("〔注〕和〔2〕");
  });

  it("as words: no raw mark", () => {
    expect(plainCitations(`${OK}又〔attention p.9｜未核对：「编的」〕`)).toBe("要除。（attention p.4）又（attention p.9：这一处没有在原文核对到）");
  });
});
