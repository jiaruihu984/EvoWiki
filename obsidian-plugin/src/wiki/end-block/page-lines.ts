/**
 * The one line at the top of a wiki page (app.js renderNote's `.topline`: feedback being handled / the page being
 * organized (a spinner and 「正在按反馈更新本页…」) / 本页已更新：… · 撤销 / 本页没有修改 (?) · 重试 / 本页已按《X》更新 N 节 · 撤销 / 这一页由备用模型写成). Drawn by the plugin in Reading view and Live Preview, never written
 * to the note file. Built once; `update()` redraws it from the controller's state. The section tags (已更新 /
 * 已锁定) are drawn with the heading's other parts (section-menu.ts createHeadingParts).
 */
import { el } from "../../ui/ew";
import { WIKI_COPY } from "../copy";
import { REVISE_COPY } from "../revise/copy";
import type { EndHub } from "./end-block";
import { pageReuse, topLine } from "./model";

export interface Drawn { el: HTMLElement; update(): void }

/** `.ew-lk` text button (app.js `el("button", "lk", …)`). */
function link(text: string, run: () => void, disabled = false): HTMLButtonElement {
  const b = el("button", "lk", text);
  b.type = "button";
  b.disabled = disabled;
  b.onclick = run;
  return b;
}

/**
 * The top line for `pageId`: `div.ew.ew-inline` holding `div.ew-topline` (role=status) while there is something to
 * say, nothing otherwise (app.js draws no line then).
 */
export function createTopLine(pageId: string, hub: EndHub): Drawn {
  const host = el("div");
  host.className = "ew ew-inline evowiki-topline-host";
  host.dataset.page = pageId;
  const line = el("div", "topline");
  line.setAttribute("role", "status");
  line.setAttribute("aria-live", "polite");
  const noteBox = el("div", "goalnote");
  /** A page another goal reuses: where it was written and what that goal adds, under the line; never in the note's text. */
  function drawNotes(): void {
    const src = hub.current;
    const r = src === null ? null : pageReuse(src.state, pageId);
    if (r === null) { noteBox.remove(); return; }
    noteBox.replaceChildren(el("div", "goalnote-from", () => (WIKI_COPY.pageWrittenFor(r.writtenFor))), ...r.notes.map((n) => {
      const it = el("div", "goalnote-item");
      it.append(el("b", "", () => (WIKI_COPY.pageUsedFor(n.goal))), n.note);
      return it;
    }));
    host.append(noteBox);
  }
  function update(): void {
    const src = hub.current;
    const t = src === null ? null : topLine(src.state, pageId);
    if (t === null) { line.remove(); drawNotes(); return; }
    line.dataset.kind = t.kind;
    line.replaceChildren();
    if (t.kind === "collapsed") {
      line.append(t.text, " · ", link(WIKI_COPY.expandPage, () => { void src?.expand(); }, t.busy));
    } else if (t.kind === "revised") {
      // the same 撤销 link as the feedback line (B11); the ledger's undo, then the note follows the page
      line.append(t.text, " · ", link(REVISE_COPY.undo, () => { void src?.reviseDecide(t.actionId, "undo"); }));
    } else if (t.kind === "hand-failed") {
      line.append(t.text, " ");
      if (t.conflict) line.append(link(WIKI_COPY.handEditKeep, () => { void src?.keepMine(pageId); }), " · ");
      line.append(link(WIKI_COPY.handEditRestore, () => { void src?.restoreNote(pageId, t.note); }));
    } else {
      const failed = t.undo.kind === "failed" ? t.undo : null;
      line.append(failed !== null && failed.text !== "" ? failed.text : t.text);
      if (t.canUndo && !(failed !== null && !failed.retry)) {
        const undoing = t.undo.kind === "undoing";
        line.append(" · ", link(undoing ? WIKI_COPY.topUndoing : WIKI_COPY.topUndo, () => { void (t.kind === "hand-edited" ? src?.undoHandEdit(pageId) : src?.undoPageFeedback(pageId)); }, undoing));
      }
    }
    if (line.parentElement !== host) host.prepend(line);
    drawNotes();
  }
  update();
  return { el: host, update };
}

/** Puts `drawn` into `parent` (`before`: in front of that child), redraws on hub changes. Returns the cleanup. */
export function mountDrawn(parent: HTMLElement, drawn: Drawn, hub: EndHub, before: Node | null = null): () => void {
  parent.insertBefore(drawn.el, before);
  const unsub = hub.subscribe(() => drawn.update());
  return () => { unsub(); drawn.el.remove(); };
}
