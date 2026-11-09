/**
 * Text-level YAML frontmatter for wiki notes (no Obsidian, no YAML library). Deliberately narrow: it understands the
 * shapes Obsidian itself writes (top-level `key: value` lines, indented continuations, block or flow lists) and reports
 * anything else as unparseable, so the caller can fail closed instead of rewriting a file it does not understand.
 * Entries it does not manage are carried over line for line.
 */

/** One top-level entry: its key (null for leading blank / comment lines) and its raw lines, without line endings. */
export interface FmEntry { key: string | null; lines: string[] }
export interface Frontmatter { entries: FmEntry[]; eol: string }
export type FmSplit =
  | { kind: "none"; rest: string }
  | { kind: "bad"; reason: string }
  | { kind: "ok"; fm: Frontmatter; rest: string };

const OPEN = /^---[ \t]*(\r?\n)/;
const CLOSE = /^---[ \t]*$/;
/** A top-level key line: a quoted key, or a plain key that does not start with a YAML indicator. */
const KEY_LINE = /^(?:"((?:[^"\\\n]|\\.)*)"|'((?:[^'\n]|'')*)'|([^\s"'#\-?:,[\]{}&*!|>%@`].*?))[ \t]*:(?:[ \t]+(.*))?$/;

/** Splits off the frontmatter block at the very start of `text`. An opening line without a closing one is "bad". */
export function splitFrontmatter(text: string): FmSplit {
  const open = OPEN.exec(text);
  if (!open) return { kind: "none", rest: text };
  const eol = open[1]!;
  const lines: string[] = [];
  let pos = open[0].length;
  for (;;) {
    if (pos > text.length) return { kind: "bad", reason: "unterminated frontmatter" };
    const nl = text.indexOf("\n", pos);
    const end = nl < 0 ? text.length : nl + 1;
    const line = text.slice(pos, end).replace(/\r?\n$/, "");
    if (CLOSE.test(line)) {
      const entries = entriesOf(lines);
      if (typeof entries === "string") return { kind: "bad", reason: entries };
      return { kind: "ok", fm: { entries, eol }, rest: text.slice(end) };
    }
    if (nl < 0) return { kind: "bad", reason: "unterminated frontmatter" };
    lines.push(line);
    pos = end;
  }
}

function entriesOf(lines: string[]): FmEntry[] | string {
  const out: FmEntry[] = [];
  const seen = new Set<string>();
  for (const line of lines) {
    const last = out[out.length - 1];
    if (/^[ \t]*(#.*)?$/.test(line)) {
      if (last) last.lines.push(line); else out.push({ key: null, lines: [line] });
      continue;
    }
    if (/^[ \t]/.test(line) || /^-(?:[ \t]|$)/.test(line)) {
      if (!last || last.key === null) return `continuation without a key: ${line}`;
      last.lines.push(line);
      continue;
    }
    const m = KEY_LINE.exec(line);
    if (!m) return `unsupported frontmatter line: ${line}`;
    const key = m[1] !== undefined ? unquoteDouble(`"${m[1]}"`) : m[2] !== undefined ? m[2].replace(/''/g, "'") : m[3]!;
    if (key === null) return `bad quoted key: ${line}`;
    if (seen.has(key)) return `duplicate key: ${key}`;
    seen.add(key);
    out.push({ key, lines: [line] });
  }
  return out;
}

function unquoteDouble(s: string): string | null {
  try {
    const v: unknown = JSON.parse(s);
    return typeof v === "string" ? v : null;
  } catch {
    return null;
  }
}

/** One scalar (plain, 'single' or "double" quoted, with an optional trailing comment); null if not a plain string. */
export function parseScalar(raw: string): string | null {
  const t = raw.trim();
  if (t.startsWith("\"")) {
    const m = /^("(?:[^"\\]|\\.)*")[ \t]*(?:#.*)?$/.exec(t);
    return m ? unquoteDouble(m[1]!) : null;
  }
  if (t.startsWith("'")) {
    const m = /^'((?:[^']|'')*)'[ \t]*(?:#.*)?$/.exec(t);
    return m ? m[1]!.replace(/''/g, "'") : null;
  }
  const v = t.replace(/[ \t]+#.*$/, "");
  if (v === "" || /^[[\]{}&*!|>%@`,#]/.test(v) || /^-(?:[ \t]|$)/.test(v) || /:([ \t]|$)/.test(v)) return null;
  return v;
}

/** The entry's single-line string value; null when it has more lines (other than blank / comment) or is not a string. */
export function entryString(e: FmEntry): string | null {
  if (e.lines.slice(1).some((l) => !/^[ \t]*(#.*)?$/.test(l))) return null;
  const m = KEY_LINE.exec(e.lines[0]!);
  return m?.[4] === undefined ? null : parseScalar(m[4]);
}

/** A string list in any shape Obsidian writes: `k: x`, `k: [a, "b"]`, `k: []`, `k:` with `- a` lines, or `k:` empty. */
export function entryList(e: FmEntry): string[] | null {
  const inline = KEY_LINE.exec(e.lines[0]!)?.[4];
  const more = e.lines.slice(1).filter((l) => !/^[ \t]*(#.*)?$/.test(l));
  if (inline !== undefined && inline.trim() !== "" && !/^#/.test(inline.trim())) {
    if (more.length) return null;
    const t = inline.trim().replace(/[ \t]+#[^\]]*$/, "");
    if (!t.startsWith("[")) { const one = parseScalar(t); return one === null ? null : [one]; }
    const m = /^\[(.*)\]$/.exec(t);
    if (!m) return null;
    if (m[1]!.trim() === "") return [];
    const items: string[] = [];
    const ITEM = /[ \t]*("(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|[^,"'[\]{}]*?)[ \t]*(,|$)/y;
    let at = 0;
    const body = m[1]!;
    while (at < body.length) {
      ITEM.lastIndex = at;
      const im = ITEM.exec(body);
      if (!im || im[0] === "") return null;
      const v = parseScalar(im[1]!);
      if (v === null) return null;
      items.push(v);
      at = ITEM.lastIndex;
      if (im[2] === "") break;
    }
    return at >= body.length ? items : null;
  }
  const items: string[] = [];
  for (const l of more) {
    const m = /^[ \t]*-[ \t]+(.*)$/.exec(l);
    const v = m ? parseScalar(m[1]!) : null;
    if (v === null) return null;
    items.push(v);
  }
  return items;
}

/** A YAML double-quoted scalar: JSON string syntax is valid YAML. */
export const yamlString = (s: string): string => JSON.stringify(s);

/** `key: value` lines for a managed entry, or a block list. */
export function scalarEntry(key: string, value: string): FmEntry { return { key, lines: [`${key}: ${yamlString(value)}`] }; }
export function listEntry(key: string, items: string[]): FmEntry {
  return { key, lines: items.length ? [`${key}:`, ...items.map((i) => `  - ${/^[A-Za-z0-9_][\w.-]*$/.test(i) ? i : yamlString(i)}`)] : [`${key}: []`] };
}

export function serializeFrontmatter(fm: Frontmatter): string {
  const eol = fm.eol;
  return `---${eol}${fm.entries.map((e) => e.lines.map((l) => l + eol).join("")).join("")}---${eol}`;
}
