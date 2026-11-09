/**
 * A 参数 change card's jump (app.js goToChange): once the note is scrolled to the changed section, the section gets the
 * travelling-dashes frame (app.js antsFrame on the `section`). Obsidian draws the note, so the frame is an overlay box laid
 * over the section's lines inside the view's scroller (it scrolls with the text): from the section's first line to the line
 * before the next heading of the same or a higher level (or the end of the generated block). Editing view (source / Live
 * Preview): CodeMirror's line geometry. Reading view: the rendered blocks from the heading to the next such heading.
 * Nothing found → no frame (the scroll stays).
 */
import { antsFrame } from "../ui/ants";
import { FenceScanner } from "./note/fences";
import { WIKI_END } from "./note/format";

const HEADING = /^(#{1,6})\s/;

/** The 0-based last line of the section that starts at `start` (pure; trailing blank lines left out). */
export function sectionEnd(lines: readonly string[], start: number): number {
  const level = HEADING.exec(lines[start] ?? "")?.[1]?.length ?? 6;
  const scan = new FenceScanner();
  scan.step(lines[start] ?? "");
  let end = lines.length - 1;
  for (let i = start + 1; i < lines.length; i++) {
    const l = lines[i]!;
    if (scan.step(l) !== "text") continue;
    const h = HEADING.exec(l);
    if (l === WIKI_END || (h !== null && h[1]!.length <= level)) { end = i - 1; break; }
  }
  while (end > start && (lines[end] ?? "").trim() === "") end--;
  return end;
}

/** The bits of CodeMirror 6's EditorView the frame reads (Obsidian's `editor.cm`). */
interface Cm {
  state: { doc: { lines: number; line(n: number): { from: number } } };
  lineBlockAt(pos: number): { top: number; bottom: number };
  documentTop: number;
  scrollDOM: HTMLElement;
  contentDOM: HTMLElement;
}

/** The view as main.ts has it (a MarkdownView): its mode, the editor's CodeMirror, the reading view's element. */
export interface FrameView {
  getMode(): "source" | "preview";
  editor: unknown;
  previewMode: { containerEl: HTMLElement };
}

const FRAME_MS = 7000;

/** A box over `rect` (viewport coordinates) inside `scroller`, framed; gone after the frame fades. */
function overlay(scroller: HTMLElement, rect: { top: number; left: number; width: number; height: number }): void {
  document.querySelectorAll(".evowiki-sec-frame").forEach((x) => x.remove());
  if (getComputedStyle(scroller).position === "static") scroller.style.position = "relative";
  const sr = scroller.getBoundingClientRect();
  const box = document.createElement("div");
  box.className = "evowiki-sec-frame";
  box.setAttribute("aria-hidden", "true");
  box.style.top = `${rect.top - sr.top + scroller.scrollTop}px`;
  box.style.left = `${rect.left - sr.left + scroller.scrollLeft}px`;
  box.style.width = `${rect.width}px`;
  box.style.height = `${rect.height}px`;
  scroller.append(box);
  antsFrame(box);
  window.setTimeout(() => box.remove(), FRAME_MS + 1000);
}

function frameEditor(cm: Cm, from: number, to: number): boolean {
  const doc = cm.state.doc;
  if (from + 1 > doc.lines) return false;
  const a = cm.lineBlockAt(doc.line(from + 1).from), b = cm.lineBlockAt(doc.line(Math.min(to + 1, doc.lines)).from);
  const cr = cm.contentDOM.getBoundingClientRect();
  overlay(cm.scrollDOM, { top: cm.documentTop + a.top, left: cr.left, width: cr.width, height: b.bottom - a.top });
  return true;
}

function framePreview(root: HTMLElement, heading: string, level: number): boolean {
  const blocks = [...root.querySelectorAll<HTMLElement>(".markdown-preview-section > div")];
  const levelOf = (b: HTMLElement): number | null => {
    const h = b.querySelector("h1, h2, h3, h4, h5, h6");
    return h === null ? null : Number(h.tagName.slice(1));
  };
  // Obsidian's data-heading is the heading's own text (the textContent also has our ⋯ section-menu button)
  const nameOf = (b: HTMLElement): string => {
    const h = b.querySelector("h1, h2, h3, h4, h5, h6")!;
    return (h.getAttribute("data-heading") ?? h.textContent ?? "").trim();
  };
  const at = blocks.findIndex((b) => levelOf(b) === level && nameOf(b) === heading);
  if (at < 0) return false;
  let last = at;
  for (let i = at + 1; i < blocks.length; i++) {
    const l = levelOf(blocks[i]!);
    // the generated block ends with a rule (what follows it, e.g. 真题练习, is not the section's)
    if ((l !== null && l <= level) || blocks[i]!.querySelector(":scope > hr") !== null) break;
    last = i;
  }
  const r0 = blocks[at]!.getBoundingClientRect(), r1 = blocks[last]!.getBoundingClientRect();
  const scroller = root.querySelector<HTMLElement>(".markdown-preview-view") ?? root;
  overlay(scroller, { top: r0.top, left: r0.left, width: r0.width, height: r1.bottom - r0.top });
  return true;
}

/** Frames lines `from`..sectionEnd of `text` in `view`; false when the section could not be placed. */
export function frameSection(view: FrameView, text: string, from: number): boolean {
  const lines = text.split(/\r\n?|\n/);
  const to = sectionEnd(lines, from);
  if (view.getMode() === "source") {
    const cm = (view.editor as { cm?: Cm } | null)?.cm;
    return cm !== undefined && frameEditor(cm, from, to);
  }
  const h = HEADING.exec(lines[from] ?? "");
  if (h === null) return false;
  return framePreview(view.previewMode.containerEl, (lines[from] ?? "").slice(h[0].length).trim(), h[1]!.length);
}
