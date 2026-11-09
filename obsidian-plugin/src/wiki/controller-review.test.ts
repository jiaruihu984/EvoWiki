import type { PageRoundMemo, WikiControllerDeps } from "./controller/deps";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/errors";
import { WIKI_COPY } from "./copy";
import type { WikiController } from "./controller";
import { firstPage, flush, methods, newGoal, setup, type Setup, until, view } from "./controller-test-kit";
import { FakeWikiApi } from "./fake";
import { currentCard, currentEntry, pendingCount } from "./review-entries";
import { todayStep } from "./progress";
import { PCA_GOAL } from "./fixtures";
import { errorText } from "./format";
import type { ReviewItemOut, ReviewTodayOut } from "./schema";

/** firstPage (p1 open) + 今天's reads (init). */
async function reviewReady(o: { api?: FakeWikiApi; demo?: boolean } = {}): Promise<Setup> {
  const s = setup(o);
  await firstPage(s.c);
  await s.c.init();
  await s.c.reloadReview();
  return s;
}
const todayOf = (c: WikiController): ReviewTodayOut => {
  const t = c.state.review.today;
  if (t.kind !== "ready") throw new Error(`today is ${t.kind}`);
  return t.value;
};
function itemWhere(c: WikiController, f: (i: ReviewItemOut) => boolean): ReviewItemOut {
  const i = todayOf(c).items.find(f);
  if (!i) throw new Error("no such review item");
  return i;
}
/** The first item in 回想 mode. */
const cueItem = (c: WikiController): ReviewItemOut => itemWhere(c, (i) => i.mode === "cue");
const checkItem = (c: WikiController): ReviewItemOut => itemWhere(c, (i) => i.mode === "check");
const runsFor = (s: Setup, pageId: string): number => s.sync.runs.filter((r) => r.pageId === pageId).length;

/**
 * p1 (open first) has section s3 = c3.notation / c3.procedure / c4.procedure; p2 (the PCA goal) also uses c3.
 * Declaring c3.notation and c4.procedure known while p2 is the server's current page leaves p1's snapshot behind,
 * so p1's next GET folds s3 by itself (unlocked). A 没想起来 on c3.procedure then unfolds s3 on p1 (a real change).
 */
async function foldedS3(s: Setup): Promise<{ p1: string; p2: string }> {
  await firstPage(s.c);
  const p1 = view(s.c).page.page_id;
  await newGoal(s.c, PCA_GOAL);
  const p2 = view(s.c).page.page_id;
  await s.api.declare("c3.notation", "know");
  await s.api.declare("c4.procedure", "know");
  await s.c.openWikiPage(p1);
  expect(view(s.c).page.sections.find((x) => x.id === "s3")?.folded).toBe(true);
  return { p1, p2 };
}

describe("今天复习", () => {
  it("再来一批: GET more=true, the next batch reviewed in place; the done card offers it only while cards are due", async () => {
    const { c, api } = setup();
    await firstPage(c);
    await api.putSettings({ review_per_day: 1 });
    await c.reloadReview();
    c.startReview();
    const cur = currentEntry(c.state);
    if (cur?.kind !== "item") throw new Error("no card");
    if (cur.item.mode === "check") await c.answerReview(cur.key, 0);
    else { c.revealCue(cur.key); await c.gradeReview(cur.key, "recalled"); }
    c.nextReview(cur.key);
    await vi.waitFor(() => { expect(c.state.bot.celebrate).not.toBeNull(); });
    await vi.waitFor(() => { expect(c.state.review.today.kind === "ready" && c.state.review.today.value.items).toEqual([]); });
    const today = c.state.review.today;
    expect(today.kind === "ready" && today.value.due).toBeGreaterThan(0);
    await c.moreReview();
    expect(api.calls[api.calls.length - 1]).toEqual({ method: "reviewToday", args: [true] });
    expect(c.state.review.active).toBe(true);
    expect(c.state.bot.celebrate).toBeNull();
    const next = currentEntry(c.state);
    expect(next).not.toBeNull();
    expect(next?.key).not.toBe(cur.key);
    c.dispose();
  });

  it("init GETs today's list; leaving for 助手 reads nothing more", async () => {
    const { c, api } = setup();
    await firstPage(c);
    api.calls.length = 0;
    await c.init();
    expect(methods(api).filter((m) => m === "reviewToday")).toHaveLength(1);
    const n = api.calls.length;
    c.setTab("assistant");
    expect(api.calls.length).toBe(n);
  });

  it("unconfigured: today says needsSettings and nothing is sent", async () => {
    const { c, api } = setup({ configured: false });
    await c.init();
    expect(c.state.review.today).toEqual({ kind: "error", message: WIKI_COPY.needsSettings });
    expect(methods(api)).toEqual([]);
  });

  it("the cue is covered: grading before 看答案 sends nothing; after it, one POST", async () => {
    const { c, api } = await reviewReady();
    const item = cueItem(c);
    api.calls.length = 0;
    await c.gradeReview(item.cell, "recalled");
    expect(methods(api)).toEqual([]);
    expect(c.state.review.results).toEqual({});
    let notified = 0;
    c.subscribe(() => { notified++; });
    c.revealCue(item.cell);
    expect(c.state.review.revealed).toEqual([item.cell]);
    expect(notified).toBe(1);
    c.revealCue(item.cell); // already revealed: unchanged
    expect(c.state.review.revealed).toEqual([item.cell]);
    expect(methods(api)).toEqual([]); // 看答案 is local
    await c.gradeReview(item.cell, "recalled");
    expect(api.calls[0]).toEqual({ method: "gradeReview",
      args: [{ cell: item.cell, cells: item.cells, grade: "recalled", page_id: item.page_id, section_id: item.section_id, artifact_id: item.artifact_id }] });
  });

  it.each([["recalled", "known"], ["fuzzy", "shaky"], ["forgot", "gap"]] as const)(
    "自评 %s → band to %s, a plain result line, done_today + 1", async (grade, to) => {
      const { c } = await reviewReady();
      const item = itemWhere(c, (i) => i.mode === "cue" && i.band === "likely");
      const before = todayOf(c).done_today;
      c.revealCue(item.cell);
      await c.gradeReview(item.cell, grade);
      const r = c.state.review.results[item.cell];
      expect(r?.band).toEqual({ cell: item.cell, from: "likely", to });
      expect(r).toMatchObject({ grade, correct: null, correctIndex: null, choice: null });
      // likely → known is a tier up, likely → shaky / gap a tier down; nothing on the page changed (no fold / rewrite).
      const tier = to === "known" ? WIKI_COPY.reviewSurer(item.concept_name) : WIKI_COPY.reviewLessSure(item.concept_name);
      expect(r?.line).toEqual({ tone: "ok", text: `${WIKI_COPY.reviewNoted}${tier}` });
      expect(r?.line.text).not.toMatch(/LLM|→/);
      expect(todayOf(c).done_today).toBe(before + 1);
      expect(c.state.busy).toBeNull();
    });

  it("a graded page that is not open goes to 待同步 and syncs when it next lands", async () => {
    const s = setup();
    const { p1, p2 } = await foldedS3(s);
    await s.c.openWikiPage(p2);
    await s.c.init();
    await s.c.reloadReview();
    const item = itemWhere(s.c, (i) => i.cells.includes("c3.procedure") && i.page_id === p1);
    expect(item.section_id).toBe("s3");
    s.c.revealCue(item.cell);
    s.api.calls.length = 0;
    const p1Runs = runsFor(s, p1);
    await s.c.gradeReview(item.cell, "forgot");
    expect(methods(s.api)).toEqual(["gradeReview", "openPage", "catalog", "kGrid"]);
    expect(s.api.calls[1]?.args).toEqual([p2]); // the refresh GETs the open page, not the graded one
    expect(runsFor(s, p1)).toBe(p1Runs);
    // The card's own section, by its heading, even though p1 is not the shown page.
    const line = s.c.state.review.results[item.cell]?.line.text ?? "";
    expect(line).toContain(WIKI_COPY.reviewUnfoldedHere(item.heading));
    await s.c.openWikiPage(p1);
    expect(runsFor(s, p1)).toBe(p1Runs + 1);
  });

  it("the graded page is the open page: refresh GET page and sync it", async () => {
    const s = setup();
    const { p1 } = await foldedS3(s);
    await s.c.init();
    await s.c.reloadReview();
    const item = itemWhere(s.c, (i) => i.cells.includes("c3.procedure") && i.page_id === p1);
    s.c.revealCue(item.cell);
    s.api.calls.length = 0;
    const p1Runs = runsFor(s, p1);
    await s.c.gradeReview(item.cell, "forgot");
    expect(methods(s.api)).toEqual(["gradeReview", "openPage", "catalog", "kGrid"]);
    expect(s.api.calls[1]?.args).toEqual([p1]);
    expect(runsFor(s, p1)).toBe(p1Runs + 1);
    expect(view(s.c).page.sections.find((x) => x.id === "s3")?.folded).toBe(false);
    expect(view(s.c).touched.s3).toBe("unfolded");
    const heading = view(s.c).page.sections.find((x) => x.id === "s3")?.heading ?? "";
    expect(s.c.state.review.results[item.cell]?.line.text).toContain(WIKI_COPY.reviewUnfoldedHere(heading));
  });

  it("no page open: gradeReview, catalog, K (loaded by init)", async () => {
    const s = setup();
    await firstPage(s.c);
    // A fresh controller over the same fake: nothing is open.
    const t = setup({ api: s.api });
    await t.c.init();
    await t.c.reloadReview();
    const item = cueItem(t.c);
    t.c.revealCue(item.cell);
    s.api.calls.length = 0;
    await t.c.gradeReview(item.cell, "fuzzy");
    expect(methods(s.api)).toEqual(["gradeReview", "catalog", "kGrid"]);
    expect(t.c.state.review.results[item.cell]?.band.to).toBe("shaky");
  });

  it("a loaded K is refreshed too", async () => {
    const { c, api } = await reviewReady();
    expect(c.state.k.kind).toBe("ready");
    const item = cueItem(c);
    c.revealCue(item.cell);
    api.calls.length = 0;
    await c.gradeReview(item.cell, "fuzzy");
    expect(methods(api)).toEqual(["gradeReview", "openPage", "catalog", "kGrid"]);
  });

  it("确认题: one POST with choice, result shows correct / correct_index, done_today + 1 (a check answer counts too)", async () => {
    const { c, api } = await reviewReady();
    const item = checkItem(c);
    const before = todayOf(c).done_today;
    api.calls.length = 0;
    await c.answerReview(item.cell, 0);
    expect(api.calls[0]).toEqual({ method: "gradeReview",
      args: [{ cell: item.cell, choice: 0, page_id: item.page_id, section_id: item.section_id, artifact_id: item.artifact_id }] });
    const r = c.state.review.results[item.cell];
    expect(r?.choice).toBe(0);
    expect(typeof r?.correctIndex).toBe("number");
    expect(r?.correct).toBe(r?.correctIndex === 0);
    if (!r) throw new Error("no result");
    expect(r.grade).toBeNull();
    expect(r.line.tone).toBe("ok");
    // No verdict sentence (user review): only what changed, if anything.
    expect(r.line.text).not.toMatch(/答对|没答对|正确答案/);
    expect(todayOf(c).done_today).toBe(before + 1);
    // 不知道 on a fresh setup: correct null.
    const t = await reviewReady();
    const it2 = checkItem(t.c);
    await t.c.answerReview(it2.cell, -1);
    expect(t.c.state.review.results[it2.cell]).toMatchObject({ correct: null, choice: -1 });
    expect(t.c.state.review.results[it2.cell]?.line.text).not.toMatch(/不知道|正确答案/);
  });

  it("answering a cue item or grading a check item sends nothing", async () => {
    const { c, api } = await reviewReady();
    const cue = cueItem(c);
    const chk = checkItem(c);
    c.revealCue(chk.cell);
    api.calls.length = 0;
    await c.answerReview(cue.cell, 0);
    await c.gradeReview(chk.cell, "recalled");
    await c.gradeReview("nope.term", "recalled");
    expect(methods(api)).toEqual([]);
  });

  it("a second click on a graded item sends nothing; busy blocks", async () => {
    const { c, api } = await reviewReady();
    const [a, b] = todayOf(c).items.filter((i) => i.mode === "cue");
    if (!a || !b) throw new Error("need two cue items");
    c.revealCue(a.cell);
    c.revealCue(b.cell);
    api.calls.length = 0;
    const first = c.gradeReview(a.cell, "recalled");
    expect(c.state.busy).not.toBeNull();
    await c.gradeReview(b.cell, "recalled"); // busy: nothing
    await c.answerReview(checkItem(c).cell, 0); // busy: nothing
    await first;
    expect(methods(api).filter((m) => m === "gradeReview")).toHaveLength(1);
    api.calls.length = 0;
    await c.gradeReview(a.cell, "forgot"); // already graded
    expect(methods(api)).toEqual([]);
    expect(c.state.review.results[a.cell]?.band.to).toBe("known");
  });

  it("下一张: only an answered card can be moved past, once, locally", async () => {
    const { c, api } = await reviewReady();
    const item = cueItem(c);
    api.calls.length = 0;
    c.nextReview(item.cell); // not answered: nothing
    expect(c.state.review.seen).toEqual([]);
    c.revealCue(item.cell);
    await c.gradeReview(item.cell, "recalled");
    api.calls.length = 0;
    let notified = 0;
    c.subscribe(() => { notified++; });
    c.nextReview(item.cell);
    c.nextReview(item.cell); // already moved past: unchanged
    expect(c.state.review.seen).toEqual([item.cell]);
    expect(notified).toBe(1);
    expect(methods(api)).toEqual([]);
  });

  it("offline: grade sends nothing and says so", async () => {
    const { c, api, conn } = await reviewReady();
    const item = cueItem(c);
    c.revealCue(item.cell);
    conn.emit("offline");
    api.calls.length = 0;
    await c.gradeReview(item.cell, "recalled");
    await c.answerReview(checkItem(c).cell, 0);
    expect(methods(api)).toEqual([]);
    expect(c.state.review.note).toEqual({ text: WIKI_COPY.offline, tone: "error" });
    expect(c.state.review.results).toEqual({});
  });

  it("failure: one request, the reason in review.note, no result", async () => {
    const { c, api } = await reviewReady();
    const item = cueItem(c);
    const before = todayOf(c).done_today;
    c.revealCue(item.cell);
    api.failNext("gradeReview", new ApiError(422, "这一格不对"));
    api.calls.length = 0;
    await c.gradeReview(item.cell, "recalled");
    expect(methods(api)).toEqual(["gradeReview"]);
    expect(c.state.review.note).toEqual({ text: errorText(new ApiError(422, "x")), tone: "error" });
    expect(c.state.review.results).toEqual({});
    expect(todayOf(c).done_today).toBe(before);
    expect(c.state.busy).toBeNull();
  });

  it("每天最多: PUT {review_per_day}, then GET catalog and today; invalid sends nothing", async () => {
    const { c, api } = await reviewReady();
    const item = cueItem(c);
    c.revealCue(item.cell);
    await c.gradeReview(item.cell, "recalled");
    api.calls.length = 0;
    await c.setReviewPerDay(3);
    expect(methods(api)).toEqual(["putSettings", "catalog", "reviewToday"]);
    expect(api.calls[0]?.args).toEqual([{ review_per_day: 3 }]);
    const cat = c.state.catalog;
    expect(cat.kind === "ready" && cat.value.settings.review_per_day).toBe(3);
    expect(todayOf(c).limit).toBe(3);
    expect(todayOf(c).items.length).toBeLessThanOrEqual(2); // 3 minus the one done today
    expect(c.state.review.revealed).toEqual([item.cell]); // kept
    expect(c.state.review.results[item.cell]).toBeDefined(); // kept
    expect(c.state.review.note).toEqual({ text: WIKI_COPY.settingsSaved, tone: "ok" });
    expect(c.state.busy).toBeNull();
    for (const bad of [1.5, -1, Number.NaN]) {
      api.calls.length = 0;
      await c.setReviewPerDay(bad);
      expect(methods(api), String(bad)).toEqual([]);
      expect(c.state.review.note).toEqual({ text: WIKI_COPY.reviewPerDayInvalid, tone: "error" });
    }
    api.calls.length = 0;
    await c.setReviewPerDay(0);
    expect(methods(api)).toEqual(["putSettings", "catalog", "reviewToday"]);
    // No upper cap (the server has none): 250 is sent as it is.
    api.calls.length = 0;
    await c.setReviewPerDay(250);
    expect(methods(api)).toEqual(["putSettings", "catalog", "reviewToday"]);
    expect(api.calls[0]?.args).toEqual([{ review_per_day: 250 }]);
  });

  it("每天最多: a failed PUT shows its reason in review.note; a failed today GET adds the refresh line", async () => {
    const { c, api } = await reviewReady();
    api.failNext("putSettings", new ApiError(422, "不行"));
    api.calls.length = 0;
    await c.setReviewPerDay(4);
    expect(methods(api)).toEqual(["putSettings"]);
    expect(c.state.review.note).toEqual({ text: errorText(new ApiError(422, "x")), tone: "error" });
    api.failNext("reviewToday", new ApiError(500, "x"));
    await c.setReviewPerDay(4);
    expect(c.state.review.note).toEqual({ text: WIKI_COPY.settingsSaved, tone: "ok", sub: WIKI_COPY.refreshFailed });
    expect(c.state.review.today.kind).toBe("ready"); // the loaded list stays
  });
});

describe("回顾", () => {
  it("刷新: GET today, clears revealed / results / seen", async () => {
    const { c, api } = await reviewReady();
    const item = cueItem(c);
    c.revealCue(item.cell);
    await c.gradeReview(item.cell, "fuzzy");
    c.revealCue(itemWhere(c, (i) => i.mode === "cue" && i.cell !== item.cell).cell);
    expect(c.state.review.revealed).toHaveLength(2);
    c.nextReview(item.cell);
    expect(c.state.review.seen).toEqual([item.cell]);
    api.calls.length = 0;
    await c.reloadReview();
    expect(methods(api)).toEqual(["reviewToday"]);
    expect(c.state.review.revealed).toEqual([]);
    expect(c.state.review.results).toEqual({});
    expect(c.state.review.seen).toEqual([]);
    expect(todayOf(c).items.some((i) => i.cell === item.cell)).toBe(false); // reviewed today
  });

  it("刷新 while busy does nothing; unconfigured says needsSettings", async () => {
    const { c, api } = await reviewReady();
    const item = cueItem(c);
    c.revealCue(item.cell);
    const write = c.gradeReview(item.cell, "recalled");
    api.calls.length = 0;
    await c.reloadReview();
    expect(methods(api)).not.toContain("reviewToday");
    await write;
    const u = setup({ configured: false });
    await u.c.reloadReview();
    expect(methods(u.api)).toEqual([]);
    expect(u.c.state.review.today).toEqual({ kind: "error", message: WIKI_COPY.needsSettings });
  });
});

describe("Task 3 carry-over", () => {
  it("reloadCatalog: a failure keeps the catalog with its own wording; success clears the note", async () => {
    const { c, api } = setup();
    await c.init();
    api.failNext("catalog", new ApiError(500, "坏了"));
    await c.reloadCatalog();
    expect(c.state.catalog.kind).toBe("ready");
    expect(c.state.catalogNote).toEqual({ text: WIKI_COPY.reloadFailed(errorText(new ApiError(500, "x"))), tone: "error" });
    expect(c.state.catalogNote?.text).toBe(`没能刷新目录：${WIKI_COPY.failed}`);
    await c.reloadCatalog();
    expect(c.state.catalogNote).toBeNull();
  });
});

describe("今天 · 下一步 and the in-place review (controller)", () => {
  const cardsOf = (c: WikiController): string[] => todayOf(c).items.map((i) => i.cell);

  it("returning to 今天 from 助手 re-reads the review list and the catalog (a long-lived controller is never stale)", async () => {
    const { c, api } = await reviewReady();
    api.calls.length = 0;
    c.setTab("assistant");
    expect(api.calls).toEqual([]);                       // leaving reads nothing
    c.setTab("today");
    await until(() => methods(api).includes("reviewToday") && methods(api).includes("catalog"));
    await flush();
    expect(methods(api).filter((m) => m === "reviewToday")).toHaveLength(1);
    expect(c.state.review.today.kind).toBe("ready");
  });

  it("refreshToday shows the server's current list (a card done elsewhere is gone) and keeps the list when the re-read fails", async () => {
    const { c, api } = await reviewReady();
    const item = cueItem(c);
    await api.gradeReview({ cell: item.cell, grade: "recalled", page_id: item.page_id, section_id: item.section_id });
    expect(cardsOf(c)).toContain(item.cell);            // the shown list is stale until a re-read
    await c.refreshToday(true);
    expect(cardsOf(c)).not.toContain(item.cell);
    expect(c.state.review).toMatchObject({ revealed: [], results: {}, seen: [], removed: [] });
    api.failNext("reviewToday", new ApiError(500, "boom"));
    const kept = c.state.review.today;
    await c.refreshToday(true);
    expect(c.state.review.today).toEqual(kept);          // quiet: no error replaces a shown list
  });

  it("refreshToday coalesces an in-flight re-read and skips within the TTL (force overrides)", async () => {
    const { c, api } = await reviewReady();
    api.calls.length = 0;
    await Promise.all([c.refreshToday(), c.refreshToday(), c.refreshToday()]);
    expect(methods(api).filter((m) => m === "reviewToday")).toHaveLength(1);
    expect(methods(api).filter((m) => m === "catalog")).toHaveLength(1);
    api.calls.length = 0;
    await c.refreshToday();                              // within 30 s of the last one
    expect(api.calls).toEqual([]);
    await c.refreshToday(true);
    expect(methods(api)).toContain("reviewToday");
  });

  it("a forced re-read while one is in flight is followed by one more read (never answered by the older one); several forced ones share it", async () => {
    const { c, api } = await reviewReady();
    api.calls.length = 0;
    const first = c.refreshToday(true);
    const second = c.refreshToday(true);
    const third = c.refreshToday(true);
    await Promise.all([first, second, third]);
    expect(methods(api).filter((m) => m === "reviewToday")).toHaveLength(2);
  });

  it("a path plan is read again only when its catalog card changed", async () => {
    const s = setup();
    await newGoal(s.c, "学统计推断");
    await s.c.openPath(null);
    await s.c.init();
    expect(methods(s.api).filter((m) => m === "getPath").length).toBeGreaterThan(0);
    s.api.calls.length = 0;
    await s.c.refreshToday(true);
    expect(methods(s.api)).not.toContain("getPath");     // same card, cached plan reused
    await s.c.continueLearning();                         // a page was made: the card moved on
    s.api.calls.length = 0;
    await s.c.refreshToday(true);
    expect(methods(s.api)).toContain("getPath");
  });

  it("markBar writes the bar position into the current review slot without notifying", async () => {
    const { c } = await reviewReady();
    c.startReview();
    const drawnFrom = c.state.review;
    c.revealCue("x|y");                       // a newer review object replaces the one the bar was drawn from
    expect(c.state.review).not.toBe(drawnFrom);
    let n = 0;
    c.subscribe(() => { n++; });
    c.markBar(2);
    expect(c.state.review.barAt).toBe(2);
    expect(n).toBe(0);
  });

  it("refreshToday does nothing mid-review", async () => {
    const { c, api } = await reviewReady();
    c.startReview();
    api.calls.length = 0;
    await c.refreshToday();
    expect(api.calls).toEqual([]);
  });

  it("startReview shows the review in place on 今天; with no cards it does nothing", async () => {
    const { c } = await reviewReady();
    c.setTab("assistant");
    c.startReview();
    expect(c.state.tab).toBe("today");
    expect(c.state.review.active).toBe(true);
    const empty = setup();
    await empty.c.init();
    expect(todayOf(empty.c).items).toEqual([]);
    empty.c.startReview();
    expect(empty.c.state.review.active).toBe(false);
  });

  it("先不复习了 returns to 下一步, keeps every undone card, sends nothing and leaves 每天最多 alone", async () => {
    const { c, api } = await reviewReady();
    const all = cardsOf(c);
    const limit = todayOf(c).limit;
    c.startReview();
    api.calls.length = 0;
    c.pauseReview();
    expect(c.state.review.active).toBe(false);
    expect(c.state.review.finished).toBe(false);
    expect(api.calls).toEqual([]);
    expect(cardsOf(c)).toEqual(all);
    expect(todayOf(c).limit).toBe(limit);
    expect(c.state.review.seen).toEqual([]);
    expect(pendingCount(c.state)).toBe(all.length);
    c.startReview();                                      // resumes with the same cards
    expect(c.state.review.active).toBe(true);
    expect(currentCard(c.state)?.cell).toBe(all[0]);
  });

  it("answering the last card and 完成 ends the review: 下一步 turns into 继续 with 复习完了", async () => {
    const s = setup();
    await firstPage(s.c);
    await newGoal(s.c, "学统计推断");                                // a path, so there is something to continue
    await s.c.init();
    await s.c.reloadReview();
    s.c.startReview();
    for (let guard = 0; guard < 20 && s.c.state.review.active; guard++) {
      const card = currentCard(s.c.state);
      if (card === null) break;
      if (card.mode === "cue") { s.c.revealCue(card.cell); await s.c.gradeReview(card.cell, "recalled"); } else await s.c.answerReview(card.cell, 0);
      expect(s.c.state.review.results[card.cell]).toBeDefined();
      // every answered card stays (its result line, and after a wrong answer its follow-ups) until 下一张 / 完成
      expect(s.c.state.review.seen).not.toContain(card.cell);
      expect(s.c.state.review.note?.text).toBe(s.c.state.review.results[card.cell]?.line.text);
      s.c.nextReview(card.cell);
      expect(s.c.state.review.seen).toContain(card.cell);
    }
    expect(s.c.state.review.active).toBe(false);
    expect(s.c.state.review.finished).toBe(true);
    expect(currentCard(s.c.state)).toBeNull();
    expect(pendingCount(s.c.state)).toBe(0);
  });

  it("finishing the last card sets bot.celebrate with this round's n and ok (check right / cue not 没想起来); not before, and only once", async () => {
    const s = await reviewReady();
    s.c.startReview();
    let n = 0;
    for (let guard = 0; guard < 20 && s.c.state.review.active; guard++) {
      expect(s.c.state.bot.celebrate).toBeNull();
      const card = currentCard(s.c.state);
      if (card === null) break;
      if (card.mode === "cue") { s.c.revealCue(card.cell); await s.c.gradeReview(card.cell, "forgot"); } else await s.c.answerReview(card.cell, -1);
      n++;
      s.c.nextReview(card.cell);
    }
    expect(s.c.state.review.finished).toBe(true);
    expect(n).toBeGreaterThan(0);
    expect(s.c.state.bot.celebrate).toEqual({ n, ok: 0, at: expect.any(Number) as number });
    s.c.closeCelebrate();
    expect(s.c.state.bot.celebrate).toBeNull();
    s.c.pauseReview();
    expect(s.c.state.bot.celebrate).toBeNull();
  });

  async function playRound(s: Setup): Promise<void> {
    s.c.startReview();
    for (let guard = 0; guard < 20 && s.c.state.review.active; guard++) {
      const card = currentCard(s.c.state);
      if (card === null) break;
      if (card.mode === "cue") { s.c.revealCue(card.cell); await s.c.gradeReview(card.cell, "recalled"); } else await s.c.answerReview(card.cell, 0);
      s.c.nextReview(card.cell);
    }
    await flush();
  }

  it("the end of a round re-reads review/today quietly: the done card's streak_days (3 before the round) shows 4 after", async () => {
    const api = new FakeWikiApi();
    const orig = api.reviewToday.bind(api);
    let streak = 3;
    const read = vi.spyOn(api, "reviewToday").mockImplementation(async () => ({ ...(await orig()), page_round_today: 0, streak_days: streak }));
    const s = await reviewReady({ api });
    expect(todayOf(s.c).streak_days).toBe(3);
    streak = 4;   // the server counts this round only once it has happened
    await playRound(s);
    expect(read).toHaveBeenCalled();
    expect(s.c.state.review.finished).toBe(true);
    expect(todayOf(s.c).streak_days).toBe(4);
  });

  it("a failed re-read at the end of a round keeps the shown value (no error state)", async () => {
    const s = await reviewReady();
    const read = vi.spyOn(s.api, "reviewToday").mockRejectedValue(new Error("down"));
    const before = todayOf(s.c).streak_days;
    await playRound(s);
    expect(read).toHaveBeenCalled();
    expect(todayOf(s.c).streak_days).toBe(before);
  });

  it("继续: opens the path and makes its next page (pathNext flow), landing on the note", async () => {
    const s = setup();
    await newGoal(s.c, "学统计推断");
    await s.c.openPath(null);
    await s.c.init();
    expect(todayStep(s.c.state)).toMatchObject({ kind: "start_page", index: 1 });       // the server's step
    s.api.calls.length = 0;
    await s.c.continueLearning();
    expect(methods(s.api)).toContain("pathNext");
    expect(methods(s.api)).toContain("organize");
    expect(s.c.state.page.kind).toBe("open");
    expect(s.c.state.busy).toBeNull();
  });

  it("继续 with nothing to continue does nothing", async () => {
    const { c, api } = setup();
    await c.init();
    expect(todayStep(c.state)?.kind).toBe("none");
    api.calls.length = 0;
    await c.continueLearning();
    expect(api.calls).toEqual([]);
  });

  describe("反馈 on a review card", () => {
    it("hands the shown card to the assistant as 「这张卡片：<question>」 with its page; nothing is sent", async () => {
      const s = await reviewReady();
      s.c.startReview();
      const card = currentCard(s.c.state);
      if (card === null) throw new Error("no card");
      const n = s.api.calls.length;
      s.c.askAboutCard(card.cell);
      const question = card.mode === "check" ? card.check?.stem ?? "" : card.cue;
      expect(s.asked).toEqual([{ pageId: card.page_id, title: card.page_title, quote: { text: `这张卡片：${question}`, heading: card.heading, kind: "wrong_answer" } }]);
      expect(s.api.calls.length).toBe(n);
      s.c.askAboutCard("other|cell");
      expect(s.asked).toHaveLength(1);
    });
  });
});

describe("「考考我这页」: the page's cards as the round", () => {
  const pageRound = async () => {
    const s = await reviewReady();
    const pageItem = cueItem(s.c);
    const reviewPage = vi.fn(() => Promise.resolve({ ...todayOf(s.c), items: [pageItem], due: 1 }));
    s.api.reviewPage = reviewPage;
    const total = todayOf(s.c).items.length;
    await s.c.startChatReview("p9");
    return { s, pageItem, total, reviewPage };
  };
  it("a reload goes on with the cards not answered yet; the round's end forgets it", async () => {
    const { s, pageItem } = await pageRound();
    let memo: PageRoundMemo | null = null;
    (s.c as unknown as { core: { deps: WikiControllerDeps } }).core.deps.pageRoundMemo = { load: () => memo, save: (m) => { memo = m; } };
    await s.c.startChatReview("p9");
    expect(memo).toEqual({ pageId: "p9", answered: [] });
    s.c.revealCue(pageItem.cell);
    await s.c.gradeReview(pageItem.cell, "recalled");
    expect(memo).toEqual({ pageId: "p9", answered: [pageItem.card_id] });
    s.c.nextReview(pageItem.cell);
    await until(() => s.c.state.review.pageRound === null);
    expect(memo).toBeNull();
    memo = { pageId: "p9", answered: [] };       // a reload: the card not answered shows again
    await s.c.init();
    expect(s.c.state.review).toMatchObject({ active: true, pageRound: "p9" });
    expect(currentCard(s.c.state)?.cell).toBe(pageItem.cell);
    s.c.pauseReview();
    await until(() => s.c.state.review.pageRound === null);
    memo = { pageId: "p9", answered: [pageItem.card_id] };   // it was the last one: no round, nothing kept
    await s.c.init();
    expect(s.c.state.review.active).toBe(false);
    expect(memo).toBeNull();
  });
  it("shows on the same state and leaves the sidebar's tab alone", async () => {
    const { s, pageItem, reviewPage } = await pageRound();
    expect(s.c.state.review).toMatchObject({ active: true, pageRound: "p9" });
    expect(currentCard(s.c.state)?.cell).toBe(pageItem.cell);
    expect(reviewPage).toHaveBeenCalledWith("p9");
  });
  it("its grade goes through the one grade route with all the card's cells; at the end today's list is back, none of the round left", async () => {
    const { s, pageItem } = await pageRound();
    const gradeReview = vi.spyOn(s.api, "gradeReview");
    s.c.revealCue(pageItem.cell);
    await s.c.gradeReview(pageItem.cell, "recalled");
    expect(gradeReview).toHaveBeenCalledWith(expect.objectContaining({ cell: pageItem.cell, cells: pageItem.cells, grade: "recalled", page_round: true }));
    expect(todayOf(s.c).done_today).toBe(0);                 // outside the daily batch
    s.c.nextReview(pageItem.cell);
    await flush();
    await until(() => s.c.state.review.pageRoundDone !== null);
    expect(s.c.state.review).toMatchObject({ active: false, seen: [], results: {}, pageRound: null, pageRoundDone: 1 });
    expect(todayOf(s.c)).toMatchObject({ done_today: 0, page_round_today: 1 });
  });
  it("结束复习 in the middle: today's list is back too, the page's card is not left as today's", async () => {
    const { s, total } = await pageRound();
    expect(todayOf(s.c).items).toHaveLength(1);
    s.c.pauseReview();
    await until(() => s.c.state.review.pageRound === null && s.c.state.review.today.kind === "ready");
    expect(todayOf(s.c).items).toHaveLength(total);
  });
  it("a failed read says so in the card's place; no round starts", async () => {
    const s = await reviewReady();
    s.api.reviewPage = vi.fn(() => Promise.reject(new ApiError(404, "没有这个页面")));
    await s.c.startChatReview("p9");
    expect(s.c.state.review.today.kind).toBe("error");
    expect(s.c.state.review.active).toBe(false);
  });
});
