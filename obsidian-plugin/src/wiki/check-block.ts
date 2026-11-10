import { labelNode } from "../i18n/index";
/**
 * The `evowiki-check` code block in a wiki note (note/format.ts writes it): the block names only the page and the
 * section; the question, the options and the answer come from the wiki controller and are drawn here,
 * never written to the file. Answering goes through the controller's answerCheck (POST checks/{sid}), exactly like
 * the sidebar. main.ts only registers the processor and feeds the hub the current controller.
 */
import { btn, el } from "../ui/ew";
import { ewNotice } from "../ui/ew-notice";
import { s as svg } from "../ui/svg";
import { WIKI_COPY } from "./copy";
import { type FollowupHost, renderCheckFollow } from "./followup";
import { errorText } from "./format";
import { isMasteredCheck } from "./goal-checks";
import { letter } from "./render-common";
import { type CheckOut, IDK } from "./schema";
import type { WikiState } from "./state";

type OpenCheckPage = (pageId: string) => void | Promise<void>;
type OpenPageView = Extract<WikiState["page"], { kind: "open" }>;

/** Where a check block points: the page and the section of its question. */
export interface CheckRef { pageId: string; sectionId: string }

/** What a check block reads and calls: WikiController satisfies it. */
export interface CheckSource extends Partial<FollowupHost> {
  readonly state: WikiState;
  subscribe(fn: () => void): () => void;
  answerCheck(sid: string, choice: number): unknown;
}

/** The 粗心答错 / 现在会了 / 还没掌握 row needs the three of the controller's follow-up methods. */
export function followupHostOf(x: Partial<FollowupHost>): FollowupHost | null {
  return x.followupView && x.followup && x.followupBlockedWhy ? x as FollowupHost : null;
}

/** `page: <id>` and `section: <id>` lines (blank lines and other keys ignored); null when either is missing. */
export function parseCheckBlock(source: string): CheckRef | null {
  let pageId: string | null = null, sectionId: string | null = null;
  for (const line of source.split(/\r\n?|\n/)) {
    const m = /^\s*(page|section)\s*:\s*(\S+)\s*$/.exec(line);
    if (!m) continue;
    if (m[1] === "page") pageId = m[2]!;
    else sectionId = m[2]!;
  }
  return pageId !== null && sectionId !== null ? { pageId, sectionId } : null;
}

/**
 * Holds the plugin's current wiki controller (null before the wiki is first used, or after it was disposed) and tells
 * every mounted block when the controller or its state changes. The section 「⋯」 menus (section-menu.ts) read the same
 * hub, so `T` may ask for more than a check block needs.
 */
export class CheckBlockHub<T extends CheckSource = CheckSource> {
  private source: T | null = null;
  private unsub: (() => void) | null = null;
  private readonly listeners = new Set<() => void>();
  /** Read views survive navigation and Markdown remounts; account/controller replacement clears them. */
  private readonly pages = new Map<string, OpenPageView>();
  /** Whether a note (its vault path; null when not known) is the current user's: another user's note gets no blocks (main.ts). */
  owns: (notePath: string | null) => boolean = () => true;

  get current(): T | null { return this.source; }

  set(source: T | null): void {
    if (source === this.source) return;
    this.unsub?.();
    this.unsub = null;
    this.pages.clear();
    this.source = source;
    if (source) this.unsub = source.subscribe(() => this.emit());
    this.emit();
  }

  subscribe(fn: () => void): () => void {
    this.listeners.add(fn);
    return () => { this.listeners.delete(fn); };
  }

  page(pageId: string): OpenPageView | null { return this.pages.get(pageId) ?? null; }

  private emit(): void {
    const p = this.source?.state.page;
    if (p?.kind === "open") this.pages.set(p.page.page_id, p);
    for (const fn of [...this.listeners]) fn();
  }
}

/** app.js ICON_Q (the question mark in a circle); `.ew-check .ew-h svg` gives it size and stroke. */
const questionIcon = (): SVGElement => svg("svg", { viewBox: "0 0 24 24" },
  svg("circle", { cx: 12, cy: 12, r: 9 }), svg("path", { d: "M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .9-1 1.6V14" }), svg("path", { d: "M12 17h.01" }));

/** app.js checkCard: `.ew-check` > `.ew-h`, `.ew-stem`, `.ew-opts` (A–D + dashed 不知道), the follow-ups. */
function card(ref: CheckRef, pageTitle: string, c: CheckOut, s: WikiState, error: string | null, answer: (choice: number) => void, fu: FollowupHost | null): HTMLElement {
  const answered = c.answered !== null;
  const disabled = answered || s.busy !== null;
  const box = el("div", "check");
  box.dataset.answered = String(answered);
  const head = el("div", "h");
  head.append(questionIcon(), labelNode(() => (WIKI_COPY.checkTitle)));
  box.append(head, el("div", "stem", c.stem));
  const opts = el("div", "opts");
  const option = (i: number, label: string, cls: string): HTMLButtonElement => {
    const b = el("button", cls, label);
    b.type = "button";
    b.disabled = disabled;
    // app.js: the right option is .right; the picked one (an option or 不知道), when it is not the right one, is .wrong.
    if (answered && i === c.correct_index) b.classList.add("ew-right");
    else if (answered && i === c.answered) b.classList.add("ew-wrong");
    b.onclick = () => answer(i);
    return b;
  };
  c.options.forEach((o, i) => opts.append(option(i, `${letter(i)}. ${o}`, "o")));
  opts.append(option(IDK, WIKI_COPY.idk, "o idk"));
  box.append(opts);
  // User review: no verdict sentence; the options' colours say it. Wrong or 不知道: the follow-ups under the options
  // (after 不知道 there is no 粗心答错).
  if (fu !== null && answered && c.correct !== true) {
    const target = { kind: "check" as const, pageId: ref.pageId, pageTitle, sid: ref.sectionId, idk: c.answered === IDK, artifactId: c.artifact_id };
    box.append(renderCheckFollow(fu, target));
  }
  if (error !== null && !answered) {
    const e = el("div", "muted", error);
    e.setAttribute("role", "alert");
    box.append(e);
  }
  return box;
}

/** A plain line in place of the card when the question is not at hand. */
const line = (text: string): HTMLElement => el("div", "muted", text);

/** A previously read note keeps its question while another page is active. Unread notes offer to load theirs. */
export function renderCheckBlock(source: string, from: CheckSource | null, open: OpenCheckPage = () => undefined, retained: OpenPageView | null = null): HTMLElement {
  const ref = parseCheckBlock(source);
  if (ref === null) return line(WIKI_COPY.checkUnreadable);
  const opener = (): HTMLElement => {
    const b = btn(() => (WIKI_COPY.checkOpen), "sm", () => { void Promise.resolve(open(ref.pageId)).catch((e: unknown) => ewNotice(errorText(e))); });
    b.dataset.action = "check-open";
    b.disabled = (from?.state.busy ?? null) !== null;
    return b;
  };
  if (from === null) return opener();
  const s = from.state;
  const p = s.page.kind === "open" && s.page.page.page_id === ref.pageId ? s.page : retained ?? s.page;
  if (p.kind === "loading" && p.pageId === ref.pageId) return line(WIKI_COPY.checkLoading);
  if (p.kind !== "open" || p.page.page_id !== ref.pageId) return opener();
  const sec = p.page.sections.find((x) => x.id === ref.sectionId);
  if (!sec || sec.check === null) return line(WIKI_COPY.checkGone);
  if (isMasteredCheck(sec) || sec.folded || (p.page.collapsed && !sec.pinned)) {
    const hidden = el("span"); hidden.hidden = true; return hidden;
  }
  const note = p.sectionNotes[ref.sectionId];
  const error = s.page.kind === "error" && s.page.pageId === ref.pageId ? s.page.message
    : note && note.tone === "error" ? note.text : null;
  const artifact = sec.check.artifact_id;
  const answer = async (choice: number): Promise<void> => {
    if (from.state.busy !== null) return;
    const current = from.state.page;
    if (current.kind !== "open" || current.page.page_id !== ref.pageId) await open(ref.pageId);
    const loaded = from.state.page;
    // Navigation may win while loading, or a rewrite may replace the question: never apply an old choice elsewhere.
    if (loaded.kind !== "open" || loaded.page.page_id !== ref.pageId
      || loaded.page.sections.find((x) => x.id === ref.sectionId)?.check?.artifact_id !== artifact) return;
    await from.answerCheck(ref.sectionId, choice);
  };
  return card(ref, p.page.title, sec.check, s, error, (choice) => { void answer(choice).catch((e: unknown) => ewNotice(errorText(e))); }, followupHostOf(from));
}

/** Draws the block into `host` (inside a `div.ew.ew-inline`) and redraws it on every hub change; returns the cleanup. */
export function mountCheckBlock(host: HTMLElement, source: string, hub: CheckBlockHub, open: OpenCheckPage = () => undefined): () => void {
  const root = el("div");
  root.className = "ew ew-inline evowiki-check";
  host.replaceChildren(root);
  const ref = parseCheckBlock(source);
  const draw = () => {
    const content = renderCheckBlock(source, hub.current, open, ref === null ? null : hub.page(ref.pageId));
    host.hidden = content.hidden;
    root.replaceChildren(...(content.hidden ? [] : [content]));
  };
  draw();
  return hub.subscribe(draw);
}
