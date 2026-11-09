import { expect, it } from "vitest";
import { renderWikiNote } from "../wiki/note";
import { quoteFor, resolveActiveNote } from "./target";

const f = (path: string) => ({ path, basename: path.replace(/^.*\//, "").replace(/\.md$/, ""), extension: path.endsWith(".md") ? "md" : "png" });
const wiki = renderWikiNote({ pageId: "page_pca", title: "主成分分析", goal: "学 PCA", diffText: null, collapsed: false,
  sections: [{ id: "s1", heading: "直观理解", role: "explain", cue: "方差", body: "正文", folded: false }] }).markdown;

it("resolves page, note, unsupported and none", () => {
  expect(resolveActiveNote(null, null, null)).toEqual({ kind: "none" });
  expect(resolveActiveNote(f("a.png"), null, null)).toEqual({ kind: "none" });
  expect(resolveActiveNote(f(".evowiki/backups/x.md"), "pca", "")).toEqual({ kind: "none" });
  expect(resolveActiveNote(f("EvoWiki/主成分分析.md"), null, wiki)).toEqual({ kind: "page", pageId: "page_pca", title: "主成分分析" });
  expect(resolveActiveNote(f("EvoWiki/主成分分析.md"), "pca", wiki)).toMatchObject({ kind: "page" }); // wiki wins
  expect(resolveActiveNote(f("ML/PCA.md"), "pca", "# PCA")).toEqual({ kind: "note", path: "ML/PCA.md", conceptId: "pca", title: "PCA" });
  expect(resolveActiveNote(f("ML/PCA.md"), "pca", null)).toEqual({ kind: "note", path: "ML/PCA.md", conceptId: "pca", title: "PCA" });
  expect(resolveActiveNote(f("Diary.md"), null, "hello")).toEqual({ kind: "unsupported", title: "Diary" });
  expect(resolveActiveNote(f("Two.md"), null, wiki + "\n" + wiki)).toEqual({ kind: "unsupported", title: "Two" }); // ambiguous block
});

it("quotes: never truncated, refused over 4000 code points; heading capped at 300", () => {
  expect(quoteFor("  ", null)).toEqual({ ok: false, reason: "blank" });
  expect(quoteFor("字".repeat(4001), null)).toEqual({ ok: false, reason: "too-long" });
  expect(quoteFor("😀".repeat(4000), null)).toMatchObject({ ok: true }); // 4000 code points = 8000 UTF-16 units, still allowed
  const r = quoteFor("一段", "标".repeat(400));
  expect(r.ok && [...r.quote.heading!].length).toBe(300);
  expect(quoteFor("一段", null)).toEqual({ ok: true, quote: { text: "一段", heading: null, kind: "text" } });
  expect(quoteFor("一段", "推导")).toEqual({ ok: true, quote: { text: "一段", heading: "推导", kind: "text" } });
  expect(quoteFor("题目：x", null, true)).toEqual({ ok: true, quote: { text: "题目：x", heading: null, kind: "wrong_answer" } });
});
