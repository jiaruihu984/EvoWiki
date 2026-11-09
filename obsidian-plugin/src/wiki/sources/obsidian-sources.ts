/**
 * The real SourcePort (ports.ts) over Obsidian: the originals written once under
 * `<notes folder>/资料/` (through ObsidianNoteVault: create never overwrites), and opening a file. The only vault write
 * is NoteVault's createBinary (+ ensureFolder) into that folder; nothing is deleted, moved or modified.
 */
import { type App, TFile } from "obsidian";
import { ObsidianNoteVault } from "../../notes/note-service";
import type { SourcePort } from "./ports";
import { freeOriginalPath, writeOriginal } from "./save-original";

/** Obsidian shows these in a tab of its own; any other kind goes to the system's app. */
const OBSIDIAN_VIEWS = new Set(["md", "pdf", "png", "jpg", "jpeg", "webp", "gif"]);

export function obsidianSourcePort(app: App, notesFolder: () => string): SourcePort {
  const vault = new ObsidianNoteVault(app.vault);
  const fileAt = (path: string): TFile | null => {
    const f = app.vault.getAbstractFileByPath(path);
    return f instanceof TFile ? f : null;
  };
  return {
    originalPath: (name) => freeOriginalPath(vault, notesFolder(), name),
    writeOriginal: (path, data) => writeOriginal(vault, path, data),
    openFile: async (path, page) => {
      const f = fileAt(path);
      if (f === null) return false;
      try {
        if (page !== undefined && f.extension.toLowerCase() === "pdf") { await app.workspace.openLinkText(`${path}#page=${page}`, "", false); return true; }
        if (OBSIDIAN_VIEWS.has(f.extension.toLowerCase())) { await app.workspace.getLeaf(false).openFile(f); return true; }
        const a = app as unknown as { openWithDefaultApp?(p: string): Promise<void> | void };
        if (typeof a.openWithDefaultApp !== "function") return false;
        await a.openWithDefaultApp(path);
        return true;
      } catch { return false; }
    },
    exists: (path) => fileAt(path) !== null,
  };
}
