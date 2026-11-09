/**
 * A `page_revise` draft (materials spec 2026-10-20 §7 D, UI principles 2026-10-21 §三) shown inside the wiki page as track
 * changes: what the overlay, the sticky bar, the top line, the chat card and the 参数 row show, as pure functions of the
 * ledger rows (`ActionOut.revise`) and the note's text. Nothing here writes the note: the draft reaches the file only
 * through the note sync after 确认替换 (the ledger's approve).
 *
 * Where the marks go (full-demo app.js `revData` / `trackEls`): each changed section is found in the note by its `##`
 * heading (section-menu.ts matchSection, the way the note writer names sections). Inside it, the old text is split into
 * paragraphs and each paragraph the draft changes or drops is matched to the note's own paragraph (closest text, in
 * order); only those paragraphs are replaced by the marked text, so the rest of the section (formulas, interactives,
 * the check) stays as Obsidian draws it. New paragraphs go after the paragraph before them. A section whose changed
 * paragraphs cannot all be matched is shown whole in marked text instead (never a guess at a wrong place); a section that
 * cannot be found at all gets no marks (the bar still counts it). The page's opening section (an organizer: no `##` line
 * in the note) is the text between the start marker and the first section heading.
 *
 * A paragraph part carries the Markdown the note has / would have for the old and the new text (note/format.ts
 * noteBlock: citations, 和资料不同 strips, escapes as the note writer puts them); overlay.ts renders both the way the note
 * is rendered and puts the marks on the rendered text, so lists, bold, code and math keep their look.
 */
import { diffArrays, diffChars } from "diff";
import type { ActionOut, ReviseOut, RevisedSectionOut } from "../bot/bot-schema";
import { FenceScanner } from "../note/fences";
import { noteBlock, WIKI_END, WIKI_START } from "../note/format";
import type { PageSourceOut } from "../schema";
import { matchSection, noteHeadings } from "../section-menu";
import type { WikiState } from "../state";
import { pendingRevise } from "./rows";

// ---- the character diff ----

export type DiffOp = ["=" | "-" | "+", string];

/** The most edits (inserted + deleted characters) a character diff searches for, and the longest it may run (ms). */
export const WORD_DIFF_MAX_EDITS = 6000;
export const WORD_DIFF_TIMEOUT_MS = 2000;

/**
 * app.js `wordDiff`: a character diff (jsdiff's Myers diff over code points, `diffChars`; not `diffWords`, which cannot
 * split Chinese text); an equal run shorter than 4 characters between two changes is folded into both sides, so the marks
 * read as phrases rather than scattered single characters. Fail closed: past the size / time cap the whole text is shown
 * as replaced (one deletion, one insertion), never a wrong or partial diff.
 */
export function wordDiff(a: string, b: string): DiffOp[] {
  const changes = diffChars(a, b, { maxEditLength: WORD_DIFF_MAX_EDITS, timeout: WORD_DIFF_TIMEOUT_MS });
  if (changes === undefined) {
    const whole: DiffOp[] = [];
    if (a !== "") whole.push(["-", a]);
    if (b !== "") whole.push(["+", b]);
    return whole;
  }
  const ops: DiffOp[] = [];
  for (const c of changes) {
    if (c.value === "") continue;
    const k: DiffOp[0] = c.added ? "+" : c.removed ? "-" : "=";
    const l = ops[ops.length - 1];
    if (l && l[0] === k) l[1] += c.value; else ops.push([k, c.value]);
  }
  const out: DiffOp[] = [];
  let del = "", add = "";
  const flush = (): void => { if (del) out.push(["-", del]); if (add) out.push(["+", add]); del = add = ""; };
  ops.forEach(([k, t], x) => {
    if (k === "=" && ([...t].length >= 4 || x === 0 || x === ops.length - 1)) { flush(); out.push(["=", t]); }
    else if (k === "=") { del += t; add += t; } else if (k === "-") del += t; else add += t;
  });
  flush();
  return out;
}

// ---- paragraphs ----

/** ⟦和资料不同：…⟧: the writer thinks the material is wrong (shown with a 和资料不同 label, never as ⟦⟧). */
export const NOTE_RE = /^⟦和资料不同[：:]\s*([\s\S]*?)⟧$/;
export const noteText = (t: string): string | null => { const m = NOTE_RE.exec(t.trim()); return m === null ? null : m[1]!; };

/** Blank-line separated paragraphs (a fenced block with blank lines inside stays one), trimmed, empty ones dropped. */
export function paragraphs(body: string): string[] {
  const out: string[] = [];
  let cur: string[] = [];
  const scan = new FenceScanner();
  for (const line of body.replace(/\r\n?/g, "\n").split("\n")) {
    if (scan.step(line) !== "text") { cur.push(line); continue; }
    if (line.trim() === "") { if (cur.length) out.push(cur.join("\n").trim()); cur = []; continue; }
    cur.push(line);
  }
  if (cur.length) out.push(cur.join("\n").trim());
  return out.filter((p) => p !== "");
}

/** The text a paragraph shows (page links as their text, emphasis marks and escapes dropped; math is left as written). */
export function shownText(p: string): string {
  return p.replace(/\[([^[\]\n]+)\]\(#p:[A-Za-z0-9_-]+\)/g, "$1").replace(/\*\*([^*\n]+)\*\*/g, "$1").replace(/\\([\\`*_{}[\]()#+\-.!|~<>%])/g, "$1");
}

/** The most paragraph edits a paragraph diff searches for. */
export const PARA_DIFF_MAX_EDITS = 4000;

export type ParaOp = { kind: "same"; i: number; j: number } | { kind: "change"; i: number; j: number } | { kind: "del"; i: number } | { kind: "ins"; j: number };

/**
 * Old vs new paragraphs: equal ones (LCS on the text) are kept; between two kept ones, old and new paragraphs are paired
 * in order as changes (a 和资料不同 note is never paired: it is a new line of its own), the rest are dropped / added.
 */
export function paraDiff(oldP: readonly string[], newP: readonly string[]): ParaOp[] {
  const key = (s: string): string => s.replace(/\s+/g, "");
  const out: ParaOp[] = [];
  let dels: number[] = [], inss: number[] = [];
  const flush = (): void => {
    const pairable = inss.filter((j) => noteText(newP[j]!) === null);
    const k = Math.min(dels.length, pairable.length);
    const paired = new Set(pairable.slice(0, k));
    // in new-text order: a change at its new paragraph, the extra additions where they are; drops before both
    for (const i of dels.slice(k)) out.push({ kind: "del", i });
    let d = 0;
    for (const j of inss) out.push(paired.has(j) ? { kind: "change", i: dels[d++]!, j } : { kind: "ins", j });
    dels = []; inss = [];
  };
  // keys computed once; past the cap (undefined) nothing is kept equal: all old are dropped and paired with the new in order
  const changes = diffArrays(oldP.map(key), newP.map(key), { maxEditLength: PARA_DIFF_MAX_EDITS, timeout: WORD_DIFF_TIMEOUT_MS })
    ?? [{ removed: true, added: false, count: oldP.length, value: [] }, { removed: false, added: true, count: newP.length, value: [] }];
  let i = 0, j = 0;
  for (const c of changes) {
    if (c.removed) { for (let x = 0; x < c.count; x++) dels.push(i++); } else if (c.added) { for (let x = 0; x < c.count; x++) inss.push(j++); } else {
      flush();
      for (let x = 0; x < c.count; x++) out.push({ kind: "same", i: i++, j: j++ });
    }
  }
  flush();
  return out;
}

// ---- where the marks go ----

/**
 * One marked element: a heading (its diff ops), a paragraph (the note Markdown before / after: `old` null = added, `new`
 * null = dropped, both equal = kept as it is) or a 和资料不同 note (`added`: a new line).
 */
export type TrackPart = { kind: "h2"; ops: DiffOp[] } | { kind: "p"; old: string | null; new: string | null } | { kind: "note"; text: string; added: boolean };
/** Note lines (0-based): `replace` hides lines [from, to] and shows the parts there; `after` shows them after `line`. */
export type Anchor = { kind: "replace"; from: number; to: number } | { kind: "after"; line: number };
export interface TrackPiece { at: Anchor; parts: TrackPart[] }
export interface RevisePlan {
  actionId: string; pageId: string; revise: ReviseOut; why: string;
  /** Who asked for the draft (the ?'s default reason). */
  source: ActionOut["source"];
  pieces: TrackPiece[];
}

/** A note paragraph: its lines [from, to] and its text. */
interface Block { from: number; to: number; text: string }

function blocksIn(lines: readonly string[], from: number, to: number): Block[] {
  const out: Block[] = [];
  let start = -1;
  const scan = new FenceScanner();
  const end = (k: number): void => { if (start >= 0) out.push({ from: start, to: k, text: lines.slice(start, k + 1).join("\n") }); start = -1; };
  for (let k = from; k < to; k++) {
    const line = lines[k]!;
    const kind = scan.step(line);
    if (kind === "open") { if (start < 0) start = k; continue; }
    if (kind !== "text") continue;
    if (line.trim() === "") { end(k - 1); continue; }
    if (start < 0) start = k;
  }
  end(to - 1);
  return out;
}

/** Character-bigram Dice similarity of the shown texts (whitespace and escapes ignored). */
function similar(a: string, b: string): number {
  const norm = (s: string): string[] => [...shownText(s).replace(/\s+/g, "")];
  const x = norm(a), y = norm(b);
  if (x.join("") === y.join("")) return 1;
  if (x.length < 2 || y.length < 2) return 0;
  const grams = (c: string[]): Map<string, number> => {
    const g = new Map<string, number>();
    for (let k = 0; k + 1 < c.length; k++) { const t = c[k]! + c[k + 1]!; g.set(t, (g.get(t) ?? 0) + 1); }
    return g;
  };
  const gx = grams(x), gy = grams(y);
  let both = 0;
  for (const [t, c] of gx) both += Math.min(c, gy.get(t) ?? 0);
  return (2 * both) / (x.length - 1 + y.length - 1);
}
const MATCH = 0.6;
const INTERACTIVE = /^〔互动[：:]/;

/**
 * Where each section of the open page is in the note: `from` is its own `##` line (for the opening section, which has
 * none, the line before its text: `headless`), `to` where it ends.
 */
type SectionRange = { from: number; to: number; headless: boolean };
function sectionRanges(s: WikiState, text: string, lines: readonly string[]): Map<string, SectionRange> | null {
  const nh = noteHeadings(text);
  if (nh === null) return null;
  const start = lines.indexOf(WIKI_START), end = lines.indexOf(WIKI_END, start + 1);
  if (start < 0 || end < 0) return null;
  const heads = [...nh.byLine.entries()].sort((a, b) => a[0] - b[0]).map(([line, ref]) => ({ line, sid: matchSection(s, ref)?.id ?? null, related: ref.heading === "相关页面" }));
  const out = new Map<string, SectionRange>();
  // a section without its own heading (a check, a folded callout) after this one ends it too
  const cut = (from: number, to: number): number => {
    for (let q = from + 1; q < to; q++) if (/^```evowiki-check\b/.test(lines[q]!) || /^> \[!evowiki-known\]/.test(lines[q]!)) return q;
    return to;
  };
  heads.forEach((h, k) => {
    if (h.sid === null) return;
    let to = end;
    for (const x of heads.slice(k + 1)) if (x.sid !== null || x.related) { to = x.line; break; }
    if (!out.has(h.sid)) out.set(h.sid, { from: h.line, to: cut(h.line, to), headless: false });
  });
  // the page's opening section (note/format.ts sectionLines: an organizer is its text, no heading) is found by its own
  // lines as the note writer puts them, before the first section heading; `from` is the line before them. Not found
  // (edited by hand): no marks, never a guess at a wrong place
  const p = s.page;
  const first = p.kind === "open" ? p.page.sections[0] : undefined;
  if (first !== undefined && first.role === "organizer" && !first.folded && !out.has(first.id) && first.body.trim() !== "") {
    const own = noteBlock(first.body, p.kind === "open" ? p.page.sources ?? [] : []).split("\n");
    const stop = heads.find((x) => x.sid !== null || x.related)?.line ?? end;
    for (let k = start + 1; k + own.length <= stop; k++) {
      if (own.every((l, q) => lines[k + q] === l)) { out.set(first.id, { from: k - 1, to: k + own.length, headless: true }); break; }
    }
  }
  return out;
}

const lastFilled = (lines: readonly string[], from: number, to: number): number => {
  let k = to - 1;
  while (k > from && lines[k]!.trim() === "") k--;
  return k;
};

/** The materials a citation can name: the page's own (the old text), plus the draft's (the new text; a new one has no link yet). */
interface Cites { old: PageSourceOut[]; new: PageSourceOut[] }
function citesOf(s: WikiState, r: ReviseOut): Cites {
  const own = s.page.kind === "open" ? s.page.page.sources ?? [] : [];
  const extra = r.sources.filter((x) => !own.some((o) => o.id === x.source_id))
    .map((x): PageSourceOut => ({ id: x.source_id, title: x.title, current_title: x.title, kind: "text", origin: null, deleted: x.deleted }));
  return { old: own, new: [...own, ...extra] };
}

/** A changed paragraph: the note Markdown of both texts. */
const changePart = (a: string, b: string, c: Cites): TrackPart => ({ kind: "p", old: noteBlock(a, c.old), new: noteBlock(b, c.new) });
/** A paragraph shown whole: dropped (`-`), added (`+`) or as it is (`=`); a 和资料不同 note keeps its own look. */
function wholePart(text: string, k: DiffOp[0], c: Cites): TrackPart {
  const note = noteText(text);
  if (note !== null) return { kind: "note", text: note, added: k === "+" };
  if (k === "+") return { kind: "p", old: null, new: noteBlock(text, c.new) };
  const md = noteBlock(text, c.old);
  return { kind: "p", old: md, new: k === "-" ? null : md };
}

/**
 * Where the marks of the ready draft go in this note text; null when the page is not in review mode, or the note is not
 * the open page's wiki note.
 */
export function revisePlan(s: WikiState, pageId: string, text: string): RevisePlan | null {
  const row = pendingRevise(s, pageId);
  const p = s.page;
  if (row === null || p.kind !== "open" || p.page.page_id !== pageId) return null;
  const lines = text.split(/\r\n?|\n/);
  const ranges = sectionRanges(s, text, lines);
  if (ranges === null) return null;
  const start = lines.indexOf(WIKI_START), end = lines.indexOf(WIKI_END, start + 1);
  const tail = lastFilled(lines, start, end);
  const cites = citesOf(s, row.revise);
  const pieces: TrackPiece[] = [];
  const put = (at: Anchor, parts: TrackPart[]): void => {
    if (parts.length === 0) return;
    if (at.kind === "after") {
      const same = pieces.find((x) => (x.at.kind === "after" ? x.at.line : x.at.to) === at.line);
      if (same) { same.parts.push(...parts); return; }
    }
    pieces.push({ at, parts });
  };
  let prevEnd: number | null = null;
  for (const c of row.revise.sections) {
    const r = ranges.get(c.section_id);
    if (c.status === "added" || r === undefined) {
      if (c.status === "added") {
        const parts: TrackPart[] = [{ kind: "h2", ops: [["+", c.heading]] }, ...paragraphs(c.new_body).map((x) => wholePart(x, "+", cites))];
        put({ kind: "after", line: prevEnd ?? tail }, parts);
      }
      continue;          // a section the note does not show (folded, edited away): no marks, the bar still counts it
    }
    const last = lastFilled(lines, r.from, r.to);
    prevEnd = last;
    if (c.status === "removed") {
      const body = paragraphs(c.old_body).map((x) => wholePart(x, "-", cites));
      if (r.headless) put({ kind: "replace", from: r.from + 1, to: last }, body);
      else put({ kind: "replace", from: r.from, to: last }, [{ kind: "h2", ops: [["-", c.old_heading ?? c.heading]] }, ...body]);
      continue;
    }
    sectionPieces(c, lines, r, last, cites, put);
  }
  const sorted = pieces.sort((a, b) => (a.at.kind === "after" ? a.at.line + 0.5 : a.at.from) - (b.at.kind === "after" ? b.at.line + 0.5 : b.at.from));
  return { actionId: row.action_id, pageId, revise: row.revise, why: row.why, source: row.source, pieces: sorted };
}

/** The marks of one changed section: its heading if renamed, then each changed / dropped / added paragraph in place. */
function sectionPieces(c: RevisedSectionOut, lines: readonly string[], r: SectionRange, last: number, cites: Cites,
  put: (at: Anchor, parts: TrackPart[]) => void): void {
  const from = r.from;
  // the opening section's heading is not in the note: a renamed one has nothing to mark
  const heading = !r.headless && c.old_heading !== null && c.old_heading !== c.heading;
  const oldP = paragraphs(c.old_body), newP = paragraphs(c.new_body);
  const ops = paraDiff(oldP, newP);
  const blocks = blocksIn(lines, from + 1, last + 1);
  // each old paragraph the draft changes or drops: the note's paragraph that says it (in order)
  const at = new Map<number, Block>();
  let cursor = 0;
  for (let i = 0; i < oldP.length; i++) {
    const kept = ops.some((o) => o.kind === "same" && o.i === i);
    let best = -1, score = MATCH;
    for (let k = cursor; k < blocks.length; k++) {
      const b = blocks[k]!;
      const v = INTERACTIVE.test(oldP[i]!) ? (/^ {0,3}(`{3,}|~{3,})/.test(b.text) ? MATCH : 0) : similar(oldP[i]!, b.text);
      if (v > score || (v === 1 && score < 1)) { best = k; score = v; if (v === 1) break; }
    }
    if (best >= 0) { at.set(i, blocks[best]!); cursor = best + 1; } else if (!kept) { at.clear(); break; }
  }
  const touched = ops.filter((o) => o.kind === "change" || o.kind === "del");
  // an added paragraph goes after the paragraph before it in the draft: when that one (a kept paragraph) was not found in
  // the note, its place is not known for sure, so the section is shown whole instead
  let gap = false, unsure = false;
  for (const o of ops) {
    if (o.kind === "same") gap = !at.has(o.i);
    else if (o.kind === "ins") unsure ||= gap;
    else gap = false;
  }
  const placed = !unsure && touched.every((o) => at.has(o.i));
  const head: TrackPart[] = heading ? [{ kind: "h2", ops: wordDiff(c.old_heading!, c.heading) }] : [];
  if (!placed) {
    // the note's text was changed by hand: the whole section in marked text (heading kept unless renamed)
    const parts: TrackPart[] = ops.map((o): TrackPart => o.kind === "same" ? wholePart(oldP[o.i]!, "=", cites)
      : o.kind === "del" ? wholePart(oldP[o.i]!, "-", cites) : o.kind === "ins" ? wholePart(newP[o.j]!, "+", cites)
        : changePart(oldP[o.i]!, newP[o.j]!, cites));
    if (heading) put({ kind: "replace", from, to: last }, [...head, ...parts]);
    else if (last > from) put({ kind: "replace", from: from + 1, to: last }, parts);
    else put({ kind: "after", line: from }, parts);
    return;
  }
  if (heading) put({ kind: "replace", from, to: from }, head);
  let prev = from;
  for (const o of ops) {
    if (o.kind === "same") { const b = at.get(o.i); if (b) prev = b.to; continue; }
    if (o.kind === "ins") { put({ kind: "after", line: prev }, [wholePart(newP[o.j]!, "+", cites)]); continue; }
    const b = at.get(o.i)!;
    const part = o.kind === "del" ? wholePart(oldP[o.i]!, "-", cites) : changePart(oldP[o.i]!, newP[o.j]!, cites);
    put({ kind: "replace", from: b.from, to: b.to }, [part]);
    prev = b.to;
  }
}
