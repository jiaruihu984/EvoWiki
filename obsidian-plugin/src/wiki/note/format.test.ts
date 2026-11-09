import { createHash } from "node:crypto";
import { describe, expect, it } from "vitest";
import {
  claimsPage, MINE_HINT, normalizeBody, pageLinksInBody, parseWikiFile, parseWikiNote, renderWikiBody, renderWikiNote, sectionLines, unverifiedSpan,
  updatedAtText, updateWikiNote, visibleBlanks, WIKI_END, WIKI_START, wikiHash,
} from "./format";
import type { PageSourceOut } from "../schema";
import type { WikiNoteInput, WikiNoteSection } from "./types";

const sec = (id: string, role: WikiNoteSection["role"], over: Partial<WikiNoteSection> = {}): WikiNoteSection =>
  ({ id, heading: `标题${id}`, role, cue: `提示${id}`, body: `正文${id}第一行\n\n正文${id}第二行`, folded: false, ...over });
const page = (over: Partial<WikiNoteInput> = {}): WikiNoteInput => ({
  pageId: "p_zscore", title: "z-score 标准化", goal: "看懂论文里的 z-score",
  diffText: null, collapsed: false,
  sections: [sec("o", "organizer"), sec("l", "line"), sec("r", "recap"), sec("e", "explain"), sec("c", "compare"), sec("k", "check")],
  ...over,
});
const NOW = new Date(2026, 9, 9, 10, 42, 30);
/** The text between the markers. */
const inner = (md: string) => md.slice(md.indexOf(`${WIKI_START}\n`) + WIKI_START.length + 1, md.indexOf(WIKI_END));

/** A v1 note exactly as the E3a writer produced it (HTML-comment header, H1, goal line, disclaimer). */
const V1_BODY = "# z-score 标准化\n\n*目标：看懂 z-score*\n\n正文o\n\n## 标题e\n\n正文e\n\n*EvoWiki 生成的内容。改了这一块，EvoWiki 就不会再更新它；你自己的笔记写在下面。*\n";
const v1Note = (pageId = "p_zscore", body = V1_BODY): string => {
  const h = createHash("sha256").update(JSON.stringify([pageId, "evowiki-wiki-note/1", normalizeBody(body)]), "utf8").digest("hex");
  return `<!-- EVOWIKI:WIKI:START\npage_id=${pageId}\nformat=evowiki-wiki-note/1\ngenerated_hash=sha256:${h}\n-->\n${body}<!-- EVOWIKI:WIKI:END -->\n\n## 我的笔记\n\n`;
};

describe("renderWikiNote (v2)", () => {
  it("machine fields in frontmatter, the block between %% comments, 我的笔记 with the hint outside", () => {
    const { markdown, hash } = renderWikiNote(page());
    expect(hash).toMatch(/^sha256:[0-9a-f]{64}$/);
    expect(markdown.startsWith([
      "---", "目标: \"看懂论文里的 z-score\"", "evowiki_title: \"z-score 标准化\"", "evowiki_page_id: \"p_zscore\"", "evowiki_format: \"evowiki-wiki-note/2\"",
      `evowiki_hash: "${hash}"`, "cssclasses:", "  - evowiki-page", "---", WIKI_START, "正文o第一行", "",
    ].join("\n"))).toBe(true);
    expect(markdown.endsWith(`${WIKI_END}\n\n## 我的笔记\n\n${MINE_HINT}\n\n`)).toBe(true);
    expect(MINE_HINT).toBe("<span class=\"evowiki-mine-hint\">写在这里的内容 EvoWiki 不会改动。</span>");
  });

  it("no H1, no HTML comment, no goal line, no disclaimer, no bracket lookup mark", () => {
    const md = renderWikiNote(page({ sections: [sec("e", "explain", { body: "单位一样。⟦待查证：样本方差除以 n − 1⟧" })] })).markdown;
    expect(md).not.toMatch(/^# /m);
    // The title is only the evowiki_title property (the plugin draws it as the h1), never in the body.
    expect(inner(md)).not.toContain("z-score 标准化");
    expect(md).toContain("\nevowiki_title: \"z-score 标准化\"\n");
    expect(md).not.toContain("<!--");
    expect(md).not.toContain("*目标：");
    expect(md).not.toContain("EvoWiki 生成的内容");
    expect(md).not.toMatch(/[⟦[]待查证/);
  });

  it("properties: 折起 counts folded sections (absent at 0), 更新于 only with a time, 目标 absent when blank", () => {
    const folded = renderWikiNote(page({ sections: [sec("e", "explain", { folded: true }), sec("r", "recap", { folded: true }),
      sec("k", "check", { folded: true }), sec("c", "compare")] }), NOW).markdown;
    expect(folded).toContain("\n折起: \"2 段已掌握的\"\n");
    expect(folded).toContain("\n更新于: \"2026-10-09 10:42\"\n");
    expect(updatedAtText(NOW)).toBe("2026-10-09 10:42");
    const plain = renderWikiNote(page({ goal: "  " })).markdown;
    expect(plain).not.toContain("折起");
    expect(plain).not.toContain("更新于");
    expect(plain).not.toContain("目标");
    expect(renderWikiNote(page({ goal: "a\nb  c" })).markdown).toContain("\n目标: \"a b c\"\n");
  });

  it("styles each role differently; a check section is an evowiki-check block with ids only", () => {
    const md = renderWikiNote(page()).markdown;
    expect(md).toContain(`${WIKI_START}\n正文o第一行\n\n正文o第二行\n`); // organizer: lead paragraph, no heading
    expect(md).not.toContain("标题o");
    expect(md).toContain("\n## 标题l\n"); // line with two paragraphs: a section (one paragraph: one bullet, below)
    const block = "前文\n\n```evoui\nasset: ia_1\n```";
    expect(renderWikiNote(page({ sections: [sec("b", "line", { body: block })] })).markdown)
      .toContain(`\n## 标题b\n\n${block}\n`); // an interactive keeps its fence
    expect(md).toContain("\n## 标题r\n\n正文r第一行\n\n正文r第二行\n"); // recap: a normal section (no 提醒 card)
    expect(md).not.toContain("[!tip]");
    expect(md).toContain("\n## 标题e\n\n正文e第一行\n\n正文e第二行\n"); // explain
    expect(md).toContain("\n## 标题c\n\n正文c第一行\n");              // compare
    expect(md).not.toContain("标题k");
    expect(md).not.toContain("正文k");
    expect(md).toContain("\n## 标题c\n\n正文c第一行\n\n正文c第二行\n\n```evowiki-check\npage: p_zscore\nsection: k\n```\n");
  });

  it("check block: folded checks leave no placeholder, invalid ids are skipped, visible checks are hashed", () => {
    const folded = renderWikiNote(page({ sections: [sec("k", "check", { folded: true })] }), NOW).markdown;
    expect(inner(folded)).toBe("");
    expect(folded).not.toContain("evowiki-check");
    expect(folded).not.toContain("折起");
    expect(folded).not.toContain("evowiki-known");
    for (const id of ["a b", "a`b", "a\nb", ""]) {
      expect(renderWikiBody(page({ sections: [sec(id, "check"), sec("e", "explain")] }))).not.toContain("evowiki-check");
    }
    for (const pageId of ["p`x", "p``"]) { // writable as a note, not inside the block → no block (fail closed)
      expect(renderWikiNote(page({ pageId, sections: [sec("k", "check"), sec("e", "explain")] })).markdown).not.toContain("evowiki-check");
    }
    const one = renderWikiNote(page({ sections: [sec("k", "check")] })).markdown;
    expect(parseWikiNote(one)?.edited).toBe(false);
    expect(parseWikiNote(one.replace("section: k", "section: k2"))?.edited).toBe(true);
    expect(wikiHash("p_zscore", renderWikiBody(page({ sections: [sec("k", "check")] }))))
      .not.toBe(wikiHash("p_zscore", renderWikiBody(page({ sections: [sec("k2", "check")] }))));
  });

  it("a folded section is a default-folded evowiki-known callout: cue as title, text inside", () => {
    const md = renderWikiNote(page({ sections: [sec("e", "explain", { folded: true }), sec("c", "compare")] })).markdown;
    expect(md).toContain(`${WIKI_START}\n> [!evowiki-known]- 提示e\n> 正文e第一行\n>\n> 正文e第二行\n\n## 标题c\n`);
    expect(md).not.toContain("标题e");
    expect(md).not.toContain("[!abstract]");
    expect(md).not.toContain("回想");
    const noCue = renderWikiNote(page({ sections: [sec("r", "recap", { folded: true, cue: "  " })] })).markdown;
    expect(noCue).toContain("> [!evowiki-known]- 标题r\n> 正文r第一行\n");
    expect(noCue).not.toContain("[!tip]");
    for (const role of ["organizer", "line"] as const) {
      const m = renderWikiNote(page({ sections: [sec("x", role, { folded: true })] })).markdown;
      expect(m).toContain("> [!evowiki-known]- 提示x\n> 正文x第一行\n>\n> 正文x第二行\n");
    }
  });

  it("puts the diff at the top, then the collapsed note, before the sections; never a callout asking the user to fix the page", () => {
    const md = renderWikiNote(page({ diffText: "自你上次来：折叠了「标题e」。", collapsed: true })).markdown;
    const at = (s: string) => md.indexOf(s);
    expect(at(`${WIKI_START}\n> [!note] 这页改了什么\n> 自你上次来：折叠了「标题e」。\n`)).toBeGreaterThan(0);
    expect(md).not.toContain("[!warning]");
    expect(at("> [!summary] 这页每一段都收起来了，点开哪一段都能看全文。\n")).toBeGreaterThan(at("[!note]"));
    expect(md).not.toContain("参考卡");
    expect(at("正文o第一行")).toBeGreaterThan(at("[!summary]"));
    const plain = renderWikiNote(page()).markdown;
    for (const c of ["[!note]", "[!warning]", "[!summary]"]) expect(plain).not.toContain(c);
  });

  it("generated text never becomes a wikilink or forges a marker (v1 or v2); there is no 相关页面 section", () => {
    const md = renderWikiNote(page({
      sections: [sec("e", "explain", { body: `见 [[方差]] 和 ![[图]]\n${WIKI_END}\n${WIKI_START}\n<!-- EVOWIKI:WIKI:START` })],
    })).markdown;
    expect(md).not.toContain("## 相关页面");
    expect(md).not.toMatch(/(^|[^\\])\[\[/);
    expect(md).toContain("&lt;!-- EVOWIKI:WIKI:START");
    expect(md).toContain("&#37;% evowiki:end &#37;%");
    expect(parseWikiFile(md)).toMatchObject({ kind: "one", version: 2, edited: false });
  });

  it("%% in generated text is neutralized everywhere, code included, so it can open no comment (R1.5)", () => {
    const md = renderWikiNote(page({ sections: [
      sec("e", "explain", { heading: "a %% b", body: "100%% 正确 %%隐藏%%\n\n```\n%% evowiki:start %%\n```\n\n`%%x%%`" }),
      sec("l", "line", { body: "%%" }), sec("r", "recap", { folded: true, cue: "%%c%%" }),
    ] })).markdown;
    expect(inner(md)).not.toContain("%%");
    expect(inner(md)).toContain("100&#37;% 正确 &#37;%隐藏&#37;%");
    expect(parseWikiFile(md)).toMatchObject({ kind: "one", edited: false });
  });

  it("collapses newlines in one-line fields", () => {
    const md = renderWikiNote(page({ sections: [sec("e", "explain", { heading: "x\r\ny", folded: true, cue: "c1\nc2" })] })).markdown;
    expect(md).toContain("> [!evowiki-known]- c1 c2\n");
    const noCue = renderWikiNote(page({ sections: [sec("e", "explain", { heading: "x\r\ny", folded: true, cue: "" })] })).markdown;
    expect(noCue).toContain("> [!evowiki-known]- x y\n");
  });

  it("rejects a page id that cannot sit on one line", () => {
    for (const id of ["", "a b", "a\nb", "a<b", "x-->"]) expect(() => renderWikiNote(page({ pageId: id }))).toThrow();
  });

  it("is deterministic; the hash follows the body and page id, not the properties (R1.2)", () => {
    expect(renderWikiNote(page())).toEqual(renderWikiNote(page()));
    expect(renderWikiNote(page({ goal: "别的" })).hash).toBe(renderWikiNote(page()).hash);
    expect(renderWikiNote(page(), NOW).hash).toBe(renderWikiNote(page()).hash);
    expect(renderWikiNote(page({ pageId: "p_other" })).hash).not.toBe(renderWikiNote(page()).hash);
    expect(renderWikiNote(page({ sections: [sec("e", "explain", { folded: true })] })).hash)
      .not.toBe(renderWikiNote(page({ sections: [sec("e", "explain")] })).hash);
    expect(renderWikiNote(page()).hash).toBe(wikiHash("p_zscore", renderWikiBody(page())));
  });
});

describe("待查证 pill", () => {
  it("the claim stays in the text and is followed by a pill whose tooltip says what to check", () => {
    const md = renderWikiNote(page({ sections: [sec("e", "explain", { body: "单位一样。⟦待查证：样本方差除以 n − 1⟧。" })] })).markdown;
    expect(md).toContain("单位一样。样本方差除以 n − 1<span class=\"evowiki-unverified\" title=\"样本方差除以 n − 1\">待查证</span>。");
  });

  it("bare and ASCII-bracket forms; one-line fields too", () => {
    const md = renderWikiNote(page({ sections: [
      sec("e", "explain", { body: "甲⟦待查证⟧ 乙[待查证：丙]" }), sec("l", "line", { body: "丁⟦待查证:戊⟧" }),
    ] })).markdown;
    expect(md).toContain("甲<span class=\"evowiki-unverified\">待查证</span> 乙丙<span class=\"evowiki-unverified\" title=\"丙\">待查证</span>");
    expect(md).toContain("：丁戊<span class=\"evowiki-unverified\" title=\"戊\">待查证</span>\n");
  });

  it("the title attribute is entity-encoded: no quote, tag, link, tag sign or comment can get out", () => {
    expect(unverifiedSpan("a\" onmouseover=\"x' <b> [[y]] #t %% `c` $m$ & \\")).toBe(
      "<span class=\"evowiki-unverified\" title=\"a&#34; onmouseover&#61;&#34;x&#39; &#60;b&#62; &#91;&#91;y&#93;&#93; &#35;t &#37;&#37; &#96;c&#96; &#36;m&#36; &#38; &#92;\">待查证</span>");
    const md = renderWikiNote(page({ sections: [sec("e", "explain", { body: "x⟦待查证：a\" b=\"c [[d]] %% <!--⟧ y" })] })).markdown;
    const span = /<span class="evowiki-unverified" title="([^"]*)">待查证<\/span>/.exec(md);
    expect(span?.[1]).toBe("a&#34; b&#61;&#34;c &#91;&#91;d&#93;&#93; &#37;&#37; &#60;&#33;&#45;&#45;");
    expect(inner(md)).not.toContain("%%");
    expect(md).not.toMatch(/(^|[^\\])\[\[/);
    expect(parseWikiNote(md)!.edited).toBe(false);
  });

  it("inside code the mark is left as written; private-use placeholders in the input cannot inject a pill", () => {
    const md = renderWikiNote(page({ sections: [sec("e", "explain", { body: "`⟦待查证：x⟧` 和\n```\n⟦待查证：y⟧\n```\n0" })] })).markdown;
    expect(md).toContain("`⟦待查证：x⟧` 和\n```\n⟦待查证：y⟧\n```\n0\n");
    expect(md).not.toContain("evowiki-unverified");
  });

  it("a backslash before the pill does not escape it", () => {
    const md = renderWikiNote(page({ sections: [sec("e", "explain", { body: "a⟦待查证：b\\⟧" })] })).markdown;
    expect(md).toContain("ab\\ <span class=\"evowiki-unverified\"");
  });
});

describe("parseWikiNote / parseWikiFile", () => {
  it("reads back what was rendered, also with CRLF and with the user's text before the block", () => {
    const { markdown, hash } = renderWikiNote(page(), NOW);
    expect(parseWikiNote(markdown)).toEqual({ pageId: "p_zscore", hash, edited: false });
    expect(parseWikiNote(markdown.replace(/\n/g, "\r\n"))).toEqual({ pageId: "p_zscore", hash, edited: false });
    expect(parseWikiNote(markdown.replace(`---\n${WIKI_START}`, `---\n我的前言\n\n${WIKI_START}`))).toEqual({ pageId: "p_zscore", hash, edited: false });
  });

  it("any change of text between the markers counts as edited; property changes do not", () => {
    const { markdown } = renderWikiNote(page(), NOW);
    expect(parseWikiNote(markdown.replace("正文e第一行", "正文e第1行"))!.edited).toBe(true);
    expect(parseWikiNote(markdown.replace("目标: \"看懂论文里的 z-score\"", "目标: 我改的\ntags: [x]"))!.edited).toBe(false);
    expect(parseWikiNote(markdown.replace("\n## 我的笔记\n", "\n## 我的笔记\n我写的\n"))!.edited).toBe(false);
  });

  it("Obsidian re-serializing the frontmatter (unquoted scalars, flow list) still reads back", () => {
    const { markdown, hash } = renderWikiNote(page());
    const resaved = markdown.replace("evowiki_page_id: \"p_zscore\"", "evowiki_page_id: p_zscore")
      .replace(`evowiki_hash: "${hash}"`, `evowiki_hash: ${hash}`).replace("cssclasses:\n  - evowiki-page", "cssclasses: [evowiki-page]");
    expect(parseWikiNote(resaved)).toEqual({ pageId: "p_zscore", hash, edited: false });
  });

  it("opening or folding a callout is not an edit", () => {
    const { markdown, hash } = renderWikiNote(page({ sections: [sec("e", "explain", { folded: true }), sec("r", "recap", { folded: true })] }));
    for (const v of [
      markdown.replace("[!evowiki-known]- 提示e", "[!evowiki-known]+ 提示e"),
      markdown.replace("[!evowiki-known]- 提示e", "[!evowiki-known] 提示e"),
      markdown.replace("[!evowiki-known]- 提示r", "[!evowiki-known]+ 提示r"),
    ]) expect(parseWikiNote(v)).toEqual({ pageId: "p_zscore", hash, edited: false });
  });

  it("ambiguous (R1.4): markers not paired, page id without markers, bad properties, broken frontmatter", () => {
    const { markdown, hash } = renderWikiNote(page());
    for (const bad of [
      markdown + `${WIKI_START}\n`,
      markdown.replace(`${WIKI_END}\n`, ""),
      markdown.replace(`${WIKI_START}\n`, ""),
      markdown.replace(`${WIKI_START}\n`, "").replace(WIKI_END, WIKI_START),
      markdown.replace(`${WIKI_START}\n`, "").replace(`${WIKI_END}\n`, ""),
      markdown.replace("evowiki-wiki-note/2", "evowiki-wiki-note/9"),
      markdown.replace(`evowiki_hash: "${hash}"`, "evowiki_hash: x"),
      markdown.replace(`evowiki_hash: "${hash}"\n`, ""),
      markdown.replace("evowiki_page_id: \"p_zscore\"", "evowiki_page_id: \"a b\""),
      markdown.replace("evowiki_page_id: \"p_zscore\"", "evowiki_page_id:\n  - p_zscore"),
      markdown.replace(/\n---\n/, "\n"), // unterminated frontmatter
      markdown.replace("cssclasses:", "? cssclasses"),
      markdown.replace("目标:", "evowiki_page_id:"), // duplicate key
    ]) expect(parseWikiFile(bad).kind, JSON.stringify(bad.slice(0, 400))).toBe("ambiguous");
    expect(parseWikiFile(`${WIKI_START}\nx\n${WIKI_END}\n`).kind).toBe("ambiguous"); // markers without a page id
    expect(parseWikiFile("# my note\n").kind).toBe("none");
    expect(parseWikiFile("---\ntags: [a]\n---\n# my note\n").kind).toBe("none");
    expect(parseWikiFile(`x ${WIKI_START}\n`).kind).toBe("none"); // a marker mid-line is the user's text
  });

  it("reads an unedited v1 note as version 1, and an edited one as edited", () => {
    expect(parseWikiFile(v1Note())).toMatchObject({ kind: "one", version: 1, pageId: "p_zscore", edited: false, pre: "", fm: null });
    expect(parseWikiFile(v1Note().replace("正文e", "我改的"))).toMatchObject({ kind: "one", version: 1, edited: true });
    expect(parseWikiNote(v1Note())).toMatchObject({ pageId: "p_zscore", edited: false });
    const withProps = `---\ntags: [mine]\n---\n${v1Note()}`;
    expect(parseWikiFile(withProps)).toMatchObject({ kind: "one", version: 1, edited: false, pre: "" });
    expect(parseWikiFile(v1Note() + v1Note()).kind).toBe("ambiguous");
  });
});

describe("updateWikiNote", () => {
  it("v1 unedited → v2: frontmatter added, H1 / goal line / disclaimer gone, pristine 我的笔记 gets the hint", () => {
    const u = updateWikiNote(v1Note(), page(), NOW);
    if (u.kind !== "write") throw new Error(u.kind);
    expect(u.text).toBe(renderWikiNote(page(), NOW).markdown);
  });

  it("a v2 note with the earlier abstract fold, unedited (fold sign toggled or not), is rewritten to evowiki-known; edited is left", () => {
    const input = page({ sections: [sec("e", "explain", { folded: true })] });
    const { markdown, hash } = renderWikiNote(input, NOW);
    const oldBody = "> [!abstract]- 回想：提示e\n> **标题e**\n>\n> 正文e第一行\n>\n> 正文e第二行\n";
    const old = markdown.replace(renderWikiBody(input), oldBody).replace(hash, wikiHash("p_zscore", oldBody));
    for (const v of [old, old.replace("[!abstract]-", "[!abstract]+")]) {
      expect(parseWikiFile(v)).toMatchObject({ kind: "one", version: 2, edited: false });
      const u = updateWikiNote(v, input, NOW);
      if (u.kind !== "write") throw new Error(u.kind);
      expect(u.text).toBe(markdown);
    }
    expect(updateWikiNote(old.replace("正文e第一行", "我改的"), input, NOW).kind).toBe("user_edited");
  });

  it("v1 with the user's notes and properties → v2; their keys and every byte of 我的笔记 are kept", () => {
    const mine = "\n## 我的笔记\n\n我的笔记内容 [[均值]]\r\n";
    const v1 = `---\ntags: [mine]\ncssclasses: wide\n---\n我的开头\n${v1Note().replace(/\n## 我的笔记\n\n$/, mine)}`;
    const u = updateWikiNote(v1, page(), NOW);
    if (u.kind !== "write") throw new Error(u.kind);
    expect(u.text.startsWith("---\ntags: [mine]\ncssclasses:\n  - wide\n  - evowiki-page\n目标: ")).toBe(true);
    expect(u.text).toContain(`\n---\n我的开头\n${WIKI_START}\n`);
    expect(u.text.endsWith(`${WIKI_END}\n${mine}`)).toBe(true);
    expect(parseWikiFile(u.text)).toMatchObject({ kind: "one", version: 2, edited: false });
  });

  it("v1 edited, v2 edited, ambiguous, another page → user_edited", () => {
    expect(updateWikiNote(v1Note().replace("正文e", "我改的"), page(), NOW).kind).toBe("user_edited");
    const v2 = renderWikiNote(page()).markdown;
    expect(updateWikiNote(v2.replace("正文e第一行", "我改的"), page({ goal: "新" }), NOW).kind).toBe("user_edited");
    expect(updateWikiNote(v2.replace(`${WIKI_END}\n`, ""), page(), NOW).kind).toBe("user_edited");
    expect(updateWikiNote(v2, page({ pageId: "p_other" }), NOW).kind).toBe("user_edited");
    expect(updateWikiNote("# nothing\n", page(), NOW).kind).toBe("user_edited");
  });

  it("same body and display values → unchanged despite the user's keys; changed body → rewrites only managed keys, refreshes 更新于", () => {
    const v2 = renderWikiNote(page(), new Date(2026, 0, 1, 8, 0)).markdown;
    const touched = v2.replace("---\n目标:", "---\ntags:\n  - 统计\naliases: [z]\n目标:").replace("目标: \"看懂论文里的 z-score\"", "目标: 看懂论文里的 z-score")
      .replace("  - evowiki-page", "  - evowiki-page\n  - wide") + "我写的一行\n";
    expect(updateWikiNote(touched, page(), NOW).kind).toBe("unchanged");
    const next = page({ goal: "新目标", sections: [sec("o", "organizer"), sec("e", "explain", { folded: true })] });
    const u = updateWikiNote(touched, next, NOW);
    if (u.kind !== "write") throw new Error(u.kind);
    const fm = u.text.slice(0, u.text.indexOf(WIKI_START));
    expect(fm).toBe([
      "---", "tags:", "  - 统计", "aliases: [z]", "目标: \"新目标\"", "折起: \"1 段已掌握的\"", "更新于: \"2026-10-09 10:42\"",
      "evowiki_title: \"z-score 标准化\"", "evowiki_page_id: \"p_zscore\"", "evowiki_format: \"evowiki-wiki-note/2\"", `evowiki_hash: "${u.hash}"`,
      "cssclasses:", "  - evowiki-page", "  - wide", "---", "",
    ].join("\n"));
    expect(u.text.endsWith(`${WIKI_END}\n\n## 我的笔记\n\n${MINE_HINT}\n\n我写的一行\n`)).toBe(true);
  });

  it("a goal-only change (same body) rewrites the properties; identical input stays unchanged", () => {
    const v2 = renderWikiNote(page(), new Date(2026, 0, 1, 8, 0)).markdown;
    expect(updateWikiNote(v2, page(), NOW).kind).toBe("unchanged");
    const u = updateWikiNote(v2, page({ goal: "新目标" }), NOW);
    if (u.kind !== "write") throw new Error(u.kind);
    expect(u.text).toBe(renderWikiNote(page({ goal: "新目标" }), NOW).markdown);
    expect(updateWikiNote(u.text, page({ goal: "新目标" }), NOW).kind).toBe("unchanged");
    // A 目标 the user retyped is a property edit, never user_edited: the generated value is written back.
    expect(updateWikiNote(v2.replace("目标: \"看懂论文里的 z-score\"", "目标: 我改的"), page(), NOW).kind).toBe("write");
    // A stale 折起 (with no folded section) is removed too.
    const stale = updateWikiNote(v2.replace("---\n目标:", "---\n折起: \"9 段已掌握的\"\n目标:"), page(), NOW);
    if (stale.kind !== "write") throw new Error(stale.kind);
    expect(stale.text).not.toContain("折起");
  });

  it("evowiki_title (T10, the note page's h1): not hashed; a title change rewrites it; an older note without it gets it", () => {
    const v2 = renderWikiNote(page(), NOW);
    expect(renderWikiNote(page({ title: "别的标题" }), NOW).hash).toBe(v2.hash);
    const u = updateWikiNote(v2.markdown, page({ title: "标准分数" }), NOW);
    if (u.kind !== "write") throw new Error(u.kind);
    expect(u.text).toContain("\nevowiki_title: \"标准分数\"\n");
    const old = v2.markdown.replace("evowiki_title: \"z-score 标准化\"\n", "");
    expect(parseWikiNote(old)).toEqual({ pageId: "p_zscore", hash: v2.hash, edited: false });
    const w = updateWikiNote(old, page(), NOW);
    if (w.kind !== "write") throw new Error(w.kind);
    expect(w.text).toContain("更新于: \"2026-10-09 10:42\"\nevowiki_title: \"z-score 标准化\"\nevowiki_page_id:");
    expect(renderWikiNote(page({ title: "  " })).markdown).not.toContain("evowiki_title");
  });

  it("missing display keys go next to the others in the order 目标, 折起, 更新于, before the machine keys", () => {
    const bare = renderWikiNote(page({ goal: "" })).markdown.replace("---\n", "---\ntags: [a]\n");
    const u = updateWikiNote(bare, page({ sections: [sec("e", "explain", { folded: true })] }), NOW);
    if (u.kind !== "write") throw new Error(u.kind);
    expect(u.text.startsWith("---\ntags: [a]\n目标: \"看懂论文里的 z-score\"\n折起: \"1 段已掌握的\"\n更新于: \"2026-10-09 10:42\"\nevowiki_title: \"z-score 标准化\"\nevowiki_page_id:")).toBe(true);
    const mid = renderWikiNote(page({ goal: "" }), NOW).markdown;
    const w = updateWikiNote(mid, page({ goal: "g", sections: [sec("e", "explain", { folded: true })] }), NOW);
    if (w.kind !== "write") throw new Error(w.kind);
    expect(w.text.startsWith("---\n目标: \"g\"\n折起: \"1 段已掌握的\"\n更新于: ")).toBe(true);
  });

  it("a cssclasses value it cannot read → user_edited, nothing written", () => {
    const v2 = renderWikiNote(page()).markdown.replace("cssclasses:\n  - evowiki-page", "cssclasses:\n  - [a, b]");
    expect(updateWikiNote(v2, page({ sections: [sec("o", "organizer")] }), NOW).kind).toBe("user_edited");
  });

  it("removes 折起 when nothing is folded any more, and keeps CRLF frontmatter line endings", () => {
    const folded = renderWikiNote(page({ sections: [sec("e", "explain", { folded: true })] })).markdown.replace(/\n/g, "\r\n");
    const u = updateWikiNote(folded, page({ sections: [sec("e", "explain")] }), NOW);
    if (u.kind !== "write") throw new Error(u.kind);
    expect(u.text).not.toContain("折起");
    expect(u.text.startsWith("---\r\n目标: ")).toBe(true);
    expect(parseWikiFile(u.text)).toMatchObject({ kind: "one", edited: false });
  });
});

describe("claimsPage (R1.3)", () => {
  it("v2: the frontmatter evowiki_page_id line, quoted or not", () => {
    const md = renderWikiNote(page()).markdown;
    expect(claimsPage(md, "p_zscore")).toBe(true);
    expect(claimsPage(md, "p_z")).toBe(false);
    expect(claimsPage(md.replace(/\n/g, "\r\n"), "p_zscore")).toBe(true);
    expect(claimsPage(md.replace("\"p_zscore\"", "p_zscore"), "p_zscore")).toBe(true);
    expect(claimsPage(md.replace("\"p_zscore\"", "'p_zscore'"), "p_zscore")).toBe(true);
    expect(claimsPage(md + md, "p_zscore")).toBe(true); // broken still claims → sync refuses it
    expect(claimsPage(md.replace(`${WIKI_END}\n`, ""), "p_zscore")).toBe(true);
    expect(claimsPage(md.replace(/\n---\n/, "\n"), "p_zscore")).toBe(true); // unterminated frontmatter still claims
  });

  it("v2: the same line outside the frontmatter is someone's text", () => {
    const other = renderWikiNote(page({ pageId: "p_other", sections: [sec("o", "organizer", { body: "evowiki_page_id: p_zscore" })] })).markdown;
    expect(claimsPage(other, "p_zscore")).toBe(false);
    expect(claimsPage(other + "evowiki_page_id: p_zscore\n", "p_zscore")).toBe(false);
    expect(claimsPage("evowiki_page_id: p_zscore\n", "p_zscore")).toBe(false);
    expect(claimsPage(other, "p_other")).toBe(true);
  });

  it("v1: the START marker immediately followed by an exact page_id line (for migration)", () => {
    expect(claimsPage(v1Note(), "p_zscore")).toBe(true);
    expect(claimsPage(v1Note(), "p_z")).toBe(false);
    expect(claimsPage("page_id=p_zscore\n", "p_zscore")).toBe(false);
    expect(claimsPage(`---\ntags: [a]\n---\n${v1Note()}`, "p_zscore")).toBe(true);
  });
});

describe("hash and line breaks", () => {
  it("normalizeBody drops only the fold sign on callout header lines, CRLF and trailing newlines", () => {
    expect(normalizeBody("> [!tip]- a\r\n> > [!note]+ b\n- [!x]- c\n\n")).toBe("> [!tip] a\n> > [!note] b\n- [!x]- c");
    expect(wikiHash("p", "> [!a]- x")).toBe(wikiHash("p", "> [!a]+ x\n"));
    expect(wikiHash("p", "x")).not.toBe(wikiHash("q", "x"));
    expect(normalizeBody("x\ry\r\n")).toBe("x\ny");
  });

  it("a lone CR is a line break: the heading collapses to one line and the note reads back unedited", () => {
    const md = renderWikiNote(page({ sections: [sec("e", "explain", { heading: "a\rb", body: "a\rb" })] })).markdown;
    expect(md).toContain("\n## a b\n\na\nb\n");
    expect(md).not.toContain("\r");
    expect(parseWikiNote(md)).toMatchObject({ pageId: "p_zscore", edited: false });
  });

  it("a blank diff text renders no callout; an empty page still parses", () => {
    const md = renderWikiNote(page({ diffText: "  " })).markdown;
    expect(md).not.toContain("[!note]");
    expect(md).not.toContain("[!warning]");
    expect(parseWikiFile(renderWikiNote(page({ sections: [] })).markdown)).toMatchObject({ kind: "one", edited: false, body: "" });
  });
});

/**
 * Test-side model of what Obsidian turns into vault links or tags, written independently of format.ts: strip blockquote/
 * callout prefixes, skip fenced code, group lines into paragraphs (link text may span a line break), blank out backslash
 * escapes, inline code and math, then look for wikilinks/embeds, inline and reference links, and tags (line start or after
 * whitespace; `#` + space or `#` is a heading, not a tag).
 */
function obsidianLinks(md: string): string[] {
  const found: string[] = [];
  const paragraphs: string[] = [];
  let para: string[] = [];
  let fence: string | null = null;
  const flush = () => { if (para.length) paragraphs.push(para.join("\n")); para = []; };
  // Frontmatter values are properties, not Markdown: only `[[` would become a link there (checked separately).
  for (const raw of md.replace(/\r\n?/g, "\n").replace(/^---\n[\s\S]*?\n---\n/, "").split("\n")) {
    const line = raw.replace(/^(?:> ?)+/, "");
    const f = /^ {0,3}(`{3,}|~{3,})/.exec(line);
    if (fence !== null) {
      if (f && f[1]![0] === fence[0] && f[1]!.length >= fence.length && /^ {0,3}[`~]+\s*$/.test(line)) fence = null;
      continue;
    }
    if (f) { flush(); fence = f[1]!; continue; }
    if (line.trim() === "") flush(); else para.push(line);
  }
  flush();
  const blank = (m: string) => m.replace(/[^\n]/g, " ");
  for (const p of paragraphs) {
    const text = p.replace(/\\./g, "  ").replace(/(`+)[\s\S]*?\1/g, blank)
      .replace(/\$\$[\s\S]*?\$\$/g, blank).replace(/\$[^\s$][^$]*?(?<=\S)\$(?!\d)/g, blank);
    for (const m of text.matchAll(/!?\[\[[^\]]+\]\]/g)) found.push(m[0]);
    for (const m of text.matchAll(/!?\[[^\]]*\]\([^)]*\)/g)) found.push(m[0]);
    for (const m of text.matchAll(/\[[^\]]+\]\[[^\]]*\]/g)) found.push(m[0]);
    for (const m of text.matchAll(/^\[[^\]]+\]:\s*\S+/gm)) found.push(m[0]);
    for (const m of text.matchAll(/(?:^|\s)(#[^\s#]+)/g)) found.push(m[1]!);
  }
  return found;
}

describe("generated text never becomes an Obsidian link or tag", () => {
  const NASTY = [
    "见 [文字](目标.md) 和 ![图](a.png)，还有 [[双链]]、![[嵌入]]。",
    "#标签 在行首，句中 #tag2 也是，C# 和 a#b 不是。",
    "嵌套 [a [b]](c.md) 和 [x](<带 空格.md>)。",
  ].join("\n");
  const CODE_INLINE = "`[[keep]] #keep [k](k.md)`";
  const CODE_FENCE = "```\n[[x]] #y [z](w.md)\n```";
  const body = `${NASTY}\n\n行内代码 ${CODE_INLINE} 原样。\n\n${CODE_FENCE}\n`;

  it("positive control: the scanner finds every form in the raw text, and none inside code", () => {
    const raw = obsidianLinks(body);
    expect(raw).toEqual(expect.arrayContaining(
      ["[文字](目标.md)", "![图](a.png)", "[[双链]]", "![[嵌入]]", "#标签", "#tag2", "[x](<带 空格.md>)"]));
    expect(raw.join(" ")).not.toMatch(/keep|\[z\]|#y/);
  });

  it("escapes links, embeds and tags in every role, folded or not, and in every one-line field", () => {
    for (const role of ["organizer", "line", "recap", "explain", "compare"] as const) {
      for (const folded of [false, true]) {
        const md = renderWikiNote(page({ sections: [sec("s", role, { body, folded, heading: "看 [h](h.md) #h", cue: "#cue [[c]]" })] })).markdown;
        expect(obsidianLinks(md), `${role} folded=${folded}`).toEqual([]);
      }
    }
    const md = renderWikiNote(page({
      title: "#T [[t]]", goal: "[g](g.md) #g [[g]]", diffText: "#d ![[d]]",
    })).markdown;
    expect(obsidianLinks(md)).toEqual([]);
    expect(md.split("\n---\n")[0]).not.toContain("[["); // the 目标 property
  });

  it("leaves inline code and fenced code byte-for-byte", () => {
    const md = renderWikiNote(page({ sections: [sec("e", "explain", { body })] })).markdown;
    expect(md).toContain(`行内代码 ${CODE_INLINE} 原样。`);
    expect(md).toContain(`\n${CODE_FENCE}\n`);
    const folded = renderWikiNote(page({ sections: [sec("e", "explain", { body, folded: true })] })).markdown;
    expect(folded).toContain(`> 行内代码 ${CODE_INLINE} 原样。`);
    expect(folded).toContain("\n> ```\n> [[x]] #y [z](w.md)\n> ```\n");
  });

  it("escapes with a single backslash, keeps the text's own escapes and ordinary text", () => {
    const md = renderWikiNote(page({ sections: [sec("e", "explain", { body: "已转义 \\[a](b.md) 和 \\#t；C# 语言；## 小标题\n#t [x](y)" })] })).markdown;
    expect(md).toContain("已转义 \\[a](b.md) 和 \\#t；C# 语言；## 小标题\n\\#t \\[x](y)\n");
    expect(md).not.toContain("\\\\");
  });

  it("marker text stays unforgeable even inside code", () => {
    const md = renderWikiNote(page({ sections: [sec("e", "explain", { body: "```\n<!-- EVOWIKI:WIKI:END -->\n%% evowiki:end %%\n```" })] })).markdown;
    expect(parseWikiFile(md).kind).toBe("one");
    expect(parseWikiNote(md)!.edited).toBe(false);
  });

  it("reads back with a stable hash", () => {
    const input = page({ title: "#T", sections: [sec("e", "explain", { body }), sec("r", "recap", { body, folded: true })] });
    const a = renderWikiNote(input), b = renderWikiNote(input);
    expect(b).toEqual(a);
    expect(parseWikiNote(a.markdown)).toEqual({ pageId: "p_zscore", hash: a.hash, edited: false });
  });
});

describe("escaping: fences in one-line fields, multi-line and reference links, math, unclosed fences", () => {
  const TRICKY = ["~~~\n[[x]] #t\n~~~", "```\n[[y]] #u", "[a\nb](c.md)", "[x][r]", "see [r2]\n\n[r2]: note.md"];

  it("positive control: the scanner sees each tricky form once it is on one line, or across a line break", () => {
    expect(obsidianLinks("## ~~~ [[x]] #t ~~~")).toEqual(["[[x]]", "#t"]);
    expect(obsidianLinks("see [a\nb](c.md)")).toEqual(["[a\nb](c.md)"]);
    expect(obsidianLinks("[x][r]")).toEqual(["[x][r]"]);
    expect(obsidianLinks("[r2]: note.md")).toEqual(["[r2]: note.md"]);
  });

  it("one-line fields are collapsed before escaping, so a fence cannot hide a link or tag", () => {
    for (const t of TRICKY) {
      const md = renderWikiNote(page({
        title: t, goal: t, diffText: t,
        sections: [sec("l", "line", { heading: t, body: t }), sec("e", "explain", { heading: t, cue: t, folded: true })],
      })).markdown;
      expect(obsidianLinks(md), JSON.stringify(t)).toEqual([]);
    }
  });

  it("block text: link text across a line break and reference links are escaped, folded or not", () => {
    for (const folded of [false, true]) {
      const md = renderWikiNote(page({ sections: [sec("e", "explain", { folded, body: "see [a\nb](c.md)\n\n[x][r] and [r]\n\n[r]: note.md" })] })).markdown;
      expect(obsidianLinks(md), `folded=${folded}`).toEqual([]);
    }
  });

  it("math is left verbatim; a lone $ is not math", () => {
    const body = "期望 $E[X](1-p)$，求和 $$\\sum_i [a](b)$$。\n价格 $5 和 [x](y) $6";
    const md = renderWikiNote(page({ sections: [sec("e", "explain", { body })] })).markdown;
    expect(md).toContain("期望 $E[X](1-p)$，求和 $$\\sum_i [a](b)$$。\n价格 $5 和 \\[x](y) $6\n");
    expect(obsidianLinks(md)).toEqual([]);
  });

  it("an unclosed fence is closed, so the rest of the note is not swallowed", () => {
    const md = renderWikiNote(page({ sections: [sec("e", "explain", { body: "```js\nlet a = [[1]];" }), sec("x", "explain", { body: "#after" })] })).markdown;
    expect(md).toContain("\n```js\nlet a = [[1]];\n```\n\n## 标题x\n\n\\#after\n");
    const fences = md.split("\n").filter((l) => /^(?:> ?)*```/.test(l));
    expect(fences.length % 2).toBe(0);
    expect(parseWikiNote(md)!.edited).toBe(false);
  });
});

describe("bold that would not close in Live Preview", () => {
  it("**…（…）**就是 becomes <strong>; other bold, and bold in code, stay as written", () => {
    const body = "**节点（node）**就是对象。**节点集合**记作 V，**边（edge）** 表示关系，“**度**”很常用。`**甲（乙）**丙`";
    const md = renderWikiNote(page({ sections: [sec("e", "explain", { body })] })).markdown;
    expect(md).toContain("<strong>节点（node）</strong>就是对象。**节点集合**记作 V，**边（edge）** 表示关系，“**度**”很常用。`**甲（乙）**丙`");
  });
});

describe("page links: `[text](#p:<id>)` in block text stays a link (the note's click handler opens the page)", () => {
  const LINK = "[基因配对](#p:pg_ca62d07257d34436832c)";

  it("is kept as written in section text, folded or not, and is the only link Obsidian sees", () => {
    for (const folded of [false, true]) {
      const md = renderWikiNote(page({ sections: [sec("e", "explain", { folded, body: `见 ${LINK}，还有 [x](y.md) 和 #t。` })] })).markdown;
      expect(md, `folded=${folded}`).toContain(`${folded ? "> " : ""}见 ${LINK}，还有 \\[x](y.md) 和 \\#t。`);
      expect(obsidianLinks(md)).toEqual([LINK]);
    }
  });

  it("a one-line section keeps it in its text, not in its heading", () => {
    const md = renderWikiNote(page({ sections: [sec("l", "line", { heading: `看 ${LINK}`, body: `回到${LINK}的\n例子，[x](y.md)。` })] })).markdown;
    expect(md).toContain(`- **看 \\${LINK}**：回到${LINK}的 例子，\\[x](y.md)。`);
    expect(obsidianLinks(md)).toEqual([LINK]);
  });

  it("the link text is still neutralised (a tag inside it), and the hash reads back stable", () => {
    const input = page({ sections: [sec("e", "explain", { body: "看 [#协方差 矩阵](#p:p3)。" })] });
    const a = renderWikiNote(input);
    expect(a.markdown).toContain("看 [\\#协方差 矩阵](#p:p3)。");
    expect(renderWikiNote(input)).toEqual(a);
    expect(parseWikiNote(a.markdown)).toEqual({ pageId: "p_zscore", hash: a.hash, edited: false });
  });

  it("any other shape stays escaped: embed, own escape, unsafe id, other targets, bracket or newline in the text", () => {
    const body = [
      "![图](#p:p3)", "\\[已转义](#p:p3)", "[a](#p:../x)", "[b](#p: p3)", "[c](#p:p3 \"t\")", "[d](#heading)", "[e](p:p3)",
      "[f [g]](#p:p3)", "[h\ni](#p:p3)", "[`code`](#p:p3)", "[$m$](#p:p3)",
    ].join(" ；\n\n");
    const md = renderWikiNote(page({ sections: [sec("e", "explain", { body })] })).markdown;
    expect(obsidianLinks(md)).toEqual([]);
    expect(md).toContain("!\\[图](#p:p3)");
  });

  it("one-line fields (heading, cue, goal) and code keep no page link", () => {
    const md = renderWikiNote(page({
      goal: LINK, diffText: LINK,
      sections: [sec("l", "line", { heading: LINK, body: "无" }), sec("e", "explain", { heading: LINK, cue: LINK, folded: true, body: `\`${LINK}\`` })],
    })).markdown;
    // diffText is block text (a quoted callout), so its page link stays; nothing else does.
    expect(obsidianLinks(md)).toEqual([LINK]);
    expect(md).toContain(`> \`${LINK}\``);
  });

  it("pageLinksInBody reads back exactly the links the writer kept (not in code, also in a folded section)", () => {
    for (const folded of [false, true]) {
      const body = inner(renderWikiNote(page({ sections: [sec("e", "explain", { folded, body: "看 [甲](#p:a) 和 `[乙](#p:b)`\n\n```\n[丙](#p:c)\n```" })] })).markdown);
      const found = pageLinksInBody(body);
      expect(found.map((f) => f.id), `folded=${folded}`).toEqual(["a"]);
      expect(body.slice(found[0]!.from, found[0]!.to)).toBe("[甲](#p:a)");
      expect(body.slice(found[0]!.textFrom, found[0]!.textTo)).toBe("甲");
    }
  });
});

describe("citations and the 和资料不同 strip", () => {
  const src = (over: Partial<PageSourceOut> = {}): PageSourceOut =>
    ({ id: "s1", title: "线性代数讲义", current_title: "线性代数讲义", kind: "pdf", origin: "EvoWiki/资料/线性代数讲义.pdf", deleted: false, ...over });
  const body = (text: string, sources: PageSourceOut[]) =>
    renderWikiBody(page({ sources, sections: [sec("e", "explain", { body: text })] }));
  it("links the original at the PDF page, shown with the printed page", () => {
    expect(body("向量记作 \\mathbf{v}〔线性代数讲义 p.12〕。", [src()]))
      .toContain("向量记作 \\mathbf{v}[[EvoWiki/资料/线性代数讲义.pdf#page=12|线性代数讲义 p.12]]。");
  });
  it("stored form: printed page and PDF page differ, ranges keep the first PDF page", () => {
    expect(body("〔线性代数讲义 p.12｜17〕", [src()])).toContain("[[EvoWiki/资料/线性代数讲义.pdf#page=17|线性代数讲义 p.12]]");
    expect(body("〔线性代数讲义 p.12–13｜17〕", [src()])).toContain("#page=17|线性代数讲义 p.12–13]]");
    expect(body("〔线性代数讲义 p.12–13〕", [src()])).toContain("#page=12|线性代数讲义 p.12–13]]");
  });
  it("a deleted material is text, not a link", () => {
    const b = body("〔线性代数讲义 p.12〕", [src({ deleted: true })]);
    expect(b).toContain("线性代数讲义 p.12（已删除）");
    expect(b).not.toContain("[[");
  });
  it("no origin (a url, pasted text): plain text", () => {
    const b = body("〔线性代数讲义 p.12〕", [src({ origin: null })]);
    expect(b).toContain("线性代数讲义 p.12");
    expect(b).not.toMatch(/\[\[|〔/);
  });
  it("a url or pasted-text material is plain 「名 p.N」, never a wikilink (final review #9)", () => {
    for (const s of [src({ kind: "url", origin: "https://example.com/paper" }), src({ kind: "text", origin: "EvoWiki/资料/x.md" }),
      src({ origin: "https://example.com/a.pdf" })]) {
      const b = body("〔线性代数讲义 p.12｜14〕", [s]);
      expect(b).toContain("线性代数讲义 p.12");
      expect(b).not.toMatch(/\[\[|〔|https|#page/);
    }
  });
  it("a name that is not one of the page's materials stays as written, never a link", () => {
    const b = body("〔别的书 p.3〕", [src()]);
    expect(b).toContain("〔别的书 p.3〕");
    expect(b).not.toContain("[[");
  });
  it("] and | in a name cannot end the link or forge another one", () => {
    const b = body("〔讲义]]x|[[y p.3〕", [src({ title: "讲义]]x|[[y" })]);
    const m = /\[\[(.*?)\]\]/.exec(b)!;
    expect(m[1]).toBe("EvoWiki/资料/线性代数讲义.pdf#page=3|讲义］］x｜［［y p.3");
    expect(b.match(/\[\[/g)).toHaveLength(1);
  });
  it("an origin that could re-aim the link is not linked", () => {
    expect(body("〔线性代数讲义 p.1〕", [src({ origin: "a]]b.pdf" })])).not.toContain("[[");
    expect(body("〔线性代数讲义 p.1〕", [src({ origin: "a|b#c.pdf" })])).not.toContain("[[");
  });
  it("inside code or math the mark is left alone", () => {
    expect(body("`〔线性代数讲义 p.12〕`", [src()])).toContain("`〔线性代数讲义 p.12〕`");
  });
  it("no sources: marks stay text", () => {
    expect(body("〔线性代数讲义 p.12〕", [])).toContain("〔线性代数讲义 p.12〕");
  });
  it("⟦和资料不同：…⟧ becomes the evowiki-differs strip", () => {
    expect(body("⟦和资料不同：通行写法是 A⟧", [])).toContain("<span class=\"evowiki-differs\">和资料不同：通行写法是 A</span>");
    expect(body("⟦和资料不同：<b>x</b> [[y]]⟧", [])).toContain("&lt;b>x&lt;/b> \\[\\[y]]</span>");
  });
  it("the page's materials changing (one deleted) changes the text and the hash", () => {
    const at = (deleted: boolean) => renderWikiNote(page({ sources: [src({ deleted })], sections: [sec("e", "explain", { body: "〔线性代数讲义 p.12〕" })] }));
    expect(at(true).markdown).not.toBe(at(false).markdown);
    expect(at(true).hash).not.toBe(at(false).hash);
  });
  it("a line section with several paragraphs (an accepted rewrite) stays a section, not one bullet", () => {
    const base = { id: "s1", heading: "头", cue: "", folded: false, role: "line" as const };
    expect(sectionLines("p", { ...base, body: "一句。" }).join("\n")).toContain("- **头**：一句。");
    expect(sectionLines("p", { ...base, body: "第一段。\n\n第二段。" })[0]).toBe("## 头");
  });

  it("a folded section and a line role render them too", () => {
    const b = renderWikiBody(page({ sources: [src()], sections: [sec("l", "line", { body: "见〔线性代数讲义 p.2〕" }), sec("f", "explain", { folded: true, body: "〔线性代数讲义 p.3〕" })] }));
    expect(b).toContain("#page=2|"); expect(b).toContain("#page=3|");
  });
});

describe("a fill-in's blank", () => {
  it("is escaped outside code, so markdown draws it", () => {
    expect(visibleBlanks("第 1 个位置：$4 \\times 2 =$ ____")).toBe("第 1 个位置：$4 \\times 2 =$ \\_\\_\\_\\_");
    expect(visibleBlanks("```evoui\n{\"a\":\"____\"}\n```")).toBe("```evoui\n{\"a\":\"____\"}\n```");
    expect(visibleBlanks(visibleBlanks("____"))).toBe(visibleBlanks("____"));
  });
});
