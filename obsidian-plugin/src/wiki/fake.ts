/**
 * FakeWikiApi: an in-memory /wiki server following the v5 prototype scenario (fixtures.ts). Used by the demo
 * command and by tests. Every value it returns or emits carries exactly the pydantic field set of
 * wiki_schemas.py (schema-parity.test.ts checks this), and is a fresh copy.
 */
import type { FileOut } from "./schema";
import { AbortedError, ApiError, OfflineError } from "../api/errors";
import type { WriteOut } from "./schema";
import type { StreamOpts, WikiApi } from "./api";
import type { BlockEvent, BlockProgressIn, BlockProgressOut } from "./evoui/block-event";
import type {
  ActionOut, ActivityOut, BadgeOut, BotSettings, BotGoalOut, PauseUntil, Reaction, ReadingEntry, ReviewReaction, ReviewReminderOut, BotReadingOut, SourceReviseOut, SuggestionOut, WhyOut,
} from "./bot/bot-schema";
import {
  CONCEPTS, DEFAULT_SPEC_ITEMS, embedBlock, ENGINE_PARAMS, ENGINE_PROMPTS, PROMPT_STEPS, DEMO_ASSET, DEMO_INTERACTIVE_HTML, DRAFT_QUESTIONS, FAKE_WRITER,
  INITIAL_BANDS, MU, OUTLINE_P1, OUTLINE_PCA, P1_TITLE, PATH_GOAL_HINT, PATH_PAGES, RSI_CHANGES,
  BOT_ACTIONS, BOT_ACTIVITY, BOT_SUGGESTIONS, BOT_WHY, DEFAULT_BOT_SETTINGS,
  type DraftQuestionFixture, type OutlineFixture, type SectionFixture,
} from "./fixtures";
import { FakeSourceStore } from "./fake-source-store";
import { GraphFixture } from "./graph-page/fixture";
import type { NodeActionOut } from "./graph-page/parse";
import {
  CHIP_LABEL, cellOf, FEEDBACK_CHIPS, FOLDABLE_ROLES, GRADES, IDK, LANGUAGES, MAX_DOMAIN, MAX_EXEMPLAR_REF, MAX_EXEMPLAR_TEXT,
  MAX_EXEMPLAR_TITLE, MAX_EXEMPLARS, MAX_ITEM_TEXT, MAX_NOTE_SUMMARY,
  MAX_PAGE_FEEDBACK_TEXT, MAX_SPEC_ITEMS, SPEC_SCOPES, splitCell,
  type Exemplar, type FeedbackChip, type PatchStatus, type PrefetchOut, type RepParams, type SpecItem,
  type AnswersIn, type Aspect, type Band, type CatalogOut, type CellBeliefOut, type CellChangeOut, type CellOut,
  type Changed, type ChangeOut, type CheckAnswerOut, type CheckOut, type ConceptRole,
  type DeclareOut, type Declared, type DiffNoteOut, type DoneOut,
  type DraftConceptOut, type ErrorEvent, type ShelveOut, type FeedbackOut, type GoalDraftOut, type GuidelineIn,
  type GuidelineOut, type GuidelinesOut, type KGridOut, type NoteKind, type NoteOut,
  type OrganizeEvent, type PageCardOut, type PageOut,
  type SectionOut, type SectionRole, type SettingsIO, type EngineOut, type PathNextOut,
  type PathOut, type PathPageDoneOut, type ReviewGradeIn, type ReviewGradeOut, type ReviewTodayOut,
  type ConflictOut, type Grade, type PageState, type ProgressOut, type GoalProgressOut, type StepOut, type PathCardOut, type PathPageStatus,
  type ReviewItemOut, type ReviewMode, type SettingsPut, type GraphOut, type PathEditIn, type PathEditOut, type SplitOut,
  type AssetGeneratedOut, AssetMeta, type ExemplarIn, type HandEditIn, type HandEditOut, type SignalCreatedOut,
  type ComponentOut, type FeedbackMarkOut,
  type FeedbackUndoOut, type CheckFollowupIn, type CheckFollowupOut, type ConfirmAnswerIn, type ConfirmAnswerOut, type ConfirmCardOut, type ReviewTargetOut, FOLLOWUP_CHOICES, type PageFeedbackOut, type PromptStepOut, type PageFeedbackIn, type RsiChangeOut, type RsiStatus, type RsiSwitch, type SpecItemIn, type SpecItemPatchIn, type TeachSpecOut,
  type SectionEditIn, type SourceDetail, type SourceEvent, type SourceOut, type SourceUploadIn, type SourceUseIn, type SourceUseOut,
  type ExamSelfIn, type ExamSelfOut, type ExamSummaryOut, type PageExamOut, type RuleDecisionOut,
} from "./schema";

import type { MemoryConflictOut } from "./memory-conflicts";

type Failure = ApiError | OfflineError | ErrorEvent;

export interface FakeOptions {
  /** ms between streamed events; 0 in tests. */ delayMs?: number;
  /** Next call of the named method fails once with this (then behaves normally). */
  failNext?: Partial<Record<keyof WikiApi, ApiError | OfflineError | ErrorEvent>>;
  /** No model reachable (see also __llmDown). */
  llmUnavailable?: boolean;
  /** The clock (default: the real one): review "today" (UTC day), digest start / end. */
  now?: () => Date;
  /** Replaces the seeded Bot state (fixtures.ts BOT_*) piece by piece: the demo fixture's data. */
  bot?: Partial<{ suggestions: SuggestionOut[]; actions: ActionOut[]; activity: ActivityOut[]; why: Record<string, WhyOut>; settings: BotSettings }>;
  /** Replaces the seeded RSI change list (fixtures.ts RSI_CHANGES). */
  rsi?: RsiChangeOut[];
}

interface Concept { id: string; name: string; aspects: readonly Aspect[]; prereqs: readonly string[] }
const CONCEPT_LIST: readonly Concept[] = Object.values(CONCEPTS);
const CONCEPT_BY_ID = new Map<string, Concept>(CONCEPT_LIST.map((c) => [c.id, c]));
const STREAMING: ReadonlySet<keyof WikiApi> = new Set<keyof WikiApi>(["organize", "followSource"]);
/** Methods that never hit the usage limit (reads: no LLM call, nothing written). */
const READS: ReadonlySet<keyof WikiApi> = new Set<keyof WikiApi>([
  "openPage", "catalog", "kGrid", "getSettings", "guidelines", "getPath", "reviewToday",
  "rsiChanges", "rsiSwitch", "teachSpec", "memoryConflicts", "assetMeta", "assetGenerated", "assetHtml", "blockProgress", "components", "engine", "prompts", "graph",
  "botSuggestions", "botActions", "botWhy", "botRecent", "botBadge", "botSettings", "botGoals", "writes", "files", "runStream", "getPage",
  "listSources", "getSource", "followSource", "sourceFile", "pageExam",
]);
/**
 * Writes that never reach the LLM on the real server (the 429 only comes from the gateway before a provider call), so
 * the usage limit does not block them either.
 */
const NO_LLM: ReadonlySet<keyof WikiApi> = new Set<keyof WikiApi>([
  "pageFeedback", "pageFit", "handEdit", "undoHandEdit", "putRsiSwitch", "undoPatch", "approvePatch", "discardPatch", "addSpecItem", "editSpecItem", "deleteSpecItem", "takeOverSpecItem",
  "addExemplar", "deleteExemplar", "undoFeedback", "blockEvents", "saveBlockProgress", "confirmAnswer",
  "wantNode", "editPath", "undoSplitRow",
  "putPrompt", "resetPrompt", "botReact", "botReviewReact", "botDecide", "botRedraft", "putBotSettings", "reviseFromSource", "botPause", "botPark", "postReading",
  // the upload itself never calls the model (the import runs after it)
  "uploadSource", "addSourceUse", "renameSource", "setSourceOrigin", "deleteSource", "editSections", "cancelRun",
  // 真题 self-reports never call the model
  "examSelf",
]);
/** The fake's stand-ins for model-written text (the real content comes from the backend). */
/** The effect line of a change the fake just applied (the backend's 「数据还在收集」 with the change's real after-side count, 0 right after it applied). */
const FEEDBACK_PATCH_SENTENCE = "（按你的反馈补充）这里再多讲一步：先看一个具体的数，再看公式。";
const DEFAULT_STYLE_BEFORE = "按默认的讲法写。";
const REVIEW_REWRITTEN_PREFIX = "（新版）";
const QUOTE_LEN = 40;
/** GoalDraftOut fields for a goal that is not waiting on a round (and no map): spread, then set the outcome. */
const NO_ROUND: Pick<GoalDraftOut, "draft_id" | "page_id" | "path_id" | "map_id" | "round" | "round_title" | "round_why"
  | "results" | "map" | "decisions"> = {
  draft_id: null, page_id: null, path_id: null, map_id: null, round: 0, round_title: "", round_why: "", results: [], map: null,
  decisions: [],
};
const FAKE_COMPONENTS: readonly ComponentOut[] = [
  { component_id: "slider", name: "滑块看变化", description: "拖动滑块，直接看数字变了以后结果怎么变",
    learning_question: "把数字调大调小，结果会怎么变？", params: { min: "滑块最小值", max: "滑块最大值", step: "每次变化多少" }, height: 480,
    scenarios: [], configurable: false, config_schema: null, example_configs: [] },
  { component_id: "step-by-step", name: "一步一步算", description: "把一个计算拆成几步，一次看一步",
    learning_question: "这个计算每一步在做什么？", params: { steps: "要拆成几步" }, height: 440,
    scenarios: [], configurable: false, config_schema: null, example_configs: [] },
];
const FEELING: Partial<Record<FeedbackChip, string>> = { too_shallow: "太浅", too_hard: "有点难" };
const feelingOf = (chip: FeedbackChip): string => FEELING[chip] ?? CHIP_LABEL[chip];
/** One page-feedback patch the fake applied (wikiapp/feedback_loop.py): what it added where, and what undo restores. */
interface FeedbackRec {
  page_id: string; feedback_id: string; at: string; summary: string; reading: string;
  /** Sections it appended to: the record before, the paragraph it added, the body it left. */
  patched: { before: SectionRec; add: string; left: string }[];
  /** Component sections it inserted (section ids) and their asset ids. */
  inserted: string[]; assets: string[]; undone: boolean;
}

/** The retired RSI-B undo's reason appended to `why` (old track-B rows of the fixtures). */
const ROLLBACK_REASON = "你撤销了这条改动";
/** layer2.outcomes.effect_line of a change the user undid (the final point's note user_undo). */
const USER_UNDO_LINE = "你撤销了";
/** llm/usage.py BudgetExceededError.line. */
const BUDGET_DETAIL = "今天的花费到了上限 $1.00，这次没有完成。明天 0 点恢复，也可以在设置里调高。";
/** The server's display name for a page id it cannot resolve (views.names_out). */
const UNNAMED_PAGE = "未命名页面";

/** Inlined on purpose (the fake does not depend on format.ts). */
const ASPECT_FULL: Record<Aspect, string> = {
  term: "术语 / 定义", purpose: "用途 / 何时用", notation: "公式 / 记号", procedure: "计算过程",
  interpret: "解读结果", mechanism: "原理 / 推导" };
const BAND_ZH: Record<Band, string> = { known: "确定会", likely: "可能会", shaky: "模糊", gap: "不会", none: "还没测过" };
const UP: Record<Band, Band> = { gap: "shaky", shaky: "likely", likely: "known", known: "known", none: "likely" };
const DOWN: Record<Band, Band> = { known: "likely", likely: "shaky", shaky: "gap", gap: "gap", none: "gap" };
/** Band order for "below likely" (none counts as gap). Progressed / slipped skip none (not a tier; backend goals/review.py). */
const RANK: Record<Band, number> = { none: 0, gap: 0, shaky: 1, likely: 2, known: 3 };
const DAY_MS = 86_400_000;
const utcDay = (d: Date): string => d.toISOString().slice(0, 10);

interface SectionRec {
  fx: SectionFixture; role: SectionRole; cue: string; body: string; noteBody?: string; folded: boolean; fold_locked: boolean;
  pinned: boolean; changed: Changed | null; check: CheckOut | null;
}
interface PageRec {
  id: string; title: string; goal: string; target: string; outline: OutlineFixture; organized: boolean;
  sections: SectionRec[]; collapsed: boolean; done: boolean; seen: Map<string, Band>;
}
/** One answer to a check / review question (the evidence a follow-up is about); `ref` is the `answer_ref` the API returned. */
interface AnswerRec {
  ref: string; pageId: string; sid: string; cell: string; recall: boolean; choice: number | null; wrong: boolean;
  stem: string; options: string[]; answer: number; cue: string; pick: string; followed: boolean;
}
/** A scheduled confirmation question of a slip / learned follow-up. */
interface FollowupRec {
  id: string; kind: "slip" | "learned"; cell: string; pageId: string; sid: string; stem: string; options: string[]; answer: number;
  due: Date; outcome: "confirmed" | "refuted" | null; teach: ReviewTargetOut | null;
}
/** The confirmation question comes this long after the follow-up (backend CONFIRM_DELAY). */
const CONFIRM_DELAY_MS = 3_600_000;
interface DraftRec { id: string; goal: string; questions: DraftQuestionFixture[] }
interface PathRec {
  id: string; goal: string; pages: { aim: string; targets: string[]; uses: string[]; pageId: string | null }[];
  /** The next page written ahead (prefetchNext); claimed by pathNext. */
  prefetch?: { index: number; pageId: string; status: PrefetchOut["status"] } | null;
}
/** Every band change (for the digest); forgot: caused by a 没想起来 self-grade. */
interface BandLogRec { at: Date; cell: string; from: Band; to: Band; forgot: boolean }
/** Every review: grade null = a check answered from the review list (counted in done_today, as the server's reviews_done). */
interface ReviewLogRec { at: Date; cells: string[]; grade: Grade | null }

const http = (status: number, detail: string): ApiError => new ApiError(status, detail);
/** B3: a card takes accepted / not_interested while it is open (pending / shown), `done` only after accepted; every other repeat is a 409. */
const BOT_OPEN: ReadonlySet<string> = new Set(["pending", "shown"]);
/** B3 approval levels a user may choose per scope; a scope not listed in either table cannot be set. */
const BOT_CHOICES: Readonly<Record<string, readonly string[]>> = {
  page_body: ["ask", "auto_undo"], new_page: ["ask", "auto_undo"], delete_merge: ["ask", "auto_undo"], structure: ["ask", "auto_undo"],
  teaching_params: ["ask", "auto_undo"], rsi: ["ask", "auto_undo"], path: ["ask", "auto_undo"] };
/**
 * actions.py can_undo: the one rule behind ActionOut.can_undo and the ledger rows of the change list (the plugin never computes it).
 * By kind, as actions.py registers them; every other kind (actions.py's default and the kinds it registers as "while its undo
 * data exists") can be undone, since an executed row of this fake always keeps its undo data.
 */
function botCanUndo(a: ActionOut): boolean {
  if (a.status !== "executed") return false;
  switch (a.kind) {
    case "rsi_patch": case "rsi_method": return true;            // the executor undoes the RSI change itself
    case "bot_write": return a.content.picks === undefined;      // the 「要写 N 页」 batch has no undo data
    default: return true;
  }
}
/** store/bot.py APPROVED_STALE: an approved row older than this waits for the user again. */
const BOT_APPROVED_STALE_MS = 2 * 60_000;
/** Fixed scopes: user_notes never, suggestion auto, preference auto_undo. */
const BOT_FIXED: Readonly<Record<string, string>> = { user_notes: "never", suggestion: "auto", preference: "auto_undo" };
/** bot/settings.py Field bounds [lo, hi]. */
const BOT_BOUNDS = { today_cap: [0, 50], page_cap: [0, 20], wrong_count: [2, 20], wrong_days: [1, 60], idle_days: [1, 60],
  open_question_days: [1, 60], daily_hour: [0, 23], daily_minute: [0, 59], cooldown_hours: [0.001, 720], failure_streak: [1, 20] } as const;
/** schedule.local_day_bounds(now, tz)[1]: the next local midnight, as an ISO instant. */
function localDayEnd(now: Date, tz: string): string {
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit",
    minute: "2-digit", second: "2-digit", hourCycle: "h23" }).formatToParts(now);
  const n = (t: string): number => Number(parts.find((p) => p.type === t)?.value);
  const offset = Date.UTC(n("year"), n("month") - 1, n("day"), n("hour"), n("minute"), n("second")) - Math.floor(now.getTime() / 1000) * 1000;
  return new Date(Date.UTC(n("year"), n("month") - 1, n("day") + 1) - offset).toISOString();
}
/** ReviewItemOut.card_id: stable per (page, section, mode). */
const cardIdOf = (pageId: string, sid: string, mode: ReviewMode): string => `rc_${pageId}_${sid}_${mode}`;
const foldable = (role: SectionRole): boolean => FOLDABLE_ROLES.has(role);

function change(part: Partial<ChangeOut> = {}): ChangeOut {
  return { rewritten: [], inserted: [], moved: [], folded: [], unfolded: [], llm_calls: 0, other_pages_stale: [],
    unresolved_conflicts: [], conflicts: [], budget_exceeded: false, ...part };
}
/** One recorded page_feedback / note_edit signal (FakeWikiApi.__signals). */
export interface FakeSignal {
  kind: "page_feedback" | "page_fit" | "note_edit" | "card_feedback" | "check_unclear"; page_id: string; section_id: string | null; chips: FeedbackChip[]; text: string;
}
/** teach/contracts.py PageFeedbackIn.chips max_length. */
const MAX_FEEDBACK_CHIPS = 6;
/** teach/contracts.py RepParams defaults. */
const DEFAULT_PARAMS: RepParams = { recap_max_per_page: 4, analogy_level: 1, fading_level: 1, interactive_bias: 1 };
/** wikiapp/teachspec.py. */
/** bot/prompts.py: the reasons a prompt cannot be saved (422). */
const PROMPT_EMPTY = "写法不能是空的。";
const PROMPT_TOO_LONG = "写法太长了，最多 4000 个字。";
const PROMPT_MARKERS = "写法里不能有 ⟦ 或 ⟧。";
const PROMPT_FORMAT = "这里只写怎么做；输出的格式是固定的，不能在这里改。";
const PROMPT_FORMAT_WORDS = ["json", "字段", "输出格式", "格式要求"];
const TOO_MANY_EXEMPLARS = `范例最多 ${MAX_EXEMPLARS} 个`;
/** The spec as stored (the params never change here: RepParams is out of scope). */
interface SpecRec { version: number; items: SpecItem[]; exemplars: Exemplar[] }

/** Who a fake rule comes from: the default spec, the user, or an RSI change. */
export type RuleKind = "default" | "user" | "rsi" | "pending";
/** The provenance fields (teach/contracts.py SpecItem) of a rule of that kind. */
export function provenanceOf(kind: RuleKind, ref: string | null = null):
    Pick<SpecItem, "origin" | "authority" | "rev_by" | "origin_ref"> {
  if (kind === "default") return { origin: "default", authority: "rsi", rev_by: "system", origin_ref: ref };
  if (kind === "user") return { origin: "user_ui", authority: "user", rev_by: "user", origin_ref: ref };
  // Memory's backfill (rules/authority.py SYSTEM_ADDS): waits in 个性化 · 待确认的偏好
  if (kind === "pending") return { origin: "memory_backfill", authority: "pending", rev_by: "system", origin_ref: ref };
  return { origin: (ref ?? "").startsWith("harness:") ? "rsi_h" : "rsi_b", authority: "rsi", rev_by: "rsi", origin_ref: ref };
}
function specItem(part: Pick<SpecItem, "item_id" | "text"> & Partial<SpecItem> & { kind: RuleKind }): SpecItem {
  const { kind, ...rest } = part;
  return { scope: "any", domain: null, ...provenanceOf(kind, rest.origin_ref ?? null), rev: 1, protected: false,
           helpful: 0, harmful: 0, ...rest };
}
/** SpecItemIn / SpecItemPatchIn text: 1–500, not blank (pydantic); stored stripped. */
function checkItemText(text: string | null): string {
  if (text === null || !text.trim()) throw http(422, "规则不能为空");
  if (text.length > MAX_ITEM_TEXT) throw http(422, `一条规则最多 ${MAX_ITEM_TEXT} 字`);
  return text.trim();
}
function checkScope(scope: string): void {
  if (!(SPEC_SCOPES as readonly string[]).includes(scope)) throw http(422, "未知的适用范围");
}
function checkDomain(domain: string | null): void {
  if (domain !== null && domain.length > MAX_DOMAIN) throw http(422, `领域最多 ${MAX_DOMAIN} 字`);
}
/** teachspec.norm_domain: stripped; empty → null. */
function normDomain(domain: string | null): string | null {
  const d = domain?.trim() ?? "";
  return d === "" ? null : d;
}
function itemIndex(s: SpecRec, itemId: string): number {
  const n = s.items.findIndex((i) => i.item_id === itemId);
  if (n < 0) throw http(404, `没有这条规则：${itemId}`);
  return n;
}

/** A stable `art_` + 24 hex id for a piece of fake content (the real ones are content hashes made by the server). */
export function fakeArtifactId(key: string): string {
  let h = 0x811c9dc5;
  let out = "";
  for (let r = 0; r < 3; r++) {
    for (const ch of `${r}:${key}`) h = Math.imul(h ^ ch.codePointAt(0)!, 0x01000193) >>> 0;
    out += h.toString(16).padStart(8, "0");
  }
  return `art_${out}`;
}

export class FakeWikiApi implements WikiApi {
  /** Every call, in order: method name + arguments (for tests). */
  readonly calls: { method: keyof WikiApi; args: unknown[] }[] = [];
  private readonly delayMs: number;
  private llmUnavailable: boolean;
  private budget = false;
  private handEdits = 0;
  private readonly failures = new Map<keyof WikiApi, Failure>();

  private readonly bands = new Map<string, Band>(Object.entries(INITIAL_BANDS));
  private readonly declared = new Map<string, Declared>();
  private readonly beforeDeclare = new Map<string, Band>();
  private readonly notes: NoteOut[] = [];
  private readonly pages = new Map<string, PageRec>();
  private currentPage: string | null = null;
  private readonly drafts = new Map<string, DraftRec>();
  /** Pages written ahead (prefetchNext "ready") whose run organize has not replayed yet. */
  private readonly readyAhead = new Set<string>();
  private settings: SettingsIO = { review_per_day: 10, page_limit_usd: null, language: "zh" };
  private readonly guidelineList: GuidelineOut[] = [];
  private seq = { draft: 0, note: 0, guideline: 0, path: 0, signal: 0, specItem: 0, exemplar: 0,
    feedback: 0, feedbackPatch: 0, component: 0, asset: 0, answer: 0, followup: 0 };
  private readonly now: () => Date;
  private readonly paths = new Map<string, PathRec>();
  private readonly bandLog: BandLogRec[] = [];
  private readonly reviewLog: ReviewLogRec[] = [];
  private pageRoundAnswers = 0;
  private readonly answers: AnswerRec[] = [];
  private readonly followups: FollowupRec[] = [];
  private readonly signals: FakeSignal[] = [];
  private readonly feedbacks: FeedbackRec[] = [];
  /** Card ids hidden for good (too_easy / drop) and card ids shown in their rewritten version. */
  private readonly removedCards = new Set<string>();
  private readonly rewrittenCards = new Set<string>();
  private rsi: RsiChangeOut[] = structuredClone([...RSI_CHANGES]);
  private rsiOn = false;
  /** The Bot's state (fixtures.ts BOT_*); `seq` numbers the preference actions, `prefs` counts the 不感兴趣 clicks per card kind and page (the cooldown doubles with each). */
  /** GET /wiki/writes answers from this list (tests set it). */
  writesNow: WriteOut[] = [];
  private readonly bot = { suggestions: structuredClone([...BOT_SUGGESTIONS]), actions: structuredClone([...BOT_ACTIONS]),
    activity: structuredClone([...BOT_ACTIVITY]), why: structuredClone({ ...BOT_WHY }) as Record<string, WhyOut>, settings: structuredClone(DEFAULT_BOT_SETTINGS), seq: 0, prefs: new Map<string, number>(), reviewLessAt: null as number | null };
  /** null: nothing saved yet (the virtual v1). */
  private spec: SpecRec | null = null;
  /** item id → why it was adopted (the migrated feedback guidelines' evidence, fixed at the first write). */
  private specEvidence: Record<string, string> = {};
  /** The user's decisions in force (teach.spec_out `decisions`), newest first; `reaccepted` (the text added again) drops one. */
  private specDecisions: RuleDecisionOut[] = [];
  /** Accepted B patch id → the rule it wrote (what an undo must still find to revert it). */
  private readonly acceptedItems = new Map<string, SpecItem>();
  /** 真题练习 per page (__setPageExam); a page without one answers an empty block. */
  private readonly exams = new Map<string, PageExamOut>();
  private readonly assets = new Map<string, { meta: AssetMeta; html: string }>(
    [[DEMO_ASSET.asset_id, { meta: structuredClone(DEMO_ASSET), html: DEMO_INTERACTIVE_HTML }]]);
  /** GET /wiki/graph and its actions: graph.html's data. */
  private readonly graphFx = new GraphFixture();
  /** 资料 (/wiki/sources). */
  private readonly sources: FakeSourceStore;

  constructor(opts: FakeOptions = {}) {
    this.delayMs = opts.delayMs ?? 0;
    this.now = opts.now ?? ((): Date => new Date());
    this.llmUnavailable = opts.llmUnavailable ?? false;
    this.sources = new FakeSourceStore(this.now);
    if (opts.bot) Object.assign(this.bot, opts.bot);
    if (opts.rsi) this.rsi = structuredClone(opts.rsi);
    for (const [m, err] of Object.entries(opts.failNext ?? {})) this.failNext(m as keyof WikiApi, err);
  }

  failNext(method: keyof WikiApi, err: ApiError | OfflineError | ErrorEvent): void {
    if (!(err instanceof Error) && !STREAMING.has(method)) {
      throw new TypeError(`an ErrorEvent failure only applies to streaming methods, not ${method}`);
    }
    this.failures.set(method, err);
  }

  /** Test hook: the page is a reference card (shelved), every foldable section folded, whatever its cells say. */
  __collapse(pageId: string): void {
    const p = this.pages.get(pageId);
    if (!p) throw new Error(`no page ${pageId}`);
    p.collapsed = true;
    for (const s of p.sections) if (foldable(s.role)) s.folded = true;
  }

  /** Test hook: the model is (un)reachable. */
  __llmDown(on: boolean): void {
    this.llmUnavailable = on;
  }

  /**
   * Test hook: the usage limit is reached (on) or lifted. While on, every JSON method except the reads and the
   * non-LLM writes (NO_LLM) rejects with ApiError(429) before touching any state, and the streams send one budget_exceeded error event and end.
   */
  __budget(on: boolean): void {
    this.budget = on;
  }

  // ---------------- goals ----------------

  /**
   * Test hook: a new goal as the chat's goal_new makes it (no route of the plugin's makes one): with a goal naming
   * PATH_GOAL_HINT a learning path; the first goal a draft of DRAFT_QUESTIONS (answerDraft then writes p1); a later goal
   * its page at once. Not recorded in `calls`.
   */
  __goal(text: string, background = ""): GoalDraftOut {
    return structuredClone(((): GoalDraftOut => {
      const goal = text.trim();
      if (goal.includes(PATH_GOAL_HINT)) {
        return this.pathDraft([...this.paths.values()].find((x) => x.goal === goal) ?? this.addPath(goal));
      }
      if (this.pages.size === 0) {
        const id = `d${++this.seq.draft}`;
        this.drafts.set(id, { id, goal, questions: DRAFT_QUESTIONS });
        const bg = background.trim();
        if (bg) this.addNote("c2.procedure", "self", bg.slice(0, 240), `${id}/background`);
        return {
          ...NO_ROUND, draft_id: id, round: 1, title: goal,
          concepts: ["c1", "c2", "c3", "c4"].map((c) => this.draftConcept(c, c === "c1", false)),
          questions: DRAFT_QUESTIONS.map((q, index) => {
            const { conceptId, aspect } = splitCell(q.cell);
            return { index, kind: "quiz" as const, target: q.cell, cell: q.cell, concept_id: conceptId,
              concept_name: this.conceptName(conceptId), aspect: aspect as Aspect, stem: q.stem,
              options: [...q.options], option_means: null, intent: "goal_use" };
          }),
          notes: { from_background: bg ? 1 : 0, reused_concepts: 0, dropped_questions: 0 },
        };
      }
      const page = this.addPage(goal, goal, "c5", OUTLINE_PCA);
      return {
        ...NO_ROUND, page_id: page.id, title: goal,
        concepts: [this.draftConcept("c3", false, true), this.draftConcept("c5", true, false)],
        questions: [], notes: { from_background: 0, reused_concepts: 2, dropped_questions: 0 },
      };
    })());
  }

  answerDraft(draftId: string, body: AnswersIn): Promise<GoalDraftOut> {
    return this.run("answerDraft", [draftId, body], () => {
      const d = this.drafts.get(draftId);
      if (!d) throw http(404, "找不到这个草稿");
      if (!body.skip) {
        const answers = body.answers ?? [];
        if (answers.length !== d.questions.length) throw http(422, "答案个数和题目不一致");
        d.questions.forEach((q, i) => {
          const a = answers[i] ?? IDK;
          if (!Number.isInteger(a) || a < IDK || a >= q.options.length) throw http(422, "答案必须是选项序号或 -1");
        });
        d.questions.forEach((q, i) => {
          const a = answers[i] ?? IDK;
          const ref = `${draftId}/q${i}`;
          // Note text as the backend writes it (D21): the chosen option, or the stem for 不知道.
          this.setBand(q.cell, a === IDK ? "gap" : a === q.answer ? "known" : "shaky");
          this.addNote(q.cell, "quiz", a === IDK ? q.stem : q.options[a] ?? "", ref);
        });
      }
      this.drafts.delete(draftId);
      // One round, then the page (the real backend may ask another round or plan a path / map).
      const results = body.skip ? [] : d.questions.map((q, index) => {
        const choice = body.answers?.[index] ?? IDK;
        return { index, kind: "quiz" as const, choice, answer: q.answer, correct: choice === q.answer, means: null,
          intent: "goal_use" as const, node_id: splitCell(q.cell).conceptId };
      });
      return {
        ...NO_ROUND, page_id: this.addPage(P1_TITLE, d.goal, "c1", OUTLINE_P1).id, title: d.goal,
        concepts: ["c1", "c2", "c3", "c4"].map((c) => this.draftConcept(c, c === "c1", false)),
        questions: [], notes: { from_background: 0, reused_concepts: 0, dropped_questions: 0 }, results,
      };
    });
  }

  // ---------------- page ----------------

  organize(pageId: string, on: (e: OrganizeEvent) => void, o: StreamOpts = {}): Promise<void> {
    return this.stream<OrganizeEvent>("organize", [pageId, on, o], on, o.signal, async (s) => {
      const p = this.pages.get(pageId);
      if (!p) { await s.emit({ event: "error", data: { code: "not_found", retryable: false, detail: "" } }); return; }
      // A claimed ready prefetch is replayed (its written sections, no LLM call) and consumed: the next organize writes anew.
      const replay = this.readyAhead.delete(p.id);
      const reorganize = p.organized && !replay;
      const secs = replay ? p.sections : p.outline.sections.map((fx) => this.freshSection(fx));
      await s.emit({ event: "outline", data: {
        page_id: p.id, source: "organizer", profile: p.outline.profile, assumed: p.outline.assumed.map((c) => this.cellOut(c)),
        sections: secs.map((x) => ({ ...this.sectionOut(x), cue: "", body: "", changed: null })),
        dropped_checks: [] } });
      await s.emit({ event: "writer", data: { model: FAKE_WRITER.model, fallback: false, reason: "" } });
      let written = 0;
      for (const x of secs) {
        if (x.role === "check") continue;
        written++;
        await s.emit({ event: "section_delta", data: { section_id: x.fx.id, cue: x.cue, body: x.body, heading: x.fx.heading } });
        await s.emit({ event: "section_done", data: { section_id: x.fx.id, cached: false } });
      }
      await s.gate();
      if (replay) {
        s.deliver({ event: "done", data: { page_id: p.id, llm_calls: 0,
          cost_usd: 0, model: FAKE_WRITER.model, fallback: false, prefetched: true } });
        return;
      }
      if (reorganize) for (const x of secs) x.changed = "rewritten";
      p.sections = secs;
      p.organized = true;
      p.seen = this.snapshot(p);
      s.deliver({ event: "done", data: { page_id: p.id, llm_calls: 1 + written,
        cost_usd: 0, model: FAKE_WRITER.model, fallback: false, prefetched: false } });
    });
  }

  /** `opts.rewrite` (the page view's open) is recorded as a second arg; re-opens record the page id only. */
  openPage(pageId: string, opts: { rewrite?: boolean } = {}): Promise<PageOut> {
    return this.run("openPage", opts.rewrite === true ? [pageId, { rewrite: true }] : [pageId], () => {
      const p = this.page(pageId);
      this.currentPage = p.id;
      let diff: DiffNoteOut | null = null;
      if (p.organized) {
        const changes: CellChangeOut[] = this.pageCells(p).filter((c) => (p.seen.get(c) ?? this.band(c)) !== this.band(c))
          .map((c) => ({ cell: c, from: p.seen.get(c) ?? "none", to: this.band(c) }));
        if (changes.length > 0) {
          const hit = new Set(changes.map((c) => c.cell));
          const folded: string[] = [];
          const unfolded: string[] = [];
          for (const s of p.collapsed ? [] : p.sections) {
            if (!foldable(s.role) || s.fold_locked || !s.fx.cells.some((c) => hit.has(c))) continue;
            const want = this.allKnown(s.fx.cells);
            if (want === s.folded) continue;
            s.folded = want;
            s.changed = want ? "folded" : "unfolded";
            (want ? folded : unfolded).push(s.fx.id);
          }
          diff = { cell_changes: changes, folded, unfolded, patched: [] };
        }
      }
      p.seen = this.snapshot(p);
      return this.pageOut(p, diff);
    });
  }

  fold(pageId: string, sid: string, folded: boolean): Promise<SectionOut> {
    return this.run("fold", [pageId, sid, folded], () => {
      const s = this.section(pageId, sid);
      if (!foldable(s.role)) throw http(409, "这一段不能折叠");
      s.folded = folded;
      s.fold_locked = true;
      s.changed = folded ? "folded" : "unfolded";
      return this.sectionOut(s);
    });
  }

  pin(pageId: string, sid: string, pinned: boolean): Promise<SectionOut> {
    return this.run("pin", [pageId, sid, pinned], () => {
      const s = this.section(pageId, sid);
      s.pinned = pinned;
      return this.sectionOut(s);
    });
  }

  feedbackKnow(pageId: string, sid: string, artifactId?: string | null): Promise<FeedbackOut> {
    return this.run("feedbackKnow", artifactId ? [pageId, sid, artifactId] : [pageId, sid], () => {
      const s = this.section(pageId, sid);
      if (s.pinned) throw http(409, "这一段已钉住");
      for (const c of s.fx.cells) {
        this.setBand(c, UP[this.band(c)]);
        this.addNote(c, "feedback", `对「${s.fx.heading}」点了“这部分我懂”`, `${pageId}/${sid}`);
      }
      this.touchSeen(pageId, s.fx.cells);
      const folded: string[] = [];
      if (foldable(s.role)) {
        s.folded = true;
        s.fold_locked = true;
        s.changed = "folded";
        folded.push(sid);
      }
      return { section: this.sectionOut(s), change: change({ folded, other_pages_stale: this.othersUsing(s.fx.cells, pageId) }) };
    });
  }


  answerCheck(pageId: string, sid: string, choice: number, artifactId?: string | null): Promise<CheckAnswerOut> {
    return this.run("answerCheck", artifactId ? [pageId, sid, choice, artifactId] : [pageId, sid, choice], () => {
      const s = this.section(pageId, sid);
      const fx = s.fx.check;
      if (!fx || !s.check) throw http(404, "这一段没有确认题");
      if (!Number.isInteger(choice) || choice < IDK || choice >= fx.options.length) throw http(422, "选项超出范围");
      const correct = choice === IDK ? null : choice === fx.answer;
      for (const c of s.fx.cells) {
        this.setBand(c, correct === null ? "gap" : correct ? "known" : "shaky");
        this.addNote(c, "check", choice === IDK ? fx.stem : fx.options[choice] ?? "", `${pageId}/${sid}`);
      }
      this.touchSeen(pageId, s.fx.cells);
      s.check = { ...s.check, answered: choice, correct_index: fx.answer, correct };
      const ref = `check:${pageId}:${sid}`;
      this.answers.push({ ref, pageId, sid, cell: s.fx.cells[0] ?? "", recall: false, choice, wrong: correct !== true, stem: fx.stem,
        options: [...fx.options], answer: fx.answer, cue: "", pick: choice === IDK ? "" : fx.options[choice] ?? "", followed: false });
      return { correct, correct_index: fx.answer, change: change({ other_pages_stale: this.othersUsing(s.fx.cells, pageId) }), answer_ref: ref };
    });
  }

  done(pageId: string): Promise<DoneOut> {
    return this.run("done", [pageId], () => this.markDone(this.page(pageId)));
  }

  /** 恢复成完整页: shelved off; every unlocked section that is not all known unfolds (409 when the page is not shelved). */
  expand(pageId: string): Promise<ShelveOut> {
    return this.run("expand", [pageId], () => {
      const p = this.page(pageId);
      if (!p.collapsed) throw http(409, "这一页没有收起");
      return { change: change({ unfolded: this.expandPage(p) }) };
    });
  }

  /** 收为参考卡: shelved on; every unlocked foldable section whose cells are all known folds (409 unless can_shelve). */
  collapse(pageId: string): Promise<ShelveOut> {
    return this.run("collapse", [pageId], () => {
      const p = this.page(pageId);
      if (!this.canShelve(p)) throw http(409, "这一页现在不能收为参考卡");
      p.collapsed = true;
      const folded: string[] = [];
      for (const s of p.sections) {
        if (!foldable(s.role) || s.folded || s.fold_locked || !this.allKnown(s.fx.cells)) continue;
        s.folded = true;
        s.changed = "folded";
        folded.push(s.fx.id);
      }
      return { change: change({ folded }) };
    });
  }

  // ---------------- catalog / K ----------------

  catalog(): Promise<CatalogOut> {
    return this.run("catalog", [], () => {
      const all = [...this.pages.values()];
      return {
        learning: all.filter((p) => !p.collapsed).map((p) => this.card(p)),
        shelf: { pages: all.filter((p) => p.collapsed).map((p) => this.card(p)) },
        settings: this.settings,
        current_page: this.currentPage,
        names: this.namesOut([...all.map((p) => p.id), ...(this.currentPage === null ? [] : [this.currentPage])]),
        paths: [...this.paths.values()].map((x) => this.pathCard(x)), maps: [],
      };
    });
  }

  kGrid(): Promise<KGridOut> {
    return this.run("kGrid", [], () => {
      const cur = this.currentPage === null ? undefined : this.pages.get(this.currentPage);
      const curCells = new Set(cur ? this.pageCells(cur) : []);
      return {
        concepts: CONCEPT_LIST.map((c) => ({
          id: c.id, name: c.name, role: this.conceptRole(c.id), aspects: [...c.aspects], prereqs: [...c.prereqs],
          on_current_page: c.aspects.some((a) => curCells.has(`${c.id}.${a}`)),
          cells: c.aspects.map((a) => this.belief(`${c.id}.${a}`)),
          notes: this.notes.filter((n) => splitCell(n.cell).conceptId === c.id),
        })),
      };
    });
  }

  declare(cell: string, value: Declared | null): Promise<DeclareOut> {
    return this.run("declare", [cell, value], () => {
      const { conceptId, aspect } = splitCell(cell);
      const c = CONCEPT_BY_ID.get(conceptId);
      if (!c || !c.aspects.includes(aspect as Aspect)) throw http(404, "没有这个格子");
      if (value === null) {
        const prev = this.beforeDeclare.get(cell);
        if (prev !== undefined) this.setBand(cell, prev);
        this.declared.delete(cell);
        this.beforeDeclare.delete(cell);
        // The backend writes the raw value as the note text (kchange.DECLARE_NOTE): know / dont / clear.
        this.addNote(cell, "declare", "clear", `k/${cell}`);
      } else {
        this.markDeclared(cell, value);
      }
      this.touchSeen(this.currentPage, [cell]);
      const stale = [...this.pages.values()]
        .filter((p) => p.id !== this.currentPage && this.pageCells(p).includes(cell)).map((p) => p.id);
      return { cell: this.belief(cell), change: change({ other_pages_stale: stale }) };
    });
  }

  getSettings(): Promise<SettingsIO> { return this.run("getSettings", [], () => this.settings); }

  /** As the server: review_per_day / language / page_limit_usd only when present; any other field is a 422. */
  putSettings(s: SettingsPut): Promise<SettingsIO> {
    return this.run("putSettings", [s], () => {
      const extra = Object.keys(s).filter((k) => !["review_per_day", "language", "page_limit_usd"].includes(k));
      if (extra.length > 0) throw http(422, `未知的设置：${extra.join("、")}`);
      const perDay = s.review_per_day ?? this.settings.review_per_day;
      if (!Number.isInteger(perDay) || perDay < 0) throw http(422, "每天复习条数不能小于 0");
      const language = s.language ?? this.settings.language;
      if (!(LANGUAGES as readonly string[]).includes(language)) throw http(422, "未知的语言");
      const limit = "page_limit_usd" in s ? s.page_limit_usd ?? null : this.settings.page_limit_usd;
      if (limit !== null && !(limit >= 0.05 && limit <= 5)) throw http(422, "每页上限 0.05–5 美元");
      this.settings = { ...this.settings, review_per_day: perDay, language, page_limit_usd: limit };
      return this.settings;
    });
  }

  // ---------------- guidelines ----------------

  guidelines(): Promise<GuidelinesOut> { return this.run("guidelines", [], () => ({ items: this.guidelineList })); }

  addGuideline(body: GuidelineIn): Promise<GuidelineOut> {
    return this.run("addGuideline", [body], () => {
      const text = body.text;
      if (!text.trim() || text.length > 300) throw http(422, "原则 1–300 字");
      const g: GuidelineOut = { guideline_id: `g${++this.seq.guideline}`, text, source: "user", evidence: null };
      this.guidelineList.push(g);
      return g;
    });
  }

  deleteGuideline(id: string): Promise<void> {
    return this.run("deleteGuideline", [id], () => {
      const i = this.guidelineList.findIndex((g) => g.guideline_id === id);
      if (i < 0) throw http(404, "找不到这条原则");
      this.guidelineList.splice(i, 1);
    });
  }

  // ---------------- learning path ----------------

  getPath(pathId: string): Promise<PathOut> {
    return this.run("getPath", [pathId], () => this.pathOut(this.pathRec(pathId)));
  }

  pathNext(pathId: string): Promise<PathNextOut> {
    return this.run("pathNext", [pathId], () => {
      const path = this.pathRec(pathId);
      const i = path.pages.findIndex((pg) => this.pathStatus(pg.pageId) === "not_started");
      const pg = path.pages[i];
      if (!pg) throw http(409, "这条路径的页都已经开始了");
      const index = i + 1;
      const pf = path.prefetch ?? null;
      path.prefetch = null;
      // As paths.next_path_page: a live prefetch of this index is claimed; a failed (or stale) one is discarded.
      if (pf !== null && pf.index === index && pf.status !== "failed") {
        pg.pageId = pf.pageId;
        this.currentPage = pf.pageId;
        return { path_id: path.id, index, page_id: pf.pageId, prefetched: true };
      }
      // A discarded prefetch's reserved page stays unused (ids are never reused).
      const page = this.addPage(`${path.goal}（第 ${index}/${path.pages.length} 页）`, path.goal, pg.targets[0] ?? "",
        index === 3 ? OUTLINE_PCA : OUTLINE_P1);
      pg.pageId = page.id;
      return { path_id: path.id, index, page_id: page.id, prefetched: false };
    });
  }

  /**
   * Test hook (the server starts this when page k of a path is opened): the path's next page written ahead. `ready`:
   * the page is written now (as a claimed prefetch commits it) and organize replays it (no LLM call counted); `writing`:
   * it exists unwritten and organize writes it (the server's organize attaches to the run); `failed`: 下一页 discards
   * it. Returns the reserved page id.
   */
  prefetchNext(pathId: string, status: PrefetchOut["status"]): string {
    const path = this.pathRec(pathId);
    const i = path.pages.findIndex((pg) => this.pathStatus(pg.pageId) === "not_started");
    const pg = path.pages[i];
    if (!pg) throw new Error("nothing left to prefetch");
    const index = i + 1;
    const keep = this.currentPage;
    const page = this.addPage(`${path.goal}（第 ${index}/${path.pages.length} 页）`, path.goal, pg.targets[0] ?? "",
      index === 3 ? OUTLINE_PCA : OUTLINE_P1);
    this.currentPage = keep;
    if (status === "ready") {
      this.readyAhead.add(page.id);
      page.sections = page.outline.sections.map((fx) => this.freshSection(fx));
      page.organized = true;
      page.seen = this.snapshot(page);
    }
    path.prefetch = { index, pageId: page.id, status };
    return page.id;
  }

  pathPageDone(pathId: string, index: number): Promise<PathPageDoneOut> {
    return this.run("pathPageDone", [pathId, index], () => {
      const path = this.paths.get(pathId);
      const pg = path !== undefined && Number.isInteger(index) && index >= 1 ? path.pages[index - 1] : undefined;
      if (!path || !pg) throw http(404, "找不到这一页");
      if (pg.pageId === null) throw http(409, "这一页还没开始");
      const done = this.markDone(this.page(pg.pageId));
      return { path: this.pathOut(path), done };
    });
  }

  // ---------------- review ----------------

  /** As the server: `more` = the next full batch (the cards answered today are no longer candidates). */
  reviewToday(more = false): Promise<ReviewTodayOut> {
    return this.run("reviewToday", more ? [true] : [], () => this.todayOut(more));
  }

  reviewPage(pageId: string): Promise<ReviewTodayOut> {
    return this.run("reviewPage", [pageId], () => {
      this.page(pageId);
      const out = this.todayOut(true);
      const items = out.items.filter((i) => i.page_id === pageId);
      return { ...out, items, due: items.length, confirms: [] };
    });
  }

  gradeReview(body: ReviewGradeIn): Promise<ReviewGradeOut> {
    return this.run("gradeReview", [body], () => {
      // Every field but the cell is optional on the wire (the server checks grade xor choice).
      const b = body;
      const grade = b.grade ?? null;
      const choice = b.choice ?? null;
      const pageId = b.page_id ?? null;
      const sid = b.section_id ?? null;
      if ((grade === null) === (choice === null)) throw http(422, "请给出 grade，或 choice");
      if (grade !== null && !(GRADES as readonly string[]).includes(grade)) throw http(422, "未知的自评档位");
      if (choice !== null && (!Number.isInteger(choice) || choice < IDK)) throw http(422, "选项超出范围");
      if (choice !== null && (pageId === null || sid === null)) throw http(422, "答确认题要给出 page_id 和 section_id");
      if (sid !== null && pageId === null) throw http(422, "给出 section_id 时也要给出 page_id");
      const cell = b.cell;
      const graded = [...new Set([cell, ...(b.cells ?? [])])];
      for (const c of graded) {
        const { conceptId, aspect } = splitCell(c);
        if (!CONCEPT_BY_ID.get(conceptId)?.aspects.includes(aspect as Aspect)) throw http(404, "找不到这一格");
      }
      if (pageId !== null) this.page(pageId);
      const sec = pageId !== null && sid !== null ? this.section(pageId, sid) : undefined;
      if (graded.length > 1 && (grade === null || sec === undefined || !graded.every((c) => sec.fx.cells.includes(c))))
        throw http(422, "一张卡只能评这一段回想提示里的格子");
      const from = this.band(cell);
      let to: Band;
      let correct: boolean | null = null;
      let correctIndex: number | null = null;
      let text: string;
      if (choice !== null) {
        const fx = sec?.fx.check;
        if (!fx || !sec?.check) throw http(422, "这一段没有确认题");
        if (choice >= fx.options.length) throw http(422, "选项超出范围");
        correct = choice === IDK ? null : choice === fx.answer;
        correctIndex = fx.answer;
        to = correct === true ? UP[from] : DOWN[from];
        // Note text as answerCheck writes it: the chosen option, or the stem for 不知道.
        text = choice === IDK ? fx.stem : fx.options[choice] ?? "";
      } else {
        const g = grade as Grade;
        to = g === "recalled" ? UP[from] : g === "fuzzy" ? "shaky" : "gap";
        text = g;
      }
      const at = this.now();
      // One grade of a cue card is one answer for each of its cells (the lead's band is the one shown).
      for (const c of graded) {
        const t = c === cell ? to : grade === "recalled" ? UP[this.band(c)] : grade === "fuzzy" ? "shaky" : "gap";
        this.setBand(c, t, grade === "forgot");
        this.addNote(c, "review", text, `review/${c}`);
      }
      if (b.page_round === true) this.pageRoundAnswers++;
      else this.reviewLog.push({ at, cells: graded, grade });
      const target = pageId ?? this.currentPage;
      const tp = target === null ? undefined : this.pages.get(target);
      const unfolded: string[] = [];
      if (tp && !tp.collapsed && RANK[to] < RANK.likely) {
        for (const s of tp.sections) {
          if (!foldable(s.role) || !s.folded || s.fold_locked || !graded.some((c) => s.fx.cells.includes(c))) continue;
          s.folded = false;
          s.changed = "unfolded";
          unfolded.push(s.fx.id);
        }
      }
      this.touchSeen(target, graded);
      const ref = `review:${pageId ?? "-"}:${sid ?? "-"}:${++this.seq.answer}`;
      if (pageId !== null && sid !== null && sec) {
        const fx = choice !== null ? sec.fx.check : undefined;
        this.answers.push({ ref, pageId, sid, cell, recall: choice === null, choice, followed: false,
          wrong: choice === null ? grade !== "recalled" : correct !== true,
          stem: fx?.stem ?? "", options: fx ? [...fx.options] : [], answer: fx?.answer ?? 0, cue: sec.cue,
          pick: choice === null ? text : choice === IDK ? "" : fx?.options[choice] ?? "" });
      }
      return {
        band: { cell, from, to }, correct, correct_index: correctIndex, page_id: target,
        change: change({ unfolded, other_pages_stale: this.othersUsing(graded, target ?? "") }), answer_ref: ref,
      };
    });
  }

  // ---------------- feedback signals (wikiapp/feedback.py) ----------------

  /**
   * Body checks first (pydantic, 422), then the page (404) and the section (422); the signal is recorded from here on.
   * An organized page is then really patched (placeholder text): the target section (the asked one, else the first
   * explain) gets a paragraph, a library component may be inserted after it, concepts named in the text are declared
   * 「不会」, and (with text and `apply` not false) a track-B change applies by itself (its rule joins the spec). A page that is not
   * organized, or an unreachable model, is `understood: false` with only the signal kept.
   */
  pageFeedback(pageId: string, body: PageFeedbackIn): Promise<PageFeedbackOut> {
    return this.run("pageFeedback", [pageId, body], () => {
      const chips = body.chips ?? [];
      const text = body.text ?? "";
      if (chips.length > MAX_FEEDBACK_CHIPS) throw http(422, `最多选 ${MAX_FEEDBACK_CHIPS} 个`);
      if (chips.some((c) => !(FEEDBACK_CHIPS as readonly string[]).includes(c))) throw http(422, "未知的反馈选项");
      if (text.length > MAX_PAGE_FEEDBACK_TEXT) throw http(422, `最多 ${MAX_PAGE_FEEDBACK_TEXT} 字`);
      if (chips.length === 0 && !text.trim()) throw http(422, "pick a chip or write something");
      const sig = this.recordSignal("page_feedback", pageId, body.section_id ?? null, [...chips], text.trim());
      const p = this.page(pageId);
      const no = (reason: string): PageFeedbackOut => ({ signal_id: sig.signal_id, understood: false, reason, reading: null,
        dropped: [], feedback_id: null, summary: "", sections: [], component: null, missing: [], unfolded: [],
        rsi: "not_understood", llm_calls: 0 });
      if (this.budget) return no(BUDGET_DETAIL);
      if (!p.organized) return no("页面还没生成");
      if (this.llmUnavailable) return no("现在连不上模型");
      const target = body.section_id ? p.sections.find((s) => s.fx.id === body.section_id)
        : p.sections.find((s) => s.role === "explain");
      if (!target) return no("没能理解这条反馈（这一页没有可以补充的小节），只记下了它");

      const quote = text.trim().slice(0, QUOTE_LEN);
      const reading = `你说这一页${chips.map(feelingOf).join("、")}${quote ? `：${quote}` : ""}`;
      const concepts = [...new Set(this.pageCells(p).map((c) => splitCell(c).conceptId))];
      const missingIds = text ? concepts.filter((id) => text.includes(this.conceptName(id))) : [];
      const comp = chips.includes("too_hard") && text.includes("例子") ? FAKE_COMPONENTS[0] : undefined;
      const feedbackId = `fb_${String(++this.seq.feedback).padStart(4, "0")}`;

      // the page: a paragraph on the target, a component section after it
      const before = structuredClone(target);
      target.body = `${target.body}\n\n${FEEDBACK_PATCH_SENTENCE}`;
      target.changed = "rewritten";
      const sections: PageFeedbackOut["sections"] = [{ section_id: target.fx.id, heading: target.fx.heading, change: "added" }];
      let component: PageFeedbackOut["component"] = null;
      const rec: FeedbackRec = { page_id: pageId, feedback_id: feedbackId, at: this.now().toISOString(), reading,
        summary: `这一页按你的反馈改了：在「${target.fx.heading}」后面补了一段${comp ? `，并放了「${comp.name}」` : ""}`,
        patched: [{ before, add: FEEDBACK_PATCH_SENTENCE, left: target.body }], inserted: [], assets: [], undone: false };
      if (comp) {
        const sid = `fs_${++this.seq.component}`;
        const assetId = `ia_${++this.seq.asset}`;
        const meta: AssetMeta = { ...structuredClone(DEMO_ASSET), asset_id: assetId, page_id: pageId, section_id: sid,
          interactive_id: comp.component_id, title: comp.name, height: comp.height };
        meta.check = { ...meta.check, interactive_id: comp.component_id };
        this.assets.set(assetId, { meta, html: DEMO_INTERACTIVE_HTML });
        const heading = `动手看一看：${comp.name}`;
        const fx: SectionFixture = { id: sid, heading, role: "explain", cells: [], cue: "", why: "", body: "" };
        const sec = this.freshSection(fx);
        sec.body = `${comp.learning_question}\n\n${embedBlock(meta)}`;
        sec.changed = "inserted";
        p.sections.splice(p.sections.indexOf(target) + 1, 0, sec);
        rec.inserted.push(sid);
        rec.assets.push(assetId);
        sections.push({ section_id: sid, heading, change: "inserted" });
        component = { kind: "component", component_id: comp.component_id, name: comp.name, asset_id: assetId, section_id: sid, why: "" };
      }
      this.feedbacks.push(rec);

      // K: what the user said they do not know
      const unfolded: string[] = [];
      const missing: PageFeedbackOut["missing"] = missingIds.map((id) => {
        const cells = (CONCEPT_BY_ID.get(id)?.aspects ?? []).map((a) => cellOf(id, a));
        for (const cell of cells) this.markDeclared(cell, "dont");
        return { concept_id: id, name: this.conceptName(id), cells };
      });
      for (const s of p.sections) {
        if (s.folded && !s.fold_locked && s.fx.cells.some((c) => missing.some((m) => m.cells.includes(c)))) {
          s.folded = false;
          s.changed = "unfolded";
          unfolded.push(s.fx.id);
        }
      }

      // the teaching rule this asks for
      const spec = this.effectiveSpec().items;
      const kind = chips.includes("too_shallow") ? "讲得太浅" : chips.includes("too_hard") ? "讲得太难" : "讲法不合适";
      const old = spec[spec.length - 1]?.text ?? DEFAULT_STYLE_BEFORE;
      const ruleAfter = quote ? `按你说的「${quote}」来讲。` : `${kind}时，按你的感觉调整讲法。`;
      let rsi: RsiStatus = "not_requested";
      // No RSI round runs for this one feedback (RSI-B retired 2026-10-29): its signal counts toward the next
      // harness round like any other, so no change row appears here.
      if (body.apply !== false) rsi = text.trim() ? "queued" : "no_text";
      return {
        signal_id: sig.signal_id, understood: true, reason: "",
        reading: { reading, missing: missingIds,
          page: { summary: rec.summary.replace("这一页按你的反馈改了：", ""), sections: [{ section_id: target.fx.id, add: FEEDBACK_PATCH_SENTENCE }],
            component: comp?.component_id ?? null, component_after: comp ? target.fx.id : null, component_config: null, component_why: "", block: null, block_why: "" },
          style: { kind, before: old, after: ruleAfter } },
        dropped: [], feedback_id: feedbackId, summary: rec.summary, sections, component, missing, unfolded, rsi, llm_calls: 1 };
    });
  }

  pageFit(pageId: string): Promise<SignalCreatedOut> {
    return this.run("pageFit", [pageId], () => this.recordSignal("page_fit", pageId, null, [], "基本合适"));
  }

  handEdit(pageId: string, body: HandEditIn): Promise<HandEditOut> {
    return this.run("handEdit", [pageId, body], () => {
      if (body.summary.length > MAX_NOTE_SUMMARY) throw http(422, `改动摘要最多 ${MAX_NOTE_SUMMARY} 字`);
      const p = this.page(pageId);
      for (const e of body.sections ?? []) {
        const x = p.sections.find((y) => y.fx.id === e.section_id);
        if (x === undefined) throw http(409, "这一段已经不在这一页了");
        x.body = e.body; x.noteBody = e.note_body; x.pinned = true; x.changed = null;
        if (e.cue !== undefined && e.cue !== null) x.cue = e.cue;
        if (e.folded !== undefined && e.folded !== null) x.folded = e.folded;
      }
      p.sections = p.sections.filter((x) => !(body.removed ?? []).some((r) => r.section_id === x.fx.id));
      this.recordSignal("note_edit", pageId, null, [], body.summary);
      return { edit_id: `he${this.handEdits++}`, page: this.pageOut(p, null) };
    });
  }

  undoHandEdit(pageId: string, editId: string): Promise<HandEditOut> {
    return this.run("undoHandEdit", [pageId, editId], () => ({ edit_id: null, page: this.pageOut(this.page(pageId), null) }));
  }

  /** Test hook: every signal recorded so far, in order. */
  __signals(): FakeSignal[] {
    return structuredClone(this.signals);
  }

  // ---------------- the 个性化 tab (GET /wiki/engine) ----------------

  /** The demo's ENGINE.prompts / ENGINE.params rows (fixtures.ts), as fresh copies. Read-only: nothing changes them. */
  engine(): Promise<EngineOut> {
    return this.run("engine", [], () => ({ prompts: structuredClone([...ENGINE_PROMPTS]), params: structuredClone([...ENGINE_PARAMS]) }));
  }

  // ---------------- Prompt 设置 (service/routes_prompts.py) ----------------

  /** step → the user's own text (absent = the default). */
  private readonly promptEdits = new Map<string, string>();
  private promptOut(step: string): PromptStepOut {
    const d = PROMPT_STEPS.find((p) => p.step === step);
    if (!d) throw http(404, `unknown prompt step '${step}'`);
    const mine = this.promptEdits.get(step);
    return { ...d, text: mine ?? d.default_text, edited: mine !== undefined };
  }

  prompts(): Promise<PromptStepOut[]> { return this.run("prompts", [], () => PROMPT_STEPS.map((p) => this.promptOut(p.step))); }

  putPrompt(step: string, text: string): Promise<PromptStepOut> {
    return this.run("putPrompt", [step, text], () => {
      this.promptOut(step);
      const t = text.trim();
      if (!t && step !== "language_style") throw http(422, PROMPT_EMPTY);
      if (t.length > 4000) throw http(422, PROMPT_TOO_LONG);
      if (t.includes("⟦") || t.includes("⟧")) throw http(422, PROMPT_MARKERS);
      const low = t.toLowerCase();
      if (PROMPT_FORMAT_WORDS.some((w) => low.includes(w))) throw http(422, PROMPT_FORMAT);
      const d = PROMPT_STEPS.find((p) => p.step === step) as PromptStepOut;
      const before = this.promptEdits.get(step) ?? null;
      if (t === d.default_text) this.promptEdits.delete(step); else this.promptEdits.set(step, t);
      const after = this.promptEdits.get(step) ?? null;
      if (after !== before) this.promptChange(step, before, after);
      return this.promptOut(step);
    });
  }

  resetPrompt(step: string): Promise<PromptStepOut> {
    return this.run("resetPrompt", [step], () => {
      this.promptOut(step);
      const before = this.promptEdits.get(step) ?? null;
      this.promptEdits.delete(step);
      if (before !== null) this.promptChange(step, before, null);
      return this.promptOut(step);
    });
  }

  private promptSeq = 0;
  /** bot/prompts.py: a changed step writes an already-executed ledger row (shown in 最近改了什么; only the newest of a step can be undone). */
  private promptChange(step: string, before: string | null, after: string | null): void {
    const name = (PROMPT_STEPS.find((p) => p.step === step) as PromptStepOut).name;
    const at = this.now().toISOString();
    const a: ActionOut = { action_id: `act_prompt_${++this.promptSeq}`, kind: "prompt_override", scope: "teaching_params", level: "auto_undo", status: "executed",
      source: "bot", summary: after === null ? `「${name}」恢复了默认写法` : `改了「${name}」的写法`, why: "你在 Prompt 设置里改的",
      content: { step, before, after }, page_id: null, detail: "", created_at: at, decided_at: at, expires_at: null, can_undo: true, diff: [],
      confirm: false, cost_usd: null, made: [], revise: null, result: {}, section_id: null };
    for (const old of this.bot.actions) {
      if (old.kind === "prompt_override" && old.status === "executed" && old.content.step === step) {
        old.can_undo = false;
        const row = this.rsi.find((x) => x.patch_id === old.action_id);
        if (row) row.can_undo = false;
      }
    }
    this.bot.actions.push(a);
    this.rsi.push(this.ledgerRow(a));
  }

  // ---------------- RSI changes (service/routes_rsi.py) ----------------

  /** Every patch of either track, newest first (the plugin filters what it shows). */
  rsiChanges(): Promise<RsiChangeOut[]> {
    return this.run("rsiChanges", [], () => [...this.rsi].sort((a, b) => Date.parse(b.at) - Date.parse(a.at)));
  }
  rsiSwitch(): Promise<RsiSwitch> { return this.run("rsiSwitch", [], () => ({ on: this.rsiOn })); }
  putRsiSwitch(on: boolean): Promise<RsiSwitch> { return this.run("putRsiSwitch", [on], () => { this.rsiOn = on; return { on }; }); }

  /**
   * personal_accepted only (else 409). A track-B undo takes its rule out of the spec (a new version) and appends the
   * rollback reason to `why` (changes.change_out); when the rule is no longer what the patch wrote (removed or edited,
   * or never in this spec: the fixture pb3) it is a 409 conflict, as spec_ops.revert refuses it. A track-A undo only
   * changes the status; so does a layer-2 method change's, which is a 409 unless it can be undone (only the latest of
   * its kind can).
   */
  undoPatch(patchId: string): Promise<RsiChangeOut> {
    return this.run("undoPatch", [patchId], () => {
      const c = this.patch(patchId);
      if (c.status !== "personal_accepted") {
        throw http(409, `patch ${patchId} is ${c.track}/${c.status}; only accepted patches can be undone`);
      }
      const botAct = this.bot.actions.find((a) => a.action_id === patchId);
      if (botAct) { this.undoBotAction(botAct); return c; }   // a Bot preference row: the ledger undoes it
      if (c.layer === 2 && !c.can_undo) throw http(409, `method change ${patchId} is not the latest of its kind`);
      if (c.track === "B" && c.layer === 0) {
        const wrote = this.acceptedItems.get(patchId);
        const itemId = wrote?.item_id ?? `si-${patchId}`;
        this.writeSpec(undefined, (s) => {
          const now = s.items.find((i) => i.item_id === itemId);
          if (!wrote || !now || now.text !== wrote.text || now.scope !== wrote.scope || now.domain !== wrote.domain) {
            throw http(409, `cannot undo: item ${itemId} was changed or removed since this patch`);
          }
          return { ...s, items: s.items.filter((i) => i.item_id !== itemId) };
        });
        this.acceptedItems.delete(patchId);
        c.why = `${c.why}（${ROLLBACK_REASON}）`.slice(0, 600);
      }
      this.decide(c, "rolled_back");
      c.effect_line = USER_UNDO_LINE;                       // cycle.py: the user's undo says who rolled it back
      // the backend marks the pages rewritten under the undone rule and clears the rest; the fake never rewrites a
      // page, so every page it marked is back to what it was written under
      c.pages_waiting = 0; c.pages_failed = 0;
      return c;
    });
  }

  /** Off: 采用 a code change is refused (forge/patch/land.py approve: 「代码修改已关闭」); 不要 still works. Tests set it. */
  codePatchesOff = false;

  /**
   * forge/change_source.py ForgePatchChangeSource: only a code change waiting for review (`forgep:…`, awaiting_user) is
   * decided, else 409 「这项改动不在等你审阅」 (404 unknown). Approved: personal_accepted, not undoable until it lands, its line
   * 已批准、还没生效的代码改动. Discarded: rejected, its line 没采用的代码改动 (forge/undo.py DISCARDED_LINE).
   * 采用 while off: 409 with code `patches_off`.
   */
  approvePatch(patchId: string): Promise<RsiChangeOut> {
    return this.run("approvePatch", [patchId], () => {
      const c = this.reviewPatch(patchId);
      if (this.codePatchesOff) throw new ApiError(409, "代码修改已关闭", "patches_off");
      this.decide(c, "personal_accepted");
      c.can_undo = false;
      c.summary = c.summary.replace(/^[^：]*：/, "已批准、还没生效的代码改动：");
      return c;
    });
  }

  discardPatch(patchId: string): Promise<RsiChangeOut> {
    return this.run("discardPatch", [patchId], () => {
      const c = this.decide(this.reviewPatch(patchId), "rejected");
      c.summary = c.summary.replace(/^[^：]*：/, "没采用的代码改动：");
      return c;
    });
  }

  private reviewPatch(patchId: string): RsiChangeOut {
    const c = this.patch(patchId);
    if (!patchId.startsWith("forgep:") || c.status !== "awaiting_user") throw new ApiError(409, "这项改动不在等你审阅", "patch_not_awaiting_user");
    return c;
  }

  // ---------------- B1 undo, B4 card feedback, B5 components ----------------

  /**
   * 404 for an unknown feedback (or another page's); 409 when it is already undone or the page's content changed since.
   * The patched sections return to their record before it and the component sections it inserted go away.
   */
  undoFeedback(pageId: string, feedbackId: string): Promise<FeedbackUndoOut> {
    return this.run("undoFeedback", [pageId, feedbackId], () => {
      const p = this.page(pageId);
      const f = this.feedbacks.find((x) => x.feedback_id === feedbackId && x.page_id === pageId);
      if (!f) throw http(404, "找不到这条反馈");
      if (f.undone || !this.feedbackIntact(p, f)) throw http(409, "这一页之后又改过了，不能撤回");
      for (const x of f.patched) {
        const n = p.sections.findIndex((s) => s.fx.id === x.before.fx.id);
        if (n >= 0) p.sections[n] = structuredClone(x.before);
      }
      p.sections = p.sections.filter((s) => !f.inserted.includes(s.fx.id));
      f.undone = true;
      return { feedback_id: feedbackId, page_id: pageId };
    });
  }


  components(): Promise<ComponentOut[]> {
    return this.run("components", [], () => FAKE_COMPONENTS.map((c) => ({ ...c, params: { ...c.params } })));
  }


  /** Test hook: replace the change list. */
  __setChanges(list: RsiChangeOut[]): void {
    this.rsi = structuredClone(list);
  }

  // ---------------- Bot (service/routes_bot.py) ----------------

  /** Pending / shown suggestions of a placement, best first; listing marks them shown (as the backend does). "page" needs a page id (422). */
  botSuggestions(placement: "today" | "page", pageId?: string): Promise<SuggestionOut[]> {
    return this.run("botSuggestions", [placement, pageId], () => {
      if (placement === "page" && !pageId) throw http(422, "placement=page needs page_id");
      const now = this.now().getTime();
      const rows = this.bot.suggestions.filter((s) => s.placement === placement && (placement === "today" || s.page_id === pageId)
        && Date.parse(s.expires_at) > now && this.botListable(s)).sort((a, b) => b.priority - a.priority || Date.parse(b.created_at) - Date.parse(a.created_at));
      for (const s of rows) if (s.status === "pending") s.status = "shown";
      return rows;
    });
  }

  /**
   * The reaction becomes the status (accepted keeps the card listed). 404 unknown; 409 for a repeat, `done` before `accepted`, or a card whose day is
   * over. 不感兴趣 also writes an undoable preference action.
   */
  botReact(id: string, reaction: Reaction): Promise<SuggestionOut> {
    return this.run("botReact", [id, reaction], () => {
      const s = this.bot.suggestions.find((x) => x.suggestion_id === id);
      if (!s) throw http(404, `unknown suggestion '${id}'`);
      if (Date.parse(s.expires_at) <= this.now().getTime()) throw http(409, `suggestion ${id} is expired`);
      if (BOT_OPEN.has(s.status) ? reaction === "done" : !(s.status === "accepted" && reaction === "done")) {
        throw http(409, BOT_OPEN.has(s.status) ? `suggestion ${id} must be accepted before it is done` : `suggestion ${id} is already ${s.status}`);
      }
      s.status = reaction;
      const dedup = `${s.kind}:${s.page_id ?? s.suggestion_id}`;
      if (reaction === "accepted") this.bot.prefs.set(dedup, 0);   // suppression.react: 开始 resets the 不感兴趣 counter
      if (reaction === "not_interested") {
        const at = this.now().toISOString();
        const actionId = `act_pref_${id}`;   // B3 upserts the one `pref:<suggestion_id>` row: a repeat reuses it
        this.bot.prefs.set(dedup, (this.bot.prefs.get(dedup) ?? 0) + 1);
        const summary = `以后少提醒「${s.title}」`;     // bot/suggestions.py: the ledger row's own summary and why
        this.bot.actions = this.bot.actions.filter((x) => x.action_id !== actionId);
        this.rsi = this.rsi.filter((x) => x.patch_id !== actionId);
        this.bot.actions.push({ action_id: actionId, kind: "preference", scope: "preference", level: "auto_undo", status: "executed", source: "bot",
          summary, why: "你点了不感兴趣",
          content: { dedup_key: dedup, suggestion_id: id }, page_id: null, detail: "", created_at: at, decided_at: at, expires_at: null, can_undo: true, diff: [], confirm: false, cost_usd: null, made: [], revise: null, result: {}, section_id: null });
        // The ledger row as the change list shows it (change_source.change_out: the action's own summary and why).
        this.rsi.push({ patch_id: actionId, track: "G", status: "personal_accepted", summary,
          why: "你点了不感兴趣", signals: [], before: "", after: "", at, can_undo: true, reading: "", kind: "preference", rule_before: "", rule_after: "",
          user_edited: false, layer: 0, target: "知识库", outcome: "pending", effect: [], effect_line: "", pages_waiting: 0, pages_failed: 0, tool_live: false });
        s.preference_action_id = actionId;
      }
      return s;
    });
  }

  /** bot/suggestions.py `_review_hidden`: last click + cooldown_hours x 2^count. */
  private reviewHidden(): boolean {
    const n = this.bot.prefs.get("review_due") ?? 0;
    const at = this.bot.reviewLessAt;
    return n > 0 && at !== null && this.now().getTime() < at + this.bot.settings.cooldown_hours * 3600_000 * 2 ** n;
  }

  botReviewReminder(): Promise<ReviewReminderOut> {
    return this.run("botReviewReminder", [], () => ({ hidden: this.reviewHidden(), preference_action_id: null }));
  }

  /** bot/suggestions.py `review_react`: the counter always moves, an open review_due card takes the reaction, 减少提醒 writes one row per click. */
  botReviewReact(reaction: ReviewReaction): Promise<ReviewReminderOut> {
    return this.run("botReviewReact", [reaction], () => {
      const linked = this.bot.suggestions.find((x) => x.kind === "review_due" && BOT_OPEN.has(x.status)) ?? null;
      if (linked !== null) linked.status = reaction;
      if (reaction === "accepted") {
        this.bot.prefs.set("review_due", 0);
        return { hidden: false, preference_action_id: null };
      }
      const at = this.now();
      const stamp = at.toISOString();
      const previous = this.bot.reviewLessAt;
      this.bot.prefs.set("review_due", (this.bot.prefs.get("review_due") ?? 0) + 1);
      this.bot.reviewLessAt = at.getTime();
      const actionId = `act_pref_review_${++this.bot.seq}`;
      const summary = "以后少提醒复习", why = "你点了「减少提醒」";
      this.bot.actions.push({ action_id: actionId, kind: "preference", scope: "preference", level: "auto_undo", status: "executed", source: "bot",
        summary, why, content: { dedup_key: "review_due", suggestion_id: linked?.suggestion_id ?? null, previous_at: previous },
        page_id: null, detail: "", created_at: stamp, decided_at: stamp, expires_at: null, can_undo: true, diff: [], confirm: false, cost_usd: null, made: [], revise: null, result: {}, section_id: null });
      this.rsi.push({ patch_id: actionId, track: "G", status: "personal_accepted", summary, why, signals: [], before: "", after: "", at: stamp, can_undo: true, reading: "", kind: "preference", rule_before: "", rule_after: "",
        user_edited: false, layer: 0, target: "知识库", outcome: "pending", effect: [], effect_line: "", pages_waiting: 0, pages_failed: 0, tool_live: false });
      return { hidden: this.reviewHidden(), preference_action_id: actionId };
    });
  }

  /** Test / dev-fixture seam: puts a ledger row (replaces the row with the same id); `ledger`: fields of its change row. */
  putBotAction(a: ActionOut, ledger: Partial<RsiChangeOut> = {}): void {
    const k = this.bot.actions.findIndex((x) => x.action_id === a.action_id);
    if (k >= 0) this.bot.actions[k] = structuredClone(a); else this.bot.actions.push(structuredClone(a));
    // as botDecide: an executed row is also a change of 最近改了什么
    if (a.status === "executed" && !this.rsi.some((x) => x.patch_id === a.action_id)) this.rsi.push({ ...this.ledgerRow(a), ...ledger });
  }

  /** Test / dev-fixture seam: adds a Bot activity row (replaces the row with the same run id). */
  putActivity(r: ActivityOut): void {
    const k = r.run_id === null ? -1 : this.bot.activity.findIndex((x) => x.run_id === r.run_id);
    if (k >= 0) this.bot.activity[k] = structuredClone(r); else this.bot.activity.push(structuredClone(r));
  }

  /** Newest first. */
  botActions(status?: string): Promise<ActionOut[]> {
    return this.run("botActions", [status], () => this.bot.actions.filter((a) => status === undefined || a.status === status || (status === "proposed" && this.botStale(a)))
      .sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at)));
  }

  /**
   * approve / reject: a proposed row (approve also finishes a transient approved one); the same op again returns the row; a cross-state op is a 409 (`动作已经是 <status>，不能批准|拒绝`).
   * undo: executed rows only (409 `这类动作不需要撤销` for level auto); 404 unknown.
   */
  botDecide(id: string, op: "approve" | "reject" | "undo"): Promise<ActionOut> {
    return this.run("botDecide", [id, op], () => {
      const a = this.botAction(id);
      if (op === "undo") return this.undoBotAction(a);
      // actions.py: the same op again returns the row (executed / rejected); reject is only for proposed rows; approve also finishes an approved one.
      if (a.status === (op === "approve" ? "executed" : "rejected")) return a;
      if (a.status !== "proposed" && !(op === "approve" && a.status === "approved")) throw http(409, `动作已经是 ${a.status}，不能${op === "approve" ? "批准" : "拒绝"}`);
      a.status = op === "approve" ? "executed" : "rejected";
      a.decided_at = this.now().toISOString();
      a.can_undo = botCanUndo(a);
      if (a.status === "executed" && !this.rsi.some((x) => x.patch_id === a.action_id)) this.rsi.push(this.ledgerRow(a));
      return a;
    });
  }

  /** 重试 of a draft that no longer fits: the same row waits again with its draft fitting the page now (stale off). */
  botRedraft(id: string): Promise<ActionOut> {
    return this.run("botRedraft", [id], () => {
      const a = this.botAction(id);
      if (a.status !== "proposed" && a.status !== "failed") throw http(409, `动作已经是 ${a.status}，不能重新生成`);
      a.status = "proposed"; a.detail = ""; a.decided_at = null;
      if (a.revise) a.revise = { ...a.revise, stale: false };
      return a;
    });
  }

  botWhy(q: { triggerId: string } | { suggestionId: string }): Promise<WhyOut> {
    return this.run("botWhy", [q], () => {
      const s = "suggestionId" in q ? this.bot.suggestions.find((x) => x.suggestion_id === q.suggestionId) : undefined;
      if ("suggestionId" in q && !s) throw http(404, `no suggestion ${q.suggestionId}`);
      const tid = "suggestionId" in q ? s?.trigger_id ?? "" : q.triggerId;
      const w = this.bot.why[tid];
      if (!w) throw http(404, `no trigger ${tid}`);
      const out = structuredClone(w);
      for (const o of out.outputs) o.status = this.bot.suggestions.find((x) => x.suggestion_id === o.id)?.status ?? o.status;
      if (s && s.status !== "pending" && s.status !== "shown") out.reaction = { status: s.status, reacted_at: this.now().toISOString() };
      return out;
    });
  }

  /** Newest first; the silent rows only with `skipped`. */
  botRecent(skipped: boolean): Promise<ActivityOut[]> {
    return this.run("botRecent", [skipped], () => this.bot.activity.filter((r) => skipped || r.result !== "skipped")
      .sort((a, b) => Date.parse(b.at) - Date.parse(a.at)));
  }

  /** today_new = unreacted (pending / shown), unexpired today cards (suggestions.badge); pending_actions = proposed actions. */
  /** GET /wiki/writes: the runs of `writesNow` (tests set it), filtered by the ids; a page without a run is absent. */
  writes(pageIds?: string[]): Promise<WriteOut[]> {
    return this.run("writes", pageIds === undefined ? [] : [pageIds], () => this.writesNow.filter((w) => pageIds === undefined
      ? w.state === "queued" || w.state === "planning" || w.state === "writing" : pageIds.includes(w.page_id)));
  }

  /** GET /wiki/pages/{id}: the page as openPage shows it, read only. */
  getPage(pageId: string): Promise<PageOut> { return this.run("getPage", [pageId], () => this.pageOut(this.page(pageId), null)); }

  /** GET /wiki/files answers from this list (tests set it). */
  filesNow: FileOut[] = [];
  files(): Promise<FileOut[]> { return this.run("files", [], () => this.filesNow); }

  /** GET /wiki/pages/{id}/run/stream replays this exact task; a missing or foreign task is 404. */
  runEvents: Record<string, { pageId: string; events: OrganizeEvent[] }> = {};
  runStream(pageId: string, runId: string, on: (e: OrganizeEvent) => void, o: StreamOpts = {}): Promise<void> {
    return this.run("runStream", [pageId, runId, on, o], () => {
      const run = this.runEvents[runId];
      if (run === undefined || run.pageId !== pageId) throw new ApiError(404, "没有这一页的写作任务");
      return run.events;
    }).then((evs) => { for (const e of evs) { if (o.signal?.aborted) throw new AbortedError(); on(e); } });
  }

  /** POST /wiki/pages/{id}/run/cancel: the fake's runs are its streams, which the caller aborts; nothing else to stop. */
  cancelRun(pageId: string): Promise<void> { return this.run("cancelRun", [pageId], () => undefined); }

  botBadge(): Promise<BadgeOut> {
    return this.run("botBadge", [], () => ({ today_new: this.bot.suggestions.filter((s) => s.placement === "today" && (s.status === "pending" || s.status === "shown")
      && Date.parse(s.expires_at) > this.now().getTime()).length,
      pending_actions: this.bot.actions.filter((a) => a.status === "proposed" || this.botStale(a)).length }));
  }

  botSettings(): Promise<BotSettings> { return this.run("botSettings", [], () => this.bot.settings); }

  /**
   * 422 for a number outside bot/settings.py's bounds, an unknown time zone, paused=until without a time, or a level the scope does not allow
   * (the texts B3 sends in `detail[0].msg`, without pydantic's "Value error, " prefix).
   */
  putBotSettings(s: BotSettings): Promise<BotSettings> {
    return this.run("putBotSettings", [s], () => {
      for (const [key, [lo, hi]] of Object.entries(BOT_BOUNDS)) {
        const x = s[key as keyof typeof BOT_BOUNDS];
        if (typeof x !== "number" || x < lo || x > hi) throw http(422, `${key} must be ${lo}–${hi}`);
      }
      try { new Intl.DateTimeFormat("en", { timeZone: s.timezone }); } catch { throw http(422, `unknown time zone '${s.timezone}'`); }
      if (s.paused === "until" && s.paused_until === null) throw http(422, "paused=until needs paused_until");
      for (const [scope, level] of Object.entries(s.levels)) {
        const fixed = BOT_FIXED[scope];
        if (fixed !== undefined) {
          if (level !== fixed) throw http(422, `${scope} 的审批级别固定为 ${fixed}，不能修改`);
        } else if (!BOT_CHOICES[scope]?.includes(level)) {
          const choices = BOT_CHOICES[scope] ?? [];
          throw http(422, `${scope} 的级别只能是 ${choices.join(" / ")}`);
        }
      }
      this.bot.settings = structuredClone(s);
      return this.bot.settings;
    });
  }

  /** tomorrow = until the end of the local day (B3 pause); forever; off. */
  botPause(until: PauseUntil): Promise<BotSettings> {
    return this.run("botPause", [until], () => {
      const s = this.bot.settings;
      if (until === "tomorrow") { s.paused = "until"; s.paused_until = localDayEnd(this.now(), s.timezone); }
      else { s.paused = until === "forever" ? "always" : "no"; s.paused_until = null; }
      return s;
    });
  }

  /** B5: the goals with the fake's parked set; no weekly review or reading data in the fake (reading unknown). */
  botGoals(): Promise<BotGoalOut[]> {
    return this.run("botGoals", [], () => this.graphFx.goalPaths().map((g) => this.goalOut(g.path_id, g.goal)));
  }
  reviseFromSource(sourceId: string, pageIds: string[]): Promise<SourceReviseOut> {
    return this.run("reviseFromSource", [sourceId, pageIds], () => ({
      started: pageIds.map((page_id, i) => ({ action_id: `fake-rv-${sourceId}-${i}`, run_id: `fake-run-${sourceId}-${i}`, page_id })),
    }));
  }
  botPark(pathId: string, park: boolean): Promise<BotGoalOut> {
    return this.run("botPark", [pathId, park], () => {
      const g = this.graphFx.goalPaths().find((x) => x.path_id === pathId);
      if (!g) throw http(404, `没有这条学习路径：${pathId}`);
      if (park && !this.parked.has(pathId)) this.parked.set(pathId, this.now().toISOString());
      if (!park) this.parked.delete(pathId);
      return this.goalOut(g.path_id, g.goal);
    });
  }
  private goalOut(pathId: string, goal: string): BotGoalOut {
    return { path_id: pathId, goal, parked: this.parked.has(pathId), parked_at: this.parked.get(pathId) ?? null,
      last_review: null, reading_minutes: null };
  }
  replanPath(pathId: string): Promise<PathEditOut> {
    return this.run("replanPath", [pathId], () => this.graphFx.pathOf(pathId));
  }
  /** Recorded as sent (`reading`); a batch id seen before is a duplicate, like the server. */
  postReading(batchId: string, entries: ReadingEntry[]): Promise<BotReadingOut> {
    return this.run("postReading", [batchId, entries], () => {
      if (this.readingBatches.has(batchId)) return { accepted: 0, dropped: 0, duplicate: true };
      this.readingBatches.add(batchId);
      this.reading.push(...structuredClone(entries));
      return { accepted: entries.length, dropped: 0, duplicate: false };
    });
  }
  readonly reading: ReadingEntry[] = [];
  private readonly readingBatches = new Set<string>();
  private readonly parked = new Map<string, string>();

  private botStale(a: ActionOut): boolean {
    return a.status === "approved" && Date.parse(a.decided_at ?? a.created_at) < this.now().getTime() - BOT_APPROVED_STALE_MS;
  }

  /** pending / shown / accepted: a started card stays listed until its day ends (B3). */
  private botListable(s: SuggestionOut): boolean {
    return s.status === "pending" || s.status === "shown" || s.status === "accepted";
  }

  /** The ledger change an executed action leaves (B3 LedgerChangeSource: patch_id === action_id, track G). */
  private ledgerRow(a: ActionOut): RsiChangeOut {
    const at = a.decided_at ?? this.now().toISOString();
    return { patch_id: a.action_id, track: "G", status: "personal_accepted", summary: a.summary, why: a.why, signals: [], before: "", after: "", at,
      can_undo: a.kind === "prompt_override" ? a.can_undo : botCanUndo(a), reading: "", kind: a.scope, rule_before: "", rule_after: "", user_edited: false, layer: 0, target: "知识库", outcome: "pending",
      effect: [], effect_line: "", pages_waiting: 0, pages_failed: 0, tool_live: false };
  }

  private botAction(id: string): ActionOut {
    const a = this.bot.actions.find((x) => x.action_id === id);
    if (!a) throw http(404, `no action ${id}`);
    return a;
  }

  /** executed and undoable only; a preference undo puts its card back and rolls its change row back. */
  private undoBotAction(a: ActionOut): ActionOut {
    if (a.status !== "executed") throw http(409, `动作已经是 ${a.status}，不能撤销`);
    if (a.level === "auto") throw http(409, "这类动作不需要撤销");   // an ask / auto_undo row goes to its executor, which undoes it
    if (a.kind === "prompt_override" && !a.can_undo) throw http(409, "这一步的写法后来又改过了，先撤销后来的那次");
    a.status = "undone";
    a.can_undo = false;
    a.decided_at = this.now().toISOString();
    if (a.kind === "prompt_override") {
      const step = String(a.content.step);
      if (typeof a.content.before === "string") this.promptEdits.set(step, a.content.before); else this.promptEdits.delete(step);
    }
    if (a.kind === "preference") {
      // the counter goes back by one; the card is listed again unless its day is over
      const dedup = typeof a.content.dedup_key === "string" ? a.content.dedup_key : "";
      const s = this.bot.suggestions.find((x) => x.suggestion_id === a.content.suggestion_id);
      if (s || dedup === "review_due") this.bot.prefs.set(dedup, Math.max(0, (this.bot.prefs.get(dedup) ?? 0) - 1));
      if (dedup === "review_due" && "previous_at" in a.content) this.bot.reviewLessAt = a.content.previous_at as number | null;
      if (s) {
        if (Date.parse(s.expires_at) > this.now().getTime()) s.status = "shown";
        s.preference_action_id = null;
      }
    }
    // a page write taken back: its 最近做了什么 row is struck through (the page goes to the trash)
    for (const r of this.bot.activity) if (r.write != null && r.write.action_id === a.action_id) { r.write.undone = true; r.write.can_undo = false; }
    const c = this.rsi.find((x) => x.patch_id === a.action_id);
    if (c) this.decide(c, "rolled_back");
    return a;
  }

  // ---------------- teaching spec (wikiapp/teach.py, teachspec.py) ----------------

  teachSpec(): Promise<TeachSpecOut> { return this.run("teachSpec", [], () => this.specOut()); }

  /** 待确认的记忆 (memory/conflicts.py): the open groups; tests set them with __setConflicts (settling is the chat's). */
  private memConflicts: MemoryConflictOut[] = [];
  memoryConflicts(): Promise<MemoryConflictOut[]> { return this.run("memoryConflicts", [], () => structuredClone(this.memConflicts)); }
  __setConflicts(groups: MemoryConflictOut[]): void { this.memConflicts = structuredClone(groups); }

  addSpecItem(body: SpecItemIn, base?: number): Promise<TeachSpecOut> {
    return this.run("addSpecItem", [body, base], () => {
      const text = checkItemText(body.text);
      const scope = body.scope ?? "any";
      const domain = body.domain ?? null;
      checkScope(scope);
      checkDomain(domain);
      this.writeSpec(base, (s) => {
        return { ...s, items: [...s.items, specItem({ item_id: `si${++this.seq.specItem}`, text, scope,
          domain: normDomain(domain), kind: "user" })] };
      });
      // the checkpoint's `reaccepted`: an earlier rejection / revocation of this text no longer holds
      this.specDecisions = this.specDecisions.filter((d) => d.text.trim() !== text);
      return this.specOut();
    });
  }

  /** teach.take_over_flow: a pending or RSI rule becomes the user's, unedited (already the user's → 422). */
  takeOverSpecItem(itemId: string, base?: number): Promise<TeachSpecOut> {
    return this.run("takeOverSpecItem", [itemId, base], () => {
      const cur = this.effectiveSpec().items.find((i) => i.item_id === itemId);
      if (cur === undefined) throw http(404, `没有这条规则：${itemId}`);
      if (cur.authority === "user") throw http(422, "这条已经是你的规则了");
      return this.writeSpec(base, (s) => ({ ...s, items: s.items.map((i) => (i.item_id === itemId ? { ...i, authority: "user" as const } : i)) }));
    });
  }

  /** Only the keys sent change (`domain: null` clears it); the rule becomes source user. */
  editSpecItem(itemId: string, body: SpecItemPatchIn, base?: number): Promise<TeachSpecOut> {
    return this.run("editSpecItem", [itemId, body, base], () => {
      const sent = body;
      if (Object.values(sent).every((v) => v === undefined)) throw http(422, "请至少改一项：text、scope 或 domain");
      const text = sent.text === undefined ? undefined : checkItemText(sent.text);
      if (sent.scope !== undefined && sent.scope !== null) checkScope(sent.scope);
      if (sent.domain !== undefined) checkDomain(sent.domain);
      return this.writeSpec(base, (s) => {
        const n = itemIndex(s, itemId);
        const old = s.items[n] as SpecItem;
        const item: SpecItem = { ...old, text: text ?? old.text, scope: sent.scope ?? old.scope,
          domain: sent.domain === undefined ? old.domain : normDomain(sent.domain), authority: "user", rev: old.rev + 1,
          rev_by: "user" };
        return { ...s, items: s.items.map((x, i) => (i === n ? item : x)) };
      });
    });
  }

  deleteSpecItem(itemId: string, base?: number): Promise<TeachSpecOut> {
    return this.run("deleteSpecItem", [itemId, base], () => {
      const gone = this.effectiveSpec().items.find((i) => i.item_id === itemId);
      this.writeSpec(base, (s) => {
        const n = itemIndex(s, itemId);
        return { ...s, items: s.items.filter((_, i) => i !== n) };
      });
      // rules/authority.py: removing a pending rule is `rejected`, any other one the user had is `revoked`
      if (gone !== undefined) {
        this.specDecisions.unshift({ decision_id: `rd${++this.seq.specItem}`, rule_id: gone.item_id, text: gone.text,
          action: gone.authority === "pending" ? "rejected" : "revoked", via: "ui" });
      }
      return this.specOut();
    });
  }

  /** wiki_page: the server snapshots the organized page (unknown 404, not organized 422); vault_note: the upload. */
  addExemplar(body: ExemplarIn, base?: number): Promise<TeachSpecOut> {
    return this.run("addExemplar", [body, base], () => {
      const b = body as { kind?: string; ref?: string; title?: string; text?: string };
      const ref = b.ref ?? "";
      if (!ref || ref.length > MAX_EXEMPLAR_REF) throw http(422, `ref 1–${MAX_EXEMPLAR_REF} 字`);
      let ex: Omit<Exemplar, "exemplar_id">;
      if (b.kind === "wiki_page") {
        const p = this.pages.get(ref);
        if (!p) throw http(404, `没有这一页：${ref}`);
        if (!p.organized) throw http(422, "这一页还没整理好，不能做范例");
        const text = this.pageMarkdown(p);
        if (text.length > MAX_EXEMPLAR_TEXT) throw http(422, `范例最多 ${MAX_EXEMPLAR_TEXT} 字，这一页有 ${text.length} 字`);
        ex = { kind: "wiki_page", ref, title: p.title.trim().slice(0, MAX_EXEMPLAR_TITLE) || ref.slice(0, MAX_EXEMPLAR_TITLE), text };
      } else if (b.kind === "vault_note") {
        const title = b.title ?? "";
        const text = b.text ?? "";
        if (!title || title.length > MAX_EXEMPLAR_TITLE) throw http(422, `标题 1–${MAX_EXEMPLAR_TITLE} 字`);
        if (!text || text.length > MAX_EXEMPLAR_TEXT) throw http(422, `范例 1–${MAX_EXEMPLAR_TEXT} 字`);
        ex = { kind: "vault_note", ref, title, text };
      } else {
        throw http(422, "未知的范例类型");
      }
      return this.writeSpec(base, (s) => {
        if (s.exemplars.length >= MAX_EXEMPLARS) throw http(422, TOO_MANY_EXEMPLARS);
        return { ...s, exemplars: [...s.exemplars, { exemplar_id: `ex${++this.seq.exemplar}`, ...ex }] };
      });
    });
  }

  deleteExemplar(exemplarId: string, base?: number): Promise<TeachSpecOut> {
    return this.run("deleteExemplar", [exemplarId, base], () => this.writeSpec(base, (s) => {
      if (s.exemplars.every((e) => e.exemplar_id !== exemplarId)) throw http(404, `没有这个范例：${exemplarId}`);
      return { ...s, exemplars: s.exemplars.filter((e) => e.exemplar_id !== exemplarId) };
    }));
  }

  // ---------------- interactives (service/routes_refine.py) ----------------

  assetGenerated(text: string, kind: "evoui" | "html"): Promise<AssetGeneratedOut> {
    return this.run("assetGenerated", [text, kind], () => ({ generated: false }));
  }
  assetMeta(assetId: string): Promise<AssetMeta> {
    return this.run("assetMeta", [assetId], () => this.asset(assetId).meta);
  }

  assetHtml(assetId: string): Promise<string> {
    return this.run("assetHtml", [assetId], () => this.asset(assetId).html);
  }

  /**
   * POST checks/{sid}/followup as the server does it: 404 unknown page / section / answer, 422 not a wrong answer (or a slip
   * on 不知道), 409 already followed up; slip / learned: 503 while the model is down (__llmDown), otherwise a new question on
   * the same cell is scheduled one hour out (use failNext for the 502); unclear: where to go back to, the quote, no model.
   */
  followup(pageId: string, sid: string, body: CheckFollowupIn): Promise<CheckFollowupOut> {
    return this.run("followup", [pageId, sid, body], () => {
      if (!(FOLLOWUP_CHOICES as readonly string[]).includes(body.choice)) throw http(422, "bad choice");
      const sec = this.section(pageId, sid);
      const ref = body.answer_ref ?? null;
      const mine = this.answers.filter((x) => (ref !== null ? x.ref === ref : x.pageId === pageId && x.sid === sid));
      const a = mine[mine.length - 1];
      if (!a) throw http(404, "这一段没有找到这次作答");
      if (!a.wrong) throw http(422, "这次作答没有答错");
      if (a.followed) throw http(409, "这道题已经选过「为什么答错」了");
      const idk = !a.recall && a.choice === IDK;
      if (body.choice === "slip" && idk) throw http(422, "选了「不知道」不算粗心");
      const { conceptId } = splitCell(a.cell);
      const name = this.conceptName(conceptId);
      const from = this.band(a.cell);
      const id = `fu${++this.seq.followup}`;
      const teach = this.teaching(pageId, a.cell);
      const base = { followup_id: id, choice: body.choice, cell: a.cell, concept_name: name, llm_calls: 0 };
      if (body.choice === "unclear") {
        const to = DOWN[from];
        this.setBand(a.cell, to);
        this.addNote(a.cell, "self", "看不懂", `followup:${id}`);
        a.followed = true;
        const heading = teach?.heading ?? sec.fx.heading;
        const text = a.recall ? `回想提示：${a.cue}\n我的回想：${a.pick}`
          : `题目：${a.stem}\n选项：\n${a.options.map((o, j) => `${j + 1}) ${o}`).join("\n")}\n我选的：${idk ? "不知道" : a.pick}\n正确答案：${a.options[a.answer] ?? ""}`;
        const signal = this.recordSignal("check_unclear", teach?.page_id ?? pageId, teach?.section_id ?? sid, [], text.slice(0, 2000));
        return { ...base, status: "done", confirm_due: null, band: { cell: a.cell, from, to }, review: teach,
          suggest: teach ? null : { concept_id: conceptId, concept_name: name, goal: `弄懂「${name}」` },
          quote: { kind: "wrong_answer", text: text.slice(0, 4000), heading: heading || null }, signal_id: signal.signal_id };
      }
      if (this.llmUnavailable) throw http(503, "出确认题需要 LLM");
      // A new question on the same cell: another stem, the options turned around (never the original).
      const options = [...a.options].reverse();
      const answer = options.length - 1 - a.answer;
      const to = body.choice === "slip" ? UP[from] : from;
      if (to !== from) this.setBand(a.cell, to);
      a.followed = true;
      const due = new Date(this.now().getTime() + CONFIRM_DELAY_MS);
      this.followups.push({ id, kind: body.choice, cell: a.cell, pageId, sid, stem: `换一个角度：${a.stem}`, options, answer, due, outcome: null, teach });
      return { ...base, status: "pending", confirm_due: due.toISOString(), band: { cell: a.cell, from, to }, review: null, suggest: null,
        quote: null, signal_id: null };
    });
  }

  /** POST review/confirms/{id}: 404 unknown, 409 answered, 422 no such option; works with the model down. */
  confirmAnswer(followupId: string, body: ConfirmAnswerIn): Promise<ConfirmAnswerOut> {
    return this.run("confirmAnswer", [followupId, body], () => {
      const f = this.followups.find((x) => x.id === followupId);
      if (!f) throw http(404, "没有这道确认题");
      if (f.outcome !== null) throw http(409, "这道确认题已经答过了");
      const choice = body.choice;
      if (!Number.isInteger(choice) || choice < IDK || choice >= f.options.length) throw http(422, "没有这个选项");
      const correct = choice === IDK ? null : choice === f.answer;
      f.outcome = correct ? "confirmed" : "refuted";
      const from = this.band(f.cell);
      const to = correct ? UP[from] : DOWN[from];
      if (to !== from) this.setBand(f.cell, to);
      this.addNote(f.cell, "check", choice === IDK ? f.stem : f.options[choice] ?? "", `confirm:${f.id}`);
      let signal_id: string | null = null;
      let reteach: ReviewTargetOut | null = null;
      if (f.kind === "learned" && f.outcome === "refuted") {
        signal_id = this.recordSignal("check_unclear", f.teach?.page_id ?? f.pageId, f.teach?.section_id ?? f.sid, [], `说学会了，确认题没答对：${f.stem}`).signal_id;
        reteach = f.teach;
      }
      return { followup_id: f.id, correct, correct_index: f.answer, outcome: f.outcome, band: { cell: f.cell, from, to }, reteach, signal_id };
    });
  }

  /** The prose section that teaches `cell`: the answer's page first, then any organized page in page order. */
  private teaching(pageId: string, cell: string): ReviewTargetOut | null {
    const first = this.pages.get(pageId);
    for (const p of [...(first ? [first] : []), ...[...this.pages.values()].filter((x) => x.id !== pageId)]) {
      if (!p.organized) continue;
      const s = p.sections.find((x) => x.role !== "check" && x.fx.cells.includes(cell));
      if (s) return { page_id: p.id, page_title: p.title, section_id: s.fx.id, heading: s.fx.heading };
    }
    return null;
  }

  /** Recorded in `calls`; nothing else happens (the server's grading is not modelled). */
  blockEvents(assetId: string, events: readonly BlockEvent[]): Promise<{ inK: boolean }> {
    return this.run("blockEvents", [assetId, events], () => ({ inK: false }));
  }

  /** Stored block progress by asset id (a test may seed it). */
  readonly blockProgressStore = new Map<string, BlockProgressIn>();
  blockProgress(assetId: string): Promise<BlockProgressOut> {
    return this.run("blockProgress", [assetId], () => this.blockProgressStore.get(assetId) ?? { revision: 0, data: null });
  }
  /** Kept only when the revision is newer (as the server does). */
  saveBlockProgress(assetId: string, progress: BlockProgressIn): Promise<{ stale: boolean }> {
    return this.run("saveBlockProgress", [assetId, progress], () => {
      if (progress.revision <= (this.blockProgressStore.get(assetId)?.revision ?? 0)) return { stale: true };
      this.blockProgressStore.set(assetId, progress);
      return { stale: false };
    });
  }

  // ---------------- the node graph (graph.html's data, graph-page/fixture.ts) ----------------

  graph(): Promise<GraphOut> { return this.run("graph", [], () => this.graphFx.graph()); }
  splitNode(nodeId: string): Promise<SplitOut> { return this.run("splitNode", [nodeId], () => this.graphFx.split(nodeId)); }
  wantNode(nodeId: string, mapId: string): Promise<NodeActionOut> {
    return this.run("wantNode", [nodeId, mapId], () => this.graphFx.want(nodeId, mapId));
  }
  editPath(pathId: string, edit: PathEditIn): Promise<PathEditOut> {
    return this.run("editPath", [pathId, edit], () => this.graphFx.editPath(pathId, edit));
  }
  splitRow(pathId: string, pageKey: string): Promise<PathEditOut> { return this.run("splitRow", [pathId, pageKey], () => this.graphFx.splitRow(pathId, pageKey)); }
  undoSplitRow(pathId: string, pageKey: string, changeId: string): Promise<PathEditOut> {
    return this.run("undoSplitRow", [pathId, pageKey, changeId], () => this.graphFx.undoSplitRow(pathId, pageKey, changeId));
  }

  // ---------------- sources (资料) ----------------

  listSources(): Promise<SourceOut[]> { return this.run("listSources", [], () => this.sources.list()); }
  getSource(sourceId: string): Promise<SourceDetail> { return this.run("getSource", [sourceId], () => this.sources.detail(sourceId)); }
  uploadSource(input: SourceUploadIn): Promise<SourceOut> {
    return this.run("uploadSource", [input], () => this.sources.upload(input));
  }
  /** A reading source steps through its stages (delayMs apart) and ends; an ended one answers its end at once. 404 first. */
  followSource(sourceId: string, on: (e: SourceEvent) => void, o: StreamOpts = {}): Promise<void> {
    return this.stream<SourceEvent>("followSource", [sourceId, on, o], on, o.signal, async (s) => {
      for (const p of this.sources.steps(sourceId)) {
        await s.gate();
        this.sources.record(sourceId, p);
        s.deliver({ event: "progress", data: p });
      }
      await s.gate();
      s.deliver(this.sources.finish(sourceId));
    });
  }
  addSourceUse(sourceId: string, use: SourceUseIn): Promise<SourceUseOut> {
    return this.run("addSourceUse", [sourceId, use], () => this.sources.addUse(sourceId, use));
  }
  renameSource(sourceId: string, title: string): Promise<SourceOut> {
    return this.run("renameSource", [sourceId, title], () => this.sources.rename(sourceId, title));
  }
  setSourceOrigin(sourceId: string, origin: string): Promise<SourceOut> {
    return this.run("setSourceOrigin", [sourceId, origin], () => this.sources.setOrigin(sourceId, origin));
  }
  editSections(sourceId: string, edits: SectionEditIn[]): Promise<SourceDetail> {
    return this.run("editSections", [sourceId, edits], () => this.sources.editSections(sourceId, edits));
  }
  retrySource(sourceId: string): Promise<SourceOut> {
    return this.run("retrySource", [sourceId], () => this.sources.retry(sourceId));
  }

  deleteSource(sourceId: string): Promise<void> {
    return this.run("deleteSource", [sourceId], () => { this.sources.remove(sourceId); });
  }
  sourceFile(sourceId: string): Promise<ArrayBuffer> { return this.run("sourceFile", [sourceId], () => this.sources.file(sourceId)); }

  // ---------------- 真题 ----------------

  pageExam(pageId: string): Promise<PageExamOut> {
    return this.run("pageExam", [pageId], () => this.exams.get(pageId)
      ?? { asset_id: null, quiz_count: 0, open_items: [], sources: [], frequent: false });
  }
  /** The server's rules: 404 an unknown item, 422 right / wrong without an answer, a second report today is a duplicate. */
  examSelf(itemId: string, result: ExamSelfIn["result"]): Promise<ExamSelfOut> {
    return this.run("examSelf", [itemId, result], () => {
      const it = [...this.exams.values()].flatMap((e) => e.open_items).find((x) => x.item_id === itemId);
      if (it === undefined) throw http(404, "没有这道题");
      if (result !== "done" && !it.has_key) throw http(422, "这道题没有答案，只能标做完");
      if (it.done) return { status: "duplicate" as const };
      it.done = true;
      return { status: result === "done" ? "done" as const : "score" as const };
    });
  }
  /** Test hook: what GET /wiki/pages/{id}/exam answers for a page (null: the empty block again). */
  __setPageExam(pageId: string, exam: PageExamOut | null): void {
    if (exam === null) this.exams.delete(pageId); else this.exams.set(pageId, structuredClone(exam));
  }
  /** Test hook: the 真题 summary of a source (GET /wiki/sources/{id}). */
  __setSourceExam(sourceId: string, exam: ExamSummaryOut | null): void { this.sources.setExam(sourceId, exam); }

  /** Test hook: the next new source's import ends with an error (`detail`) and the source is failed. */
  __failSourceImport(detail: string): void { this.sources.failNextImport(detail); }

  /** Test hook: store (or replace) an interactive. */
  __putAsset(meta: AssetMeta, html: string): void {
    this.assets.set(meta.asset_id, { meta: structuredClone(meta), html });
  }

  // ---------------- plumbing ----------------

  private takeFailure(method: keyof WikiApi, args: unknown[]): Failure | undefined {
    this.calls.push({ method, args });
    // A follow-up of kind unclear never reaches the model; slip / learned write the confirmation question with it.
    const noLlm = NO_LLM.has(method) || (method === "followup" && (args[2] as CheckFollowupIn | undefined)?.choice === "unclear");
    if (this.budget && !READS.has(method) && !noLlm) return STREAMING.has(method)
      ? { code: "budget_exceeded", retryable: false, detail: BUDGET_DETAIL }
      : new ApiError(429, BUDGET_DETAIL);
    const f = this.failures.get(method);
    this.failures.delete(method);
    return f;
  }

  /** JSON call: record, maybe fail, run, return a fresh copy. */
  private run<T>(method: keyof WikiApi, args: unknown[], fn: () => T): Promise<T> {
    const f = this.takeFailure(method, args);
    if (f instanceof Error) return Promise.reject(f);
    try { return Promise.resolve(structuredClone(fn())); } catch (e) { return Promise.reject(e instanceof Error ? e : new Error(String(e))); }
  }

  /**
   * SSE call. `gate` waits delayMs (except before the first event) and throws AbortedError once the signal is
   * aborted; `deliver` hands a copy to `on`; `emit` = gate + deliver. Bodies commit state after a gate, so an
   * abort before the commit leaves the state as it was (a rewrite aborted between `change` and `done` keeps its change).
   */
  private async stream<E extends { event: string }>(
    method: keyof WikiApi, args: unknown[], on: (e: E) => void, signal: AbortSignal | undefined,
    body: (s: { gate(): Promise<void>; deliver(e: E): void; emit(e: E): Promise<void> }) => Promise<void>,
  ): Promise<void> {
    const f = this.takeFailure(method, args);
    if (f instanceof Error) throw f;
    let first = true;
    const gate = async (): Promise<void> => {
      if (!first) await this.pause(signal);
      first = false;
      if (signal?.aborted) throw new AbortedError();
    };
    const deliver = (e: E): void => { on(structuredClone(e)); };
    const emit = async (e: E): Promise<void> => { await gate(); deliver(e); };
    // a source's progress stream ends with SourceFailed {detail}; the page streams with the whole ErrorEvent
    if (f) { await emit({ event: "error", data: method === "followSource" ? { detail: f.detail } : f } as unknown as E); return; }
    await body({ gate, deliver, emit });
  }

  private pause(signal: AbortSignal | undefined): Promise<void> {
    if (this.delayMs <= 0 || signal?.aborted) return Promise.resolve();
    return new Promise<void>((resolve) => {
      const finish = (): void => { clearTimeout(timer); signal?.removeEventListener("abort", finish); resolve(); };
      const timer = setTimeout(finish, this.delayMs);
      signal?.addEventListener("abort", finish, { once: true });
    });
  }

  // ---------------- state helpers ----------------

  private band(cell: string): Band { return this.bands.get(cell) ?? "none"; }
  /** Every change is logged for the digest (forgot: a 没想起来 self-grade caused it). */
  /** The non-clearing branch of declare: remember the band before, set the declared one, note it. */
  private markDeclared(cell: string, value: Declared): void {
    if (!this.beforeDeclare.has(cell)) this.beforeDeclare.set(cell, this.band(cell));
    this.declared.set(cell, value);
    this.setBand(cell, value === "know" ? "known" : "gap");
    this.addNote(cell, "declare", value, `k/${cell}`);
  }

  private setBand(cell: string, b: Band, forgot = false): void {
    const from = this.band(cell);
    this.bands.set(cell, b);
    if (from !== b) this.bandLog.push({ at: this.now(), cell, from, to: b, forgot });
  }
  /** The acting page saw this change happen: refresh its snapshot for these cells only (other pages stay stale). */
  private touchSeen(pageId: string | null, cells: readonly string[]): void {
    const p = pageId === null ? undefined : this.pages.get(pageId);
    if (!p) return;
    for (const c of cells) if (p.seen.has(c)) p.seen.set(c, this.band(c));
  }
  private allKnown(cells: readonly string[]): boolean { return cells.every((c) => this.band(c) === "known"); }
  private conceptName(id: string): string { return CONCEPT_BY_ID.get(id)?.name ?? id; }

  private addNote(cell: string, kind: NoteKind, text: string, ref: string): void {
    this.notes.push({ entry_id: `n${++this.seq.note}`, cell, kind, text, evidence_ref: ref, label: null });
  }

  private draftConcept(id: string, target: boolean, reused: boolean): DraftConceptOut {
    const c = CONCEPT_BY_ID.get(id);
    return { id, name: c?.name ?? id, target, reused, aspects: [...(c?.aspects ?? [])], prereqs: [...(c?.prereqs ?? [])] };
  }

  private addPage(title: string, goal: string, target: string, outline: OutlineFixture): PageRec {
    const p: PageRec = { id: `p${this.pages.size + 1}`, title, goal, target, outline, organized: false, sections: [],
      collapsed: false, done: false, seen: new Map() };
    p.seen = this.snapshot(p);
    this.pages.set(p.id, p);
    this.currentPage = p.id;
    return p;
  }

  private page(pageId: string): PageRec {
    const p = this.pages.get(pageId);
    if (!p) throw http(404, "找不到这一页");
    return p;
  }

  private section(pageId: string, sid: string): SectionRec {
    const s = this.page(pageId).sections.find((x) => x.fx.id === sid);
    if (!s) throw http(404, "找不到这一段");
    return s;
  }

  /** Section cells then assumed cells, in order, without repeats. */
  private pageCells(p: PageRec): string[] {
    return [...new Set([...p.outline.sections.flatMap((s) => s.cells), ...p.outline.assumed])];
  }

  private snapshot(p: PageRec): Map<string, Band> {
    return new Map(this.pageCells(p).map((c) => [c, this.band(c)]));
  }

  private isStale(p: PageRec): boolean {
    return this.pageCells(p).some((c) => (p.seen.get(c) ?? this.band(c)) !== this.band(c));
  }

  private othersUsing(cells: readonly string[], exceptPage: string): string[] {
    return [...this.pages.values()]
      .filter((p) => p.id !== exceptPage && this.pageCells(p).some((c) => cells.includes(c))).map((p) => p.id);
  }

  private freshSection(fx: SectionFixture): SectionRec {
    return {
      fx, role: fx.role, cue: fx.cue, body: fx.body, folded: foldable(fx.role) && this.allKnown(fx.cells), fold_locked: false,
      pinned: false, changed: null,
      check: fx.check ? { stem: fx.check.stem, options: [...fx.check.options], answered: null, correct_index: null, correct: null, artifact_id: null } : null,
    };
  }

  private cellOut(cell: string): CellOut {
    const { conceptId, aspect } = splitCell(cell);
    return { cell, concept_id: conceptId, concept_name: this.conceptName(conceptId), aspect: aspect as Aspect,
      band: this.band(cell), declared: this.declared.get(cell) ?? null };
  }

  private belief(cell: string): CellBeliefOut {
    const { conceptId, aspect } = splitCell(cell);
    const b = this.band(cell);
    const [m, u] = MU[b];
    return { cell, concept_id: conceptId, aspect: aspect as Aspect, band: b, m, u, recall: 1,
      direct: this.notes.filter((n) => n.cell === cell).length, inferred_pos: 0, inferred_neg: 0, inferred_only: false,
      declared: this.declared.get(cell) ?? null };
  }

  private sectionOut(s: SectionRec): SectionOut {
    return {
      id: s.fx.id, heading: s.fx.heading, role: s.role, cue: s.cue, body: s.body, note_body: s.noteBody ?? null, folded: s.folded, fold_locked: s.fold_locked,
      foldable: foldable(s.role), pinned: s.pinned, changed: s.changed, why: s.fx.why, cells: s.fx.cells.map((c) => this.cellOut(c)),
      lookups: [...(s.fx.lookups ?? [])], fingerprint: `${s.fx.heading}|${s.body}`, artifact_id: fakeArtifactId(`text:${s.fx.id}:${s.body}`), check: s.check ? { ...s.check, artifact_id: fakeArtifactId(`check:${s.fx.id}:${s.check.stem}`), options: [...s.check.options] } : null,
    };
  }

  /** Line sections whose cells are now shaky / gap: the text line and its structured twin. */
  private conflicts(p: PageRec): { text: string; out: ConflictOut }[] {
    const out: { text: string; out: ConflictOut }[] = [];
    for (const s of p.sections) {
      if (s.role !== "line") continue;
      for (const c of s.fx.cells) {
        const b = this.band(c);
        if (b !== "shaky" && b !== "gap") continue;
        const { conceptId, aspect } = splitCell(c);
        out.push({ text: `「${s.fx.heading}」只一行带过，但其中 ${this.conceptName(conceptId)}·${ASPECT_FULL[aspect as Aspect]} 现在是${BAND_ZH[b]}`,
          out: { kind: s.pinned ? "pinned_line_weak" : "line_weak", section_id: s.fx.id, cell: c } });
      }
    }
    return out;
  }

  private pageOut(p: PageRec, diff: DiffNoteOut | null): PageOut {
    const org = p.organized;
    const path = [...this.paths.values()].find((x) => x.pages.some((pg) => pg.pageId === p.id));
    return {
      next_step: path === undefined ? null : this.pathStep(path, p.id),
      page_id: p.id, title: p.title, folder: "", parent_name: "", trashed: false, goal: p.goal, organized: org, source: org ? "organizer" : null,
      profile: org ? p.outline.profile : "", assumed: org ? p.outline.assumed.map((c) => this.cellOut(c)) : [],
      sections: org ? p.sections.map((s) => this.sectionOut(s)) : [], diff,
      unresolved_conflicts: org ? this.conflicts(p).map((c) => c.text) : [],
      conflicts: org ? this.conflicts(p).map((c) => c.out) : [], budget_exceeded: false,
      collapsed: p.collapsed, can_shelve: this.canShelve(p),
      written_for: "", goal_notes: [], shelf_line: org ? p.sections.find((s) => s.cue.trim() !== "")?.cue ?? "" : "", done: p.done, llm_calls: 0,
      writer: org ? { model: FAKE_WRITER.model, at: FAKE_WRITER.at, cost_usd: 0, fallback_reason: null } : null,
      feedback: org ? this.feedbackMark(p) : null, hand_edit: null, rewriting: false, sources: [],
    };
  }

  // ---------------- signal / RSI / spec / asset helpers ----------------

  private recordSignal(kind: FakeSignal["kind"], pageId: string, sid: string | null, chips: FeedbackChip[],
                       text: string): { signal_id: string } {
    const p = this.pages.get(pageId);
    if (!p) throw http(404, `没有这个页面：${pageId}`);
    if (sid !== null && !(p.organized && p.sections.some((s) => s.fx.id === sid))) throw http(422, `这一页没有这一段：${sid}`);
    this.signals.push({ kind, page_id: pageId, section_id: sid, chips, text });
    return { signal_id: `sig_${String(++this.seq.signal).padStart(4, "0")}` };
  }

  /** The feedback's sections still hold exactly what it left (and its inserted sections are still there). */
  private feedbackIntact(p: PageRec, f: FeedbackRec): boolean {
    return f.patched.every((x) => p.sections.find((s) => s.fx.id === x.before.fx.id)?.body === x.left)
      && f.inserted.every((id) => p.sections.some((s) => s.fx.id === id));
  }

  /** PageOut.feedback: the latest un-undone feedback of this page whose text is still on it; null otherwise. */
  private feedbackMark(p: PageRec): FeedbackMarkOut | null {
    const f = [...this.feedbacks].reverse().find((x) => x.page_id === p.id && !x.undone);
    if (!f || !f.patched.some((x) => p.sections.find((s) => s.fx.id === x.before.fx.id)?.body.includes(x.add))) return null;
    return { feedback_id: f.feedback_id, at: f.at, summary: f.summary, reading: f.reading,
      sections: p.sections.map((s) => s.fx.id).filter((id) => f.patched.some((x) => x.before.fx.id === id) || f.inserted.includes(id)),
      assets: [...f.assets], can_undo: this.feedbackIntact(p, f) };
  }

  private patch(patchId: string): RsiChangeOut {
    const c = this.rsi.find((x) => x.patch_id === patchId);
    if (!c) throw http(404, `no patch ${patchId}`);
    return c;
  }

  private decide(c: RsiChangeOut, status: PatchStatus): RsiChangeOut {
    c.status = status;
    c.can_undo = status === "personal_accepted";
    c.at = this.now().toISOString();
    return c;
  }

  /** teachspec.migrate: the default rules, then the old guidelines (user → user, feedback → rsi); no count cap. */
  private virtualSpec(): SpecRec {
    const migrated = this.guidelineList.map((g) => specItem({ item_id: g.guideline_id, text: g.text,
      kind: g.source === "user" ? "user" : "rsi", origin_ref: g.source === "user" ? null : `guideline:${g.guideline_id}` }));
    const defaults = DEFAULT_SPEC_ITEMS.map((d) => specItem({ ...d, kind: "default" }));
    return { version: 1, items: [...defaults, ...migrated], exemplars: [] };
  }

  private migrationEvidence(): Record<string, string> {
    const out: Record<string, string> = {};
    for (const g of this.guidelineList) if (g.source === "feedback" && g.evidence) out[g.guideline_id] = g.evidence;
    return out;
  }

  private effectiveSpec(): SpecRec { return this.spec ?? this.virtualSpec(); }

  private specOut(): TeachSpecOut {
    const s = this.effectiveSpec();
    const ids = new Set(s.items.map((i) => i.item_id));
    const evidence = Object.fromEntries(Object.entries(this.spec ? this.specEvidence : this.migrationEvidence())
      .filter(([k]) => ids.has(k)));
    return { version: s.version, stored: this.spec !== null, items: s.items, exemplars: s.exemplars, params: { ...DEFAULT_PARAMS },
      evidence, decisions: this.specDecisions.filter((d) => !ids.has(d.rule_id)), max_items: MAX_SPEC_ITEMS, max_exemplars: MAX_EXEMPLARS };
  }

  /**
   * Test / fixture hook: Memory's backfill writes a pending rule (origin memory_backfill, the system's write), with the
   * evidence line its 「?」 shows. Returns its id.
   */
  __pendingRule(text: string, why: string | null = null): string {
    const id = `si${++this.seq.specItem}`;
    this.writeSpec(undefined, (s) => ({ ...s, items: [...s.items, specItem({ item_id: id, text, kind: "pending" })] }));
    if (why !== null) this.specEvidence[id] = why;
    return id;
  }

  /**
   * teach.commit_spec: `base` (when given) must be the current version (else 409, nothing written); `change` may throw
   * (404 / 422) before anything is written; the result is saved as version + 1 (the virtual v1 is migrated first).
   */
  private writeSpec(base: number | undefined, change: (s: SpecRec) => SpecRec): TeachSpecOut {
    const cur = this.effectiveSpec();
    if (base !== undefined && base !== cur.version) throw http(409, `教学规范已经是第 ${cur.version} 版，请刷新后再改`);
    const next = change(cur);
    if (this.spec === null) this.specEvidence = this.migrationEvidence();
    this.spec = { version: cur.version + 1, items: [...next.items], exemplars: [...next.exemplars] };
    return this.specOut();
  }

  /** teach.page_markdown: `# title`, then per section `## heading`, the cue as a quote, the body (a check: stem + options). */
  private pageMarkdown(p: PageRec): string {
    const parts = [`# ${p.title.trim()}`];
    for (const s of p.sections) {
      const block = [`## ${s.fx.heading.trim()}`];
      if (s.cue.trim()) block.push(`> ${s.cue.trim()}`);
      if (s.body.trim()) block.push(s.body.trim());
      else if (s.check) block.push([s.check.stem.trim(), ...s.check.options.map((o) => `- ${o}`)].join("\n"));
      parts.push(block.join("\n\n"));
    }
    return `${parts.join("\n\n")}\n`;
  }

  private asset(assetId: string): { meta: AssetMeta; html: string } {
    const a = this.assets.get(assetId);
    if (!a) throw http(404, `没有这个交互：${assetId}`);
    return a;
  }

  private card(p: PageRec): PageCardOut {
    return { page_id: p.id, title: p.title, goal: p.goal, stale: this.isStale(p), done: p.done, organized: p.organized };
  }

  private conceptRole(conceptId: string): ConceptRole | null {
    if (conceptId === "c1") return "learn";
    if (conceptId === "c5" && [...this.pages.values()].some((p) => p.target === "c5")) return "learn";
    return null;
  }

  /** views.names_out: the page ids' titles; unknown ids get the server's 未命名 name. */
  private namesOut(pages: string[]): Record<string, string> {
    const out: Record<string, string> = {};
    for (const q of pages) if (!(q in out)) out[q] = this.pages.get(q)?.title || UNNAMED_PAGE;
    return out;
  }

  /** PageOut.can_shelve: organized, not shelved, and every cell of its target concept known. */
  private canShelve(p: PageRec): boolean {
    const c = CONCEPT_BY_ID.get(p.target);
    return p.organized && !p.collapsed && c !== undefined && this.allKnown(c.aspects.map((a) => cellOf(c.id, a)));
  }

  /** 恢复成完整页: unfold every unlocked section that is not all known. */
  private expandPage(p: PageRec): string[] {
    p.collapsed = false;
    const unfolded: string[] = [];
    for (const s of p.sections) {
      if (!s.folded || s.fold_locked || this.allKnown(s.fx.cells)) continue;
      s.folded = false;
      s.changed = "unfolded";
      unfolded.push(s.fx.id);
    }
    return unfolded;
  }

  // ---------------- path / review helpers ----------------

  /** page.mark_done: done(pageId) and a path page's 学完了. */
  private markDone(p: PageRec): DoneOut {
    p.done = true;
    return {};
  }

  private pathRec(pathId: string): PathRec {
    const path = this.paths.get(pathId);
    if (!path) throw http(404, "找不到这条学习路径");
    return path;
  }

  private addPath(goal: string): PathRec {
    const path: PathRec = { id: `path${++this.seq.path}`, goal,
      pages: PATH_PAGES.map((x) => ({ aim: x.aim, targets: [...x.targets], uses: [...x.uses], pageId: null })) };
    this.paths.set(path.id, path);
    return path;
  }

  /** A new goal's answer for a path (__goal): no draft, no page; every concept the pages use, in page order. */
  private pathDraft(path: PathRec): GoalDraftOut {
    const ids = [...new Set(path.pages.flatMap((pg) => [...pg.uses, ...pg.targets]))];
    return {
      ...NO_ROUND, path_id: path.id, title: path.goal,
      concepts: ids.map((c) => this.draftConcept(c, path.pages.some((pg) => pg.targets.includes(c)), false)),
      questions: [], notes: { from_background: 0, reused_concepts: 0, dropped_questions: 0 },
    };
  }

  /** No page yet → not_started; page done → done; otherwise learning. */
  private pathStatus(pageId: string | null): PathPageStatus {
    if (pageId === null) return "not_started";
    return this.pages.get(pageId)?.done ? "done" : "learning";
  }

  private pathOut(path: PathRec): PathOut {
    const pages = path.pages.map((pg, i) => ({
      index: i + 1, aim: pg.aim,
      concepts: [...pg.uses.map((c) => ({ concept_id: c, name: this.conceptName(c), target: false })),
        ...pg.targets.map((c) => ({ concept_id: c, name: this.conceptName(c), target: true }))],
      status: this.pathStatus(pg.pageId), page_id: pg.pageId, trashed: false, skipped: false, key: "", title: "", abstract: "", sections: [] as string[], exam: 0,
      state: this.pageState(pg.pageId), progress: null,
    }));
    const pf = path.prefetch ?? null;
    return { path_id: path.id, goal: path.goal, source: "llm", pages,
      next_index: pages.find((pg) => pg.status === "not_started")?.index ?? null,
      prefetch: pf === null ? null : { index: pf.index, page_id: pf.pageId, status: pf.status }, sources: [] };
  }

  /** The server's page state (wikiapp.progress.page_state), as far as the fake knows it: no runs, no K mastery. */
  private pageState(pageId: string | null, read: string | null = null): PageState {
    const p = pageId === null ? undefined : this.pages.get(pageId);
    if (p === undefined) return "planned";
    return p.done || p.id === read ? "read" : p.organized ? "written" : "planned";
  }

  /** A path's next step (wikiapp.progress): its first row not read, `read` counted as read (a page's end shows the step after it). */
  private pathStep(path: PathRec, read: string | null = null): StepOut {
    const base = { goal_id: path.id, goal: path.goal, map_id: null, region_id: null, region_name: "", path_id: path.id,
      draft_id: null, progress: null, failure: null, review_first: null };
    const i = path.pages.findIndex((pg) => this.pageState(pg.pageId, read) !== "read");
    const pg = path.pages[i];
    if (pg === undefined) return { ...base, kind: "goal_done", index: null, page_id: null, title: "" };
    const kind = this.pageState(pg.pageId, read) === "written" ? "read" : "start_page";
    return { ...base, kind, index: i + 1, page_id: pg.pageId, title: pg.aim };
  }

  /** How many times GET /wiki/progress was read (it is read with every catalog read, so it is not in `calls`). */
  progressReads = 0;

  /** GET /wiki/progress: every path a goal (the fake has no maps); 今天 = the first goal not done. */
  reviewFirst(): Promise<void> { return Promise.resolve(); }
  progress(): Promise<ProgressOut> {
    this.progressReads++;
    return Promise.resolve(((): ProgressOut => {
      const goals = [...this.paths.values()].map((path): GoalProgressOut => {
        const step = this.pathStep(path);
        const done = path.pages.filter((pg) => this.pageState(pg.pageId) === "read").length;
        return { goal_id: path.id, kind: "path", goal: path.goal, state: step.kind === "goal_done" ? "done" : done > 0 ? "learning" : "not_started",
          blocks: [], path_ids: [path.id], pages_done: done, pages_total: path.pages.length, step };
      });
      const none: StepOut = { kind: "none", goal_id: null, goal: "", map_id: null, region_id: null, region_name: "", path_id: null, index: null,
        page_id: null, title: "", draft_id: null, progress: null, failure: null, review_first: null };
      return { today: goals.find((g) => g.state !== "done")?.step ?? none, goals };
    })());
  }

  private pathCard(path: PathRec): PathCardOut {
    const out = this.pathOut(path);
    return { path_id: path.id, goal: path.goal, pages: out.pages.length,
      done: out.pages.filter((pg) => pg.status === "done").length, next_index: out.next_index };
  }

  /**
   * Everything due for review now, in page / section order, one item per card: a section with a recall cue (not check
   * sections) with its known / likely cells, and the first cell of every unanswered check. A cell goes to one card
   * (first wins); a cell reviewed today (UTC) is left out.
   */
  private reviewCandidates(includeGraded = false): ReviewItemOut[] {
    const today = utcDay(this.now());
    const skip = new Set(includeGraded ? [] : this.reviewLog.filter((r) => utcDay(r.at) === today).flatMap((r) => r.cells));
    const out: ReviewItemOut[] = [];
    const add = (p: PageRec, s: SectionRec, cells: string[], mode: ReviewMode): void => {
      const mine = cells.filter((c) => !skip.has(c));
      if (mine.length === 0) return;
      for (const c of mine) skip.add(c);
      if (this.removedCards.has(cardIdOf(p.id, s.fx.id, mode))) return;
      out.push(this.reviewItem(p, s, mine, mode));
    };
    for (const p of this.pages.values()) {
      if (!p.organized) continue;
      for (const s of p.sections) {
        if (s.role === "check") {
          const first = s.fx.cells[0];
          if (s.check && s.check.answered === null && first !== undefined) add(p, s, [first], "check");
          continue;
        }
        if (s.cue === "") continue;
        add(p, s, s.fx.cells.filter((c) => this.band(c) === "known" || this.band(c) === "likely"), "cue");
      }
    }
    return out;
  }

  private reviewItem(p: PageRec, s: SectionRec, cells: string[], mode: ReviewMode): ReviewItemOut {
    const cell = cells[0] ?? "";
    const { conceptId, aspect } = splitCell(cell);
    const cardId = cardIdOf(p.id, s.fx.id, mode);
    const mark = this.rewrittenCards.has(cardId) ? REVIEW_REWRITTEN_PREFIX : "";
    return {
      card_id: cardId, cell, cells, concept_id: conceptId, concept_name: this.conceptName(conceptId), aspect: aspect as Aspect, band: this.band(cell),
      mode, page_id: p.id, page_title: p.title, section_id: s.fx.id, heading: s.fx.heading,
      cue: mode === "cue" ? `${mark}${s.cue}` : "",
      answer: mode === "cue" ? s.body.split("\n")[0] ?? "" : "",
      check: mode === "check" && s.check ? { stem: `${mark}${s.check.stem}`, options: [...s.check.options] } : null,
      recall_now: mode === "cue" ? 0.55 : 0.5, days_to_threshold: -0.5, in_goal: true, exam: false, artifact_id: fakeArtifactId(`card:${cardId}`),
    };
  }

  private todayOut(more = false): ReviewTodayOut {
    const today = utcDay(this.now());
    const items = this.reviewCandidates();
    const doneToday = this.reviewLog.filter((r) => utcDay(r.at) === today).length;
    const limit = this.settings.review_per_day;
    return { items: items.slice(0, Math.max(0, limit - (more ? 0 : doneToday))), due: items.length, done_today: doneToday, limit,
      confirms: this.confirmCards(), streak_days: this.streakDays(), page_round_today: this.pageRoundAnswers };
  }

  /** ReviewTodayOut.streak_days: consecutive local days (Bot time zone) with a review grade or check answer, ending today (or yesterday while today has none). */
  private streakDays(): number {
    const tz = this.bot.settings.timezone;
    const day = (d: Date): string => new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" }).format(d);
    const days = new Set(this.reviewLog.map((r) => day(r.at)));
    let t = this.now().getTime();
    if (!days.has(day(new Date(t)))) t -= DAY_MS;
    let n = 0;
    while (days.has(day(new Date(t)))) { n++; t -= DAY_MS; }
    return n;
  }

  /** Due confirmation questions (due <= now), oldest first. */
  private confirmCards(): ConfirmCardOut[] {
    const now = this.now().getTime();
    return this.followups.filter((f) => f.outcome === null && f.due.getTime() <= now)
      .sort((x, y) => x.due.getTime() - y.due.getTime() || x.id.localeCompare(y.id))
      .map((f) => {
        const { conceptId, aspect } = splitCell(f.cell);
        return { followup_id: f.id, kind: f.kind, cell: f.cell, concept_id: conceptId, concept_name: this.conceptName(conceptId),
          aspect: aspect as Aspect, page_id: f.pageId, page_title: this.page(f.pageId).title,
          check: { stem: f.stem, options: [...f.options] }, due: f.due.toISOString(), artifact_id: fakeArtifactId(`confirm:${f.id}`) };
      });
  }

}
