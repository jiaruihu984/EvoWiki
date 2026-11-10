import { FakeGoalApi } from "./learn/goal-fake";
/**
 * Task 12: the whole flow of the Obsidian redesign, on fake data only (FakeWikiApi, an in-memory vault under the real
 * note writer, the obsidian mock). Real renderers, real glue: the 今天 sidebar (WikiView), the end-of-note block, the top
 * line, the section tags and the ⋯ menu through the Reading-view post-processors, the small and the big star map. Only
 * the clock, the canvas and Obsidian itself are stand-ins. One ordered story, each `it` a step of it:
 *
 *   open → 今天 (复习 N 张 · 大约 X 分钟) → review in place (这张卡有问题…) → 继续 → read the note (⋯ on hover) → the end block →
 *   记下 → the two notices → the page patch tag and the top line → 撤回 → 需要你看的 → edit and accept → 最近改了 →
 *   想学点新的 (a recommendation → the dialog form) → the small star map → the big star map (filter, pick, card).
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import type { WorkspaceLeaf } from "obsidian";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import { notices as shownNotices } from "../../test/obsidian-mock";
import { h } from "../ui/dom";
import { CheckBlockHub } from "./check-block";
import { WikiController } from "./controller";
import { firstPage, newGoal, setup, type Setup, until, view } from "./controller-test-kit";
import { WIKI_COPY } from "./copy";
import { endBlockPostProcessor } from "./end-block/end-block-view";
import { MemVault } from "./end-block/test-kit";
import { patchedSections, topLine } from "./end-block/model";
import { claimsPage } from "./note/format";
import { syncWithVault } from "./note/sync";
import type { WikiNoteInput } from "./note-port";
import { pendingCards, reviewMinutes } from "./review-entries";
import { noteHeadings } from "./section-menu";
import { sectionMenuPostProcessor } from "./section-menu-view";
import { MiniCloud } from "./cloud/mini-cloud";
import { StarMapView } from "./starmap/starmap-view";
import { WikiView } from "./wiki-view";
import { wikiActions } from "./wiring";

// This story checks notes and navigation, not the third-party confetti animation.
vi.mock("canvas-confetti", () => ({ default: vi.fn() }));

interface Flow extends Setup {
  vault: MemVault;
  hub: CheckBlockHub<WikiController>;
  side: WikiView;
  openedMap: { count: number };
  noteHost: HTMLElement;
  cloud: MiniCloud | null;
}
let f: Flow;

const q = <T extends Element>(root: ParentNode, sel: string): T => {
  const x = root.querySelector<T>(sel);
  if (x === null) throw new Error(`no ${sel}`);
  return x;
};
/** The end block's parts (app.js endBlock): a pill by its label, the saved / error line after .ew-act. */
const chip = (end: ParentNode, label: string): HTMLButtonElement =>
  [...end.querySelectorAll<HTMLButtonElement>(".ew-pill")].find((b) => b.textContent === label)!;
const savedLine = (end: ParentNode): string | null => end.querySelector(".ew-act ~ .ew-hint")?.textContent ?? null;
const section = (name: string): HTMLElement => q(f.side.contentEl, `[data-section=${name}]`);
/** 个性化 · 活动记录's change rows (changes apply by themselves; each shows what changed, why, its effect and 撤销). */
const changeRows = (): HTMLElement[] => [...f.side.contentEl.querySelectorAll<HTMLElement>(".ew-side-body[data-tab=engine] .ew-pz-log > .ew-pz-it")];
const tabDots = (): boolean[] => [...f.side.contentEl.querySelectorAll<HTMLElement>(".ew-seg > button")].map((t) => t.querySelector(".ew-cnt") !== null);
const settle = async (): Promise<void> => { for (let i = 0; i < 12; i++) await new Promise((r) => setTimeout(r, 0)); };
const wait = async (cond: () => boolean): Promise<void> => { await until(cond); await settle(); };

/** The text of the vault's note that claims `pageId` (the real writer's output). */
function noteOf(pageId: string): string {
  for (const t of f.vault.files.values()) if (claimsPage(t, pageId)) return t;
  throw new Error(`no note for ${pageId}`);
}

/**
 * Reading view of the note the controller's open page was written as, built through the real post-processors: the top
 * line before the first content, the ⋯ and the tag in every ## heading, the end block before 我的笔记.
 */
function drawNote(pageId: string): void {
  const text = noteOf(pageId);
  const lines = text.split("\n");
  const nh = noteHeadings(text);
  if (nh === null) throw new Error("not a wiki note");
  f.noteHost.replaceChildren();
  const ctxFor = (lineStart: number, lineEnd = lineStart) =>
    ({ getSectionInfo: () => ({ text, lineStart, lineEnd }), addChild: () => undefined }) as unknown as Parameters<ReturnType<typeof sectionMenuPostProcessor>>[1];
  const start = lines.indexOf("%% evowiki:start %%");
  const first = lines.findIndex((l, i) => i > start && l.trim() !== "");
  const top = h("div", {}, h("p", {}, lines[first] ?? ""));
  f.noteHost.append(top);
  endBlockPostProcessor(f.hub)(top, ctxFor(first));
  for (const [n, ref] of nh.byLine) {
    const el = h("div", {}, h("h2", {}, ref.heading));
    f.noteHost.append(el);
    sectionMenuPostProcessor(f.hub)(el, ctxFor(n));
    endBlockPostProcessor(f.hub)(el, ctxFor(n));
  }
  const mine = lines.indexOf("## 我的笔记");
  const end = h("div", {}, h("h2", {}, "我的笔记"));
  f.noteHost.append(end);
  endBlockPostProcessor(f.hub)(end, ctxFor(mine));
}

beforeAll(async () => {
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
    () => ({ width: 300, height: 200, top: 0, left: 0, right: 300, bottom: 200, x: 0, y: 0, toJSON: () => ({}) }));
  shownNotices.length = 0;
  const s = setup();
  const vault = new MemVault();
  let n = 0;
  const deps = { store: vault, now: () => new Date(Date.UTC(2026, 9, 10, 8, 0, n)), rand: () => `abcd${String(++n).padStart(4, "0")}` };
  s.sync.run = async (input: WikiNoteInput) => { s.sync.runs.push(input); return syncWithVault(deps, "Notes", input); };
  // The learner already has a first page (p1, a z-score page: its cards come due) and then names a bigger goal (a path).
  await firstPage(s.c);
  await newGoal(s.c, "学统计推断");
  const hub = new CheckBlockHub<WikiController>();
  hub.set(s.c);
  const openedMap = { count: 0 };
  const holder: { cloud: MiniCloud | null } = { cloud: null };
  const side = new WikiView({} as WorkspaceLeaf, s.c, wikiActions(() => s.c), () => ({ renderMarkdown: (el, md) => { el.textContent = md; }, names: s.c.names }),
    null, null, () => (holder.cloud = new MiniCloud({ openStarMap: () => { openedMap.count++; }, onCssChange: () => () => undefined,
      goals: { source: () => s.c.graphSource(), openGoal: () => undefined, progress: () => null, followStep: () => undefined, configured: () => true } })));
  const noteHost = h("div", { class: "markdown-preview-view evowiki-page" });
  document.body.append(side.contentEl, noteHost);
  f = { ...s, vault, hub, side, openedMap, noteHost, get cloud() { return holder.cloud; } };
});
afterAll(() => {
  vi.restoreAllMocks();
  document.body.replaceChildren();
});

describe("the whole flow (fake data)", () => {
  it("1 open → 今天: 待办事项, 想学点新的？, 知识图谱 in order (no 需要你看的); the review task card names the cards and the time", async () => {
    await f.side.onOpen();
    await wait(() => f.c.state.review.today.kind === "ready" && f.c.state.catalog.kind === "ready");
    const groups = [...f.side.contentEl.querySelectorAll<HTMLElement>(".ew-side-body > [data-section]")].map((x) => x.dataset.section);
    expect(groups).toEqual(["next", "learn", "map"]);
    const n = pendingCards(f.c.state).length;
    expect(n).toBeGreaterThan(0);
    const next = section("next");
    expect(q(next, ".ew-task h2").textContent).toBe("今日复习");
    expect(q(next, ".ew-task .ew-faint").textContent).toBe(`${n} 张 · 约 ${reviewMinutes(n)} 分钟`);
    expect(next.querySelector("button.ew-bigbtn, [data-role=then]")).toBeNull();
    // The header has exactly the three tabs; the changes 个性化 has not shown yet (the fixtures') put the red dot on 个性化, not on 今天 (shown).
    const tabs = [...f.side.contentEl.querySelectorAll<HTMLElement>(".ew-seg > button")];
    expect(tabs.map((t) => t.firstChild?.textContent)).toEqual(["今天", "助手", "个性化"]);
    expect(tabs.map((t) => t.querySelector(".ew-cnt") !== null)).toEqual([false, false, true]);
    expect(f.side.contentEl.querySelector(".ew-attn, .ew-prop")).toBeNull();
    expect(f.side.contentEl.textContent).not.toContain("最近改了");
  });

  it("2 review in place: one card at a time, a grade moves on, 反馈 hands the card to the assistant, 先不复习了 keeps the rest", async () => {
    const n = pendingCards(f.c.state).length;
    q<HTMLButtonElement>(section("next"), ".ew-task [data-action=bot-start]").click();
    expect(q(section("next"), ".ew-rvtop .ew-rvbar")).not.toBeNull();
    // 反馈 on the first card: the assistant opens with the card quoted (nothing is sent, no box in the card).
    f.asked.length = 0;
    f.api.calls.length = 0;
    const fb = q<HTMLButtonElement>(section("next"), "[data-action=card-fb]");
    expect(fb.textContent).toBe(WIKI_COPY.reviewProblem);
    fb.click();
    expect(f.asked).toHaveLength(1);
    expect(f.asked[0]!.quote.text.startsWith("这张卡片：")).toBe(true);
    // Grade the first card (cue: uncover then 想起来了; check: the first option), then pause.
    const answerCard = async (): Promise<void> => {
      const reveal = section("next").querySelector<HTMLButtonElement>("[data-action=review-reveal]");
      if (reveal !== null) {
        reveal.click(); q<HTMLButtonElement>(section("next"), "[data-action=review-grade]").click();
        await settle();                    // a self-grade moves on by itself: no 下一张 click
        return;
      }
      q<HTMLButtonElement>(section("next"), "[data-action=review-answer]").click();
      await settle();
      // an answered check card stays (its marks) until 下一张 / 完成
      q<HTMLButtonElement>(section("next"), "[data-action=review-next]").click();
      await settle();
    };
    await answerCard();
    expect(f.c.state.review.seen.length).toBe(1);
    q<HTMLButtonElement>(section("next"), "[data-action=review-pause]").click();
    expect(q(section("next"), ".ew-task .ew-faint").textContent).toBe(`${n - 1} 张 · 约 ${reviewMinutes(n - 1)} 分钟`);
    // Back in, through the rest: after the last card the review task card is gone (no 复习完了。 line, no 继续 button).
    q<HTMLButtonElement>(section("next"), ".ew-task [data-action=bot-start]").click();
    for (let i = 0; i < n + 2 && f.c.state.review.active; i++) await answerCard();
    expect(f.c.state.review.active).toBe(false);
    expect(section("next").querySelector("[data-role=review-done], button.ew-bigbtn")).toBeNull();
    // what follows is the goal's own step (GET /wiki/progress), not a review card's 继续
    expect(section("next").querySelector(".ew-task [data-action=continue]")).toBeNull();
    expect([...section("next").querySelectorAll(".ew-task h2")].some((h) => h.textContent?.startsWith("复习 "))).toBe(false);
  });

  it("3 继续: the path's next page is made, its note is written once and opened", async () => {
    const before = f.vault.files.size;
    f.opened.length = 0;
    void f.c.continueLearning();
    await wait(() => f.c.state.page.kind === "open" && f.c.state.busy === null && f.vault.files.size > before);
    expect(f.opened.length).toBe(1);
    expect(view(f.c).page.organized).toBe(true);
  });

  it("4 read the note: a quiet body, the ⋯ is a button in every section heading that opens the section menu", () => {
    const id = view(f.c).page.page_id;
    drawNote(id);
    const buttons = [...f.noteHost.querySelectorAll<HTMLButtonElement>("button.ew-more")];
    expect(buttons.length).toBeGreaterThan(0);
    expect(buttons.every((b) => b.closest("h2") !== null && b.textContent === "⋯")).toBe(true);
    // Reading view of the note text shows none of the drawn parts: they live only in the DOM.
    const text = noteOf(id);
    for (const s of [WIKI_COPY.endTitle, WIKI_COPY.feelHard, WIKI_COPY.sectionTag, WIKI_COPY.topUndo]) expect(text).not.toContain(s);
    // Hover-only: the stylesheet hides the button until its heading is hovered or the button has focus.
    const css = readStyles();
    expect(css).toMatch(/\.ew-more\{margin-left:auto;opacity:0/);
    expect(css).toMatch(/\.ew-more:focus-visible/);
    const visible = buttons[0]!;
    visible.click();
    const titles = [...f.noteHost.querySelectorAll(".ew-menu > button")].map((b) => b.textContent);
    expect(titles).toEqual([WIKI_COPY.know, WIKI_COPY.menuDont, WIKI_COPY.menuAsk, WIKI_COPY.pin]);
    expect(visible.getAttribute("aria-expanded")).toBe("true");
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(visible.getAttribute("aria-expanded")).toBe("false");
    expect(f.noteHost.querySelector(".ew-menu")).toBeNull();
  });

  it("5 the end block comes before 我的笔记: the pills are one choice; 基本合适 sends nothing", async () => {
    const id = view(f.c).page.page_id;
    const end = q(f.noteHost, ".ew-endb");
    expect(end.closest(".evowiki-end-host")?.nextElementSibling?.textContent).toContain("我的笔记");
    expect(end.querySelector("h3")?.textContent).toBe(WIKI_COPY.endTitle);
    expect(end.querySelector("textarea, input")).toBeNull();
    f.api.calls.length = 0;
    chip(end, "基本合适").click();
    expect(chip(end, "基本合适").getAttribute("aria-pressed")).toBe("true");
    await settle();
    expect(f.api.calls.some((c) => c.method === "pageFeedback")).toBe(false);
    expect(f.sentFresh).toEqual([]);
    expect(savedLine(end)).toBeNull();
    expect(f.c.endBlockState(id).draft.chips).toEqual(["right"]);
  });

  it("6 理解有困难 goes to the assistant; the assistant's page change (feedback applied on the server) lands in the note", async () => {
    const id = view(f.c).page.page_id;
    const end = q(f.noteHost, ".ew-endb");
    chip(end, "理解有困难").click();
    expect([chip(end, "理解有困难"), chip(end, "基本合适")].map((c) => c.getAttribute("aria-pressed"))).toEqual(["true", "false"]);
    expect(f.sentFresh.map((x) => [x.pageId, x.text])).toEqual([[id, "这页理解有困难"]]);
    // What the assistant then does on the server, as the page edit it reports.
    f.notices.length = 0;
    await f.api.pageFeedback(id, { chips: ["too_hard"], text: "术语没先讲就用上了", section_id: null, apply: true });
    await f.c.pageEditedElsewhere(id);
    await f.c.reloadRsi();
    await settle();
    expect(f.notices).toHaveLength(0);
    // The applied 讲法 change lights 个性化's dot.
    expect(tabDots()).toEqual([false, false, true]);
    expect(savedLine(end)).toBeNull();
  });

  it("7 the page now says what changed: the top line (with 撤销) and 「已更新」 on the patched section", () => {
    const id = view(f.c).page.page_id;
    const tl = topLine(f.c.state, id);
    expect(tl?.kind).toBe("changed");
    const line = q<HTMLElement>(f.noteHost, ".ew-topline");
    expect(line.textContent).toContain("本页已更新");
    expect(q(line, "button.ew-lk").textContent).toBe(WIKI_COPY.topUndo);
    const tags = [...f.noteHost.querySelectorAll<HTMLElement>("h2 .ew-tag")];
    expect(tags).toHaveLength(patchedSections(f.c.state, id).size);
    expect(tags.length).toBeGreaterThan(0);
    expect(tags[0]!.textContent).toBe(WIKI_COPY.sectionTag);
  });

  it("8 撤销 takes the patch back: the line and the tags go, the restore notice is said once", async () => {
    const id = view(f.c).page.page_id;
    f.notices.length = 0;
    q<HTMLButtonElement>(f.noteHost, ".ew-topline .ew-lk").click();
    await wait(() => f.api.calls.some((c) => c.method === "undoFeedback") && f.c.state.busy === null);
    expect(f.notices).toContain(WIKI_COPY.noticeUndone);
    expect(f.noteHost.querySelector(".ew-topline")).toBeNull();
    expect(f.noteHost.querySelectorAll("h2 .ew-tag").length).toBe(0);
    expect(patchedSections(f.c.state, id).size).toBe(0);
  });

  it("9 个性化 · 活动记录: an applied change with its why and 撤销; no 接受 / 不用; opening the tab clears the dot; 撤销 undoes it", async () => {
    f.c.setTab("engine");
    f.c.personalView({ log: true, older: true });
    await wait(() => changeRows().length > 0);
    expect(tabDots()).toEqual([false, false, false]);   // the changes are seen now; a wait for a yes / no waits on its page, not here
    const tab = q<HTMLElement>(f.side.contentEl, ".ew-side-body[data-tab=engine]");
    for (const word of ["接受", "不用", "等你决定"]) expect(q(tab, ".ew-pz-log").textContent, word).not.toContain(word);
    const row = changeRows().find((r) => r.dataset.chg === "rsi:pa2")!;
    expect(q(row, ".ew-pz-t").textContent).not.toBe("");
    expect(q(row, ":scope > .ew-qtip .ew-tipbox > span").textContent).not.toBe("");   // the reason is in the ? tooltip
    expect(q(row, ":scope > .ew-btn.ew-sm").textContent).toBe("撤销");
    expect(row.querySelector(".ew-st, [data-role=target], [data-role=effect]")).toBeNull();
    const key = row.dataset.chg;
    f.api.calls.length = 0;
    q<HTMLButtonElement>(row, "[data-action=rsi-undo]").click();
    await wait(() => f.api.calls.some((c) => c.method === "undoPatch") && f.c.state.busy === null);
    expect(f.api.calls.find((c) => c.method === "undoPatch")?.args[0]).toBe("pa2");
    expect(f.api.calls.map((c) => c.method)).toContain("rsiChanges");
    const undone = changeRows().find((r) => r.dataset.chg === key)!;
    expect(q(undone, ".ew-pz-t").classList.contains("ew-off")).toBe(true);
    expect(q(undone, ".ew-pz-tag").textContent).toBe("已撤销");
    // Back to 今天: seen changes do not light the dot again.
    f.c.setTab("today");
    await settle();
    expect(tabDots()).toEqual([false, false, false]);
  });

  it("10 想学点新的: K-derived recommendations (at most three); one opens the 学点新的 tab with its name", () => {
    const learn = section("learn");
    expect(q(learn, "h2").textContent).toBe(WIKI_COPY.learnTitle);
    const recs = [...learn.querySelectorAll<HTMLButtonElement>("[data-action=recommend]")];
    expect(recs.length).toBeGreaterThan(0);
    expect(recs.length).toBeLessThanOrEqual(3);
    const name = recs[0]!.querySelector("b")?.textContent ?? "";
    f.learned.length = 0;
    recs[0]!.click();
    expect(f.learned).toEqual([name]);
  });

  it("11 知识图谱 in 今天: the small cloud draws the page's own graph once it is read; a click opens the big map", async () => {
    const block = section("map");
    expect(q(block, "h2").textContent).toBe("知识图谱");
    const mini = q<HTMLElement>(block, ".ew-mini");
    expect(mini).toBe(f.cloud!.root);
    await wait(() => f.cloud!.drawn !== null);
    expect(f.cloud!.drawn!.nodes.length).toBeGreaterThan(0);
    mini.click();
    expect(f.openedMap.count).toBe(1);
  });

  it("12 知识图谱: the graph page reads GET /wiki/graph; a pick from the hidden list opens the node card with its learning-path navigation", async () => {
    // The Cloud's browser APIs (jsdom has none of them): no frames, reduced motion, a ResizeObserver that never fires.
    vi.stubGlobal("requestAnimationFrame", () => 1);
    vi.stubGlobal("cancelAnimationFrame", () => undefined);
    vi.stubGlobal("matchMedia", () => ({ matches: true }));
    vi.stubGlobal("ResizeObserver", class { observe(): void {} disconnect(): void {} });
    try {
      const big = new StarMapView({} as WorkspaceLeaf, { source: f.c.graphSource(), goalApi: () => new FakeGoalApi({ roundMs: 0 }), openLearn: () => undefined, openGoal: () => undefined, enterRegion: () => undefined, showPage: () => undefined, writePage: () => undefined, openRow: () => undefined, onCssChange: () => () => undefined });
      document.body.append(big.contentEl);
      await big.onOpen();
      await wait(() => big.contentEl.querySelector("ul.evowiki-sr-only [data-node]") !== null);
      expect(big.contentEl.querySelector(".ew-legend")!.textContent).toBe("掌握熟悉不会");
      const py = [...big.contentEl.querySelectorAll<HTMLButtonElement>("ul.evowiki-sr-only [data-node]")].find((b) => b.dataset.node === "py")!;
      py.click();
      const card = q(big.contentEl, "[data-card=node]");
      expect(card.querySelector("h2")!.textContent).toBe("Python掌握");
      expect(card.querySelector("button")).not.toBeNull();   // the card only tells (no 我想学这个)
      await big.onClose();
    } finally { vi.unstubAllGlobals(); }
  });
});

/** styles.css as text (the hover rules cannot be computed in jsdom). */
const readStyles = (): string => readFileSync(resolve(process.cwd(), "styles.css"), "utf8");
