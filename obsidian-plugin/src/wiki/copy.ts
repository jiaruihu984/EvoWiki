import { tr } from "../i18n/index";
/** All UI copy of the personal-wiki sidebar (Chinese). New UI text goes here, never as literals elsewhere. */
import type { UnsafeWhy } from "./note/hand-edit";

/** Up to three names joined with 、, then 等 when there are more. */
const listNames = (names: string[]): string => names.slice(0, 3).join("、") + (names.length > 3 ? tr("wiki.copy.listNames.1") : "");

const HAND_EDIT_WHY: Record<UnsafeWhy, string> = {
  get block() { return tr("wiki.copy.HAND_EDIT_WHY.block.2"); },
  get fence() { return tr("wiki.copy.HAND_EDIT_WHY.fence.3"); },
  get check() { return tr("wiki.copy.HAND_EDIT_WHY.check.4"); },
  get heading() { return tr("wiki.copy.HAND_EDIT_WHY.heading.5"); },
  get cue() { return tr("wiki.copy.HAND_EDIT_WHY.cue.6"); },
  get first() { return tr("wiki.copy.HAND_EDIT_WHY.first.7"); },
  get source() { return tr("wiki.copy.HAND_EDIT_WHY.source.8"); },
  get all() { return tr("wiki.copy.HAND_EDIT_WHY.all.9"); },
  get large() { return tr("wiki.copy.HAND_EDIT_WHY.large.10"); },
};

export const WIKI_COPY = {
  get blockEventStorageFailed() { return tr("wiki.copy.WIKI_COPY.blockEventStorageFailed.11"); },
  get blockProgressStorageFailed() { return tr("wiki.copy.WIKI_COPY.blockProgressStorageFailed.12"); },
  get viewTitle() { return tr("wiki.copy.WIKI_COPY.viewTitle.13"); },
  get demoBanner() { return tr("wiki.copy.WIKI_COPY.demoBanner.14"); },
  // the sidebar header and its two tabs (labels never change; no count badge)
  brand: "EvoWiki",
  get tabToday() { return tr("wiki.copy.WIKI_COPY.tabToday.15"); }, get tabAssistant() { return tr("wiki.copy.WIKI_COPY.tabAssistant.16"); },
  // 今天: what is shown until at least one of its reads has landed
  get todayReading() { return tr("wiki.copy.WIKI_COPY.todayReading.17"); }, get todayLoadFailed() { return tr("wiki.copy.WIKI_COPY.todayLoadFailed.18"); },
  // 最近为你改了什么 (GET /wiki/rsi/changes): never a patch id, a track, a raw status or a number from the backend's A track
  get rsiTitle() { return tr("wiki.copy.WIKI_COPY.rsiTitle.19"); },
  get rsiLoading() { return tr("wiki.copy.WIKI_COPY.rsiLoading.20"); }, get rsiEmpty() { return tr("wiki.copy.WIKI_COPY.rsiEmpty.21"); },
  get rsiStatus() { return { live: tr("wiki.copy.WIKI_COPY.rsiStatus.live.22"), declined: tr("wiki.copy.WIKI_COPY.rsiStatus.declined.23"), undone: tr("wiki.copy.WIKI_COPY.rsiStatus.undone.24"), review: tr("wiki.copy.WIKI_COPY.rsiStatus.review.25") }; },
  /** A rule change's pages still out of date: each is rewritten when the learner next opens it (no page cap). */
  rsiPagesWaiting: (n: number) => tr("wiki.copy.WIKI_COPY.rsiPagesWaiting.26", { v0: n }),
  rsiPagesFailed: (n: number) => tr("wiki.copy.WIKI_COPY.rsiPagesFailed.27", { v0: n }),
  get rsiUndo() { return tr("wiki.copy.WIKI_COPY.rsiUndo.28"); },
  get rsiBusy() { return tr("wiki.copy.WIKI_COPY.rsiBusy.29"); }, get rsiGone() { return tr("wiki.copy.WIKI_COPY.rsiGone.30"); }, get rsiFailed() { return tr("wiki.copy.WIKI_COPY.rsiFailed.31"); },
  /** A code change under review: 采用 / 不要, their notices, and 采用 refused while code changes are switched off. */
  get rsiAdopt() { return tr("wiki.copy.WIKI_COPY.rsiAdopt.32"); }, get rsiDiscard() { return tr("wiki.copy.WIKI_COPY.rsiDiscard.33"); },
  get rsiAdopted() { return tr("wiki.copy.WIKI_COPY.rsiAdopted.34"); }, get rsiDiscarded() { return tr("wiki.copy.WIKI_COPY.rsiDiscarded.35"); },
  get rsiPatchesOff() { return tr("wiki.copy.WIKI_COPY.rsiPatchesOff.36"); },
  // 个性化 · 活动记录 · 撤销, and the lines shared by the sidebar's write buttons
  get noteNotYet() { return tr("wiki.copy.WIKI_COPY.noteNotYet.37"); }, get attnWorking() { return tr("wiki.copy.WIKI_COPY.attnWorking.38"); }, get attnBlockedBusy() { return tr("wiki.copy.WIKI_COPY.attnBlockedBusy.39"); }, get attnBlockedDemo() { return tr("wiki.copy.WIKI_COPY.attnBlockedDemo.40"); },
  // 本页
  get tocTitle() { return tr("wiki.copy.WIKI_COPY.tocTitle.41"); },
  get tocFolded() { return tr("wiki.copy.WIKI_COPY.tocFolded.42"); }, get tocLearning() { return tr("wiki.copy.WIKI_COPY.tocLearning.43"); }, get tocNew() { return tr("wiki.copy.WIKI_COPY.tocNew.44"); }, tocChecks: (n: number) => tr("wiki.copy.WIKI_COPY.tocChecks.45", { v0: n }), get tocAnswered() { return tr("wiki.copy.WIKI_COPY.tocAnswered.46"); },
  tocJump: (heading: string) => tr("wiki.copy.WIKI_COPY.tocJump.47", { v0: heading }),
  get tocUntitled() { return tr("wiki.copy.WIKI_COPY.tocUntitled.48"); },
  get foldShort() { return tr("wiki.copy.WIKI_COPY.foldShort.49"); }, get unfoldShort() { return tr("wiki.copy.WIKI_COPY.unfoldShort.50"); },
  get feelShallow() { return tr("wiki.copy.WIKI_COPY.feelShallow.51"); }, get feelRight() { return tr("wiki.copy.WIKI_COPY.feelRight.52"); }, get feelHard() { return tr("wiki.copy.WIKI_COPY.feelHard.53"); },
  get feelSaved() { return tr("wiki.copy.WIKI_COPY.feelSaved.54"); },
  // 笔记末尾的「读完了」区块 (end-block/): drawn by the plugin, never written to the note
  get endTitle() { return tr("wiki.copy.WIKI_COPY.endTitle.55"); },
  /** 内容太简单 / 理解有困难: sent to the assistant as the learner's message, with the page quoted (app.js sendToAssistant). */
  get endSendShallow() { return tr("wiki.copy.WIKI_COPY.endSendShallow.56"); }, get endSendHard() { return tr("wiki.copy.WIKI_COPY.endSendHard.57"); },
  get endNext() { return tr("wiki.copy.WIKI_COPY.endNext.58"); },
  reviewFirst: (name: string): string => tr("wiki.copy.WIKI_COPY.reviewFirst.59", { v0: name }),
  get reviewFirstGo() { return tr("wiki.copy.WIKI_COPY.reviewFirstGo.60"); }, get reviewFirstSkip() { return tr("wiki.copy.WIKI_COPY.reviewFirstSkip.61"); },
  reviewFirstAsk: (name: string): string => tr("wiki.copy.WIKI_COPY.reviewFirstAsk.62", { v0: name }),
  get endGoalDone() { return tr("wiki.copy.WIKI_COPY.endGoalDone.63"); },
  endNextBlock: (name: string) => (name !== "" ? tr("wiki.copy.WIKI_COPY.endNextBlock.64", { v0: name }) : tr("wiki.copy.WIKI_COPY.endNextBlock.65")),
  get endPageDone() { return tr("wiki.copy.WIKI_COPY.endPageDone.66"); },
  get endDoneFailed() { return tr("wiki.copy.WIKI_COPY.endDoneFailed.67"); },
  get endPageDoneLine() { return tr("wiki.copy.WIKI_COPY.endPageDoneLine.68"); },
  // the line at the top of the page
  topChanged: (summary: string) => tr("wiki.copy.WIKI_COPY.topChanged.69", { v0: summary }),
  /** Older servers (and stored summaries) lead with this; the top line strips it before adding its own 「本页已更新：」. */
  get topChangedLegacy() { return tr("wiki.copy.WIKI_COPY.topChangedLegacy.70"); },
  pageWrittenFor: (goal: string) => tr("wiki.copy.WIKI_COPY.pageWrittenFor.71", { v0: goal }),
  pageUsedFor: (goal: string) => tr("wiki.copy.WIKI_COPY.pageUsedFor.72", { v0: goal }),
  get topUndo() { return tr("wiki.copy.WIKI_COPY.topUndo.73"); }, get topUndoing() { return tr("wiki.copy.WIKI_COPY.topUndoing.74"); },
  get topUndoFailed() { return tr("wiki.copy.WIKI_COPY.topUndoFailed.75"); },
  get topUndoChanged() { return tr("wiki.copy.WIKI_COPY.topUndoChanged.76"); },
  handEditUnsafe: (label: string, why: UnsafeWhy): string => tr("wiki.copy.WIKI_COPY.handEditUnsafe.77", { v0: label ? tr("wiki.copy.WIKI_COPY.handEditUnsafe.78", { v0: label }) : "", v1: HAND_EDIT_WHY[why] }),
  handEditRefused: (detail: string): string => tr("wiki.copy.WIKI_COPY.handEditRefused.79", { v0: detail }),
  get handEditRestore() { return tr("wiki.copy.WIKI_COPY.handEditRestore.80"); },
  get handEditKeep() { return tr("wiki.copy.WIKI_COPY.handEditKeep.81"); },
  get handEditStaleMark() { return tr("wiki.copy.WIKI_COPY.handEditStaleMark.82"); },
  get sectionTag() { return tr("wiki.copy.WIKI_COPY.sectionTag.83"); },
  // corner notices
  get noticeUndone() { return tr("wiki.copy.WIKI_COPY.noticeUndone.84"); },
  // section feedback (controller.feedback)
  get feedbackPageGone() { return tr("wiki.copy.WIKI_COPY.feedbackPageGone.85"); },
  get feedbackSectionGone() { return tr("wiki.copy.WIKI_COPY.feedbackSectionGone.86"); },
  /** 「哪一部分」: the whole page, then every section in page order (R3.2; sections without a `##` heading too). */
  get feedbackOrganizer() { return tr("wiki.copy.WIKI_COPY.feedbackOrganizer.87"); }, get feedbackCheck() { return tr("wiki.copy.WIKI_COPY.feedbackCheck.88"); },
  feedbackLine: (t: string) => tr("wiki.copy.WIKI_COPY.feedbackLine.89", { v0: t }), feedbackRecap: (t: string) => tr("wiki.copy.WIKI_COPY.feedbackRecap.90", { v0: t }),
  feedbackFolded: (label: string) => tr("wiki.copy.WIKI_COPY.feedbackFolded.91", { v0: label }),
  /**
   * A hand edit's summary (the `note_edit` signal's text, never shown to the user): where each change is,
   * then its removed / added lines.
   */
  noteEditPart: (label: string) => `【${label}】`, get noteEditStart() { return tr("wiki.copy.WIKI_COPY.noteEditStart.92"); },
  noteEditDel: (line: string) => tr("wiki.copy.WIKI_COPY.noteEditDel.93", { v0: line }), noteEditAdd: (line: string) => tr("wiki.copy.WIKI_COPY.noteEditAdd.94", { v0: line }),
  noteEditMore: (n: number) => tr("wiki.copy.WIKI_COPY.noteEditMore.95", { v0: n }),
  noteEditTooMany: (before: number, after: number) => tr("wiki.copy.WIKI_COPY.noteEditTooMany.96", { v0: before, v1: after }),
  get reorganizePage() { return tr("wiki.copy.WIKI_COPY.reorganizePage.97"); },
  // learning path
  pathPageTitle: (goal: string, i: number, n: number) => tr("wiki.copy.WIKI_COPY.pathPageTitle.98", { v0: goal, v1: i, v2: n }),
  pathNext: (i: number) => tr("wiki.copy.WIKI_COPY.pathNext.99", { v0: i }), get pathDone() { return tr("wiki.copy.WIKI_COPY.pathDone.100"); },
  pathCard: (done: number, n: number) => tr("wiki.copy.WIKI_COPY.pathCard.101", { v0: done, v1: n }),
  // Offline: what a failed call says (a notice, the line by a write button, a read's reason in 设置).
  get offline() { return tr("wiki.copy.WIKI_COPY.offline.102"); },
  // The two offline banners (the top of the sidebar, the centre pane): with 重试. Nothing else says offline.
  get notConnected() { return tr("wiki.copy.WIKI_COPY.notConnected.103"); },
  // A read that failed while the server answers: one short line in that section, then 重试.
  get readFailedShort() { return tr("wiki.copy.WIKI_COPY.readFailedShort.104"); },
  get needsSettings() { return tr("wiki.copy.WIKI_COPY.needsSettings.105"); },
  get failed() { return tr("wiki.copy.WIKI_COPY.failed.106"); }, get retry() { return tr("wiki.copy.WIKI_COPY.retry.107"); }, get stop() { return tr("wiki.copy.WIKI_COPY.stop.108"); }, get stopped() { return tr("wiki.copy.WIKI_COPY.stopped.109"); }, get restart() { return tr("wiki.copy.WIKI_COPY.restart.110"); }, get loading() { return tr("wiki.copy.WIKI_COPY.loading.111"); },
  // Usage limit (429 / budget_exceeded), never retried: the server's sentence names the limit; this only when it has none.
  get budget() { return tr("wiki.copy.WIKI_COPY.budget.112"); },
  // Names the server and the sidebar show for an id nothing resolves (never the id itself).
  get unnamedConcept() { return tr("wiki.copy.WIKI_COPY.unnamedConcept.113"); }, get unnamedPage() { return tr("wiki.copy.WIKI_COPY.unnamedPage.114"); },
  // A section id the open page has no heading for (web OTHER_PAGE_SECTION): the id itself is never shown.
  get otherPageSection() { return tr("wiki.copy.WIKI_COPY.otherPageSection.115"); }, otherPageSections: (n: number) => tr("wiki.copy.WIKI_COPY.otherPageSections.116", { v0: n }),
  get refreshFailed() { return tr("wiki.copy.WIKI_COPY.refreshFailed.117"); },
  get refreshOffline() { return tr("wiki.copy.WIKI_COPY.refreshOffline.118"); },
  // a question's 不知道 option
  get idk() { return tr("wiki.copy.WIKI_COPY.idk.119"); },
  // page
  get noPage() { return tr("wiki.copy.WIKI_COPY.noPage.120"); },
  openCurrent: (title: string) => tr("wiki.copy.WIKI_COPY.openCurrent.121", { v0: title }),
  get startPage() { return tr("wiki.copy.WIKI_COPY.startPage.122"); }, get pageGoal() { return tr("wiki.copy.WIKI_COPY.pageGoal.123"); }, get reorganize() { return tr("wiki.copy.WIKI_COPY.reorganize.124"); }, get openNote() { return tr("wiki.copy.WIKI_COPY.openNote.125"); },
  get outlining() { return tr("wiki.copy.WIKI_COPY.outlining.126"); }, get writing() { return tr("wiki.copy.WIKI_COPY.writing.127"); }, get organizeDone() { return tr("wiki.copy.WIKI_COPY.organizeDone.128"); }, get sectionWritingShort() { return tr("wiki.copy.WIKI_COPY.sectionWritingShort.129"); },
  get collapsedNote() { return tr("wiki.copy.WIKI_COPY.collapsedNote.130"); }, get expandPage() { return tr("wiki.copy.WIKI_COPY.expandPage.131"); }, get topCollapsed() { return tr("wiki.copy.WIKI_COPY.topCollapsed.132"); },
  get expandDone() { return tr("wiki.copy.WIKI_COPY.expandDone.133"); }, get shelvePage() { return tr("wiki.copy.WIKI_COPY.shelvePage.134"); }, get shelveDone() { return tr("wiki.copy.WIKI_COPY.shelveDone.135"); },
  get know() { return tr("wiki.copy.WIKI_COPY.know.136"); }, get pin() { return tr("wiki.copy.WIKI_COPY.pin.137"); }, get unpin() { return tr("wiki.copy.WIKI_COPY.unpin.138"); },
  get sectionMenu() { return tr("wiki.copy.WIKI_COPY.sectionMenu.139"); }, get sectionPinned() { return tr("wiki.copy.WIKI_COPY.sectionPinned.140"); },
  get sectionPinnedWhy() { return tr("wiki.copy.WIKI_COPY.sectionPinnedWhy.141"); },
  // the section 「⋯」 menu (section-menu.ts)
  get menuDont() { return tr("wiki.copy.WIKI_COPY.menuDont.142"); }, get menuPinnedHint() { return tr("wiki.copy.WIKI_COPY.menuPinnedHint.143"); }, get menuAsk() { return tr("wiki.copy.WIKI_COPY.menuAsk.144"); }, get menuMore() { return tr("wiki.copy.WIKI_COPY.menuMore.145"); },
    /** 「这里的概念我不会」 results: up to three names, then 等. */
  get dontDone() { return tr("wiki.copy.WIKI_COPY.dontDone.146"); }, get reteachDraft() { return tr("wiki.copy.WIKI_COPY.reteachDraft.147"); },
  dontPartial: (n: number, names: string[]) => tr("wiki.copy.WIKI_COPY.dontPartial.148", { v0: n, v1: listNames(names) }),
  get dontNone() { return tr("wiki.copy.WIKI_COPY.dontNone.149"); },
  /** 星图里的「我会 / 我不会」(整个概念): the result Notices. */
  conceptDeclared: (name: string, v: "know" | "dont") => (v === "know" ? tr("wiki.copy.WIKI_COPY.conceptDeclared.150", { v0: name }) : tr("wiki.copy.WIKI_COPY.conceptDeclared.151", { v0: name })),
  get conceptNoAspects() { return tr("wiki.copy.WIKI_COPY.conceptNoAspects.152"); },
  conceptDeclarePartial: (name: string) => tr("wiki.copy.WIKI_COPY.conceptDeclarePartial.153", { v0: name }),
  get fold() { return tr("wiki.copy.WIKI_COPY.fold.154"); },
  // the evowiki-check block in a wiki note (check-block.ts)
  get checkTitle() { return tr("wiki.copy.WIKI_COPY.checkTitle.155"); }, get checkOpen() { return tr("wiki.copy.WIKI_COPY.checkOpen.156"); }, get checkLoading() { return tr("wiki.copy.WIKI_COPY.checkLoading.157"); },
  get checkGone() { return tr("wiki.copy.WIKI_COPY.checkGone.158"); },
  get checkUnreadable() { return tr("wiki.copy.WIKI_COPY.checkUnreadable.159"); },
  // the embedded-html block (an interactive) in a note (embed/embed-block.ts)
  get embedUnreadable() { return tr("wiki.copy.WIKI_COPY.embedUnreadable.160"); }, get embedMissing() { return tr("wiki.copy.WIKI_COPY.embedMissing.161"); },
  get embedFetch() { return tr("wiki.copy.WIKI_COPY.embedFetch.162"); }, get embedFetching() { return tr("wiki.copy.WIKI_COPY.embedFetching.163"); }, get embedFetchFailed() { return tr("wiki.copy.WIKI_COPY.embedFetchFailed.164"); },
  get embedPaused() { return tr("wiki.copy.WIKI_COPY.embedPaused.165"); },
  // the evowiki-image block (an openly licensed image) in a note (embed/image-block.ts)
  get imageUnreadable() { return tr("wiki.copy.WIKI_COPY.imageUnreadable.166"); }, get imageMissing() { return tr("wiki.copy.WIKI_COPY.imageMissing.167"); },
  // the evoui block (a building-block interactive) in a note (evoui/evoui-block.ts)
  get evouiRun() { return tr("wiki.copy.WIKI_COPY.evouiRun.168"); },
  get evouiRunWhy() { return [tr("wiki.copy.WIKI_COPY.evouiRunWhy.169"), tr("wiki.copy.WIKI_COPY.evouiRunWhy.170")]; },
  get evouiRunWhyLabel() { return tr("wiki.copy.WIKI_COPY.evouiRunWhyLabel.171"); },
  get evouiUnreadable() { return tr("wiki.copy.WIKI_COPY.evouiUnreadable.172"); }, get blockResultRejected() { return tr("wiki.copy.WIKI_COPY.blockResultRejected.173"); }, get blockProgressRejected() { return tr("wiki.copy.WIKI_COPY.blockProgressRejected.174"); }, get blockProgressStale() { return tr("wiki.copy.WIKI_COPY.blockProgressStale.175"); }, get evouiMissing() { return tr("wiki.copy.WIKI_COPY.evouiMissing.176"); },
  knowFolded: (h: string) => tr("wiki.copy.WIKI_COPY.knowFolded.177", { v0: h }),
  knowShort: (h: string) => tr("wiki.copy.WIKI_COPY.knowShort.178", { v0: h }),
  rewrote: (h: string) => tr("wiki.copy.WIKI_COPY.rewrote.179", { v0: h }),
  folded: (h: string) => tr("wiki.copy.WIKI_COPY.folded.180", { v0: h }), unfolded: (h: string) => tr("wiki.copy.WIKI_COPY.unfolded.181", { v0: h }),
  pinned: (h: string) => tr("wiki.copy.WIKI_COPY.pinned.182", { v0: h }), unpinned: (h: string) => tr("wiki.copy.WIKI_COPY.unpinned.183", { v0: h }),
  get done() { return tr("wiki.copy.WIKI_COPY.done.184"); },
  get askTitle() { return tr("wiki.copy.WIKI_COPY.askTitle.185"); }, get askPlaceholder() { return tr("wiki.copy.WIKI_COPY.askPlaceholder.186"); }, get askButton() { return tr("wiki.copy.WIKI_COPY.askButton.187"); },

  get noteUserEdited() { return tr("wiki.copy.WIKI_COPY.noteUserEdited.188"); },
  /** A note the user renamed by hand is renamed back to its page title. */
  fileRenamedBack: (title: string): string => tr("wiki.copy.WIKI_COPY.fileRenamedBack.189", { v0: title }),
  get noteBackupFailed() { return tr("wiki.copy.WIKI_COPY.noteBackupFailed.190"); },
  get noteSyncFailed() { return tr("wiki.copy.WIKI_COPY.noteSyncFailed.191"); },
  get otherUsersNote() { return tr("wiki.copy.WIKI_COPY.otherUsersNote.192"); },
  knownRetracted: (lines: readonly string[]): string =>
    (lines.length > 1 ? tr("wiki.copy.WIKI_COPY.knownRetracted.193", { v0: lines[0], v1: lines.length }) : tr("wiki.copy.WIKI_COPY.knownRetracted.194", { v0: lines[0] })),
  profileKept: (n: number): string => tr("wiki.copy.WIKI_COPY.profileKept.195", { v0: n }),
  oldNotesLeft: (paths: readonly string[], to: string) => tr("wiki.copy.WIKI_COPY.oldNotesLeft.196", { v0: paths.join("、"), v1: to }),
  get openNoteShort() { return tr("wiki.copy.WIKI_COPY.openNoteShort.197"); },
  noteSep: "；", listSep: "、",
  // catalog
  get learning() { return tr("wiki.copy.WIKI_COPY.learning.198"); }, get stale() { return tr("wiki.copy.WIKI_COPY.stale.199"); },
  paren: (s: string) => `（${s}）`,
  reason: (s: string) => tr("wiki.copy.WIKI_COPY.reason.200", { v0: s }),
  get settingsSaved() { return tr("wiki.copy.WIKI_COPY.settingsSaved.201"); }, reloadFailed: (why: string) => tr("wiki.copy.WIKI_COPY.reloadFailed.202", { v0: why }),
  get reloadCatalog() { return tr("wiki.copy.WIKI_COPY.reloadCatalog.203"); },
  // after 这一页我学完了
  get pageDone() { return tr("wiki.copy.WIKI_COPY.pageDone.204"); },
  // a result line: what changed on the page
  changedTail: (n: number) => tr("wiki.copy.WIKI_COPY.changedTail.205", { v0: n }), changedAlone: (n: number) => tr("wiki.copy.WIKI_COPY.changedAlone.206", { v0: n }),
  // review
  // 今天 · 下一步 and the in-place review
  get nextTitle() { return tr("wiki.copy.WIKI_COPY.nextTitle.207"); },
  nextWhere: (goal: string, k: number, title: string | null) => tr("wiki.copy.WIKI_COPY.nextWhere.208", { v0: goal, v1: k, v2: title !== null ? ` · ${title}` : "" }),
  get nextPreparing() { return tr("wiki.copy.WIKI_COPY.nextPreparing.209"); },
  // 想学点新的 card and the 新目标 dialog
  get learnTitle() { return tr("wiki.copy.WIKI_COPY.learnTitle.210"); },
  get learnTopicLabel() { return tr("wiki.copy.WIKI_COPY.learnTopicLabel.211"); }, get learnPlaceholder() { return tr("wiki.copy.WIKI_COPY.learnPlaceholder.212"); }, get learnStart() { return tr("wiki.copy.WIKI_COPY.learnStart.213"); },
  get learnLead() { return tr("wiki.copy.WIKI_COPY.learnLead.214"); }, get learnReading() { return tr("wiki.copy.WIKI_COPY.learnReading.215"); },
  get learnKFailed() { return tr("wiki.copy.WIKI_COPY.learnKFailed.216"); }, get learnEmpty() { return tr("wiki.copy.WIKI_COPY.learnEmpty.217"); },
  reviewProgress: (i: number, n: number) => tr("wiki.copy.WIKI_COPY.reviewProgress.218", { v0: i, v1: n }),
  reviewFromSection: (page: string, heading: string) => `${page} · ${heading}`,
  get reviewNext() { return tr("wiki.copy.WIKI_COPY.reviewNext.219"); }, get reviewNextDone() { return tr("wiki.copy.WIKI_COPY.reviewNextDone.220"); }, get reviewRight() { return tr("wiki.copy.WIKI_COPY.reviewRight.221"); },
  get reviewProblem() { return tr("wiki.copy.WIKI_COPY.reviewProblem.222"); }, get reviewEnd() { return tr("wiki.copy.WIKI_COPY.reviewEnd.223"); },
  chatReviewPage: (n: number) => tr("wiki.copy.WIKI_COPY.chatReviewPage.224", { v0: n }), chatReviewLeft: (n: number) => tr("wiki.copy.WIKI_COPY.chatReviewLeft.225", { v0: n }), get chatReviewResume() { return tr("wiki.copy.WIKI_COPY.chatReviewResume.226"); },
  chatReviewDone: (n: number) => tr("wiki.copy.WIKI_COPY.chatReviewDone.227", { v0: n }), get chatReviewNone() { return tr("wiki.copy.WIKI_COPY.chatReviewNone.228"); },
  /** 反馈 on a review card: the quote the assistant gets (app.js `这张卡片：${c.q}`). */
  cardQuote: (question: string) => tr("wiki.copy.WIKI_COPY.cardQuote.229", { v0: question }),
  /** A wrong answer handed to the assistant (题目 / 我选的 / 正确答案, as the 还没掌握 follow-up quotes it). */
  wrongQuote: (stem: string, picked: string, right: string) => tr("wiki.copy.WIKI_COPY.wrongQuote.230", { v0: stem, v1: picked, v2: right }),
  get pageRewritten() { return tr("wiki.copy.WIKI_COPY.pageRewritten.231"); },
  // after a wrong answer: 粗心答错 / 现在会了 / 还没掌握 (followup.ts)
  get followupSlip() { return tr("wiki.copy.WIKI_COPY.followupSlip.232"); }, get followupLearned() { return tr("wiki.copy.WIKI_COPY.followupLearned.233"); }, get followupUnclear() { return tr("wiki.copy.WIKI_COPY.followupUnclear.234"); },
  get followupNoAssistant() { return tr("wiki.copy.WIKI_COPY.followupNoAssistant.235"); },
  get followupOffline() { return tr("wiki.copy.WIKI_COPY.followupOffline.236"); }, get followupDemo() { return tr("wiki.copy.WIKI_COPY.followupDemo.237"); },
  get followupFailed() { return tr("wiki.copy.WIKI_COPY.followupFailed.238"); },
  get followupNotFound() { return tr("wiki.copy.WIKI_COPY.followupNotFound.239"); }, get followupInvalid() { return tr("wiki.copy.WIKI_COPY.followupInvalid.240"); },
  get followupDone() { return tr("wiki.copy.WIKI_COPY.followupDone.241"); }, get followupRetry() { return tr("wiki.copy.WIKI_COPY.followupRetry.242"); },
  /** followup.py ALREADY: this answer has its follow-up (any other 409 only means nothing was recorded, and the buttons stay). */
  get followupAlreadyServer() { return tr("wiki.copy.WIKI_COPY.followupAlreadyServer.243"); }, get followupNoModel() { return tr("wiki.copy.WIKI_COPY.followupNoModel.244"); },
  get followupNoQuestion() { return tr("wiki.copy.WIKI_COPY.followupNoQuestion.245"); },
  // confirmation questions in 复习 (a new question on the same point, due about an hour after 粗心答错 / 现在会了)
  confirmFrom: (concept: string, page: string) => tr("wiki.copy.WIKI_COPY.confirmFrom.246", { v0: concept, v1: page }),
  get confirmUnclear() { return tr("wiki.copy.WIKI_COPY.confirmUnclear.247"); }, get confirmReread() { return tr("wiki.copy.WIKI_COPY.confirmReread.248"); },
  get confirmNotFound() { return tr("wiki.copy.WIKI_COPY.confirmNotFound.249"); }, get confirmAnswered() { return tr("wiki.copy.WIKI_COPY.confirmAnswered.250"); }, get confirmInvalid() { return tr("wiki.copy.WIKI_COPY.confirmInvalid.251"); },
  get confirmReteachFailed() { return tr("wiki.copy.WIKI_COPY.confirmReteachFailed.252"); },
  /** Not in the demo: the note exists but Obsidian could not show it (never a silent click). */
  noteOpenFailed: (title: string) => tr("wiki.copy.WIKI_COPY.noteOpenFailed.253", { v0: title }),
  get reviewBlockedOffline() { return tr("wiki.copy.WIKI_COPY.reviewBlockedOffline.254"); }, get reviewBlockedBusy() { return tr("wiki.copy.WIKI_COPY.reviewBlockedBusy.255"); },
  get reviewPerDayInvalid() { return tr("wiki.copy.WIKI_COPY.reviewPerDayInvalid.256"); },
  get reviewToSection() { return tr("wiki.copy.WIKI_COPY.reviewToSection.257"); },
  get reviewThinkFirst() { return tr("wiki.copy.WIKI_COPY.reviewThinkFirst.258"); }, get reviewReveal() { return tr("wiki.copy.WIKI_COPY.reviewReveal.259"); },
  get gradeRecalled() { return tr("wiki.copy.WIKI_COPY.gradeRecalled.260"); }, get gradeFuzzy() { return tr("wiki.copy.WIKI_COPY.gradeFuzzy.261"); }, get gradeForgot() { return tr("wiki.copy.WIKI_COPY.gradeForgot.262"); },
  // the line under a graded card: only what the system actually did
  get reviewNoted() { return tr("wiki.copy.WIKI_COPY.reviewNoted.263"); },
  reviewSurer: (concept: string) => tr("wiki.copy.WIKI_COPY.reviewSurer.264", { v0: concept }),
  reviewLessSure: (concept: string) => tr("wiki.copy.WIKI_COPY.reviewLessSure.265", { v0: concept }),
  reviewUnfoldedHere: (heading: string) => tr("wiki.copy.WIKI_COPY.reviewUnfoldedHere.266", { v0: heading }),
  reviewUnfolded: (n: number) => tr("wiki.copy.WIKI_COPY.reviewUnfolded.267", { v0: n }),
  reviewFoldedHere: (heading: string) => tr("wiki.copy.WIKI_COPY.reviewFoldedHere.268", { v0: heading }),
  reviewFolded: (n: number) => tr("wiki.copy.WIKI_COPY.reviewFolded.269", { v0: n }),
  reviewRewrote: (n: number) => tr("wiki.copy.WIKI_COPY.reviewRewrote.270", { v0: n }),
  get declareKnow() { return tr("wiki.copy.WIKI_COPY.declareKnow.271"); }, get declareDont() { return tr("wiki.copy.WIKI_COPY.declareDont.272"); }, get declareClear() { return tr("wiki.copy.WIKI_COPY.declareClear.273"); },
  declared: (label: string) => tr("wiki.copy.WIKI_COPY.declared.274", { v0: label }),
} as const;

/** The 用量 section of the plugin settings (usage-section.ts). */
export const USAGE_COPY = {
  get confirmTitle() { return tr("wiki.copy.USAGE_COPY.confirmTitle.275"); },
  unpriced: (count: number): string => tr("wiki.copy.USAGE_COPY.unpriced.276", { v0: count }),
  get confirmBudget() { return tr("wiki.copy.USAGE_COPY.confirmBudget.277"); },
  get confirmCancelled() { return tr("wiki.copy.USAGE_COPY.confirmCancelled.278"); },
  get billingNotice() { return tr("wiki.copy.USAGE_COPY.billingNotice.279"); },
  get versionMismatch() { return tr("wiki.copy.USAGE_COPY.versionMismatch.280"); },
  get title() { return tr("wiki.copy.USAGE_COPY.title.281"); },
  get needsSettings() { return tr("wiki.copy.USAGE_COPY.needsSettings.282"); },
  get loading() { return tr("wiki.copy.USAGE_COPY.loading.283"); },
  totals: (today: string, month: string) => tr("wiki.copy.USAGE_COPY.totals.284", { v0: today, v1: month }),
  groupLine: (group: string, today: string, month: string) => tr("wiki.copy.USAGE_COPY.groupLine.285", { v0: group, v1: today, v2: month }),
  get noSpend() { return tr("wiki.copy.USAGE_COPY.noSpend.286"); },
  get noLimit() { return tr("wiki.copy.USAGE_COPY.noLimit.287"); },
  get daily() { return tr("wiki.copy.USAGE_COPY.daily.288"); }, get monthly() { return tr("wiki.copy.USAGE_COPY.monthly.289"); },
  get invalidLimit() { return tr("wiki.copy.USAGE_COPY.invalidLimit.290"); },
  get saving() { return tr("wiki.copy.USAGE_COPY.saving.291"); }, get saved() { return tr("wiki.copy.USAGE_COPY.saved.292"); },
  get readFailed() { return tr("wiki.copy.USAGE_COPY.readFailed.293"); }, get reread() { return tr("wiki.copy.USAGE_COPY.reread.294"); },
  get lessThanCent() { return tr("wiki.copy.USAGE_COPY.lessThanCent.295"); },
} as const;

/** Command palette names (Ctrl+P). Plain Chinese; ids live in main.ts and never change once shipped (hotkeys). */
export const COMMAND_COPY = {
  // app.js COMMANDS, in its order.
  get starMap() { return tr("wiki.copy.COMMAND_COPY.starMap.296"); },
  get engine() { return tr("wiki.copy.COMMAND_COPY.engine.297"); },
  get review() { return tr("wiki.copy.COMMAND_COPY.review.298"); },
  get newGoal() { return tr("wiki.copy.COMMAND_COPY.newGoal.299"); },
  get recentChanges() { return tr("wiki.copy.COMMAND_COPY.recentChanges.300"); },
  get settings() { return tr("wiki.copy.COMMAND_COPY.settings.301"); },
} as const;
