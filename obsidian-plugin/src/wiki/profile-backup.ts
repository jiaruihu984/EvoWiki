// The profile note's backups (no obsidian import: tested on its own).

/** The most recent profile backups kept. */
export const PROFILE_BACKUPS = 20;
const BACKUP_ROOT = ".evowiki/backups";
/** A backup folder as `backupProfile` makes it: <stamp>-<rand> directly under the backup root, nothing else. */
const BACKUP_DIR = /^\.evowiki\/backups\/\d{8}T\d{9}Z-[a-z0-9]+$/;

/**
 * What the backups use of the vault. Writing goes through the registered note-writing path (ObsidianNoteVault: occupied,
 * ensureFolder, create: the same calls a page note's backup makes, sync.ts). Removing is the only new power: one file, and
 * one folder only while it is empty (never recursive), so nothing but what is named here can go.
 */
export interface BackupStore {
  occupied(path: string): Promise<boolean>;
  ensureFolder(path: string): Promise<void>;
  create(path: string, text: string): Promise<void>;
  list(path: string): Promise<{ files: string[]; folders: string[] }>;
  removeFile(path: string): Promise<void>;
  removeEmptyDir(path: string): Promise<void>;
}

/** The file a profile note's backup is: `<note name>.profile.bak` (a page note's backup is `.md.bak`, never mistaken for it). */
const backupName = (path: string): string => `${path.replace(/^.*\//, "").replace(/\.md$/i, "")}.profile.bak`;

/**
 * The note's text kept as `.evowiki/backups/<stamp>-<rand>/<name>.profile.bak` (the page notes' backup folder and naming),
 * then the oldest of this note's backups beyond `keep` are removed: only a file of exactly that name inside a folder of
 * exactly that shape directly under the backup root, and the folder after it only if nothing else is in it. Throws when the
 * copy could not be written; a failed removal of an old one is only logged.
 */
export async function backupProfile(s: BackupStore, path: string, text: string, now: Date, rand: string, keep = PROFILE_BACKUPS): Promise<void> {
  const name = backupName(path);
  const dir = `${BACKUP_ROOT}/${now.toISOString().replace(/[-:.]/g, "")}-${rand}`;
  if (!BACKUP_DIR.test(dir)) throw new Error(`not a backup folder name: ${dir}`);
  if (await s.occupied(`${dir}/${name}`)) throw new Error(`backup exists: ${dir}`);
  await s.ensureFolder(dir);
  await s.create(`${dir}/${name}`, text);
  await pruneBackups(s, (file) => Promise.resolve(file.endsWith(`/${name}`)), keep);
}

/** Only direct files in our timestamp folders; removal of folders is always non-recursive. */
export async function pruneBackups(s: BackupStore, belongs: (file: string) => Promise<boolean>, keep = 20): Promise<void> {
  try {
    const mine: string[] = [];
    for (const dir of (await s.list(BACKUP_ROOT)).folders) {
      if (!BACKUP_DIR.test(dir)) continue;
      for (const file of (await s.list(dir)).files) {
        if (!file.startsWith(`${dir}/`) || file.slice(dir.length + 1).includes("/")) continue;
        if (await belongs(file)) mine.push(file);
      }
    }
    mine.sort();
    for (const file of mine.slice(0, Math.max(0, mine.length - keep))) {
      await s.removeFile(file);
      const dir = file.slice(0, file.lastIndexOf("/"));
      const left = await s.list(dir);
      if (left.files.length === 0 && left.folders.length === 0) await s.removeEmptyDir(dir);
    }
  } catch (e) {
    console.warn("EvoWiki: could not remove old backups", e);
  }
}
