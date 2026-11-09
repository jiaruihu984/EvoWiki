import type { FollowupView } from "./followup";
/** State of the personal-wiki sidebar. Pure data; WikiController owns every transition. */
import type { ConnState } from "../connection";
import { WIKI_COPY } from "./copy";
import type { ConflictView } from "./memory-conflicts";
import type { BotSlot } from "./bot/bot-state";
import { INITIAL_BOT } from "./bot/bot-state";
import type { SyncResult } from "./note-port";
import type {
  ExamSummaryOut,
  ProgressOut, CatalogOut, CellChangeOut, Changed, ConfirmAnswerOut, EngineOut, Grade, KGridOut, PageOut, PathOut,
  PromptStepOut, ReviewTodayOut, RsiChangeOut, SourceOut, SourceProgress, TeachSpecOut,
} from "./schema";
import type { Recommendation } from "./starmap/frontier";

/** The three sidebar tabs, in order (今天 / 助手 / 个性化; 个性化's id stays "engine"). */
export const TABS = ["today", "assistant", "engine"] as const;
export type Tab = (typeof TABS)[number];
/**
 * Tab values from before the two-tab sidebar (本页 / 目录 / 复习 and the older path / k / goal / guidelines). Kept only so
 * a stored or stale value still lands somewhere: every one of them lands on 今天.
 */
export const LEGACY_TABS = ["page", "catalog", "review", "path", "k", "goal", "guidelines"] as const;
export type LegacyTab = (typeof LEGACY_TABS)[number];
/** 读完觉得: 太浅了 / 正好 / 有点难. */
export const FEELS = ["shallow", "right", "hard"] as const;
export type Feel = (typeof FEELS)[number];

/** Where a tab value lands: 助手 and 个性化 as themselves; 今天, every legacy value and anything unknown on 今天. Never throws. */
export function landTab(t: string): Tab {
  return t === "assistant" || t === "engine" ? t : "today";
}
export type Load<T> = { kind: "idle" } | { kind: "loading" } | { kind: "ready"; value: T } | { kind: "error"; message: string };
/**
 * A result line shown next to what produced it. `sub`: a second line under it, set when the write succeeded but a
 * follow-up refresh GET failed (the main line still describes the write).
 */
export interface ResultNote { text: string; tone: "ok" | "error"; retry?: RetryAction; sub?: string }
export type RetryAction = { kind: "organize" };
/** Live text of one section while a stream runs (the last section_delta wins); heading null = unchanged. */
export interface StreamingSection { cue: string; body: string; heading: string | null }
/** `missing`: an organized page whose note was not found in the vault (offers 「写入库里」). */
export type NoteSyncState =
  | { kind: "idle" } | { kind: "syncing" } | { kind: "done"; result: SyncResult } | { kind: "error" } | { kind: "missing" };
export interface PageView {
  page: PageOut;
  /** Live text from section_delta while a stream runs. */
  streaming: Record<string, StreamingSection>;
  status: ResultNote | null;           // page-level status line
  sectionNotes: Record<string, ResultNote>;
  noteSync: NoteSyncState;
  /**
   * 「改了」 markers: what this visit did to each section (web PageView `touched`). Display only; the backend's
   * section.changed is never shown. Cleared on a page switch and when generating / reorganizing starts.
   */
  touched: Record<string, Changed>;
  /**
   * The last `writer` event of the stream running / just run on this page (null: none since the page was last read).
   * While set it says who writes the page (the fallback line); once a GET lands, PageOut.writer says it instead.
   */
  streamWriter: { fallback: boolean; reason: string } | null;
}
export type Busy = null | { what: string; sid?: string; abortable: boolean };
/** The learning path tab: pathId null = the path list (drawn from the catalog's paths); note = 下一页 / 学完 results. */
export interface PathSlot { pathId: string | null; view: Load<PathOut>; note: ResultNote | null }
/**
 * One answered review item: the band change, the self-grade (null for a check answer), the check result (null for a
 * self-grade) and its plain result line.
 */
export interface ReviewResult {
  band: CellChangeOut; grade: Grade | null; correct: boolean | null; correctIndex: number | null; choice: number | null; line: ResultNote;
}
/** One answered confirmation question (by followup_id): what was picked, the server's verdict and its plain result line. */
/** `line`: only the reason a card closed (gone / already answered); an answered card has no verdict sentence. */
export interface ConfirmResult { choice: number; out: ConfirmAnswerOut; line: ResultNote | null; /** The server said gone / already answered: no verdict, only 下一张. */ closed?: boolean }
/**
 * 今天复习: the list, which cues were uncovered (看答案), the answered items by cell, the cards the user moved past
 * (下一张; the shown card is the first item not in `seen`), and the area's note.
 */
export interface ReviewSlot {
  today: Load<ReviewTodayOut>; revealed: string[]; results: Record<string, ReviewResult>; seen: string[]; note: ResultNote | null;
  /** Answered confirmation questions by followup_id (their card key in `seen` is `confirm:<followup_id>`). */
  confirmResults: Record<string, ConfirmResult>;
  /** True while 今天 shows the in-place review (a card) instead of the 下一步 button. 先不复习了 turns it off; nothing else changes. */
  active: boolean;
  /** The last card was just done: 下一步 says 复习完了。 until a refresh brings new cards. */
  finished: boolean;
  /** Cells whose card was removed by 太简单了 / 不想要了 (only after the backend said yes). */
  removed: string[];
  /** app.js TODAY.barAt: where the progress bar was last drawn (done cards); null = not drawn this round. Written by the renderer. */
  barAt: number | null;
  /** The answer just recorded (card key + ms): its right-answer pop and 答对了 line animate for a short while (app.js TODAY.fresh). */
  fresh: { key: string; at: number } | null;
  /** A round ended (set when it finishes, cleared by 开始): the review card's why then reads as a re-check. */
  roundDone: boolean;
  /** Keys already answered when 开始 was pressed: this round's n / ok do not count them (app.js resets `res` on start). */
  roundBase: string[];
  /** 「考考我这页」's round: `today` holds this page's cards until it ends, then today's list again. */
  pageRound: string | null;
  /** `page_round_today` when that round began, and how many answers the server has for the round once it ended (null: no such line). */
  pageRoundBase: number; pageRoundDone: number | null;
}
/** The pill chosen in the 「读完了」 block of a page: none or one (kept per page; nothing is written to the note). */
export interface EndDraft { chips: Feel[] }
/** The block's own line: saved (a plain line, e.g. the last page of a path) or an error line (继续学习 failed). */
export type EndPhase = { kind: "idle" } | { kind: "saved"; text: string } | { kind: "error"; text: string };
/** 撤销 of the page's feedback patch: the last try failed (`retry` false: the page changed since, trying again cannot work). */
export type UndoState = { kind: "idle" } | { kind: "undoing" } | { kind: "failed"; text: string; retry: boolean };
/**
 * Everything the end block and the page's top line keep per page, until Obsidian restarts. `handFailed`: the user's
 * edit in the note could not go up (`text` says why); `note` is the note text it was read from. `handLine`: his edit
 * just went up; the top line says so until he leaves the page (then 撤销 is in 活动记录).
 */
export interface PageEnd { draft: EndDraft; phase: EndPhase; undo: UndoState; handFailed: { text: string; note: string; conflict?: boolean } | null; handLine: boolean }
export const EMPTY_END: PageEnd = {
  draft: { chips: [] }, phase: { kind: "idle" }, undo: { kind: "idle" }, handFailed: null, handLine: false,
};
export type PageSlot =
  | { kind: "none" } | { kind: "loading"; pageId: string } | { kind: "error"; pageId: string; message: string }
  | ({ kind: "open" } & PageView);
/**
 * 个性化 · 资料's local state: which card asks to confirm a delete, which one is being renamed (and
 * the text typed so far), and one line under a card (a failed stream with 重试).
 */
export interface SourceUi {
  confirm: string | null; rename: string | null; renameDraft: string;
  notes: Record<string, { text: string; retry: boolean }>;
}
export const INITIAL_SOURCE_UI: SourceUi = { confirm: null, rename: null, renameDraft: "", notes: {} };
/**
 * 个性化's local state (app.js PREFS.manage / edit / log / older): 管理偏好 shown in place of the tab body, the rule being
 * edited there, 活动记录 expanded, its 更早 rows shown.
 */
export interface PersonalUi {
  manage: boolean; edit: string | null; log: boolean; older: boolean;
  /** 我的学习偏好 / 资料 expanded (app.js PREFS.rulesOpen / srcOpen; folded by default, like 活动记录). */
  rules: boolean; sources: boolean;
  /** Text typed in a textarea (`p:<id>` a pending rule, `e:<id>` the rule being edited), kept across redraws. */
  drafts: Record<string, string>;
}
export const INITIAL_PERSONAL: PersonalUi = { manage: false, edit: null, log: false, older: false, rules: false, sources: false, drafts: {} };
export interface WikiState {
  conn: ConnState; demo: boolean; configured: boolean;
  tab: Tab; busy: Busy;
  catalog: Load<CatalogOut>; catalogNote: ResultNote | null;
  /** GET /wiki/progress, read with the catalog (after every write that changes progress): where learning goes on. */
  progress: Load<ProgressOut>;
  /** 想学点新的: the topic typed in the card's box (kept across re-renders). */
  learnTopic: string;
  page: PageSlot;
  path: PathSlot;
  review: ReviewSlot;
  k: Load<KGridOut>; kSelected: string | null; kNote: ResultNote | null;
  /**
   * 最近为你改了什么: every change the backend lists (the renderer filters what is shown), and the result line of the last
   * 接受 / 不用 / 撤销 with the change it is about (drawn under that card, or at the top of the section when the card is
   * gone). The change being decided is `busy.sid` (busy.what "rsi").
   */
  rsi: Load<RsiChangeOut[]>; rsiNote: RsiNote | null;
  /** 设置 · 自动改进: whether rounds run (GET /wiki/rsi/switch). */
  rsiSwitch: Load<boolean>;
  /**
   * 参数: GET /wiki/engine (学习状态评估) and the teaching rules of GET /wiki/teach/spec (讲解方式, read
   * only). Read when the tab is shown (loadEngine); a failed re-read keeps what is shown.
   */
  engine: Load<EngineOut>;
  /** 个性化: the whole teaching spec read with the rules (我的学习偏好, 待确认的偏好, the decisions of 管理偏好). */
  prefs: Load<TeachSpecOut>;
  /** 个性化's 待确认的记忆 (GET MEMORY_CONFLICTS_PATH, read with the tab): each open group as the card draws it. */
  conflicts: Load<ConflictView[]>;
  personal: PersonalUi;
  /** Prompt 设置: GET /wiki/prompts (every step's wording, the user's edit or the default). Read with the tab; a failed re-read keeps what is shown. */
  prompts: Load<PromptStepOut[]>;
  /**
   * 个性化 · 资料: GET /wiki/sources (newest first), the live progress of the sources being read by id (written in place, the
   * sidebar draws it without a redraw; once a source ends the list is redrawn), and the section's local state.
   */
  sources: Load<SourceOut[]>; sourceProgress: Record<string, SourceProgress>; sourceUi: SourceUi;
  /** 真题: the summary of each source used as past exams (GET /wiki/sources/{id} `exam`), the card's 真题 line. */
  sourceExam: Record<string, ExamSummaryOut>;
  /**
   * Every learning path read so far (GET /wiki/paths/{id}) by id: only used to name the next page (its aim) and to find
   * the path the last opened page belongs to. The catalog's path cards stay the truth for what is next.
   */
  pathPlans: Record<string, PathOut>;
  /** The 「读完了」 block's draft and the page's top line, by page id. */
  pageEnd: Record<string, PageEnd>;
  /** 粗心选错了 / 我学会了 / 看不懂 after a wrong answer, by followupKey; a question without an entry shows the three buttons. */
  followups: Record<string, FollowupView>;
  /** 想学点新的's recommendations when fixed by the dev-only demo fixture (fixtures-demo.ts); null = the frontier of K. */
  recommendations: readonly Recommendation[] | null;
  /** The Bot: today cards, page chips, the action ledger, recent activity and settings (bot/bot-controller.ts). */
  bot: BotSlot;
  /** The page write the learner stopped (「停止」): its line reads 已停止 · 重新写 until that page is written again. */
  stopped: StoppedWrite | null;
  /** The goal the open page came from (a learning path, the chat's run): only its note shows; null = no goal context (the file tree, a page link): every goal's. */
  pageGoal: { pageId: string; mapId: string } | null;
}
export interface StoppedWrite { pageId: string; title: string; goal: string }
/** `patchId` null: about the list itself (a failed re-read while a list is shown). */
export interface RsiNote { patchId: string | null; note: ResultNote }

export const INITIAL_WIKI_STATE: WikiState = {
  conn: "unknown", demo: false, configured: true,
  tab: "today", busy: null,
  catalog: { kind: "idle" }, catalogNote: null,
  progress: { kind: "idle" },
  learnTopic: "",
  page: { kind: "none" },
  path: { pathId: null, view: { kind: "idle" }, note: null },
  review: { today: { kind: "idle" }, revealed: [], results: {}, confirmResults: {}, seen: [], note: null, active: false, finished: false, removed: [], barAt: null, fresh: null, roundDone: false, roundBase: [], pageRound: null, pageRoundBase: 0, pageRoundDone: null },
  k: { kind: "idle" }, kSelected: null, kNote: null,
  rsi: { kind: "idle" }, rsiNote: null, rsiSwitch: { kind: "idle" },
  engine: { kind: "idle" }, prompts: { kind: "idle" },
  prefs: { kind: "idle" }, conflicts: { kind: "idle" }, personal: INITIAL_PERSONAL,
  sources: { kind: "idle" }, sourceProgress: {}, sourceUi: INITIAL_SOURCE_UI, sourceExam: {},
  pathPlans: {},
  pageEnd: {},
  followups: {},
  recommendations: null,
  bot: INITIAL_BOT,
  stopped: null,
  pageGoal: null,
};

/** What a note sync has to say (A5, shown as a notice): user_edited / backup_failed / a thrown sync; null otherwise. */
export function noteSyncLine(s: NoteSyncState): ResultNote | null {
  if (s.kind === "error") return { text: WIKI_COPY.noteSyncFailed, tone: "error" };
  if (s.kind !== "done") return null;
  if (s.result === "user_edited") return { text: WIKI_COPY.noteUserEdited, tone: "error" };
  if (s.result === "backup_failed") return { text: WIKI_COPY.noteBackupFailed, tone: "error" };
  return null;
}
