import { describe, expect, it } from "vitest";
import { writtenFrom } from "./edit-diff";
import { fromNoteBlock, noteBlock, renderWikiNote, renderWikiBody, updateWikiNote, WIKI_END, WIKI_START } from "./format";
import { planHandEdit } from "./hand-edit";
import type { WikiNoteInput, WikiNoteSection } from "./types";
import type { HandEditIn, PageSourceOut } from "../schema";

const NOW = new Date("2026-11-04T08:00:00Z");
const sec = (id: string, heading: string, body: string, o: Partial<WikiNoteSection> = {}): WikiNoteSection =>
  ({ id, heading, role: "explain", cue: "", body, folded: false, ...o });
const SRC: PageSourceOut = { source_id: "src1", title: "教材", kind: "pdf", origin: "资料/教材.pdf", deleted: false } as unknown as PageSourceOut;
const WEB: PageSourceOut = { source_id: "src2", title: "网页", kind: "url", origin: "https://x.org", deleted: false } as unknown as PageSourceOut;
const EMBED = "```embedded-html\ntype: embedded-html\nid: ia1\nsrc: assets/ia1.html\ntitle: 拖一拖\nheight: 400\n```";
const EVOUI = "```evoui\nasset: ia2\n```";
const IMAGE = "```evowiki-image\nasset: ia3\nalt: 图\ntitle: 图\nlicense: CC0\nsource: Wikimedia Commons\n```";

function input(sections: WikiNoteSection[]): WikiNoteInput {
  return { pageId: "p1", title: "标题", goal: "", diffText: null, collapsed: false, sections, sources: [SRC, WEB] };
}
/** The note as written for `i`, with `edit` applied to the text between the markers. */
function edited(i: WikiNoteInput, edit: (body: string) => string): string {
  const { markdown } = renderWikiNote(i, NOW);
  const a = markdown.indexOf(`${WIKI_START}\n`) + WIKI_START.length + 1, b = markdown.indexOf(WIKI_END);
  return markdown.slice(0, a) + edit(markdown.slice(a, b)) + markdown.slice(b);
}
function plan(i: WikiNoteInput, edit: (body: string) => string): HandEditIn {
  const p = planHandEdit(edited(i, edit), writtenFrom(i), i.sources);
  if (p.kind !== "ok") throw new Error(`expected ok, got ${JSON.stringify(p)}`);
  return p.edit;
}
/** The input as the server answers after `edit`: the changed sections hold what was sent, the deleted ones are gone. */
function applied(i: WikiNoteInput, e: HandEditIn): WikiNoteInput {
  return { ...i, sections: i.sections.filter((s) => !(e.removed ?? []).includes(s.id)).map((s) => {
    const x = (e.sections ?? []).find((y) => y.section_id === s.id);
    return x === undefined ? s : { ...s, body: x.body, noteBody: x.note_body, heading: x.heading ?? s.heading, cue: x.cue ?? (x.body === s.body ? s.cue : ""), folded: x.folded ?? s.folded };
  }) };
}
/** What the note's block reads once the edit went up, after the sync wrote it. */
function blockAfter(i: WikiNoteInput, text: string, e: HandEditIn): string {
  const u = updateWikiNote(text, applied(i, e), NOW, true);
  if (u.kind !== "write") throw new Error(u.kind);
  return u.text.slice(u.text.indexOf(WIKI_START) + WIKI_START.length + 1, u.text.indexOf(WIKI_END));
}

const PAGE = input([
  sec("s1", "开篇", "第一句。\n第二句。", { role: "organizer" }),
  sec("s2", "均值", "均值是总和除以个数〔教材 p.12｜30〕，公式 $\\mu = \\sum x / N$。\n\n要删的一句。\n\n> 这里简化了：只看离散情形", { cue: "μ 怎么算" }),
  sec("s3", "方差", `方差看离散程度。\n\n${EMBED}\n\n$$\n\\sigma^2 = \\frac{1}{N}\n$$\n\n${EVOUI}\n\n${IMAGE}`),
  sec("s4", "一句话", "顺带一提。", { role: "line" }),
  sec("q1", "确认题", "题干", { role: "check" }),
]);

describe("the way back from note Markdown", () => {
  it("editing prose preserves completion checks drawn outside the editable body", () => {
    const i = input([...PAGE.sections, sec("goal_check_1", "确认题", "", { role: "check" })]);
    const e = plan(i, (body) => body.replace("第一句。", "改过的第一句。"));
    expect(e.removed).not.toContain("goal_check_1");
    expect(e.sections?.map((s) => s.section_id)).toEqual(["s1"]);
  });
  it.each([
    ["a citation link", "见〔教材 p.12｜30〕。"],
    ["a citation range", "见〔教材 p.12–13｜30〕。"],
    ["a plain-text citation", "见〔网页 p.3〕。"],
    ["a lookup pill with its claim", "光速是⟦待查证：299792 km/s⟧。"],
    ["a bare lookup pill", "这里⟦待查证⟧。"],
    ["a disagreement strip", "⟦和资料不同：通行写法是 A<B & C⟧"],
    ["an escaped bracket and tag", "数组 [1,2] 的 #tag 和 [[别的页]]。"],
    ["a page link", "见[均值](#p:pg_1)。"],
    ["a bold that does not show", "**节点（node）**就是点。"],
    ["inline code and math stay verbatim", "`a[0]` 和 $x[1]$ 还有 \\#。"],
    ["a comment opener and marker percent", "<!-- x --> 和 100%% 以及 `<!--`。"],
  ])("%s: block(back(block(x))) is block(x)", (_n, x) => {
    const shown = noteBlock(x, [SRC, WEB]);
    expect(noteBlock(fromNoteBlock(shown, [SRC, WEB]), [SRC, WEB])).toBe(shown);
  });

  it("a claim he changed keeps the pill, not the old claim", () => {
    const shown = noteBlock("光速是⟦待查证：299792 km/s⟧。", []).replace("299792 km/s<", "三十万公里<");
    expect(fromNoteBlock(shown)).toBe("光速是三十万公里⟦待查证⟧。");
  });
});

describe("planHandEdit: what goes up", () => {
  it("a changed section goes up with the fingerprint of the copy the note was written from; an untouched one sends none", () => {
    const i = input([sec("s1", "一", "第一段。", { fingerprint: "fp-1" }), sec("s2", "二", "第二段。", { fingerprint: "fp-2" })]);
    const e = plan(i, (b) => b.replace("第二段。", "第二段改了。"));
    expect(e.sections!.map((x) => [x.section_id, x.fingerprint])).toEqual([["s2", "fp-2"]]);
    const old = input([sec("s1", "一", "第一段。"), sec("s2", "二", "第二段。")]);       // a copy from before fingerprints
    expect(plan(old, (b) => b.replace("第二段。", "第二段改了。")).sections![0]!.fingerprint).toBeUndefined();
  });

  it("a changed sentence: the note text as typed, the reading text with its marks back; the rest of the section is byte for byte", () => {
    const e = plan(PAGE, (b) => b.replace("均值是总和除以个数", "我的说法：平均数等于总和除以个数"));
    expect(e.removed).toEqual([]);
    expect(e.sections).toHaveLength(1);
    const s = e.sections![0]!;
    expect(s.section_id).toBe("s2");
    expect(s.note_body).toContain("[[资料/教材.pdf#page=30|教材 p.12]]");
    expect(s.body).toBe(PAGE.sections[1]!.body.replace("均值是总和除以个数", "我的说法：平均数等于总和除以个数"));
    expect(e.summary).toContain("我的说法");
  });

  it("a deleted sentence is an edit", () => {
    const e = plan(PAGE, (b) => b.replace("要删的一句。\n\n", ""));
    expect(e.sections![0]!.body).not.toContain("要删的一句");
    expect(e.sections![0]!.body).toContain("〔教材 p.12｜30〕");
  });

  it("the blocks of a section he edited around survive byte for byte, and formulas with them", () => {
    const e = plan(PAGE, (b) => b.replace("方差看离散程度。", "方差看离散程度，我加一句。"));
    const s = e.sections![0]!;
    expect(s.body).toBe(PAGE.sections[2]!.body.replace("方差看离散程度。", "方差看离散程度，我加一句。"));
    for (const block of [EMBED, EVOUI, IMAGE, "$$\n\\sigma^2 = \\frac{1}{N}\n$$"]) expect(s.body).toContain(block);
  });

  it("his own wikilink, tag and image embed stay his in the note, and read as he typed them", () => {
    const text = edited(PAGE, (b) => b.replace("第二句。", "第二句，见 [[我的笔记]] #复习 ![[图.png]]。"));
    const p = planHandEdit(text, writtenFrom(PAGE), PAGE.sources);
    if (p.kind !== "ok") throw new Error("not ok");
    expect(p.edit.sections![0]!.note_body).toContain("[[我的笔记]] #复习 ![[图.png]]");
    expect(p.edit.sections![0]!.body).toContain("[[我的笔记]] #复习 ![[图.png]]");
    // the note reads exactly what he typed after the write
    expect(blockAfter(PAGE, text, p.edit)).toContain("第二句，见 [[我的笔记]] #复习 ![[图.png]]。");
  });

  it("after the write the whole block is what he left: no edit jumps anywhere", () => {
    const edit = (b: string): string => b.replace("第二句。", "第二句，改过。").replace("要删的一句。\n\n", "");
    const text = edited(PAGE, edit);
    const p = planHandEdit(text, writtenFrom(PAGE), PAGE.sources);
    if (p.kind !== "ok") throw new Error("not ok");
    expect(blockAfter(PAGE, text, p.edit)).toBe(edit(renderWikiBody(PAGE)));
  });

  it("another section's update does not touch the edited one", () => {
    const text = edited(PAGE, (b) => b.replace("第二句。", "第二句，改过。"));
    const p = planHandEdit(text, writtenFrom(PAGE), PAGE.sources);
    if (p.kind !== "ok") throw new Error("not ok");
    const next = applied(PAGE, p.edit);
    next.sections[3] = { ...next.sections[3]!, body: "系统重写了这一句。" };
    next.sections[2] = { ...next.sections[2]!, folded: true, cue: "方差" };
    const u = updateWikiNote(text, next, NOW, true);
    if (u.kind !== "write") throw new Error(u.kind);
    expect(u.text).toContain("第二句，改过。");
    expect(u.text).toContain("系统重写了这一句。");
    expect(u.text).toContain("> [!evowiki-known]- 方差");
  });

  it("an edit inside a folded section stays in its callout; a new title is its cue", () => {
    const folded = input([sec("s0", "开头", "开头句。"), sec("s1", "均值", "第一句。\n\n第二句。", { folded: true, cue: "μ 怎么算" })]);
    const e = plan(folded, (b) => b.replace("> 第二句。", "> 第二句，改过。").replace("]- μ 怎么算", "]- 我的提示"));
    expect(e.sections![0]).toMatchObject({ section_id: "s1", note_body: "第一句。\n\n第二句，改过。", cue: "我的提示" });
    expect(e.sections![0]!.folded).toBeUndefined();
  });

  it("text typed right below a folded callout joins the section, which opens", () => {
    const folded = input([sec("s0", "开头", "开头句。"), sec("s1", "均值", "第一句。", { folded: true, cue: "c" }), sec("s2", "后面", "后面句。")]);
    const e = plan(folded, (b) => b.replace("> 第一句。\n", "> 第一句。\n\n写在外面。\n"));
    expect(e.sections![0]).toMatchObject({ section_id: "s1", folded: false });
    expect(e.sections![0]!.note_body).toContain("写在外面。");
  });

  it("a changed heading goes up; a one-line section keeps its form", () => {
    const e = plan(PAGE, (b) => b.replace("## 均值", "## 平均数").replace("- **一句话**：顺带一提。", "- **一句话**：顺带提一下。"));
    expect(e.sections!.map((s) => [s.section_id, s.heading])).toEqual([["s2", "平均数"], ["s4", undefined]]);
    expect(e.sections![1]!.note_body).toBe("顺带提一下。");
  });
});

describe("planHandEdit: the edges", () => {
  it("a new ## heading typed inside a section stays that section's text", () => {
    const e = plan(PAGE, (b) => b.replace("要删的一句。", "要删的一句。\n\n## 我加的小标题\n\n我的内容。"));
    expect(e.sections).toHaveLength(1);
    expect(e.sections![0]!.section_id).toBe("s2");
    expect(e.sections![0]!.body).toContain("## 我加的小标题\n\n我的内容。");
  });

  it("a whole section deleted: it leaves the page", () => {
    const body = renderWikiBody(PAGE);
    const a = body.indexOf("## 均值"), b = body.indexOf("## 方差");
    const e = plan(PAGE, () => body.slice(0, a) + body.slice(b));
    expect(e).toMatchObject({ sections: [], removed: ["s2"] });
  });

  it("all the text of a section deleted, its heading left: the section is deleted", () => {
    const e = plan(PAGE, (b) => b.replace(/均值是总和[\s\S]*?只看离散情形\n/, ""));
    expect(e.removed).toEqual(["s2"]);
  });

  it("a selection deleted across two sections: what is left of the second joins the first", () => {
    const e = plan(PAGE, (b) => b.replace(/要删的一句。[\s\S]*?方差看离散程度。\n/, ""));
    expect(e.removed).toEqual(["s3"]);
    expect(e.sections!.map((s) => s.section_id)).toEqual(["s2"]);
    expect(e.sections![0]!.body).toContain("均值是总和");
    expect(e.sections![0]!.body).toContain("$$\n\\sigma^2");
    expect(e.sections![0]!.body).toContain(EMBED);
  });

  it("a check section deleted whole is a deletion; changed in part it fails closed", () => {
    const body = renderWikiBody(PAGE);
    const at = body.indexOf("```evowiki-check");
    expect(plan(PAGE, () => body.slice(0, at))).toMatchObject({ removed: ["q1"] });
    const p = planHandEdit(edited(PAGE, (b) => b.replace("section: q1", "section: q2")), writtenFrom(PAGE), PAGE.sources);
    expect(p).toMatchObject({ kind: "unsafe", why: "check" });
  });

  it("a line typed above the first section goes to its start", () => {
    const e = plan(PAGE, (b) => `我在最上面写的。\n\n${b}`);
    expect(e.sections![0]).toMatchObject({ section_id: "s1" });
    expect(e.sections![0]!.note_body.startsWith("我在最上面写的。\n\n第一句。")).toBe(true);
  });

  it("only blank lines moved: nothing goes up", () => {
    expect(planHandEdit(edited(PAGE, (b) => b.replace("## 方差", "\n## 方差")), writtenFrom(PAGE), PAGE.sources)).toEqual({ kind: "none" });
  });
});

describe("planHandEdit: fails closed", () => {
  const bad = (edit: (b: string) => string): unknown => planHandEdit(edited(PAGE, edit), writtenFrom(PAGE), PAGE.sources);

  it("an interactive changed in part", () => {
    expect(bad((b) => b.replace("src: assets/ia1.html", "src: assets/other.html"))).toMatchObject({ kind: "unsafe", why: "block", label: "方差" });
    expect(bad((b) => b.replace("asset: ia2", "asset: ia9"))).toMatchObject({ kind: "unsafe", why: "block" });
    expect(bad((b) => b.replace("license: CC0", "license: 我的"))).toMatchObject({ kind: "unsafe", why: "block" });
  });

  it("a block deleted whole is a real edit", () => {
    const e = plan(PAGE, (b) => b.replace(`${EMBED}\n\n`, ""));
    expect(e.sections![0]!.body).not.toContain("embedded-html");
    expect(e.sections![0]!.body).toContain(EVOUI);
  });

  it("a block's closing fence deleted: the fence would swallow the rest of the note", () => {
    expect(bad((b) => b.replace("\n```\n\n```evoui", "\n\n```evoui").replace("asset: ia3\nalt: 图\ntitle: 图\nlicense: CC0\nsource: Wikimedia Commons\n```", "asset: ia3\nalt: 图\ntitle: 图\nlicense: CC0\nsource: Wikimedia Commons")))
      .toMatchObject({ kind: "unsafe" });
  });

  it("a section's written source unknown: only a section he changed is refused", () => {
    const w = writtenFrom(PAGE);
    const old = { ...w, layout: { sections: w.layout.sections.map((s) => (s.id === "s2" ? { ...s, src: undefined as never } : s)) } };
    expect(planHandEdit(edited(PAGE, (b) => b.replace("均值是", "均值即")), old, PAGE.sources)).toMatchObject({ kind: "unsafe", why: "source", label: "均值" });
    expect(planHandEdit(edited(PAGE, (b) => b.replace("第二句。", "第二句，改过。")), old, PAGE.sources)).toMatchObject({ kind: "ok" });
  });

  it("the first section's heading deleted while its text stays: it has none to join", () => {
    const i = input([sec("s1", "第一节", "第一句。"), sec("s2", "第二节", "第二句。")]);
    expect(planHandEdit(edited(i, (b) => b.replace("## 第一节\n\n", "")), writtenFrom(i), [])).toMatchObject({ kind: "unsafe", why: "first", label: "第一节" });
  });

  it("every section deleted", () => {
    expect(bad(() => "")).toMatchObject({ kind: "unsafe", why: "all" });
  });

  it("a heading with a link would not read back the same", () => {
    expect(bad((b) => b.replace("## 均值", "## [[均值]]"))).toMatchObject({ kind: "unsafe", why: "heading" });
  });

  it("a note of another hash, or not edited: nothing", () => {
    expect(planHandEdit(renderWikiNote(PAGE, NOW).markdown, writtenFrom(PAGE), PAGE.sources)).toEqual({ kind: "none" });
    expect(planHandEdit(edited(PAGE, (b) => `${b}x\n`), { ...writtenFrom(PAGE), hash: "sha256:0" }, [])).toEqual({ kind: "none" });
  });
});
