import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderNext } from "../render-next";
import { fakeActions, fakeCtx } from "../render-test-kit";
import { INITIAL_WIKI_STATE, type WikiState } from "../state";
import { confetti, renderDoneCard } from "./done-card";

const { fire, create } = vi.hoisted(() => {
  const fire = vi.fn((_o: unknown) => Promise.resolve());
  return { fire, create: vi.fn((_c: unknown, _o: unknown) => fire) };
});
vi.mock("canvas-confetti", () => ({ default: { create } }));

/** The 500 / 1400 ms animation windows read Date.now(): freeze it so a slow run cannot close a window mid-test. */
beforeEach(() => { vi.useFakeTimers({ now: new Date(2026, 9, 15, 9, 0) }); });
afterEach(() => { document.body.replaceChildren(); vi.restoreAllMocks(); vi.useRealTimers(); fire.mockClear(); create.mockClear(); });

const st = (n: number, ok: number, streak?: number, age = 0): WikiState => ({
  ...INITIAL_WIKI_STATE, configured: true, conn: "online",
  review: { ...INITIAL_WIKI_STATE.review, today: { kind: "ready", value: { items: [], due: 0, done_today: n, limit: 10, confirms: [], page_round_today: 0, streak_days: streak ?? 0 } } },
  bot: { ...INITIAL_WIKI_STATE.bot, today: { kind: "ready", value: [] }, celebrate: { n, ok, at: Date.now() - age } } });
const draw = (s: WikiState) => {
  const a = fakeActions();
  const c = renderDoneCard(s, a);
  if (c !== null) document.body.append(c);
  return { c, a };
};
const cells = (c: HTMLElement): string[][] => [...c.querySelectorAll(".ew-dstats > div")].map((d) => [d.querySelector("b")!.firstChild!.textContent!, d.querySelector("b small")?.textContent ?? "", d.querySelector("span")!.textContent]);

describe("done card", () => {
  beforeEach(() => { vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null); });

  it("3/3: title 复习完成, no sentence, the stat cells with the unit in a small span", () => {
    const { c } = draw(st(3, 3, 6));
    expect(c!.querySelector("h2")?.textContent).toBe("复习完成");
    expect(c!.querySelector(".ew-dtext .ew-muted")).toBeNull();
    expect(cells(c!)).toEqual([["3", "张", "已复习"], ["3 / 3", "", "答对"], ["6", "天", "连续学习"]]);
  });

  it("0/2: 复习完成 and 2 张 明日待复习", () => {
    const { c } = draw(st(2, 0, 1));
    expect(c!.querySelector("h2")?.textContent).toBe("复习完成");
    expect(cells(c!)).toEqual([["2", "张", "已复习"], ["2", "张", "明日待复习"], ["1", "天", "连续学习"]]);
  });

  it("2/3: 1 张 明日待复习", () => {
    const { c } = draw(st(3, 2));
    expect(c!.querySelector("h2")?.textContent).toBe("复习完成");
    expect(cells(c!).map((x) => x[2])).toEqual(["已复习", "明日待复习"]);
  });

  it("streak_days 0: the 连续学习 cell is not drawn", () => {
    expect(cells(draw(st(3, 3)).c!)).toEqual([["3", "张", "已复习"], ["3 / 3", "", "答对"]]);
  });

  it("structure: .ew-card.ew-donec.ew-anim, × button, ring with tr / fg / ck; the same canvas and anim on a re-render, plain after 1400 ms", () => {
    vi.useFakeTimers();
    const s = st(3, 3, 2);
    const { c } = draw(s);
    expect(c!.className).toBe("ew-card ew-donec ew-anim");
    const x = c!.querySelector<HTMLButtonElement>("button.ew-dclose")!;
    expect([x.textContent, x.title, x.getAttribute("aria-label")]).toEqual(["×", "", "关闭"]);
    expect([...c!.querySelectorAll(".ew-ring svg > *")].map((e) => e.getAttribute("class"))).toEqual(["ew-tr", "ew-fg", "ew-ck"]);
    const cv = c!.querySelector("canvas.ew-confetti")!;
    expect(cv).not.toBeNull();
    vi.advanceTimersByTime(300);
    const again = renderDoneCard(s, fakeActions())!;
    document.body.replaceChildren(again);
    expect(again.className).toBe("ew-card ew-donec ew-anim");
    expect(again.querySelector("canvas")).toBe(cv);
    expect(cv.isConnected).toBe(true);
    vi.advanceTimersByTime(1200);
    const late = renderDoneCard(s, fakeActions())!;
    expect(late.className).toBe("ew-card ew-donec");
    expect(late.querySelector("canvas")).toBe(cv);
    expect(draw(st(1, 1, undefined, 5000)).c!.querySelector("canvas")).toBeNull();
  });

  it("× calls closeCelebrate", () => {
    const { c, a } = draw(st(1, 1));
    c!.querySelector<HTMLButtonElement>(".ew-dclose")!.click();
    expect(a.closeCelebrate).toHaveBeenCalledTimes(1);
  });

  it("no celebration: nothing; in 待办事项 the card comes right after the title", () => {
    expect(renderDoneCard({ ...INITIAL_WIKI_STATE }, fakeActions())).toBeNull();
    const root = renderNext(st(2, 1, 3), fakeActions(), fakeCtx());
    expect(root.children[1]?.className).toContain("ew-donec");
  });
});

describe("confetti (canvas-confetti)", () => {
  const rect = (r: Partial<DOMRect>): DOMRect => ({ left: 0, top: 0, width: 0, height: 0, right: 0, bottom: 0, x: 0, y: 0, toJSON: () => ({}), ...r });
  const setup = (style = ""): { cv: HTMLCanvasElement; ring: HTMLElement } => {
    const cv = document.createElement("canvas");
    const ring = document.createElement("div");
    ring.setAttribute("style", style);
    document.body.append(cv, ring);
    vi.spyOn(cv, "getBoundingClientRect").mockReturnValue(rect({ left: 10, top: 20, width: 200, height: 100 }));
    vi.spyOn(ring, "getBoundingClientRect").mockReturnValue(rect({ left: 60, top: 40, width: 100, height: 60 }));
    return { cv, ring };
  };

  it("fires 36 pieces from the ring's centre after 850 ms, on this canvas, (celebrations ignore reduced motion)", () => {
    const { cv, ring } = setup();
    confetti(cv, ring);
    vi.advanceTimersByTime(849);
    expect(create).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(create).toHaveBeenCalledWith(cv, { resize: true, useWorker: false, disableForReducedMotion: false });
    expect(fire).toHaveBeenCalledTimes(1);
    const o = fire.mock.calls[0]![0] as Record<string, unknown>;
    expect(o).toMatchObject({ particleCount: 36, angle: 90, ticks: 90, origin: { x: (60 - 10 + 50) / 200, y: (40 - 20 + 30) / 100 } });
    expect(o.ticks as number / 60 * 1000).toBe(1500); // ~1500 ms at the library's 60 fps pace
  });

  it("uses the theme's colours (--known, --viz-1, --viz-2, --shaky); none set means the library's own", () => {
    const a = setup("--known:#2f9e5b;--viz-1:#2f6fdf;--viz-2:#e07b2a;--shaky:#d99a1e");
    confetti(a.cv, a.ring);
    vi.advanceTimersByTime(850);
    expect((fire.mock.calls[0]![0] as { colors: string[] }).colors).toEqual(["#2f9e5b", "#2f6fdf", "#e07b2a", "#d99a1e"]);
    fire.mockClear();
    const b = setup();
    confetti(b.cv, b.ring);
    vi.advanceTimersByTime(850);
    expect(fire.mock.calls[0]![0]).not.toHaveProperty("colors");
  });

  it("draws nothing for a canvas removed before the burst, an empty canvas, or one that throws (jsdom's stub)", () => {
    const a = setup();
    confetti(a.cv, a.ring);
    a.cv.remove();
    vi.advanceTimersByTime(900);
    const b = setup();
    vi.spyOn(b.cv, "getBoundingClientRect").mockReturnValue(rect({}));
    confetti(b.cv, b.ring);
    vi.advanceTimersByTime(900);
    expect(create).not.toHaveBeenCalled();
    create.mockImplementationOnce(() => { throw new Error("no 2d context"); });
    const c = setup();
    confetti(c.cv, c.ring);
    expect(() => vi.advanceTimersByTime(900)).not.toThrow();
  });
});
