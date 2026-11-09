/**
 * app.js `showChange` after the tab switch: find `[data-chg="<key>"]`, scroll it to the middle, then the dashed ants frame.
 * The tab may still be drawing, so it looks on the next frame and for a few more frames before giving up (silently: the
 * row may be gone, e.g. already rolled back and no longer shown).
 */
import { antsFrame } from "../../ui/ants";

const MAX_FRAMES = 20;

export function revealChange(root: ParentNode, key: string, reduce: boolean, frame: (f: () => void) => void): void {
  let left = MAX_FRAMES;
  const look = (): void => {
    const row = [...root.querySelectorAll<HTMLElement>("[data-chg]")].find((x) => x.dataset.chg === key);
    if (row === undefined) { if (--left > 0) frame(look); return; }
    row.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "center" });
    antsFrame(row);
  };
  frame(look);
}

/** What main.ts gives `showChangeOnScreen`: the wiki view's leaf and root, and how to bring that leaf on screen. */
export interface RevealHost<L> {
  view: () => { leaf: L; root: ParentNode } | null;
  revealLeaf: (leaf: L) => unknown;
  reduce: () => boolean;
  frame: (f: () => void) => void;
}

/** 查看: bring the sidebar leaf on screen (it may be collapsed or behind another leaf), then scroll to the row and frame it. */
export async function showChangeOnScreen<L>(h: RevealHost<L>, key: string): Promise<void> {
  const v = h.view();
  if (v === null) return;
  try { await h.revealLeaf(v.leaf); } catch (e) { console.warn("EvoWiki: could not reveal the wiki sidebar", e); }
  revealChange(v.root, key, h.reduce(), h.frame);
}
