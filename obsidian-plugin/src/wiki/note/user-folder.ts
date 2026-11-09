/**
 * Each user's notes live in their own folder, <notes folder>/<user id>: Wiki (the pages and their assets), 资料 and 我.md.
 * Another user's folder is never read or written; changing the user only changes which folder is used.
 *
 * The one move here (main.ts, on load): the layout from before per-user folders — exactly <notes folder>/Wiki, /资料 and
 * /我.md, nothing else — goes into the current user's folder, each only where that user has nothing of that name yet
 * (nothing is overwritten); renames go through Obsidian's fileManager, so [[links]] to the files follow.
 */
import type { App } from "obsidian";
import { ObsidianNoteVault } from "../../notes/note-service";
import { SOURCES_COPY } from "../sources/sources-copy";

export const userFolder = (s: { notesFolder: string; userId: string }): string => `${s.notesFolder}/${s.userId}`;

/** The vault side of the move: a rename keeps the [[links]] to the files. */
export interface MoveVault {
  exists(path: string): boolean;
  ensureFolder(path: string): Promise<void>;
  rename(from: string, to: string): Promise<void>;
}

/**
 * The layout from before per-user folders (Wiki, 资料, 我.md right under the notes folder) moves into the current user's
 * folder, each where that user has nothing yet. Returns what could not be moved (a clash or a failed rename).
 */
export async function moveIntoUserFolder(v: MoveVault, s: { notesFolder: string; userId: string }): Promise<string[]> {
  const to = userFolder(s);
  const left: string[] = [];
  for (const name of ["Wiki", SOURCES_COPY.folder, "我.md"]) {
    const from = `${s.notesFolder}/${name}`;
    if (!v.exists(from) || name.toLowerCase() === s.userId.toLowerCase()) continue;
    if (v.exists(`${to}/${name}`)) { left.push(from); continue; }
    try {
      await v.ensureFolder(to);
      await v.rename(from, `${to}/${name}`);
    } catch (e) {
      console.warn(`EvoWiki: could not move ${from} into ${to}`, e);
      left.push(from);
    }
  }
  return left;
}

/** MoveVault on the open vault: existence by path, folders through ObsidianNoteVault, renames through fileManager. */
export function obsidianMoveVault(app: App): MoveVault {
  const notes = new ObsidianNoteVault(app.vault);
  return {
    exists: (p) => app.vault.getAbstractFileByPath(p) !== null,
    ensureFolder: (p) => notes.ensureFolder(p),
    rename: async (from, to) => {
      const f = app.vault.getAbstractFileByPath(from);
      if (f === null) throw new Error(`Not found: ${from}`);
      await app.fileManager.renameFile(f, to);
    },
  };
}
