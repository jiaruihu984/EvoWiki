import { afterEach, expect, it, vi } from "vitest";
import type { App, WorkspaceLeaf } from "obsidian";
import { clearEwNotices, ewNotices } from "../test/ew-notices";
import type EvoWikiPlugin from "./main";
import { ApiError } from "./api/errors";
import { DEFAULT_SETTINGS, type EvoWikiSettings } from "./settings";
import { bindNotesFolder, EvoWikiSettingTab } from "./settings-tab";
import { type SettingsHost, SettingsView } from "./settings-view";
import { flush, methods, setup } from "./wiki/controller-test-kit";
import type { LanguageHost } from "./wiki/language-setting";
import { wikiActions } from "./wiki/wiring";
import type { ReviewLimitHost } from "./wiki/review-limit-setting";
import { DemoUsageClient } from "./wiki/fixtures-demo";
import { UsageClient } from "./wiki/usage";
import { MaintenanceClient, type BackupStatus } from "./maintenance";

afterEach(() => { clearEwNotices(); document.body.replaceChildren(); });

/** The plugin's wiki hosts on one fake wiki controller (unconfigured unless asked). */
function guidelines(configured = false) {
  const { api, c, conn } = setup({ configured });
  const review: ReviewLimitHost = { state: () => c.state, subscribe: (fn) => c.subscribe(fn),
    setReviewPerDay: (n) => void c.setReviewPerDay(n), load: () => undefined };
  const language: LanguageHost = { state: () => c.state, subscribe: (fn) => c.subscribe(fn), setLanguage: (l) => c.setLanguage(l),
    load: () => undefined };
  return { api, c, conn, engineSettingsHost: vi.fn(() => ({ state: () => c.state, subscribe: (fn: () => void) => c.subscribe(fn), actions: wikiActions(() => c), load: () => { void c.loadEngine(); } })), teachHost: vi.fn(() => c.teachPort()), reviewLimitHost: vi.fn(() => review), languageHost: vi.fn(() => language) };
}

/** A plugin stand-in (SettingsHost): settings, updateSettings that applies the patch, the wiki hosts. */
function plugin(over: Partial<typeof DEFAULT_SETTINGS> = {}, g = guidelines()) {
  const usageLog: string[] = [];
  const listeners = new Set<() => void>();
  const data: { settings: EvoWikiSettings } = { settings: { ...DEFAULT_SETTINGS, userId: "", ...over } };
  const p = {
    data,
    currentSettings: (): EvoWikiSettings => data.settings,
    updateSettings: vi.fn((patch: Partial<EvoWikiSettings>) => { data.settings = { ...data.settings, ...patch }; return Promise.resolve(); }),
    engineSettingsHost: g.engineSettingsHost, teachHost: g.teachHost, reviewLimitHost: g.reviewLimitHost, languageHost: g.languageHost,
    usageClient: vi.fn(() => new UsageClient(() => ({ baseUrl: "http://h:1", userId: "u" }), {
      request: (r) => { usageLog.push(`${r.method} ${r.url}`); return new Promise(() => undefined); } })),
    onIdentityChange: vi.fn((fn: () => void) => { listeners.add(fn); return () => { listeners.delete(fn); }; }),
    usageLog, listeners,
  } satisfies SettingsHost & Record<string, unknown>;
  return p;
}
const tabFor = (p: ReturnType<typeof plugin>) => new EvoWikiSettingTab({} as App, p as unknown as EvoWikiPlugin & SettingsHost);
const set = (tab: EvoWikiSettingTab): HTMLElement => {
  const s = tab.containerEl.querySelector<HTMLElement>(":scope > .ew > .ew-set");
  if (!s) throw new Error("no .ew > .ew-set");
  return s;
};
const sections = (s: HTMLElement): HTMLElement[] => [...s.querySelectorAll<HTMLElement>(":scope > section")];
const section = (s: HTMLElement, title: string): HTMLElement => {
  const x = sections(s).find((y) => y.querySelector(":scope > h2")?.textContent === title);
  if (!x) throw new Error(`no section ${title}`);
  return x;
};
const rowNames = (box: HTMLElement): string[] => [...box.querySelectorAll(".ew-srow")].filter((r) => !(r as HTMLElement).hidden)
  .map((r) => r.querySelector(".ew-n")?.textContent ?? "");
const commit = (input: HTMLInputElement, v: string) => { input.value = v; input.dispatchEvent(new Event("change")); };
const ctl = <T extends HTMLElement>(root: HTMLElement, label: string): T => {
  const x = root.querySelector<T>(`[aria-label="${label}"]`);
  if (!x) throw new Error(`no control ${label}`);
  return x;
};
const settle = async () => { for (let i = 0; i < 6; i++) await Promise.resolve(); };

it("notes folder: an invalid value sets aria-invalid and saves nothing; a valid one saves the normalised folder and clears it", async () => {
  const input = document.createElement("input");
  const save = vi.fn((_f: string) => Promise.resolve());
  bindNotesFolder(input, save);
  for (const bad of ["../x", "/abs", ".obsidian", ".evowiki/backups/x", "a\\b"]) {
    commit(input, bad);
    expect(input.getAttribute("aria-invalid"), bad).toBe("true");
  }
  expect(save).not.toHaveBeenCalled();
  commit(input, "Study//Concepts/");
  await Promise.resolve();
  expect(save).toHaveBeenCalledTimes(1);
  expect(save).toHaveBeenCalledWith("Study/Concepts");
  expect(input.hasAttribute("aria-invalid")).toBe(false);
  expect(input.value).toBe("Study/Concepts");
});

it("app.js renderSettings: h1 EvoWiki 设置, the five sections in order, every row .ew-srow with .ew-n / .ew-d, the demo's words", () => {
  const tab = tabFor(plugin());
  tab.display();
  const s = set(tab);
  expect(s.firstElementChild?.tagName).toBe("H1");
  expect(s.firstElementChild?.textContent).toBe("EvoWiki 设置");
  expect(sections(s).map((x) => x.querySelector(":scope > h2")?.firstChild?.firstChild?.textContent ?? x.querySelector(":scope > h2")?.textContent)).toEqual([undefined, "学习", "样板页", "用量", "其他", "开启 Bot", "自动改进讲解和助手", "提示词设置", "掌握程度评估参数"]);
  expect([...section(s, "其他").querySelectorAll(".ew-srow")].map((r) => [r.querySelector(".ew-n")?.textContent, r.querySelector(".ew-d")?.textContent]))
    .toEqual([["服务器地址", undefined], ["用户名", undefined]]);
  expect(rowNames(section(s, "学习"))).toEqual(["语言", "每天最多复习几张", "选中文字问助手"]);
  expect(s.querySelector(".ew-d")).toBeNull();
  expect(s.textContent).not.toMatch(/自动整理|隔几天整理/);
  for (const i of s.querySelectorAll("input")) expect(i.type).toBe("text");
  // switches: button.ew-switch role=switch aria-checked
  const sw = [...s.querySelectorAll<HTMLButtonElement>("button.ew-switch")];
  expect(sw.map((b) => [b.getAttribute("role"), b.getAttribute("aria-checked"), b.getAttribute("aria-label")])).toEqual([
    ["switch", "true", "选中文字问助手"]]);
  expect(s.textContent).not.toMatch(/复习标签|本页|S3|condition|Server URL/);
  tab.hide();
});

it("display() again: still exactly one 设置; hide() disposes and calls the base hide", () => {
  const tab = tabFor(plugin());
  tab.display();
  tab.display();
  expect(tab.containerEl.querySelectorAll(".ew-set")).toHaveLength(1);
  const superHide = vi.spyOn(Object.getPrototypeOf(EvoWikiSettingTab.prototype) as { hide(): void }, "hide");
  tab.hide();
  expect(superHide).toHaveBeenCalledTimes(1);
  superHide.mockRestore();
});

it("用量 unconfigured: the setup line, no request; configured: reads", () => {
  const p = plugin();
  const tab = tabFor(p);
  tab.display();
  expect(section(set(tab), "用量").textContent).toContain("先在上面填服务器地址和用户名。");
  expect(p.usageLog).toEqual([]);
  tab.hide();
  const q = plugin({ userId: "jerry" });
  const t2 = tabFor(q);
  t2.display();
  expect(q.usageLog).toEqual(["GET http://h:1/usage", "GET http://h:1/usage/settings"]);
  t2.hide();
});

it("committing a user id: invalid → flagged, nothing saved; valid → saved, the server-backed sections drawn again once", async () => {
  const p = plugin();
  const tab = tabFor(p);
  tab.display();
  const s = set(tab);
  const userInput = ctl<HTMLInputElement>(s, "用户名");
  commit(userInput, "a:b");                                 // not a valid user id: flagged, nothing saved
  await settle();
  expect(userInput.getAttribute("aria-invalid")).toBe("true");
  expect(s.textContent).toContain("只能用英文字母、数字和 _ . -");   // says why
  expect(p.updateSettings).not.toHaveBeenCalled();
  commit(userInput, "jerry");
  await settle();
  expect(p.updateSettings).toHaveBeenCalledWith({ userId: "jerry" });
  expect(userInput.hasAttribute("aria-invalid")).toBe(false);
  expect(s.textContent).not.toContain("只能用英文字母");
  expect(p.teachHost).toHaveBeenCalledTimes(2);        // 样板页 drawn again for the new identity
  expect(p.reviewLimitHost).toHaveBeenCalledTimes(2);  // 每天最多复习几张 too
  for (const fn of p.listeners) fn();                 // the plugin's own identity notice: already drawn for it, not again
  expect(p.teachHost).toHaveBeenCalledTimes(2);
  expect(sections(s)).toHaveLength(9);
  expect(section(s, "用量").textContent).not.toContain("先在上面填服务器地址和用户名。");
  tab.hide();
  expect(p.listeners.size).toBe(0);
});

it("a new identity committed elsewhere (the other 设置): onIdentityChange redraws this one", () => {
  const p = plugin();
  const tab = tabFor(p);
  tab.display();
  p.data.settings = { ...p.data.settings, userId: "other" };
  for (const fn of p.listeners) fn();
  expect(p.languageHost).toHaveBeenCalledTimes(2);
  tab.hide();
});

it("demo fixture: the 用量 section reads the fixture's numbers (今天 $0.31 · 本月 $4.12) even with no server set", async () => {
  const p = plugin();
  p.usageClient.mockReturnValue(new DemoUsageClient());
  const host = { ...p, usageConfigured: () => true };
  const tab = new EvoWikiSettingTab({} as App, host as unknown as EvoWikiPlugin & SettingsHost);
  tab.display();
  await vi.waitFor(() => expect(section(set(tab), "用量").textContent).toContain("$4.12"));
  expect(section(set(tab), "用量").textContent).toContain("$0.31");
  tab.hide();
});

it("server address: an invalid one is flagged with its reason; a valid one is saved normalised", async () => {
  const p = plugin();
  const tab = tabFor(p);
  tab.display();
  const url = ctl<HTMLInputElement>(set(tab), "服务器地址");
  commit(url, "ftp://x");
  await settle();
  expect(url.getAttribute("aria-invalid")).toBe("true");
  expect(url.closest(".ew-srow")?.querySelector(".ew-muted")?.textContent).toBe("请用 http:// 或 https:// 开头。");
  commit(url, "http://127.0.0.1:9999/x");
  await settle();
  expect(p.updateSettings).toHaveBeenCalledWith({ serverUrl: "http://127.0.0.1:9999" });
  expect(p.languageHost).toHaveBeenCalledTimes(2);
  tab.hide();
});

it("选中文字问助手: the switch saves allowSelectionContext; a failed save flips it back and says so", async () => {
  const p = plugin();
  const tab = tabFor(p);
  tab.display();
  const sw = ctl<HTMLButtonElement>(set(tab), "选中文字问助手");
  sw.click();
  await settle();
  expect(p.updateSettings).toHaveBeenCalledWith({ allowSelectionContext: false });
  expect(sw.getAttribute("aria-checked")).toBe("false");
  p.updateSettings.mockImplementationOnce(() => Promise.reject(new Error("disk")));
  const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
  sw.click();
  await settle();
  expect(sw.getAttribute("aria-checked")).toBe("false");
  expect(ewNotices()).toEqual(["没能保存这个设置，请再试一次。"]);
  expect(sw.closest(".ew-srow")?.querySelector(".ew-muted")?.textContent).toBe("没能保存这个设置，请再试一次。");
  warn.mockRestore();
  tab.hide();
});

it("样板页: reads the spec once and lists the exemplars with 删除; nothing to add here (no 用当前打开的笔记)", async () => {
  const g = guidelines(true);
  const p = plugin({}, g);
  const tab = tabFor(p);
  tab.display();
  const sec = section(set(tab), "样板页");
  for (let i = 0; i < 3; i++) await flush();
  expect(methods(g.api)).toContain("teachSpec");
  expect(sec.querySelector("[data-action=add-exemplar]")).toBeNull();
  expect(sec.textContent).not.toContain("用当前打开的笔记");
  tab.hide();
});

it("每天最多复习几张 / 语言 sit on the wiki controller; hide() stops them following", async () => {
  const g = guidelines(true);
  await g.c.init();
  const tab = tabFor(plugin({}, g));
  tab.display();
  const s = set(tab);
  const box = ctl<HTMLInputElement>(s, "每天最多复习几张");
  expect(box.value).toBe("10");
  expect(ctl<HTMLSelectElement>(s, "语言").value).toBe("zh");
  g.api.calls.length = 0;
  box.value = "6"; box.dispatchEvent(new Event("change"));
  for (let i = 0; i < 3; i++) await flush();
  expect(methods(g.api)).toContain("putSettings");
  expect(box.value).toBe("6");
  tab.hide();
  const before = tab.containerEl.textContent;
  await g.c.setReviewPerDay(7);
  expect(tab.containerEl.textContent).toBe(before);
});

it("the 设置 view: .ew > .ew-scroll > the same .ew-set, display text 设置; closing disposes", async () => {
  const p = plugin();
  const v = new SettingsView({} as WorkspaceLeaf, p);
  expect(v.getViewType()).toBe("evowiki-settings");
  expect(v.getDisplayText()).toBe("设置");
  await v.onOpen();
  const root = v.contentEl.firstElementChild as HTMLElement;
  expect(root.className).toBe("ew");
  expect(root.firstElementChild?.className).toBe("ew-scroll");
  expect(root.firstElementChild?.firstElementChild?.className).toBe("ew-set");
  expect(p.listeners.size).toBe(1);
  await v.onClose();
  expect(p.listeners.size).toBe(0);
  expect(v.contentEl.childElementCount).toBe(0);
});

it("the real settings tab saves Bot controls and opens a prompt through controller actions; help never activates the editor", async () => {
  const g = guidelines(true), p = plugin({}, g), tab = tabFor(p);
  tab.display();
  await flush(); await flush();
  set(tab).querySelector<HTMLButtonElement>("#bot-enabled")!.click();
  await flush();
  expect(g.c.state.bot.settings.kind === "ready" && g.c.state.bot.settings.value.paused).toBe("always");
  const cap = set(tab).querySelector<HTMLInputElement>("#bot-perday")!;
  commit(cap, "8"); await flush();
  expect(g.c.state.bot.settings.kind === "ready" && g.c.state.bot.settings.value.today_cap).toBe(8);
  const open = vi.spyOn(g.c, "openPrompt");
  const button = set(tab).querySelector<HTMLButtonElement>('[data-prompt="write"]')!;
  const row = button.closest(".ew-prow")!;
  row.querySelector<HTMLButtonElement>(".ew-qbtn")!.click();
  expect(open).not.toHaveBeenCalled();
  expect(row.querySelector("button button")).toBeNull();
  button.click();
  expect(open).toHaveBeenCalledWith("write");
  tab.hide();
});

it("提示词设置: one row per step the backend lists (its name, its description behind ?), no 讲解方式 row; 评估参数' ? are the backend's too", async () => {
  const g = guidelines(true), tab = tabFor(plugin({}, g));
  tab.display();
  await flush(); await flush();
  const p = g.c.state.prompts, e = g.c.state.engine;
  if (p.kind !== "ready" || e.kind !== "ready") throw new Error("not read");
  const rows = [...section(set(tab), "提示词设置").querySelectorAll<HTMLElement>(":scope > .ew-prow")];
  expect(rows.map((r) => r.querySelector("[data-prompt]")?.getAttribute("data-prompt"))).toEqual(p.value.map((x) => x.step));
  expect(rows.map((r) => r.querySelector("[data-prompt] > span")?.textContent)).toEqual(p.value.map((x) => x.name));
  expect(rows.map((r) => r.querySelector("[role=tooltip]")?.textContent)).toEqual(p.value.map((x) => x.description));
  expect(set(tab).querySelector("[data-prompt=rules]")).toBeNull();
  const params = [...section(set(tab), "掌握程度评估参数").querySelectorAll<HTMLElement>(":scope > .ew-srow")];
  expect(params.map((r) => [r.querySelector(".ew-n")?.textContent, r.querySelector("[role=tooltip]")?.textContent]))
    .toEqual(e.value.params.map((x) => [x.label, x.description]));
  tab.hide();
});

it("Bot, prompts and assessment read errors stay in their own settings sections and disappear offline", async () => {
  const g = guidelines(true);
  for (const method of ["botSettings", "prompts", "engine"] as const) g.api.failNext(method, new ApiError(503, "unavailable"));
  const tab = tabFor(plugin({}, g)); tab.display();
  await flush(); await flush();
  const errorText = () => ["开启 Bot", "提示词设置", "掌握程度评估参数"].map((name) => section(set(tab), name).querySelector(".ew-muted")?.textContent);
  expect(errorText()).toEqual(["没能读到，重试", "没能读到，重试", "没能读到，重试"]);
  g.conn.emit("offline");
  expect(errorText()).toEqual([undefined, undefined, undefined]);
  tab.hide();
});

it("reopening settings replaces subscriptions, identity changes replace them, and hide disposes them", () => {
  const g = guidelines(), original = g.c.subscribe.bind(g.c);
  let active = 0;
  vi.spyOn(g.c, "subscribe").mockImplementation((fn) => {
    active += 1; const off = original(fn);
    return () => { active -= 1; off(); };
  });
  const p = plugin({}, g), tab = tabFor(p);
  tab.display(); const mounted = active; expect(mounted).toBeGreaterThan(0);
  tab.display(); expect(active).toBe(mounted);
  p.data.settings = { ...p.data.settings, userId: "different" };
  for (const fn of p.listeners) fn();
  expect(active).toBe(mounted);
  tab.hide(); expect(active).toBe(0);
});


const backupStatus = (warning: string | null = null): BackupStatus => ({ disabled: false, running: false,
  last_success_at: "2026-11-08T12:00:00Z", last_failure_reason: warning, warning, files: ["backup.dump"] });

it("the existing settings surface shows backup warnings and offers an explicit backup retry", async () => {
  const p = plugin({ userId: "u" });
  const requests: string[] = [];
  const maintenance = new MaintenanceClient(() => ({ baseUrl: p.data.settings.serverUrl, userId: p.data.settings.userId }), {
    request: (r) => {
      requests.push(r.method);
      return Promise.resolve({ status: 200, text: JSON.stringify(backupStatus(r.method === "GET" ? "disk full" : null)) });
    },
  });
  Object.assign(p, { maintenanceClient: () => maintenance });
  const tab = tabFor(p);
  tab.display();
  await settle();
  const backup = section(set(tab), "备份");
  expect(backup.textContent).toContain("disk full");
  const retry = [...backup.querySelectorAll("button")].find((b) => b.textContent === "重试备份")!;
  retry.click();
  await settle();
  expect(requests).toEqual(["GET", "POST"]);
  expect(backup.textContent).not.toContain("disk full");
  expect(ewNotices()).toEqual([]);
  tab.hide();
});

it("switching server discards an old in-flight backup status; hiding Settings stops late updates", async () => {
  const p = plugin({ userId: "u", serverUrl: "http://old" });
  let release = (_r: { status: number; text: string }): void => undefined;
  const maintenance = new MaintenanceClient(() => ({ baseUrl: p.data.settings.serverUrl, userId: p.data.settings.userId }), {
    request: (r) => r.url.startsWith("http://old")
      ? new Promise((resolve) => { release = resolve; })
      : Promise.resolve({ status: 200, text: JSON.stringify(backupStatus()) }),
  });
  Object.assign(p, { maintenanceClient: () => maintenance });
  const tab = tabFor(p);
  tab.display();
  p.data.settings = { ...p.data.settings, serverUrl: "http://new" };
  for (const listener of p.listeners) listener();
  await settle();
  release({ status: 200, text: JSON.stringify(backupStatus("stale old server warning")) });
  await settle();
  expect(section(set(tab), "备份").textContent).not.toContain("stale old server warning");
  tab.hide();
});


it("selecting a vault folder saves it; cancel and external paths leave the directory unchanged", async () => {
  const p = Object.assign(plugin({ notesFolder: "Existing/Notes" }), {
    pickNotesFolder: vi.fn<() => Promise<string | null>>().mockResolvedValueOnce("Study/Concepts").mockResolvedValueOnce(null).mockResolvedValueOnce("D:/outside"),
  });
  const tab = tabFor(p); tab.display();
  const button = set(tab).querySelector<HTMLButtonElement>("[data-action=choose-notes-folder]")!;
  expect(button.textContent).toBe("选择文件夹");
  button.click(); await settle();
  expect(p.updateSettings).toHaveBeenCalledWith({ notesFolder: "Study/Concepts" });
  expect(p.currentSettings().notesFolder).toBe("Study/Concepts");
  button.click(); await settle();
  button.click(); await settle();
  expect(p.updateSettings).toHaveBeenCalledTimes(1);
  expect(p.currentSettings().notesFolder).toBe("Study/Concepts");
  expect(set(tab).textContent).toContain("当前 Obsidian 库内");
});
