import { tr, onLanguageChange } from "../i18n/index";
/** All Chinese UI copy of the reading assistant. */
import { TIMEOUT_TEXT } from "../api/busy";

/** An assistant turn's busy line (assistant/loop.py BUSY_SWITCH): the composer has a model picker. */
export let ASSISTANT_BUSY = tr("assistant.copy.ASSISTANT_BUSY.1");
onLanguageChange(() => { ASSISTANT_BUSY = tr("assistant.copy.ASSISTANT_BUSY.1"); });
/** How much of the selected words the quote chip and its bubble show. */
const QUOTE_CHARS = 40;
import { ApiError, OfflineError } from "../api/errors";
import { pageRanges } from "../wiki/sources/sources-copy";
import type { DraftAttachment, TurnAttachment } from "./attachments";
import type { TurnError } from "./turn";
import type { ErrorCode } from "./types";

export const ASSISTANT_COPY = {
  /** A citation in an answer that did not check out against the original. */
  get citeMissed() { return tr("assistant.copy.ASSISTANT_COPY.citeMissed.2"); },
  get citeEarlier() { return tr("assistant.copy.ASSISTANT_COPY.citeEarlier.3"); },
  /** A citation whose material is deleted or cleaned up by now (the page notes' words). */
  get citeDeleted() { return tr("assistant.copy.ASSISTANT_COPY.citeDeleted.4"); },
  /** The learner's message that starts a goal's diagnosis in the chat (GoalRun; stage 3 T11): his 确认 in 学点新的 (app.js diagStep). */
  get diagnoseStart() { return tr("assistant.copy.ASSISTANT_COPY.diagnoseStart.5"); },
  /** The first message of a conversation that takes a goal's diagnosis over (there is no card to confirm in it). */
  get diagnoseResume() { return tr("assistant.copy.ASSISTANT_COPY.diagnoseResume.6"); },
  /** A goal whose conversation cannot be found goes on in a new one. */
  get goalChatGone() { return tr("assistant.copy.ASSISTANT_COPY.goalChatGone.7"); },
  get viewTitle() { return tr("assistant.copy.ASSISTANT_COPY.viewTitle.8"); },
  get commandOpen() { return tr("assistant.copy.ASSISTANT_COPY.commandOpen.9"); },
  get menuAsk() { return tr("assistant.copy.ASSISTANT_COPY.menuAsk.10"); },
  get send() { return tr("assistant.copy.ASSISTANT_COPY.send.11"); },
  get newSession() { return tr("assistant.copy.ASSISTANT_COPY.newSession.12"); },
  get placeholder() { return tr("assistant.copy.ASSISTANT_COPY.placeholder.13"); },
  get placeholderNoPage() { return tr("assistant.copy.ASSISTANT_COPY.placeholderNoPage.14"); },
  /** The composer while the last turn's question (ask_user) is unanswered: what he sends answers it. */
  get askPlaceholder() { return tr("assistant.copy.ASSISTANT_COPY.askPlaceholder.15"); },
  /** The question sent when only a quote is attached (the placeholder's wording as a sentence). */
  get quoteOnlyQuestion() { return tr("assistant.copy.ASSISTANT_COPY.quoteOnlyQuestion.16"); },
  get removeQuote() { return tr("assistant.copy.ASSISTANT_COPY.removeQuote.17"); },
  /** Under an answer that broke off mid-stream (the text above it stays). */
  get interrupted() { return tr("assistant.copy.ASSISTANT_COPY.interrupted.18"); },
  /** Under an answer he stopped (the text above it stays). */
  get stopped() { return tr("assistant.copy.ASSISTANT_COPY.stopped.19"); },
  get stopFailed() { return tr("assistant.copy.ASSISTANT_COPY.stopFailed.20"); },
  /** The waiting line while the model works with no step of its own running (before the first status, between steps). */
  get thinking() { return tr("assistant.copy.ASSISTANT_COPY.thinking.21"); },
  // the list under an answer of what it read (asst.js srcl)
  get sourcesHead() { return tr("assistant.copy.ASSISTANT_COPY.sourcesHead.22"); },
  /** The selected words, cut; only without them (an old quote) where it came from. */
  quoteText: (text: string, h: string | null) => {
    const t = text.replace(/\s+/g, " ").trim();
    if (t === "") return h ? tr("assistant.copy.ASSISTANT_COPY.quoteText.23", { v0: h }) : tr("assistant.copy.ASSISTANT_COPY.quoteText.24");
    return tr("assistant.copy.ASSISTANT_COPY.quoteText.25", { v0: t.length > QUOTE_CHARS ? t.slice(0, QUOTE_CHARS) + "…" : t });
  },
  get needsSettings() { return tr("assistant.copy.ASSISTANT_COPY.needsSettings.26"); },
  get restoring() { return tr("assistant.copy.ASSISTANT_COPY.restoring.27"); },
  get restoreFailed() { return tr("assistant.copy.ASSISTANT_COPY.restoreFailed.28"); },
  get reload() { return tr("assistant.copy.ASSISTANT_COPY.reload.29"); },
  get restoreGone() { return tr("assistant.copy.ASSISTANT_COPY.restoreGone.30"); },
  get summary() { return tr("assistant.copy.ASSISTANT_COPY.summary.31"); },
  get recapUnderstood() { return tr("assistant.copy.ASSISTANT_COPY.recapUnderstood.32"); },
  get recapUnclear() { return tr("assistant.copy.ASSISTANT_COPY.recapUnclear.33"); },
  get recapEdits() { return tr("assistant.copy.ASSISTANT_COPY.recapEdits.34"); },
  get historyTitle() { return tr("assistant.copy.ASSISTANT_COPY.historyTitle.35"); },
  get newTitle() { return tr("assistant.copy.ASSISTANT_COPY.newTitle.36"); },
  get searchHistory() { return tr("assistant.copy.ASSISTANT_COPY.searchHistory.37"); },
  get historyEmpty() { return tr("assistant.copy.ASSISTANT_COPY.historyEmpty.38"); },
  get historyNoHits() { return tr("assistant.copy.ASSISTANT_COPY.historyNoHits.39"); },
  get historyLoading() { return tr("assistant.copy.ASSISTANT_COPY.historyLoading.40"); },
  get historyFailed() { return tr("assistant.copy.ASSISTANT_COPY.historyFailed.41"); },
  get resumeFailed() { return tr("assistant.copy.ASSISTANT_COPY.resumeFailed.42"); },
  get resumeBusy() { return tr("assistant.copy.ASSISTANT_COPY.resumeBusy.43"); },
  get resumeNoNote() { return tr("assistant.copy.ASSISTANT_COPY.resumeNoNote.44"); },
  /** The section label above a proposal card. */
  get proposalSection() { return tr("assistant.copy.ASSISTANT_COPY.proposalSection.45"); },
  /** The card title = what will be added / rewritten (the proposal's heading). */
  proposalAsk: (kind: "add_section" | "rewrite_section", heading: string) =>
    (kind === "add_section" ? tr("assistant.copy.ASSISTANT_COPY.proposalAsk.46", { v0: heading }) : tr("assistant.copy.ASSISTANT_COPY.proposalAsk.47", { v0: heading })),
  /**
   * 重试 on a proposal the page moved under (the section it rewrites was itself edited since): the same request asked again
   * in the chat, for the page as it is now, with what the draft was for (`why`).
   */
  proposalAgain: (kind: "add_section" | "rewrite_section", heading: string, why: string) =>
    tr("assistant.copy.ASSISTANT_COPY.proposalAgain.48", { v0: kind === "add_section" ? tr("assistant.copy.ASSISTANT_COPY.proposalAgain.49", { v0: heading }) : tr("assistant.copy.ASSISTANT_COPY.proposalAgain.50", { v0: heading }), v1: why }),
  /** A proposal the page moved under: nothing was written. */
  proposalStale: (kind: "add_section" | "rewrite_section") => (kind === "add_section" ? tr("assistant.copy.ASSISTANT_COPY.proposalStale.51") : tr("assistant.copy.ASSISTANT_COPY.proposalStale.52")),
  /** What he typed in the note could not reach the server: a card click changes nothing. */
  get proposalHandBlocked() { return tr("assistant.copy.ASSISTANT_COPY.proposalHandBlocked.53"); },
  /** The line that opens the old and new text of a proposal, and how much it removes and adds. */
  get proposalDiff() { return tr("assistant.copy.ASSISTANT_COPY.proposalDiff.54"); },
  proposalDiffSize: (removed: number, added: number) => [removed > 0 ? tr("assistant.copy.ASSISTANT_COPY.proposalDiffSize.55", { v0: removed }) : "", added > 0 ? tr("assistant.copy.ASSISTANT_COPY.proposalDiffSize.56", { v0: added }) : ""].filter((x) => x !== "").join(" · "),
  proposalHeading: (h: string) => tr("assistant.copy.ASSISTANT_COPY.proposalHeading.57", { v0: h }),
  accept: (kind: "add_section" | "rewrite_section") => (kind === "add_section" ? tr("assistant.copy.ASSISTANT_COPY.accept.58") : tr("assistant.copy.ASSISTANT_COPY.accept.59")),
  reject: (kind: "add_section" | "rewrite_section") => (kind === "add_section" ? tr("assistant.copy.ASSISTANT_COPY.reject.60") : tr("assistant.copy.ASSISTANT_COPY.reject.61")),
  decided: (kind: "add_section" | "rewrite_section") => ({
    get accepted() { return kind === "add_section" ? tr("assistant.copy.ASSISTANT_COPY.decided.accepted.62") : tr("assistant.copy.ASSISTANT_COPY.decided.accepted.63"); },
    get rejected() { return kind === "add_section" ? tr("assistant.copy.ASSISTANT_COPY.decided.rejected.64") : tr("assistant.copy.ASSISTANT_COPY.decided.rejected.65"); },
    get already_decided() { return tr("assistant.copy.ASSISTANT_COPY.decided.already_decided.66"); },
    get failed() { return tr("assistant.copy.ASSISTANT_COPY.decided.failed.67"); },
    get undone() { return tr("assistant.copy.ASSISTANT_COPY.decided.undone.68"); },
  }),
  get sending() { return tr("assistant.copy.ASSISTANT_COPY.sending.69"); },
  get quoteTooLong() { return tr("assistant.copy.ASSISTANT_COPY.quoteTooLong.70"); },
  /** File names of text too long for one message, sent as a text attachment instead. */
  get selectionFile() { return tr("assistant.copy.ASSISTANT_COPY.selectionFile.71"); },
  get pastedFile() { return tr("assistant.copy.ASSISTANT_COPY.pastedFile.72"); },
  tooLong: (n: number) => `${n}/4000`,
  get retry() { return tr("assistant.copy.ASSISTANT_COPY.retry.73"); },
  get stop() { return tr("assistant.copy.ASSISTANT_COPY.stop.74"); },
  /** More files than MessageIn.attachments allows: the extra ones are not added. */
  attachTooMany: (max: number) => tr("assistant.copy.ASSISTANT_COPY.attachTooMany.75", { v0: max }),
  /** The progress stream of an attachment ended or broke before it could be used (the upload itself went through). */
  get attachLost() { return tr("assistant.copy.ASSISTANT_COPY.attachLost.76"); },
  get attachTitle() { return tr("assistant.copy.ASSISTANT_COPY.attachTitle.77"); },
  /** The composer's model picker (asst.js modelPicker): the button's label, the ? of a model that cannot be picked. */
  modelAria: (label: string) => tr("assistant.copy.ASSISTANT_COPY.modelAria.78", { v0: label }),
  /** The button: the model and its thinking level. */
  modelLabel: (model: string, level: string) => `${model} · ${level}`,
  get modelOffLabel() { return tr("assistant.copy.ASSISTANT_COPY.modelOffLabel.79"); },
  get modelPickFailed() { return tr("assistant.copy.ASSISTANT_COPY.modelPickFailed.80"); },
  /** The words in an error line that open the picker (asst.js errLine). */
  get switchModel() { return tr("assistant.copy.ASSISTANT_COPY.switchModel.81"); },
  get attachRemove() { return tr("assistant.copy.ASSISTANT_COPY.attachRemove.82"); },
  attachRemoveNamed: (name: string) => tr("assistant.copy.ASSISTANT_COPY.attachRemoveNamed.83", { v0: name }),
  /** Beside the paperclip while an attachment is still being read (send is off). */
  get attachWait() { return tr("assistant.copy.ASSISTANT_COPY.attachWait.84"); },
  /** The overlay while files are dragged over the panel. */
  get dropHere() { return tr("assistant.copy.ASSISTANT_COPY.dropHere.85"); },
  /** A file dragged in from Obsidian's file explorer that could not be read from the vault. */
  get dropFromComputer() { return tr("assistant.copy.ASSISTANT_COPY.dropFromComputer.86"); },
  get attReading() { return tr("assistant.copy.ASSISTANT_COPY.attReading.87"); },
  get attReady() { return tr("assistant.copy.ASSISTANT_COPY.attReady.88"); },
  get attCleaned() { return tr("assistant.copy.ASSISTANT_COPY.attCleaned.89"); },
  attExpires: (days: number) => (days <= 0 ? tr("assistant.copy.ASSISTANT_COPY.attExpires.90") : tr("assistant.copy.ASSISTANT_COPY.attExpires.91", { v0: days })),
};

/**
 * Why an attachment did not upload, the server's reason when it is a Chinese
 * user-facing one (the sources routes' 413 / 415 / 422 / 503 details), else one plain line.
 */
export function attachErrorText(e: unknown): string {
  if (e instanceof ApiError) {
    const reason = e.detail.trim().replace(/。+$/, "");
    if (e.code !== null && reason !== "" && !/^[[{]/.test(reason)) return reason;
    return tr("assistant.copy..92");
  }
  if (e instanceof OfflineError) return tr("assistant.copy..93");
  return tr("assistant.copy..94");
}


const EVENT_ERRORS: Record<ErrorCode, string> = {
  // the server's detail names the limit and when it lifts; this only when it has none
  get budget_exceeded() { return tr("assistant.copy.EVENT_ERRORS.budget_exceeded.95"); },
  get tool_limit() { return tr("assistant.copy.EVENT_ERRORS.tool_limit.96"); },
  // the composer has a model picker: 切换模型 is something the user can do (render.ts makes it the picker's link)
  get llm_unavailable() { return tr("assistant.copy.EVENT_ERRORS.llm_unavailable.97"); },
  get llm_busy() { return ASSISTANT_BUSY; },
  get llm_timeout() { return TIMEOUT_TEXT; },
  get bad_request() { return tr("assistant.copy.EVENT_ERRORS.bad_request.98"); },
  // The server's detail is exactly this line (contracts.py ErrorEv); the UI shows its own copy of it.
  get needs_vision() { return tr("assistant.copy.EVENT_ERRORS.needs_vision.99"); },
  // the server's detail (why the tool the turn ran itself did nothing) is shown; this only when it has none
  get refused() { return tr("assistant.copy.EVENT_ERRORS.refused.100"); },
  get internal() { return tr("assistant.copy.EVENT_ERRORS.internal.101"); },
};

/** Copy per error. The ErrorEv.detail is for logs and is never shown, except llm_busy's (「请切换模型重试」, shown as this copy) and refused's and budget_exceeded's (shown as is). */
export function errorText(e: TurnError): string {
  switch (e.kind) {
    case "event": return (e.code === "refused" || e.code === "budget_exceeded") && e.detail !== undefined && e.detail.trim() !== "" ? e.detail : EVENT_ERRORS[e.code];
    case "offline": return tr("assistant.copy..102");
    case "aborted": return tr("assistant.copy..103");
    case "broken": return tr("assistant.copy..104");
    case "not_enabled": return tr("assistant.copy..105");
    case "unknown_page": return tr("assistant.copy..106");
    case "busy": return tr("assistant.copy..107");
    case "attachment": return e.detail;
    case "session_gone": return tr("assistant.copy..108");
    case "http": return tr("assistant.copy..109");
  }
}


/**
 * A `failed` 加入页面: the server's reason when it is a Chinese user-facing one (the applier's refusal), otherwise
 * (empty, or an unexpected English exception) the generic copy.
 */
export function decidedFailedText(detail: string): string {
  const reason = detail.trim().replace(/。+$/, "");
  return reason !== "" && !/^[[{]/.test(reason) && !/^(?:\w*(?:Error|Exception)|Traceback)\b/.test(reason) ? tr("assistant.copy..110", { v0: reason }) : ASSISTANT_COPY.decided("add_section").failed;
}

/** 删除对话 (full-demo asst.js forgetDialog, the user's mockup of 2026-10-28). */
export const FORGET_COPY = {
  get menu() { return tr("assistant.copy.FORGET_COPY.menu.111"); },
  get more() { return tr("assistant.copy.FORGET_COPY.more.112"); },
  get title() { return tr("assistant.copy.FORGET_COPY.title.113"); },
  get desc() { return tr("assistant.copy.FORGET_COPY.desc.114"); },
  get keep() { return tr("assistant.copy.FORGET_COPY.keep.115"); },
  get keepBold() { return tr("assistant.copy.FORGET_COPY.keepBold.116"); },
  get prefs() { return tr("assistant.copy.FORGET_COPY.prefs.117"); },
  get materials() { return tr("assistant.copy.FORGET_COPY.materials.118"); },
  get sectionSub() { return tr("assistant.copy.FORGET_COPY.sectionSub.119"); },
  get rule() { return tr("assistant.copy.FORGET_COPY.rule.120"); },
  get fact() { return tr("assistant.copy.FORGET_COPY.fact.121"); },
  get material() { return tr("assistant.copy.FORGET_COPY.material.122"); },
  usedElsewhere: (where: string) => tr("assistant.copy.FORGET_COPY.usedElsewhere.123", { v0: where }),
  get usedTipLabel() { return tr("assistant.copy.FORGET_COPY.usedTipLabel.124"); },
  get cancel() { return tr("assistant.copy.FORGET_COPY.cancel.125"); },
  get confirm() { return tr("assistant.copy.FORGET_COPY.confirm.126"); },
  get deleting() { return tr("assistant.copy.FORGET_COPY.deleting.127"); },
  get retrying() { return tr("assistant.copy.FORGET_COPY.retrying.128"); },
  get untitled() { return tr("assistant.copy.FORGET_COPY.untitled.129"); },
  get done() { return tr("assistant.copy.FORGET_COPY.done.130"); },
  get notYet() { return tr("assistant.copy.FORGET_COPY.notYet.131"); },
  get previewFailed() { return tr("assistant.copy.FORGET_COPY.previewFailed.132"); },
  get failed() { return tr("assistant.copy.FORGET_COPY.failed.133"); },
  get offline() { return tr("assistant.copy.FORGET_COPY.offline.134"); },
} as const;

/** Why the history list or a resume failed: one plain clause (no raw server text). */
export function historyReason(e: TurnError): string {
  switch (e.kind) {
    case "offline": return tr("assistant.copy..135");
    case "not_enabled": return errorText(e);
    case "http": return tr("assistant.copy..136");
    case "session_gone": return tr("assistant.copy..137");
    default: return tr("assistant.copy..138");
  }
}

/** Copy for a failed accept/reject click. Cards have no 重试 button: the user just clicks again. */
export function proposalErrorText(e: TurnError): string {
  switch (e.kind) {
    case "offline": return tr("assistant.copy..139");
    case "http":
      if (e.status === 409) return tr("assistant.copy..140");
      if (e.status === 404) return tr("assistant.copy..141");
      return tr("assistant.copy..142");
    case "broken": return tr("assistant.copy..143");
    case "aborted": return errorText(e);
    case "not_enabled": return tr("assistant.copy..144");
    default: return tr("assistant.copy..145");
  }
}

/** The reason on a failed card, without the closing 。 (the card writes 「失败 · 原因」). */
const plain = (reason: string): string => reason.trim().replace(/。+$/, "");

/** 「约 1.2 万字」 / 「约 8 千字」 / 「约 300 字」 ("" for none). */
export function charsText(chars: number): string {
  if (chars <= 0) return "";
  const wan = Math.round(chars / 1000) / 10;
  if (wan >= 1) return tr("assistant.copy..146", { v0: wan });
  const qian = Math.round(chars / 1000);
  return qian >= 1 ? tr("assistant.copy..147", { v0: qian }) : tr("assistant.copy..148", { v0: chars });
}

/** 「40 页 · 约 1.2 万字」: what is known about the file itself (an image has nothing to say). */
export function attFacts(a: { kind: string; pages: number | null; chars?: number | null }): string {
  if (a.kind === "image") return "";
  const parts = [a.pages !== null && a.pages > 0 ? tr("assistant.copy.parts.149", { v0: a.pages }) : "", a.chars ? charsText(a.chars) : ""];
  return parts.filter(Boolean).join(" · ");
}

export type AttTone = "" | "warn" | "err";
/** The state line of a card: 正在解析 / 可用 / 部分页读不出 · 第 a–b 页 / 失败 · 原因 (never how the file was read). */
export function draftStatus(d: Pick<DraftAttachment, "status" | "error"> & { unreadable?: number[] }): [string, AttTone] {
  switch (d.status) {
    case "uploading": case "reading": return [ASSISTANT_COPY.attReading, ""];
    case "ready": return [ASSISTANT_COPY.attReady, ""];
    case "partial": {
      const r = pageRanges(d.unreadable ?? []);
      return [r ? tr("assistant.copy..150", { v0: r }) : tr("assistant.copy..151"), "warn"];
    }
    case "failed": return [d.error ? tr("assistant.copy..152", { v0: plain(d.error) }) : tr("assistant.copy..153"), "err"];
  }
}

/** The state line of a card on a sent message. */
export function turnStatus(a: Pick<TurnAttachment, "status" | "deleted"> & { unreadable?: number[] }): [string, AttTone] {
  if (a.deleted) return [ASSISTANT_COPY.attCleaned, ""];
  switch (a.status) {
    case "reading": case "uploading": return [ASSISTANT_COPY.attReading, ""];
    case "partial": {
      const r = pageRanges(a.unreadable ?? []);
      return [r ? tr("assistant.copy..154", { v0: r }) : tr("assistant.copy..155"), "warn"];
    }
    case "failed": return [tr("assistant.copy..156"), "err"];
    default: return [ASSISTANT_COPY.attReady, ""];
  }
}

/** The action card (asst.js actCard / draftList). */
/** The quiet line of a preference the assistant recorded (action-card.ts `buildNoteLine`). */
export const NOTE_COPY = {
  get done() { return tr("assistant.copy.NOTE_COPY.done.157"); },
  get undone() { return tr("assistant.copy.NOTE_COPY.undone.158"); },
  get undo() { return tr("assistant.copy.NOTE_COPY.undo.159"); },
  get failed() { return tr("assistant.copy.NOTE_COPY.failed.160"); },
} as const;

export const ACTION_COPY = {
  get diffLabel() { return tr("assistant.copy.ACTION_COPY.diffLabel.161"); },
  /** `unit`: 页 for a page list, 块 for a goal's blocks (asst.js draftList `a.unit`). */
  rowLabel: (n: number, title: string, unit = tr("assistant.copy.ACTION_COPY.rowLabel.162")) => tr("assistant.copy.ACTION_COPY.rowLabel.163", { v0: n, v1: unit, v2: title }),
  drop: (unit = tr("assistant.copy.ACTION_COPY.drop.164")) => tr("assistant.copy.ACTION_COPY.drop.165", { v0: unit }),
  dropRow: (title: string) => tr("assistant.copy.ACTION_COPY.dropRow.166", { v0: title }),
  addPage: (unit = tr("assistant.copy.ACTION_COPY.addPage.167")) => (unit === tr("assistant.copy.ACTION_COPY.addPage.168") ? tr("assistant.copy.ACTION_COPY.addPage.169") : unit === tr("assistant.copy.ACTION_COPY.addPage.170") ? tr("assistant.copy.ACTION_COPY.addPage.171") : tr("assistant.copy.ACTION_COPY.addPage.172")),
  /** A goal's draft that a newer one replaced (app.js blocksReply `res`). */
  get replaced() { return tr("assistant.copy.ACTION_COPY.replaced.173"); },
  get addGo() { return tr("assistant.copy.ACTION_COPY.addGo.174"); },
  get undo() { return tr("assistant.copy.ACTION_COPY.undo.175"); },
  get details() { return tr("assistant.copy.ACTION_COPY.details.176"); },
  get chipBusy() { return tr("assistant.copy.ACTION_COPY.chipBusy.177"); },
  get chipDone() { return tr("assistant.copy.ACTION_COPY.chipDone.178"); },
  counter: (k: number, n: number) => tr("assistant.copy.ACTION_COPY.counter.179", { v0: k, v1: n }),
  /** The result line of a card that is not done (a failed row says why, when the ledger gave a Chinese reason). */
  result: (phase: "rejected" | "undone" | "failed" | "wait" | "live", detail: string): string => {
    if (phase === "rejected") return tr("assistant.copy.ACTION_COPY.result.180");
    if (phase === "undone") return tr("assistant.copy.ACTION_COPY.result.181");
    const reason = detail.trim().replace(/。+$/, "");
    return reason !== "" && !/^[[{]/.test(reason) && !/^(?:\w*(?:Error|Exception)|Traceback)\b/.test(reason) ? tr("assistant.copy.ACTION_COPY.result.182", { v0: reason }) : tr("assistant.copy.ACTION_COPY.result.183");
  },
  /** A failed 确认 / 取消 / 撤销 click: the server's Chinese reason, else the generic line; the button works again. */
  error: (e: unknown): string => {
    if (e instanceof OfflineError) return tr("assistant.copy.ACTION_COPY.error.184");
    if (e instanceof ApiError) {
      const reason = e.detail.trim().replace(/。+$/, "");
      if (reason !== "" && !/^[[{]/.test(reason) && !/^(?:\w*(?:Error|Exception)|Traceback)\b/.test(reason)) return tr("assistant.copy.ACTION_COPY.error.185", { v0: reason });
      return e.status === 404 ? tr("assistant.copy.ACTION_COPY.error.186") : tr("assistant.copy.ACTION_COPY.error.187");
    }
    return tr("assistant.copy.ACTION_COPY.error.188");
  },
};

/** The rating row under a finished answer (app.js rateRow): tooltips, the reason chips, the note, and the error toasts. */
export const RATING_COPY = {
  get copy() { return tr("assistant.copy.RATING_COPY.copy.189"); },
  get copied() { return tr("assistant.copy.RATING_COPY.copied.190"); },
  get rate() { return tr("assistant.copy.RATING_COPY.rate.191"); },
  get rateAsk() { return tr("assistant.copy.RATING_COPY.rateAsk.192"); },
  get notePlaceholder() { return tr("assistant.copy.RATING_COPY.notePlaceholder.193"); },
  get copyFailed() { return tr("assistant.copy.RATING_COPY.copyFailed.194"); },
  error: (e: unknown): string => {
    if (e instanceof OfflineError) return tr("assistant.copy.RATING_COPY.error.195");
    if (e instanceof ApiError && e.status === 404) return tr("assistant.copy.RATING_COPY.error.196");
    return tr("assistant.copy.RATING_COPY.error.197");
  },
};

/** The quiz card (asst.js quizCard). */
/** 「这个我早会了」's one confirm question (shown as a one-question quiz under the card). */
export const KNOWN_COPY = {
  get itemFailed() { return tr("assistant.copy.KNOWN_COPY.itemFailed.198"); },
};

export const QUIZ_COPY = {
  get right() { return tr("assistant.copy.QUIZ_COPY.right.199"); },
  counter: (i: number, n: number) => tr("assistant.copy.QUIZ_COPY.counter.200", { v0: i, v1: n }),
  get idk() { return tr("assistant.copy.QUIZ_COPY.idk.201"); },
  get next() { return tr("assistant.copy.QUIZ_COPY.next.202"); },
  get seeResult() { return tr("assistant.copy.QUIZ_COPY.seeResult.203"); },
  score: (ok: number, n: number) => tr("assistant.copy.QUIZ_COPY.score.204", { v0: ok, v1: n }),
  get rightTag() { return tr("assistant.copy.QUIZ_COPY.rightTag.205"); },
  get wrongTag() { return tr("assistant.copy.QUIZ_COPY.wrongTag.206"); },
  error: (e: unknown): string => {
    if (e instanceof OfflineError) return tr("assistant.copy.QUIZ_COPY.error.207");
    if (e instanceof ApiError && e.status === 409) return tr("assistant.copy.QUIZ_COPY.error.208");
    return tr("assistant.copy.QUIZ_COPY.error.209");
  },
};

/** The status of a page row of a done card (app.js paintWrites inside .propcard): no numbers. */
export const ROW_STATUS = { get queued() { return tr("assistant.copy.ROW_STATUS.queued.210"); }, get writing() { return tr("assistant.copy.ROW_STATUS.writing.211"); }, get done() { return tr("assistant.copy.ROW_STATUS.done.212"); }, get undone() { return tr("assistant.copy.ROW_STATUS.undone.213"); }, get failed() { return tr("assistant.copy.ROW_STATUS.failed.214"); }, get unwritten() { return tr("assistant.copy.ROW_STATUS.unwritten.215"); } } as const;

/** The words of a ledger card, from the action's kind and the 「」 names in its title (asst.js flows: ask / sub / yes / no / doing / did). */
export interface ActionWords { ask: string; sub: string; yes: string; no: string; doing: string; did: string }
const quoted = (title: string): string[] => [...title.matchAll(/「([^」]*)」/g)].map((m) => m[1]!);
/**
 * `title` is the backend's one-line summary (service/assistant tools actions.py), `n` the number of pages in the draft.
 * A kind with no mapping keeps the title for ask / doing / did and the plain 确认 / 取消.
 */
export function actionWords(kind: string, title: string, n: number): ActionWords {
  const q = quoted(title);
  const x = q[0] ?? "";
  const plain: ActionWords = { ask: title, sub: "", get yes() { return tr("assistant.copy.plain.yes.216"); }, get no() { return tr("assistant.copy.plain.no.217"); }, doing: title, did: title };
  switch (kind) {
    case "page_split":
      if (x === "") return plain;
      return { get ask() { return tr("assistant.copy.ask.218", { v0: x }); }, sub: "", get yes() { return tr("assistant.copy.yes.219"); }, get no() { return tr("assistant.copy.no.220"); },
        get doing() { return tr("assistant.copy.doing.221", { v0: x }); }, get did() { return tr("assistant.copy.did.222", { v0: x }); } };
    case "node_split":
      // the parts he named are listed (and edited) on the card; without parts (n = 0) a model decides after 确认, and its title says only 拆细
      if (x === "" || n === 0) return { ...plain, get yes() { return tr("assistant.copy.yes.223"); } };
      return { get ask() { return tr("assistant.copy.ask.224", { v0: x, v1: n }); }, sub: "", get yes() { return tr("assistant.copy.yes.225"); }, get no() { return tr("assistant.copy.no.226"); }, get doing() { return tr("assistant.copy.doing.227", { v0: x }); }, get did() { return tr("assistant.copy.did.228", { v0: x, v1: n }); } };
    case "path_edit":
      if (title.startsWith(tr("assistant.copy..229")) && x !== "") {
        return { get ask() { return tr("assistant.copy.ask.230", { v0: x }); }, sub: "", get yes() { return tr("assistant.copy.yes.231"); }, get no() { return tr("assistant.copy.no.232"); }, get doing() { return tr("assistant.copy.doing.233", { v0: x }); }, get did() { return tr("assistant.copy.did.234", { v0: x }); } };
      }
      return { get ask() { return title.startsWith(tr("assistant.copy.ask.235")) ? tr("assistant.copy.ask.236", { v0: title }) : title; }, sub: "", get yes() { return tr("assistant.copy.yes.237"); }, get no() { return tr("assistant.copy.no.238"); }, get doing() { return tr("assistant.copy.doing.239"); }, get did() { return tr("assistant.copy.did.240"); } };
    case "page_merge": {
      // "把 A、B 合成一页「C」" or "把「A」和「B」合成一页「C」".
      const m = /^把 ?(.+?) ?合成一页「(.*)」$/.exec(title);
      if (m === null) return { ...plain, get yes() { return tr("assistant.copy.yes.241"); }, get no() { return tr("assistant.copy.no.242"); } };
      const names = m[1]!.replace(/[「」]/g, "").split(/、|和/).filter((t) => t !== "").map((t) => `「${t}」`);
      const both = names.length > 1 ? tr("assistant.copy.both.243", { v0: names.slice(0, -1).join("、"), v1: names[names.length - 1] }) : names.join("");
      return { get ask() { return tr("assistant.copy.ask.244", { v0: both }); }, get sub() { return tr("assistant.copy.sub.245", { v0: m[2] }); }, get yes() { return tr("assistant.copy.yes.246"); }, get no() { return tr("assistant.copy.no.247"); },
        get doing() { return tr("assistant.copy.doing.248", { v0: both }); }, get did() { return tr("assistant.copy.did.249", { v0: m[2] }); } };
    }
    case "memory_conflict_resolve": {
      // memory/conflicts.py: 「保留「X」，撤下另外 N 条」 / 「改成「Y」，撤下原来的 N 条」 → app.js cfAnswer 已保留「X」 / 已改成「Y」
      const did = title.startsWith(tr("assistant.copy.did.250")) ? tr("assistant.copy.did.251", { v0: x }) : title.startsWith(tr("assistant.copy.did.252")) ? tr("assistant.copy.did.253", { v0: x }) : title;
      return { ...plain, doing: did, did };
    }
    case "rule":
      return { get ask() { return tr("assistant.copy.ask.254"); }, sub: title.replace(/^记一条讲解规则：/, ""), get yes() { return tr("assistant.copy.yes.255"); }, get no() { return tr("assistant.copy.no.256"); }, get doing() { return tr("assistant.copy.doing.257"); }, get did() { return tr("assistant.copy.did.258"); } };
    case "goal_new": {
      // app.js blocksAct: 「X」分成 N 块 follows the draft as it is edited; done: 已把「X」分成 N 块
      if (!/^「[^」]+」分成 \d+ 块$/.test(title) || x === "") return plain;
      return { get ask() { return tr("assistant.copy.ask.259", { v0: x, v1: n }); }, sub: "", get yes() { return tr("assistant.copy.yes.260"); }, get no() { return tr("assistant.copy.no.261"); }, get doing() { return tr("assistant.copy.doing.262", { v0: x, v1: n }); }, get did() { return tr("assistant.copy.did.263", { v0: x, v1: n }); } };
    }
    case "page_new":
      if (x === "") return { ...plain, get yes() { return tr("assistant.copy.yes.264"); }, get no() { return tr("assistant.copy.no.265"); } };
      return { get ask() { return tr("assistant.copy.ask.266", { v0: x }); }, sub: "", get yes() { return tr("assistant.copy.yes.267"); }, get no() { return tr("assistant.copy.no.268"); }, get doing() { return tr("assistant.copy.doing.269", { v0: x }); }, get did() { return tr("assistant.copy.did.270", { v0: x }); } };
    case "page_practice":
      // the draft is already in the page (review mode, whose bar says 确认替换 / 取消): the card says the same words
      if (x === "") return { ...plain, get yes() { return tr("assistant.copy.yes.271"); }, get no() { return tr("assistant.copy.no.272"); } };
      return { get ask() { return tr("assistant.copy.ask.273", { v0: x }); }, sub: title.replace(/^给「[^」]*」/, ""), get yes() { return tr("assistant.copy.yes.274"); }, get no() { return tr("assistant.copy.no.275"); }, get doing() { return tr("assistant.copy.doing.276", { v0: x }); }, get did() { return tr("assistant.copy.did.277", { v0: x }); } };
    default:
      return plain;
  }
}
