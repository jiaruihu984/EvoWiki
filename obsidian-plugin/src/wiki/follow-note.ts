/**
 * The wiki follows the active EvoWiki note: when the user switches to a wiki note, the real controller loads that note's
 * page through followPage (the same GET and note update as a click in the catalog, without opening a note). main.ts
 * only wires the workspace events and reads the note's frontmatter; the decision is here.
 */
import type { WikiState } from "./state";

/** The active note as main.ts sees it: its path and its frontmatter `evowiki_page_id` (null when it has none). */
export interface ActiveNote { path: string; pageId: string | null }

/** Whether `path` lies in `folder` or below (case-insensitive, like the wiki note lookup). */
export function inFolder(path: string, folder: string): boolean {
  const pre = folder.replace(/\/+$/, "").toLowerCase() + "/";
  return pre !== "/" && path.toLowerCase().startsWith(pre);
}

/** A note in the wiki notes folder with a page id: the only notes the wiki follows. */
export function isWikiNote(note: ActiveNote | null, folder: string): note is ActiveNote & { pageId: string } {
  return note !== null && note.pageId !== null && note.pageId !== "" && inFolder(note.path, folder);
}

/**
 * The page the controller should open for the active note, or null to leave it alone: no note or no page id, a note
 * outside the wiki notes folder, the demo, a write in flight (followPage would ignore it anyway), or the controller
 * already showing, loading or failing on that very page. Another page loading is no reason to wait: the latest note
 * wins, and the controller drops the older GET when it returns (pageToken).
 */
export function pageToFollow(note: ActiveNote | null, folder: string, s: WikiState): string | null {
  if (!isWikiNote(note, folder)) return null;
  if (s.demo || s.busy !== null) return null;
  const p = s.page;
  const shown = p.kind === "none" ? null : p.kind === "open" ? p.page.page_id : p.pageId;
  return shown === note.pageId ? null : note.pageId;
}
