/**
 * Where a section starts in its wiki note (pure), so the controller can scroll there. The note is written by
 * note/format.ts; the line is found from the same writer output, so it follows any change to the format.
 */
import { renderWikiBody, sectionLines, WIKI_END, WIKI_START } from "./note/format";
import type { WikiNoteInput, WikiNoteSection } from "./note-port";

/** The lines the writer puts in the block for one section (none for a check whose ids cannot be written). */
const chunk = (pageId: string, s: WikiNoteSection): string[] => {
  const lines = sectionLines(pageId, s);
  return lines.length === 0 ? [] : lines.join("\n").split("\n");
};
/** The fold sign after `[!type]` is view state (format.ts does the same); trailing spaces do not matter. */
const same = (a: string, b: string): boolean => {
  const n = (l: string) => l.replace(/^((?:[ \t]*>[ \t]?)+[ \t]*\[![^\]\n]*\])[+-]/, "$1").trimEnd();
  return n(a) === n(b);
};

/**
 * 0-based line of `text` (a wiki note) where section `sid` of `input` starts: an unfolded section's `## heading`, a
 * folded one's `> [!evowiki-known]-` callout line (the heading is not written when there is a cue), a check's
 * ```evowiki-check fence. First the place the writer puts it (block start + what comes before it); if the note says
 * something else there (written from an older page, or edited), the same line text is searched in the block, taking the
 * one with the same rank among same-text sections. Null when the note has no block or the section is not found.
 */
export function findSectionLine(text: string, input: WikiNoteInput, sid: string): number | null {
  const lines = text.split(/\r\n?|\n/);
  const start = lines.indexOf(WIKI_START);
  const end = start < 0 ? -1 : lines.indexOf(WIKI_END, start + 1);
  if (start < 0 || end < 0) return null;
  const at = input.sections.findIndex((x) => x.id === sid);
  const sec = input.sections[at];
  if (sec === undefined) return null;
  if (sec.role === "check") {
    // The block names its section on its own line: `section: <sid>`, two lines under the fence.
    for (let i = start + 1; i < end; i++) {
      if (lines[i] !== `section: ${sid}`) continue;
      return (lines[i - 2] ?? "").startsWith("```") ? i - 2 : i;
    }
    return null;
  }
  const own = chunk(input.pageId, sec);
  const lead = own.findIndex((l) => l.trim() !== "");
  if (lead < 0) return null;
  const want = own[lead]!;
  // Where the writer put it: the header callouts (renderWikiBody with no sections), then every earlier section.
  const header = renderWikiBody({ ...input, sections: [] });
  let offset = header === "" ? 0 : header.split("\n").length;
  for (const x of input.sections.slice(0, at)) offset += chunk(input.pageId, x).length;
  const guess = start + 1 + offset + lead;
  if (guess < end && same(lines[guess]!, want)) return guess;
  const rank = input.sections.slice(0, at).filter((x) => {
    const c = chunk(input.pageId, x);
    const l = c.find((y) => y.trim() !== "");
    return l !== undefined && same(l, want);
  }).length;
  let seen = 0;
  for (let i = start + 1; i < end; i++) {
    if (!same(lines[i]!, want)) continue;
    if (seen === rank) return i;
    seen++;
  }
  return null;
}
