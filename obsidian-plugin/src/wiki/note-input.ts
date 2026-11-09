/** PageOut → E3a's WikiNoteInput. Pure; the note never carries check answers. */
import { diffNote } from "./format";
import { isMasteredCheck } from "./goal-checks";
import type { Names } from "./names";
import type { WikiNoteInput, WikiNoteSection } from "./note-port";
import type { PageOut, SectionOut } from "./schema";

const letter = (i: number): string => String.fromCharCode(65 + i);

function sectionBody(s: SectionOut): string {
  if (s.role !== "check" || s.check === null) return s.body;
  return [s.check.stem, ...s.check.options.map((o, i) => `- ${letter(i)}. ${o}`)].join("\n");
}

export function toWikiNoteInput(page: PageOut, names: Names): WikiNoteInput {
  const headings = new Map(page.sections.map((s) => [s.id, s.heading]));
  const heading = (sid: string): string | null => headings.get(sid) ?? null;
  const sections: WikiNoteSection[] = page.sections.map((s) => ({
    id: s.id, heading: s.heading, role: s.role, cue: s.cue, body: sectionBody(s),
    // a collapsed page folds everything but a locked section
    folded: (page.collapsed && !s.pinned) || s.folded || isMasteredCheck(s),
    ...(s.note_body ? { noteBody: s.note_body } : {}),
    ...(s.artifact_id ? { artifactId: s.artifact_id } : {}),
    ...(s.fingerprint ? { fingerprint: s.fingerprint } : {}),
  }));
  return {
    pageId: page.page_id,
    title: page.title,
    goal: page.goal,
    diffText: diffNote(page.diff, heading, names),
    collapsed: page.collapsed,
    sections,
    sources: page.sources ?? [],
    ...(page.folder ? { folder: page.folder } : {}),
    ...(page.trashed === true ? { trashed: true } : {}),
  };
}
