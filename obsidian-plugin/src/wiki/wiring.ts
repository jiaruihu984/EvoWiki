/**
 * Builds the personal-wiki controller for main.ts (real or demo) and maps WikiActions onto it. The only place that
 * constructs FakeWikiApi, calls E3a's syncWikiNote, reads wiki notes (findWikiNote: local, read-only, the path is for
 * opening the note; wikiNoteEditReport / wikiNoteEditReportAt: the same read plus the note's written copy under
 * .evowiki/written/, and only a diff summary of the user's edit plus the note's vault path go to note-edit, never the
 * note text), or builds the
 * interactive asset fetcher (makeAssetFetcher).
 */
import type { App } from "obsidian";
import type { StreamTransport } from "../api/client";
import { validateNotesFolder } from "../notes/note-format";
import { ewNotice } from "../ui/ew-notice";
import type { WikiActions } from "./actions";
import { HttpWikiClient, type WikiApi, type WikiHttp } from "./api";
import { WIKI_COPY } from "./copy";
import { WikiController, type WikiControllerDeps } from "./controller";
import { AssetFetcher, obsidianAssetStore } from "./embed/assets";
import { FakeWikiApi } from "./fake";
import type { SyncResult, WikiNoteInput } from "./note";
import { claimsPage } from "./note/format";
import { notePageIds, writingPageIds } from "./note/migrate";
import type { Recommendation } from "./starmap";
import { obsidianWikiNoteVault } from "./note/sync";
import { TreeMarker } from "./note/tree-marker";
import { RevealOnce, revealInExplorer } from "./note/explorer-reveal";
import { parseWikiFile } from "./note/format";
import type { HandEditPlan } from "./note/hand-edit";
import { handEditFor, type WrittenStore } from "./note/written";
import { obsidianSourcePort } from "./sources/obsidian-sources";
import { localBlockProgressStore } from "./evoui/progress-store";
import { localBlockEventStore } from "./evoui/event-store";
import { userFolder } from "./note/user-folder";

export interface WikiWiringDeps {
  app: App;
  settings: () => { serverUrl: string; userId: string; notesFolder: string };
  /** Resolves once the account root is ready (main.ts); notes are read and written only after. */
  folderReady?: () => Promise<void>;
  conn: WikiControllerDeps["conn"];
  http: WikiHttp;
  sse: StreamTransport;
  /** E3a's syncWikiNote, injected for tests. */
  syncWikiNote: (app: App, folder: string, input: WikiNoteInput, onPath?: (path: string, moved: "no" | "yes" | "failed") => void, accepted?: string, valid?: () => boolean) => Promise<SyncResult>;
  /** Opens a vault path only if it is an existing file; true when it opened one. `at`: where to scroll (controller). */
  openExisting: WikiControllerDeps["openNote"];
  /** The wiki note the user is in now (main.ts tracks it): a hand edit goes up when he leaves it. */
  openWikiNote?: () => string | null;
  /** The one asset fetcher (makeAssetFetcher; shared with the note blocks' 取回 buttons); absent = no fetching. */
  assets?: AssetFetcher;
  /** The in-page drafts' decisions, shared with the chat's cards (main.ts makes the one instance). */
  decisions?: WikiControllerDeps["decisions"];
  pageRoundMemo?: WikiControllerDeps["pageRoundMemo"];
  /** Shows the wiki sidebar. */
  showSidebar?: () => void;
  /** A page not written yet was opened: its 开始生成 tab. */
  showStartPage?: WikiControllerDeps["showStartPage"];
  /** 「⋯ → 问助手」 (main.ts: opens the 助手 tab and attaches the quote). */
  askAssistant?: WikiControllerDeps["askAssistant"];
  /** 查看 on a 活动记录 change row (main.ts: scroll + ants frame in the sidebar). */
  revealChange?: WikiControllerDeps["revealChange"];
  /** The Bot's 问助手 / chips (main.ts: opens the 助手 tab, sets the draft and sends). */
  assistantSend?: WikiControllerDeps["assistantSend"];
  resolveMemoryConflicts?: WikiControllerDeps["resolveMemoryConflicts"];
  /** 读完了's 内容太简单 / 理解有困难 (main.ts: opens the 助手 tab in a new conversation and sends). */
  assistantSendFresh?: WikiControllerDeps["assistantSendFresh"];
  /**
   * Dev-only demo fixture (plugin data `fixture: "demo"`, fixtures-demo.ts): the real controller on this fake client
   * instead of the server, always online and configured, with the demo's recommendations. Not the demo mode.
   */
  fixture?: { api: WikiApi; recommendations: readonly Recommendation[] };
  /** 学点新的 (main.ts: opens the 学点新的 tab with the topic). */
  learnNew?: (topic: string, start?: boolean) => void;
  /** Restores and continues a goal's run (main.ts), reporting failure through rejection. */
  continueGoal?: WikiControllerDeps["continueGoal"];
  /** The file tree's 「正在写」 marks (main.ts owns it and disposes it on unload); absent = one made here. */
  treeMarker?: TreeMarker;
  /** Opens the file explorer down to a path (tests: a spy). Default: explorer-reveal.ts revealInExplorer. */
  revealInExplorer?: (path: string) => void;
  /** Prompt 设置 (main.ts: opens the centre-pane editor of a step; "rules" = 讲解方式). */
  openPrompt?: (key: string) => void;
  openProfile?: () => void;
}

/** The demo never talks to the server, so the real server's connection state must not block it. */
const DEMO_CONN: WikiControllerDeps["conn"] = {
  state: "online",
  track: (p) => p,
  onChange: () => () => undefined,
};

/** P2: where the wiki notes live: the active account's independent notes root (user-folder.ts). */
export const wikiNotesFolder = (s: { notesFolder: string; userId: string }): string => userFolder(s);

/** The command T12 registers for the 设置 view (main.ts). */
export const OPEN_SETTINGS_COMMAND = "evowiki:open-settings";
/** Obsidian's internal command / settings handles (not in the public API). */
type SettingsApp = {
  commands?: { commands?: Record<string, unknown>; executeCommandById?(id: string): boolean };
  setting?: { open?(): void; openTabById?(id: string): unknown };
};

/**
 * 个性化 · 去设置里改: runs the evowiki:open-settings command when it is registered, otherwise opens Obsidian's settings on
 * the EvoWiki tab.
 */
export function openEvoWikiSettings(app: App): void {
  const a = app as unknown as SettingsApp;
  if (a.commands?.commands?.[OPEN_SETTINGS_COMMAND] !== undefined && a.commands.executeCommandById?.(OPEN_SETTINGS_COMMAND) === true) return;
  a.setting?.open?.();
  a.setting?.openTabById?.("evowiki");
}

export function makeWikiController(d: WikiWiringDeps, demo: boolean): WikiController {
  // Every short result: the demo's notices (ew-notice.ts / app.js notice: 4.2 s, like the note page's own; the same
  // for the demo wiki.
  const notice = (text: string): void => { ewNotice(text); };
  const noticeActions: NonNullable<WikiControllerDeps["noticeActions"]> = (text, actions) => { ewNotice(text, actions); };
  const showSidebar = d.showSidebar;
  const showStartPage = d.showStartPage;
  const askAssistant = d.askAssistant;
  const assistantSend = d.assistantSend;
  const assistantSendFresh = d.assistantSendFresh;
  const revealChange = d.revealChange;
  const learnNew = d.learnNew;
  const continueGoal = d.continueGoal;
  const openPrompt = d.openPrompt;
  const openProfile = d.openProfile;
  const openSettings = (): void => openEvoWikiSettings(d.app);
  if (demo) {
    return new WikiController({ blockEventStore: null, blockProgressStore: null, api: new FakeWikiApi({ delayMs: 120 }), conn: DEMO_CONN, sync: null,
      openNote: d.openExisting, configured: () => true, demo: true, notice, noticeActions, openSettings,
      ...(revealChange ? { revealChange } : {}), ...(openPrompt ? { openPrompt } : {}), ...(openProfile ? { openProfile } : {}),
      ...(showSidebar ? { showSidebar } : {}), ...(askAssistant ? { askAssistant } : {}), ...(assistantSend ? { assistantSend } : {}),
      ...(assistantSendFresh ? { assistantSendFresh } : {}), ...(learnNew ? { learnNew } : {}) });
  }
  // P2: the wiki notes live in the active account's notes root. Read per call, like the client's server URL and user id; never
  // before the account root is ready (main.ts).
  const folder = async (): Promise<string> => { await d.folderReady?.(); return wikiNotesFolder(d.settings()); };
  const fixture = d.fixture;
  const marker = d.treeMarker ?? new TreeMarker();
  // a page whose writing starts: the file explorer opens down to it (once per write; explorer-reveal.ts)
  const reveal = new RevealOnce((path) => { (d.revealInExplorer ?? ((p: string) => revealInExplorer(d.app, p)))(path); });
  // Where each page's file was put last time (folder, title, parent): a move with the same key was not the graph's doing.
  const placed = new Map<string, string>();
  // Streamed writes carry `evowiki-status: writing` and a mark in the file tree; the full sync (or a failure) takes both off.
  const runSync = async (i: WikiNoteInput, opts?: { partial?: boolean; accepted?: string; valid?: () => boolean }): Promise<SyncResult> => {
    const partial = opts?.partial === true;
    const at: { path: string; moved: "no" | "yes" | "failed" } = { path: "", moved: "no" };
    try {
      const r = await d.syncWikiNote(d.app, await folder(), partial ? { ...i, writing: true } : i, (path, moved) => { at.path = path; at.moved = moved; }, opts?.accepted, opts?.valid);
      if (r === "superseded" || opts?.valid?.() === false) return "superseded";
      if (partial && at.path !== "") { marker.set(i.pageId, at.path); reveal.started(i.pageId, at.path); }
      else if (i.placeOnly !== true) { marker.clear(i.pageId); reveal.ended(i.pageId); }   // a reconcile pass does not end a write
      const key = JSON.stringify([i.folder ?? "", i.title, i.parentName ?? null]);
      if (at.moved === "yes" && placed.get(i.pageId) === key) {
        ewNotice(WIKI_COPY.fileRenamedBack(i.title));
      }
      placed.set(i.pageId, key);
      return r;
    } catch (e) {
      marker.clear(i.pageId);
      reveal.ended(i.pageId);
      throw e;
    }
  };
  const api = fixture?.api ?? new HttpWikiClient(() => ({ baseUrl: d.settings().serverUrl, userId: d.settings().userId }), d.http, d.sse);
  const identity = d.settings();
  return new WikiController({
    blockProgressStore: fixture ? null : localBlockProgressStore(window.localStorage, { server: identity.serverUrl, user: identity.userId }),
    blockEventStore: fixture ? null : localBlockEventStore(window.localStorage, { server: identity.serverUrl, user: identity.userId }),
    api,
    conn: fixture ? DEMO_CONN : d.conn,
    sync: { run: runSync, find: async (id) => findWikiNote(d.app, await folder(), id),
      listPages: async () => notePageIds(obsidianWikiNoteVault(d.app), await folder()),
      listWriting: async () => writingPageIds(obsidianWikiNoteVault(d.app), await folder()),
      handEdit: async (i) => wikiHandEdit(obsidianWikiNoteVault(d.app), await folder(), i),
      edited: async (path, id) => wikiNoteEdited(obsidianWikiNoteVault(d.app), await folder(), path, id),
      isOpen: (path) => d.openWikiNote?.() === path },
    sourcePort: obsidianSourcePort(d.app, () => userFolder(d.settings())),
    openNote: d.openExisting,
    configured: () => { if (fixture) return true; const s = d.settings(); return s.serverUrl !== "" && s.userId !== ""; },
    demo: false,
    notice,
    noticeActions,
    openSettings,
    ...(revealChange ? { revealChange } : {}),
    ...(openPrompt ? { openPrompt } : {}),
    ...(openProfile ? { openProfile } : {}),
    ...(fixture ? { recommendations: fixture.recommendations } : {}),
    ...(showSidebar ? { showSidebar } : {}),
    ...(showStartPage ? { showStartPage } : {}),
    ...(askAssistant ? { askAssistant } : {}),
    ...(assistantSend ? { assistantSend } : {}),
    ...(d.resolveMemoryConflicts ? { resolveMemoryConflicts: d.resolveMemoryConflicts } : {}),
    ...(assistantSendFresh ? { assistantSendFresh } : {}),
    ...(learnNew ? { learnNew } : {}),
    ...(continueGoal ? { continueGoal } : {}),
    ...(d.assets ? { assets: d.assets } : {}),
    ...(d.decisions ? { decisions: d.decisions } : {}),
    ...(d.pageRoundMemo ? { pageRoundMemo: d.pageRoundMemo } : {}),
  });
}

/**
 * Appendix A·6: the interactive asset fetcher, on its own real client (server URL and user id read per call) and the
 * vault (embed/assets.ts obsidianAssetStore: create never overwrites). It writes only `<notes>/assets/<id>.html`.
 * The demo never gets one: it never writes the vault.
 */
export function makeAssetFetcher(d: Pick<WikiWiringDeps, "app" | "settings" | "http" | "sse"> & { fixtureApi?: WikiApi },
  onWritten: (assetId: string) => void): AssetFetcher {
  // The dev-only demo fixture fetches from its fake client (fixtures-demo.ts), never the server.
  const api = d.fixtureApi ?? new HttpWikiClient(() => ({ baseUrl: d.settings().serverUrl, userId: d.settings().userId }), d.http, d.sse);
  return new AssetFetcher(() => api, obsidianAssetStore(d.app), () => wikiNotesFolder(d.settings()), onWritten);
}

/** The read side of E3a's WikiNoteVault that finding a note needs. */
export interface WikiNoteReader {
  listMarkdown(folder: string): string[];
  read(path: string): Promise<string>;
}

/** The single note in `folder` that claims pageId, with the text read while looking; null for an invalid folder, none, or several. */
async function claimingNote(store: WikiNoteReader, folder: string, pageId: string): Promise<{ path: string; text: string } | null> {
  const v = validateNotesFolder(folder);
  if (!v.ok) return null;
  const hits: { path: string; text: string }[] = [];
  for (const path of store.listMarkdown(v.folder)) {
    const text = await store.read(path);
    if (claimsPage(text, pageId)) hits.push({ path, text });
  }
  return hits.length === 1 ? hits[0]! : null;
}

/** A2: the path of the single note in `folder` that claims pageId; null for an invalid folder, none, or several. */
export async function findClaimingNote(store: WikiNoteReader, folder: string, pageId: string): Promise<string | null> {
  return (await claimingNote(store, folder, pageId))?.path ?? null;
}

/** What reading a hand edit needs: the notes and their written copies (E3a's obsidianWikiNoteVault). */
export type WikiEditReader = WikiNoteReader & Pick<WrittenStore, "readHidden">;

/** The one note claiming the page and what the user changed in its block; null when no single note claims it. Local and read-only. */
export async function wikiHandEdit(store: WikiEditReader, folder: string, input: WikiNoteInput): Promise<{ path: string; text: string; plan: HandEditPlan } | null> {
  const hit = await claimingNote(store, folder, input.pageId);
  return hit === null ? null : { ...hit, plan: await handEditFor(store, hit.text, input) };
}

/** Whether `path` is the one note claiming `pageId` and its block was changed. */
export async function wikiNoteEdited(store: WikiNoteReader, folder: string, path: string, pageId: string): Promise<boolean> {
  const hit = await claimingNote(store, folder, pageId);
  if (hit === null || hit.path !== path) return false;
  const p = parseWikiFile(hit.text);
  return p.kind === "one" && p.edited;
}

export function findWikiNote(app: App, folder: string, pageId: string): Promise<string | null> {
  return findClaimingNote(obsidianWikiNoteVault(app), folder, pageId);
}

/**
 * Each action fires the controller method of the same name on the current controller (read at call time).
 * `reconnect` (the real client's conn.retryNow) runs first on retryConnection, like the concept sidebar's Retry: it
 * resets the backoff and probes the server even when no GET is in error, then the errored GETs are re-run.
 */
export function wikiActions(c: () => WikiController, reconnect?: () => Promise<void>): WikiActions {
  return {
    setTab: (t) => c().setTab(t),
    retryConnection: () => {
      if (!reconnect) { void c().retryConnection(); return; }
      const after = (): void => { void c().retryConnection(); };
      void reconnect().then(after, after);
    },
    setLearnTopic: (t) => c().setLearnTopic(t),
    openLearnNew: (...args) => c().openLearnNew(...args),
    openWikiPage: (id) => void c().openWikiPage(id),
    openPath: (id) => void c().openPath(id),
    pathNext: () => void c().pathNext(),
    pathDone: (i) => void c().pathDone(i),
    revealCue: (cell) => c().revealCue(cell),
    // A self-grade shows no result: once it is recorded the review moves straight on (nextReview is a no-op when it failed).
    gradeReview: (cell, g) => void c().gradeReview(cell, g).then(() => c().nextReview(cell)),
    answerReview: (cell, ch) => void c().answerReview(cell, ch),
    nextReview: (cell) => c().nextReview(cell),
    startReview: () => c().startReview(),
    pauseReview: () => c().pauseReview(),
    markBar: (done) => c().markBar(done),
    refreshToday: () => void c().refreshToday(),
    askAboutCard: (cell) => c().askAboutCard(cell),
    followup: (t, ch) => void c().followup(t, ch),
    answerConfirm: (id, ch) => void c().answerConfirm(id, ch),
    reteach: (id) => void c().reteach(id),
    confirmUnclear: (id) => c().confirmUnclear(id),
    setReviewPerDay: (n) => void c().setReviewPerDay(n),
    reloadReview: () => void c().reloadReview(),
    moreReview: () => void c().moreReview(),
    reloadCatalog: () => void c().reloadCatalog(),
    organize: () => void c().organize(),
    stop: () => void c().stop(),
    openCurrentNote: () => c().openCurrentNote(),
    know: (sid) => void c().know(sid),
    toggleFold: (sid) => void c().toggleFold(sid),
    togglePin: (sid) => void c().togglePin(sid),
    answerCheck: (sid, ch) => void c().answerCheck(sid, ch),
    done: () => void c().done(),
    expand: () => void c().expand(),
    retry: (r) => void c().retry(r),
    jumpToSection: (sid) => c().jumpToSection(sid),
    reloadRsi: () => void c().reloadRsi(),
    setRsiSwitch: (on) => void c().setRsiSwitch(on),
    undoPatch: (id) => void c().undoPatch(id),
    approvePatch: (id) => void c().approvePatch(id),
    discardPatch: (id) => void c().discardPatch(id),
    selectCell: (cell) => c().selectCell(cell),
    declareCell: (cell, v) => void c().declareCell(cell, v),
    loadEngine: () => void c().loadEngine(),
    openSettings: () => c().openSettings(),
    openPrompt: (key) => c().openPrompt(key),
    openProfile: () => c().openProfile(),
    continueLearning: () => void c().continueLearning(),
    answerReviewFirst: (hint, choice) => void c().answerReviewFirst(hint, choice),
    botStart: (key) => void c().botStart(key),
    botNotInterested: (key) => void c().botNotInterested(key),
    botGoConfirm: (key) => c().botGoConfirm(key),
    botDecide: (id, op) => void c().botDecide(id, op),
    openRevise: (pageId) => void c().openRevise(pageId),
    goToChange: (pageId, sid) => void c().goToChange(pageId, sid),
    botToggleWhy: (key) => void c().botToggleWhy(key),
    botSaveSettings: (s) => void c().botSaveSettings(s),
    botPause: (u) => void c().botPause(u),
    botChip: (id) => void c().botChip(id),
    closeCelebrate: () => c().closeCelebrate(),
    sourceStartRename: (id) => c().sourceStartRename(id),
    sourceSetRenameDraft: (t) => c().sourceSetRenameDraft(t),
    sourceCommitRename: () => void c().sourceCommitRename(),
    sourceCancelRename: () => c().sourceCancelRename(),
    sourceAskDelete: (id) => c().sourceAskDelete(id),
    sourceCancelDelete: () => c().sourceCancelDelete(),
    sourceConfirmDelete: (id) => void c().sourceConfirmDelete(id),
    sourceOpen: (id) => void c().sourceOpen(id),
    sourceRetryFollow: (id) => c().sourceRetryFollow(id),
    sourceRetryImport: (id) => c().sourceRetryImport(id),
    sourcesReload: () => void c().sourcesReload(),
    sourcesAdd: (files) => void c().sourcesAdd(files),
    personalView: (patch) => c().personalView(patch),
    prefDraft: (key, text) => c().prefDraft(key, text),
    prefAdopt: (id, text) => void c().prefAdopt(id, text),
    prefReject: (id) => void c().prefReject(id),
    prefSave: (id, text) => void c().prefSave(id, text),
    prefRemove: (id) => void c().prefRemove(id),
    prefRestore: (text) => void c().prefRestore(text),
    resolveConflicts: () => c().resolveConflicts(),
    reloadConflicts: () => void c().reloadConflicts(),
  };
}
