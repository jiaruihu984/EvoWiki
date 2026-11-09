import { afterEach, describe, expect, it, vi } from "vitest";
import { validateBlock } from "./validate";
import { ESCAPE_BLOCKS, ESCAPE_CHARTS, ESCAPE_PAYLOADS, engineIsSafe, trustEngineForTests } from "./engine-safety";
import { EXAMPLES } from "../evoui-fixtures";

type Eng = { evalExpr: (s: string, e: object) => unknown; render: (b: unknown, m: HTMLElement) => unknown };
const eng = (): Eng => window.EvoUI as unknown as Eng;
const ENV = { x: [1, 2], o: { a: 1 }, d: ["constructor"], e: ["__proto__"], s: "abc" };

afterEach(() => { trustEngineForTests(null); });

describe("the renderer self-test", () => {
  it("passes the checked-in (patched) renderer: no payload or escape block gets through, and blocks render", () => {
    trustEngineForTests(null);
    expect(engineIsSafe()).toBe(true);
    for (const p of ESCAPE_PAYLOADS) { let r: unknown; try { r = eng().evalExpr(p, ENV); } catch { r = undefined; } expect(r, p).toBeUndefined(); }
    expect(validateBlock(EXAMPLES[0]).ok).toBe(true);
  });

  it("the self-test logs nothing: its own refused escape blocks are not console errors of the page (sweep S8)", () => {
    const err = vi.spyOn(console, "error");
    try {
      trustEngineForTests(null);
      expect(engineIsSafe()).toBe(true);
      expect(err).not.toHaveBeenCalled();
    } finally { err.mockRestore(); }
    // console.error is back: the same refusal in a real block still reaches the console
    const err2 = vi.spyOn(console, "error").mockImplementation(() => undefined);
    try {
      eng().render(ESCAPE_BLOCKS[5], document.createElement("div"));
      expect(String(err2.mock.calls[0]?.[0])).toContain("a function can only be the fn argument of map / iterate");
    } finally { err2.mockRestore(); }
  });

  it("the stored 连通分量 stepper (trimmed) and a 真题练习 quiz render every state without a console error", () => {
    const stepper = {
      type: "evoui", title: "连通分量怎么找：逐步标记", question: "q", fallback: "未标记节点一开始是 {1,2,3,4,5}。",
      state: { step: 0 },
      derived: { comp: "step == 0 ? '（还没选种子）' : step == 1 ? '{1}' : '{1, 2}'", m1: "step >= 1", done: "step >= 2" },
      body: { kind: "box", children: [
        { kind: "player", id: "run", bind: "step", min: 0, max: 2, step: 1, interval_ms: 900, label: "第 {step} 步" },
        { kind: "plot", xmin: 0, xmax: 6, ymin: 0, ymax: 5, grid: false, items: [
          { shape: "segment", x1: 1, y1: 4, x2: 3, y2: 4, tone: "secondary" },
          { shape: "circle", cx: 1, cy: 4, r: 0.28, fill: true, label: "1", tone: "secondary" },
          { shape: "text", x: 1, y: 4.55, text: "{m1 ? '✓' : ''}", anchor: "middle", size: "sm", tone: "accent" }] },
        { kind: "title", size: "lg", text: "{comp}" },
        { kind: "if", cond: "done", then: { kind: "text", text: "结束" }, else: { kind: "text", text: "{comp}" } }] },
    };
    const exam = {
      type: "evoui", title: "特征值与特征向量", question: "q", fallback: "1. 题干", state: {}, derived: {},
      body: { kind: "quiz", id: "exam", layout: "paged", title: "真题练习", questions: [
        { q: "题干", options: ["A. x", "B. y"], correct: 0, why: "因为 x" }] },
    };
    type H = { set: (k: string, v: unknown) => void };
    const err = vi.spyOn(console, "error").mockImplementation(() => undefined);
    try {
      for (const b of [stepper, exam]) expect(validateBlock(b).ok, b.title).toBe(true);
      const mount = document.createElement("div");
      const h = (eng().render(stepper, mount) as H);
      for (const s of [1, 2, 0]) h.set("step", s);
      expect(mount.querySelector(".evoui-error")).toBeNull();
      const m2 = document.createElement("div");
      eng().render(exam, m2);
      expect(m2.querySelector(".evoui-error")).toBeNull();
      expect(err).not.toHaveBeenCalled();
    } finally { err.mockRestore(); }
  });

  it("every escape block is refused by the patched runtime on its own (no sentinel in the output)", () => {
    for (const b of ESCAPE_BLOCKS) {
      const mount = document.createElement("div");
      try { eng().render(b, mount); } catch { /* refused */ }
      expect(mount.textContent).not.toContain("PWNED7");
    }
  });

  it.each(["iterate(2, y => y, f => f)[0]", "map([1], y => y => y)", "sin", "map", "x => x"])("function-valued expression %s is listed and never yields a function", (p) => {
    expect(ESCAPE_PAYLOADS).toContain(p);
    let r: unknown; try { r = eng().evalExpr(p, ENV); } catch { r = undefined; }
    expect(typeof r).not.toBe("function");
    expect(r).toBeUndefined();
  });

  it("a chart whose x / series key is a prototype name draws nothing from the prototype (D1 member())", () => {
    for (const b of ESCAPE_CHARTS) {
      const mount = document.createElement("div");
      try { eng().render(b, mount); } catch { /* refused: the block then shows its fallback */ }
      expect(mount.textContent).not.toMatch(/function|native code/i);
      expect(mount.querySelector("polyline, rect.fill")).toBeNull();
    }
  });

  it("passes stubs that refuse everything (undefined or a throw)", () => {
    const real = eng(), ev = real.evalExpr;
    try {
      real.evalExpr = () => undefined;
      trustEngineForTests(null);
      expect(engineIsSafe()).toBe(true);
      real.evalExpr = () => { throw new Error("no"); };
      trustEngineForTests(null);
      expect(engineIsSafe()).toBe(true);
    } finally { real.evalExpr = ev; }
  });

  it("catches a deliberately vulnerable evaluator (any payload returning a function or an object)", () => {
    const real = eng(), ev = real.evalExpr;
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    try {
      for (const bad of [() => Function, () => ({}), (s: string) => (s === 'sin[["constructor"]]' ? Function : undefined),
        (s: string) => (s === 'x["\u005f_proto__"]' ? [] : undefined)]) {
        real.evalExpr = bad;
        trustEngineForTests(null);
        expect(engineIsSafe()).toBe(false);
        expect(validateBlock(EXAMPLES[0]).ok).toBe(false);
      }
      real.evalExpr = undefined as unknown as Eng["evalExpr"];
      trustEngineForTests(null);
      expect(engineIsSafe()).toBe(false);
    } finally { real.evalExpr = ev; warn.mockRestore(); }
  });

  it("catches a vulnerable renderer through the indirect whole-block shapes even when evalExpr is clean", () => {
    const real = eng(), render = real.render;
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    try {
      for (let i = 0; i < ESCAPE_BLOCKS.length; i++) {
        // a renderer that "runs" exactly block i's final call and prints the result
        real.render = (b, m) => { if (b === ESCAPE_BLOCKS[i]) m.textContent = "PWNED7"; return {}; };
        trustEngineForTests(null);
        expect(engineIsSafe(), `block ${i}`).toBe(false);
      }
      real.render = undefined as unknown as Eng["render"];
      trustEngineForTests(null);
      expect(engineIsSafe()).toBe(false);
    } finally { real.render = render; warn.mockRestore(); }
  });

  it("covers the reported escape and its variants", () => {
    for (const p of ['sin[["constructor"]]', 'sin["con"+"structor"]', 'x[["__proto__"]]', 's["\u0063onstructor"]']) expect(ESCAPE_PAYLOADS).toContain(p);
  });
});
