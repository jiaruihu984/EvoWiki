/**
 * The 看改动 of a page-edit proposal: its old and new text (ProposalChange) as lines. A paragraph kept shows as context, one
 * dropped or added whole as a del / add line, one changed as a single line with its deletions and additions marked (when too
 * little of it stays to read that way, as a del line and an add line). The paragraph and character diffs are review mode's
 * (wiki/revise/model.ts, jsdiff). Pure.
 */
import { paragraphs, paraDiff, wordDiff, type DiffOp } from "../wiki/revise/model";
import type { ProposalChange } from "./types";

export type ChangeLine =
  | { kind: "ctx" | "add" | "del"; text: string }
  | { kind: "mod"; ops: DiffOp[] };

export interface ChangeView { lines: ChangeLine[]; removed: number; added: number }

/** A kept paragraph is context only: this many characters of it are shown. */
const CTX_CHARS = 40;
/** Below this share of its characters kept, a changed paragraph is shown as one dropped and one added. */
const MARKED_MIN = 0.3;

const size = (s: string): number => [...s.replace(/\s+/g, "")].length;

export function changeView(c: ProposalChange): ChangeView {
  const lines: ChangeLine[] = [];
  let removed = 0, added = 0;
  const del = (text: string): void => { lines.push({ kind: "del", text }); removed += size(text); };
  const add = (text: string): void => { lines.push({ kind: "add", text }); added += size(text); };
  const oldP = paragraphs(c.old_body), newP = paragraphs(c.new_body);
  // paraDiff puts the paragraphs dropped between two kept ones first; they are the later ones of the old text: read in its order
  const ops = paraDiff(oldP, newP);
  const inOrder: typeof ops = [];
  let dropped: typeof ops = [];
  for (const o of ops) {
    if (o.kind === "del") dropped.push(o);
    else if (o.kind === "same") { inOrder.push(...dropped, o); dropped = []; } else inOrder.push(o);
  }
  inOrder.push(...dropped);
  for (const o of inOrder) {
    if (o.kind === "same") {
      const chars = [...oldP[o.i]!];
      lines.push({ kind: "ctx", text: chars.length > CTX_CHARS ? `${chars.slice(0, CTX_CHARS).join("")}…` : oldP[o.i]! });
    } else if (o.kind === "del") del(oldP[o.i]!);
    else if (o.kind === "ins") add(newP[o.j]!);
    else {
      const a = oldP[o.i]!, b = newP[o.j]!;
      const ops = wordDiff(a, b);
      const kept = ops.reduce((n, [k, t]) => (k === "=" ? n + size(t) : n), 0);
      if (kept >= MARKED_MIN * Math.max(size(a), size(b))) {
        lines.push({ kind: "mod", ops });
        for (const [k, t] of ops) { if (k === "-") removed += size(t); else if (k === "+") added += size(t); }
      } else { del(a); add(b); }
    }
  }
  return { lines, removed, added };
}
