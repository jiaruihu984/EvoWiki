import { type App, PluginSettingTab } from "obsidian";
import type EvoWikiPlugin from "./main";
import { renderSettingsInto, type SettingsHost } from "./settings-view";
import { el } from "./ui/ew";

export { bindNotesFolder } from "./settings-view";

/**
 * Obsidian's settings tab for EvoWiki: the same 设置 as the main-area view (settings-view.ts renderSettingsInto, app.js
 * renderSettings), inside a `.ew` wrapper so the demo's CSS applies in Obsidian's settings modal too.
 */
export class EvoWikiSettingTab extends PluginSettingTab {
  private handle: { dispose(): void } | null = null;
  constructor(app: App, private readonly plugin: EvoWikiPlugin & SettingsHost) { super(app, plugin); }
  override display(): void {
    this.handle?.dispose();
    const wrap = el("div");
    wrap.className = "ew";
    this.containerEl.replaceChildren(wrap);
    this.handle = renderSettingsInto(wrap, this.plugin);
  }
  /** Closing the tab drops whatever the server-backed parts still have in flight and stops them following the wiki. */
  override hide(): void {
    this.handle?.dispose();
    this.handle = null;
    super.hide();
  }
}
