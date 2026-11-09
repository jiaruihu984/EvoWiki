import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ewNotice, NOTICE_ACTION_MS, NOTICE_MS } from "./ew-notice";

describe("ewNotice (app.js notice + shell .notices/.notice)", () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); document.body.replaceChildren(); });

  it("puts one div.ew.ew-layer on body holding div.ew-notices aria-live=polite, reused by later notices", () => {
    ewNotice("一");
    ewNotice("二");
    const layers = document.body.querySelectorAll(":scope > div.ew.ew-layer");
    expect(layers.length).toBe(1);
    const host = layers[0]!.firstElementChild!;
    expect(host.className).toBe("ew-notices");
    expect(host.getAttribute("aria-live")).toBe("polite");
    expect([...host.children].map((n) => n.textContent)).toEqual(["一", "二"]);
  });
  it("a plain notice is div.ew-notice > span and leaves after 4200 ms", () => {
    const n = ewNotice("已记下");
    expect(n.className).toBe("ew-notice");
    expect(n.children.length).toBe(1);
    expect(n.firstElementChild!.tagName).toBe("SPAN");
    expect(n.firstElementChild!.hasAttribute("class")).toBe(false);
    vi.advanceTimersByTime(NOTICE_MS - 1);
    expect(n.isConnected).toBe(true);
    vi.advanceTimersByTime(1);
    expect(n.isConnected).toBe(false);
    expect(NOTICE_MS).toBe(4200);
  });
  it("with an action: a button.ew-lk type=button; stays 7000 ms; clicking runs it and removes the notice", () => {
    const run = vi.fn();
    const n = ewNotice("已删除", { label: "撤销", run });
    const a = n.querySelector("button")!;
    expect(a.className).toBe("ew-lk");
    expect(a.type).toBe("button");
    expect(a.textContent).toBe("撤销");
    vi.advanceTimersByTime(NOTICE_MS);
    expect(n.isConnected).toBe(true);
    vi.advanceTimersByTime(NOTICE_ACTION_MS - NOTICE_MS);
    expect(n.isConnected).toBe(false);
    expect(NOTICE_ACTION_MS).toBe(7000);

    const m = ewNotice("已删除", { label: "撤销", run });
    m.querySelector("button")!.click();
    expect(run).toHaveBeenCalledTimes(1);
    expect(m.isConnected).toBe(false);
  });
  it("re-attaches the layer if something removed it from body", () => {
    ewNotice("一");
    document.body.replaceChildren();
    const n = ewNotice("二");
    expect(n.isConnected).toBe(true);
    expect(document.body.querySelectorAll(".ew-layer").length).toBe(1);
  });
  it("several actions: one .ew-lk per action in order, each runs only its own callback and removes the notice; 7000 ms", () => {
    const a = vi.fn(), b = vi.fn();
    const n = ewNotice("偏好已改", [{ label: "查看", run: a }, { label: "撤回", run: b }]);
    const lks = [...n.querySelectorAll("button.ew-lk")] as HTMLButtonElement[];
    expect(lks.map((x) => x.textContent)).toEqual(["查看", "撤回"]);
    expect(lks.every((x) => x.type === "button" && !x.hasAttribute("style"))).toBe(true);
    vi.advanceTimersByTime(NOTICE_ACTION_MS - 1);
    expect(n.isConnected).toBe(true);
    vi.advanceTimersByTime(1);
    expect(n.isConnected).toBe(false);

    const m = ewNotice("偏好已改", [{ label: "查看", run: a }, { label: "撤回", run: b }]);
    (m.querySelectorAll("button")[1] as HTMLButtonElement).click();
    expect(a).not.toHaveBeenCalled();
    expect(b).toHaveBeenCalledTimes(1);
    expect(m.isConnected).toBe(false);
    const k = ewNotice("偏好已改", [{ label: "查看", run: a }, { label: "撤回", run: b }]);
    (k.querySelectorAll("button")[0] as HTMLButtonElement).click();
    expect(a).toHaveBeenCalledTimes(1);
    expect(k.isConnected).toBe(false);
  });
  it("an empty action list behaves like no action (4200 ms)", () => {
    const n = ewNotice("x", []);
    expect(n.querySelector("button")).toBeNull();
    vi.advanceTimersByTime(NOTICE_MS);
    expect(n.isConnected).toBe(false);
  });
});
