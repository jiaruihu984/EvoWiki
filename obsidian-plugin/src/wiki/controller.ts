/**
 * WikiController: the personal-wiki sidebar's state machine. Owns every /wiki call (always through conn.track) and
 * every note sync. Rules (global-constraints.md): one write in flight (single-flight `busy`), at most one write per
 * click (except the new-page chain), nothing sent while offline or unconfigured, no automatic retries, no page GET on
 * init, and the vault note is synced only when the page content really changed (A3).
 *
 * Text setters (setLearnTopic, setAskDraft) update the state without notifying subscribers, so a re-render never steals focus
 * from the field being typed in.
 *
 * This class is the facade main.ts, the views and the tests use. The work is in controller/: the shared context
 * (core.ts: deps, the view state and its setter, the page token, busy), the layers every area builds on (notes.ts: pages
 * landing and their notes in step; reads.ts: the GETs and the P5 refresh; streams.ts: the organize stream and the
 * new-page chain), and one module per area (page, end block, learning path, review, catalog, 个性化, materials drafts, K,
 * evoui blocks). The facade wires them, owns the Bot and 资料 controllers, and runs what spans the areas (init, the
 * 今天 re-read, the reconnect re-reads, the tabs, dispose). An area calls another one only through the hooks it is
 * given here.
 */
import type { DraftDecisions } from "./revise/decisions";
import { BotController } from "./bot/bot-controller";
import { paintRunWrites } from "./bot/paint-writes";
import type { PauseUntil, ReadingEntry, SettingsPatch } from "./bot/bot-schema";
import { WIKI_COPY } from "./copy";
import type { BlockEvent, BlockProgressData } from "./evoui/block-event";
import type { FollowupView } from "./followup";
import { errorText } from "./format";
import type { ReviewFirstChoice } from "./review-first";
import type { ReviewFirstOut } from "./schema";
import type { GraphSource } from "./graph-page/store";
import type { NameBook } from "./names";
import type {
  Declared, ExamSelfIn, ExamSelfOut, FeedbackChip, FollowupChoice, FollowupTarget, Grade, Language, PageExamOut,
  SectionEditIn, SourceDetail, SourceOut,
  StepOut,
} from "./schema";
import { SourcesController, type SourceUpdate } from "./sources/sources-controller";
import type { StarMapSource, StarMapStore } from "./starmap/store";
import { type Feel, type LegacyTab, type PageEnd, type PersonalUi, type RetryAction, type Tab, type WikiState, landTab } from "./state";
import type { TeachPort } from "./teach/model";
import { WritesWatcher, type WritesSnapshot } from "./writes-watcher";
import { Blocks } from "./controller/blocks";
import { Catalog } from "./controller/catalog";
import { WikiCore } from "./controller/core";
import type { WikiControllerDeps } from "./controller/deps";
import { EndBlock } from "./controller/end-block";
import { Engine } from "./controller/engine";
import { Personal } from "./controller/personal";
import { dayOfKey } from "./personal-rules";
import { TODAY_TTL_MS } from "./controller/helpers";

/** Changes often come together (a run's round, its write starting, a merge settling): one read for a burst. */
const CHANGED_DEBOUNCE_MS = 300;
import { Knowledge } from "./controller/knowledge";
import { Learning } from "./controller/learn";
import { Notes } from "./controller/notes";
import { PageActions } from "./controller/page";
import { Reads } from "./controller/reads";
import { Review } from "./controller/review";
import { Revise } from "./controller/revise";
import { Streams } from "./controller/streams";

export type { NoteLocator, WikiControllerDeps } from "./controller/deps";

export class WikiController {
  /** The shared context and the layers the areas build on (bottom up). */
  private readonly core: WikiCore;
  private readonly notes: Notes;
  private readonly reads: Reads;
  private readonly streams: Streams;
  /** The areas (each its own module under controller/). */
  private readonly page: PageActions;
  private readonly endBlock: EndBlock;
  private readonly learn: Learning;
  private readonly review: Review;
  private readonly catalog: Catalog;
  private readonly engine: Engine;
  private readonly personal: Personal;
  private readonly revise: Revise;
  private readonly knowledge: Knowledge;
  private readonly blocks: Blocks;
  private readonly unsubConn: () => void;
  /** The shared draft decisions (one decision with the chat's card): a change redraws the page's bar. */
  private readonly unsubDecisions: () => void;
  /** 今天's quiet re-read: the one in flight, when the last one finished, and the path card each cached plan was read for. */
  private todayRefresh: Promise<void> | null = null;
  private todayAgain: Promise<void> | null = null;
  private todayRefreshedAt = 0;

  constructor(deps: WikiControllerDeps) {
    this.core = new WikiCore(deps);
    this.notes = new Notes(this.core);
    this.notes.ledgerMoved = () => { void this.bot.ledgerChanged(); };
    this.reads = new Reads(this.core, this.notes);
    this.streams = new Streams(this.core, this.notes, this.reads);
    this.page = new PageActions(this.core, this.notes, this.reads, this.streams);
    this.endBlock = new EndBlock(this.core, this.reads, this.notes, {
      done: () => this.done(), followStep: (step) => this.learn.followStep(step),
    });
    this.learn = new Learning(this.core, this.notes, this.reads, this.streams, (id) => this.openWikiPage(id));
    this.review = new Review(this.core, this.notes, this.reads, {
      botReviewFinished: (n, ok) => this.botReviewFinished(n, ok), closeCelebrate: () => this.bot.closeCelebrate(), openWikiPage: (id) => this.openWikiPage(id),
      jumpTo: (sid) => this.page.jumpTo(sid),
    });
    this.catalog = new Catalog(this.core, this.reads);
    this.engine = new Engine(this.core, this.reads, {
      botLoadEngine: () => this.bot.loadEngine(), botLoadActions: () => this.bot.loadActions(), sourcesLoad: () => this.sources.load(),
      reviseRow: (id) => this.revise.reviseRow(id), reviseDecide: (id, op) => this.reviseDecide(id, op),
    });
    this.personal = new Personal(this.core, { loadEngine: () => this.loadEngine(), rereadChanges: () => this.engine.rereadChanges() });
    this.revise = new Revise(this.core, this.notes, {
      ledgerChanged: () => this.bot.ledgerChanged(), openWikiPage: (id) => this.openWikiPage(id),
      jumpToSection: (sid) => this.jumpToSection(sid),
    });
    this.knowledge = new Knowledge(this.core, this.notes, this.reads);
    this.blocks = new Blocks(this.core, this.reads);
    this.unsubDecisions = deps.decisions?.subscribe(() => { this.set({}); }) ?? (() => undefined);
    this.unsubConn = deps.conn.onChange((s) => {
      const was = this._state.conn;
      this.set({ conn: s });
      // Back online (the monitor's own re-ping, or another call that got an answer): the sidebar's offline banner goes
      // and every read that failed meanwhile is made again (what loaded stays as it is).
      if (was === "offline" && s === "online" && !this.disposed) void this.retryConnection();
    });
    // A page written from a chat attachment: its original goes into the vault, then the note is written again with links.
    this.notes.keepOriginals = (page) => {
      for (const src of page.sources ?? []) {
        if (src.deleted || src.origin !== null || src.kind === "url" || src.kind === "text") continue;
        void this.sources.keepOriginal({ id: src.id, title: src.current_title, kind: src.kind }).then((saved) => {
          if (saved) void this.notes.pageEditedElsewhere(page.page_id);
        });
      }
    };
    this.sources = new SourcesController({
      api: deps.api, get: () => this._state, patch: (p) => this.set(p), configured: () => deps.configured(),
      notice: (t) => deps.notice?.(t), call: (f) => this.core.call(f), disposed: () => this.disposed,
      port: deps.sourcePort ?? null,
      progress: (p) => { this._state = { ...this._state, sourceProgress: p }; for (const l of this.progressListeners) l(); },
    });
    const sync = deps.sync;
    if (sync !== null && !deps.demo) {
      this.core.watcher = new WritesWatcher({
        api: deps.api, call: (f) => this.core.call(f),
        active: () => !this.disposed && deps.configured() && deps.conn.state !== "offline",
        foreground: (id) => this.notes.streamingPage === id,
        foregroundDone: (id) => this.notes.foregroundResult.get(id) ?? true,
        streamed: (id, page, valid) => { this.notes.streamBackground(sync, id, page, valid); },
        // a write that ended may have settled a page merge on its commit (the originals went to the trash): files follow
        ended: async (id, valid) => { const ok = await this.notes.syncEdited(id, false, false, valid); if (valid()) void this.notes.reconcileFiles(); return ok === "done"; },
        started: (live, valid) => this.notes.clearStaleWriting(live, valid),
        drop: (id) => { this.notes.dropBackgroundSync(id); },
        forgetForeground: (id) => { this.notes.foregroundResult.delete(id); },
      });
    }
    this.bot = new BotController({
      api: deps.api, get: () => this._state, patch: (p) => this.set(p), configured: () => deps.configured(),
      notice: (t) => deps.notice?.(t), noticeActions: (t, a) => { if (deps.noticeActions) deps.noticeActions(t, a); else deps.notice?.(t); },
      call: (f) => this.core.call(f),
      showChange: (key) => { this.showChange(key); },
      openRevise: (pageId) => { void this.openRevise(pageId); },
      openWikiPage: (id) => this.openWikiPage(id),
      openPath: (id) => this.openPath(id),
      pathNext: () => this.pathNext(),
      startReview: () => this.startReview(),
      askAssistant: (text, pageId) => {
        if (pageId === undefined) { deps.assistantSend?.(text); return; }
        const pg = this._state.page;
        deps.assistantSend?.(text, { pageId, title: pg.kind === "open" && pg.page.page_id === pageId ? pg.page.title : this.names.page(pageId) ?? "" });
      },
      reloadRsi: () => this.rereadRsi(),
      engineShown: () => this._state.tab === "engine",
      sharedWrites: () => this.core.watcher?.snapshot() ?? null,
      paintWrites: (w) => { if (typeof document !== "undefined") paintRunWrites(document, w); },
    });
    this.core.watcher?.subscribe(() => this.revise.followReviseRuns());
    this.core.subscribe(() => this.revise.fetchDraftAssets());
  }

  /** The deps the controller was built with (the context holds them). */
  private get deps(): WikiControllerDeps { return this.core.deps; }
  private set deps(d: WikiControllerDeps) { this.core.deps = d; }
  private get _state(): WikiState { return this.core.state; }
  private set _state(s: WikiState) { this.core.state = s; }
  private get disposed(): boolean { return this.core.disposed; }
  private set(patch: Partial<WikiState>): void { this.core.set(patch); }

  /** The Bot (loads, reactions, approvals, settings); the Bot actions below forward to it. */
  private readonly bot: BotController;
  /** 个性化 · 资料 (list, progress streams, add / rename / delete / open); the source actions below forward to it. */
  private readonly sources: SourcesController;
  /** Told when a source's progress changed WITHOUT a redraw (the sidebar updates that card in place). */
  private readonly progressListeners = new Set<() => void>();
  subscribeProgress(fn: () => void): () => void {
    this.progressListeners.add(fn);
    return () => this.progressListeners.delete(fn);
  }

  get state(): WikiState { return this._state; }

  /** Names collected from everything loaded so far (P7); renderers read it through RenderCtx. */
  get names(): NameBook { return this.core.names; }

  subscribe(fn: () => void): () => void { return this.core.subscribe(fn); }

  /** Aborts any stream; later results are ignored and queued syncs dropped. */
  dispose(): void {
    if (this.changedTimer !== null) { clearTimeout(this.changedTimer); this.changedTimer = null; }
    this.unsubDecisions();
    this.unWatch?.(); this.unWatch = null;
    const waiters = this.core.idleWaiters;
    this.core.idleWaiters = [];
    for (const w of waiters) w();
    this.core.abort?.abort();
    this.core.abort = null;
    this.core.pageToken++;
    this.core.disposed = true;
    this.blocks.warnUnsent();
    for (const sl of this.core.sleepers) { clearTimeout(sl.timer); sl.resolve(); }
    this.core.sleepers.clear();
    this.unsubConn();
    this.bot.dispose();
    this.core.watcher?.stop();
    this.core.listeners.clear();
    this.progressListeners.clear();
    this.sources.leave();
    this.knowledge.dispose();
  }

  // ---------------- view ----------------

  /**
   * 今天 needs its reads: GET /wiki, the K grid, today's review list and the changes list, in parallel (reads, no LLM),
   * each failing on its own (a failure only affects the group that reads it). Never the current page: a page GET may
   * run the lazy patch = LLM calls. Unconfigured: nothing is sent, all four say so.
   */
  async init(): Promise<void> {
    const configured = this.deps.configured();
    this.set({ configured });
    if (!configured) {
      const error = { kind: "error" as const, message: WIKI_COPY.needsSettings };
      this.set({ catalog: error, k: error, rsi: error, review: { ...this._state.review, today: error } });
      return;
    }
    const s = this._state;
    this.set({ catalog: { kind: "loading" } });
    await Promise.all([
      this.reads.loadCatalog(null),
      s.k.kind === "ready" ? Promise.resolve() : this.reads.loadK(null),
      s.review.today.kind === "ready" ? Promise.resolve() : this.reads.loadReview(null),
      s.rsi.kind === "ready" ? Promise.resolve() : this.reads.loadRsi(null),
      s.prefs.kind === "ready" ? Promise.resolve() : this.engine.loadPrefs(),
    ]);
    await this.reads.loadPathPlans();
    await this.review.resumePageRound();
  }

  /**
   * Re-reads what 今天 shows when the view opens or comes back (init skips what is already loaded, so a long-lived
   * controller would show a stale list): the review list, the catalog's paths and the path plans. Quiet: a failed
   * re-read keeps what is shown. Does nothing mid-review (the cards under the user must not change), while a write
   * runs, or unconfigured.
   */
  async refreshToday(force = false): Promise<void> {
    if (this.disposed || this._state.busy || !this.deps.configured() || this._state.review.active) return;
    if (this.todayRefresh !== null) {
      if (!force) return this.todayRefresh;
      // A forced re-read (a new page was written) must not be answered by a read that began before: one more follows it.
      this.todayAgain ??= this.todayRefresh.then(() => { this.todayAgain = null; return this.refreshToday(true); });
      return this.todayAgain;
    }
    if (!force && Date.now() - this.todayRefreshedAt < TODAY_TTL_MS) return;
    this.todayRefresh = this.doRefreshToday().finally(() => { this.todayRefresh = null; this.todayRefreshedAt = Date.now(); });
    return this.todayRefresh;
  }

  private async doRefreshToday(): Promise<void> {
    const seq = ++this.reads.reviewSeq;
    const quiet = (): void => undefined;
    const catalog = this._state.catalog.kind === "ready" ? quiet : null;
    const review = async (): Promise<void> => {
      try {
        const t = await this.core.call(() => this.deps.api.reviewToday());
        if (this.disposed || seq !== this.reads.reviewSeq || this._state.review.active) return;
        const r = this._state.review;
        for (const k of Object.keys(this._state.followups)) if (k.startsWith("card:")) this.core.clearFollowup(k);
        this.core.setReview({ today: { kind: "ready", value: t }, revealed: [], results: {}, confirmResults: {}, seen: [], removed: [], barAt: null,
          note: null, finished: t.items.length + t.confirms.length > 0 ? false : r.finished });
      } catch (e) {
        if (this.disposed || seq !== this.reads.reviewSeq) return;
        if (this._state.review.today.kind !== "ready") this.core.setReview({ today: { kind: "error", message: errorText(e) } });
      }
    };
    await Promise.all([review(), this.reads.loadCatalog(catalog), this.reads.loadRsi(this._state.rsi.kind === "ready" ? quiet : null), this.bot.refresh()]);
    // the Bot's read skipped before the server first answered (the connection not known yet): read it now it does
    if (!this.disposed && this.bot.skipped() && this._state.conn !== "offline") await this.bot.refresh();
    if (!this.disposed) await this.reads.loadPathPlans();
    // Once per session (on load): the files of pages moved or renamed since the last session follow.
    if (!this.disposed && !this.reconciledOnLoad) {
      this.reconciledOnLoad = true; void this.reconcileFiles(); this.core.watcher?.start();
      // a page write starting or ending moves where learning goes on (writing → read)
      // (the watcher ticks every second while a page is written: only a page's write state changing counts)
      let seen = "";
      const w = this.core.watcher;
      this.unWatch = w?.subscribe(() => {
        const key = (w.snapshot()?.writes ?? []).map((x) => `${x.page_id}:${x.state}`).sort().join(",");
        if (key !== seen) { seen = key; this.wikiChanged(); }
      }) ?? null;
    }
  }

  private reconciledOnLoad = false;
  private changedTimer: ReturnType<typeof setTimeout> | null = null;
  private unWatch: (() => void) | null = null;

  /**
   * The wiki changed outside this controller's own writes (a chat goal made or its block entered, a path or map edit, a
   * page write starting or ending in the background, a merge settling, 今天 shown): soon, once for a burst of them, GET
   * /wiki/progress (where learning goes on) and GET /wiki/files (a page the server trashed goes to Obsidian's trash, a
   * moved one follows). The controller's own writes read the catalog and progress already.
   */
  wikiChanged(): void {
    if (this.disposed || !this.deps.configured() || this.changedTimer !== null) return;
    this.changedTimer = setTimeout(() => {
      this.changedTimer = null;
      if (this.disposed) return;
      void this.reads.loadProgress();
      void this.reconcileFiles();
    }, CHANGED_DEBOUNCE_MS);
  }

  /** The wiki view closed (the controller lives on): what only 个性化 shows stops (progress streams, the 真题 poll). */
  viewClosed(): void {
    if (!this.disposed) this.sources.leave();
  }

  /** The wiki view opened (again): on 个性化 its 资料 are read again, which restarts their 真题 summaries. */
  viewOpened(): void {
    if (!this.disposed && this._state.tab === "engine") void this.sources.load();
  }

  /**
   * The two tabs (今天 / 助手). Any other value (a former tab name, or an unknown one) lands on 今天 (landTab). Switching
   * only changes the tab: it reads nothing (init did, and every write refreshes what it changed).
   */
  setTab(t: Tab | LegacyTab): void {
    const tab = landTab(t);
    const back = tab === "today" && this._state.tab !== "today";
    if (this._state.tab === "engine" && tab !== "engine") this.sources.leave();
    this.set({ tab });
    if (back) { void this.refreshToday(); this.wikiChanged(); }
    if (tab === "engine") void this.loadEngine();
  }

  /**
   * The offline banner's 重试 (and coming back online): every read that failed is made again, in every tab — 今天
   * (init skips what already loaded), the Bot's lists, 个性化 (when it was read before) and the small map. What loaded is
   * not read again. One at a time: a call while one runs waits for it.
   */
  retryConnection(): Promise<void> {
    if (this.disposed) return Promise.resolve();
    this.retrying ??= this.doRetryConnection().finally(() => { this.retrying = null; });
    return this.retrying;
  }

  private retrying: Promise<void> | null = null;

  private async doRetryConnection(): Promise<void> {
    const s = this._state;
    const err = (l: { kind: string }): boolean => l.kind === "error";
    const b = s.bot;
    // The writes watcher waited while offline: it looks at the live writes now (the files catch up).
    this.core.watcher?.kick();
    const reads: Promise<void>[] = [];
    if (err(s.catalog) || err(s.k) || err(s.review.today) || err(s.rsi) || err(s.prefs)) reads.push(this.init());
    // The Bot's lists (a failed read, or a refresh skipped while offline).
    if (this.bot.needsRead()) reads.push(this.bot.refresh());
    // 个性化 is read when shown: again when any of its reads failed, or it is shown now with a part never read (offline).
    const engine = [s.engine, s.prefs, b.recent, b.settings, s.sources];
    if (engine.some(err) || (s.tab === "engine" && engine.some((l) => l.kind !== "ready"))) reads.push(this.loadEngine());
    const store = this.knowledge.starStore;
    if (store !== null && (store.state.status === "error" || store.state.stale)) reads.push(store.refresh());
    reads.push(this.flushBlockEvents());
    reads.push(this.flushBlockProgress());
    await Promise.all(reads);
  }

  // ---------------- 资料 (forwarded to SourcesController) ----------------

  sourcesReload(): Promise<void> { return this.disposed ? Promise.resolve() : this.sources.load(); }
  sourceStartRename(id: string): void { if (!this.disposed) this.sources.startRename(id); }
  sourceSetRenameDraft(text: string): void { if (!this.disposed) this.sources.setRenameDraft(text); }
  sourceCommitRename(): Promise<void> { return this.disposed ? Promise.resolve() : this.sources.commitRename(); }
  sourceCancelRename(): void { if (!this.disposed) this.sources.cancelRename(); }
  sourceAskDelete(id: string): void { if (!this.disposed) this.sources.askDelete(id); }
  sourceCancelDelete(): void { if (!this.disposed) this.sources.cancelDelete(); }
  sourceConfirmDelete(id: string): Promise<void> { return this.disposed ? Promise.resolve() : this.sources.confirmDelete(id); }
  sourceOpen(id: string): Promise<void> { return this.disposed ? Promise.resolve() : this.sources.open(id); }
  sourceRetryFollow(id: string): void { if (!this.disposed) this.sources.retryFollow(id); }
  sourceRetryImport(id: string): void { if (!this.disposed) void this.sources.retryImport(id); }
  // 学点新的 · 上传附件 (the goal card's material): the same upload and original-saving path as 从电脑里选.
  async sourcesAdd(files: File[]): Promise<void> {
    if (this.disposed) return;
    this.personalView({ sources: true });       // the card shows it being read
    await Promise.all(files.map((f) => this.sources.attachFile(f).catch((e: unknown) => {
      this.deps.notice?.(e instanceof Error && e.message !== "" ? e.message : WIKI_COPY.failed);
    })));
  }
  sourceAttach(file: File): Promise<SourceOut> { return this.disposed ? Promise.reject(new Error("disposed")) : this.sources.attachFile(file); }
  sourceWatch(id: string, on: (u: SourceUpdate) => void): () => void { return this.disposed ? () => undefined : this.sources.watch(id, on); }
  openSourceAt(id: string, page: number): Promise<"opened" | "gone" | "failed"> { return this.disposed ? Promise.resolve("failed") : this.sources.openAt(id, page); }
  sourceDetail(id: string): Promise<SourceDetail> { return this.disposed ? Promise.reject(new Error("disposed")) : this.sources.detail(id); }
  sourceEditSections(id: string, edits: SectionEditIn[]): Promise<SourceDetail> {
    return this.disposed ? Promise.reject(new Error("disposed")) : this.sources.editSections(id, edits);
  }

  // ---------------- Bot (forwarded to BotController) ----------------

  /** 开始 on a task card ("review" = the local review card, else a suggestion id). */
  botStart(key: string): Promise<void> { return this.disposed ? Promise.resolve() : this.bot.startKey(key); }
  botGoConfirm(key: string): void { if (!this.disposed) this.bot.goConfirm(key); }
  botNotInterested(key: string): Promise<void> { return this.disposed ? Promise.resolve() : this.bot.notInterestedKey(key); }
  botDecide(id: string, op: "approve" | "reject" | "undo"): Promise<void> {
    if (this.disposed) return Promise.resolve();
    // a materials draft decided in 个性化 (撤销 of a confirmed one): the same path as the page's own buttons
    return this.revise.reviseRow(id) !== null ? this.reviseDecide(id, op) : this.bot.decide(id, op);
  }
  /** A ledger action was decided or undone (a merge undone restores pages and trashes one): the files follow too. */
  /** A background wrap-up may have left a preference, a fact or a memory group waiting: what 个性化's count reads, again. */
  async waitingChanged(): Promise<void> {
    if (this.disposed) return;
    await Promise.all([this.engine.loadPrefs(), this.botLedgerChanged()]);
  }

  botLedgerChanged(): Promise<void> {
    if (this.disposed) return Promise.resolve();
    void this.notes.reconcileFiles();
    return this.bot.ledgerChanged();
  }
  /** 在个性化里看: 个性化 · 活动记录, the row of this ledger action framed. */
  botShowAction(actionId: string): void { if (!this.disposed) this.showChange(`act:${actionId}`); }
  /**
   * 查看 a change (app.js showChange): 个性化 with 活动记录 expanded (更早 too when the row is there), then the row
   * `[data-chg=key]` scrolled into view with the ants frame (main.ts reaches the DOM).
   */
  showChange(key: string): void {
    if (this.disposed) return;
    const older = dayOfKey(this._state, key, new Date()) === "older" || this._state.personal.older;
    this.personal.view({ manage: false, log: true, older });
    this.setTab("engine");
    this.deps.revealChange?.(key);
  }
  /** 个性化 · 管理偏好 (a rule's result in the chat, the command). */
  openManagePrefs(): void {
    if (this.disposed) return;
    this.personal.view({ manage: true });
    this.setTab("engine");
  }
  /** `key`: a trigger id, or "s:" + suggestion id when the row has no trigger. */
  botToggleWhy(key: string): Promise<void> {
    if (this.disposed) return Promise.resolve();
    if (key.startsWith("w:")) { this.bot.toggleWriteWhy(key); return Promise.resolve(); }   // a page-write row: its chain is on the row
    return this.bot.toggleWhy(key, key.startsWith("s:") ? { suggestionId: key.slice(2) } : { triggerId: key });
  }
  botSaveSettings(patch: SettingsPatch): Promise<void> { return this.disposed ? Promise.resolve() : this.bot.saveSettings(patch); }
  botPause(until: PauseUntil): Promise<void> { return this.disposed ? Promise.resolve() : this.bot.pause(until); }
  botChip(id: string): Promise<void> { return this.disposed ? Promise.resolve() : this.bot.chipKey(id); }
  /** The page's suggestion chips (re-read when a page opens; a later task renders them). */
  loadBotChips(pageId: string): Promise<void> { return this.disposed ? Promise.resolve() : this.bot.loadChips(pageId); }
  /** The review round ended: the completion card (n cards, ok right) and `done` on its suggestion. */
  botReviewFinished(n: number, ok: number): void { if (!this.disposed) this.bot.reviewFinished(n, ok); }
  closeCelebrate(): void { if (!this.disposed) this.bot.closeCelebrate(); }
  /** The review bar finished sliding to `done` cards: written into the current slot, no redraw (the slot it was drawn from may be gone). */
  markBar(done: number): void { if (!this.disposed) this._state.review.barAt = done; }
  /** The view drew the all-done check (see BotController.markAllDoneSeen). */
  markAllDoneSeen(): void { if (!this.disposed) this.bot.markAllDoneSeen(); }

  // ---------------- settings, Prompt 设置, 讲解方式 ----------------

  /** 个性化 · 去设置里改: the 设置 view. */
  openSettings(): void {
    if (!this.disposed) this.deps.openSettings?.();
  }

  /** Opens the centre-pane editor of a step ("rules" = 讲解方式). */
  openProfile(): void { if (!this.disposed) this.deps.openProfile?.(); }

  openPrompt(key: string): void {
    if (!this.disposed) this.deps.openPrompt?.(key);
  }

  /**
   * What the 讲解方式 settings section talks to (R6.2: its state is not here): this controller's client (the fake for
   * the demo) and its gates.
   */
  teachPort(): TeachPort {
    return { api: this.deps.api, configured: () => this.deps.configured(), conn: this.deps.conn, demo: this.deps.demo };
  }

  // ---------------- notes and files (controller/notes.ts) ----------------

  reconcileFiles(only?: readonly string[]): Promise<boolean> { return this.notes.reconcileFiles(only); }
  pageEditedElsewhere(pageId: string, partial = false, discardEmpty = false): Promise<boolean> { return this.notes.pageEditedElsewhere(pageId, partial, discardEmpty); }
  /** ⋯ 重命名 landed: the open page reads it and the note's file follows. */
  async pageRenamed(pageId: string): Promise<void> {
    await this.notes.pageEditedElsewhere(pageId);
    await this.notes.reconcileFiles([pageId]);
  }
  handEditFirst(pageId: string): Promise<boolean> { return this.notes.handEditFirst(pageId); }
  noteLeft(path: string, pageId: string): Promise<void> { return this.notes.noteLeft(path, pageId); }

  // ---------------- page (controller/page.ts) ----------------

  openWikiPage(pageId: string): Promise<void> { return this.page.openWikiPage(pageId); }
  openPageLink(pageId: string): Promise<void> { return this.page.openPageLink(pageId); }
  startPage(pageId: string, title: string, goal: string): Promise<void> { return this.page.startPage(pageId, title, goal); }
  followPage(pageId: string): Promise<void> { return this.page.followPage(pageId); }
  organize(): Promise<void> { return this.page.organize(); }
  stop(): Promise<void> { return this.page.stop(); }
  stopPage(pageId: string): Promise<boolean> { return this.page.stopPage(pageId); }
  writeRun(pageId: string): "writing" | "stopped" | null { return this.page.writeRun(pageId); }
  writingPage(): string | null { return this.page.writingPage(); }
  restart(pageId: string): Promise<void> { return this.page.restart(pageId); }
  openCurrentNote(): void { this.page.openCurrentNote(); }
  jumpToSection(sid: string): void { this.page.jumpToSection(sid); }
  goToChange(pageId: string, sid: string | null): Promise<void> { return this.page.goToChange(pageId, sid); }
  feedback(sid: string | null, chips: FeedbackChip[]): Promise<void> { return this.page.feedback(sid, chips); }
  writeNote(): Promise<void> { return this.page.writeNote(); }
  know(sid: string): Promise<void> { return this.page.know(sid); }
  toggleFold(sid: string): Promise<void> { return this.page.toggleFold(sid); }
  togglePin(sid: string): Promise<void> { return this.page.togglePin(sid); }
  answerCheck(sid: string, choice: number): Promise<void> { return this.page.answerCheck(sid, choice); }
  done(): Promise<void> { return this.page.done(); }
  expand(): Promise<void> { return this.page.expand(); }
  collapse(): Promise<void> { return this.page.collapse(); }
  retry(r: RetryAction): Promise<void> { return this.page.retry(r); }
  askAssistant(sid: string): void { this.page.askAssistant(sid); }

  // ---------------- 读完了 block / top line (controller/end-block.ts) ----------------

  endBlockState(pageId: string): PageEnd { return this.endBlock.endBlockState(pageId); }
  pickEndChip(pageId: string, feel: Feel): void { this.endBlock.pickEndChip(pageId, feel); }
  undoPageFeedback(pageId: string): Promise<void> { return this.endBlock.undoPageFeedback(pageId); }
  undoHandEdit(pageId: string): Promise<void> { return this.endBlock.undoHandEdit(pageId); }
  restoreNote(pageId: string, note: string): Promise<void> { return this.endBlock.restoreNote(pageId, note); }
  keepMine(pageId: string): Promise<void> { return this.endBlock.keepMine(pageId); }
  finishPage(pageId: string): Promise<void> { return this.endBlock.finishPage(pageId); }
  /** Does what a server step says (GET /wiki/progress): 今天's goal rows, 下一步, a page's end. */
  followStep(step: StepOut): Promise<void> { return this.learn.followStep(step); }

  // ---------------- goal and learning path (controller/learn.ts) ----------------

  setLearnTopic(text: string): void { this.learn.setLearnTopic(text); }
  openLearnNew(topic: string): void { this.learn.openLearnNew(topic); }
  /** Why nothing can be sent now (offline, not set up), or null. */
  blocked(): string | null { return this.core.blocked(); }

  /** A page's organize stream as it writes (WikiCore.writeProgress); returns the remover. */
  onWriteProgress(fn: (p: { pageId: string; done: number; of: number; ended: boolean; ok: boolean }) => void): () => void {
    this.core.writeProgress.add(fn);
    return () => { this.core.writeProgress.delete(fn); };
  }
  openPageRow(pageId: string): Promise<void> { return this.page.openPageRow(pageId); }
  pageFromGoal(pageId: string, mapId: string): void { this.page.pageFromGoal(pageId, mapId); }
  learnFirstPage(pathId: string, onPage?: (pageId: string) => void, openNote = true, key?: string): Promise<void> { return this.learn.learnFirstPage(pathId, onPage, openNote, key); }
  learnWritePage(pageId: string, title: string, goal: string, onPage?: (pageId: string) => void, openNote = true): Promise<void> { return this.learn.learnWritePage(pageId, title, goal, onPage, openNote); }
  openPath(id: string | null): Promise<void> { return this.learn.openPath(id); }
  pathNext(): Promise<void> { return this.learn.pathNext(); }
  pathDone(index: number): Promise<void> { return this.learn.pathDone(index); }
  continueLearning(): Promise<void> { return this.learn.continueLearning(); }
  answerReviewFirst(hint: ReviewFirstOut, choice: ReviewFirstChoice): Promise<void> { return this.learn.answerReviewFirst(hint, choice); }

  // ---------------- review, follow-ups, recap (controller/review.ts) ----------------

  revealCue(cell: string): void { this.review.revealCue(cell); }
  gradeReview(cell: string, grade: Grade): Promise<void> { return this.review.gradeReview(cell, grade); }
  answerReview(cell: string, choice: number): Promise<void> { return this.review.answerReview(cell, choice); }
  nextReview(cell: string): void { this.review.nextReview(cell); }
  startReview(inChat = false): void {
    this.review.startReview(inChat);
    if (!this.disposed && this.state.review.active) void this.bot.reviewStarted();
  }
  /** 「帮我复习」 / 「考考我」 in the chat (`pageId`: 「考考我这页」, that page's cards): today's list read again, the same round as 今天 (same state, same grade route); nothing left: not started. */
  async startChatReview(pageId: string | null = null): Promise<void> {
    await this.init();
    if (this.disposed) return;
    if (pageId !== null) { await this.review.startPageRound(pageId); return; }
    await this.refreshToday(true);
    if (this.disposed) return;
    if (this.state.review.today.kind !== "ready") await this.reads.loadReview(null);
    if (!this.disposed && !this.state.review.active) this.startReview(true);
  }
  pauseReview(): void { this.review.pauseReview(); }
  askAboutCard(cell: string): void { this.review.askAboutCard(cell); }
  reloadReview(): Promise<void> { return this.review.reloadReview(); }
  moreReview(): Promise<void> { return this.review.moreReview(); }
  followupView(key: string): FollowupView { return this.core.followupView(key); }
  followupBlockedWhy(): string | null { return this.core.followupBlockedWhy(); }
  followup(target: FollowupTarget, choice: FollowupChoice): Promise<void> { return this.review.followup(target, choice); }
  answerConfirm(followupId: string, choice: number): Promise<void> { return this.review.answerConfirm(followupId, choice); }
  reteach(followupId: string): Promise<void> { return this.review.reteach(followupId); }
  confirmUnclear(followupId: string): void { this.review.confirmUnclear(followupId); }

  // ---------------- catalog and settings (controller/catalog.ts) ----------------

  reloadCatalog(): Promise<void> { return this.catalog.reloadCatalog(); }
  setReviewPerDay(n: number): Promise<void> { return this.catalog.setReviewPerDay(n); }
  setLanguage(language: Language): Promise<string | null> { return this.catalog.setLanguage(language); }
  setPageLimit(limit: number | null): Promise<string | null> { return this.catalog.setPageLimit(limit); }

  // ---------------- 个性化: reads, prompts, RSI changes (controller/engine.ts) ----------------

  loadEngine(): Promise<void> { return this.engine.loadEngine(); }
  /** Re-read localized projections only. Never POST the Bot refresh or restart business controllers. */
  refreshAccountLanguage(): Promise<void> { return this.catalog.refreshLanguage(); }
  async refreshLocale(): Promise<void> {
    if (this.disposed || this._state.demo || !this.deps.configured()) return;
    await Promise.all([this._state.engine.kind === "ready" ? this.engine.loadEngine() : this.bot.loadEngine(),
      this._state.rsi.kind === "ready" ? this.reads.loadRsi(null) : Promise.resolve()]);
  }
  savePrompt(step: string, text: string): Promise<boolean> { return this.engine.savePrompt(step, text); }
  resetPrompt(step: string): Promise<string | null> { return this.engine.resetPrompt(step); }
  reloadRsi(): Promise<void> { return this.engine.reloadRsi(); }
  rereadRsi(): Promise<void> { return this.engine.rereadRsi(); }
  async undoPatch(patchId: string): Promise<void> {
    const r = this.state.rsi;
    const was = r.kind === "ready" ? r.value.find((c) => c.patch_id === patchId) : undefined;
    await this.engine.undoPatch(patchId);
    if (was?.kind === "page_body") await this.pageBodyUndone(patchId);
    if (was?.kind === "preference") await this.bot.readReviewReminder();   // 以后少提醒复习 taken back: the card follows
  }
  setRsiSwitch(on: boolean): Promise<void> { return this.engine.setRsiSwitch(on); }
  /** A page's text taken back from 活动记录 (a hand edit, an assistant's edit): its note follows. */
  private async pageBodyUndone(patchId: string): Promise<void> {
    const actions = this.state.bot.actions;
    const row = actions.kind === "ready" ? actions.value.find((a) => a.action_id === patchId) : undefined;
    const shown = this.state.page.kind === "open" ? this.state.page.page.page_id : null;
    const pageId = row?.page_id ?? shown;
    if (pageId !== null) await this.notes.pageEditedElsewhere(pageId);
  }
  approvePatch(patchId: string): Promise<void> { return this.engine.approvePatch(patchId); }
  discardPatch(patchId: string): Promise<void> { return this.engine.discardPatch(patchId); }

  // ---------------- 个性化: preferences (controller/personal.ts) ----------------

  personalView(patch: Partial<PersonalUi>): void { this.personal.view(patch); }
  prefDraft(key: string, text: string): void { this.personal.draft(key, text); }
  prefAdopt(id: string, text: string): Promise<boolean> { return this.personal.adopt(id, text); }
  prefReject(id: string): Promise<boolean> { return this.personal.reject(id); }
  prefSave(id: string, text: string): Promise<boolean> { return this.personal.save(id, text); }
  prefRemove(id: string): Promise<boolean> { return this.personal.remove(id); }
  prefRestore(text: string): Promise<boolean> { return this.personal.restore(text); }
  /** 待确认的记忆 · 处理: the 助手 settles the groups in a new chat (deps.resolveMemoryConflicts); nothing offline / unconfigured / in the demo. */
  resolveConflicts(): void {
    const blocked = this.core.blocked();
    if (blocked) { this.core.deps.notice?.(blocked); return; }
    if (this.core.deps.demo) return;
    this.core.deps.resolveMemoryConflicts?.();
  }
  reloadConflicts(): Promise<void> { return this.engine.reloadConflicts(); }

  // ---------------- materials drafts (controller/revise.ts) ----------------

  reviseDecide(actionId: string, op: "approve" | "reject" | "undo"): Promise<void> { return this.revise.reviseDecide(actionId, op); }
  /** 重试 of an in-page draft that no longer fits its page (revise.ts). */
  reviseRedraft(actionId: string): Promise<void> { return this.revise.reviseRedraft(actionId); }
  /** The in-page drafts' decisions shared with the chat's cards (the page's bar reads them). */
  get draftDecisions(): DraftDecisions | undefined { return this.core.deps.decisions; }
  openRevise(pageId: string): Promise<void> { return this.revise.openRevise(pageId); }

  // ---------------- K, 知识星图, 知识图谱 (controller/knowledge.ts) ----------------

  selectCell(cell: string | null): void { this.knowledge.selectCell(cell); }
  declareCell(cell: string, v: Declared | null): Promise<void> { return this.knowledge.declareCell(cell, v); }
  declareSectionDont(sid: string): Promise<void> { return this.knowledge.declareSectionDont(sid); }
  starMapSource(): StarMapSource { return this.knowledge.starMapSource(); }
  graphSource(): GraphSource { return this.knowledge.graphSource(); }
  /** The writes watcher's last read (null in the demo or before the first read): the assistant's cards use it too. */
  writesSnapshot(): WritesSnapshot | null { return this.core.watcher?.snapshot() ?? null; }
  onWritesChanged(fn: () => void): () => void { return this.core.watcher?.subscribe(fn) ?? (() => undefined); }
  noteWriteProgress(pageId: string): { pass: number | null; section: number | null; of: number | null } | undefined {
    const p = this.core.noteWriteProgress;
    if (this.writingPage() === pageId && p?.pageId === pageId) return p;
    const w = this.writesSnapshot()?.writes.find((w) => w.page_id === pageId && ["queued", "planning", "writing"].includes(w.state));
    return w === undefined ? undefined : { pass: w.pass, section: w.section, of: w.of };
  }
  starMapStore(): StarMapStore { return this.knowledge.starMapStore(); }
  declareConcept(conceptId: string, v: Declared, opts: { quietOk?: boolean } = {}): Promise<boolean> { return this.knowledge.declareConcept(conceptId, v, opts); }

  // ---------------- evoui blocks, 真题, reading time (controller/blocks.ts) ----------------

  canReportReading(): boolean { return this.blocks.canReportReading(); }
  reportReading(batchId: string, entries: ReadingEntry[]): Promise<void> { return this.blocks.reportReading(batchId, entries); }
  blockEvent(assetId: string, ev: BlockEvent): boolean { return this.blocks.blockEvent(assetId, ev); }
  pageExam(pageId: string): Promise<PageExamOut | null> { return this.blocks.pageExam(pageId); }
  examSelf(itemId: string, result: ExamSelfIn["result"]): Promise<ExamSelfOut> { return this.blocks.examSelf(itemId, result); }
  blockProgress(assetId: string): Promise<BlockProgressData | null> { return this.blocks.blockProgress(assetId); }
  saveBlockProgress(assetId: string, progress: BlockProgressData): boolean { return this.blocks.saveBlockProgress(assetId, progress); }
  reconcileBlockVisibility(): Promise<void> { return this.blocks.reconcileBlockVisibility(); }
  flushBlockProgress(): Promise<void> { return this.blocks.flushBlockProgress(); }
  flushBlockEvents(): Promise<void> { return this.blocks.flushBlockEvents(); }
}
