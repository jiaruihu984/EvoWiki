/**
 * Track-change marks on rendered Markdown (materials D, UI principles 2026-10-21 §三; live check 2026-10-21 V3 / V4).
 *
 * A changed paragraph is rendered twice the way the note itself is rendered (Obsidian's MarkdownRenderer on the note
 * Markdown model.ts made with the note writer's rules: citations, 和资料不同 strips, escapes), once for the old text and
 * once for the new one. The words are then compared on the rendered text (model.ts wordDiff) and the marks go onto the
 * new rendering's text nodes: added runs wrapped in `<ins>`, dropped runs put back as `<del>` where they were. So list
 * items, bold, inline code, links and inline math keep their look inside review mode.
 *
 * Inline things whose text is not the reader's text (math, embeds, pills) count as one token each: equal when their
 * rendering is equal, else dropped + added whole. A code block's text is the source's text, so it is marked like any
 * other text, inside the block (its monospace and grey box stay; only `-` → `−` is marked when that is all that changed),
 * as long as both renderings have the same number of code blocks (a mark never runs from one block into another). A
 * paragraph holding a block that cannot be marked word by word (display math, whose rendering is not its source; a
 * table, a callout, an embed; or code blocks added / dropped) is shown as the old block struck through, then the new
 * block as an added block. Either way, two renderings that read the same but for whitespace are shown unmarked (as
 * model.ts paraDiff pairs paragraphs and the backend counts sections). Display only: nothing here writes the note.
 */
import { el } from "../../ui/ew";
import { type DiffOp, wordDiff } from "./model";

/** Renders note Markdown into `into` (main.ts: MarkdownRenderer.render with the view as owner). */
export type MdRender = (md: string, into: HTMLElement) => Promise<void>;

/** Inline elements compared as one token. */
const ATOM = ".math, mjx-container, img, svg, iframe, input, button, .internal-embed, .evowiki-unverified, .evowiki-differs";
/** Blocks that cannot carry word marks: the whole paragraph is shown old-struck / new-added. */
const BLOCK = "table, .math-block, mjx-container[display='true'], .callout, iframe, .internal-embed, .evowiki-embed, hr";
/** Code blocks: marked inside, character by character, when old and new have as many. */
const CODE = "pre";

type Tok = { text: Text; ch: string } | { atom: Element; ch: string };

/**
 * The rendered text as one string (an atom is one private-use character, the same one for an equal rendering; `key` says
 * what "equal" is: its HTML by default).
 */
function tokens(root: Node, atoms: Map<string, string>, key: (e: Element) => string = (e) => e.outerHTML): { str: string; toks: Tok[] } {
  const toks: Tok[] = [];
  const walk = (n: Node): void => {
    for (const c of [...n.childNodes]) {
      if (c.nodeType === Node.TEXT_NODE) {
        const t = (c as Text).data;
        // the line breaks between block elements are not text the reader sees
        if (t.trim() === "" && t.includes("\n")) continue;
        for (const ch of t) toks.push({ text: c as Text, ch });
      } else if (c instanceof Element) {
        if (c.matches(ATOM)) {
          const k = key(c);
          let ch = atoms.get(k);
          if (ch === undefined) { ch = String.fromCodePoint(0xe000 + atoms.size); atoms.set(k, ch); }
          toks.push({ atom: c, ch });
        } else walk(c);
      }
    }
  };
  walk(root);
  return { str: toks.map((x) => x.ch).join(""), toks };
}

/** A `<del>` holding dropped tokens: their text, and copies of dropped atoms. */
function delOf(toks: readonly Tok[]): HTMLElement {
  const d = document.createElement("del");
  let run = "";
  for (const t of toks) {
    if ("text" in t) { run += t.ch; continue; }
    if (run) { d.append(run); run = ""; }
    d.append(t.atom.cloneNode(true));
  }
  if (run) d.append(run);
  return d;
}

/**
 * Puts the marks of old → new onto `next` (the new text's rendering), in place. `prev` is only read. Returns the diff
 * of the rendered texts (atoms as private-use characters).
 */
export function markRendered(prev: HTMLElement, next: HTMLElement): DiffOp[] {
  const atoms = new Map<string, string>();
  const a = tokens(prev, atoms), b = tokens(next, atoms);
  const ops = wordDiff(a.str, b.str);
  const added = new Set<number>();
  const dropped = new Map<number, Tok[]>();
  let i = 0, j = 0;
  for (const [k, t] of ops) {
    const n = [...t].length;
    if (k === "=") { i += n; j += n; } else if (k === "+") { for (let q = 0; q < n; q++) added.add(j + q); j += n; } else {
      dropped.set(j, [...(dropped.get(j) ?? []), ...a.toks.slice(i, i + n)]);
      i += n;
    }
  }
  // dropped text after the last new token: right after it (or into the empty rendering)
  const tail = dropped.get(b.toks.length);
  if (tail !== undefined) {
    const last = b.toks[b.toks.length - 1];
    if (last === undefined) (next.lastElementChild ?? next).append(delOf(tail));
    else ("text" in last ? last.text : last.atom).after(delOf(tail));
  }
  // each text node is rebuilt from its tokens; an atom gets its dropped run before it and an <ins> around it
  let k = 0;
  while (k < b.toks.length) {
    const t = b.toks[k]!;
    if (!("text" in t)) {
      const d = dropped.get(k);
      if (d !== undefined) t.atom.before(delOf(d));
      if (added.has(k)) { const w = document.createElement("ins"); t.atom.replaceWith(w); w.append(t.atom); }
      k++;
      continue;
    }
    const node = t.text;
    const frag = document.createDocumentFragment();
    let run = "", ins = false;
    const flush = (): void => {
      if (!run) return;
      if (ins) { const w = document.createElement("ins"); w.textContent = run; frag.append(w); } else frag.append(run);
      run = "";
    };
    for (; k < b.toks.length; k++) {
      const x = b.toks[k]!;
      if (!("text" in x) || x.text !== node) break;
      const d = dropped.get(k);
      if (d !== undefined) { flush(); frag.append(delOf(d)); }
      if (added.has(k) !== ins) { flush(); ins = added.has(k); }
      run += x.ch;
    }
    flush();
    node.replaceWith(frag);
  }
  return ops;
}

/** A whole rendered paragraph as dropped (`<del class=ew-delblk>`) or added (`<ins class=ew-insblk>`). */
function wholeBlock(kind: "del" | "ins", r: HTMLElement): HTMLElement {
  const w = el(kind, kind === "del" ? "delblk" : "insblk");
  w.append(...r.childNodes);
  return w;
}

/**
 * The two renderings read the same but for whitespace (a re-indented code block, a formula or table written with other
 * spacing): the rule the paragraphs are paired by (model.ts paraDiff) and the backend counts sections by (bot/diffs.py
 * same_text). Atoms (a formula, an embed) compare by their rendering, whitespace ignored too.
 */
function sameShown(prev: HTMLElement, next: HTMLElement): boolean {
  const atoms = new Map<string, string>();
  const flat = (r: HTMLElement): string => tokens(r, atoms, (e) => e.outerHTML.replace(/\s+/g, "")).str.replace(/\s+/g, "");
  return flat(prev) === flat(next);
}

/**
 * The marked nodes of one paragraph from its two renderings (null: no such text): unmarked when they differ only in
 * whitespace; old struck + new added when a block cannot be marked inside; else marks on the new rendering's text.
 */
export function markedNodes(prev: HTMLElement | null, next: HTMLElement | null, same: boolean): Node[] {
  if (next === null) return prev === null ? [] : [wholeBlock("del", prev)];
  if (prev === null) return [wholeBlock("ins", next)];
  if (same || sameShown(prev, next)) return [...next.childNodes];
  const whole = prev.querySelector(BLOCK) !== null || next.querySelector(BLOCK) !== null
    || prev.querySelectorAll(CODE).length !== next.querySelectorAll(CODE).length;
  if (whole) return [wholeBlock("del", prev), wholeBlock("ins", next)];
  markRendered(prev, next);
  return [...next.childNodes];
}

/**
 * `div.ew-trk.evowiki-trk-md` for one paragraph part: filled once both texts are rendered. When rendering fails the
 * paragraph is shown as its marked plain text (never the old text alone).
 */
export function renderedPart(prev: string | null, next: string | null, render: MdRender): HTMLElement {
  const box = el("div", "trk");
  box.classList.add("evowiki-trk-md");
  const one = (md: string | null): Promise<HTMLElement | null> => {
    if (md === null) return Promise.resolve(null);
    const d = document.createElement("div");
    return render(md, d).then(() => d);
  };
  void Promise.all([one(prev), one(next)]).then(([a, b]) => { box.replaceChildren(...markedNodes(a, b, prev === next)); }, () => {
    const p = el("p");
    for (const [k, t] of wordDiff(prev ?? "", next ?? "")) p.append(k === "=" ? document.createTextNode(t) : el(k === "-" ? "del" : "ins", "", t));
    box.replaceChildren(p);
  });
  return box;
}
