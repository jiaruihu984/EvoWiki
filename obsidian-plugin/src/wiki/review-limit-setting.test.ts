import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { clearEwNotices, ewNotices } from "../../test/ew-notices";
import { flush, methods, setup } from "./controller-test-kit";
import type { WikiController } from "./controller";
import { mountReviewLimitSetting, type ReviewLimitHost } from "./review-limit-setting";

let el: HTMLElement;
beforeEach(() => { el = document.createElement("div"); document.body.append(el); });
afterEach(() => { el.remove(); clearEwNotices(); });

/** The plugin's reviewLimitHost on a fake wiki controller. */
const hostOf = (c: WikiController): ReviewLimitHost => ({
  state: () => c.state, subscribe: (fn) => c.subscribe(fn), setReviewPerDay: (n) => void c.setReviewPerDay(n),
  load: () => { const k = c.state.catalog.kind; if (k === "idle" || k === "error") void c.init(); },
});
const box = (): HTMLInputElement => {
  const x = el.querySelector<HTMLInputElement>('input[aria-label="每天最多复习几张"]');
  if (!x) throw new Error("no box");
  return x;
};
const status = (): string => el.querySelector(".ew-srow .ew-muted")?.textContent ?? "";
const change = (v: string): void => { box().value = v; box().dispatchEvent(new Event("change")); };

describe("每天最多复习几张 (settings row)", () => {
  it("app.js row: .ew-srow (.ew-n + a text input); reads the catalog when not loaded, shows its review_per_day", async () => {
    const { c, api } = setup();
    const sec = mountReviewLimitSetting(el, hostOf(c));
    expect(el.querySelector(".ew-srow > div > .ew-n")?.textContent).toBe("每天最多复习几张");
    expect(el.querySelector(".ew-srow")?.lastElementChild).toBe(box());
    expect(box().disabled).toBe(true);
    expect(status()).toBe("正在读取…");
    await flush(); await flush();
    expect(methods(api)[0]).toBe("catalog");
    expect([box().type, box().value]).toEqual(["text", "10"]);
    expect(box().disabled).toBe(false);
    expect(status()).toBe("");
    sec.dispose();
  });

  it("a change goes through setReviewPerDay (one PUT, then catalog + today) and shows its own result", async () => {
    const { c, api } = setup();
    await c.init();
    api.calls.length = 0;
    mountReviewLimitSetting(el, hostOf(c));
    expect(methods(api)).toEqual([]);            // a loaded catalog is not read again
    change("4");
    await flush(); await flush(); await flush();
    expect(methods(api)).toEqual(["putSettings", "catalog", "reviewToday"]);
    expect(api.calls[0]?.args).toEqual([{ review_per_day: 4 }]);
    expect(box().value).toBe("4");
    expect(status()).toBe("");                     // saved: told as the demo's notice, no line left in the row
    expect(ewNotices()).toEqual(["已保存。"]);
  });

  it("invalid (blank, 1.5, -1): nothing sent, the controller's reason shows", async () => {
    const { c, api } = setup();
    await c.init();
    mountReviewLimitSetting(el, hostOf(c));
    for (const bad of ["", "1.5", "-1"]) {
      api.calls.length = 0;
      change(bad);
      await flush();
      expect(methods(api), bad).toEqual([]);
      expect(status(), bad).toBe("每天复习张数要填 0 或更大的整数。");
      expect(box().getAttribute("aria-invalid"), bad).toBe("true");
      expect(ewNotices().pop(), bad).toBe("每天复习张数要填 0 或更大的整数。");
    }
  });

  it("a review note from elsewhere (before any change here) is not shown; offline disables the box", async () => {
    const { c, conn } = setup();
    await c.init();
    await c.setReviewPerDay(5);                  // e.g. an earlier change: its note is not this row's
    mountReviewLimitSetting(el, hostOf(c));
    expect(status()).toBe("");
    expect(ewNotices()).toEqual([]);
    expect(box().value).toBe("5");
    conn.emit("offline");
    expect(box().disabled).toBe(true);
  });

  it("unconfigured: says to fill the server and user first; dispose stops following the controller", async () => {
    const { c, api } = setup({ configured: false });
    const sec = mountReviewLimitSetting(el, hostOf(c));
    await flush();
    expect(methods(api)).toEqual([]);
    expect(status()).toBe("先在上面填服务器地址和用户名。");
    expect(box().disabled).toBe(true);
    sec.dispose();
    const before = status();
    c.setTab("review");
    expect(status()).toBe(before);
  });
});
