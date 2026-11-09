import type { EvoWikiSettings, PluginData } from "../../settings";
import { validateNotesFolder } from "../../notes/note-format";

/** Settings always carry the current identity's resolved, independent vault root. */
export const userFolder = (s: { notesFolder: string; userId: string }): string => s.notesFolder;

export const accountIdentity = (s: Pick<EvoWikiSettings, "serverUrl" | "userId">): string => JSON.stringify([s.serverUrl, s.userId]);
const overlaps = (a: string, b: string): boolean => {
  const x = a.toLowerCase(), y = b.toLowerCase();
  return x === y || x.startsWith(`${y}/`) || y.startsWith(`${x}/`);
};

/** Assign independent sibling roots. Nested roots would let one account's recursive note scan read another's files. */
export function accountFolder(data: Pick<PluginData, "settings" | "noteFolders">, next: EvoWikiSettings,
  occupied: (path: string) => boolean = () => false): EvoWikiSettings {
  const folders = data.noteFolders ??= {};
  const identity = accountIdentity(next);
  const currentIdentity = accountIdentity(data.settings);
  const saved = folders[identity];
  // A deliberate folder edit changes this account's root; an identity change restores its saved root.
  const requested = identity !== currentIdentity && saved ? saved : next.notesFolder;
  const checked = validateNotesFolder(requested);
  if (!checked.ok) throw new Error(checked.reason);
  let root = checked.folder;
  const otherRoots = Object.entries(folders).filter(([key]) => key !== identity).map(([, path]) => path);
  const conflict = (path: string): boolean => otherRoots.some((other) => overlaps(path, other));
  if (saved && conflict(root)) throw new Error(`EvoWiki: account folders overlap: ${root}`);
  if (!saved && otherRoots.length > 0) {
    const base = `${Object.values(folders)[0]} (${next.userId})`;
    let index = 1;
    root = base;
    while (conflict(root) || occupied(root)) root = `${base} ${++index}`;
  } else if (conflict(root)) throw new Error(`EvoWiki: account folders overlap: ${root}`);
  folders[identity] = root;
  return { ...next, notesFolder: root };
}
