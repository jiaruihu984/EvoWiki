/**
 * The writes watcher (assistant-actions Task 13): ONE background poll of GET /wiki/writes (no ids), independent of which
 * panel or tab is open. It makes the wiki files follow the Bot's (and the assistant's) page writes:
 *
 * - a page enters planning -> its file appears (frontmatter `evowiki-status: writing`) and the file tree shows 「正在写」;
 * - while it is planning / writing the watcher follows GET /wiki/pages/{id}/run/stream (a replay + follow that starts,
 *   consumes and opens nothing) and writes each section into the file as it lands, like the foreground stream;
 * - it is over (the stream said done / error, or the write left the live list, whichever comes first, once) -> one
 *   normal, non-partial sync of the page (`host.ended`, a read with no side effects); a read that failed is tried again
 *   on the next tick. Ends never block a tick. The watcher never writes a file the user's own stream writes.
 *
 * The poll runs every POLL_MS while something is live and every IDLE_MS otherwise (`kick()` brings the next one forward
 * after an action that may have started a write). File synchronization continues while the app is hidden and stops with `stop()`.
 * The same snapshot feeds 参数's rows (`snapshot()`), so nothing polls twice. Pages the user's own foreground stream is
 * writing are left to that stream.
 */
import type { WriteOut } from "./schema";
import type { WikiApi } from "./api";
import type { FileOut, OrganizeEvent, PageOut, SectionOut } from "./schema";

export const POLL_MS = 1100;
export const IDLE_MS = 10_000;
/** A watcher read this young is used as it is by the panels that show write status (the watcher polls every POLL_MS). */
export const SHARED_FRESH_MS = 2500;

/** A read of GET /wiki/writes (no ids: the live writes) and when it was made. */
export interface WritesSnapshot { at: number; writes: readonly WriteOut[] }

/**
 * The writes of `ids` from the watcher's last read, when it is fresh and every page asked for is in it; otherwise null and
 * the caller asks GET /wiki/writes?ids itself (a page that left the live list gets its final state from the server).
 */
export function sharedWritesFor(snap: WritesSnapshot | null, ids: readonly string[], now: number = Date.now()): WriteOut[] | null {
  if (snap === null || now - snap.at >= SHARED_FRESH_MS || !ids.every((id) => snap.writes.some((x) => x.page_id === id))) return null;
  return snap.writes.filter((x) => ids.includes(x.page_id));
}

const LIVE = new Set(["queued", "planning", "writing"]);
/** The kinds whose run stream is the organize events (a section rewrite has its own events and no file to create). */
const ORGANIZE_KINDS = new Set(["organize", "prefetch"]);

export interface WatcherHost {
  api: Pick<WikiApi, "writes" | "files" | "runStream">;
  /** Conn-tracked, like WikiController.call. */
  call<T>(f: () => Promise<T>): Promise<T>;
  /** Configured, online-able and not disposed: false = nothing is asked. */
  active(): boolean;
  /** The user's own organize stream is writing this page right now: the watcher leaves it alone. */
  foreground(pageId: string): boolean;
  /**
   * How that stream ended (true = done: the file holds the landed page; false = stopped or failed while the server's run may
   * go on). The watcher takes a page back when it is false.
   */
  foregroundDone?(pageId: string): boolean;
  /** The page as streamed so far: the note gets it (partial sync, status writing, tree mark). */
  streamed(pageId: string, page: PageOut, valid: () => boolean): void;
  /** The write is over: sync the landed page (non-partial). False = the read failed (tried again next tick). */
  ended(pageId: string, valid: () => boolean): Promise<boolean>;
  /** The first live set after start: files left `writing` by an earlier session whose page is not live are cleared. */
  started?(live: ReadonlySet<string>, valid: (pageId: string) => boolean): void | Promise<boolean>;
  /** The user's own stream took over this page: its queued background partial syncs are dropped. */
  drop?(pageId: string): void;
  /** The watcher has used how the foreground stream on this page ended (or the page left the live list): forget it. */
  forgetForeground?(pageId: string): void;
}

interface Follow { ac: AbortController; page: PageOut }

export function skeleton(f: FileOut): PageOut {
  return {
    page_id: f.page_id, next_step: null, title: f.title, folder: f.folder, parent_name: f.parent_name, trashed: f.trashed, goal: "",
    organized: true, source: null, profile: "", assumed: [], sections: [], diff: null, unresolved_conflicts: [],
    conflicts: [], budget_exceeded: false, collapsed: false, can_shelve: false, written_for: "", goal_notes: [], shelf_line: "", done: false, llm_calls: 0, writer: null,
    feedback: null, hand_edit: null, rewriting: false, sources: [],
  };
}

export class WritesWatcher {
  private timer: ReturnType<typeof setTimeout> | null = null;
  private stopped = true;
  private ticking = false;
  private again = false;
  private snap: { at: number; writes: WriteOut[] } | null = null;
  private live = new Map<string, WriteOut>();
  private readonly follows = new Map<string, Follow>();
  /** Writes whose end sync is due, and those being synced right now (a tick does not wait for them). */
  private readonly pendingEnd = new Set<string>();
  private readonly ending = new Set<string>();
  /** Pages the watcher followed in their current write (only these get an end sync). */
  private readonly followed = new Set<string>();
  /** Pages whose end was dispatched or whose stream said done / error: not followed or ended again while still live. */
  private readonly finished = new Set<string>();
  /** Pages whose file got its first (skeleton) write in this write: a re-follow does not write it again. */
  private readonly created = new Set<string>();
  /** Live pages the user's own stream wrote (or writes): left alone until their write leaves the live list. */
  private readonly skipped = new Set<string>();
  private readonly identities = new Map<string, { runId: string }>();
  private generation = 0;
  private first = true;
  private recovering = false;
  private readonly subs = new Set<() => void>();

  constructor(private readonly host: WatcherHost, private readonly now: () => number = Date.now) {}

  start(): void {
    if (!this.stopped) return;
    this.stopped = false;
    this.schedule(0);
  }

  stop(): void {
    this.stopped = true;
    this.generation++;
    this.identities.clear();
    if (this.timer !== null) { clearTimeout(this.timer); this.timer = null; }
    for (const f of this.follows.values()) f.ac.abort();
    this.follows.clear();
  }

  /** An action that may have started a write (approve, confirm, 看不懂, a path edit...): look now. */
  kick(): void {
    if (this.stopped) return;
    if (this.ticking) { this.again = true; return; }
    this.schedule(0);
  }

  /** The last read of GET /wiki/writes (the live writes) and when it was read; null before the first. */
  snapshot(): WritesSnapshot | null { return this.snap; }

  /** Called after every read of GET /wiki/writes (the 知识图谱 rows paint their status from the same snapshot). */
  subscribe(fn: () => void): () => void {
    this.subs.add(fn);
    return () => { this.subs.delete(fn); };
  }

  private schedule(ms: number): void {
    if (this.stopped) return;
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = setTimeout(() => { this.timer = null; void this.tick(); }, ms);
  }

  private async tick(): Promise<void> {
    if (this.stopped || this.ticking) return;
    if (!this.host.active()) { this.schedule(POLL_MS); return; }
    this.ticking = true;
    const generation = this.generation;
    let next = IDLE_MS;
    try {
      let out: WriteOut[];
      try { out = await this.host.call(() => this.host.api.writes()); } catch (e) {
        console.warn("EvoWiki: could not read the page writes", e);
        return;
      }
      if (this.stopped || generation !== this.generation) return;
      const cur = new Map(out.filter((w) => LIVE.has(w.state)).map((w) => [w.page_id, w] as const));
      this.snap = { at: this.now(), writes: out };
      for (const fn of [...this.subs]) {
        try { fn(); } catch (e) { console.warn("EvoWiki: a writes subscriber failed", e); }
      }
      for (const [id, w] of cur) {
        if (w.run_id === null) throw new Error("Live page write has no run identity");
        if (!this.identities.has(id) || this.identities.get(id)!.runId !== w.run_id) {
          this.follows.get(id)?.ac.abort();
          this.follows.delete(id);
          this.pendingEnd.delete(id); this.ending.delete(id);
          this.followed.delete(id); this.finished.delete(id);
          this.created.delete(id); this.skipped.delete(id);
          this.host.drop?.(id);
          if (this.identities.has(id)) this.host.forgetForeground?.(id);
          this.identities.set(id, { runId: w.run_id });
        }
      }
      for (const id of this.live.keys()) if (!cur.has(id)) this.left(id);
      this.live = cur;
      // The server can finish before a disconnected foreground stream releases ownership. Keep its end pending
      // until the stream's outcome is known instead of forgetting the page when it first leaves the live list.
      for (const id of this.skipped) if (!cur.has(id) && !this.host.foreground(id)) this.left(id);
      if (this.first && !this.recovering) {
        this.recovering = true;
        const valid = (id: string): boolean => !this.stopped && this.generation === generation && !this.live.has(id) && !this.host.foreground(id);
        void Promise.resolve().then(async () => this.host.started?.(new Set(cur.keys()), valid))
          .then((ok) => { if (this.generation !== generation) return; if (ok !== false) this.first = false; else this.schedule(POLL_MS); },
            (e: unknown) => { console.warn("EvoWiki: could not recover unfinished file syncs", e); this.schedule(POLL_MS); })
          .finally(() => { this.recovering = false; });
      }
      for (const [id, w] of cur) {
        // 停止 or a failed foreground stream while the server's run goes on: the watcher takes the page back
        if (this.skipped.has(id) && !this.host.foreground(id) && this.host.foregroundDone?.(id) === false) {
          this.skipped.delete(id);
          this.host.forgetForeground?.(id);
        }
        if (this.host.foreground(id)) this.takenOver(id);
        else if ((w.state === "planning" || w.state === "writing") && ORGANIZE_KINDS.has(w.kind ?? "organize") && !this.follows.has(id)
          && !this.finished.has(id) && !this.skipped.has(id)) void this.follow(id);
      }
      this.runEnds();
      if (cur.size > 0 || this.skipped.size > 0 || this.pendingEnd.size > 0 || this.ending.size > 0) next = POLL_MS;
    } finally {
      this.ticking = false;
      if (this.again) { this.again = false; next = 0; }
      this.schedule(next);
    }
  }

  /** The write left the live list: a followed one gets its end sync once (unless the stream's done already got it). */
  private left(id: string): void {
    const f = this.follows.get(id);
    if (f !== undefined) { f.ac.abort(); this.follows.delete(id); }
    // a foreground stream that did not finish the page leaves its file to the watcher's end sync
    const abandoned = this.skipped.has(id) && !this.host.foreground(id) && this.host.foregroundDone?.(id) === false;
    if ((this.followed.has(id) && !this.finished.has(id) && !this.skipped.has(id)) || abandoned) this.pendingEnd.add(id);
    this.followed.delete(id);
    this.created.delete(id);
    if (!this.host.foreground(id)) { this.skipped.delete(id); this.host.forgetForeground?.(id); }
    // `finished` goes with the page once nothing about its end is outstanding
    if (!this.pendingEnd.has(id) && !this.ending.has(id)) this.finished.delete(id);
  }

  /** The user's own stream writes this page: the follow stops, its queued syncs go, and nothing of the watcher's touches the file. */
  private takenOver(id: string): void {
    this.skipped.add(id);
    const f = this.follows.get(id);
    if (f !== undefined) { f.ac.abort(); this.follows.delete(id); }
    this.pendingEnd.delete(id);
    this.followed.delete(id);
    this.host.drop?.(id);
  }

  /** Starts the end sync of every page due (not waited for: a busy foreground write must not stall the poll). */
  private runEnds(): void {
    for (const id of [...this.pendingEnd]) {
      if (this.ending.has(id)) continue;
      this.ending.add(id);
      const identity = this.identities.get(id);
      const valid = (): boolean => !this.stopped && this.identities.get(id) === identity && !this.host.foreground(id);
      void this.host.ended(id, valid).then((ok) => ok, (e: unknown) => { console.warn("EvoWiki: could not finish a page's file", e); return false; })
        .then((ok) => {
          if (this.identities.get(id) !== identity) return;
          this.ending.delete(id);
          if (!valid()) return;
          if (!ok) return;                                   // a failed read stays pending: the next tick tries again
          this.pendingEnd.delete(id);
          if (this.live.has(id)) this.finished.add(id); else this.finished.delete(id);
        });
    }
  }

  /** Follows the page's run stream and writes the sections into the file as they land. */
  private async follow(pageId: string): Promise<void> {
    // Registered before anything is awaited: a slow read cannot start a second follow.
    const identity = this.identities.get(pageId)!;
    const ac = new AbortController();
    let finished = false;
    const valid = (): boolean => !this.stopped && !ac.signal.aborted && !finished && this.identities.get(pageId) === identity && !this.finished.has(pageId) && !this.host.foreground(pageId);
    const f: Follow = { ac, page: skeleton({ page_id: pageId, title: "", folder: "", parent_name: "", trashed: false, organized: false }) };
    this.follows.set(pageId, f);
    this.followed.add(pageId);
    const drop = (): void => { if (this.follows.get(pageId) === f) this.follows.delete(pageId); };
    // An early failure: the next tick tries again. Only this follow's own flag is cleared (a stale, aborted follow must not
    // clear a newer one's), and only while nothing was streamed in this write: a partly streamed file keeps its end sync.
    const quit = (): void => {
      const mine = this.follows.get(pageId) === f;
      drop();
      if (mine && !this.created.has(pageId)) this.followed.delete(pageId);
    };
    let file: FileOut | undefined;
    try { file = (await this.host.call(() => this.host.api.files())).find((x) => x.page_id === pageId); } catch (e) {
      console.warn("EvoWiki: could not read the page's file data", e);
      quit();
      return;
    }
    if (ac.signal.aborted || this.stopped || file === undefined || !this.live.has(pageId) || this.host.foreground(pageId)) {
      quit();
      return;
    }
    f.page = skeleton(file);
    if (!this.created.has(pageId)) { this.created.add(pageId); this.host.streamed(pageId, f.page, valid); }   // planning: the file and its mark appear
    const state = { streaming: new Map<string, { cue: string; body: string; heading: string | null }>(), written: new Set<string>() };
    const on = (e: OrganizeEvent): void => {
      if (ac.signal.aborted || this.identities.get(pageId) !== identity || this.stopped) return;
      if (this.host.foreground(pageId)) { this.takenOver(pageId); return; }
      switch (e.event) {
        case "outline": {
          const d = e.data;
          f.page = { ...f.page, organized: true, sections: d.sections, profile: d.profile, assumed: d.assumed, source: d.source };
          state.streaming.clear();
          state.written.clear();
          this.host.streamed(pageId, shown(f.page, state.written), valid);
          return;
        }
        case "section_added": {
          const rest = f.page.sections.filter((x) => x.id !== e.data.section.id);
          const after = e.data.after;
          const found = after === null ? 0 : rest.findIndex((x) => x.id === after) + 1;
          const i = after !== null && found === 0 ? rest.length : found;
          f.page = { ...f.page, sections: [...rest.slice(0, i), e.data.section, ...rest.slice(i)] };
          return;
        }
        case "section_delta":
          state.streaming.set(e.data.section_id, { cue: e.data.cue, body: e.data.body, heading: e.data.heading ?? null });
          return;
        case "section_done": {
          const sid = e.data.section_id;
          const st = state.streaming.get(sid);
          f.page = { ...f.page, sections: f.page.sections.map((x): SectionOut => (x.id !== sid ? x : {
            ...x, ...(st ? { cue: st.cue, body: st.body, ...(st.heading !== null ? { heading: st.heading } : {}) } : {}),
            ...(e.data.cached ? { changed: "cached" as const } : {}),
          })) };
          state.streaming.delete(sid);
          state.written.add(sid);
          this.host.streamed(pageId, shown(f.page, state.written), valid);
          return;
        }
        case "done":
        case "error":
          finished = true;
          return;
        default:
          return;
      }
    };
    try {
      await this.host.call(() => this.host.api.runStream(pageId, identity.runId, on, { signal: ac.signal }));
    } catch (e) {
      if (ac.signal.aborted) return;
      // 404: the run is over already (or the page has none); the end-of-write sync below decides what the file is.
      if (!(typeof e === "object" && e !== null && (e as { status?: number }).status === 404)) {
        console.warn("EvoWiki: could not follow a page write", e);
        drop();
        return;                                                            // still live: the next tick follows again
      }
      finished = true;
    }
    drop();
    if (ac.signal.aborted || this.identities.get(pageId) !== identity || this.stopped) return;
    if (finished && !this.finished.has(pageId)) { this.finished.add(pageId); this.pendingEnd.add(pageId); this.kick(); }
  }
}

/** The page as the foreground stream shows the note: the sections written so far plus the check sections, in page order. */
function shown(page: PageOut, written: ReadonlySet<string>): PageOut {
  return { ...page, sections: page.sections.filter((x) => x.role === "check" || written.has(x.id)) };
}
