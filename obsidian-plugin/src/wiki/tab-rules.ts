/**
 * When the sidebar switches to 助手 by itself (app.js `openTab`: opening a page, or switching to its tab, shows 助手).
 * A Markdown note becoming active — another .md file opened, or another Markdown leaf's tab switched to — calls
 * `toAssistant`. Focus coming back to the same note (from the sidebar or any other view) is not a change. Nothing
 * happens before `ready()` (Obsidian's layout restore on start opens the last notes; start shows 今天), and `ready()`
 * takes the note active then as already seen. Pure: main.ts feeds it the workspace events.
 */
export class NoteActivation {
  private live = false;
  private leaf: unknown = null;
  private path: string | null = null;
  constructor(private readonly toAssistant: () => void) {}

  /** Start-up is over: the note active now (leaf and path, or nulls) is the one already shown. */
  ready(leaf: unknown, path: string | null): void {
    this.live = true;
    this.leaf = leaf;
    this.path = path;
  }

  /** `file-open`: a file opened in the active leaf (null: the last file closed). */
  fileOpened(path: string | null, extension: string | null): void {
    if (path === null || extension !== "md") { this.path = path; return; }
    this.changed(this.leaf, path);
  }

  /** `active-leaf-change` to a Markdown leaf (others are ignored: focus in the sidebar is no note change). */
  markdownLeafActive(leaf: unknown, path: string | null): void {
    if (path === null) return;
    this.changed(leaf, path);
  }

  private changed(leaf: unknown, path: string): void {
    const same = leaf === this.leaf && path === this.path;
    this.leaf = leaf;
    this.path = path;
    if (this.live && !same) this.toAssistant();
  }
}
