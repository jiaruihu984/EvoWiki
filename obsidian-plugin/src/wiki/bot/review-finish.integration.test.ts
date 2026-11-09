import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { firstPage, flush, setup, type Setup } from "../controller-test-kit";
import { currentCard } from "../review-entries";
import { taskCards } from "./render-tasks";
import { renderNext } from "../render-next";
import { fakeActions, fakeCtx } from "../render-test-kit";

/** The 500 / 1400 ms animation windows read Date.now(): freeze it so a slow run cannot close a window mid-test. */
beforeEach(() => { vi.useFakeTimers({ toFake: ["Date"] }); vi.setSystemTime(new Date(2026, 9, 15, 9, 0)); });
afterEach(() => { document.body.replaceChildren(); vi.useRealTimers(); });

/** The view's loop: every state change rebuilds the whole block. */
function mount(s: Setup): () => HTMLElement {
  const host = document.createElement("div");
  document.body.append(host);
  const draw = (): void => { host.replaceChildren(renderNext(s.c.state, fakeActions(), fakeCtx())); };
  s.c.subscribe(draw);
  draw();
  return () => host;
}

async function begin(): Promise<{ s: Setup; host: () => HTMLElement }> {
  const s = setup();
  await firstPage(s.c);
  await s.c.init();
  await s.c.reloadReview();
  await s.c.refreshToday(true);
  const host = mount(s);
  await s.c.botStart("review");
  expect(s.c.state.review.active).toBe(true);
  return { s, host };
}

describe("review round through the real controller and renderer", () => {
  it("a linked round: the done card keeps .ew-anim and the same connected canvas through the follow-up puts; ok counts right answers", async () => {
    const { s, host } = await begin();
    let seenCanvas: Element | null = null;
    for (let guard = 0; guard < 20 && s.c.state.review.active; guard++) {
      const card = currentCard(s.c.state);
      if (card === null) break;
      if (card.mode === "cue") { s.c.revealCue(card.cell); await s.c.gradeReview(card.cell, "recalled"); } else await s.c.answerReview(card.cell, 0);
      s.c.nextReview(card.cell);
    }
    const cel = s.c.state.bot.celebrate;
    expect(cel).not.toBeNull();
    expect(cel!.ok).toBeGreaterThan(0);
    seenCanvas = host().querySelector("canvas.ew-confetti");
    expect(host().querySelector(".ew-donec")?.className).toBe("ew-card ew-donec ew-anim");
    expect(seenCanvas).not.toBeNull();
    await flush(); // the done reaction and its refresh re-render the block again
    expect(s.c.state.bot.reviewLinked).toBeNull();
    expect(host().querySelector(".ew-donec")?.className).toBe("ew-card ew-donec ew-anim");
    expect(host().querySelector("canvas.ew-confetti")).toBe(seenCanvas);
    expect(seenCanvas!.isConnected).toBe(true);
  });

  it("a right check answer keeps its pop and okline through the refresh re-render that follows", async () => {
    const { s, host } = await begin();
    const orig = s.api.gradeReview.bind(s.api);
    vi.spyOn(s.api, "gradeReview").mockImplementation(async (b) => ({ ...(await orig(b)), correct: "choice" in b ? true : null }));
    let right = false;
    for (let guard = 0; guard < 20 && !right; guard++) {
      const card = currentCard(s.c.state);
      if (card === null) break;
      if (card.mode === "cue") { s.c.revealCue(card.cell); await s.c.gradeReview(card.cell, "recalled"); s.c.nextReview(card.cell); continue; }
      await s.c.answerReview(card.cell, 0);
      right = s.c.state.review.results[card.cell]?.correct === true;
    }
    expect(right).toBe(true);
    expect(host().querySelector(".ew-okline")?.className).toBe("ew-okline ew-anim");
    expect(host().querySelector(".ew-o.ew-right")?.className).toContain("ew-pop");
  });

  /** Answers every card right (checks forced right, cues recalled), moving past each; returns how many were answered. */
  async function finishAllRight(s: Setup, stopAfter = Infinity): Promise<number> {
    const orig = s.api.gradeReview.bind(s.api);
    vi.spyOn(s.api, "gradeReview").mockImplementation(async (b) => ({ ...(await orig(b)), correct: "choice" in b ? true : null }));
    let n = 0;
    for (let guard = 0; guard < 20 && s.c.state.review.active && n < stopAfter; guard++) {
      const card = currentCard(s.c.state);
      if (card === null) break;
      if (card.mode === "cue") { s.c.revealCue(card.cell); await s.c.gradeReview(card.cell, "recalled"); } else await s.c.answerReview(card.cell, 0);
      s.c.nextReview(card.cell);
      n++;
    }
    return n;
  }

  it("all answers right: ok equals n", async () => {
    const { s } = await begin();
    const n = await finishAllRight(s);
    expect(s.c.state.bot.celebrate).toMatchObject({ n, ok: n });
  });

  it("pause then resume: 开始 starts the count again, so answers before the pause are not counted twice", async () => {
    const { s } = await begin();
    const first = await finishAllRight(s, 1);
    expect(first).toBe(1);
    s.c.pauseReview();
    s.c.startReview();
    const rest = await finishAllRight(s);
    expect(rest).toBeGreaterThan(0);
    expect(s.c.state.bot.celebrate).toMatchObject({ n: rest, ok: rest });
  });

  it("finish, ×, then a refresh brings a confirmation question: the review card still says it is a re-check; 开始 clears that", async () => {
    const { s } = await begin();
    await finishAllRight(s);
    expect(s.c.state.review.roundDone).toBe(true);
    s.c.closeCelebrate();
    const orig = s.api.reviewToday.bind(s.api);
    vi.spyOn(s.api, "reviewToday").mockImplementation(async () => {
      const t = await orig();
      const confirm = { followup_id: "f9", concept_id: "c1", concept_name: "概念", page_id: "p1", page_title: "页二", section_id: "s1", check: { stem: "题", options: ["甲", "乙"] } } as never;
      return { ...t, items: [], confirms: [confirm] };
    });
    await s.c.refreshToday(true);
    const card = taskCards(s.c.state).find((c) => c.key === "review");
    expect(card?.topic).toBe("刚才答错的那道题");
    s.c.startReview();
    expect(s.c.state.review.roundDone).toBe(false);
    expect(taskCards(s.c.state).find((c) => c.key === "review")?.topic).toBe("概念");
  });
});
