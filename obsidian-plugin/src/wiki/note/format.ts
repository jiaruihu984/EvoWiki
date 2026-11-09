import { tr, onLanguageChange } from "../../i18n";
import { createHash } from "node:crypto";
import {
  entryList, entryString, type FmEntry, type Frontmatter, listEntry, parseScalar, scalarEntry, serializeFrontmatter,
  splitFrontmatter,
} from "./frontmatter";
import { FenceScanner } from "./fences";
import type { WikiNoteInput, WikiNoteSection } from "./types";
import type { PageSourceOut } from "../schema";
import { localDayKey } from "../../ui/dates";
import { isGoalCheck } from "../goal-checks";

/**
 * Wiki note format v2. The machine fields live in frontmatter properties; the generated block sits between two Obsidian
 * comments, which neither Live Preview nor Reading view shows:
 *
 *   ---
 *   目标: "…"                      ← shown as properties (Chinese keys only; styles.css hides the rest)
 *   折起: "2 段已掌握的"
 *   更新于: "2026-10-09 10:42"
 *   evowiki_title: "…"             ← the page title, drawn by the plugin as the note's h1 (not hashed, like the others)
 *   evowiki_page_id: "…"
 *   evowiki_format: "evowiki-wiki-note/2"
 *   evowiki_hash: "sha256:…"
 *   cssclasses:
 *     - evowiki-page
 *   ---
 *   %% evowiki:start %%
 *   …generated body…
 *   %% evowiki:end %%
 *
 *   ## 我的笔记
 *
 * R1.2: the hash covers the body between the markers (normalized), the page id and the format; properties are not
 * hashed, so the user may add tags or other keys. v1 notes (HTML-comment header) are still read, claimed and, when
 * unedited, migrated to v2 on the next sync.
 */
export const WIKI_START = "%% evowiki:start %%";
export const WIKI_END = "%% evowiki:end %%";
export const WIKI_FORMAT = "evowiki-wiki-note/2";
export const WIKI_FORMAT_V1 = "evowiki-wiki-note/1";
export const PAGE_CSS_CLASS = "evowiki-page";
export const MINE_HEADING = "## 我的笔记";
export const isMineHeading = (line: string): boolean => /^##\s+(?:我的笔记|My notes)\s*$/.test(line);
export let MINE_HINT = `<span class="evowiki-mine-hint">${tr("note.mineHint")}</span>`;
onLanguageChange(() => { MINE_HINT = `<span class="evowiki-mine-hint">${tr("note.mineHint")}</span>`; });
/** Frontmatter keys this plugin writes (R1.1). Every other key belongs to the user and is kept. */
export const FM_KEYS = {
  goal: "目标", folded: "折起", updated: "更新于", title: "evowiki_title",
  pageId: "evowiki_page_id", format: "evowiki_format", hash: "evowiki_hash", css: "cssclasses",
  /** "writing" while the page's sections stream in; absent once it is written (Task 13). */
  status: "evowiki-status",
} as const;
export const STATUS_WRITING = "writing";
/** What follows the END line in a freshly written note (v1 and v2). A v1 note with exactly this tail gets the v2 one. */
const MINE_TAIL_V1 = `\n${MINE_HEADING}\n\n`;
const mineTail = (): string => `\n${tr("note.mineHeading")}\n\n${MINE_HINT}\n\n`;

const V1_START = "<!-- EVOWIKI:WIKI:START";
const V1_KEYS = ["page_id", "format", "generated_hash"] as const;
type V1Header = Record<(typeof V1_KEYS)[number], string>;

const PAGE_ID = /^[^\s<>]+$/;
const HASH = /^sha256:[0-9a-f]{64}$/;
/** CRLF and a lone CR are both line breaks. */
const lf = (s: string) => s.replace(/\r\n?/g, "\n");
/** The fold sign after `[!type]` on a callout header line is view state, not content (R2). */
const unfold = (line: string) => line.replace(/^((?:[ \t]*>[ \t]?)+[ \t]*\[![^\]\n]*\])[+-]/, "$1");
export const normalizeBody = (body: string): string => lf(body).replace(/\n+$/, "").split("\n").map(unfold).join("\n");
const hashOf = (pageId: string, format: string, body: string) =>
  `sha256:${createHash("sha256").update(JSON.stringify([pageId, format, normalizeBody(body)]), "utf8").digest("hex")}`;
export const wikiHash = (pageId: string, body: string): string => hashOf(pageId, WIKI_FORMAT, body);

/**
 * What Obsidian shows verbatim in `s` (LF only), as offsets [from, to): fenced code (``` or ~~~; an unclosed fence runs to
 * the end and is returned as `openFence`), then, outside fences, inline code (a backtick run closed by a run of the same
 * length) and math (`$$…$$`, or `$…$` with no space just inside either `$` and no digit right after the closing one).
 * Spans never cross a blank line, and a backslash escape opens nothing. `fences: false` is for one-line fields, which
 * keep no block structure.
 */
function verbatimRanges(s: string, fences: boolean): { ranges: [number, number][]; openFence: string | null } {
  const ranges: [number, number][] = [];
  const prose: [number, number][] = [];
  let pos = 0, from = 0;
  const scan = new FenceScanner();
  let at = 0;
  for (const line of fences ? s.split("\n") : []) {
    const start = pos;
    pos = Math.min(pos + line.length + 1, s.length);
    const kind = scan.step(line);
    if (kind === "open") { at = start; prose.push([from, start]); }
    else if (kind === "close") { ranges.push([at, pos]); from = pos; }
  }
  const fence = scan.fence;
  if (fence) ranges.push([at, s.length]);
  else prose.push([from, s.length]);
  const blankLineAt = (j: number) => s[j] === "\n" && /^\n[ \t]*\n/.test(s.slice(j, j + 64));
  const run = (i: number) => { let n = 0; while (s[i + n] === "`") n++; return n; };
  for (const [a, b] of prose) {
    let i = a;
    while (i < b) {
      const c = s[i];
      if (c === "\\") { i += 2; continue; }
      if (c !== "`" && c !== "$") { i++; continue; }
      let j: number, close = -1, len: number;
      if (c === "`") {
        len = run(i);
        for (j = i + len; j < b && close < 0 && !blankLineAt(j);) {
          if (s[j] === "`") { const k = run(j); if (k === len) close = j; else j += k; } else j++;
        }
      } else if (s[i + 1] === "$") {
        len = 2;
        for (j = i + 2; j < b && close < 0 && !blankLineAt(j);) {
          if (s[j] === "\\") j += 2;
          else if (s[j] === "$" && s[j + 1] === "$") close = j;
          else j++;
        }
      } else {
        len = 1;
        if (/^\s?$/.test(s[i + 1] ?? "")) { i++; continue; }
        for (j = i + 1; j < b && close < 0 && !blankLineAt(j);) {
          if (s[j] === "\\") j += 2;
          else if (s[j] === "$" && !/\s/.test(s[j - 1]!) && !/\d/.test(s[j + 1] ?? "")) close = j;
          else j++;
        }
      }
      if (close >= 0) { ranges.push([i, close + len]); i = close + len; } else i += len;
    }
  }
  return { ranges, openFence: fence ? fence.ch.repeat(fence.n) : null };
}

/**
 * Outside verbatim ranges: an existing backslash escape (kept as is); every `[` (wikilinks, embeds, inline and reference
 * links, also when the link text spans a line break; an escaped `[` always looks the same); a tag `#` at line start or
 * after whitespace. `<!--` anywhere, code included.
 */
const UNSAFE = /\\[\s\S]|<!--|\[|(?<=^|\s)#(?=[^\s#])/gm;
/**
 * The writer's lookup mark (`⟦待查证：X⟧`, from the backend's goals/writer.py; also a bare `⟦待查证⟧` and the ASCII
 * `[待查证：X]`). The claim X stays in the text; a small pill follows it and its tooltip says what to check.
 */
const LOOKUP = /⟦待查证(?:[：:]([^⟧]*))?⟧|\[待查证[：:]([^\]\n]*)\]/g;
/**
 * A link to another wiki page, as the backend writes it in section text: `[text](#p:<page_id>)`. Only this exact shape
 * stays a Markdown link (in block text, outside code and math): one-line text with no bracket, backtick, `$`, backslash or
 * lookup mark, a page id of letters, digits, `_` and `-`, and no `!` or backslash right before it (an embed or the
 * text's own escape). The note's click handler (note-links.ts) opens the page; everything else is neutralised as before.
 */
export const PAGE_LINK = /(?<![!\\])\[([^[\]\n`$\\⟦⟧]+)\]\(#p:([A-Za-z0-9_-]+)\)/g;
/**
 * The strip the writer puts where the page disagrees with the material (`⟦和资料不同：通行写法是 A⟧`): the same words in a
 * styled span (the demo's 「和资料不同」 strip).
 */
const DIFFERS = /⟦(和资料不同[：:][^⟧]*)⟧/g;
/**
 * A citation as the backend stores it: `〔名 p.印码｜PDF页〕` (印码 may be a range, `12–13`). The older `〔名 p.N〕` has no
 * `｜` (印码 = PDF页 = N). The name is the material's title when the page was written (PageSourceOut.title).
 */
const CITE = /〔([^〔〕｜\n]+?) p\.(\d+(?:[–-]\d+)?)(?:｜(\d+))?〕/g;
/** The link target must be one plain vault path: nothing that ends, splits or re-aims a wikilink or opens a comment. */
const LINKABLE = /^[^[\]|#^\n\\]+$/;
/** Whether the material's original is a file in the vault the citation can link to at a page. */
const inVault = (src: PageSourceOut): src is PageSourceOut & { origin: string } =>
  src.kind !== "url" && src.kind !== "text" && src.origin !== null && !src.origin.includes("://")
  && LINKABLE.test(src.origin) && !src.origin.includes("%%");
/** Characters that could end a wikilink or open a comment become their full-width look-alikes in a link's shown text. */
const shown = (s: string) => s.replace(/[\n\r]+/g, " ").replace(/\[/g, "［").replace(/\]/g, "］").replace(/\|/g, "｜").replace(/%/g, "％");
/** A citation's markup, or null when it names no material of the page (the mark then stays as written). */
function citation(name: string, print: string, pdf: string | undefined, sources: PageSourceOut[]): { md: string; code: string } | null {
  const src = sources.find((x) => x.title === name);
  if (!src) return null;
  const text = `${name} p.${print}`;
  const plain = (t: string) => neutral(t, false).text;
  if (src.deleted) return { md: plain(`${text}（已删除）`), code: `${text}（已删除）` };
  // Only an uploaded file has an original in the vault: a url or pasted-text material (its origin a web address or
  // nothing) is plain text, never a wikilink that would create a stray note (final review #9).
  if (!inVault(src)) return { md: plain(text), code: text };
  const page = pdf ?? /^\d+/.exec(print)![0];
  return { md: `[[${src.origin}#page=${page}|${shown(text)}]]`, code: text };
}

/** A `**bold**` span on one line. */
const BOLD = /(?<!\*)\*\*(?![\s*])([^*\n]+?)(?<![\s*])\*\*(?!\*)/g;
const PUNCT = /[\p{P}\p{S}]/u;
/** Whether a `**` next to `inner` (its edge character) can open or close with `outer` on its other side (CommonMark). */
const flanks = (inner: string, outer: string | undefined): boolean =>
  !PUNCT.test(inner) || outer === undefined || /\s/.test(outer) || PUNCT.test(outer);
/**
 * A `**bold**` whose delimiter sits between punctuation and a letter (`**节点（node）**就是`) does not close in Live Preview
 * (CommonMark), which then shows the `**` and bolds the rest of the line: outside code and math it becomes
 * `<strong>…</strong>`, which shows the same in both views. Every other bold stays as written.
 */
function boldThatShows(s: string, verbatim: [number, number][]): string {
  return s.replace(BOLD, (m: string, inner: string, at: number) => {
    if (verbatim.some(([x, y]) => at >= x && at < y)) return m;
    const shows = flanks(inner[0]!, s[at - 1]) && flanks(inner[inner.length - 1]!, s[at + m.length]);
    return shows ? m : `<strong>${inner}</strong>`;
  });
}

/** Private-use placeholders for the pills and page links while the text is escaped; input copies of them are dropped. */
const SLOT = /(\d+)/g;
/** Every ASCII punctuation character becomes a numeric entity, so the attribute can neither close nor carry Markdown. */
const attr = (s: string) => lf(s).replace(/\s+/g, " ").trim().replace(/[!-/:-@[-`{-~]/g, (c) => `&#${c.charCodeAt(0)};`);
export const unverifiedSpan = (what: string): string => {
  const t = attr(what);
  return t ? `<span class="evowiki-unverified" title="${t}">待查证</span>` : "<span class=\"evowiki-unverified\">待查证</span>";
};

/**
 * R6 / R1.5: generated text never becomes the user's wikilink, embed, Markdown link or tag, never forges a marker and
 * never opens a comment. Each escape is one backslash, so Obsidian shows the same characters. Code and math are left
 * alone, except `<!--` and `%%` (marker safety wins over code fidelity). Lookup marks become pills (in prose only).
 */
function neutral(input: string, fences: boolean, links = false, sources: PageSourceOut[] = []): { text: string; openFence: string | null } {
  const s0 = input.replace(/[]/g, "");
  const r0 = verbatimRanges(s0, fences).ranges;
  /** Per placeholder: what it becomes in prose, and in code (where nothing may be added). */
  const spans: { md: string; code: string }[] = [];
  const s1 = s0.replace(LOOKUP, (m: string, a: string | undefined, b: string | undefined, at: number) => {
    if (r0.some(([x, y]) => at >= x && at < y)) return m;
    const what = a ?? b ?? "";
    spans.push({ md: unverifiedSpan(what), code: "（待查证）" });
    // A backslash right before the pill would escape its `<`.
    const gap = (what || s0.slice(0, at)).endsWith("\\") ? " " : "";
    return `${what}${gap}${spans.length - 1}`;
  });
  // Differs strips and citations: block text, and the one-line text of a section that has materials.
  const rich = links || sources.length > 0;
  const rd = verbatimRanges(s1, fences).ranges;
  const s1d = !rich ? s1 : s1.replace(DIFFERS, (m: string, what: string, at: number) => {
    if (rd.some(([x, y]) => at >= x && at < y)) return m;
    const t = lf(what).replace(/\s+/g, " ").trim();
    spans.push({ md: `<span class="evowiki-differs">${neutral(t.replace(/&/g, "&amp;").replace(/</g, "&lt;"), false).text}</span>`, code: m });
    return `${spans.length - 1}`;
  });
  const rc = verbatimRanges(s1d, fences).ranges;
  const s1c = sources.length === 0 ? s1d : s1d.replace(CITE, (m: string, name: string, print: string, pdf: string | undefined, at: number) => {
    if (rc.some(([x, y]) => at >= x && at < y)) return m;
    const c = citation(name, print, pdf, sources);
    if (c === null) return m;
    spans.push(c);
    return `${spans.length - 1}`;
  });
  const r1 = links ? verbatimRanges(s1c, fences).ranges : [];
  const sl = !links ? s1c : s1c.replace(PAGE_LINK, (m: string, text: string, id: string, at: number) => {
    if (r1.some(([x, y]) => at >= x && at < y)) return m;
    spans.push({ md: `[${neutral(text, false).text}](#p:${id})`, code: m });
    return `${spans.length - 1}`;
  });
  const s = boldThatShows(sl, verbatimRanges(sl, fences).ranges);
  const { ranges, openFence } = verbatimRanges(s, fences);
  const inCode = new Set<number>();
  for (const m of s.matchAll(SLOT)) if (ranges.some(([x, y]) => m.index >= x && m.index < y)) inCode.add(Number(m[1]));
  const text = s.replace(UNSAFE, (m: string, at: number) => {
    if (m === "<!--") return "&lt;!--";
    if (m.startsWith("\\") || ranges.some(([a, b]) => at >= a && at < b)) return m;
    return `\\${m}`;
  }).replace(/%(?=%)/g, "&#37;")
    .replace(SLOT, (_m: string, n: string) => (inCode.has(Number(n)) ? spans[Number(n)]!.code : spans[Number(n)]!.md));
  return { text, openFence };
}
/**
 * The page links of a written body (the text between the markers), as offsets into it: what PAGE_LINK matches outside
 * code and math, by the same verbatim rules the writer used (a one-level quote prefix, as in a folded section, counts as
 * indentation). `text` is where the link text is, `id` the page id.
 */
export function pageLinksInBody(body: string): { from: number; to: number; textFrom: number; textTo: number; id: string }[] {
  const s = lf(body);
  const plain = s.replace(/^>[ \t]?/gm, (m) => " ".repeat(m.length));
  const { ranges } = verbatimRanges(plain, true);
  const out: { from: number; to: number; textFrom: number; textTo: number; id: string }[] = [];
  for (const m of s.matchAll(PAGE_LINK)) {
    if (ranges.some(([a, b]) => m.index >= a && m.index < b)) continue;
    out.push({ from: m.index, to: m.index + m[0].length, textFrom: m.index + 1, textTo: m.index + 1 + m[1]!.length, id: m[2]! });
  }
  return out;
}
/** One-line fields: collapse first, so no fence is left to hide anything, then escape. */
const oneLine = (s: string, sources: PageSourceOut[] = [], links = false) =>
  neutral(lf(s).replace(/\s*\n\s*/g, " ").trim(), false, links, sources).text;
/** The same one-line escaper for other generated Markdown (a hand edit's check): one line, no link / tag / comment. */
export const oneLineMarkdown = oneLine;
/**
 * Block text. An unclosed fence is closed, so it cannot swallow the rest of the note (END marker, 我的笔记). Page links
 * (PAGE_LINK) stay links here and in a one-line section's text only: the other one-line fields (headings, cues)
 * keep no link of any kind.
 */
function block(s: string, sources: PageSourceOut[] = []): string {
  const { text, openFence } = neutral(lf(s), true, true, sources);
  const t = text.replace(/\n+$/, "");
  return openFence ? `${t}\n${openFence}` : t;
}
/**
 * A section text as the note writer puts it in a note (citations as their link / text, 和资料不同 strips, page links,
 * escapes): what revise/overlay.ts renders for the old and the new text of a changed paragraph, so the marks sit on the
 * page's own rendering.
 */
export const noteBlock = (s: string, sources: PageSourceOut[] = []): string => block(s, sources);
const quoted = (md: string) => md.split("\n").map((l) => (l === "" ? ">" : `> ${l}`));
const quote = (s: string, sources: PageSourceOut[] = []) => quoted(block(s, sources));

/** Whether a 一行带过 section's text keeps its one line (see sectionLines). */
const oneLineSection = (text: string): boolean => !text.includes("```") && !/\n[ \t]*\n/.test(text.trim());

/**
 * The way back from note Markdown to section text, for a line the user changed (note/hand-edit.ts; a line he did not
 * change is its source line, not this): outside code and math, a citation link or its plain text → 〔名 p.印码｜PDF页〕, the
 * two pills → ⟦待查证：X⟧ and ⟦和资料不同：…⟧, `<strong>` → `**`, the escapes `neutral` adds → the plain characters.
 * `block(fromNoteBlock(x, sources), sources)` is `x` for every `x` that `block` wrote. A link or mark the user typed
 * himself stays as he typed it.
 */
export function fromNoteBlock(md: string, sources: PageSourceOut[] = []): string {
  const s = lf(md);
  const ranges = [...verbatimRanges(s, true).ranges].sort((a, b) => a[0] - b[0]);
  let out = "", at = 0;
  for (const [a, b] of ranges) {
    out += proseBack(s.slice(at, a), sources) + anyBack(s.slice(a, b));
    at = b;
  }
  return out + proseBack(s.slice(at), sources);
}
const anyBack = (s: string): string => s.replace(/&lt;!--/g, "<!--").replace(/&#37;/g, "%");
const escapeRe = (s: string): string => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const unescaped = (s: string): string => s.replace(/\\([\s\S])/g, (m: string, c: string) => (c === "[" || c === "#" ? c : m));
const entities = (s: string): string => s.replace(/&#(\d+);/g, (_m, n: string) => String.fromCharCode(Number(n)));

function proseBack(prose: string, sources: PageSourceOut[]): string {
  let s = prose.replace(/\[\[([^\]\n|#]+)#page=(\d+)\|([^\]\n]+?) p\.(\d+(?:[–-]\d+)?)\]\]/g, (m: string, origin: string, pdf: string, name: string, print: string) => {
    const src = sources.find((x) => inVault(x) && x.origin === origin && shown(x.title) === name);
    return src === undefined ? m : `〔${src.title} p.${print}｜${pdf}〕`;
  });
  // a material with no file in the vault (or deleted) is written as plain text
  for (const src of sources) {
    if (inVault(src) && !src.deleted) continue;
    const name = escapeRe(neutral(src.title, false).text);
    s = s.replace(new RegExp(`${name} p\\.(\\d+(?:[–-]\\d+)?)(?:（已删除）)?`, "g"), (_m, print: string) => `〔${src.title} p.${print}〕`);
  }
  s = s.replace(/<span class="evowiki-differs">([^<]*)<\/span>/g, (_m, t: string) => `⟦${unescaped(t).replace(/&lt;/g, "<").replace(/&amp;/g, "&")}⟧`);
  let kept = "", last = 0;
  for (const m of s.matchAll(/<span class="evowiki-unverified"(?: title="([^"]*)")?>待查证<\/span>/g)) {
    const what = entities(m[1] ?? "");
    const front = neutral(what, false).text;
    const before = kept + s.slice(last, m.index);
    // the claim stands right before its pill; a claim he changed keeps the pill without it
    kept = what !== "" && before.endsWith(front) ? `${before.slice(0, before.length - front.length)}⟦待查证：${what}⟧` : `${before}⟦待查证⟧`;
    last = m.index + m[0].length;
  }
  s = kept + s.slice(last);
  return anyBack(unescaped(s.replace(/<strong>([^<\n]*)<\/strong>/g, "**$1**")));
}

/** The callout type of a folded section; styles.css gives it the 「已掌握」 pill. */
export const KNOWN_CALLOUT = "evowiki-known";
/** The code-block language of a check section; main.ts registers its processor (src/wiki/check-block.ts). */
export const CHECK_BLOCK = "evowiki-check";
/** An id the check block can carry on one line (no whitespace, no backtick). */
const CHECK_ID = /^[^\s`]+$/;
/**
 * A check section is an `evowiki-check` code block holding only where its question lives (page and section id): the
 * question, the options and any answer are drawn by the processor and never written (R3.1: the block is hashed).
 */
export const checkBlockLines = (pageId: string, sid: string): string[] =>
  CHECK_ID.test(pageId) && CHECK_ID.test(sid) ? [`\`\`\`${CHECK_BLOCK}`, `page: ${pageId}`, `section: ${sid}`, "```", ""] : [];

/**
 * A folded section is a default-folded `evowiki-known` callout: the cue (or, without one, the heading) is its title and
 * the section text is inside. A hidden check leaves no block or placeholder.
 */
export function sectionLines(pageId: string, s: WikiNoteSection, sources: PageSourceOut[] = []): string[] {
  // Completion checks are drawn below the footer, outside the editable note body.
  if (isGoalCheck(s)) return [];
  if (s.role === "check") return s.folded ? [] : checkBlockLines(pageId, s.id);
  const heading = oneLine(s.heading);
  // A hand edit shows exactly as it was typed (note/hand-edit.ts).
  const typed = s.noteBody ?? null;
  const text = typed ?? visibleBlanks(block(s.body, sources));
  if (s.folded) return [`> [!${KNOWN_CALLOUT}]- ${oneLine(s.cue) || heading}`, ...(typed !== null ? quoted(typed) : quote(s.body, sources)), ""];
  switch (s.role) {
    case "organizer": return [text, ""];
    // A 一行带过 section with a fenced block (an interactive) cannot be flattened: the reader would see its source. Nor can
    // one with several paragraphs (an accepted rewrite keeps the section's role): they would run together in one bullet.
    case "line":
      if (!oneLineSection(typed ?? s.body)) return [`## ${heading}`, "", text, ""];
      return [`- **${heading}**：${typed ?? oneLine(s.body, sources, true)}`, ""];
    // A recap is a normal section (T10: the note page has no 提醒 card).
    case "recap": case "explain": case "compare": return [`## ${heading}`, "", text, ""];
  }
}

/**
 * Where each section sits in the generated body, as 0-based line ranges [from, to) of the body split on "\n" (the same
 * lines normalizeBody keeps; the last range may run past the last line, since trailing blank lines are dropped).
 * Lines before the first section are page-level callouts.
 */
export interface WikiBodyLayout { sections: { id: string; from: number; to: number }[] }

function bodyLines(i: WikiNoteInput): { out: string[]; layout: WikiBodyLayout } {
  const out: string[] = [];
  let n = 0;
  // An entry may hold several lines (a block of section text): count them as the split body will.
  const push = (...xs: string[]) => { for (const x of xs) { out.push(x); n += x.split("\n").length; } };
  if (i.diffText?.trim()) push(tr("note.changed"), ...quote(i.diffText), "");
  if (i.collapsed) push(tr("note.collapsed"), "");
  const sections: WikiBodyLayout["sections"] = [];
  for (const s of i.sections) {
    const from = n;
    push(...sectionLines(i.pageId, s, i.sources ?? []));
    sections.push({ id: s.id, from, to: n });
  }
  while (out.length && out[out.length - 1] === "") out.pop();
  return { out, layout: { sections } };
}

/** The line ranges of renderWikiBody(i) (edit-diff.ts: which section a user's edit falls in). */
export const wikiBodyLayout = (i: WikiNoteInput): WikiBodyLayout => bodyLines(i).layout;

/** The generated body between the markers. No H1 (the title is the evowiki_title property, drawn as the h1), no goal line, no disclaimer. */
/** Outside code fences, a run of underscores is a fill-in's blank: escaped, or markdown draws nothing. */
export function visibleBlanks(text: string): string {
  const scan = new FenceScanner();
  return text.split("\n").map((line) => (scan.step(line) === "text" ? line.replace(/(?<![\\_])_{3,}/g, (m) => "\\_".repeat(m.length)) : line)).join("\n");
}

export function renderWikiBody(i: WikiNoteInput): string {
  const { out } = bodyLines(i);
  return out.length ? out.join("\n") + "\n" : "";
}

const pad = (n: number) => String(n).padStart(2, "0");
/** 「更新于」 in local time, minutes only. */
export const updatedAtText = (d: Date): string => `${localDayKey(d)} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
/** A display property: one line; `[[` is broken with a zero-width space so the value never becomes a link. */
const prop = (s: string) => lf(s).replace(/\s+/g, " ").trim().replace(/\[\[/g, "[​[");
const foldedCount = (i: WikiNoteInput) => i.sections.filter((s) => s.folded && s.role !== "check").length;

/** The keys the note page draws (目标 / 更新于 in its header, evowiki_title as its h1), in their frontmatter order. */
const DISPLAY_KEYS: readonly string[] = [FM_KEYS.goal, FM_KEYS.folded, FM_KEYS.updated, FM_KEYS.title];
/** The generated display values (目标, 折起, evowiki_title); null means the key is left out. */
const displayValues = (i: WikiNoteInput): [string, string | null][] => {
  const goal = prop(i.goal), n = foldedCount(i), title = prop(i.title);
  return [[FM_KEYS.goal, goal || null], [FM_KEYS.folded, n > 0 ? tr("note.masteredSections", { n }) : null], [FM_KEYS.title, title || null]];
};

/**
 * R1.2: rewrites only this plugin's keys (in place; a missing display key goes next to the other display keys in the
 * order 目标, 折起, 更新于, or before the first evowiki_* key; other missing keys are appended) and adds `evowiki-page` to cssclasses;
 * every other key and cssclasses entry is kept line for line. 「更新于」 is set only when `now` is given. Null when an
 * existing cssclasses value cannot be read safely (the caller then writes nothing).
 */
function mergeFrontmatter(fm: Frontmatter | null, i: WikiNoteInput, hash: string, now: Date | null): Frontmatter | null {
  const entries: FmEntry[] = fm ? [...fm.entries] : [];
  const set = (key: string, e: FmEntry | null) => {
    const at = entries.findIndex((x) => x.key === key);
    if (e === null) { if (at >= 0) entries.splice(at, 1); return; }
    if (at >= 0) { entries[at] = e; return; }
    const rank = DISPLAY_KEYS.indexOf(key);
    if (rank < 0) { entries.push(e); return; }
    const before = entries.findLastIndex((x) => x.key !== null && DISPLAY_KEYS.indexOf(x.key) >= 0 && DISPLAY_KEYS.indexOf(x.key) < rank);
    const after = entries.findIndex((x) => x.key !== null && DISPLAY_KEYS.indexOf(x.key) > rank);
    const machine = entries.findIndex((x) => x.key?.startsWith("evowiki_") ?? false);
    const to = before >= 0 ? before + 1 : after >= 0 ? after : machine >= 0 ? machine : entries.length;
    entries.splice(to, 0, e);
  };
  for (const [k, v] of displayValues(i)) set(k, v === null ? null : scalarEntry(k, v));
  if (now) set(FM_KEYS.updated, scalarEntry(FM_KEYS.updated, updatedAtText(now)));
  set(FM_KEYS.status, i.writing === true ? scalarEntry(FM_KEYS.status, STATUS_WRITING) : null);
  set(FM_KEYS.pageId, scalarEntry(FM_KEYS.pageId, i.pageId));
  set(FM_KEYS.format, scalarEntry(FM_KEYS.format, WIKI_FORMAT));
  set(FM_KEYS.hash, scalarEntry(FM_KEYS.hash, hash));
  const css = entries.find((x) => x.key === FM_KEYS.css);
  if (!css) set(FM_KEYS.css, listEntry(FM_KEYS.css, [PAGE_CSS_CLASS]));
  else {
    const items = entryList(css);
    if (items === null) return null;
    if (!items.includes(PAGE_CSS_CLASS)) set(FM_KEYS.css, listEntry(FM_KEYS.css, [...items, PAGE_CSS_CLASS]));
  }
  return { entries, eol: fm?.eol ?? "\n" };
}

const blockText = (body: string) => `${WIKI_START}\n${body}${WIKI_END}`;

export function renderWikiNote(i: WikiNoteInput, now: Date | null = null): { markdown: string; hash: string } {
  if (!PAGE_ID.test(i.pageId)) throw new Error(`EvoWiki: page id not writable on one line: ${JSON.stringify(i.pageId)}`);
  const body = renderWikiBody(i);
  const hash = wikiHash(i.pageId, body);
  const fm = mergeFrontmatter(null, i, hash, now)!;
  const markdown = `${serializeFrontmatter(fm)}${blockText(body)}\n${mineTail()}`;
  const p = parseWikiFile(markdown);
  // Unreachable; fail closed.
  if (p.kind !== "one" || p.version !== 2 || p.edited || p.hash !== hash) throw new Error("EvoWiki: rendered wiki note does not parse");
  return { markdown, hash };
}

export interface WikiOne {
  kind: "one"; version: 1 | 2; pageId: string; hash: string; edited: boolean;
  /** Frontmatter at the top of the file (v1 notes may have one the user added), null if none. */
  fm: Frontmatter | null;
  /** Text between the frontmatter and the START line, the body, the END line's own ending, and everything after. */
  pre: string; body: string; endEol: string; after: string;
}
export type WikiFile = { kind: "none" } | { kind: "ambiguous"; reason: string } | WikiOne;

const lineMarkers = (text: string, marker: string) =>
  [...text.matchAll(new RegExp(`(^|\\n)${marker.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(\\r?\\n|$)`, "g"))]
    .map((m) => ({ at: m.index + m[1]!.length, len: m[0].length - m[1]!.length, eol: m[2]! }));

/**
 * Markers count only as whole lines (a quoted marker mid-line is the user's text). v2: frontmatter `evowiki_page_id`
 * plus exactly one START and one END line. Anything half-there is "ambiguous" (R1.4), which callers treat as edited.
 */
export function parseWikiFile(text: string): WikiFile {
  const sp = splitFrontmatter(text);
  if (sp.kind === "bad") {
    const marked = lineMarkers(text, WIKI_START).length + lineMarkers(text, WIKI_END).length > 0
      || /(^|\n)<!-- EVOWIKI:WIKI:(START|END)/.test(text) || /(^|\n)evowiki_page_id[ \t]*:/.test(text);
    return marked ? { kind: "ambiguous", reason: `frontmatter: ${sp.reason}` } : { kind: "none" };
  }
  const fm = sp.kind === "ok" ? sp.fm : null;
  const rest = sp.rest;
  const get = (k: string) => { const e = fm?.entries.find((x) => x.key === k); return e ? entryString(e) : null; };
  const starts = lineMarkers(rest, WIKI_START), ends = lineMarkers(rest, WIKI_END);
  if (fm?.entries.some((x) => x.key === FM_KEYS.pageId)) {
    const pageId = get(FM_KEYS.pageId), hash = get(FM_KEYS.hash);
    if (pageId === null || !PAGE_ID.test(pageId) || get(FM_KEYS.format) !== WIKI_FORMAT || hash === null || !HASH.test(hash)) {
      return { kind: "ambiguous", reason: "missing or unknown evowiki properties" };
    }
    if (starts.length !== 1 || ends.length !== 1) return { kind: "ambiguous", reason: "expected exactly one START and one END line" };
    const s = starts[0]!, e = ends[0]!;
    if (e.at < s.at || s.eol === "") return { kind: "ambiguous", reason: "END before START, or START on the last line" };
    const body = rest.slice(s.at + s.len, e.at);
    return { kind: "one", version: 2, pageId, hash, edited: wikiHash(pageId, body) !== hash, fm,
      pre: rest.slice(0, s.at), body, endEol: e.eol, after: rest.slice(e.at + e.len) };
  }
  const v1 = parseV1(rest);
  if (v1.kind === "one") {
    return { kind: "one", version: 1, pageId: v1.header.page_id, hash: v1.header.generated_hash,
      edited: hashOf(v1.header.page_id, WIKI_FORMAT_V1, v1.body) !== v1.header.generated_hash, fm,
      pre: v1.before, body: v1.body, endEol: v1.endEol, after: v1.after };
  }
  if (v1.kind === "ambiguous") return v1;
  if (starts.length || ends.length) return { kind: "ambiguous", reason: "markers without evowiki_page_id" };
  return { kind: "none" };
}

type V1Parsed = { kind: "none" } | { kind: "ambiguous"; reason: string }
  | { kind: "one"; before: string; header: V1Header; body: string; endEol: string; after: string };
/** The v1 block (HTML-comment START with header lines … END), read for migration only. */
function parseV1(text: string): V1Parsed {
  const starts = [...text.matchAll(/(^|\n)<!-- EVOWIKI:WIKI:START\r?\n/g)].map((m) => m.index + m[1]!.length);
  const ends = [...text.matchAll(/(^|\n)<!-- EVOWIKI:WIKI:END -->(\r?\n|$)/g)]
    .map((m) => ({ at: m.index + m[1]!.length, len: m[0].length - m[1]!.length, eol: m[2]! }));
  if (starts.length === 0 && ends.length === 0) return { kind: "none" };
  if (starts.length !== 1 || ends.length !== 1) return { kind: "ambiguous", reason: "expected exactly one START and one END marker" };
  const s = starts[0]!, e = ends[0]!;
  if (e.at < s) return { kind: "ambiguous", reason: "END before START" };
  const closeRe = /\r?\n-->\r?\n/g;
  closeRe.lastIndex = s;
  const close = closeRe.exec(text);
  if (!close || close.index > e.at) return { kind: "ambiguous", reason: "unterminated header" };
  const header: Partial<V1Header> = {};
  for (const line of text.slice(s + V1_START.length, close.index).replace(/^\r?\n/, "").split(/\r?\n/)) {
    const m = /^([a-z_]+)=(.*)$/.exec(line);
    if (!m || !(V1_KEYS as readonly string[]).includes(m[1]!) || m[1]! in header) return { kind: "ambiguous", reason: `bad header line: ${line}` };
    header[m[1] as keyof V1Header] = m[2]!;
  }
  if (V1_KEYS.some((k) => !header[k]) || header.format !== WIKI_FORMAT_V1 || !PAGE_ID.test(header.page_id!)) {
    return { kind: "ambiguous", reason: "missing or unknown header fields" };
  }
  return { kind: "one", before: text.slice(0, s), header: header as V1Header, body: text.slice(close.index + close[0].length, e.at),
    endEol: e.eol, after: text.slice(e.at + e.len) };
}

export function parseWikiNote(markdown: string): { pageId: string; hash: string; edited: boolean } | null {
  const p = parseWikiFile(markdown);
  return p.kind === "one" ? { pageId: p.pageId, hash: p.hash, edited: p.edited } : null;
}

/** Whether the frontmatter already holds the display values this input would write (absent where they are left out). */
function displayCurrent(fm: Frontmatter | null, i: WikiNoteInput): boolean {
  const status = fm?.entries.find((x) => x.key === FM_KEYS.status);
  if (i.writing === true ? status === undefined || entryString(status) !== STATUS_WRITING : status !== undefined) return false;
  return displayValues(i).every(([k, v]) => {
    const e = fm?.entries.find((x) => x.key === k);
    return v === null ? e === undefined : e !== undefined && entryString(e) === v;
  });
}

export type WikiUpdate = { kind: "user_edited"; reason: string } | { kind: "unchanged" } | { kind: "write"; text: string; hash: string };

/**
 * The next text of a note that claims this page (pure; sync.ts does the backup and the write). v1 or v2, unedited →
 * v2; the same v2 hash → unchanged; edited, ambiguous or another page → user_edited (never overwritten, E3a/E3b), except
 * `accepted`: an edited note whose edit the server took (`i` now holds it) gets the block like any other. Every byte
 * outside the frontmatter and the block is kept; a v1 note whose 我的笔记 tail is still the untouched default gets
 * the v2 tail with the hint line.
 */
export function updateWikiNote(text: string, i: WikiNoteInput, now: Date, accepted = false): WikiUpdate {
  if (!PAGE_ID.test(i.pageId)) throw new Error(`EvoWiki: page id not writable on one line: ${JSON.stringify(i.pageId)}`);
  const p = parseWikiFile(text);
  if (p.kind !== "one") return { kind: "user_edited", reason: p.kind === "none" ? "no wiki block" : p.reason };
  if (p.pageId !== i.pageId) return { kind: "user_edited", reason: "another page" };
  if (p.edited && !accepted) return { kind: "user_edited", reason: "generated text was edited" };
  const body = renderWikiBody(i);
  const hash = wikiHash(i.pageId, body);
  // The same body and the same generated 目标 / 折起 → nothing to write (更新于 changes only on a write).
  if (!p.edited && p.version === 2 && p.hash === hash && displayCurrent(p.fm, i)) return { kind: "unchanged" };
  const fm = mergeFrontmatter(p.fm, i, hash, now);
  if (fm === null) return { kind: "user_edited", reason: "cssclasses could not be read" };
  const after = p.version === 1 && p.endEol === "\n" && p.after === MINE_TAIL_V1 ? mineTail() : p.after;
  const next = `${serializeFrontmatter(fm)}${p.pre}${blockText(body)}${p.endEol}${after}`;
  const q = parseWikiFile(next);
  if (q.kind !== "one" || q.version !== 2 || q.edited || q.hash !== hash) return { kind: "user_edited", reason: "result would not parse" };
  return { kind: "write", text: next, hash };
}

/** The line a kept note of a deleted (or merged) page starts with (trashNote in sync.ts). */
export let TRASHED_LINE = tr("note.trashed");
onLanguageChange(() => { TRASHED_LINE = tr("note.trashed"); });

/**
 * What the note of a page in the trash keeps when it holds the user's own text (in 我的笔记, or above the block):
 * that text and the user's own properties, without the generated block and the evowiki properties (so it no longer
 * claims the page), led by TRASHED_LINE. null: nothing of the user's (the whole note can go to the trash).
 */
export function personalRemainder(p: WikiOne): string | null {
  const mine = p.after.replace(/^##\s+(?:我的笔记|My notes)\s*$/m, "").replace(/<span class="evowiki-mine-hint">[^<]*<\/span>/g, "");
  if (p.pre.trim() === "" && mine.trim() === "") return null;
  const managed = new Set<string>(Object.values(FM_KEYS));
  const entries = (p.fm?.entries ?? []).flatMap((e): FmEntry[] => {
    if (e.key === FM_KEYS.css) {
      const items = entryList(e);
      if (items === null) return [e];
      const rest = items.filter((i) => i !== PAGE_CSS_CLASS);
      return rest.length > 0 ? [listEntry(FM_KEYS.css, rest)] : [];
    }
    return e.key !== null && managed.has(e.key) ? [] : [e];
  });
  const fm = entries.some((e) => e.key !== null) ? serializeFrontmatter({ entries, eol: p.fm?.eol ?? "\n" }) : "";
  return `${fm}${p.pre}${TRASHED_LINE}\n${p.after}`;
}

/**
 * R1.3 / R7: a file claims a page by its frontmatter line `evowiki_page_id: <id>` (v2, also when the rest is broken; an
 * unterminated frontmatter is searched up to the START line), or by a line-start v1 START marker immediately followed
 * by the line `page_id=<id>`. The same text anywhere else is someone's text, not a claim.
 */
export function claimsPage(text: string, pageId: string): boolean {
  if (/^---[ \t]*\r?\n/.test(text)) {
    for (const line of text.split(/\r?\n/).slice(1)) {
      if (/^---[ \t]*$/.test(line) || line === WIKI_START) break;
      const m = /^evowiki_page_id[ \t]*:(.*)$/.exec(line);
      if (m && parseScalar(m[1]!) === pageId) return true;
    }
  }
  const id = pageId.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`(^|\\n)<!-- EVOWIKI:WIKI:START\\r?\\npage_id=${id}(\\r?\\n|$)`).test(text);
}
