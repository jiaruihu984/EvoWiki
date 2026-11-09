/**
 * Where a wiki note lives (Task 13): `<wiki folder>/<page folder>/<page title>.md`, the names the page has in the graph and
 * on the path list (no position number). Pure; sync.ts asks the vault which paths are taken.
 *
 * Names in the order they are tried (deterministic, no model): the title; on a clash in the same folder the title with
 * the parent node's name 「定义（食品添加剂）」; then a counter 「（2）」, 「（3）」… after the last of those.
 */
import { sanitizeFileName } from "../../notes/note-format";

/** The most counter suffixes tried after the parent name before giving up. */
const MAX_COUNTER = 99;

/** Passed to sanitizeFileName as the fallback id: what comes back when the text holds nothing usable. */
const NO_NAME = "-no-name";

/** The folder segment for `PageOut.folder` ("" and names that sanitize to nothing → the Wiki root). One level only. */
export function folderSegment(folder: string | undefined): string {
  const f = (folder ?? "").trim();
  if (f === "") return "";
  const s = sanitizeFileName(f, NO_NAME);
  return s === NO_NAME ? "" : s;
}

/** The file base names to try, best first, without duplicates. */
export function candidateNames(title: string, pageId: string, parentName: string | null | undefined): string[] {
  const out: string[] = [];
  const add = (raw: string): string => { const n = sanitizeFileName(raw, pageId); if (!out.includes(n)) out.push(n); return n; };
  // Reserve space before appending a suffix; sanitizing an overlong combined name would discard the suffix.
  const append = (base: string, suffix: string): string =>
    add(`${Array.from(base).slice(0, 120 - Array.from(suffix).length).join("")}${suffix}`);
  const base = add(title);
  const parent = (parentName ?? "").trim();
  const parentPart = Array.from(sanitizeFileName(parent, pageId));
  const suffix = (counter: string): string => parent === "" ? counter
    : `（${parentPart.slice(0, 117 - Array.from(counter).length).join("")}）${counter}`;
  if (parent !== "") append(base, suffix(""));
  for (let n = 2; n <= MAX_COUNTER; n++) append(base, suffix(`（${n}）`));
  return out;
}

/** `<root>/<folder>` (the root alone for no folder). */
export function placementFolder(root: string, folder: string | undefined): string {
  const seg = folderSegment(folder);
  return seg === "" ? root : `${root}/${seg}`;
}

/**
 * The page's own file (`own`) when it sits at ANY candidate path (compared ignoring case: Obsidian paths clash that
 * way), so a file does not flip between names from one session to the next; else the first candidate that is free.
 * Throws when none is.
 */
export async function placeNote(occupied: (path: string) => Promise<boolean>, root: string,
  p: { pageId: string; title: string; folder?: string; parentName?: string | null }, own: string | null): Promise<string> {
  const dir = placementFolder(root, p.folder);
  const paths = candidateNames(p.title, p.pageId, p.parentName).map((name) => `${dir}/${name}.md`);
  if (own !== null && paths.some((path) => path.toLowerCase() === own.toLowerCase())) return own;
  for (const path of paths) if (!(await occupied(path))) return path;
  throw new Error(`EvoWiki: no free file name for page ${p.pageId} in ${dir}`);
}

/** Old names 「X（i-n）」 (the plugin's 「X（i/n）」 title with `/` made `-`): true for a file's base name. */
export const isLegacyPositionName = (base: string): boolean => /（\d+-\d+）$/.test(base);
