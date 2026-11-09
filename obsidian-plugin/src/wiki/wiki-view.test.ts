import { describe, expect, it, vi } from "vitest";
import type { WorkspaceLeaf } from "obsidian";
import { firstPage, flush, methods, setup } from "./controller-test-kit";
import { fakeActions, fakeCtx } from "./render-test-kit";
import { RSI_CHANGES } from "./fixtures";
import { type MountedPanel, WikiView } from "./wiki-view";
import { AccountCheck } from "../account";
import { ApiError } from "../api/errors";
import { DEFAULT_SETTINGS } from "../settings";
import { USAGE_COPY, WIKI_COPY } from "./copy";

describe("WikiView: account preparation", () => {
  it("keeps a dismissed budget error and retry visible until explicit preparation succeeds, then unsubscribes on close", async () => {
    const { c } = setup();
    let confirm = (): void => {};
    const prepareUser = vi.fn().mockRejectedValueOnce(new ApiError(403, USAGE_COPY.confirmCancelled, "budget_confirmation_required"))
      .mockImplementationOnce(() => new Promise<void>((resolve) => { confirm = resolve; }))
      .mockResolvedValue(undefined);
    const account = new AccountCheck({
      me: () => Promise.resolve({ user_id: "me", background: "none", condition: "S2", lang: "zh" }),
      createUser: vi.fn(), conn: { track: (p) => p, onChange: () => () => {} },
      settings: () => DEFAULT_SETTINGS, uiLocale: () => "zh", registerFailed: vi.fn(), prepareUser,
    });
    await account.init();
    const actions = fakeActions();
    let retry = Promise.resolve();
    actions.retryConnection.mockImplementation(() => { retry = account.init(); });
    const view = new WikiView({} as WorkspaceLeaf, c, actions, () => fakeCtx(), null, null, null, null, account);
    await view.onOpen();
    await flush();
    const status = (): HTMLElement | null => view.contentEl.querySelector("[data-role=account-status]");
    const slot = (): HTMLElement => view.contentEl.querySelector(".ew-side")!.children[1] as HTMLElement;
    expect(status()?.textContent).toBe(`${USAGE_COPY.confirmCancelled} ${WIKI_COPY.retry}`);
    expect(status()?.getAttribute("role")).toBe("alert");
    expect(slot().hidden).toBe(true);
    c.setTab("engine");
    expect(status()?.textContent).toContain(USAGE_COPY.confirmCancelled);
    status()!.querySelector("button")!.click();
    await flush();
    expect(account.state.kind).toBe("loading");
    expect(status()?.textContent).toBe(WIKI_COPY.loading);
    expect(status()?.querySelector("button")).toBeNull();
    expect(slot().hidden).toBe(true);
    confirm();
    await retry;
    expect(account.state.kind).toBe("ready");
    expect(status()).toBeNull();
    expect(slot().hidden).toBe(false);
    await view.onClose();
    const closed = view.contentEl.querySelector(".ew-side-body");
    await account.init();
    expect(view.contentEl.querySelector(".ew-side-body")).toBe(closed);
    account.dispose();
  });
});

describe("WikiView: the 助手 tab panel", () => {
  it("mounts the panel once per open (factory gets the view); follows only while 助手 is shown; detaches on close", async () => {
    const { c } = setup();
    const open = vi.fn();
    const close = vi.fn();
    const panel: MountedPanel = { root: document.createElement("div"), open, close };
    const factory = vi.fn((_v: WikiView) => panel);
    const view = new WikiView({} as WorkspaceLeaf, c, fakeActions(), () => fakeCtx(), factory);
    await view.onOpen();
    expect(factory).toHaveBeenCalledTimes(1);
    expect(factory.mock.calls[0]![0]).toBe(view);
    expect(open).not.toHaveBeenCalled();                  // 本页 is shown: no following, no session read
    c.setTab("assistant");
    expect(open).toHaveBeenCalledTimes(1);
    expect(panel.root.closest(".ew-side")?.parentElement?.parentElement).toBe(view.contentEl);
    c.setTab("assistant");
    expect(open).toHaveBeenCalledTimes(1);
    c.setTab("engine");
    expect(close).toHaveBeenCalledTimes(1);
    c.setTab("assistant");
    expect(open).toHaveBeenCalledTimes(2);
    expect(factory).toHaveBeenCalledTimes(1);
    expect(panel.root.closest(".ew-side")?.parentElement?.parentElement).toBe(view.contentEl);
    await view.onClose();
    expect(close).toHaveBeenCalledTimes(2);
  });

  it("works without a panel factory", async () => {
    const { c } = setup();
    const view = new WikiView({} as WorkspaceLeaf, c, fakeActions(), () => fakeCtx());
    await view.onOpen();
    c.setTab("assistant");
    expect(view.contentEl.querySelector(".ew-side-body")).toBeNull();
    await view.onClose();
  });

  it("tells onClosed, with its controller, when it closes", async () => {
    const { c } = setup();
    const onClosed = vi.fn();
    const view = new WikiView({} as WorkspaceLeaf, c, fakeActions(), () => fakeCtx(), null, onClosed);
    await view.onOpen();
    expect(onClosed).not.toHaveBeenCalled();
    await view.onClose();
    expect(onClosed.mock.calls).toEqual([[c]]);
  });
});

describe("WikiView: 今天 re-reads when a page lands", () => {
  it("a page that opens after the view did re-reads the review list (once), a page already open at open time does not", async () => {
    const { c, api } = setup();
    const view = new WikiView({} as WorkspaceLeaf, c, fakeActions(), () => fakeCtx());
    await view.onOpen();
    await flush();
    api.calls.length = 0;
    await firstPage(c);
    await flush();
    expect(methods(api).filter((m) => m === "reviewToday")).toHaveLength(1);
    await view.onClose();
    const again = new WikiView({} as WorkspaceLeaf, c, fakeActions(), () => fakeCtx());
    api.calls.length = 0;
    await again.onOpen();
    await flush();
    expect(methods(api).filter((m) => m === "reviewToday").length).toBe(0);   // 今天 was already read: init skips it, and a page already open at open time adds no forced re-read
  });

  it("a page that lands during a review is not forgotten: the re-read happens once the review is over", async () => {
    const { c, api } = setup();
    await firstPage(c);
    await c.init();
    await c.reloadReview();
    c.startReview();
    expect(c.state.review.active).toBe(true);
    const view = new WikiView({} as WorkspaceLeaf, c, fakeActions(), () => fakeCtx());
    await view.onOpen();
    await flush();
    api.calls.length = 0;
    (view as unknown as { seenPage: string | null }).seenPage = null;   // as if the open page had just landed
    c.setTab("today");
    await flush();
    expect(methods(api).filter((m) => m === "reviewToday")).toHaveLength(0);   // the cards under the user do not change
    c.pauseReview();
    await flush();
    expect(methods(api).filter((m) => m === "reviewToday")).toHaveLength(1);
    await view.onClose();
  });
});

describe("WikiView: Bot chips load when a page opens", () => {
  const pageChips = (api: { calls: { method: string; args: unknown[] }[] }): unknown[][] =>
    api.calls.filter((x) => x.method === "botSuggestions" && x.args[0] === "page").map((x) => x.args);
  it("botSuggestions(page, id) once for a page that opens after the view did, and once for a page already open", async () => {
    const { c, api } = setup();
    const view = new WikiView({} as WorkspaceLeaf, c, fakeActions(), () => fakeCtx());
    await view.onOpen();
    await flush();
    api.calls.length = 0;
    await firstPage(c);
    await flush();
    expect(pageChips(api)).toHaveLength(1);
    c.setTab("today");
    await flush();
    expect(pageChips(api)).toHaveLength(1);
    await view.onClose();
    api.calls.length = 0;
    const again = new WikiView({} as WorkspaceLeaf, c, fakeActions(), () => fakeCtx());
    await again.onOpen();
    await flush();
    expect(pageChips(api)).toHaveLength(1);
    expect(pageChips(api)[0]?.[1]).toBe(c.state.page.kind === "open" ? c.state.page.page.page_id : undefined);
    await again.onClose();
  });
});

describe("WikiView: the red dots (app.js pending / updateDots / SEEN)", () => {
  /** The count on a tab (0 = none). */
  const dotOf = (v: WikiView, tab: string): number =>
    Number(v.contentEl.querySelector(`.ew-seg > button[data-tab=${tab}] .ew-cnt`)?.textContent ?? 0);
  it("new RSI changes raise 个性化 while 今天 is shown (今天's own keys are seen); opening 个性化 clears them, and it stays cleared", async () => {
    const { c, api } = setup();
    api.__pendingRule("讲数学时先给能算的小例子");
    let saved: string[] = [];
    const store = { load: () => saved, save: vi.fn((k: string[]) => { saved = k; }) };
    const view = new WikiView({} as WorkspaceLeaf, c, fakeActions(), () => fakeCtx(), null, null, null, store);
    await view.onOpen();
    await flush();
    expect(c.state.rsi.kind === "ready" && c.state.rsi.value.some((x) => x.patch_id === "pm1" && x.status === "personal_accepted")).toBe(true);
    // what waits for the learner on 个性化: preferences to confirm (a ledger wait waits on its page)
    const waitingNow = (): number => c.state.prefs.kind === "ready" ? c.state.prefs.value.items.filter((x) => x.authority === "pending").length : 0;
    expect(dotOf(view, "today")).toBe(0);
    expect(dotOf(view, "engine")).toBeGreaterThan(waitingNow());
    expect(saved.some((k) => k.startsWith("today|attn:"))).toBe(false);
    c.setTab("engine");
    await flush();
    expect(waitingNow()).toBeGreaterThan(0);
    expect(dotOf(view, "engine")).toBe(waitingNow());          // only what waits for a decision stays counted
    expect(saved).toContain(`engine@${Math.max(...RSI_CHANGES.filter((x) => ["personal_accepted", "rolled_back", "rejected"].includes(x.status)).map((x) => Date.parse(x.at)))}`);
    c.setTab("today");
    await flush();                                        // 今天's cards have landed; shown, they are seen
    expect(dotOf(view, "today")).toBe(0);
    expect(dotOf(view, "engine")).toBe(waitingNow());
    await view.onClose();
    // A restart: the same items never raise the count again.
    const again = new WikiView({} as WorkspaceLeaf, c, fakeActions(), () => fakeCtx(), null, null, null, store);
    c.setTab("assistant");
    await again.onOpen();
    expect(dotOf(again, "engine")).toBe(waitingNow());
    expect(dotOf(again, "today")).toBe(0);
    await again.onClose();
  });
});


describe("WikiView: the all-done check", () => {
  it("is marked seen after the box is drawn (once), so the next draw has no .ew-anim", async () => {
    const { c } = setup();
    const ready = { kind: "ready" } as const;
    const state = { ...c.state, configured: true, conn: "online" as const, progress: { kind: "ready" as const, value: { goals: [{ goal_id: "g", kind: "path" as const, goal: "pca", state: "done" as const, blocks: [], path_ids: [], pages_done: 1, pages_total: 1, step: { kind: "goal_done" as const, goal_id: "g", goal: "pca", map_id: null, region_id: null, region_name: "", path_id: null, index: null, page_id: null, title: "", draft_id: null, progress: null, failure: null, review_first: null } }], today: { kind: "none" as const, goal_id: null, goal: "", map_id: null, region_id: null, region_name: "", path_id: null, index: null, page_id: null, title: "", draft_id: null, progress: null, failure: null, review_first: null } } },
      review: { ...c.state.review, today: { ...ready, value: { items: [], due: 0, done_today: 0, limit: 10, no_material: [], confirms: [] } } },
      bot: { ...c.state.bot, today: { ...ready, value: [] }, allDoneSeen: false } };
    const listeners = new Set<() => void>();
    const mark = vi.fn(() => { state.bot = { ...state.bot, allDoneSeen: true }; });
    const fake = { get state() { return state; }, subscribe: (f: () => void) => { listeners.add(f); return () => listeners.delete(f); }, subscribeProgress: () => () => undefined,
      init: vi.fn(), viewOpened: vi.fn(), viewClosed: vi.fn(), loadBotChips: vi.fn(), refreshToday: vi.fn(), markAllDoneSeen: mark };
    const view = new WikiView({} as WorkspaceLeaf, fake as never, fakeActions(), () => fakeCtx());
    await view.onOpen();
    expect(view.contentEl.querySelector(".ew-alldone.ew-anim")).not.toBeNull();
    expect(mark).toHaveBeenCalledTimes(1);
    for (const f of listeners) f();
    expect(view.contentEl.querySelector(".ew-alldone")).not.toBeNull();
    expect(view.contentEl.querySelector(".ew-alldone.ew-anim")).toBeNull();
    expect(mark).toHaveBeenCalledTimes(1);
  });
});
