/**
 * 设置 = app.js renderSettings, built once by renderSettingsInto for both places it shows: the main-area 设置 view
 * (SettingsView: `.ew` > `.ew-scroll` > `.ew-set`) and Obsidian's settings tab (settings-tab.ts: `.ew` > `.ew-set`).
 * h1 「EvoWiki 设置」, then notes folder, 学习 / 样板页 / 用量 / 其他 and the Bot, prompt and assessment sections.
 * Every row works on the real setting it names: identity and local settings through the plugin's updateSettings (identity
 * fields commit on change, never per keystroke; an invalid value is flagged aria-invalid and not saved), the wiki
 * settings through the wiki controller's hosts, 样板页 on a TeachModel, 用量 on the usage API. A new server or
 * user id draws the server-backed sections again for the new identity (nothing read for the old one stays).
 */
import { ItemView, type WorkspaceLeaf } from "obsidian";
import { SETTINGS_COPY } from "./copy";
import { MAINTENANCE_COPY, renderMaintenanceSection, type MaintenanceClient } from "./maintenance";
import { validateNotesFolder } from "./notes/note-format";
import { type EvoWikiSettings, isValidUserId, normalizeServerUrl } from "./settings";
import { el, type Label } from "./ui/ew";
import { ewNotice } from "./ui/ew-notice";
import { setStatus, settingRow, statusLine, switchButton, textInput } from "./ui/settings-rows";
import { type LanguageHost, mountLanguageSetting } from "./wiki/language-setting";
import { mountPageLimitSetting, type PageLimitHost } from "./wiki/page-limit-setting";
import { mountReviewLimitSetting, type ReviewLimitHost } from "./wiki/review-limit-setting";
import type { TeachPort } from "./wiki/teach/model";
import { mountExemplarSection } from "./wiki/teach/teach-section";
import type { UsageClient } from "./wiki/usage";
import { renderUsageSection } from "./wiki/usage-section";
import { type EngineSettingsHost, mountEngineSettings } from "./wiki/engine-settings";
import { setupText } from "./setup-copy";
import { btn } from "./ui/ew";

export const SETTINGS_VIEW_TYPE = "evowiki-settings";

/** What the settings read and write (EvoWikiPlugin satisfies it; tests: a fake). */
export interface SettingsHost {
  /** The settings in force now (read at call time, never captured). */
  currentSettings(): EvoWikiSettings;
  pickNotesFolder?(): Promise<string | null>;
  openInitialSetup?(): Promise<void>;
  /** Serialized; an identity change rebuilds the plugin's state (SettingsUpdater). */
  updateSettings(patch: Partial<EvoWikiSettings>): Promise<void>;
  languageHost(): LanguageHost;
  /** 用量 · 每页最多花（美元） (the wiki settings' page_limit_usd); absent: no such row. */
  pageLimitHost?(): PageLimitHost;
  reviewLimitHost(): ReviewLimitHost;
  teachHost(): TeachPort;
  usageClient(): UsageClient;
  maintenanceClient?(): MaintenanceClient | null;
  engineSettingsHost(): EngineSettingsHost;
  /** 用量 has something to ask (default: a server and user id are set; the demo fixture answers without them). */
  usageConfigured?(): boolean;
  /** Told after a new server / user id took effect (from either place the settings show); returns the unsubscribe. */
  onIdentityChange?(fn: () => void): () => void;
}

/** Identity fields apply on commit (blur/Enter), never per keystroke: every applied change rebuilds the plugin's state. */
function commitOnChange(input: HTMLInputElement, apply: (v: string) => Promise<void>): void {
  input.addEventListener("change", () => { void apply(input.value); });
}

/** An invalid value is flagged (aria-invalid) and not saved, so the previous setting stays in force. */
function flag(input: HTMLInputElement, ok: boolean): void {
  if (ok) input.removeAttribute("aria-invalid");
  else input.setAttribute("aria-invalid", "true");
}

/** E1: an invalid folder is flagged (aria-invalid) and not saved, so the previous setting stays in force. */
export function bindNotesFolder(input: HTMLInputElement, save: (folder: string) => Promise<void>): void {
  commitOnChange(input, async (v) => {
    const r = validateNotesFolder(v);
    flag(input, r.ok);
    if (!r.ok) return;
    input.value = r.folder;
    await save(r.folder);
  });
}

type Disposable = { dispose(): void };
const identity = (s: EvoWikiSettings): string => JSON.stringify([s.serverUrl, s.userId]);

/** A failed save of a local setting: said in the row's `.ew-muted` line and as a notice (fail closed, never silent). */
function failed(line: HTMLElement, e: unknown, text: string): void {
  console.warn("EvoWiki: could not save a setting", e);
  setStatus(line, text);
  ewNotice(text);
}

/** Builds 设置 into `parent` (appends the `.ew-set`); dispose() stops every section and drops what is in flight. */
export function renderSettingsInto(parent: HTMLElement, host: SettingsHost): Disposable {
  const s = el("div", "set");
  parent.append(s);
  s.append(el("h1", "", () => (SETTINGS_COPY.title)));
  if (host.openInitialSetup) s.append(btn(() => setupText("revise"), "", () => { void host.openInitialSetup?.().catch((error: unknown) => { ewNotice(error instanceof Error ? error.message : setupText("failed")); }); }));
  const sec = (title: Label | null): HTMLElement => { const x = el("section"); if (title !== null) x.append(el("h2", "", title)); return x; };
  let alive = true;

  // ---- 连接 (local; identity) ----
  const folderSection = s.appendChild(sec(null));
  const a = sec(() => SETTINGS_COPY.otherHeading);
  const cur = host.currentSettings();
  const url = textInput(cur.serverUrl, () => (SETTINGS_COPY.serverName));
  const urlRow = settingRow(a, () => (SETTINGS_COPY.serverName), null, url);
  const urlLine = urlRow.left.appendChild(statusLine());
  commitOnChange(url, async (v) => {
    const r = normalizeServerUrl(v);
    flag(url, r.ok);
    setStatus(urlLine, r.ok ? "" : r.reason);
    if (!r.ok) return;
    try { await host.updateSettings({ serverUrl: r.url }); } catch (e) { failed(urlLine, e, SETTINGS_COPY.legacySaveFailed); return; }
    if (alive) redrawIdentity();
  });
  const user = textInput(cur.userId, () => (SETTINGS_COPY.userName));
  const userRow = settingRow(a, () => (SETTINGS_COPY.userName), null, user);
  const userLine = userRow.left.appendChild(statusLine());
  commitOnChange(user, async (v) => {
    const ok = isValidUserId(v.trim());
    flag(user, ok);
    setStatus(userLine, ok ? "" : SETTINGS_COPY.userIdInvalid);
    if (!ok) return;
    try { await host.updateSettings({ userId: v.trim() }); } catch (e) { failed(userLine, e, SETTINGS_COPY.legacySaveFailed); return; }
    if (alive) redrawIdentity();
  });
  const folder = textInput(cur.notesFolder, () => (SETTINGS_COPY.notesFolderName));
  const folderControls = el("div", "row");
  folderControls.append(folder);
  const folderRow = settingRow(folderSection, () => (SETTINGS_COPY.notesFolderName), null, folderControls);
  const folderLine = folderRow.left.appendChild(statusLine());
  const saveFolder = async (notesFolder: string): Promise<void> => {
    try { await host.updateSettings({ notesFolder }); setStatus(folderLine, ""); } catch (e) { failed(folderLine, e, SETTINGS_COPY.legacySaveFailed); }
  };
  bindNotesFolder(folder, saveFolder);
  if (host.pickNotesFolder) {
    const choose = btn(() => setupText("chooseFolder"), "", () => {
      choose.disabled = true;
      void host.pickNotesFolder!().then(async (selected) => {
        if (!alive || selected === null) return;
        const result = validateNotesFolder(selected);
        flag(folder, result.ok);
        if (!result.ok) { setStatus(folderLine, setupText("invalidFolder")); return; }
        folder.value = result.folder;
        await saveFolder(result.folder);
      }).catch((error: unknown) => { if (alive) failed(folderLine, error, SETTINGS_COPY.legacySaveFailed); })
        .finally(() => { choose.disabled = false; });
    });
    choose.dataset.action = "choose-notes-folder";
    folderControls.append(choose);
  }

  // ---- the server-backed sections: drawn now and again for a new identity ----
  const parts: Disposable[] = [];
  const slots = {
    learn: s.appendChild(sec(() => SETTINGS_COPY.learnHeading)),
    exemplars: s.appendChild(sec(() => SETTINGS_COPY.exemplarsHeading)),
    usage: s.appendChild(sec(() => SETTINGS_COPY.usageHeading)),
    ...(host.maintenanceClient ? { maintenance: s.appendChild(sec(() => MAINTENANCE_COPY.heading)) } : {}),
  };
  s.append(a);
  let drawnFor = "";
  const drawServerParts = (): void => {
    for (const p of parts.splice(0)) p.dispose();
    drawnFor = identity(host.currentSettings());
    for (const [k, old] of Object.entries(slots) as [keyof typeof slots, HTMLElement][]) {
      const fresh = sec(() => k === "learn" ? SETTINGS_COPY.learnHeading : k === "exemplars" ? SETTINGS_COPY.exemplarsHeading : k === "usage" ? SETTINGS_COPY.usageHeading : MAINTENANCE_COPY.heading);
      old.replaceWith(fresh);
      slots[k] = fresh;
    }
    // 学习: 生成内容的语言, 每天最多复习几张 (the wiki controller), 选中文字问助手 (local).
    parts.push(mountLanguageSetting(slots.learn, host.languageHost()));
    parts.push(mountReviewLimitSetting(slots.learn, host.reviewLimitHost()));
    const askLine = statusLine();
    const ask = switchButton(host.currentSettings().allowSelectionContext, () => (SETTINGS_COPY.selectionName), (v) => {
      setStatus(askLine, "");
      host.updateSettings({ allowSelectionContext: v }).catch((e: unknown) => {
        ask.setAttribute("aria-checked", String(!v));
        failed(askLine, e, SETTINGS_COPY.legacySaveFailed);
      });
    });
    settingRow(slots.learn, () => (SETTINGS_COPY.selectionName), null, ask).left.append(askLine);
    parts.push(mountExemplarSection(slots.exemplars, host.teachHost()));
    const st = host.currentSettings();
    parts.push(renderUsageSection(slots.usage, {
      client: host.usageClient(),
      configured: () => host.usageConfigured?.() ?? (st.serverUrl !== "" && st.userId !== ""),
    }));
    if (host.pageLimitHost) parts.push(mountPageLimitSetting(slots.usage, host.pageLimitHost()));
    if (slots.maintenance) {
      const maintenance = host.maintenanceClient?.() ?? null;
      slots.maintenance.hidden = maintenance === null;
      if (maintenance) parts.push(renderMaintenanceSection(slots.maintenance, maintenance,
        () => st.serverUrl !== "" && st.userId !== ""));
    }
    parts.push(mountEngineSettings(s, host.engineSettingsHost()));
  };
  /** A committed server or user id: the server-backed sections again, for the new identity (once per identity). */
  const redrawIdentity = (): void => {
    if (!alive || identity(host.currentSettings()) === drawnFor) return;
    drawServerParts();
  };
  drawServerParts();

  const offIdentity = host.onIdentityChange?.(redrawIdentity) ?? null;
  return {
    dispose: () => {
      alive = false;
      offIdentity?.();
      for (const p of parts.splice(0)) p.dispose();
    },
  };
}

/** The main-area 设置 tab (app.js openTab("set")): `.ew` > `.ew-scroll` > `.ew-set`. */
export class SettingsView extends ItemView {
  private handle: Disposable | null = null;
  constructor(leaf: WorkspaceLeaf, private readonly host: SettingsHost) { super(leaf); }
  getViewType(): string { return SETTINGS_VIEW_TYPE; }
  getDisplayText(): string { return SETTINGS_COPY.viewTitle; }
  override getIcon(): string { return "settings"; }
  override onOpen(): Promise<void> {
    this.handle?.dispose();
    const root = el("div");
    root.className = "ew";
    // className set directly: the bare class token as a string literal reads as a banned event name (isolation.test.ts).
    const scroll = root.appendChild(el("div"));
    scroll.className = "ew-scroll";
    this.contentEl.replaceChildren(root);
    this.handle = renderSettingsInto(scroll, this.host);
    return Promise.resolve();
  }
  override onClose(): Promise<void> {
    this.handle?.dispose();
    this.handle = null;
    this.contentEl.replaceChildren();
    return Promise.resolve();
  }
}
