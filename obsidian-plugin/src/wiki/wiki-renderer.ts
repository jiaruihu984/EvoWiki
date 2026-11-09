import { labelNode } from "../i18n/index";
import { localizedAttribute } from "../i18n/index";
/**
 * The EvoWiki sidebar (shell.html `aside.side`, app.js `sideTab`), inside a `.ew` wrapper:
 * `aside.ew-side` > `.ew-side-head` (`.ew-brand` star + EvoWiki, `.ew-seg` role=tablist: 今天 / 助手 / 参数 with
 * aria-selected and the red count `.ew-cnt`; offline, then the one 未连接到服务器 · 重试 line), then the tab slot (`div` min-height:0) holding the shown tab: 今天's
 * `.ew-side-body`, 参数's `.ew-side-body.ew-set.ew-side-set`, or the assistant panel's root. The header is built once
 * (only its attributes change); the 助手 panel is mounted once and only shown or hidden, so its input, caret and
 * streaming transcript are untouched by wiki updates. update() rebuilds the other tab bodies from the state but keeps
 * the focused field (and its caret), the open <details> and the body's scroll position.
 */
import { btn, el } from "../ui/ew";
import type { AccountState } from "../account";
import { WIKI_COPY } from "./copy";
import type { CloudSlot, RenderCtx, WikiActions } from "./actions";
import type { Counts } from "./dots";
import { renderEngine } from "./render-engine";
import { offlineBanner } from "./render-common";
import { renderToday } from "./render-today";
import { progressOf, updateSourceCard } from "./sources/render-sources";
import { type Tab, TABS, type WikiState } from "./state";
import { TODAY_COPY } from "./today-copy";

const TAB_LABEL: Record<Tab, string> = { get today() { return TODAY_COPY.tabToday; }, get assistant() { return TODAY_COPY.tabAssistant; }, get engine() { return TODAY_COPY.tabEngine; } };
const SVG = "http://www.w3.org/2000/svg";
/** shell.html line 261: the brand star. */
const STAR = "M12 3l2.6 5.6 6 .7-4.5 4.1 1.2 6L12 16.5 6.7 19.4l1.2-6L3.4 9.3l6-.7z";

function brandMark(): SVGSVGElement {
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  const path = document.createElementNS(SVG, "path");
  path.setAttribute("d", STAR);
  svg.append(path);
  return svg;
}
/** Attributes that identify a control across rebuilds (with its tag). */
const KEY_ATTRS = ["aria-label", "data-action", "data-tab", "data-id", "data-sid", "data-cell", "data-concept", "data-value", "data-q",
  "data-choice", "data-followup"];

interface Focus { key: string; start: number | null; end: number | null }

const focusKey = (e: Element): string => [e.tagName, ...KEY_ATTRS.map((k) => e.getAttribute(k) ?? "")].join("|");
/** A <details>' own data-details-key when it has one (its summary text may change), else its class and summary text. */
const detailsKey = (d: HTMLDetailsElement): string => (d.dataset.detailsKey !== undefined ? `key|${d.dataset.detailsKey}`
  : [d.className, d.querySelector("summary")?.textContent ?? ""].join("|"));

export class WikiRenderer {
  /** The `.ew` wrapper (the view's whole content). */
  readonly root: HTMLDivElement = el("div");
  private readonly side: HTMLElement = el("aside", "side");
  private readonly tabs = new Map<Tab, HTMLButtonElement>();
  /** shell.html `<div id="side" style="min-height:0">`: the shown tab's body. */
  private readonly slot: HTMLDivElement = el("div");
  private body: HTMLElement | null = null;
  private bodyTab: Tab | null = null;
  /**
   * The one offline line of the whole sidebar (今天 / 助手 / 参数): 未连接到服务器 + 重试, last in the header, hidden
   * while connected. The tab bodies say nothing about being offline (their failed reads stay empty).
   */
  private readonly offline: HTMLDivElement = el("div");
  private connectionStatus = "";
  /** Open / closed state of every <details> seen so far, by key (kept across tab switches). */
  private readonly details = new Map<string, boolean>();

  /**
   * `assistant`: the assistant panel's root, shown as the 助手 tab (null: the tab is empty). `cloud`: the small
   * knowledge cloud of 今天, persistent nodes moved across redraws (null: 今天 shows the block without it).
   */
  constructor(private readonly actions: WikiActions, private readonly ctx: () => RenderCtx,
              private readonly assistant: HTMLElement | null = null, private readonly cloud: CloudSlot | null = null) {
    this.root.className = "ew";
    const head = el("div", "side-head");
    const brand = el("div", "brand");
    brand.append(brandMark(), labelNode(() => (TODAY_COPY.brand)));
    const seg = el("div", "seg");
    seg.setAttribute("role", "tablist");
    for (const t of TABS) {
      const b = el("button", null, () => (TAB_LABEL[t]));
      b.type = "button";
      b.setAttribute("role", "tab");
      b.setAttribute("aria-selected", "false");
      b.dataset.tab = t;
      b.onclick = () => this.actions.setTab(t);
      this.tabs.set(t, b);
      seg.append(b);
    }
    head.append(brand, seg, this.offline);
    this.offline.hidden = true;
    this.slot.style.minHeight = "0";
    // A grid item defaults to min-width:auto: one long unbreakable line (a prompt's text in 参数) would widen the whole
    // sidebar past the pane and clip every row's control. min-width 0 keeps the body at the pane's width.
    this.slot.style.minWidth = "0";
    if (assistant) { assistant.style.display = "none"; this.slot.append(assistant); }
    this.side.append(head, this.slot);
    this.root.append(this.side);
  }

  update(s: WikiState, counts: Counts = { today: 0, engine: 0, log: 0 }, account: AccountState | null = null): void {
    const tab = s.tab;
    for (const [t, b] of this.tabs) {
      b.setAttribute("aria-selected", String(t === tab));
      // app.js `updateDots`: the label, then the red count when there is one; the aria-label names it.
      const k = t === "today" ? counts.today : t === "engine" ? counts.engine : 0;
      b.replaceChildren(TAB_LABEL[t]);
      if (k > 0) b.append(el("span", "cnt", String(k)));
      localizedAttribute(b, "aria-label", () => (k > 0 ? TODAY_COPY.tabCountLabel(TAB_LABEL[t], k) : TAB_LABEL[t]));
    }
    const off = s.conn === "offline";
    const message = account?.kind === "error" ? account.message : account?.kind === "loading" ? WIKI_COPY.loading : null;
    const status = message === null ? (off ? "offline" : "") : `${account?.kind}:${message}`;
    if (status !== this.connectionStatus) {
      this.connectionStatus = status;
      let line = offlineBanner(off, () => this.actions.retryConnection());
      if (message !== null) {
        line = el("div", "muted");
        line.dataset.role = "account-status";
        line.setAttribute("role", account?.kind === "error" ? "alert" : "status");
        line.append(el("span", "", message));
        if (account?.kind === "error") {
          const retry = btn(() => (WIKI_COPY.retry), "link", () => this.actions.retryConnection());
          retry.dataset.action = "retry-connection";
          line.append(" ", retry);
        }
      }
      this.offline.replaceChildren(...(line ? [line] : []));
      this.offline.hidden = line === null;
    }
    this.slot.hidden = account?.kind === "error" || account?.kind === "loading";
    this.slot.inert = this.slot.hidden;
    const focus = this.captureFocus();
    if (this.body !== null) for (const d of this.body.querySelectorAll("details")) this.details.set(detailsKey(d), d.open);
    const scroll = this.body !== null && this.bodyTab === tab ? this.body.scrollTop : 0;
    this.body?.remove();
    this.body = null;
    this.bodyTab = null;

    if (this.assistant) this.assistant.style.display = tab === "assistant" ? "" : "none";
    if (tab !== "assistant") {
      const ctx: RenderCtx = this.cloud ? { ...this.ctx(), cloud: this.cloud } : this.ctx();
      const body = tab === "engine" ? renderEngine(s, this.actions, counts.log) : renderToday(s, this.actions, ctx);
      this.slot.insertBefore(body, this.assistant && this.assistant.parentElement === this.slot ? this.assistant : null);
      this.body = body;
      this.bodyTab = tab;
      for (const d of body.querySelectorAll("details")) {
        const open = this.details.get(detailsKey(d));
        if (open !== undefined) d.open = open;
      }
      body.scrollTop = scroll;
    }
    // The cloud was moved (or left out): it picks up whether it is on screen and reads its data once shown.
    this.cloud?.sync();
    if (focus) this.restoreFocus(focus);
  }

  /**
   * A source's progress changed without a redraw (SourcesController.follow): only the reading cards' status text and bar
   * change, in place; the panel, its scroll, focus and any open rename field stay as they are.
   */
  progress(s: WikiState): void {
    if (this.body === null || this.bodyTab !== "engine" || s.sources.kind !== "ready") return;
    for (const x of s.sources.value) if (x.status === "reading") updateSourceCard(this.body, x, progressOf(s, x));
  }

  private captureFocus(): Focus | null {
    const a = document.activeElement;
    if (!a || this.body === null || !this.body.contains(a)) return null;
    let start: number | null = null;
    let end: number | null = null;
    if (a instanceof HTMLInputElement || a instanceof HTMLTextAreaElement) {
      try { start = a.selectionStart; end = a.selectionEnd; } catch { /* no selection on this input type */ }
    }
    return { key: focusKey(a), start, end };
  }

  private restoreFocus(f: Focus): void {
    if (this.body === null) return;
    const e = [...this.body.querySelectorAll("input, textarea, select, button")].find((x) => focusKey(x) === f.key);
    if (!(e instanceof HTMLElement)) return;
    e.focus();
    if ((e instanceof HTMLInputElement || e instanceof HTMLTextAreaElement) && f.start !== null && f.end !== null) {
      try { e.setSelectionRange(f.start, f.end); } catch { /* no selection on this input type */ }
    }
  }
}
