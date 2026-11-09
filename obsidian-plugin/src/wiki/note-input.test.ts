import { describe, expect, it } from "vitest";
import { FakeWikiApi } from "./fake";
import { OUTLINE_P1, P1_TITLE } from "./fixtures";
import { diffNote } from "./format";
import { NameBook } from "./names";
import { parseWikiNote, renderWikiNote } from "./note";
import { toWikiNoteInput } from "./note-input";
import type { PageOut } from "./schema";

async function p1(): Promise<{ api: FakeWikiApi; page: PageOut }> {
  const api = new FakeWikiApi();
  const d = api.__goal("学会 z-score 标准化");
  const pid = (await api.answerDraft(d.draft_id ?? "", { answers: [-1, 0, 1], skip: false })).page_id ?? "";
  await api.organize(pid, () => undefined);
  return { api, page: await api.openPage(pid) };
}
function book(page: PageOut): NameBook {
  const n = new NameBook();
  n.addPage(page);
  return n;
}

describe("toWikiNoteInput", () => {
  it("removes explicitly mastered checks from the note without deleting their server questions", async () => {
    const { page } = await p1();
    const check = page.sections.find((s) => s.id === "s5")!;
    expect(check.cells.length).toBeGreaterThan(0);
    check.cells = check.cells.map((cell) => ({ ...cell, declared: "know" }));
    const input = toWikiNoteInput(page, book(page));
    expect(input.sections.find((s) => s.id === "s5")!.folded).toBe(true);
    expect(renderWikiNote(input).markdown).not.toContain("section: s5\n");
    expect(check.check).not.toBeNull();
    check.pinned = true;
    expect(renderWikiNote(toWikiNoteInput(page, book(page))).markdown).toContain("section: s5\n");
  });

  it("copies the P1 page field by field; no diff / conflicts → null", async () => {
    const { page: raw } = await p1();
    const page: PageOut = { ...raw, diff: null, unresolved_conflicts: [] };
    const input = toWikiNoteInput(page, book(page));
    expect(input.pageId).toBe(page.page_id);
    expect(input.title).toBe(P1_TITLE);
    expect(input.goal).toBe(page.goal);
    expect(input.collapsed).toBe(false);
    expect(input.diffText).toBeNull();
    expect(input.sections.map((s) => s.id)).toEqual(OUTLINE_P1.sections.map((s) => s.id));
    for (const [i, s] of page.sections.entries()) {
      const out = input.sections[i]!;
      expect(Object.keys(out).sort()).toEqual(["artifactId", "body", "cue", "fingerprint", "folded", "heading", "id", "role"]);
      expect([out.id, out.heading, out.role, out.cue, out.folded]).toEqual([s.id, s.heading, s.role, s.cue, s.folded]);
      if (s.role !== "check") expect(out.body).toBe(s.body);
    }
  });

  it("check section body = stem + lettered options, no answer", async () => {
    const { page } = await p1();
    const input = toWikiNoteInput(page, book(page));
    const check = input.sections.find((s) => s.id === "s5");
    expect(check?.role).toBe("check");
    expect(check?.body).toBe([
      "某次考试 z = −2 表示什么？",
      "- A. 高于均值 2 分",
      "- B. 低于均值 2 个标准差",
      "- C. 排名倒数第 2",
      "- D. 无法判断",
    ].join("\n"));
  });

  it("check section without a check keeps its body", async () => {
    const { page: raw } = await p1();
    const page: PageOut = { ...raw, sections: raw.sections.map((s) => (s.role === "check" ? { ...s, check: null, body: "原样" } : s)) };
    expect(toWikiNoteInput(page, book(page)).sections.find((s) => s.id === "s5")?.body).toBe("原样");
  });

  it("diffText; unresolved conflicts never reach the note", async () => {
    const { page: raw } = await p1();
    const page: PageOut = {
      ...raw,
      diff: { cell_changes: [{ cell: "c1.interpret", from: "gap", to: "known" }], folded: ["s4"], unfolded: [], patched: ["s9"] },
      unresolved_conflicts: ["「z 的公式」只一行带过"],
    };
    const names = book(page);
    const input = toWikiNoteInput(page, names);
    const heading = (sid: string) => page.sections.find((s) => s.id === sid)?.heading ?? null;
    expect(input.diffText).toBe(diffNote(page.diff, heading, names));
    expect(input.diffText).toContain("「z = ±1 是什么意思」");
    // A section not on the page is 另一页上的一段, never its id (controller ruling, web format.ts).
    expect(input.diffText).toContain("补写了另一页上的一段");
    expect(input.diffText).not.toContain("s9");
    expect(renderWikiNote(input).markdown).not.toContain("[!warning]");
    expect(() => renderWikiNote(input)).not.toThrow();
  });

  it("a collapsed page folds every section (A4)", async () => {
    const { api, page: first } = await p1();
    api.__collapse(first.page_id);
    const page = await api.openPage(first.page_id);
    expect(page.collapsed).toBe(true);
    const input = toWikiNoteInput(page, book(page));
    expect(input.collapsed).toBe(true);
    expect(input.sections.length).toBe(page.sections.length);
    expect(input.sections.every((s) => s.folded)).toBe(true);
    expect(() => renderWikiNote(input)).not.toThrow();
  });

  it("renders the P1 page without throwing", async () => {
    const { page } = await p1();
    const md = renderWikiNote(toWikiNoteInput(page, book(page))).markdown;
    expect(md).not.toContain(`# ${P1_TITLE}`); // v2: the file name is the title
    expect(parseWikiNote(md)).toMatchObject({ pageId: page.page_id, edited: false });
  });
});
