import { setUiLanguage } from "../i18n";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { clearEwNotices, ewNotices } from "../../test/ew-notices";
import { ApiError } from "../api/errors";
import { flush, methods, setup } from "./controller-test-kit";
import type { WikiController } from "./controller";
import { type LanguageHost, mountLanguageSetting } from "./language-setting";

let el: HTMLElement;
beforeEach(() => { setUiLanguage("zh"); el = document.createElement("div"); document.body.append(el); });
afterEach(() => { el.remove(); clearEwNotices(); });

/** The plugin's languageHost on a fake wiki controller. */
const hostOf = (c: WikiController): LanguageHost => ({
  state: () => c.state, subscribe: (fn) => c.subscribe(fn), setLanguage: (l) => c.setLanguage(l),
  load: () => { const k = c.state.catalog.kind; if (k === "idle" || k === "error") void c.init(); },
});
const select = (): HTMLSelectElement => {
  const x = el.querySelector<HTMLSelectElement>('select[data-field="language"]');
  if (!x) throw new Error("no select");
  return x;
};
const status = (): string => el.querySelector(".ew-srow .ew-muted")?.textContent ?? "";
const choose = (v: string): void => { select().value = v; select().dispatchEvent(new Event("change")); };
const settle = async () => { for (let i = 0; i < 4; i++) await flush(); };

describe("语言 (settings row)", () => {
  it("app.js row: .ew-srow name / description, 简体中文 / English (zh / en); reads the catalog when not loaded and shows its value", async () => {
    const { c, api } = setup();
    mountLanguageSetting(el, hostOf(c));
    expect(el.querySelector(".ew-srow > div > .ew-n")?.textContent).toBe("语言");
    expect(el.querySelector(".ew-srow")?.lastElementChild).toBe(select());
    expect([...select().options].map((o) => [o.value, o.textContent])).toEqual([["zh", "简体中文"], ["en", "English"]]);
    expect(select().disabled).toBe(true);
    await settle();
    expect(methods(api)[0]).toBe("catalog");
    expect(select().value).toBe("zh");
    expect(select().disabled).toBe(false);
    await api.putSettings({ language: "en" });
    await c.init();
    expect(select().value).toBe("en");
  });

  it("a change: PUT exactly {language} (no review_per_day), then use the authoritative response; 已保存。", async () => {
    const { c, api } = setup();
    await c.init();
    api.calls.length = 0;
    mountLanguageSetting(el, hostOf(c));
    expect(methods(api)).toEqual([]);              // a loaded catalog is not read again
    choose("en");
    expect(select().disabled).toBe(true);
    await settle();
    expect(methods(api)).toEqual(["putSettings"]);
    expect(api.calls[0]?.args).toEqual([{ language: "en" }]);
    expect(c.state.catalog.kind === "ready" && c.state.catalog.value.settings.language).toBe("en");
    expect(select().value).toBe("en");
    expect(status()).toBe("");
    expect(ewNotices()).toEqual(["Saved."]);
  });

  it("the demo wiki: the value is shown, the dropdown is disabled and the row says changes are not possible (finding 5)", async () => {
    const { c, api } = setup({ demo: true });
    mountLanguageSetting(el, hostOf(c));
    await settle();
    expect(select().disabled).toBe(true);
    expect(status()).toBe("现在开着演示数据，这里不能改；关掉演示后再改。");
    choose("en");                                      // even if a change event arrives: nothing sent
    await settle();
    expect(methods(api).filter((m) => m === "putSettings")).toEqual([]);
    expect(select().value).toBe("zh");
  });

  it("while the catalog is read: its own 正在读取… line", () => {
    const { c } = setup({ delayMs: 50 });
    mountLanguageSetting(el, hostOf(c));
    expect(status()).toBe("正在读取…");
  });

  it("a failure: the dropdown goes back, the row says why (never the detail)", async () => {
    const { c, api } = setup();
    await c.init();
    mountLanguageSetting(el, hostOf(c));
    api.failNext("putSettings", new ApiError(422, "Input should be 'zh' or 'en'"));
    choose("en");
    await settle();
    expect(methods(api).slice(-1)).toEqual(["putSettings"]);
    expect(select().value).toBe("zh");
    expect(select().disabled).toBe(false);
    expect(status()).not.toContain("Input should");
    expect(status()).not.toBe("");
    expect(status()).not.toBe("已保存。");
    expect(ewNotices()).toEqual([status()]);           // the failure is said in the row and as a notice
  });

  it("unconfigured: needsSettings, nothing sent", async () => {
    const { c, api } = setup({ configured: false });
    mountLanguageSetting(el, hostOf(c));
    await settle();
    expect(status()).toBe("先在上面填服务器地址和用户名。");
    expect(select().disabled).toBe(true);
    expect(methods(api).filter((m) => m === "putSettings")).toEqual([]);
  });
});

describe("WikiController.setLanguage / setReviewPerDay bodies", () => {
  it("setLanguage: unchanged sends nothing; no catalog → a reason, nothing sent", async () => {
    const { c, api } = setup();
    expect(await c.setLanguage("en")).toBe("没能读到，重试");
    expect(methods(api)).toEqual([]);
    await c.init();
    api.calls.length = 0;
    expect(await c.setLanguage("zh")).toBeNull();
    expect(methods(api)).toEqual([]);
  });

  it("setReviewPerDay sends {review_per_day}, never language", async () => {
    const { c, api } = setup();
    await c.init();
    api.calls.length = 0;
    await c.setReviewPerDay(5);
    const put = api.calls.find((x) => x.method === "putSettings");
    expect(put?.args).toEqual([{ review_per_day: 5 }]);
    expect(Object.keys(put?.args[0] as object)).not.toContain("language");
  });
});
