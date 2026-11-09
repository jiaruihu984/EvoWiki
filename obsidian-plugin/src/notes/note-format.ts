const RESERVED = /^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$/i;
const MAX_NAME = 120; // code points
/**
 * E1: deterministic, cross-platform-safe file name; falls back to the concept id. Caps at 120 code points first and
 * trims afterwards, so the name never ends in a space or dot, never splits a surrogate pair, and stays ≤ 120 code points
 * even with the reserved-name "-" suffix.
 */
export function sanitizeFileName(name: string, conceptId: string): string {
  const trimEnds = (s: string) => s.replace(/^[. ]+|[. ]+$/g, "");
  const clean = (x: string) => {
    const replaced = trimEnds(x.replace(/[\\/:*?"<>|#^[\]\u0000-\u001f]/g, "-").replace(/\s+/g, " "));
    const cps = Array.from(replaced).slice(0, MAX_NAME);
    let t = trimEnds(cps.join(""));
    if (RESERVED.test(t)) {
      const keep = Array.from(t);
      t = (keep.length + 1 > MAX_NAME ? keep.slice(0, MAX_NAME - 1).join("") : t) + "-";
    }
    return t;
  };
  return clean(name) || clean(conceptId) || "concept";
}

/** E1/A1: a vault-relative POSIX folder that is not Obsidian's config and not EvoWiki's internal area. */
export function validateNotesFolder(input: string): { ok: true; folder: string } | { ok: false; reason: string } {
  const raw = input.trim();
  if (raw === "") return { ok: true, folder: "EvoWiki" };
  if (/^[/\\]|^[A-Za-z]:|\\/.test(raw)) return { ok: false, reason: "Use a vault-relative folder with / separators." };
  const parts = raw.split("/").filter((x) => x !== "");
  if (parts.some((x) => x === ".." || x === ".")) return { ok: false, reason: "'..' and '.' are not allowed." };
  const first = parts[0]?.toLowerCase();
  if (first === ".obsidian" || first === ".evowiki") return { ok: false, reason: "That folder is reserved." };
  return { ok: true, folder: parts.join("/") };
}

/** A3: EvoWiki's internal area is excluded from every note/link scan explicitly. */
export const isInternalPath = (path: string): boolean => /^\.evowiki(\/|$)/i.test(path);
