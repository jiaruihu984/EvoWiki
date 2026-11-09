/** Test-only helpers shared by the WikiController tests (no production code imports this). */
import type { StartPage } from "./start-page-view";
import type { ConnState } from "../connection";
import type { WikiControllerDeps } from "./controller/deps";
import type { EwNoticeAction } from "../ui/ew-notice";
import { type NoteLocator, WikiController } from "./controller";
import { FakeWikiApi } from "./fake";
import { DEFAULT_BOT_SETTINGS } from "./fixtures";
import type { HandEditPlan, NoteSync, SyncResult, WikiNoteInput } from "./note-port";
import { IDK } from "./schema";
import type { SourcePort } from "./sources/ports";
import type { PageView } from "./state";
import { PageNotWritten } from "./controller/learn";

export class FakeConn {
  state: ConnState = "online";
  private readonly fns = new Set<(s: ConnState) => void>();
  track<T>(p: Promise<T>): Promise<T> { return p; }
  onChange(fn: (s: ConnState) => void): () => void { this.fns.add(fn); return () => this.fns.delete(fn); }
  emit(s: ConnState): void { this.state = s; for (const f of this.fns) f(s); }
}

export class RecSync implements NoteSync {
  /** Every full sync (the landed page). */
  readonly runs: WikiNoteInput[] = [];
  /** Every streamed (partial) write while a stream ran; they answer `partialResult` at once (no hold, no throwNext). */
  readonly streamed: WikiNoteInput[] = [];
  partialResult: SyncResult = "updated";
  readonly finds: string[] = [];
  result: SyncResult = "created";
  throwNext = false;
  /** When set, run() waits for release(). */
  hold = false;
  private waiting: (() => void)[] = [];
  release(): void { const w = this.waiting; this.waiting = []; for (const f of w) f(); }
  get pending(): number { return this.waiting.length; }
  /** The `accepted` text of each full run, in order. */
  readonly accepted: (string | undefined)[] = [];
  async run(input: WikiNoteInput, opts?: { partial?: boolean; accepted?: string }): Promise<SyncResult> {
    if (opts?.partial) { this.streamed.push(input); return Promise.resolve(this.partialResult); }
    this.runs.push(input);
    this.accepted.push(opts?.accepted);
    if (this.hold) await new Promise<void>((r) => this.waiting.push(r));
    if (this.throwNext) { this.throwNext = false; throw new Error("bad folder"); }
    return this.result;
  }
  /** Page ids that have a file (listPages, the reconcile pass). */
  pages: string[] = [];
  listPages(): Promise<string[]> { return Promise.resolve([...this.pages]); }
  find(pageId: string): Promise<string | null> { this.finds.push(pageId); return Promise.resolve(`Wiki/${pageId}.md`); }
  /** Every handEdit input; what it answers (null = no note claims the page). */
  readonly handReads: WikiNoteInput[] = [];
  hand: { path: string; text: string; plan: HandEditPlan } | null = null;
  handEdit(input: WikiNoteInput): Promise<{ path: string; text: string; plan: HandEditPlan } | null> { this.handReads.push(input); return Promise.resolve(this.hand); }
  /** Every edited call [path, pageId]; true while `hand` holds an edit. */
  readonly editedAsked: [string, string][] = [];
  edited(path: string, pageId: string): Promise<boolean> {
    this.editedAsked.push([path, pageId]);
    return Promise.resolve(this.hand !== null && this.hand.plan.kind !== "none");
  }
  /** The note the user is in. */
  openPath: string | null = null;
  isOpen(path: string): boolean { return path === this.openPath; }
}

/**
 * `locators`: the `at` passed with each opened path (undefined = just open it). `notices`: every Notice text the
 * controller asked to show.
 */
export interface Setup {
  api: FakeWikiApi; conn: FakeConn; sync: RecSync; opened: string[]; locators: (NoteLocator | undefined)[];
  notices: string[];
  /** Every 读完了 内容太简单 / 理解有困难 sent to the assistant (deps.assistantSendFresh). */
  sentFresh: { pageId: string; title: string; text: string }[];
  /** Every topic the controller opened the 学点新的 tab with (deps.learnNew; `o.learnNew` replaces the recorder). */
  learned: string[];
  /** Every Notice-with-a-button the controller asked for, and how many times it was asked to show the sidebar. */
  sidebarShown: { count: number };
  /** The pages whose 开始生成 tab was shown (deps.showStartPage). */
  startPages: StartPage[];
  /** Every 「⋯ → 问助手」 hand-off. */
  asked: { pageId: string; title: string; quote: { text: string; heading: string | null; kind?: "text" | "wrong_answer" } }[];
  /** Every Bot notice with buttons, and every text the Bot sent to the 助手 tab. */
  actionNotices: { text: string; actions: EwNoticeAction[] }[];
  sent: string[];
  /** The page target each of those was sent with (null = the current conversation). */
  sentTo: ({ pageId: string; title: string } | null)[];
  /** Every 查看 key the controller asked the host to reveal (deps.revealChange). */
  revealed: string[];
  c: WikiController;
}
export function setup(o: { api?: FakeWikiApi; demo?: boolean; configured?: boolean; delayMs?: number;
  assets?: { retrieveAll(ids: readonly string[]): Promise<void> }; learnNew?: (topic: string, start?: boolean) => void; sourcePort?: SourcePort; blockEventStore?: WikiControllerDeps["blockEventStore"]; blockProgressStore?: WikiControllerDeps["blockProgressStore"];
  decisions?: WikiControllerDeps["decisions"]; resolveMemoryConflicts?: () => void } = {}): Setup {
  const api = o.api ?? new FakeWikiApi({ delayMs: o.delayMs ?? 0, bot: { settings: { ...structuredClone(DEFAULT_BOT_SETTINGS), timezone: Intl.DateTimeFormat().resolvedOptions().timeZone } } });
  const conn = new FakeConn();
  const sync = new RecSync();
  const opened: string[] = [];
  const locators: (NoteLocator | undefined)[] = [];
  const sentFresh: Setup["sentFresh"] = [];
  const notices: string[] = [];
  const learned: string[] = [];
  const sidebarShown = { count: 0 };
  const startPages: StartPage[] = [];
  const asked: Setup["asked"] = [];
  const actionNotices: Setup["actionNotices"] = [];
  const sent: string[] = [];
  const sentTo: Setup["sentTo"] = [];
  const revealed: string[] = [];
  const c = new WikiController({
    api, conn, blockEventStore: o.blockEventStore ?? null, blockProgressStore: o.blockProgressStore ?? null, sync: o.demo ? null : sync, openNote: (p, at) => { opened.push(p); locators.push(at); return true; },
    configured: () => o.configured ?? true, demo: o.demo ?? false,
    notice: (t) => { notices.push(t); },
    showSidebar: () => { sidebarShown.count++; },
    showStartPage: (p) => { startPages.push(p); },
    noticeActions: (text, actions) => { actionNotices.push({ text, actions }); },
    revealChange: (key) => { revealed.push(key); },
    assistantSend: (text, page) => { sent.push(text); sentTo.push(page ?? null); },
    askAssistant: (pageId, title, quote) => { asked.push({ pageId, title, quote }); },
    assistantSendFresh: (pageId, title, text) => { sentFresh.push({ pageId, title, text }); },
    ...(o.assets ? { assets: o.assets } : {}),
    ...(o.sourcePort ? { sourcePort: o.sourcePort } : {}),
    ...(o.decisions ? { decisions: o.decisions } : {}),
    ...(o.resolveMemoryConflicts ? { resolveMemoryConflicts: o.resolveMemoryConflicts } : {}),
    learnNew: o.learnNew ?? ((t) => { learned.push(t); }),
  });
  apis.set(c, api);
  return { api, conn, sync, opened, locators, notices, sentFresh, learned, sidebarShown, startPages, asked, actionNotices, sent, sentTo, revealed, c };
}
/** The fake each setup() controller runs on (firstPage makes its goal there). */
const apis = new WeakMap<WikiController, FakeWikiApi>();
export const methods = (api: FakeWikiApi): string[] => api.calls.map((x) => x.method);
export const view = (c: WikiController): PageView => {
  const p = c.state.page;
  if (p.kind !== "open") throw new Error(`page is ${p.kind}`);
  return p;
};
export async function until(f: () => boolean): Promise<void> {
  for (let i = 0; i < 1000; i++) {
    if (f()) return;
    await new Promise((r) => setTimeout(r, 2));
  }
  throw new Error("timeout");
}
export const flush = (): Promise<void> => new Promise((r) => setTimeout(r, 0));

/**
 * p1 written and open: the first goal (newGoal). A write that fails is left as the controller shows it (the test looks
 * at that).
 */
export async function firstPage(c: WikiController): Promise<void> {
  await newGoal(c, "学会 z-score 标准化");
}
/**
 * A goal made as the chat's goal_new makes it (the fake's __goal) and followed as the chat's goal run does: a page is
 * written (learnWritePage; a draft first gets its three answers by the fake directly), a learning path is opened and the
 * catalog read again.
 */
export async function newGoal(c: WikiController, goal: string): Promise<void> {
  const api = apis.get(c);
  if (api === undefined) throw new Error("newGoal takes a controller made by setup()");
  const d = api.__goal(goal);
  if (d.path_id !== null) { await c.openPath(d.path_id); await c.reloadCatalog(); return; }
  const pageId = d.draft_id !== null ? (await api.answerDraft(d.draft_id, { answers: [IDK, 0, 1], skip: false })).page_id : d.page_id;
  try { await c.learnWritePage(pageId ?? "", d.title, goal.trim()); } catch (e) { if (!(e instanceof PageNotWritten)) throw e; }
}
