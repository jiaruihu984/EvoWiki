/**
 * Obsidian glue for the section heading parts and the 「⋯」 menu (logic in section-menu.ts): the Reading-view
 * post-processor, the Live Preview decoration (a CM6 widget at the end of each matching `##` line) and the demo's own
 * menu (app.js sectionMenu: `div.ew-menu` role=menu under the button). main.ts only registers these. Both views read the
 * note's own text (the post-processor's section info, the editor's document); nothing is written.
 */
import { RangeSetBuilder, StateEffect, type Extension } from "@codemirror/state";
import { Decoration, type DecorationSet, type EditorView, ViewPlugin, type ViewUpdate, WidgetType } from "@codemirror/view";
import { editorLivePreviewField, type MarkdownPostProcessorContext, MarkdownRenderChild } from "obsidian";
import { el } from "../ui/ew";
import { openMenuAt } from "../ui/floating";
import {
  createHeadingParts, type HeadingRef, type MenuEntry, menuKey, mountSectionMenu, noteHeadings, type OpenMenu, runSectionAction, sectionMenu,
  type SectionHub, type SectionSource,
} from "./section-menu";
import { belowBlock, endLineTo, hasStart, ownedNote } from "./note-doc";

/** The open menu (one at a time, as app.js closeMenu removes every `.menu`). */
let openOne: { layer: HTMLElement; anchor: HTMLElement; off: () => void } | null = null;

/** app.js closeMenu: removes the menu and sets its button back to aria-expanded=false. */
export function closeSectionMenu(): void {
  const o = openOne;
  if (o === null) return;
  openOne = null;
  o.off();
  o.layer.remove();
  o.anchor.setAttribute("aria-expanded", "false");
}

/**
 * The element the menu is positioned in (app.js `$("view")`): the leaf's `.view-content`, which does not scroll with the
 * note, else the nearest preview / editor, else the body.
 */
function viewOf(anchor: HTMLElement): HTMLElement {
  return anchor.closest<HTMLElement>(".view-content") ?? anchor.closest<HTMLElement>(".markdown-preview-view, .cm-editor") ?? anchor.ownerDocument.body;
}

/**
 * app.js sectionMenu: a second click on the open menu's button closes it; otherwise `div.ew-menu` role=menu with the
 * entries in order (标记 / hr / 讲法 + 问助手 / hr / 重新组织, 锁定), disabled ones with title 先解除锁定 when locked; placed
 * where app.js puts it (ui/floating.ts). The focus moves to its first item; arrow keys, Home, End move it; a click
 * outside or Esc closes it.
 */
export const openSectionMenu: OpenMenu = (button, from, ref) => {
  const wasOpen = button.getAttribute("aria-expanded") === "true";
  closeSectionMenu();
  if (wasOpen) return;
  const m = sectionMenu(from.state, ref);
  if (m === null) return;
  button.setAttribute("aria-expanded", "true");
  const layer = el("div");
  layer.className = "ew ew-inline evowiki-menu-layer";
  const menu = el("div", "menu");
  menu.setAttribute("role", "menu");
  const item = (e: MenuEntry): void => {
    const b = el("button", "", e.title);
    b.type = "button";
    b.disabled = e.disabled;
    b.setAttribute("role", "menuitem");
    b.onclick = (ev) => { ev.stopPropagation(); closeSectionMenu(); void runSectionAction(from, m.sid, e.action); };
    menu.append(b);
  };
  const group = (g: MenuEntry["group"]): MenuEntry[] => m.entries.filter((x) => x.group === g);
  for (const e of group("mark")) item(e);
  menu.append(el("hr"));
  for (const e of group("ask")) item(e);
  menu.append(el("hr"));
  for (const e of group("more")) item(e);
  layer.append(menu);
  const view = viewOf(button);
  view.append(layer);
  const close = (refocus: boolean): void => { closeSectionMenu(); if (refocus) button.focus(); };
  // app.js: left = the button's right − 190, 4px under it, kept inside the view; here it also flips above when there is
  // no room below and follows the button while the note scrolls (ui/floating.ts).
  const stop = openMenuAt(button, menu, close, true, { placement: "bottom-start", gap: 4, overhang: (w) => w - 190 });
  const doc = view.ownerDocument;
  const onClick = (ev: MouseEvent): void => { if (!menu.contains(ev.target as Node)) closeSectionMenu(); };
  const onKey = (ev: KeyboardEvent): void => { if (ev.key === "Escape") close(true); };
  doc.addEventListener("click", onClick);
  doc.addEventListener("keydown", onKey);
  openOne = { layer, anchor: button, off: () => { stop(); doc.removeEventListener("click", onClick); doc.removeEventListener("keydown", onKey); } };
};

/** Reading view: a `##` section of a wiki note gets its parts (tags, 「⋯」, 「正在写…」) in and after its heading. */
export function sectionMenuPostProcessor(hub: SectionHub, open: OpenMenu = openSectionMenu) {
  return (elm: HTMLElement, ctx: MarkdownPostProcessorContext): void => {
    const heading = elm.querySelector("h2");
    if (heading === null) return;
    const info = ctx.getSectionInfo(elm);
    const ref = info ? noteHeadings(info.text)?.byLine.get(info.lineStart) : undefined;
    if (ref === undefined) return;
    const child = new MarkdownRenderChild(elm);
    child.register(mountSectionMenu(heading, ref, hub, open));
    ctx.addChild(child);
  };
}

/** Sent when the controller changes, so the decorations are rebuilt. */
const redraw = StateEffect.define<null>();

const cleanups = new WeakMap<HTMLElement, () => void>();

/** Live Preview: the same parts at the end of the heading line (「正在写…」 inline, as a span, inside the line). */
class PartsWidget extends WidgetType {
  constructor(readonly ref: HeadingRef, private readonly hub: SectionHub, private readonly open: OpenMenu) { super(); }
  override eq(o: PartsWidget): boolean {
    return o.ref.pageId === this.ref.pageId && o.ref.heading === this.ref.heading && o.ref.ordinal === this.ref.ordinal && o.hub === this.hub;
  }
  override toDOM(): HTMLElement {
    const parts = createHeadingParts(this.ref, this.hub, this.open);
    const host = el("span");
    host.className = "evowiki-heading-host";
    host.append(parts.inline);
    cleanups.set(host, this.hub.subscribe(() => parts.update()));
    return host;
  }
  override destroy(dom: HTMLElement): void { cleanups.get(dom)?.(); cleanups.delete(dom); }
  /** The editor leaves the button's clicks alone (no cursor move into the heading). */
  override ignoreEvent(): boolean { return true; }
}

/**
 * The decorations, and where the block's END line ends (null when the note was not parsed: no controller, Source mode,
 * or not a single v2 wiki note).
 */
interface Built { decorations: DecorationSet; endTo: number | null }
const NOTHING: Built = { decorations: Decoration.none, endTo: null };

/** Live Preview only (not Source mode): a widget at the end of every `##` line that matches a section of the open page. */
function decorations(view: EditorView, hub: SectionHub, open: OpenMenu): Built {
  const from = hub.current;
  if (from === null || !view.state.field(editorLivePreviewField, false)) return NOTHING;
  const doc = view.state.doc;
  const nh = noteHeadings(doc.toString());
  if (nh === null) return NOTHING;
  const b = new RangeSetBuilder<Decoration>();
  for (const [n, ref] of [...nh.byLine].sort((x, y) => x[0] - y[0])) {
    if (n + 1 > doc.lines || sectionMenu(from.state, ref) === null) continue;
    const line = doc.line(n + 1);
    b.add(line.to, line.to, Decoration.widget({ widget: new PartsWidget(ref, hub, open), side: 1 }));
  }
  return { decorations: b.finish(), endTo: endLineTo(doc) };
}

/** The controller as the menus see it: which controller, and menuKey of its state. */
const hubKey = (hub: SectionHub): [SectionSource | null, string] => [hub.current, hub.current ? menuKey(hub.current.state) : ""];

export function sectionMenuExtension(hub: SectionHub, open: OpenMenu = openSectionMenu): Extension {
  return ViewPlugin.fromClass(class {
    decorations: DecorationSet;
    /** The document may be a wiki note (it has a START line); other notes are never parsed. */
    private wiki: boolean;
    /** Where the END line ends, from the last parse (null: none); typing below it does not re-read the note. */
    private endTo: number | null = null;
    private seen: [SectionSource | null, string];
    private readonly unsub: () => void;
    constructor(view: EditorView) {
      this.wiki = hasStart(view.state.doc);
      this.seen = hubKey(hub);
      this.decorations = Decoration.none;
      this.rebuild(view);
      // A controller change redraws only a wiki note's editor, and only when what decides the menus changed (not on
      // streamed chunks, busy or connection changes; the widgets redraw their own parts).
      this.unsub = hub.subscribe(() => {
        const k = hubKey(hub);
        if (k[0] === this.seen[0] && k[1] === this.seen[1]) return;
        this.seen = k;
        if (this.wiki) view.dispatch({ effects: redraw.of(null) });
      });
    }
    update(u: ViewUpdate): void {
      let changed = false;
      if (u.docChanged) {
        if (this.wiki && this.endTo !== null && belowBlock(u.changes, u.startState.doc, u.state.doc, this.endTo)) {
          // Typing in 我的笔记: the same menus, only moved with the text (nothing above the edit moves).
          this.decorations = this.decorations.map(u.changes);
        } else {
          if (this.wiki) this.wiki = hasStart(u.state.doc);
          else u.changes.iterChangedRanges((_fa, _ta, fb, tb) => { if (!this.wiki && hasStart(u.state.doc, fb, tb)) this.wiki = true; });
          changed = true;
        }
      }
      const mode = u.startState.field(editorLivePreviewField, false) !== u.state.field(editorLivePreviewField, false);
      if (changed || mode || u.transactions.some((t) => t.effects.some((e) => e.is(redraw)))) this.rebuild(u.view);
    }
    private rebuild(view: EditorView): void {
      const r = this.wiki && ownedNote(view.state, hub) ? decorations(view, hub, open) : NOTHING;
      this.decorations = r.decorations;
      this.endTo = r.endTo;
    }
    destroy(): void { this.unsub(); }
  }, { decorations: (v) => v.decorations });
}
