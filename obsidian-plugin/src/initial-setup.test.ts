import type { App } from "obsidian";
import type { LocalSetupResult } from "./local-setup";
import { afterEach, expect, it, vi } from "vitest";
import { InitialSetupModal, type SetupDeps } from "./initial-setup";
import { setUiLanguage } from "./i18n";
import { DEFAULT_SETTINGS } from "./settings";
import { SetupService, type SetupSnapshot } from "./setup-service";
import { ProfileClient, type WikiHttp } from "./wiki/api";
import { FakeWikiApi } from "./wiki/fake";
import { UsageClient, USAGE_FIXTURE } from "./wiki/usage";
import type { UsageSettingsIn, UsageSettingsOut } from "./generated/service";

const mounted: InitialSetupModal[] = [];
afterEach(() => { for (const modal of mounted.splice(0)) modal.onClose(); document.body.replaceChildren(); setUiLanguage("zh"); });
function setup(required = true, step = 0, forgeAllowed = false) {
  const api = new FakeWikiApi({ delayMs: 0 });
  let budget: UsageSettingsOut = { budget_confirmed: !required, daily_limit_usd: 5, monthly_limit_usd: 30 };
  const patches: UsageSettingsIn[] = [];
  const snapshot: SetupSnapshot = { model_configured: true, rsi: { on: false, available: false, blockers: ["improver_model_missing"] },
    forge: { tool: false, patch: false, requested_tool: false, requested_patch: false, global_allowed: forgeAllowed, blockers: forgeAllowed ? ["patch_globally_disabled"] : ["forge_globally_disabled"] },
    background: { paused: false, includes_memory: true, idle_write: true }, backup: { enabled: true, directory: "D:/backups", keep: 14, every_seconds: 86400 }, usage_timezone: "Asia/Shanghai" };
  const http: WikiHttp = { request: (request) => {
    const path = new URL(request.url).pathname;
    let answer: unknown;
    if (path === "/usage") answer = USAGE_FIXTURE;
    else if (path === "/usage/settings") {
      if (request.method === "PATCH") { const body = JSON.parse(request.body as string) as UsageSettingsIn; patches.push(body); if (body.confirm_budget) budget.budget_confirmed = true; budget = { ...budget, ...body }; }
      answer = budget;
    } else if (path === "/wiki/setup") answer = snapshot;
    else if (path === "/wiki/setup/forge") answer = snapshot.forge;
    else if (path === "/wiki/profile") answer = { text: "## About me\n- Synthetic background\n", version: "v1", locale: "en", format_version: 2, kept: [] };
    else throw new Error(`Unexpected fake route: ${path}`);
    return Promise.resolve({ status: 200, text: JSON.stringify(answer) });
  } };
  const config = () => ({ baseUrl: "http://fake:8002", userId: "first-use" });
  const models = { current: "fake-model", reasoning: "default", models: [{ id: "fake-model", name: "Synthetic model", provider: "fake", provider_name: "Fake", available: true, reason: "", levels: [{ reasoning: "default", name: "Default" }] }] };
  const done = vi.fn(); const reject = vi.fn(); const saveBookmark = vi.fn(() => Promise.resolve());
  const deps: SetupDeps = { api, usage: new UsageClient(config, http), profile: new ProfileClient(config, http), setup: new SetupService(config, http),
    native: null, maintenance: null, assistant: { listModels: () => Promise.resolve(models), pickModel: () => Promise.resolve(models) },
    settings: () => DEFAULT_SETTINGS, updateSettings: () => Promise.resolve(), bookmark: () => step, saveBookmark,
    openBackground: () => Promise.resolve(), finished: () => Promise.resolve() };
  const app = { vault: { adapter: {}, getName: () => "Synthetic vault" } } as unknown as App;
  const modal = new InitialSetupModal(app, deps, required, undefined, done, reject);
  mounted.push(modal); document.body.append(modal.containerEl); modal.onOpen();
  const wait = async (name: string): Promise<void> => { await vi.waitFor(() => {
    expect(modal.contentEl.querySelector("[data-setup-step]")?.getAttribute("data-setup-step")).toBe(name);
    expect(modal.contentEl.querySelector<HTMLButtonElement>("[data-action=setup-next], [data-action=setup-finish]")?.disabled).toBe(false);
  }); };
  const next = (): void => { modal.contentEl.querySelector<HTMLButtonElement>("[data-action=setup-next]")!.click(); };
  return { modal, api, deps, snapshot, patches, budget: () => budget, done, reject, wait, next, saveBookmark };
}
it("the six real steps keep budget unconfirmed until Finish and default risky automation off", async () => {
  setUiLanguage("en"); const s = setup(); await s.wait("language");
  for (const step of ["api", "background", "budgets", "automation", "summary"]) {
    s.next(); await s.wait(step); expect(s.budget().budget_confirmed).toBe(false); expect(s.patches.some((patch) => patch.confirm_budget)).toBe(false);
  }
  expect(s.modal.contentEl.textContent).not.toMatch(/\p{Script=Han}/u);
  s.modal.contentEl.querySelector<HTMLButtonElement>("[data-action=setup-finish]")!.click();
  await vi.waitFor(() => expect(s.done).toHaveBeenCalledOnce());
  expect(s.budget().budget_confirmed).toBe(true);
  const bot = await s.api.botSettings(); expect(bot.paused).toBe("always"); expect(bot.kinds.idle_write).toBe(false); expect(bot.levels.rsi).toBe("ask");
  expect((await s.api.rsiSwitch()).on).toBe(false);
});
it("cancelling the wizard never confirms a new account's budget", async () => {
  const s = setup(); await s.wait("language"); s.modal.onClose();
  expect(s.reject).toHaveBeenCalledOnce(); expect(s.patches).toEqual([]); expect(s.budget().budget_confirmed).toBe(false);
});
it("an unfinished flow resumes its bookmarked step with saved service values", async () => {
  const s = setup(true, 2); await s.wait("background"); expect(s.modal.contentEl.querySelector<HTMLTextAreaElement>("[data-field=background-who]")!.value).toContain("Synthetic background");
  expect(s.patches).toEqual([]);
});

it("an external account language refresh preserves the budget input and its unsaved selection", async () => {
  const s = setup(true, 3); await s.wait("budgets");
  await vi.waitFor(() => expect(s.modal.contentEl.querySelector("input")).not.toBeNull());
  const input = s.modal.contentEl.querySelector<HTMLInputElement>("input")!;
  input.value = "12.34"; input.focus(); input.setSelectionRange(2, 4);
  await s.api.putSettings({ language: "en" }); window.dispatchEvent(new Event("focus"));
  await vi.waitFor(() => expect(s.modal.titleEl.textContent).toBe("Initial setup"));
  expect(s.modal.contentEl.querySelector("input")).toBe(input); expect(input.value).toBe("12.34"); expect(input.selectionStart).toBe(2);
});
it("forge switches use independent native blockers", async () => {
  const s = setup(true, 4, true);
  await s.wait("automation");
  expect(s.modal.contentEl.querySelector<HTMLButtonElement>("[data-field=tool]")!.disabled).toBe(false);
  expect(s.modal.contentEl.querySelector<HTMLButtonElement>("[data-field=patch]")!.disabled).toBe(true);
});

it("saving an API provider refreshes the real RSI prerequisites before permission choices", async () => {
  const s = setup(true, 1);
  const status = { ok: true, code: "ok", revision: "v1", providers: [{ id: "anthropic", configured: true, base_url: "http://localhost", model: "qa", models: ["qa"] }], selected_provider: "anthropic", errors: [], restart: { state: "running", port: 8002 } };
  s.deps.native = { request: vi.fn(() => Promise.resolve(status)) } as unknown as NonNullable<SetupDeps["native"]>;
  await s.wait("api");
  const fresh = { ...s.snapshot, rsi: { on: false, available: true, blockers: [] } };
  const refresh = vi.spyOn(s.deps.setup, "get").mockResolvedValue(fresh);
  [...s.modal.contentEl.querySelectorAll<HTMLButtonElement>("button")].find((b) => b.textContent === "保存并重启")!.click();
  await vi.waitFor(() => expect(refresh).toHaveBeenCalled()); await s.wait("api");
  for (const step of ["background", "budgets", "automation"]) { s.next(); await s.wait(step); }
  expect(s.modal.contentEl.querySelector<HTMLButtonElement>("[data-field=rsi]")!.disabled).toBe(false);
});

it("API labels and reasoning refresh without replacing unsaved key and model inputs", async () => {
  const s = setup(true, 1);
  const status = { ok: true, code: "ok", revision: "v1", providers: [{ id: "anthropic", configured: true, base_url: "http://localhost", model: "qa", models: ["qa"] }], selected_provider: "anthropic", errors: [], restart: { state: "running", port: 8002 } };
  s.deps.native = { request: vi.fn(() => Promise.resolve(status)) } as unknown as NonNullable<SetupDeps["native"]>;
  await s.wait("api"); const key = s.modal.contentEl.querySelector<HTMLInputElement>('[data-field="api-key"]')!;
  key.value = "qa-unsaved"; key.dispatchEvent(new Event("input"));
  const model = s.modal.contentEl.querySelector<HTMLSelectElement>('[data-field="local-model"]')!;
  model.value = "qa"; model.dispatchEvent(new Event("change"));
  const reasoning = s.modal.contentEl.querySelector<HTMLSelectElement>('[data-setup-model="reasoning"]')!;
  vi.spyOn(s.deps.assistant, "listModels").mockResolvedValue({ current: "fake-model", reasoning: "default", models: [{ id: "fake-model", name: "English model", provider: "fake", provider_name: "Fake", available: true, reason: "", levels: [{ reasoning: "default", name: "English reasoning" }] }] });
  setUiLanguage("en"); await vi.waitFor(() => expect(reasoning.options[0]!.textContent).toBe("English reasoning"));
  expect(s.modal.contentEl.querySelector('[data-field="local-model"]')).toBe(model); expect(model.value).toBe("qa");
  expect(s.modal.contentEl.querySelector('[data-setup-model="model"]')).toBeNull();
  expect(s.modal.contentEl.querySelector('[data-field="api-key"]')).toBe(key); expect(key.value).toBe("qa-unsaved");
  expect(key.placeholder).toBe("Leave empty to keep the saved key"); expect(s.modal.contentEl.textContent).toContain("Configured");
});


it("language step uses the current vault automatically and preserves the saved note directory", async () => {
  const s = setup();
  const saved = { ...DEFAULT_SETTINGS, notesFolder: "Existing/Notes" };
  s.deps.settings = () => saved;
  const update = vi.spyOn(s.deps, "updateSettings");
  await s.wait("language");
  expect(s.modal.contentEl.querySelector("[data-field=language]")).not.toBeNull();
  expect(s.modal.contentEl.querySelector("[data-field=notes-folder], .setup-path")).toBeNull();
  s.next(); await s.wait("api");
  expect(update).not.toHaveBeenCalled();
  expect(s.deps.settings().notesFolder).toBe("Existing/Notes");
});

function nativeApiSetup(configured = true) {
  const s = setup(false, 1);
  const status = { ok: true, code: "ok", revision: "v1", providers: [
    { id: "anthropic", configured: true, base_url: "http://localhost", model: "provider-default", models: ["provider-default"] },
    { id: "fake", configured: true, base_url: "http://localhost", model: "fake-default", models: ["fake-default"] },
  ], selected_provider: "anthropic", errors: [], restart: { state: "running", port: 8002 } };
  const request = vi.fn((body: { op: string }) => Promise.resolve(body.op === "apply"
    ? { ...status, providers: status.providers.map((provider) => ({ ...provider, configured, model: "canonical-model" })) } : status));
  s.deps.native = { request } as unknown as NonNullable<SetupDeps["native"]>;
  const pick = vi.spyOn(s.deps.assistant, "pickModel");
  return { ...s, request, pick };
}
it("native setup uses one model field initialized from the current chat and provider defaults on switch", async () => {
  const s = nativeApiSetup(); await s.wait("api");
  const provider = s.modal.contentEl.querySelector<HTMLSelectElement>('[data-field="provider"]')!;
  expect(provider.value).toBe("fake");
  expect(s.modal.contentEl.querySelector<HTMLSelectElement>('[data-field="local-model"]')!.value).toBe("Synthetic model");
  expect(s.modal.contentEl.querySelector('[data-setup-model="model"]')).toBeNull();
  const reasoning = s.modal.contentEl.querySelector('[data-setup-model="reasoning"]')!;
  const model = s.modal.contentEl.querySelector('[data-field="local-model"]')!;
  const controls = [...s.modal.contentEl.querySelectorAll<HTMLElement>('[data-native-action], details')];
  expect(reasoning).not.toBeNull();
  expect(model.compareDocumentPosition(reasoning) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  for (const control of controls) expect(reasoning.compareDocumentPosition(control) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(s.modal.contentEl.textContent).not.toContain("删除已保存的密钥");
  expect(s.modal.contentEl.querySelectorAll('button[role="switch"]')).toHaveLength(1);
  expect([...s.modal.contentEl.querySelector<HTMLSelectElement>('[data-field="local-model"]')!.options].map((option) => option.value)).toEqual(["fake-default", "Synthetic model"]);
  provider.value = "anthropic"; provider.dispatchEvent(new Event("change"));
  expect(s.modal.contentEl.querySelector<HTMLSelectElement>('[data-field="local-model"]')!.value).toBe("provider-default");
});
it("saving native API configuration selects its saved canonical model", async () => {
  const s = nativeApiSetup(); await s.wait("api");
  [...s.modal.contentEl.querySelectorAll<HTMLButtonElement>("button")].find((button) => button.textContent === "保存并重启")!.click();
  await vi.waitFor(() => expect(s.pick).toHaveBeenCalledWith({ model: "fake/canonical-model" }));
  await s.wait("api");
  const applied = s.request.mock.calls.find(([body]) => body.op === "apply")?.[0];
  expect(applied).not.toHaveProperty("clear_key");
});
it("checking native API configuration leaves the selected chat model unchanged", async () => {
  const s = nativeApiSetup(); await s.wait("api");
  const check = [...s.modal.contentEl.querySelectorAll<HTMLButtonElement>("button")].find((button) => button.textContent === "检查配置")!;
  expect(check).toBeDefined(); check.click();
  await vi.waitFor(() => expect(s.request).toHaveBeenCalledWith(expect.objectContaining({ op: "check" })));
  await s.wait("api"); expect(s.pick).not.toHaveBeenCalled();
});
it("setup without a native helper retains one model selector", async () => {
  const s = setup(false, 1); await s.wait("api");
  expect(s.modal.contentEl.querySelectorAll('[data-setup-model="model"]')).toHaveLength(1);
  expect(s.modal.contentEl.querySelector('[data-field="local-model"]')).toBeNull();
});

it("catalogue refresh merges only provider models and preserves the key and model controls", async () => {
  const s = nativeApiSetup(); await s.wait("api");
  const key = s.modal.contentEl.querySelector<HTMLInputElement>('[data-field="api-key"]')!;
  const model = s.modal.contentEl.querySelector<HTMLSelectElement>('[data-field="local-model"]')!;
  key.value = "fake-draft-key"; key.dispatchEvent(new Event("input"));
  const status = await s.request({ op: "status" });
  vi.spyOn(s.deps.native!, "request").mockResolvedValueOnce({ ...status, model_catalog: { provider: "fake", models: ["new-model", "fake-default"] } });
  [...s.modal.contentEl.querySelectorAll<HTMLButtonElement>("button")].find((button) => button.textContent === "获取模型")!.click();
  await vi.waitFor(() => expect([...model.options].map((option) => option.value)).toEqual(["new-model", "fake-default", "Synthetic model"]));
  expect(s.modal.contentEl.querySelector('[data-field="api-key"]')).toBe(key); expect(key.value).toBe("fake-draft-key");
  expect(s.modal.contentEl.querySelector('[data-field="local-model"]')).toBe(model); expect(model.value).toBe("Synthetic model");
  expect(s.pick).not.toHaveBeenCalled();
});
it("a failed catalogue refresh reports failure and preserves known models", async () => {
  const s = nativeApiSetup(); await s.wait("api");
  const status = await s.request({ op: "status" });
  vi.spyOn(s.deps.native!, "request").mockResolvedValueOnce({ ...status, ok: false, code: "models_unauthorized" });
  [...s.modal.contentEl.querySelectorAll<HTMLButtonElement>("button")].find((button) => button.textContent === "获取模型")!.click();
  await vi.waitFor(() => expect(s.modal.contentEl.textContent).toContain("无法读取该厂商的模型列表"));
  expect([...s.modal.contentEl.querySelector<HTMLSelectElement>('[data-field="local-model"]')!.options].map((option) => option.value)).toEqual(["fake-default", "Synthetic model"]);
});

it("configured providers load automatically and stale results cannot cross provider boundaries", async () => {
  const s = nativeApiSetup(); await s.wait("api");
  expect(s.request).toHaveBeenCalledWith(expect.objectContaining({ op: "models", provider: "fake" }));
  const status = await s.request({ op: "status" });
  let complete!: (result: LocalSetupResult) => void;
  const request = vi.spyOn(s.deps.native!, "request").mockImplementationOnce(() => new Promise((resolve) => { complete = resolve; }));
  [...s.modal.contentEl.querySelectorAll<HTMLButtonElement>("button")].find((button) => button.textContent === "获取模型")!.click();
  expect([...s.modal.contentEl.querySelectorAll<HTMLButtonElement>('[data-native-action]')].every((button) => button.disabled)).toBe(true);
  const provider = s.modal.contentEl.querySelector<HTMLSelectElement>('[data-field="provider"]')!;
  provider.value = "anthropic"; provider.dispatchEvent(new Event("change"));
  complete({ ...status, model_catalog: { provider: "fake", models: ["wrong-provider-model"] } });
  await vi.waitFor(() => expect(request).toHaveBeenCalledWith(expect.objectContaining({ op: "models", provider: "anthropic" })));
  expect([...s.modal.contentEl.querySelector<HTMLSelectElement>('[data-field="local-model"]')!.options].map((option) => option.value)).toEqual(["provider-default"]);
});
