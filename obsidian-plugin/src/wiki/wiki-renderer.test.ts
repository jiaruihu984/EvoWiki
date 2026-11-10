import { afterEach, describe, expect, it } from "vitest";
import { WIKI_COPY } from "./copy";
import { RSI_CHANGES } from "./fixtures";
import { btn, btns, fakeActions, fakeCtx } from "./render-test-kit";
import type { CatalogOut, RsiChangeOut } from "./schema";
import { INITIAL_WIKI_STATE, type WikiState } from "./state";
import { WikiRenderer } from "./wiki-renderer";

afterEach(() => { document.body.replaceChildren(); });

const CATALOG: CatalogOut = {
  learning: [], shelf: { pages: [] },
  settings: { review_per_day: 10, page_limit_usd: null, language: "zh" }, current_page: null, names: {}, paths: [], maps: [],
};
const base: WikiState = { ...INITIAL_WIKI_STATE, conn: "online", catalog: { kind: "ready", value: CATALOG } };
const mount = (assistant: HTMLElement | null = null): { r: WikiRenderer; a: ReturnType<typeof fakeActions> } => {
  const a = fakeActions();
  const r = new WikiRenderer(a, () => fakeCtx(), assistant);
  document.body.append(r.root);
  return { r, a };
};
const body = (r: WikiRenderer): HTMLElement => r.root.querySelector<HTMLElement>(".ew-side-body")!;
const tabs = (r: WikiRenderer): HTMLButtonElement[] => btns(r.root, ".ew-seg > button");

describe("WikiRenderer: the shell (shell.html aside.side)", () => {
  it(".ew > aside.ew-side (no aria-label: no hover tooltip) > .ew-side-head (.ew-brand star + EvoWiki, .ew-seg tablist) + the min-height:0 / min-width:0 slot", () => {
    const { r } = mount();
    r.update({ ...base, tab: "today" });
    expect(r.root.className).toBe("ew");
    const side = r.root.firstElementChild as HTMLElement;
    expect([side.tagName, side.className, side.getAttribute("aria-label")]).toEqual(["ASIDE", "ew-side", null]);
    const [head, slot] = [...side.children] as HTMLElement[];
    expect(head!.className).toBe("ew-side-head");
    const brand = head!.querySelector(".ew-brand")!;
    expect(brand.textContent).toBe("EvoWiki");
    expect(brand.querySelector("svg")?.getAttribute("viewBox")).toBe("0 0 24 24");
    expect(brand.querySelector("svg path")?.getAttribute("d")).toBe("M12 3l2.6 5.6 6 .7-4.5 4.1 1.2 6L12 16.5 6.7 19.4l1.2-6L3.4 9.3l6-.7z");
    const seg = head!.querySelector(".ew-seg")!;
    expect(seg.getAttribute("role")).toBe("tablist");
    expect(slot!.getAttribute("style")).toBe("min-height: 0; min-width: 0;");   // min-width: a long prompt line must not widen the sidebar
    expect(slot!.firstElementChild).toBe(body(r));
  });

  it("three tabs 今天 / 助手 / 个性化 (type=button role=tab aria-selected); a click → setTab", () => {
    const { r, a } = mount();
    r.update({ ...base, tab: "today" });
    expect(tabs(r).map((t) => [t.textContent, t.type, t.getAttribute("role"), t.getAttribute("aria-selected")]))
      .toEqual([["今天", "button", "tab", "true"], ["助手", "button", "tab", "false"], ["个性化", "button", "tab", "false"]]);
    tabs(r)[2]!.click();
    expect(a.setTab).toHaveBeenCalledWith("engine");
    r.update({ ...base, tab: "engine" });
    expect(tabs(r).map((t) => t.getAttribute("aria-selected"))).toEqual(["false", "false", "true"]);
    expect(body(r).className).toBe("ew-side-body ew-pz");
  });

  it("the red counts: label, then .ew-cnt with the number (> 0) and an aria-label; 助手 never", () => {
    const { r } = mount();
    r.update({ ...base, tab: "assistant" }, { today: 2, engine: 0, log: 0 });
    expect(tabs(r).map((t) => t.querySelector(".ew-cnt")?.textContent ?? null)).toEqual(["2", null, null]);
    expect(tabs(r).map((t) => t.getAttribute("aria-label"))).toEqual(["今天，2 项新的", "助手", "个性化"]);
    expect(tabs(r)[0]!.firstChild?.textContent).toBe("今天");
    r.update({ ...base, tab: "today" }, { today: 0, engine: 3, log: 0 });
    expect(tabs(r).map((t) => t.querySelector(".ew-cnt")?.textContent ?? null)).toEqual([null, null, "3"]);
    expect(tabs(r).map((t) => t.getAttribute("aria-label"))).toEqual(["今天", "助手", "个性化，3 项新的"]);
    expect(tabs(r)[2]!.className).toBe("");
  });

  it("the header nodes are built once (a focused tab keeps its focus across updates)", () => {
    const { r } = mount();
    r.update(base);
    const t = tabs(r)[1]!;
    t.focus();
    r.update({ ...base, conn: "offline" });
    expect(tabs(r)[1]).toBe(t);
    expect(document.activeElement).toBe(t);
  });
});

describe("WikiRenderer: the 助手 tab (the assistant's own persistent panel)", () => {
  it("the panel root sits in the slot inside .ew; shown only on 助手 (no wiki body then); never rebuilt", () => {
    const panel = document.createElement("div");
    panel.className = "ew-asst";
    const input = document.createElement("textarea");
    panel.append(input);
    const { r } = mount(panel);
    r.update({ ...base, tab: "today" });
    expect(panel.parentElement?.parentElement?.className).toBe("ew-side");
    expect(panel.closest(".ew")).toBe(r.root);
    expect(panel.style.display).toBe("none");
    r.update({ ...base, tab: "assistant" });
    expect(panel.style.display).toBe("");
    expect(body(r)).toBeNull();
    input.value = "我在打字";
    input.focus();
    input.setSelectionRange(2, 2);
    r.update({ ...base, tab: "assistant", conn: "offline" });
    expect(panel.querySelector("textarea")).toBe(input);
    expect(input.value).toBe("我在打字");
    expect(document.activeElement).toBe(input);
    r.update({ ...base, tab: "today" });
    expect(panel.isConnected).toBe(true);
    expect(panel.style.display).toBe("none");
  });

  it("without a panel the 助手 tab is just empty", () => {
    const { r } = mount();
    r.update({ ...base, tab: "assistant" });
    expect(body(r)).toBeNull();
  });
});

describe("WikiRenderer: re-render keeps focus, caret and scroll (no sidebar <details> since Prompt 设置 moved to its own view)", () => {
  const PM1 = RSI_CHANGES.find((c) => c.patch_id === "pm1") as RsiChangeOut;
  const withPm1: WikiState = { ...base, tab: "engine", rsi: { kind: "ready", value: [PM1] }, personal: { ...base.personal, log: true, older: true } };

  it("a focused 撤销 in 个性化 · 活动记录 keeps focus across update()", () => {
    const { r } = mount();
    r.update(withPm1);
    btn(r.root, "[data-action=rsi-undo]").focus();
    r.update({ ...withPm1, conn: "online" });
    expect(document.activeElement).toBe(btn(r.root, "[data-action=rsi-undo]"));
  });

  it("a focused 重试 button keeps focus; the body's scroll position stays on the same tab", () => {
    const { r } = mount();
    const s: WikiState = { ...base, review: { ...base.review, today: { kind: "error", message: "x" } } };
    r.update(s);
    const retry = btn(r.root, "[data-role=next-error] button");
    retry.focus();
    body(r).scrollTop = 40;
    r.update({ ...s });
    expect(document.activeElement?.textContent).toBe(WIKI_COPY.retry);
    expect(document.activeElement).toBe(retry);
    expect(body(r).scrollTop).toBe(40);
  });
});

describe("WikiRenderer: offline is one line at the top of the sidebar (今天 / 助手 / 个性化)", () => {
  const e = { kind: "error" as const, message: WIKI_COPY.offline };
  // Every read of every tab failed.
  const failed: WikiState = {
    ...base, catalog: e, k: e, rsi: e, engine: e, prefs: e, prompts: e, review: { ...base.review, today: e },
    bot: { ...base.bot, today: e, actions: e, recent: e, settings: e },
  };
  const banners = (r: WikiRenderer): HTMLElement[] => [...r.root.querySelectorAll<HTMLElement>("[data-role=offline]")];
  const count = (r: WikiRenderer, t: string): number => (r.root.textContent ?? "").split(t).length - 1;

  it("offline: exactly one 未连接到服务器 · 重试 line, in the header, on every tab; no section says anything about its failed read", () => {
    const assistant = document.createElement("div");
    const { r } = mount(assistant);
    for (const tab of ["today", "assistant", "engine"] as const) {
      r.update({ ...failed, conn: "offline", tab });
      expect(banners(r), tab).toHaveLength(1);
      const b = banners(r)[0]!;
      expect(b.closest(".ew-side-head"), tab).not.toBeNull();
      expect([b.className, b.textContent]).toEqual(["ew-muted", "未连接到服务器 重试"]);
      expect(btn(b, "[data-action=retry-connection]").className).toBe("ew-btn ew-link");
      expect(count(r, WIKI_COPY.notConnected), tab).toBe(1);
      expect(r.root.textContent, tab).not.toContain("没能读到");
      expect(r.root.textContent, tab).not.toContain("你的笔记不受影响");
    }
  });

  it("重试 asks for everything to be read again (one action)", () => {
    const { r, a } = mount();
    r.update({ ...failed, conn: "offline", tab: "engine" });
    btn(banners(r)[0]!, "button").click();
    expect(a.retryConnection).toHaveBeenCalledTimes(1);
  });

  it("back online: the banner goes; a read that still failed is one short 没能读到，重试 line in its own section", () => {
    const { r } = mount();
    r.update({ ...failed, conn: "offline", tab: "today" });
    expect(banners(r)).toHaveLength(1);
    r.update({ ...failed, conn: "online", tab: "today" });
    expect(banners(r)).toHaveLength(0);
    expect(r.root.textContent).not.toContain(WIKI_COPY.notConnected);
    const today = [...r.root.querySelectorAll<HTMLElement>("[data-section=next] [data-role=next-error]")];
    expect(today.map((x) => x.textContent)).toEqual(["没能读到，重试"]);
    // 我的学习偏好 / 活动记录 / 资料 opened (folded by default): their failed reads say so inside
    r.update({ ...failed, conn: "online", tab: "engine", personal: { ...failed.personal, rules: true, log: true, sources: true } });
    expect(banners(r)).toHaveLength(0);
    const lines = [...body(r).querySelectorAll<HTMLElement>(".ew-pz-box .ew-muted:has(> button), .ew-pz-log > .ew-muted:has(> button)")].map((x) => x.textContent);
    expect(lines.length).toBeGreaterThan(0);
    expect(lines.every((t) => t === "没能读到，重试")).toBe(true);
    // Everything read: nothing at all.
    r.update({ ...base, conn: "online", tab: "today" });
    expect(r.root.textContent).not.toContain("没能读到");
  });
});


it("unrelated controller refreshes retain Today input, mini canvas and goal rows in the document", () => {
  const a = fakeActions(), mini = document.createElement("div"), canvas = document.createElement("canvas"), goals = document.createElement("div");
  mini.append(canvas);
  const line = document.createElement("div");
  const r = new WikiRenderer(a, () => fakeCtx(), null, { root: mini, goals: { root: goals, line }, sync: () => undefined });
  document.body.append(r.root);
  r.update({ ...base, tab: "today" });
  const input = r.root.querySelector<HTMLInputElement>('[data-action="learn-topic"]')!;
  input.value = "unfinished topic"; input.focus(); input.setSelectionRange(3, 7);
  const oldBody = body(r), next = r.root.querySelector('[data-section="next"]');
  const mutations: MutationRecord[] = [];
  const observer = new MutationObserver((records) => mutations.push(...records));
  observer.observe(oldBody, { childList: true, subtree: true });
  r.update({ ...base, tab: "today", learnTopic: input.value });
  mutations.push(...observer.takeRecords()); observer.disconnect();
  expect(body(r)).toBe(oldBody);
  expect(r.root.querySelector('[data-action="learn-topic"]')).toBe(input);
  expect([input.value, input.selectionStart, input.selectionEnd]).toEqual(["unfinished topic", 3, 7]);
  expect(r.root.contains(canvas)).toBe(true); expect(r.root.contains(goals)).toBe(true);
  expect(r.root.querySelector('[data-section="next"]')).toBe(next);
  expect(mutations).toHaveLength(0);
});


it("Today updates busy, configuration and read errors while retaining unaffected sections", () => {
  const { r } = mount();
  const ready = { ...base, tab: "today" as const, configured: true };
  r.update(ready);
  const oldBody = body(r), learn = r.root.querySelector('[data-section="learn"]');
  r.update({ ...ready, busy: { what: "organize", abortable: true } });
  expect(body(r)).toBe(oldBody);
  expect(r.root.querySelector('[data-section="learn"]')).toBe(learn);
  r.update({ ...ready, configured: false });
  expect(r.root.querySelector<HTMLButtonElement>('[data-action="learn-start"]')!.disabled).toBe(true);
  expect(r.root.querySelector('[data-section="next"]')!.textContent).toContain(WIKI_COPY.needsSettings);
  r.update({ ...ready, catalog: { kind: "error", message: "failed" } });
  expect(r.root.querySelector('[data-section="next"] [data-action="retry"]')).not.toBeNull();
  expect(body(r)).toBe(oldBody);
});
