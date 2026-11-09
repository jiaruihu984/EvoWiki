import { expect, it } from "vitest";
import { isInternalPath, sanitizeFileName, validateNotesFolder } from "./note-format";

it("sanitizes file names deterministically, with a concept-id fallback (E1)", () => {
  expect(sanitizeFileName('A/B:C*D?"E<F>G|H#I^J[K]', "x")).toBe("A-B-C-D--E-F-G-H-I-J-K-");
  expect(sanitizeFileName("主成分分析", "pca")).toBe("主成分分析");
  expect(sanitizeFileName("  ..PCA.  ", "pca")).toBe("PCA");
  expect(sanitizeFileName("CON", "c")).toBe("CON-");
  expect(sanitizeFileName("aux.txt", "c")).toBe("aux.txt-");
  expect(sanitizeFileName("...", "pca")).toBe("pca");
  expect(sanitizeFileName("x".repeat(300), "c")).toHaveLength(120);
});

it("caps at 120 code points first, then trims, so the name never ends in a space/dot or splits a surrogate pair (final review)", () => {
  const cps = (s: string) => Array.from(s).length;
  // A space (or dot) exactly at the cut is trimmed after capping.
  expect(sanitizeFileName("x".repeat(119) + " yz", "c")).toBe("x".repeat(119));
  expect(sanitizeFileName("x".repeat(118) + ". yz", "c")).toBe("x".repeat(118));
  // A surrogate pair straddling UTF-16 index 120 is kept whole (120 code points, no lone surrogate).
  const emoji = sanitizeFileName("x".repeat(119) + "😀z", "c");
  expect(emoji).toBe("x".repeat(119) + "😀");
  expect(cps(emoji)).toBe(120);
  expect(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(emoji)).toBe(false);
  // A long name of CJK characters is capped by code points too.
  expect(cps(sanitizeFileName("分".repeat(300), "c"))).toBe(120);
  // A reserved name that hits the cap stays ≤ 120 code points with its "-" suffix.
  const reserved = sanitizeFileName("aux." + "a".repeat(200), "c");
  expect(cps(reserved)).toBe(120);
  expect(reserved.endsWith("-")).toBe(true);
  expect(reserved.startsWith("aux.")).toBe(true);
});

it("validates the notes folder as a safe vault-relative POSIX path (A1)", () => {
  expect(validateNotesFolder("")).toEqual({ ok: true, folder: "EvoWiki" });
  expect(validateNotesFolder(" Study/EvoWiki/ ")).toEqual({ ok: true, folder: "Study/EvoWiki" });
  for (const bad of ["/abs", "\\abs", "C:/x", "a\\b", "../x", "a/../b", "./a", ".obsidian", ".Obsidian/plugins", ".evowiki", ".EvoWiki/backups/x"]) {
    expect(validateNotesFolder(bad).ok, bad).toBe(false);
  }
});

it("recognises EvoWiki's internal area case-insensitively (A3)", () => {
  expect(isInternalPath(".evowiki/backups/x/PCA.md.bak")).toBe(true);
  expect(isInternalPath(".EvoWiki/a.md")).toBe(true);
  expect(isInternalPath("EvoWiki/PCA.md")).toBe(false);
  expect(isInternalPath(".evowikiX/a.md")).toBe(false);
});
