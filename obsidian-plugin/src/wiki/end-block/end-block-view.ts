/**
 * Obsidian glue for the plugin-drawn parts of a wiki note (nothing here writes to the note): the 「读完了」 block before
 * `## 我的笔记` (with the 真题练习 block above it, exam/exam-block.ts, when an exam port is given) and the line at the top of the page (the section tags are drawn with the heading's ⋯, section-menu-view.ts).
 * Reading view: one post-processor. Live Preview: one CM6 state field of block widgets (block widgets cannot come from a
 * view plugin). Source mode
 * shows none of it. A note without the `## 我的笔记` anchor gets no end block (fail closed: no guessing where it goes).
 */
import { type EditorState, type Extension, type Range, StateField } from "@codemirror/state";
import { Decoration, type DecorationSet, EditorView, WidgetType } from "@codemirror/view";
import { editorLivePreviewField, type MarkdownPostProcessorContext, MarkdownRenderChild } from "obsidian";
import { h } from "../../ui/dom";
import { isMineHeading, WIKI_END, WIKI_START } from "../note/format";
import { belowBlock, endLineTo, hasMineAfter, hasStart, ownedNote, startLineTo } from "../note-doc";
import { noteHeadings } from "../section-menu";
import { type ExamPort, mountExamBlock } from "../exam/exam-block";
import { type EndHub, mountEndBlock } from "./end-block";
import { createTopLine, mountDrawn } from "./page-lines";

/** Where the parts sit in a note's text (0-based line numbers), or null for any file that is not one v2 wiki note. */
interface Layout { pageId: string; mine: number | null; first: number | null }
const layouts = new Map<string, Layout | null>();
const LAYOUT_CACHE = 4;

function layoutOf(text: string): Layout | null {
  if (layouts.has(text)) return layouts.get(text)!;
  const nh = noteHeadings(text);
  let out: Layout | null = null;
  if (nh !== null) {
    const lines = text.split(/\r\n?|\n/);
    const start = lines.indexOf(WIKI_START);
    const end = lines.indexOf(WIKI_END, start + 1);
    if (start >= 0 && end > start) {
      const mine = lines.findIndex((line, index) => index > end && isMineHeading(line));
      let first: number | null = null;
      for (let i = start + 1; i < end; i++) if (lines[i]!.trim() !== "") { first = i; break; }
      out = { pageId: nh.pageId, mine: mine < 0 ? null : mine, first };
    }
  }
  if (layouts.size >= LAYOUT_CACHE) layouts.delete(layouts.keys().next().value!);
  layouts.set(text, out);
  return out;
}

/** The bottom of the page: 真题练习 (when `exam` is given), then the end block, both before `before`. Returns the cleanup. */
function mountBottom(parent: HTMLElement, pageId: string, hub: EndHub, exam: ExamPort | null, before: Node | null = null): () => void {
  const offExam = exam === null ? null : mountExamBlock(parent, pageId, exam, before);
  const offEnd = mountEndBlock(parent, pageId, hub, before);
  return () => { offEnd(); offExam?.(); };
}

/** Reading view: the end block before the 我的笔记 heading, the top line before the first content. */
export function endBlockPostProcessor(hub: EndHub, exam: ExamPort | null = null) {
  return (el: HTMLElement, ctx: MarkdownPostProcessorContext): void => {
    const info = ctx.getSectionInfo(el);
    if (info === null || !info.text.includes(WIKI_START)) return;
    const layout = layoutOf(info.text);
    if (layout === null) return;
    const child = new MarkdownRenderChild(el);
    const heading = el.querySelector("h2");
    if (heading !== null && layout.mine === info.lineStart) child.register(mountBottom(el, layout.pageId, hub, exam, heading));
    if (layout.first !== null && info.lineStart <= layout.first && layout.first <= info.lineEnd) {
      child.register(mountDrawn(el, createTopLine(layout.pageId, hub), hub, el.firstChild));
    }
    ctx.addChild(child);
  };
}

const unmounts = new WeakMap<HTMLElement, () => void>();

/** One widget kind: a host element with `mount` filling it (and its cleanup run when CodeMirror drops the widget). */
class PartWidget extends WidgetType {
  constructor(private readonly key: string, private readonly hub: EndHub, private readonly tag: "div" | "span", private readonly cls: string,
    private readonly mount: (host: HTMLElement, hub: EndHub) => () => void) { super(); }
  override eq(o: PartWidget): boolean { return o.key === this.key && o.hub === this.hub; }
  override toDOM(): HTMLElement {
    const host = h(this.tag, { class: this.cls });
    unmounts.set(host, this.mount(host, this.hub));
    return host;
  }
  override destroy(dom: HTMLElement): void { unmounts.get(dom)?.(); unmounts.delete(dom); }
  override ignoreEvent(): boolean { return true; }
}

const endWidget = (pageId: string, hub: EndHub, exam: ExamPort | null) =>
  new PartWidget(`end:${pageId}`, hub, "div", "evowiki-end-widget", (host, hb) => mountBottom(host, pageId, hb, exam));
const topWidget = (pageId: string, hub: EndHub) =>
  new PartWidget(`top:${pageId}`, hub, "div", "evowiki-topline-widget", (host, hb) => mountDrawn(host, createTopLine(pageId, hb), hb));

/** The decorations, the end of the `## 我的笔记` line they were built for (null: none) and whether the note is a wiki note. */
interface Built { decorations: DecorationSet; mineTo: number | null; wiki: boolean }
const NOTHING: Built = { decorations: Decoration.none, mineTo: null, wiki: false };

function build(state: EditorState, hub: EndHub, exam: ExamPort | null): Built {
  const doc = state.doc;
  if (!ownedNote(state, hub)) return NOTHING;
  if (!state.field(editorLivePreviewField, false) || !hasStart(doc)) return { ...NOTHING, wiki: hasStart(doc) };
  const nh = noteHeadings(doc.toString());
  const endTo = endLineTo(doc);
  const startTo = startLineTo(doc);
  if (nh === null || endTo === null || startTo === null) return { ...NOTHING, wiki: true };
  const ranges: Range<Decoration>[] = [];
  ranges.push(Decoration.widget({ widget: topWidget(nh.pageId, hub), block: true, side: 1 }).range(startTo));
  let mineTo: number | null = null;
  if (hasMineAfter(doc, endTo)) {
    ranges.push(Decoration.widget({ widget: endWidget(nh.pageId, hub, exam), block: true, side: 1 }).range(endTo));
    for (let n = doc.lineAt(endTo).number + 1; n <= doc.lines; n++) if (isMineHeading(doc.line(n).text)) { mineTo = doc.line(n).to; break; }
  }
  return { decorations: Decoration.set(ranges, true), mineTo, wiki: true };
}

/** Live Preview (not Source mode): the widgets of a wiki note. Typing below the 我的笔记 heading never re-reads the note. */
export function endBlockExtension(hub: EndHub, exam: ExamPort | null = null): Extension {
  const field = StateField.define<Built>({
    create: (state) => build(state, hub, exam),
    update(v, tr) {
      const mode = tr.startState.field(editorLivePreviewField, false) !== tr.state.field(editorLivePreviewField, false);
      if (mode) return build(tr.state, hub, exam);
      if (!tr.docChanged) return v;
      if (v.mineTo !== null && belowBlock(tr.changes, tr.startState.doc, tr.state.doc, v.mineTo, v.mineTo)) {
        return { ...v, decorations: v.decorations.map(tr.changes), mineTo: tr.changes.mapPos(v.mineTo) };
      }
      if (!v.wiki) {
        let found = false;
        tr.changes.iterChangedRanges((_fa, _ta, fb, tb) => { if (!found && hasStart(tr.state.doc, fb, tb)) found = true; });
        if (!found) return v;
      }
      return build(tr.state, hub, exam);
    },
    provide: (f) => EditorView.decorations.from(f, (v) => v.decorations),
  });
  return field;
}
