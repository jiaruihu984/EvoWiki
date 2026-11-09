import { describe, expect, it, vi } from "vitest";
import { CheckBlockHub } from "./check-block";
import { firstPage, setup } from "./controller-test-kit";
import { WIKI_COPY } from "./copy";
import { renderWikiNote } from "./note/format";
import { toWikiNoteInput } from "./note-input";
import type { SectionOut } from "./schema";
import {
  headingView, type HeadingRef, matchSection, menuButton, menuKey, mountSectionMenu, noteHeadings, runSectionAction, sectionMenu, type SectionSource,
} from "./section-menu";
import { sectionQuote } from "./section-quote";
import type { WikiState } from "./state";

async function opened() {
  const s = setup();
  await firstPage(s.c);
  return s;
}
type Open = Extract<WikiState["page"], { kind: "open" }>;
const page = (st: WikiState): Open => { if (st.page.kind !== "open") throw new Error("no page"); return st.page; };
/** The state with the open page's sections mapped. */
const withSections = (st: WikiState, f: (x: SectionOut, i: number) => SectionOut): WikiState =>
  ({ ...st, page: { ...page(st), page: { ...page(st).page, sections: page(st).page.sections.map(f) } } });
const noteOf = (st: WikiState, names = setup().c.names) => renderWikiNote(toWikiNoteInput(page(st).page, names)).markdown;
const lineOf = (text: string, line: string) => text.split("\n").indexOf(line);

describe("noteHeadings", () => {
  it("finds the ## lines between the markers only (not 我的笔记, not fenced code), with page id and same-text ordinals", async () => {
    const { c } = await opened();
    const st = withSections(c.state, (x) => (x.id === "s3" ? { ...x, role: "explain" as const, heading: "z = ±1 是什么意思" } : x));
    const md = noteOf(st);
    const nh = noteHeadings(md)!;
    expect(nh.pageId).toBe(page(st).page.page_id);
    const refs = [...nh.byLine.entries()].sort((a, b) => a[0] - b[0]).map(([, r]) => r);
    expect(refs.map((r) => [r.heading, r.ordinal])).toEqual([["z = ±1 是什么意思", 0], ["z = ±1 是什么意思", 1]]);
    expect(nh.byLine.get(lineOf(md, "## 我的笔记"))).toBeUndefined();
    // A ## inside fenced code in a section body is not a heading.
    const fenced = noteOf(withSections(c.state, (x) => (x.id === "s4" ? { ...x, body: "```\n## 不是标题\n```" } : x)));
    expect([...noteHeadings(fenced)!.byLine.values()].map((r) => r.heading)).not.toContain("不是标题");
  });

  it("null for a file that is not a v2 wiki note", () => {
    expect(noteHeadings("## 标题\n正文\n")).toBeNull();
    expect(noteHeadings("---\nevowiki_page_id: p1\n---\n%% evowiki:start %%\n## a\n%% evowiki:end %%\n")).toBeNull(); // no hash/format
  });
});

describe("matchSection (R4.1)", () => {
  it("matches the heading text the note writes, same-name headings in order; another page or no match → null", async () => {
    const { c } = await opened();
    const st = withSections(c.state, (x) => (x.id === "s3" ? { ...x, role: "compare" as const, heading: "z = ±1 是什么意思" } : x));
    const id = page(st).page.page_id;
    expect(matchSection(st, { pageId: id, heading: "z = ±1 是什么意思", ordinal: 0 })?.id).toBe("s3");
    expect(matchSection(st, { pageId: id, heading: "z = ±1 是什么意思", ordinal: 1 })?.id).toBe("s4");
    expect(matchSection(st, { pageId: id, heading: "z = ±1 是什么意思", ordinal: 2 })).toBeNull();
    expect(matchSection(st, { pageId: id, heading: "没有这一段", ordinal: 0 })).toBeNull();
    expect(matchSection(st, { pageId: "other", heading: "z = ±1 是什么意思", ordinal: 0 })).toBeNull();
    // Only sections the note writes as ## (explain / compare / recap, unfolded): a folded section never matches; a recap
    // is a normal ## section since T10.
    expect(matchSection(withSections(st, (x) => (x.id === "s4" ? { ...x, folded: true } : x)),
      { pageId: id, heading: "z = ±1 是什么意思", ordinal: 1 })).toBeNull();
    expect(matchSection(c.state, { pageId: id, heading: "回顾：方差与标准差", ordinal: 0 })?.role).toBe("recap");
    expect(matchSection({ ...c.state, page: { kind: "loading", pageId: id } }, { pageId: id, heading: "z = ±1 是什么意思", ordinal: 0 }))
      .toBeNull();
  });

  it("a ## subheading inside a section's text never takes a section's menu, nor shifts the ordinals", async () => {
    const { c } = await opened();
    // s1 (organizer, before s4) has a body subheading with s4's heading text; the note writes it unescaped as a ## line.
    const st = withSections(c.state, (x) => (x.id === "s1" ? { ...x, body: `${x.body}

## z = ±1 是什么意思

更多` } : x));
    const md = noteOf(st);
    const refs = [...noteHeadings(md)!.byLine.entries()].sort((a, b) => a[0] - b[0]).map(([, r]) => r)
      .filter((r) => r.heading === "z = ±1 是什么意思");
    expect(refs.map((r) => r.ordinal)).toEqual([0, 1]);
    expect(matchSection(st, refs[0]!)).toBeNull(); // the body subheading
    expect(matchSection(st, refs[1]!)?.id).toBe("s4"); // the section's own heading
    expect(sectionMenu(st, refs[0]!)).toBeNull();
  });

  it("menuKey changes with what decides the menus only (not busy, pins or connection)", async () => {
    const { c } = await opened();
    const k = menuKey(c.state);
    expect(menuKey({ ...c.state, busy: { what: "rewrite", sid: "s4", abortable: true }, conn: "offline" })).toBe(k);
    expect(menuKey(withSections(c.state, (x) => (x.id === "s4" ? { ...x, pinned: true } : x)))).toBe(k);
    expect(menuKey(withSections(c.state, (x) => (x.id === "s4" ? { ...x, heading: "别的" } : x)))).not.toBe(k);
    expect(menuKey(withSections(c.state, (x) => (x.id === "s1" ? { ...x, body: "## 新小标题" } : x)))).not.toBe(k);
    expect(menuKey({ ...c.state, page: { kind: "none" } })).toBe("");
  });

  it("escaped heading text: the note's written form matches its section", async () => {
    const { c } = await opened();
    const st = withSections(c.state, (x) => (x.id === "s4" ? { ...x, heading: "[x] 和 #tag" } : x));
    const md = noteOf(st);
    const ref = [...noteHeadings(md)!.byLine.values()].find((r) => r.heading.includes("tag"))!;
    expect(ref.heading).toBe("\\[x] 和 \\#tag");
    expect(matchSection(st, ref)?.id).toBe("s4");
  });
});

describe("sectionMenu", () => {
  const ref = (st: WikiState): HeadingRef => ({ pageId: page(st).page.page_id, heading: "z = ±1 是什么意思", ordinal: 0 });

  it("app.js entries in order and groups (no 讲深一点 / 换个讲法 / 重新组织本页); pinned → know off; busy / offline → all but 问助手 off; empty body → none", async () => {
    const { c } = await opened();
    const m = sectionMenu(c.state, ref(c.state))!;
    expect(m.sid).toBe("s4");
    expect(m.entries.map((e) => [e.action, e.title, e.disabled, e.group])).toEqual([
      ["know", "标记为已掌握", false, "mark"], ["dont", "标记为不会", false, "mark"],
      ["ask", "问助手", false, "ask"], ["pin", "锁定这一节", false, "more"]]);
    const pinned = withSections(c.state, (x) => (x.id === "s4" ? { ...x, pinned: true } : x));
    expect(sectionMenu(pinned, ref(pinned))!.entries.map((e) => [e.title, e.disabled])).toEqual([
      [WIKI_COPY.know, true], [WIKI_COPY.menuDont, false], [WIKI_COPY.menuAsk, false], [WIKI_COPY.unpin, false]]);
    // A pinned section's 标记为已掌握 carries 先解除锁定 (app.js: whenever pinned); an unpinned one carries no hint, offline or not.
    expect(sectionMenu(pinned, ref(pinned))!.entries.filter((e) => e.hint !== undefined).map((e) => [e.action, e.hint]))
      .toEqual([["know", "先解除锁定"]]);
    const offlinePinned = { ...pinned, conn: "offline" as const };
    expect(sectionMenu(offlinePinned, ref(offlinePinned))!.entries.filter((e) => e.hint !== undefined).length).toBe(1);
    const offline = { ...c.state, conn: "offline" as const };
    expect(sectionMenu(offline, ref(offline))!.entries.some((e) => e.hint !== undefined)).toBe(false);
    for (const st of [{ ...c.state, busy: { what: "know", abortable: false } }, { ...c.state, conn: "offline" as const },
      { ...c.state, configured: false }]) {
      expect(sectionMenu(st, ref(st))!.entries.map((e) => [e.action, e.disabled]))
        .toEqual([["know", true], ["dont", true], ["ask", false], ["pin", true]]);
    }
    // pinned and blocked together
    const both = { ...pinned, conn: "offline" as const };
    expect(sectionMenu(both, ref(both))!.entries.filter((e) => !e.disabled).map((e) => e.action)).toEqual(["ask"]);
    const empty = withSections(c.state, (x) => (x.id === "s4" ? { ...x, body: " " } : x));
    expect(sectionMenu(empty, ref(empty))).toBeNull();
  });

  it("no feedback entries any more; no menu on a collapsed page; 这里的概念我不会 off without cells", async () => {
    const { c } = await opened();
    const titles = (st: WikiState) => sectionMenu(st, ref(st))!.entries.map((e) => e.title);
    expect(titles(c.state)).not.toContain("太浅了");
    expect(titles(c.state)).not.toContain("有点难");
    expect(titles(c.state)).not.toContain("这一节还有别的问题…");
    // A collapsed page writes every section folded, so no heading matches and there is no menu (nothing to expand from here).
    const collapsed = withSections({ ...c.state, page: { ...page(c.state), page: { ...page(c.state).page, collapsed: true } } },
      (x) => ({ ...x, folded: true }));
    expect(sectionMenu(collapsed, ref(collapsed))).toBeNull();
    const none = withSections(c.state, (x) => (x.id === "s4" ? { ...x, cells: [] } : x));
    expect(sectionMenu(none, ref(none))!.entries.find((e) => e.action === "dont")!.disabled).toBe(true);
  });

  it("runSectionAction calls the controller's section actions", async () => {
    const { c } = await opened();
    const a = { state: c.state, know: vi.fn(), togglePin: vi.fn(), declareSectionDont: vi.fn(), askAssistant: vi.fn() };
    const say = vi.fn();
    for (const x of ["know", "dont", "ask", "pin"] as const) await runSectionAction(a, "s4", x, say);
    expect(say).not.toHaveBeenCalled(); // nothing changed the result lines: no notice
    expect(a.know.mock.calls).toEqual([["s4"]]);
    expect(a.declareSectionDont.mock.calls).toEqual([["s4"]]);
    expect(a.askAssistant.mock.calls).toEqual([["s4"]]);
    expect(a.togglePin.mock.calls).toEqual([["s4"]]);
  });

  it("a success says nothing (the page shows it); a failure shows the controller's error line instead", async () => {
    const s = await opened();
    const say = vi.fn();
    await runSectionAction(s.c, "s4", "pin", say);
    await runSectionAction(s.c, "s4", "pin", say);
    await runSectionAction(s.c, "s4", "know", say);
    expect(say).not.toHaveBeenCalled();
    const { ApiError } = await import("../api/errors");
    s.api.failNext("pin", new ApiError(500, "x"));
    await runSectionAction(s.c, "s4", "pin", say);
    const note = page(s.c.state).sectionNotes.s4!;
    expect(note.tone).toBe("error");
    expect(say.mock.calls).toEqual([[note.text]]);
  });
});

describe("controller side of the menu", () => {
  it("这里的概念我不会: one declare per distinct cell of the section; Notice names up to three concepts; the first failure stops", async () => {
    const s = await opened();
    const sec = page(s.c.state).page.sections.find((x) => x.id === "s4")!;
    const cells = [...new Set(sec.cells.map((x) => x.cell))];
    expect(cells.length).toBeGreaterThan(1);
    const declare = vi.spyOn(s.api, "declare");
    s.notices.length = 0;
    await s.c.declareSectionDont("s4");
    expect(declare.mock.calls).toEqual(cells.map((x) => [x, "dont"]));
    const names = [...new Set(sec.cells.map((x) => x.concept_name))];
    expect(names.length).toBeGreaterThan(0);
    expect(s.notices).toEqual([WIKI_COPY.dontDone]);
    expect(s.asked.map((a) => a.quote.heading)).toEqual([sec.heading]);   // the chat takes over; the note stays
    expect(s.notices[0]).toBe("已标记为不会");
    // failing on the second cell: only the first is recorded and the Notice says so
    declare.mockRestore();
    const orig = s.api.declare.bind(s.api);
    let n = 0;
    const failing = vi.spyOn(s.api, "declare").mockImplementation((...a) => (++n === 2 ? Promise.reject(new Error("boom")) : orig(...a)));
    s.notices.length = 0;
    await s.c.declareSectionDont("s4");
    expect(failing).toHaveBeenCalledTimes(2);
    expect(s.notices).toEqual([WIKI_COPY.dontPartial(1, [sec.cells.find((x) => x.cell === cells[0])!.concept_name])]);
  });

  it("a partial result lists three names then 等", () => {
    expect(WIKI_COPY.dontPartial(4, ["甲", "乙", "丙", "丁"])).toBe("只标记了前 4 个：甲、乙、丙等 · 不会。");
  });

  it("问助手: hands the section's heading and opening to the assistant dep; nothing is sent to the server", async () => {
    const s = await opened();
    const sec = page(s.c.state).page.sections.find((x) => x.id === "s4")!;
    const before = s.api.calls.length;
    s.c.askAssistant("s4");
    s.c.askAssistant("nope");
    expect(s.asked).toEqual([{ pageId: page(s.c.state).page.page_id, title: page(s.c.state).page.title, quote: sectionQuote(sec) }]);
    expect(s.api.calls.length).toBe(before);
    const q = sectionQuote(sec);
    expect(q.heading).toBe(sec.heading);
    expect(q.text.startsWith(`${sec.heading}
`)).toBe(true);
    expect([...q.text].length).toBeLessThanOrEqual([...sec.heading].length + 1 + 200);
    expect(sectionQuote({ heading: "标题", body: "" })).toEqual({ text: "标题", heading: "标题" });
  });
});

describe("the 「⋯」 button", () => {
  it("mountSectionMenu: hidden until the hub's controller has the page open; a click opens the menu; cleanup removes it", async () => {
    const { c } = setup();
    const hub = new CheckBlockHub<SectionSource & { answerCheck(sid: string, ch: number): unknown; subscribe(fn: () => void): () => void }>();
    const h2 = document.createElement("h2");
    const open = vi.fn();
    const pageId = "p1";
    const r: HeadingRef = { pageId, heading: "z = ±1 是什么意思", ordinal: 0 };
    const block = document.createElement("div");
    block.append(h2);
    h2.append("z = ±1 是什么意思");
    const stop = mountSectionMenu(h2, r, hub, open);
    const parts = h2.querySelector<HTMLElement>(":scope > span.ew.ew-inline")!;
    expect(parts).not.toBeNull();
    expect(parts.children.length).toBe(0); // no controller: nothing drawn
    hub.set(c);
    expect(parts.children.length).toBe(0); // no page open
    await firstPage(c);
    expect(page(c.state).page.page_id).toBe(pageId);
    const b = parts.querySelector<HTMLButtonElement>("button.ew-more")!;
    expect(b.textContent).toBe("⋯");
    expect(b.type).toBe("button");
    expect(b.getAttribute("aria-haspopup")).toBe("menu");
    expect(b.getAttribute("aria-expanded")).toBe("false");
    expect(b.getAttribute("aria-label")).toBe(WIKI_COPY.sectionMenu);
    expect([...parts.children].map((x) => x.className)).toEqual(["ew-more"]);
    b.click();
    expect(open).toHaveBeenCalledOnce();
    expect(open.mock.calls[0]!.slice(1)).toEqual([c, r]);
    // pinned → 已钉住 before ⋯
    const sid = matchSection(c.state, r)!.id;
    await c.togglePin(sid);
    expect([...parts.children].map((x) => x.className)).toEqual(["ew-pin", "ew-more"]);
    expect(parts.children[0]!.firstChild?.textContent).toBe("已锁定");
    stop();
    expect(h2.querySelector("button")).toBeNull();
    expect(block.children.length).toBe(1);
  });

  it("headingView: the tag while the section carries the last feedback patch", async () => {
    const { c } = await opened();
    const r: HeadingRef = { pageId: page(c.state).page.page_id, heading: "z = ±1 是什么意思", ordinal: 0 };
    expect(headingView(c.state, r)).toEqual({ tag: false, revised: false, pin: false, menu: true });
    const fb = { ...page(c.state), page: { ...page(c.state).page,
      feedback: { summary: "x", sections: ["s4"], can_undo: true } as unknown as NonNullable<Open["page"]["feedback"]> } };
    expect(headingView({ ...c.state, page: fb }, r).tag).toBe(true);
    expect(headingView(null, r)).toEqual({ tag: false, revised: false, pin: false, menu: false });
  });

  it("menuButton does not open when the heading no longer matches", () => {
    const { c } = setup();
    const open = vi.fn();
    const b = menuButton({ pageId: "p1", heading: "x", ordinal: 0 }, { current: c, subscribe: () => () => undefined }, open);
    b.click();
    expect(open).not.toHaveBeenCalled();
  });
});
