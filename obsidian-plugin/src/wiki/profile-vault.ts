import { type App, TFile } from "obsidian";
import { backupProfile } from "./profile-backup";
import type { BackupStore } from "./profile-backup";
import type { ProfileVault } from "./profile-note";

/** The profile note 「我」 over the vault: create makes the folder first; replace writes only while the note is unchanged. */
export function obsidianProfileVault(app: App, backups: BackupStore): ProfileVault {
  const file = (path: string): TFile | null => {
    const f = app.vault.getAbstractFileByPath(path);
    return f instanceof TFile ? f : null;
  };
  return {
    read: async (path) => { const f = file(path); return f === null ? null : app.vault.read(f); },
    create: async (path, text) => {
      const dir = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
      if (dir !== "" && app.vault.getAbstractFileByPath(dir) === null) await app.vault.createFolder(dir);
      await app.vault.create(path, text);
    },
    replace: async (path, expected, text) => {
      const f = file(path);
      if (f === null) return false;
      let ok = false;
      await app.vault.process(f, (data) => { ok = data === expected; return ok ? text : data; });
      return ok;
    },
    backup: (path, text) => backupProfile(backups, path, text, new Date(), Math.random().toString(36).slice(2, 8)),
  };
}
