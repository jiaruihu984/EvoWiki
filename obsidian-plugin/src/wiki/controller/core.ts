/**
 * The context every part of the wiki controller shares (controller.ts is the facade): the deps, the view state and its
 * one setter (`set` notifies the subscribers), the page token and the running stream's abort, the single-flight `busy`
 * bookkeeping, and the small helpers that put a write's result where it shows (P4). The areas get this object and the
 * layers below them (notes, reads, streams); they never reach into each other.
 */
import { WIKI_COPY } from "../copy";
import { errorText, type Heading } from "../format";
import { NameBook } from "../names";
import { WritesWatcher } from "../writes-watcher";
import type { FollowupView } from "../followup";
import type { PageOut, SectionOut } from "../schema";
import {
  INITIAL_WIKI_STATE, type Busy, type PageView, type PathSlot, type ResultNote, type ReviewSlot, type WikiState,
} from "../state";
import type { WikiControllerDeps } from "./deps";
import { type Spot, type Area, type Target, type Ctx, refreshText, bad, type Mark, withMarks, freshView } from "./helpers";

export class WikiCore {
  constructor(public deps: WikiControllerDeps) {
    // Own review / bot slots: barAt (renderer) and allDoneSeen (view) are written in place, and the initial state is shared.
    this.state = { ...INITIAL_WIKI_STATE, review: { ...INITIAL_WIKI_STATE.review }, bot: { ...INITIAL_WIKI_STATE.bot }, sourceUi: { ...INITIAL_WIKI_STATE.sourceUi, notes: {} }, conn: deps.conn.state, demo: deps.demo, configured: deps.configured(),
      recommendations: deps.recommendations ?? null };
  }

  /** Names collected from everything loaded so far (P7); renderers read it through RenderCtx. */
  readonly names = new NameBook();
  /** followupKey → the `answer_ref` of that question's latest answer (lost on a restart: the server then takes the latest wrong answer). */
  readonly answerRefs = new Map<string, string>();
  state: WikiState;
  readonly listeners = new Set<() => void>();
  /**
   * A page's organize stream as it writes (the server's own step states: its outline, then each section_done): `done` of
   * `of` sections, `ended` once the stream is over (written, failed or stopped). 学点新的's page cards fill with it.
   */
  readonly writeProgress = new Set<(p: { pageId: string; done: number; of: number; ended: boolean; ok: boolean }) => void>();
  noteWriteProgress: { pageId: string; pass: number | null; section: number | null; of: number | null } | null = null;
  emitWriteProgress(p: { pageId: string; done: number; of: number; ended: boolean; ok: boolean }): void {
    if (this.noteWriteProgress?.pageId === p.pageId) this.noteWriteProgress = p.ended ? null : { ...this.noteWriteProgress, section: p.done, of: p.of };
    for (const fn of [...this.writeProgress]) { try { fn(p); } catch (e) { console.warn("EvoWiki: a write progress listener failed", e); } }
  }
  disposed = false;
  /** Bumped by every page switch (openWikiPage, new page) and by dispose; older page results are dropped. */
  pageToken = 0;
  abort: AbortController | null = null;
  /** The page whose write run `abort`'s stream follows (「停止」 cancels that run on the server). */
  abortPage: string | null = null;
  /** Bumped whenever a write starts (busy goes from null to set): a read started before it may be older than it. */
  writeEpoch = 0;
  /** Resolved when busy goes back to null (or on dispose). */
  idleWaiters: (() => void)[] = [];
  /** Pending timers of the 读完了 block (notices, polls); dispose() ends them. */
  readonly sleepers = new Set<{ timer: ReturnType<typeof setTimeout>; resolve: () => void }>();
  /** The one background poll of the page writes (real mode only): files follow the Bot's writes. */
  watcher: WritesWatcher | null = null;

  subscribe(fn: () => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  /** A timer the controller can end: resolves after `ms`, or at once on dispose (callers check `disposed`). */
  sleep(ms: number): Promise<void> {
    return new Promise((resolve) => {
      const sl = { timer: setTimeout(() => { this.sleepers.delete(sl); resolve(); }, ms), resolve };
      this.sleepers.add(sl);
    });
  }

  // ---------------- internals ----------------

  /** The open page a catalog / K action refreshes after it (P5), with the current page token; null = none. */
  currentCtx(): Ctx | null {
    const s = this.state.page;
    return s.kind === "open" ? { pageId: s.page.page_id, token: this.pageToken } : null;
  }

  /**
   * Guards a catalog / K / path / review write: nothing while busy (silently); needsSettings / offline, then the P9
   * `invalid` text go to the area's note and nothing is sent; otherwise busy is set.
   */
  beginArea(area: Area, busy: NonNullable<Busy>, invalid: string | null = null): boolean {
    if (this.disposed || this.state.busy) return false;
    const err = this.blocked() ?? invalid;
    if (err) { this.setAreaNote(area, bad(err)); return false; }
    this.set({ busy });
    return true;
  }

  /** A catalog / K / path / review write that threw: its text goes to the area's note; no retry, nothing re-read. */
  areaFailed(area: Area, e: unknown): void {
    if (this.disposed) return;
    this.setAreaNote(area, bad(errorText(e)));
    this.set({ busy: null });
  }

  setAreaNote(area: Area, note: ResultNote | null): void {
    if (area === "catalog") this.set({ catalogNote: note });
    else if (area === "k") this.set({ kNote: note });
    else if (area === "path") this.setPath({ note });
    else this.setReview({ note });
  }

  areaNote(area: Area): ResultNote | null {
    switch (area) {
      case "catalog": return this.state.catalogNote;
      case "k": return this.state.kNote;
      case "path": return this.state.path.note;
      case "review": return this.state.review.note;
    }
  }

  set(patch: Partial<WikiState>): void {
    if (this.disposed) return;
    const wasBusy = this.state.busy !== null;
    this.state = { ...this.state, ...patch };
    if (!wasBusy && this.state.busy !== null) this.writeEpoch++;
    if (this.state.busy === null && this.idleWaiters.length > 0) {
      const waiters = this.idleWaiters;
      this.idleWaiters = [];
      for (const w of waiters) w();
    }
    for (const l of this.listeners) l();
  }

  /** Resolves once no write is in flight (at once when none is). */
  whenIdle(): Promise<void> {
    if (this.state.busy === null || this.disposed) return Promise.resolve();
    return new Promise((resolve) => { this.idleWaiters.push(resolve); });
  }

  call<T>(f: () => Promise<T>): Promise<T> { return this.deps.conn.track(f()); }

  /** The text to show instead of sending a write, or null to go ahead. */
  blocked(): string | null {
    if (!this.deps.configured()) return WIKI_COPY.needsSettings;
    if (this.deps.conn.state === "offline") return WIKI_COPY.offline;
    return null;
  }

  /**
   * Patches the open view of `pageId`. With `seed`, a view still loading that page is first created from the seed
   * (a new page that has no GET yet). Anything else (another page shown) is left alone.
   */
  patchView(pageId: string, f: (v: PageView) => Partial<PageView>, seed?: () => PageOut): void {
    const s = this.state.page;
    let v: PageView;
    if (s.kind === "open" && s.page.page_id === pageId) v = s;
    else if (seed && s.kind === "loading" && s.pageId === pageId) v = freshView(seed());
    else return;
    this.set({ page: { ...v, ...f(v), kind: "open" } });
  }

  /** The open, organized page a page action works on; null while a write is in flight (single-flight). */
  actionView(): PageView | null {
    const s = this.state.page;
    if (this.disposed || this.state.busy || s.kind !== "open" || !s.page.organized) return null;
    return s;
  }

  /**
   * Guards a write (needsSettings / offline, then the P9 `invalid` text) and starts it: the blocking text goes to
   * `spot` and nothing is sent; otherwise busy is set and the page token captured.
   */
  begin(pageId: string, spot: Spot, busy: NonNullable<Busy>, invalid: string | null = null): Ctx | null {
    const err = this.blocked() ?? invalid;
    if (err) { this.put(pageId, spot, bad(err)); return null; }
    this.set({ busy });
    return { pageId, token: this.pageToken };
  }

  /** A write that threw: its text goes to `spot`, nothing is retried, nothing is re-read. */
  failed(ctx: Ctx, spot: Spot, e: unknown): void {
    if (this.disposed) return;
    if (ctx.token === this.pageToken) this.put(ctx.pageId, spot, bad(errorText(e)));
    this.set({ busy: null });
  }

  /** Puts a result line where the action shows it (P4). */
  put(pageId: string, spot: Spot, note: ResultNote): void {
    this.patchView(pageId, (v) => {
      if (spot.kind === "section") return { sectionNotes: { ...v.sectionNotes, [spot.sid]: note } };
      return { status: note };
    });
  }

  /** `heading(sid)`: section id → heading on the shown page; null when it has none (format.ts says 另一页上的一段). */
  headings(pageId: string): Heading {
    const s = this.state.page;
    const sections = s.kind === "open" && s.page.page_id === pageId ? s.page.sections : [];
    return (sid) => sections.find((x) => x.id === sid)?.heading ?? null;
  }

  /** A result line's lead heading: unknown → 另一页上的一段 (never the section id; web PageView does the same). */
  headingText(pageId: string): (sid: string) => string {
    const hd = this.headings(pageId);
    return (sid) => hd(sid) ?? WIKI_COPY.otherPageSection;
  }

  /** Replaces one section of the shown page with the server's copy. */
  writeBack(pageId: string, sec: SectionOut): void {
    this.patchView(pageId, (v) => ({ page: { ...v.page, sections: v.page.sections.map((x) => (x.id === sec.id ? sec : x)) } }));
  }

  /**
   * Adds 「改了」 markers to the page a write ran on, only while that page is still the one shown (same page token).
   * Display only: never part of the A3 sync decision.
   */
  touch(ctx: Ctx, marks: Mark[]): void {
    if (marks.length === 0 || ctx.token !== this.pageToken) return;
    this.patchView(ctx.pageId, (v) => ({ touched: withMarks(v.touched, marks) }));
  }

  /**
   * The failure handler for refresh GETs after a successful write: the write's result line keeps its text and gets the
   * refresh line under it (refreshOffline / refreshFailed); with no result line, the refresh line is shown alone. Never
   * the plain offline / failed text, which would say a sent write was not sent and invite a duplicate click.
   */
  refreshFail(target: Target): (e: unknown) => void {
    return (e) => {
      if (this.disposed) return;
      const sub = refreshText(e);
      const add = (n: ResultNote | null | undefined): ResultNote => (n ? { ...n, sub } : bad(sub));
      if (target.kind === "area") {
        const a = target.area;
        this.setAreaNote(a, add(this.areaNote(a)));
        return;
      }
      const spot = target.spot;
      this.patchView(target.pageId, (v) => {
        if (spot.kind === "section") return { sectionNotes: { ...v.sectionNotes, [spot.sid]: add(v.sectionNotes[spot.sid]) } };
        return { status: add(v.status) };
      });
    };
  }

  /** A quiet refresh retry answered: the refresh line refreshFail added at `target` goes (the result line stays). */
  clearRefreshSub(target: Target): void {
    if (this.disposed) return;
    const drop = (n: ResultNote | null | undefined): ResultNote | null => {
      if (!n || n.sub === undefined) return n ?? null;
      if (n.text === n.sub) return null;               // the refresh line was shown alone
      const rest = { ...n };
      delete rest.sub;
      return rest;
    };
    if (target.kind === "area") {
      const n = this.areaNote(target.area);
      if (n?.sub !== undefined) this.setAreaNote(target.area, drop(n));
      return;
    }
    const spot = target.spot;
    this.patchView(target.pageId, (v) => {
      if (spot.kind === "section") {
        const { [spot.sid]: n, ...others } = v.sectionNotes;
        const kept = drop(n);
        return { sectionNotes: kept === null ? others : { ...others, [spot.sid]: kept } };
      }
      return { status: drop(v.status) };
    });
  }

  setPath(patch: Partial<PathSlot>): void {
    this.set({ path: { ...this.state.path, ...patch } });
  }

  setReview(patch: Partial<ReviewSlot>): void {
    this.set({ review: { ...this.state.review, ...patch } });
  }

  // ---------------- 粗心选错了 / 我学会了 / 看不懂 ----------------

  followupView(key: string): FollowupView { return this.state.followups[key] ?? { kind: "idle", error: null }; }

  /** Why the three buttons cannot be used now (the demo records nothing: same reason on a check, a card and a block). */
  followupBlockedWhy(): string | null {
    if (this.deps.demo) return WIKI_COPY.followupDemo;
    if (!this.deps.configured()) return WIKI_COPY.needsSettings;
    return this.deps.conn.state === "offline" ? WIKI_COPY.followupOffline : null;
  }

  setFollowup(key: string, v: FollowupView): void {
    this.set({ followups: { ...this.state.followups, [key]: v } });
  }

  /** A new answer for a check / card is a new question to follow up: the old row (its note, buttons gone) is forgotten. */
  clearFollowup(key: string): void {
    this.answerRefs.delete(key);
    if (this.state.followups[key] === undefined) return;
    const rest = { ...this.state.followups };
    delete rest[key];
    this.state = { ...this.state, followups: rest };
  }
}
