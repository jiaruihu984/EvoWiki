/**
 * When a page starts being written, Obsidian's file explorer opens the folders down to its file, so its row 「…生成中」
 * (tree-marker.ts) is in view at once (user request 2026-10-24). Once per write: the first streamed sync of a page; never
 * for a page only queued, never on every section.
 *
 * Only the explorer's own folder items are used (the file explorer view's `fileItems`, each folder's `setCollapsed`, its
 * tree's `infinityScroll.scrollIntoView`): the same steps as its `revealInFolder`, without the parts that move focus
 * (`setFocusedItem`, `setActiveLeaf`). No focus change, nothing opened, no sidebar tab switched: an explorer that is not on
 * screen (another tab in front, the sidebar collapsed, closed) is left as it is. The explorer is not an official API: a
 * shape this code does not know does nothing (logged once).
 */
import type { App } from "obsidian";

/** The write events that decide when to reveal: `started` on every streamed sync, `ended` when the write is over. */
export class RevealOnce {
  private readonly shown = new Set<string>();
  constructor(private readonly reveal: (path: string) => void) {}

  /** A streamed sync of `pageId` put its file at `path`: the first one of this write reveals it. */
  started(pageId: string, path: string): void {
    if (this.shown.has(pageId) || path === "") return;
    this.shown.add(pageId);
    try { this.reveal(path); } catch (e) { console.warn("EvoWiki: could not open the file explorer to the page", e); }
  }

  /** The write ended (written, failed): the next write of the page reveals again. */
  ended(pageId: string): void { this.shown.delete(pageId); }
}

interface FolderItem { collapsed?: boolean; setCollapsed?: (collapsed: boolean, animate?: boolean) => unknown }
interface ExplorerView {
  fileItems?: Record<string, FolderItem | undefined>;
  tree?: { infinityScroll?: { scrollIntoView?: (item: unknown, margin?: number) => void } };
}

let warned = false;

/** Opens the explorer's folders down to `path` and scrolls its row into view, when the explorer is on screen. */
export function revealInExplorer(app: App, path: string): void {
  const leaf = (app.workspace as App["workspace"] | undefined)?.getLeavesOfType("file-explorer")[0];
  if (!leaf || !leaf.view.containerEl.isShown()) return;
  const view = leaf.view as unknown as ExplorerView;
  const items = view.fileItems;
  if (items === undefined || typeof items !== "object") {
    if (!warned) { warned = true; console.warn("EvoWiki: the file explorer has no fileItems; its folders are left as they are"); }
    return;
  }
  const parts = path.split("/");
  for (let i = 1; i < parts.length; i++) {
    const folder = items[parts.slice(0, i).join("/")];
    if (folder?.collapsed === true) folder.setCollapsed?.(false);
  }
  const row = items[path];
  // after the folders opened (their rows exist then), like revealInFolder's own nextFrame
  if (row !== undefined) window.requestAnimationFrame(() => { view.tree?.infinityScroll?.scrollIntoView?.(row, 4); });
}
