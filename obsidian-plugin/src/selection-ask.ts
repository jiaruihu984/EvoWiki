/** The heading a selection sits under, for the editor menu's 问陪读助手 quote (main.ts does the Obsidian wiring). */

export interface HeadingLike { heading: string; position: { start: { line: number } } }

/** The last heading at or above `line` (0-based); null when there is none or its text is blank (never ""). */
export function headingAt(headings: readonly HeadingLike[] | undefined, line: number): string | null {
  let under: HeadingLike | undefined;
  for (const h of headings ?? []) if (h.position.start.line <= line) under = h;
  return under !== undefined && under.heading.trim() !== "" ? under.heading : null;
}
