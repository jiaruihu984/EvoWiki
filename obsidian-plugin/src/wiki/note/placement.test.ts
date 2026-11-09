import { describe, expect, it } from "vitest";
import { renderWikiNote } from "./format";
import { legacyPageIds, writingPageIds } from "./migrate";
import { candidateNames, folderSegment, isLegacyPositionName, placeNote } from "./placement";

const input = (pageId: string, title: string) =>
  ({ pageId, title, goal: "", diffText: null, collapsed: false, sections: [] });

describe("placement", () => {
  it("names: title, title（parent）, then （2）… after the last; no duplicates", () => {
    expect(candidateNames("定义", "p1", "食品添加剂").slice(0, 3)).toEqual(["定义", "定义（食品添加剂）", "定义（食品添加剂）（2）"]);
    expect(candidateNames("定义", "p1", null).slice(0, 2)).toEqual(["定义", "定义（2）"]);
  });
  it("a title with / or : is made safe; an empty one falls back to the page id", () => {
    expect(candidateNames("a/b", "p1", null)[0]).toBe("a-b");
    expect(candidateNames("", "p1", null)[0]).toBe("p1");
  });
  it.each(["题", "🧠"])("reserves distinct parent and counter suffixes for a 120-code-point %s title", async (char) => {
    const title = char.repeat(120);
    const names = candidateNames(title, "p1", "parent");
    expect(names).toHaveLength(100);
    expect(new Set(names).size).toBe(100);
    expect(names[1]).toBe(`${char.repeat(112)}（parent）`);
    expect(names[2]).toBe(`${char.repeat(109)}（parent）（2）`);
    expect(names.every((name) => Array.from(name).length <= 120)).toBe(true);
    expect(candidateNames(title, "p1", null)[1]).toBe(`${char.repeat(117)}（2）`);
    const taken = new Set(names.slice(0, -1).map((name) => `w/${name}.md`));
    expect(await placeNote((path) => Promise.resolve(taken.has(path)), "w", { pageId: "p1", title, parentName: "parent" }, null))
      .toBe(`w/${names[names.length - 1]}.md`);
  });
  it("keeps brackets balanced for a parent longer than the filename budget", () => {
    const names = candidateNames("title", "p1", "🧠".repeat(200));
    expect(names).toHaveLength(100);
    expect(names[2]).toBe(`t（${"🧠".repeat(114)}）（2）`);
    expect(names.every((name) => Array.from(name).length <= 120)).toBe(true);
  });
  it("folder: one sanitized level; empty -> the root", () => {
    expect(folderSegment("统计")).toBe("统计");
    expect(folderSegment("")).toBe("");
    expect(folderSegment(undefined)).toBe("");
    expect(folderSegment("a/b")).toBe("a-b");
  });
  it("the page's own file counts as free (case-insensitive); deterministic", async () => {
    const taken = new Set(["w/统计/定义.md", "w/统计/定义（食品）.md"]);
    const occ = (p: string) => Promise.resolve(taken.has(p));
    const p = { pageId: "p1", title: "定义", folder: "统计", parentName: "食品" };
    expect(await placeNote(occ, "w", p, null)).toBe("w/统计/定义（食品）（2）.md");
    expect(await placeNote(occ, "w", p, "w/统计/定义（食品）.md")).toBe("w/统计/定义（食品）.md");
    // the page's file at any candidate stays even though an earlier candidate is free
    expect(await placeNote(occ, "w", { ...p, title: "定义" }, "w/统计/定义（食品）（2）.md")).toBe("w/统计/定义（食品）（2）.md");
    expect(await placeNote(occ, "w", p, null)).toBe(await placeNote(occ, "w", p, null));
  });
  it("legacy names", () => {
    expect(isLegacyPositionName("pca（1-4）")).toBe(true);
    expect(isLegacyPositionName("pca")).toBe(false);
  });
  it("legacyPageIds: the pages of notes named （i-n）, read only", async () => {
    const files = new Map([
      ["EvoWiki/Wiki/pca（1-4）.md", renderWikiNote(input("p1", "pca")).markdown],
      ["EvoWiki/Wiki/均值.md", renderWikiNote(input("p2", "均值")).markdown],
      ["EvoWiki/Wiki/我的（1-2）.md", "我自己的\n"],
    ]);
    const store = { listMarkdown: () => [...files.keys()].sort(), read: (p: string) => Promise.resolve(files.get(p)!) };
    expect(await legacyPageIds(store, "EvoWiki/Wiki")).toEqual(["p1"]);
  });
  it("writingPageIds: the notes whose status is writing", async () => {
    const files = new Map([
      ["EvoWiki/Wiki/a.md", renderWikiNote({ ...input("p1", "a"), writing: true }).markdown],
      ["EvoWiki/Wiki/b.md", renderWikiNote(input("p2", "b")).markdown],
    ]);
    const store = { listMarkdown: () => [...files.keys()].sort(), read: (p: string) => Promise.resolve(files.get(p)!) };
    expect(await writingPageIds(store, "EvoWiki/Wiki")).toEqual(["p1"]);
  });
});
