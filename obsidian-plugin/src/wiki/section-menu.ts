import { localizedAttribute } from "../i18n/index";
/**
 * The 「⋯」 menu next to a section heading in a wiki note (Reading view and Live Preview; section-menu-view.ts draws it).
 * Pure logic: which `##` lines of a note are page sections, which section of the controller's open page a heading is
 * (R4.1: by heading text, same-name headings in order; no match → no menu), and what the menu offers. The actions are
 * the controller's own section actions, the ones the sidebar used to call.
 */
import { el } from "../ui/ew";
import { ewNotice } from "../ui/ew-notice";
import { writeBlocked } from "./actions";
import { WIKI_COPY } from "./copy";
import { patchedSections } from "./end-block/model";
import { qTip } from "./q-tip";
import { REVISE_COPY } from "./revise/copy";
import { appliedRevise } from "./revise/rows";
import { NameBook } from "./names";
import { FenceScanner } from "./note/fences";
import { parseWikiFile, sectionLines, WIKI_END, WIKI_START } from "./note/format";
import { toWikiNoteInput } from "./note-input";
import type { PageOut, SectionOut } from "./schema";
import type { ResultNote, WikiState } from "./state";

/** A `##` heading inside the generated block: its page, its text as written in the note, and which same-text one it is. */
export interface HeadingRef { pageId: string; heading: string; ordinal: number }

/** The `##` headings of a wiki note, by 0-based line number. */
export interface NoteHeadings { pageId: string; byLine: Map<number, HeadingRef> }

const H2 = /^## +(.*?)[ \t]*$/;

/** One `##` line found by h2Lines: its index in the scanned lines, its text, and which same-text one it is. */
interface H2Line { at: number; heading: string; ordinal: number }

/** The `##` lines of lines[from, to), outside fenced code, with per-text ordinals (the note and the page side alike). */
function h2Lines(lines: readonly string[], from: number, to: number): H2Line[] {
  const out: H2Line[] = [];
  const seen = new Map<string, number>();
  const scan = new FenceScanner();
  for (let i = from; i < to; i++) {
    const line = lines[i]!;
    if (scan.step(line) !== "text") continue;
    const m = H2.exec(line);
    if (!m) continue;
    const heading = m[1]!;
    const ordinal = seen.get(heading) ?? 0;
    seen.set(heading, ordinal + 1);
    out.push({ at: i, heading, ordinal });
  }
  return out;
}

/** The last few documents' answers (several wiki notes may be open at once). */
const cache = new Map<string, NoteHeadings | null>();
const CACHE_SIZE = 4;

/**
 * The `##` lines strictly between the START and END marker lines of a v2 wiki note (frontmatter `evowiki_page_id`),
 * outside fenced code. Null for any other file (no wiki block, a v1 or a broken note). 「我的笔记」 is after END, so
 * it is never one. Body subheadings are found too; matchSection decides which line is a section's own heading.
 */
export function noteHeadings(text: string): NoteHeadings | null {
  if (!mayBeWikiNote(text)) return null;
  if (cache.has(text)) return cache.get(text)!;
  const out = parse(text);
  if (cache.size >= CACHE_SIZE) cache.delete(cache.keys().next().value!);
  cache.set(text, out);
  return out;
}

/** A cheap first look: only a text with a START line can be a wiki note (the editor asks for every open note). */
export const mayBeWikiNote = (text: string): boolean => text.includes(WIKI_START);

function parse(text: string): NoteHeadings | null {
  const p = parseWikiFile(text);
  if (p.kind !== "one" || p.version !== 2) return null;
  const lines = text.split(/\r\n?|\n/);
  const start = lines.indexOf(WIKI_START);
  const end = lines.indexOf(WIKI_END, start + 1);
  if (start < 0 || end < 0) return null;
  const byLine = new Map<number, HeadingRef>();
  for (const x of h2Lines(lines, start + 1, end)) byLine.set(x.at, { pageId: p.pageId, heading: x.heading, ordinal: x.ordinal });
  return { pageId: p.pageId, byLine };
}

/** `heading` + NUL + ordinal. */
const key = (heading: string, ordinal: number): string => `${heading}\u0000${ordinal}`;

/**
 * The `##` lines the note writer would put in the block for `page` (the same section text, in order), keyed by text and
 * ordinal: the section id when the line is that section's own heading, null for a body subheading or 「相关页面」.
 */
function pageHeadings(page: PageOut): Map<string, string | null> {
  const input = toWikiNoteInput(page, new NameBook());
  const lines: string[] = [];
  const own = new Map<number, string>();
  for (const x of input.sections) {
    const chunk = sectionLines(input.pageId, x).join("\n").split("\n");
    if ((x.role === "explain" || x.role === "compare" || x.role === "recap") && !x.folded) own.set(lines.length, x.id);
    lines.push(...chunk);
  }
  const out = new Map<string, string | null>();
  for (const x of h2Lines(lines, 0, lines.length)) out.set(key(x.heading, x.ordinal), own.get(x.at) ?? null);
  return out;
}

const pageCache = new WeakMap<PageOut, Map<string, string | null>>();

/**
 * R4.1: the section of the controller's open page that `ref` names. The note's ordinal-th `## T` is mapped to the
 * ordinal-th `## T` of the block written from the current page, and counts only when that line is a section's own
 * heading (never a `##` inside a section's text). Null when the open page is another one (or none), or nothing matches.
 */
export function matchSection(s: WikiState, ref: HeadingRef): SectionOut | null {
  const p = s.page;
  if (p.kind !== "open" || p.page.page_id !== ref.pageId) return null;
  let m = pageCache.get(p.page);
  if (m === undefined) { m = pageHeadings(p.page); pageCache.set(p.page, m); }
  const sid = m.get(key(ref.heading, ref.ordinal)) ?? null;
  return sid === null ? null : p.page.sections.find((x) => x.id === sid) ?? null;
}

/**
 * What the menus' visibility depends on: the open page id and, per section, what decides a match or a menu (id,
 * heading, role, folded, and the landed body: empty or not, and its own `##` lines shift the ordinals). Disabled states
 * are read when the menu opens, so busy, pins and streamed chunks (kept outside page.sections) do not change it.
 */
export function menuKey(s: WikiState): string {
  const p = s.page;
  if (p.kind !== "open") return "";
  return JSON.stringify([p.page.page_id, p.page.collapsed,
    p.page.sections.map((x) => [x.id, x.heading, x.role, x.folded, x.body.trim() === "" ? 0 : x.body])]);
}

export type SectionAction = "know" | "dont" | "ask" | "pin";
/** Where an entry sits in the menu (app.js sectionMenu): marks, then 问助手, then the lock entry, a separator between. */
export type MenuGroup = "mark" | "ask" | "more";
/** `hint`: why a disabled entry is off, when the user can fix it (the view appends it to the title; a Menu has no tooltip). */
export interface MenuEntry { action: SectionAction; title: string; disabled: boolean; group: MenuGroup; hint?: string }

/**
 * The menu for `ref` (app.js sectionMenu): 标记为已掌握 / 标记为不会, then 问助手, then 锁定这一节 / 解除锁定. Rewriting
 * (讲深一点, 换个讲法, 重新组织) is said in the assistant. Null when there is none (no matching section, or no text yet).
 * Everything but 问助手 is disabled while a write runs, offline or unconfigured; a pinned section cannot be folded but
 * can be unpinned. (A collapsed page has no `##` section headings, so it has no menu to hold 恢复成完整页.) 深度精修这一页 is not offered: the server has no refine action to call yet.
 */
export function sectionMenu(s: WikiState, ref: HeadingRef): { sid: string; entries: MenuEntry[] } | null {
  const x = matchSection(s, ref);
  if (x === null || x.body.trim() === "") return null;
  const blocked = writeBlocked(s);
  const locked = blocked || x.pinned;
  // app.js sectionMenu: a locked section's 标记为已掌握 carries the title 先解除锁定.
  const hint = x.pinned ? { hint: WIKI_COPY.menuPinnedHint } : {};
  const entries: MenuEntry[] = [
    { action: "know", title: WIKI_COPY.know, disabled: locked, group: "mark", ...hint },
    { action: "dont", title: WIKI_COPY.menuDont, disabled: blocked || x.cells.length === 0, group: "mark" },
    { action: "ask", title: WIKI_COPY.menuAsk, disabled: false, group: "ask" },
    { action: "pin", title: x.pinned ? WIKI_COPY.unpin : WIKI_COPY.pin, disabled: blocked, group: "more" },
  ];
  return { sid: x.id, entries };
}

/** What the menu calls: WikiController satisfies it. */
export interface SectionActions {
  know(sid: string): unknown;
  togglePin(sid: string): unknown;
  /** 这里的概念我不会: 不会 for every cell of the section; stops at the first failure. */
  declareSectionDont(sid: string): unknown;
  /** 问助手: the section's heading and opening become the assistant's quote. */
  askAssistant(sid: string): unknown;
}

/** The ResultNote a finished action left on the section. */
function resultNote(s: WikiState, sid: string): ResultNote | null {
  const p = s.page;
  if (p.kind !== "open") return null;
  return p.sectionNotes[sid] ?? null;
}

/**
 * Runs the action. The page shows the result of 标记为已掌握 and 锁定 / 解除锁定, so a
 * success says nothing (app.js sectionMenu); a failure is still shown as the controller's error line (fail closed).
 * 标记为不会 notifies through the controller itself; 问助手 opens the assistant.
 */
export async function runSectionAction(a: SectionSource, sid: string, action: SectionAction, say: (text: string) => void = ewNotice): Promise<void> {
  const before = resultNote(a.state, sid);
  const after = (): void => {
    const n = resultNote(a.state, sid);
    if (n === null || n === before) return;
    if (n.tone === "error") say(n.text);
  };
  switch (action) {
    case "know": await a.know(sid); after(); return;
    case "dont": await a.declareSectionDont(sid); return;
    case "ask": await a.askAssistant(sid); return;
    case "pin": await a.togglePin(sid); after(); return;
  }
}

/** What the menus read: the hub's current real controller (null before the wiki is used, and for the demo). */
export type SectionSource = { readonly state: WikiState } & SectionActions;
export interface SectionHub {
  readonly current: SectionSource | null; subscribe(fn: () => void): () => void;
  /** Whether a note (its vault path) is the current user's (CheckBlockHub.owns); absent = every note. */
  owns?(notePath: string | null): boolean;
}
/** Opens the menu under the button (section-menu-view.ts: an Obsidian Menu). */
export type OpenMenu = (button: HTMLElement, from: SectionSource, ref: HeadingRef) => void;

/**
 * app.js sectionEl's `button.more` 「⋯」 (aria-label 这一节的操作, aria-haspopup menu, aria-expanded); a click opens the menu
 * when the heading still matches a section of the open page.
 */
export function menuButton(ref: HeadingRef, hub: SectionHub, open: OpenMenu): HTMLButtonElement {
  const b = el("button", "more", "⋯");
  b.type = "button";
  localizedAttribute(b, "aria-label", () => (WIKI_COPY.sectionMenu));
  b.setAttribute("aria-haspopup", "menu");
  b.setAttribute("aria-expanded", "false");
  b.onclick = (e: MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    const from = hub.current;
    if (from !== null && sectionMenu(from.state, ref) !== null) open(b, from, ref);
  };
  return b;
}

/**
 * What a section heading shows besides its text (app.js sectionEl): the tags, the 「⋯」 button, 「正在写…」. `revised`: the
 * confirmed materials draft changed this section (app.js `s.tag` 已更新, gone again with 撤销).
 */
export interface HeadingView { tag: boolean; revised: boolean; pin: boolean; menu: boolean }

export function headingView(s: WikiState | null, ref: HeadingRef): HeadingView {
  const x = s === null ? null : matchSection(s, ref);
  if (s === null || x === null) return { tag: false, revised: false, pin: false, menu: false };
  const rv = appliedRevise(s, ref.pageId);
  return {
    tag: patchedSections(s, ref.pageId).has(x.id),
    revised: rv !== null && rv.revise.sections.some((c) => c.section_id === x.id && c.status !== "removed"),
    pin: x.pinned,
    menu: sectionMenu(s, ref) !== null,
  };
}

/**
 * The parts of one section heading, drawn from the hub's controller: `inline` = `span.ew.ew-inline` holding (in app.js
 * order, after the heading text) `span.ew-tag` 按你的反馈改过, `span.ew-pin` 已钉住 and `button.ew-more` 「⋯」. Parts that do not apply are
 * left out of the DOM (as app.js leaves them out), not hidden.
 */
export interface HeadingParts { inline: HTMLElement; update(): void }

export function createHeadingParts(ref: HeadingRef, hub: SectionHub, open: OpenMenu): HeadingParts {
  const inline = el("span");
  inline.className = "ew ew-inline evowiki-heading-parts";
  const tag = el("span", "tag", () => (WIKI_COPY.sectionTag));
  const revised = el("span", "tag", () => (REVISE_COPY.sectionTag));
  revised.dataset.role = "revised";
  const pin = el("span", "pin", () => (WIKI_COPY.sectionPinned));
  pin.append(qTip(() => (WIKI_COPY.sectionPinnedWhy), () => (WIKI_COPY.sectionPinned)));
  const more = menuButton(ref, hub, open);
  let seen = "";
  function update(): void {
    const v = headingView(hub.current?.state ?? null, ref);
    const key = `${v.tag}${v.revised}${v.pin}${v.menu}`;
    if (key === seen) return;
    seen = key;
    inline.replaceChildren(...[v.tag ? tag : null, v.revised ? revised : null, v.pin ? pin : null, v.menu ? more : null].filter((x) => x !== null));
  }
  update();
  return { inline, update };
}

/**
 * Reading view: the parts go into the heading element (after its text); redrawn on every hub change. Returns the cleanup.
 */
export function mountSectionMenu(heading: HTMLElement, ref: HeadingRef, hub: SectionHub, open: OpenMenu): () => void {
  const parts = createHeadingParts(ref, hub, open);
  heading.append(parts.inline);
  const unsub = hub.subscribe(() => parts.update());
  return () => { unsub(); parts.inline.remove(); };
}
