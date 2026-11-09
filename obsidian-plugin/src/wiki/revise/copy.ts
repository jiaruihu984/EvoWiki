import { tr } from "../../i18n/index";
/** 用资料改这一页 copy, verbatim from the approved demo (full-demo app.js revBar / revActCard / revRow / reviseTop). */
export const REVISE_COPY = {
  get confirm() { return tr("wiki.revise.copy.REVISE_COPY.confirm.1"); },
  get cancel() { return tr("wiki.revise.copy.REVISE_COPY.cancel.2"); },
  get undo() { return tr("wiki.revise.copy.REVISE_COPY.undo.3"); },
  get rebuild() { return tr("wiki.revise.copy.REVISE_COPY.rebuild.4"); },
  get barAria() { return tr("wiki.revise.copy.REVISE_COPY.barAria.5"); },
  get barTip() { return tr("wiki.revise.copy.REVISE_COPY.barTip.6"); },
  get seeInPage() { return tr("wiki.revise.copy.REVISE_COPY.seeInPage.7"); },
  get undone() { return tr("wiki.revise.copy.REVISE_COPY.undone.8"); },
  /** 确认替换 / 撤销 went through but the note could not be brought to the page (copy guide §7: what failed, then 重试). */
  get noteFailed() { return tr("wiki.revise.copy.REVISE_COPY.noteFailed.9"); },
  get retry() { return tr("wiki.revise.copy.REVISE_COPY.retry.10"); },
  /** A draft that no longer fits its page (its section gone or renamed): written again for the page as it is now. */
  get redraft() { return tr("wiki.revise.copy.REVISE_COPY.redraft.11"); },
  /** After a 确认替换 / 取消 that did not go through (the page's bar and the chat's card; app.js draftBtns). */
  get retryDecision() { return tr("wiki.revise.copy.REVISE_COPY.retryDecision.12"); },
  /** The tag on a section heading the confirmed draft changed (app.js sectionEl `s.tag`). */
  get sectionTag() { return tr("wiki.revise.copy.REVISE_COPY.sectionTag.13"); },
  /** The chat's one-line card. */
  inPage: (n: number): string => tr("wiki.revise.copy.REVISE_COPY.inPage.14", { v0: n }),
  get writing() { return tr("wiki.revise.copy.REVISE_COPY.writing.15"); },
  live: (n: number): string => tr("wiki.revise.copy.REVISE_COPY.live.16", { v0: n }),
  get openPage() { return tr("wiki.revise.copy.REVISE_COPY.openPage.17"); },
  draft: (n: number): string => tr("wiki.revise.copy.REVISE_COPY.draft.18", { v0: n }),
  get newer() { return tr("wiki.revise.copy.REVISE_COPY.newer.19"); },
  get cancelled() { return tr("wiki.revise.copy.REVISE_COPY.cancelled.20"); },
  get undoneState() { return tr("wiki.revise.copy.REVISE_COPY.undoneState.21"); },
  get failed() { return tr("wiki.revise.copy.REVISE_COPY.failed.22"); },
  get delLabel() { return tr("wiki.revise.copy.REVISE_COPY.delLabel.23"); },
  get insLabel() { return tr("wiki.revise.copy.REVISE_COPY.insLabel.24"); },
  get noteLabel() { return tr("wiki.revise.copy.REVISE_COPY.noteLabel.25"); },
};
