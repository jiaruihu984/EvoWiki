import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/errors";
import { MAX_QUOTE_CHARS } from "../assistant/types";
import { ASSISTANT_COPY } from "../assistant/copy";
import { HttpWikiClient } from "./api";
import { CheckBlockHub, mountCheckBlock } from "./check-block";
import { firstPage, setup, until, view, type Setup } from "./controller-test-kit";
import { WIKI_COPY } from "./copy";
import { EmbedHub } from "./embed/embed-block";
import { useTransportForTests } from "./evoui/frame-host";
import { trustEngineForTests } from "./evoui/frame/engine-safety";
import { inlineTransport } from "./evoui/frame/inline-transport";
import { type EvouiPort, mountEvouiBlock } from "./evoui/evoui-block";
import { FakeWikiApi } from "./fake";
import { followupKey } from "./followup";
import { currentEntry, pendingCount } from "./review-entries";
import { renderReviewCard } from "./render-review-card";
import { btns, fakeCtx } from "./render-test-kit";
import type { FollowupTarget } from "./schema";
import { wikiActions } from "./wiring";

const CHECK: FollowupTarget = { kind: "check", pageId: "p1", pageTitle: "t", sid: "s5", idk: false };
// One renderer (renderCheckFollow): the review card's row carries data-* hooks, the note's check card none.
const rows = (el: ParentNode) => btns(el, "[data-action=followup], .ew-follow > .ew-row > button.ew-btn");
const label = (el: ParentNode) => rows(el).map((b) => b.textContent);
/** A line on a noted row (already chosen, no assistant); a plain success has none (user review). */
const note = (el: ParentNode) => el.querySelector("[data-role=followup-note], .ew-follow > .ew-muted")?.textContent ?? null;
/** The noted choice: the one pressed button (aria-pressed="true"); the others are disabled. */
const pressed = (el: ParentNode) => rows(el).find((b) => b.getAttribute("aria-pressed") === "true")?.textContent ?? null;
const othersOff = (el: ParentNode) => rows(el).filter((b) => b.getAttribute("aria-pressed") !== "true").every((b) => b.disabled);
/** 看不懂 leaves no link (no 去复习, no 学这个): only buttons of the three. */
const links = (el: ParentNode) => btns(el, "[data-action=followup-open], [data-action=followup-learn], .ew-follow > button.ew-lk");
const errorLine = (el: ParentNode) => el.querySelector("[data-role=followup-error], .ew-follow > .ew-muted[role=alert]")?.textContent ?? null;
const whyLine = (el: ParentNode) => el.querySelector("[data-role=followup-why], .ew-follow > .ew-muted:not([role])")?.textContent ?? null;
const THREE = ["粗心答错", "现在会了", "还没掌握"];
const HOUR = 3_600_000;

async function checkBlock(choice: number, api?: FakeWikiApi): Promise<Setup & { el: HTMLElement; hub: CheckBlockHub }> {
  const s = setup(api ? { api } : {});
  await firstPage(s.c);
  const hub = new CheckBlockHub();
  hub.set(s.c);
  const el = document.createElement("div");
  mountCheckBlock(el, "page: p1\nsection: s5\n", hub);
  // choice -1 = 不知道
  const buttons = btns(el, ".ew-opts > button.ew-o:not(.ew-idk)");
  (choice < 0 ? btns(el, ".ew-opts > button.ew-idk")[0]! : buttons[choice]!).click();
  await until(() => view(s.c).page.sections.find((x) => x.id === "s5")!.check!.answered !== null && s.c.state.busy === null);
  return { ...s, el, hub };
}
const lastFollowup = (s: Setup): { args: unknown[] } => [...s.api.calls].reverse().find((x) => x.method === "followup")!;

describe("the check block", () => {
  it("a wrong answer shows the three buttons under the verdict; a right one shows none", async () => {
    const wrong = await checkBlock(0);
    expect(label(wrong.el)).toEqual(THREE);
    const right = await checkBlock(1);
    expect(rows(right.el)).toHaveLength(0);
    expect(right.el.querySelector(".ew-follow")).toBeNull();
  });

  it("after 不知道 there is no 粗心选错了", async () => {
    const idk = await checkBlock(-1);
    expect(label(idk.el)).toEqual(["现在会了", "还没掌握"]);
  });

  it("粗心选错了 / 我学会了: the answer's answer_ref goes with it, no sentence, the row stays (chosen pressed, others off), nothing opens and no assistant", async () => {
    for (const [i, choice] of [[0, "slip"], [1, "learned"]] as const) {
      const s = await checkBlock(0);
      s.opened.length = 0;
      rows(s.el)[i]!.click();
      await until(() => pressed(s.el) !== null);
      expect(pressed(s.el)).toBe(THREE[i]);
      expect(rows(s.el)).toHaveLength(3);
      expect(othersOff(s.el)).toBe(true);
      expect(rows(s.el)[i]!.disabled).toBe(false);
      expect(note(s.el)).toBeNull();
      expect(s.el.querySelector(".ew-res")).toBeNull();
      expect(links(s.el)).toHaveLength(0);
      expect(s.opened).toEqual([]);
      expect(s.asked).toEqual([]);
      expect(lastFollowup(s).args).toEqual(["p1", "s5", { choice, answer_ref: "check:p1:s5", artifact_id: expect.stringMatching(/^art_[0-9a-f]{24}$/) as string }]);
    }
  });

  it("看不懂: pressed, no sentence, the assistant opens at once with the question quoted; no 去复习, no 学这个, nothing opened or written", async () => {
    const s = await checkBlock(0);
    s.opened.length = 0;
    rows(s.el)[2]!.click();
    await until(() => pressed(s.el) !== null);
    expect(pressed(s.el)).toBe("还没掌握");
    expect(othersOff(s.el)).toBe(true);
    expect(note(s.el)).toBeNull();
    expect(links(s.el)).toHaveLength(0);
    expect(s.opened).toEqual([]);
    expect(s.asked).toHaveLength(1);
    const q = s.asked[0]!;
    expect(q.pageId).toBe("p1");
    expect(q.quote.text).toContain("题目：");
    expect(q.quote.text).toContain("我选的：");
    expect(q.quote.text).toContain("正确答案：");
    expect(q.quote.kind).toBe("wrong_answer"); // the 助手 chip shows the question itself
  });

  it.each([
    [404, WIKI_COPY.followupNotFound], [422, WIKI_COPY.followupInvalid],
    [503, WIKI_COPY.followupNoModel], [502, WIKI_COPY.followupNoQuestion], [500, WIKI_COPY.followupFailed],
  ])("a %i is one plain line (no server text), the buttons stay and a second try works", async (status, line) => {
    const s = await checkBlock(0);
    s.api.failNext("followup", new ApiError(status, "secret server detail"));
    rows(s.el)[0]!.click();
    await until(() => errorLine(s.el) !== null);
    expect(errorLine(s.el)).toBe(line);
    expect(s.el.textContent).not.toContain("secret");
    expect(rows(s.el)).toHaveLength(3);
    expect(rows(s.el).every((b) => !b.disabled)).toBe(true);
    expect(s.api.calls.filter((x) => x.method === "followup")).toHaveLength(1);
    rows(s.el)[0]!.click();
    await until(() => pressed(s.el) !== null);
  });

  it("the model down: slip / learned say so and record nothing, 看不懂 still works", async () => {
    const s = await checkBlock(0);
    s.api.__llmDown(true);
    rows(s.el)[1]!.click();
    await until(() => errorLine(s.el) !== null);
    expect(errorLine(s.el)).toBe(WIKI_COPY.followupNoModel);
    rows(s.el)[2]!.click();
    await until(() => pressed(s.el) !== null);
    expect(pressed(s.el)).toBe("还没掌握");
  });

  it("offline: the buttons are disabled with a reason and nothing is sent", async () => {
    const s = await checkBlock(0);
    s.conn.emit("offline");
    s.c.setTab("today");
    await until(() => whyLine(s.el) !== null);
    expect(whyLine(s.el)).toBe(WIKI_COPY.followupOffline);
    expect(rows(s.el).every((b) => b.disabled)).toBe(true);
    const n = s.api.calls.length;
    rows(s.el)[0]!.click();
    expect(s.api.calls.length).toBe(n);
  });
});

describe("a new answer forgets the old follow-up", () => {
  it("a check answered wrong again later shows the buttons again", async () => {
    const s = await checkBlock(0);
    rows(s.el)[0]!.click();
    await until(() => pressed(s.el) !== null);
    expect(othersOff(s.el)).toBe(true);
    const key = followupKey(CHECK);
    expect(s.c.followupView(key).kind).toBe("done");
    const sec = (): unknown => view(s.c).page.sections.find((x) => x.id === "s5")!.check;
    (sec() as { answered: number | null }).answered = null;
    await s.c.answerCheck("s5", 0);
    expect(s.c.followupView(key).kind).toBe("idle");
    await until(() => pressed(s.el) === null && rows(s.el).every((b) => !b.disabled));
  });

  it("a graded review card drops its old row", async () => {
    const s = setup();
    await firstPage(s.c);
    const core = (s.c as unknown as { core: { setFollowup(k: string, v: unknown): void; clearFollowup(k: string): void } }).core;
    core.setFollowup("card:rc_x", { kind: "done", choice: "slip", note: "n" });
    core.clearFollowup("card:rc_x");
    expect(s.c.followupView("card:rc_x")).toEqual({ kind: "idle", error: null });
  });
});

describe("controller details", () => {
  it("the demo gives the same reason on a check and a card", () => {
    const s = setup({ demo: true });
    expect(s.c.followupBlockedWhy()).toBe(WIKI_COPY.followupDemo);
  });

  it("a server quote is clipped to the assistant's limit", async () => {
    const s = await checkBlock(0);
    s.api.followup = () => Promise.resolve({ followup_id: "fu1", choice: "unclear", cell: "c1.interpret", concept_name: "z", status: "done",
      confirm_due: null, band: { cell: "c1.interpret", from: "gap", to: "gap" }, review: null, suggest: null,
      quote: { kind: "wrong_answer", text: "字".repeat(MAX_QUOTE_CHARS + 500), heading: "h" }, signal_id: null, llm_calls: 0 });
    rows(s.el)[2]!.click();
    await until(() => s.asked.length === 1);
    expect([...s.asked[0]!.quote.text]).toHaveLength(MAX_QUOTE_CHARS);
    expect(s.asked[0]!.quote.heading).toBe("h");
  });

});

describe("the review card", () => {
  async function wrongCard(pick = 0): Promise<{ s: Setup; root: () => HTMLElement }> {
    const s = setup();
    await firstPage(s.c);
    await s.c.init();
    await s.c.reloadReview();
    s.c.startReview();
    const a = wikiActions(() => s.c);
    for (let g = 0; g < 20; g++) {
      const e = currentEntry(s.c.state);
      if (e === null || e.kind !== "item") break;
      const card = e.item;
      if (card.mode === "cue") { s.c.revealCue(card.cell); await s.c.gradeReview(card.cell, "recalled"); s.c.nextReview(card.cell); continue; }
      await s.c.answerReview(card.cell, pick);
      if (s.c.state.review.results[card.cell]?.correct !== true) return { s, root: () => renderReviewCard(s.c.state, a, fakeCtx())! };
      s.c.nextReview(card.cell);
    }
    throw new Error("no wrong check card in the fake data");
  }

  it("a wrong check answer keeps the card with the three buttons and 下一张; 看不懂 sends the card's answer_ref and offers no link", async () => {
    const { s, root } = await wrongCard();
    expect(label(root())).toEqual(THREE);
    expect(root().querySelector("[data-action=review-next]")?.textContent).toMatch(/下一张|完成/);
    rows(root())[2]!.click();
    await until(() => Object.values(s.c.state.followups).some((v) => v.kind === "done"));
    const again = root();
    expect(note(again)).toBeNull();
    expect(again.querySelector("[data-action=followup-open], [data-action=followup-learn]")).toBeNull();
    expect(pressed(again)).toBe("还没掌握");
    expect(rows(again)).toHaveLength(3);
    expect(othersOff(again)).toBe(true);
    const [pid, sid, body] = lastFollowup(s).args as [string, string, { choice: string; answer_ref: string }];
    expect([pid, sid, body.choice]).toEqual(["p1", "s5", "unclear"]);
    expect(body.answer_ref).toMatch(/^review:p1:s5:\d+$/);
    expect(s.asked).toHaveLength(1);
  });

  it("a cue card graded 忘了 keeps the card with just 下一张 (no result line), without follow-ups (app.js reviewCard)", async () => {
    const s = setup();
    await firstPage(s.c);
    await s.c.init();
    await s.c.reloadReview();
    s.c.startReview();
    const a = wikiActions(() => s.c);
    const e = currentEntry(s.c.state);
    if (e === null || e.kind !== "item" || e.item.mode !== "cue") throw new Error("expected a cue card first");
    s.c.revealCue(e.item.cell);
    await s.c.gradeReview(e.item.cell, "forgot");
    const root = renderReviewCard(s.c.state, a, fakeCtx())!;
    expect(label(root)).toEqual([]);
    expect(root.querySelector(".ew-res")).toBeNull();
    expect(root.querySelector("[data-action=review-next]")?.className).toBe("ew-btn ew-primary");
  });

  it("a 409 on a follow-up: the row noted (pressed, others off) with the already-done line", async () => {
    const s = await checkBlock(0);
    s.api.failNext("followup", new ApiError(409, WIKI_COPY.followupAlreadyServer));
    rows(s.el)[0]!.click();
    await until(() => note(s.el) !== null);
    expect(note(s.el)).toBe(WIKI_COPY.followupDone);
    expect(othersOff(s.el)).toBe(true);
  });

  it("any other 409: nothing was recorded, the row says so and the buttons stay", async () => {
    const s = await checkBlock(0);
    s.api.failNext("followup", new ApiError(409, "x"));
    rows(s.el)[0]!.click();
    await until(() => s.c.state.followups[Object.keys(s.c.state.followups)[0]!]?.kind === "idle");
    expect(othersOff(s.el)).toBe(false);
  });

  it("without an assistant hook the quote is not dropped silently", async () => {
    const s = await checkBlock(0);
    (s.c as unknown as { deps: { askAssistant?: unknown } }).deps.askAssistant = undefined;
    rows(s.el)[2]!.click();
    await until(() => note(s.el) !== null);
    expect(note(s.el)).toBe(WIKI_COPY.followupNoAssistant);
  });

  it("after 不知道 on a card there is no 粗心选错了", async () => {
    const { root } = await wrongCard(-1);
    expect(label(root())).toEqual(["现在会了", "还没掌握"]);
  });
});

describe("confirmation questions in the review", () => {
  let t: Date;
  const mk = (): FakeWikiApi => { t = new Date("2026-10-09T08:00:00Z"); return new FakeWikiApi({ now: () => t }); };
  async function pending(kind: 0 | 1): Promise<Setup & { el: HTMLElement; root: () => HTMLElement }> {
    const s = await checkBlock(0, mk());
    rows(s.el)[kind]!.click();
    await until(() => pressed(s.el) !== null);
    const a = wikiActions(() => s.c);
    return { ...s, root: () => renderReviewCard(s.c.state, a, fakeCtx())! };
  }
  const advance = async (s: Setup, ms: number): Promise<void> => { t = new Date(t.getTime() + ms); await s.c.reloadReview(); };
  const answers = (el: ParentNode) => btns(el, "[data-action=confirm-answer]");
  const confirmEntry = (s: Setup) => {
    const e = currentEntry(s.c.state);
    if (e === null || e.kind !== "confirm") throw new Error("expected a confirmation question");
    return e.confirm;
  };

  it("a new question comes only when it is due, first in the review, counted in 复习 N 张", async () => {
    const s = await pending(1);
    await s.c.reloadReview();
    const base = pendingCount(s.c.state);
    expect(s.c.state.review.today.kind === "ready" && s.c.state.review.today.value.confirms).toEqual([]);
    await advance(s, HOUR - 1000);
    expect(pendingCount(s.c.state)).toBe(base);
    await advance(s, 2000);
    expect(pendingCount(s.c.state)).toBe(base + 1);
    s.c.startReview();
    confirmEntry(s);
    const card = s.root();
    expect(card.querySelector(".ew-rv-src")).toBeNull();
    expect(card.querySelector(".ew-rvtop .ew-rvbar")).not.toBeNull();
    expect(answers(card)[answers(card).length - 1]?.dataset.choice).toBe("-1");
  });

  it("a right answer confirms; 下一张 moves on past it", async () => {
    const s = await pending(0);
    await advance(s, HOUR + 1000);
    s.c.startReview();
    const c = confirmEntry(s);
    answers(s.root())[c.check.options.length - 2]!.click();   // the fake turns the original options around: the key was 1
    await until(() => s.c.state.review.confirmResults[c.followup_id] !== undefined);
    const card = s.root();
    expect(card.querySelector(".ew-res")).toBeNull();   // no verdict sentence (user review)
    expect(card.querySelector("[data-role=reteach]")).toBeNull();
    expect(card.querySelectorAll(".ew-o.ew-right")).toHaveLength(1);
    card.querySelector<HTMLButtonElement>("[data-action=review-next]")!.click();
    expect(currentEntry(s.c.state)?.kind).not.toBe("confirm");
  });

  it("a wrong answer after 我学会了 refutes it: 重新读 opens the section and nothing is rewritten by itself", async () => {
    const s = await pending(1);
    await advance(s, HOUR + 1000);
    s.c.startReview();
    const c = confirmEntry(s);
    answers(s.root())[0]!.click();
    await until(() => s.c.state.review.confirmResults[c.followup_id] !== undefined);
    const out = s.c.state.review.confirmResults[c.followup_id]!.out;
    expect(out.outcome).toBe("refuted");
    expect(out.reteach).not.toBeNull();
    const card = s.root();
    expect(card.querySelector(".ew-res")).toBeNull();
    expect(card.querySelectorAll(".ew-o.ew-wrong")).toHaveLength(1);
    const unclear = card.querySelector<HTMLButtonElement>("[data-action=confirm-unclear]")!;
    const reread = card.querySelector<HTMLButtonElement>("[data-action=confirm-reread]")!;
    expect([unclear.textContent, reread.textContent]).toEqual(["还没掌握", "重新读"]);
    s.opened.length = 0;
    reread.click();
    await until(() => s.opened.length > 0);
  });

  it("还没掌握 opens the assistant with the question quoted (题目 / 我选的 / 正确答案); nothing is sent or rewritten", async () => {
    const s = await pending(1);
    await advance(s, HOUR + 1000);
    s.c.startReview();
    const c = confirmEntry(s);
    answers(s.root())[0]!.click();
    await until(() => s.c.state.review.confirmResults[c.followup_id] !== undefined);
    const n = s.api.calls.length;
    s.root().querySelector<HTMLButtonElement>("[data-action=confirm-unclear]")!.click();
    const r = s.c.state.review.confirmResults[c.followup_id]!;
    expect(s.asked).toEqual([{ pageId: c.page_id, title: c.page_title, quote: {
      text: `题目：${c.check.stem}
我选的：${c.check.options[0]}
正确答案：${c.check.options[r.out.correct_index]}`,
      heading: r.out.reteach?.heading ?? null, kind: "wrong_answer" } }]);
    expect(s.api.calls.length).toBe(n);
  });

  it("a refuted 粗心选错了 offers no way back (only 我学会了 re-teaches)", async () => {
    const s = await pending(0);
    await advance(s, HOUR + 1000);
    s.c.startReview();
    const c = confirmEntry(s);
    answers(s.root())[0]!.click();
    await until(() => s.c.state.review.confirmResults[c.followup_id] !== undefined);
    expect(s.c.state.review.confirmResults[c.followup_id]!.out.outcome).toBe("refuted");
    expect(s.root().querySelector("[data-role=reteach]")).toBeNull();
  });

  it("a 404 / 409 on a confirmation answer closes the card: options off, 下一张 moves on", async () => {
    const s = await pending(1);
    await advance(s, HOUR + 1000);
    s.c.startReview();
    const c = confirmEntry(s);
    s.api.failNext("confirmAnswer", new ApiError(404, "x"));
    answers(s.root())[0]!.click();
    await until(() => s.c.state.review.confirmResults[c.followup_id] !== undefined);
    const card = s.root();
    expect(answers(card).every((b) => b.disabled)).toBe(true);
    expect(card.querySelector("[data-role=review-error]")?.textContent).toBe(WIKI_COPY.confirmNotFound);
    expect(card.querySelector("[data-role=reteach]")).toBeNull();
    card.querySelector<HTMLButtonElement>("[data-action=review-next]")!.click();
    expect(currentEntry(s.c.state)?.kind).not.toBe("confirm");
  });

  it("不知道 is a refutation; a failed answer says so in plain words and the question stays", async () => {
    const s = await pending(1);
    await advance(s, HOUR + 1000);
    s.c.startReview();
    const c = confirmEntry(s);
    s.api.failNext("confirmAnswer", new ApiError(422, "secret"));
    answers(s.root())[answers(s.root()).length - 1]!.click();
    await until(() => s.c.state.review.note !== null);
    expect(s.c.state.review.note?.text).toBe(WIKI_COPY.confirmInvalid);
    expect(s.c.state.review.confirmResults[c.followup_id]).toBeUndefined();
    answers(s.root())[answers(s.root()).length - 1]!.click();
    await until(() => s.c.state.review.confirmResults[c.followup_id] !== undefined);
    const out = s.c.state.review.confirmResults[c.followup_id]!.out;
    expect([out.correct, out.outcome]).toEqual([null, "refuted"]);
  });
});

describe("an evoui quiz has no follow-up buttons (its evidence is still kept)", () => {
  beforeEach(() => { useTransportForTests(inlineTransport); trustEngineForTests(true); vi.spyOn(console, "warn").mockImplementation(() => undefined); });
  afterEach(() => { vi.restoreAllMocks(); document.body.replaceChildren(); });
  const quiz = { type: "evoui", title: "题", question: "?", state: {}, derived: {}, fallback: "f",
    body: { kind: "box", children: [{ kind: "quiz", questions: [{ q: "二加二？", options: ["三", "四"], correct: 1, why: "因为二加二等于四" }] }] } };
  const flush = async (): Promise<void> => { for (let i = 0; i < 6; i++) await Promise.resolve(); };

  it("a wrong answer draws nothing under the block and still delivers its result events", async () => {
    const got: unknown[] = [];
    const port: EvouiPort = { read: () => Promise.resolve(JSON.stringify(quiz)), canFetch: () => false, assetPath: (id) => `Wiki/assets/${id}.json`,
      retrieve: () => Promise.resolve(false), onEvent: (_id, ev) => { got.push(ev); } };
    const el = document.createElement("div");
    document.body.append(el);
    mountEvouiBlock(el, "asset: ia_1", port, new EmbedHub());
    await flush();
    const r = el.querySelectorAll<HTMLInputElement>("fieldset input[type=radio]")[0]!;
    r.checked = true;
    r.dispatchEvent(new Event("change"));
    el.querySelector<HTMLButtonElement>(".evoui-check")!.click();
    await flush();
    expect(got.length).toBeGreaterThan(0);
    expect(el.querySelector("[data-role=followup]")).toBeNull();
    expect(rows(el)).toHaveLength(0);
    expect(el.querySelector(".evowiki-evoui-followups")).toBeNull();
  });
});

describe("the interface", () => {
  const http = (reqs: { method: string; url: string; body?: string }[], body: unknown = {}) => ({
    request: (r: { method: string; url: string; body?: string | ArrayBuffer }) => { reqs.push(r as { method: string; url: string; body?: string }); return Promise.resolve({ status: 200, text: JSON.stringify(body) }); },
  });
  const client = (h: ReturnType<typeof http>): HttpWikiClient => new HttpWikiClient(() => ({ baseUrl: "http://x", userId: "u" }), h, undefined as never);

  it("followup posts {choice, answer_ref?} to the section of the page", async () => {
    const reqs: { method: string; url: string; body?: string }[] = [];
    const api = client(http(reqs));
    await api.followup("p1", "s 5", { choice: "slip", answer_ref: "check:p1:s5" });
    await api.followup("p1", "s5", { choice: "unclear" });
    await api.followup("p1", "s5", { choice: "learned", answer_ref: null });
    expect(reqs.map((r) => [r.method, r.url])).toEqual([
      ["POST", "http://x/wiki/pages/p1/checks/s%205/followup"], ["POST", "http://x/wiki/pages/p1/checks/s5/followup"],
      ["POST", "http://x/wiki/pages/p1/checks/s5/followup"]]);
    expect(reqs.map((r): unknown => JSON.parse(r.body!))).toEqual([{ choice: "slip", answer_ref: "check:p1:s5" }, { choice: "unclear" }, { choice: "learned" }]);
  });

  it("confirmAnswer posts {choice} to review/confirms/{id}", async () => {
    const reqs: { method: string; url: string; body?: string }[] = [];
    await client(http(reqs)).confirmAnswer("fu 1", { choice: -1 });
    expect(reqs[0]).toMatchObject({ method: "POST", url: "http://x/wiki/review/confirms/fu%201" });
    expect(JSON.parse(reqs[0]!.body!)).toEqual({ choice: -1 });
  });

  it("the assistant's placeholder", () => { expect(ASSISTANT_COPY.placeholder).toBe("这里我不太理解……"); });
});
