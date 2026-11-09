/**
 * Startup self-test of the EvoUI evaluator (a defense independent of the D1 file): known sandbox-escape expressions are
 * evaluated through the real `window.EvoUI.evalExpr`; any of them returning something other than `undefined` (or throwing)
 * means the renderer would hand out a function or a prototype (`sin[["constructor"]]` reaches Function), so no block is
 * drawn at all (every block shows its fallback) until the renderer is fixed. The verdict is cached per page load.
 */
import { evoEngine } from "./engine";

const ENV = { x: [1, 2], o: { a: 1 }, d: ["constructor"], e: ["__proto__"], s: "abc" };

/** Escape payloads: computed members with array / concatenated keys, on builtins, arrays, objects and strings. */
export const ESCAPE_PAYLOADS: readonly string[] = [
  'sin[["constructor"]]', 'sin["con"+"structor"]', 'sin[["con"+"structor"]]', "sin[d]", "sin.constructor", "fmt[d]", "map[d]", "range[d]",
  'x[["constructor"]]', 'x[["__proto__"]]', 'x["__proto__"]', 'x["con"+"structor"]', "x[d]", "x[e]",
  'o[["constructor"]]', "o[d]", "o[e]", "o.constructor", "o.__proto__", "s[d]", 's["\u0063onstructor"]', 'x["\u005f_proto__"]',
  // a computed key is a number on a real array: no text key (this one reads 1 on a renderer that lost that), no number on an object
  'map(["a"], k => o[k])[0]', 'map([0], k => s[k])[0]',
  // D1 b1a56a5: a function is never a value (a result, a lambda return, a bare builtin): evalExpr must throw or give a non-function.
  "iterate(2, y => y, f => f)[0]", "map([1], y => y => y)", "sin", "map", "x => x",
];

const SENTINEL = "PWNED7";
const RUN = `g("return '${SENTINEL}'")()`;
const wrap = (state: Record<string, unknown>, derived: Record<string, string>): Record<string, unknown> => ({
  type: "evoui", title: "t", question: "q", fallback: "f", state, derived, body: { kind: "text", text: "{h}" },
});
/**
 * Whole blocks, run through the real renderer: indirect shapes where the key or the callee is built in other derived
 * values (a state array key, an arrow or a builtin stored in a derived, a concatenated string, a chained iterate). Each
 * tries to call `Function("return 'PWNED7'")()`; a renderer that lets any of them through prints the sentinel.
 */
export const ESCAPE_BLOCKS: readonly Record<string, unknown>[] = [
  wrap({ k: ["constructor"] }, { f: "x => x", g: "f[k]", h: RUN }),
  wrap({ k: ["constructor"] }, { m: "map", g: "m[k]", h: RUN }),
  wrap({ k: ["constructor"] }, { g: "sin[k]", h: RUN }),
  wrap({ x: 1 }, { k: '["con"+"structor"]', g: "sin[k]", h: RUN }),
  wrap({ a: "con" }, { s: 'a+"structor"', g: "sin[s]", h: RUN }),
  wrap({ k: ["constructor"] }, { g: "iterate(2, y => y, f => f)[0][k]", h: RUN }),
  wrap({ k: ["__proto__"] }, { g: "sin[k]", h: RUN }),
  // the shapes of the list functions: a key or a callee stashed through fold / filter / join
  wrap({ x: 1 }, { k: 'join(["con","structor"])', g: "sin[k]", h: RUN }),
  wrap({ x: 1 }, { k: 'fold(["con","structor"],"",(s,y)=>s+y)', g: "sin[k]", h: RUN }),
  wrap({ k: ["constructor"] }, { f: "filter([1], y => y => y)", g: "f[0][k]", h: RUN }),
  wrap({ k: ["constructor"] }, { f: "fold([1], 0, (s, y) => y => y)", g: "f[k]", h: RUN }),
];
/**
 * NOT part of engineIsSafe(): an unpatched member() would only read Object's constructor into a NaN that the chart drops, so there
 * is no observable sentinel to detect a regression with; the unit test (no series path drawn) guards it instead.
 * Whole blocks that must be refused or fall back: a chart whose x / series key is a prototype name (D1 member()). */
export const ESCAPE_CHARTS: readonly Record<string, unknown>[] = [
  { type: "evoui", title: "t", question: "q", fallback: "f", state: { n: 3 }, derived: {},
    body: { kind: "chart", chart: "line", x: "constructor", series: [{ key: "constructor", label: "s" }], data: "[{a: 1}, {a: 2}]" } },
];

interface Evaluator { evalExpr?: (src: string, env: object) => unknown; render?: (block: unknown, mount: HTMLElement) => unknown }

let cached: boolean | null = null;
let forced: boolean | null = null;

/** Whether the renderer passed every payload. Cached; tests may force a verdict with trustEngineForTests. */
export function engineIsSafe(): boolean {
  if (forced !== null) return forced;
  if (cached !== null) return cached;
  const ev = (evoEngine() as Evaluator | null)?.evalExpr;
  let safe = typeof ev === "function";
  if (safe) {
    for (const p of ESCAPE_PAYLOADS) {
      try {
        if (ev!(p, ENV) !== undefined) { safe = false; break; }
      } catch { /* a throw is a refusal: fine */ }
    }
  }
  const render = (evoEngine() as Evaluator | null)?.render;
  if (safe && typeof render !== "function") safe = false;
  if (safe) {
    // A refused escape block falls back, and the renderer logs that refusal with console.error (ESCAPE_BLOCKS[5]: "a function
    // can only be the fn argument of map / iterate"). That is the expected outcome here, not an error of the page: keep it
    // out of the console, where it read as an error of the first block the page shows.
    const logError = console.error;
    console.error = () => undefined;
    try {
      for (const b of ESCAPE_BLOCKS) {
        const mount = document.createElement("div");
        try { render!.call(evoEngine(), b, mount); } catch { /* refused: fine */ }
        if ((mount.textContent ?? "").includes(SENTINEL)) { safe = false; break; }
      }
    } finally { console.error = logError; }
  }
  if (!safe) console.warn("EvoWiki: the EvoUI renderer failed the safety self-test; interactive blocks are not drawn");
  cached = safe;
  return safe;
}

/** Test hooks: force the verdict (true/false), or null to run the real self-test again. */
export function trustEngineForTests(v: boolean | null): void { forced = v; cached = null; }
