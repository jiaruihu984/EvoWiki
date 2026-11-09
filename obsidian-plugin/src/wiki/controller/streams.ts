/**
 * The organize stream and the new-page chain (a chat goal's page / path next / 开始生成 → organize → GETs, sync, open the note),
 * and the streamed-section helpers the section rewrite stream uses too.
 */
import { WIKI_COPY } from "../copy";
import { opLine } from "../format";
import type { OrganizeEvent, SectionDeltaEvent, SectionOut } from "../schema";
import {
  type StreamEnd, type StreamOpen, type Seed, RETRY_ORGANIZE, isAbort, ok, bad, thrownNote, eventNote, emptyPage, without,
} from "./helpers";
import type { WikiCore } from "./core";
import type { Notes } from "./notes";
import type { Reads } from "./reads";

export class Streams {
  constructor(private readonly core: WikiCore, private readonly notes: Notes, private readonly reads: Reads) {}

  /**
   * section_done: the streamed cue / body (and heading, when the writer sent one) land in the section (a cached one gets
   * no marker). The same id may come again later (the writer revised it): the last one wins.
   */
  landStreamed(pageId: string, sid: string, cached: boolean): void {
    this.core.patchView(pageId, (v) => {
      const st = v.streaming[sid];
      const sections = v.page.sections.map((x) => (x.id !== sid ? x : {
        // the fingerprint named the text before this one: a note written from it would refuse his hand edit as a conflict
        ...x, ...(st ? { cue: st.cue, body: st.body, fingerprint: "", ...(st.heading !== null ? { heading: st.heading } : {}) } : {}),
        ...(cached ? { changed: "cached" as const } : {}),
      }));
      return { page: { ...v.page, sections }, streaming: without(v.streaming, sid) };
    });
  }

  /** section_delta: the section's live text (replaces any earlier delta of the same id: the last one wins). */
  streamDelta(pageId: string, d: SectionDeltaEvent): void {
    const { section_id: sid, cue, body, heading } = d;
    this.core.patchView(pageId, (v) => ({ streaming: { ...v.streaming, [sid]: { cue, body, heading: heading ?? null } } }));
  }

  /** section_added: a section the writer inserted goes in after `after` (null = page top; unknown = the end); a known id is replaced. */
  addStreamed(pageId: string, sec: SectionOut, after: string | null): void {
    this.core.patchView(pageId, (v) => {
      const rest = v.page.sections.filter((x) => x.id !== sec.id);
      const found = after === null ? 0 : rest.findIndex((x) => x.id === after) + 1;
      const i = after !== null && found === 0 ? rest.length : found;
      return { page: { ...v.page, sections: [...rest.slice(0, i), sec, ...rest.slice(i)] } };
    });
  }

  /**
   * While a stream runs: the note gets the page as written so far, through the usual sync queue (per page only the
   * latest waiting input is kept). `only` (organize): the sections written in this stream plus the check sections (they
   * come whole with the outline), in page order; null (a section rewrite): every section. With `open.pending` (a new
   * page) the first such write opens the note. The GET after `done` syncs the whole page as before.
   */
  syncStreamed(pageId: string, token: number, only: ReadonlySet<string> | null, open: StreamOpen | null): void {
    const sync = this.core.deps.sync;
    const s = this.core.state.page;
    if (sync === null || this.core.disposed || token !== this.core.pageToken || s.kind !== "open" || s.page.page_id !== pageId
      || !s.page.organized) return;
    const page = only === null ? s.page : { ...s.page, sections: s.page.sections.filter((x) => x.role === "check" || only.has(x.id)) };
    const input = this.notes.noteInput(page);
    const json = JSON.stringify(input);
    if (this.notes.lastSynced.get(pageId) === json) return;
    const written = this.notes.enqueueSync(sync, pageId, input, json, true);
    if (open === null || !open.pending) return;
    open.pending = false;
    void written.then(async () => { if (await this.notes.findAndOpen(pageId, token)) open.opened = true; });
  }

  /**
   * New page chain (a chat goal's page, POST path next, 开始生成): organize stream straight away, then GETs, sync, open
   * note.
   */
  async runCreate(pageId: string, draftTitle: string, goal: string, openNote = true): Promise<void> {
    const token = ++this.core.pageToken;
    const seed: Seed = (organized) => emptyPage(pageId, draftTitle || this.core.names.page(pageId) || WIKI_COPY.unnamedPage, goal, organized);
    this.core.set({ page: { kind: "loading", pageId }, busy: { what: "organize", abortable: true } });
    const open: StreamOpen = { pending: openNote, opened: false };
    const end = await this.stream(pageId, token, seed, open);
    // openNote false (学点新的 stays in front, its page card fills): the note is not opened
    await this.afterStream(pageId, token, end, seed, openNote && !open.opened);
  }

  /** Runs POST organize; event effects land only while `token` is current. */
  async stream(pageId: string, token: number, seed: Seed, open: StreamOpen | null = null, fresh = false): Promise<StreamEnd> {
    const ac = new AbortController();
    this.core.abort = ac;
    this.core.abortPage = pageId;
    // (also tells the subscribers that this page's write can now be stopped)
    const stopped = this.core.state.stopped;
    this.core.set({ stopped: stopped?.pageId === pageId ? null : stopped });
    this.notes.streamingPage = pageId;
    this.notes.foregroundResult.delete(pageId);
    this.notes.dropBackgroundSync(pageId);
    let end = null as StreamEnd | null;
    const live = (): boolean => !this.core.disposed && token === this.core.pageToken;
    /** Sections written in this stream (the note shows these while it runs). */
    const written = new Set<string>();
    let of = 0;
    const on = (e: OrganizeEvent): void => {
      if (end !== null) return;
      switch (e.event) {
        case "outline": {
          if (!live()) return;
          const { sections, profile, assumed, source } = e.data;
          of = sections.length;
          this.core.emitWriteProgress({ pageId, done: written.size, of, ended: false, ok: false });
          this.core.patchView(pageId, (v) => ({
            page: { ...v.page, organized: true, sections, profile, assumed, source },
            streaming: {}, status: ok(WIKI_COPY.writing), streamWriter: null,
          }), () => seed(true));
          const s = this.core.state.page;
          // The view's title may still be the draft-title seed: it only fills a gap in the name book.
          if (s.kind === "open") this.core.names.addPage(s.page, false);
          return;
        }
        case "writer": {
          if (!live()) return;
          const { fallback, reason } = e.data;
          this.core.patchView(pageId, () => ({ streamWriter: { fallback, reason } }));
          return;
        }
        case "section_added": {
          if (!live()) return;
          this.addStreamed(pageId, e.data.section, e.data.after);
          return;
        }
        case "section_delta": {
          if (!live()) return;
          this.streamDelta(pageId, e.data);
          return;
        }
        case "section_done": {
          if (!live()) return;
          this.landStreamed(pageId, e.data.section_id, e.data.cached);
          written.add(e.data.section_id);
          this.core.emitWriteProgress({ pageId, done: written.size, of: Math.max(of, written.size), ended: false, ok: false });
          this.syncStreamed(pageId, token, written, open);
          return;
        }
        case "done": {
          end = { kind: "done" };
          if (live()) {
            const line = opLine({ message: WIKI_COPY.organizeDone, llmCalls: e.data.llm_calls, changed: 0 }, false);
            this.core.patchView(pageId, () => ({ status: ok(line), streaming: {} }));
          }
          return;
        }
        case "error": {
          const d = e.data;
          end = { kind: "failed", note: eventNote(d, RETRY_ORGANIZE) };
          return;
        }
      }
    };
    try {
      await this.core.call(() => this.core.deps.api.organize(pageId, on, { signal: ac.signal, ...(fresh ? { fresh } : {}) }));
    } catch (e) {
      if (end === null) end = isAbort(e) || ac.signal.aborted ? { kind: "aborted" } : { kind: "failed", note: thrownNote(e) };
    } finally {
      if (this.core.abort === ac) this.core.abort = null;
      if (this.notes.streamingPage === pageId) this.notes.streamingPage = null;
    }
    // A transport that resolves normally after 「停止」 is a stop too, not a failure.
    if (end === null && ac.signal.aborted) end = { kind: "aborted" };
    this.core.emitWriteProgress({ pageId, done: written.size, of: Math.max(of, written.size), ended: true, ok: end?.kind === "done" });
    // A 2xx stream that ended without done / error is a failure too.
    this.notes.foregroundResult.set(pageId, end?.kind === "done");
    return end ?? { kind: "failed", note: { ...bad(WIKI_COPY.failed), retry: RETRY_ORGANIZE } };
  }

  /** P5 after an organize stream: done → GET page (same page) + catalog (+ K), sync, maybe open the note. */
  async afterStream(pageId: string, token: number, end: StreamEnd, seed: Seed, openNote: boolean): Promise<void> {
    if (this.core.disposed) return;
    const live = token === this.core.pageToken;
    // The stream is over: 「停止」 can no longer do anything during the follow-up GETs.
    const busy = this.core.state.busy;
    if (busy?.abortable) this.core.set({ busy: { ...busy, abortable: false } });
    if (end.kind === "failed") {
      if (live) this.core.patchView(pageId, () => ({ status: end.note, streaming: {} }), () => seed(false));
      this.core.set({ busy: null });
      // The streamed file is brought to what the server holds (its 「正在写」 goes; a page with nothing loses the file).
      if (this.notes.streamedNotes.has(pageId)) void this.notes.pageEditedElsewhere(pageId);
      return;
    }
    if (end.kind === "aborted") {
      if (live) {
        this.core.patchView(pageId, () => ({ status: null, streaming: {} }));
        const page = await this.reads.getAndLand(pageId, token, false);
        this.core.set({ busy: null });
        // 停止 on a page that holds nothing yet (its first write): syncLanded skips an unorganized page, so the streamed
        // file (its partial sections, 「生成中」) goes as after a failed write.
        if (page !== null && page.organized) await this.notes.syncLanded(page, page.diff !== null || page.llm_calls > 0);
        else if (this.notes.streamedNotes.has(pageId)) void this.notes.pageEditedElsewhere(pageId);
      } else {
        this.core.set({ busy: null });
        if (this.notes.streamedNotes.has(pageId)) void this.notes.pageEditedElsewhere(pageId);
      }
      return;
    }
    // done
    if (!live) this.notes.pendingSync.add(pageId); // the user switched away: sync when the page next lands
    const fail = this.core.refreshFail({ kind: "page", pageId, spot: { kind: "status" } });
    const page = live ? await this.reads.getAndLand(pageId, token, false, fail) : null;
    await this.reads.loadCatalog(fail);
    await this.reads.loadK(this.core.state.k.kind === "idle" ? null : fail);   // today's star map reads K
    this.core.set({ busy: null });
    // Opened or left while it wrote: the note holds the streamed copy, whose fingerprints are from before the text, and a
    // hand edit would be refused as a conflict with nothing. It is brought to the page now.
    if (page === null) { if (!live) await this.notes.pageEditedElsewhere(pageId); return; }
    await this.notes.syncLanded(page, true);
    if (openNote && token === this.core.pageToken && !this.core.disposed) await this.notes.findAndOpen(pageId, token);
  }
}
