import { tr } from "./i18n/index";
/** The account check's words (account.ts): an unknown user id is registered automatically; a failed registration is a notice. */
export const COPY = {
  get offline() { return tr("copy.COPY.offline.1"); },
  dataUnreadable: (path: string) => tr("copy.COPY.dataUnreadable.2", { v0: path }),
  registerFailed: (id: string, detail: string) => tr("copy.COPY.registerFailed.3", { v0: id, v1: detail }),
} as const;

/** The plugin settings page: Chinese, plain speech, no research-stage wording. */
export const SETTINGS_COPY = {
  // The 设置 view / settings tab (app.js renderSettings), verbatim.
  get viewTitle() { return tr("copy.SETTINGS_COPY.viewTitle.4"); },
  get title() { return tr("copy.SETTINGS_COPY.title.5"); },
  get otherHeading() { return tr("copy.SETTINGS_COPY.otherHeading.6"); },
  get learnHeading() { return tr("copy.SETTINGS_COPY.learnHeading.7"); },
  get exemplarsHeading() { return tr("copy.SETTINGS_COPY.exemplarsHeading.8"); },
  get usageHeading() { return tr("copy.SETTINGS_COPY.usageHeading.9"); },
  get saved() { return tr("copy.SETTINGS_COPY.saved.10"); },
  get serverName() { return tr("copy.SETTINGS_COPY.serverName.11"); },
  get userName() { return tr("copy.SETTINGS_COPY.userName.12"); },
  get userIdInvalid() { return tr("copy.SETTINGS_COPY.userIdInvalid.13"); },
  get notesFolderName() { return tr("copy.SETTINGS_COPY.notesFolderName.14"); },
  get selectionName() { return tr("copy.SETTINGS_COPY.selectionName.15"); },
  get reviewPerDayName() { return tr("copy.SETTINGS_COPY.reviewPerDayName.16"); },
  get reviewPerDayWaiting() { return tr("copy.SETTINGS_COPY.reviewPerDayWaiting.17"); },
  get needsSettings() { return tr("copy.SETTINGS_COPY.needsSettings.18"); },
  get languageName() { return tr("copy.SETTINGS_COPY.languageName.19"); },
  get languageSaved() { return tr("copy.SETTINGS_COPY.languageSaved.20"); },
  get languageWaiting() { return tr("copy.SETTINGS_COPY.languageWaiting.21"); },
  get demoNoEdit() { return tr("copy.SETTINGS_COPY.demoNoEdit.22"); },
  get languageBusy() { return tr("copy.SETTINGS_COPY.languageBusy.23"); },
  get languageNotReady() { return tr("copy.SETTINGS_COPY.languageNotReady.24"); },
  // 用量 · 每页最多花（美元） (wiki settings page_limit_usd; null = no cap)
  get pageLimitName() { return tr("copy.SETTINGS_COPY.pageLimitName.25"); },
  get pageLimitInvalid() { return tr("copy.SETTINGS_COPY.pageLimitInvalid.26"); },
  get privacyName() { return tr("copy.SETTINGS_COPY.privacyName.27"); },
  get privacySummary() { return tr("copy.SETTINGS_COPY.privacySummary.28"); },
  get privacyDetailsTitle() { return tr("copy.SETTINGS_COPY.privacyDetailsTitle.29"); },
  get privacyDetails() { return [
    tr("copy.SETTINGS_COPY.privacyDetails.30"),
    tr("copy.SETTINGS_COPY.privacyDetails.31"),
    tr("copy.SETTINGS_COPY.privacyDetails.32"),
    tr("copy.SETTINGS_COPY.privacyDetails.33"),
    tr("copy.SETTINGS_COPY.privacyDetails.34"),
    tr("copy.SETTINGS_COPY.privacyDetails.35"),
    tr("copy.SETTINGS_COPY.privacyDetails.36"),
    tr("copy.SETTINGS_COPY.privacyDetails.37"),
    tr("copy.SETTINGS_COPY.privacyDetails.38"),
    tr("copy.SETTINGS_COPY.privacyDetails.39"),
  ]; },
  get legacySaveFailed() { return tr("copy.SETTINGS_COPY.legacySaveFailed.40"); },
} as const;

/** 样板页 in the plugin settings (src/wiki/teach/): the pages EvoWiki copies the style of. */
export const TEACH_COPY = {
  get loading() { return tr("copy.TEACH_COPY.loading.41"); },
  get retry() { return tr("copy.TEACH_COPY.retry.42"); },
  get conflict() { return tr("copy.TEACH_COPY.conflict.43"); },
  get conflictRereadFailed() { return tr("copy.TEACH_COPY.conflictRereadFailed.44"); },
  get busy() { return tr("copy.TEACH_COPY.busy.45"); },
  get notReady() { return tr("copy.TEACH_COPY.notReady.46"); },
  // 样板页 (exemplars, Task 7)
  get exemplarsEmpty() { return tr("copy.TEACH_COPY.exemplarsEmpty.47"); },
  get exemplarWikiPage() { return tr("copy.TEACH_COPY.exemplarWikiPage.48"); },
  exemplarVaultNote: (path: string) => tr("copy.TEACH_COPY.exemplarVaultNote.49", { v0: path }),
  get exemplarRemove() { return tr("copy.TEACH_COPY.exemplarRemove.50"); },
  get exemplarGone() { return tr("copy.TEACH_COPY.exemplarGone.51"); },
  get exemplarGoneRereadFailed() { return tr("copy.TEACH_COPY.exemplarGoneRereadFailed.52"); },
  get exemplarDeleteFailed() { return tr("copy.TEACH_COPY.exemplarDeleteFailed.53"); },
} as const;
