import { tr } from "../i18n/index";
/** The 个性化 tab's words (full-demo app.js `prefsBox` / `pendingBox` / `logBox` / `manageView` / `pageWaits`, verbatim). */
import type { Origin } from "./schema";

export const PERSONAL_COPY = {
  get tab() { return tr("wiki.personal-copy.PERSONAL_COPY.tab.1"); },
  get meTitle() { return tr("wiki.personal-copy.PERSONAL_COPY.meTitle.2"); },
  get factsTitle() { return tr("wiki.personal-copy.PERSONAL_COPY.factsTitle.3"); },
  get factWhy() { return tr("wiki.personal-copy.PERSONAL_COPY.factWhy.4"); },
  get factOk() { return tr("wiki.personal-copy.PERSONAL_COPY.factOk.5"); }, get factNo() { return tr("wiki.personal-copy.PERSONAL_COPY.factNo.6"); },
  get prefsTitle() { return tr("wiki.personal-copy.PERSONAL_COPY.prefsTitle.7"); },
  /** The two owners of a shown preference (app.js OWN). */
  get ownUser() { return tr("wiki.personal-copy.PERSONAL_COPY.ownUser.8"); }, get ownAi() { return tr("wiki.personal-copy.PERSONAL_COPY.ownAi.9"); },
  get prefsEmpty() { return tr("wiki.personal-copy.PERSONAL_COPY.prefsEmpty.10"); },
  count: (n: number): string => tr("wiki.personal-copy.PERSONAL_COPY.count.11", { v0: n }),
  get manage() { return tr("wiki.personal-copy.PERSONAL_COPY.manage.12"); },
  get prefWhy() { return tr("wiki.personal-copy.PERSONAL_COPY.prefWhy.13"); },
  get pendingTitle() { return tr("wiki.personal-copy.PERSONAL_COPY.pendingTitle.14"); },
  get pendingWhy() { return [tr("wiki.personal-copy.PERSONAL_COPY.pendingWhy.15"), tr("wiki.personal-copy.PERSONAL_COPY.pendingWhy.16")] as const; },
  get pendingWhyLabel() { return tr("wiki.personal-copy.PERSONAL_COPY.pendingWhyLabel.17"); },
  get pendingAria() { return tr("wiki.personal-copy.PERSONAL_COPY.pendingAria.18"); },
  get adopt() { return tr("wiki.personal-copy.PERSONAL_COPY.adopt.19"); }, get reject() { return tr("wiki.personal-copy.PERSONAL_COPY.reject.20"); },
  get adopted() { return tr("wiki.personal-copy.PERSONAL_COPY.adopted.21"); }, get rejected() { return tr("wiki.personal-copy.PERSONAL_COPY.rejected.22"); },
  get emptyPref() { return tr("wiki.personal-copy.PERSONAL_COPY.emptyPref.23"); },
  get logTitle() { return tr("wiki.personal-copy.PERSONAL_COPY.logTitle.24"); },
  get today() { return tr("wiki.personal-copy.PERSONAL_COPY.today.25"); }, get yesterday() { return tr("wiki.personal-copy.PERSONAL_COPY.yesterday.26"); }, get older() { return tr("wiki.personal-copy.PERSONAL_COPY.older.27"); },
  get changeWhy() { return tr("wiki.personal-copy.PERSONAL_COPY.changeWhy.28"); }, get runWhy() { return tr("wiki.personal-copy.PERSONAL_COPY.runWhy.29"); },
  get undone() { return tr("wiki.personal-copy.PERSONAL_COPY.undone.30"); }, get declined() { return tr("wiki.personal-copy.PERSONAL_COPY.declined.31"); },
  /** 管理偏好 */
  get back() { return tr("wiki.personal-copy.PERSONAL_COPY.back.32"); },
  get manageTitle() { return tr("wiki.personal-copy.PERSONAL_COPY.manageTitle.33"); },
  get edit() { return tr("wiki.personal-copy.PERSONAL_COPY.edit.34"); }, get del() { return tr("wiki.personal-copy.PERSONAL_COPY.del.35"); }, get save() { return tr("wiki.personal-copy.PERSONAL_COPY.save.36"); }, get cancel() { return tr("wiki.personal-copy.PERSONAL_COPY.cancel.37"); },
  get editAria() { return tr("wiki.personal-copy.PERSONAL_COPY.editAria.38"); },
  get saved() { return tr("wiki.personal-copy.PERSONAL_COPY.saved.39"); }, get deleted() { return tr("wiki.personal-copy.PERSONAL_COPY.deleted.40"); },
  get historyTitle() { return tr("wiki.personal-copy.PERSONAL_COPY.historyTitle.41"); },
  get howRejected() { return tr("wiki.personal-copy.PERSONAL_COPY.howRejected.42"); }, get howRevoked() { return tr("wiki.personal-copy.PERSONAL_COPY.howRevoked.43"); }, get howAuto() { return tr("wiki.personal-copy.PERSONAL_COPY.howAuto.44"); },
  get restore() { return tr("wiki.personal-copy.PERSONAL_COPY.restore.45"); }, get restored() { return tr("wiki.personal-copy.PERSONAL_COPY.restored.46"); },
  get sourcesTitle() { return tr("wiki.personal-copy.PERSONAL_COPY.sourcesTitle.47"); },
  /** A change waiting for a yes / no, on its page (app.js pageWaits). */
  get waitAria() { return tr("wiki.personal-copy.PERSONAL_COPY.waitAria.48"); }, get waitWhy() { return tr("wiki.personal-copy.PERSONAL_COPY.waitWhy.49"); },
  get waitDone() { return tr("wiki.personal-copy.PERSONAL_COPY.waitDone.50"); }, get waitCancelled() { return tr("wiki.personal-copy.PERSONAL_COPY.waitCancelled.51"); },
} as const;

/** The plain line of where a rule came from (its "?" when there is no evidence line, else the line after it). */
export function originLine(origin: Origin): string | null {
  switch (origin) {
    case "user_chat": return tr("wiki.personal-copy..52");
    case "user_ui": return tr("wiki.personal-copy..53");
    case "rsi_b": case "rsi_h": return tr("wiki.personal-copy..54");
    case "memory_backfill": return tr("wiki.personal-copy..55");
    default: return null;
  }
}
