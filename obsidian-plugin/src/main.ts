import { pickNotesFolder } from "./ui/folder-picker";
import { resolveProfilePath } from "./wiki/profile-path";
import { type App, type Command, type Component, getLanguage, type MarkdownPostProcessorContext, MarkdownRenderChild, MarkdownRenderer, MarkdownView, normalizePath, Notice, Plugin, TFile, type WorkspaceLeaf } from "obsidian";
import { AccountCheck } from "./account";
import { EvoWikiClient } from "./api/client";
import { nodeStreamTransport } from "./api/transport-node-sse";
import { sourcesPort } from "./assistant/attachments";
import { AssistantClient } from "./assistant/client";
import { AssistantController } from "./assistant/controller";
import { ASSISTANT_COPY } from "./assistant/copy";
import { COPY } from "./copy";
import { createObsidianHost } from "./assistant/host";
import { AssistantPanel } from "./assistant/assistant-panel";
import { ASSISTANT_VIEW_TYPE, AssistantView } from "./assistant/assistant-view";
import { sendToAssistant } from "./assistant/send-to";
import { recordStore } from "./assistant/session-store";
import { quoteFor, resolveActiveNote } from "./assistant/target";
import { readConceptId } from "./concept/mapping";
import { ConnectionMonitor } from "./connection";
import { prepareInitialSetup, type SetupDeps } from "./initial-setup";
import { LocalSetupClient } from "./local-setup";
import { SetupService } from "./setup-service";
import { setupText } from "./setup-copy";
import { localizedText } from "./i18n";
import { MaintenanceClient } from "./maintenance";
import { headingAt } from "./selection-ask";
import { quoteArtifactId } from "./assistant/quote-artifact";
import type { Quote } from "./assistant/turn";
import { type EvoWikiSettings, loadPluginData, MAX_GOAL_RUNS, MAX_PROFILE_BASES, parsePluginData, type PluginData } from "./settings";
import { EvoWikiSettingTab } from "./settings-tab";
import { detachRemovedViews } from "./removed-views";
import { SETTINGS_VIEW_TYPE, type SettingsHost, SettingsView } from "./settings-view";
import { validateNotesFolder } from "./notes/note-format";
import { ewNotice } from "./ui/ew-notice";
import { SettingsUpdater } from "./settings-updater";
import { CheckBlockHub, mountCheckBlock } from "./wiki/check-block";
import { COMMAND_COPY, WIKI_COPY } from "./wiki/copy";
import { ReadingReporter, TICK_MS as READING_TICK_MS } from "./wiki/reading-reporter";
import type { AssetFetcher } from "./wiki/embed/assets";
import { EMBED_FENCE, EmbedHub, type EmbedPort, mountEmbedBlock } from "./wiki/embed/embed-block";
import { IMAGE_FENCE, type ImagePort, mountImageBlock } from "./wiki/embed/image-block";
import { EVOUI_FENCE } from "./wiki/evoui/block";
import { type EvouiPort, mountEvouiBlock } from "./wiki/evoui/evoui-block";
import { blockWebRtcUdp } from "./ui/no-webrtc";
import { forgetThemeRules } from "./wiki/evoui/frame-theme";
import type { NoteLocator, WikiController } from "./wiki/controller";
import { inFolder, isWikiNote, pageToFollow } from "./wiki/follow-note";
import { syncWikiNote } from "./wiki/note";
import { FM_KEYS, PAGE_CSS_CLASS } from "./wiki/note/format";
import { obsidianWikiHttp } from "./wiki/transport";
import { ProfileNote } from "./wiki/profile-note";
import { localizedAttribute, localeOf, onLanguageChange, setUiLanguage, uiLanguage, tr } from "./i18n";
import { frameSection } from "./wiki/note-frame";
import { HttpWikiClient, ProfileClient } from "./wiki/api";
import { BOT_COPY } from "./wiki/bot/bot-copy";
import { showChangeOnScreen } from "./wiki/bot/reveal-change";
import { waitBars, waitSig } from "./wiki/page-waits";
import { WIKI_VIEW_TYPE, WikiView } from "./wiki/wiki-view";
import { STAR_COPY } from "./wiki/starmap/copy";
import type { GraphSource } from "./wiki/graph-page/store";
import type { PathEditIn } from "./wiki/schema";
import type { GoalProgressHost } from "./wiki/cloud/goal-progress";
import { MiniCloud } from "./wiki/cloud/mini-cloud";
import { NoteActivation } from "./wiki/tab-rules";
import { STARMAP_VIEW_TYPE, StarMapView } from "./wiki/starmap/starmap-view";
import { FakeGoalApi, FixtureGoalApi } from "./wiki/learn/goal-fake";
import { type GoalApi, HttpGoalApi } from "./wiki/learn/goal-api";
import { LEARN_VIEW_TYPE, LearnView } from "./wiki/learn/learn-view";
import { PROMPT_VIEW_TYPE, PromptView } from "./wiki/prompt-view";
import { type StartPage, START_PAGE_VIEW_TYPE, StartPageView } from "./wiki/start-page-view";
import { endBlockExtension, endBlockPostProcessor } from "./wiki/end-block/end-block-view";
import { type ExamPort, mountQuizAsset } from "./wiki/exam/exam-block";
import type { ExamItemUi } from "./wiki/exam/exam-model";
import { reviseEditorExtension, reviseReadingPostProcessor, watchReviseBar } from "./wiki/revise/overlay";
import { legacyPageIds } from "./wiki/note/migrate";
import { obsidianWikiNoteVault } from "./wiki/note/sync";
import { TreeMarker } from "./wiki/note/tree-marker";
import { applyHeader, type HeaderHooks, type HeaderInfo, headerInfo, watchHeader } from "./wiki/note-page/header";
import { sectionMenuExtension, sectionMenuPostProcessor } from "./wiki/section-menu-view";
import { noteLinksExtension, noteLinksPostProcessor } from "./wiki/note-links";
import type { LanguageHost } from "./wiki/language-setting";
import type { PageLimitHost } from "./wiki/page-limit-setting";
import type { ReviewLimitHost } from "./wiki/review-limit-setting";
import type { TeachPort } from "./wiki/teach/model";
import { UsageClient } from "./wiki/usage";
import { moveIntoUserFolder, obsidianMoveVault, userFolder } from "./wiki/note/user-folder";
import { renderChatReview } from "./wiki/render-chat-review";
import { makeAssetFetcher, makeWikiController, wikiActions, wikiNotesFolder } from "./wiki/wiring";
import { DraftDecisions } from "./wiki/revise/decisions";
import type { PageRoundMemo } from "./wiki/controller/deps";
import { obsidianBackupStore } from "./notes/backup-store";
import { obsidianProfileVault } from "./wiki/profile-vault";
import { createDemoFixture, DEMO_FIXTURE, type DemoFixture } from "./wiki/fixtures-demo";

/** Hand edit: how long after leaving an EvoWiki note it is checked for an edit (lets Obsidian's save land first). */
export const LEAVE_DELAY_MS = 2000;
/** How often the Bot is pulled while Obsidian stays open. */
export const BOT_POLL_MS = 5 * 60_000;
/** After a turn: when what the background wrap-up of the last conversation made is read (a wrap-up with a rule to judge takes up to a minute). */
const WRAPUP_READ_MS = [12_000, 30_000, 75_000];
/** The window-focus Bot refresh runs at most this often (focus fires on every alt-tab). */
export const BOT_FOCUS_MIN_MS = 30_000;

export default class EvoWikiPlugin extends Plugin implements SettingsHost {
  data!: PluginData;
  private client!: EvoWikiClient;
  private conn!: ConnectionMonitor;
  private readonly settingsUpdater = this.makeSettingsUpdater();
  /** GET /me for the configured user (an unknown one is registered), its state and the user's lang (account.ts). */
  account!: AccountCheck;
  private assistant!: AssistantController;
  private wrapupReads: number[] = [];
  /** The personal-wiki controller (created on first use); wikiDemo says whether it runs on the fake client. */
  private wiki: WikiController | null = null;
  private wikiDemo = false;

  /** The active note's selection as the chat's live quote. */
  private selectionToChat(): void {
    const view = this.app.workspace.getActiveViewOfType(MarkdownView);
    if (view === null || view.file === null || !this.data.settings.allowSelectionContext) return;
    const sel = view.editor.getSelection();
    if (sel.trim() === "") { this.assistant.selectionChanged(null); return; }
    const target = resolveActiveNote(view.file, readConceptId(this.frontmatter(view.file)), view.editor.getValue());
    if (target.kind !== "page" && target.kind !== "note") { this.assistant.selectionChanged(null); return; }
    const r = quoteFor(sel, headingAt(this.app.metadataCache.getFileCache(view.file)?.headings, view.editor.getCursor("from").line));
    // too long for a quote: the menu's 问陪读助手 sends it as an attachment
    this.assistant.selectionChanged(r.ok ? this.quoteWithArtifact(r.quote, target.kind === "page" ? target.pageId : null) : null);
  }

  /** The quote with the artifact id of the section it came from, read from the page as last rendered (nothing when unknown). */
  private quoteWithArtifact(q: Quote, pageId: string | null): Quote {
    if (pageId === null) return q;
    const p = this.wiki?.state.page;
    const id = quoteArtifactId(p?.kind === "open" ? p.page : null, pageId, q.heading);
    return id === null ? q : { ...q, artifactId: id };
  }
  /** Dev-only demo fixture (plugin data `fixture: "demo"`, wiki/fixtures-demo.ts); null = the server, as usual. */
  private fixture: DemoFixture | null = null;
  /** The real wiki controller as seen by the evowiki-check blocks in notes (never the demo one). */
  private readonly checkHub = new CheckBlockHub<WikiController>();
  /** One decision per in-page draft, shared by the page's bar (wiki) and the chat's card (assistant). */
  private readonly draftDecisions = new DraftDecisions();
  /** The mounted embedded-html blocks (redrawn on a theme change or a fetched asset) and the one asset fetcher. */
  private readonly embedHub = new EmbedHub();
  /** 真题练习: each question's state (看答案, the pressed choice) for this session, kept across note redraws. */
  private readonly examUi = new Map<string, ExamItemUi>();
  /** T10: the note page header (h1 + 目标 / 更新于) watched per open Markdown view. */
  private readonly headerWatch = new Map<MarkdownView, () => void>();
  /** The 用资料改这一页 bar of each open Markdown view (review mode), stopped with its header watch. */
  private readonly barWatch = new Map<MarkdownView, () => void>();
  private assets!: AssetFetcher;
  /** The file the wiki last saw become active (followWikiNote acts only when it changes). */
  private lastActivePath: string | null = null;
  /** app.js openTab: a Markdown note becoming active shows the sidebar's 助手 tab (after start-up; start shows 今天). */
  // (not during a review: 跳到这一节 reads the note, and the card with its grades stays on 今天)
  private readonly noteActivation = new NoteActivation(() => { if (this.wiki?.state.review.active !== true) this.wiki?.setTab("assistant"); });
  /** A wiki note that became active while the demo was open or a write ran: followed once that is over. */
  private pendingFollow: TFile | null = null;
  /**
   * The hand edit's leave trigger: the EvoWiki note the user is in (null = none), and the reports due for the
   * notes just left (by path; canceled when the note becomes active again before its delay is over).
   */
  private editWatch: { path: string; pageId: string } | null = null;
  private readonly leaveTimers = new Map<string, ReturnType<typeof setTimeout>>();
  /** The learner profile note 「我」 kept in step with the server's copy (null: the demo fixture, no server). */
  private profile: ProfileNote | null = null;
  /** 设置 surfaces told after a new server / user id (SettingsHost.onIdentityChange). */
  private readonly identityListeners = new Set<() => void>();
  private budgetSetup: AbortController | null = null;
  /** Resolves once the old layout was moved into the user's folder (moveOldLayout): no note is read or written before. */
  private folderReady: Promise<void> = Promise.resolve();

  private notStarted = false;

  /**
   * Obsidian's own loadData answers null for a data.json it cannot parse: the plugin would run on the default server and
   * user, then save those over his file. Here that throws, and onload stops.
   */
  override async loadData(): Promise<unknown> {
    const { adapter } = this.app.vault;
    const path = `${this.manifest.dir}/data.json`;
    return await adapter.exists(path) ? parsePluginData(await adapter.read(path)) : null;
  }

  override async onload(): Promise<void> {
    try {
      this.data = loadPluginData(await this.loadData());
    } catch (e) {
      console.error("EvoWiki: data.json is unreadable; not starting", e);
      this.notStarted = true;
      new Notice(COPY.dataUnreadable(`${this.manifest.dir}/data.json`), 0);
      return;
    }
    // Dev only (plugin data `fixture: "demo"`): the real wiki, assistant and usage section on the demo's data.
    setUiLanguage(this.data.bootstrapLanguage ?? localeOf(obsidianLocale()));
    this.register(onLanguageChange(() => {
      this.data.bootstrapLanguage = uiLanguage();
      void this.saveData(this.data);
      void this.profile?.refreshLanguage();
      if (this.wiki && !this.wikiDemo) {
        void this.wiki.refreshLocale();
        const catalog = this.wiki.state?.catalog;
        if (catalog?.kind === "ready" && catalog.value.settings.language !== uiLanguage())
          void this.wiki.refreshAccountLanguage().catch(() => undefined);
      }
      if (this.assistant?.state.models) void this.assistant.loadModels();
      const commands = (this.app as App & { commands?: { commands: Record<string, Command> } }).commands?.commands;
      if (commands) for (const command of this.wikiCommands()) {
        const registered = commands[`${this.manifest.id}:${command.id}`];
        if (registered) registered.name = command.name;
      }
    }));
    this.fixture = this.data.fixture === DEMO_FIXTURE ? createDemoFixture({ delayMs: 120 }) : null;
    this.fixture?.seedSessions(this.data.assistantSessions, { server: this.data.settings.serverUrl, user: this.data.settings.userId });
    this.fixture?.seedSeen(this.data.seenDots);
    this.client = new EvoWikiClient(() => ({ baseUrl: this.data.settings.serverUrl, userId: this.data.settings.userId }),
      obsidianWikiHttp);
    // The probe: any HTTP answer means the server is there (an unknown user's 404 included).
    this.conn = new ConnectionMonitor(async () => { if (this.fixture === null) await this.client.me(); });
    // back online (the sidebar's 重试, a reconnect ping): the chat's messages that could not be sent go out now
    this.conn.onChange((s) => {
      if (s === "online" && this.account?.state.kind === "ready") void this.assistant?.resendOffline();
    });
    // The dev fixture needs no server: its probe always answers, and the account check's calls to the absent server never
    // switch the offline banners on (they follow a real server only).
    if (this.fixture !== null) this.conn.track = <T,>(p: Promise<T>): Promise<T> => p;
    this.account = this.makeAccount();
    this.account.subscribe(() => {
      if (this.account.state.kind === "ready" && this.conn.state === "online") void this.assistant?.resendOffline();
    });
    if (this.fixture === null) {
      this.folderReady = new Promise((resolve) => { this.app.workspace.onLayoutReady(() => { void this.moveOldLayout().then(resolve, resolve); }); });
      this.profile = new ProfileNote(
        new ProfileClient(() => ({ baseUrl: this.data.settings.serverUrl, userId: this.data.settings.userId }), obsidianWikiHttp),
        obsidianProfileVault(this.app, obsidianBackupStore(this.app)), () => this.resolveProfilePath(), undefined,
        recordStore(() => this.data.profileBases, () => this.saveData(this.data), MAX_PROFILE_BASES),
        (n) => { ewNotice(WIKI_COPY.profileKept(n)); },
        (lines, undo) => {
          ewNotice(WIKI_COPY.knownRetracted(lines), undo === null ? undefined : { label: WIKI_COPY.topUndo, run: () => { void undo().catch(() => { ewNotice(WIKI_COPY.topUndoFailed); }); } });
        });
      this.registerEvent(this.app.vault.on("modify", (f) => this.profile?.edited(f.path)));
      this.registerEvent(this.app.vault.on("rename", (file, previous) => {
        if (!(file instanceof TFile)) return;
        let changed = false;
        for (const [identity, path] of Object.entries(this.data.profilePaths ?? {})) if (path === previous) {
          this.data.profilePaths![identity] = file.path; changed = true;
        }
        if (!changed) return;
        const version = this.data.profileBases[previous];
        if (version !== undefined) { this.data.profileBases[file.path] = version; delete this.data.profileBases[previous]; }
        void this.saveData(this.data);
      }));
    }
    this.assistant = new AssistantController({
      api: this.fixture?.assistant ?? new AssistantClient(() => ({ baseUrl: this.data.settings.serverUrl, userId: this.data.settings.userId }),
        obsidianWikiHttp, nodeStreamTransport),
      store: recordStore(() => this.data.assistantSessions, () => this.saveData(this.data)),
      // 查看详情 on an action card: its result (a page at its section, a goal in 学点新的, 个性化 · 管理偏好)
      results: {
        openPage: (pageId, sectionId) => {
          const c = this.wikiController();
          void c.openWikiPage(pageId).then(() => { if (sectionId !== null) c.jumpToSection(sectionId); }, () => { ewNotice(WIKI_COPY.failed); });
        },
        openGoal: (actionId, mapId) => { void this.openGoalResult(actionId, mapId); },
        openRules: () => { void this.openEngine("manage"); },
        openReview: (pageId) => { void this.wikiController().startChatReview(pageId); },
      },
      identity: () => ({ server: this.data.settings.serverUrl, user: this.data.settings.userId }),
      newId: () => crypto.randomUUID(),
      // An accepted 加入页面: the personal wiki re-reads that page and updates its wiki note (E3b). The demo wiki never
      // writes the vault, so nothing then; otherwise the real controller (built here if no wiki view was opened yet).
      pageEdited: (pageId) => { if (!this.wikiDemo) void this.wikiController().pageEditedElsewhere(pageId); },
      handEditFirst: (pageId) => (this.wikiDemo ? Promise.resolve(true) : this.wikiController().handEditFirst(pageId)),
      openCitation: (id, page) => this.wikiController().openSourceAt(id, page),
      // Chat attachments: uploaded as materials with use "ask" and the conversation's session (/wiki/sources).
      attachments: sourcesPort(this.fixture?.wiki ?? new HttpWikiClient(
        () => ({ baseUrl: this.data.settings.serverUrl, userId: this.data.settings.userId }), obsidianWikiHttp, nodeStreamTransport)),
      // An action card decided a ledger row: 个性化 re-reads it.
      ledgerChanged: () => {
        void this.wiki?.botLedgerChanged(); void this.syncProfile();
        if (!this.wikiDemo) void this.wikiController().reconcileBlockVisibility();
      },
      memoryForgotten: () => { if (!this.wikiDemo) void this.wikiController().reconcileBlockVisibility(); },
      // an in-page draft's 确认替换 / 取消: one decision with the page's bar
      decisions: this.draftDecisions,
      // what the turn wrote about him (remember_about_me / remember_preference) shows in 「我」
      turnDone: () => {
        const learn = this.app.workspace.getLeavesOfType(LEARN_VIEW_TYPE)[0]?.view;
        if (learn instanceof LearnView) learn.refreshShownMap();
        void this.syncProfile();
        this.afterWrapup();
      },
      // The wiki's writes watcher already reads the live writes: the cards use that read while it is fresh.
      sharedWrites: () => this.wiki?.writesSnapshot() ?? null,
      // A goal confirmed in the chat goes on in the chat: its diagnosis, the first block, the first page (as 学点新的's).
      goals: {
        api: () => this.goalApi(), writeFirstPage: (t, onPage) => this.writeFirstPage(t, onPage),
        pageFromGoal: (pageId, mapId) => { this.wikiController().pageFromGoal(pageId, mapId); },
        changed: () => { this.wiki?.wikiChanged(); },
        lastGoal: {
          load: () => window.localStorage.getItem(`evowikiLastGoal:${this.data.settings.userId}`),
          save: (id) => { window.localStorage.setItem(`evowikiLastGoal:${this.data.settings.userId}`, id); },
        },
        kept: recordStore(() => this.data.goalRuns, () => this.saveData(this.data), MAX_GOAL_RUNS),
        // 重新写 on the card after the chat's first page was stopped
        stoppedWrite: () => (this.wiki !== null && !this.wikiDemo ? this.wiki.state.stopped?.pageId ?? null : null),
        restartWrite: (pageId) => this.wikiController().restart(pageId),
        stopPage: (pageId) => (this.wikiDemo ? Promise.resolve(false) : this.wikiController().stopPage(pageId)),
      },
    });
    const assistantHost = createObsidianHost(this.app, (id) => this.conceptNameZh(id),
      (pageId) => { void this.wikiController().openWikiPage(pageId); },
      (pageId) => { if (!this.wikiDemo) void this.wikiController().openRevise(pageId); });
    assistantHost.reviewLive = () => this.wiki?.state.review.active === true;
    assistantHost.reviewCard = () => {
      const c = this.wikiController();
      // a reloaded chat: the round is read when its card is first drawn
      if (c.state.review.today.kind === "idle") queueMicrotask(() => { void c.init().then(() => c.refreshToday()); });
      return renderChatReview(c.state, { ...wikiActions(() => c), startReview: () => void c.startChatReview() },
        { renderMarkdown: (el, md) => { void MarkdownRenderer.render(this.app, md, el, "", this); }, names: c.names });
    };
    // The old standalone view stays registered (a saved layout may restore it); nothing opens it any more (Task 8).
    this.registerView(ASSISTANT_VIEW_TYPE, (leaf) => new AssistantView(leaf, this.assistant, assistantHost));
    // Reading assistant (F3o): an explicit click only. The selection becomes a quote shown above the composer and is
    // sent only when the user clicks 问. Offered on the active note's own editor, for a concept-linked or personal-wiki note.
    // The wiki sidebar opens on its 助手 tab first, so the quote's focus lands in a visible input.
    this.registerEvent(this.app.workspace.on("editor-menu", (menu, editor, info) => {
      const file = info.file;
      const { allowSelectionContext, serverUrl, userId } = this.data.settings;
      if (!file || !allowSelectionContext || serverUrl === "" || userId === "" || file.path !== this.app.workspace.getActiveFile()?.path) return;
      const sel = editor.getSelection();
      if (sel.trim() === "") return;
      const target = resolveActiveNote(file, readConceptId(this.frontmatter(file)), editor.getValue());
      if (target.kind !== "page" && target.kind !== "note") return;
      menu.addItem((i) => i.setTitle(ASSISTANT_COPY.menuAsk).setIcon("messages-square").onClick(() => {
        const r = quoteFor(sel, headingAt(this.app.metadataCache.getFileCache(file)?.headings, editor.getCursor("from").line));
        if (!r.ok && r.reason === "blank") return;
        const attach = (): void => {
          if (r.ok) { this.assistant.attachQuote(target, this.quoteWithArtifact(r.quote, target.kind === "page" ? target.pageId : null)); return; }
          // too long for a quote: it goes along as a text attachment (never refused, never cut)
          void this.assistant.attachTextFile(target, sel).then((ok) => { if (!ok) ewNotice(ASSISTANT_COPY.quoteTooLong); });
        };
        void this.openAssistant().then(attach, (e: unknown) => { console.warn("EvoWiki: could not open the 助手 tab", e); attach(); });
      }));
    }));

    // What is selected in the note is what he means by 这句: the composer shows it as the quote (× takes it off); 问 sends it. Focus in the sidebar keeps it.
    let selectionTimer: number | null = null;
    this.registerDomEvent(document, "selectionchange", () => {
      if (selectionTimer !== null) window.clearTimeout(selectionTimer);
      selectionTimer = window.setTimeout(() => { selectionTimer = null; this.selectionToChat(); }, 120);
    });
    this.registerView(WIKI_VIEW_TYPE, (leaf) => new WikiView(leaf, this.wikiController(), wikiActions(() => this.wikiController(),
      () => (this.wikiDemo ? Promise.resolve() : this.reconnect())),
      (v) => ({ renderMarkdown: (el, md) => { void MarkdownRenderer.render(this.app, md, el, "", v); }, names: this.wikiController().names }),
      // 助手 tab: the one assistant controller (Ruling R8.1), the same panel the old view mounts.
      // The Bot's page chips ride in through a narrow source (reads this.wiki; never builds a controller).
      (v) => new AssistantPanel(this.assistant, assistantHost, v, {
        list: (pageId) => this.wiki?.state.bot.chips[pageId] ?? [],
        settings: () => { const st = this.wiki?.state.bot.settings; return st && st.kind === "ready" ? st.value : null; },
        onChip: (s) => { void this.wiki?.botChip(s.suggestion_id); },
        subscribe: (fn) => this.wiki?.subscribe(fn) ?? (() => undefined),
        ledger: {
          rows: () => { const a = this.wiki?.state.bot.actions; return a && a.kind === "ready" ? a.value : null; },
          show: (id) => { this.wiki?.botShowAction(id); },
        },
      }),
      // Closing the demo wiki's leaf ends the demo; following and the settings rows go back to the real wiki.
      (c) => this.wikiViewClosed(c),
      // 今天's small knowledge cloud: a live miniature of the 知识图谱 page, on the graph the goal rows read.
      () => new MiniCloud({ openStarMap: (goal) => { void this.openStarMap(goal); },
        goals: this.goalProgressHost(), onCssChange: (fn) => this.onCssChange(fn) }),
      // The red dots' seen keys (plugin data, bounded).
      { load: () => this.data.seenDots, save: (keys) => { this.data.seenDots = keys; void this.saveData(this.data); } },
      this.fixture !== null || this.wikiDemo ? null : this.account));
    // 知识图谱 (main-area tab, graph.html on GET /wiki/graph). The command comes with the other entry points; the ribbon icon opens it now.
    this.registerView(STARMAP_VIEW_TYPE, (leaf) => new StarMapView(leaf, {
      source: this.graphSource(),
      openLearn: () => { void this.openLearn("", true); },
      // A written page opens (at the section that teaches the node); a page in the trash is written again, not opened.
      showPage: (pageId, sectionId) => {
        const c = this.wikiController();
        c.openWikiPage(pageId).then(() => { if (sectionId !== null) c.jumpToSection(sectionId); }, () => { ewNotice(WIKI_COPY.failed); });
      },
      writePage: (pageId, title, goal) => { void this.wikiController().learnWritePage(pageId, title, goal); },
      pageRenamed: (pageId) => { void this.wikiController().pageRenamed(pageId); },
      openRow: (r) => {
        if (r.pageId !== null) this.wikiController().openPageRow(r.pageId).catch(() => { ewNotice(WIKI_COPY.failed); });
        else void this.openStartPage({ pageId: "", title: r.title, goal: r.goal, pathId: r.pathId, key: r.key });
      },
      onCssChange: (fn) => this.onCssChange(fn),
      // The page's one offline banner follows the connection; its 重试 is the sidebar's: reconnect, reload everything.
      connection: {
        offline: () => !this.wikiDemo && this.conn.state === "offline",
        onChange: (fn) => this.conn.onChange(() => fn()),
        retry: async () => {
          if (!this.wikiDemo) await this.reconnect().catch(() => undefined);
          await this.wikiController().retryConnection();
        },
      },
      // a ledger action waiting with no page: its bar at the top of the panel (page-waits.ts)
      waits: {
        bars: () => { const c = this.wikiController(); return waitBars(c.state, null, { botDecide: (id, op) => { void c.botDecide(id, op); } }); },
        sig: () => waitSig(this.wikiController().state, null),
        subscribe: (fn) => this.wikiController().subscribe(fn),
      },
    }));
    const ribbon = this.addRibbonIcon("network", STAR_COPY.ribbon, () => { void this.openStarMap(); });
    if (ribbon) localizedAttribute(ribbon, "aria-label", () => STAR_COPY.ribbon);
    // 学点新的 (main-area tab, big-goal-demo.html) on the big-goal routes (the scripted fake only for the demo fixture).
    this.registerView(LEARN_VIEW_TYPE, (leaf) => new LearnView(leaf, {
      api: () => this.goalApi(),
      onCssChange: (fn) => this.onCssChange(fn),
      // The existing map the goal grows out of (the star map: K, the catalog and the goal membership).
      graph: () => this.graphSource(),
      configured: () => this.wikiController().state.configured,
      // 上传附件: 个性化 · 资料's own upload (the original saved under <notes>/资料/) and its source routes.
      sources: {
        upload: (file) => this.wikiController().sourceAttach(file),
        follow: (id, on) => this.wikiController().sourceWatch(id, on),
        detail: (id) => this.wikiController().sourceDetail(id),
        editSections: (id, edits) => this.wikiController().sourceEditSections(id, edits),
      },
      writeFirstPage: (t, onPage) => this.writeFirstPage(t, onPage),
      pageFromGoal: (pageId, mapId) => { this.wikiController().pageFromGoal(pageId, mapId); },
      pageRenamed: (pageId) => { void this.wikiController().pageRenamed(pageId); },
      // a page card's title, once its page has a section; 读懂这一篇: a unit read already, a 先补 chip's page
      openPage: (pageId) => { this.wikiController().openPageRow(pageId).catch(() => { ewNotice(WIKI_COPY.failed); }); },
      // the page cards fill as the wiki controller streams a page (its outline, each section done)
      writeProgress: (fn) => this.wikiController().onWriteProgress(fn),
      // 学习路径's block marks: GET /wiki/progress as the wiki read it last (the demo wiki has none)
      progress: () => { const w = this.wiki; return w !== null && !this.wikiDemo && w.state.progress.kind === "ready" ? w.state.progress.value : null; },
      onProgress: (fn) => this.wikiController().subscribe(fn),
      // 开始 (chat-first blocks): the goal goes to the 助手 (its tab opens) in a new conversation; the view follows its
      // goal_new rows.
      blocked: () => this.wikiController().blocked(),
      assistant: {
        start: (text, learn, materials) => {
          const chat = this.assistant.startGoalChat(text, learn, materials);
          if (chat !== null) void this.openAssistant().catch((e: unknown) => { console.warn("EvoWiki: could not open the 助手 tab", e); });
          return chat;
        },
        onGoalRow: (fn) => this.assistant.onGoalRow(fn),
        pending: () => this.assistant.pendingGoal(),
        showGoal: (actionId) => {
          const shown = this.app.workspace.getLeavesOfType(LEARN_VIEW_TYPE)[0]?.view.containerEl.isShown() ?? false;
          if (shown) this.assistant.showGoalChat(actionId);
        },
        run: (actionId) => (actionId !== undefined ? this.assistant.goalRun(actionId) : this.assistant.currentGoal()) ?? null,
        resume: async () => {
          const run = await this.assistant.resumeGoal();
          // only the tab in front: a tab rebuilt behind the note he reads leaves his chat alone
          const shown = this.app.workspace.getLeavesOfType(LEARN_VIEW_TYPE)[0]?.view.containerEl.isShown() ?? false;
          if (run !== null && shown) { this.assistant.showGoalChat(run.actionId); void run.resumeDiagnosis(); }
          return run;
        },
        openGoal: (mapId) => this.assistant.openGoal(mapId),
        // the chat's diagnosis as 学点新的 shows it (told only when it changes: a stream's every delta is not)
        diag: () => this.assistant.diag(),
        onDiag: (fn) => {
          let seen = JSON.stringify(this.assistant.diag());
          return this.assistant.subscribe(() => { const now = JSON.stringify(this.assistant.diag()); if (now !== seen) { seen = now; fn(); } });
        },
        // 学点新的's list edits the waiting draft as the chat card did; 确认 / 取消 decide it
        draft: {
          move: (id, from, to) => { this.assistant.moveDraft(id, from, to); },
          remove: (id, k) => { this.assistant.removeDraft(id, k); },
          add: (id, title) => { this.assistant.addDraft(id, title); },
          rename: (id, k, title) => { this.assistant.renameDraft(id, k, title); },
          confirm: (id) => this.assistant.confirmAction(id),
          cancel: (id) => this.assistant.cancelAction(id),
        },
      },
    }));
    // Prompt 设置: one main-area tab per step, and one for 讲解方式 (app.js renderPrompt / renderRules).
    this.registerView(PROMPT_VIEW_TYPE, (leaf) => new PromptView(leaf, { controller: () => this.wikiController() }));
    // A page not written yet: its title, 目标 and 开始生成 (the write's note then opens in that tab).
    this.registerView(START_PAGE_VIEW_TYPE, (leaf) => new StartPageView(leaf, {
      start: (p) => {
        if (p.pageId !== "") { void this.wikiController().startPage(p.pageId, p.title, p.goal); return; }
        this.writeFirstPage({ pathId: p.pathId ?? null, pageId: null, title: p.title, goal: p.goal, ...(p.key !== undefined ? { key: p.key } : {}) })
          .catch((e: unknown) => { ewNotice(e instanceof Error && e.message !== "" ? e.message : WIKI_COPY.failed); });
      } }));
    // 设置 (main-area tab, app.js renderSettings): the same content as the settings tab.
    this.registerView(SETTINGS_VIEW_TYPE, (leaf) => new SettingsView(leaf, this));
    // 确认题 in a wiki note: the block names page + section; the question and the answer come from the wiki controller.
    // Blocks only in the current user's notes: in another user's they would act on that user's page ids.
    if (this.fixture === null) this.checkHub.owns = (p) => p !== null && inFolder(p, wikiNotesFolder(this.data.settings));
    const owned = (f: (el: HTMLElement, ctx: MarkdownPostProcessorContext) => void) =>
      (el: HTMLElement, ctx: MarkdownPostProcessorContext): void => { if (this.checkHub.owns(ctx.sourcePath)) f(el, ctx); };
    this.registerMarkdownCodeBlockProcessor("evowiki-check", (source, el, ctx) => {
      if (!this.checkHub.owns(ctx.sourcePath)) return;
      const child = new MarkdownRenderChild(el);
      child.register(mountCheckBlock(el, source, this.checkHub, (pageId) => { void this.wikiController().followPage(pageId); }));
      ctx.addChild(child);
    });
    // An interactive (embedded-html block, appendix A·6) in any note: the vault file in a sandboxed iframe, or 取回.
    // A written asset redraws only the blocks waiting for it; a theme change only frames whose theme values changed.
    this.assets = makeAssetFetcher({ app: this.app, settings: () => this.data.settings, http: obsidianWikiHttp, sse: nodeStreamTransport,
      ...(this.fixture ? { fixtureApi: this.fixture.wiki } : {}) }, (id) => this.embedHub.assetWritten(id));
    this.registerEvent(this.app.workspace.on("css-change", () => { forgetThemeRules(); this.embedHub.themeChanged(); }));
    blockWebRtcUdp(window);
    this.registerEvent(this.app.workspace.on("window-open", (_w, win) => { blockWebRtcUdp(win); }));
    // The user's own wiki uses the same block through another plugin (R8.1). Registered only once the layout is ready,
    // i.e. after every plugin enabled at startup has loaded, so that plugin always registers first and keeps drawing
    // them; then ours fails and EvoWiki stays out of the way. Residual case: that plugin enabled later in the session
    // finds the language taken by EvoWiki and fails to register until Obsidian restarts (then it loads first).
    this.app.workspace.onLayoutReady(() => this.registerEmbedProcessor());
    // 这部分我懂 / 再讲深一点 / 换个讲法 / 钉住: a 「⋯」 menu by each section heading of a wiki note (Reading view + Live Preview).
    this.registerMarkdownPostProcessor(owned(sectionMenuPostProcessor(this.checkHub)));
    this.registerEditorExtension(sectionMenuExtension(this.checkHub));
    // 读完了 block before 我的笔记, the top line and the 「按你的反馈改过」 tags: drawn by the plugin, never written to the note.
    // 真题练习 above it (the page's past exam questions; the quiz is the exam asset in an evoui block).
    this.registerMarkdownPostProcessor(owned(endBlockPostProcessor(this.checkHub, this.examPort())));
    this.registerEditorExtension(endBlockExtension(this.checkHub, this.examPort()));
    // A materials draft shown in the page as track changes (display only: the note is written after 确认替换).
    // review mode renders a changed paragraph as the note renders it (marks on the rendered text)
    const renderMd = (md: string, into: HTMLElement, owner: Component): Promise<void> => MarkdownRenderer.render(this.app, md, into, "", owner);
    this.registerMarkdownPostProcessor(owned(reviseReadingPostProcessor(this.checkHub, renderMd)));
    this.registerEditorExtension(reviseEditorExtension(this.checkHub, renderMd));
    // Page links in a wiki note (`[text](#p:<id>)`, app.js a.inl): a click opens that page (or a notice says why not).
    // Only in the current user's notes: a link in another user's note names that user's page.
    const openPageLink = (pageId: string, notePath: string | null): void => {
      if (notePath === null || !inFolder(notePath, wikiNotesFolder(this.data.settings))) { ewNotice(WIKI_COPY.otherUsersNote); return; }
      void this.wikiController().openPageLink(pageId);
    };
    this.registerMarkdownPostProcessor(noteLinksPostProcessor(openPageLink));
    this.registerEditorExtension(noteLinksExtension(openPageLink));
    this.addSettingTab(new EvoWikiSettingTab(this.app, this));
    this.registerMarkdownPostProcessor((element) => {
      for (const hint of element.querySelectorAll<HTMLElement>(".evowiki-mine-hint")) localizedText(hint, () => tr("note.mineHint"));
    });
    // The command palette = app.js COMMANDS, in its order. Ids kept where they existed, so hotkeys survive.
    for (const c of this.wikiCommands()) this.addCommand(c);
    this.registerEvent(this.app.workspace.on("file-open", (f) => {
      this.followWikiNote(f); this.watchNoteEdits(f); this.noteActivation.fileOpened(f?.path ?? null, f?.extension ?? null);
    }));
    // A switch to another note's tab: the wiki follows it too. Focus coming back to the same note (from the sidebar or any
    // other view) is not a change, and followWikiNote ignores it.
    this.registerEvent(this.app.workspace.on("active-leaf-change", (leaf) => {
      if (leaf?.view instanceof MarkdownView) {
        this.followWikiNote(leaf.view.file); this.watchNoteEdits(leaf.view.file); this.noteActivation.markdownLeafActive(leaf, leaf.view.file?.path ?? null);
      } else if (leaf?.getRoot?.() === this.app.workspace.rootSplit) {
        this.watchNoteEdits(null);     // another tab in the main area (学点新的): he left the note; a side panel is not leaving
      }
    }));
    // The note being edited moved (a page's note goes to its goal's folder as its write ends): the watch follows it, or leaving
    // it looks for the old path, finds no note there and sends nothing.
    this.registerEvent(this.app.vault.on("rename", (f, old) => {
      if (this.editWatch?.path === old) this.editWatch = { ...this.editWatch, path: f.path };
    }));
    // A note opened as it is created (the first section of a page) has no frontmatter in the cache yet: it is watched once it has.
    this.registerEvent(this.app.metadataCache.on("changed", (f) => {
      if (this.editWatch === null && f === this.app.workspace.getActiveViewOfType(MarkdownView)?.file) this.watchNoteEdits(f);
    }));
    // The note page header (title + 目标 / 更新于) in place of Obsidian's inline title and properties (T10).
    const headers = (): void => this.refreshNoteHeaders();
    this.registerEvent(this.app.workspace.on("layout-change", headers));
    this.registerEvent(this.app.workspace.on("file-open", headers));
    this.registerEvent(this.app.workspace.on("active-leaf-change", headers));
    this.registerEvent(this.app.metadataCache.on("changed", headers));
    this.app.workspace.onLayoutReady(headers);
    // the note restored on start opens no file-open: its hand edits are watched from here
    this.app.workspace.onLayoutReady(() => { this.watchNoteEdits(this.app.workspace.getActiveFile()); });
    // The Bot pull: every 5 minutes and when the window regains focus (opening 今天 refreshes in setTab).
    this.registerInterval(window.setInterval(() => this.botTick(), BOT_POLL_MS));
    // B5 reading time: active reading of a wiki note is counted every few seconds and sent in batches (ReadingReporter).
    this.registerInterval(window.setInterval(() => this.reading.tick(), READING_TICK_MS));
    for (const ev of ["keydown", "mousedown", "mousemove", "wheel", "touchstart"] as const) {
      this.registerDomEvent(document, ev, () => this.reading.activity(), { passive: true, capture: true });
    }
    this.registerDomEvent(window, "blur", () => { void this.reading.flush(); });
    this.registerEvent(this.app.workspace.on("active-leaf-change", () => { void this.reading.flush(); }));
    // Opening a goal shows its conversation (the last note stays the active file, so the panel alone would show that note's); a note follows again.
    let onLearn = false;
    this.registerEvent(this.app.workspace.on("active-leaf-change", (leaf) => {
      const v = leaf?.view;
      if (v instanceof LearnView) {
        onLearn = true;
        const id = v.shownRunId;
        if (id !== null) this.assistant.showGoalChat(id);
      } else if (v instanceof MarkdownView && onLearn) {
        onLearn = false;
        this.assistant.followNotes();
      }
    }));
    this.registerDomEvent(window, "focus", () => {
      if (this.wiki && !this.wikiDemo) void this.wiki.refreshAccountLanguage().catch(() => undefined);
      if (this.conn.state === "offline") void this.reconnect().catch(() => undefined);
      const now = Date.now();
      if (now - this.botFocusAt < BOT_FOCUS_MIN_MS) return;
      this.botFocusAt = now;
      this.markSeen();
      this.botTick();
      void this.syncProfile();
    });
    this.app.workspace.onLayoutReady(() => {
      detachRemovedViews(this.app.workspace);   // a saved layout's retired sidebar / graph leaf: no empty panel
      // A new user is registered first: the sidebar and 我.md read as that user.
      void this.account.init().finally(() => {
        void this.folderReady?.then(() => this.syncProfile());
        this.markSeen();
        // After this load returns: a re-enable / update runs onload before Obsidian restores the sidebar leaf it emptied on
        // unload, so a check now would not see it and a second EvoWiki pane would be added on every plugin reload.
        window.setTimeout(() => {
          void this.openWikiOnStart().catch((e: unknown) => { console.warn("EvoWiki: could not open the sidebar on start", e); });
        }, 0);
      });
    });
  }

  /** When the window-focus Bot refresh last ran (throttle). */
  private botFocusAt = 0;
  /** B5 reading time of wiki notes (sent only for the real wiki, online and configured). */
  private readonly reading = new ReadingReporter({
    now: () => Date.now(),
    currentPage: () => this.activeWikiPageId(),
    attentive: () => document.visibilityState === "visible" && document.hasFocus(),
    canSend: () => this.wiki !== null && !this.wikiDemo && this.wiki.canReportReading(),
    send: (id, entries) => (this.wiki === null ? Promise.reject(new Error("no wiki")) : this.wiki.reportReading(id, entries)),
    newBatchId: () => `rd-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`,
  });
  /** A forced 今天 re-read (it carries the Bot refresh) once a sidebar controller exists; it single-flights and skips mid-review. */
  /**
   * A new conversation's last one is wrapped up in the background (its summary, its memory, a preference waiting to be
   * confirmed): 个性化's row and 「我」 are read again once that has had time to end. Only reads; the next turn starts over.
   */
  private afterWrapup(): void {
    for (const t of this.wrapupReads.splice(0)) window.clearTimeout(t);
    this.wrapupReads = WRAPUP_READ_MS.map((ms) => window.setTimeout(() => {
      void this.wiki?.waitingChanged();
      void this.syncProfile();
    }, ms));
  }

  /** 「考考我这页」's unfinished round, per server and user. */
  private pageRoundKey(): string { return `evowiki.pageRound:${this.data.settings.serverUrl}:${this.data.settings.userId}`; }

  private markSeen(): void {
    if (this.fixture !== null) return;
    void this.client.seen().catch((e: unknown) => { console.warn("EvoWiki: could not report the window in front", e); });
  }

  private botTick(): void {
    if (this.wiki === null || this.wikiDemo) return;
    void this.wiki.refreshToday(true).catch((e: unknown) => { console.warn("EvoWiki: Bot refresh failed", e); });
  }

  /**
   * app.js boot: on start the right sidebar shows the EvoWiki view on 今天, without taking the focus from the note. The
   * note active now counts as already shown (switching to it later is no note change: 助手 waits for another note).
   */
  private async openWikiOnStart(): Promise<void> {
    const ws = this.app.workspace;
    const active = ws.getActiveViewOfType?.(MarkdownView) ?? null;
    this.noteActivation.ready(active?.leaf ?? null, active?.file?.path ?? null);
    const existing = ws.getLeavesOfType(WIKI_VIEW_TYPE)[0];
    const leaf = existing ?? ws.getRightLeaf(false);
    if (!leaf) return;
    if (!existing) await leaf.setViewState({ type: WIKI_VIEW_TYPE, active: false });
    this.wikiController().setTab("today");
    void this.wikiController().refreshToday();   // 待办事项 reads the Bot list too; init does not
    await ws.revealLeaf(leaf);
    // revealLeaf may make the sidebar leaf active: the focus goes back to the note the user was in.
    if (active !== null && ws.activeLeaf !== active.leaf) ws.setActiveLeaf(active.leaf, { focus: true });
    // The note restored with the layout opened before the plugin listened: the wiki follows it now (no file-open comes).
    if (active?.file) this.followWikiNote(active.file);
  }

  override onunload(): void {
    for (const t of this.wrapupReads.splice(0)) window.clearTimeout(t);
    // The session is NOT ended here (R4): it stays persisted and the next load resumes it.
    if (this.notStarted) return;
    void this.reading.flush();          // the last batch, best effort (unsent seconds are lost on unload)
    this.budgetSetup?.abort();
    this.account?.dispose(); this.conn.stop();
    this.checkHub.set(null);
    for (const [v, stop] of this.headerWatch) { stop(); applyHeader(v.containerEl, null); }
    this.headerWatch.clear();
    for (const stop of this.barWatch.values()) stop();
    this.barWatch.clear();
    for (const t of this.leaveTimers.values()) clearTimeout(t);
    this.leaveTimers.clear();
    this.wiki?.dispose();
    this.treeMarker.dispose();
    this.assistant?.dispose();
  }

  /** Serialized (see SettingsUpdater): overlapping commits never lose a change. */
  updateSettings(patch: Partial<EvoWikiSettings>): Promise<void> { return this.settingsUpdater.update(patch); }

  /**
   * The palette = app.js COMMANDS, in its order (Obsidian shows 「EvoWiki: <name>」). 恢复成完整页 is not a command: a
   * collapsed page offers it in the note itself (the end block's top line).
   */
  private wikiCommands(): Command[] {
    return [
      { id: "initial-setup", name: setupText("title"), callback: () => { void this.openInitialSetup().catch((error: unknown) => ewNotice(error instanceof Error ? error.message : setupText("failed"))); } },
      { id: "open-starmap", name: COMMAND_COPY.starMap, callback: () => void this.openStarMap() },
      { id: "open-engine", name: COMMAND_COPY.engine, callback: () => void this.openEngine("tab") },
      { id: "open-review", name: COMMAND_COPY.review, callback: () => void this.openReview() },
      { id: "new-goal", name: COMMAND_COPY.newGoal, callback: () => { void this.openLearn("", true); } },
      { id: "open-assistant", name: ASSISTANT_COPY.commandOpen, callback: () => void this.openAssistant() },
      { id: "recent-changes", name: COMMAND_COPY.recentChanges, callback: () => void this.openEngine("log") },
      { id: "open-settings", name: COMMAND_COPY.settings, callback: () => void this.openSettings() },
    ];
  }

  /**
   * 个性化 / 活动记录 (app.js COMMANDS): the sidebar on its 个性化 tab (keeping whichever wiki is open); "log" expands
   * 活动记录 and scrolls to it once the tab has drawn; "manage" opens 管理偏好 (a rule's result in the chat).
   */
  private async openEngine(what: "tab" | "log" | "manage"): Promise<void> {
    await this.openWiki(this.wiki !== null && this.wikiDemo);
    const c = this.wikiController();
    if (what === "manage") { c.openManagePrefs(); return; }
    c.personalView(what === "log" ? { manage: false, log: true } : { manage: false });
    c.setTab("engine");
    if (what === "tab") return;
    const scroll = (): void => { this.wikiLeaf()?.view.containerEl.querySelector<HTMLElement>("#pz-log")?.scrollIntoView({ block: "start" }); };
    if (typeof window !== "undefined" && typeof window.requestAnimationFrame === "function") window.requestAnimationFrame(scroll);
    else scroll();
  }

  /** 打开设置: the main-area 设置 tab (app.js openTab("set")); an open one is focused instead of opening a second. */
  async openSettings(): Promise<void> {
    const existing = this.app.workspace.getLeavesOfType(SETTINGS_VIEW_TYPE)[0];
    if (existing) { await this.app.workspace.revealLeaf(existing); return; }
    const leaf = this.app.workspace.getLeaf("tab");
    await leaf.setViewState({ type: SETTINGS_VIEW_TYPE, active: true });
    await this.app.workspace.revealLeaf(leaf);
  }

  /** The page id of the active note when it is an EvoWiki page note (in the wiki notes folder, with evowiki_page_id), else null. */
  private activeWikiPageId(): string | null {
    const f = this.app.workspace.getActiveFile();
    if (!f || f.extension !== "md") return null;
    const raw: unknown = (this.frontmatter(f) as Record<string, unknown> | undefined)?.evowiki_page_id;
    const note = { path: f.path, pageId: typeof raw === "string" ? raw : null };
    return isWikiNote(note, wikiNotesFolder(this.data.settings)) ? note.pageId : null;
  }

  private makeSettingsUpdater(): SettingsUpdater {
    return new SettingsUpdater({
      get: () => this.data.settings,
      save: async (next) => { this.data.settings = next; await this.saveData(this.data); },
      reinit: async () => {
        this.budgetSetup?.abort();
        // First, before any await: nothing loaded or in flight for the old identity may be shown or sent again.
        this.resetWiki();
        this.closeOtherUsersNotes();
        await this.account.init(); // GET /me again (registering an unknown user), for the new identity
        // 设置 (the view and the settings tab) draws its server-backed sections again, on the new identity's wiki.
        for (const fn of [...this.identityListeners]) fn();
        void this.syncProfile(); // the new user's 我.md, in their folder
        void this.openWikiOnStart().catch((e: unknown) => { console.warn("EvoWiki: could not open the sidebar", e); });  // resetWiki closed it
        this.assistant.identityChanged(); // the same note, re-shown under the new server/user (Ruling F1)
      },
    });
  }

  /**
   * 重新连接 / 重试 (the wiki sidebar and the 知识图谱 page): probe the server now; an account check that failed (a failed
   * registration included) runs once more. A plain offline → online change re-runs it by itself (AccountCheck).
   */
  private async reconnect(): Promise<void> {
    await this.conn.retryNow();
    if (this.account.state.kind === "error") await this.account.init();
  }

  /**
   * The account check reads the settings per call (the client's X-EvoWiki-User), so a new identity needs only init().
   * A registered user gets Obsidian's UI language; a failed registration is said in a notice (fail closed, never silent).
   */
  private makeAccount(): AccountCheck {
    return new AccountCheck({
      me: async () => { if (this.fixture === null) await this.client.checkServer(); return this.client.me(); },
      createUser: (body) => this.client.createUser(body), conn: this.conn, settings: () => this.data.settings,
      uiLocale: () => obsidianLocale(),
      registerFailed: (text) => { ewNotice(text); },
      registered: () => { void this.wiki?.retryConnection(); },
      prepareUser: async () => {
        if (this.fixture !== null) return;
        this.budgetSetup?.abort();
        this.budgetSetup = new AbortController();
        const config = { baseUrl: this.data.settings.serverUrl, userId: this.data.settings.userId };
        await prepareInitialSetup(this.app, this.setupDeps(config), this.budgetSetup.signal);
      },
    });
  }

  private setupDeps(config = { baseUrl: this.data.settings.serverUrl, userId: this.data.settings.userId }): SetupDeps {
    const identity = JSON.stringify([config.baseUrl, config.userId]);
    return {
      api: new HttpWikiClient(() => config, obsidianWikiHttp, nodeStreamTransport),
      setup: new SetupService(() => config, obsidianWikiHttp),
      usage: new UsageClient(() => config, obsidianWikiHttp),
      assistant: new AssistantClient(() => config, obsidianWikiHttp, nodeStreamTransport),
      profile: new ProfileClient(() => config, obsidianWikiHttp),
      native: this.data.localSetup ? new LocalSetupClient(this.data.localSetup) : null,
      maintenance: this.maintenanceClient(), settings: () => this.data.settings,
      updateSettings: (patch) => this.updateSettings(patch),
      bookmark: () => Number(this.data.setupSteps?.[identity] ?? 0),
      saveBookmark: async (step) => { (this.data.setupSteps ??= {})[identity] = String(step); await this.saveData(this.data); },
      openBackground: () => this.openProfileNote(),
      finished: async () => { await this.profile?.sync(); await this.openWiki(false); },
    };
  }

  async openInitialSetup(): Promise<void> {
    if (this.fixture !== null) return;
    this.budgetSetup?.abort(); this.budgetSetup = new AbortController();
    await prepareInitialSetup(this.app, this.setupDeps(), this.budgetSetup.signal, true);
  }

  /**
   * Opens a vault path in the current leaf only if it resolves to an existing file, and says whether one was opened (the
   * wiki shows a notice when none was). Never workspace.openLinkText, which CREATES the target when it does not resolve.
   * `at` (the wiki's 本页 小目录): scroll to the line it finds in the note's text, reusing a tab that already shows the note.
   */
  private openIfExists(p: string, at?: NoteLocator, frame?: boolean): boolean {
    const f = this.app.vault.getAbstractFileByPath(p);
    if (!(f instanceof TFile)) return false;
    const failed = (e: unknown): void => { console.warn("EvoWiki: could not open the note", f.path, e); ewNotice(WIKI_COPY.noteOpenFailed(f.basename)); };
    if (at !== undefined) { this.revealNoteAt(f, at, frame).catch(failed); return true; }
    // The note's own tab when it has one, brought to the front. Not getActiveFile(): it is the last note shown even while
    // 学点新的 / 知识图谱 is the tab in front, so 继续学习 on a note opened before did nothing (user report 2026-10-28).
    const open = this.app.workspace.getLeavesOfType("markdown").find((l) => l.view instanceof MarkdownView && l.view.file?.path === f.path);
    if (open !== undefined) {
      if (this.app.workspace.getActiveViewOfType(MarkdownView)?.leaf !== open) this.app.workspace.setActiveLeaf(open, { focus: true });
      Promise.resolve(this.app.workspace.revealLeaf(open)).catch(failed);
      return true;
    }
    this.app.workspace.getLeaf(false).openFile(f, { active: true }).catch(failed);
    return true;
  }

  /**
   * Shows `f` at the line `at` finds (the note's open tab if any, else the current leaf); no line → just shows it. `frame`:
   * once it has scrolled, the section starting at that line gets the dashed frame (note-frame.ts).
   */
  private async revealNoteAt(f: TFile, at: NoteLocator, frame: boolean = false): Promise<void> {
    let line: number | null = null, text = "";
    try { text = await this.app.vault.cachedRead(f); line = at(text); } catch { line = null; }
    const eState = line === null ? undefined : { line };
    const open = this.app.workspace.getLeavesOfType("markdown")
      .find((l) => l.view instanceof MarkdownView && l.view.file?.path === f.path);
    let view: MarkdownView | null = null;
    if (open !== undefined && open.view instanceof MarkdownView) {
      this.app.workspace.setActiveLeaf(open, { focus: true });
      if (eState !== undefined) open.view.setEphemeralState(eState);
      view = open.view;
    } else {
      const leaf = this.app.workspace.getLeaf(false);
      await leaf.openFile(f, { active: true, eState });
      view = leaf.view instanceof MarkdownView ? leaf.view : null;
    }
    if (!frame || line === null || view === null) return;
    const v = view, from = line;
    // after the scroll has landed (and the reading view has drawn the blocks there)
    window.setTimeout(() => { try { frameSection(v, text, from); } catch (e) { console.warn("EvoWiki: could not frame the section", e); } }, 150);
  }

  /** The embedded-html code block processor; quiet (one console.info) when another plugin already has the language. */
  private registerEmbedProcessor(): void {
    try {
      this.registerMarkdownCodeBlockProcessor(EMBED_FENCE, (source, el, ctx) => {
        const child = new MarkdownRenderChild(el);
        child.register(mountEmbedBlock(el, source, ctx.sourcePath, this.embedPort(), this.embedHub));
        ctx.addChild(child);
      });
    } catch {
      console.info("EvoWiki: embedded-html blocks are drawn by another plugin");
    }
    try {
      this.registerMarkdownCodeBlockProcessor(EVOUI_FENCE, (source, el, ctx) => {
        const child = new MarkdownRenderChild(el);
        child.register(mountEvouiBlock(el, source, this.evouiPort(), this.embedHub));
        ctx.addChild(child);
      });
    } catch {
      console.info("EvoWiki: evoui blocks are drawn by another plugin");
    }
    try {
      this.registerMarkdownCodeBlockProcessor(IMAGE_FENCE, (source, el, ctx) => {
        const child = new MarkdownRenderChild(el);
        child.register(mountImageBlock(el, source, this.imagePort(), this.embedHub));
        ctx.addChild(child);
      });
    } catch {
      console.info("EvoWiki: evowiki-image blocks are drawn by another plugin");
    }
  }

  /** What an evowiki-image block reads and calls (embed/image-block.ts): the vault's `assets/<id>.webp` and 取回. */
  private imagePort(): ImagePort {
    return {
      resource: (path) => {
        const p = normalizePath(path);
        const exact = this.app.vault.getAbstractFileByPath(p);
        const f = exact instanceof TFile ? exact : this.fileIgnoringCase(p);
        return f instanceof TFile ? this.app.vault.getResourcePath(f) : null;
      },
      canFetch: () => this.canFetchAssets(),
      assetPath: (id) => this.assets.pathFor(id, "image"),
      retrieve: async (id) => { const r = await this.assets.retrieve(id, "image"); return r === "written" || r === "present"; },
    };
  }

  /** The vault file at `path`, found ignoring case like the fetcher's existence check; null when there is none. */
  private async readVaultText(path: string): Promise<string | null> {
    const p = normalizePath(path);
    const exact = this.app.vault.getAbstractFileByPath(p);
    const f = exact instanceof TFile ? exact : this.fileIgnoringCase(p);
    return f instanceof TFile ? this.app.vault.cachedRead(f) : null;
  }

  /** 取回 is offered for the real wiki: online and configured, or on the demo fixture (its fake client), never the demo. */
  /** Whether the server stored this content. The demo fixture's content ships with the plugin; no server to ask = not known. */
  private isGenerated(text: string, kind: "evoui" | "html"): Promise<boolean> {
    if (this.wikiDemo) return Promise.resolve(true);
    return this.canFetchAssets() ? this.assets.isGenerated(text, kind) : Promise.resolve(false);
  }

  private canFetchAssets(): boolean {
    if (this.wikiDemo) return false;
    if (this.fixture !== null) return true;
    const s = this.data.settings;
    return s.serverUrl !== "" && s.userId !== "" && this.conn.state !== "offline";
  }

  /** The demo fixture's usage client (fixed numbers) for the settings tab's 用量; null = the server's, as usual. */
  fixtureUsageClient(): UsageClient | null {
    return this.fixture?.usage ?? null;
  }

  /** What the 真题练习 block calls (exam/exam-block.ts): the real wiki controller, and its quiz asset fetched, then drawn. */
  private examPort(): ExamPort {
    return {
      hub: this.checkHub,
      ui: this.examUi,
      mountQuiz: (el, assetId) => mountQuizAsset(el, assetId,
        (id) => (this.canFetchAssets() ? this.assets.retrieve(id, "evoui") : Promise.resolve()),
        (host, source) => mountEvouiBlock(host, source, this.evouiPort(), this.embedHub)),
    };
  }

  /** What an evoui block reads and calls (evoui/evoui-block.ts): cached `assets/<id>.json`, 取回, and its result events. */
  private evouiPort(): EvouiPort {
    const { serverUrl, userId } = this.data.settings;
    const current = (): boolean => this.data.settings.serverUrl === serverUrl && this.data.settings.userId === userId;
    return {
      read: async (path) => {
        if (!current()) return null;
        const text = await this.readVaultText(path);
        return current() ? text : null;
      },
      canFetch: () => current() && this.canFetchAssets(),
      isGenerated: async (text) => {
        if (!current()) return false;
        const generated = await this.isGenerated(text, "evoui");
        return current() && generated;
      },
      image: async (id) => {
        if (!current()) return null;
        const bytes = await this.imageBytes(id);
        return current() ? bytes : null;
      },
      assetPath: (id) => current() ? this.assets.pathFor(id, "evoui") : null,
      retrieve: async (id) => {
        if (!current()) return false;
        const result = await this.assets.retrieve(id, "evoui");
        return current() && (result === "written" || result === "present");
      },
      // Keep the controller lookup dynamic for reconstruction within this identity, but reject a previous account's frame.
      onEvent: (assetId, ev) => { if (current() && !this.wikiDemo) this.wikiController().blockEvent(assetId, ev); },
      progress: async (assetId) => {
        if (!current() || this.wikiDemo) return null;
        const progress = await this.wikiController().blockProgress(assetId);
        return current() ? progress : null;
      },
      onProgress: (assetId, progress) => { if (current() && !this.wikiDemo) this.wikiController().saveBlockProgress(assetId, progress); },
    };
  }

  /** The vault's `assets/<id>.webp`, fetched first when it is not there yet; null when it cannot be had. */
  private async imageBytes(id: string): Promise<ArrayBuffer | null> {
    const path = this.assets.pathFor(id, "image");
    if (path === null) return null;
    const find = (): TFile | null => {
      const f = this.app.vault.getAbstractFileByPath(normalizePath(path));
      return f instanceof TFile ? f : this.fileIgnoringCase(normalizePath(path));
    };
    if (find() === null && this.canFetchAssets()) await this.assets.retrieve(id, "image");
    const file = find();
    return file === null ? null : this.app.vault.readBinary(file);
  }

  /** The vault file at `path` matched ignoring case, walking only the folders on the path; null when there is none. */
  private fileIgnoringCase(path: string): TFile | null {
    type Node = { name?: string; children?: Node[] };
    let node: Node | undefined = this.app.vault.getRoot();
    for (const seg of path.toLowerCase().split("/")) {
      node = node?.children?.find((c) => c.name?.toLowerCase() === seg);
      if (node === undefined) return null;
    }
    return node instanceof TFile ? node : null;
  }

  /** What an embedded-html block reads and calls (embed/embed-block.ts); the 取回 button only for the real, online wiki. */
  private embedPort(): EmbedPort {
    return {
      // Found ignoring case, like the fetcher's existence check (ObsidianNoteVault.occupied): a file it counts as
      // present is the file drawn here. The exact path first; else one folder level at a time (no whole-vault scan).
      read: (path) => this.readVaultText(path),
      canFetch: () => this.canFetchAssets(),
      isGenerated: (text) => this.isGenerated(text, "html"),
      assetPath: (id) => this.assets.pathFor(id),
      retrieve: async (id) => { const r = await this.assets.retrieve(id); return r === "written" || r === "present"; },
      onEvent: (assetId, ev) => { if (!this.wikiDemo) this.wikiController().blockEvent(assetId, ev); },
      // Read at the note's `.evowiki-page` element (where the EXACT block defines the demo's tokens, --viz-1 and the
      // mastery colours included; Obsidian's own variables are inherited there too). A block not (yet) in the DOM, or in
      // a note that is not a wiki note, reads the body: Obsidian's names only, and the asset keeps its own fallbacks.
      theme: (at) => {
        const doc = at.ownerDocument;
        const page = at.isConnected ? at.closest<HTMLElement>(`.${PAGE_CSS_CLASS}`) : null;
        const cs = getComputedStyle(page ?? doc.body);
        return { read: (name) => cs.getPropertyValue(name), dark: doc.body.classList.contains("theme-dark") };
      },
    };
  }

  /**
   * The wiki controller, built on first use. A new server or user id drops it (resetWiki, from the settings reinit), so
   * the next use builds a fresh one: nothing loaded for the old identity is shown or written for the new one.
   */
  private wikiController(): WikiController {
    if (this.wiki === null) {
      const c = makeWikiController({
        app: this.app, settings: () => this.data.settings, conn: this.conn, http: obsidianWikiHttp, sse: nodeStreamTransport,
        syncWikiNote, folderReady: () => this.folderReady, openExisting: (p, at, frame) => this.openIfExists(p, at, frame),
        openWikiNote: () => this.editWatch?.path ?? null,
        assets: this.assets, treeMarker: this.treeMarker, decisions: this.draftDecisions,
        pageRoundMemo: {
          load: () => { const v = window.localStorage.getItem(this.pageRoundKey()); return v === null ? null : JSON.parse(v) as PageRoundMemo; },
          save: (m) => { if (m === null) window.localStorage.removeItem(this.pageRoundKey()); else window.localStorage.setItem(this.pageRoundKey(), JSON.stringify(m)); },
        },
        ...(this.fixture ? { fixture: { api: this.fixture.wiki, recommendations: this.fixture.recommendations } } : {}),
        showSidebar: () => { void this.openWiki(this.wikiDemo); },
        showStartPage: (p) => { void this.openStartPage(p); },
        openProfile: () => { void this.openProfileNote(); },
        // 查看 on a 活动记录 row (app.js showChange): the controller has switched the tab; scroll to the row, then the ants frame.
        revealChange: (key) => {
          const ws = this.app.workspace;
          void showChangeOnScreen({
            view: () => { const leaf = this.wikiLeaf(); return leaf ? { leaf, root: leaf.view.containerEl } : null; },
            revealLeaf: (leaf) => ws.revealLeaf(leaf),
            reduce: () => typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches,
            frame: (f) => { if (typeof window.requestAnimationFrame === "function") window.requestAnimationFrame(f); else window.setTimeout(f, 16); },
          }, key);
        },
        learnNew: (topic) => { void this.openLearn(topic, true); },
        continueGoal: (mapId) => this.continueGoal(mapId),
        openPrompt: (key) => { void this.openPromptEditor(key); },
        // 「⋯ → 问助手」 on a section: the same explicit-click path as the editor menu's 问陪读助手.
        // The Bot's 问助手 / chips: the 助手 tab, the text sent as a user turn (the draft is left alone).
        // With a page target the question goes into that page's conversation (assistant/send-to.ts).
        // 个性化 · 待确认的记忆 · 处理: a new chat in the 助手 tab (intent resolve_memory_conflicts)
        resolveMemoryConflicts: () => {
          if (this.assistant.startConflictsChat() !== null) void this.openAssistant().catch((e: unknown) => { console.warn("EvoWiki: could not open the 助手 tab", e); });
        },
        assistantSend: (text, page) => {
          const go = (): void => { void sendToAssistant(this.assistant, text, page, (t) => { ewNotice(BOT_COPY.askLost(t)); }); };
          void this.openAssistant().then(go, (e: unknown) => { console.warn("EvoWiki: could not open the 助手 tab", e); go(); });
        },
        askAssistant: (pageId, title, quote, reteach) => {
          const r = quoteFor(quote.text, quote.heading, quote.kind === "wrong_answer");
          if (!r.ok && r.reason === "blank") return;
          const page = { kind: "page", pageId, title } as const;
          const attach = (): void => {
            if (r.ok && this.wiki?.state.review.active === true && reteach === undefined && this.assistant.quoteInReview(this.quoteWithArtifact(r.quote, pageId))) return;
            if (r.ok) { this.assistant.attachQuote(page, this.quoteWithArtifact(r.quote, pageId), reteach); return; }
            void this.assistant.attachTextFile(page, quote.text).then((ok) => { if (!ok) ewNotice(ASSISTANT_COPY.quoteTooLong); });
          };
          void this.openAssistant().then(attach, (e: unknown) => { console.warn("EvoWiki: could not open the 助手 tab", e); attach(); });
        },
        // 读完了's 内容太简单 / 理解有困难: a new conversation on the page, the tap sent as the learner's message.
        assistantSendFresh: (pageId, title, text) => {
          const page = { kind: "page", pageId, title } as const;
          const go = (): void => { void this.assistant.sendIn(page, text); };
          void this.openAssistant().then(go, (e: unknown) => { console.warn("EvoWiki: could not open the 助手 tab", e); go(); });
        },
      }, this.wikiDemo);
      this.wiki = c;
      // The chat's review card is the same round: it follows when the round moves.
      let round: unknown[] = [];
      c.subscribe(() => {
        const s = c.state, r = s.review;
        const now = [r.today, r.results, r.revealed, r.seen, r.removed, r.confirmResults, r.note, r.active, r.finished, r.pageRoundDone, s.followups, s.busy, s.conn, s.configured];
        if (this.wiki === c && now.some((v, i) => v !== round[i])) { round = now; this.assistant.reviewChanged(); }
      });
      // After the vault layout is ready: before that the vault may not be indexed, and no file would be found.
      if (!this.wikiDemo && this.fixture === null) this.app.workspace.onLayoutReady(() => { void this.migrateWikiFiles(c); });
      // A note that became active while a write ran was not followed then: follow it once the write is over.
      if (!this.wikiDemo) c.subscribe(() => { if (this.wiki === c && c.state.busy === null) this.followPending(); });
      // The headers' writing line follows the controller's write (停止 while it streams, 已停止 · 重新写 after).
      if (!this.wikiDemo) {
        c.subscribe(() => {
          const key = JSON.stringify([c.writingPage(), c.state.stopped?.pageId ?? null]);
          if (this.wiki !== c) return;
          if (key === this.headerRunKey) return;
          this.headerRunKey = key;
          this.refreshNoteHeaders();
        });
      }
    }
    this.checkHub.set(this.wikiDemo ? null : this.wiki);
    return this.wiki;
  }

  /**
   * The wiki sidebar leaf on screen: a reloaded plugin can leave more than one wiki leaf in the saved layout, and only the
   * shown one has the rows to scroll to or focus (the first one otherwise).
   */
  private wikiLeaf(): WorkspaceLeaf | undefined {
    const leaves = this.app.workspace.getLeavesOfType(WIKI_VIEW_TYPE);
    return leaves.find((l) => l.view.containerEl.isConnected && l.view.containerEl.offsetParent !== null) ?? leaves[0];
  }

  /** The file tree's 「正在写」 marks (one for the plugin; removed on unload). */
  private readonly treeMarker = new TreeMarker();

  /**
   * Once per vault: notes still named 「X（i-n）」 are moved to their page's title and folder (the reconcile pass of those
   * pages). The flag is set only when every one of those pages was read and moved; otherwise (server unreachable, a move
   * that failed) it stays off and the next load tries again.
   */
  private async migrateWikiFiles(c: WikiController): Promise<void> {
    if (this.data.wikiFilesMigrated === true) return;
    const s = this.data.settings;
    if (s.serverUrl === "" || s.userId === "") return;
    try {
      await this.folderReady;
      const ids = await legacyPageIds(obsidianWikiNoteVault(this.app), wikiNotesFolder(s));
      if (!(await c.reconcileFiles(ids))) { console.warn("EvoWiki: the old wiki files were not all renamed; trying again on the next load"); return; }
      this.data.wikiFilesMigrated = true;
      await this.saveData(this.data);
    } catch (e) {
      console.warn("EvoWiki: could not rename the old wiki files", e);
    }
  }

  /** The open notes that belong to another user (in the notes folder, outside this user's folder) are closed. */
  private closeOtherUsersNotes(): void {
    const s = this.data.settings;
    this.app.workspace.getLeavesOfType("markdown").forEach((l) => {
      const p = (l.view as MarkdownView).file?.path;
      if (p !== undefined && inFolder(p, s.notesFolder) && !inFolder(p, userFolder(s))) l.detach();
    });
  }

  /**
   * Notes of the layout from before per-user folders (Wiki, 资料, 我.md right under the notes folder) move into the current
   * user's folder; what could not move stays where it is, and a notice says so.
   */
  private async moveOldLayout(): Promise<void> {
    const s = this.data.settings;
    if (s.userId === "" || !validateNotesFolder(userFolder(s)).ok) return;
    let left: string[];
    try {
      left = await moveIntoUserFolder(obsidianMoveVault(this.app), s);
    } catch (e) {
      // nothing past this point moved: the old layout stays where it is, and it says so (never an unhandled rejection)
      console.warn("EvoWiki: could not move the old notes into the user's folder", e);
      left = [s.notesFolder];
    }
    if (left.length > 0) ewNotice(WIKI_COPY.oldNotesLeft(left, userFolder(s)));
  }

  /**
   * Identity change (settings reinit): closes the wiki leaf, detaches the note blocks, disposes the controller (real or
   * demo) and forgets the followed note. The next use builds a controller for the new server / user.
   */
  private resetWiki(): void {
    this.app.workspace.getLeavesOfType(WIKI_VIEW_TYPE).forEach((l) => l.detach());
    this.app.workspace.getLeavesOfType(STARMAP_VIEW_TYPE).forEach((l) => l.detach());
    this.app.workspace.getLeavesOfType(PROMPT_VIEW_TYPE).forEach((l) => l.detach());
    this.app.workspace.getLeavesOfType(START_PAGE_VIEW_TYPE).forEach((l) => l.detach());
    this.app.workspace.getLeavesOfType(LEARN_VIEW_TYPE).forEach((l) => l.detach());
    this.checkHub.set(null);
    this.wiki?.dispose();
    this.wiki = null;
    this.wikiDemo = false;
    this.lastActivePath = null;
    this.pendingFollow = null;
    // A leave report still due belongs to the old server / user: dropped, never sent to the new one.
    for (const t of this.leaveTimers.values()) clearTimeout(t);
    this.leaveTimers.clear();
    this.editWatch = null;
  }

  /**
   * A wiki leaf closed. If it was the demo's, demo mode ends: the demo controller is disposed, and the next use (the
   * settings rows, following a note, 打开个人 Wiki) builds the real one. Closing the real wiki's leaf changes nothing.
   */
  private wikiViewClosed(c: WikiController): void {
    if (this.wiki !== c || !this.wikiDemo) return;
    c.dispose();
    this.wiki = null;
    this.wikiDemo = false;
  }

  /** The wiki note left unfollowed (demo open, or a write running), if it is still the latest one: followed now. */
  private followPending(): void {
    const f = this.pendingFollow;
    if (f === null) return;
    this.pendingFollow = null;
    this.followWikiNote(f);
  }

  /**
   * The wiki follows the active EvoWiki note: only when the active file really changed, a note in the wiki notes folder
   * whose evowiki_page_id is not the page the real controller shows loads that page (followPage: same GET + sync as a
   * click in the catalog, but the tab stays and no note is opened). The real controller is built if needed; never in
   * demo mode; the wiki view is not opened. Decision: follow-note.ts.
   * A wiki note is marked as seen (lastActivePath) only when it was followed or needs no follow: one that became active
   * during the demo or while a write ran is followed later (the write ending, or 打开个人 Wiki / closing the demo).
   */
  private followWikiNote(f: TFile | null): void {
    const path = f?.path ?? null;
    this.pendingFollow = null; // a newer note event: an older unfollowed note is no longer the one to follow
    if (path === this.lastActivePath) return;
    if (f === null || f.extension !== "md") { this.lastActivePath = path; return; }
    const raw: unknown = (this.frontmatter(f) as Record<string, unknown> | undefined)?.evowiki_page_id;
    const note = { path: f.path, pageId: typeof raw === "string" ? raw : null };
    const folder = wikiNotesFolder(this.data.settings);
    if (!isWikiNote(note, folder)) { this.lastActivePath = path; return; }
    if (this.wikiDemo) { this.pendingFollow = f; return; }
    const c = this.wikiController();
    if (c.state.busy !== null) { this.pendingFollow = f; return; }
    this.lastActivePath = path;
    const id = pageToFollow(note, folder, c.state);
    if (id !== null) void c.followPage(id);
  }

  /**
   * Hand edit, trigger 2: when the active file moves away from an EvoWiki note (a note in the wiki notes
   * folder with evowiki_page_id), the real controller checks that note for an edit LEAVE_DELAY_MS later (Obsidian saves
   * the editor shortly after typing stops). Coming back to it before then cancels that. Focus moving to the sidebar is
   * no file change, so no leave. Never in demo mode; the report itself is gated and deduped by the controller.
   */
  private watchNoteEdits(f: TFile | null): void {
    const path = f?.path ?? null;
    const prev = this.editWatch;
    if (prev !== null && prev.path === path) return;
    if (path !== null) {
      const due = this.leaveTimers.get(path);
      if (due !== undefined) { clearTimeout(due); this.leaveTimers.delete(path); }
    }
    this.editWatch = null;
    if (f !== null && f.extension === "md") {
      const raw: unknown = (this.frontmatter(f) as Record<string, unknown> | undefined)?.evowiki_page_id;
      const note = { path: f.path, pageId: typeof raw === "string" ? raw : null };
      if (isWikiNote(note, wikiNotesFolder(this.data.settings))) this.editWatch = { path: note.path, pageId: note.pageId };
    }
    if (prev === null) return;
    const old = this.leaveTimers.get(prev.path);
    if (old !== undefined) clearTimeout(old);
    this.leaveTimers.set(prev.path, setTimeout(() => {
      this.leaveTimers.delete(prev.path);
      if (!this.wikiDemo) void this.wikiController().noteLeft(prev.path, prev.pageId);
    }, LEAVE_DELAY_MS));
  }

  /** Opens the wiki in the right sidebar. Switching between real and demo closes the open wiki leaf first (only one). */
  private async openWiki(demo: boolean): Promise<void> {
    if (this.wiki === null || demo !== this.wikiDemo) {
      this.app.workspace.getLeavesOfType(WIKI_VIEW_TYPE).forEach((l) => l.detach());
      this.checkHub.set(null);
      this.wiki?.dispose();
      this.wiki = null;
      this.wikiDemo = demo;
    }
    const existing = this.app.workspace.getLeavesOfType(WIKI_VIEW_TYPE)[0];
    const leaf = existing ?? this.app.workspace.getRightLeaf(false);
    if (!leaf) return;
    if (!existing) await leaf.setViewState({ type: WIKI_VIEW_TYPE, active: true });
    await this.app.workspace.revealLeaf(leaf);
    // Coming back to a long-lived controller: 今天 re-reads its review list and paths (init skips what is loaded).
    if (existing) void this.wikiController().refreshToday();
    // Back on the real wiki: the note in the editor (not followed while the demo was open) is followed now.
    if (!this.wikiDemo) this.followPending();
  }

  /** A concept's name for the assistant: the wiki's names; never the id. */
  private conceptNameZh(id: string): string {
    return this.wiki?.names.concept(id) || WIKI_COPY.unnamedConcept;
  }

  /**
   * 打开陪读助手 / 问陪读助手: opens (or reveals) the wiki sidebar, keeping whichever wiki (real or demo) is open, and
   * switches it to the 助手 tab. The old standalone assistant view is never opened.
   */
  private async openAssistant(): Promise<void> {
    await this.openWiki(this.wiki !== null && this.wikiDemo);
    this.wikiController().setTab("assistant");
  }

  /** The 知识图谱 page's graph, resolved per call: a reset or the demo switch builds a new controller. */
  private graphSource(): GraphSource {
    const c = (): GraphSource => this.wikiController().graphSource();
    const src = {
      isOffline: () => c().isOffline(),
      load: () => c().load(),
      split: (id: string) => c().split(id),
      want: (id: string, mapId: string) => c().want(id, mapId),
      editPath: (pathId: string, edit: PathEditIn) => c().editPath(pathId, edit),
      splitRow: (pathId: string, key: string) => c().splitRow(pathId, key),
      undoSplitRow: (pathId: string, key: string, changeId: string) => c().undoSplitRow(pathId, key, changeId),
      kChanged: (fn: () => void) => c().kChanged?.(fn) ?? (() => undefined),
      // The shared background writes watcher (real wiki only): the rows' live status.
      writes: {
        snapshot: () => c().writes?.snapshot() ?? null,
        subscribe: (fn: () => void) => c().writes?.subscribe(fn) ?? (() => undefined),
        lookup: (ids: string[]) => c().writes?.lookup(ids) ?? Promise.resolve([]),
      },
    };
    return Object.defineProperty(src, "demo", { get: () => this.wikiDemo, enumerable: true }) as GraphSource;
  }

  /** Obsidian's `css-change` (the theme switched); returns the unsubscribe. */
  private onCssChange(fn: () => void): () => void {
    const ref = this.app.workspace.on("css-change", fn);
    return () => { this.app.workspace.offref(ref); };
  }

  /**
   * 学点新的's goal API: the big-goal routes on the server in the settings (read per request). With plugin data
   * `fixture: "demo"` the scripted fake (goal-fake.ts) answers only the screenshot goal 「我要学机器学习」; any other goal
   * still goes to the server (FixtureGoalApi).
   */
  /**
   * 生成第一页 (学点新的, and a goal confirmed in the chat): the wiki controller writes the first page and opens it. `onPage`:
   * the page's id once known, before its note opens.
   */
  private async writeFirstPage(t: { pathId: string | null; pageId: string | null; title: string; goal: string; key?: string },
    onPage?: (pageId: string) => void): Promise<void> {
    const c = this.wikiController();
    // the note opens with its first section written, and the rest fills in front of him: the text is there long before the visuals
    if (t.pathId !== null) await c.learnFirstPage(t.pathId, onPage, true, t.key);
    else if (t.pageId !== null) await c.learnWritePage(t.pageId, t.title, t.goal, onPage, true);
  }

  private goalApi(): GoalApi {
    const http = new HttpGoalApi(() => ({ baseUrl: this.data.settings.serverUrl, userId: this.data.settings.userId }), obsidianWikiHttp);
    return this.fixture !== null ? new FixtureGoalApi(new FakeGoalApi(), http) : http;
  }

  /**
   * 学点新的 (开始 / a recommendation / the command): opens the tab, or reveals the open one, with `topic` prefilled.
   * A different topic starts a fresh flow; an empty or the same topic keeps the running one.
   */
  /** Goal-card details reopen the persistent learning path, including after the in-memory run is gone. */
  private async openGoalResult(actionId: string, mapId: string | null): Promise<void> {
    const run = this.assistant.goalRun(actionId);
    if (run === undefined) { if (mapId !== null) await this.openGoal(mapId); return; }
    await this.openLearn("");
    const view = this.app.workspace.getLeavesOfType(LEARN_VIEW_TYPE)[0]?.view;
    if (view instanceof LearnView) view.showRun(run);
  }

  /** An existing goal's persistent path, from Today or a saved goal card. */
  async openGoal(mapId: string): Promise<void> {
    await this.openLearn("");
    const view = this.app.workspace.getLeavesOfType(LEARN_VIEW_TYPE)[0]?.view;
    if (view instanceof LearnView) await view.showGoal(mapId);
  }

  /** `fresh`: nothing typed means a new goal (新建目标), not the goal showing. */
  async openLearn(topic: string, fresh = false): Promise<void> {
    const ws = this.app.workspace;
    let leaf = ws.getLeavesOfType(LEARN_VIEW_TYPE)[0];
    if (!leaf) {
      leaf = ws.getLeaf("tab");
      await leaf.setViewState({ type: LEARN_VIEW_TYPE, active: true });
    }
    await ws.revealLeaf(leaf);
    // A deferred tab (restored from the layout, never shown) has no LearnView yet: load it, or the topic would be lost and
    // the view would open on its own default.
    await leaf.loadIfDeferred?.();
    ws.setActiveLeaf(leaf, { focus: true });      // revealLeaf only brings it forward: leaving it for a note must be a leaf change
    const view = leaf.view;
    if (!(view instanceof LearnView)) return;
    if (fresh && topic.trim() === "") view.newGoal();
    else view.openTopic(topic);
  }

  /** A goal's next block: the chat's run of that map goes on (the 助手 tab shows it). */
  private continueGoal(mapId: string, region: string | null = null): boolean {
    const ok = this.assistant.continueGoal(mapId, region);
    if (ok) {
      void this.openAssistant().catch((e: unknown) => { console.warn("EvoWiki: could not open the 助手 tab", e); });
      // what the block opens (its parts to pick from, its pages) is in 学点新的: it shows this goal, whichever it showed
      void this.openLearn("").then(() => {
        const view = this.app.workspace.getLeavesOfType(LEARN_VIEW_TYPE)[0]?.view, run = this.assistant.currentGoal();
        if (view instanceof LearnView && run !== null) view.showRun(run);
      }).catch((e: unknown) => { console.warn("EvoWiki: could not open 学点新的", e); });
    } else void this.openStarMap(mapId);
    return ok;
  }

  /** What 今天's goal rows need: the graph, opening 知识图谱 on a goal, opening a page (a plain GET, never a write), the connection. */
  private goalProgressHost(): GoalProgressHost {
    return {
      progress: () => { const p = this.wiki?.state.progress; return p?.kind === "ready" ? p.value : null; },
      followStep: (step) => { void this.wikiController().followStep(step); },
      source: () => this.wikiController().graphSource(),
      openGoal: (id) => { void this.openGoal(id); },
      configured: () => this.wikiController().state.configured,
      offline: () => !this.wikiDemo && this.conn.state === "offline",
      // The graph also changes with the ledger (an action applied / undone) and when a page write ends: read it again then.
      stamp: () => {
        const a = this.wiki?.state.bot.actions;
        const rows = a && a.kind === "ready" ? a.value.map((r) => `${r.action_id}:${r.status}`).join(",") : "";
        const live = (this.wiki?.graphSource().writes?.snapshot() ?? []).map((w) => `${w.page_id}:${w.state}`).join(",");
        return `${rows}|${live}`;
      },
      onChange: (fn) => {
        const offs = [this.wiki?.subscribe(fn), this.wiki?.graphSource().writes?.subscribe(fn)];
        return () => { for (const off of offs) off?.(); };
      },
    };
  }

  /** 我 in 个性化: the note is made or brought up to date first. */
  private syncProfile(): Promise<void> { return this.account?.state.kind === "ready" ? this.profile?.sync() ?? Promise.resolve() : Promise.resolve(); }

  private async openProfileNote(): Promise<void> {
    try {
      await this.profile?.sync();
      if (!this.openIfExists(await this.resolveProfilePath())) ewNotice(WIKI_COPY.noteNotYet);
    } catch (error: unknown) { ewNotice(error instanceof Error ? error.message : WIKI_COPY.failed); }
  }

  private async resolveProfilePath(): Promise<string> {
    await this.folderReady;
    return resolveProfilePath(this.data, uiLanguage(), {
      exists: (path) => this.app.vault.getAbstractFileByPath(path) !== null,
      rename: async (from, to) => {
        const file = this.app.vault.getAbstractFileByPath(from);
        if (!(file instanceof TFile)) throw new Error(tr("runtime.profileRenameConflict", { path: from }));
        await this.app.fileManager.renameFile(file, to);
      },
      save: () => this.saveData(this.data),
    });
  }

  /** A page not written yet: its 开始生成 tab (a new tab; an open one for the same page is focused instead). */
  private async openStartPage(p: StartPage): Promise<void> {
    const ws = this.app.workspace;
    const same = ws.getLeavesOfType(START_PAGE_VIEW_TYPE).find((l) => l.view instanceof StartPageView && l.view.pageId === p.pageId && l.view.rowKey === p.key);
    if (same) { ws.setActiveLeaf(same, { focus: true }); await ws.revealLeaf(same); return; }
    const leaf = ws.getLeaf("tab");
    await leaf.setViewState({ type: START_PAGE_VIEW_TYPE, state: { ...p }, active: true });
    await ws.revealLeaf(leaf);
  }

  /** Prompt 设置: opens the editor of a step ("rules" = 讲解方式) in a main-area tab; an open one for the same step is focused instead. */
  async openPromptEditor(key: string): Promise<void> {
    const ws = this.app.workspace;
    const same = ws.getLeavesOfType(PROMPT_VIEW_TYPE).find((l) => {
      const v = l.view;
      return v instanceof PromptView ? v.stepKey === key : (l.getViewState().state as { key?: unknown } | undefined)?.key === key;
    });
    if (same) { await ws.revealLeaf(same); return; }
    const leaf = ws.getLeaf("tab");
    await leaf.setViewState({ type: PROMPT_VIEW_TYPE, state: { key }, active: true });
    await ws.revealLeaf(leaf);
  }

  /**
   * Opens the 知识星图 in a main-area tab; an open one is focused instead of opening a second. `goal`: a goal's map id
   * focuses it, null clears the focus (the 今天 thumbnail); absent leaves the map as it is.
   */
  async openStarMap(goal?: string | null): Promise<void> {
    const existing = this.app.workspace.getLeavesOfType(STARMAP_VIEW_TYPE)[0];
    const leaf = existing ?? this.app.workspace.getLeaf("tab");
    if (!existing) await leaf.setViewState({ type: STARMAP_VIEW_TYPE, active: true });
    await this.app.workspace.revealLeaf(leaf);
    if (goal === undefined) return;
    await leaf.loadIfDeferred?.();
    if (leaf.view instanceof StarMapView) leaf.view.focusOn(goal);
  }

  /**
   * 今天复习: opens (or reveals) the wiki sidebar, keeping whichever wiki (real or demo) is open, on its 今天 tab
   * and starts the review in place when there are cards; else it focuses the 下一步 button.
   */
  private async openReview(): Promise<void> {
    await this.openWiki(this.wiki !== null && this.wikiDemo);
    const c = this.wikiController();
    c.setTab("today");
    // One read: init fills what is missing, refreshToday joins the re-read openWiki / setTab already started (or is skipped by its TTL).
    await c.init();
    await c.refreshToday();
    // Cards to do: the review starts in place. None: the cursor goes to the 下一步 button (continue learning), once the view has drawn it.
    c.startReview();
    if (!c.state.review.active) {
      const focus = (): void => {
        this.wikiLeaf()?.view.containerEl
          .querySelector<HTMLElement>("[data-section=next] .ew-bigbtn")?.focus();
      };
      if (typeof window !== "undefined" && typeof window.requestAnimationFrame === "function") window.requestAnimationFrame(focus);
      else focus();
    }
  }

  /**
   * What 每天最多复习几张 in the settings reads and writes: the wiki controller (built on first use; the demo one while the
   * demo wiki is open). Opening the row reads the catalog (the limit lives in its settings) unless it is loaded or loading.
   */
  reviewLimitHost(): ReviewLimitHost {
    const c = (): WikiController => this.wikiController();
    return {
      state: () => c().state, subscribe: (fn) => c().subscribe(fn),
      setReviewPerDay: (n) => void c().setReviewPerDay(n),
      load: () => { const k = c().state.catalog.kind; if (k === "idle" || k === "error") void c().init(); },
    };
  }

  /** Shared Obsidian settings: the current identity's real controller and actions. */
  engineSettingsHost() {
    const c = this.wikiController();
    const actions = wikiActions(() => c);
    actions.openPrompt = (key) => {
      (this.app as typeof this.app & { setting: { close(): void } }).setting.close();
      void this.openPromptEditor(key);
    };
    return { state: () => c.state, subscribe: (fn: () => void) => c.subscribe(fn),
      actions, load: () => { void c.loadEngine(); } };
  }

  /** What 用量 · 每页最多花（美元） in the settings reads and writes: the wiki controller, as languageHost. */
  pageLimitHost(): PageLimitHost {
    const c = (): WikiController => this.wikiController();
    return {
      state: () => c().state, subscribe: (fn) => c().subscribe(fn),
      setPageLimit: (limit) => c().setPageLimit(limit),
      load: () => { const k = c().state.catalog.kind; if (k === "idle" || k === "error") void c().init(); },
    };
  }

  /** What 生成内容的语言 in the settings reads and writes: the wiki controller, as reviewLimitHost. */
  languageHost(): LanguageHost {
    const c = (): WikiController => this.wikiController();
    return {
      state: () => c().state, subscribe: (fn) => c().subscribe(fn),
      setLanguage: (lang) => c().setLanguage(lang),
      load: () => { const k = c().state.catalog.kind; if (k === "idle" || k === "error") void c().init(); },
    };
  }

  /**
   * What the 样板页 settings section talks to (R6.2): the current identity's wiki client and gates, taken from the wiki
   * controller (built on first use; the demo one, on the fake, while the demo wiki is open). The section asks again for
   * every draw, so after a new server / user id (resetWiki) it gets the new identity's client.
   */
  teachHost(): TeachPort {
    return this.wikiController().teachPort();
  }

  /** SettingsHost: the settings in force now. */
  currentSettings(): EvoWikiSettings { return this.data.settings; }

  pickNotesFolder(): Promise<string | null> { return pickNotesFolder(this.app); }

  /** SettingsHost: the usage API for the identity in force at call time (settings are read per request, never captured). */
  usageClient(): UsageClient {
    return this.fixtureUsageClient() ?? new UsageClient(() => ({ baseUrl: this.data.settings.serverUrl, userId: this.data.settings.userId }), obsidianWikiHttp);
  }
  maintenanceClient(): MaintenanceClient | null {
    return this.fixture !== null ? null : new MaintenanceClient(
      () => ({ baseUrl: this.data.settings.serverUrl, userId: this.data.settings.userId }), obsidianWikiHttp,
    );
  }
  /** SettingsHost: the demo fixture's 用量 answers without a server; otherwise a server and user id must be set. */
  usageConfigured(): boolean {
    const st = this.data.settings;
    return this.fixtureUsageClient() !== null || (st.serverUrl !== "" && st.userId !== "");
  }

  /** SettingsHost: told right after a new server / user id took effect (settings reinit). */
  onIdentityChange(fn: () => void): () => void {
    this.identityListeners.add(fn);
    return () => { this.identityListeners.delete(fn); };
  }

  private frontmatter(f: TFile): unknown { return this.app.metadataCache.getFileCache(f)?.frontmatter; }

  /** The note header's 停止 / 重新写: the wiki controller's stop and restart. */
  private readonly headerHooks: HeaderHooks = {
    stop: () => { void this.wikiController().stop(); },
    restart: (pageId) => { void this.wikiController().restart(pageId); },
  };
  /** The last write state the headers were drawn with (a controller change redraws them only when it moved). */
  private headerRunKey = "";

  /** T10: every open Markdown view gets (or loses) the note page header; views that closed stop being watched. */
  private refreshNoteHeaders(): void {
    const seen = new Set<MarkdownView>();
    for (const leaf of this.app.workspace.getLeavesOfType("markdown")) {
      const v = leaf.view;
      if (!(v instanceof MarkdownView)) continue;
      seen.add(v);
      const info = (): HeaderInfo | null => {
        const f = v.file, i = f ? headerInfo(this.frontmatter(f), f.basename) : null;
        // the page's write as this plugin runs it: 停止 while it streams, 已停止 · 重新写 after (the demo wiki writes nothing)
        const run = i !== null && this.wiki !== null && !this.wikiDemo ? this.wiki.writeRun(i.pageId) : null;
        return i !== null && run !== null ? { ...i, run } : i;
      };
      if (this.headerWatch.has(v)) applyHeader(v.containerEl, info(), this.headerHooks);
      else this.headerWatch.set(v, watchHeader(v.containerEl, info, this.headerHooks));
      if (!this.barWatch.has(v)) {
        this.barWatch.set(v, watchReviseBar(v.containerEl, () => {
          const f = v.file;
          const id = f ? (this.frontmatter(f) as Record<string, unknown> | undefined)?.[FM_KEYS.pageId] : undefined;
          return typeof id === "string" && id !== "" ? id : null;
        }, this.checkHub, () => (v.file ? v.getViewData() : null)));
      }
    }
    for (const [v, stop] of this.headerWatch) if (!seen.has(v)) { stop(); this.headerWatch.delete(v); }
    for (const [v, stop] of this.barWatch) if (!seen.has(v)) { stop(); this.barWatch.delete(v); }
  }

}

/**
 * Obsidian's UI language: getLanguage() (Obsidian 1.8+); before that the "language" key Obsidian keeps in localStorage
 * (absent = English).
 */
function obsidianLocale(): string {
  try {
    if (typeof getLanguage === "function") return getLanguage();
    return window.localStorage.getItem("language") ?? "en";
  } catch {
    return "en";
  }
}
