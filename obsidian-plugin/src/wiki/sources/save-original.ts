/**
 * Saving the original of a file that came from the computer: written once to `<notes folder>/资料/<name>`, never over an
 * existing file (a taken name gets " (2)", " (3)", … before the extension). The only vault write of 资料, and only into
 * that folder, through the NoteVault port (ensureFolder + createBinary). Deleting a source never touches these files.
 */
import { validateNotesFolder } from "../../notes/note-format";
import type { BinaryVault, NoteVault } from "../../notes/note-service";
import { SOURCES_COPY } from "./sources-copy";

export type OriginalStore = Pick<NoteVault, "occupied" | "ensureFolder"> & BinaryVault;

/** The most " (n)" tried before giving up (a hundred copies of one name is not a real case). */
const MAX_COPIES = 200;

/** A file name that is safe in a vault path: no folders, no characters Obsidian and Windows refuse, never empty. */
export function safeFileName(name: string): string {
  const base = name.replace(/\\/g, "/").split("/").pop() ?? "";
  const clean = base.replace(/[\u0000-\u001f<>:"|?*#^[\]]/g, "_").replace(/^\.+/, "").trim();
  return clean === "" ? "file" : clean;
}

/** `<notes>/资料`, or null when the notes folder setting is not a valid vault folder. */
export function originalsFolder(notesFolder: string): string | null {
  const v = validateNotesFolder(notesFolder);
  return v.ok ? `${v.folder}/${SOURCES_COPY.folder}` : null;
}

/**
 * The path a file named `name` would get: the first free one in the originals folder (nothing is written). Throws when the
 * folder is invalid or no name is free.
 */
export async function freeOriginalPath(store: Pick<OriginalStore, "occupied">, notesFolder: string, name: string): Promise<string> {
  const dir = originalsFolder(notesFolder);
  if (dir === null) throw new Error("EvoWiki: the notes folder is not a valid vault folder");
  const file = safeFileName(name);
  const dot = file.lastIndexOf(".");
  const stem = dot > 0 ? file.slice(0, dot) : file;
  const ext = dot > 0 ? file.slice(dot) : "";
  for (let n = 1; n <= MAX_COPIES; n++) {
    const path = `${dir}/${n === 1 ? file : `${stem} (${n})${ext}`}`;
    if (!(await store.occupied(path))) return path;
  }
  throw new Error(`EvoWiki: no free name for ${file}`);
}

/** Writes `data` at `path` (from freeOriginalPath): the originals folder is made if missing, an existing file is never touched (createBinary rejects). */
export async function writeOriginal(store: Pick<OriginalStore, "ensureFolder" | "createBinary">, path: string, data: ArrayBuffer): Promise<void> {
  const dir = path.slice(0, path.lastIndexOf("/"));
  await store.ensureFolder(dir);
  await store.createBinary(path, data);
}
