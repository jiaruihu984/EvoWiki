import { localizedText, tr } from "../../i18n";
/**
 * 「生成中」 in the file tree (Task 13, spec §5): while a page streams, its file's row in Obsidian's file explorer gets the
 * class `ew-writing` and a text span 「生成中」 (the demo's `.item.wr` + `span.ws`, 11px, text only). The file explorer is not
 * an official API: the rows are found by `.nav-file-title[data-path]` and the marks are put back when Obsidian redraws the
 * tree. A row that is not in the DOM (folder collapsed, explorer closed) just has no mark; the page's own header shows
 * 「生成中」 from the file's frontmatter then (note-page/header.ts). Nothing here writes to the vault.
 */
export const WRITING_CLASS = "ew-writing";
export const WRITING_MARK_CLASS = "ew-writing-mark";

export class TreeMarker {
  private readonly paths = new Map<string, string>();
  private obs: MutationObserver | null = null;
  private frame: number | null = null;

  constructor(private readonly doc: Document = document) {}

  /** Page `pageId`'s file (now at `path`) is being written. */
  set(pageId: string, path: string): void {
    if (this.paths.get(pageId) === path) return;
    this.paths.set(pageId, path);
    this.watch();
    this.apply();
  }

  /** The page is written (or the write ended): its mark goes. */
  clear(pageId: string): void {
    if (!this.paths.delete(pageId)) return;
    this.apply();
    if (this.paths.size === 0) this.unwatch();
  }

  /** Marks the plugin put on rows right now (for tests). */
  marked(): string[] {
    return [...this.doc.querySelectorAll<HTMLElement>(`.nav-file-title.${WRITING_CLASS}`)].map((e) => e.dataset.path ?? "");
  }

  dispose(): void {
    this.paths.clear();
    this.apply();
    this.unwatch();
  }

  /** Puts the mark on the rows of the paths and takes it off every other row. Idempotent. */
  apply(): void {
    const want = new Set(this.paths.values());
    for (const row of this.doc.querySelectorAll<HTMLElement>(".nav-file-title[data-path]")) {
      const on = want.has(row.dataset.path ?? "");
      const mark = row.querySelector(`:scope > .${WRITING_MARK_CLASS}`);
      if (on && mark === null) {
        row.classList.add(WRITING_CLASS);
        const s = this.doc.createElement("span");
        s.className = WRITING_MARK_CLASS;
        localizedText(s, () => tr("note.writing"));
        row.append(s);
      } else if (!on && mark !== null) {
        row.classList.remove(WRITING_CLASS);
        mark.remove();
      } else if (on) row.classList.add(WRITING_CLASS);
    }
  }

  // Obsidian rebuilds rows when the tree changes (a rename, a folder opening): look again, once per frame.
  private watch(): void {
    const win = this.doc.defaultView;
    if (this.obs !== null || win === null || typeof win.MutationObserver !== "function" || this.doc.body === null) return;
    this.obs = new win.MutationObserver(() => {
      if (this.frame !== null) return;
      this.frame = win.requestAnimationFrame(() => { this.frame = null; this.apply(); });
    });
    this.obs.observe(this.doc.body, { childList: true, subtree: true });
  }

  private unwatch(): void {
    this.obs?.disconnect();
    this.obs = null;
    if (this.frame !== null) { this.doc.defaultView?.cancelAnimationFrame(this.frame); this.frame = null; }
  }
}
