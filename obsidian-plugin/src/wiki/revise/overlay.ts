import { localizedAttribute } from "../../i18n/index";
/**
 * Review mode of a wiki note (UI principles 2026-10-21 §三; full-demo app.js revBar / trackEls): while the page has a ready
 * `page_revise` draft, the note SHOWS the draft as track changes. It is a display overlay only: the note file is never
 * written here (the draft reaches it through the note sync after 确认替换).
 *
 * - The sticky bar (drawn exactly when the note has marks) 「按《X》改了 N 节」(+ 从资料重写) + circled ? + 确认替换 / 取消 sits at the top of the page, under the
 *   header, in the view's sizer (Reading view: `.markdown-preview-sizer`; Live Preview: `.cm-sizer`), so it stays in
 *   reach while the page scrolls. `watchReviseBar` keeps it there as Obsidian rebuilds the sizers (like the header).
 * - The marks: Reading view — one post-processor; a block inside a replaced range hides its own content and the block
 *   where the range starts shows the marked text. Live Preview — one CM6 state field of block widgets (replace / after),
 *   rebuilt when the plan for the document changes (a view plugin tells it when the ledger moves). A paragraph is
 *   rendered as the note renders it and marked on the rendered text (marks.ts), so its Markdown keeps its look.
 * - A ⟦和资料不同：…⟧ paragraph the page already has is drawn with its 和资料不同 label (Reading view).
 */
import { type EditorState, type Extension, type Range, StateEffect, StateField } from "@codemirror/state";
import { Decoration, type DecorationSet, EditorView, ViewPlugin, WidgetType } from "@codemirror/view";
import { Component, editorLivePreviewField, type MarkdownPostProcessorContext, MarkdownRenderChild } from "obsidian";
import { btn, el } from "../../ui/ew";
import { qTip } from "../q-tip";
import { WIKI_START } from "../note/format";
import { HEADER_CLASS, onlyHosts, REVBAR_CLASS } from "../note-page/header";
import { ownedNote } from "../note-doc";
import { noteHeadings } from "../section-menu";
import type { WikiState } from "../state";
import { REVISE_COPY as T } from "./copy";
import { type MdRender, renderedPart } from "./marks";
import { type DiffOp, noteText, type RevisePlan, revisePlan, type TrackPart, type TrackPiece } from "./model";
import { barTitle, reviseTip } from "./rows";
import type { DraftDecisions, DraftOp } from "./decisions";
import { pageWaitActions, waitBar } from "../page-waits";
import { writeBlocked } from "../actions";

/** Renders note Markdown into an element for an owner (main.ts: MarkdownRenderer.render(app, md, into, "", owner)). */
export type OwnedRender = (md: string, into: HTMLElement, owner: Component) => Promise<void>;

/** What the overlay reads and calls: WikiController satisfies it. */
export interface ReviseSource {
  readonly state: WikiState;
  reviseDecide(actionId: string, op: "approve" | "reject" | "undo"): unknown;
  /** 重试 of a draft that no longer fits the page (written again for the page as it is now); absent = no 重试. */
  reviseRedraft?(actionId: string): unknown;
  /** The decisions shared with the chat's cards (one decision, two places); absent = the bar keeps its own busy. */
  readonly draftDecisions?: DraftDecisions | undefined;
  /** A ledger action waiting on this page (page-waits.ts): 确认… / 取消; absent = those bars are not drawn. */
  botDecide?(actionId: string, op: "approve" | "reject"): unknown;
}
export interface ReviseHub {
  readonly current: ReviseSource | null; subscribe(fn: () => void): () => void;
  /** Whether a note (its vault path) is the current user's (CheckBlockHub.owns); absent = every note. */
  owns?(notePath: string | null): boolean;
}

// ---- DOM ----

/** app.js `qTip`: the shared circled ? (placed, one at a time, Esc / outside click close it). */
export { qTip };

/** 删去 / 新增 marks (`<del>` / `<ins>`; the CSS adds the hidden 删去：/ 新增： for screen readers). */
function marked(parent: HTMLElement, ops: readonly DiffOp[]): void {
  for (const [k, t] of ops) parent.append(k === "=" ? document.createTextNode(t) : el(k === "-" ? "del" : "ins", "", t));
}

/** app.js srcNote: the 和资料不同 label, then the text. */
export function srcNote(text: string): HTMLParagraphElement {
  const p = el("p", "srcnote");
  p.append(el("span", "snl", () => (T.noteLabel)), text);
  return p;
}

/**
 * One piece's marked elements (app.js trackEls): headings with marks, paragraphs rendered and marked (marks.ts), an added
 * note as a whole green line.
 */
export function trackParts(parts: readonly TrackPart[], render: MdRender): HTMLElement[] {
  return parts.map((x) => {
    if (x.kind === "note") {
      if (!x.added) return srcNote(x.text);
      const ins = el("ins", "insblk");
      ins.append(srcNote(x.text));
      return ins;
    }
    if (x.kind === "p") {
      const e = renderedPart(x.old, x.new, render);
      // an added EvoUI block (the assistant's practice draft, inline): drawn live by the note's own block processor, so it
      // can be tried before 确认替换; inline, it reports nothing (full-demo app.js pracDraftEl)
      // a library component (its embed fence; the asset is fetched when the draft is read: controller/revise.ts) the same way
      if (x.old === null && x.new !== null && /^```(evoui|embedded-html)\b/.test(x.new.trim())) e.classList.add("ew-prac");
      return e;
    }
    const e = el("h2", "trk");
    marked(e, x.ops);
    return e;
  });
}

/** `div.ew.ew-inline.evowiki-trk-host` holding the marked parts. */
export function trackHost(parts: readonly TrackPart[], render: MdRender): HTMLElement {
  const host = el("div");
  host.className = "ew ew-inline evowiki-trk-host";
  host.append(...trackParts(parts, render));
  return host;
}

/**
 * app.js revBar: the sticky bar of a page in review mode. `busy`: a decision is being sent (both buttons wait). A stale
 * draft (the page changed where it goes: it cannot be put in as it is) offers no 确认替换 and says nothing: 取消 and a quiet
 * 重试 that writes it again for the page as it is now (`redraft`; app.js draftBtns `a.dead`). `failed`: the last decision
 * did not go through: a quiet 请重试 after the buttons sends it again.
 */
export function buildBar(plan: Pick<RevisePlan, "actionId" | "revise" | "why"> & Partial<Pick<RevisePlan, "source">>, busy: boolean,
  decide: (op: DraftOp) => void, failed: DraftOp | null = null, redraft = true): HTMLElement {
  const b = el("div", "revbar");
  b.setAttribute("role", "region");
  localizedAttribute(b, "aria-label", () => (T.barAria));
  b.dataset.action = plan.actionId;
  b.append(el("span", "rb-t", barTitle(plan.revise)));
  if (plan.revise.rebuild) b.append(el("span", "chip", () => (T.rebuild)));
  b.append(qTip(reviseTip({ why: plan.why, source: plan.source }, plan.revise), () => (T.barTip)));
  const acts = el("span", "rb-acts");
  const no = btn(() => (T.cancel), "sm", () => decide("reject"));
  no.disabled = busy;
  no.dataset.op = "reject";
  if (plan.revise.stale) {
    acts.append(no);
    if (redraft) {
      const r = btn(() => (T.redraft), "link", () => decide("redraft"));
      r.classList.add("ew-retry");
      r.dataset.op = "redraft";
      r.disabled = busy;
      acts.append(r);
    }
  } else {
    const ok = btn(() => (T.confirm), "primary sm", () => decide("approve"));
    ok.disabled = busy;
    ok.dataset.op = "approve";
    acts.append(ok, no);
  }
  if (failed !== null && !busy && !(failed === "redraft" && plan.revise.stale)) {
    const r = btn(() => (T.retryDecision), "link", () => decide(failed));
    r.classList.add("ew-retry");
    r.dataset.op = "retry";
    acts.append(r);
  }
  b.append(acts);
  return b;
}

// ---- the plan, cached per state and text ----

let memo: { s: WikiState; pageId: string; text: string; plan: RevisePlan | null } | null = null;
function planFor(s: WikiState, pageId: string, text: string): RevisePlan | null {
  if (memo !== null && memo.s === s && memo.pageId === pageId && memo.text === text) return memo.plan;
  const plan = revisePlan(s, pageId, text);
  memo = { s, pageId, text, plan };
  return plan;
}
const pieceKey = (x: TrackPiece): string => JSON.stringify(x);

// ---- the bar in a Markdown view ----

/** The bar for one view: built on `update()` from the hub; nothing while the page has no ready draft. */
export interface BarDrawn { el: HTMLElement; update(): void }

/**
 * `text`: the note the view shows. The draft's bar is drawn exactly when the marks are (a plan for this note text: the open
 * page, a ready draft), so a split showing another note never offers 确认替换 / 取消 without the marks. Under it, one bar
 * per ledger action waiting on this page (page-waits.ts: 确认… / 取消), whatever the note text.
 */
export function createBar(pageId: () => string | null, hub: ReviseHub, text: () => string | null): BarDrawn {
  const host = el("div");
  host.className = `ew ew-inline ${REVBAR_CLASS}`;
  let busy: string | null = null;
  let shown = "";
  function update(): void {
    const src = hub.current, pid = pageId(), t = text();
    const plan = src === null || pid === null || t === null ? null : planFor(src.state, pid, t);
    // shared with the chat's card: decided there (or here) is decided, the bar goes at once
    const shared = src?.draftDecisions, dec = plan === null ? null : shared?.get(plan.actionId) ?? null;
    const row = dec?.settled ? null : plan;
    if (row === null) busy = null;
    const acts = src === null || pid === null || src.botDecide === undefined ? [] : pageWaitActions(src.state, pid);
    const off = src === null || writeBlocked(src.state) || src.state.demo;
    const waiting = row === null ? false : dec !== null ? dec.busy !== null : busy === row.actionId, failed = dec?.failed ?? null;
    const sig = JSON.stringify([row === null ? null : [row.actionId, row.revise.sections.length, row.revise.rebuild, row.revise.sources, row.revise.stale, row.why, waiting, failed],
      acts.map((x) => [x.action_id, x.status, x.summary, x.why]), acts.length > 0 && off]);
    if (sig === shown) return;
    shown = sig;
    const waits = acts.map((x) => waitBar(x, off, (id, op) => { void src?.botDecide?.(id, op); }));
    if (row === null) { host.replaceChildren(...waits); return; }
    const send = (op: DraftOp): unknown => (op === "redraft" ? src!.reviseRedraft?.(row.actionId) : src!.reviseDecide(row.actionId, op));
    host.replaceChildren(buildBar(row, waiting, (op) => {
      if (shared !== undefined) { void send(op); return; }
      if (busy !== null) return;
      busy = row.actionId;
      update();
      void Promise.resolve(send(op)).finally(() => { busy = null; shown = ""; update(); });
    }, failed, src?.reviseRedraft !== undefined), ...waits);
  }
  update();
  return { el: host, update };
}

/** Where the bar goes: after the header in the reading sizer (Obsidian's `.mod-header`, or ours), or before the editor's content. */
function slots(viewEl: HTMLElement): { parent: HTMLElement; before: Node | null }[] {
  const out: { parent: HTMLElement; before: Node | null }[] = [];
  for (const sizer of viewEl.querySelectorAll<HTMLElement>(".markdown-preview-view .markdown-preview-sizer")) {
    const head = sizer.querySelector(":scope > .mod-header") ?? sizer.querySelector(`:scope > .${HEADER_CLASS}`);
    let before = head !== null ? head.nextSibling : sizer.querySelector(":scope > .markdown-preview-pusher")?.nextSibling ?? sizer.firstChild;
    while (before instanceof HTMLElement && before.classList.contains(REVBAR_CLASS)) before = before.nextSibling;
    out.push({ parent: sizer, before });
  }
  for (const content of viewEl.querySelectorAll<HTMLElement>(".markdown-source-view .cm-sizer > .cm-contentContainer")) {
    if (content.parentElement !== null) out.push({ parent: content.parentElement, before: content });
  }
  return out;
}

/**
 * Keeps the bar in both modes of one Markdown view (one bar per mode), current with the hub, and put back when Obsidian
 * rebuilds a sizer. `pageId`: the note's page id (null: not a wiki note). Returns the stop function.
 *
 * Idempotent: a bar already in its place is never moved, and the bar's own insertions are ignored, so this watcher and
 * the header's (note-page/header.ts, which puts the header above the bar) settle after one round.
 */
export function watchReviseBar(viewEl: HTMLElement, pageId: () => string | null, hub: ReviseHub, text: () => string | null): () => void {
  const bars = new Map<HTMLElement, BarDrawn>();
  let busy = false;
  const place = (records?: readonly MutationRecord[]): void => {
    // our own hosts moving (this bar, the header above it) never puts a bar out of place
    if (busy || (records !== undefined && onlyHosts(records, REVBAR_CLASS, HEADER_CLASS))) return;
    busy = true;
    try {
      const live = new Set<HTMLElement>();
      for (const { parent, before } of slots(viewEl)) {
        live.add(parent);
        let bar = bars.get(parent);
        if (bar === undefined) { bar = createBar(pageId, hub, text); bars.set(parent, bar); }
        if (bar.el.parentElement !== parent || (before !== bar.el && bar.el.nextSibling !== before)) parent.insertBefore(bar.el, before);
      }
      for (const [p, bar] of bars) if (!live.has(p)) { bar.el.remove(); bars.delete(p); }
    } finally { busy = false; }
  };
  place();
  const unsub = hub.subscribe(() => { for (const b of bars.values()) b.update(); });
  const win = viewEl.ownerDocument.defaultView;
  if (win === null || typeof win.MutationObserver !== "function") return () => { unsub(); for (const b of bars.values()) b.el.remove(); };
  const obs = new win.MutationObserver(place);
  const watch = (): void => {
    for (const s of viewEl.querySelectorAll(".markdown-preview-sizer, .cm-sizer")) obs.observe(s, { childList: true });
  };
  watch();
  const top = new win.MutationObserver(() => { watch(); place(); });
  for (const c of viewEl.querySelectorAll(".markdown-reading-view, .markdown-source-view")) top.observe(c, { childList: true });
  return () => { unsub(); obs.disconnect(); top.disconnect(); for (const b of bars.values()) b.el.remove(); bars.clear(); };
}

// ---- Reading view ----

const OFF = "evowiki-rv-off";

/** A rendered paragraph that is a ⟦和资料不同：…⟧ line: drawn with its label (the note text is unchanged). */
function styleNote(el0: HTMLElement): void {
  for (const p of el0.querySelectorAll<HTMLParagraphElement>(":scope > p")) {
    const t = noteText(p.textContent ?? "");
    if (t === null) continue;
    const host = el("div");
    host.className = "ew ew-inline evowiki-srcnote-host";
    host.append(srcNote(t));
    p.replaceWith(host);
  }
}

/**
 * One rendered block of a wiki note: hidden while a replaced range covers it; the marked text where a piece starts in it.
 * The block's lines are asked again on every update (an edit above it moves them). Returns the cleanup.
 */
export function mountReadingBlock(block: HTMLElement, pageId: string, lines: () => { text: string; lineStart: number; lineEnd: number } | null,
  hub: ReviseHub, render: OwnedRender): () => void {
  // Obsidian may post-process the same block again: the earlier mount goes first (one host per block)
  mounted.get(block)?.();
  const host = el("div");
  host.className = "ew ew-inline evowiki-trk-host";
  let shown = "";
  // what the renderer attaches to the marks shown now (math, embeds): unloaded when they are redrawn or the mount stops
  let owner: Component | null = null;
  function update(): void {
    const src = hub.current, info = lines();
    const plan = src === null || info === null ? null : planFor(src.state, pageId, info.text);
    let off = false;
    const mine: TrackPiece[] = [];
    if (plan !== null && info !== null) {
      for (const x of plan.pieces) {
        if (x.at.kind === "replace") {
          // any overlap hides the whole block: a block that runs past the range never shows its old text next to the marks
          if (x.at.from <= info.lineEnd && info.lineStart <= x.at.to) off = true;
          if (x.at.from >= info.lineStart && x.at.from <= info.lineEnd) mine.push(x);
        } else if (x.at.line >= info.lineStart && x.at.line <= info.lineEnd) mine.push(x);
      }
    }
    const sig = JSON.stringify([off, mine.map(pieceKey)]);
    if (sig === shown) return;
    shown = sig;
    block.classList.toggle(OFF, off);
    owner?.unload();
    const o = new Component();
    o.load();
    owner = o;
    host.replaceChildren(...mine.flatMap((x) => trackParts(x.parts, (md, into) => render(md, into, o))));
    if (mine.length > 0) { if (host.parentElement !== block) block.append(host); } else host.remove();
  }
  update();
  const unsub = hub.subscribe(update);
  let done = false;
  // idempotent, and a mount a newer one replaced never touches the block (its hidden class belongs to the newer one)
  const stop = (): void => {
    if (done) return;
    done = true;
    unsub();
    host.remove();
    owner?.unload();
    owner = null;
    if (mounted.get(block) !== stop) return;
    block.classList.remove(OFF);
    mounted.delete(block);
  };
  mounted.set(block, stop);
  return stop;
}
/** The mount of each rendered block (mountReadingBlock). */
const mounted = new WeakMap<HTMLElement, () => void>();

/** Reading view: every block of a v2 wiki note follows the review mode; ⟦和资料不同⟧ lines get their label. */
export function reviseReadingPostProcessor(hub: ReviseHub, render: OwnedRender) {
  return (block: HTMLElement, ctx: MarkdownPostProcessorContext): void => {
    const info = ctx.getSectionInfo(block);
    if (info === null || !info.text.includes(WIKI_START)) return;
    const nh = noteHeadings(info.text);
    if (nh === null) return;
    styleNote(block);
    const child = new MarkdownRenderChild(block);
    child.register(mountReadingBlock(block, nh.pageId, () => ctx.getSectionInfo(block), hub, render));
    ctx.addChild(child);
  };
}

// ---- Live Preview ----

/**
 * The owner of what the renderer attached to a widget's DOM, keyed by that DOM: CodeMirror may hand the DOM to an equal
 * widget instance (`eq`) and call `destroy` on that one, so the owner cannot live on the instance that built it.
 */
const widgetOwners = new WeakMap<HTMLElement, Component>();

export class TrackWidget extends WidgetType {
  constructor(private readonly key: string, private readonly parts: readonly TrackPart[], private readonly render: OwnedRender) { super(); }
  override eq(o: TrackWidget): boolean { return o.key === this.key; }
  // a real block box (CodeMirror measures it); the display:contents host goes inside. `markdown-rendered`: the rendered
  // paragraphs get the note's own Markdown styles (lists, code) as in Reading view
  override toDOM(): HTMLElement {
    const w = document.createElement("div");
    w.className = "evowiki-trk-widget markdown-rendered";
    // what the renderer attaches (math, embeds) lives as long as this widget's DOM
    const owner = new Component();
    owner.load();
    widgetOwners.set(w, owner);
    w.append(trackHost(this.parts, (md, into) => this.render(md, into, owner)));
    return w;
  }
  override destroy(dom: HTMLElement): void { widgetOwners.get(dom)?.unload(); widgetOwners.delete(dom); }
  override ignoreEvent(): boolean { return true; }
}

const refresh = StateEffect.define<null>();
interface Built { decorations: DecorationSet; sig: string }

function build(state: EditorState, hub: ReviseHub, render: OwnedRender): Built {
  const src = hub.current;
  if (src === null || !state.field(editorLivePreviewField, false) || !ownedNote(state, hub)) return { decorations: Decoration.none, sig: "" };
  const text = state.doc.toString();
  if (!text.includes(WIKI_START)) return { decorations: Decoration.none, sig: "" };
  const nh = noteHeadings(text);
  const plan = nh === null ? null : planFor(src.state, nh.pageId, text);
  if (plan === null) return { decorations: Decoration.none, sig: "" };
  const doc = state.doc;
  const ranges: Range<Decoration>[] = [];
  for (const x of plan.pieces) {
    const key = pieceKey(x);
    const w = new TrackWidget(key, x.parts, render);
    if (x.at.kind === "replace") {
      if (x.at.to + 1 > doc.lines) continue;
      ranges.push(Decoration.replace({ widget: w, block: true }).range(doc.line(x.at.from + 1).from, doc.line(x.at.to + 1).to));
    } else {
      if (x.at.line + 1 > doc.lines) continue;
      ranges.push(Decoration.widget({ widget: w, block: true, side: 1 }).range(doc.line(x.at.line + 1).to));
    }
  }
  return { decorations: Decoration.set(ranges, true), sig: plan.pieces.map(pieceKey).join("\n") };
}

/** The plan's signature for a document, as the field would build it (the view plugin compares it to decide on a rebuild). */
function sigOf(state: EditorState, hub: ReviseHub, render: OwnedRender): string { return build(state, hub, render).sig; }

/** Live Preview (not Source mode): the marks of a note in review mode. Nothing outside a wiki note. */
export function reviseEditorExtension(hub: ReviseHub, render: OwnedRender): Extension {
  const field = StateField.define<Built>({
    create: (state) => build(state, hub, render),
    update(v, tr) {
      const mode = tr.startState.field(editorLivePreviewField, false) !== tr.state.field(editorLivePreviewField, false);
      if (mode || tr.docChanged || tr.effects.some((e) => e.is(refresh))) return build(tr.state, hub, render);
      return v;
    },
    provide: (f) => EditorView.decorations.from(f, (v) => v.decorations),
  });
  // the ledger moved (a draft is ready, confirmed, cancelled): the field is rebuilt when this document's marks change
  const follow = ViewPlugin.define((view) => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    const unsub = hub.subscribe(() => {
      if (timer !== null) return;
      timer = setTimeout(() => {
        timer = null;
        if (sigOf(view.state, hub, render) !== view.state.field(field).sig) view.dispatch({ effects: refresh.of(null) });
      }, 0);
    });
    return { destroy() { unsub(); if (timer !== null) clearTimeout(timer); } };
  });
  return [field, follow];
}
