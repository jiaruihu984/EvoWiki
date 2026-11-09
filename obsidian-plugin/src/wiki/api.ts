import type { ProfileOut } from "../generated/wiki";
import { jsonCall, type StreamTransport } from "../api/client";
import { ApiError, apiErrorOf } from "../api/errors";
import { SseDecoder, type SseMessage } from "../api/sse";
import { MEMORY_CONFLICTS_PATH, writeOut } from "../assistant/client";
import { ContractError } from "../assistant/parse";
import type { WriteOut } from "./schema";
import type { BlockEvent, BlockProgressIn, BlockProgressOut } from "./evoui/block-event";
import type {
  ActionOut, ActivityOut, BadgeOut, BotSettings, BotGoalOut, PauseUntil, Reaction, ReadingEntry, ReviewReaction, ReviewReminderOut, BotReadingOut, SourceReviseOut, SuggestionOut, WhyOut,
} from "./bot/bot-schema";
import type { NodeActionOut } from "./graph-page/parse";
import { type MemoryConflictOut, parseConflicts } from "./memory-conflicts";
import { buildMultipart } from "./multipart";
import {
  ORGANIZE_EVENTS, SOURCE_EVENTS,
  type AnswersIn, type ProgressOut, type ReviewFirstIn, type CatalogOut, type CheckAnswerOut, type DeclareOut,
  type Declared, type DoneOut, type ShelveOut, type FeedbackOut, type GoalDraftOut, type GuidelineIn,
  type GuidelineOut, type GuidelinesOut, type KGridOut, type OrganizeEvent,
  type PageOut, type PathNextOut, type PathOut, type PathPageDoneOut, type ReviewGradeIn,
  type ReviewGradeOut, type ReviewTodayOut, type SectionOut, type SettingsIO, type SettingsPut,
  type AssetGeneratedOut, AssetMeta, type ExemplarIn, type HandEditIn, type HandEditOut, type SignalCreatedOut,
  type ComponentOut, type FeedbackUndoOut, type PageFeedbackIn,
  type CheckFollowupIn, type CheckFollowupOut, type ConfirmAnswerIn, type ConfirmAnswerOut,
  type EngineOut, type PromptStepOut, type PageFeedbackOut, type RsiChangeOut, type RsiSwitch, type SpecItemIn, type SpecItemPatchIn, type TeachSpecOut,
  type GraphOut, type PathEditIn, type PathEditOut, type SplitOut,
  type BlockResultsOut, type ExamSelfIn, type ExamSelfOut, type PageExamOut,
  type SectionEditIn, type SourceDetail, type SourceEvent, type SourceOut, type SourceUploadIn, type SourceUseIn, type SourceUseOut,
} from "./schema";
import type { FileOut } from "./schema";

/**
 * `body`: a JSON string, or an ArrayBuffer (multipart upload) whose `contentType` is also given (requestUrl takes it as
 * its own option). `binary`: the caller wants the response bytes (`bytes`); `text` is then only read for an error body.
 */
export interface WikiHttpRequest {
  method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE"; url: string; headers: Record<string, string>; body?: string | ArrayBuffer;
  contentType?: string; binary?: boolean;
}
/** Resolves with any HTTP status; throws OfflineError only when the server is unreachable. */
export interface WikiHttp { request(req: WikiHttpRequest): Promise<{ status: number; text: string; bytes?: ArrayBuffer }> }
export interface WikiConfig { baseUrl: string; userId: string }
/** `fresh` (organize only): 重新组织 — write the page anew; a write still running is followed, a finished one never replayed. */
/** The artifact id the client was shown, added to a request body only when known (never guessed, never looked up). */
function withArtifact<T extends Record<string, unknown>>(body: T, id: string | null | undefined): T {
  return id ? { ...body, artifact_id: id } : body;
}

export interface StreamOpts { signal?: AbortSignal; fresh?: boolean; artifactId?: string | null }

export interface WikiApi {
  /** The next diagnosis round (same draft_id), or what the goal finished as (page / path / map). */
  answerDraft(draftId: string, body: AnswersIn): Promise<GoalDraftOut>;
  organize(pageId: string, on: (e: OrganizeEvent) => void, o?: StreamOpts): Promise<void>;
  /** POST /wiki/pages/{id}/open: the user opens the page. `rewrite`: the page view's open; only then may the backend start a page rewrite an RSI change is waiting for. Re-opens after an action leave it off. Never writes an unwritten page. */
  openPage(pageId: string, opts?: { rewrite?: boolean }): Promise<PageOut>;
  /** GET /wiki/pages/{id}: the page, read only (nothing is opened, called or started). For background work. */
  getPage(pageId: string): Promise<PageOut>;
  fold(pageId: string, sid: string, folded: boolean): Promise<SectionOut>;
  pin(pageId: string, sid: string, pinned: boolean): Promise<SectionOut>;
  /** `artifactId` = the section version the user was shown (RSI feedback targets exactly that); absent = not sent. */
  feedbackKnow(pageId: string, sid: string, artifactId?: string | null): Promise<FeedbackOut>;
  answerCheck(pageId: string, sid: string, choice: number, artifactId?: string | null): Promise<CheckAnswerOut>;
  done(pageId: string): Promise<DoneOut>;
  /** 恢复成完整页 / 收为参考卡 (409 when the page cannot be shelved). */
  expand(pageId: string): Promise<ShelveOut>;
  collapse(pageId: string): Promise<ShelveOut>;
  catalog(): Promise<CatalogOut>;
  /** GET /wiki/progress: the one next step (今天) and every goal's (wikiapp.progress); 0 LLM calls. */
  progress(): Promise<ProgressOut>;
  /** 先复习 / 跳过 on 「建议先复习 X」: closes it; changes no path. */
  reviewFirst(body: ReviewFirstIn): Promise<void>;
  kGrid(): Promise<KGridOut>;
  declare(cell: string, value: Declared | null): Promise<DeclareOut>;
  getSettings(): Promise<SettingsIO>;
  /** Sends only the fields given (the server leaves absent ones alone). */
  putSettings(s: SettingsPut): Promise<SettingsIO>;
  guidelines(): Promise<GuidelinesOut>;
  addGuideline(body: GuidelineIn): Promise<GuidelineOut>;
  deleteGuideline(id: string): Promise<void>;
  getPath(pathId: string): Promise<PathOut>;
  pathNext(pathId: string, key?: string): Promise<PathNextOut>;
  pathPageDone(pathId: string, index: number): Promise<PathPageDoneOut>;
  /** GET today's review list; `more`: the next full batch once today's (`limit`) is answered (nothing asks for it yet). */
  reviewToday(more?: boolean): Promise<ReviewTodayOut>;
  /** 「考考我这页」: the page's cards, due or not (404 unknown page). */
  reviewPage(pageId: string): Promise<ReviewTodayOut>;
  gradeReview(body: ReviewGradeIn): Promise<ReviewGradeOut>;
  // feedback signals
  /** Understood and answered at once (201): the signal, what was understood, how the page changed, the style plan. */
  pageFeedback(pageId: string, body: PageFeedbackIn): Promise<PageFeedbackOut>;
  /** Back to the page as before that feedback (404 unknown, 409 already undone or the page changed since); callers re-read the page. */
  undoFeedback(pageId: string, feedbackId: string): Promise<FeedbackUndoOut>;
  pageFit(pageId: string): Promise<SignalCreatedOut>;
  /** A hand edit in the note becomes the page's text (409 a section is gone or a run is writing; 422 not allowed). */
  handEdit(pageId: string, body: HandEditIn): Promise<HandEditOut>;
  /** Its sections as they were (404 unknown, 409 already undone or changed since). */
  undoHandEdit(pageId: string, editId: string): Promise<HandEditOut>;
  // RSI changes
  /** Newest first. */
  rsiChanges(): Promise<RsiChangeOut[]>;
  rsiSwitch(): Promise<RsiSwitch>;
  putRsiSwitch(on: boolean): Promise<RsiSwitch>;
  /** 撤销 any shown change (every layer; the backend dispatches by id). */
  undoPatch(patchId: string): Promise<RsiChangeOut>;
  /** 采用 / 不要 a code change waiting for review (`forgep:…`): 409 when it is not under review (or approve while code changes are off). */
  approvePatch(patchId: string): Promise<RsiChangeOut>;
  discardPatch(patchId: string): Promise<RsiChangeOut>;
  /** The 个性化 tab: each step's prompt versions, model and text; θ_infer rows with 默认 / when and why tuned. */
  engine(): Promise<EngineOut>;
  /** Prompt 设置: every model step's guidance (GET /wiki/prompts). */
  prompts(): Promise<PromptStepOut[]>;
  /** Saves the user's wording for a step; 422 carries a plain Chinese reason in `detail`. */
  putPrompt(step: string, text: string): Promise<PromptStepOut>;
  /** Back to the default wording (DELETE); returns the step with the default text. */
  resetPrompt(step: string): Promise<PromptStepOut>;
  /** `cardId` = ReviewItemOut.card_id. */
  components(): Promise<ComponentOut[]>;
  /** 个性化's 待确认的记忆: the open groups of possibly conflicting memories (MEMORY_CONFLICTS_PATH; read only). */
  memoryConflicts(): Promise<MemoryConflictOut[]>;
  // teaching spec; `base` = ?base_version= (409 when it is not the current version)
  teachSpec(): Promise<TeachSpecOut>;
  addSpecItem(body: SpecItemIn, base?: number): Promise<TeachSpecOut>;
  editSpecItem(itemId: string, body: SpecItemPatchIn, base?: number): Promise<TeachSpecOut>;
  deleteSpecItem(itemId: string, base?: number): Promise<TeachSpecOut>;
  /** 设为我的偏好 without an edit: a pending or RSI rule becomes the user's (POST …/take_over). */
  takeOverSpecItem(itemId: string, base?: number): Promise<TeachSpecOut>;
  addExemplar(body: ExemplarIn, base?: number): Promise<TeachSpecOut>;
  deleteExemplar(exemplarId: string, base?: number): Promise<TeachSpecOut>;
  /** One LLM call; nothing in the spec changes. */
  // interactives
  assetMeta(assetId: string): Promise<AssetMeta>;
  /** Whether the server stored this content (read: nothing written). */
  assetGenerated(text: string, kind: "evoui" | "html"): Promise<AssetGeneratedOut>;
  /** The interactive's HTML as-is (not JSON). */
  assetHtml(assetId: string): Promise<string>;
  /**
   * Evidence only: result events of an evoui asset block (at most 200 per post). POST /wiki/assets/{id}/results {events};
   * the server grades items against the stored block (a graded item with a known cell becomes K evidence) and skips an
   * event it already recorded, so a resend is harmless. 422: an event the block cannot have (nothing is recorded).
   */
  blockEvents(assetId: string, events: readonly BlockEvent[]): Promise<{ inK: boolean }>;
  /** The reader's stored progress in an evoui asset block (GET /wiki/assets/{id}/progress); revision 0 + data null = none. */
  blockProgress(assetId: string): Promise<BlockProgressOut>;
  /** Stores evoui.js `progress()` ({revision, data}); the server keeps it only when the revision is newer (else stale: true). */
  saveBlockProgress(assetId: string, progress: BlockProgressIn): Promise<{ stale: boolean }>;
  /**
   * 粗心选错了 / 我学会了 / 看不懂 after a wrong answer (a page check, or a review card in check mode). 404 no such answer,
   * 422 not a wrong answer / a slip on 不知道, 409 already followed up; slip / learned only: 503 no model, 502 no usable
   * confirmation question (nothing recorded).
   */
  followup(pageId: string, sid: string, body: CheckFollowupIn): Promise<CheckFollowupOut>;
  /** Answers a due confirmation question (choice -1 = 不知道). 404 unknown, 409 already answered, 422 no such option. */
  confirmAnswer(followupId: string, body: ConfirmAnswerIn): Promise<ConfirmAnswerOut>;
  // Bot (/wiki/bot/*)
  /** Pending / shown suggestions; "page" needs `pageId`. */
  botSuggestions(placement: "today" | "page", pageId?: string): Promise<SuggestionOut[]>;
  /** 409 when the suggestion already has a final reaction; 404 unknown. */
  botReact(id: string, reaction: Reaction): Promise<SuggestionOut>;
  botReviewReminder(): Promise<ReviewReminderOut>;
  botReviewReact(reaction: ReviewReaction): Promise<ReviewReminderOut>;
  /** Newest first; `status` = proposed / executed / ... */
  botActions(status?: string): Promise<ActionOut[]>;
  /** POST /wiki/sources/{id}/revise: a draft per page (a page with an open draft is skipped). */
  reviseFromSource(sourceId: string, pageIds: string[]): Promise<SourceReviseOut>;
  /** approve / reject repeated returns the row (idempotent); 409 for any other state (e.g. reject after executed, approve after rejected / undone / failed); 404 unknown. */
  botDecide(id: string, op: "approve" | "reject" | "undo"): Promise<ActionOut>;
  /**
   * 重试 on an in-page draft that no longer fits its page (POST /wiki/bot/actions/{id}/redraft): written again for the same
   * request against the page as it is now (a practice: the same row with a new draft; a materials draft: a new row being
   * written). A refusal is an ApiError (409, its detail the reason). Optional: a fake without it offers no 重试.
   */
  botRedraft?(id: string): Promise<ActionOut>;
  botWhy(q: { triggerId: string } | { suggestionId: string }): Promise<WhyOut>;
  /** The last 14 days of what the Bot did; `skipped` also lists the silent ones. */
  botRecent(skipped: boolean): Promise<ActivityOut[]>;
  /**
   * Where each page's write is (GET /wiki/writes): state, section / of, cost so far. A page without a run is absent.
   * Without ids: every write that is queued, planning or writing right now.
   */
  writes(pageIds?: string[]): Promise<WriteOut[]>;
  /** Every page of the user as its file needs it (GET /wiki/files): a read with no side effects. */
  files(): Promise<FileOut[]>;
  /**
   * GET /wiki/pages/{id}/run/stream: replays and follows the page's live write run (the organize events) without
   * starting, consuming or opening anything. Rejects with ApiError 404 when the page has no live run.
   */
  runStream(pageId: string, runId: string, on: (e: OrganizeEvent) => void, o?: StreamOpts): Promise<void>;
  /** 「停止」: POST /wiki/pages/{id}/run/cancel cancels the page's live write run (204). ApiError 404 when it has none. */
  cancelRun(pageId: string): Promise<void>;
  botBadge(): Promise<BadgeOut>;
  botSettings(): Promise<BotSettings>;
  putBotSettings(s: BotSettings): Promise<BotSettings>;
  botPause(until: PauseUntil): Promise<BotSettings>;
  /** B5: the goals as the Bot sees them (parked, the latest weekly review, this week's reading minutes). */
  botGoals(): Promise<BotGoalOut[]>;
  /** B5 搁置 / 恢复 a goal (only the Bot's reminders and weekly review stop; the goal is untouched). 404 unknown path. */
  botPark(pathId: string, park: boolean): Promise<BotGoalOut>;
  /** 重新规划 a path (LLM, the user's own edits kept). */
  replanPath(pathId: string): Promise<PathEditOut>;
  /** B5 reading time: one batch of active reading seconds per page (a resent batch id is not counted twice). */
  postReading(batchId: string, entries: ReadingEntry[]): Promise<BotReadingOut>;
  // the node graph (知识图谱, plan 2026-10-13-unified-node-graph §4)
  /** GET /wiki/graph: nodes, relations, groups (levels 1 and 2) and the goals with their paths. */
  graph(): Promise<GraphOut>;
  /** 看不懂: the model splits the node into finer children (one LLM call). */
  splitNode(nodeId: string): Promise<SplitOut>;
  /** 我会了，跳过: records the learner's words as evidence and skips the node's page in that goal's path. */
  /** 我想学这个: adds a page of the learner's own (mine) to that goal's path. */
  wantNode(nodeId: string, mapId: string): Promise<NodeActionOut>;
  /** A user edit of a learning path (move / remove / add); the server keeps it through later replans. */
  editPath(pathId: string, edit: PathEditIn): Promise<PathEditOut>;
  /** 拆成多页: the row's node is split and its parts take the row's place; `undo` in the answer undoes it. */
  splitRow(pathId: string, pageKey: string): Promise<PathEditOut>;
  /** Takes 拆成多页 back (`change_id` = the split's `undo`): the original row again. */
  undoSplitRow(pathId: string, pageKey: string, changeId: string): Promise<PathEditOut>;
  // sources (资料, /wiki/sources)
  /** The live sources, newest first. */
  listSources(): Promise<SourceOut[]>;
  /** One source with its sections and uses (404: unknown, deleted or someone else's). */
  getSource(sourceId: string): Promise<SourceDetail>;
  /**
   * POST /wiki/sources (multipart): a file, a URL or pasted text. 201 a new source (its import has started), 200 the same
   * content again (the existing source; a given `use` is still recorded). 413 too big, 415 unsupported file, 422 bad input.
   */
  uploadSource(input: SourceUploadIn): Promise<SourceOut>;
  /** GET …/progress (SSE): progress items, then done or error; nothing is delivered after either. */
  followSource(sourceId: string, on: (e: SourceEvent) => void, o?: StreamOpts): Promise<void>;
  addSourceUse(sourceId: string, use: SourceUseIn): Promise<SourceUseOut>;
  /** PATCH {title}: 1–200 characters after trimming (422 otherwise). */
  renameSource(sourceId: string, title: string): Promise<SourceOut>;
  /** Where the original now lies in the vault (the plugin saved a copy of a chat attachment there). */
  setSourceOrigin(sourceId: string, origin: string): Promise<SourceOut>;
  /**
   * PATCH …/sections (the material's 目录): new titles / levels; the server recomputes the parents and answers the source
   * with its sections. 409 while the source is still being read, 422 an unknown section or a level outside 0..2.
   */
  editSections(sourceId: string, edits: SectionEditIn[]): Promise<SourceDetail>;
  /** POST …/retry: a failed import runs again (202, reading); 409 when it needs none. */
  retrySource(sourceId: string): Promise<SourceOut>;
  /** Soft delete (204); a running import is stopped. */
  deleteSource(sourceId: string): Promise<void>;
  /** The original file's bytes (for pasted text: its UTF-8). */
  sourceFile(sourceId: string): Promise<ArrayBuffer>;
  // 真题 (past exam questions)
  /** GET /wiki/pages/{id}/exam: the page's 真题练习 (the quiz asset + the questions drawn natively); 404 an unknown page. */
  pageExam(pageId: string): Promise<PageExamOut>;
  /**
   * POST /wiki/exam/items/{id}/self: 我答对了 / 没答对 / 我做完了. 404 an unknown item, 422 right / wrong on an item without an
   * answer; a report racing another for the same item can get 409 (the caller treats it as already recorded).
   */
  examSelf(itemId: string, result: ExamSelfIn["result"]): Promise<ExamSelfOut>;
}

type Method = WikiHttpRequest["method"];
const enc = encodeURIComponent;
const ORGANIZE_NAMES: ReadonlySet<string> = new Set(ORGANIZE_EVENTS);
const SOURCE_NAMES: ReadonlySet<string> = new Set(SOURCE_EVENTS);
const page = (pageId: string): string => `/wiki/pages/${enc(pageId)}`;
const section = (pageId: string, sid: string): string => `${page(pageId)}/sections/${enc(sid)}`;
const path = (pathId: string): string => `/wiki/paths/${enc(pathId)}`;
const patch = (patchId: string): string => `/wiki/rsi/patches/${enc(patchId)}`;
const asset = (assetId: string): string => `/wiki/assets/${enc(assetId)}`;
const node = (nodeId: string): string => `/wiki/nodes/${enc(nodeId)}`;
const source = (sourceId: string): string => `/wiki/sources/${enc(sourceId)}`;
/** The optional SourceUploadIn fields, in the order they are sent (absent / empty ones are not sent). */
const UPLOAD_FIELDS = ["title", "origin", "use", "goal_id", "page_id", "session_id"] as const;
/** `?base_version=<n>` when given (an integer ≥ 1); nothing when undefined. */
const based = (url: string, base: number | undefined): string => (base === undefined ? url : `${url}?base_version=${base}`);

/** The learner profile 「我」 (GET / PUT /wiki/profile): the note's text as the server keeps it, and his saved text merged. */
export class ProfileClient {
  constructor(private readonly config: () => WikiConfig, private readonly http: WikiHttp) {}
  get(): Promise<ProfileOut> { return jsonCall<ProfileOut, Method>(this.http, this.config(), "GET", "/wiki/profile"); }
  put(text: string, base: string | null): Promise<ProfileOut> { return jsonCall<ProfileOut, Method>(this.http, this.config(), "PUT", "/wiki/profile", { text, base }); }
  /** What a deleted line of 已经会的 took back counts again (its ledger row's undo). */
  undoRetract(actionId: string): Promise<unknown> { return jsonCall<unknown, Method>(this.http, this.config(), "POST", `/wiki/bot/actions/${encodeURIComponent(actionId)}/undo`); }
}

export class HttpWikiClient implements WikiApi {
  constructor(
    private readonly config: () => WikiConfig,
    private readonly http: WikiHttp,
    private readonly sse: StreamTransport,
  ) {}

  answerDraft(draftId: string, body: AnswersIn): Promise<GoalDraftOut> {
    const wire = body.skip ? { skip: true } : { answers: body.answers };
    return this.call("POST", `/wiki/goals/${enc(draftId)}/answers`, wire);
  }
  organize(pageId: string, on: (e: OrganizeEvent) => void, o: StreamOpts = {}): Promise<void> {
    return this.stream(`${page(pageId)}/organize${o.fresh === true ? "?fresh=true" : ""}`, undefined, ORGANIZE_NAMES, (m) => on(m as OrganizeEvent), o.signal);
  }
  openPage(pageId: string, opts: { rewrite?: boolean } = {}): Promise<PageOut> {
    return this.call("POST", `${page(pageId)}/open${opts.rewrite === true ? "?rewrite=1" : ""}`);
  }
  fold(pageId: string, sid: string, folded: boolean): Promise<SectionOut> {
    return this.call("POST", `${section(pageId, sid)}/fold`, { folded });
  }
  pin(pageId: string, sid: string, pinned: boolean): Promise<SectionOut> {
    return this.call("POST", `${section(pageId, sid)}/pin`, { pinned });
  }
  feedbackKnow(pageId: string, sid: string, artifactId?: string | null): Promise<FeedbackOut> {
    return this.call("POST", `${section(pageId, sid)}/feedback`, withArtifact({ kind: "know" }, artifactId));
  }
  answerCheck(pageId: string, sid: string, choice: number, artifactId?: string | null): Promise<CheckAnswerOut> {
    return this.call("POST", `${page(pageId)}/checks/${enc(sid)}`, withArtifact({ choice }, artifactId));
  }
  done(pageId: string): Promise<DoneOut> { return this.call("POST", `${page(pageId)}/done`); }
  expand(pageId: string): Promise<ShelveOut> { return this.call("POST", `${page(pageId)}/expand`); }
  collapse(pageId: string): Promise<ShelveOut> { return this.call("POST", `${page(pageId)}/collapse`); }
  catalog(): Promise<CatalogOut> { return this.call("GET", "/wiki"); }
  progress(): Promise<ProgressOut> { return this.call("GET", "/wiki/progress"); }
  async reviewFirst(body: ReviewFirstIn): Promise<void> { await this.call("POST", "/wiki/progress/review-first", body); }
  kGrid(): Promise<KGridOut> { return this.call("GET", "/wiki/k"); }
  declare(cell: string, value: Declared | null): Promise<DeclareOut> {
    return this.call("POST", `/wiki/k/cells/${enc(cell)}/declare`, { value });
  }
  getSettings(): Promise<SettingsIO> { return this.call("GET", "/wiki/settings"); }
  putSettings(s: SettingsPut): Promise<SettingsIO> {
    const wire: Record<string, unknown> = {};
    if (s.review_per_day !== undefined) wire.review_per_day = s.review_per_day;
    if (s.language !== undefined) wire.language = s.language;
    // null is sent (it removes the cap); absent leaves the server's value.
    if (s.page_limit_usd !== undefined) wire.page_limit_usd = s.page_limit_usd;
    return this.call("PUT", "/wiki/settings", wire);
  }
  guidelines(): Promise<GuidelinesOut> { return this.call("GET", "/wiki/guidelines"); }
  addGuideline(body: GuidelineIn): Promise<GuidelineOut> { return this.call("POST", "/wiki/guidelines", { text: body.text }); }
  deleteGuideline(id: string): Promise<void> { return this.call("DELETE", `/wiki/guidelines/${enc(id)}`); }
  getPath(pathId: string): Promise<PathOut> { return this.call("GET", path(pathId)); }
  pathNext(pathId: string, key?: string): Promise<PathNextOut> { return this.call("POST", `${path(pathId)}/next`, key !== undefined ? { key } : undefined); }
  pathPageDone(pathId: string, index: number): Promise<PathPageDoneOut> {
    return this.call("POST", `${path(pathId)}/pages/${index}/done`);
  }
  reviewToday(more = false): Promise<ReviewTodayOut> {
    return this.call("GET", more ? "/wiki/review/today?more=true" : "/wiki/review/today");
  }
  reviewPage(pageId: string): Promise<ReviewTodayOut> { return this.call("GET", `/wiki/review/page/${enc(pageId)}`); }
  gradeReview(b: ReviewGradeIn): Promise<ReviewGradeOut> {
    const wire: Record<string, unknown> = { cell: b.cell };
    if ("grade" in b) wire.grade = b.grade; else wire.choice = b.choice;
    if (b.cells !== undefined && b.cells.length > 1) wire.cells = b.cells;
    if (b.page_round === true) wire.page_round = true;
    if (b.page_id !== null) wire.page_id = b.page_id;
    if (b.section_id !== null) wire.section_id = b.section_id;
    return this.call("POST", "/wiki/review/grade", withArtifact(wire, b.artifact_id));
  }

  pageFeedback(pageId: string, b: PageFeedbackIn): Promise<PageFeedbackOut> {
    const wire: Record<string, unknown> = { chips: b.chips, text: b.text, section_id: b.section_id };
    if (b.apply !== undefined) wire.apply = b.apply;
    return this.call("POST", `${page(pageId)}/feedback`, withArtifact(wire, b.artifact_id));
  }
  undoFeedback(pageId: string, feedbackId: string): Promise<FeedbackUndoOut> {
    return this.call("POST", `${page(pageId)}/feedback/${enc(feedbackId)}/undo`);
  }
  pageFit(pageId: string): Promise<SignalCreatedOut> { return this.call("POST", `${page(pageId)}/fit`); }
  handEdit(pageId: string, b: HandEditIn): Promise<HandEditOut> { return this.call("POST", `${page(pageId)}/hand-edit`, b); }
  undoHandEdit(pageId: string, editId: string): Promise<HandEditOut> { return this.call("POST", `${page(pageId)}/hand-edit/${enc(editId)}/undo`); }
  rsiChanges(): Promise<RsiChangeOut[]> { return this.call("GET", "/wiki/rsi/changes"); }
  rsiSwitch(): Promise<RsiSwitch> { return this.call("GET", "/wiki/rsi/switch"); }
  putRsiSwitch(on: boolean): Promise<RsiSwitch> { return this.call("PUT", "/wiki/rsi/switch", { on }); }
  engine(): Promise<EngineOut> { return this.call("GET", "/wiki/engine"); }
  prompts(): Promise<PromptStepOut[]> { return this.call("GET", "/wiki/prompts"); }
  putPrompt(step: string, text: string): Promise<PromptStepOut> { return this.call("PUT", `/wiki/prompts/${enc(step)}`, { text }); }
  resetPrompt(step: string): Promise<PromptStepOut> { return this.call("DELETE", `/wiki/prompts/${enc(step)}`); }
  undoPatch(patchId: string): Promise<RsiChangeOut> { return this.call("POST", `${patch(patchId)}/undo`); }
  approvePatch(patchId: string): Promise<RsiChangeOut> { return this.call("POST", `${patch(patchId)}/approve`); }
  discardPatch(patchId: string): Promise<RsiChangeOut> { return this.call("POST", `${patch(patchId)}/discard`); }
  followup(pageId: string, sid: string, b: CheckFollowupIn): Promise<CheckFollowupOut> {
    const wire: Record<string, unknown> = { choice: b.choice };
    if (b.answer_ref !== undefined && b.answer_ref !== null) wire.answer_ref = b.answer_ref;
    return this.call("POST", `${page(pageId)}/checks/${enc(sid)}/followup`, withArtifact(wire, b.artifact_id));
  }
  confirmAnswer(followupId: string, b: ConfirmAnswerIn): Promise<ConfirmAnswerOut> {
    return this.call("POST", `/wiki/review/confirms/${enc(followupId)}`, withArtifact({ choice: b.choice }, b.artifact_id));
  }
  botSuggestions(placement: "today" | "page", pageId?: string): Promise<SuggestionOut[]> {
    const q = new URLSearchParams({ placement });
    if (pageId !== undefined) q.set("page_id", pageId);
    return this.call("GET", `/wiki/bot/suggestions?${q.toString()}`);
  }
  botReact(id: string, reaction: Reaction): Promise<SuggestionOut> {
    return this.call("POST", `/wiki/bot/suggestions/${enc(id)}/react`, { reaction });
  }
  botReviewReminder(): Promise<ReviewReminderOut> {
    return this.call("GET", "/wiki/bot/review-reminder");
  }
  botReviewReact(reaction: ReviewReaction): Promise<ReviewReminderOut> {
    return this.call("POST", "/wiki/bot/review-reminder", { reaction });
  }
  botActions(status?: string): Promise<ActionOut[]> {
    return this.call("GET", status === undefined ? "/wiki/bot/actions" : `/wiki/bot/actions?${new URLSearchParams({ status }).toString()}`);
  }
  botDecide(id: string, op: "approve" | "reject" | "undo"): Promise<ActionOut> {
    return this.call("POST", `/wiki/bot/actions/${enc(id)}/${op}`);
  }
  botRedraft(id: string): Promise<ActionOut> {
    return this.call("POST", `/wiki/bot/actions/${enc(id)}/redraft`);
  }
  botWhy(q: { triggerId: string } | { suggestionId: string }): Promise<WhyOut> {
    const p = "triggerId" in q ? new URLSearchParams({ trigger_id: q.triggerId }) : new URLSearchParams({ suggestion_id: q.suggestionId });
    return this.call("GET", `/wiki/bot/why?${p.toString()}`);
  }
  botRecent(skipped: boolean): Promise<ActivityOut[]> {
    return this.call("GET", `/wiki/bot/recent?${new URLSearchParams({ days: "14", skipped: String(skipped) }).toString()}`);
  }
  async writes(pageIds?: string[]): Promise<WriteOut[]> {
    const q = pageIds === undefined ? "" : `?${new URLSearchParams({ page_ids: pageIds.join(",") }).toString()}`;
    const r: unknown = await this.call("GET", `/wiki/writes${q}`);
    if (!Array.isArray(r)) throw new ContractError("writes is not a list");
    return r.map(writeOut);
  }
  getPage(pageId: string): Promise<PageOut> { return this.call("GET", page(pageId)); }
  async files(): Promise<FileOut[]> {
    const r: unknown = await this.call("GET", "/wiki/files");
    if (!Array.isArray(r)) throw new ContractError("files is not a list");
    return r.map(fileOut);
  }
  runStream(pageId: string, runId: string, on: (e: OrganizeEvent) => void, o: StreamOpts = {}): Promise<void> {
    return this.stream(`${page(pageId)}/run/stream?run_id=${encodeURIComponent(runId)}`, undefined, ORGANIZE_NAMES, (m) => on(m as OrganizeEvent), o.signal, "GET");
  }
  cancelRun(pageId: string): Promise<void> { return this.call("POST", `${page(pageId)}/run/cancel`); }
  botBadge(): Promise<BadgeOut> { return this.call("GET", "/wiki/bot/badge"); }
  botSettings(): Promise<BotSettings> { return this.call("GET", "/wiki/bot/settings"); }
  reviseFromSource(sourceId: string, pageIds: string[]): Promise<SourceReviseOut> {
    return this.call("POST", `/wiki/sources/${enc(sourceId)}/revise`, { page_ids: pageIds });
  }
  putBotSettings(s: BotSettings): Promise<BotSettings> { return this.call("PUT", "/wiki/bot/settings", s); }
  botPause(until: PauseUntil): Promise<BotSettings> { return this.call("POST", "/wiki/bot/pause", { until }); }
  botGoals(): Promise<BotGoalOut[]> { return this.call("GET", "/wiki/bot/goals"); }
  botPark(pathId: string, park: boolean): Promise<BotGoalOut> {
    return this.call("POST", `/wiki/bot/goals/${enc(pathId)}/${park ? "park" : "resume"}`);
  }
  replanPath(pathId: string): Promise<PathEditOut> { return this.call("POST", `${path(pathId)}/replan`); }
  postReading(batchId: string, entries: ReadingEntry[]): Promise<BotReadingOut> {
    return this.call("POST", "/wiki/reading", { batch_id: batchId, entries });
  }
  components(): Promise<ComponentOut[]> { return this.call("GET", "/wiki/components"); }
  graph(): Promise<GraphOut> { return this.call("GET", "/wiki/graph"); }
  splitNode(nodeId: string): Promise<SplitOut> { return this.call("POST", `${node(nodeId)}/split`); }
  wantNode(nodeId: string, mapId: string): Promise<NodeActionOut> { return this.call("POST", `${node(nodeId)}/want`, { map_id: mapId }); }
  editPath(pathId: string, edit: PathEditIn): Promise<PathEditOut> {
    const wire: Record<string, unknown> = edit.op === "move" ? { op: "move", from: edit.from, to: edit.to }
      : edit.op === "remove" ? { op: "remove", index: edit.index } : edit.op === "unremove" ? { op: "unremove", page_key: edit.page_key }
      : edit.op === "retitle" ? { op: "retitle", page_key: edit.page_key, title: edit.title }
      : { op: "add", title: edit.title };
    if (edit.op === "add" && edit.node_id !== undefined) wire.node_id = edit.node_id;
    if (edit.op === "add" && edit.to_index !== undefined) wire.to_index = edit.to_index;
    return this.call("POST", `${path(pathId)}/edits`, wire);
  }
  splitRow(pathId: string, pageKey: string): Promise<PathEditOut> { return this.call("POST", `${path(pathId)}/rows/${enc(pageKey)}/split`); }
  undoSplitRow(pathId: string, pageKey: string, changeId: string): Promise<PathEditOut> {
    return this.call("POST", `${path(pathId)}/rows/${enc(pageKey)}/split/undo`, { change_id: changeId });
  }
  async memoryConflicts(): Promise<MemoryConflictOut[]> { return parseConflicts(await this.call("GET", MEMORY_CONFLICTS_PATH)); }
  teachSpec(): Promise<TeachSpecOut> { return this.call("GET", "/wiki/teach/spec"); }
  addSpecItem(b: SpecItemIn, base?: number): Promise<TeachSpecOut> {
    return this.call("POST", based("/wiki/teach/items", base), { text: b.text, scope: b.scope, domain: b.domain });
  }
  editSpecItem(itemId: string, b: SpecItemPatchIn, base?: number): Promise<TeachSpecOut> {
    const wire: Record<string, unknown> = {};
    if (b.text !== undefined) wire.text = b.text;
    if (b.scope !== undefined) wire.scope = b.scope;
    if (b.domain !== undefined) wire.domain = b.domain;
    return this.call("PATCH", based(`/wiki/teach/items/${enc(itemId)}`, base), wire);
  }
  deleteSpecItem(itemId: string, base?: number): Promise<TeachSpecOut> {
    return this.call("DELETE", based(`/wiki/teach/items/${enc(itemId)}`, base));
  }
  takeOverSpecItem(itemId: string, base?: number): Promise<TeachSpecOut> {
    return this.call("POST", based(`/wiki/teach/items/${enc(itemId)}/take_over`, base));
  }
  addExemplar(b: ExemplarIn, base?: number): Promise<TeachSpecOut> {
    const wire = b.kind === "wiki_page" ? { kind: b.kind, ref: b.ref } : { kind: b.kind, ref: b.ref, title: b.title, text: b.text };
    return this.call("POST", based("/wiki/teach/exemplars", base), wire);
  }
  deleteExemplar(exemplarId: string, base?: number): Promise<TeachSpecOut> {
    return this.call("DELETE", based(`/wiki/teach/exemplars/${enc(exemplarId)}`, base));
  }
  assetMeta(assetId: string): Promise<AssetMeta> { return this.call("GET", `${asset(assetId)}/meta`); }
  assetGenerated(text: string, kind: "evoui" | "html"): Promise<AssetGeneratedOut> { return this.call("POST", "/wiki/assets/generated", { text, kind }); }
  async assetHtml(assetId: string): Promise<string> {
    // no-cache: requestUrl keeps Chromium's HTTP cache, and the server sends max-age; an id reused by another database
    // (a fresh dev / test DB numbers from ia_0001 again) would otherwise come back as the old body and fail the sha256
    const headers = { ...this.headers(false), "Cache-Control": "no-cache" };
    const res = await this.http.request({ method: "GET", url: this.config().baseUrl + asset(assetId), headers });
    if (res.status < 200 || res.status >= 300) throw apiErrorOf(res.status, res.text);
    return res.text;
  }
  async blockEvents(assetId: string, events: readonly BlockEvent[]): Promise<{ inK: boolean }> {
    const out = await this.call<BlockResultsOut>("POST", `${asset(assetId)}/results`, { events });
    return { inK: out.events.some((e) => e.status === "k") };
  }
  blockProgress(assetId: string): Promise<BlockProgressOut> { return this.call("GET", `${asset(assetId)}/progress`); }
  async saveBlockProgress(assetId: string, progress: BlockProgressIn): Promise<{ stale: boolean }> {
    const out = await this.call<BlockResultsOut>("POST", `${asset(assetId)}/results`, { progress });
    return { stale: out.progress_stale };
  }

  listSources(): Promise<SourceOut[]> { return this.call("GET", "/wiki/sources"); }
  getSource(sourceId: string): Promise<SourceDetail> { return this.call("GET", source(sourceId)); }
  pageExam(pageId: string): Promise<PageExamOut> { return this.call("GET", `${page(pageId)}/exam`); }
  examSelf(itemId: string, result: ExamSelfIn["result"]): Promise<ExamSelfOut> {
    return this.call("POST", `/wiki/exam/items/${enc(itemId)}/self`, { result });
  }
  async uploadSource(input: SourceUploadIn): Promise<SourceOut> {
    const fields: [string, string][] = [];
    for (const k of UPLOAD_FIELDS) {
      const v = input[k];
      if (v !== undefined && v !== "") fields.push([k, v]);
    }
    if (input.url !== undefined) fields.push(["url", input.url]);
    if (input.text !== undefined) fields.push(["text", input.text]);
    const f = input.file;
    const { body, contentType } = buildMultipart(fields, f && { field: "file", name: f.name, data: f.data, type: f.type });
    const headers = { ...this.headers(false), "Content-Type": contentType };
    const res = await this.http.request({ method: "POST", url: this.config().baseUrl + "/wiki/sources", headers, body, contentType });
    if (res.status < 200 || res.status >= 300) throw apiErrorOf(res.status, res.text);
    try { return JSON.parse(res.text) as SourceOut; } catch {
      throw new ApiError(res.status, `invalid JSON response: ${res.text.slice(0, 200)}`);
    }
  }
  followSource(sourceId: string, on: (e: SourceEvent) => void, o: StreamOpts = {}): Promise<void> {
    return this.stream(`${source(sourceId)}/progress`, undefined, SOURCE_NAMES, (m) => on(m as SourceEvent), o.signal, "GET");
  }
  addSourceUse(sourceId: string, u: SourceUseIn): Promise<SourceUseOut> {
    const wire: Record<string, unknown> = { use: u.use };
    for (const k of ["goal_id", "page_id", "session_id"] as const) if (u[k] !== undefined && u[k] !== null) wire[k] = u[k];
    return this.call("POST", `${source(sourceId)}/uses`, wire);
  }
  renameSource(sourceId: string, title: string): Promise<SourceOut> { return this.call("PATCH", source(sourceId), { title }); }
  setSourceOrigin(sourceId: string, origin: string): Promise<SourceOut> { return this.call("PATCH", source(sourceId), { origin }); }
  editSections(sourceId: string, edits: SectionEditIn[]): Promise<SourceDetail> {
    return this.call("PATCH", `${source(sourceId)}/sections`, edits.map((e) => ({ id: e.id,
      ...(e.title !== undefined ? { title: e.title } : {}), ...(e.depth !== undefined ? { depth: e.depth } : {}) })));
  }
  retrySource(sourceId: string): Promise<SourceOut> { return this.call("POST", `${source(sourceId)}/retry`); }
  deleteSource(sourceId: string): Promise<void> { return this.call("DELETE", source(sourceId)); }
  async sourceFile(sourceId: string): Promise<ArrayBuffer> {
    const res = await this.http.request({ method: "GET", url: this.config().baseUrl + `${source(sourceId)}/file`,
      headers: this.headers(false), binary: true });
    if (res.status < 200 || res.status >= 300) throw apiErrorOf(res.status, res.text);
    if (res.bytes === undefined) throw new ApiError(res.status, "the server sent no file bytes");
    return res.bytes;
  }

  private headers(json: boolean): Record<string, string> {
    const h: Record<string, string> = { "X-EvoWiki-User": this.config().userId };
    if (json) h["Content-Type"] = "application/json";
    return h;
  }

  private call<T>(method: Method, path: string, body?: unknown): Promise<T> {
    return jsonCall<T, Method>(this.http, this.config(), method, path, body);
  }

  /**
   * An SSE request (POST with an optional JSON body, or GET). Same containment as EvoWikiClient.stream: onChunk runs inside the transport's data callback,
   * where a throw would be uncaught, so the first failure is recorded, later chunks are ignored, and it is rethrown after
   * the transport resolves. Only names in `allowed` reach `onMsg`; nothing is delivered after `done` or `error`.
   */
  private async stream(path: string, body: unknown, allowed: ReadonlySet<string>, onMsg: (m: SseMessage) => void,
                       signal?: AbortSignal, method: "GET" | "POST" = "POST"): Promise<void> {
    const dec = new SseDecoder();
    let failure: { error: unknown } | undefined;
    let finished = false;
    const onChunk = (chunk: string): void => {
      if (failure || finished) return;
      let msgs: SseMessage[];
      try { msgs = dec.push(chunk); } catch {
        failure = { error: new Error("malformed event stream") };
        return;
      }
      for (const m of msgs) {
        if (!allowed.has(m.event)) continue;
        if (m.event === "done" || m.event === "error") finished = true;
        try { onMsg(m); } catch (e) {
          failure = { error: e };
          return;
        }
        if (finished) return;
      }
    };
    const res = await this.sse.stream({ method, url: this.config().baseUrl + path, headers: this.headers(body !== undefined),
      body: body === undefined ? undefined : JSON.stringify(body), signal }, onChunk);
    if (res.status < 200 || res.status >= 300) throw apiErrorOf(res.status, res.text);
    if (failure) throw failure.error;
  }
}

/** One entry of GET /wiki/files, checked (a malformed one is a contract error, not a guess). */
export function fileOut(x: unknown): FileOut {
  if (x === null || typeof x !== "object" || Array.isArray(x)) throw new ContractError("FileOut is not an object");
  const o = x as Record<string, unknown>;
  if (typeof o.page_id !== "string" || typeof o.title !== "string" || typeof o.folder !== "string"
    || typeof o.parent_name !== "string" || typeof o.trashed !== "boolean" || typeof o.organized !== "boolean") {
    throw new ContractError("FileOut has a missing or mistyped field");
  }
  return { page_id: o.page_id, title: o.title, folder: o.folder, parent_name: o.parent_name, trashed: o.trashed, organized: o.organized };
}
