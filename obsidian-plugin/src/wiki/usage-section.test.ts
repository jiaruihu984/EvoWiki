import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { clearEwNotices, ewNotices } from "../../test/ew-notices";
import { ApiError, OfflineError } from "../api/errors";
import type { WikiHttp, WikiHttpRequest } from "./api";
import { USAGE_COPY } from "./copy";
import { SETTINGS_FIXTURE, USAGE_FIXTURE, UsageClient } from "./usage";
import type { UsageSettingsOut } from "../generated/service";
import { renderUsageSection } from "./usage-section";

type Answer = { status: number; text: string };
type Res = Answer | Error | Promise<Answer>;

/** Answers by "METHOD /path" (a queued answer first, else a tiny in-memory server); records every request. */
function fakeHttp() {
  const log: string[] = [];
  const bodies: unknown[] = [];
  const next = new Map<string, Res[]>();
  let settings: UsageSettingsOut = structuredClone(SETTINGS_FIXTURE);
  const http: WikiHttp = {
    request(r: WikiHttpRequest) {
      const key = `${r.method} ${r.url.replace("http://h:1", "")}`;
      log.push(key);
      bodies.push(r.body === undefined ? undefined : JSON.parse(r.body as string));
      const queued = next.get(key)?.shift();
      if (queued instanceof Error) return Promise.reject(queued);
      if (queued !== undefined) return Promise.resolve(queued);
      if (key === "GET /usage") {
        const out = { ...USAGE_FIXTURE, daily_limit_usd: settings.daily_limit_usd, monthly_limit_usd: settings.monthly_limit_usd };
        return Promise.resolve({ status: 200, text: JSON.stringify(out) });
      }
      if (key === "GET /usage/settings") return Promise.resolve({ status: 200, text: JSON.stringify(settings) });
      if (key === "PATCH /usage/settings") {
        const b = JSON.parse((r.body as string | undefined) ?? "{}") as Partial<UsageSettingsOut> & { confirm_budget?: boolean };
        settings = { ...settings, ...b };
        if (b.confirm_budget) settings.budget_confirmed = true;
        return Promise.resolve({ status: 200, text: JSON.stringify(settings) });
      }
      return Promise.resolve({ status: 404, text: "" });
    },
  };
  const queue = (key: string, r: Res): void => { next.set(key, [...(next.get(key) ?? []), r]); };
  const useSettings = (value: UsageSettingsOut): void => { settings = structuredClone(value); };
  return { http, log, bodies, queue, useSettings };
}

const flush = async (): Promise<void> => { for (let i = 0; i < 10; i++) await Promise.resolve(); };
let el: HTMLElement;
beforeEach(() => { el = document.createElement("div"); document.body.append(el); });
afterEach(() => { el.remove(); clearEwNotices(); });

function open(configured = true, before: (f: ReturnType<typeof fakeHttp>) => void = () => undefined) {
  const f = fakeHttp();
  before(f);
  const client = new UsageClient(() => ({ baseUrl: "http://h:1", userId: "u" }), f.http);
  const sec = renderUsageSection(el, { client, configured: () => configured });
  return { ...f, sec };
}
const input = (label: string): HTMLInputElement => {
  const x = el.querySelector<HTMLInputElement>(`input[aria-label="${label}"]`);
  if (!x) throw new Error(`no input ${label}`);
  return x;
};
const change = (x: HTMLInputElement, v: string): void => { x.value = v; x.dispatchEvent(new Event("change")); };
const text = (): string => el.textContent ?? "";

describe("用量 section", () => {
  it("open: two GETs, today and month totals, per-group lines", async () => {
    const f = open();
    await flush();
    expect(f.log).toEqual(["GET /usage", "GET /usage/settings"]);
    // app.js: the totals line (plain div), then one .ew-muted line per group that spent something.
    expect(el.querySelector(".ew-muted")?.previousElementSibling?.textContent).toBe("今天花了 $0.12 · 本月 $3.40");
    expect([...el.querySelectorAll(".ew-muted")].map((x) => x.textContent).filter((t) => t !== "")).toEqual(
      ["页面：今天 $0.10 · 本月 $3.00", "助手：今天 不到 $0.01 · 本月 $0.38", "复习：今天 $0.02 · 本月 $0.02"]);
    expect(text()).toContain("页面：今天 $0.10 · 本月 $3.00");
    expect(text()).toContain("助手：今天 不到 $0.01 · 本月 $0.38");
    expect(text()).not.toContain("知识图谱：");          // no spend → no line
    expect(input(USAGE_COPY.daily).value).toBe("2");
    expect(input(USAGE_COPY.monthly).value).toBe("");
    expect(input(USAGE_COPY.daily).type).toBe("text");
    expect(el.querySelector("select")).toBeNull();
    const rows = [...el.querySelectorAll(".ew-srow")];
    expect(rows.map((r) => [r.querySelector(".ew-n")?.textContent, r.querySelector(".ew-d")?.textContent])).toEqual([
      ["每天最多花（美元）", undefined], ["每月最多花（美元）", undefined]]);
    for (const r of rows) expect(r.lastElementChild?.matches("input")).toBe(true);
    expect(text()).not.toMatch(/token|调用次数|calls/i);
  });

  it("no spend at all → 还没有花费", async () => {
    open(true, (f) => f.queue("GET /usage", { status: 200, text: JSON.stringify({ ...USAGE_FIXTURE,
      today: { ...USAGE_FIXTURE.today, total_usd: 0, by_group: {} }, month: { ...USAGE_FIXTURE.month, total_usd: 0, by_group: {} } }) }));
    await flush();
    expect(text()).toContain("还没有花费");
    expect(text()).toContain("今天花了 $0 · 本月 $0");
  });

  it("a daily limit change → one PATCH with only that field", async () => {
    const f = open();
    await flush();
    change(input(USAGE_COPY.daily), "3");
    await flush();
    expect(f.log.slice(2)).toEqual(["PATCH /usage/settings", "GET /usage"]);
    expect(f.bodies[2]).toEqual({ daily_limit_usd: 3 });
    expect(input(USAGE_COPY.daily).value).toBe("3");
    expect(ewNotices()).toEqual(["已保存。"]);
  });

  it("an empty limit removes it (null); non-numbers are flagged and not sent", async () => {
    const f = open();
    await flush();
    const daily = input(USAGE_COPY.daily);
    change(daily, "abc");
    await flush();
    expect(f.log).toHaveLength(2);
    expect(daily.getAttribute("aria-invalid")).toBe("true");
    expect(text()).toContain("上限只收数字（美元）。");
    expect(ewNotices()).toEqual(["上限只收数字（美元）。"]);
    change(daily, "");
    await flush();
    expect(f.log.slice(2)).toEqual(["PATCH /usage/settings", "GET /usage"]);
    expect(f.bodies[2]).toEqual({ daily_limit_usd: null });
    expect(daily.hasAttribute("aria-invalid")).toBe(false);
    expect(daily.value).toBe("");
  });

  it("a failed PATCH: the reason, the old value back, no retry", async () => {
    const f = open();
    await flush();
    f.queue("PATCH /usage/settings", { status: 503, text: '{"detail":"预算保存失败，请重试"}' });
    const daily = input(USAGE_COPY.daily);
    change(daily, "5");
    await flush();
    expect(f.log.slice(2)).toEqual(["PATCH /usage/settings"]);
    expect(daily.value).toBe("2");
    expect(text()).toContain("预算保存失败，请重试");
    expect(text()).not.toContain("切换模型");
    expect(ewNotices()).toEqual(["预算保存失败，请重试"]);
    expect(daily.disabled).toBe(false);
    const monthly = input(USAGE_COPY.monthly);
    f.queue("PATCH /usage/settings", new ApiError(500, "x"));
    change(monthly, "30");
    await flush();
    expect(f.log.slice(2)).toEqual(["PATCH /usage/settings", "PATCH /usage/settings"]);
    expect(monthly.value).toBe("");
  });

  it("offline PATCH: 未连接到服务器", async () => {
    const f = open();
    await flush();
    f.queue("PATCH /usage/settings", new OfflineError());
    change(input(USAGE_COPY.monthly), "10");
    await flush();
    expect(f.log.slice(2)).toEqual(["PATCH /usage/settings"]);
    expect(text()).toContain("未连接到服务器");
    expect(input(USAGE_COPY.monthly).value).toBe("");
  });

  it("while a PATCH runs, everything is disabled (single flight)", async () => {
    const f = open();
    await flush();
    let release: (r: Answer) => void = () => undefined;
    f.queue("PATCH /usage/settings", new Promise<Answer>((r) => { release = r; }));
    change(input(USAGE_COPY.daily), "4");
    await flush();
    const controls = [...el.querySelectorAll<HTMLInputElement>("input")];
    expect(controls.length).toBe(2);
    for (const c of controls) expect(c.disabled).toBe(true);
    expect(text()).toContain("正在保存…");
    change(input(USAGE_COPY.monthly), "9");
    await flush();
    expect(f.log.slice(2)).toEqual(["PATCH /usage/settings"]);
    release({ status: 200, text: JSON.stringify({ ...SETTINGS_FIXTURE, daily_limit_usd: 4 }) });
    await flush();
    for (const c of controls) expect(c.disabled).toBe(false);
    expect(input(USAGE_COPY.daily).value).toBe("4");
    expect(input(USAGE_COPY.monthly).value).toBe("");
    expect(f.log.slice(2)).toEqual(["PATCH /usage/settings", "GET /usage"]);
  });

  it("a failed read: the reason and 重新读取 (one click, one read)", async () => {
    const f = open(true, (x) => x.queue("GET /usage", new OfflineError()));
    await flush();
    expect(text()).toContain("未连接到服务器");
    expect(f.log).toEqual(["GET /usage", "GET /usage/settings"]);
    const btn = [...el.querySelectorAll("button")].find((b) => b.textContent === "重试");
    if (!btn) throw new Error("no 重新读取");
    btn.click();
    await flush();
    expect(f.log.slice(2)).toEqual(["GET /usage", "GET /usage/settings"]);
    expect(text()).toContain("今天花了 $0.12 · 本月 $3.40");
  });

  it("unconfigured: no request", async () => {
    const f = open(false);
    await flush();
    expect(f.log).toEqual([]);
    expect(text()).toContain("先在上面填服务器地址和用户名。");
    expect(el.querySelector("input, select")).toBeNull();
  });

  it("dispose: a late answer draws nothing", async () => {
    const f = open();
    f.sec.dispose();
    await flush();
    expect(f.log).toEqual(["GET /usage", "GET /usage/settings"]);
    expect(el.querySelector("input")).toBeNull();
  });

  it("a first budget remains unconfirmed while editing, and requires its explicit confirmation", async () => {
    const f = open(true, (x) => x.useSettings({ ...SETTINGS_FIXTURE, budget_confirmed: false,
      daily_limit_usd: 5, monthly_limit_usd: 30 }));
    await flush();
    const confirm = [...el.querySelectorAll("button")].find((b) => b.textContent === USAGE_COPY.confirmBudget)!;
    expect(confirm.hidden).toBe(false);
    expect(input(USAGE_COPY.daily).value).toBe("5");
    expect(input(USAGE_COPY.monthly).value).toBe("30");
    change(input(USAGE_COPY.daily), "6");
    await flush();
    expect(confirm.hidden).toBe(false);
    confirm.click();
    await flush();
    expect(f.bodies.filter((_, i) => f.log[i] === "PATCH /usage/settings")).toEqual([
      { daily_limit_usd: 6 }, { confirm_budget: true }]);
    expect(confirm.hidden).toBe(true);
  });

  it("a failed first confirmation keeps the budget form and enables an explicit retry", async () => {
    const f = open(true, (x) => x.useSettings({ ...SETTINGS_FIXTURE, budget_confirmed: false }));
    await flush();
    const confirm = [...el.querySelectorAll("button")].find((b) => b.textContent === USAGE_COPY.confirmBudget)!;
    f.queue("PATCH /usage/settings", new OfflineError());
    confirm.click();
    await flush();
    expect(confirm.hidden).toBe(false);
    expect(confirm.disabled).toBe(false);
    expect(text()).toContain("未连接到服务器");
    expect(f.log.filter((x) => x === "PATCH /usage/settings")).toHaveLength(1);
    confirm.click();
    await flush();
    expect(confirm.hidden).toBe(true);
  });

  it("shows unpriced calls even when their recorded dollar cost is zero", async () => {
    const usage = structuredClone(USAGE_FIXTURE);
    for (const period of [usage.today, usage.month]) {
      period.total_usd = 0;
      for (const group of Object.values(period.by_group)) group.cost_usd = 0;
    }
    usage.month.by_group.assistant!.unpriced_calls = 2;
    usage.month.by_group.assistant!.cost_usd = 0;
    open(true, (f) => f.queue("GET /usage", { status: 200, text: JSON.stringify(usage) }));
    await flush();
    expect(text()).toContain(USAGE_COPY.unpriced(2));
    expect(text()).not.toContain(USAGE_COPY.noSpend);
  });
});
