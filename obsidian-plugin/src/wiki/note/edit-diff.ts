/**
 * A wiki note's written copy, and what the user changed against it, as a short summary (the `note_edit` signal's text;
 * note/hand-edit.ts sends it with the edit). Pure: the caller reads the note and the written copy.
 *
 * R4.1 / Q8 (revised): the diff is made against the body the plugin last wrote into the note (`Written`: kept in the
 * vault under .evowiki/written/, see written.ts, and named by the note's own evowiki_hash), so every difference is the
 * user's even after the page changed on the server. A note written before that copy existed falls back to rendering
 * the current page, used only when that render has exactly the stored hash (writtenFallback).
 */
import { diffArrays } from "diff";
import { WIKI_COPY } from "../copy";
import { renderWikiBody, wikiBodyLayout, wikiHash, type WikiBodyLayout } from "./format";
import type { WikiNoteInput, WikiNoteSection } from "./types";

/** A section as it was written: what its note lines came from (note/hand-edit.ts reads an edit back with this). */
export type WrittenSource = Pick<WikiNoteSection, "role" | "heading" | "cue" | "body" | "folded" | "noteBody" | "fingerprint">;
export interface WrittenSection { id: string; label: string; from: number; to: number; artifactId?: string; src: WrittenSource }
/** Where each section sits in a written body, with the section's name as it was when written (sectionLabel). */
export interface WrittenLayout { sections: WrittenSection[] }
/** The generated body the plugin wrote into a page's note: its page, its hash (the note's evowiki_hash), its layout. */
export interface Written { pageId: string; hash: string; body: string; layout: WrittenLayout }

/** The Written for `input`: renderWikiBody, its hash and its layout with each section's name resolved now. */
export function writtenFrom(input: WikiNoteInput): Written {
  const body = renderWikiBody(input);
  const l: WikiBodyLayout = wikiBodyLayout(input);
  return {
    pageId: input.pageId, hash: wikiHash(input.pageId, body), body,
    layout: { sections: l.sections.map((x, k) => {
      const sec = input.sections[k]!;
      const src: WrittenSource = { role: sec.role, heading: sec.heading, cue: sec.cue, body: sec.body, folded: sec.folded, ...(sec.noteBody ? { noteBody: sec.noteBody } : {}), ...(sec.fingerprint ? { fingerprint: sec.fingerprint } : {}) };
      return { ...x, label: sectionLabel(sec), ...(sec.artifactId ? { artifactId: sec.artifactId } : {}), src };
    }) },
  };
}

/** A note written before the written copy existed: the current page's render, only if it is exactly what was written. */
export function writtenFallback(input: WikiNoteInput, hash: string): Written | null {
  const w = writtenFrom(input);
  return w.hash === hash ? w : null;
}

/** HandEditIn.summary's limit (schema MAX_NOTE_SUMMARY). */
export const MAX_SUMMARY = 2000;
/** Each listed line is cut to this many characters. */
export const MAX_LINE = 200;
/** Either side longer than this: no line-by-line diff. */
export const MAX_DIFF_LINES = 3000;

/**
 * R3.2: a section's name, the same as 「哪一部分」 in the feedback dialog (organizer / line / recap / check have no `##`
 * heading of their own; a folded one says so).
 */
export function sectionLabel(x: Pick<WikiNoteSection, "role" | "heading" | "cue" | "folded">): string {
  const name = x.heading.trim() || x.cue.trim() || WIKI_COPY.tocUntitled;
  const base = x.role === "organizer" ? WIKI_COPY.feedbackOrganizer
    : x.role === "line" ? WIKI_COPY.feedbackLine(name)
      : x.role === "recap" ? WIKI_COPY.feedbackRecap(name)
        : x.role === "check" ? WIKI_COPY.feedbackCheck : name;
  return x.folded ? WIKI_COPY.feedbackFolded(base) : base;
}

/** One run of changed lines: the old lines it touches (by index) and the removed / added text. */
export interface Hunk { at: number[]; del: string[]; add: string[] }

/** The most inserted + deleted lines a line diff searches for; past it the whole span between head and tail is one hunk. */
const HUNK_MAX_EDITS = 6000;

/** Line diff of a vs b (jsdiff `diffArrays`; a common head and tail are trimmed first), grouped into runs of changes. */
export function hunks(a: readonly string[], b: readonly string[]): Hunk[] {
  let p = 0;
  while (p < a.length && p < b.length && a[p] === b[p]) p++;
  let s = 0;
  while (s < a.length - p && s < b.length - p && a[a.length - 1 - s] === b[b.length - 1 - s]) s++;
  const A = a.slice(p, a.length - s), B = b.slice(p, b.length - s);
  // Fail closed past the cap: the whole span is one replaced run, never a partial diff.
  const changes = diffArrays(A, B, { maxEditLength: HUNK_MAX_EDITS, timeout: 2000 })
    ?? [{ removed: true, added: false, count: A.length, value: [] }, { removed: false, added: true, count: B.length, value: [] }];
  const out: Hunk[] = [];
  let cur: (Hunk & { pos: number }) | null = null;
  const close = (): void => {
    if (cur === null) return;
    // A pure insertion is placed on the old line before it (the end of the section it was typed into).
    out.push({ at: cur.at.length ? cur.at : [Math.max(0, cur.pos - 1)], del: cur.del, add: cur.add });
    cur = null;
  };
  let i = 0, j = 0;
  for (const c of changes) {
    if (c.count === 0) continue;
    if (!c.removed && !c.added) { close(); i += c.count; j += c.count; continue; }
    cur ??= { at: [], del: [], add: [], pos: p + i };
    for (let x = 0; x < c.count; x++) {
      if (c.removed) { cur.at.push(p + i); cur.del.push(A[i]!); i++; } else { cur.add.push(B[j]!); j++; }
    }
  }
  close();
  return out;
}

export type Region = { kind: "start" } | { kind: "section"; index: number };

export function regionOf(layout: WrittenLayout, line: number): Region {
  const k = layout.sections.findIndex((x) => line >= x.from && line < x.to);
  if (k >= 0) return { kind: "section", index: k };
  const last = layout.sections.length - 1;
  return last >= 0 && line >= layout.sections[last]!.to ? { kind: "section", index: last } : { kind: "start" };
}

const cut = (line: string): string => {
  const cps = Array.from(line);
  return cps.length > MAX_LINE ? cps.slice(0, MAX_LINE - 1).join("") + "…" : line;
};
const blank = (line: string): boolean => line.trim() === "";

/**
 * Groups of lines, joined; when that is over MAX_SUMMARY, whole groups while they fit, then the 「还有 N 处」 tail. Only
 * a first group too long by itself is cut line by line (it then counts as not listed in N).
 */
function pack(groups: string[][]): string {
  const all = groups.map((g) => g.join("\n")).join("\n");
  if (all.length <= MAX_SUMMARY) return all;
  // Room for the tail at its longest (every group unlisted) and the line break before it.
  const room = MAX_SUMMARY - WIKI_COPY.noteEditMore(groups.length).length - 1;
  let out = "", done = 0;
  for (const g of groups) {
    const next = (out ? out + "\n" : "") + g.join("\n");
    if (next.length > room) break;
    out = next;
    done++;
  }
  if (done === 0) {
    for (const line of groups[0]!) {
      const next = (out ? out + "\n" : "") + line;
      if (next.length > room) break;
      out = next;
    }
  }
  const tail = WIKI_COPY.noteEditMore(groups.length - done);
  return out ? `${out}\n${tail}` : tail;
}

/**
 * The summary of an edit: every run of changed lines under its section's name. Blank lines are not listed; when nothing
 * else changed it still names the edit (an edit with only blank lines is never sent).
 */
export function editSummary(before: string[], after: string[], layout: WrittenLayout): string {
  if (before.length > MAX_DIFF_LINES || after.length > MAX_DIFF_LINES) return WIKI_COPY.noteEditTooMany(before.length, after.length);
  const label = (r: Region): string => r.kind === "start" ? WIKI_COPY.noteEditStart : layout.sections[r.index]!.label;
  const groups: string[][] = [];
  for (const h of hunks(before, after)) {
    const del = h.del.filter((x) => !blank(x)), add = h.add.filter((x) => !blank(x));
    if (del.length === 0 && add.length === 0) continue;
    groups.push([WIKI_COPY.noteEditPart(label(regionOf(layout, h.at[0]!))),
      ...del.map((x) => WIKI_COPY.noteEditDel(cut(x))), ...add.map((x) => WIKI_COPY.noteEditAdd(cut(x)))]);
  }
  return groups.length === 0 ? WIKI_COPY.noteEditMore(0) : pack(groups);
}
