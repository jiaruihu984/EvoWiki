/** What the personal-wiki renderers may trigger, and what they are given. Task 10/11 wire these to WikiController. */
import type { ReviewFirstChoice } from "./review-first";
import type { ReviewFirstOut } from "./schema";
import { WIKI_COPY } from "./copy";
import type { PauseUntil, SettingsPatch } from "./bot/bot-schema";
import type { Names } from "./names";
import type { FollowupChoice, FollowupTarget, Declared, Grade } from "./schema";
import type { LegacyTab, PersonalUi, RetryAction, Tab, WikiState } from "./state";

/** Everything a renderer may trigger. main/view wire it to WikiController; tests pass vi.fn()s. */
export interface WikiActions {
  setTab(t: Tab | LegacyTab): void; retryConnection(): void;
  /** 想学点新的: the card's topic box (local only); open the 新目标 dialog (topic may be empty). */
  setLearnTopic(text: string): void;
  /** 想学点新的 · 开始 / a recommendation: the 学点新的 flow with the topic (for now the 新目标 dialog; T11 repoints it). */
  /** `start`: he typed it on 今天, it goes at once; a recommendation only fills the form. */
  openLearnNew(topic: string): void;
  openWikiPage(id: string): void; openPath(id: string | null): void; pathNext(): void; pathDone(index: number): void;
  revealCue(cell: string): void; gradeReview(cell: string, grade: Grade): void; answerReview(cell: string, choice: number): void;
  /** 下一张 / 完成: move past an answered review card. */
  nextReview(cell: string): void;
  /** 下一步 · 复习 N 张: show the review in place. 先不复习了: back to 下一步, every card kept. */
  startReview(): void; pauseReview(): void;
  /** The review bar's new position (done cards), written in place after its slide; no redraw. */
  markBar(done: number): void;
  /** Re-read what 今天 shows (the review list, the paths) when the view opens or comes back. */
  refreshToday(): void;
  /** 这张卡有问题…: open / close the box; pick a kind (null = clear it, or pick again to clear); the text (local only); 记下. */
  /** 反馈 on a review card: 助手 with the card quoted. */
  askAboutCard(cell: string): void;
  /** 粗心选错了 / 我学会了 / 看不懂 after a wrong answer (看不懂 only opens the assistant with the question quoted). */
  followup(target: FollowupTarget, choice: FollowupChoice): void;
  /**
   * A due confirmation question: answer it (-1 = 不知道); after a refuted 我学会了, 重新读 (the section that teaches it) or
   * 还没掌握 (助手 with the question quoted).
   */
  answerConfirm(followupId: string, choice: number): void; reteach(followupId: string): void; confirmUnclear(followupId: string): void;
  setReviewPerDay(n: number): void; reloadReview(): void;
  /** 再来一批 on the review-finished card: the next full batch (GET review/today?more=true) starts in place. */
  moreReview(): void;
  reloadCatalog(): void; organize(): void; stop(): void; openCurrentNote(): void;
  know(sid: string): void; toggleFold(sid: string): void;
  togglePin(sid: string): void; answerCheck(sid: string, c: number): void;
  done(): void; expand(): void; retry(r: RetryAction): void;
  /** 本页 小目录: open the page's note and scroll to the section. */
  jumpToSection(sid: string): void;
  /** 个性化 · 活动记录: 重试 the list; 撤销 one change; 采用 / 不要 a code change waiting for review. */
  setRsiSwitch(on: boolean): void; reloadRsi(): void; undoPatch(id: string): void; approvePatch(id: string): void; discardPatch(id: string): void;
  selectCell(c: string | null): void; declareCell(c: string, v: Declared | null): void;
  /** 个性化: re-read the prompts / θ_infer and the rules (its 重试 links); 去设置里改 → the 设置 view. */
  loadEngine(): void; openSettings(): void;
  /** 个性化's local view: 管理偏好, the rule being edited, 活动记录 expanded, 更早 shown. */
  personalView(patch: Partial<PersonalUi>): void;
  /** Text typed in one of 个性化's textareas (kept without a redraw). */
  prefDraft(key: string, text: string): void;
  /** 待确认的偏好: 设为我的偏好 (with the text as the user left it), 不采用. */
  prefAdopt(id: string, text: string): void; prefReject(id: string): void;
  /** 管理偏好: 保存 an edit, 删除, 恢复 a rule not adopted or taken back. */
  prefSave(id: string, text: string): void; prefRemove(id: string): void; prefRestore(text: string): void;
  /** 待确认的记忆: 处理 (a new chat in the 助手, intent resolve_memory_conflicts); 重试 a failed read of the groups. */
  resolveConflicts(): void; reloadConflicts(): void;
  /** Prompt 设置: a row opens its centre-pane editor ("rules" = 讲解方式, else the step key). */
  openPrompt(key: string): void;
  /** 我 in 个性化: his profile note. */
  openProfile(): void;
  /** Bot. `key` of a task card: "review" for the local review card, else a suggestion_id. */
  /** 今天's 下一步 card: where learning goes on (next-step.ts nextStep: an unwritten page, the next page, the next block). */
  continueLearning(): void;
  /** 先复习 / 跳过 on the step's 「建议先复习 X」. */
  answerReviewFirst(hint: ReviewFirstOut, choice: ReviewFirstChoice): void;
  botStart(key: string): void;
  botNotInterested(key: string): void;
  /** 去确认 on a 资料更新 card: opens the page of the first draft waiting for 确认替换, in review mode. */
  botGoConfirm(key: string): void;
  botDecide(id: string, op: "approve" | "reject" | "undo"): void;
  /** 在页面里看 on a materials draft row: open the page in review mode at its first marked section. */
  openRevise(pageId: string): void;
  /** A click on a 活动记录 change: open the page the change is in, at its section (framed) when known. */
  goToChange(pageId: string, sid: string | null): void;
  /** key = trigger id, or "s:" + suggestion_id when there is no trigger. */
  botToggleWhy(key: string): void;
  botSaveSettings(patch: SettingsPatch): void;
  botPause(until: PauseUntil): void;
  /** A page chip's suggestion_id. */
  botChip(id: string): void;
  closeCelebrate(): void;
  /** 个性化 · 资料: rename, delete, open, retry a lost progress stream, 重试 the list. */
  sourceStartRename(id: string): void; sourceSetRenameDraft(text: string): void; sourceCommitRename(): void; sourceCancelRename(): void;
  sourceAskDelete(id: string): void; sourceCancelDelete(): void; sourceConfirmDelete(id: string): void;
  /** 添加资料: each file through the same upload as 学点新的 (kept, full import); a refusal is a notice. */
  sourcesAdd(files: File[]): void;
  sourceOpen(id: string): void; sourceRetryFollow(id: string): void; sourceRetryImport(id: string): void; sourcesReload(): void;
}

/**
 * The small knowledge cloud in 今天 (知识图谱): a persistent node (the `.ew-mini` box) that is moved, never rebuilt; the sidebar tells it when it was redrawn.
 */
export interface CloudSlot { readonly root: HTMLElement; sync(): void;
  /** The goal rows under the cloud and their failure line (app.js goalsBlock). */
  readonly goals?: { readonly root: HTMLElement; readonly line: HTMLElement } | null }
export interface RenderCtx { renderMarkdown(el: HTMLElement, md: string): void; names: Names; cloud?: CloudSlot }

/** Write buttons are disabled while anything is in flight, offline, or before the plugin is configured. */
export function writeBlocked(s: WikiState): boolean {
  return s.busy !== null || s.conn === "offline" || !s.configured;
}

/**
 * Why a new goal cannot start now (not set up / offline / another write running), or null. `own`: the busy kinds that
 * belong to the goal flow itself (they do not count as "another write").
 */
export function writeBlockedReason(s: WikiState, own: readonly string[] = []): string | null {
  if (!s.configured) return WIKI_COPY.needsSettings;
  if (s.conn === "offline") return WIKI_COPY.offline;
  if (s.busy !== null && !own.includes(s.busy.what)) return WIKI_COPY.nextPreparing;
  return null;
}
