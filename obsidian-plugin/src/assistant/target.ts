/** Which note or page the active file is for the reading assistant, and the quote the editor menu attaches. Pure. */
import { isInternalPath } from "../notes/note-format";
import { parseWikiNote } from "../wiki/note";
import type { ActiveNote } from "./session-store";
import type { Quote } from "./turn";
import { MAX_HEADING_CHARS, MAX_QUOTE_CHARS } from "./types";

export interface FileLike { path: string; basename: string; extension: string }

/** Personal-wiki block first (page), then evowiki.concept_id (note), else unsupported; no/internal/non-md file → none. */
export function resolveActiveNote(file: FileLike | null, conceptId: string | null, text: string | null): ActiveNote {
  if (!file || file.extension !== "md" || isInternalPath(file.path)) return { kind: "none" };
  const wiki = text === null ? null : parseWikiNote(text);
  if (wiki) return { kind: "page", pageId: wiki.pageId, title: file.basename };
  if (conceptId !== null) return { kind: "note", path: file.path, conceptId, title: file.basename };
  return { kind: "unsupported", title: file.basename };
}

/** Code points, as pydantic counts them. The quote text is never truncated; a heading is metadata and is cut. */
export function quoteFor(raw: string, heading: string | null, isQuestion = false): { ok: true; quote: Quote } | { ok: false; reason: "blank" | "too-long" } {
  if (raw.trim() === "") return { ok: false, reason: "blank" };
  if ([...raw].length > MAX_QUOTE_CHARS) return { ok: false, reason: "too-long" };
  const cut = heading === null ? null : [...heading].slice(0, MAX_HEADING_CHARS).join("");
  return { ok: true, quote: { text: raw, heading: cut, kind: isQuestion ? "wrong_answer" : "text" } };
}
