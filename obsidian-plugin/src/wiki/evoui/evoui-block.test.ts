import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { OfflineError } from "../../api/errors";
import { setup } from "../controller-test-kit";
import { WIKI_COPY } from "../copy";
import { EmbedHub } from "../embed/embed-block";
import { useTransportForTests, resetFramesForTests } from "./frame-host";
import { trustEngineForTests } from "./frame/engine-safety";
import { inlineTransport } from "./frame/inline-transport";
import { EXAMPLES, fence, SONNET } from "./evoui-fixtures";
import type { BlockProgressData } from "./block-event";
import { type BlockEvent, type EvouiPort, mountEvouiBlock } from "./evoui-block";
import { resetMathForTests } from "./math";

const ASSET = "EvoWiki/Wiki/assets/ia_1.json";
const frame = (): Promise<void> => new Promise((r) => { requestAnimationFrame(() => { r(); }); });
const flush = async (): Promise<void> => { for (let i = 0; i < 5; i++) await Promise.resolve(); };
type TestPort = EvouiPort & { events: [string, BlockEvent][]; retrieved: string[] };

function port(files: Record<string, string> = {}, over: Partial<EvouiPort> = {}): TestPort {
  const events: [string, BlockEvent][] = [];
  const retrieved: string[] = [];
  return {
    events, retrieved,
    read: (p) => Promise.resolve(files[p] ?? null),
    canFetch: () => true,
    assetPath: (id) => `EvoWiki/Wiki/assets/${id}.json`,
    retrieve: (id) => { retrieved.push(id); return Promise.resolve(false); },
    onEvent: (id, ev) => { events.push([id, ev]); },
    ...over,
  };
}
const mount = (src: string, p: EvouiPort, hub = new EmbedHub()): { el: HTMLElement; off: () => void } => {
  const el = document.createElement("div");
  document.body.append(el);
  return { el, off: mountEvouiBlock(el, src, p, hub) };
};
let warn: ReturnType<typeof vi.spyOn>;
let err: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  trustEngineForTests(true);          // the self-test has its own tests (engine-safety.test.ts)
  useTransportForTests(inlineTransport);
  resetFramesForTests();
  warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
  err = vi.spyOn(console, "error").mockImplementation(() => undefined);
});
afterEach(() => { warn.mockRestore(); err.mockRestore(); document.body.replaceChildren(); });

describe("an inline evoui block", () => {
  it("draws D1's variance example in place: dragging the slider updates text and plot without rebuilding the DOM", async () => {
    const { el } = mount(fence(EXAMPLES[0]), port());
    expect(el.querySelector(".evowiki-evoui .evoui .evoui-title")?.textContent).toBe("分散程度和方差");
    const slider = el.querySelector<HTMLInputElement>("input[type=range]")!;
    const label = slider.parentElement!.querySelector("span")!;
    const before = label.textContent;
    expect(before).toContain("分散程度：1.0");
    slider.value = "2.5";
    slider.dispatchEvent(new Event("input"));
    await frame();
    expect(label.textContent).toContain("2.5");
    expect(label.textContent).not.toBe(before);
    expect(el.querySelector("input[type=range]")).toBe(slider);          // same element: not rebuilt
    expect(el.querySelector(".evoui-badge")?.textContent).toBe("很散");   // derived text followed
    expect(el.querySelector(".evoui-plot")).not.toBeNull();
    expect(el.querySelector(".evowiki-evoui-fallback")).toBeNull();
  });

  it("draws every Sonnet block and a slider moves its readout", async () => {
    for (const b of SONNET) {
      const { el } = mount(fence(b), port());
      expect(el.querySelector(".evoui-title")?.textContent).toBeTruthy();
      expect(el.querySelector(".evoui-error")).toBeNull();
      const range = el.querySelector<HTMLInputElement>("input[type=range]");
      if (range !== null) {
        const text = el.textContent;
        range.value = range.value === range.max ? range.min : range.max;
        range.dispatchEvent(new Event("input"));
        await frame();
        expect(el.textContent).not.toBe(text);
      }
    }
  });

  it("an invalid block shows its own fallback text; without one, a plain line; nothing throws", () => {
    const bad: Record<string, unknown> = { ...EXAMPLES[0], body: { kind: "script", src: "x" } };
    const a = mount(fence(bad), port());
    expect(a.el.querySelector(".evowiki-evoui-fallback")?.textContent).toBe(String(bad.fallback));
    expect(a.el.querySelector(".evoui")).toBeNull();
    const noFb = { ...bad };
    delete noFb.fallback;
    expect(mount(fence(noFb), port()).el.textContent).toBe(WIKI_COPY.evouiUnreadable);
    expect(mount("{broken", port()).el.textContent).toBe(WIKI_COPY.evouiUnreadable);
    expect(mount("", port()).el.textContent).toBe(WIKI_COPY.evouiUnreadable);
  });

  it("a malicious expression is rejected before anything is drawn (fallback shown)", () => {
    const evil = { ...EXAMPLES[0], derived: { ...(EXAMPLES[0]!.derived as object), x: "k.constructor" } };
    const { el } = mount(fence(evil), port());
    expect(el.querySelector(".evoui")).toBeNull();
    expect(el.querySelector(".evowiki-evoui-fallback")).not.toBeNull();
  });

  it("a renderer that throws shows the fallback, not an error", () => {
    const engine = window.EvoUI!;
    const orig = engine.render.bind(engine);
    engine.render = () => { throw new Error("boom"); };
    try {
      const { el } = mount(fence(EXAMPLES[0]), port());
      expect(el.querySelector(".evowiki-evoui-fallback")?.textContent).toBe(String(EXAMPLES[0]!.fallback));
    } finally { engine.render = orig; }
  });

  it("a run-time error inside the renderer shows the fallback text inside the block", () => {
    const rt = {
      type: "evoui", title: "t", question: "q", state: { a: 1 }, derived: { d: "map(a, x => x)" }, fallback: "文字版",
      body: { kind: "text", text: "{d}" },                              // map of a number: fails only when evaluated
    };
    const { el } = mount(fence(rt), port());
    expect(el.querySelector(".evoui-error")?.textContent).toContain("文字版");
  });

  it("cleanup does not throw", () => {
    const { off } = mount(fence(EXAMPLES[0]), port());
    expect(() => off()).not.toThrow();
  });
});

describe("result events", () => {
  const quiz = EXAMPLES[1]!;
  const correct = [...JSON.stringify(quiz).matchAll(/"correct":(\d)/g)].map((m) => Number(m[1]));
  const answer = (el: HTMLElement, picks: number[]): void => {
    el.querySelectorAll("fieldset").forEach((f, i) => {
      const r = f.querySelectorAll<HTMLInputElement>("input[type=radio]")[picks[i]!]!;
      r.checked = true;
      r.dispatchEvent(new Event("change"));
    });
  };
  const asAsset = async (block: unknown, p: TestPort = port({ "EvoWiki/Wiki/assets/ia_q.json": JSON.stringify(block) })) => {
    const m = mount("asset: ia_q", p);
    await flush();
    return { ...m, p };
  };

  it("an asset block hands the renderer's events to onEvent under the asset id: one item per question and a summary per judged press", async () => {
    const { el, p } = await asAsset(quiz);
    const n = el.querySelectorAll("fieldset").length;
    expect(n).toBe(correct.length);
    answer(el, correct);
    const check = el.querySelector<HTMLButtonElement>(".evoui-check")!;
    check.click();
    await flush();
    expect(p.events.every(([id]) => id === "ia_q")).toBe(true);
    const evs = p.events.map(([, ev]) => ev);
    expect(evs.filter((ev) => ev.type === "item").map((ev) => [ev.item, ev.response, ev.correct])).toEqual(correct.map((c, i) => [i, c, true]));
    expect(evs.filter((ev) => ev.type === "summary")).toHaveLength(1);
    expect(evs.every((ev) => ev.block === "ia_q" && typeof ev.session === "string" && ev.attempt === 1)).toBe(true);
    check.click();                           // an unchanged re-press is not a new attempt: no new events
    await flush();
    expect(p.events).toHaveLength(evs.length);
    answer(el, correct.map((c) => (c === 0 ? 1 : 0)));
    check.click();
    await flush();
    const second = p.events.slice(evs.length).map(([, ev]) => ev);
    expect(second.filter((ev) => ev.type === "item").every((ev) => ev.attempt === 2 && ev.correct === false)).toBe(true);
  });

  it("a multi-select answer (which onQuizResult never carried) arrives as an item event with the chosen list", async () => {
    const multi = { type: "evoui", title: "多选", question: "?", state: {}, derived: {}, fallback: "f",
      body: { kind: "quiz", questions: [{ q: "哪些是偶数？", type: "multi", options: ["1", "2", "4"], correct: [1, 2], why: "w" }] } };
    const { el, p } = await asAsset(multi);
    const boxes = el.querySelectorAll<HTMLInputElement>("fieldset input[type=checkbox]");
    for (const i of [1, 2]) { boxes[i]!.checked = true; boxes[i]!.dispatchEvent(new Event("change")); }
    el.querySelector<HTMLButtonElement>(".evoui-check")!.click();
    await flush();
    const item = p.events.map(([, ev]) => ev).find((ev) => ev.type === "item");
    expect(item).toMatchObject({ kind: "quiz", item: 0, response: [1, 2], correct: true });
  });

  it("an inline block reports nothing (the server grades against the stored asset)", async () => {
    const p = port();
    const { el } = mount(fence(quiz), p);
    answer(el, correct);
    el.querySelector<HTMLButtonElement>(".evoui-check")!.click();
    await flush();
    expect(p.events).toEqual([]);
  });

  it("a listener that throws, or none, never breaks the block", async () => {
    const files = { "EvoWiki/Wiki/assets/ia_q.json": JSON.stringify(quiz) };
    const { el } = await asAsset(quiz, port(files, { onEvent: () => { throw new Error("x"); } }));
    answer(el, correct);
    expect(() => el.querySelector<HTMLButtonElement>(".evoui-check")!.click()).not.toThrow();
    const none = port(files);
    delete (none as { onEvent?: unknown }).onEvent;
    const m2 = await asAsset(quiz, none);
    answer(m2.el, correct);
    expect(() => m2.el.querySelector<HTMLButtonElement>(".evoui-check")!.click()).not.toThrow();
  });
});

describe("progress save and restore", () => {
  const quiz = EXAMPLES[1]!;
  const files = { "EvoWiki/Wiki/assets/ia_q.json": JSON.stringify(quiz) };
  const wait = (ms: number): Promise<void> => new Promise((r) => { setTimeout(r, ms); });

  it("an asset block reports its progress; a later mount gets it back and shows the saved answers (restore sends no events)", async () => {
    const saved: BlockProgressData[] = [];
    const first = port(files, { progress: () => Promise.resolve(null), onProgress: (_id, p) => { saved.push(p); } });
    const a = mount("asset: ia_q", first);
    await flush();
    const radios = a.el.querySelectorAll<HTMLInputElement>("fieldset input[type=radio]");
    radios[0]!.checked = true;
    radios[0]!.dispatchEvent(new Event("change"));
    await wait(350);                        // evoui.js debounces onProgress by 250 ms
    expect(saved.length).toBeGreaterThan(0);
    const last = saved[saved.length - 1]!;
    expect(last.revision).toBeGreaterThan(0);
    a.off();
    const second = port(files, { progress: (id) => Promise.resolve(id === "ia_q" ? last : null) });
    const b = mount("asset: ia_q", second);
    await flush();
    expect(b.el.querySelector<HTMLInputElement>("fieldset input[type=radio]")!.checked).toBe(true);
    expect(second.events).toEqual([]);
  });

  it("an unreadable progress (rejected) renders the block fresh; an inline block neither restores nor saves", async () => {
    const seen: string[] = [];
    const p = port(files, { progress: () => Promise.reject(new Error("offline")), onProgress: (id) => { seen.push(id); } });
    const { el } = mount("asset: ia_q", p);
    await flush();
    expect(el.querySelector("fieldset")).not.toBeNull();
    const inlineSeen: string[] = [];
    const q = port({}, { progress: (id) => { inlineSeen.push(id); return Promise.resolve(null); }, onProgress: (id) => { inlineSeen.push(id); } });
    const inline = mount(fence(quiz), q);
    inline.el.querySelector<HTMLInputElement>("fieldset input[type=radio]")!.click();
    await wait(350);
    expect(inlineSeen).toEqual([]);
  });
});

describe("an asset reference", () => {
  const files: Record<string, string> = { [ASSET]: JSON.stringify(EXAMPLES[0]) };

  it("reads the cached assets/<id>.json and draws it", async () => {
    const { el } = mount("asset: ia_1", port(files));
    await flush();
    expect(el.querySelector(".evoui-title")?.textContent).toBe("分散程度和方差");
  });

  it("a missing file is fetched once by itself and a failed fetch says so; a written asset redraws the waiting block", async () => {
    const p = port();
    const { el } = mount("asset: ia_1", p);
    await flush(); await flush();
    expect(p.retrieved).toEqual(["ia_1"]);
    expect(el.textContent).toContain(WIKI_COPY.evouiMissing);
    expect(el.textContent).toContain(WIKI_COPY.embedFetchFailed);
    expect(el.querySelector(".evowiki-embed-fetch")).not.toBeNull();

    let late = false;
    const hub = new EmbedHub();
    const m2 = mount("asset: ia_1", port({}, { read: (path) => Promise.resolve(late ? files[path] ?? null : null) }), hub);
    await flush();
    expect(m2.el.querySelector(".evoui")).toBeNull();
    late = true;
    hub.assetWritten("ia_1");
    await flush();
    expect(m2.el.querySelector(".evoui-title")?.textContent).toBe("分散程度和方差");
  });

  it("no 取回 button offline; a file that is not JSON shows the plain line; an invalid block shows its fallback", async () => {
    const a = mount("asset: ia_1", port({}, { canFetch: () => false }));
    await flush();
    expect(a.el.querySelector(".evowiki-embed-fetch")).toBeNull();
    const b = mount("asset: ia_1", port({ [ASSET]: "<html>" }));
    await flush();
    expect(b.el.textContent).toBe(WIKI_COPY.evouiUnreadable);
    const c = mount("asset: ia_1", port({ [ASSET]: JSON.stringify({ ...EXAMPLES[0], body: { kind: "x" } }) }));
    await flush();
    expect(c.el.querySelector(".evowiki-evoui-fallback")).not.toBeNull();
  });

  it("an id the vault cannot hold is unreadable, not fetched", () => {
    const { el } = mount("asset: ia_1", port({}, { assetPath: () => null }));
    expect(el.textContent).toBe(WIKI_COPY.evouiUnreadable);
  });
});

describe("the engine itself (defense in depth)", () => {
  it("evalExpr never reaches the prototype chain or a global", () => {
    const eng = window.EvoUI as unknown as { evalExpr(s: string, env: object): unknown };
    const ev = (src: string, env: object): unknown => eng.evalExpr(src, env);
    for (const e of ["x.constructor", "x.__proto__", "fmt.constructor", "x[d]"]) expect(() => ev(e, { x: [1], d: ["constructor"] }), e).toThrow();
    expect(() => ev("window", {})).toThrow();
    expect(() => ev("Function", {})).toThrow();
  });
});

describe("slider input is coalesced per animation frame", () => {
  it("a burst of input events re-evaluates once, with the last value", async () => {
    const { el } = mount(fence(EXAMPLES[0]), port());
    const slider = el.querySelector<HTMLInputElement>("input[type=range]")!;
    const label = slider.parentElement!.querySelector("span")!;
    const before = label.textContent;
    for (const v of ["0.5", "1.5", "2.4"]) { slider.value = v; slider.dispatchEvent(new Event("input", { bubbles: true })); }
    expect(label.textContent).toBe(before);              // held back until the frame
    await frame();
    expect(label.textContent).toContain("2.4");
  });

  it("a block refused by the self-test shows its fallback instead of drawing", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    trustEngineForTests(false);
    const { el } = mount(fence(EXAMPLES[0]), port());
    expect(el.querySelector(".evoui")).toBeNull();
    expect(el.querySelector(".evowiki-evoui-fallback")?.textContent).toBe(String(EXAMPLES[0]!.fallback));
    warn.mockRestore();
  });
});

describe("math nodes go through Obsidian's MathJax", () => {
  const MATH = { type: "evoui", title: "公式", question: "怎么写？", fallback: "x 的平方", state: {}, derived: {},
    body: { kind: "box", children: [{ kind: "math", tex: "x^2 + y^2 = r^2", display: true }, { kind: "math", tex: "\frac{1}{2}" }] } };

  it("typesets each formula with renderMath once MathJax is loaded, and finishes the styles", async () => {
    const obsidian = await import("obsidian");
    vi.mocked(obsidian.renderMath).mockClear();
    vi.mocked(obsidian.finishRenderMath).mockClear();
    const { el } = mount(fence(MATH), port());
    await flush();
    const ms = [...el.querySelectorAll(".evoui-math mjx-container")];
    expect(ms.map((m) => [m.textContent, m.getAttribute("display")])).toEqual(
      [["[math] x^2 + y^2 = r^2", "true"], ["[math] \frac{1}{2}", "false"]]);
    expect(el.querySelectorAll(".evoui-math code")).toHaveLength(0);
    expect(obsidian.finishRenderMath).toHaveBeenCalled();
  });

  it("keeps the LaTeX source of a formula MathJax cannot read", async () => {
    const bad = { ...MATH, body: { kind: "box", children: [{ kind: "math", tex: "\\frac{1}{", display: true }] } };
    const { el } = mount(fence(bad), port());
    await flush();
    expect(el.querySelectorAll("mjx-container")).toHaveLength(0);
    expect(el.querySelector(".evoui-math code")?.textContent).toBe("\\frac{1}{");
  });

  it("keeps the LaTeX source when MathJax cannot load (and tries again for the next block)", async () => {
    const obsidian = await import("obsidian");
    resetMathForTests();
    vi.mocked(obsidian.loadMathJax).mockRejectedValueOnce(new Error("no mathjax"));
    const a = mount(fence(MATH), port());
    await flush();
    expect([...a.el.querySelectorAll(".evoui-math code")].map((c) => c.textContent)).toEqual(["x^2 + y^2 = r^2", "\frac{1}{2}"]);
    const b = mount(fence(MATH), port());
    await flush();
    expect(b.el.querySelectorAll(".evoui-math mjx-container")).toHaveLength(2);
  });
});

describe("a block with an app node runs by itself only when the server stored its content", () => {
  const app = (fallback = "文字版"): Record<string, unknown> => ({
    type: "evoui", title: "小程序", question: "q", state: {}, derived: {}, fallback,
    body: { kind: "box", children: [{ kind: "app", html: "<p>x</p><script>evowiki.ready()</script>" }] },
  });
  const run = (el: HTMLElement): HTMLButtonElement | null => el.querySelector<HTMLButtonElement>(".evowiki-evoui-run");

  it("generated: draws at once, no button", async () => {
    const asked: string[] = [];
    const src = fence(app());
    const { el } = mount(src, port({}, { isGenerated: (t) => { asked.push(t); return Promise.resolve(true); } }));
    expect(run(el)).toBeNull();                                  // no button while the server is asked
    expect(el.querySelector(".evoui-app-frame")).toBeNull();     // and nothing running
    await flush();
    expect(asked).toEqual([src]);
    expect(run(el)).toBeNull();
    expect(el.querySelector(".evoui")).not.toBeNull();
  });

  it("a server that does not answer: the text and the offer come in under a second, and its late yes runs the block", async () => {
    vi.useFakeTimers();
    try {
      let answer: (v: boolean) => void = () => undefined;
      const { el } = mount(fence(app()), port({}, { isGenerated: () => new Promise<boolean>((r) => { answer = r; }) }));
      await flush();
      expect(run(el)).toBeNull();
      vi.advanceTimersByTime(800);
      expect(run(el)).not.toBeNull();
      answer(true);
      await flush();
      expect(run(el)).toBeNull();
    } finally { vi.useRealTimers(); }
  });

  it("not generated, failing or no way to ask: its text and 运行这个交互; nothing runs until the press, and the press is for this mount", async () => {
    for (const isGenerated of [() => Promise.resolve(false), () => Promise.reject(new Error("offline")), undefined]) {
      const { el } = mount(fence(app()), port({}, isGenerated === undefined ? {} : { isGenerated }));
      await flush();
      expect(el.querySelector(".evowiki-evoui-fallback")?.textContent).toBe("文字版");
      expect(el.querySelector(".evoui")).toBeNull();
      expect(run(el)?.textContent).toBe(WIKI_COPY.evouiRun);
      run(el)!.click();
      expect(el.querySelector(".evoui")).not.toBeNull();
      expect(run(el)).toBeNull();
      const again = mount(fence(app()), port({}, { isGenerated: () => Promise.resolve(false) }));
      await flush();
      expect(run(again.el)).not.toBeNull();                      // the same block in a new mount asks again
    }
  });

  it("a marker in the note proves nothing: the same verdict comes from the server, an app hidden deep in the block is found", async () => {
    const asked: string[] = [];
    const forged = { ...app(), generated: true, source: "evowiki", signature: "ok" };
    const { el } = mount(fence(forged), port({}, { isGenerated: (t) => { asked.push(t); return Promise.resolve(false); } }));
    await flush();
    expect(asked).toHaveLength(1);
    expect(run(el)).not.toBeNull();
  });

  it("an asset file is asked about as it is on disk", async () => {
    const text = JSON.stringify(app());
    const asked: string[] = [];
    const { el } = mount("asset: ia_a", port({ "EvoWiki/Wiki/assets/ia_a.json": text }, { isGenerated: (t) => { asked.push(t); return Promise.resolve(false); } }));
    await flush(); await flush();
    expect(asked).toEqual([text]);
    expect(run(el)).not.toBeNull();
  });

  it("a block without an app never asks", async () => {
    const asked: string[] = [];
    const { el } = mount(fence(EXAMPLES[0]), port({}, { isGenerated: (t) => { asked.push(t); return Promise.resolve(false); } }));
    await flush();
    expect(asked).toEqual([]);
    expect(el.querySelector(".evoui")).not.toBeNull();
  });
});


describe("frame teardown progress", () => {
  it("flushes the pending renderer progress before disposing a block", async () => {
    vi.useFakeTimers();
    try {
      const saves = vi.fn();
      const p = port({ [ASSET]: JSON.stringify(EXAMPLES[0]) }, { onProgress: saves });
      const { el, off } = mount("asset: ia_1", p);
      await flush();
      const slider = el.querySelector<HTMLInputElement>("input[type=range]")!;
      slider.value = "2.5";
      slider.dispatchEvent(new Event("input", { bubbles: true }));
      await vi.advanceTimersByTimeAsync(16);
      off();
      await flush();
      expect(saves).toHaveBeenCalledTimes(1);
    } finally { vi.useRealTimers(); }
  });
});


it("waits for the prior mount's final snapshot before a replacement restores progress", async () => {
  const transports: { receive: (m: import("./protocol").FromFrame) => void; sent: import("./protocol").ToFrame[] }[] = [];
  useTransportForTests((_host, receive) => {
    const transport = { receive, sent: [] as import("./protocol").ToFrame[] };
    transports.push(transport);
    return { start: () => receive({ t: "up", boot: "test-document" }), post: (m) => { transport.sent.push(m); }, remove: () => undefined, connected: () => true, setHeight: () => undefined };
  });
  let saved: BlockProgressData | null = null;
  const progress = vi.fn(() => Promise.resolve(saved));
  const p = port({ [ASSET]: JSON.stringify(EXAMPLES[0]) }, { progress, onProgress: (_id, value) => { saved = value; } });
  const first = mount("asset: ia_1", p);
  await flush();
  expect(transports).toHaveLength(1);
  first.off();
  const second = mount("asset: ia_1", p);
  await flush();
  expect(progress).toHaveBeenCalledTimes(1);
  expect(transports).toHaveLength(1);
  transports[0]!.receive({ t: "stopped", progress: { revision: 9, v: 2, state: { spread: 2.5 }, nodes: {} } });
  await flush(); await flush();
  expect(progress).toHaveBeenCalledTimes(2);
  expect(transports).toHaveLength(2);
  expect(transports[1]!.sent[0]).toMatchObject({ t: "init", progress: { revision: 9 } });
  second.off();
  transports[1]!.receive({ t: "stopped", progress: null });
});


it("restores a graceful-stop snapshot after its offline POST and a plugin controller reload", async () => {
  vi.useFakeTimers();
  let kept: import("./progress-store").QueuedBlockProgress[] = [];
  const store = { load: () => structuredClone(kept), save: (progress: readonly import("./progress-store").QueuedBlockProgress[]) => { kept = structuredClone([...progress]); } };
  try {
    const firstApp = setup({ blockProgressStore: store });
    vi.spyOn(firstApp.api, "saveBlockProgress").mockRejectedValue(new OfflineError());
    const first = mount("asset: ia_1", port({ [ASSET]: JSON.stringify(EXAMPLES[0]) }, {
      onProgress: (asset, progress) => { firstApp.c.saveBlockProgress(asset, progress); },
    }));
    await flush();
    const slider = first.el.querySelector<HTMLInputElement>("input[type=range]")!;
    slider.value = "2.5";
    slider.dispatchEvent(new Event("input", { bubbles: true }));
    first.off(); // Before both the animation-frame coalescing and the 250ms progress debounce.
    await flush(); await flush();
    expect(kept).toHaveLength(1);
    const original = structuredClone(kept[0]!.progress);
    expect(original.revision).toBeGreaterThan(0);
    firstApp.c.dispose();
    const secondApp = setup({ blockProgressStore: store });
    const post = vi.spyOn(secondApp.api, "saveBlockProgress").mockRejectedValue(new OfflineError());
    const second = mount("asset: ia_1", port({ [ASSET]: JSON.stringify(EXAMPLES[0]) }, {
      progress: (asset) => secondApp.c.blockProgress(asset),
      onProgress: (asset, progress) => { secondApp.c.saveBlockProgress(asset, progress); },
    }));
    await flush(); await flush();
    expect(second.el.querySelector<HTMLInputElement>("input[type=range]")!.value).toBe("2.5");
    expect(await secondApp.c.blockProgress("ia_1")).toEqual(original);
    post.mockResolvedValue({ stale: false });
    await secondApp.c.flushBlockProgress(); await flush();
    expect(kept).toEqual([]);
    second.off(); secondApp.c.dispose();
  } finally { vi.useRealTimers(); }
});
