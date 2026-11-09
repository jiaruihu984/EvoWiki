/**
 * Pages landing in the view and their vault notes following them (A3): the serial sync queue, the 「待同步」 pages, the
 * files that follow a page's title / folder / trash (reconcile), the user's own stream vs the writes watcher, the
 * interactives fetched after a sync, and hand edits going up from the note. Below the reads and the streams; uses only the core.
 */
import { assetIdsIn, evouiAssetIdsIn, imageAssetIdsIn } from "../embed/assets";
import { toWikiNoteInput } from "../note-input";
import { skeleton } from "../writes-watcher";
import type { NoteSync, WikiNoteInput } from "../note-port";
import { ApiError } from "../../api/errors";
import { type ChangeOut, type PageOut } from "../schema";
import type { FileOut } from "../schema";
import { WIKI_COPY } from "../copy";
import { EMPTY_END, type NoteSyncState, noteSyncLine, type PageEnd } from "../state";
import type { NoteLocator } from "./deps";
import { placementKey, type SyncJob, type EditedSync, diffMarks, withMarks, freshView, SYNCED } from "./helpers";
import type { WikiCore } from "./core";

export class Notes {
  constructor(private readonly core: WikiCore) {}

  /** A hand edit made or took back a 活动记录 row (the facade re-reads the list). */
  ledgerMoved: () => void = () => undefined;
  /** A3: JSON of the last sync input per page this session. */
  readonly lastSynced = new Map<string, string>();
  /** A3 trigger 4 (「待同步」): pages to sync once when their PageOut next lands. */
  readonly pendingSync = new Set<string>();
  /** Trashed pages whose file was already looked at this session (trashFileOf). */
  private readonly trashedSeen = new Set<string>();
  /** Where each page's file was last put this session (placementKey of its input). */
  private readonly lastPlaced = new Map<string, string>();
  /** Pages whose note got a streamed (partial) write since their last full sync: the next landing syncs them. */
  readonly streamedNotes = new Set<string>();
  /** Serial sync: at most one running; per page only the latest waiting input is kept. */
  private readonly syncQueue = new Map<string, SyncJob>();
  private syncing = false;
  /** pageEditedElsewhere: the latest call per page; an older call's GET is dropped (a newer one is on its way). */
  private readonly editSeq = new Map<string, number>();
  /** How the user's own organize stream on a page ended (true = done, so the file holds the landed page); absent while it runs. */
  readonly foregroundResult = new Map<string, boolean>();
  /** Called with every page a note is made from (WikiController: a cited original not in the vault yet is saved there). */
  keepOriginals: ((page: PageOut) => void) | null = null;
  /** The page the user's own organize stream is writing (the writes watcher leaves it to that stream). */
  streamingPage: string | null = null;
  private reconciling: Promise<boolean> | null = null;
  /** The ids the running pass covers (null: all pages) and the ids asked for meanwhile. */
  private reconcilingOnly: readonly string[] | null = null;
  private readonly reconcileMore: string[] = [];
  private reconcileAgain = false;
  /** writeAll ran (this controller writes every page's note into an empty folder once). */
  private wroteAll = false;

  /**
   * The files follow the pages (spec §5): for the pages that have a file in the wiki folder (`only`: just these), the
   * page is read (GET, no LLM) and the file is moved / renamed / trashed to where PageOut.folder, title and the
   * parent put it - not only for pages synced this session. Runs on load and when a ledger action moved pages.
   * Resolves true when every page was read and every move that was needed happened; false otherwise (offline, a failed
   * read or move: the caller may try again later).
   */
  reconcileFiles(only?: readonly string[]): Promise<boolean> {
    if (this.reconciling !== null) {
      // One pass at a time. A full pass asked for meanwhile runs after the current one, whatever that one covered;
      // a partial one asked for while a full one runs is covered by it.
      if (only === undefined) this.reconcileAgain = true;
      else if (this.reconcilingOnly !== null) this.reconcileMore.push(...only);
      return this.reconciling;
    }
    this.reconcilingOnly = only ?? null;
    const run = (async (): Promise<boolean> => {
      let ok = true;
      let cur: readonly string[] | undefined = only;
      do {
        this.reconcileAgain = false;
        const more = this.reconcileMore.splice(0);
        if (cur !== undefined && more.length > 0) cur = [...cur, ...more];
        ok = (await this.reconcileOnce(cur)) && ok;
        cur = this.reconcileAgain ? undefined : cur;
        this.reconcilingOnly = cur ?? null;
      } while ((this.reconcileAgain || this.reconcileMore.length > 0) && !this.core.disposed);
      return ok;
    })().finally(() => { this.reconciling = null; this.reconcilingOnly = null; });
    this.reconciling = run;
    return run;
  }

  private async reconcileOnce(only?: readonly string[]): Promise<boolean> {
    const sync = this.core.deps.sync;
    if (sync === null || sync.listPages === undefined || this.core.disposed || !this.core.deps.configured()) return true;
    let ok = true;
    let ids: string[];
    try { ids = [...(only ?? await sync.listPages())]; } catch (e) { console.warn("EvoWiki wiki: could not list the wiki files", e); return false; }
    if (ids.length === 0 && (only !== undefined || this.wroteAll)) return true;
    // A note whose page is not in /wiki/files (the server does not know it) is skipped and counts as done: nothing can be asked of it.
    // One side-effect-free read of every page's file data (title, folder, parent, trashed); no page is opened.
    let files: Map<string, FileOut>;
    try { files = new Map((await this.core.call(() => this.core.deps.api.files())).map((f) => [f.page_id, f] as const)); } catch (e) {
      console.warn("EvoWiki wiki: could not read the pages' file data", e);
      return false;
    }
    if (ids.length === 0) return this.writeAll(sync, [...files.values()]);
    for (const id of ids) {
      if (this.core.disposed) return false;
      const file = files.get(id);
      if (file === undefined) continue;                                 // a note whose page the server does not know: left as it is
      try {
        if (!file.trashed) this.trashedSeen.delete(id); else this.trashedSeen.add(id);
        const input = { ...this.noteInput(skeleton(file)), placeOnly: true };
        this.lastPlaced.set(id, placementKey(input));
        if (await sync.run(input) === "move_failed") ok = false;
      } catch (e) {
        ok = false;
        console.warn("EvoWiki wiki: could not put a wiki file where its page belongs", e);
      }
    }
    return ok;
  }

  /**
   * No note of this user in the vault yet (a new vault, or the user's first time here after a switch): every written page
   * gets its note, read with the read-only GET (nothing is opened, called or written on the server). Once per controller
   * (a load, a user switch): notes the user deletes later are not written back.
   */
  private async writeAll(sync: NoteSync, files: readonly FileOut[]): Promise<boolean> {
    this.wroteAll = true;
    let ok = true;
    for (const f of files) {
      if (this.core.disposed) return false;
      if (!f.organized || f.trashed) continue;
      try {
        const page = await this.core.call(() => this.core.deps.api.getPage(f.page_id));
        const input = this.noteInput(page);
        await this.enqueueSync(sync, f.page_id, input, JSON.stringify(input));
      } catch (e) {
        ok = false;
        console.warn("EvoWiki wiki: could not read a page for its note", e);
      }
    }
    return ok;
  }

  /** The watcher's page as streamed so far: the note gets it (partial sync: status writing, the tree mark). */
  streamBackground(sync: NoteSync, pageId: string, page: PageOut, valid?: () => boolean): void {
    if (this.core.disposed || this.streamingPage === pageId) return;      // the user's own stream writes this file
    const input = this.noteInput(page);
    const json = JSON.stringify(input);
    if (this.lastSynced.get(pageId) === json) return;
    void this.enqueueSync(sync, pageId, input, json, true, true, valid);
  }

  /** The user's own stream took over a page: the watcher's queued syncs for it are dropped (theirs resolve unwritten). */
  dropBackgroundSync(pageId: string): void {
    const job = this.syncQueue.get(pageId);
    if (job === undefined || !job.bg) return;
    this.syncQueue.delete(pageId);
    if (this.lastSynced.get(job.pageId) === job.json) this.lastSynced.delete(job.pageId);
    for (const w of job.waiters) w(false);
  }

  /**
   * Load: a note an earlier session left with `evowiki-status: writing` whose page has no live write is brought to the page
   * (its status goes; a page that holds nothing loses the file).
   */
  async clearStaleWriting(live: ReadonlySet<string>, valid: (pageId: string) => boolean): Promise<boolean> {
    const sync = this.core.deps.sync;
    if (sync === null || sync.listWriting === undefined || this.core.disposed) return true;
    let ids: string[];
    try { ids = await sync.listWriting(); } catch (e) { console.warn("EvoWiki wiki: could not list the notes being written", e); return false; }
    let recovered = true;
    for (const id of ids) if (!live.has(id) && !this.core.disposed) {
      if (!valid(id)) continue;
      if (await this.syncEdited(id, false, true, () => valid(id)) === "failed") recovered = false;
    }
    return recovered;
  }

  /** Once per session and page: the trashed page's file (if it has one) goes to Obsidian's trash. */
  trashFileOf(pageId: string): void {
    if (this.trashedSeen.has(pageId) || this.core.deps.sync === null) return;
    this.trashedSeen.add(pageId);
    void this.pageEditedElsewhere(pageId);
  }

  /** The note input of a page: the file name and folder come from the page itself (folder, title) and the graph (parent). */
  noteInput(page: PageOut): WikiNoteInput {
    this.keepOriginals?.(page);
    if (page.trashed !== true) this.trashedSeen.delete(page.page_id);   // seen outside the trash: a later trashing is looked at again
    const input = toWikiNoteInput(page, this.core.names);
    const parent = page.parent_name ?? "";
    return parent === "" ? input : { ...input, parentName: parent };
  }

  /**
   * The reading assistant applied 加入页面 to `pageId` (main.ts passes this as AssistantController's pageEdited). Demo
   * (sync null) or unconfigured: nothing. Otherwise one GET of the page (a read: no retry, failures only console.warn,
   * nothing on screen); the shown page lands like a refresh; then its note is synced — the edit itself is the content
   * change (A3's change condition is skipped), but an input equal to the last one synced this session is not rewritten.
   * The sync line shows the result when the page is shown; otherwise the note is written silently.
   *
   * Busy ruling (race-free): while a write is in flight this waits until it is over, then GETs — so it never lands into a
   * streaming view and never sends while a write runs. If another write starts while the GET is out, the GET may be older
   * than that write: it is dropped and the page goes to 「待同步」 (synced when its PageOut next lands). Of several calls
   * for the same page, only the latest one's GET is used.
   */
  async pageEditedElsewhere(pageId: string, partial = false, discardEmpty = false): Promise<boolean> {
    return (await this.syncEdited(pageId, partial, discardEmpty)) === "done";
  }

  /** pageEditedElsewhere's outcome: done, deferred (a write started while the GET was out: 待同步), or failed (the GET). */
  async syncEdited(pageId: string, partial = false, discardEmpty = false, valid?: () => boolean): Promise<EditedSync> {
    const sync = this.core.deps.sync;
    if (sync === null || this.core.disposed || !this.core.deps.configured()) return "done";
    const seq = (this.editSeq.get(pageId) ?? 0) + 1;
    this.editSeq.set(pageId, seq);
    // Re-checked after waking: a write may begin between busy clearing and this continuation.
    while (this.core.state.busy !== null && !this.core.disposed) await this.core.whenIdle();
    if (this.core.disposed || this.editSeq.get(pageId) !== seq || valid?.() === false) return "done";
    const epoch = this.core.writeEpoch;
    const token = this.core.pageToken;
    let page: PageOut;
    try {
      page = await this.core.call(() => this.core.deps.api.getPage(pageId));
    } catch (e) {
      if (!this.core.disposed) console.warn("EvoWiki wiki: could not re-read a page the reading assistant edited", e);
      return "failed";
    }
    if (this.core.disposed || this.editSeq.get(pageId) !== seq || valid?.() === false) return "done";
    if (this.core.writeEpoch !== epoch) { this.pendingSync.add(pageId); return "deferred"; }
    const s = this.core.state.page;
    if (token === this.core.pageToken && s.kind === "open" && s.page.page_id === pageId) this.land(page, false);
    else this.core.names.addPage(page);
    // A write that ended (failed / undone) on a page that holds nothing: the file made for it goes (its mark with it).
    const gone = !partial && !page.organized && page.trashed !== true && (this.streamedNotes.has(pageId) || discardEmpty);
    if (!page.organized && page.trashed !== true && !partial && !gone) return "done";
    this.pendingSync.delete(pageId);
    const input = gone ? { ...this.noteInput(page), trashed: true } : this.noteInput(page);
    const json = JSON.stringify(input);
    // A note that holds a streamed write is always brought to the landed page (it takes the 「正在写」 mark off).
    if (this.lastSynced.get(pageId) === json && !(this.streamedNotes.has(pageId) && !partial)) return "done";
    let synced = false;
    await this.enqueueSync(sync, pageId, input, json, partial, false, valid, (ok) => { synced = ok; });
    return synced ? "done" : "failed";
  }

  // ---------------- A3 bookkeeping ----------------

  /**
   * Records a write's ChangeOut: its other_pages_stale go to 「待同步」 (A3-4). Returns whether the current page's
   * content or folds changed (A3-2: sync the page from the GET that follows).
   */
  noteChange(c: ChangeOut): boolean {
    for (const id of c.other_pages_stale) this.pendingSync.add(id);
    return [c.rewritten, c.inserted, c.moved, c.folded, c.unfolded].some((l) => l.length > 0);
  }

  /** Lands a GET page in the view: a refresh of the shown page keeps its notes; otherwise a new view. */
  land(page: PageOut, fresh: boolean): void {
    this.core.names.addPage(page);
    const s = this.core.state.page;
    if (!fresh && s.kind === "open" && s.page.page_id === page.page_id) {
      // A refresh keeps the result notes of the sections still on the page.
      const ids = new Set(page.sections.map((x) => x.id));
      const sectionNotes = Object.fromEntries(Object.entries(s.sectionNotes).filter(([sid]) => ids.has(sid)));
      // Same page: this visit's markers stay; the GET's lazy-update diff adds its own.
      this.core.set({ page: { ...s, page: page.next_step === null ? { ...page, next_step: s.page.next_step } : page, streaming: {}, sectionNotes, touched: withMarks(s.touched, diffMarks(page.diff)), streamWriter: null } });
    }
    else this.core.set({ page: { kind: "open", ...freshView(page) } });
  }

  /**
   * A3: sync a landed page when `changed` (triggers 1–3) or it waits in 「待同步」 (4), and its input differs from the
   * last one synced this session. Resolves once that input (or a newer one for the same page) has been written.
   */
  syncLanded(page: PageOut, changed: boolean): Promise<void> {
    const sync = this.core.deps.sync;
    if (sync === null || this.core.disposed) return Promise.resolve();
    if (page.trashed === true) { this.trashFileOf(page.page_id); return Promise.resolve(); }
    if (!page.organized) return Promise.resolve();
    const waiting = this.pendingSync.delete(page.page_id);
    // A note that holds a streamed (partial) page is brought back to the landed page whatever the write did.
    const streamed = this.streamedNotes.has(page.page_id);
    // The page's title or folder changed since the last sync this session: its file follows (rename / move).
    const lastPlace = this.lastPlaced.get(page.page_id);
    const input = this.noteInput(page);
    const moved = lastPlace !== undefined && lastPlace !== placementKey(input);
    if (!changed && !waiting && !streamed && !moved) {
      // Viewing re-renders an existing note (never creates one): the first view this session, so an unedited note
      // written by an older renderer picks up how notes are written now (a current one stays unchanged, an edited one
      // the user's); later views only when a material of the page was deleted since (「名 p.N（已删除）」, final review #15).
      const json = JSON.stringify(input);
      const last = this.lastSynced.get(page.page_id);
      if (last === json || (last !== undefined && !(page.sources ?? []).some((x) => x.deleted))) return Promise.resolve();
      return sync.find(page.page_id).then((path) => (path === null || this.core.disposed ? undefined : this.enqueueSync(sync, page.page_id, input, json)),
        () => undefined);
    }
    const json = JSON.stringify(input);
    // After a streamed write the full sync runs even when the text matches (it fetches the interactives, reports edits).
    if (!streamed && this.lastSynced.get(page.page_id) === json) return Promise.resolve();
    return this.enqueueSync(sync, page.page_id, input, json);
  }

  /**
   * Queues one sync of `input` (serial; per page only the latest waiting input is kept); resolves once written.
   * `partial`: the page as streamed so far (see SyncJob).
   */
  enqueueSync(sync: NoteSync, pageId: string, input: WikiNoteInput, json: string, partial = false, bg = false, valid?: () => boolean, settled?: (ok: boolean) => void): Promise<void> {
    this.lastSynced.set(pageId, json);
    this.lastPlaced.set(pageId, placementKey(input));
    if (partial) this.streamedNotes.add(pageId); else this.streamedNotes.delete(pageId);
    this.core.patchView(pageId, () => ({ noteSync: { kind: "syncing" } }));
    return new Promise<void>((resolve) => {
      const prev = this.syncQueue.get(pageId);
      this.syncQueue.set(pageId, { pageId, input, json, partial, bg, ...(valid ? { valid } : {}), waiters: [...(prev?.waiters ?? []), (ok) => { settled?.(ok); resolve(); }] });
      void this.pumpSync(sync);
    });
  }

  private async pumpSync(sync: NoteSync): Promise<void> {
    if (this.syncing) return;
    this.syncing = true;
    try {
      for (;;) {
        const job = this.syncQueue.values().next().value;
        if (job === undefined) break;
        this.syncQueue.delete(job.pageId);
        let completed = false;
        if (!this.core.disposed && job.valid?.() !== false) {
          let ns: NoteSyncState;
          try {
            // A streamed write leaves the note to the full sync that follows (its text is not the page yet).
            const up = job.partial ? { input: job.input } : await this.takeHandEdit(sync, job.input, false, false, job.valid);
            if (job.valid?.() === false) {
              if (this.lastSynced.get(job.pageId) === job.json) this.lastSynced.delete(job.pageId);
              for (const w of job.waiters) w(false);
              continue;
            }
            ns = { kind: "done", result: await sync.run(up.input, { ...(job.partial ? { partial: true } : up.accepted !== undefined ? { accepted: up.accepted } : {}), ...(job.valid ? { valid: job.valid } : {}) }) };
            if (!job.partial && SYNCED.has(ns.result)) this.fetchAssets(up.input);
          } catch (e) {
            // A5: nothing was written; show it, do not retry. A page not shown has no sync line: log it.
            ns = { kind: "error" };
            const shown = this.core.state.page;
            if (shown.kind !== "open" || shown.page.page_id !== job.pageId) console.warn("EvoWiki wiki: note sync failed", e);
            if (this.lastSynced.get(job.pageId) === job.json) this.lastSynced.delete(job.pageId);
          }
          if (job.valid?.() === false) {
            if (this.lastSynced.get(job.pageId) === job.json) this.lastSynced.delete(job.pageId);
            for (const w of job.waiters) w(false);
            continue;
          }
          completed = ns.kind === "done" && !["backup_failed", "move_failed", "superseded"].includes(ns.result);
          if (!this.syncQueue.has(job.pageId)) this.core.patchView(job.pageId, () => ({ noteSync: ns }));
          // the page has no status area of its own any more (the note is the page): a failed write says so here
          if (ns.kind === "error") this.core.deps.notice?.(WIKI_COPY.noteSyncFailed);
        }
        if (!completed && this.lastSynced.get(job.pageId) === job.json) this.lastSynced.delete(job.pageId);
        for (const w of job.waiters) w(completed);
      }
    } finally {
      this.syncing = false;
    }
  }

  /**
   * Appendix A·6: after a page's note was written or found current, the interactives its section bodies point at
   * (`assets/<id>.html` in embedded-html blocks) are fetched (the asset fetcher writes them). Only online, configured
   * and not in the demo; no LLM, so `busy` is neither checked nor set. Failures are logged; nothing is shown.
   */
  private fetchAssets(input: WikiNoteInput): void {
    const assets = this.core.deps.assets;
    if (assets === undefined || this.core.deps.demo || this.core.disposed || !this.core.deps.configured() || this.core.deps.conn.state === "offline") return;
    const ids = [...new Set(input.sections.flatMap((s) => assetIdsIn(s.body)))];
    const evoui = [...new Set(input.sections.flatMap((s) => evouiAssetIdsIn(s.body)))];
    const images = [...new Set(input.sections.flatMap((s) => imageAssetIdsIn(s.body)))];
    const warn = (e: unknown): void => { console.warn("EvoWiki wiki: could not fetch interactives", e); };
    if (ids.length > 0) void assets.retrieveAll(ids).catch(warn);
    if (evoui.length > 0) void assets.retrieveAll(evoui, "evoui").catch(warn);
    if (images.length > 0) void assets.retrieveAll(images, "image").catch(warn);
  }

  /**
   * Hand edit, trigger 2: main.ts calls this about 2 s after the user left a wiki note (the active file moved away from it
   * and did not come back). An edited note goes through a sync of the page as it is now (takeHandEdit).
   */
  async noteLeft(path: string, pageId: string): Promise<void> {
    this.patchEnd(pageId, { handLine: false });       // he has left the page: 撤销 is in 活动记录 from here on
    const sync = this.core.deps.sync;
    if (sync === null || !this.handEditsOn()) return;
    if (!(await sync.edited(path, pageId).catch(() => false))) return;
    this.lastSynced.delete(pageId);
    await this.pageEditedElsewhere(pageId);
  }

  /** Whether a hand edit could go up now: not the demo, configured, online. */
  private handEditsOn(): boolean {
    const d = this.core.deps;
    return !d.demo && d.configured() && d.conn.state !== "offline";
  }

  /**
   * Hand edit, trigger 1: before a sync writes the note, what the user changed in its block becomes the page's text
   * (POST hand-edit; note/hand-edit.ts reads it), and the sync then writes the note from the page the server answered:
   * the edited sections read exactly as typed, the others are brought up to date. Not while he is in the note. An edit
   * that cannot go safely stays in the note as typed, and the page's top line says so (fail closed). Offline, or
   * when the request fails for another reason, nothing changes and the next trigger tries again.
   */
  private async takeHandEdit(sync: NoteSync, input: WikiNoteInput, evenOpen = false, overServer = false, valid?: () => boolean): Promise<{ input: WikiNoteInput; accepted?: string }> {
    if (input.trashed === true || input.placeOnly === true || !this.handEditsOn()) return { input };
    const pageId = input.pageId;
    const read = await sync.handEdit(input).catch(() => null);
    if (valid?.() === false || read === null || (!evenOpen && sync.isOpen(read.path))) return { input };
    const { plan, text } = read;
    if (plan.kind === "none") {       // no edit, or only blank lines moved: the note is written as usual
      this.handFailed(pageId, null);
      return { input, accepted: text };
    }
    if (plan.kind === "unsafe") {
      this.handFailed(pageId, { text: WIKI_COPY.handEditUnsafe(plan.label, plan.why), note: text });
      return { input };
    }
    let page: PageOut;
    try {
      let edit = plan.edit;
      if (overServer) {
        const latest = await this.core.call(() => this.core.deps.api.getPage(pageId));
        const versions = new Map(latest.sections.map((s) => [s.id, s.fingerprint]));
        const fingerprint = (id: string): string => {
          const version = versions.get(id);
          if (!version) throw new ApiError(409, WIKI_COPY.handEditVersionUnknown, "hand_edit_stale");
          return version;
        };
        edit = { ...edit,
          sections: (edit.sections ?? []).map((s) => ({ ...s, fingerprint: fingerprint(s.section_id) })),
          removed: (edit.removed ?? []).map((s) => ({ ...s, fingerprint: fingerprint(s.section_id) })),
        };
      }
      page = (await this.core.deps.api.handEdit(pageId, edit)).page;
    } catch (e) {
      if (e instanceof ApiError && e.status >= 400 && e.status < 500) this.handFailed(pageId, { text: WIKI_COPY.handEditRefused(e.detail), note: text, conflict: e.status === 409 && e.code === "hand_edit_stale" });
      else console.warn("EvoWiki wiki: could not send a hand edit", e);
      return { input };
    }
    if (this.core.disposed || valid?.() === false) return { input };
    this.handFailed(pageId, null);
    this.patchEnd(pageId, { handLine: true });
    this.ledgerMoved();
    const s = this.core.state.page;
    if (s.kind === "open" && s.page.page_id === pageId) this.land(page, false);
    else this.core.names.addPage(page);
    const fresh = this.noteInput(page);
    this.lastSynced.set(pageId, JSON.stringify(fresh));
    return { input: fresh, accepted: text };
  }

  /**
   * Hand edit, trigger 3: before the chat's card changes this page, what he typed in the note and the server has not seen
   * goes up now, even while he is in the note, so the page version the card checks is the one he sees. False: it could not
   * go up, the card must not touch the page.
   */
  async handEditFirst(pageId: string): Promise<boolean> {
    const sync = this.core.deps.sync;
    if (sync === null || !this.handEditsOn()) return true;
    let input: WikiNoteInput;
    try { input = this.noteInput(await this.core.call(() => this.core.deps.api.getPage(pageId))); } catch { return false; }
    const read = await sync.handEdit(input).catch(() => null);
    if (read === null || read.plan.kind === "none") return true;
    const up = await this.takeHandEdit(sync, input, true);
    if (up.accepted === undefined) return false;
    await sync.run(up.input, { accepted: up.accepted });
    return true;
  }

  /** The top line's state for a hand edit that could not go up; a new failure also gets a corner notice (he may have left the note). */
  private handFailed(pageId: string, failed: { text: string; note: string; conflict?: boolean } | null): void {
    const cur = this.core.state.pageEnd[pageId] ?? EMPTY_END;
    if (cur.handFailed?.text === failed?.text && cur.handFailed?.note === failed?.note) return;
    if (failed !== null && cur.handFailed?.text !== failed.text) this.core.deps.notice?.(failed.text);
    this.patchEnd(pageId, { handFailed: failed });
  }

  private patchEnd(pageId: string, patch: Partial<PageEnd>): void {
    const cur = this.core.state.pageEnd[pageId] ?? EMPTY_END;
    if (Object.entries(patch).every(([k, v]) => cur[k as keyof PageEnd] === v)) return;
    this.core.set({ pageEnd: { ...this.core.state.pageEnd, [pageId]: { ...cur, ...patch } } });
  }

  /** 保留我的版本: the explicit choice uses current section versions; a later server change still conflicts. */
  async keepMine(page: PageOut): Promise<void> {
    const sync = this.core.deps.sync;
    if (sync === null) return;
    const up = await this.takeHandEdit(sync, this.noteInput(page), true, true);
    if (up.accepted === undefined) return;
    const result = await sync.run(up.input, { accepted: up.accepted });
    this.core.patchView(page.page_id, () => ({ noteSync: { kind: "done", result } }));
  }

  /** 恢复 EvoWiki 的版本: the note is written from the page over the text `note`, if it is still that text. */
  async restoreNote(page: PageOut, note: string): Promise<void> {
    const sync = this.core.deps.sync;
    if (sync === null) return;
    const input = this.noteInput(page);
    this.lastSynced.set(page.page_id, JSON.stringify(input));
    const result = await sync.run(input, { accepted: note });
    this.core.patchView(page.page_id, () => ({ noteSync: { kind: "done", result } }));
  }

  /**
   * Read-only, local: the note claiming pageId → open it; none (or not opened) → noteSync `missing` (the caller
   * writes the note: PageActions.writeNote). A sync line that already says something (failed /
   * user_edited / backup_failed / syncing) is left alone. Never syncs by itself.
   */
  async findAndOpen(pageId: string, token: number, at?: NoteLocator, frame?: boolean): Promise<boolean> {
    const sync = this.core.deps.sync;
    if (sync === null) return false;
    let path: string | null;
    try { path = await sync.find(pageId); } catch { path = null; }
    if (this.core.disposed || token !== this.core.pageToken) return false;
    if (path !== null && this.core.deps.openNote(path, at, frame)) return true;
    this.core.patchView(pageId, (v) => {
      const ns = v.noteSync;
      const quiet = ns.kind === "idle" || ns.kind === "missing" || (ns.kind === "done" && noteSyncLine(ns) === null);
      return quiet && v.page.organized ? { noteSync: { kind: "missing" } } : {};
    });
    return false;
  }
}
