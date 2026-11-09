import { tr, labelNode } from "../../i18n/index";
/**
 * The note page's header (app.js renderNote: `h1` with the page title, then `dl.meta` 目标 / 更新于 with the divider),
 * drawn by the plugin at the top of a wiki note in Reading view and Live Preview; Obsidian's inline title and
 * properties are hidden for these notes (styles.css T10). The values are the note's own frontmatter: evowiki_title
 * (fallback: the file's base name), 目标, 更新于. Nothing is written to the file.
 *
 * Where it goes: Reading view — Obsidian's `.mod-header` (the first block of `.markdown-preview-sizer`, holding the inline
 * title and the properties), else the top of the sizer; Live Preview — the editor's `.cm-sizer`, before
 * `.cm-contentContainer`. Obsidian rebuilds both now and then, so each view is watched (a MutationObserver on the sizer)
 * and the header is put back when it went missing; `ensureHeader` is idempotent (never two headers).
 *
 * The 用资料改这一页 bar (revise/overlay.ts `watchReviseBar`) lives in the same sizers, right after the header. The two
 * watchers must never chase each other: the header's place is computed with the bar's hosts skipped (it goes before
 * them), a correct header is never touched, and mutations that only move the bar's hosts are ignored here.
 */
import { btn, el } from "../../ui/ew";
import { WIKI_COPY } from "../copy";
import { FM_KEYS, PAGE_CSS_CLASS, STATUS_WRITING } from "../note/format";

/**
 * `writing`: frontmatter `evowiki-status: writing`, so 「生成中」 shows at the top of the page even where the file tree has
 * no mark. `run` (the wiki controller's, header-run below): "writing" = this plugin streams the page's write, the line gets
 * 停止; "stopped" = 停止 was clicked, the line reads 已停止 with 重新写.
 */
export interface HeaderInfo { pageId: string; title: string; goal: string; updated: string; writing?: boolean; run?: "writing" | "stopped" }
/** What the line's buttons do (main.ts: the wiki controller's stop / restart). */
export interface HeaderHooks { stop(): void; restart(pageId: string): void }

export const HEADER_CLASS = "evowiki-note-header";
/** The host of the 用资料改这一页 bar (revise/overlay.ts), which sits right after the header. */
export const REVBAR_CLASS = "evowiki-revbar-host";
const isBar = (n: Node | null): n is HTMLElement => n instanceof HTMLElement && n.classList.contains(REVBAR_CLASS);

/**
 * Whether a batch of mutations only placed our own hosts of these classes (no node of Obsidian's moved). A host that is
 * no longer in the document (Obsidian re-rendered and dropped it) does not count: that one must be put back.
 */
export function onlyHosts(records: readonly MutationRecord[], ...cls: string[]): boolean {
  return records.every((r) => [...r.addedNodes, ...r.removedNodes].every((n) => n instanceof HTMLElement && n.isConnected
    && cls.some((c) => n.classList.contains(c))));
}
const str = (v: unknown): string => (typeof v === "string" ? v.trim() : typeof v === "number" ? String(v) : "");

/** Whether the frontmatter's cssclasses (a list or a string) holds `evowiki-page`. */
function hasPageClass(fm: Record<string, unknown>): boolean {
  const c = fm.cssclasses ?? fm.cssclass;
  const items = Array.isArray(c) ? c : typeof c === "string" ? c.split(/[\s,]+/) : [];
  return items.some((x) => x === PAGE_CSS_CLASS);
}

/** The header of a wiki note (frontmatter evowiki_page_id + cssclasses evowiki-page), null for any other note. */
export function headerInfo(fm: unknown, basename: string): HeaderInfo | null {
  if (fm === null || typeof fm !== "object") return null;
  const f = fm as Record<string, unknown>;
  if (str(f[FM_KEYS.pageId]) === "" || !hasPageClass(f)) return null;
  return { pageId: str(f[FM_KEYS.pageId]), title: str(f[FM_KEYS.title]) || basename, goal: str(f[FM_KEYS.goal]), updated: str(f[FM_KEYS.updated]),
    ...(str(f[FM_KEYS.status]) === STATUS_WRITING ? { writing: true } : {}) };
}

const keyOf = (i: HeaderInfo): string => JSON.stringify([i.pageId, i.title, i.goal, i.updated, i.writing, i.run]);

/**
 * The writing line under the title (`span.ws`, app.js paintWrites): 生成中 (the pulsing dot) + 停止 while this plugin
 * streams the write; 已停止 + 重新写 after 停止; plain 生成中 for a write it does not stream (the Bot's); else nothing.
 */
function writeLine(i: HeaderInfo, hooks: HeaderHooks | null): HTMLElement | null {
  const ws = el("span", "ws");
  if (i.run === "writing" && hooks !== null) {
    const b = btn(() => (WIKI_COPY.stop), "xs", () => hooks.stop());
    b.dataset.action = "stop";
    ws.append(el("i"), labelNode(() => (WIKI_COPY.sectionWritingShort)), b);
    return ws;
  }
  if (i.run === "stopped" && hooks !== null) {
    const b = btn(() => (WIKI_COPY.restart), "xs", () => hooks.restart(i.pageId));
    b.dataset.action = "restart";
    ws.append(labelNode(() => (WIKI_COPY.stopped)), b);
    return ws;
  }
  if (i.writing === true) { ws.append(labelNode(() => (WIKI_COPY.sectionWritingShort))); return ws; }
  return null;
}

/**
 * `div.ew.ew-inline.evowiki-note-header` > `h1` + `dl.ew-meta` (dt 目标 / dd, dt 更新于 / dd; a row without a value is
 * left out), exactly app.js's DOM inside the wrapper.
 */
export function buildHeader(i: HeaderInfo, hooks: HeaderHooks | null = null): HTMLElement {
  const host = el("div");
  host.className = `ew ew-inline ${HEADER_CLASS}`;
  host.dataset.key = keyOf(i);
  const m = el("dl", "meta");
  if (i.goal !== "") m.append(el("dt", "", () => tr("note.header.goal")), el("dd", "", i.goal));
  if (i.updated !== "") m.append(el("dt", "", () => tr("note.header.updated")), el("dd", "", i.updated));
  host.append(el("h1", "", i.title));
  const line = writeLine(i, hooks);
  if (line !== null) host.append(line);
  host.append(m);
  return host;
}

/**
 * Puts the header for `info` into `parent` (before `before`, else at the end) unless the same one is already there;
 * replaces a stale one; removes it when `info` is null. Returns whether the DOM changed.
 */
export function ensureHeader(parent: HTMLElement, info: HeaderInfo | null, before: Node | null = null, hooks: HeaderHooks | null = null): boolean {
  const found = [...parent.children].filter((c) => c.classList.contains(HEADER_CLASS));
  if (info === null) { found.forEach((c) => c.remove()); return found.length > 0; }
  // At the end of .mod-header its place among Obsidian's (hidden) children does not matter; before a node it must be right there.
  // the 用资料改这一页 bar sits between the header and `before`: it does not count as being out of place
  let next = found[0]?.nextSibling ?? null;
  while (isBar(next)) next = next.nextSibling;
  const ok = found.length === 1 && (found[0] as HTMLElement).dataset.key === keyOf(info)
    && (before === null || next === before);
  if (ok) return false;
  found.forEach((c) => c.remove());
  // a new header goes above any bar right before `before`, so the bar never has to move for it
  let at = before !== null && before.parentNode === parent ? before : null;
  while (at !== null && isBar(at.previousSibling)) at = at.previousSibling;
  parent.insertBefore(buildHeader(info, hooks), at);
  return true;
}

/** Reading view: the header's parent (Obsidian's `.mod-header`, else the sizer) inside a preview container. */
export function readingSlot(preview: HTMLElement): { parent: HTMLElement; before: Node | null } | null {
  const sizer = preview.querySelector<HTMLElement>(".markdown-preview-sizer");
  if (sizer === null) return null;
  const mod = sizer.querySelector<HTMLElement>(":scope > .mod-header");
  if (mod !== null) return { parent: mod, before: null };
  const pusher = sizer.querySelector(":scope > .markdown-preview-pusher");
  let before = pusher !== null ? pusher.nextSibling : sizer.firstChild;
  // skip our own nodes (the header, the bar after it): `before` is the first of Obsidian's blocks, as ensureHeader checks it
  while (before instanceof HTMLElement && (before.classList.contains(HEADER_CLASS) || isBar(before))) before = before.nextSibling;
  return { parent: sizer, before };
}

/** Live Preview: before `.cm-contentContainer` in the editor's `.cm-sizer`. */
export function editorSlot(source: HTMLElement): { parent: HTMLElement; before: Node | null } | null {
  const content = source.querySelector<HTMLElement>(".cm-sizer > .cm-contentContainer");
  return content === null || content.parentElement === null ? null : { parent: content.parentElement, before: content };
}

/** Puts / refreshes / removes the header in both of a Markdown view's modes. */
export function applyHeader(viewEl: HTMLElement, info: HeaderInfo | null, hooks: HeaderHooks | null = null): void {
  for (const preview of viewEl.querySelectorAll<HTMLElement>(".markdown-preview-view")) {
    const slot = readingSlot(preview);
    if (slot !== null) ensureHeader(slot.parent, info, slot.before, hooks);
  }
  for (const source of viewEl.querySelectorAll<HTMLElement>(".markdown-source-view")) {
    const slot = editorSlot(source);
    if (slot !== null) ensureHeader(slot.parent, info, slot.before, hooks);
  }
}

/**
 * Keeps one Markdown view's header current: applies it now, and again whenever Obsidian changes the children of the
 * reading sizer / its `.mod-header` or the editor sizer (re-render, scroll virtualisation). Returns the stop function.
 */
export function watchHeader(viewEl: HTMLElement, info: () => HeaderInfo | null, hooks: HeaderHooks | null = null): () => void {
  let busy = false;
  const run = (records?: readonly MutationRecord[]): void => {
    // the bar's own moves never put the header out of place (ensureHeader skips the bar's hosts)
    if (busy || (records !== undefined && onlyHosts(records, REVBAR_CLASS))) return;
    busy = true;
    try { applyHeader(viewEl, info(), hooks); } finally { busy = false; }
  };
  run();
  const win = viewEl.ownerDocument.defaultView;
  if (win === null || typeof win.MutationObserver !== "function") return () => undefined;
  const obs = new win.MutationObserver(run);
  const watch = (): void => {
    for (const s of viewEl.querySelectorAll(".markdown-preview-sizer")) obs.observe(s, { childList: true });
    for (const m of viewEl.querySelectorAll(".markdown-preview-sizer > .mod-header")) obs.observe(m, { childList: true });
    for (const s of viewEl.querySelectorAll(".cm-sizer")) obs.observe(s, { childList: true });
  };
  watch();
  // The sizers themselves are created lazily (the reading view on first switch): watch the view's subtree shallowly too.
  const top = new win.MutationObserver(() => { watch(); run(); });
  for (const c of viewEl.querySelectorAll(".markdown-reading-view, .markdown-source-view")) top.observe(c, { childList: true });
  return () => { obs.disconnect(); top.disconnect(); };
}
