import { tr } from "../../i18n";
import { randomBytes } from "node:crypto";
import type { App, TAbstractFile } from "obsidian";
import { TFile } from "obsidian";
import { validateNotesFolder } from "../../notes/note-format";
import { type NoteVault, ObsidianNoteVault } from "../../notes/note-service";
import { ewNotice } from "../../ui/ew-notice";
import { claimsPage, parseWikiFile, personalRemainder, renderWikiNote, updateWikiNote } from "./format";
import { placementFolder, placeNote } from "./placement";
import type { WikiNoteInput } from "./types";
import { keepWritten, type WrittenStore } from "./written";
import { obsidianBackupStore } from "../../notes/backup-store";
import { pruneBackups, type BackupStore } from "../profile-backup";

export type SyncResult = "superseded" | "created" | "updated" | "unchanged" | "user_edited" | "backup_failed" | "trashed"
  /** A page in the trash whose note held the user's own text: the note stays with that text only (trashNote). */
  | "kept_mine"
  /** A placeOnly run whose rename / move did not happen (the reason was told). */
  | "move_failed";

/**
 * The 7.1e NoteVault port plus a read-only listing and a read of the hidden written copies (written.ts). All writes go
 * through ObsidianNoteVault (note-service.ts).
 */
export interface WikiNoteVault extends NoteVault, WrittenStore, BackupStore {
  /** Markdown file paths at or below `folder` (case-insensitive prefix), sorted. */
  listMarkdown(folder: string): string[];
  /** fileManager.renameFile: renames or moves a note and keeps the [[links]] to it (the target folder exists). Rejects on a clash or a busy file. */
  rename(from: string, to: string): Promise<void>;
  /** Into Obsidian's trash, the way the user set it (system trash or .trash). */
  trash(path: string): Promise<void>;
}
export interface SyncDeps {
  valid?: () => boolean;
  store: WikiNoteVault; now: () => Date; rand: () => string;
  /** Says why a rename or a move did not happen (the note stays where it is). */
  notify?: (text: string) => void;
  /** Where the page's file is after this sync ("" = none) and whether it was moved ("failed": the move did not happen). */
  onPath?: (path: string, moved: "no" | "yes" | "failed") => void;
}

/** 2026-10-08T08:00:00.123Z → 20261008T080000123Z */
const stamp = (d: Date) => d.toISOString().replace(/[-:.]/g, "");
const noteBase = (path: string) => path.replace(/^.*\//, "").replace(/\.md$/i, "");

/** R7–R9. Every read and check finishes before the first write. */
export async function syncWithVault(d: SyncDeps, folderSetting: string, input: WikiNoteInput, accepted?: string): Promise<SyncResult> {
  const v = validateNotesFolder(folderSetting);
  if (!v.ok) throw new Error(`EvoWiki: invalid notes folder: ${v.reason}`);
  if (d.valid?.() === false) return "superseded";
  const now = d.now();
  const fresh = renderWikiNote(input, now); // throws on an unwritable page id
  const claimants: { path: string; text: string }[] = [];
  for (const path of d.store.listMarkdown(v.folder)) {
    const text = await d.store.read(path);
    if (claimsPage(text, input.pageId)) claimants.push({ path, text });
  }
  if (claimants.length > 1) {
    throw new Error(`EvoWiki: ${claimants.length} notes claim page ${input.pageId}: ${claimants.map((c) => c.path).join(", ")}`);
  }
  if (d.valid?.() === false) return "superseded";
  const own = claimants[0] ?? null;
  if (input.trashed === true) {
    d.onPath?.("", "no");
    return own === null ? "unchanged" : trashNote(d, own);
  }
  // A page being streamed does not know its folder yet (emptyPage): its file stays where the reconcile pass put it.
  const path = input.writing === true && own !== null ? own.path
    : await placeNote((p) => d.store.occupied(p), v.folder, input, own?.path ?? null);
  if (own !== null) {
    const r = input.placeOnly === true ? "unchanged" : await update(d, own, input, now, accepted);
    // The file follows the page's title and folder (even when its text is the user's); a failed move is told, never hidden.
    if (path === own.path) d.onPath?.(own.path, "no");
    else if (await move(d, own.path, path, v.folder, input)) d.onPath?.(path, "yes");
    else { d.onPath?.(own.path, "failed"); if (input.placeOnly === true) return "move_failed"; }
    return r;
  }
  if (input.placeOnly === true) { d.onPath?.("", "no"); return "unchanged"; }
  // Q8: the copy of what is written goes first (a failed note write then leaves only a harmless orphan copy).
  await keepWritten(d.store, input);
  await d.store.ensureFolder(placementFolder(v.folder, input.folder));
  if (d.valid?.() === false) return "superseded";
  await d.store.create(path, fresh.markdown);
  d.onPath?.(path, "no");
  return "created";
}

/** Renames / moves a note to `to`; on failure nothing changes and the reason is told. */
async function move(d: SyncDeps, from: string, to: string, root: string, input: WikiNoteInput): Promise<boolean> {
  try {
    await d.store.ensureFolder(placementFolder(root, input.folder));
    if (d.valid?.() === false) return false;
    await d.store.rename(from, to);
    return true;
  } catch (e) {
    d.notify?.(tr("note.renameFailed", { title: input.title, detail: e instanceof Error ? e.message : String(e) }));
    return false;
  }
}

/**
 * The page is in the trash: an unedited note goes to Obsidian's trash; one whose 我的笔记 (or the text above the
 * block) holds the user's own words stays as that text without the generated block, saying why (personalRemainder;
 * written only if the file is still what was read); an edited or unreadable one stays as it is.
 */
async function trashNote(d: SyncDeps, note: { path: string; text: string }): Promise<SyncResult> {
  const p = parseWikiFile(note.text);
  if (p.kind !== "one" || p.edited) return "user_edited";
  const kept = personalRemainder(p);
  if (kept === null) {
    if (d.valid?.() === false) return "superseded";
    await d.store.trash(note.path);
    return "trashed";
  }
  let changed = false;
  await d.store.process(note.path, (data) => {
    if (d.valid?.() === false || data !== note.text) { changed = true; return data; }
    return kept;
  });
  return d.valid?.() === false ? "superseded" : changed ? "user_edited" : "kept_mine";
}

/**
 * v1 or v2 note, unedited → rewritten as v2 (migration included); edited → the note stays as typed (user_edited, R1.4)
 * until the server holds the edit: `accepted` is the exact note text whose edit it took, and only that text is written
 * over (the block then reads what he typed, with the page's other sections brought up to date). The new text is computed
 * before the backup, and written only if the file is still exactly what was backed up.
 */
async function update(d: SyncDeps, note: { path: string; text: string }, input: WikiNoteInput, now: Date, accepted?: string): Promise<SyncResult> {
  const u = updateWikiNote(note.text, input, now, accepted === note.text);
  // An unedited note that is current: its body is exactly this render, so its copy can be kept now if it is missing
  // (notes written before the copies existed).
  if (u.kind === "unchanged") { await keepWritten(d.store, input); return "unchanged"; }
  if (u.kind !== "write") return u.kind;

  const dir = `.evowiki/backups/${stamp(now)}-${d.rand()}`;
  const backupPath = `${dir}/${noteBase(note.path)}.md.bak`;
  try {
    if (await d.store.occupied(backupPath)) return "backup_failed";
    await d.store.ensureFolder(dir);
    await d.store.create(backupPath, note.text);
  } catch {
    return "backup_failed";
  }
  await pruneBackups(d.store, async (file) => {
    if (!/\/[^/]+\.md\.bak$/.test(file)) return false;
    const text = await d.store.readHidden(file);
    return text !== null && claimsPage(text, input.pageId);
  });

  await keepWritten(d.store, input);
  let changed = false;
  await d.store.process(note.path, (data) => {
    // The file must be exactly what was backed up; any concurrent edit, inside or outside the markers, aborts.
    if (d.valid?.() === false || data !== note.text) { changed = true; return data; }
    return u.text;
  });
  // On a concurrent edit the backup stays as an orphan: a stale backup is harmless, a lost edit is not.
  return d.valid?.() === false ? "superseded" : changed ? "user_edited" : "updated";
}

/** Obsidian's own trash setting: fileManager.trashFile (1.6.6+) follows it; older versions get vault.trash with the config's choice. */
async function trashLikeTheUser(app: App, file: TAbstractFile): Promise<void> {
  const fm = app.fileManager as { trashFile?: (f: TAbstractFile) => Promise<void> };
  if (typeof fm.trashFile === "function") { await fm.trashFile(file); return; }
  const cfg = (app.vault as { getConfig?: (k: string) => unknown }).getConfig?.("trashOption");
  await app.vault.trash(file, cfg === "system");
}

export function obsidianWikiNoteVault(app: App): WikiNoteVault {
  const nv = new ObsidianNoteVault(app.vault);
  return {
    ...obsidianBackupStore(app),
    occupied: (p) => nv.occupied(p),
    read: (p) => nv.read(p),
    create: (p, t) => nv.create(p, t),
    process: (p, fn) => nv.process(p, fn),
    ensureFolder: (p) => nv.ensureFolder(p),
    // Read only: .evowiki/ is not indexed, so the adapter reads it (no TFile).
    readHidden: async (p) => ((await app.vault.adapter.exists(p)) ? app.vault.adapter.read(p) : null),
    rename: async (from, to) => {
      const f = app.vault.getAbstractFileByPath(from);
      if (!(f instanceof TFile)) throw new Error(`Not a file: ${from}`);
      await app.fileManager.renameFile(f, to);
    },
    trash: async (path) => {
      const f = app.vault.getAbstractFileByPath(path);
      if (!(f instanceof TFile)) throw new Error(`Not a file: ${path}`);
      await trashLikeTheUser(app, f);
    },
    listMarkdown: (folder) => {
      const pre = folder.toLowerCase() + "/";
      return app.vault.getMarkdownFiles().map((f) => f.path).filter((p) => p.toLowerCase().startsWith(pre)).sort();
    },
  };
}

const chains = new Map<string, Promise<unknown>>();
/** R10: calls with the same key run one after another; a failure does not block the next call. */
export function serialized<T>(key: string, fn: () => Promise<T>): Promise<T> {
  const prev = chains.get(key) ?? Promise.resolve();
  const run = prev.then(fn, fn);
  const tail = run.then(() => undefined, () => undefined);
  chains.set(key, tail);
  void tail.then(() => { if (chains.get(key) === tail) chains.delete(key); });
  return run;
}

export function syncWikiNote(app: App, folder: string, input: WikiNoteInput,
  onPath?: SyncDeps["onPath"], accepted?: string, valid?: () => boolean): Promise<SyncResult> {
  return serialized(input.pageId, () => syncWithVault(
    { ...(valid ? { valid } : {}), store: obsidianWikiNoteVault(app), now: () => new Date(), rand: () => randomBytes(4).toString("hex"),
      notify: (t) => { ewNotice(t); }, ...(onPath ? { onPath } : {}) }, folder, input, accepted));
}
