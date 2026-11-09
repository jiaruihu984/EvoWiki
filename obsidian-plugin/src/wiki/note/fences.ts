/**
 * Fenced code blocks in note Markdown, by CommonMark's rules, for every scanner that skips code to find headings or
 * paragraphs: a fence opens on a line indented at most 3 spaces with a run of at least three backticks or tildes (a
 * backtick fence's info string may not contain a backtick: then it is inline code, not a fence); it closes on a line
 * indented at most 3 spaces with a run of the same character at least as long as the opener's and nothing after it but
 * blanks. An unclosed fence runs to the end of the text.
 */

/** An open fence: its character and the length of its opening run. */
export interface Fence { ch: "`" | "~"; n: number }

/** The fence `line` opens, or null. */
export function fenceOpen(line: string): Fence | null {
  const m = /^ {0,3}(`{3,}|~{3,})/.exec(line);
  if (!m) return null;
  const ch = m[1]![0] as "`" | "~";
  if (ch === "`" && line.slice(m[0].length).includes("`")) return null;
  return { ch, n: m[1]!.length };
}

/** Whether `line` closes `fence`. */
export function fenceCloses(line: string, fence: Fence): boolean {
  const m = /^ {0,3}(`+|~+)[ \t]*$/.exec(line);
  return m !== null && m[1]![0] === fence.ch && m[1]!.length >= fence.n;
}

/** What a line is, read in order: a fence's opening line, its closing line, a line inside it, or text outside code. */
export type FenceLine = "open" | "close" | "code" | "text";

/** Reads lines in order and says which are fenced code. */
export class FenceScanner {
  /** The fence open after the last line read, or null. */
  fence: Fence | null = null;

  step(line: string): FenceLine {
    if (this.fence !== null) {
      if (fenceCloses(line, this.fence)) { this.fence = null; return "close"; }
      return "code";
    }
    const f = fenceOpen(line);
    if (f !== null) { this.fence = f; return "open"; }
    return "text";
  }
}
