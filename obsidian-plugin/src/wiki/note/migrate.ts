/**
 * The pages that have a note in the wiki folder (Task 13): the reconcile pass looks at these pages (controller
 * reconcileFiles), and the one-time migration of the old file names 「X（i-n）.md」 (the plugin's 「X（i/n）」 title with
 * `/` made `-`, flat in the Wiki folder) looks at those among them. Read-only here; the move itself is sync.ts's.
 */
import { validateNotesFolder } from "../../notes/note-format";
import { entryString } from "./frontmatter";
import { FM_KEYS, parseWikiFile, STATUS_WRITING } from "./format";
import { isLegacyPositionName } from "./placement";

export interface LegacyScanVault {
  listMarkdown(folder: string): string[];
  read(path: string): Promise<string>;
}

/** Page ids of the notes in the wiki folder (at or below), in path order, no repeats; `keep` picks files by base name. */
export async function notePageIds(store: LegacyScanVault, folderSetting: string,
  keep: (base: string) => boolean = () => true): Promise<string[]> {
  const v = validateNotesFolder(folderSetting);
  if (!v.ok) return [];
  const ids: string[] = [];
  for (const path of store.listMarkdown(v.folder)) {
    if (!keep(path.replace(/^.*\//, "").replace(/\.md$/i, ""))) continue;
    const p = parseWikiFile(await store.read(path));
    if (p.kind === "one" && !ids.includes(p.pageId)) ids.push(p.pageId);
  }
  return ids;
}

/** Page ids of the notes whose frontmatter says `evowiki-status: writing`, in path order. */
export async function writingPageIds(store: LegacyScanVault, folderSetting: string): Promise<string[]> {
  const v = validateNotesFolder(folderSetting);
  if (!v.ok) return [];
  const ids: string[] = [];
  for (const path of store.listMarkdown(v.folder)) {
    const p = parseWikiFile(await store.read(path));
    const status = p.kind === "one" ? p.fm?.entries.find((x) => x.key === FM_KEYS.status) : undefined;
    if (p.kind === "one" && status !== undefined && entryString(status) === STATUS_WRITING && !ids.includes(p.pageId)) ids.push(p.pageId);
  }
  return ids;
}

/** Page ids of the notes whose file name ends in 「（i-n）」. */
export const legacyPageIds = (store: LegacyScanVault, folderSetting: string): Promise<string[]> =>
  notePageIds(store, folderSetting, isLegacyPositionName);
