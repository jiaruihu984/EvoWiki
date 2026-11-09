/**
 * Where things sit in an open wiki note's editor document (Live Preview widgets): the END line of the generated block,
 * whether a line has a marker, and whether an edit can leave the parsed note unchanged. Shared by the 「⋯」 menus
 * (section-menu-view.ts) and the end-of-note block (end-block/end-block-view.ts); read-only, nothing is written.
 */
import type { ChangeSet, EditorState, Text } from "@codemirror/state";
import { editorInfoField } from "obsidian";
import { isMineHeading, WIKI_END, WIKI_START } from "./note/format";

/** Whether the note an editor shows (Obsidian's editorInfoField) is the current user's, as the hub says. */
export const ownedNote = (state: EditorState, hub: { owns?(notePath: string | null): boolean }): boolean =>
  hub.owns?.(state.field(editorInfoField, false)?.file?.path ?? null) ?? true;

/** The end of the END line that follows the START line (the note parsed as one block, so there is exactly one). */
export function endLineTo(doc: Text): number | null {
  let started = false;
  for (let n = 1; n <= doc.lines; n++) {
    const line = doc.line(n);
    if (!started) started = line.text === WIKI_START;
    else if (line.text === WIKI_END) return line.to;
  }
  return null;
}

/** The end of the START line (the top of the generated block), or null when there is none. */
export function startLineTo(doc: Text): number | null {
  for (let n = 1; n <= doc.lines; n++) if (doc.line(n).text === WIKI_START) return doc.line(n).to;
  return null;
}

/** Whether the line `## 我的笔记` follows the END line that ends at `endTo` (the end block's anchor; absent = fail closed). */
export function hasMineAfter(doc: Text, endTo: number): boolean {
  for (let n = doc.lineAt(endTo).number + 1; n <= doc.lines; n++) if (isMineHeading(doc.line(n).text)) return true;
  return false;
}

/** Whether some line of `doc` between `from` and `to` contains `%%` (a marker line, or one being typed). */
export function touchesMarker(doc: Text, from: number, to: number): boolean {
  for (let n = doc.lineAt(from).number, last = doc.lineAt(to).number; n <= last; n++) if (doc.line(n).text.includes("%%")) return true;
  return false;
}

/**
 * An edit that cannot change what is read from the note: every change starts after `minFrom` (default: the END line;
 * the end block passes the end of the `## 我的笔记` line, its anchor) and no line it touches, before or after, has `%%`.
 * The headings, the page id and the markers are all above that point, so the parse would come out the same.
 */
export function belowBlock(changes: ChangeSet, before: Text, after: Text, endTo: number, minFrom = endTo): boolean {
  let quiet = true;
  changes.iterChangedRanges((fa, ta, fb, tb) => {
    if (!quiet) return;
    quiet = fa > minFrom && !touchesMarker(before, fa, ta) && !touchesMarker(after, fb, tb);
  });
  return quiet;
}

/** Whether the document has a START line, read line by line (no copy of the whole text). */
export function hasStart(doc: Text, from = 0, to = doc.length): boolean {
  for (let n = doc.lineAt(from).number, last = doc.lineAt(to).number; n <= last; n++) if (doc.line(n).text === WIKI_START) return true;
  return false;
}
