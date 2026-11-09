/**
 * SourcesController: 参数 · 资料 (WikiState.sources / sourceProgress / sourceUi). Owned by WikiController, which forwards the
 * source actions to it. Rules: a failed re-read keeps what is shown; every failed write says why in a notice (never silent);
 * nothing is sent offline, unconfigured or in the demo; materials are added in the assistant (chat attachments), not here;
 * deleting a source never deletes a vault file. A progress item updates the card in place (no redraw); only a source that ends redraws the section.
 */
import { AbortedError, ApiError } from "../../api/errors";
import { writeBlockedReason } from "../actions";
import type { WikiApi } from "../api";
import { WIKI_COPY } from "../copy";
import { errorText } from "../format";
import type { ExamSummaryOut, SectionEditIn, SourceDetail, SourceEvent, SourceOut, SourceProgress, SourceStatus, SourceUploadIn } from "../schema";
import type { SourceUi, WikiState } from "../state";
import type { SourcePort } from "./ports";
import { extOf, isSupportedName, KIND_EXT, SOURCES_COPY } from "./sources-copy";

export interface SourcesHost {
  api: WikiApi;
  get(): WikiState;
  patch(p: Partial<WikiState>): void;
  /** Writes the progress map into the state WITHOUT the full-redraw listeners, then tells the in-place ones. */
  progress(p: Record<string, SourceProgress>): void;
  configured(): boolean;
  notice(t: string): void;
  /** Conn-tracked, like WikiController.call. */
  call<T>(f: () => Promise<T>): Promise<T>;
  disposed(): boolean;
  /** The vault side; null = no vault (the demo, tests that do not need it). */
  port: SourcePort | null;
}

/** One source's state as watch() reports it: `error` is the server's line when the import failed. */
export interface SourceUpdate { status: SourceStatus; progress: SourceProgress | null; error: string | null }

/** A file bigger than this is not saved to the vault: the server would refuse it anyway (EVOWIKI_SOURCE_MAX_MB's default). */
const MAX_SAVE_BYTES = 50 * 1024 * 1024;

/** The server's own user-facing sentence for a refused upload / rename (413 / 415 / 422), else the shared error text. */
export function sourceErrorText(e: unknown): string {
  if (e instanceof ApiError && (e.status === 413 || e.status === 415 || e.status === 422) && e.detail.trim() !== "") return e.detail;
  return errorText(e);
}

/** attachFile: the file itself was refused (unsupported, unreadable, the server's 413 / 415 / 422). */
export class SourceRefused extends Error {}
/**
 * attachFile / editSections: the write could not be tried now (not set up, offline, the demo, a write running, the server
 * busy or down). `message` is the plugin's own line for that state (WIKI_COPY / SOURCES_COPY / errorText).
 */
export class SourceBlocked extends Error {}

/** A refusal of the file itself: no cause (the plugin's own check) or the server's 413 / 415 / 422. */
/** While a material's 真题 are still being found (state extracting) and 参数 is shown, its summary is read again this often. */
export const EXAM_POLL_MS = 5000;

/** How often a watched source's stream is reconnected while the server still says it is being read (reset by any event). */
const REFOLLOWS = 2;

const refusesFile = (cause: unknown): boolean =>
  cause === undefined || (cause instanceof ApiError && (cause.status === 413 || cause.status === 415 || cause.status === 422));

export class SourcesController {
  /** Running progress streams by source id. */
  private readonly follows = new Map<string, AbortController>();
  /** Bumped by every list read: an older answer is dropped. */
  private seq = 0;
  /** The 参数 tab was left (leave() ran) and not shown again (load() ran): no new progress stream is started meanwhile. */
  private left = false;
  /** Sources with a rename / delete in flight (a second click meanwhile does nothing). */
  private readonly pending = new Set<string>();
  /** Listeners outside 参数 by source id (watch()): told by the one progress stream follow() runs. */
  private readonly watchers = new Map<string, Set<(u: SourceUpdate) => void>>();
  /** Reconnects used per watched source since its last event (at most REFOLLOWS). */
  private readonly refollows = new Map<string, number>();
  /** The vault copy a download made, by source id (open again reuses it instead of writing "(2)"). */
  private readonly downloaded = new Map<string, string>();
  /** Sources keepOriginal already tried this session. */
  private readonly keeping = new Set<string>();
  /** The next read of the 真题 summaries while one is extracting (null: none planned). */
  private examTimer: ReturnType<typeof setTimeout> | null = null;
  /** Bumped by every 真题 read: an older answer is dropped. */
  private examSeq = 0;

  constructor(private readonly h: SourcesHost) {}

  // ---------------- reading ----------------

  /** GET /wiki/sources; every source still being read gets a progress stream. A failed re-read keeps the list shown. */
  async load(): Promise<void> {
    if (this.h.disposed()) return;
    if (!this.h.configured()) {
      this.h.patch({ sources: { kind: "error", get message() { return WIKI_COPY.needsSettings; } } });
      return;
    }
    this.left = false;
    const seq = ++this.seq;
    if (this.h.get().sources.kind !== "ready") this.h.patch({ sources: { kind: "loading" } });
    try {
      const value = await this.h.call(() => this.h.api.listSources());
      if (this.h.disposed() || seq !== this.seq) return;
      // A stream cut when the tab was left may have ended on a step (术语 1/1): a source no longer read goes by the list's count.
      const reading = new Set(value.filter((s) => s.status === "reading").map((s) => s.id));
      this.h.progress(Object.fromEntries(Object.entries(this.h.get().sourceProgress).filter(([id]) => reading.has(id) || this.follows.has(id))));
      this.h.patch({ sources: { kind: "ready", value } });
      for (const s of value) if (s.status === "reading") this.follow(s.id);
      void this.loadExams();
    } catch (e) {
      if (this.h.disposed() || seq !== this.seq) return;
      if (this.h.get().sources.kind !== "ready") this.h.patch({ sources: { kind: "error", message: errorText(e) } });
    }
  }

  /** Stops every progress stream (the 参数 tab was left, or the controller is gone). The imports go on in the server. */
  leave(): void {
    this.left = true;
    this.stopExamPoll();
    // A stream a watcher (学点新的's material card) still listens to goes on; its stop ends it.
    for (const [id, c] of [...this.follows]) {
      if (this.watchers.has(id)) continue;
      c.abort();
      this.follows.delete(id);
    }
  }

  // ---------------- 真题 summaries ----------------

  private stopExamPoll(): void {
    if (this.examTimer !== null) clearTimeout(this.examTimer);
    this.examTimer = null;
  }

  /**
   * The 真题 line of every listed source used as past exams (a `exam` use): GET /wiki/sources/{id} `exam`. A failed read
   * keeps the summary shown before (logged). Written only when something changed; read again every EXAM_POLL_MS while one
   * is still extracting and 参数 is shown.
   */
  async loadExams(): Promise<void> {
    this.stopExamPoll();
    if (this.h.disposed() || this.left) return;
    const seq = ++this.examSeq;
    const ids = this.list().filter((s) => !s.deleted && s.uses.some((u) => u.use === "exam")).map((s) => s.id);
    const before = this.h.get().sourceExam;
    const got: Record<string, ExamSummaryOut> = {};
    for (const id of ids) {
      try {
        const d = await this.h.call(() => this.h.api.getSource(id));
        if (d.exam !== null) got[id] = d.exam;
      } catch (e) {
        console.warn("EvoWiki: 真题 summary not read; keeping what is shown", e);
        const old = before[id];
        if (old !== undefined) got[id] = old;
      }
      if (this.h.disposed() || seq !== this.examSeq) return;
    }
    if (JSON.stringify(got) !== JSON.stringify(this.h.get().sourceExam)) this.h.patch({ sourceExam: got });
    if (!this.left && Object.values(got).some((e) => e.state === "extracting")) {
      this.examTimer = setTimeout(() => { this.examTimer = null; void this.loadExams(); }, EXAM_POLL_MS);
    }
  }

  private list(): SourceOut[] {
    const s = this.h.get().sources;
    return s.kind === "ready" ? s.value : [];
  }

  private setList(value: SourceOut[], extra: Partial<WikiState> = {}): void {
    this.seq++; // a list read still in flight is older than this write: its answer is dropped
    this.h.patch({ sources: { kind: "ready", value }, ...extra });
  }

  private replace(id: string, f: (s: SourceOut) => SourceOut): void {
    this.setList(this.list().map((s) => (s.id === id ? f(s) : s)));
  }

  private ui(p: Partial<SourceUi>): void { this.h.patch({ sourceUi: { ...this.h.get().sourceUi, ...p } }); }

  private note(id: string, text: string | null, retry = false): void {
    const notes = { ...this.h.get().sourceUi.notes };
    if (text === null) delete notes[id]; else notes[id] = { text, retry };
    this.ui({ notes });
  }

  // ---------------- progress ----------------

  /**
   * Follows a source being read: progress items update its card in place, the end redraws it. One stream per source;
   * watch() listeners hear the same stream. `force`: start it although 参数 was left (a watcher needs it).
   */
  follow(id: string, force = false): void {
    if (this.h.disposed() || (this.left && !force) || this.follows.has(id)) return;
    const ctl = new AbortController();
    this.follows.set(id, ctl);
    let ended = false;
    let status: SourceStatus = "reading";
    const on = (ev: SourceEvent): void => {
      if (ctl.signal.aborted || this.h.disposed()) return;
      this.refollows.delete(id); // the stream works: a later loss starts a fresh budget
      if (ev.event === "progress") {
        // once usable the card says 读好了 and a finished step (74/74 段) is no chapter count; done gives the count
        if (status === "reading" || ev.data.done < ev.data.total) this.h.progress({ ...this.h.get().sourceProgress, [id]: ev.data });
        this.tell(id, { status, progress: ev.data, error: null });
        return;
      }
      if (ev.event === "usable") {
        // Usable while reading goes on: the card shows the status, a watcher can go on (学点新的's 开始); the stream continues to done.
        const d = ev.data;
        status = d.status;
        const others = { ...this.h.get().sourceProgress };
        delete others[id];   // 「正在整理符号和术语」 1/1 is no chapter count either
        this.h.progress(others);
        this.replace(id, (s) => ({ ...s, title: d.title ?? s.title, status: d.status, pages: d.pages ?? s.pages, unreadable_pages: d.unreadable_pages, note: d.note }));
        this.tell(id, { status, progress: null, error: null });
        return;
      }
      ended = true;
      this.follows.delete(id);
      if (ev.event === "done") {
        const d = ev.data;
        // 「读好了 · N 节」 counts sections: the last progress item may count chunks, so the end item's count replaces it
        // (the stored end record says the same after a reload).
        const last = this.h.get().sourceProgress[id];
        this.h.progress({ ...this.h.get().sourceProgress, [id]: { stage: last?.stage ?? "summary", done: d.sections, total: d.sections, message: last?.message ?? "" } });
        this.replace(id, (s) => ({ ...s, title: d.title ?? s.title, status: d.status, pages: d.pages ?? s.pages, unreadable_pages: d.unreadable_pages, note: d.note, error: null, error_code: null }));
        this.tell(id, { status: d.status, progress: null, error: null });
      } else {
        this.replace(id, (s) => ({ ...s, status: "failed", error: ev.data.detail, error_code: ev.data.code, can_retry: true }));
        this.tell(id, { status: "failed", progress: null, error: ev.data.detail });
      }
    };
    void this.h.api.followSource(id, on, { signal: ctl.signal }).then(() => {
      if (this.follows.get(id) === ctl) this.follows.delete(id);
      // The stream closed with no end item: read the truth once instead of leaving a card reading forever.
      if (!ended && !ctl.signal.aborted && !this.h.disposed()) void this.resync(id);
    }, (e: unknown) => {
      if (this.follows.get(id) === ctl) this.follows.delete(id);
      if (ctl.signal.aborted || e instanceof AbortedError || this.h.disposed()) return;
      this.note(id, SOURCES_COPY.readFailedShort, true);
      // A watcher cannot press 重试: read the truth once for it.
      if (this.watchers.has(id)) void this.resync(id);
    });
  }

  private tell(id: string, u: SourceUpdate): void {
    for (const f of [...(this.watchers.get(id) ?? [])]) f(u);
  }

  /** 重试 on a failed import: it runs again and its card follows it. A refusal says so on the card. */
  async retryImport(id: string): Promise<void> {
    this.note(id, null);
    try {
      const out = await this.h.call(() => this.h.api.retrySource(id));
      this.replace(id, () => out);
      this.follow(id);
    } catch (e) {
      this.note(id, e instanceof ApiError && e.detail.trim() !== "" ? e.detail : SOURCES_COPY.retryFailed);
    }
  }

  /** 重试 on a lost progress stream. */
  retryFollow(id: string): void {
    this.note(id, null);
    void this.resync(id);
  }

  /** Re-reads the list; a source still reading is followed again; a watcher hears how it ended. */
  private async resync(id: string): Promise<void> {
    try {
      const value = await this.h.call(() => this.h.api.listSources());
      if (this.h.disposed()) return;
      this.h.patch({ sources: { kind: "ready", value } });
      void this.loadExams();
      const s = value.find((x) => x.id === id);
      if (s?.status === "reading") {
        if (!this.watchers.has(id)) { this.follow(id); return; }
        // A watched stream that keeps breaking while the server says "reading": reconnect at most REFOLLOWS times.
        const used = this.refollows.get(id) ?? 0;
        if (used >= REFOLLOWS) {
          this.refollows.delete(id);
          this.tell(id, { status: "failed", progress: null, error: SOURCES_COPY.streamLost });
          return;
        }
        this.refollows.set(id, used + 1);
        this.follow(id, true);
      } else this.tell(id, s ? { status: s.status, progress: s.progress, error: s.error } : { status: "failed", progress: null, error: SOURCES_COPY.streamLost });
    } catch {
      if (this.h.disposed()) return;
      this.note(id, SOURCES_COPY.readFailedShort, true);
      this.tell(id, { status: "failed", progress: null, error: SOURCES_COPY.streamLost });
    }
  }

  /** Why nothing can be changed now (offline, not set up, the demo, a write running), or null. */
  private blocked(): string | null {
    const s = this.h.get();
    if (s.demo) return SOURCES_COPY.blockedDemo;
    return writeBlockedReason(s);
  }

  /**
   * 学点新的 · 上传附件: one file from the computer exactly as addFiles adds it (the original saved once under
   * `<notes>/资料/`, the source in 资料's list), without the 「已加入」 notice. Rejects with SourceRefused when the file
   * itself was refused, SourceBlocked (the plugin's line for that state) when it could not be tried now.
   */
  async attachFile(f: File): Promise<SourceOut> {
    const reason = this.blocked();
    if (reason !== null) throw new SourceBlocked(reason);
    let why: { text: string; cause: unknown } | null = null;
    const s = await this.addFile(f, (text, cause) => { why = { text, cause }; });
    if (s !== null) return s;
    const w = why as { text: string; cause: unknown } | null;
    if (w === null || refusesFile(w.cause)) throw new SourceRefused(w?.text ?? "");
    throw new SourceBlocked(errorText(w.cause));
  }

  /**
   * One file from the computer: `refuse` gets the line (and the error, when the server refused) when it is not added
   * (null); a failed vault copy is a notice only.
   */
  private async addFile(f: File, refuse: (text: string, cause?: unknown) => void): Promise<SourceOut | null> {
    if (!isSupportedName(f.name)) { refuse(SOURCES_COPY.unsupported(f.name)); return null; }
    let data: ArrayBuffer;
    try { data = await f.arrayBuffer(); } catch { refuse(SOURCES_COPY.readFailed(f.name)); return null; }
    const port = this.h.port;
    let path: string | null = null;
    if (port !== null && data.byteLength <= MAX_SAVE_BYTES) {
      try { path = await port.originalPath(f.name); } catch { this.h.notice(SOURCES_COPY.saveCopyFailed(f.name)); }
    }
    const known = new Set(this.list().map((s) => s.id));
    const s = await this.upload({ file: { name: f.name, data, ...(f.type !== "" ? { type: f.type } : {}) }, ...(path !== null ? { origin: path } : {}) }, refuse);
    if (s === null) return null;
    if (port !== null && path !== null && !known.has(s.id) && s.origin === path) {
      try { await port.writeOriginal(path, data); } catch { this.h.notice(SOURCES_COPY.saveCopyFailed(f.name)); }
    }
    return s;
  }

  /** POST one file; the new source goes first in the list (an existing one stays where it is) and is followed. Null = refused (`refuse` told). */
  private async upload(input: SourceUploadIn, refuse: (text: string, cause?: unknown) => void = (t) => this.h.notice(t)): Promise<SourceOut | null> {
    try {
      const out = await this.h.call(() => this.h.api.uploadSource(input));
      if (this.h.disposed()) return null;
      const cur = this.list();
      const loaded = this.h.get().sources.kind === "ready";
      const has = cur.some((s) => s.id === out.id);
      this.setList(has ? cur.map((s) => (s.id === out.id ? out : s)) : [out, ...cur]);
      // The list was not read yet (the first read is in flight): the card above stands alone, so read the whole list again.
      if (!loaded) void this.load();
      if (out.status === "reading") this.follow(out.id);
      return out;
    } catch (e) {
      if (!this.h.disposed()) refuse(sourceErrorText(e), e);
      return null;
    }
  }

  // ---------------- one source outside 参数 (学点新的's material card) ----------------

  /**
   * 学点新的's material card listens to one source: `on` hears follow()'s own stream (progress → still reading; done; error
   * → failed with the server's line), so an attach opens ONE stream, shared with 参数. A source no longer being read is
   * told its state at once. Returns stop; the stream is stopped too when nobody needs it (no watcher, 参数 left).
   */
  watch(id: string, on: (u: SourceUpdate) => void): () => void {
    const cur = this.list().find((x) => x.id === id);
    if (cur !== undefined && cur.status !== "reading" && !this.follows.has(id)) {
      queueMicrotask(() => { if (!this.h.disposed()) on({ status: cur.status, progress: cur.progress, error: cur.error }); });
      return () => undefined;
    }
    const set = this.watchers.get(id) ?? new Set<(u: SourceUpdate) => void>();
    set.add(on);
    this.watchers.set(id, set);
    this.follow(id, true);
    return () => {
      set.delete(on);
      if (set.size > 0) return;
      this.watchers.delete(id);
      this.refollows.delete(id);
      if (this.left) { this.follows.get(id)?.abort(); this.follows.delete(id); }
    };
  }

  /** GET one source with its sections (its 目录). */
  detail(id: string): Promise<SourceDetail> { return this.h.call(() => this.h.api.getSource(id)); }

  /** PATCH its sections (rename / relevel); refused while writes are blocked (offline, not set up, the demo, a write running). */
  async editSections(id: string, edits: SectionEditIn[]): Promise<SourceDetail> {
    const reason = this.blocked();
    if (reason !== null) throw new SourceBlocked(reason);
    return this.h.call(() => this.h.api.editSections(id, edits));
  }

  // ---------------- rename / delete / open ----------------

  startRename(id: string): void {
    const s = this.list().find((x) => x.id === id);
    if (s !== undefined) this.ui({ rename: id, renameDraft: s.title, confirm: null });
  }

  /** The typed name, kept without a redraw (the field keeps its caret). */
  setRenameDraft(text: string): void { this.h.get().sourceUi.renameDraft = text; }

  cancelRename(): void { this.ui({ rename: null, renameDraft: "" }); }

  async commitRename(): Promise<void> {
    const ui = this.h.get().sourceUi;
    const id = ui.rename;
    if (id === null || this.pending.has(id)) return;
    const s = this.list().find((x) => x.id === id);
    const title = ui.renameDraft.trim();
    if (s === undefined || title === "" || title === s.title) { this.cancelRename(); return; }
    const reason = this.blocked();
    if (reason !== null) { this.h.notice(reason); return; }
    this.pending.add(id);
    try {
      const out = await this.h.call(() => this.h.api.renameSource(id, title));
      if (this.h.disposed()) return;
      this.replace(id, (x) => ({ ...x, title: out.title }));
      this.cancelRename();
      this.h.notice(SOURCES_COPY.renamed);
    } catch (e) {
      if (!this.h.disposed()) this.h.notice(sourceErrorText(e));
    } finally {
      this.pending.delete(id);
    }
  }

  askDelete(id: string): void { this.ui({ confirm: id, rename: null, renameDraft: "" }); }
  cancelDelete(): void { this.ui({ confirm: null }); }

  async confirmDelete(id: string): Promise<void> {
    if (this.pending.has(id)) return;
    const reason = this.blocked();
    if (reason !== null) { this.h.notice(reason); return; }
    const s = this.list().find((x) => x.id === id);
    this.pending.add(id);
    try {
      await this.h.call(() => this.h.api.deleteSource(id));
      if (this.h.disposed()) return;
      this.follows.get(id)?.abort();
      this.follows.delete(id);
      const progress = Object.fromEntries(Object.entries(this.h.get().sourceProgress).filter(([k]) => k !== id));
      this.setList(this.list().filter((x) => x.id !== id), { sourceProgress: progress, sourceUi: { ...this.h.get().sourceUi, confirm: null } });
      this.h.notice(SOURCES_COPY.deleted(s?.title ?? ""));
    } catch (e) {
      if (!this.h.disposed()) this.h.notice(errorText(e));
    } finally {
      this.pending.delete(id);
    }
  }

  /**
   * 打开: the vault file when the source came from one (origin), else the original downloaded once into `<notes>/资料/`
   * (the server keeps it). A link origin opens in the browser.
   */
  async open(id: string): Promise<void> {
    const s = this.list().find((x) => x.id === id);
    const port = this.h.port;
    if (s === undefined) return;
    // Nothing is opened silently: the demo has no real file, and without a vault port there is nothing to open with.
    if (this.h.get().demo) { this.h.notice(SOURCES_COPY.openDemo); return; }
    if (port === null) { this.h.notice(SOURCES_COPY.openFailed(s.title)); return; }
    const origin = s.origin;
    if (origin !== null && /^https?:\/\//i.test(origin)) { window.open(origin, "_blank", "noopener"); return; }
    const tryOpen = async (path: string): Promise<boolean> => {
      try { return port.exists(path) && await port.openFile(path); } catch { return false; }
    };
    if (origin !== null && await tryOpen(origin)) return;
    const again = this.downloaded.get(id);
    if (again !== undefined && await tryOpen(again)) return;
    try {
      const path = await this.saveOriginal(s, port);
      if (!(await tryOpen(path))) this.h.notice(SOURCES_COPY.openFailed(s.title));
    } catch (e) {
      this.h.notice(e instanceof ApiError ? errorText(e) : SOURCES_COPY.openFailed(s.title));
    }
  }

  /** The original downloaded once into `<notes>/资料/`; the server then knows the path, so citations can link to it. */
  private async saveOriginal(s: Pick<SourceOut, "id" | "title" | "kind">, port: SourcePort): Promise<string> {
    const data = await this.h.call(() => this.h.api.sourceFile(s.id));
    if (data.byteLength > MAX_SAVE_BYTES) throw new Error("too large for a vault copy");
    const name = hasExt(s.title) ? s.title : `${s.title}.${KIND_EXT[s.kind]}`;
    const path = await port.originalPath(name);
    await port.writeOriginal(path, data);
    this.downloaded.set(s.id, path);
    await this.h.call(() => this.h.api.setSourceOrigin(s.id, path));
    return path;
  }

  /**
   * A citation in an answer: the original at PDF page `page`. "gone": the material is deleted or cleaned up (he is told).
   * Reads the source itself: a chat attachment need not be in the list 资料 shows.
   */
  async openAt(id: string, page: number): Promise<"opened" | "gone" | "failed"> {
    if (this.h.get().demo) { this.h.notice(SOURCES_COPY.openDemo); return "opened"; }
    const port = this.h.port;
    let s: SourceDetail;
    try {
      s = await this.h.call(() => this.h.api.getSource(id));
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) { this.h.notice(SOURCES_COPY.citedGone); return "gone"; }
      this.h.notice(e instanceof ApiError ? errorText(e) : SOURCES_COPY.openFailed(id));
      return "failed";
    }
    if (port === null) { this.h.notice(SOURCES_COPY.openFailed(s.title)); return "failed"; }
    if (s.origin !== null && /^https?:\/\//i.test(s.origin)) { window.open(s.origin, "_blank", "noopener"); return "opened"; }
    try {
      const known = [s.origin, this.downloaded.get(id)].find((p): p is string => typeof p === "string" && port.exists(p));
      const path = known ?? await this.saveOriginal(s, port);
      if (await port.openFile(path, page)) return "opened";
    } catch (e) {
      if (e instanceof ApiError) { this.h.notice(errorText(e)); return "failed"; }
    }
    this.h.notice(SOURCES_COPY.openFailed(s.title));
    return "failed";
  }

  /**
   * A page cites this material and its original is not in the vault (a chat attachment has no vault path): saved there
   * once, so the page's citations can link to it. True when it was saved; once per source a session, whatever the outcome.
   */
  keepOriginal(s: Pick<SourceOut, "id" | "title" | "kind">): Promise<boolean> {
    const port = this.h.port;
    if (port === null || this.h.get().demo || this.keeping.has(s.id)) return Promise.resolve(false);
    this.keeping.add(s.id);
    return this.saveOriginal(s, port).then(() => true, (e: unknown) => {
      console.warn("EvoWiki: could not save the original of", s.id, e);
      return false;
    });
  }
}

/** True when `title` already ends in an extension the backend reads (a renamed 「讲义.pdf」). */
function hasExt(title: string): boolean { return isSupportedName(title) && extOf(title) !== ""; }
