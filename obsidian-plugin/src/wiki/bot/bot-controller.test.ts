import { afterEach, describe, expect, it, vi } from "vitest";
import type { WriteOut } from "../schema";
import { ApiError } from "../../api/errors";
import type { BotSettings } from "./bot-schema";
import { firstPage, flush, methods, newGoal, setup } from "../controller-test-kit";
import { WIKI_COPY } from "../copy";
import { FakeWikiApi } from "../fake";
import type { SuggestionOut } from "./bot-schema";
import { BOT_COPY } from "./bot-copy";

const todayList = (s: ReturnType<typeof setup>): SuggestionOut[] => {
  const t = s.c.state.bot.today;
  if (t.kind !== "ready") throw new Error(t.kind);
  return t.value;
};
const last = <T,>(xs: readonly T[]): T | undefined => xs[xs.length - 1];
const reacts = (api: FakeWikiApi): unknown[][] => api.calls.filter((x) => x.method === "botReact").map((x) => x.args);
/** A page written, its cards due: a review round can start. */
async function withDue(): Promise<ReturnType<typeof setup>> {
  const s = setup();
  await firstPage(s.c);
  await s.c.init();
  await s.c.reloadReview();
  await s.c.refreshToday(true);
  return s;
}
const reviewReacts = (api: FakeWikiApi): unknown[] => api.calls.filter((x) => x.method === "botReviewReact").map((x) => x.args[0]);
const act = (s: ReturnType<typeof setup>, label: string): void => {
  const n = last(s.actionNotices);
  const a = n?.actions.find((x) => x.label === label);
  if (!a) throw new Error(`no action ${label}`);
  a.run();
};

describe("Bot loads", () => {
  it("refresh (via refreshToday) fills bot.today without bot_failing, and the ledger", async () => {
    const s = setup();
    const bad: SuggestionOut = { ...todayList0(), suggestion_id: "sg_bad", kind: "bot_failing", title: "坏了" };
    vi.spyOn(s.api, "botSuggestions").mockImplementation(async (p, id) => (p === "today"
      ? [bad, ...(await FakeWikiApi.prototype.botSuggestions.call(s.api, p, id))] : []));
    await s.c.refreshToday(true);
    const ids = todayList(s).map((x) => x.suggestion_id);
    expect(ids).toContain("sg_review");
    expect(ids).not.toContain("sg_bad");
    expect(s.c.state.bot.actions.kind).toBe("ready");
  });

  it("refresh also loads the Bot settings when not ready (the chips are gated by them), and not again once ready", async () => {
    const s = setup();
    expect(s.c.state.bot.settings.kind).not.toBe("ready");
    await s.c.refreshToday(true);
    expect(s.c.state.bot.settings.kind).toBe("ready");
    const n = () => s.api.calls.filter((x) => x.method === "botSettings").length;
    const first = n();
    await s.c.refreshToday(true);
    expect(n()).toBe(first);
  });

  it("the Bot's time zone follows this computer: a different stored zone is replaced quietly, an equal one is left", async () => {
    const zone = vi.spyOn(Intl.DateTimeFormat.prototype, "resolvedOptions").mockReturnValue({ timeZone: "Europe/Paris" } as Intl.ResolvedDateTimeFormatOptions);
    const s = setup({ api: new FakeWikiApi({ delayMs: 0 }) });
    await s.c.refreshToday(true);
    const puts = s.api.calls.filter((x) => x.method === "putBotSettings");
    expect(puts).toHaveLength(1);
    const st = s.c.state.bot.settings;
    expect(st.kind === "ready" ? st.value.timezone : null).toBe("Europe/Paris");
    expect(s.notices).toEqual([]);
    await s.c.refreshToday(true);
    expect(s.api.calls.filter((x) => x.method === "putBotSettings")).toHaveLength(1);   // now equal: left
    zone.mockRestore();
  });

  it("a failed refresh keeps the previous list and does not fail the other reads", async () => {
    const s = setup();
    await s.c.refreshToday(true);
    const before = todayList(s);
    s.api.failNext("botSuggestions", new ApiError(500, "boom"));
    await s.c.refreshToday(true);
    expect(todayList(s)).toEqual(before);
    expect(s.c.state.review.today.kind).toBe("ready");
  });

  it("a Bot read failure with nothing shown is an error state; the other reads still land", async () => {
    const s = setup();
    s.api.failNext("botSuggestions", new ApiError(500, "boom"));
    await s.c.refreshToday(true);
    expect(s.c.state.bot.today.kind).toBe("error");
    expect(s.c.state.review.today.kind).toBe("ready");
  });

  it("sends nothing when unconfigured or offline", async () => {
    const s = setup({ configured: false });
    await s.c.refreshToday(true);
    expect(methods(s.api).some((m) => m.startsWith("bot"))).toBe(false);
    const o = setup();
    (o.conn).emit("offline");
    await o.c.loadEngine();
    expect(methods(o.api).some((m) => m.startsWith("bot"))).toBe(false);
  });

  it("loadEngine reads the ledger, recent activity (never the silent rows) and the settings", async () => {
    const s = setup();
    await s.c.loadEngine();
    const b = s.c.state.bot;
    expect(b.actions.kind).toBe("ready");
    expect(b.settings.kind).toBe("ready");
    if (b.recent.kind !== "ready") throw new Error(b.recent.kind);
    expect(b.recent.value.every((r) => r.result !== "skipped")).toBe(true);
  });

  it("loadChips keeps only chat chips of the page; a failed read keeps what is shown", async () => {
    const s = setup();
    await s.c.loadBotChips("p2");
    expect(s.c.state.bot.chips.p2?.map((x) => x.suggestion_id)).toEqual(["sg_chat_p2_1", "sg_chat_p2_2"]);
    s.api.failNext("botSuggestions", new ApiError(500, "boom"));
    await s.c.loadBotChips("p2");
    expect(s.c.state.bot.chips.p2).toHaveLength(2);
  });
});

function todayList0(): SuggestionOut {
  return { suggestion_id: "x", kind: "review_due", title: "t", why: "w", action: { type: "start_practice" }, placement: "today", page_id: null,
    priority: 1, status: "pending", created_at: "2026-10-15T00:00:00Z", expires_at: "2099-01-01T00:00:00Z", trigger_id: null, preference_action_id: null };
}

describe("Bot cards", () => {
  it("start on start_practice: reacted accepted, the card hidden, the review starts", async () => {
    const s = setup();
    await s.c.refreshToday(true);
    const sp = vi.spyOn(s.c, "startReview");
    await s.c.botStart("sg_review");
    expect(reacts(s.api)).toEqual([["sg_review", "accepted"]]);
    expect(sp).toHaveBeenCalledOnce();
    expect(s.c.state.bot.hidden).toContain("sg_review");
  });

  it("start on open_page with ask: opens the page (awaited), then sends the ask into that page's conversation", async () => {
    const s = setup();
    await firstPage(s.c);
    await s.c.refreshToday(true);
    const order: string[] = [];
    const real = s.c.openWikiPage.bind(s.c);
    vi.spyOn(s.c, "openWikiPage").mockImplementation(async (id) => {
      order.push("open-start");
      await new Promise((r) => setTimeout(r, 5));
      await real(id);
      order.push(`open-end:${s.sent.length}`);
    });
    await s.c.botStart("sg_wrong");
    expect(order).toEqual(["open-start", "open-end:0"]);   // nothing was sent before the note was open
    expect(s.sent).toEqual(["为什么投影长度要除以 |b|？"]);
    expect(s.sentTo).toHaveLength(1);
    expect(s.sentTo[0]?.pageId).toBe("p1");
    expect(s.sentTo[0]?.title).not.toBe("");
    expect(reacts(s.api)).toEqual([["sg_wrong", "accepted"]]);
  });

  it("open_page with ask whose page did not open notices the question instead of sending it", async () => {
    const s = setup();
    await s.c.refreshToday(true);
    vi.spyOn(s.c, "openWikiPage").mockResolvedValue(undefined);
    await s.c.botStart("sg_wrong");
    expect(s.sent).toEqual([]);
    expect(s.notices).toContain(BOT_COPY.askNotSent("为什么投影长度要除以 |b|？"));
  });

  it("start on open_goal writes the next page of the card's own path, not the last-opened one", async () => {
    const s = setup();
    for (const g of ["学统计推断", "学统计推断二"]) await newGoal(s.c, g);
    const ids = s.c.state.catalog.kind === "ready" ? s.c.state.catalog.value.paths.map((p) => p.path_id) : [];
    expect(ids).toHaveLength(2);
    const goal: SuggestionOut = { ...todayList0(), suggestion_id: "sg_goal", kind: "goal_idle", action: { type: "open_goal", path_id: ids[1]! } };
    vi.spyOn(s.api, "botSuggestions").mockResolvedValue([goal]);
    await s.c.refreshToday(true);
    s.api.calls.length = 0;
    await s.c.botStart("sg_goal");
    expect(s.api.calls.filter((x) => x.method === "pathNext").map((x) => x.args)).toEqual([[ids[1]]]);
    expect(s.c.state.path.pathId).toBe(ids[1]);
  });

  it("B5 park_goal parks the card's goal for the Bot and says how to undo it; a failure says why", async () => {
    const s = setup();
    for (const g of ["学统计推断"]) await newGoal(s.c, g);
    const ids = s.c.state.catalog.kind === "ready" ? s.c.state.catalog.value.paths.map((p) => p.path_id) : [];
    const card: SuggestionOut = { ...todayList0(), suggestion_id: "sg_park", kind: "goal_review", action: { type: "park_goal", path_id: "path_math" } };
    vi.spyOn(s.api, "botSuggestions").mockResolvedValue([card]);
    await s.c.refreshToday(true);
    s.api.calls.length = 0;
    await s.c.botStart("sg_park");
    expect(s.api.calls.filter((x) => x.method === "botPark").map((x) => x.args)).toEqual([["path_math", true]]);
    expect(s.notices).toContain(BOT_COPY.parked);
    expect((await s.api.botGoals()).find((g) => g.path_id === "path_math")?.parked).toBe(true);
    expect(ids.length).toBe(1);
    s.api.failNext("botPark", new ApiError(500, "boom"));
    const again: SuggestionOut = { ...card, suggestion_id: "sg_park2" };
    vi.spyOn(s.api, "botSuggestions").mockResolvedValue([again]);
    await s.c.refreshToday(true);
    await s.c.botStart("sg_park2");
    expect(last(s.notices)).toBe(WIKI_COPY.failed);
  });

  it("资料更新: 更新这 3 页 calls reviseFromSource, reacts accepted and keeps a batch card until the drafts are decided", async () => {
    const s = setup();
    const card: SuggestionOut = { ...todayList0(), suggestion_id: "sg_rv", kind: "source_added", title: "《线性代数讲义》讲到了 3 页已有的内容",
      action: { type: "revise_from_source", source_id: "src1", page_ids: ["p1", "p2", "p3"] } };
    vi.spyOn(s.api, "botSuggestions").mockResolvedValue([card]);
    await s.c.refreshToday(true);
    s.api.calls.length = 0;
    await s.c.botStart("sg_rv");
    expect(s.api.calls.filter((x) => x.method === "reviseFromSource").map((x) => x.args)).toEqual([["src1", ["p1", "p2", "p3"]]]);
    expect(s.api.calls.filter((x) => x.method === "botReact").map((x) => x.args)).toEqual([["sg_rv", "accepted"]]);
    expect(s.c.state.bot.revise["sg_rv"]?.total).toBe(3);
    expect(s.c.state.bot.hidden).not.toContain("sg_rv");
    s.c.dispose?.();
  });

  it("B5 replan_goal replans the card's path, then shows it", async () => {
    const s = setup();
    await newGoal(s.c, "学统计推断");
    const [pid] = s.c.state.catalog.kind === "ready" ? s.c.state.catalog.value.paths.map((p) => p.path_id) : [];
    vi.spyOn(s.api, "replanPath").mockResolvedValue({ path: [], undo: null, kept: "" });
    const card: SuggestionOut = { ...todayList0(), suggestion_id: "sg_re", kind: "goal_review", action: { type: "replan_goal", path_id: pid! } };
    vi.spyOn(s.api, "botSuggestions").mockResolvedValue([card]);
    await s.c.refreshToday(true);
    await s.c.botStart("sg_re");
    expect(s.notices).toContain(BOT_COPY.replanned);
    expect(s.c.state.path.pathId).toBe(pid);
  });

  it("open_goal whose path cannot be opened notices the reason and writes nothing", async () => {
    const s = setup();
    const goal: SuggestionOut = { ...todayList0(), suggestion_id: "sg_goal", kind: "goal_idle", action: { type: "open_goal", path_id: "nope" } };
    vi.spyOn(s.api, "botSuggestions").mockResolvedValue([goal]);
    await s.c.refreshToday(true);
    s.api.calls.length = 0;
    await s.c.botStart("sg_goal");
    expect(methods(s.api)).not.toContain("pathNext");
    expect(s.notices.length).toBeGreaterThan(0);
  });

  it("a failed react notices the reason but the action still runs, and the card comes back", async () => {
    const s = setup();
    await s.c.refreshToday(true);
    const sp = vi.spyOn(s.c, "startReview");
    s.api.failNext("botReact", new ApiError(500, "boom"));
    await s.c.botStart("sg_review");
    expect(sp).toHaveBeenCalledOnce();
    expect(s.notices).toContain(WIKI_COPY.failed);
    expect(s.c.state.bot.hidden).not.toContain("sg_review");
  });

  it("start twice in a row reacts once", async () => {
    const s = setup();
    await s.c.refreshToday(true);
    vi.spyOn(s.c, "startReview");
    await Promise.all([s.c.botStart("sg_review"), s.c.botStart("sg_review")]);
    expect(reacts(s.api)).toHaveLength(1);
  });

  it("the local review card: starting the round resets the cooldown on the server, however it was opened", async () => {
    const s = await withDue();
    const sp = vi.spyOn(s.c, "startReview");
    await s.c.botStart("review");
    expect(sp).toHaveBeenCalledOnce();
    await flush();
    expect(reviewReacts(s.api)).toEqual(["accepted"]);
    s.c.pauseReview();
    s.c.startReview();                                       // 今天复习 (the command) comes in here too
    await flush();
    expect(reviewReacts(s.api)).toEqual(["accepted", "accepted"]);
  });

  it("notInterested: card hidden, notice with 撤销 only; 撤销 restores the card after the refresh", async () => {
    const s = setup();
    await s.c.refreshToday(true);
    await s.c.botNotInterested("sg_wrong");
    expect(s.c.state.bot.hidden).toContain("sg_wrong");
    const n = last(s.actionNotices);
    expect(n?.text).toBe("以后会少安排补讲");
    expect(n?.actions.map((a) => a.label)).toEqual([BOT_COPY.undo]);
    // The list read drops the card (it was reacted), and the hidden mark with it.
    await s.c.refreshToday(true);
    expect(todayList(s).map((x) => x.suggestion_id)).not.toContain("sg_wrong");
    act(s, BOT_COPY.undo);
    await vi.waitFor(() => expect(todayList(s).map((x) => x.suggestion_id)).toContain("sg_wrong"));
    expect(s.c.state.bot.hidden).not.toContain("sg_wrong");
    expect(methods(s.api)).toContain("rsiChanges");
  });

  it("notInterested: the preference row lands in the ledger and the change list before the notice (app.js prefChanged)", async () => {
    const s = setup();
    await s.c.refreshToday(true);
    await s.c.botNotInterested("sg_wrong");
    const pid = s.c.state.bot.actions.kind === "ready" ? s.c.state.bot.actions.value.find((a) => a.kind === "preference")?.action_id : undefined;
    expect(pid).toBeDefined();
    expect(s.c.state.rsi.kind === "ready" && s.c.state.rsi.value.some((c) => c.patch_id === pid)).toBe(true);
    expect(last(s.actionNotices)?.text).toBe("以后会少安排补讲");
  });

  it("notInterested without a preference_action_id: plain notice, no 撤回", async () => {
    const s = setup();
    await s.c.refreshToday(true);
    const orig = s.api.botReact.bind(s.api);
    vi.spyOn(s.api, "botReact").mockImplementation(async (id, r) => ({ ...(await orig(id, r)), preference_action_id: null }));
    await s.c.botNotInterested("sg_wrong");
    expect(s.actionNotices).toHaveLength(0);
    expect(s.notices).toContain("以后会少安排补讲");
  });

  it("减少提醒 records on the server and hides the card by its answer, with or without a listed review_due suggestion", async () => {
    for (const list of [undefined, []]) {
      const s = setup();
      if (list) vi.spyOn(s.api, "botSuggestions").mockResolvedValue(list);
      await s.c.refreshToday(true);
      await s.c.botNotInterested("review");
      expect(reviewReacts(s.api)).toEqual(["not_interested"]);
      expect(s.c.state.bot.reviewHidden).toBe(true);
      expect(last(s.actionNotices)?.text).toBe("以后会少提醒复习");
      expect(s.c.state.bot.actions.kind === "ready" && s.c.state.bot.actions.value.some((a) => a.summary === "以后少提醒复习" && a.can_undo)).toBe(true);
    }
  });

  it("the cooldown is the server's: a reload still hides the card; it comes back when the cooldown is over; a failed click brings it back", async () => {
    const s = setup();
    await s.c.refreshToday(true);
    await s.c.botNotInterested("review");
    const fresh = setup({ api: s.api });
    await fresh.c.refreshToday(true);
    expect(fresh.c.state.bot.reviewHidden).toBe(true);
    vi.useFakeTimers({ toFake: ["Date"] });
    try {
      vi.setSystemTime(new Date(Date.now() + 41 * 3600 * 1000));
      await fresh.c.refreshToday(true);
    } finally { vi.useRealTimers(); }
    expect(fresh.c.state.bot.reviewHidden).toBe(false);
    vi.spyOn(fresh.api, "botReviewReact").mockRejectedValue(new Error("x"));
    await fresh.c.botNotInterested("review");
    expect(fresh.c.state.bot.reviewHidden).toBe(false);
  });

  it("撤销 in 活动记录 brings the review card back at once", async () => {
    const s = setup();
    await s.c.refreshToday(true);
    await s.c.botNotInterested("review");
    const id = s.c.state.bot.actions.kind === "ready" ? s.c.state.bot.actions.value.find((a) => a.summary === "以后少提醒复习")?.action_id : undefined;
    await s.c.undoPatch(id ?? "");
    expect(s.c.state.bot.reviewHidden).toBe(false);
  });

  it("chip: removed, reacted accepted, its text sent to the assistant", async () => {
    const s = setup();
    await s.c.loadBotChips("p2");
    await s.c.botChip("sg_chat_p2_1");
    expect(s.c.state.bot.chips.p2?.map((x) => x.suggestion_id)).toEqual(["sg_chat_p2_2"]);
    expect(reacts(s.api)).toEqual([["sg_chat_p2_1", "accepted"]]);
    expect(s.sent).toEqual(["λ = 0.5 的方向会怎样？"]);
  });

  it("reviewFinished: 开始 remembers the linked suggestion; done reaches the API (B3 keeps the accepted review_due listed)", async () => {
    const s = await withDue();
    vi.spyOn(s.c, "startReview");
    await s.c.botStart("review");
    await flush();
    s.c.pauseReview();
    await s.c.refreshToday(true);
    expect(todayList(s).find((x) => x.suggestion_id === "sg_review")?.status).toBe("accepted");
    s.c.botReviewFinished(3, 2);
    expect(s.c.state.bot.celebrate).toEqual({ n: 3, ok: 2, at: expect.any(Number) as number });
    await flush();
    expect(reviewReacts(s.api)).toEqual(["accepted"]);
    expect(reacts(s.api)).toEqual([["sg_review", "done"]]);
    s.c.closeCelebrate();
    expect(s.c.state.bot.celebrate).toBeNull();
  });

  it("开始 on the review card of an already accepted review_due: done still follows", async () => {
    const s = await withDue();
    await s.api.botReact("sg_review", "accepted");
    await s.c.refreshToday(true);
    vi.spyOn(s.c, "startReview");
    await s.c.botStart("review");
    await flush();
    s.c.botReviewFinished(1, 1);
    await flush();
    expect(reacts(s.api)).toEqual([["sg_review", "accepted"], ["sg_review", "done"]]);   // the first is the test's own
  });

  it("an accepted card is not drawn again after a refresh (B3 keeps it listed), and an accepted chip is dropped", async () => {
    const s = setup();
    await s.c.refreshToday(true);
    await s.api.botReact("sg_wrong", "accepted");
    await s.api.botReact("sg_chat_p2_1", "accepted");
    await s.c.refreshToday(true);
    await s.c.loadBotChips("p2");
    expect(todayList(s).map((x) => x.suggestion_id)).toEqual(["sg_review"]);
    expect(s.c.state.bot.chips.p2?.map((x) => x.suggestion_id)).toEqual(["sg_chat_p2_2"]);
  });

  it("reviewFinished without 开始 on a linked suggestion reacts nothing", async () => {
    const s = setup();
    await s.c.refreshToday(true);
    s.c.botReviewFinished(1, 1);
    await flush();
    expect(reacts(s.api)).toEqual([]);
  });

  it("撤回 of a card's own preference does not clear the review's cooldown; the review's own does", async () => {
    const s = setup();
    await s.c.refreshToday(true);
    await s.c.botNotInterested("review");
    await s.c.botNotInterested("sg_wrong");
    act(s, BOT_COPY.undo);
    await vi.waitFor(() => expect(todayList(s).map((x) => x.suggestion_id)).toContain("sg_wrong"));
    expect(s.c.state.bot.reviewHidden).toBe(true);
    const reviewNotice = s.actionNotices[0];
    reviewNotice?.actions.find((a) => a.label === BOT_COPY.undo)?.run();
    await vi.waitFor(() => expect(s.c.state.bot.reviewHidden).toBe(false));
  });

  it("a failed chip react puts the chip back and notices the reason", async () => {
    const s = setup();
    await s.c.loadBotChips("p2");
    s.api.failNext("botReact", new ApiError(500, "boom"));
    await s.c.botChip("sg_chat_p2_1");
    expect(s.c.state.bot.chips.p2?.map((x) => x.suggestion_id)).toEqual(["sg_chat_p2_1", "sg_chat_p2_2"]);
    expect(s.notices).toContain(WIKI_COPY.failed);
  });

  it("a react 409 keeps the card hidden, refreshes and shows no error", async () => {
    const s = setup();
    await firstPage(s.c);
    await s.c.refreshToday(true);
    await s.api.botReact("sg_wrong", "accepted");
    const before = s.api.calls.filter((x) => x.method === "botSuggestions").length;
    await s.c.botStart("sg_wrong");
    expect(s.notices).toEqual([]);
    expect(s.c.state.bot.hidden).not.toContain("sg_wrong");   // the refresh dropped the unlisted card's mark
    expect(todayList(s).map((x) => x.suggestion_id)).not.toContain("sg_wrong");
    expect(s.api.calls.filter((x) => x.method === "botSuggestions").length).toBeGreaterThan(before);
    await s.c.botNotInterested("sg_review");
    await s.api.botReact("sg_review", "accepted").catch(() => undefined);
    await s.c.refreshToday(true);
    expect(s.notices).toEqual([]);
  });

  it("an older ledger read cannot overwrite a newer one", async () => {
    const s = setup();
    let release: () => void = () => undefined;
    const orig = s.api.botActions.bind(s.api);
    const spy = vi.spyOn(s.api, "botActions");
    spy.mockImplementationOnce(async () => { const old = await orig(); await new Promise<void>((r) => { release = r; }); return old.slice(0, 0); });
    const first = s.c.loadEngine();
    await flush();
    await s.c.loadEngine();
    release();
    await first;
    const a = s.c.state.bot.actions;
    expect(a.kind === "ready" && a.value.length).toBeGreaterThan(0);
  });
});

describe("Bot re-reads of the change list", () => {
  it("decide re-reads 最近改了什么 quietly: the shown list never goes back to loading", async () => {
    const s = setup();
    await s.c.refreshToday(true);
    await s.c.loadEngine();
    expect(s.c.state.rsi.kind).toBe("ready");
    const kinds: string[] = [];
    s.c.subscribe(() => { kinds.push(s.c.state.rsi.kind); });
    const before = s.api.calls.filter((x) => x.method === "rsiChanges").length;
    await s.c.botDecide("act_merge1", "approve");
    expect(s.api.calls.filter((x) => x.method === "rsiChanges").length).toBe(before + 1);
    expect(kinds).not.toContain("loading");
  });

  it("a failed quiet re-read keeps the list shown", async () => {
    const s = setup();
    await s.c.refreshToday(true);
    const shown = s.c.state.rsi;
    s.api.failNext("rsiChanges", new ApiError(500, "boom"));
    await s.c.botDecide("act_merge1", "approve");
    expect(s.c.state.rsi.kind).toBe("ready");
    expect(shown.kind).toBe("ready");
  });

  it("a re-read asked for while one is in flight runs once more after it", async () => {
    const s = setup();
    await s.c.refreshToday(true);
    await s.c.loadEngine();
    const real = s.api.rsiChanges.bind(s.api);
    let release: () => void = () => undefined;
    let first = true;
    s.api.rsiChanges = async () => {
      const held = first;
      first = false;
      const out = await real();
      if (held) await new Promise<void>((r) => { release = r; });
      return out;
    };
    const before = s.api.calls.filter((x) => x.method === "rsiChanges").length;
    const a = s.c.botDecide("act_merge1", "approve");
    await vi.waitFor(() => expect(s.api.calls.filter((x) => x.method === "rsiChanges").length).toBe(before + 1));
    const b = s.c.botDecide("act_edit1", "undo");
    await flush();
    release();
    await Promise.all([a, b]);
    expect(s.api.calls.filter((x) => x.method === "rsiChanges").length).toBe(before + 2);
  });
});

describe("fake react semantics", () => {
  it("accepted -> done is legal; any other second final reaction is a 409", async () => {
    const api = new FakeWikiApi({ delayMs: 0 });
    await api.botReact("sg_review", "accepted");
    expect((await api.botReact("sg_review", "done")).status).toBe("done");
    await expect(api.botReact("sg_review", "done")).rejects.toMatchObject({ status: 409 });
    await api.botReact("sg_wrong", "accepted");
    await expect(api.botReact("sg_wrong", "not_interested")).rejects.toMatchObject({ status: 409 });
  });
});

describe("Bot ledger", () => {
  it("approve notices and re-reads the ledger; twice in a row sends one request", async () => {
    const s = setup();
    await s.c.loadEngine();
    await Promise.all([s.c.botDecide("act_merge1", "approve"), s.c.botDecide("act_merge1", "approve")]);
    expect(s.api.calls.filter((x) => x.method === "botDecide")).toHaveLength(1);
    expect(s.notices).toContain(BOT_COPY.approved(""));
    const a = s.c.state.bot.actions;
    if (a.kind !== "ready") throw new Error(a.kind);
    expect(a.value.find((x) => x.action_id === "act_merge1")?.status).toBe("executed");
  });

  it("reject and undo notices", async () => {
    const s = setup();
    await s.c.loadEngine();
    await s.c.botDecide("act_merge1", "reject");
    expect(s.notices).toContain(BOT_COPY.rejected);
    await s.c.botDecide("act_edit1", "undo");
    expect(s.notices).toContain(BOT_COPY.undone);
  });

  it("a rejected decision (409) notices the reason", async () => {
    const s = setup();
    await s.c.loadEngine();
    await s.c.botDecide("act_merge1", "approve");
    await s.c.botDecide("act_merge1", "reject");
    expect(last(s.notices)).not.toBe(BOT_COPY.rejected);
  });

  it("an undo the executor refuses (409) notices its own reason, not a generic one", async () => {
    const s = setup();
    await s.c.loadEngine();
    s.api.failNext("botDecide", new ApiError(409, "加练习的写作还没有接上", "action_refused"));
    await s.c.botDecide("act_edit1", "undo");
    expect(last(s.notices)).toBe("加练习的写作还没有接上");
  });

  it("toggleWhy opens and reads the chain once; closing keeps it; a trigger-less row asks by suggestion id", async () => {
    const s = setup();
    await s.c.botToggleWhy("tr_wrong");
    expect(s.c.state.bot.whyOpen).toEqual(["tr_wrong"]);
    expect(s.c.state.bot.why.tr_wrong?.kind).toBe("ready");
    await s.c.botToggleWhy("tr_wrong");
    expect(s.c.state.bot.whyOpen).toEqual([]);
    await s.c.botToggleWhy("tr_wrong");
    expect(s.api.calls.filter((x) => x.method === "botWhy")).toHaveLength(1);
    await s.c.botToggleWhy("s:sg_wrong");
    expect(last(s.api.calls.filter((x) => x.method === "botWhy"))?.args).toEqual([{ suggestionId: "sg_wrong" }]);
  });
});

describe("Bot settings", () => {
  it("saveSettings PUTs and notices 已保存。", async () => {
    const s = setup();
    await s.c.loadEngine();
    const cur = s.c.state.bot.settings;
    if (cur.kind !== "ready") throw new Error(cur.kind);
    await s.c.botSaveSettings({ ...cur.value, today_cap: 3 });
    expect(s.notices).toContain(BOT_COPY.saved);
    const next = s.c.state.bot.settings;
    expect(next.kind === "ready" && next.value.today_cap).toBe(3);
  });

  it("a rejected save keeps the old settings and notices the error", async () => {
    const s = setup();
    await s.c.loadEngine();
    const cur = s.c.state.bot.settings;
    if (cur.kind !== "ready") throw new Error(cur.kind);
    await s.c.botSaveSettings({ ...cur.value, today_cap: 99 });
    expect(s.c.state.bot.settings).toEqual(cur);
    expect(s.notices).not.toContain(BOT_COPY.saved);
    expect(last(s.notices)).toBeTruthy();
  });

  it("a 422 notices the server detail and redraws the unchanged settings (a new object, so the select goes back)", async () => {
    const s = setup();
    await s.c.loadEngine();
    const cur = s.c.state.bot.settings;
    if (cur.kind !== "ready") throw new Error(cur.kind);
    await s.c.botSaveSettings({ ...cur.value, today_cap: 99 });
    expect(last(s.notices)).toMatch(/today_cap/);
    const after = s.c.state.bot.settings;
    expect(after).toEqual(cur);
    expect(after).not.toBe(cur);
  });

  it("pause notices: tomorrow (with the daily time), forever, off", async () => {
    const s = setup();
    await s.c.loadEngine();
    await s.c.botPause("tomorrow");
    expect(last(s.notices)).toBe("Bot 暂停到明天 8:00。");
    await s.c.botPause("forever");
    expect(last(s.notices)).toBe(BOT_COPY.pausedAlways);
    await s.c.botPause("off");
    expect(last(s.notices)).toBe(BOT_COPY.resumed);
    const st = s.c.state.bot.settings;
    expect(st.kind === "ready" && st.value.paused).toBe("no");
  });

  it("a blocked save redraws the unchanged settings (new object) so the control reverts", async () => {
    const s = setup();
    await s.c.loadEngine();
    const cur = s.c.state.bot.settings;
    if (cur.kind !== "ready") throw new Error(cur.kind);
    (s.conn).emit("offline");
    await s.c.botSaveSettings({ ...cur.value, today_cap: 9 });
    expect(last(s.notices)).toBe(WIKI_COPY.offline);
    expect(s.c.state.bot.settings).toEqual(cur);
    expect(s.c.state.bot.settings).not.toBe(cur);
    expect(s.api.calls.filter((x) => x.method === "putBotSettings")).toHaveLength(0);
  });

  it("a save asked for during an in-flight PUT is queued and sent after it (last write wins)", async () => {
    const s = setup();
    await s.c.loadEngine();
    const cur = s.c.state.bot.settings;
    if (cur.kind !== "ready") throw new Error(cur.kind);
    const real = s.api.putBotSettings.bind(s.api);
    let release: () => void = () => undefined;
    let first = true;
    s.api.putBotSettings = async (x: BotSettings) => {
      if (first) { first = false; await new Promise<void>((r) => { release = r; }); }
      return real(x);
    };
    const p1 = s.c.botSaveSettings({ ...cur.value, today_cap: 6 });
    const p2 = s.c.botSaveSettings({ ...cur.value, today_cap: 7 });
    const p3 = s.c.botSaveSettings({ ...cur.value, today_cap: 8 });
    release();
    await Promise.all([p1, p2, p3]);
    await flush();
    const now = s.c.state.bot.settings;
    expect(now.kind === "ready" && now.value.today_cap).toBe(8);
    expect(s.api.calls.filter((x) => x.method === "putBotSettings")).toHaveLength(2);
  });

  it("patches made during one in-flight PUT are merged onto the latest settings: every changed field persists", async () => {
    const s = setup();
    await s.c.loadEngine();
    const real = s.api.putBotSettings.bind(s.api);
    let release: () => void = () => undefined;
    let first = true;
    s.api.putBotSettings = async (x: BotSettings) => {
      if (first) { first = false; await new Promise<void>((r) => { release = r; }); }
      return real(x);
    };
    const p1 = s.c.botSaveSettings({ today_cap: 6 });
    const p2 = s.c.botSaveSettings({ page_cap: 3 });
    const p3 = s.c.botSaveSettings({ kinds: { chat: false } });
    const p4 = s.c.botSaveSettings({ kinds: { idle_write: false } });
    release();
    await Promise.all([p1, p2, p3, p4]);
    const now = s.c.state.bot.settings;
    if (now.kind !== "ready") throw new Error(now.kind);
    expect(now.value).toMatchObject({ today_cap: 6, page_cap: 3 });
    expect(now.value.kinds).toMatchObject({ chat: false, idle_write: false, review_due: true });
    expect(s.api.calls.filter((x) => x.method === "putBotSettings")).toHaveLength(2);
  });

  it("a pause made during an in-flight save is queued behind it, not dropped", async () => {
    const s = setup();
    await s.c.loadEngine();
    const real = s.api.putBotSettings.bind(s.api);
    let release: () => void = () => undefined;
    s.api.putBotSettings = async (x: BotSettings) => { await new Promise<void>((r) => { release = r; }); return real(x); };
    const save = s.c.botSaveSettings({ today_cap: 6 });
    const pause = s.c.botPause("forever");
    await flush();
    expect(methods(s.api)).not.toContain("botPause");
    release();
    await Promise.all([save, pause]);
    expect(methods(s.api).filter((m) => m === "putBotSettings" || m === "botPause")).toEqual(["putBotSettings", "botPause"]);
    const now = s.c.state.bot.settings;
    expect(now.kind === "ready" && now.value.paused).toBe("always");
    expect(now.kind === "ready" && now.value.today_cap).toBe(6);
  });

  it("writes are refused offline with the offline notice", async () => {
    const s = setup();
    await s.c.loadEngine();
    (s.conn).emit("offline");
    await s.c.botPause("off");
    expect(last(s.notices)).toBe(WIKI_COPY.offline);
  });
});

describe("page writes in 最近做了什么", () => {
  afterEach(() => { vi.useRealTimers(); });
  const running = (s: ReturnType<typeof setup>, over: Partial<WriteOut> = {}): void => {
    s.api.writesNow = [{ page_id: "p2", run_id: "r1", kind: "organize", state: "writing", pass: null, section: 0, of: 4, started_at: null, cost_usd: 0.01, ...over }];
  };
  /** The fixture's idle_write row for p2 made a running one. */
  const makeWriting = (s: ReturnType<typeof setup>): void => {
    const rows = (s.api as unknown as { bot: { activity: { result: string; write: { page_id: string } | null; kind: string }[] } }).bot.activity;
    const r = rows.find((x) => x.write?.page_id === "p2")!;
    r.result = "writing";
  };
  /** The reads 参数 asks for itself (with page ids); the writes watcher reads without ids. */
  const writesCalls = (s: ReturnType<typeof setup>): unknown[][] => s.api.calls.filter((x) => x.method === "writes" && x.args.length > 0).map((x) => x.args);

  it("the 最近做了什么 rows carry the page writes, and nothing polls while no row is writing", async () => {
    vi.useFakeTimers();
    const s = setup();
    s.c.setTab("engine");
    await vi.advanceTimersByTimeAsync(0);
    const r = s.c.state.bot.recent;
    if (r.kind !== "ready") throw new Error(r.kind);
    expect(r.value.filter((x) => x.write != null).map((x) => x.kind)).toEqual(["idle_write", "user_write"]);
    await vi.advanceTimersByTimeAsync(5000);
    expect(writesCalls(s)).toEqual([]);
  });

  it("a running write is read every 1100 ms and painted in place; a new section does not redraw", async () => {
    vi.useFakeTimers();
    const s = setup();
    makeWriting(s);
    running(s);
    const root = document.createElement("div");
    const c = document.createElement("span");
    c.className = "ew-cost";
    c.dataset.wcost = "p2";
    c.textContent = "$0";
    root.append(c);
    document.body.append(root);
    s.c.setTab("engine");
    await vi.advanceTimersByTimeAsync(1100);
    expect(writesCalls(s)).toEqual([[["p2"]]]);
    expect(s.c.state.bot.writes.p2?.state).toBe("writing");   // the first status lands as a redraw (a row may now be writing)
    let draws = 0;
    s.c.subscribe(() => { draws++; });
    running(s, { section: 2, cost_usd: 0.034 });
    await vi.advanceTimersByTimeAsync(1100);
    expect(writesCalls(s)).toHaveLength(2);
    expect(draws).toBe(0);
    expect(s.c.state.bot.writes.p2?.section).toBe(2);
    expect(root.querySelector(".ew-cost")?.textContent).toBe("$0.034");
    root.remove();
  });

  it("when the write ends the rows are read again (cost, 撤销) and the poll stops", async () => {
    vi.useFakeTimers();
    const s = setup();
    makeWriting(s);
    running(s);
    s.c.setTab("engine");
    await vi.advanceTimersByTimeAsync(1100);
    const recent = (): number => s.api.calls.filter((x) => x.method === "botRecent").length;
    const before = recent();
    const rows = (s.api as unknown as { bot: { activity: { result: string; write: { page_id: string } | null }[] } }).bot.activity;
    rows.find((x) => x.write?.page_id === "p2")!.result = "written";
    running(s, { state: "done", section: 3, cost_usd: 0.055 });
    await vi.advanceTimersByTimeAsync(1100);
    expect(recent()).toBe(before + 1);
    const n = writesCalls(s).length;
    await vi.advanceTimersByTimeAsync(10_000);
    expect(writesCalls(s)).toHaveLength(n);
  });

  it("nothing is asked while 参数 is not the tab on screen; the poll looks again", async () => {
    vi.useFakeTimers();
    const s = setup();
    makeWriting(s);
    running(s);
    s.c.setTab("engine");
    await vi.advanceTimersByTimeAsync(0);
    s.c.setTab("today");
    await vi.advanceTimersByTimeAsync(3300);
    expect(writesCalls(s)).toEqual([]);
    s.c.setTab("engine");
    await vi.advanceTimersByTimeAsync(2300);
    // 参数 reuses the watcher's read of the live writes: it asks the server by id only for a page that is not live any more
    expect(s.c.state.bot.writes.p2?.state).toBe("writing");
    expect(writesCalls(s)).toEqual([]);
  });

  it("a failed read of the status retries later and notices nothing", async () => {
    vi.useFakeTimers();
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const s = setup();
    makeWriting(s);
    running(s);
    s.api.failNext("writes", new ApiError(500, "boom"));
    s.c.setTab("engine");
    await vi.advanceTimersByTimeAsync(1100);
    expect(writesCalls(s)).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(5000);
    expect(writesCalls(s)).toHaveLength(2);
    expect(s.notices).toEqual([]);
    warn.mockRestore();
  });

  it("撤销 on a written page undoes its ledger row, says what happens to the page, and strikes the row", async () => {
    const s = setup();
    await s.c.loadEngine();
    await s.c.botDecide("act_write_p2", "undo");
    expect(s.notices).toContain(BOT_COPY.writeUndone);
    const r = s.c.state.bot.recent;
    if (r.kind !== "ready") throw new Error(r.kind);
    const w = r.value.find((x) => x.write?.page_id === "p2")?.write;
    expect([w?.undone, w?.can_undo]).toEqual([true, false]);
    const a = s.c.state.bot.actions;
    expect(a.kind === "ready" && a.value.find((x) => x.action_id === "act_write_p2")?.status).toBe("undone");
  });

  it("the page-write 为什么？ toggles locally (no request)", async () => {
    const s = setup();
    await s.c.loadEngine();
    const before = s.api.calls.filter((x) => x.method === "botWhy").length;
    await s.c.botToggleWhy("w:wr_p2");
    expect(s.c.state.bot.whyOpen).toContain("w:wr_p2");
    await s.c.botToggleWhy("w:wr_p2");
    expect(s.c.state.bot.whyOpen).not.toContain("w:wr_p2");
    expect(s.api.calls.filter((x) => x.method === "botWhy").length).toBe(before);
  });

  it("an approval or undo made in 参数 re-reads the rows the chat card reads (one ledger)", async () => {
    const s = setup();
    await s.c.loadEngine();
    const before = s.api.calls.filter((x) => x.method === "botRecent").length;
    await s.c.botLedgerChanged();
    expect(s.api.calls.filter((x) => x.method === "botRecent").length).toBe(before + 1);
  });
});
