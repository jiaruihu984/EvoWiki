/**
 * A hand edit in a wiki note, read back into sections (user ruling 2026-11-04: 「手改内容和那一段直接不改，进正文」). Pure: the
 * caller reads the note and its written copy and sends the result to POST /wiki/pages/{id}/hand-edit.
 *
 * The note's block is diffed line by line against what was written, and every line lands in a section: a kept line in
 * its own, a typed line in the section of the old line before it. What that means for each edge:
 * - a section changed: its text goes up twice, `note_body` exactly as typed (the note shows this, so nothing in the
 *   note moves) and `body` for everything else that reads it: unchanged lines are the written source lines byte for
 *   byte, changed lines are converted back (format.ts fromNoteBlock);
 * - a new `##` heading typed inside a section stays that section's text, under its own heading;
 * - a section's heading line deleted (so also a selection deleted across two sections): what is left of it joins the
 *   section before it, which is what the note now shows; the first section has none to join and fails closed;
 * - all of a section's text deleted, or the whole section: the section is deleted from the page (撤销 brings it back);
 * - a typed line above the first section goes to the start of the first section; EvoWiki's own hint lines there (这页
 *   改了什么) are not page text: deleting them does nothing, they stay until the page drops them.
 *
 * Fails closed (`unsafe`, the note stays as typed, nothing is sent): an interactive, image or check block changed in
 * part, a fence left open (it would swallow the rest of the note), a check section changed, a heading or recall cue that
 * would not read back the same, or a section whose written source is unknown (a copy from before sources were kept).
 */
import { diffArrays } from "diff";
import { isGoalCheck } from "../goal-checks";
import { CHECK_BLOCK, fromNoteBlock, KNOWN_CALLOUT, noteBlock, normalizeBody, oneLineMarkdown, parseWikiFile, wikiHash } from "./format";
import { editSummary, regionOf, type Written, type WrittenSection } from "./edit-diff";
import { FenceScanner } from "./fences";
import type { HandEditIn, HandEditSectionIn, PageSourceOut } from "../schema";

export type UnsafeWhy = "block" | "fence" | "check" | "heading" | "cue" | "first" | "source" | "all" | "large";

export type HandEditPlan =
  | { kind: "none" }
  | { kind: "ok"; edit: HandEditIn }
  | { kind: "unsafe"; why: UnsafeWhy; label: string };

/** The longest recall cue the server keeps (SectionText.cue). */
const MAX_CUE = 60;
const DIFF_MAX_EDITS = 6000;
/** The fences that point at something stored (embed-block.ts, evoui/block.ts, image-block.ts; named here: this module also runs outside the app, scripts/demo-vault). */
const PROTECTED = new Set(["embedded-html", "evoui", "evowiki-image", CHECK_BLOCK]);

const blank = (l: string): boolean => l.trim() === "";
function trimmed(lines: readonly string[]): string[] {
  let a = 0, b = lines.length;
  while (a < b && blank(lines[a]!)) a++;
  while (b > a && blank(lines[b - 1]!)) b--;
  return lines.slice(a, b);
}

/** The protected blocks of note lines, each as its exact text; null when a fence stays open. */
function protectedBlocks(lines: readonly string[]): string[] | null {
  const scan = new FenceScanner();
  const out: string[] = [];
  let cur: string[] | null = null;
  for (const l of lines) {
    const k = scan.step(l);
    if (k === "open") cur = PROTECTED.has(l.trim().replace(/^[`~]+/, "").trim()) ? [l] : null;
    else if (cur !== null) {
      cur.push(l);
      if (k === "close") { out.push(cur.join("\n")); cur = null; }
    }
  }
  return scan.fence === null ? out : null;
}

/** Each kept and typed line of the new body, by region: index 0 is above the first section, k + 1 is section k. */
function aligned(before: string[], after: string[], w: Written): { kept: string[][]; typedAbove: string[] } | null {
  const changes = diffArrays(before, after, { maxEditLength: DIFF_MAX_EDITS, timeout: 2000 });
  if (changes === undefined || changes === null) return null;
  const n = w.layout.sections.length;
  const kept: string[][] = Array.from({ length: n + 1 }, () => []);
  const typedAbove: string[] = [];
  const owner = (i: number): number => { const r = regionOf(w.layout, i); return r.kind === "start" ? 0 : r.index + 1; };
  let i = 0;
  for (const c of changes) {
    if (c.removed) { i += c.count; continue; }
    if (c.added) {
      const at = i === 0 ? 0 : owner(i - 1);
      if (at === 0) typedAbove.push(...c.value);
      else kept[at]!.push(...c.value);
      continue;
    }
    for (const l of c.value) { kept[owner(i)]!.push(l); i++; }
  }
  return { kept, typedAbove };
}

const CALLOUT = new RegExp(`^> \\[!${KNOWN_CALLOUT}\\][-+]? ?(.*)$`);
const unquoted = (l: string): string => l.replace(/^> ?/, "");

interface Parsed { heading?: string; title?: string; text: string[]; open?: boolean }

/** The section's parts as it now stands in the note, by the form it was written in; null when that form is gone. */
function parsed(lines: string[], first: string): Parsed | null {
  if (first.startsWith(`> [!${KNOWN_CALLOUT}]`)) {
    const m = CALLOUT.exec(lines[0]!);
    if (m === null) return null;
    const rest = lines.slice(1);
    return { title: m[1]!.trim(), text: rest.map((l) => (l.startsWith(">") ? unquoted(l) : l)), open: rest.some((l) => !blank(l) && !l.startsWith(">")) };
  }
  if (first.startsWith("## ")) {
    const m = /^## +(.+)$/.exec(lines[0]!);
    return m === null ? null : { heading: m[1]!.trim(), text: lines.slice(1) };
  }
  if (first.startsWith("- **")) {
    const m = /^- \*\*(.+?)\*\*：(.*)$/.exec(lines[0]!);
    return m === null ? null : { heading: m[1]!, text: [m[2]!, ...lines.slice(1)] };
  }
  return { text: lines };
}

/** The reading text of `note`: a line the page had is its source line, byte for byte; any other is converted back. */
function readingBody(note: string, sections: readonly WrittenSection[], sources: PageSourceOut[]): string {
  const back = new Map<string, string[]>();
  for (const s of sections) {
    const shown = (s.src.noteBody ?? noteBlock(s.src.body, sources)).split("\n");
    const source = s.src.body.replace(/\n+$/, "").split("\n");
    if (shown.length !== source.length) continue;
    shown.forEach((l, i) => { const q = back.get(l); if (q) q.push(source[i]!); else back.set(l, [source[i]!]); });
  }
  const body = fromNoteBlock(note, sources).split("\n");
  return note.split("\n").map((l, i) => back.get(l)?.shift() ?? body[i]!).join("\n");
}

/** The plan for `text` (a note claiming written.pageId, written as `written`), read against what was written. */
export function planHandEdit(text: string, written: Written, sources: PageSourceOut[] = []): HandEditPlan {
  const p = parseWikiFile(text);
  if (p.kind !== "one" || p.version !== 2 || p.pageId !== written.pageId || p.hash !== written.hash) return { kind: "none" };
  if (wikiHash(p.pageId, p.body) === p.hash) return { kind: "none" };
  const before = normalizeBody(written.body).split("\n");
  const after = normalizeBody(p.body).split("\n");
  const al = aligned(before, after, written);
  if (al === null) return { kind: "unsafe", why: "large", label: "" };
  const secs = written.layout.sections;
  const known = new Set<string>();
  for (const s of secs) {
    const blocks = s.src ? protectedBlocks((s.src.noteBody ?? noteBlock(s.src.body, sources)).split("\n")) : null;
    for (const b of blocks ?? []) known.add(b);
    // a check section's block is written from its ids, not its text
    if (s.src?.role === "check") known.add(before.slice(s.from, s.to).filter((l) => !blank(l)).join("\n"));
  }
  /** The sections still on the page, in order, with their text as it now stands. */
  interface Part { s: WrittenSection; p: Parsed; text: string[]; changed: boolean }
  const parts: Part[] = [];
  const removed: string[] = [];
  const typedAbove = trimmed(al.typedAbove);
  for (const [k, s] of secs.entries()) {
    if (s.src !== undefined && isGoalCheck({ id: s.id, role: s.src.role })) continue;
    const old = trimmed(before.slice(s.from, s.to));
    const now = trimmed(al.kept[k + 1]!);
    if (now.length === 0) { removed.push(s.id); continue; }
    const same = now.length === old.length && now.every((l, i) => l === old[i]);
    if (s.src === undefined) {
      if (same) { parts.push({ s, p: { text: now }, text: now, changed: false }); continue; }
      return { kind: "unsafe", why: "source", label: s.label };
    }
    if (s.src.role === "check") {
      if (!same) return { kind: "unsafe", why: "check", label: s.label };
      parts.push({ s, p: { text: now }, text: now, changed: false });
      continue;
    }
    const form = parsed(now, old[0]!);
    if (form !== null) {
      const body = trimmed(form.text);
      // only a heading or cue is left: all of the text was deleted
      if (body.length === 0) { removed.push(s.id); continue; }
      parts.push({ s, p: form, text: body, changed: !same });
      continue;
    }
    const prev = [...parts].reverse().find((x) => x.s.src?.role !== "check" && x.s.src !== undefined);
    if (prev === undefined) return { kind: "unsafe", why: "first", label: s.label };
    prev.text = [...prev.text, "", ...now];
    prev.changed = true;
    removed.push(s.id);
  }
  if (typedAbove.length > 0) {
    const head = parts.find((x) => x.s.src !== undefined && x.s.src.role !== "check");
    if (head === undefined) return { kind: "unsafe", why: "first", label: "" };
    head.text = [...typedAbove, "", ...head.text];
    head.changed = true;
  }
  if (parts.length === 0) return { kind: "unsafe", why: "all", label: "" };
  const changed = parts.filter((x) => x.changed);
  if (changed.length === 0 && removed.length === 0) return { kind: "none" };
  const sections: HandEditSectionIn[] = [];
  for (const { s, p: form, text: lines } of changed) {
    const src = s.src;
    const blocks = protectedBlocks(lines);
    if (blocks === null) return { kind: "unsafe", why: "fence", label: s.label };
    if (blocks.some((b) => !known.has(b))) return { kind: "unsafe", why: "block", label: s.label };
    const note = lines.join("\n");
    const out: HandEditSectionIn = { section_id: s.id, note_body: note, body: readingBody(note, secs.filter((x) => x.src !== undefined), sources) };
    if (form.heading !== undefined && form.heading !== oneLineMarkdown(src.heading)) {
      const h = fromNoteBlock(form.heading, sources);
      if (oneLineMarkdown(h) !== form.heading) return { kind: "unsafe", why: "heading", label: s.label };
      out.heading = h;
    }
    if (form.title !== undefined && form.title !== (oneLineMarkdown(src.cue) || oneLineMarkdown(src.heading))) {
      const c = fromNoteBlock(form.title, sources);
      if (oneLineMarkdown(c) !== form.title || c.length > MAX_CUE) return { kind: "unsafe", why: "cue", label: s.label };
      out.cue = c;
    }
    if (form.open === true) out.folded = false;
    if (src.fingerprint) out.fingerprint = src.fingerprint;
    sections.push(out);
  }
  const deletions = [];
  for (const id of removed) {
    const s = secs.find((x) => x.id === id)!;
    if (!s.src?.fingerprint) return { kind: "unsafe", why: "source", label: s.label };
    deletions.push({ section_id: id, fingerprint: s.src.fingerprint });
  }
  return { kind: "ok", edit: { sections, removed: deletions, summary: editSummary(before, after, written.layout) } };
}
