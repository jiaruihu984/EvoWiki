import { beforeEach, describe, expect, it } from "vitest";
import { fallbackOf, MAX_BLOCK_CHARS, parseEvouiSource } from "../block";
import { adapterProblem, expressionProblem, validateBlock } from "./validate";
import { trustEngineForTests } from "./engine-safety";
import { EXAMPLES, fence, SONNET } from "../evoui-fixtures";

// The checked-in renderer fails the self-test until D1 fixes it at the root: these tests are about the validation itself.
beforeEach(() => { trustEngineForTests(true); });

const base = (over: Record<string, unknown> = {}): Record<string, unknown> => ({
  type: "evoui", title: "t", question: "q", state: { a: 1 }, derived: {}, fallback: "看文字",
  body: { kind: "text", text: "a = {a}" }, ...over,
});

describe("parseEvouiSource", () => {
  it("inline JSON, as deep/evoui block_fence writes it", () => {
    const p = parseEvouiSource(fence(EXAMPLES[0]));
    expect(p.kind).toBe("block");
    expect(p.kind === "block" && (p.block as { title: string }).title).toBe("分散程度和方差");
  });
  it("an `asset: <id>` reference (one line, a valid asset id)", () => {
    expect(parseEvouiSource("asset: ia_12\n")).toEqual({ kind: "asset", id: "ia_12" });
    expect(parseEvouiSource("\n  asset:ia-1  \n")).toEqual({ kind: "asset", id: "ia-1" });
  });
  it("invalid for broken JSON, an unsafe id, a second line, empty text or an oversized fence", () => {
    for (const s of ["{not json", "asset: ../x", "asset: a b", "asset: con", "asset: a\nasset: b", "", "  \n", "hello", "x".repeat(130_000)]) {
      expect(parseEvouiSource(s), s.slice(0, 20)).toEqual({ kind: "invalid" });
    }
  });
});

describe("validateBlock", () => {
  it("accepts D1's examples and the Sonnet-written blocks (the spec's own validator)", () => {
    for (const b of [...EXAMPLES, ...SONNET]) {
      const v = validateBlock(b);
      expect(v.ok, JSON.stringify(v)).toBe(true);
    }
  });

  it("rejects what the spec rejects: unknown node, over the limits, bad expression, missing field, non-object", () => {
    expect(validateBlock(base({ body: { kind: "script", text: "x" } })).ok).toBe(false);
    expect(validateBlock(base({ body: { kind: "text", text: "{a +}" } })).ok).toBe(false);
    expect(validateBlock(base({ body: { kind: "text", text: "{nope}" } })).ok).toBe(false);
    expect(validateBlock(base({ body: { kind: "text", text: "x".repeat(MAX_BLOCK_CHARS) } })).ok).toBe(false);
    expect(validateBlock(base({ body: { kind: "text", text: "x".repeat(700) } })).ok).toBe(false);
    expect(validateBlock(base({ derived: { d: "a".repeat(600) } })).ok).toBe(false);
    expect(validateBlock(base({ extra: 1 })).ok).toBe(false);
    const noFallback = base();
    delete noFallback.fallback;
    expect(validateBlock(noFallback).ok).toBe(false);
    for (const x of [null, 3, "x", [], undefined]) expect(validateBlock(x).ok).toBe(false);
    const deep: Record<string, unknown> = { kind: "text", text: "x" };
    let node: Record<string, unknown> = deep;
    for (let i = 0; i < 18; i++) node = { kind: "box", children: [node] };
    expect(validateBlock(base({ body: node })).ok).toBe(false);
    const many = { kind: "box", children: Array.from({ length: 410 }, () => ({ kind: "text", text: "x" })) };
    expect(validateBlock(base({ body: many })).ok).toBe(false);
  });

  it("rejects malicious expressions: prototype chain, globals, eval, Function", () => {
    const evil = ['a[["constructor"]]', 'sin[["con"+"structor"]]', 'sin["con"+"structor"]', 'x[["__proto__"]]', 'a[["\u0063onstructor"]]',
      "a['constructor']", "sin[d]", "a[f(1)]", 'a["x"]', "sin.x", "a.", "a.1",
      "a['con' + 'structor']", "a[b](1)", "a[[0]]", "a.constructor", "a['constructor']", "a.__proto__", "a[\"__proto__\"]", "a.prototype", "constructor.constructor('return 1')()",
      "window.location", "document.cookie", "globalThis", "eval('1')", "Function('return 1')()", "self.fetch"];
    for (const e of evil) {
      expect(validateBlock(base({ derived: { d: e } })).ok, `derived ${e}`).toBe(false);
      expect(validateBlock(base({ body: { kind: "text", text: `{${e}}` } })).ok, `template ${e}`).toBe(false);
      expect(validateBlock(base({ body: { kind: "if", cond: e, then: { kind: "text", text: "x" }, else: null } })).ok, `cond ${e}`).toBe(false);
    }
    expect(validateBlock(base({ state: { __proto__x: 1 } })).ok).toBe(false);
    expect(validateBlock(JSON.parse('{"type":"evoui","title":"t","question":"q","state":{"__proto__":{"a":1}},"derived":{},"fallback":"f","body":{"kind":"text","text":"x"}}')).ok).toBe(false);
  });

  it("the token check alone rejects every escape form, by token value (escapes, concatenation, array keys, unicode)", () => {
    const evil = ['sin[["constructor"]]', 'sin[["con"+"structor"]]', 'sin["con"+"structor"]', 'x[["__proto__"]]', "x[['__proto__']]",
      'x[["\u0063onstructor"]]', 'x[["constru\ctor"]]', 'x["\_\_proto\_\_"]', "sin[d]", "sin.cos",
      'a["x"]', "a[b](1)", "a[[0]]", "a[map]", "a.", "x.constructor", 'x["constructor"]',
      "x.constructor", "constructor", "Function", "window", "a.__proto__"];
    for (const e of evil) expect(expressionProblem(e), e).not.toBeNull();
  });

  it("the token check lets ordinary indexing and members through", () => {
    for (const e of ["xs[0]", "xs[i]", "pts[1][0]", "xs[i + 1]", "bt[max(step - 1, 0)]", "d[k].t + d[k].i", "s.a[s.j + 1]", "a[b][c]", "(a)[b]", "[1, 2][0]", "d.length", "row.name", "map(range(5), i => xs[i])",
      "p.x * 2", "fmt(a, 2)", "a ? [1, 2] : [3, 4]", "{\"k\": [1]}"]) {
      expect(expressionProblem(e), e).toBeNull();
    }
  });

  it("only expression fields are scanned: plain text may say constructor or window", () => {
    const v = validateBlock(base({ title: "constructor and window", question: "what is a prototype?", fallback: "eval, Function, document",
      body: { kind: "box", children: [{ kind: "text", text: "a window onto the {a} constructor" }] } }));
    expect(v.ok, JSON.stringify(v)).toBe(true);
  });

  it("an expression costs what its loops multiply to; a block's total is capped", () => {
    expect(expressionProblem("map(range(2000), i => sum(map(range(2000), j => 1)))")).not.toBeNull();   // 4M
    expect(expressionProblem("sum(map(range(2000), i => i))")).toBeNull();
    expect(expressionProblem("sum(map(xs, a => sum(map(xs, b => 1))))")).not.toBeNull();                // unknown arrays: 2000 x 2000
    const heavy = Object.fromEntries(Array.from({ length: 12 }, (_, i) => [`d${i}`, "sum(map(range(1900), i => i))"]));
    const many = Object.fromEntries(Array.from({ length: 24 }, (_, i) => [`e${i}`, "sum(map(range(1900), i => i)) * sum(map(range(50), i => i))"]));
    expect(validateBlock(base({ derived: heavy })).ok).toBe(true);
    expect(validateBlock(base({ derived: many })).ok).toBe(false);                                       // 24 x 100k > 2M
  });

  it("rejects the indirect bypasses (key or callee built in other derived values) by the adapter's own taint rules, and by D1's validator", () => {
    const run = 'g("return typeof process")()';
    const mk = (state: Record<string, unknown>, derived: Record<string, string>): Record<string, unknown> =>
      base({ state, derived, body: { kind: "text", text: "{h}" } });
    const bypasses = [
      mk({ k: ["constructor"] }, { f: "x => x", g: "f[k]", h: run }),                       // arrow stored, indexed by an array key
      mk({ k: ["constructor"] }, { m: "map", g: "m[k]", h: run }),                           // builtin stored as a value
      mk({ k: ["con" + "structor"] }, { g: "sin[k]", h: run }),                              // state array key on a builtin
      mk({ a: "con", b: "structor" }, { s: "a + b", g: "xs[s]", xs: "[1]", h: run }),        // key built from text states
      mk({ k: [0] }, { t: '["con" + "structor"]', g: "xs[t]", xs: "[1]", h: "g(1)" }),        // key built in a derived
      mk({ n: 1 }, { g: "iterate(2, y => y, f => f)[0][n]", h: "g(1)" }),                    // function out of iterate, indexed
      mk({ k: [1] }, { w: "[sin]", g: "w[0]", h: 'g(1)' }),                                  // function inside an array
      mk({ s: "constructor" }, { v: "1", h: "v" }),                                          // forbidden text in a state value
      mk({ o: { a: ["__proto__"] } }, { v: "1", h: "v" }),                                   // ... however deeply nested
      mk({ a: "con", b: "structor" }, { xs: "[1]", h: '(xs[a + b][a + b])("return 1")()' }),  // key built inside the index, called through parentheses
      mk({ a: "con", b: "structor" }, { s: "a + b", xs: "[1]", h: "xs[(s)]" }),              // text key behind parentheses
    ];
    for (const [i, b] of bypasses.entries()) {
      const mine = adapterProblem(b);
      expect(mine, `adapter ${i}`).not.toBeNull();
      expect(validateBlock(b).ok, `validate ${i}`).toBe(false);
    }
  });

  it("taint does not reject ordinary blocks: text labels, numeric states, data arrays indexed by numbers", () => {
    const ok = base({ state: { n: 3, mode: "a" }, derived: { xs: "map(range(5), i => i * n)", label: 'mode == "a" ? "甲" : "乙"', first: "xs[0]" },
      body: { kind: "text", text: "{label} {first} {len(xs)}" } });
    expect(adapterProblem(ok)).toBeNull();
    const used = base({ state: { n: 3 }, derived: { xs: "map(range(5), i => i)", s: '"abc"', y: "xs[n]" }, body: { kind: "text", text: "{s}{y}" } });
    expect(adapterProblem(used)).toBeNull();
    const arithmetic = base({ state: { step: 1, j: 0, a: [1, 2] }, derived: { y: "a[max(step - 1, 0)]", z: "a[(j + 1)]" }, body: { kind: "text", text: "{y}{z}" } });
    expect(adapterProblem(arithmetic)).toBeNull();
  });

  describe("an array of numbers read by index", () => {
    const DX = "map(range(3), i => [1, 2, 3][i])";
    const mk = (state: Record<string, unknown>, derived: Record<string, string>, body: unknown = { kind: "text", text: "x" }): Record<string, unknown> =>
      base({ state, derived, body });

    it("is allowed, computes, and a comparison with text does not make the value text", () => {
      const blocks = [
        mk({ ord: "up" }, { ys: "ord == 'up' ? [30, 40] : [40, 30]", dy: "map(ys, v => v - 35)", pr: "map(range(2), i => dy[i] * dy[i])" }),
        mk({ m: 2 }, { ys: `map(range(3), i => [1, 2, 3][i] * m)`, total: "sum(map(ys, v => v * 10))" }),
        mk({ c: 1 }, { t: "map([0, 1], i => [3, 4][i] * c)", v: "sum(map(t, a => a * a))" }),
        mk({}, { pr: DX, d: "map(range(3), i => {x: i, y: pr[i]})" }, { kind: "chart", chart: "line", x: "x", series: [{ key: "y", label: "y" }], data: "d" }),
      ];
      for (const b of blocks) expect(adapterProblem(b), JSON.stringify(b.derived)).toBeNull();
    });

    it("text never travels through it: array + array, fmt, text options, object fields, ternaries, shadowing, any field", () => {
      const KEY = "'con' + 'structor'";
      const refused: [string, Record<string, unknown>][] = [
        ["array + array as a key", mk({}, { z: "[1]", dx: DX, dy: DX, h: "1" }, { kind: "text", text: "{z[dx + dy]}" })],
        ["the same in a derived", mk({}, { z: "[1]", dx: DX, s: "dx + dx", h: "z[s]" })],
        ["iterate makes text with no string", mk({}, { z: "[1]", dx: DX, it: "iterate(2, dx, s => s + s)", h: "z[it[1]]" })],
        ["fmt in an index", mk({}, { dx: DX, h: "dx[fmt(0, 0)]" })],
        ["a text option writes the index", mk({ k: 0 }, { dx: DX, y: "map(range(2), i => dx[k])" }, { kind: "choice", bind: "k", options: [{ label: "a", value: "length" }] })],
        ["a button writes text", mk({ k: 0 }, { dx: DX, y: "map(range(2), i => dx[k])" }, { kind: "button", label: "b", set: { k: KEY } })],
        ["a probe writes text", { ...mk({ k: 0 }, { dx: DX, y: "map(range(2), i => dx[k])" }), probes: [{ set: { k: "length" } }] }],
        ["text in map's result", mk({}, { z: "[1]", l: "map(range(2), i => ['con', 'structor'][i])", h: "map(l, k => z[k])" })],
        ["text on one branch", mk({ c: 1 }, { z: "[1]", w: `c ? [1] : [${KEY}]`, h: "map(w, k => z[k])" })],
        ["param shadows the name", mk({}, { z: "[1]", dx: DX, h: `map([${KEY}], dx => z[dx])` })],
        ["celebrate.when", mk({}, { z: "[1]", dx: DX }, { kind: "celebrate", when: "z[dx + dx]" })],
        ["button.set", mk({ k: 0 }, { z: "[1]", dx: DX }, { kind: "button", label: "b", set: { k: "z[dx + dx]" } })],
      ];
      for (const [name, b] of refused) expect(adapterProblem(b), name).not.toBeNull();
    });
  });

  it("rejects expressions that could run away: nested loops, deep nesting, text inside iterate", () => {
    const triple = "sum(map(range(2000), i => sum(map(range(2000), j => sum(map(range(2000), k => 1))))))";
    expect(validateBlock(base({ derived: { d: triple } })).ok).toBe(false);
    expect(expressionProblem("iterate(30, 'ab', s => s + s)")).not.toBeNull();
    expect(expressionProblem("(".repeat(30) + "1" + ")".repeat(30))).not.toBeNull();
    expect(expressionProblem("map(range(11), i => i * 2)")).toBeNull();
    expect(expressionProblem("iterate(13, 5, x => x - 2 * lr * x)")).toBeNull();
  });

  it("never throws, whatever it is given", () => {
    expect(() => validateBlock({ type: "evoui", body: { kind: "box", children: "x" } })).not.toThrow();
    expect(() => validateBlock(JSON.parse("[".repeat(5000) + "]".repeat(5000)))).not.toThrow();
  });
});

describe("drawings and each under the guard", () => {
  const svg = (...children: unknown[]): unknown => ({ kind: "svg", viewBox: "0 0 10 10", children });
  const shape = (at: Record<string, unknown>): unknown => ({ el: "rect", width: 1, height: 1, ...at });
  const each = (list: string, ...children: unknown[]): unknown => ({ kind: "each", in: list, as: "d", index: "i", children });
  const refused = (b: Record<string, unknown>): boolean => !validateBlock(base(b)).ok;

  it("an expression proven whole reads a table of text by a number", () => {
    const steps = [{ cap: "第一步", at: 1 }, { cap: "第二步", at: 2 }];
    expect(validateBlock(base({ state: { k: 0, steps }, body: { kind: "text", text: "{steps[k].cap} {steps[k].at + 1}" } })).ok).toBe(true);
    expect(validateBlock(base({ state: { k: 0, nodes: [{ x: 1, n: "A" }], es: [[0, 0]] }, body: svg(each("es", shape({ x: "{nodes[d[0]].x}" })), each("nodes", { el: "text", text: "{d.n}", x: "{d.x}" })) })).ok).toBe(true);
    expect(validateBlock(base({ state: { s: 0, ws: [{ p: 1, n: "C" }], ns: [5, 6] }, body: svg(each("ws", shape({ x: "{ns[s]}", click: { s: "d.p" } }))) })).ok).toBe(true);
  });

  it.each([
    ["a nested index that holds text", { state: { a: [1], b: ["constructor"], c: 0 }, body: { kind: "text", text: "{a[b[c]]}" } }],
    ["a text key in a drawing", { state: { o: { a: 1 }, k: "constructor" }, body: svg(shape({ x: "{o[k]}" })) }],
    ["a text field of a record as a key", { state: { o: { a: 1 }, k: { a: "constructor" } }, body: svg(shape({ x: "{o[k.a]}" })) }],
    ["an item of a text list as a key", { state: { o: [1], k: ["constructor"] }, body: svg(each("k", shape({ x: "{o[d]}" }))) }],
    ["an item of a text list as an html key", { state: { o: [1], k: ["constructor"] }, body: each("k", { kind: "text", text: "{o[d]}" }) }],
    ["a key folded together", { state: { o: [1] }, derived: { k: 'fold(["con","structor"],"",(s,x)=>s+x)' }, body: { kind: "text", text: "{o[k]}" } }],
    ["a key joined", { state: { o: [1] }, derived: { k: 'join(["con","structor"])' }, body: { kind: "text", text: "{o[k]}" } }],
    ["a builtin behind a derived", { derived: { g: "sin", h: "g[0]" }, body: { kind: "text", text: "{h}" } }],
    ["an arrow behind a derived", { derived: { g: "x => x", h: "g[0]" }, body: { kind: "text", text: "{h}" } }],
    ["a member of a builtin in an attribute", { body: svg(shape({ x: "{sin.constructor}" })) }],
    ["a forbidden name in a click", { body: svg(shape({ click: { a: "constructor" } })) }],
    ["a forbidden name in a drag", { body: svg(shape({ drag: { a: "__proto__" } })) }],
    ["a forbidden key in a click", { body: svg(shape({ click: { constructor: 1 } })) }],
    ["a forbidden name in each", { body: each("window", { kind: "text", text: "x" }) }],
    ["a call of a value in an attribute", { state: { f: "x" }, body: svg(shape({ x: "{f(1)}" })) }],
    ["an array literal as an index", { state: { o: [1] }, body: svg(shape({ x: '{o[["constructor"]]}' })) }],
    ["text inside fold's work", { derived: { k: 'fold(range(50),"a",(s,x)=>s+s)' }, body: { kind: "text", text: "{k}" } }],
    ["a script element", { body: svg({ el: "script", text: "x" }) }],
    ["a foreign element", { body: svg({ el: "foreignObject", children: [] }) }],
    ["an event attribute", { body: svg(shape({ onload: "x" })) }],
    ["a link", { body: svg(shape({ href: "http://x" })) }],
    ["a fill that loads something", { body: svg(shape({ fill: "url(http://x)" })) }],
    ["a style attribute", { body: svg(shape({ style: "background:url(http://x)" })) }],
  ])("refuses %s", (_name, block) => { expect(refused(block as Record<string, unknown>)).toBe(true); });
});

describe("fallbackOf", () => {
  it("the block's own fallback text (clipped), else null", () => {
    expect(fallbackOf(base())).toBe("看文字");
    expect(fallbackOf(base({ fallback: "x".repeat(2000) }))?.length).toBe(1500);
    for (const x of [base({ fallback: "  " }), base({ fallback: 3 }), null, "x", {}]) expect(fallbackOf(x)).toBeNull();
  });
});

describe("the guard reads every field the renderer evaluates", () => {
  const evil = "k.constructor";
  const withNode = (node: unknown): Record<string, unknown> => base({ body: { kind: "box", children: [node] } });
  it.each<[string, unknown]>([
    ["celebrate.when", { kind: "celebrate", when: evil }],
    ["transition.key", { kind: "transition", key: evil, children: [] }],
    ["button.set", { kind: "button", label: "x", set: { a: evil } }],
    ["table rows expression", { kind: "table", columns: ["a"], rows: evil }],
    ["table cell", { kind: "table", columns: ["a"], rows: [[`{${evil}}`]] }],
    ["chart x_label", { kind: "chart", chart: "line", data: "[]", x: "a", series: [], x_label: `{${evil}}` }],
    ["chart y_label", { kind: "chart", chart: "line", data: "[]", x: "a", series: [], y_label: `{${evil}}` }],
    ["route prompt", { kind: "route", prompt: `{${evil}}`, options: [] }],
    ["rect w", { kind: "plot", items: [{ shape: "rect", x: 0, y: 0, w: evil, h: 1 }] }],
    ["rect h", { kind: "plot", items: [{ shape: "rect", x: 0, y: 0, w: 1, h: evil }] }],
    ["circle cx", { kind: "plot", items: [{ shape: "circle", cx: evil, cy: 0, r: 1 }] }],
    ["circle cy", { kind: "plot", items: [{ shape: "circle", cx: 0, cy: evil, r: 1 }] }],
    ["circle r", { kind: "plot", items: [{ shape: "circle", cx: 0, cy: 0, r: evil }] }],
    ["node id", { kind: "quiz", id: "__proto__", questions: [] }],
  ])("%s", (_, node) => { expect(adapterProblem(withNode(node))).not.toBeNull(); });

  it("plain text in a field that is only an expression elsewhere stays allowed", () => {
    expect(adapterProblem(withNode({ kind: "timeline", events: [{ title: "t", when: "constructor 时期" }, { title: "u" }] }))).toBeNull();
    expect(adapterProblem(withNode({ kind: "chart", chart: "line", data: "[]", x: "a", series: [{ key: "a", label: "l" }] }))).toBeNull();
  });
});
