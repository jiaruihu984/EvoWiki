import { tr, onLanguageChange } from "../../i18n/index";
/** Bot copy, verbatim from the approved demo (app.js: botSettings / task cards / change rows). */
/** Task-card / row labels by suggestion kind (app.js KIND); a kind not listed is not shown. */
export const BOT_KIND: Readonly<Record<string, string>> = {
  get review_due() { return tr("wiki.bot.bot-copy.BOT_KIND.review_due.1"); }, get wrong_answers() { return tr("wiki.bot.bot-copy.BOT_KIND.wrong_answers.2"); }, get open_question() { return tr("wiki.bot.bot-copy.BOT_KIND.open_question.3"); }, get goal_idle() { return tr("wiki.bot.bot-copy.BOT_KIND.goal_idle.4"); }, get chat() { return tr("wiki.bot.bot-copy.BOT_KIND.chat.5"); }, get idle_write() { return tr("wiki.bot.bot-copy.BOT_KIND.idle_write.6"); }, get user_write() { return tr("wiki.bot.bot-copy.BOT_KIND.user_write.7"); },
  get source_added() { return tr("wiki.bot.bot-copy.BOT_KIND.source_added.8"); }, get card_stale() { return tr("wiki.bot.bot-copy.BOT_KIND.card_stale.9"); }, get goal_review() { return tr("wiki.bot.bot-copy.BOT_KIND.goal_review.10"); },
};
/** What the user did with a suggestion (app.js REACT); `chat` kind: `点了「<title>」`. */
export const BOT_REACT: Readonly<Record<string, string>> = { get accepted() { return tr("wiki.bot.bot-copy.BOT_REACT.accepted.11"); }, get not_interested() { return tr("wiki.bot.bot-copy.BOT_REACT.not_interested.12"); }, get done() { return tr("wiki.bot.bot-copy.BOT_REACT.done.13"); } };
export let BOT_REACT_NONE = tr("wiki.bot.bot-copy.BOT_REACT_NONE.14");
onLanguageChange(() => { BOT_REACT_NONE = tr("wiki.bot.bot-copy.BOT_REACT_NONE.14"); });
/** The reaction line of a suggestion (REACT label; a `chat` card that was accepted names its title). */
export function reactLabel(kind: string, status: string, title: string): string {
  if (kind === "chat" && status === "accepted") return tr("wiki.bot.bot-copy..15", { v0: title });
  return BOT_REACT[status] ?? BOT_REACT_NONE;
}

export const BOT_COPY = {
  get reviseNone() { return tr("wiki.bot.bot-copy.BOT_COPY.reviseNone.16"); },
  /** After 减少提醒 / 不感兴趣 (app.js `nope`): review = the review card; else the kind's label (BOT_KIND). */
  prefNotice: (review: boolean, kindLabel: string): string => (review ? tr("wiki.bot.bot-copy.BOT_COPY.prefNotice.17") : tr("wiki.bot.bot-copy.BOT_COPY.prefNotice.18", { v0: kindLabel })),
  /** The pill on a waiting row (asst.js ledger): a draft the learner asked for says 确认, a Bot proposal says 批准. */
  get waitConfirm() { return tr("wiki.bot.bot-copy.BOT_COPY.waitConfirm.19"); },
  get waitApprove() { return tr("wiki.bot.bot-copy.BOT_COPY.waitApprove.20"); },
  get view() { return tr("wiki.bot.bot-copy.BOT_COPY.view.21"); },
  get undo() { return tr("wiki.bot.bot-copy.BOT_COPY.undo.22"); },
  /** demo `c.done`: the action's own detail when it has one. */
  approved: (detail: string): string => (detail.trim() !== "" ? detail : tr("wiki.bot.bot-copy.BOT_COPY.approved.23")),
  get rejected() { return tr("wiki.bot.bot-copy.BOT_COPY.rejected.24"); },
  get undone() { return tr("wiki.bot.bot-copy.BOT_COPY.undone.25"); },
  /** demo `botRuns` 撤销 notice (a page the Bot wrote). */
  get writeUndone() { return tr("wiki.bot.bot-copy.BOT_COPY.writeUndone.26"); },
  get saved() { return tr("wiki.bot.bot-copy.BOT_COPY.saved.27"); },
  get resumed() { return tr("wiki.bot.bot-copy.BOT_COPY.resumed.28"); },
  /** `today`: the pause ends later today (paused between midnight and the daily check time). */
  pausedTomorrow: (hour: number, minute: number, today = false): string =>
    tr("wiki.bot.bot-copy.BOT_COPY.pausedTomorrow.29", { v0: today ? tr("wiki.bot.bot-copy.BOT_COPY.pausedTomorrow.30") : tr("wiki.bot.bot-copy.BOT_COPY.pausedTomorrow.31"), v1: hour, v2: String(minute).padStart(2, "0") }),
  get pausedAlways() { return tr("wiki.bot.bot-copy.BOT_COPY.pausedAlways.32"); },
  get goalNotOpened() { return tr("wiki.bot.bot-copy.BOT_COPY.goalNotOpened.33"); },
  get goalNoNext() { return tr("wiki.bot.bot-copy.BOT_COPY.goalNoNext.34"); },
  get parked() { return tr("wiki.bot.bot-copy.BOT_COPY.parked.35"); },
  get replanned() { return tr("wiki.bot.bot-copy.BOT_COPY.replanned.36"); },
  askNotSent: (q: string): string => tr("wiki.bot.bot-copy.BOT_COPY.askNotSent.37", { v0: q }),
  askLost: (q: string): string => tr("wiki.bot.bot-copy.BOT_COPY.askLost.38", { v0: q }),
} as const;

/** 参数 · Bot 最近做了什么 (app.js `botRuns` / `whyChain`). */
export const RUNS_COPY = {
  /** The pill on a write that was taken back (app.js `r.undone`). */
  get writeUndonePill() { return tr("wiki.bot.bot-copy.RUNS_COPY.writeUndonePill.39"); },
  /** The circled-? tooltip button's aria-label (copy guide §2). */
  get why() { return tr("wiki.bot.bot-copy.RUNS_COPY.why.40"); },
  get whyLoading() { return tr("wiki.bot.bot-copy.RUNS_COPY.whyLoading.41"); },
  get whyFailed() { return tr("wiki.bot.bot-copy.RUNS_COPY.whyFailed.42"); },
  get unknownReason() { return tr("wiki.bot.bot-copy.RUNS_COPY.unknownReason.43"); },
  get whyLabels() { return [tr("wiki.bot.bot-copy.RUNS_COPY.whyLabels.44"), tr("wiki.bot.bot-copy.RUNS_COPY.whyLabels.45"), tr("wiki.bot.bot-copy.RUNS_COPY.whyLabels.46"), tr("wiki.bot.bot-copy.RUNS_COPY.whyLabels.47"), tr("wiki.bot.bot-copy.RUNS_COPY.whyLabels.48")] as const; },
  rule: (zh: string, v: number, reason: string): string => tr("wiki.bot.bot-copy.RUNS_COPY.rule.49", { v0: zh, v1: v, v2: reason }),
  /** The tooltip's rule piece (no reason: that is its first line). */
  ruleOnly: (zh: string, v: number): string => tr("wiki.bot.bot-copy.RUNS_COPY.ruleOnly.50", { v0: zh, v1: v }),
  /** The tooltip's last line: when · what the learner did. */
  whenReact: (when: string, react: string): string => (react !== "" ? `${when} · ${react}` : when),
  /** An activity sentence for a page write (app.js botRuns w3 / w5 and the live run). */
  writeDone: (title: string): string => tr("wiki.bot.bot-copy.RUNS_COPY.writeDone.51", { v0: title }),
  writeRunning: (title: string): string => tr("wiki.bot.bot-copy.RUNS_COPY.writeRunning.52", { v0: title }),
  get gatePass() { return tr("wiki.bot.bot-copy.RUNS_COPY.gatePass.53"); },
  gateDefer: (r: string): string => tr("wiki.bot.bot-copy.RUNS_COPY.gateDefer.54", { v0: r }),
  get gateNone() { return tr("wiki.bot.bot-copy.RUNS_COPY.gateNone.55"); },
  gateSkip: (r: string): string => tr("wiki.bot.bot-copy.RUNS_COPY.gateSkip.56", { v0: r }),
  get openRemedial() { return tr("wiki.bot.bot-copy.RUNS_COPY.openRemedial.57"); },
  outChats: (n: number): string => tr("wiki.bot.bot-copy.RUNS_COPY.outChats.58", { v0: n }),
  outToday: (t: string): string => tr("wiki.bot.bot-copy.RUNS_COPY.outToday.59", { v0: t }),
  outChat: (t: string): string => tr("wiki.bot.bot-copy.RUNS_COPY.outChat.60", { v0: t }),
  outAction: (t: string): string => tr("wiki.bot.bot-copy.RUNS_COPY.outAction.61", { v0: t }),
  /** B5c: an RSI change the Bot's own cycle made (参数 · 最近改了什么). */
  outRsi: (t: string): string => tr("wiki.bot.bot-copy.RUNS_COPY.outRsi.62", { v0: t }),
  outSep: "、",
  get noOutput() { return tr("wiki.bot.bot-copy.RUNS_COPY.noOutput.63"); },
  /** A page write (app.js `botRuns` rows w5 / w3 and the live `pumpWrites` run): the reaction line, 撤销 and the 为什么？ pieces. */
  writeReact: (opened: boolean, undone: boolean): string => (undone ? tr("wiki.bot.bot-copy.RUNS_COPY.writeReact.64") : opened ? tr("wiki.bot.bot-copy.RUNS_COPY.writeReact.65") : tr("wiki.bot.bot-copy.RUNS_COPY.writeReact.66")),
  get undo() { return tr("wiki.bot.bot-copy.RUNS_COPY.undo.67"); },
  writeTrigIdle: (goal: string | null, n: number | null): string =>
    goal === null ? tr("wiki.bot.bot-copy.RUNS_COPY.writeTrigIdle.68") : n === null ? tr("wiki.bot.bot-copy.RUNS_COPY.writeTrigIdle.69", { v0: goal }) : tr("wiki.bot.bot-copy.RUNS_COPY.writeTrigIdle.70", { v0: goal, v1: n }),
  writeTrigUser: (asked: string | null, origin: string): string =>
    asked !== null && asked !== "" ? tr("wiki.bot.bot-copy.RUNS_COPY.writeTrigUser.71", { v0: asked }) : origin === "open" ? tr("wiki.bot.bot-copy.RUNS_COPY.writeTrigUser.72") : tr("wiki.bot.bot-copy.RUNS_COPY.writeTrigUser.73"),
  get writeGateIdle() { return tr("wiki.bot.bot-copy.RUNS_COPY.writeGateIdle.74"); },
  get writeGateUser() { return tr("wiki.bot.bot-copy.RUNS_COPY.writeGateUser.75"); },
  writePlan: (cost: number): string => tr("wiki.bot.bot-copy.RUNS_COPY.writePlan.76", { v0: cost.toFixed(3) }),
  writePlanRunning: (cost: number): string => tr("wiki.bot.bot-copy.RUNS_COPY.writePlanRunning.77", { v0: cost.toFixed(3) }),
  writeOut: (title: string, folder: string): string => (folder !== "" ? tr("wiki.bot.bot-copy.RUNS_COPY.writeOut.78", { v0: title, v1: folder }) : tr("wiki.bot.bot-copy.RUNS_COPY.writeOut.79", { v0: title })),
} as const;
export const RULE_ZH: Readonly<Record<string, string>> = {
  get review_due() { return tr("wiki.bot.bot-copy.RULE_ZH.review_due.80"); }, get wrong_answers() { return tr("wiki.bot.bot-copy.RULE_ZH.wrong_answers.81"); }, get goal_idle() { return tr("wiki.bot.bot-copy.RULE_ZH.goal_idle.82"); }, get open_question() { return tr("wiki.bot.bot-copy.RULE_ZH.open_question.83"); }, get chat() { return tr("wiki.bot.bot-copy.RULE_ZH.chat.84"); },
  get goal_review() { return tr("wiki.bot.bot-copy.RULE_ZH.goal_review.85"); }, get strategy_negative() { return tr("wiki.bot.bot-copy.RULE_ZH.strategy_negative.86"); },
};
export const GATE_REASON: Readonly<Record<string, string>> = {
  get paused() { return tr("wiki.bot.bot-copy.GATE_REASON.paused.87"); }, get kind_off() { return tr("wiki.bot.bot-copy.GATE_REASON.kind_off.88"); }, get cap_reached() { return tr("wiki.bot.bot-copy.GATE_REASON.cap_reached.89"); }, get cooldown() { return tr("wiki.bot.bot-copy.GATE_REASON.cooldown.90"); },
  get suppressed() { return tr("wiki.bot.bot-copy.GATE_REASON.suppressed.91"); }, get empty() { return tr("wiki.bot.bot-copy.GATE_REASON.empty.92"); }, get busy() { return tr("wiki.bot.bot-copy.GATE_REASON.busy.93"); },
};

/** 参数 · Bot 设置 (app.js `botSettings`). */
export const SETTINGS_COPY = {
  get title() { return tr("wiki.bot.bot-copy.SETTINGS_COPY.title.94"); },
  /** A save while the settings were never read (a notice; the section itself shows only 没能读到，重试). */
  get failed() { return tr("wiki.bot.bot-copy.SETTINGS_COPY.failed.95"); },
  get permSub() { return tr("wiki.bot.bot-copy.SETTINGS_COPY.permSub.96"); },
  get everSub() { return tr("wiki.bot.bot-copy.SETTINGS_COPY.everSub.97"); },
  get perDayPre() { return tr("wiki.bot.bot-copy.SETTINGS_COPY.perDayPre.98"); }, get perDayPost() { return tr("wiki.bot.bot-copy.SETTINGS_COPY.perDayPost.99"); },
  get wrongDays() { return tr("wiki.bot.bot-copy.SETTINGS_COPY.wrongDays.100"); }, get wrongPre() { return tr("wiki.bot.bot-copy.SETTINGS_COPY.wrongPre.101"); }, get wrongPost() { return tr("wiki.bot.bot-copy.SETTINGS_COPY.wrongPost.102"); },
  get idlePre() { return tr("wiki.bot.bot-copy.SETTINGS_COPY.idlePre.103"); }, get idleDays() { return tr("wiki.bot.bot-copy.SETTINGS_COPY.idleDays.104"); }, get idlePost() { return tr("wiki.bot.bot-copy.SETTINGS_COPY.idlePost.105"); },
  get perDayLabel() { return tr("wiki.bot.bot-copy.SETTINGS_COPY.perDayLabel.106"); }, get wrongDaysLabel() { return tr("wiki.bot.bot-copy.SETTINGS_COPY.wrongDaysLabel.107"); }, get wrongCountLabel() { return tr("wiki.bot.bot-copy.SETTINGS_COPY.wrongCountLabel.108"); }, get idleDaysLabel() { return tr("wiki.bot.bot-copy.SETTINGS_COPY.idleDaysLabel.109"); },
  get checkAt() { return tr("wiki.bot.bot-copy.SETTINGS_COPY.checkAt.110"); },
  get levelAsk() { return tr("wiki.bot.bot-copy.SETTINGS_COPY.levelAsk.111"); }, get levelAuto() { return tr("wiki.bot.bot-copy.SETTINGS_COPY.levelAuto.112"); },
  /** A refused number / time: the reason under its row (the box goes back to the saved value). */
  badNumber: (lo: number, hi: number): string => tr("wiki.bot.bot-copy.SETTINGS_COPY.badNumber.113", { v0: lo, v1: hi }),
  get badTime() { return tr("wiki.bot.bot-copy.SETTINGS_COPY.badTime.114"); },
  get levels() { return [tr("wiki.bot.bot-copy.SETTINGS_COPY.levels.115"), tr("wiki.bot.bot-copy.SETTINGS_COPY.levels.116"), tr("wiki.bot.bot-copy.SETTINGS_COPY.levels.117"), tr("wiki.bot.bot-copy.SETTINGS_COPY.levels.118"), tr("wiki.bot.bot-copy.SETTINGS_COPY.levels.119"), tr("wiki.bot.bot-copy.SETTINGS_COPY.levels.120")] as const; },
} as const;
