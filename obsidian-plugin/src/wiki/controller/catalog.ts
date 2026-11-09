/**
 * The catalog (P4: results in the catalog note): 刷新目录, and the settings written through PUT /wiki/settings (每天最多,
 * 生成内容的语言, 每页最多花; each PUT sends only the field it changes).
 */
import { SETTINGS_COPY } from "../../copy";
import { WIKI_COPY } from "../copy";
import { errorText } from "../format";
import { type Language, PAGE_LIMIT_USD, type SettingsIO, type SettingsPut } from "../schema";
import { type Target, MIN_REVIEW_PER_DAY, ok, bad } from "./helpers";
import type { WikiCore } from "./core";
import type { Reads } from "./reads";
import { setUiLanguage } from "../../i18n";

export class Catalog {
  private languageSeq = 0;
  constructor(private readonly core: WikiCore, private readonly reads: Reads) {}

  /** A window-focus read of account authority; independent of paid/background refresh gates. */
  async refreshLanguage(): Promise<void> {
    if (this.core.disposed || this.core.state.demo || !this.core.deps.configured()) return;
    const seq = ++this.languageSeq; const catalogSeq = this.reads.catalogSeq;
    const saved = await this.core.call(() => this.core.deps.api.getSettings());
    if (this.core.disposed || seq !== this.languageSeq || catalogSeq !== this.reads.catalogSeq) return;
    const current = this.core.state.catalog;
    if (current.kind === "ready" && current.value.settings.language !== saved.language)
      this.core.set({ catalog: { kind: "ready", value: { ...current.value, settings: saved } } });
    setUiLanguage(saved.language);
  }

  /**
   * 刷新目录: one GET catalog (a read; nothing while a write is in flight). Success clears the catalog note; a failure
   * keeps a loaded catalog and says the reload failed (reloadFailed: nothing was written, so not the refreshFailed text).
   */
  async reloadCatalog(): Promise<void> {
    if (this.core.disposed || this.core.state.busy) return;
    if (!this.core.deps.configured()) { this.core.setAreaNote("catalog", bad(WIKI_COPY.needsSettings)); return; }
    let failed = false;
    await this.reads.loadCatalog((e) => {
      failed = true;
      this.core.setAreaNote("catalog", bad(WIKI_COPY.reloadFailed(errorText(e))));
    });
    if (!failed && this.core.state.catalog.kind === "ready") this.core.set({ catalogNote: null });
  }

  /**
   * 每天最多: P9 (an integer from 0, no upper cap: the server has none; else nothing is sent); PUT settings; GET catalog, then
   * GET today's list (the limit changes it; what was uncovered / answered stays).
   */
  async setReviewPerDay(n: number): Promise<void> { await this.saveReviewPerDay(n); }

  /**
   * 生成内容的语言 (settings page): one PUT /wiki/settings {language} (the server keeps the other fields), then GET catalog. Resolves null when saved (or unchanged: nothing
   * sent), else the plain reason for the row (the settings row puts its dropdown back). Never the server's detail.
   */
  async setLanguage(language: Language): Promise<string | null> {
    const cat = this.core.state.catalog;
    if (cat.kind !== "ready") return SETTINGS_COPY.languageNotReady;
    if (this.core.disposed || this.core.state.demo) return SETTINGS_COPY.demoNoEdit;
    if (cat.value.settings.language === language) return null;
    try {
      const saved = await this.core.call(() => this.core.deps.api.putSettings({ language }));
      if (this.core.disposed) return null;
      const current = this.core.state.catalog;
      if (current.kind === "ready") this.core.set({ catalog: { kind: "ready", value: { ...current.value, settings: saved } } });
      this.reads.catalogSeq++;
      setUiLanguage(saved.language);
      return null;
    } catch (e) { return errorText(e); }
  }

  /**
   * 用量 · 每页最多花（美元）: one PUT /wiki/settings {page_limit_usd} (null = no cap; 0.05–5 else the plain
   * reason, nothing sent), then GET catalog. As setLanguage: null when saved (or unchanged), else the reason for the row.
   */
  async setPageLimit(limit: number | null): Promise<string | null> {
    if (limit !== null && !(Number.isFinite(limit) && limit >= PAGE_LIMIT_USD.min && limit <= PAGE_LIMIT_USD.max)) {
      return SETTINGS_COPY.pageLimitInvalid;
    }
    return this.putOneSetting((cur) => (cur.page_limit_usd === limit ? null : { page_limit_usd: limit }));
  }

  /** One settings field from the settings page: `change` null = unchanged (nothing sent). */
  private async putOneSetting(change: (cur: SettingsIO) => Partial<SettingsPut> | null): Promise<string | null> {
    if (this.core.disposed) return WIKI_COPY.failed;
    const cat = this.core.state.catalog;
    if (cat.kind !== "ready") return SETTINGS_COPY.languageNotReady;
    if (this.core.state.busy) return SETTINGS_COPY.languageBusy;
    const blocked = this.core.blocked();
    if (blocked) return blocked;
    const cur = cat.value.settings;
    const patch = change(cur);
    if (patch === null) return null;
    this.core.set({ busy: { what: "settings", abortable: false } });
    let saved: SettingsIO;
    try {
      saved = await this.core.call(() => this.core.deps.api.putSettings(patch));
    } catch (e) {
      if (!this.core.disposed) this.core.set({ busy: null });
      return errorText(e);
    }
    if (this.core.disposed) return null;
    const now = this.core.state.catalog;
    if (now.kind === "ready") this.core.set({ catalog: { kind: "ready", value: { ...now.value, settings: saved } } });
    // A failed re-read keeps the catalog (with the saved settings); the change itself landed.
    await this.reads.loadCatalog(() => undefined);
    this.core.set({ busy: null });
    return null;
  }

  /**
   * PUT settings {review_per_day} (loaded catalog only). P9: an integer from 0 (else the text goes to the 复习 note and
   * nothing is sent). The result and failures go to the 复习 note. Then GET catalog and today's list (its limit changed).
   */
  private async saveReviewPerDay(n: number): Promise<void> {
    if (this.core.state.catalog.kind !== "ready") return;
    const area = "review";
    const invalid = Number.isInteger(n) && n >= MIN_REVIEW_PER_DAY ? null : WIKI_COPY.reviewPerDayInvalid;
    if (!this.core.beginArea(area, { what: "settings", abortable: false }, invalid)) return;
    let saved: SettingsIO;
    try {
      saved = await this.core.call(() => this.core.deps.api.putSettings({ review_per_day: n }));
    } catch (e) { this.core.areaFailed(area, e); return; }
    if (this.core.disposed) return;
    const now = this.core.state.catalog;
    if (now.kind === "ready") this.core.set({ catalog: { kind: "ready", value: { ...now.value, settings: saved } } });
    this.core.setAreaNote(area, ok(WIKI_COPY.settingsSaved));
    const target: Target = { kind: "area", area };
    await this.reads.loadCatalog(this.core.refreshFail(target));
    await this.reads.loadReview(this.core.refreshFail(target));
    this.core.set({ busy: null });
  }
}
