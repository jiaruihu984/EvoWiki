import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { clearEwNotices, ewNotices } from "../../test/ew-notices";
import { ApiError } from "../api/errors";
import { flush, methods, setup } from "./controller-test-kit";
import type { WikiController } from "./controller";
import { mountPageLimitSetting, type PageLimitHost } from "./page-limit-setting";

let el: HTMLElement;
beforeEach(() => { el = document.createElement("div"); document.body.append(el); });
afterEach(() => { el.remove(); clearEwNotices(); });

/** The plugin's pageLimitHost on a fake wiki controller. */
const hostOf = (c: WikiController): PageLimitHost => ({
  state: () => c.state, subscribe: (fn) => c.subscribe(fn), setPageLimit: (v) => c.setPageLimit(v),
  load: () => { const k = c.state.catalog.kind; if (k === "idle" || k === "error") void c.init(); },
});
const input = (): HTMLInputElement => {
  const x = el.querySelector<HTMLInputElement>('input[aria-label="每页最多花（美元）"]');
  if (!x) throw new Error("no input");
  return x;
};
const status = (): string => el.querySelector(".ew-srow .ew-muted")?.textContent ?? "";
const type = (v: string): void => { input().value = v; input().dispatchEvent(new Event("change")); };
const settle = async () => { for (let i = 0; i < 4; i++) await flush(); };
const limit = (c: WikiController): number | null | undefined =>
  (c.state.catalog.kind === "ready" ? c.state.catalog.value.settings.page_limit_usd : undefined);

describe("用量 · 每页最多花（美元） (settings row)", () => {
  it("an .ew-srow like 每天最多花: name, a decimal text input; empty while there is no cap", async () => {
    const { c } = setup();
    mountPageLimitSetting(el, hostOf(c));
    expect(el.querySelector(".ew-srow > div > .ew-n")?.textContent).toBe("每页最多花（美元）");
    expect(input().inputMode).toBe("decimal");
    await settle();
    expect(input().disabled).toBe(false);
    expect(input().value).toBe("");
  });

  it("a number: PUT {page_limit_usd} then the catalog; 已保存。; empty again sends null", async () => {
    const { c, api } = setup();
    await c.init();
    api.calls.length = 0;
    mountPageLimitSetting(el, hostOf(c));
    type("0.5");
    await settle();
    expect(methods(api)).toEqual(["putSettings", "catalog"]);
    expect(api.calls[0]?.args).toEqual([{ page_limit_usd: 0.5 }]);
    expect(limit(c)).toBe(0.5);
    expect(input().value).toBe("0.5");
    expect(ewNotices()).toEqual(["已保存。"]);
    type("");
    await settle();
    expect(api.calls[api.calls.length - 2]?.args).toEqual([{ page_limit_usd: null }]);
    expect(limit(c)).toBeNull();
  });

  it("not a number, or under 0.05: flagged, the reason, nothing sent (no upper bound)", async () => {
    const { c, api } = setup();
    await c.init();
    mountPageLimitSetting(el, hostOf(c));
    api.calls.length = 0;
    for (const bad of ["abc", "-1", "0.01"]) {
      type(bad);
      await settle();
      expect(input().getAttribute("aria-invalid")).toBe("true");
      expect(status()).toBe("每页上限至少 0.05 美元。");
    }
    expect(methods(api)).toEqual([]);
  });

  it("a refused save: the input goes back to the server's value, the row says why", async () => {
    const { c, api } = setup();
    await c.init();
    mountPageLimitSetting(el, hostOf(c));
    api.failNext("putSettings", new ApiError(500, "boom"));
    type("1");
    await settle();
    expect(input().value).toBe("");
    expect(status()).not.toBe("");
    expect(limit(c)).toBeNull();
  });

  it("the demo wiki: shown, disabled, nothing sent", async () => {
    const { c, api } = setup({ demo: true });
    mountPageLimitSetting(el, hostOf(c));
    await settle();
    expect(input().disabled).toBe(true);
    type("1");
    await settle();
    expect(methods(api).filter((m) => m === "putSettings")).toEqual([]);
  });
});
