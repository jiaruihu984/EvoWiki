/**
 * Page links in a wiki note = app.js renderNote's `a.inl[data-open]`: the backend links one page to another as
 * `[text](#p:<page_id>)` in section text (note/format.ts PAGE_LINK keeps exactly that shape a Markdown link). Obsidian
 * would follow the href (a `#p:` href has a colon, so Obsidian takes it for an external link); here a click opens that
 * page through the wiki controller instead (openPageLink: the page note, or a notice saying why not). Never a heading
 * jump. Reading view: a post-processor; Live Preview: a view plugin that marks the link text and catches its clicks.
 * main.ts only registers these. Nothing is written.
 */
import { type EditorState, type Extension, RangeSetBuilder } from "@codemirror/state";
import { Decoration, type DecorationSet, type EditorView, ViewPlugin, type ViewUpdate } from "@codemirror/view";
import { editorInfoField, editorLivePreviewField, type MarkdownPostProcessorContext } from "obsidian";
import { pageLinksInBody, WIKI_END, WIKI_START } from "./note/format";

/** `notePath`: the note the link is in (null when not known). */
export type OpenPageLink = (pageId: string, notePath: string | null) => void;
/** The class on a page link (the Reading-view `a`, the Live Preview mark); styles.css gives it the `a.inl` look. */
export const PAGE_LINK_CLASS = "evowiki-page-link";
const ID_ATTR = "data-evowiki-page";
const HREF = /^#p:([A-Za-z0-9_-]+)$/;

/** The page id of a `#p:<id>` href, else null. */
export function pageLinkId(href: string | null): string | null {
  return href === null ? null : (HREF.exec(href)?.[1] ?? null);
}

const isWikiNote = (el: HTMLElement, ctx: MarkdownPostProcessorContext): boolean => {
  const fm = ctx.frontmatter as Record<string, unknown> | null | undefined;
  if (typeof fm?.evowiki_page_id === "string") return true;
  return ctx.getSectionInfo(el)?.text.includes(WIKI_START) ?? false;
};

/**
 * Reading view: every `a` whose href is `#p:<id>` in a wiki note gets the class and loses Obsidian's external-link
 * attributes (new-tab target, the href tooltip); a capturing listener on the block runs before Obsidian's own link
 * handlers, so the click opens the page and nothing else (a middle click does nothing).
 */
export function noteLinksPostProcessor(open: OpenPageLink) {
  return (el: HTMLElement, ctx: MarkdownPostProcessorContext): void => {
    const links = Array.from(el.querySelectorAll<HTMLAnchorElement>("a")).filter((a) => pageLinkId(a.getAttribute("href")) !== null);
    if (links.length === 0 || !isWikiNote(el, ctx)) return;
    for (const a of links) {
      a.classList.add(PAGE_LINK_CLASS);
      for (const x of ["target", "rel", "aria-label", "data-tooltip-position"]) a.removeAttribute(x);
    }
    const target = (e: Event): { a: HTMLAnchorElement; id: string } | null => {
      const a = (e.target as Element | null)?.closest?.("a") ?? null;
      if (a === null || !el.contains(a)) return null;
      const id = pageLinkId(a.getAttribute("href"));
      return id === null ? null : { a, id };
    };
    el.addEventListener("click", (e) => {
      const t = target(e);
      if (t === null) return;
      e.preventDefault();
      e.stopPropagation();
      open(t.id, ctx.sourcePath);
    }, true);
    el.addEventListener("auxclick", (e) => { if (target(e) !== null) { e.preventDefault(); e.stopPropagation(); } }, true);
  };
}

/** A page link in the generated block: where its text is (the visible part in Live Preview) and the whole `[…](…)`. */
interface Found { from: number; to: number; textFrom: number; textTo: number; id: string }

/**
 * The page links between the START and END lines, in document order (none when the note has no block): the same links
 * the writer kept (note/format.ts pageLinksInBody: nothing inside code or math). The text is read with LF line breaks,
 * as CodeMirror's document is.
 */
export function pageLinksIn(state: EditorState): Found[] {
  const doc = state.doc;
  let start: number | null = null;
  for (let n = 1; n <= doc.lines; n++) {
    const line = doc.line(n);
    if (start === null) { if (line.text === WIKI_START) start = line.to + 1; continue; }
    if (line.text === WIKI_END) {
      if (start > line.from) return [];
      return pageLinksInBody(doc.sliceString(start, line.from)).map((f) =>
        ({ from: start! + f.from, to: start! + f.to, textFrom: start! + f.textFrom, textTo: start! + f.textTo, id: f.id }));
    }
  }
  return [];
}

function marks(state: EditorState): DecorationSet {
  if (!state.field(editorLivePreviewField, false)) return Decoration.none;
  const b = new RangeSetBuilder<Decoration>();
  for (const f of pageLinksIn(state)) {
    b.add(f.textFrom, f.textTo, Decoration.mark({ class: PAGE_LINK_CLASS, attributes: { [ID_ATTR]: f.id } }));
  }
  return b.finish();
}

/**
 * Live Preview: the link text carries the class and its page id; a capturing mousedown on it is caught (so neither
 * CodeMirror nor Obsidian's link handling sees it) and the click that follows opens the page. While the cursor is inside
 * the link (its source shown), the mousedown and its click edit as usual.
 */
export function noteLinksExtension(open: OpenPageLink): Extension {
  return ViewPlugin.fromClass(class {
    decorations: DecorationSet;
    private readonly view: EditorView;
    /** The page link the last mousedown was caught on: its click opens it (a click after an editing mousedown does not). */
    private armed: string | null = null;
    private readonly onMouse = (e: MouseEvent): void => {
      if (e.type === "mousedown") this.armed = this.hit(e);
      const id = this.armed;
      if (id === null || this.idAt(e) !== id) return;
      e.preventDefault();
      e.stopPropagation();
      if (e.type === "mousedown") return;
      this.armed = null;
      if (e.type === "click" && e.button === 0) open(id, this.view.state.field(editorInfoField, false)?.file?.path ?? null);
    };
    constructor(view: EditorView) {
      this.view = view;
      this.decorations = marks(view.state);
      for (const t of ["mousedown", "click", "auxclick"] as const) view.dom.addEventListener(t, this.onMouse, true);
    }
    update(u: ViewUpdate): void {
      const mode = u.startState.field(editorLivePreviewField, false) !== u.state.field(editorLivePreviewField, false);
      if (u.docChanged || mode) this.decorations = marks(u.state);
    }
    /** The page id under the pointer, or null (not a page link, not Live Preview, or the cursor is editing it). */
    private span(e: MouseEvent): Element | null {
      const span = (e.target as Element | null)?.closest?.(`.${PAGE_LINK_CLASS}`) ?? null;
      return span !== null && this.view.dom.contains(span) ? span : null;
    }
    private idAt(e: MouseEvent): string | null { return this.span(e)?.getAttribute(ID_ATTR) ?? null; }
    private hit(e: MouseEvent): string | null {
      const span = this.span(e);
      if (span === null) return null;
      const state = this.view.state;
      if (!state.field(editorLivePreviewField, false)) return null;
      const id = span.getAttribute(ID_ATTR);
      if (id === null) return null;
      let pos: number;
      try { pos = this.view.posAtDOM(span); } catch { return null; }
      const f = pageLinksIn(state).find((x) => x.id === id && pos >= x.from && pos <= x.to);
      if (f === undefined) return null;
      const head = state.selection.main.head;
      return head >= f.from && head <= f.to ? null : id;
    }
    destroy(): void {
      for (const t of ["mousedown", "click", "auxclick"] as const) this.view.dom.removeEventListener(t, this.onMouse, true);
    }
  }, { decorations: (v) => v.decorations });
}
