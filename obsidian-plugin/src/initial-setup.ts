import { readProfileFields, writeProfileFields, type ProfileFields } from "./wiki/profile-fields";
import { App, Modal } from "obsidian";
import { ApiError } from "./api/errors";
import type { AssistantClient } from "./assistant/client";
import type { ModelsOut } from "./generated/assistant";
import { localizedText, localizedAttribute, onLanguageChange, setUiLanguage, uiLanguage } from "./i18n";
import type { LocalSetupClient, LocalSetupResult } from "./local-setup";
import type { MaintenanceClient, BackupStatus } from "./maintenance";
import { setupText as text } from "./setup-copy";
import { btn, el } from "./ui/ew";
import { selectBox, settingRow, statusLine, switchButton, textInput, setStatus } from "./ui/settings-rows";
import type { EvoWikiSettings } from "./settings";
import type { ProfileClient, WikiApi } from "./wiki/api";
import type { BotSettings } from "./wiki/bot/bot-schema";
import { qTip } from "./wiki/q-tip";
import type { UsageClient } from "./wiki/usage";
import { renderUsageSection } from "./wiki/usage-section";
import type { SetupService, SetupSnapshot } from "./setup-service";
import type { UsageSettingsOut } from "./generated/service";
import { prepareBudget } from "./budget-setup";

const steps = ["language", "api", "background", "budgets", "automation", "summary"] as const;
export interface SetupDeps {
  api: WikiApi; usage: UsageClient; assistant: Pick<AssistantClient, "listModels" | "pickModel">;
  setup: SetupService;
  profile: ProfileClient; native: LocalSetupClient | null; maintenance: MaintenanceClient | null;
  settings(): EvoWikiSettings; updateSettings(patch: Partial<EvoWikiSettings>): Promise<void>;
  bookmark(): number; saveBookmark(step: number): Promise<void>;
  openBackground(): Promise<void>; finished(): Promise<void>;
}
export async function prepareInitialSetup(app: App, deps: SetupDeps, signal?: AbortSignal, reopen = false): Promise<void> {
  signal?.throwIfAborted();
  const settings = await deps.api.getSettings();
  signal?.throwIfAborted(); setUiLanguage(settings.language);
  return prepareBudget(deps.usage, (required) => new Promise((resolve, reject) => new InitialSetupModal(app, deps, required, signal, resolve, reject).open()), signal, reopen);
}

export class InitialSetupModal extends Modal {
  private step: number;
  private completed = false;
  private live = true;
  private busy = false;
  private localeWindow: Window | null = null;
  private readonly refreshLocale = (): void => { if (this.live) void this.deps.api.getSettings().then((settings) => { if (this.live) setUiLanguage(settings.language); }).catch(() => undefined); };
  private offLanguage: (() => void) | null = null;
  private disposeStep: (() => void) | null = null;
  private settleStep: (() => Promise<void>) | null = null;
  private readonly stage = el("div", "setup-stage");
  private readonly status = statusLine();
  private readonly footer = el("div", "row");
  private nativeStatus: LocalSetupResult | null = null;
  private models: ModelsOut | null = null;
  private bot: BotSettings | null = null;
  private backup: BackupStatus | null = null;
  private snapshot: SetupSnapshot | null = null;
  private budget: UsageSettingsOut | null = null;
  private originalBot: BotSettings | null = null;
  private profileText = "";
  private profileVersion: string | null = null;
  private profileOriginal = "";
  private profileFields: ProfileFields = { who: "", want: "", how: "" };
  private profileDirty = false;
  private choices = { rsi: false, confirm: true, proactive: false, idle: false, tool: false, patch: false };
  private originalChoices = { ...this.choices };
  private provider = "";
  private endpoint = "";
  private model = "";
  private key = "";
  private readonly discoveredModels = new Map<string, string[]>();
  private readonly attemptedCatalogs = new Set<string>();
  private catalogLoading = false;
  private readonly abort = (): void => { this.close(); };
  constructor(app: App, private readonly deps: SetupDeps, private readonly required: boolean, private readonly signal: AbortSignal | undefined,
    private readonly resolve: () => void, private readonly reject: (reason: unknown) => void) {
    super(app); this.step = Math.min(5, Math.max(0, deps.bookmark()));
  }
  override onOpen(): void {
    this.contentEl.classList.add("ew", "ew-setup"); this.contentEl.append(this.stage, this.status, this.footer);
    this.signal?.addEventListener("abort", this.abort, { once: true });
    this.offLanguage = onLanguageChange(() => {
      const language = this.stage.querySelector<HTMLSelectElement>("[data-field=language]"); if (language) language.value = uiLanguage();
      void this.refreshModelLabels();
    });
    localizedText(this.titleEl, () => text("title"));
    this.localeWindow = this.containerEl.ownerDocument.defaultView; this.localeWindow?.addEventListener("focus", this.refreshLocale);
    this.busy = true; this.draw();
    void this.load().then(() => { if (this.live) { this.busy = false; this.draw(); } }, (error: unknown) => {
      this.busy = false; this.step = 1; this.draw(); this.showError(error);
    });
  }
  private async load(): Promise<void> {
    const [bot, snapshot, profile, models, budget] = await Promise.all([this.deps.api.botSettings(), this.deps.setup.get(), this.deps.profile.get(), this.deps.assistant.listModels(), this.deps.usage.settings()]);
    this.bot = this.originalBot = bot; this.models = models; this.snapshot = snapshot; this.budget = budget;
    this.profileText = this.profileOriginal = profile.text; this.profileVersion = profile.version; this.profileFields = readProfileFields(profile.text);
    this.choices = this.required ? { rsi: false, confirm: true, proactive: false, idle: false, tool: false, patch: false }
      : { rsi: snapshot.rsi.on, confirm: bot.levels.rsi !== "auto_undo" || bot.levels.teaching_params !== "auto_undo", proactive: !snapshot.background.paused,
        idle: bot.kinds.idle_write === true, tool: snapshot.forge.tool, patch: snapshot.forge.patch };
    this.originalChoices = { ...this.choices };
    if (this.deps.native) {
      this.nativeStatus = await this.deps.native.request({ op: "status", language: uiLanguage() });
      if (!this.nativeStatus.ok) { this.step = 1; setStatus(this.status, this.helperError(this.nativeStatus)); }
    }
    if (this.deps.maintenance) this.backup = await this.deps.maintenance.status();
    this.provider = this.nativeStatus?.providers?.find((p) => p.id === this.nativeStatus?.selected_provider)?.id ?? this.nativeStatus?.providers?.find((p) => p.configured)?.id ?? this.nativeStatus?.providers?.[0]?.id ?? "";
    const current = models.models.find((model) => model.id === models.current);
    if (current && this.nativeStatus?.providers?.some((provider) => provider.id === current.provider)) this.provider = current.provider;
    this.fillProvider();
    if (current?.provider === this.provider) this.model = current.name;
  }
  private fillProvider(): void {
    const provider = this.nativeStatus?.providers?.find((p) => p.id === this.provider);
    this.endpoint = provider?.base_url ?? ""; this.model = provider?.model ?? ""; this.key = "";
  }
  private draw(): void {
    if (!this.live) return;
    this.disposeStep?.(); this.disposeStep = null; this.settleStep = null;
    this.stage.dataset.setupStep = steps[this.step]!;
    this.stage.replaceChildren(el("div", "setup-progress", `${this.step + 1} / ${steps.length}`), el("h2", "", () => text(steps[this.step]!)));
    if (this.busy) this.stage.append(el("div", "muted", () => text("checking")));
    else switch (this.step) {
      case 0: this.languageStep(); break; case 1: this.apiStep(); break; case 2: this.backgroundStep(); break;
      case 3: this.budgetStep(); break; case 4: this.automationStep(); break; case 5: this.summaryStep(); break;
    }
    const previous = btn(() => text("back"), "", () => { void this.move(-1); }); previous.disabled = this.busy || this.step === 0;
    previous.dataset.action = "setup-back";
    const next = btn(() => text(this.step === 5 ? "finish" : "next"), "primary", () => { void (this.step === 5 ? this.finish() : this.move(1)); }); next.disabled = this.busy;
    next.dataset.action = this.step === 5 ? "setup-finish" : "setup-next";
    this.footer.replaceChildren(previous, next);
  }
  private languageStep(): void {
    const language = selectBox([["zh", "简体中文"], ["en", "English"]], uiLanguage(), () => text("language"));
    language.dataset.field = "language";
    language.onchange = () => { void this.run(async () => {
      const saved = await this.deps.api.putSettings({ language: language.value === "zh" ? "zh" : "en" });
      if (!this.profileDirty) { const profile = await this.deps.profile.get(); this.profileText = this.profileOriginal = profile.text; this.profileVersion = profile.version; this.profileFields = readProfileFields(profile.text); }
      setUiLanguage(saved.language);
    }); };
    settingRow(this.stage, () => text("language"), null, language);
  }
  private apiStep(): void {
    if (!this.deps.native) this.stage.append(el("div", "muted", () => text("nativeUnavailable")));
    else {
      const providers = this.nativeStatus?.providers ?? [];
      const select = selectBox(providers.map((p) => [p.id, p.id] as const), this.provider, () => text("provider"));
      select.dataset.field = "provider";
      select.onchange = () => { this.provider = select.value; this.fillProvider(); this.draw(); };
      settingRow(this.stage, () => text("provider"), null, select);
      const info = el("div", "row", () => providers.find((p) => p.id === this.provider)?.configured ? text("configured") : text("missing"));
      info.append(qTip(() => text("apiHelp"), () => text("api"))); this.stage.append(info);
      const endpoint = textInput(this.endpoint, () => text("endpoint")); endpoint.oninput = () => { this.endpoint = endpoint.value; };
      endpoint.dataset.field = "base-url";
      settingRow(this.stage, () => text("endpoint"), null, endpoint);
      const key = textInput(this.key, () => text("key")); key.type = "password"; key.autocomplete = "off";
      key.dataset.field = "api-key";
      localizedAttribute(key, "placeholder", () => text("keyKeep")); key.oninput = () => { this.key = key.value; };
      settingRow(this.stage, () => text("key"), null, key);
      const names = this.providerModels();
      const model = selectBox(names.map((name) => [name, name] as const), this.model, () => text("model"));
      model.onchange = () => { this.model = model.value; };
      model.dataset.field = "local-model";
      settingRow(this.stage, () => text("model"), null, model);
      this.reasoningSelection(this.stage);
      const refresh = btn(() => uiLanguage() === "zh" ? "获取模型" : "Get models", "", () => { void this.refreshProviderModels(); });
      const check = btn(() => text("check"), "", () => { void this.configure(false); });
      const save = btn(() => text("save"), "", () => { void this.configure(true); });
      for (const button of [refresh, check, save]) { button.dataset.nativeAction = "true"; button.disabled = this.catalogLoading; }
      this.stage.append(refresh, check, save);
      this.advancedSettings();
      const fingerprint = this.catalogFingerprint();
      if (providers.find((provider) => provider.id === this.provider)?.configured && !this.attemptedCatalogs.has(fingerprint)) void this.refreshProviderModels();
    }
    if (!this.deps.native) this.modelSelection(this.stage);
  }
  private providerModels(): string[] {
    return [...new Set([
      ...(this.discoveredModels.get(`${this.provider}\n${this.endpoint}`) ?? []),
      ...(this.nativeStatus?.providers?.find((provider) => provider.id === this.provider)?.models ?? []),
      ...(this.models?.models.filter((model) => model.provider === this.provider).map((model) => model.name) ?? []),
      this.model,
    ].filter(Boolean))];
  }
  private catalogFingerprint(): string { return JSON.stringify([this.provider, this.endpoint, this.key]); }
  private async refreshProviderModels(): Promise<void> {
    if (!this.deps.native || this.catalogLoading || this.busy) return;
    const provider = this.provider; const endpoint = this.endpoint; const fingerprint = this.catalogFingerprint();
    this.catalogLoading = true; this.attemptedCatalogs.add(fingerprint);
    setStatus(this.status, text("checking"));
    for (const button of this.stage.querySelectorAll<HTMLButtonElement>("[data-native-action]")) button.disabled = true;
    try {
      const result = await this.deps.native.request({ op: "models", language: uiLanguage(), provider, base_url: endpoint, api_key: this.key });
      if (!this.live || fingerprint !== this.catalogFingerprint()) return;
      if (!result.ok || result.model_catalog?.provider !== provider) throw new Error("model_catalog_failed");
      this.discoveredModels.set(`${provider}\n${endpoint}`, result.model_catalog.models);
      const select = this.stage.querySelector<HTMLSelectElement>('[data-field="local-model"]');
      if (select) { select.replaceChildren(...this.providerModels().map((name) => new Option(name, name))); select.value = this.model; }
      setStatus(this.status, "");
    } catch {
      if (this.live && fingerprint === this.catalogFingerprint()) setStatus(this.status, uiLanguage() === "zh" ? "无法读取该厂商的模型列表，请检查接口地址和密钥后重试。" : "Could not load this provider's models. Check the API endpoint and key, then retry.");
    } finally {
      this.catalogLoading = false;
      if (this.live) {
        for (const button of this.stage.querySelectorAll<HTMLButtonElement>("[data-native-action]")) button.disabled = false;
        if (fingerprint !== this.catalogFingerprint() && this.nativeStatus?.providers?.some((item) => item.id === this.provider && item.configured) && !this.attemptedCatalogs.has(this.catalogFingerprint())) void this.refreshProviderModels();
      }
    }
  }
  private advancedSettings(): void {
    const details = el("details"); details.append(el("summary", "", () => text("advanced")));
    let forge = this.nativeStatus?.local_options?.forge_enabled ?? false;
    settingRow(details, () => text("globalForge"), null, switchButton(forge, () => text("globalForge"), (on) => { forge = on; }));
    const directory = textInput(this.nativeStatus?.local_options?.backup_directory ?? this.snapshot?.backup.directory ?? "", () => text("backupDirectory"));
    const keep = textInput(String(this.nativeStatus?.local_options?.backup_keep ?? this.snapshot?.backup.keep ?? 14), () => text("retain")); keep.inputMode = "numeric";
    settingRow(details, () => text("backupDirectory"), null, directory); settingRow(details, () => text("retain"), null, keep);
    const save = btn(() => text("saveLocal"), "", () => { void this.run(async () => {
      if (!this.deps.native) throw new Error(text("nativeUnavailable"));
      const request = { language: uiLanguage(), forge_enabled: forge, backup_directory: directory.value, backup_keep: Number(keep.value), expected_revision: this.nativeStatus?.revision };
      const checked = await this.deps.native.request({ ...request, op: "check" }); if (!checked.ok) throw new Error(this.helperError(checked));
      const saved = await this.deps.native.request({ ...request, op: "apply", expected_revision: checked.revision }); if (!saved.ok) throw new Error(this.helperError(saved));
      this.nativeStatus = saved; this.snapshot = await this.deps.setup.get(); this.models = await this.deps.assistant.listModels();
    }); });
    save.dataset.nativeAction = "true"; save.disabled = this.catalogLoading; details.append(save);
    this.stage.append(details);
  }
  private modelSelection(target: HTMLElement): void {
    const models = this.models; if (!models) return;
    const select = selectBox(models.models.map((m) => [m.id, `${m.provider_name} · ${m.name}`] as const), models.current, () => text("model"));
    select.dataset.setupModel = "model";
    for (const option of select.options) option.disabled = !models.models.find((m) => m.id === option.value)?.available;
    select.onchange = () => { void this.run(async () => { this.models = await this.deps.assistant.pickModel({ model: select.value }); }); };
    settingRow(target, () => text("model"), null, select);
    this.reasoningSelection(target);
  }
  private reasoningSelection(target: HTMLElement): void {
    const models = this.models; if (!models) return;
    const selected = models.models.find((m) => m.id === models.current);
    if (selected?.levels.length) {
      const level = selectBox(selected.levels.map((l) => [l.reasoning, l.name] as const), models.reasoning, () => text("reasoning"));
      level.dataset.setupModel = "reasoning";
      level.onchange = () => { void this.run(async () => { this.models = await this.deps.assistant.pickModel({ reasoning: level.value }); }); };
      settingRow(target, () => text("reasoning"), null, level);
    }
  }
  private async refreshModelLabels(): Promise<void> {
    const locale = uiLanguage();
    try {
      const models = await this.deps.assistant.listModels();
      if (!this.live || uiLanguage() !== locale) return;
      this.models = models;
      const select = this.stage.querySelector<HTMLSelectElement>('[data-setup-model="model"]');
      if (select) for (const option of select.options) {
        const model = models.models.find((item) => item.id === option.value);
        if (model) option.textContent = `${model.provider_name} · ${model.name}`;
      }
      const reasoning = this.stage.querySelector<HTMLSelectElement>('[data-setup-model="reasoning"]');
      const current = models.models.find((item) => item.id === (select?.value ?? models.current));
      if (reasoning) for (const option of reasoning.options) {
        const level = current?.levels.find((item) => item.reasoning === option.value);
        if (level) option.textContent = level.name;
      }
    } catch { /* A display-only refresh keeps the current controls and draft when offline. */ }
  }
  private async configure(apply: boolean): Promise<void> {
    await this.run(async () => {
      if (!this.deps.native) throw new Error(text("nativeUnavailable"));
      const body = { language: uiLanguage(), provider: this.provider, api_key: this.key, base_url: this.endpoint, model: this.model, expected_revision: this.nativeStatus?.revision };
      const checked = await this.deps.native.request({ ...body, op: "check" });
      if (!checked.ok) { await this.recoverRevision(checked); throw new Error(this.helperError(checked)); }
      if (apply) {
        const saved = await this.deps.native.request({ ...body, op: "apply", expected_revision: checked.revision }); this.key = "";
        if (!saved.ok) { await this.recoverRevision(saved); throw new Error(this.helperError(saved)); }
        this.nativeStatus = saved;
        const provider = saved.providers?.find((provider) => provider.id === this.provider);
        [this.models, this.snapshot] = await Promise.all([
          provider?.configured ? this.deps.assistant.pickModel({ model: `${provider.id}/${provider.model}` }) : this.deps.assistant.listModels(),
          this.deps.setup.get(),
        ]);
        this.revalidateChoices(); this.fillProvider();
      }
      setStatus(this.status, text("configured"));
    });
  }
  private async recoverRevision(result: LocalSetupResult): Promise<void> {
    if (this.deps.native && /revision|conflict/.test(result.code)) this.nativeStatus = await this.deps.native.request({ op: "status", language: uiLanguage() });
  }
  private helperError(result: LocalSetupResult): string {
    if (/revision|conflict/.test(result.code)) return text("conflict");
    if (result.code === "invalid_env") return text("invalidConfiguration");
    if (result.code === "interrupted_write") return text("interruptedConfiguration");
    const field = result.errors[0]?.field ?? "";
    if (field.includes("price")) return text("invalidPrice"); if (field.includes("key")) return text("invalidKey");
    if (field.includes("url")) return text("invalidEndpoint"); if (field.includes("model")) return text("invalidModel");
    return text(result.restart?.state === "failed" ? "restartFailed" : "failed");
  }
  private backgroundStep(): void {
    this.stage.append(qTip(() => text("backgroundHelp"), () => text("background")));
    for (const [field, label] of [["who", "priorKnowledge"], ["want", "learningGoal"], ["how", "teachingStyle"]] as const) {
      const note = el("textarea", "setup-background"); note.value = this.profileFields[field];
      note.dataset.field = `background-${field}`; localizedAttribute(note, "aria-label", () => text(label));
      note.oninput = () => { this.profileFields[field] = note.value; this.profileDirty = true; };
      settingRow(this.stage, () => text(label), null, note);
    }
  }
  private budgetStep(): void {
    const usage = renderUsageSection(this.stage, { client: this.deps.usage, configured: () => true, deferConfirmation: true });
    this.disposeStep = () => usage.dispose(); this.settleStep = () => usage.settled();
    if (this.bot) {
      const timezone = textInput(this.bot.timezone, () => text("timezone"));
      timezone.onchange = () => { if (this.bot) this.bot = { ...this.bot, timezone: timezone.value }; };
      settingRow(this.stage, () => text("timezone"), null, timezone);
      const time = textInput(`${String(this.bot.daily_hour).padStart(2, "0")}:${String(this.bot.daily_minute).padStart(2, "0")}`, () => text("time")); time.type = "time";
      time.onchange = () => { const [hour, minute] = time.value.split(":").map(Number); if (this.bot && Number.isInteger(hour) && Number.isInteger(minute)) this.bot = { ...this.bot, daily_hour: hour!, daily_minute: minute! }; };
      settingRow(this.stage, () => text("time"), null, time);
    }
  }
  private automationStep(): void {
    for (const key of ["rsi", "confirm", "proactive", "idle", "tool", "patch"] as const) {
      const control = switchButton(this.choices[key], () => text(key), (value) => {
        this.choices[key] = value; if (key === "proactive" && !value) this.choices.idle = false; this.draw();
      });
      control.dataset.field = key;
      control.disabled = key === "rsi" && !this.snapshot?.rsi.available && !this.choices.rsi || (key === "tool" || key === "patch") && this.forgeBlocked(key) || key === "idle" && !this.choices.proactive;
      const row = settingRow(this.stage, () => text(key), null, control);
      if (key === "rsi" && !this.snapshot?.rsi.available) row.left.append(qTip(() => (this.snapshot?.rsi.blockers ?? []).map((code) => this.blocker(code)), () => text("rsi")));
      if ((key === "tool" || key === "patch") && this.forgeBlocked(key)) row.left.append(qTip(() => text(key === "tool" ? "toolDisabled" : "patchDisabled"), () => text(key)));
      if (key === "proactive") row.left.append(qTip(() => text("backgroundWorkHelp"), () => text(key)));
    }
  }
  private forgeBlocked(level: "tool" | "patch"): boolean {
    return !this.snapshot?.forge.global_allowed || this.snapshot.forge.blockers.includes(`${level}_globally_disabled`);
  }
  private revalidateChoices(): void {
    if (!this.snapshot?.rsi.available) this.choices.rsi = false;
    for (const level of ["tool", "patch"] as const) if (this.forgeBlocked(level)) this.choices[level] = false;
  }
  private summaryStep(): void {
    const budget = this.budget; const snapshot = this.snapshot; const backupStatus = this.backup;
    const model = this.models?.models.find((m) => m.id === this.models?.current);
    this.stage.append(el("div", "", () => `${text("model")}: ${model?.name ?? text("missing")}`));
    if (budget) this.stage.append(el("div", "", () => `${text("daily")}: ${budget.daily_limit_usd === null ? text("unlimited") : `$${budget.daily_limit_usd}`}`),
      el("div", "", () => `${text("monthly")}: ${budget.monthly_limit_usd === null ? text("unlimited") : `$${budget.monthly_limit_usd}`}`));
    for (const key of ["rsi", "confirm", "proactive", "idle", "tool", "patch"] as const) this.stage.append(el("div", "", () => `${text(key)}: ${text(this.choices[key] ? "enabled" : "disabled")}`));
    if (snapshot) {
      this.stage.append(el("div", "", () => `${text("budgetZone")}: ${snapshot.usage_timezone}`));
      const backup = snapshot.backup;
      this.stage.append(el("div", "", () => `${text("backup")}: ${text(backup.enabled ? "enabled" : "disabled")}`));
      if (backup.directory) this.stage.append(el("code", "setup-path", backup.directory), el("div", "", () => `${text("retain")}: ${backup.keep}`));
    }
    if (backupStatus) {
      this.stage.append(el("div", "", () => `${text("backup")}: ${text(backupStatus.disabled ? "disabled" : "enabled")}`), qTip(() => text("backupScope"), () => text("backup")));
      for (const path of backupStatus.files) this.stage.append(el("code", "setup-path", path));
    }
    this.stage.append(btn(() => text("openBackground"), "", () => { void this.deps.openBackground().catch((error: unknown) => this.showError(error)); }));
  }
  private async saveBackground(): Promise<void> {
    if (!this.profileDirty) return;
    this.profileText = writeProfileFields(this.profileOriginal, this.profileFields, uiLanguage());
    const out = await this.deps.profile.put(this.profileText, this.profileVersion);
    this.profileText = this.profileOriginal = out.text; this.profileVersion = out.version; this.profileFields = readProfileFields(out.text); this.profileDirty = false;
  }
  private async move(delta: number): Promise<void> {
    await this.run(async () => {
      await this.settleStep?.(); if (delta > 0 && this.step === 1) await this.requireModel();
      if (this.step === 2) await this.saveBackground(); this.key = "";
      this.step = Math.max(0, Math.min(5, this.step + delta)); await this.deps.saveBookmark(this.step);
      if (this.step === 5) { this.budget = await this.deps.usage.settings(); this.snapshot = await this.deps.setup.get(); this.revalidateChoices(); }
    });
  }
  private async requireModel(): Promise<void> {
    this.models = await this.deps.assistant.listModels();
    if (!this.models.models.some((m) => m.available && m.id === this.models?.current)) throw new Error(text("apiRequired"));
  }
  private async finish(): Promise<void> {
    await this.run(async () => {
      await this.requireModel(); this.snapshot = await this.deps.setup.get(); this.revalidateChoices(); await this.saveBackground(); if (!this.bot) throw new Error(text("failed"));
      const current = await this.deps.api.botSettings();
      const bot = { ...current, kinds: { ...current.kinds }, levels: { ...current.levels } };
      for (const field of ["timezone", "daily_hour", "daily_minute"] as const) if (this.required || this.bot[field] !== this.originalBot?.[field]) Object.assign(bot, { [field]: this.bot[field] });
      if (this.required || this.choices.proactive !== this.originalChoices.proactive) { bot.paused = this.choices.proactive ? "no" : "always"; bot.paused_until = null; }
      if (this.required || this.choices.idle !== this.originalChoices.idle) bot.kinds.idle_write = this.choices.idle;
      if (this.required || this.choices.confirm !== this.originalChoices.confirm) { bot.levels.rsi = this.choices.confirm ? "ask" : "auto_undo"; bot.levels.teaching_params = bot.levels.rsi; }
      this.bot = await this.deps.api.putBotSettings(bot);
      if (this.required || this.choices.tool !== this.originalChoices.tool || this.choices.patch !== this.originalChoices.patch) {
        const forge = await this.deps.setup.forge({ tool: this.choices.tool, patch: this.choices.patch });
        if (forge.tool !== this.choices.tool || forge.patch !== this.choices.patch) throw new Error(text("failed"));
      }
      if (this.required || this.choices.rsi !== this.originalChoices.rsi) { const rsi = await this.deps.api.putRsiSwitch(this.choices.rsi); if (rsi.on !== this.choices.rsi) throw new Error(text("failed")); }
      const budget = await this.deps.usage.patch({ confirm_budget: true }); if (!budget.budget_confirmed) throw new Error(text("failed"));
      await this.deps.saveBookmark(0); await this.deps.finished(); this.completed = true; this.resolve(); this.close();
    });
  }
  private blocker(code: string): string {
    switch (code) { case "learning_model_missing": return text("learningModelMissing"); case "improver_model_missing": return text("improverModelMissing"); case "judge_model_missing": return text("judgeModelMissing"); default: return text("failed"); }
  }
  private async run(action: () => Promise<void>): Promise<void> {
    if (this.busy || !this.live) return; this.busy = true; setStatus(this.status, "");
    for (const control of this.footer.querySelectorAll<HTMLButtonElement>("button")) control.disabled = true;
    try { await action(); } catch (error: unknown) { this.showError(error); } finally { this.busy = false; this.draw(); }
  }
  private showError(error: unknown): void {
    if (error instanceof Error && error.message === "local_setup_timeout") { setStatus(this.status, text("helperTimeout")); return; }
    if (error instanceof Error && error.message === "local_setup_in_progress") { setStatus(this.status, text("helperRunning")); return; }
    setStatus(this.status, error instanceof ApiError ? error.detail : error instanceof Error && !error.message.startsWith("local_setup_") ? error.message : text("processFailed"));
  }
  override onClose(): void {
    this.live = false; this.localeWindow?.removeEventListener("focus", this.refreshLocale); this.key = ""; this.attemptedCatalogs.clear(); this.disposeStep?.(); this.offLanguage?.(); this.signal?.removeEventListener("abort", this.abort);
    if (!this.completed) { if (this.required) this.reject(new ApiError(403, text("cancel"), "budget_confirmation_required")); else this.resolve(); }
    this.contentEl.replaceChildren();
  }
}
