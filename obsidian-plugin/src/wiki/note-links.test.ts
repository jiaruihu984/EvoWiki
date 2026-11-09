/**
 * Page links in a wiki note (`[text](#p:<id>)`, app.js a.inl): the Reading-view post-processor and the Live Preview view
 * plugin open the page through the given callback, never through Obsidian's own link handling; and the controller's
 * openPageLink opens the note, or says why not in a notice.
 */
import { EditorSelection, EditorState } from "@codemirror/state";
import { type DecorationSet, EditorView, type ViewPlugin } from "@codemirror/view";
import type { MarkdownPostProcessorContext } from "obsidian";
import { afterEach, describe, expect, it, vi } from "vitest";
import { editorLivePreviewField } from "../../test/obsidian-mock";
import { firstPage, setup } from "./controller-test-kit";
import { renderWikiNote } from "./note/format";
import { noteLinksExtension, noteLinksPostProcessor, PAGE_LINK_CLASS, pageLinkId, pageLinksIn } from "./note-links";
import type { WikiNoteInput } from "./note/types";

const ctx = (fm: Record<string, unknown> | null, text: string | null = null) =>
  ({ frontmatter: fm, sourcePath: "EvoWiki/A.md", getSectionInfo: () => (text === null ? null : { text, lineStart: 0, lineEnd: 0 }) }) as unknown as MarkdownPostProcessorContext;

/** A rendered paragraph as Obsidian draws it: the `#p:` link is an external link (its href has a colon). */
function paragraph(): { el: HTMLElement; page: HTMLAnchorElement; web: HTMLAnchorElement; outer: ReturnType<typeof vi.fn> } {
  const root = document.createElement("div");
  const el = root.appendChild(document.createElement("div"));
  const p = el.appendChild(document.createElement("p"));
  p.append("下一页的");
  const page = p.appendChild(document.createElement("a"));
  page.href = "#p:p3"; page.className = "external-link"; page.target = "_blank"; page.rel = "noopener nofollow";
  page.setAttribute("aria-label", "#p:p3"); page.textContent = "协方差矩阵";
  const web = p.appendChild(document.createElement("a"));
  web.href = "https://example.com/"; web.className = "external-link"; web.textContent = "外链";
  // Obsidian's own handling, delegated from an ancestor: must not see a page-link click.
  const outer = vi.fn();
  root.addEventListener("click", outer);
  document.body.append(root);
  return { el, page, web, outer };
}

describe("pageLinkId", () => {
  it("only `#p:` + a safe id", () => {
    expect(pageLinkId("#p:pg_ca62d07257d34436832c")).toBe("pg_ca62d07257d34436832c");
    for (const h of [null, "#heading", "#p:", "#p:a b", "#p:../x", "p:x", "https://x/#p:a"]) expect(pageLinkId(h), String(h)).toBeNull();
  });
});

describe("Reading view", () => {
  it("in a wiki note: the class, no new-tab or tooltip attributes, and a click opens the page and nothing else", () => {
    const { el, page, web, outer } = paragraph();
    const open = vi.fn();
    noteLinksPostProcessor(open)(el, ctx({ evowiki_page_id: "p2" }));
    expect(page.classList.contains(PAGE_LINK_CLASS)).toBe(true);
    expect(page.hasAttribute("target") || page.hasAttribute("aria-label") || page.hasAttribute("rel")).toBe(false);
    const ev = new MouseEvent("click", { bubbles: true, cancelable: true });
    page.dispatchEvent(ev);
    expect(open).toHaveBeenCalledWith("p3", "EvoWiki/A.md");
    expect(ev.defaultPrevented).toBe(true);
    expect(outer).not.toHaveBeenCalled();
    // Any other link is Obsidian's.
    web.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    expect(open).toHaveBeenCalledTimes(1);
    expect(outer).toHaveBeenCalledTimes(1);
    expect(web.classList.contains(PAGE_LINK_CLASS)).toBe(false);
  });

  it("a wiki note found by its block when the frontmatter is not given; any other note is left alone", () => {
    const a = paragraph(), openA = vi.fn();
    noteLinksPostProcessor(openA)(a.el, ctx(null, "%% evowiki:start %%\n…"));
    a.page.click();
    expect(openA).toHaveBeenCalledWith("p3", "EvoWiki/A.md");
    const b = paragraph(), openB = vi.fn();
    noteLinksPostProcessor(openB)(b.el, ctx({ tags: ["x"] }, "plain note"));
    b.page.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    expect(openB).not.toHaveBeenCalled();
    expect(b.page.classList.contains(PAGE_LINK_CLASS)).toBe(false);
  });
});

const input = (body: string): WikiNoteInput => ({
  pageId: "p2", title: "特征值", goal: "看懂 PCA", diffText: null, collapsed: false,
  sections: [{ id: "s3", heading: "怎么求", role: "explain", cue: "", folded: false, body }],
});

const views: EditorView[] = [];
afterEach(() => { for (const v of views.splice(0)) v.destroy(); document.body.replaceChildren(); });

function editor(md: string, open = vi.fn()) {
  const ext = noteLinksExtension(open);
  const view = new EditorView({ state: EditorState.create({ doc: md, extensions: [editorLivePreviewField, ext] }) });
  document.body.append(view.dom);
  views.push(view);
  const marked = (): string[] => {
    const set = (view.plugin(ext as ViewPlugin<{ decorations: DecorationSet }>)!).decorations;
    const out: string[] = [];
    set.between(0, view.state.doc.length, (from, to) => { out.push(view.state.doc.sliceString(from, to)); });
    return out;
  };
  return { view, open, marked };
}

describe("Live Preview", () => {
  const md = renderWikiNote(input("下一页的[协方差矩阵](#p:p3)也是这样求的。\n\n```\n[代码](#p:p9)\n```")).markdown
    + "[我的链接](#p:p7)\n";

  it("marks the link text of page links in the block only (not in code, not below the block)", () => {
    const { marked, view } = editor(md);
    expect(marked()).toEqual(["协方差矩阵"]);
    expect(pageLinksIn(view.state).map((f) => f.id)).toEqual(["p3"]);
  });

  it("a click on the marked text opens the page; mousedown is swallowed so Obsidian does not follow the href", () => {
    const { view, open } = editor(md);
    const span = view.dom.querySelector<HTMLElement>(`.${PAGE_LINK_CLASS}`)!;
    expect(span.getAttribute("data-evowiki-page")).toBe("p3");
    const down = new MouseEvent("mousedown", { bubbles: true, cancelable: true });
    span.dispatchEvent(down);
    expect(down.defaultPrevented).toBe(true);
    span.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, button: 0 }));
    expect(open).toHaveBeenCalledWith("p3", null);                // the mock editor has no file
  });

  it("while the cursor is inside the link (its source shown), clicks edit as usual", () => {
    const { view, open } = editor(md);
    const at = view.state.doc.toString().indexOf("[协方差矩阵]") + 2;
    view.dispatch({ selection: EditorSelection.cursor(at) });
    const span = view.dom.querySelector<HTMLElement>(`.${PAGE_LINK_CLASS}`)!;
    // The events reach the editor's own handlers (CodeMirror places the cursor), not stopped on the way.
    const seen: string[] = [];
    for (const t of ["mousedown", "click"]) span.addEventListener(t, () => seen.push(t));
    span.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
    span.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, button: 0 }));
    expect(seen).toEqual(["mousedown", "click"]);
    expect(open).not.toHaveBeenCalled();
  });

  it("a note without the block gets no marks", () => {
    const { marked } = editor("# 随手记\n\n[x](#p:p3)\n");
    expect(marked()).toEqual([]);
  });
});

describe("WikiController.openPageLink", () => {
  it("an existing page opens its note", async () => {
    const s = setup();
    await firstPage(s.c);
    if (s.c.state.page.kind !== "open") throw new Error("no page");
    const id = s.c.state.page.page.page_id;
    s.opened.length = 0;
    await s.c.openPageLink(id);
    expect(s.opened.length).toBe(1);
    expect(s.notices).toEqual([]);
  });

  it("a page that does not exist fails closed with a notice", async () => {
    const s = setup();
    await firstPage(s.c);
    s.opened.length = 0;
    await s.c.openPageLink("p3");
    expect(s.opened).toEqual([]);
    expect(s.notices.length).toBe(1);
    expect(s.notices[0]).not.toBe("");
  });
});
