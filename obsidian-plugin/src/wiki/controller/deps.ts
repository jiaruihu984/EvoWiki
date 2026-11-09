/** What WikiController is built with (wiring.ts builds them; tests pass fakes). */
import type { BlockProgressStore } from "../evoui/progress-store";
import type { BlockEventStore } from "../evoui/event-store";
import type { ConnState } from "../../connection";
import type { WikiApi } from "../api";
import type { EwNoticeAction } from "../../ui/ew-notice";
import type { SourcePort } from "../sources/ports";
import type { NoteSync } from "../note-port";
import type { Recommendation } from "../starmap/frontier";
import type { DraftDecisions } from "../revise/decisions";
import type { StartPage } from "../start-page-view";

/** Reads a note's text and says which 0-based line to scroll to (null = none found: just open the note). */
export type NoteLocator = (text: string) => number | null;

export interface PageRoundMemo { pageId: string; answered: string[] }

export interface WikiControllerDeps {
  api: WikiApi;
  /** Null only for demo/test wiring; real controllers always have account-scoped durable storage. */
  blockEventStore: BlockEventStore | null;
  blockProgressStore: BlockProgressStore | null;
  conn: { readonly state: ConnState; track<T>(p: Promise<T>): Promise<T>; onChange(fn: (s: ConnState) => void): () => void };
  /** null = sync disabled (demo). */
  sync: NoteSync | null;
  /**
   * Opens a vault path if it is an existing file (true = it was). `at`: where to scroll once its text is read (a 0-based
   * line; null = just open it); an already open copy of the note is reused. `frame`: also frame the section that starts
   * there (a 参数 change card's jump, note-frame.ts).
   */
  openNote: (path: string, at?: NoteLocator, frame?: boolean) => boolean;
  configured: () => boolean;               // server URL + user id set (real client); always true for the fake
  demo: boolean;
  /** A short Obsidian Notice (the 「⋯」 menu's 太浅了 / 有点难 results); absent = not shown. */
  notice?: (text: string) => void;
  /** A notice with buttons (the Bot's 查看 / 撤回); absent = the plain text only. */
  noticeActions?: (text: string, actions: EwNoticeAction[]) => void;
  /** 查看 on a change: scroll the 参数 row `[data-chg=key]` into view and draw the ants frame (main.ts reaches the DOM); absent = only the tab switch. */
  revealChange?: (key: string) => void;
  /**
   * Interactive assets (embed/assets.ts AssetFetcher): fetched into the vault after a page's note sync. Absent = never
   * (the demo).
   */
  assets?: { retrieveAll(ids: readonly string[], kind?: "html" | "evoui" | "image"): Promise<void> };
  /**
   * The in-page drafts' decisions, shared with the chat's cards (revise/decisions.ts): the page's bar and the card are one
   * decision. Absent = the bar keeps its own (tests, the demo wiki).
   */
  decisions?: DraftDecisions;
  /** Shows the wiki sidebar; absent = nothing to show. */
  showSidebar?: () => void;
  /** The unfinished 「考考我这页」 round (page, cards answered), kept over a reload; absent = not kept (tests). */
  pageRoundMemo?: { load(): PageRoundMemo | null; save(m: PageRoundMemo | null): void };
  /** A page not written yet was opened (no note): its 开始生成 tab (start-page-view.ts); absent = nothing shown. */
  showStartPage?: (page: StartPage) => void;
  /** 学点新的 (openLearnNew): opens the 学点新的 tab with the topic; absent = the 新目标 dialog (tests, older wiring). */
  learnNew?: (topic: string) => void;
  /**
   * A goal's next block (a block's last page read, 今天's 下一块): the chat's run of that map enters it, its diagnosis in the
   * 助手 when it needs one, restoring its run after reload when needed. Rejects when it cannot continue.
   */
  continueGoal?: (mapId: string, region?: string | null) => Promise<void>;
  /** 「⋯ → 问助手」 (`reteach`: 重讲这一节 waiting in the composer, unsent): opens the sidebar on 助手 with this quote above the composer; absent = nothing happens (tests). */
  askAssistant?: (pageId: string, title: string, quote: { text: string; heading: string | null; kind?: "text" | "wrong_answer" }, reteach?: string) => void;
  /**
   * 读完了's 内容太简单 / 理解有困难: the 助手 tab in a new conversation for the page, `text` sent as the learner's message
   * (nothing quoted: the turn already knows the page); absent = nothing happens (tests).
   */
  assistantSendFresh?: (pageId: string, title: string, text: string) => void;
  /** 待确认的记忆 · 处理: the 助手 tab with a new chat sent as intent resolve_memory_conflicts; absent = nothing happens (tests). */
  resolveMemoryConflicts?: () => void;
  /** The Bot's 问助手 / chips: switch to the 助手 tab, put the text in the draft and send it; absent = nothing happens (tests). */
  assistantSend?: (text: string, page?: { pageId: string; title: string }) => void;
  /** 想学点新的's recommendations instead of the frontier of K (the dev-only demo fixture, fixtures-demo.ts); absent = computed. */
  recommendations?: readonly Recommendation[];
  /** 参数 · 去设置里改: opens the 设置 view (wiring: the evowiki:open-settings command, else Obsidian's settings tab); absent = nothing. */
  openSettings?: () => void;
  /** Prompt 设置: opens the centre-pane editor of a step ("rules" = 讲解方式); absent = nothing (tests). */
  openPrompt?: (key: string) => void;
  /** 我 in 个性化: his profile note opens. */
  openProfile?: () => void;
  /** 参数 · 资料: the vault side (picker, bytes, originals, open); absent = no vault (tests, the demo). */
  sourcePort?: SourcePort;
}
