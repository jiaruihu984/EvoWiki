import { tr } from "../../i18n/index";
/** 学点新的: every string verbatim from full-demo/src/big-goal-demo.html (as build.py transforms it) and app.js. */
export const LEARN_COPY = {
  get title() { return tr("wiki.learn.copy.LEARN_COPY.title.1"); },
  get canvasLabel() { return tr("wiki.learn.copy.LEARN_COPY.canvasLabel.2"); },
  get legendKnown() { return tr("wiki.learn.copy.LEARN_COPY.legendKnown.3"); }, get legendShaky() { return tr("wiki.learn.copy.LEARN_COPY.legendShaky.4"); }, get legendGap() { return tr("wiki.learn.copy.LEARN_COPY.legendGap.5"); },
  get newGoal() { return tr("wiki.learn.copy.LEARN_COPY.newGoal.6"); },
  get goalTitle() { return tr("wiki.learn.copy.LEARN_COPY.goalTitle.7"); }, get goalLabel() { return tr("wiki.learn.copy.LEARN_COPY.goalLabel.8"); }, get backgroundLabel() { return tr("wiki.learn.copy.LEARN_COPY.backgroundLabel.9"); }, get start() { return tr("wiki.learn.copy.LEARN_COPY.start.10"); },
  /** The demo's prefilled goal: only the fake's demo value when no topic was passed. */
  get demoGoal() { return tr("wiki.learn.copy.LEARN_COPY.demoGoal.11"); },
  thinkingMap: (goal: string) => tr("wiki.learn.copy.LEARN_COPY.thinkingMap.12", { v0: goal }),
  /** No goal text, only a material (final review #7): the material's name, never empty quotes. */
  thinkingMapMaterial: (title: string) => tr("wiki.learn.copy.LEARN_COPY.thinkingMapMaterial.13", { v0: title }),
  get thinkingMapPlain() { return tr("wiki.learn.copy.LEARN_COPY.thinkingMapPlain.14"); },
  get skip() { return tr("wiki.learn.copy.LEARN_COPY.skip.15"); }, get idk() { return tr("wiki.learn.copy.LEARN_COPY.idk.16"); },
  get pathTitle() { return tr("wiki.learn.copy.LEARN_COPY.pathTitle.17"); }, get startLearn() { return tr("wiki.learn.copy.LEARN_COPY.startLearn.18"); }, get collapse() { return tr("wiki.learn.copy.LEARN_COPY.collapse.19"); },
  /** A block begun (GET /wiki/progress's blocks; not in the demo): its tag, 继续 instead of 进入, 已学完 as the known chip. */
  blockLearning: (done: number, total: number) => (total > 0 ? tr("wiki.learn.copy.LEARN_COPY.blockLearning.20", { v0: done, v1: total }) : tr("wiki.learn.copy.LEARN_COPY.blockLearning.21")),
  get blockDiagnosing() { return tr("wiki.learn.copy.LEARN_COPY.blockDiagnosing.22"); }, get blockDone() { return tr("wiki.learn.copy.LEARN_COPY.blockDone.23"); }, get continueBlock() { return tr("wiki.learn.copy.LEARN_COPY.continueBlock.24"); },
  /** Not in the demo (spec 2026-10-28 on-demand decomposition): a block entered, its parts shown, none picked yet. */
  get blockChoosing() { return tr("wiki.learn.copy.LEARN_COPY.blockChoosing.25"); },
  aim: (aim: string) => tr("wiki.learn.copy.LEARN_COPY.aim.26") + aim,
  /** A page card while its page is written (the demo's enterCard). */
  get writing() { return tr("wiki.learn.copy.LEARN_COPY.writing.27"); },
  /** User review: the canvas without an existing map and without a goal map (not in the demo; the user's words). */
  get empty() { return tr("wiki.learn.copy.LEARN_COPY.empty.28"); },
  get graphPlacesFailed() { return tr("wiki.learn.copy.LEARN_COPY.graphPlacesFailed.29"); },
  get bandKnown() { return tr("wiki.learn.copy.LEARN_COPY.bandKnown.30"); }, get bandShaky() { return tr("wiki.learn.copy.LEARN_COPY.bandShaky.31"); }, get bandGap() { return tr("wiki.learn.copy.LEARN_COPY.bandGap.32"); },
  /** Not in the demo (its script never fails): the plain lines for a failed goal call; the rest use the wiki's own. */
  get errEnterConflict() { return tr("wiki.learn.copy.LEARN_COPY.errEnterConflict.33"); },
  get errEnterGone() { return tr("wiki.learn.copy.LEARN_COPY.errEnterGone.34"); },
  get errDraftGone() { return tr("wiki.learn.copy.LEARN_COPY.errDraftGone.35"); },
  /** Not in the demo: 进入 while a step is still running, or while the diagnosis is still open (never a silent no-op). */
  get errBusy() { return tr("wiki.learn.copy.LEARN_COPY.errBusy.36"); },
  get errDiagnosing() { return tr("wiki.learn.copy.LEARN_COPY.errDiagnosing.37"); },
  /** Not in the demo: 开始 with no assistant to send the goal to (a goal is made only in the chat). */
  get noAssistant() { return tr("wiki.learn.copy.LEARN_COPY.noAssistant.38"); },
  /** 上传附件 (full-demo goalCard after the 2026-10-21 rulings): the button, the material card, its 目录. */
  get attach() { return tr("wiki.learn.copy.LEARN_COPY.attach.39"); },
  get attachTitle() { return tr("wiki.learn.copy.LEARN_COPY.attachTitle.40"); },
  get parsing() { return tr("wiki.learn.copy.LEARN_COPY.parsing.41"); },
  get removeMaterial() { return tr("wiki.learn.copy.LEARN_COPY.removeMaterial.42"); },
  get toc() { return tr("wiki.learn.copy.LEARN_COPY.toc.43"); }, get tocFold() { return tr("wiki.learn.copy.LEARN_COPY.tocFold.44"); },
  get tocRename() { return tr("wiki.learn.copy.LEARN_COPY.tocRename.45"); }, get tocNameLabel() { return tr("wiki.learn.copy.LEARN_COPY.tocNameLabel.46"); },
  tocPage: (n: number) => "p." + n,
  tocUp: (title: string) => tr("wiki.learn.copy.LEARN_COPY.tocUp.47") + title, tocDown: (title: string) => tr("wiki.learn.copy.LEARN_COPY.tocDown.48") + title,
  attached: (name: string) => "📎 " + name,
  /** Not in the demo (its script never fails): any refused upload or failed read (the user's words). */
  get attachFailed() { return tr("wiki.learn.copy.LEARN_COPY.attachFailed.49"); },
  /** Chat-first blocks (big-goal-demo.html start / pathCard / enterCard, 2026-10-23). */
  thinkingBlocks: (topic: string) => tr("wiki.learn.copy.LEARN_COPY.thinkingBlocks.50", { v0: topic }),
  splittingBlock: (name: string) => tr("wiki.learn.copy.LEARN_COPY.splittingBlock.51", { v0: name }),
  checkingPage: (name: string) => tr("wiki.learn.copy.LEARN_COPY.checkingPage.52", { v0: name }),
  /**
   * Not in the demo (spec 2026-10-28): 开始学习 on a node splits it one level; its parts are rows under it, indented, with
   * the same button (继续学习 once begun) and, on a point, ⋯ 继续拆细.
   */
  splittingParts: (name: string) => tr("wiki.learn.copy.LEARN_COPY.splittingParts.53", { v0: name }),
  get splitFurther() { return tr("wiki.learn.copy.LEARN_COPY.splitFurther.54"); }, get partSplitting() { return tr("wiki.learn.copy.LEARN_COPY.partSplitting.55"); },
  partSplit: (name: string, n: number) => tr("wiki.learn.copy.LEARN_COPY.partSplit.56", { v0: name, v1: n }),
  /** A page row's button: 开始生成, its progress while written, 打开; 重新生成 after a failed write. */
  get pageWriteFailed() { return tr("wiki.learn.copy.LEARN_COPY.pageWriteFailed.57"); }, get pageWrite() { return tr("wiki.learn.copy.LEARN_COPY.pageWrite.58"); }, get pageRewrite() { return tr("wiki.learn.copy.LEARN_COPY.pageRewrite.59"); }, get pageOpen() { return tr("wiki.learn.copy.LEARN_COPY.pageOpen.60"); },
  pagePct: (pct: number) => `${pct}%`,
  grip: "⋮⋮", more: "⋯",
  moreLabel: (name: string) => tr("wiki.learn.copy.LEARN_COPY.moreLabel.61", { v0: name }),
  get menuRename() { return tr("wiki.learn.copy.LEARN_COPY.menuRename.62"); }, get menuRenamePage() { return tr("wiki.learn.copy.LEARN_COPY.menuRenamePage.63"); }, get menuDelete() { return tr("wiki.learn.copy.LEARN_COPY.menuDelete.64"); }, get menuSplit() { return tr("wiki.learn.copy.LEARN_COPY.menuSplit.65"); },
  get save() { return tr("wiki.learn.copy.LEARN_COPY.save.66"); }, get cancel() { return tr("wiki.learn.copy.LEARN_COPY.cancel.67"); }, get nameLabel() { return tr("wiki.learn.copy.LEARN_COPY.nameLabel.68"); },
  get addBlock() { return tr("wiki.learn.copy.LEARN_COPY.addBlock.69"); }, get addPage() { return tr("wiki.learn.copy.LEARN_COPY.addPage.70"); }, get addGo() { return tr("wiki.learn.copy.LEARN_COPY.addGo.71"); },
  get confirm() { return tr("wiki.learn.copy.LEARN_COPY.confirm.72"); },
  /** A block's state word after its name (dot + word; the 知识图谱's words): 掌握 / 熟悉 / 不会 / 正在问 / 还没问. */
  get stateWord() { return { known: tr("wiki.learn.copy.LEARN_COPY.stateWord.known.73"), shaky: tr("wiki.learn.copy.LEARN_COPY.stateWord.shaky.74"), gap: tr("wiki.learn.copy.LEARN_COPY.stateWord.gap.75"), asking: tr("wiki.learn.copy.LEARN_COPY.stateWord.asking.76"), none: tr("wiki.learn.copy.LEARN_COPY.stateWord.none.77") }; },
  blockLabel: (n: number, name: string, state: string) => tr("wiki.learn.copy.LEARN_COPY.blockLabel.78", { v0: n, v1: name, v2: state }),
  pageLabel: (n: number, title: string) => tr("wiki.learn.copy.LEARN_COPY.pageLabel.79", { v0: n, v1: title }),
  blockRemoved: (name: string) => tr("wiki.learn.copy.LEARN_COPY.blockRemoved.80", { v0: name }),
  pageRenamed: (title: string) => tr("wiki.learn.copy.LEARN_COPY.pageRenamed.81", { v0: title }),
  pageRemoved: (title: string) => tr("wiki.learn.copy.LEARN_COPY.pageRemoved.82", { v0: title }),
  pageSplit: (title: string, n: number) => tr("wiki.learn.copy.LEARN_COPY.pageSplit.83", { v0: title, v1: n }),
  get undo() { return tr("wiki.learn.copy.LEARN_COPY.undo.84"); },
  /** Not in the demo (its split is instant): 拆成多页 waits on the model; the row says so (the 知识图谱 path card's words). */
  get rowSplitting() { return tr("wiki.learn.copy.LEARN_COPY.rowSplitting.85"); },
  /** Not in the demo (its script never fails): the chat could not take the goal, or its turn failed. */
  get chatFailed() { return tr("wiki.learn.copy.LEARN_COPY.chatFailed.86"); },
  /** 开始's message (goal + 背景) over the 助手's limit: the count, as the chat's composer shows it. */
  tooLong: (n: number) => `${n}/4000`,
  /** 开始 with a material and no goal words: the chat message (the server's own wording for such a goal). */
  byMaterial: (titles: string) => tr("wiki.learn.copy.LEARN_COPY.byMaterial.87", { v0: titles }),
  /**
   * 读懂这一篇 (reading-mode spec §5; not in the demo): the goal's prerequisites (the 学习路径 card, rows as ever) and its
   * units in the material's order. A unit read shows its points' colour, never 已掌握.
   */
  get prereqTitle() { return tr("wiki.learn.copy.LEARN_COPY.prereqTitle.88"); }, get readTitle() { return tr("wiki.learn.copy.LEARN_COPY.readTitle.89"); },
  readProgress: (done: number, total: number) => (done <= 0 ? tr("wiki.learn.copy.LEARN_COPY.readProgress.90", { v0: total }) : done >= total ? tr("wiki.learn.copy.LEARN_COPY.readProgress.91", { v0: total }) : tr("wiki.learn.copy.LEARN_COPY.readProgress.92", { v0: done, v1: total })),
  get readUnit() { return tr("wiki.learn.copy.LEARN_COPY.readUnit.93"); }, get continueUnit() { return tr("wiki.learn.copy.LEARN_COPY.continueUnit.94"); }, get unitReading() { return tr("wiki.learn.copy.LEARN_COPY.unitReading.95"); }, get unitDone() { return tr("wiki.learn.copy.LEARN_COPY.unitDone.96"); }, get unitSkipped() { return tr("wiki.learn.copy.LEARN_COPY.unitSkipped.97"); },
  get unitOpening() { return tr("wiki.learn.copy.LEARN_COPY.unitOpening.98"); },
  unitNotWritten: (title: string) => tr("wiki.learn.copy.LEARN_COPY.unitNotWritten.99", { v0: title }),
  useChip: (name: string) => tr("wiki.learn.copy.LEARN_COPY.useChip.100", { v0: name }),
  usesHint: (names: readonly string[]) => tr("wiki.learn.copy.LEARN_COPY.usesHint.101", { v0: names.join("、") }),
  get learnFirst() { return tr("wiki.learn.copy.LEARN_COPY.learnFirst.102"); }, get readAnyway() { return tr("wiki.learn.copy.LEARN_COPY.readAnyway.103"); },
  get unitUncertain() { return tr("wiki.learn.copy.LEARN_COPY.unitUncertain.104"); },
  get unitUncertainWhy() { return tr("wiki.learn.copy.LEARN_COPY.unitUncertainWhy.105"); },
  /** Not in the demo: the confirmed goal's row carried no draft / map to open. */
  get errGoalLost() { return tr("wiki.learn.copy.LEARN_COPY.errGoalLost.106"); },
} as const;
