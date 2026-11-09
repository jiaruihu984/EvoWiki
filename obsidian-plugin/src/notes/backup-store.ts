import { promises } from "node:fs";
import type { App, FileSystemAdapter } from "obsidian";
import type { BackupStore } from "../wiki/profile-backup";
import { ObsidianNoteVault } from "./note-service";

/**
 * The vault side of the profile note's backups (wiki/profile-backup.ts decides what is kept and what goes). Writing is
 * ObsidianNoteVault's, the registered path a page note's backup takes. The only removal in the plugin besides Obsidian's
 * trash is here: one file, and a folder only while it is empty (never recursive).
 */
export function obsidianBackupStore(app: App): BackupStore {
  const notes = new ObsidianNoteVault(app.vault);
  const a = app.vault.adapter;
  return {
    occupied: (p) => notes.occupied(p),
    ensureFolder: (p) => notes.ensureFolder(p),
    create: (p, t) => notes.create(p, t),
    list: (p) => a.list(p),
    removeFile: (p) => a.remove(p),
    // node's rmdir, which refuses a folder with anything in it: Obsidian's own rmdir with false is fs.rm without
    // recursive and fails with EISDIR on every real folder, so emptied backup folders piled up (walk 2026-11-05)
    removeEmptyDir: (p) => promises.rmdir((a as FileSystemAdapter).getFullPath(p)),
  };
}
