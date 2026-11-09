import { tr } from "../i18n/index";
/**
 * The sidebar shell and 今天 copy, verbatim from the approved demo (full-demo/src/app.js: sideTab, todayView, nextBlock,
 * reviewCard, learnCard, attention). Strings the demo has that WIKI_COPY already spells the same way are taken from
 * WIKI_COPY by the renderers; only what is new (or worded differently there) lives here.
 */
export const TODAY_COPY = {
  // shell.html: aside.side + .side-head
  get sideAria() { return tr("wiki.today-copy.TODAY_COPY.sideAria.1"); },
  tabCountLabel: (name: string, k: number): string => tr("wiki.today-copy.TODAY_COPY.tabCountLabel.2", { v0: name, v1: k }),
  brand: "EvoWiki",
  get tabToday() { return tr("wiki.today-copy.TODAY_COPY.tabToday.3"); }, get tabAssistant() { return tr("wiki.today-copy.TODAY_COPY.tabAssistant.4"); }, get tabEngine() { return tr("wiki.today-copy.TODAY_COPY.tabEngine.5"); },
  // todayView / nextBlock
  get todoTitle() { return tr("wiki.today-copy.TODAY_COPY.todoTitle.6"); },
  get todoLoading() { return tr("wiki.today-copy.TODAY_COPY.todoLoading.7"); },
  get todoEmpty() { return tr("wiki.today-copy.TODAY_COPY.todoEmpty.8"); },
  dueHidden: (n: number): string => tr("wiki.today-copy.TODAY_COPY.dueHidden.9", { v0: n }),
  get todoLoadFailed() { return tr("wiki.today-copy.TODAY_COPY.todoLoadFailed.10"); },
  get mapTitle() { return tr("wiki.today-copy.TODAY_COPY.mapTitle.11"); },
  get mapEmpty() { return tr("wiki.today-copy.TODAY_COPY.mapEmpty.12"); },
  get miniAria() { return tr("wiki.today-copy.TODAY_COPY.miniAria.13"); },
  // goalsBlock
  goalOpenAria: (name: string): string => tr("wiki.today-copy.TODAY_COPY.goalOpenAria.14", { v0: name }),
  goalAt: (a: number, n: number): string => tr("wiki.today-copy.TODAY_COPY.goalAt.15", { v0: a, v1: n }),
  get goalDone() { return tr("wiki.today-copy.TODAY_COPY.goalDone.16"); },
  get continueStep() { return tr("wiki.today-copy.TODAY_COPY.continueStep.17"); },
  /** No path yet (a region still being diagnosed / planned): nothing is learned yet. */
  get goalNotStarted() { return tr("wiki.today-copy.TODAY_COPY.goalNotStarted.18"); },
  // taskCard / nextBlock
  reviewSize: (n: number, minutes: number): string => tr("wiki.today-copy.TODAY_COPY.reviewSize.19", { v0: n, v1: minutes }),
  get reviewRecheck() { return tr("wiki.today-copy.TODAY_COPY.reviewRecheck.20"); },
  get reviewStart() { return tr("wiki.today-copy.TODAY_COPY.reviewStart.21"); }, get reviewLess() { return tr("wiki.today-copy.TODAY_COPY.reviewLess.22"); }, get notInterested() { return tr("wiki.today-copy.TODAY_COPY.notInterested.23"); },
  get whyLabel() { return tr("wiki.today-copy.TODAY_COPY.whyLabel.24"); },
  get allDone() { return tr("wiki.today-copy.TODAY_COPY.allDone.25"); },
  // reviewCard
  get cardIssue() { return tr("wiki.today-copy.TODAY_COPY.cardIssue.26"); }, get endReview() { return tr("wiki.today-copy.TODAY_COPY.endReview.27"); },
  // doneCard
  get doneTitle() { return tr("wiki.today-copy.TODAY_COPY.doneTitle.28"); }, get doneReviewed() { return tr("wiki.today-copy.TODAY_COPY.doneReviewed.29"); }, get doneTomorrow() { return tr("wiki.today-copy.TODAY_COPY.doneTomorrow.30"); }, get doneRight() { return tr("wiki.today-copy.TODAY_COPY.doneRight.31"); }, get doneStreak() { return tr("wiki.today-copy.TODAY_COPY.doneStreak.32"); }, get doneMore() { return tr("wiki.today-copy.TODAY_COPY.doneMore.33"); },
  get unitCards() { return tr("wiki.today-copy.TODAY_COPY.unitCards.34"); }, get unitDays() { return tr("wiki.today-copy.TODAY_COPY.unitDays.35"); },
  // learnCard
  get learnPlaceholder() { return tr("wiki.today-copy.TODAY_COPY.learnPlaceholder.36"); },
} as const;
