/* EvoUI v1 renderer: JSON block -> DOM. No eval, no HTML from the model: a small expression parser
   over a whitelisted environment, and a fixed set of node kinds. Throws EvoUIError on anything else. */
(function (global) {
  class EvoUIError extends Error {}
  const fail = (m) => { throw new EvoUIError(m); };

  /* ---------- expressions ---------- */
  function lex(s) {
    const out = []; let i = 0; const ops = ["===", "!==", "**", "==", "!=", "<=", ">=", "&&", "||", "=>"];
    outer: while (i < s.length) {
      const c = s[i];
      if (/\s/.test(c)) { i++; continue; }
      if (/[0-9]/.test(c) || (c === "." && /[0-9]/.test(s[i + 1] ?? ""))) { let j = i; while (j < s.length && /[0-9.]/.test(s[j])) j++; if (/[eE]/.test(s[j] ?? "")) { j++; if (/[+-]/.test(s[j])) j++; while (/[0-9]/.test(s[j] ?? "")) j++; } out.push({ t: "num", v: parseFloat(s.slice(i, j)) }); i = j; continue; }
      if (c === '"' || c === "'") { let j = i + 1, v = ""; while (j < s.length && s[j] !== c) { if (s[j] === "\\") j++; v += s[j]; j++; } if (j >= s.length) fail("unterminated string"); out.push({ t: "str", v }); i = j + 1; continue; }
      if (/[A-Za-z_一-鿿]/.test(c)) { let j = i; while (j < s.length && /[\w一-鿿]/.test(s[j])) j++; out.push({ t: "id", v: s.slice(i, j) }); i = j; continue; }
      for (const op of ops) if (s.startsWith(op, i)) { out.push({ t: "op", v: op === "===" ? "==" : op === "!==" ? "!=" : op }); i += op.length; continue outer; }
      if ("+-*/%<>!?:.,()[]{}".includes(c)) { out.push({ t: "op", v: c }); i++; continue; }
      fail("unexpected character " + JSON.stringify(c));
    }
    out.push({ t: "eof" }); return out;
  }
  function parse(src) {
    const tk = lex(String(src)); let p = 0;
    const peek = () => tk[p], eat = (v) => { const t = tk[p]; if (v && !(t.t === "op" && t.v === v)) fail(`expected ${v} in ${src}`); p++; return t; };
    const isOp = (v) => tk[p].t === "op" && tk[p].v === v;
    function expr() { // arrow or ternary
      if (tk[p].t === "id" && tk[p + 1].t === "op" && tk[p + 1].v === "=>") { const name = tk[p].v; p += 2; return { k: "fn", param: name, body: expr() }; }
      if (isOp("(") && tk[p + 1].t === "id" && tk[p + 2].t === "op" && tk[p + 2].v === "," && tk[p + 3].t === "id" && tk[p + 4].t === "op" && tk[p + 4].v === ")" && tk[p + 5].t === "op" && tk[p + 5].v === "=>") {
        const name = tk[p + 1].v, second = tk[p + 3].v; p += 6; return { k: "fn", param: name, param2: second, body: expr() };
      }
      const c = binary(0); if (isOp("?")) { eat("?"); const a = expr(); eat(":"); const b = expr(); return { k: "cond", c, a, b }; } return c;
    }
    const PREC = [["||"], ["&&"], ["==", "!="], ["<", "<=", ">", ">="], ["+", "-"], ["*", "/", "%"], ["**"]];
    function binary(lv) {
      if (lv >= PREC.length) return unary();
      let l = binary(lv + 1);
      while (tk[p].t === "op" && PREC[lv].includes(tk[p].v)) { const op = eat().v; const r = lv === PREC.length - 1 ? binary(lv) : binary(lv + 1); l = { k: "bin", op, l, r }; }
      return l;
    }
    function unary() { if (isOp("-")) { eat(); return { k: "neg", e: unary() }; } if (isOp("!")) { eat(); return { k: "not", e: unary() }; } if (isOp("+")) { eat(); return unary(); } return postfix(primary()); }
    function postfix(e) {
      for (;;) {
        if (isOp(".")) { eat(); const t = eat(); if (t.t !== "id") fail("bad member"); e = { k: "mem", o: e, name: { k: "lit", v: t.v } }; }
        else if (isOp("[")) { eat("["); const i = expr(); eat("]"); e = { k: "mem", o: e, name: i }; }
        else if (isOp("(")) { eat("("); const args = []; if (!isOp(")")) { args.push(expr()); while (isOp(",")) { eat(","); args.push(expr()); } } eat(")"); e = { k: "call", f: e, args }; }
        else return e;
      }
    }
    function primary() {
      const t = peek();
      if (t.t === "num" || t.t === "str") { p++; return { k: "lit", v: t.v }; }
      if (t.t === "id") { p++; if (t.v === "true") return { k: "lit", v: true }; if (t.v === "false") return { k: "lit", v: false }; if (t.v === "null") return { k: "lit", v: null }; return { k: "id", v: t.v }; }
      if (isOp("(")) { eat("("); const e = expr(); eat(")"); return e; }
      if (isOp("[")) { eat("["); const items = []; if (!isOp("]")) { items.push(expr()); while (isOp(",")) { eat(","); if (isOp("]")) break; items.push(expr()); } } eat("]"); return { k: "arr", items }; }
      if (isOp("{")) { eat("{"); const props = []; if (!isOp("}")) { do { if (isOp("}")) break; const kt = eat(); if (kt.t !== "str" && kt.t !== "id") fail("bad key"); eat(":"); props.push([kt.v, expr()]); } while (isOp(",") && eat(",")); } eat("}"); return { k: "obj", props }; }
      fail(`unexpected ${t.v ?? t.t} in ${src}`);
    }
    const ast = expr(); if (tk[p].t !== "eof") fail(`trailing input in ${src}`); return ast;
  }
  /* What native code does with a value (array-to-string in sum / abs / +, a template) costs by its size, and the step budget
     does not see native code. So every array and object knows how many leaves it reaches (shared parts counted each time):
     over MAX_WEIGHT it is refused as it is built, and coercing one is charged to the budget. */
  const WEIGHT = new WeakMap(), MAX_WEIGHT = 1000000, MAX_TEXT_VALUE = 100000;
  const weightOf = (v) => (v === null || typeof v !== "object" ? 1 : WEIGHT.get(v) ?? (Array.isArray(v) ? v.length + 1 : Object.keys(v).length + 1));
  const weigh = (v) => {
    if (v === null || typeof v !== "object") return v;
    let w = 1;
    for (const x of Array.isArray(v) ? v : Object.values(v)) w += weightOf(x);
    if (w > MAX_WEIGHT) fail("value too large");
    WEIGHT.set(v, w);
    return v;
  };
  const fmt = (x, d = 2) => { const n = Number(x); if (!isFinite(n)) return String(x); const r = n.toFixed(d); return /^-0(\.0+)?$/.test(r) ? r.slice(1) : r; };
  const BUILTINS = {
    sin: Math.sin, cos: Math.cos, tan: Math.tan, atan2: Math.atan2, sqrt: Math.sqrt, abs: Math.abs, min: Math.min, max: Math.max,
    round: Math.round, floor: Math.floor, ceil: Math.ceil, exp: Math.exp, log: Math.log, pow: Math.pow, PI: Math.PI,
    fmt, deg2rad: (d) => d * Math.PI / 180,
    range: (n) => { n = Math.min(Math.max(0, Math.floor(n)), 2000); return Array.from({ length: n }, (_, i) => i); },
    map: (a, f) => { if (!Array.isArray(a) || typeof f !== "function") fail("map(array, fn)"); return weigh(a.slice(0, 2000).map((x, i) => f(x, i))); },
    sum: (a) => (Array.isArray(a) ? a.reduce((s, x) => { charge(x); return s + Number(x); }, 0) : fail("sum(array)")),
    len: (a) => (Array.isArray(a) || typeof a === "string" ? a.length : fail("len(array)")),
    iterate: (n, x, f) => { n = Math.min(Math.max(0, Math.floor(n)), 2000); const out = []; for (let i = 0; i < n; i++) { out.push(x); x = f(x, i); } return weigh(out); },
    asin: Math.asin, acos: Math.acos, atan: Math.atan, hypot: Math.hypot, sign: Math.sign, log2: Math.log2, log10: Math.log10,
    clamp: (x, a, b) => Math.min(b, Math.max(a, x)), lerp: (a, b, t) => a + (b - a) * t, remap: (x, a, b, c, d) => c + (x - a) * (d - c) / (b - a),
    rand: (seed) => { let x = Math.imul((Number(seed) * 1000 | 0) ^ 0x9e3779b9, 0x85ebca6b); x ^= x >>> 13; x = Math.imul(x, 0xc2b2ae35); x ^= x >>> 16; return (x >>> 0) / 4294967296; },
    filter: (a, f) => (Array.isArray(a) && typeof f === "function" ? weigh(a.slice(0, 2000).filter((x, i) => f(x, i))) : fail("filter(array, fn)")),
    fold: (a, x, f) => { if (!Array.isArray(a) || typeof f !== "function") fail("fold(array, start, fn)"); a.slice(0, 2000).forEach((v) => { x = f(x, v); }); return x; },
    slice: (a, i, j) => (Array.isArray(a) ? weigh(a.slice(i, j)) : fail("slice(array, from, to)")),
    concat: (a, b) => (Array.isArray(a) && Array.isArray(b) ? weigh(a.concat(b).slice(0, 2000)) : fail("concat(array, array)")),
    reverse: (a) => (Array.isArray(a) ? weigh([...a].reverse()) : fail("reverse(array)")),
    sort: (a) => (Array.isArray(a) ? weigh([...a].sort((x, y) => Number(x) - Number(y))) : fail("sort(array)")),
    put: (a, i, v) => { if (!Array.isArray(a)) fail("put(array, index, value)"); const o = [...a]; if (Number.isInteger(i) && i >= 0 && i < o.length) o[i] = v; return weigh(o); },
    swap: (a, i, j) => { if (!Array.isArray(a)) fail("swap(array, i, j)"); const o = [...a]; if (Number.isInteger(i) && Number.isInteger(j) && i >= 0 && j >= 0 && i < o.length && j < o.length) { const t = o[i]; o[i] = o[j]; o[j] = t; } return weigh(o); },
    repeat: (v, n) => { n = Math.min(Math.max(0, Math.floor(n)), 2000); return weigh(Array.from({ length: n }, () => v)); },
    indexOf: (a, v) => (Array.isArray(a) || typeof a === "string" ? a.indexOf(v) : -1),
    has: (a, v) => (Array.isArray(a) || typeof a === "string" ? a.includes(v) : false),
    join: (a, sep = "") => (Array.isArray(a) ? a.map((x) => (x !== null && typeof x === "object" ? "" : String(x))).join(String(sep)).slice(0, MAX_TEXT_VALUE) : fail("join(array, separator)")),
    str: (x) => String(x !== null && typeof x === "object" ? "" : x),
    /* a path's d: a circle's arc from angle a to angle b in degrees, clockwise from the +x axis (in a y-down picture) */
    arc: (cx, cy, r, a, b) => { const t = Math.PI / 180, A = a * t, B = b * t, big = Math.abs(b - a) % 360 > 180 ? 1 : 0, f = (v) => Math.round(v * 100) / 100;
      return `M${f(cx + r * Math.cos(A))} ${f(cy + r * Math.sin(A))}A${f(r)} ${f(r)} 0 ${big} ${b > a ? 1 : 0} ${f(cx + r * Math.cos(B))} ${f(cy + r * Math.sin(B))}`; },
  };
  /* Sandbox rules (runtime, whatever the validator let through):
     - member keys are strings or numbers only; constructor / __proto__ / prototype are refused;
     - every member read is an own-property read (array / string: an in-range integer index or
       "length"; anything else is undefined); functions have no members;
     - a computed key (x[expression], anything but a literal) is a number on a real array: text that
       is not all digits throws, and on anything but an array the result is undefined;
     - only builtins and lambdas made by this evaluator can be called, and a function can only be
       passed where a builtin takes one (map's 2nd, iterate's 3rd argument) — so no value is a function;
     - every evaluation pass has a step budget (STEP_LIMIT nodes), so no expression can hang a page. */
  const BLOCKED = new Set(["constructor", "__proto__", "prototype"]);
  const LAMBDAS = new WeakSet();
  const BUILTIN_FNS = new Set();
  const STEP_LIMIT = 200000;
  let steps = 0;
  const resetSteps = () => { steps = 0; };
  const charge = (v) => { if (v !== null && typeof v === "object") { steps += weightOf(v); if (steps > STEP_LIMIT) fail("expression too expensive (step limit)"); } };
  const hasOwn = (o, k) => Object.prototype.hasOwnProperty.call(o, k);
  function member(o, k) {
    if (o == null) fail("member of null");
    if (typeof o === "function") fail("functions have no members");
    if (typeof k !== "string" && typeof k !== "number") fail("member key must be text or a number");
    if (typeof k === "string" && BLOCKED.has(k)) fail("member " + k + " is not allowed");
    if (Array.isArray(o) || typeof o === "string") {
      if (k === "length") return o.length;
      const i = typeof k === "number" ? k : (/^(0|[1-9][0-9]*)$/.test(k) ? Number(k) : NaN);
      return Number.isInteger(i) && i >= 0 && i < o.length ? o[i] : undefined;
    }
    if (typeof o !== "object") return undefined;
    const key = String(k);
    if (BLOCKED.has(key)) fail("member " + key + " is not allowed");
    return hasOwn(o, key) ? o[key] : undefined;
  }
  function computedMember(o, k) {
    if (typeof k === "string" && !/^(0|[1-9][0-9]*)$/.test(k)) fail("a computed member key must be a number");
    if (typeof k !== "string" && typeof k !== "number") fail("member key must be text or a number");
    return o == null || typeof o === "function" || Array.isArray(o) ? member(o, k) : undefined;
  }
  function ev(a, env) {
    if (++steps > STEP_LIMIT) fail("expression too expensive (step limit)");
    switch (a.k) {
      case "lit": return a.v;
      case "id": if (Object.prototype.hasOwnProperty.call(env, a.v)) return env[a.v]; if (Object.prototype.hasOwnProperty.call(BUILTINS, a.v)) return BUILTINS[a.v]; fail("unknown name " + a.v);
      case "arr": return weigh(a.items.map((x) => ev(x, env)));
      case "obj": { const o = {}; for (const [k, v] of a.props) o[k] = ev(v, env); return weigh(o); }
      case "neg": { const v = ev(a.e, env); charge(v); return -v; }
      case "not": return !ev(a.e, env);
      case "cond": return ev(a.c, env) ? ev(a.a, env) : ev(a.b, env);
      case "mem": { const o = ev(a.o, env), k = ev(a.name, env); return a.name.k === "lit" ? member(o, k) : computedMember(o, k); }
      case "call": { const f = ev(a.f, env); if (typeof f !== "function" || !(BUILTIN_FNS.has(f) || LAMBDAS.has(f))) fail("not a function"); const args = a.args.map((x) => ev(x, env));
        args.forEach((v, i) => { if (typeof v === "function" && FN_ARG.get(f) !== i) fail("a function can only be the fn argument of map / iterate"); });
        if (f !== BUILTINS.map && f !== BUILTINS.iterate && f !== BUILTINS.filter && f !== BUILTINS.fold && f !== BUILTINS.sum && f !== BUILTINS.len) args.forEach(charge);
        return f(...args); }
      case "fn": { const f = (x, y) => { const e = Object.assign(Object.create(null), env, { [a.param]: x }); if (a.param2) e[a.param2] = y; return noFn(ev(a.body, e)); }; LAMBDAS.add(f); return f; }
      case "bin": {
        if (a.op === "&&") return ev(a.l, env) && ev(a.r, env); if (a.op === "||") return ev(a.l, env) || ev(a.r, env);
        const l = ev(a.l, env), r = ev(a.r, env);
        if (a.op !== "==" && a.op !== "!=") { charge(l); charge(r); }
        switch (a.op) { case "+": { const v = l + r; if (typeof v === "string" && v.length > MAX_TEXT_VALUE) fail("text too long"); return v; } case "-": return l - r; case "*": return l * r; case "/": return l / r; case "%": return l % r; case "**": return l ** r;
          case "==": return l === r; case "!=": return l !== r; case "<": return l < r; case "<=": return l <= r; case ">": return l > r; case ">=": return l >= r; }
      }
    }
    fail("bad expression");
  }
  const cache = new Map();
  const compile = (src) => { if (typeof src === "number" || typeof src === "boolean") return { k: "lit", v: src }; if (!cache.has(src)) cache.set(src, parse(src)); return cache.get(src); };
  for (const v of Object.values(BUILTINS)) if (typeof v === "function") BUILTIN_FNS.add(v);
  function noFn(v) { if (typeof v === "function") fail("a function is not a value"); return v; }
  const FN_ARG = new Map([[BUILTINS.map, 1], [BUILTINS.iterate, 2], [BUILTINS.filter, 1], [BUILTINS.fold, 2]]);
  const evalExpr = (src, env) => noFn(ev(compile(src), env));
  /* 0..n-1 in an order fixed by `seed` (the same on every render, so a reload does not move the options). */
  const seededOrder = (n, seed) => {
    let x = 7; for (const c of String(seed)) x = (Math.imul(x, 31) + c.charCodeAt(0)) >>> 0;
    const out = Array.from({ length: n }, (_, i) => i);
    for (let i = n - 1; i > 0; i--) { x = (Math.imul(x, 1103515245) + 12345) >>> 0; const j = (x >>> 8) % (i + 1); [out[i], out[j]] = [out[j], out[i]]; }
    return out;
  };
  /* The value of a template: a lone "{expr}" keeps its type (a list, a number); anything else is text. */
  const bound = (v, E) => { if (typeof v !== "string") return v; const m = /^\{([^{}]+)\}$/.exec(v); if (m) return evalExpr(m[1], E); return v.includes("{") ? template(v, E) : v; };
  const COLORS = { blue: "var(--eu-sky,var(--eu-blue))", orange: "var(--eu-miss)", green: "var(--eu-right)", red: "var(--eu-wrong)", yellow: "var(--eu-shaky)", purple: "var(--eu-c6)", teal: "var(--eu-teal)", gray: "var(--eu-fg2)",
    text: "var(--eu-fg)", muted: "var(--eu-fg2)", line: "var(--eu-line)", bg: "var(--eu-bg)", surface: "var(--eu-surface)", none: "none", transparent: "transparent", currentColor: "var(--eu-fg)",
    info: "var(--eu-sky,var(--eu-blue))", success: "var(--eu-right)", warning: "var(--eu-shaky)", danger: "var(--eu-wrong)", discovery: "var(--eu-c6)", secondary: "var(--eu-fg2)", default: "var(--eu-fg)", subtle: "var(--eu-line)", primary: "var(--eu-fg)", "surface-secondary": "var(--eu-surface)", "surface-tertiary": "var(--eu-line)" };
  for (const c of ["blue", "orange", "green", "red", "yellow", "purple", "teal", "gray"]) COLORS[c + "-soft"] = `color-mix(in srgb,${COLORS[c]} 22%,var(--eu-bg))`;
  /* The picture follows the note's theme, so a written colour becomes the nearest one of ours (a fixed hex would vanish in dark mode). */
  function nearestColor(s) {
    let r, g, b, a = 1, m = /^#([0-9a-f]{3,8})$/i.exec(s);
    if (m) { let x = m[1]; if (x.length < 6) x = [...x].map((c) => c + c).join(""); r = parseInt(x.slice(0, 2), 16); g = parseInt(x.slice(2, 4), 16); b = parseInt(x.slice(4, 6), 16); if (x.length === 8) a = parseInt(x.slice(6, 8), 16) / 255; }
    else if ((m = /^rgba?\(([^)]+)\)$/.exec(s))) { const p = m[1].split(/[\s,/]+/).map(parseFloat); [r, g, b] = p; if (p.length > 3) a = p[3]; }
    else return null;
    if (![r, g, b].every(isFinite)) return null;
    const hi = Math.max(r, g, b) / 255, lo = Math.min(r, g, b) / 255, l = (hi + lo) / 2, sat = hi === lo ? 0 : (hi - lo) / (1 - Math.abs(2 * l - 1));
    const light = l > 0.82 || a < 0.5;
    if (sat < 0.15) return COLORS[l > 0.9 ? "surface" : l < 0.3 ? "text" : light ? "gray-soft" : "gray"];
    const d = (hi - lo) * 255; let hue = hi * 255 === r ? ((g - b) / d + 6) % 6 : hi * 255 === g ? (b - r) / d + 2 : (r - g) / d + 4; hue *= 60;
    const name = hue < 18 || hue >= 330 ? "red" : hue < 42 ? "orange" : hue < 70 ? "yellow" : hue < 165 ? "green" : hue < 195 ? "teal" : hue < 255 ? "blue" : "purple";
    return COLORS[light ? name + "-soft" : name];
  }
  const colorOf = (v) => { if (v == null || v === false) return null; const s = String(v).trim(); return has(COLORS, s) ? COLORS[s] : nearestColor(s) || COLORS.gray; };
  const template = (s, env) => String(s ?? "").replace(/\{([^{}]+)\}/g, (_, e) => { const v = evalExpr(e, env); return v == null ? "" : String(v); });

  /* ---------- validation ----------
     Mirrors deep/evoui/validate.py (Python) rule for rule; tests/deep/test_evoui_parity.py runs one
     corpus through both. The tables below are deep/evoui/contracts.py's. */
  const BOX_STYLE = ["background", "width", "height", "flex", "radius", "padding", "align", "color", "clip", "click"];
  const FIELDS = {
    box: [["children"], ["gap", "border", "tone", "justify", ...BOX_STYLE]], row: [["children"], ["gap", "justify", "wrap", ...BOX_STYLE]],
    grid: [["children"], ["columns", "gap"]], "grid-item": [["children"], ["span", "gap", "border", "tone", ...BOX_STYLE]],
    text: [["text"], ["size", "weight", "tone", "color", "align", "rich"]], title: [["text"], ["size", "tone", "color"]], caption: [["text"], ["align"]],
    badge: [["text"], ["tone"]], math: [["tex"], ["display"]], table: [["columns", "rows"], ["caption"]],
    icon: [["name"], ["tone"]], divider: [[], []],
    slider: [["bind", "min", "max", "step"], ["label"]], input: [["bind", "min", "max", "step"], ["label"]],
    choice: [["bind", "options"], ["style"]], select: [["bind", "options"], ["label"]],
    toggle: [["bind"], ["label"]], checkbox: [["bind"], ["label"]], button: [["label", "set"], ["tone"]],
    if: [["cond", "then"], ["else"]],
    plot: [["xmin", "xmax", "ymin", "ymax", "items"], ["grid", "height", "legend"]],
    chart: [["chart", "data", "x", "series"], ["annotations", "caption", "curve", "donut", "gradient", "hover", "stack", "visible_points", "x_label", "y_label", "y_min"]],
    quiz: [["questions"], ["cases", "id", "layout", "title"]], flashcards: [["cards"], ["id", "shuffle", "title"]],
    reveal: [["children"], ["after", "id", "label", "mode"]], animate: [["children"], ["effect"]], transition: [["children", "key"], []],
    celebrate: [["when"], []], timer: [["mode"], ["autostart", "bind", "id", "label", "seconds"]],
    player: [["bind", "min", "max", "step"], ["id", "interval_ms", "label", "loop"]],
    pager: [["children"], ["gate", "id"]], "fill-blank": [["sentence", "options", "correct", "why"], ["cell", "id"]],
    sentence: [["sentence", "words", "answer", "why"], ["cell", "id"]], route: [["prompt", "options"], ["bind", "id", "preference"]],
    app: [["html"], ["height", "id", "images", "items"]], svg: [["viewBox", "children"], ["images", "tween"]], each: [["in", "children"], ["as", "index"]], timeline: [["events"], []], "deep-dive": [["children"], ["label"]] };
  /* A drawing's elements (`svg` node). Real SVG names; attributes are numbers or templates ("{i*5}"); arrow / node / plot are ours. */
  const SVG_ELS = { g: [], path: ["d"], rect: ["width", "height"], circle: ["r"], ellipse: ["rx", "ry"], line: [], polyline: ["points"], polygon: ["points"], text: ["text"], image: ["src", "width", "height"],
    arrow: ["x1", "y1", "x2", "y2"], node: ["width", "height"], plot: ["x", "y"], defs: [], clipPath: ["id"], animate: ["attributeName", "dur"], animateTransform: ["attributeName", "dur", "type"], animateMotion: ["dur", "path"] };
  const SVG_ATTRS = new Set(["x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry", "width", "height", "d", "points", "transform", "fill", "stroke", "stroke-width", "stroke-dasharray", "stroke-dashoffset", "stroke-linecap", "stroke-linejoin", "opacity", "fill-opacity", "stroke-opacity", "clip-path",
    "text-anchor", "dominant-baseline", "font-size", "font-weight", "font-style", "attributeName", "values", "from", "to", "by", "dur", "begin", "repeatCount", "keyTimes", "calcMode", "type", "path", "rotate", "text", "wrap", "tip", "bend", "both", "label", "src", "alt", "id", "color", "left", "top", "grid", "xlabel", "ylabel"]);
  const SVG_BOOL = ["grid", "both"], SMIL_ATTRS = ["x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry", "width", "height", "d", "points", "transform", "fill", "stroke", "opacity", "fill-opacity", "stroke-opacity", "stroke-width", "stroke-dashoffset"];
  const SVG_KIDS = ["g", "plot", "defs", "clipPath", "rect", "circle", "ellipse", "path", "line", "polygon", "polyline", "text", "image", "node", "arrow"];
  const SVG_COLORS = ["transparent", "blue", "orange", "green", "red", "yellow", "purple", "teal", "gray", "blue-soft", "orange-soft", "green-soft", "red-soft", "yellow-soft", "purple-soft", "teal-soft", "gray-soft", "text", "muted", "line", "bg", "surface", "none", "currentColor",
    "info", "success", "warning", "danger", "discovery", "secondary", "default", "subtle", "primary", "surface-secondary", "surface-tertiary"];
  const COLOR_ATTRS = ["fill", "stroke", "color", "background"];
  const COLOR_RE = /^(#[0-9a-fA-F]{3,8}|rgba?\([0-9.,%\s/]+\))$/, PATH_RE = /^[MmLlHhVvCcSsQqTtAaZz0-9eE\s,.+-]*$/, VIEWBOX_RE = /^-?[0-9.]+[ ,]+-?[0-9.]+[ ,]+[0-9.]+[ ,]+[0-9.]+$/;
  const MAX_MARKS = 6000;
  const SHAPE_FIELDS = { arrow: [["x1", "y1", "x2", "y2"], ["tone", "label", "dashed"]], point: [["x", "y"], ["tone", "label"]], polyline: [["points"], ["tone", "dashed"]], segment: [["x1", "y1", "x2", "y2"], ["tone", "dashed"]], label: [["x", "y", "text"], ["tone"]],
    rect: [["x", "y", "w", "h"], ["tone", "fill", "dashed", "label"]], circle: [["cx", "cy", "r"], ["tone", "fill", "dashed", "label"]], text: [["x", "y", "text"], ["tone", "anchor", "size"]] };
  const TONES = ["default", "secondary", "accent", "success", "warning", "danger"], BOX_TONES = ["plain", "surface"], BOX_TONED = ["box", "grid-item"];
  const CONTROLS = ["slider", "input", "choice", "select", "toggle", "checkbox", "player", "route"], NUMBER_CONTROLS = ["slider", "input", "player"], BOOL_CONTROLS = ["toggle", "checkbox"], OPTION_CONTROLS = ["choice", "select"];
  const TEXT_SIZES = ["xs", "sm", "md", "lg"], TITLE_SIZES = ["md", "lg", "xl", "2xl"], CHART_KINDS = ["line", "area", "bar", "scatter", "pie"], ANCHORS = ["start", "middle", "end"];
  /* chart options (contracts.CHART_CURVES / CHART_OPTION_KINDS / ANNOTATION_FIELDS) */
  const CHART_CURVES = ["linear", "smooth", "step"], CARTESIAN = ["line", "area", "bar"];
  const CHART_OPTION_KINDS = { stack: ["bar", "area"], curve: ["line", "area"], gradient: ["area"], hover: CARTESIAN, visible_points: CARTESIAN, annotations: CARTESIAN, donut: ["pie"] };
  const ANNOTATION_FIELDS = ["x", "y", "label", "tone"];
  const ICONS = ["arrow-down", "arrow-up", "arrow-left", "arrow-right", "check", "cross", "dot", "plus", "minus", "info", "warning"];
  const CELL_RE = /^[A-Za-z0-9_-]{1,64}\.[a-z_]{1,32}$/, CASE_ID = /^[a-z0-9_-]{1,32}$/, NODE_ID = /^[a-z0-9_-]{1,32}$/;
  /* Stateful learning nodes (contracts.STATEFUL_KINDS): they may carry `id`, and each gets a node key
     (`id`, else `<kind>#<n>`) that its progress and result events are filed under. */
  const QUIZ_LAYOUTS = ["list", "paged"], QUESTION_TYPES = ["single", "multi", "tf"];
  const QUESTION_FIELDS = [["q", "options", "correct", "why"], ["case", "cell", "type", "feedback", "hint"]];
  const CARD_FIELDS = [["front", "back"], ["cell", "hint"]];
  /* timeline events (contracts.TIMELINE_EVENT_FIELDS / MIN_EVENTS) and the deep-dive's default summary line
     (contracts.DEEP_DIVE_LABEL); after DeepTutor's book blocks (HKUDS/DeepTutor, Apache-2.0), ideas only */
  const TIMELINE_EVENT_FIELDS = [["title"], ["text", "tone", "when"]], MIN_EVENTS = 2, DEEP_DIVE_LABEL = "深入一点";
  /* practice nodes (contracts.PRACTICE_KINDS): the kinds whose done a reveal (mode "after") can wait for
     and a gated pager page needs before it lets the reader on */
  const PRACTICE = ["quiz", "flashcards", "fill-blank", "sentence", "pager"];
  /* fill-blank / sentence blanks (contracts.BLANK), their option / route option fields */
  const BLANK = "___", FILL_OPTION_FIELDS = [["label"], ["why"]], ROUTE_OPTION_FIELDS = [["label", "value", "next"], ["desc"]];
  const REVEAL_MODES = ["click", "steps", "after"], ANIMATE_EFFECTS = ["rise", "fade"], TIMER_MODES = ["countdown", "stopwatch"];
  const STATEFUL = ["quiz", "flashcards", "reveal", "pager", "fill-blank", "sentence", "timer", "route", "player", "app"];
  const LIMITS = { block: 40000, nodes: 400, depth: 16, expr: 500, text: 600, state: 32, derived: 48, items: 60, minCards: 2, options: 8, fallback: 1500, probes: 6, columns: 4, exprRows: 200, cases: 6, tex: 600, revealSteps: 12, timerSeconds: 3600, minInterval: 100, maxInterval: 5000,
    annotations: 8, minWindow: 3, blanks: 8, words: 12, wordChars: 60, minRouteOptions: 2, routeValue: 64 };
  /* app (contracts.APP_*): the sandboxed HTML mini-app's limits, CSP, CDN allowlist and static scan
     (regex sources shared verbatim with contracts.py; tests/deep/test_evoui_parity.py compares them) */
  const APP = {
    "cdns": ["https://cdn.jsdelivr.net/", "https://unpkg.com/", "https://cdnjs.cloudflare.com/"],
    "csp": "default-src 'none'; script-src 'unsafe-inline' https://cdn.jsdelivr.net/ https://unpkg.com/ https://cdnjs.cloudflare.com/; style-src 'unsafe-inline' https://cdn.jsdelivr.net/ https://unpkg.com/ https://cdnjs.cloudflare.com/; img-src data: blob:; font-src data: https://cdn.jsdelivr.net/ https://unpkg.com/ https://cdnjs.cloudflare.com/; media-src data: blob:; connect-src 'none'; form-action 'none'; base-uri 'none'",
    "sandbox": "allow-scripts",
    "html": 60000,
    "minHeight": 200,
    "maxHeight": 900,
    "height": 420,
    "minResize": 200,
    "maxResize": 1200,
    "items": 40,
    "label": 120,
    "message": 2000,
    "save": 16000,
    "events": 500,
    "readyMs": 8000,
    "latency": 3600000,
    "itemReports": 3,
    "itemFields": [["id", "label"], ["cell"]],
    "images": 4,
    "imageFields": [["id", "title", "license", "source", "source_url"], ["alt", "asset", "author", "license_url"]],
    "forbidden": [["<(?:iframe|object|embed|base)(?![A-Za-z0-9_-])", "i", "no <iframe>, <object>, <embed> or <base>"], ["<meta(?![A-Za-z0-9_-])[^>]*http-equiv", "i", "no <meta http-equiv>"], ["(?<![A-Za-z0-9_$])document[\\t\\n\\v\\f\\r \\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]*\\.[\\t\\n\\v\\f\\r \\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]*cookie(?![A-Za-z0-9_$])", "", "no document.cookie"], ["(?<![A-Za-z0-9_$])(?:localStorage|sessionStorage|indexedDB)(?![A-Za-z0-9_$])", "", "no localStorage / sessionStorage / indexedDB (keep progress with evowiki.save)"], ["(?<![A-Za-z0-9_$.])(?:window|self|globalThis)[\\t\\n\\v\\f\\r \\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]*(?:\\.[\\t\\n\\v\\f\\r \\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]*|\\[[\\t\\n\\v\\f\\r \\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]*[\"'`])(?:parent|top|opener|frameElement)(?![A-Za-z0-9_$])", "", "no window.parent / window.top (talk to the page through window.evowiki)"], ["(?<![A-Za-z0-9_$.#-])(?:parent|top|opener)[\\t\\n\\v\\f\\r \\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]*[.\\[]", "", "no parent. / top. access (talk to the page through window.evowiki; do not name a variable parent or top)"], ["(?<![A-Za-z0-9_$])location[\\t\\n\\v\\f\\r \\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]*(?:=(?![=>])|\\.[\\t\\n\\v\\f\\r \\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]*(?:(?:href|search|pathname|host|hostname|port|protocol)[\\t\\n\\v\\f\\r \\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]*=(?![=>])|(?:assign|replace|reload)(?![A-Za-z0-9_$])))", "", "no navigation (location = / location.href = / assign / replace / reload): the app stays on its page"], ["(?<![A-Za-z0-9_$])(?:webkit|moz)?RTC(?:PeerConnection|DataChannel)(?![A-Za-z0-9_$])", "", "no WebRTC"], ["<link(?![A-Za-z0-9_-])[^>]*(?:dns-prefetch|preconnect|prefetch|prerender)", "i", "no <link> dns-prefetch / preconnect / prefetch / prerender"]],
    "urls": [["(?<![A-Za-z0-9_$-])(?:src|href|srcset|poster)[\\t\\n\\v\\f\\r \\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]*=[\\t\\n\\v\\f\\r \\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]*\\\"([^\\\"]*)\\\"", "i"], ["(?<![A-Za-z0-9_$-])(?:src|href|srcset|poster)[\\t\\n\\v\\f\\r \\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]*=[\\t\\n\\v\\f\\r \\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]*'([^']*)'", "i"], ["(?<![A-Za-z0-9_$-])(?:src|href|srcset|poster)[\\t\\n\\v\\f\\r \\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]*=[\\t\\n\\v\\f\\r \\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]*`([^`]*)`", "i"], ["(?<![A-Za-z0-9_$-])(?:src|href|srcset|poster)[\\t\\n\\v\\f\\r \\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]*=[\\t\\n\\v\\f\\r \\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]*([^\"'`>\\t\\n\\v\\f\\r \\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]+)", "i"], ["(?<![A-Za-z0-9_$-])url[\\t\\n\\v\\f\\r \\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]*\\([\\t\\n\\v\\f\\r \\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]*[\"'`]?([^\"'`)]*)", ""], ["(?<![A-Za-z0-9_$.])import[\\t\\n\\v\\f\\r \\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]*\\(?[\\t\\n\\v\\f\\r \\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]*[\"'`]([^\"'`]*)", ""], ["(?<![A-Za-z0-9_$.])(?:import|export)(?![A-Za-z0-9_$])[^;\"'`]*?(?<![A-Za-z0-9_$])from[\\t\\n\\v\\f\\r \\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]*[\"'`]([^\"'`]*)", ""]],
    "external": "^(?:[A-Za-z][A-Za-z0-9+.-]*:|[/\\\\]{2})",
    "local": "^(?:data|blob|javascript|about):",
    "trim": "^[\\t\\n\\v\\f\\r \\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]+|[\\t\\n\\v\\f\\r \\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]+$"
  };
  const TOP = ["type", "title", "question", "state", "derived", "body", "fallback", "probes"], TOP_REQ = ["type", "title", "question", "state", "derived", "body", "fallback"];
  const NAME_RE = /^[A-Za-z_一-鿿][A-Za-z0-9_一-鿿]*$/;
  const isObj = (x) => x !== null && typeof x === "object" && !Array.isArray(x);
  const isNum = (x) => typeof x === "number" && isFinite(x);
  const isInt = (x) => Number.isInteger(x);
  const isStr = (x) => typeof x === "string";
  const has = (o, k) => Object.prototype.hasOwnProperty.call(o, k);
  function freeNames(ast, bound, out) {
    switch (ast.k) {
      case "id": if (!bound.has(ast.v)) out.push(ast.v); return;
      case "lit": return;
      case "arr": ast.items.forEach((x) => freeNames(x, bound, out)); return;
      case "obj": ast.props.forEach(([, v]) => freeNames(v, bound, out)); return;
      case "neg": case "not": freeNames(ast.e, bound, out); return;
      case "cond": freeNames(ast.c, bound, out); freeNames(ast.a, bound, out); freeNames(ast.b, bound, out); return;
      case "mem": freeNames(ast.o, bound, out); freeNames(ast.name, bound, out);
        if (ast.name.k === "lit" && typeof ast.name.v === "string" && BLOCKED.has(ast.name.v)) out.push("member:" + ast.name.v);
        if (ast.name.k === "arr" || ast.name.k === "obj") out.push("member:computed");
        return;
      case "call": freeNames(ast.f, bound, out); ast.args.forEach((x) => freeNames(x, bound, out)); return;
      case "fn": freeNames(ast.body, new Set([...bound, ast.param, ast.param2]), out); return;
      case "bin": freeNames(ast.l, bound, out); freeNames(ast.r, bound, out); return;
    }
  }
  /* Functions never become values: a builtin function name may only be the callee of a call, and an
     arrow may only be a direct argument of a call. So nothing can store, index or return a function. */
  const FN_NAMES = new Set(Object.keys(BUILTINS).filter((k) => typeof BUILTINS[k] === "function"));
  function fnUse(ast, bound, ctx, out) {
    switch (ast.k) {
      case "id": if (!bound.has(ast.v) && FN_NAMES.has(ast.v) && ctx !== "callee") out.push("fn-value:" + ast.v); return;
      case "fn": if (ctx !== "arg") out.push("fn-arrow"); fnUse(ast.body, new Set([...bound, ast.param, ast.param2]), null, out); return;
      case "call": fnUse(ast.f, bound, "callee", out); ast.args.forEach((x) => fnUse(x, bound, "arg", out)); return;
      case "lit": return;
      case "arr": ast.items.forEach((x) => fnUse(x, bound, null, out)); return;
      case "obj": ast.props.forEach(([, v]) => fnUse(v, bound, null, out)); return;
      case "neg": case "not": fnUse(ast.e, bound, null, out); return;
      case "cond": fnUse(ast.c, bound, null, out); fnUse(ast.a, bound, null, out); fnUse(ast.b, bound, null, out); return;
      case "mem": fnUse(ast.o, bound, null, out); fnUse(ast.name, bound, null, out); return;
      case "bin": fnUse(ast.l, bound, null, out); fnUse(ast.r, bound, null, out); return;
    }
  }
  /* the block cap counts the html of the app nodes the walk found (`apps`) as "" (validate.py without_app_html);
     anything else shaped like an app node (a state value, say) keeps its html and counts */
  const withoutAppHtml = (apps) => function (k, v) { return k === "html" && apps.has(this) && isStr(v) ? "" : v; };
  /* The app html's static scan (validate.py app_html_problems, the same regex sources in APP): forbidden tags /
     storage / parent access, and every URL in src= / href= / url( / import that names a scheme or a //host other
     than data: / blob: / javascript: / about: or an allowlisted CDN. Best effort: the iframe sandbox + CSP are the
     boundary. Compiled on first use; an engine that cannot compile them (no lookbehind) refuses every app. */
  let APP_RX = null;
  function appHtmlProblems(html) {
    if (APP_RX === null) {
      try {
        APP_RX = { forbidden: APP.forbidden.map(([p, f, m]) => [new RegExp(p, f), m]), urls: APP.urls.map(([p, f]) => new RegExp(p, "g" + f)),
          external: new RegExp(APP.external), local: new RegExp(APP.local, "i"), trim: new RegExp(APP.trim, "g") };
      } catch (x) { APP_RX = false; }
    }
    if (!APP_RX) return ["this renderer cannot check app html (regular expressions unsupported)"];
    const out = APP_RX.forbidden.filter(([rx]) => rx.test(html)).map(([, m]) => m), bad = [];
    for (const rx of APP_RX.urls) for (const mt of html.matchAll(rx)) {
      const u = mt[1].replace(APP_RX.trim, "");
      if (APP_RX.external.test(u) && !APP_RX.local.test(u) && !APP.cdns.some((c) => u.toLowerCase().startsWith(c)) && !bad.includes(u)) bad.push(u);
    }
    return out.concat(bad.map((u) => "external URL " + u.slice(0, 80) + " (only " + APP.cdns.join(" ") + ")"));
  }
  /* a route option value's kind + value ("s:" text, "n:" number, "b:" true/false; null: none of them) */
  const routeValue = (v) => (typeof v === "boolean" ? "b:" + v : isNum(v) ? "n:" + v : isStr(v) ? "s:" + v : null);
  function validate(block) {
    const errs = []; const e = (m) => { errs.push(m); };
    if (!isObj(block)) return ["block is not an object"];
    if (block.type !== "evoui") e("type must be evoui");
    for (const k of Object.keys(block)) if (!TOP.includes(k)) e("unknown field " + k);
    for (const k of TOP_REQ) if (!has(block, k)) e("missing " + k);
    const text = (v, where, max) => { if (!isStr(v)) return e(where + ": must be text"); if (!v.trim()) e(where + ": empty"); if (v.length > max) e(where + ": longer than " + max); };
    if (has(block, "title")) text(block.title, "title", 120);
    if (has(block, "question")) text(block.question, "question", 300);
    if (has(block, "fallback")) text(block.fallback, "fallback", LIMITS.fallback);
    const BUILT = new Set(Object.keys(BUILTINS)), RESERVED = new Set(["true", "false", "null"]);
    let state = {}, derived = {};
    if (has(block, "state")) { if (isObj(block.state)) state = block.state; else e("state must be an object"); }
    if (has(block, "derived")) { if (isObj(block.derived)) derived = block.derived; else e("derived must be an object"); }
    if (Object.keys(state).length > LIMITS.state) e("more than " + LIMITS.state + " states");
    if (Object.keys(derived).length > LIMITS.derived) e("more than " + LIMITS.derived + " derived values");
    const scope = new Set();
    const goodName = (k, where) => { if (!NAME_RE.test(k) || BUILT.has(k) || RESERVED.has(k)) e(where + ": bad name " + k); else if (scope.has(k)) e(where + ": name " + k + " used twice"); scope.add(k); };
    for (const k of Object.keys(state)) goodName(k, "state");
    const expr = (src, where) => {
      if (isNum(src)) return;
      if (!isStr(src)) return e(where + ": expression must be text or a number");
      if (src.length > LIMITS.expr) return e(where + ": expression longer than " + LIMITS.expr);
      let ast; try { ast = parse(src); } catch (x) { return e(where + ": " + x.message); }
      const names = []; freeNames(ast, new Set(), names); fnUse(ast, new Set(), null, names);
      for (const n of names) {
        if (n === "member:computed") e(where + ": a member key cannot be an array or object");
        else if (n === "fn-arrow") e(where + ": an arrow function can only be an argument of a call");
        else if (n.startsWith("fn-value:")) e(where + ": " + n.slice(9) + " can only be called, not used as a value");
        else if (n.startsWith("member:")) e(where + ": member " + n.slice(7) + " is not allowed");
        else if (!scope.has(n) && !BUILT.has(n)) e(where + ": unknown name " + n);
      }
    };
    const tpl = (s, where) => {
      if (!isStr(s)) return e(where + ": template must be text");
      if (s.length > LIMITS.text) return e(where + ": longer than " + LIMITS.text);
      for (const m of s.matchAll(/\{([^{}]+)\}/g)) expr(m[1], where);
      if (s.replace(/\{([^{}]+)\}/g, "").includes("{")) e(where + ": a { that never closes");
    };
    for (const [k, v] of Object.entries(derived)) { if (!isStr(v)) e("derived." + k + ": must be an expression"); else expr(v, "derived." + k); goodName(k, "derived"); }
    const fields = (o, req, opt, where) => { for (const f of req) if (!has(o, f)) e(where + ": missing " + f); for (const f of Object.keys(o)) if (f !== "kind" && f !== "shape" && !req.includes(f) && !opt.includes(f)) e(where + ": unknown field " + f); };
    const bool = (o, f, where) => { if (has(o, f) && typeof o[f] !== "boolean") e(where + "." + f + ": must be true/false"); };
    const oneOf = (o, f, vals, where) => { if (has(o, f) && !vals.includes(o[f])) e(where + "." + f + ": must be one of " + vals.join("/")); };
    let count = 0; const nodeIds = new Set(), idNodes = new Map(), revealRefs = [], pagers = [], apps = [], appNodes = new Set();
    let inEach = 0;
    const scoped = (names, fn) => { const fresh = names.filter((x) => !scope.has(x)); fresh.forEach((x) => scope.add(x)); fn(); fresh.forEach((x) => scope.delete(x)); };
    const eachNames = (n, where) => {
      const names = [isStr(n.as) ? n.as : "d", isStr(n.index) ? n.index : "i"];
      for (const x of names) if (!NAME_RE.test(x) || BUILT.has(x) || RESERVED.has(x)) e(where + ": bad name " + x); else if (scope.has(x)) e(where + ": " + x + " is already a name here: give this each another one");
      if (has(n, "in")) expr(n.in, where + ".in");
      return names;
    };
    const stateMap = (m, where, names) => {
      if (!isObj(m) || !Object.keys(m).length) return e(where + ": {state: value}");
      for (const [k, v] of Object.entries(m)) {
        if (!has(state, k)) e(where + ": " + k + " is not a state");
        if (isStr(v)) scoped(names, () => expr(v, where + "." + k)); else if (!(isNum(v) || typeof v === "boolean")) e(where + "." + k + ": a number, true/false or an expression");
      }
    };
    const pictures = (n, where) => {
      if (!Array.isArray(n.images) || n.images.length > APP.images) return e(where + ".images: a list of up to " + APP.images + " images");
      const seen = new Set();
      n.images.forEach((im, i) => {
        const w = `${where}.images[${i}]`;
        if (!isObj(im)) return e(w + ": an image");
        fields(im, APP.imageFields[0], APP.imageFields[1], w);
        for (const k of Object.keys(im)) text(im[k], w + "." + k, k !== "source_url" && k !== "license_url" ? APP.label : LIMITS.text);
        if (!has(im, "id")) return;
        if (!(isStr(im.id) && NODE_ID.test(im.id))) e(w + ".id: [a-z0-9_-], 1–32"); else if (seen.has(im.id)) e(w + ".id: used twice"); else seen.add(im.id);
      });
    };
    const markAttr = (k, v, n, where) => {
      if (isNum(v)) return;
      if (typeof v === "boolean") { if (!SVG_BOOL.includes(k)) e(where + "." + k + ": a number or text, not true/false"); return; }
      if (Array.isArray(v)) { if (!((n.el === "plot" && (k === "x" || k === "y") && v.length === 2 && v.every(isNum)) || (k === "points" && v.every((p) => Array.isArray(p) && p.length === 2 && p.every(isNum))))) e(where + "." + k + ": not a list here"); return; }
      if (!isStr(v)) return e(where + "." + k + ": a number or text");
      tpl(v, where + "." + k);
      const fixed = !v.includes("{");
      if (fixed && COLOR_ATTRS.includes(k) && !SVG_COLORS.includes(v) && !COLOR_RE.test(v)) e(where + "." + k + ": " + v + " is not a colour: use " + SVG_COLORS.slice(1, 17).join(" "));
      if (fixed && k === "d" && !PATH_RE.test(v)) e(where + ".d: only path commands and numbers");
      if (fixed && k === "clip-path" && !/^url\(#[a-z0-9_-]+\)$/i.test(v)) e(where + ".clip-path: url(#id)");
      if (fixed && k === "attributeName" && !SMIL_ATTRS.includes(v)) e(where + ".attributeName: " + v + " cannot be animated");
      if (k === "id" && !(fixed && NODE_ID.test(v))) e(where + ".id: [a-z0-9_-], 1–32");
    };
    const mark = (n, path, depth, images, locals) => {
      count += 1;
      if (count > LIMITS.nodes) { if (count === LIMITS.nodes + 1) e("more than " + LIMITS.nodes + " nodes"); return; }
      if (depth > LIMITS.depth) return e(path + ": nested deeper than " + LIMITS.depth);
      if (!isObj(n)) return e(path + ": not an element");
      if (n.kind === "each") {
        const where = path + ".each"; fields(n, FIELDS.each[0], FIELDS.each[1], where);
        const names = eachNames(n, where);
        if (!Array.isArray(n.children)) { if (has(n, "children")) e(where + ".children: must be a list"); return; }
        scoped(names, () => n.children.forEach((c, i) => mark(c, `${where}[${i}]`, depth + 1, images, [...locals, ...names])));
        return;
      }
      if (n.kind === "if") {
        const where = path + ".if"; fields(n, FIELDS.if[0], FIELDS.if[1], where);
        if (has(n, "cond")) expr(n.cond, where + ".cond");
        if (has(n, "then")) mark(n.then, where + ".then", depth + 1, images, locals);
        if (has(n, "else") && n.else !== null) mark(n.else, where + ".else", depth + 1, images, locals);
        return;
      }
      if (has(n, "kind")) return e(path + ": " + n.kind + " cannot go inside svg: only drawing elements, each and if");
      const req = has(SVG_ELS, n.el) ? SVG_ELS[n.el] : null;
      if (!req) return e(path + ": unknown element " + n.el + ": use " + Object.keys(SVG_ELS).slice(0, 14).join(" "));
      const where = path + "." + n.el;
      for (const f of req) if (!has(n, f)) e(where + ": missing " + f);
      for (const [k, v] of Object.entries(n)) {
        if (k === "el" || k === "children") continue;
        if (k === "click" || k === "drag") { stateMap(v, where + "." + k, k === "drag" ? ["mx", "my"] : []); continue; }
        if (!SVG_ATTRS.has(k)) { e(where + ": unknown attribute " + k); continue; }
        markAttr(k, v, n, where);
      }
      if (n.el === "image" && isStr(n.src) && !images.has(n.src)) e(where + ".src: " + n.src + " is not one of this svg's images");
      if (has(n, "children")) {
        if (!Array.isArray(n.children)) return e(where + ".children: must be a list");
        if (!SVG_KIDS.includes(n.el)) return e(where + ": " + n.el + " takes no children");
        n.children.forEach((c, i) => mark(c, `${where}[${i}]`, depth + 1, images, locals));
      }
    };
    const walk = (n, path, depth) => {
      count += 1;
      if (count > LIMITS.nodes) { if (count === LIMITS.nodes + 1) e("more than " + LIMITS.nodes + " nodes"); return; }
      if (depth > LIMITS.depth) return e(path + ": nested deeper than " + LIMITS.depth);
      if (!isObj(n)) return e(path + ": not a node");
      const spec = has(FIELDS, n.kind) ? FIELDS[n.kind] : null;
      if (!spec) return e(path + ": unknown kind " + n.kind);
      const where = path + "." + n.kind; fields(n, spec[0], spec[1], where);
      if (has(n, "tone")) oneOf(n, "tone", BOX_TONED.includes(n.kind) ? BOX_TONES : TONES, where);
      if (has(n, "gap") && !(isInt(n.gap) && n.gap >= 0 && n.gap <= 4)) e(where + ".gap: 0–4");
      for (const f of ["columns", "span"]) if ((n.kind === "grid" || n.kind === "grid-item") && has(n, f) && !(isInt(n[f]) && n[f] >= 1 && n[f] <= LIMITS.columns)) e(where + "." + f + ": 1–" + LIMITS.columns);
      oneOf(n, "justify", ["start", "between", "center", "end"], where); if (n.kind === "caption") oneOf(n, "align", ["start", "center", "end"], where); oneOf(n, "size", n.kind === "title" ? TITLE_SIZES : TEXT_SIZES, where);
      oneOf(n, "weight", ["normal", "medium", "bold"], where); oneOf(n, "style", ["segmented", "buttons"], where);
      if (inEach && STATEFUL.includes(n.kind)) e(where + ": " + n.kind + " keeps its own progress: it cannot go inside each");
      if (spec[1].includes("background")) {
        for (const f of ["background", "width", "height", "flex", "radius", "padding", "color"]) if (has(n, f)) markAttr(f, n[f], n, where);
        oneOf(n, "align", ["start", "center", "end", "stretch"], where); bool(n, "clip", where);
        if (has(n, "click")) stateMap(n.click, where + ".click", []);
      }
      if ((n.kind === "text" || n.kind === "title") && has(n, "color")) markAttr("color", n.color, n, where);
      if (n.kind === "text") { oneOf(n, "align", ["start", "center", "end"], where); bool(n, "rich", where); }
      if (STATEFUL.includes(n.kind) && has(n, "id")) { if (!(isStr(n.id) && NODE_ID.test(n.id))) e(where + ".id: [a-z0-9_-], 1–32"); else if (nodeIds.has(n.id)) e(where + ".id: used twice"); else { nodeIds.add(n.id); idNodes.set(n.id, n); } }
      for (const f of ["border", "wrap", "grid", "display"]) bool(n, f, where);
      if (n.kind === "svg") {
        if (has(n, "viewBox") && !(isStr(n.viewBox) && VIEWBOX_RE.test(n.viewBox))) e(where + ".viewBox: four numbers, \"0 0 640 300\"");
        if (has(n, "tween") && !(isInt(n.tween) && n.tween >= 0 && n.tween <= 2000)) e(where + ".tween: 0–2000 ms");
        if (has(n, "images")) pictures(n, where);
        const images = new Set((Array.isArray(n.images) ? n.images : []).map((im) => im && im.id));
        if (has(n, "children")) { if (!Array.isArray(n.children)) e(where + ".children: must be a list"); else n.children.forEach((c, i) => mark(c, `${where}[${i}]`, depth + 1, images, [])); }
        return;
      }
      if (n.kind === "each") {
        const names = eachNames(n, where);
        if (!Array.isArray(n.children)) { if (has(n, "children")) e(where + ".children: must be a list"); return; }
        inEach += 1; scoped(names, () => n.children.forEach((c, i) => walk(c, `${where}[${i}]`, depth + 1))); inEach -= 1;
        return;
      }
      if (has(n, "children")) { if (!Array.isArray(n.children)) e(where + ".children: must be a list"); else n.children.forEach((c, i) => walk(c, `${where}[${i}]`, depth + 1)); }
      if (CONTROLS.includes(n.kind) && has(n, "bind") && !(isStr(n.bind) && has(state, n.bind))) e(where + ": bind " + n.bind + " is not a state");
      const bound = isStr(n.bind) && has(state, n.bind);
      if (NUMBER_CONTROLS.includes(n.kind) && bound && !isNum(state[n.bind])) e(where + ": bind must be a number state");
      if (BOOL_CONTROLS.includes(n.kind) && bound && typeof state[n.bind] !== "boolean" && state[n.bind] !== 0 && state[n.bind] !== 1) e(where + ": bind must be a true/false state, or 0 / 1");
      switch (n.kind) {
        case "text": case "badge": case "title": case "caption": if (has(n, "text")) tpl(n.text, where + ".text"); break;
        case "math": if (has(n, "tex")) text(n.tex, where + ".tex", LIMITS.tex); break;
        case "icon": oneOf(n, "name", ICONS, where); break;
        case "table": table(n, where); break;
        case "button":
          if (has(n, "label")) tpl(n.label, where + ".label");
          if (has(n, "set")) {
            if (!isObj(n.set) || !Object.keys(n.set).length || Object.keys(n.set).length > LIMITS.state) e(where + ".set: {state: expression}, 1–" + LIMITS.state);
            else for (const [k, v] of Object.entries(n.set)) { if (!has(state, k)) e(where + ".set: " + k + " is not a state"); expr(v, where + ".set." + k); }
          }
          break;
        case "slider": case "input": case "player":
          if (n.kind === "player") {
            if (has(n, "interval_ms") && !(isInt(n.interval_ms) && n.interval_ms >= LIMITS.minInterval && n.interval_ms <= LIMITS.maxInterval)) e(where + ".interval_ms: a whole number " + LIMITS.minInterval + "–" + LIMITS.maxInterval);
            bool(n, "loop", where);
          }
          for (const f of ["min", "max", "step"]) if (has(n, f) && !isNum(n[f])) e(where + "." + f + ": must be a number");
          if (isNum(n.min) && isNum(n.max) && !(n.min < n.max)) e(where + ": min must be below max");
          if (isNum(n.step) && !(n.step > 0)) e(where + ": step must be positive");
          if (has(n, "label")) tpl(n.label, where + ".label");
          break;
        case "choice": case "select":
          if (has(n, "label")) tpl(n.label, where + ".label");
          if (!Array.isArray(n.options) || !n.options.length || n.options.length > LIMITS.options) { if (has(n, "options")) e(where + ".options: 1–" + LIMITS.options + " options"); break; }
          n.options.forEach((o, i) => { if (!isObj(o) || !has(o, "label") || !has(o, "value") || Object.keys(o).length !== 2) e(`${where}.options[${i}]: {label, value}`); else text(o.label, `${where}.options[${i}].label`, LIMITS.text); });
          break;
        case "toggle": case "checkbox":
          if (has(n, "label")) tpl(n.label, where + ".label");
          break;
        case "if":
          if (has(n, "cond")) expr(n.cond, where + ".cond");
          if (has(n, "then")) walk(n.then, where + ".then", depth + 1);
          if (has(n, "else") && n.else !== null) walk(n.else, where + ".else", depth + 1);
          break;
        case "plot": {
          for (const f of ["xmin", "xmax", "ymin", "ymax"]) if (has(n, f) && !isNum(n[f])) e(where + "." + f + ": must be a number");
          if (isNum(n.xmin) && isNum(n.xmax) && !(n.xmin < n.xmax)) e(where + ": xmin must be below xmax");
          if (isNum(n.ymin) && isNum(n.ymax) && !(n.ymin < n.ymax)) e(where + ": ymin must be below ymax");
          if (has(n, "height") && !(isNum(n.height) && n.height >= 160 && n.height <= 420)) e(where + ".height: 160–420");
          if (has(n, "legend")) {
            if (!Array.isArray(n.legend)) e(where + ".legend: must be a list");
            else n.legend.forEach((l, i) => { if (!isObj(l) || !has(l, "label") || Object.keys(l).some((k) => k !== "label" && k !== "tone")) e(`${where}.legend[${i}]: {label, tone}`); else { text(l.label, `${where}.legend[${i}].label`, LIMITS.text); oneOf(l, "tone", TONES, `${where}.legend[${i}]`); } });
          }
          if (!Array.isArray(n.items)) { if (has(n, "items")) e(where + ".items: must be a list"); break; }
          if (n.items.length > LIMITS.items) e(where + ".items: more than " + LIMITS.items);
          n.items.forEach((it, i) => {
            const w = `${where}.items[${i}]`;
            if (!isObj(it)) return e(w + ": not an item");
            const sp = has(SHAPE_FIELDS, it.shape) ? SHAPE_FIELDS[it.shape] : null;
            if (!sp) return e(w + ": unknown shape " + it.shape);
            fields(it, sp[0], sp[1], w); oneOf(it, "tone", TONES, w); oneOf(it, "anchor", ANCHORS, w); oneOf(it, "size", ["sm", "md"], w); bool(it, "dashed", w); bool(it, "fill", w);
            for (const f of sp[0]) if (has(it, f) && f !== "text") expr(it[f], w + "." + f);
            if (has(it, "text")) tpl(it.text, w + ".text");
            if (has(it, "label")) tpl(it.label, w + ".label");
          });
          break;
        }
        case "chart":
          oneOf(n, "chart", CHART_KINDS, where);
          if (has(n, "data")) { if (!isStr(n.data)) e(where + ".data: must be an expression"); else expr(n.data, where + ".data"); }
          if (has(n, "x") && !isStr(n.x)) e(where + ".x: must be a field name");
          if (has(n, "series")) { if (!Array.isArray(n.series) || !n.series.length) e(where + ".series: at least 1 series"); else if (n.chart === "pie" && n.series.length !== 1) e(where + ".series: a pie has exactly one series"); else n.series.forEach((sr, i) => { if (!isObj(sr) || !isStr(sr.key) || !isStr(sr.label) || Object.keys(sr).length !== 2) e(`${where}.series[${i}]: {key, label}`); }); }
          for (const f of ["caption", "x_label", "y_label"]) if (has(n, f)) tpl(n[f], where + "." + f);
          if (has(n, "y_min") && !isNum(n.y_min)) e(where + ".y_min: must be a number");
          for (const [f, kinds] of Object.entries(CHART_OPTION_KINDS)) if (has(n, f) && CHART_KINDS.includes(n.chart) && !kinds.includes(n.chart)) e(`${where}.${f}: only for ${kinds.join("/")} charts`);
          for (const f of ["stack", "gradient", "donut"]) bool(n, f, where);
          oneOf(n, "curve", CHART_CURVES, where);
          if (has(n, "hover")) { if (!(isStr(n.hover) && has(state, n.hover))) e(where + ".hover: " + n.hover + " is not a state"); else if (!isNum(state[n.hover])) e(where + ".hover: must be a number state"); }
          if (has(n, "visible_points") && !(isInt(n.visible_points) && n.visible_points >= LIMITS.minWindow)) e(where + ".visible_points: a whole number, at least " + LIMITS.minWindow);
          if (has(n, "annotations")) {
            if (!Array.isArray(n.annotations) || n.annotations.length > LIMITS.annotations) { e(where + ".annotations: a list of at most " + LIMITS.annotations); break; }
            n.annotations.forEach((a, i) => {
              const w = `${where}.annotations[${i}]`;
              if (!isObj(a) || Object.keys(a).some((k) => !ANNOTATION_FIELDS.includes(k)) || has(a, "x") === has(a, "y") || !has(a, "label")) return e(w + ": {x or y, label, tone}");
              const f = has(a, "x") ? "x" : "y"; expr(a[f], w + "." + f); tpl(a.label, w + ".label"); oneOf(a, "tone", TONES, w);
            });
          }
          break;
        case "reveal":
          if (Array.isArray(n.children) && !(n.children.length >= 1 && n.children.length <= LIMITS.revealSteps)) e(where + ".children: 1–" + LIMITS.revealSteps + " steps");
          oneOf(n, "mode", REVEAL_MODES, where);
          if (has(n, "label")) tpl(n.label, where + ".label");
          if (n.mode === "after") {
            if (!has(n, "after")) e(where + ".after: mode after needs the id of a practice node");
            else if (!isStr(n.after)) e(where + ".after: must be a node id");
            else revealRefs.push([n, where]);
          } else if (has(n, "after")) e(where + ".after: only with mode after");
          break;
        case "animate": oneOf(n, "effect", ANIMATE_EFFECTS, where); break;
        case "transition": if (has(n, "key")) expr(n.key, where + ".key"); break;
        case "celebrate": if (has(n, "when")) expr(n.when, where + ".when"); break;
        case "timer":
          oneOf(n, "mode", TIMER_MODES, where);
          if (n.mode === "countdown") { if (!(isInt(n.seconds) && n.seconds >= 1 && n.seconds <= LIMITS.timerSeconds)) e(where + ".seconds: a countdown needs seconds, a whole number 1–" + LIMITS.timerSeconds); }
          else if (n.mode === "stopwatch" && has(n, "seconds")) e(where + ".seconds: only for a countdown");
          if (has(n, "bind")) { if (!(isStr(n.bind) && has(state, n.bind))) e(where + ": bind " + n.bind + " is not a state"); else if (!isNum(state[n.bind])) e(where + ": bind must be a number state"); }
          bool(n, "autostart", where);
          if (has(n, "label")) tpl(n.label, where + ".label");
          break;
        case "pager":
          if (Array.isArray(n.children) && !n.children.length) e(where + ".children: at least 1 page");
          bool(n, "gate", where); pagers.push([n, where]);
          break;
        case "fill-blank": {
          if (has(n, "sentence")) { text(n.sentence, where + ".sentence", LIMITS.text); if (isStr(n.sentence) && n.sentence.split(BLANK).length - 1 !== 1) e(where + ".sentence: exactly one blank " + BLANK); }
          let nOpts = 0;
          if (!Array.isArray(n.options) || n.options.length < 2 || n.options.length > LIMITS.options) { if (has(n, "options")) e(where + ".options: 2–" + LIMITS.options + " options {label, why}"); }
          else {
            nOpts = n.options.length; const labels = new Set();
            n.options.forEach((o, i) => {
              const w = `${where}.options[${i}]`;
              if (!isObj(o)) return e(w + ": {label, why}");
              fields(o, FILL_OPTION_FIELDS[0], FILL_OPTION_FIELDS[1], w);
              for (const f of ["label", "why"]) if (has(o, f)) text(o[f], w + "." + f, LIMITS.text);
              if (isStr(o.label)) { if (labels.has(o.label.trim())) e(w + ".label: used twice"); labels.add(o.label.trim()); }
            });
          }
          if (has(n, "correct") && !(isInt(n.correct) && n.correct >= 0 && n.correct < nOpts)) e(where + ".correct: an option number");
          if (has(n, "why")) text(n.why, where + ".why", LIMITS.text);
          if (has(n, "cell") && !(isStr(n.cell) && CELL_RE.test(n.cell))) e(where + ".cell: a cell id like c1.interpret");
          break;
        }
        case "sentence": {
          let nb = 0;
          if (has(n, "sentence")) { text(n.sentence, where + ".sentence", LIMITS.text); if (isStr(n.sentence)) { nb = n.sentence.split(BLANK).length - 1; if (!(nb >= 1 && nb <= LIMITS.blanks)) e(where + ".sentence: 1–" + LIMITS.blanks + " blanks " + BLANK); } }
          let nWords = 0;
          if (!Array.isArray(n.words) || n.words.length < Math.max(1, nb) || n.words.length > LIMITS.words) { if (has(n, "words")) e(where + ".words: one word per blank or more, at most " + LIMITS.words); }
          else {
            nWords = n.words.length; const seen = new Set();
            n.words.forEach((wd, i) => { text(wd, `${where}.words[${i}]`, LIMITS.wordChars); if (isStr(wd)) { if (seen.has(wd.trim())) e(`${where}.words[${i}]: used twice`); seen.add(wd.trim()); } });
          }
          if (has(n, "answer")) {
            const a = n.answer;
            if (!(Array.isArray(a) && a.length && a.every((x) => isInt(x) && x >= 0 && x < nWords) && new Set(a).size === a.length)) e(where + ".answer: distinct word numbers, one per blank");
            else if (nb >= 1 && nb <= LIMITS.blanks && a.length !== nb) e(`${where}.answer: ${nb} blanks need ${nb} word numbers`);
          }
          if (has(n, "why")) text(n.why, where + ".why", LIMITS.text);
          if (has(n, "cell") && !(isStr(n.cell) && CELL_RE.test(n.cell))) e(where + ".cell: a cell id like c1.interpret");
          break;
        }
        case "route": {
          if (has(n, "prompt")) tpl(n.prompt, where + ".prompt");
          if (has(n, "preference") && !(isStr(n.preference) && NODE_ID.test(n.preference))) e(where + ".preference: [a-z0-9_-], 1–32");
          if (!Array.isArray(n.options) || n.options.length < LIMITS.minRouteOptions) { if (has(n, "options")) e(where + ".options: at least " + LIMITS.minRouteOptions + " options {label, value, next, desc}"); break; }
          const sk = bound ? routeValue(state[n.bind]) : null, values = new Set();
          n.options.forEach((o, i) => {
            const w = `${where}.options[${i}]`;
            if (!isObj(o)) return e(w + ": {label, value, next, desc}");
            fields(o, ROUTE_OPTION_FIELDS[0], ROUTE_OPTION_FIELDS[1], w);
            for (const f of ["label", "next", "desc"]) if (has(o, f)) text(o[f], w + "." + f, LIMITS.text);
            if (!has(o, "value")) return;
            const vk = routeValue(o.value);
            if (vk === null || (vk[0] === "s" && !(o.value.length >= 1 && o.value.length <= LIMITS.routeValue))) return e(w + ".value: text (1–" + LIMITS.routeValue + "), a number or true/false");
            if (values.has(vk)) e(w + ".value: used twice");
            values.add(vk);
            if (bound && (sk === null || sk[0] !== vk[0])) e(w + ".value: must have the type of the state " + n.bind);
          });
          break;
        }
        case "app": {
          apps.push(where); appNodes.add(n);
          if (has(n, "html")) { text(n.html, where + ".html", APP.html); if (isStr(n.html) && n.html.trim() && n.html.length <= APP.html) for (const m of appHtmlProblems(n.html)) e(where + ".html: " + m); }
          if (has(n, "height") && !(isInt(n.height) && n.height >= APP.minHeight && n.height <= APP.maxHeight)) e(where + ".height: a whole number " + APP.minHeight + "–" + APP.maxHeight);
          if (has(n, "images")) {
            if (!Array.isArray(n.images) || n.images.length > APP.images) e(where + ".images: a list of up to " + APP.images + " images");
            else {
              const seen = new Set();
              n.images.forEach((im, i) => {
                const w = `${where}.images[${i}]`;
                if (!isObj(im)) return e(w + ": an image");
                fields(im, APP.imageFields[0], APP.imageFields[1], w);
                for (const k of Object.keys(im)) text(im[k], w + "." + k, k !== "source_url" && k !== "license_url" ? APP.label : LIMITS.text);
                if (!has(im, "id")) return;
                if (!(isStr(im.id) && NODE_ID.test(im.id))) e(w + ".id: [a-z0-9_-], 1–32"); else if (seen.has(im.id)) e(w + ".id: used twice"); else seen.add(im.id);
              });
            }
          }
          if (!has(n, "items")) break;
          if (!Array.isArray(n.items) || n.items.length > APP.items) { e(where + ".items: a list of up to " + APP.items + " {id, label, cell}"); break; }
          const ids = new Set();
          n.items.forEach((it, i) => {
            const w = `${where}.items[${i}]`;
            if (!isObj(it)) return e(w + ": {id, label, cell}");
            fields(it, APP.itemFields[0], APP.itemFields[1], w);
            if (has(it, "id")) { if (!(isStr(it.id) && NODE_ID.test(it.id))) e(w + ".id: [a-z0-9_-], 1–32"); else if (ids.has(it.id)) e(w + ".id: used twice"); else ids.add(it.id); }
            if (has(it, "label")) text(it.label, w + ".label", APP.label);
            if (has(it, "cell") && !(isStr(it.cell) && CELL_RE.test(it.cell))) e(w + ".cell: a cell id like c1.interpret");
          });
          break;
        }
        case "timeline":
          if (!Array.isArray(n.events) || n.events.length < MIN_EVENTS) { if (has(n, "events")) e(where + ".events: at least " + MIN_EVENTS + " events {title, when, text, tone}"); break; }
          n.events.forEach((x, i) => {
            const w = `${where}.events[${i}]`;
            if (!isObj(x)) return e(w + ": {title, when, text, tone}");
            fields(x, TIMELINE_EVENT_FIELDS[0], TIMELINE_EVENT_FIELDS[1], w);
            for (const f of ["title", "when", "text"]) if (has(x, f)) text(x[f], w + "." + f, LIMITS.text);
            oneOf(x, "tone", TONES, w);
          });
          break;
        case "deep-dive":
          if (Array.isArray(n.children) && !n.children.length) e(where + ".children: at least 1 node");
          if (has(n, "label")) text(n.label, where + ".label", LIMITS.text);
          break;
        case "flashcards": {
          bool(n, "shuffle", where);
          if (has(n, "title")) text(n.title, where + ".title", LIMITS.text);
          if (!Array.isArray(n.cards) || n.cards.length < LIMITS.minCards) { if (has(n, "cards")) e(where + ".cards: at least " + LIMITS.minCards + " cards"); break; }
          n.cards.forEach((c, i) => {
            const w = `${where}.cards[${i}]`;
            if (!isObj(c)) return e(w + ": not a card");
            fields(c, CARD_FIELDS[0], CARD_FIELDS[1], w);
            for (const f of ["front", "back", "hint"]) if (has(c, f)) text(c[f], w + "." + f, LIMITS.text);
            if (has(c, "cell") && !(isStr(c.cell) && CELL_RE.test(c.cell))) e(w + ".cell: a cell id like c1.interpret");
          });
          break;
        }
        case "quiz": {
          if (!Array.isArray(n.questions) || !n.questions.length) { if (has(n, "questions")) e(where + ".questions: at least 1 question"); break; }
          const cases = quizCases(n, where);
          oneOf(n, "layout", QUIZ_LAYOUTS, where);
          if (has(n, "title")) text(n.title, where + ".title", LIMITS.text);
          n.questions.forEach((q, i) => {
            const w = `${where}.q${i}`;
            if (!isObj(q)) return e(w + ": not a question");
            fields(q, QUESTION_FIELDS[0], QUESTION_FIELDS[1], w);
            oneOf(q, "type", QUESTION_TYPES, w);
            if (has(q, "hint")) text(q.hint, w + ".hint", LIMITS.text);
            if (cases !== null && !cases.has(q.case)) e(w + ".case: must be one of the quiz's cases");
            if (cases === null && has(q, "case")) e(w + ".case: the quiz has no cases");
            if (has(q, "cell") && !(isStr(q.cell) && CELL_RE.test(q.cell))) e(w + ".cell: a cell id like c1.interpret");
            if (has(q, "q")) text(q.q, w + ".q", LIMITS.text);
            if (has(q, "why")) text(q.why, w + ".why", LIMITS.text);
            if (!Array.isArray(q.options) || q.options.length < 2 || q.options.length > LIMITS.options || !q.options.every(isStr)) e(w + ".options: 2–" + LIMITS.options + " texts");
            const nOpts = Array.isArray(q.options) ? q.options.length : 0;
            if (q.type === "tf" && nOpts !== 2) e(w + ".options: a true/false question has exactly 2 options");
            if (q.type === "multi") { const c = q.correct; if (!(Array.isArray(c) && c.length && c.every((x) => isInt(x) && x >= 0 && x < nOpts) && new Set(c).size === c.length)) e(w + ".correct: a multi question's correct is a list of distinct option numbers"); }
            else if (!(isInt(q.correct) && q.correct >= 0 && q.correct < nOpts)) e(w + ": correct out of range");
            if (has(q, "feedback")) { if (!Array.isArray(q.feedback) || q.feedback.length !== nOpts) e(w + ".feedback: one text per option"); else q.feedback.forEach((t, j) => text(t, `${w}.feedback[${j}]`, LIMITS.text)); }
          });
          break;
        }
      }
    };
    function quizCases(n, where) {
      if (!has(n, "cases")) return null;
      const ids = new Set();
      if (!Array.isArray(n.cases) || !n.cases.length || n.cases.length > LIMITS.cases) { e(where + ".cases: 1–" + LIMITS.cases + " cases"); return ids; }
      n.cases.forEach((c, i) => {
        const w = `${where}.cases[${i}]`;
        if (!isObj(c) || Object.keys(c).some((k) => !["id", "label", "advice"].includes(k)) || !has(c, "id") || !has(c, "label")) return e(w + ": {id, label, advice}");
        if (!(isStr(c.id) && CASE_ID.test(c.id))) e(w + ".id: [a-z0-9_-], 1–32");
        else if (ids.has(c.id)) e(w + ".id: used twice");
        else ids.add(c.id);
        text(c.label, w + ".label", LIMITS.text);
        if (has(c, "advice")) text(c.advice, w + ".advice", LIMITS.text);
      });
      return ids;
    }
    function table(n, where) {
      let cols = n.columns;
      if (has(n, "columns")) {
        if (!Array.isArray(cols) || !cols.length) { e(where + ".columns: at least 1 text"); cols = null; }
        else cols.forEach((c, i) => text(c, `${where}.columns[${i}]`, LIMITS.text));
      }
      if (has(n, "caption")) tpl(n.caption, where + ".caption");
      if (!has(n, "rows")) return;
      if (isStr(n.rows)) return expr(n.rows, where + ".rows");
      if (!Array.isArray(n.rows)) return e(where + ".rows: an expression or a list of rows");
      n.rows.forEach((r, i) => {
        if (!Array.isArray(r) || (Array.isArray(cols) && r.length !== cols.length)) return e(`${where}.rows[${i}]: one cell per column`);
        r.forEach((c, j) => tpl(c, `${where}.rows[${i}][${j}]`));
      });
    }
    const inside = (n, out) => { if (!isObj(n)) return out; for (const c of Array.isArray(n.children) ? n.children : []) { out.push(c); inside(c, out); }
      for (const f of ["then", "else"]) if (isObj(n[f])) { out.push(n[f]); inside(n[f], out); } return out; };
    if (has(block, "body")) walk(block.body, "body", 1);
    for (const [rv, where] of revealRefs) {               // after the walk: a target may come later in the block
      const t = idNodes.get(rv.after);
      if (!t) e(where + ".after: no node has the id " + rv.after);
      else if (!PRACTICE.includes(t.kind)) e(`${where}.after: ${rv.after} is a ${t.kind}, not a practice node (${PRACTICE.join(" / ")})`);
      else if (inside(rv, []).includes(t)) e(`${where}.after: ${rv.after} is inside this reveal (it would never open)`);
    }
    for (const [pg, where] of pagers) if (inside(pg, []).some((x) => isObj(x) && x.kind === "pager")) e(where + ": a pager cannot hold another pager");
    for (const where of apps.slice(1)) e(where + ": at most one app per block");
    if (JSON.stringify(block, withoutAppHtml(appNodes)).length > LIMITS.block) e("block larger than " + LIMITS.block + " characters");
    if (has(block, "probes")) {
      if (!Array.isArray(block.probes) || block.probes.length > LIMITS.probes) e("probes: a list of up to " + LIMITS.probes);
      else block.probes.forEach((p, i) => { if (!isObj(p) || !isObj(p.set) || !Object.keys(p.set).length || Object.keys(p).length !== 1) return e(`probes[${i}]: {set: {state: value}}`); for (const k of Object.keys(p.set)) if (!has(state, k)) e(`probes[${i}]: ${k} is not a state`); });
    }
    return errs;
  }

  /* ---------- render ---------- */
  const SVGNS = "http://www.w3.org/2000/svg";
  /* render(block, mount, opts): builds into mount's own document (works in popout windows).
     opts.onQuizResult({quiz, question, chosen, correct, case, cell}) is called once per question
     each time 「检查答案」 is pressed (send them to POST /wiki/assets/{id}/results); `quiz` numbers
     the block's quiz nodes in document order from 0 (`if` branches included), `question` the
     question within that quiz. onQuizResult and results().answers carry single-answer questions
     (single / tf) only: a multi-select answer is a list and reaches the host only as an item event
     (opts.onEvent) — hosts must forward item events to record multi-select answers.
     opts.renderMath(tex, displayMode) -> Element: the host's LaTeX renderer (Obsidian: renderMath,
     web: KaTeX); without it a formula shows as its LaTeX source.
     opts.image(image) -> Promise<{mime, bytes} | null>: the bytes of one of an app's declared images (image: its entry in
     the node's `images`); without it `evowiki.image` rejects.
     opts.blockId: the host's id for this block (copied into every event's `block`; null without it).
     Every event also carries `session`: 16 random hex chars made once per render (the server's
     duplicate key includes it, so a new render whose attempt / revision start over is not a resend).
     opts.progress: a progress object from an earlier session to restore (see below); restoring never
     fires events, onProgress or confetti.
     opts.onProgress(progress): called with the current progress after changes (debounced 250 ms).
     opts.immediateProgress: report each committed change synchronously (a removable frame must hand it off before teardown).
     opts.scheduleUpdate(update): optionally coalesce painting; state and progress still change synchronously.
     opts.onEvent(ev): called with each result event as it happens (item / summary / route / request).
     The returned handle is {state, set, results(), progress(), events(), ack(n), flush(), rejected(), destroy()}:
       results() = {state, answers: [{quiz, question, chosen}]} for everything answered this session;
       progress() = a copy of the progress object {v: 2, revision, state, nodes: {<node key>: …}};
       events()   = a copy of the unsent event log (every event onEvent saw, oldest first);
       ack(n)     drops the first n events of that log once the host has delivered them;
       flush()    calls onProgress now if a change is still waiting for its debounce; returns progress().
       rejected() = how many app bridge messages were dropped (see app() below; each also console.warn).
       destroy()  for a host unmounting the block: stops every timer, player, resize observer, flashcard
                  tip and app bridge (message listener, theme watchers) at once, sends a pending progress
                  (onProgress, as flush()) and then goes quiet — later changes, events and onProgress are
                  dropped; an app frame that loads again shows nothing new. Returns progress(); idempotent.

     Restoring opts.progress ({v: 2, revision, state, nodes}): anything else is ignored with a warning;
     the revision continues from the restored one; a state key the block does not have is ignored, one
     whose JSON type differs keeps the block's value (warning); node progress for a key the block does
     not have is dropped, the rest waits until its node is built and asks ctx.nodeProgress (whose check
     discards a misfit for that node only). Progress of a node not built yet (an `if` branch not shown)
     is carried over unchanged in progress().

     Node keys: every stateful node (STATEFUL) is filed under its `id`, else `<kind>#<n>` with n its
     index among the block's nodes of that kind in document order (children, then `then`, then `else`;
     `if` branches count whether shown or not) — deep.evoui.node_keys(block) in Python, same numbering.

     ctx — the internal API a stateful node uses (built once per render, shared by all nodes):
       ctx.nodeKey(n)                     the node's key (above).
       ctx.nodeProgress(key, init, check) the node's live progress object: the one already in use (a
                                          rebuilt `if` branch gets its old progress back), else the
                                          restored one if check(restored) is true (a false or throwing
                                          check discards it for this node only, console.warn), else
                                          init(). Mutate the returned object in place, then ctx.change().
                                          It must stay plain JSON.
       ctx.change()                       after any change of state or node progress: revision += 1 and
                                          onProgress is scheduled (debounced 250 ms).
       ctx.change({lazy: true})           the same for a change made by a clock tick (timer, player):
                                          onProgress at most once a second while ticks go on.
       ctx.session                        this render's random id (16 hex chars), on every event.
       ctx.emit(ev)                       a result event: `type` first, then `block` (opts.blockId|null),
                                          `session` (ctx.session), the event's own fields and `revision` (the current one — call
                                          change() first). Appended to the unsent log; opts.onEvent is
                                          called in try/catch. Returns the event as sent. A summary sent
                                          while a timer node runs also carries `timerMs` (its elapsed ms).
       ctx.clocks                         the timer nodes ({alive(), running(), elapsed()}) for timerMs.
       ctx.pace                           ms between a playing player's steps (null when none plays):
                                          plot / chart / table moves shorten to fit.
       ctx.now()                          milliseconds (monotonic) for latencyMs / timeMs.
       ctx.life(el)                       lifeOf(el, ctx.now) that is also true once the render is destroyed.
       ctx.onDestroy(fn)                  run fn at handle.destroy() (stop intervals, observers, listeners);
                                          returns an unsubscribe. ctx.destroyed() says whether it happened.
       ctx.isDone(key)                    whether that node has reached done.
       ctx.markDone(key, {restored})      the node reached done (sticky; later calls are no-ops). Calls
                                          the listeners with (key, {restored}) — `restored` is true when
                                          the done state came from opts.progress (no events, no confetti).
       ctx.onDone(key, fn)                listen for markDone(key); returns an unsubscribe function. A
                                          listener living inside an `if` branch should unsubscribe (or
                                          ignore calls) once its element is no longer connected.
                                          key "*": every node's markDone (a gated pager listens so).
       ctx.peek(key)                      a node's live progress, else its restored one not yet taken
                                          (unchecked: check it before use), else null. Read only.
     Item / summary events (D3): item {type, block, session, node, kind, item, attempt, revision, response,
     correct, hintUsed, latencyMs}; summary {type, block, session, node, kind, attempt, revision, total, right,
     missed, timeMs (first item shown -> done), hintsUsed[, timerMs]}, emitted once per attempt when
     the node reaches done.
     Motion helpers for nodes (module level, see "motion"): enterEl, leaveEl, swapIn, flipFirst /
     flipLast, tweenSvg, lifeOf; PRACTICE lists the kinds a reveal can wait for (and a gated pager
     waits for); their roots carry data-practice = the node key.
     Route events (D3): {type: "route", block, session, node, option (the chosen value), next, preference
     (the node's `preference` or null), revision}. */
  const ICON_PATHS = {
    "arrow-down": "M8 2v10M3.5 7.5 8 12l4.5-4.5", "arrow-up": "M8 14V4M3.5 8.5 8 4l4.5 4.5",
    "arrow-left": "M14 8H4M8.5 3.5 4 8l4.5 4.5", "arrow-right": "M2 8h10M7.5 3.5 12 8l-4.5 4.5",
    check: "M2.5 8.5 6.5 12.5 13.5 4", cross: "M3.5 3.5l9 9M12.5 3.5l-9 9", dot: "M8 6.2a1.8 1.8 0 1 0 0 3.6a1.8 1.8 0 1 0 0-3.6Z",
    plus: "M8 3v10M3 8h10", minus: "M3 8h10", info: "M8 1.8a6.2 6.2 0 1 0 0 12.4A6.2 6.2 0 1 0 8 1.8ZM8 7v4.5M8 4.6v.4",
    warning: "M8 1.8 14.6 13.6H1.4ZM8 6v3.6M8 11.4v.4" };
  const SERIES_TONES = ["t-accent", "t-warning", "t-success", "t-danger"];

  /* ---------- motion (EvoUI v2): enter 180 ms + 4 px rise, exit 120 ms, layout move 200 ms ----------
     Shared by `if` (every branch switch), transition, animate, reveal, plot / chart / table updates —
     and meant for Task 5b's pager. Content is always in the DOM at once; motion is only visual. Under
     prefers-reduced-motion (or without the browser APIs) nothing moves: an instant swap. */
  const ENTER_MS = 180, EXIT_MS = 120, MOVE_MS = 200, ENTER_DELAY_MS = 60;
  const EASE = "cubic-bezier(.2,.7,.2,1)";
  function calmOf(doc) {
    const win = doc && doc.defaultView;
    try { return !!(win && win.matchMedia && win.matchMedia("(prefers-reduced-motion: reduce)").matches); } catch (x) { return false; }
  }
  /* enterEl(el, effect, delay): el fades in over 180 ms (effect "rise", the default: with a 4 px rise;
     "fade": opacity only), starting `delay` ms later (invisible until then). Replays when called again. */
  function enterEl(el, effect, delay) {
    if (!el || el.nodeType !== 1 || calmOf(el.ownerDocument)) return;
    const cls = effect === "fade" ? "evoui-fadein" : "evoui-enter";
    el.classList.remove("evoui-enter", "evoui-fadein"); if (el.isConnected) void el.getBoundingClientRect();
    el.classList.add(cls); el.style.animationDelay = delay ? delay + "ms" : "";
    const done = (ev) => { if (ev.target !== el) return; el.classList.remove(cls); el.style.animationDelay = ""; el.removeEventListener("animationend", done); el.removeEventListener("animationcancel", done); };
    el.addEventListener("animationend", done); el.addEventListener("animationcancel", done);
  }
  /* leaveEl(el, rect): el (still where it was; rect = its box measured before the change) is taken out
     of the flow at that spot and fades out in 120 ms, then removed. While it leaves it is aria-hidden,
     inert, pointer-events none and marked `data-leaving` (the render check's text skips it; timers and
     players inside it stop); names / ids inside are stripped so its radios never join a live group.
     Removed at once (returns false) under reduced motion, when detached or when it has no size. */
  /* unnamed(el): el and everything in it lose name / id / form (a ghost's radios never join a live group).
     Call it on a clone BEFORE inserting it: an inserted checked radio would uncheck its live twin. */
  function unnamed(el) {
    for (const x of [el, ...el.querySelectorAll("[name],[id],[form]")]) { x.removeAttribute("name"); x.removeAttribute("id"); x.removeAttribute("form"); }
    return el;
  }
  /* ghostOf(el): the leaving copy of a live element — a deep clone, unnamed, whose iframes (an app's
     sandbox) are swapped for empty boxes of the same size BEFORE it is inserted: a cloned srcdoc frame
     would boot a second copy of the app on the same bridge channel (rejected messages, render check
     failures). */
  function ghostOf(el) {
    const c = el.cloneNode(true), src = [...el.querySelectorAll("iframe")];
    [...c.querySelectorAll("iframe")].forEach((f, i) => {
      const o = src[i], r = o ? o.getBoundingClientRect() : null, ph = el.ownerDocument.createElement("div");
      ph.className = "evoui-ghost-frame"; ph.setAttribute("aria-hidden", "true");
      ph.style.width = (r && r.width ? r.width + "px" : "100%"); ph.style.height = (r && r.height ? r.height + "px" : (o && o.style.height) || "0px");
      f.replaceWith(ph);
    });
    return unnamed(c);
  }
  function leaveEl(el, rect) {
    if (!el || el.nodeType !== 1) return false;
    const gone = () => { el.remove(); return false; };
    if (calmOf(el.ownerDocument) || !el.isConnected || !rect || !(rect.width > 0 && rect.height > 0)) return gone();
    el.setAttribute("data-leaving", ""); el.setAttribute("aria-hidden", "true"); el.inert = true; el.classList.add("evoui-leaving");
    el.classList.remove("evoui-enter", "evoui-fadein");
    unnamed(el);
    Object.assign(el.style, { position: "absolute", margin: "0", left: "0px", top: "0px", width: rect.width + "px", height: rect.height + "px", animationDelay: "" });
    const op = el.offsetParent; if (!op) return gone();
    const pr = op.getBoundingClientRect();
    el.style.left = (rect.left - pr.left - op.clientLeft + op.scrollLeft) + "px";
    el.style.top = (rect.top - pr.top - op.clientTop + op.scrollTop) + "px";
    setTimeout(() => el.remove(), EXIT_MS);
    return true;
  }
  const leaving = (el) => !!(el && el.closest && el.closest("[data-leaving]"));
  /* swapIn(slot, next, {instant, effect}): the slot's content becomes `next` (an element, or null for
     nothing). The old content leaves (120 ms) and next enters (180 ms + 4 px rise, 60 ms in) — unless
     `instant` (first render) or reduced motion. Ghosts of an earlier swap are dropped at once. */
  function swapIn(slot, next, o = {}) {
    for (const g of [...slot.children]) if (g.hasAttribute("data-leaving")) g.remove();
    const old = [...slot.childNodes], moving = !o.instant && !calmOf(slot.ownerDocument);
    const rects = old.map((c) => (moving && c.nodeType === 1 ? c.getBoundingClientRect() : null));
    let left = 0;
    old.forEach((c, i) => { if (rects[i] && leaveEl(c, rects[i])) left += 1; else c.remove(); });
    if (next) { slot.insertBefore(next, slot.firstChild); if (moving) enterEl(next, o.effect, left ? ENTER_DELAY_MS : 0); }
  }
  /* FLIP layout move (200 ms): flipFirst(els, keyOf) measures before a change; flipLast(els, keyOf,
     before, ms) slides every element whose key was there before from its old place to its new one. */
  function flipFirst(els, keyOf) {
    const m = new Map(), seen = new Map();
    for (const el of els) { const k0 = keyOf(el), n = (seen.get(k0) || 0) + 1; seen.set(k0, n); m.set(k0 + "#" + n, el.getBoundingClientRect()); }
    return m;
  }
  function flipLast(els, keyOf, before, ms = MOVE_MS) {
    if (!els.length || !before.size || calmOf(els[0].ownerDocument)) return;
    const seen = new Map();
    for (const el of els) {
      const k0 = keyOf(el), n = (seen.get(k0) || 0) + 1; seen.set(k0, n);
      const r0 = before.get(k0 + "#" + n); if (!r0 || typeof el.animate !== "function") continue;
      const r1 = el.getBoundingClientRect(), dx = r0.left - r1.left, dy = r0.top - r1.top;
      if (Math.abs(dx) < 0.5 && Math.abs(dy) < 0.5) continue;
      try { el.animate([{ transform: `translate(${dx}px, ${dy}px)` }, { transform: "none" }], { duration: ms, easing: EASE }); } catch (x) { /* no WAAPI: no move */ }
    }
  }
  /* tweenSvg(from, to, ms): a re-drawn plot / chart moves instead of jumping. When the new svg has the
     same element structure as the old one, every geometry attribute (x y width height cx cy r x1 y1 x2
     y2, polyline points, arrow-head paths) glides from its value on screen to the new one. Text changes
     at once. Returns a cancel function (or null when nothing moves). */
  const TWEEN_ATTRS = ["x", "y", "width", "height", "cx", "cy", "r", "x1", "y1", "x2", "y2"];
  const NUM_RE = /-?\d*\.?\d+(?:e[-+]?\d+)?/gi;
  function tweenSvg(from, to, ms) {
    const doc = to.ownerDocument, win = doc && doc.defaultView;
    if (!(ms > 0) || !from || calmOf(doc) || !win || typeof win.requestAnimationFrame !== "function") return null;
    const a = from.children, b = to.children; if (a.length !== b.length || !b.length) return null;
    for (let i = 0; i < b.length; i++) if (a[i].tagName !== b[i].tagName) return null;
    const jobs = [];
    for (let i = 0; i < b.length; i++) {
      const o = a[i], nu = b[i];
      for (const at of TWEEN_ATTRS) {
        if (!o.hasAttribute(at) || !nu.hasAttribute(at)) continue;
        const x0 = parseFloat(o.getAttribute(at)), x1 = parseFloat(nu.getAttribute(at));
        if (isFinite(x0) && isFinite(x1) && Math.abs(x1 - x0) > 0.01) { jobs.push({ el: nu, at, x0: [x0], x1: [x1], skel: null }); nu.setAttribute(at, String(x0)); }
      }
      for (const at of ["points", "d"]) {
        if (!o.hasAttribute(at) || !nu.hasAttribute(at)) continue;
        const s0 = o.getAttribute(at), s1 = nu.getAttribute(at);
        if (s0 === s1 || (at === "d" && /[Aa]/.test(s1))) continue;        // arcs (pie slices) jump
        const skel = s1.replace(NUM_RE, "#"); if (s0.replace(NUM_RE, "#") !== skel) continue;
        jobs.push({ el: nu, at, x0: s0.match(NUM_RE).map(Number), x1: s1.match(NUM_RE).map(Number), skel }); nu.setAttribute(at, s0);
      }
    }
    if (!jobs.length) return null;
    const t0 = win.performance.now(); let raf = 0, live = true;
    const paint = (k) => { for (const j of jobs) {
      const v = j.x0.map((x, i) => x + (j.x1[i] - x) * k);
      if (!j.skel) j.el.setAttribute(j.at, String(v[0])); else { let i = 0; j.el.setAttribute(j.at, j.skel.replace(/#/g, () => String(Math.round(v[i++] * 100) / 100))); }
    } };
    const frame = (now) => { if (!live) return; const t = Math.max(0, Math.min(1, (now - t0) / ms)), k = 1 - Math.pow(1 - t, 3);   /* a frame stamp may predate t0 */ paint(t >= 1 ? 1 : k); if (t < 1 && to.isConnected) raf = win.requestAnimationFrame(frame); else paint(1); };
    raf = win.requestAnimationFrame(frame);
    return () => { if (!live) return; live = false; win.cancelAnimationFrame(raf); paint(1); };
  }
  /* lifeOf(el, now): () -> true once el is gone — it was in the document and is no longer (or is a
     leaving ghost), or it never got there within 10 s. Interval-driven nodes (timer, player) stop then. */
  function lifeOf(el, now) {
    const born = now(); let seen = false;
    return () => { if (el.isConnected) { seen = true; return leaving(el); } return seen || now() - born > 10000; };
  }

  /* ---------- chart geometry (EvoUI v2): nice y ticks, curves, bar shapes ----------
     All in pixel space (the viewBox is the box's width in px). Path strings keep the same command letters
     for the same number of points, so tweenSvg can glide them (arcs never glide). */
  let GRAD_ID = 0;                               // gradient ids, unique in the document (evoui-g<n>-…)
  /* niceScale(lo, hi, fixedLo): a domain on round numbers (lo kept as given when fixedLo) and its 4–6
     ticks (multiples of a 1 / 2 / 2.5 / 5 × 10^k step inside the domain), with the decimals they need. */
  function niceScale(lo, hi, fixedLo) {
    if (!(hi > lo)) hi = lo + 1;
    const raw = (hi - lo) / 4, mag = Math.pow(10, Math.floor(Math.log10(raw))), f = raw / mag;
    const m = f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10, step = m * mag;
    const a = fixedLo ? lo : Math.floor(lo / step + 1e-9) * step, b = Math.ceil(hi / step - 1e-9) * step;
    const dec = Math.max(0, -Math.floor(Math.log10(step) + 1e-9) + (m === 2.5 ? 1 : 0)), ticks = [];
    for (let k = Math.ceil(a / step - 1e-9); k * step <= b + step * 1e-9 && ticks.length < 12; k++) ticks.push(Number((k * step).toFixed(dec + 2)));
    return { lo: a, hi: b > a ? b : a + step, ticks, dec };
  }
  /* numText(v, dec): a tick / value label with thousands separators ("12,500", "0.25"). */
  const numText = (v, dec) => (Math.abs(v) < 1e-12 ? 0 : v).toLocaleString("en-US", dec == null ? { maximumFractionDigits: 2 } : { minimumFractionDigits: dec, maximumFractionDigits: dec });
  /* textW(s, px): estimated width of a label at font size px (CJK ≈ 1 em, the rest ≈ 0.6 em). */
  const textW = (s, px = 11) => { let w = 0; for (const ch of String(s)) w += ch.codePointAt(0) > 0x2e80 ? px : px * 0.6; return w; };
  const r2 = (x) => Math.round(x * 100) / 100, pxy = (p) => r2(p[0]) + "," + r2(p[1]);
  /* curveSegs(P, curve): the segments through pixel points P. "smooth" is a monotone cubic in x (the
     Fritsch–Carlson tangents: it never overshoots the data), "step" is step-after, else straight lines. */
  function curveSegs(P, curve) {
    const n = P.length, segs = [];
    if (curve === "smooth" && n > 2) {
      const s = [], m = new Array(n);
      for (let i = 0; i < n - 1; i++) { const hx = P[i + 1][0] - P[i][0]; s.push(hx ? (P[i + 1][1] - P[i][1]) / hx : 0); }
      for (let i = 1; i < n - 1; i++) {
        const h0 = P[i][0] - P[i - 1][0], h1 = P[i + 1][0] - P[i][0], p = (s[i - 1] * h1 + s[i] * h0) / ((h0 + h1) || 1);
        m[i] = (Math.sign(s[i - 1]) + Math.sign(s[i])) * Math.min(Math.abs(s[i - 1]), Math.abs(s[i]), 0.5 * Math.abs(p)) || 0;
      }
      m[0] = (3 * s[0] - m[1]) / 2; m[n - 1] = (3 * s[n - 2] - m[n - 2]) / 2;
      if (Math.sign(m[0]) !== Math.sign(s[0])) m[0] = 0;
      if (Math.sign(m[n - 1]) !== Math.sign(s[n - 2])) m[n - 1] = 0;
      for (let i = 0; i < n - 1; i++) { const a = P[i], b = P[i + 1], d = (b[0] - a[0]) / 3; segs.push({ a, b, c1: [a[0] + d, a[1] + d * m[i]], c2: [b[0] - d, b[1] - d * m[i + 1]] }); }
      return segs;
    }
    for (let i = 0; i < n - 1; i++) segs.push({ a: P[i], b: P[i + 1], step: curve === "step" });
    return segs;
  }
  /* curvePath(P, curve): "M…" through P; curveBack(P, curve): the same curve walked backwards ("L…"),
     to close an area onto the series below it. A single point is a zero-length line. */
  function curvePath(P, curve) {
    if (P.length < 2) return "M" + pxy(P[0]) + "L" + pxy(P[0]);
    let d = "M" + pxy(P[0]);
    for (const s of curveSegs(P, curve)) d += s.c1 ? "C" + pxy(s.c1) + " " + pxy(s.c2) + " " + pxy(s.b) : s.step ? "L" + pxy([s.b[0], s.a[1]]) + "L" + pxy(s.b) : "L" + pxy(s.b);
    return d;
  }
  function curveBack(P, curve) {
    if (P.length < 2) return "L" + pxy(P[0]) + "L" + pxy(P[0]);
    const segs = curveSegs(P, curve); let d = "L" + pxy(P[P.length - 1]);
    for (let i = segs.length - 1; i >= 0; i--) { const s = segs[i]; d += s.c1 ? "C" + pxy(s.c2) + " " + pxy(s.c1) + " " + pxy(s.a) : s.step ? "L" + pxy([s.b[0], s.a[1]]) + "L" + pxy(s.a) : "L" + pxy(s.a); }
    return d;
  }
  /* barPath(x, y, w, h, r): a bar whose top edge is at y (h ≥ 0 down to its base), top corners rounded r. */
  function barPath(x, y, w, h, r) {
    r = Math.max(0, Math.min(r, w / 2, h));
    return "M" + pxy([x, y + h]) + "L" + pxy([x, y + r]) + "Q" + pxy([x, y]) + " " + pxy([x + r, y]) + "L" + pxy([x + w - r, y]) +
      "Q" + pxy([x + w, y]) + " " + pxy([x + w, y + r]) + "L" + pxy([x + w, y + h]) + "Z";
  }

  function render(block, mount, opts = {}) {
    const doc = mount.ownerDocument || document;
    // Only source-defined system labels use this catalog. Model/user text stays as supplied.
    let language = opts.language === "en" ? "en" : "zh";
    const labels = {"深入一点": "Go deeper", "图": "Image", "拖动查看更多": "Drag to see more", "图表：用 ← → 逐个查看数据点": "Chart: use ← → to inspect data points", "，Shift + 滚轮或拖动左右移动": "; Shift + scroll or drag to pan", "点一下隐藏这条": "Click to hide this series", "点一下显示这条": "Click to show this series", "合计": "Total", "检查答案": "Check answer", "对。": "Correct.", "不对。": "Incorrect.", "收起提示": "Hide hint", "提示": "Hint", "答对 {0} / {1}": "Correct: {0} / {1}", "测验": "Quiz", "答对了": "Correct", "答错了": "Incorrect", "上一题": "Previous question", "查看结果": "View results", "下一题": "Next question", "选出所有适用项": "Select all that apply", "提交答案": "Submit answer", "测验结果": "Quiz results", "第 {0} 轮": "Round {0}", "全部答对": "All correct", "这一轮没有答对的题": "No correct answers this round", "答对 {0} 题，错 {1} 题": "{0} correct, {1} incorrect", "答对": "Correct", "答错": "Incorrect", "正确答案：{0}": "Correct answer: {0}", "你选了 {0}{1}。{2}": "You selected {0}{1}. {2}", "，漏了 {0}": "; missed {0}", "重新开始": "Restart", "只做错题": "Retry incorrect answers", "已请求新的一套": "Requested a new set", "再来一套": "Another set", "点击翻面": "Click to flip", "左滑没记住": "Swipe left: not remembered", "右滑记住": "Swipe right: remembered", "键盘：空格 ← → z": "Keyboard: Space ← → Z", "闪卡": "Flashcards", "没记住": "Not remembered", "记住了": "Remembered", "撤销（z）": "Undo (Z)", "洗牌": "Shuffle", "记住了（最后一张）": "Remembered (last card)", "闪卡 {0}/{1}，{2}：{3}。空格翻面，← 没记住，→ 记住": "Flashcard {0}/{1}, {2}: {3}. Space to flip, ← not remembered, → remembered", "背面": "Back", "正面": "Front", "背面：": "Back: ", "已撤销": "Undone", "已洗牌": "Shuffled", " · 复习没记住的": " · Review missed cards", "全部记住了": "All remembered", "还不太熟，再练一轮": "Keep practicing with another round", "记住了 {0}%": "Remembered {0}%", "{0} {1} 张": "{0}: {1} cards", "复习 {0} 张没记住的": "Review {0} missed cards", "已请求更多卡片": "Requested more cards", "更多卡片": "More cards", "再遮住": "Hide again", "已遮住": "Hidden", "完成上面的练习后显示": "Complete the practice above to reveal", "{0} ({1}/{2})": "{0} ({1}/{2})", "下一步": "Next step", "显示": "Show", "已全部显示": "All steps revealed", "已显示第 {0} 步，共 {1} 步": "Revealed step {0} of {1}", "开始": "Start", "重置": "Reset", "时间到": "Time is up", "暂停": "Pause", "继续": "Resume", "倒计时": "Countdown", "秒表": "Stopwatch", "回到开头": "Back to start", "上一步": "Previous step", "播放": "Play", "速度": "Speed", "第 {0} 步，共 {1} 步": "Step {0} of {1}", "进度": "Progress", "从头播放": "Play from start", "播放器": "Player", "不对": "Incorrect", "正确答案": "Correct answer", "再试一次": "Try again", "填空": "Fill in the blank", "选项": "Options", "不对，正确答案是": "Incorrect. The correct answer is ", "空": "Blank", "对：": "Correct: ", "错：": "Incorrect: ", "拼句": "Build a sentence", "撤销上一个词": "Undo last word", "词库": "Word bank", "不对，正确的句子是：": "Incorrect. The correct sentence is: ", "第 {0} 个空：{1}": "Blank {0}: {1}", "撤销了「": "Undid “", "第 {0} 个空": "Blank {0}", "练习": "Practice", "上一页": "Previous page", "下一页": "Next page", "做完这一页的练习再继续": "Complete this page’s practice to continue", "第 {0} 页，共 {1} 页": "Page {0} of {1}", "完成：答对 {0} / {1}": "Finished: {0} / {1} correct", "完成": "Finish", "已完成": "Completed", "重新载入": "Reload", "小程序没有加载出来，这是文字版：": "The app could not load. Text version: ", "已选：": "Selected: ", "交互出错，改显示文字：": "The interaction failed. Text version: "};
    const managedFrames = new Set();
    const bindings = new Map(), UI_TEXT = Symbol("system-label");
    const uiValue = (read) => ({ [UI_TEXT]: true, toString: () => String(read()), valueOf() { return this.toString(); } });
    const ui = (key, values = []) => ({ [UI_TEXT]: true, toString: () => (language === "en" ? labels[key] ?? key : key).replace(/\{(\d+)\}/g, (_, i) => String(values[Number(i)])), valueOf() { return this.toString(); } });
    const resolveUI = (value) => { while (typeof value === "function") value = value(); return value; };
    const bindUI = (el, key, value, apply) => {
      const raw = resolveUI(value); let props = bindings.get(el);
      const paint = () => { const text = resolveUI(value); apply(text == null ? "" : String(text)); };
      if (raw && raw[UI_TEXT]) { if (!props) { props = new Map(); bindings.set(el, props); } props.set(key, paint); }
      else if (props) { props.delete(key); if (!props.size) bindings.delete(el); }
      paint();
    };
    const uiProp = (el, key, value) => { bindUI(el, "prop:" + key, value, (text) => { el[key] = text; }); return el[key]; };
    const uiAttr = (el, key, value) => bindUI(el, "attr:" + key, value, (text) => el.setAttribute(key, text));
    const uiNode = (value) => { const el = doc.createTextNode(""); uiProp(el, "textContent", value); return el; };
    const setLanguage = (value) => {
      const next = value === "en" ? "en" : "zh"; if (next === language || ctx.destroyed()) return;
      language = next; root.lang = language;
      for (const frame of managedFrames) { if (!frame.isConnected) { managedFrames.delete(frame); continue; } frame.contentWindow.postMessage({t: "locale", locale: language}, "*"); }
      for (const [el, props] of bindings) { if (!root.contains(el) && el !== root) { bindings.delete(el); continue; } for (const apply of props.values()) apply(); }
    };

    const h = (tag, cls, txt) => { const el = doc.createElement(tag); if (cls) el.className = cls; if (txt != null) uiProp(el, "textContent", () => txt); return el; };
    const errs = validate(block); if (errs.length) throw new EvoUIError(errs.join("\n"));
    const QUIZ_NO = new Map(), KEYS = keyMap(block.body);
    for (const n of KEYS.keys()) if (n.kind === "quiz") QUIZ_NO.set(n, QUIZ_NO.size);
    const state = JSON.parse(JSON.stringify(block.state));
    const answered = new Map();      // "quiz.question" -> {quiz, question, chosen}
    const ctx = makeCtx(state, KEYS, opts);
    let appAccepted = 0, appRejected = 0;        // app bridge messages this render (accepted count is capped at APP.events)
    const root = h("div", "evoui"); root.lang = language;
    if (block.body.kind !== "app") root.append(h("div", "evoui-title", block.title));   // a whole-block app is its own page: its title is its own
    const host = h("div"); root.append(host); mount.replaceChildren(root);
    const env = () => { resetSteps(); const e = Object.assign(Object.create(null), state); for (const [k, v] of Object.entries(block.derived)) e[k] = evalExpr(v, e); return e; };
    /* Built once; a state change runs the updaters, so a control being dragged is never replaced. */
    let ups = [];
    const set = (k, v) => {
      state[k] = v; ctx.change();
      if (typeof opts.scheduleUpdate === "function") opts.scheduleUpdate(update); else update();
    };
    /* `building` > 0 while nodes are built (first render, a switched `if` branch): a node built but not
       yet in the document counts as alive then (a reveal's "after" listener relies on it). */
    let building = 0;
    const afterOffs = new Map();          // reveal node -> its "after" listener's unsubscribe (one per node)
    const pagerOffs = new Map();          // pager node -> its done listener's unsubscribe (one per node)
    const pacers = new Map();             // playing player -> its step ms; ctx.pace = the fastest
    const setPace = (k, ms) => { if (ms == null) pacers.delete(k); else pacers.set(k, ms); ctx.pace = pacers.size ? Math.min(...pacers.values()) : null; };
    const build = (n, U) => { building += 1; try { return node(n, U); } finally { building -= 1; } };
    const calm = () => calmOf(doc);
    const moveMs = () => (ctx.pace ? Math.max(60, Math.min(MOVE_MS, ctx.pace * 0.8)) : MOVE_MS);   // a playing player sets the pace
    const svgIcon = (d, cls) => { const s = doc.createElementNS(SVGNS, "svg"); uiAttr(s, "viewBox", () => "0 0 16 16"); uiAttr(s, "aria-hidden", () => "true"); if (cls) uiAttr(s, "class", () => cls); for (const x of [].concat(d)) { const pa = doc.createElementNS(SVGNS, "path"); uiAttr(pa, "d", () => x); s.append(pa); } return s; };
    function node(n, U) {
      switch (n.kind) {
        case "box": case "grid-item": { const d = h("div", `evoui-box g${n.gap ?? 2}${n.justify ? " j-" + n.justify : ""}${n.border ? " bordered" : ""}${n.tone === "surface" ? " surface" : ""}${n.kind === "grid-item" && (n.span || 1) > 1 ? " spanned" : ""}`); if (n.kind === "grid-item") d.dataset.span = String(n.span || 1); boxStyle(d, n, U); n.children.forEach((c) => d.append(node(c, U))); return d; }
        case "grid": {
          const cols = n.columns || 2, d = h("div", `evoui-grid c${cols} g${n.gap ?? 2}`);
          n.children.forEach((c) => { const el = node(c, U); if (el.dataset && el.dataset.span) el.style.gridColumn = `span ${Math.min(cols, Number(el.dataset.span))}`; d.append(el); });
          return d;
        }
        case "title": { const d = h("div", `evoui-num z-${n.size || "lg"} t-${n.tone || "default"}`); U.push((E) => { uiProp(d, "textContent", () => template(n.text, E)); if (n.color != null) d.style.color = colorOf(bound(n.color, E)) || ""; }); return d; }
        case "caption": { const d = h("div", "evoui-text s-sm t-secondary"); if (n.align) d.style.textAlign = n.align; U.push((E) => { uiProp(d, "textContent", () => template(n.text, E)); }); return d; }
        case "math": {
          const d = h(n.display ? "div" : "span", "evoui-math" + (n.display ? " display" : ""));
          let el = null; if (typeof opts.renderMath === "function") { try { el = opts.renderMath(n.tex, !!n.display); } catch (x) { console.error(x); } }
          if (el && el.nodeType === 1) d.append(el); else d.append(h("code", "", n.tex));
          return d;
        }
        case "table": {
          const wrap = h("div", "evoui-tablewrap"), t = h("table", "evoui-table"), hd = h("tr"), body = h("tbody");
          n.columns.forEach((c) => hd.append(h("th", "", c))); const th = h("thead"); th.append(hd); t.append(th, body); wrap.append(t);
          const cap = n.caption ? h("div", "evoui-text s-sm t-secondary") : null; if (cap) wrap.append(cap);
          U.push((E) => {
            let rows;
            if (typeof n.rows === "string") { rows = evalExpr(n.rows, E); if (!Array.isArray(rows)) fail("table rows must be an array"); rows = rows.slice(0, LIMITS.exprRows).map((r) => (Array.isArray(r) ? r : [r]).slice(0, n.columns.length).map((v) => (typeof v === "number" ? fmt(v, Number.isInteger(v) ? 0 : 2) : v == null ? "" : String(v)))); }
            else rows = n.rows.map((r) => r.map((c) => template(c, E)));
            const keyOf = (tr) => [...tr.children].map((td) => td.textContent).join(""), moving = typeof n.rows === "string" && body.childElementCount && t.isConnected && !calm();
            const before = moving ? flipFirst([...body.children], keyOf) : null;          // re-sorted rows slide (200 ms)
            body.replaceChildren(...rows.map((r) => { const tr = h("tr"); r.forEach((c) => tr.append(h("td", "", c))); return tr; }));
            if (before) flipLast([...body.children], keyOf, before, moveMs());
            if (cap) uiProp(cap, "textContent", () => template(n.caption, E));
          });
          return wrap;
        }
        case "icon": {
          const s = doc.createElementNS(SVGNS, "svg"); uiAttr(s, "viewBox", () => "0 0 16 16"); uiAttr(s, "class", () => `evoui-icon t-${n.tone || "secondary"}`); uiAttr(s, "aria-hidden", () => "true");
          const p = doc.createElementNS(SVGNS, "path"); uiAttr(p, "d", () => ICON_PATHS[n.name]); s.append(p); return s;
        }
        case "divider": return h("hr", "evoui-divider");
        case "input": {
          const w = h("label", "evoui-input"), t = h("span"), i = h("input");
          Object.assign(i, { type: "number", min: n.min, max: n.max, step: n.step, value: state[n.bind] }); i.inputMode = "decimal";
          i.onchange = () => { const v = Number(i.value); if (i.value === "" || !isFinite(v)) { i.value = state[n.bind]; return; } set(n.bind, Math.min(n.max, Math.max(n.min, v))); };
          U.push((E) => { uiProp(t, "textContent", () => template(n.label ?? "{" + n.bind + "}", E)); if (Number(i.value) !== Number(state[n.bind])) i.value = state[n.bind]; });
          w.append(t, i); return w;
        }
        case "select": {
          const w = h("label", "evoui-select"), t = h("span"), s = h("select");
          n.options.forEach((o, oi) => { const op = h("option", "", o.label); op.value = String(oi); s.append(op); });
          s.onchange = () => set(n.bind, n.options[Number(s.value)].value);
          U.push((E) => { uiProp(t, "textContent", () => n.label ? template(n.label, E) : ""); t.hidden = !n.label; const k = n.options.findIndex((o) => o.value === state[n.bind]); if (k >= 0 && s.value !== String(k)) s.value = String(k); });
          w.append(t, s); return w;
        }
        case "button": {
          const b = h("button", `evoui-btn t-${n.tone || "default"}`); b.type = "button";
          let lastE = null; b.onclick = () => { try { resetSteps(); const E = lastE || env(); const vals = Object.entries(n.set).map(([k, v]) => [k, typeof v === "string" ? evalExpr(v, E) : v]); vals.forEach(([k, v]) => { state[k] = v; }); ctx.change(); update(); } catch (x) { fallback(x); } };
          U.push((E) => { lastE = E; uiProp(b, "textContent", () => template(n.label, E)); }); return b;
        }
        case "row": { const d = h("div", `evoui-row g${n.gap ?? 2} j-${n.justify || "start"}${n.wrap ? " wrap" : ""}`); boxStyle(d, n, U); n.children.forEach((c) => d.append(node(c, U))); return d; }
        case "text": { const d = h("div", `evoui-text s-${n.size || "md"} w-${n.weight || "normal"} t-${n.tone || "default"}`); if (n.align) d.style.textAlign = n.align; let shown = null;
          U.push((E) => { const t = template(n.text, E); if (n.color != null) d.style.color = colorOf(bound(n.color, E)) || ""; if (t === shown) return; shown = t;
            if (!n.rich) { uiProp(d, "textContent", () => t); return; }
            d.replaceChildren(...t.split("**").map((x, i) => { if (i % 2 === 0) return uiNode(() => x); const b = h("b", "", x); return b; })); });
          return d; }
        case "badge": { const d = h("span", `evoui-badge t-${n.tone || "secondary"}`); U.push((E) => { uiProp(d, "textContent", () => template(n.text, E)); }); return d; }
        case "slider": {
          const w = h("label", "evoui-slider"), t = h("span"), i = h("input");
          Object.assign(i, { type: "range", min: n.min, max: n.max, step: n.step, value: state[n.bind] });
          i.oninput = () => { ctx.sliding = true; set(n.bind, Number(i.value)); ctx.sliding = false; };
          U.push((E) => { uiProp(t, "textContent", () => template(n.label ?? "{" + n.bind + "}", E)); if (Number(i.value) !== Number(state[n.bind])) i.value = state[n.bind]; });
          w.append(t, i); return w;
        }
        case "choice": {
          const d = h("div", `evoui-choice ${n.style === "buttons" ? "buttons" : "segmented"}`); uiAttr(d, "role", () => "group");
          const bs = n.options.map((o) => { const b = h("button", "", o.label); b.type = "button"; b.onclick = () => set(n.bind, o.value); d.append(b); return [b, o]; });
          U.push(() => bs.forEach(([b, o]) => uiAttr(b, "aria-pressed", () => String(state[n.bind] === o.value)))); return d;
        }
        case "toggle": case "checkbox": { const w = h("label", "evoui-toggle"), i = h("input"), t = h("span"); i.type = "checkbox"; i.onchange = () => set(n.bind, typeof state[n.bind] === "number" ? Number(i.checked) : i.checked); U.push((E) => { i.checked = !!state[n.bind]; uiProp(t, "textContent", () => template(n.label ?? n.bind, E)); }); w.append(i, t); return w; }
        case "if": {         // every branch switch after the first render: old exits 120 ms, new enters 180 ms + 4 px
          const slot = h("div", "evoui-slot"); let branch = null, inner = [];
          U.push((E) => { const want = evalExpr(n.cond, E) ? "then" : n.else ? "else" : "none";
            if (want !== branch) { const first = branch === null; branch = want; inner = []; swapIn(slot, want === "none" ? null : build(n[want], inner), { instant: first }); }
            inner.forEach((f) => f(E)); });
          return slot;
        }
        case "plot": case "chart": return plotNode(n, U);     // a re-draw glides (tweenSvg, 200 ms layout move) when its shapes stay the same
        case "reveal": return reveal(n, U);
        case "timeline": return timeline(n);
        case "deep-dive": return deepDive(n, U);
        case "animate": {               // its children enter (180 ms; "rise" + 4 px) whenever it appears, 50 ms apart
          const d = h("div", "evoui-box g2 evoui-animate");
          n.children.forEach((c, i) => { const el = node(c, U); d.append(el); enterEl(el, n.effect, Math.min(i, 4) * 50); });
          return d;
        }
        case "transition": {            // the key changes: the old look exits 120 ms, the new content enters 180 ms + 4 px
          const wrap = h("div", "evoui-tr"), inner = h("div", "evoui-box g2 evoui-tr-inner"); wrap.append(inner);
          let last = null;
          U.push((E) => {                // runs before the children's updaters: inner still shows the old values
            const v = evalExpr(n.key, E), k = v === undefined ? "undefined" : JSON.stringify(v);
            if (last === null || k === last) { last = k; return; }
            last = k;
            for (const g of [...wrap.children]) if (g !== inner) g.remove();
            if (calm() || !wrap.isConnected) return;
            const r = inner.getBoundingClientRect(), ghost = ghostOf(inner);   // unnamed, frames emptied, before it is inserted
            wrap.append(ghost); leaveEl(ghost, r); enterEl(inner, "rise", ENTER_DELAY_MS);
          });
          n.children.forEach((c) => inner.append(node(c, U)));
          return wrap;
        }
        case "celebrate": {             // confetti when `when` goes false -> true after the first render (never on restore)
          const d = h("span", "evoui-celebrate"); d.hidden = true; let prev = null;
          U.push((E) => { const on = !!evalExpr(n.when, E); if (prev === false && on && d.isConnected && !leaving(d)) celebrate(doc); prev = on; });
          return d;
        }
        case "timer": return timer(n, U);
        case "player": return player(n, U);
        case "quiz": return practiceEl(n, quiz(n));
        case "flashcards": return practiceEl(n, flashcards(n));
        case "fill-blank": return fillBlank(n);
        case "sentence": return sentenceBuilder(n);
        case "pager": return pager(n, U);
        case "route": return route(n, U);
        case "app": return app(n, U);
        case "svg": return drawing(n, U);
        case "each": {            // one copy of the children per item, in the parent's own layout
          const d = h("div", "evoui-each");
          U.push(region(d, null, () => { const ups = [], els = n.children.map((c) => build(c, ups)); return { nodes: els, ups }; }, n));
          return d;
        }
      }
      fail("unknown kind " + n.kind);
    }
    /* ---------- each, box styling and drawings ---------- */
    let marks = 0;
    /* What one `each` keeps up to date: an instance per item, reused by position so a dragged or moving element stays the same element. */
    function region(parent, anchor, make, n) {
      const live = [], as = n.as || "d", idx = n.index || "i";
      const drop = (inst) => { disposeAll(inst.ups); inst.nodes.forEach((x) => x.remove()); };
      const up = (E) => {
        let list = evalExpr(n.in, E);
        if (typeof list === "number") list = BUILTINS.range(list);
        if (!Array.isArray(list)) fail("each needs a list");
        list = list.slice(0, 2000);
        while (live.length > list.length) drop(live.pop());
        while (live.length < list.length) { const inst = make(), into = anchor ? anchor.parentNode : parent; inst.nodes.forEach((x) => into.insertBefore(x, anchor)); live.push(inst); }
        for (let k = 0; k < list.length; k++) {
          const F = Object.assign(Object.create(null), E); F[as] = list[k]; F[idx] = k;
          for (const f of live[k].ups) f(F);
        }
      };
      up.dispose = () => { while (live.length) drop(live.pop()); };
      return up;
    }
    /* What a region put in the picture after it was built is not among the nodes it was built with: it goes when the region says so. */
    const disposeAll = (ups) => { for (const f of ups) if (f.dispose) f.dispose(); };
    const SIZE = (v) => (typeof v === "number" || /^[0-9.]+$/.test(String(v)) ? v + "px" : /^[0-9.]+(%|px)$/.test(String(v)) ? String(v) : "");
    const RADII = { sm: 4, md: 6, lg: 10, xl: 14, "2xl": 18, full: 999 };
    function boxStyle(d, n, U) {
      const st = (f, fn) => { if (!has(n, f)) return; U.push((E) => fn(bound(n[f], E))); };
      st("background", (v) => { d.style.background = colorOf(v) || ""; });
      st("color", (v) => { d.style.color = colorOf(v) || ""; });
      st("width", (v) => { d.style.width = SIZE(v); });
      st("height", (v) => { d.style.height = SIZE(v); });
      st("flex", (v) => { const x = Number(v); d.style.flex = isFinite(x) && x >= 0 ? `${x} ${x} 0` : ""; d.style.minWidth = "0"; });
      st("radius", (v) => { const x = RADII[v] ?? Number(v); d.style.borderRadius = isFinite(x) ? x + "px" : ""; });
      st("padding", (v) => { const x = Number(v); d.style.padding = isFinite(x) ? x * 4 + "px" : ""; });
      if (n.align) d.style.alignItems = n.align === "start" || n.align === "end" ? "flex-" + n.align : n.align;
      else if (n.kind === "row" && has(n, "height")) d.style.alignItems = "stretch";     // a bar made of boxes
      if (n.clip) d.style.overflow = "hidden";
      if (n.click) touchable(d, n.click, () => last);
      let last = null; if (n.click) U.push((E) => { last = E; });
    }
    /* click: a map of state -> value, run on a click, Enter or Space. */
    const applySet = (map, F) => { resetSteps(); const vals = Object.entries(map).map(([k, v]) => [k, typeof v === "string" ? evalExpr(v, F) : v]); vals.forEach(([k, v]) => { state[k] = v; }); ctx.change(); update(); };
    function touchable(el, map, envOf) {
      el.classList.add("evoui-hit"); uiAttr(el, "tabindex", () => "0"); uiAttr(el, "role", () => "button");
      const go = (ev) => { ev.stopPropagation(); try { applySet(map, envOf()); } catch (x) { fallback(x); } };
      el.addEventListener("click", go);
      el.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); go(ev); } });
    }
    const svgGap = (s) => Number(s.getAttribute("viewBox").trim().split(/[ ,]+/)[2]);
    /* drag: a map of state -> value with mx, my the pointer in the coordinates of what is dragged. Arrow keys move it by 2 % of the picture. */
    function draggable(el, map, envOf, pos, svg) {
      el.classList.add("evoui-drag"); uiAttr(el, "tabindex", () => "0"); uiAttr(el, "role", () => "slider");
      let raf = 0, pending = null;
      const flush = () => { raf = 0; if (!pending) return; const [mx, my] = pending; pending = null; try { const F = Object.assign(Object.create(null), envOf()); F.mx = mx; F.my = my; applySet(map, F); } catch (x) { fallback(x); } };
      const at = (ev) => { const m = svg.getScreenCTM(); if (!m) return [0, 0]; const p = svg.createSVGPoint(); p.x = ev.clientX; p.y = ev.clientY; const q = p.matrixTransform(m.inverse()); return pos(q.x, q.y); };
      el.addEventListener("pointerdown", (ev) => { ev.preventDefault(); ev.stopPropagation(); el.setPointerCapture(ev.pointerId); ctx.dragging = true; });
      el.addEventListener("pointermove", (ev) => { if (!ctx.dragging || !el.hasPointerCapture(ev.pointerId)) return; pending = at(ev); if (!raf) raf = requestAnimationFrame(flush); });
      const end = (ev) => { if (el.hasPointerCapture(ev.pointerId)) el.releasePointerCapture(ev.pointerId); ctx.dragging = false; };
      el.addEventListener("pointerup", end); el.addEventListener("pointercancel", end);
      el.addEventListener("keydown", (ev) => {
        const d = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[ev.key]; if (!d) return; ev.preventDefault();
        const b = el.getBoundingClientRect(), g = svgGap(svg) * 0.02, k = svg.getBoundingClientRect().width / svgGap(svg) || 1;
        pending = at({ clientX: b.x + b.width / 2 + d[0] * g * k, clientY: b.y + b.height / 2 + d[1] * g * k }); flush();
      });
    }
    /* The tween: numbers in an attribute glide to their new value when the rest of the text stays the same. Only state changes that jump
       (a button, a step); a slider or a drag follows the hand. */
    const NUM = /-?\d*\.?\d+(?:e[-+]?\d+)?/gi;
    function tweener(ms) {
      const jobs = new Map(); let raf = 0, t0 = 0, timer = 0;
      const finish = () => { cancelAnimationFrame(raf); clearTimeout(timer); raf = 0; for (const [, j] of jobs) uiAttr(j.el, j.k, () => j.to); jobs.clear(); };
      const frame = (t) => {
        const q = Math.min(1, (t - t0) / ms), e = 1 - (1 - q) * (1 - q);
        for (const j of jobs.values()) { let i = 0; uiAttr(j.el, j.k, () => j.to.replace(NUM, () => { const a = j.a[i], b = j.b[i]; i++; return String(Math.round((a + (b - a) * e) * 100) / 100); })); }
        if (q < 1) raf = requestAnimationFrame(frame); else finish();
      };
      return (el, k, to) => {
        const old = el.getAttribute(k), now = ms > 0 && !ctx.dragging && !ctx.sliding && !calm() && el.isConnected;
        const key = k + "\0"; let bag = el.__jobs; if (!bag) { bag = {}; Object.defineProperty(el, "__jobs", { value: bag }); }
        const running = bag[key]; if (running && running.to === to) return;
        if (old === to && !running) return;
        if (!now || old == null || old.replace(NUM, "#") !== to.replace(NUM, "#") || jobs.size > 800) { if (running) { jobs.delete(running); delete bag[key]; } uiAttr(el, k, () => to); return; }
        const a = (old.match(NUM) || []).map(Number), b = (to.match(NUM) || []).map(Number);
        if (a.length !== b.length || a.every((x, i) => x === b[i])) { uiAttr(el, k, () => to); return; }
        const job = { el, k, to, a, b };
        if (running) jobs.delete(running);
        bag[key] = job; jobs.set(job, job);
        t0 = performance.now();
        if (!raf) raf = requestAnimationFrame(frame);
        clearTimeout(timer); timer = setTimeout(finish, ms + 80);      // a hidden page runs no frames
      };
    }
    const CJK = /[\u2e80-\uffef]/;
    const textWidth = (t, fs) => { let w = 0; for (const c of t) w += CJK.test(c) ? fs : fs * 0.56; return w; };
    function lines(t, width, fs) {
      const out = [];
      for (const para of String(t).split("\n")) {
        let cur = "", w = 0, tokens = para.match(/[\u2e80-\uffef]|[^\u2e80-\uffef\s]+\s*|\s+/g) || [""];
        for (const tk of tokens) { const tw = textWidth(tk, fs); if (w + tw > width && cur) { out.push(cur.trim()); cur = ""; w = 0; } cur += tk; w += tw; }
        out.push(cur.trim());
      }
      return out;
    }
    function drawing(n, U) {
      const wrap = h("div", "evoui-svgwrap"), svg = doc.createElementNS(SVGNS, "svg"), vw = svgGap({ getAttribute: () => n.viewBox }); wrap.append(svg);
      uiAttr(svg, "viewBox", () => n.viewBox); uiAttr(svg, "class", () => "evoui-svg"); svg.style.maxWidth = Math.round(vw * 1.25) + "px";
      const put = tweener(n.tween ?? 220);
      const images = new Map((n.images || []).map((im) => [im.id, im]));
      if (images.size) { const credit = h("div", "evoui-app-credit"); for (const im of images.values()) credit.append(h("div", "", uiValue(() => [ui("图"), im.title, im.author, im.license, im.source].filter(Boolean).join(" · ")))); wrap.append(credit); }
      let k = 1;                 // screen px per unit: small type stays readable on a narrow screen
      const refit = () => { const w = svg.getBoundingClientRect().width; if (w > 0) k = w / vw; if (k >= 1 && !refit.small) return; refit.small = k < 1; for (const t of svg.querySelectorAll("text[data-fs]")) uiAttr(t, "font-size", () => Math.max(Number(t.dataset.fs), k < 1 ? 10 / k : 0)); };
      const tags = { arrow: "g", node: "g", plot: "g" };
      const make = (c, parent, sc, ups) => {
        if (c.kind === "each") {
          const anchor = doc.createComment(""); parent.append(anchor);
          ups.push(region(parent, anchor, () => { const box = doc.createDocumentFragment(), inner = []; c.children.forEach((x) => make(x, box, sc, inner)); return { nodes: [...box.childNodes], ups: inner }; }, c));
          return;
        }
        if (c.kind === "if") {
          const anchor = doc.createComment(""); parent.append(anchor); let on = null, nodes = [], inner = [];
          const up = (E) => { const want = evalExpr(c.cond, E) ? "then" : c.else ? "else" : "none";
            if (want !== on) { on = want; disposeAll(inner); nodes.forEach((x) => x.remove()); inner = []; const box = doc.createDocumentFragment(); if (want !== "none") make(c[want], box, sc, inner); nodes = [...box.childNodes]; const into = anchor.parentNode; nodes.forEach((x) => into.insertBefore(x, anchor)); }
            inner.forEach((f) => f(E)); };
          up.dispose = () => { disposeAll(inner); nodes.forEach((x) => x.remove()); nodes = []; inner = []; on = null; };
          ups.push(up);
          return;
        }
        if (++marks > MAX_MARKS) fail("too many drawn elements");
        const tag = tags[c.el] || c.el, el = doc.createElementNS(SVGNS, tag); parent.append(el);
        let last = null; ups.push((E) => { last = E; });
        const coords = c.el === "plot" ? plotCoords(c, el, sc, ups) : sc;
        if (c.click) touchable(el, c.click, () => last);
        if (c.drag) draggable(el, c.drag, () => last, (x, y) => (sc.plot ? [sc.invX(x), sc.invY(y)] : [x, y]), svg);
        if (c.el === "arrow") { arrowMark(c, el, sc, ups, put); return; }
        if (c.el === "node") { nodeMark(c, el, sc, ups, put); return; }
        if (c.el === "image") imageMark(c, el, images);
        if (c.el === "text") { const fs = Number(c["font-size"]); el.dataset.fs = String(isFinite(fs) && fs > 0 ? fs : 13); uiAttr(el, "font-size", () => el.dataset.fs); }
        for (const [a, v] of Object.entries(c)) {
          if (a === "el" || a === "children" || a === "click" || a === "drag" || a === "text" || a === "wrap" || a === "tip" || a === "src" || a === "alt" || a === "left" || a === "top" || a === "grid" || a === "xlabel" || a === "ylabel" || (a === "font-size" && c.el === "text")) continue;
          if (c.el === "plot" && !["opacity", "transform"].includes(a)) continue;
          if (a === "y" && c.el === "rect" && sc.plot) continue;
          if (typeof v === "string" && !v.includes("{") && !(sc.plot && GEO.has(a))) { const x = attrOut(a, v, c, sc, 0); if (x != null) uiAttr(el, a, () => x); continue; }
          if (typeof v === "number" && !sc.plot) { uiAttr(el, a, () => v); continue; }
          ups.push((E) => { const x = attrOut(a, bound(v, E), c, sc, E); if (x == null) el.removeAttribute(a); else put(el, a, String(x)); });
        }
        if (c.el === "rect" && sc.plot) plotRect(c, el, sc, ups);
        if (c.el === "text") ups.push((E) => { const t = template(c.text, E); if (el.__t === t) return; el.__t = t; textInto(el, t, c, E); });
        if ((c.el === "path" || c.el === "polyline" || c.el === "line") && !has(c, "fill")) uiAttr(el, "fill", () => "none");
        if (c.el === "line" && !has(c, "stroke")) uiAttr(el, "stroke", () => "var(--eu-fg2)");
        if (sc.plot && c.el === "path") { uiAttr(el, "vector-effect", () => "non-scaling-stroke"); ups.push(() => uiAttr(el, "transform", () => sc.matrix())); }
        if (c.tip) { const ti = doc.createElementNS(SVGNS, "title"); el.append(ti); ups.push((E) => { uiProp(ti, "textContent", () => template(c.tip, E)); }); }
        if (c.children) c.children.forEach((x) => make(x, el, coords, ups));
      };
      const textInto = (el, t, c, E) => {
        const wrapW = c.wrap != null ? Number(bound(c.wrap, E)) : 0;
        if (!(wrapW > 0)) { if (t.includes("\n")) tspans(el, t.split("\n"), Number(el.getAttribute("x")) || 0, 1.25); else { const ti = el.querySelector("title"); uiProp(el, "textContent", () => t); if (ti) el.append(ti); } return; }
        tspans(el, lines(t, wrapW, Number(el.dataset.fs)), Number(el.getAttribute("x")) || 0, 1.3);
      };
      const tspans = (el, ls, x, lh) => { el.replaceChildren(...ls.map((l, i) => { const s = doc.createElementNS(SVGNS, "tspan"); uiAttr(s, "x", () => x); uiAttr(s, "dy", () => i ? lh + "em" : "0"); uiProp(s, "textContent", () => l); return s; })); };
      const GEO = new Set(["x", "cx", "x1", "x2", "y", "cy", "y1", "y2", "width", "height", "points"]);
      const pointsOut = (v, sc) => {
        if (Array.isArray(v)) return v.map((p) => (Array.isArray(p) ? `${round2(sc.X(Number(p[0])))},${round2(sc.Y(Number(p[1])))}` : "")).join(" ");
        if (!sc.plot) return String(v);
        const ns = String(v).match(NUM) || [], out = []; for (let i = 0; i + 1 < ns.length; i += 2) out.push(`${round2(sc.X(Number(ns[i])))},${round2(sc.Y(Number(ns[i + 1])))}`); return out.join(" ");
      };
      const attrOut = (a, v, c, sc) => {
        if (v == null || v === false) return null;
        if (COLOR_ATTRS.includes(a)) return colorOf(v);
        if (a === "clip-path") return /^url\(#[A-Za-z0-9_-]+\)$/.test(String(v)) ? String(v) : null;
        if (a === "points") return pointsOut(v, sc);
        if (typeof v === "object") return null;
        if (a === "d" && !PATH_RE.test(String(v))) return null;
        if (!sc.plot || !GEO.has(a) || c.el === "text" && a !== "x" && a !== "y") return v;
        const x = Number(v); if (!isFinite(x)) return null;
        if (a === "x" || a === "cx" || a === "x1" || a === "x2") return round2(sc.X(x));
        if (a === "width") return round2(Math.abs(sc.X(x) - sc.X(0)));
        if (a === "height") return round2(Math.abs(sc.Y(x) - sc.Y(0)));
        return round2(sc.Y(x));
      };
      const round2 = (x) => Math.round(x * 100) / 100;
      /* A plot's rect: y is its lower edge, so bars stand on the axis. */
      const plotRect = (c, el, sc, ups) => { ups.push((E) => { const y = Number(bound(c.y ?? 0, E)), hh = Number(bound(c.height, E)); if (isFinite(y) && isFinite(hh)) put(el, "y", String(round2(Math.min(sc.Y(y), sc.Y(y + hh))))); }); };
      function arrowMark(c, el, sc, ups, put2) {
        const line = doc.createElementNS(SVGNS, "path"), head = doc.createElementNS(SVGNS, "path"), head2 = c.both ? doc.createElementNS(SVGNS, "path") : null, lab = has(c, "label") ? doc.createElementNS(SVGNS, "text") : null;
        el.append(line, head); if (head2) el.append(head2); if (lab) { uiAttr(lab, "text-anchor", () => "middle"); uiAttr(lab, "class", () => "halo"); lab.dataset.fs = "12"; uiAttr(lab, "font-size", () => 12); el.append(lab); }
        uiAttr(line, "fill", () => "none");
        ups.push((E) => {
          const n4 = ["x1", "y1", "x2", "y2"].map((a) => Number(bound(c[a], E))); if (!n4.every(isFinite)) return;
          const x1 = sc.X(n4[0]), y1 = sc.Y(n4[1]), x2 = sc.X(n4[2]), y2 = sc.Y(n4[3]), sw = Number(has(c, "stroke-width") ? bound(c["stroke-width"], E) : 1.5) || 1.5, L = 5 + 2.2 * sw, bend = Number(has(c, "bend") ? bound(c.bend, E) : 0) || 0;
          const mx = (x1 + x2) / 2 - (y2 - y1) * bend, my = (y1 + y2) / 2 + (x2 - x1) * bend;
          const a2 = bend ? Math.atan2(y2 - my, x2 - mx) : Math.atan2(y2 - y1, x2 - x1), a1 = bend ? Math.atan2(y1 - my, x1 - mx) : a2 + Math.PI;
          const ex = x2 - Math.cos(a2) * L * 0.7, ey = y2 - Math.sin(a2) * L * 0.7, sx = head2 ? x1 - Math.cos(a1) * L * 0.7 : x1, sy = head2 ? y1 - Math.sin(a1) * L * 0.7 : y1;
          const cc = has(c, "stroke") ? colorOf(bound(c.stroke, E)) || "var(--eu-fg2)" : "var(--eu-fg2)";
          put2(line, "d", bend ? `M${round2(sx)} ${round2(sy)}Q${round2(mx)} ${round2(my)} ${round2(ex)} ${round2(ey)}` : `M${round2(sx)} ${round2(sy)}L${round2(ex)} ${round2(ey)}`);
          uiAttr(line, "stroke", () => cc); uiAttr(line, "stroke-width", () => sw);
          for (const a of ["stroke-dasharray", "opacity"]) if (has(c, a)) uiAttr(el, a, () => bound(c[a], E));
          const tip = (hd, x, y, a) => { put2(hd, "d", `M${round2(x)} ${round2(y)}L${round2(x - L * Math.cos(a - 0.42))} ${round2(y - L * Math.sin(a - 0.42))}L${round2(x - L * Math.cos(a + 0.42))} ${round2(y - L * Math.sin(a + 0.42))}Z`); uiAttr(hd, "fill", () => cc); uiAttr(hd, "stroke", () => "none"); };
          tip(head, x2, y2, a2); if (head2) tip(head2, x1, y1, a1);
          if (lab) { const t = template(c.label, E), nx = bend ? (x1 + x2) / 2 - (y2 - y1) * bend / 2 : (x1 + x2) / 2, ny = bend ? (y1 + y2) / 2 + (x2 - x1) * bend / 2 : (y1 + y2) / 2; put2(lab, "x", String(round2(nx))); put2(lab, "y", String(round2(ny - 5))); uiAttr(lab, "fill", () => cc); if (lab.textContent !== t) uiProp(lab, "textContent", () => t); }
        });
      }
      const STRONG = new Set(["blue", "orange", "green", "red", "purple", "teal", "gray", "text", "info", "success", "danger", "discovery", "primary"]);
      /* x, y, width, height like a rect; the text sits in the middle and wraps inside it. */
      function nodeMark(c, el, sc, ups, put2) {
        const r = doc.createElementNS(SVGNS, "rect"), t = doc.createElementNS(SVGNS, "text"); el.append(r, t);
        uiAttr(t, "text-anchor", () => "middle"); uiAttr(t, "dominant-baseline", () => "central");
        let shown = null;
        ups.push((E) => {
          const nx = Number(bound(c.x ?? 0, E)), ny = Number(bound(c.y ?? 0, E)), nw = Number(bound(c.width, E)), nh = Number(bound(c.height, E)); if (![nx, ny, nw, nh].every(isFinite)) return;
          const X = sc.X(nx), W = Math.abs(sc.X(nx + nw) - X), Y0 = sc.plot ? Math.min(sc.Y(ny), sc.Y(ny + nh)) : sc.Y(ny), H = Math.abs(sc.Y(ny + nh) - sc.Y(ny));
          put2(r, "x", String(round2(X))); put2(r, "y", String(round2(Y0))); put2(r, "width", String(round2(W))); put2(r, "height", String(round2(H)));
          uiAttr(r, "rx", () => has(c, "rx") ? bound(c.rx, E) : 6);
          const fillV = has(c, "fill") ? bound(c.fill, E) : "surface";
          uiAttr(r, "fill", () => colorOf(fillV) || "none"); uiAttr(r, "stroke", () => (has(c, "stroke") ? colorOf(bound(c.stroke, E)) : "var(--eu-line)") || "none");
          if (has(c, "stroke-width")) uiAttr(r, "stroke-width", () => bound(c["stroke-width"], E));
          if (has(c, "stroke-dasharray")) uiAttr(r, "stroke-dasharray", () => bound(c["stroke-dasharray"], E));
          if (has(c, "opacity")) uiAttr(el, "opacity", () => bound(c.opacity, E));
          uiAttr(t, "fill", () => has(c, "color") ? colorOf(bound(c.color, E)) || "var(--eu-fg)" : STRONG.has(String(fillV)) ? "var(--eu-bg)" : "var(--eu-fg)");
          if (has(c, "font-weight")) uiAttr(t, "font-weight", () => bound(c["font-weight"], E));
          const text2 = has(c, "text") ? template(c.text, E) : "";
          const key = text2 + "|" + W + "|" + H + "|" + X + "|" + Y0;
          if (key === shown) return; shown = key;
          let fs = Number(has(c, "font-size") ? bound(c["font-size"], E) : 13) || 13, ls = lines(text2, Math.max(8, W - 10), fs);
          while (fs > 9 && ls.length * fs * 1.25 > H - 2) { fs -= 1; ls = lines(text2, Math.max(8, W - 10), fs); }
          t.dataset.fs = String(fs); uiAttr(t, "font-size", () => Math.max(fs, k < 1 ? 10 / k : 0));
          put2(t, "y", String(round2(Y0 + H / 2 - (ls.length - 1) * 0.625 * fs))); uiAttr(t, "x", () => round2(X + W / 2));
          tspans(t, ls, round2(X + W / 2), 1.25);
        });
      }
      function imageMark(c, el, imgs) {
        uiAttr(el, "preserveAspectRatio", () => has(c, "alt") ? "xMidYMid slice" : "xMidYMid slice");
        const im = imgs.get(c.src);
        Promise.resolve().then(() => ctx.image(im)).then((got) => { if (!got) return; const bytes = new Uint8Array(got.bytes); let bin = ""; for (let i = 0; i < bytes.length; i += 8192) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 8192));
          uiAttr(el, "href", () => "data:" + (got.mime === "image/png" ? "image/png" : "image/webp") + ";base64," + btoa(bin)); }, (x) => console.warn("EvoUI image: " + x));
      }
      /* A plot: a window onto data coordinates (x, y are its ranges; y goes up), with axes. */
      function plotCoords(c, el, sc, ups) {
        const g = { plot: true, xd: [0, 1], yd: [0, 1] };
        const L = Number(c.left ?? 44), T = Number(c.top ?? 10), W = Number(c.width ?? (vw - L - 12)), H = Number(c.height ?? 240);
        g.X = (x) => L + (x - g.xd[0]) / (g.xd[1] - g.xd[0]) * W; g.Y = (y) => T + H - (y - g.yd[0]) / (g.yd[1] - g.yd[0]) * H;
        g.invX = (px) => g.xd[0] + (px - L) / W * (g.xd[1] - g.xd[0]); g.invY = (py) => g.yd[0] + (T + H - py) / H * (g.yd[1] - g.yd[0]);
        g.matrix = () => { const sx = W / (g.xd[1] - g.xd[0]), sy = -H / (g.yd[1] - g.yd[0]); return `matrix(${sx} 0 0 ${sy} ${L - g.xd[0] * sx} ${T + H - g.yd[0] * sy})`; };
        const axes = doc.createElementNS(SVGNS, "g"); uiAttr(axes, "class", () => "axis"); el.append(axes);
        let key = "";
        ups.unshift((E) => {
          const xd = bound(c.x, E), yd = bound(c.y, E);
          if (!Array.isArray(xd) || !Array.isArray(yd) || !(Number(xd[1]) > Number(xd[0])) || !(Number(yd[1]) > Number(yd[0]))) fail("plot ranges must be [low, high]");
          g.xd = [Number(xd[0]), Number(xd[1])]; g.yd = [Number(yd[0]), Number(yd[1])];
          const K = g.xd.join() + g.yd.join(); if (K === key) return; key = K;
          axes.replaceChildren();
          const tick = (lo, hi) => { const st = Math.pow(10, Math.floor(Math.log10((hi - lo) / 5))), e = (hi - lo) / 5 / st, s2 = (e >= 5 ? 5 : e >= 2 ? 2 : 1) * st, out = []; for (let v = Math.ceil(lo / s2) * s2; v <= hi + s2 * 1e-6; v += s2) out.push(Math.round(v / s2) * s2); return out; };
          const add = (tag, at, txt) => { const e = doc.createElementNS(SVGNS, tag); for (const [a, v] of Object.entries(at)) uiAttr(e, a, () => v); if (txt != null) uiProp(e, "textContent", () => txt); axes.append(e); };
          const fm = (v) => String(Math.round(v * 1000) / 1000);
          for (const v of tick(...g.xd)) { if (c.grid) add("line", { x1: g.X(v), x2: g.X(v), y1: T, y2: T + H, class: "grid" }); add("text", { x: g.X(v), y: T + H + 15, "text-anchor": "middle", "font-size": 11 }, fm(v)); }
          for (const v of tick(...g.yd)) { if (c.grid) add("line", { x1: L, x2: L + W, y1: g.Y(v), y2: g.Y(v), class: "grid" }); add("text", { x: L - 6, y: g.Y(v) + 4, "text-anchor": "end", "font-size": 11 }, fm(v)); }
          add("line", { x1: L, x2: L + W, y1: T + H, y2: T + H, class: "base" }); add("line", { x1: L, x2: L, y1: T, y2: T + H, class: "base" });
          if (c.xlabel) add("text", { x: L + W / 2, y: T + H + 31, "text-anchor": "middle", "font-size": 12 }, template(c.xlabel, E));
          if (c.ylabel) add("text", { x: L, y: T - 2 < 8 ? 9 : T - 2, "text-anchor": "start", "font-size": 12 }, template(c.ylabel, E));
        });
        return g;
      }
      const top = { plot: false, X: (x) => x, Y: (y) => y };
      const inner = [];
      n.children.forEach((c) => make(c, svg, top, inner));
      U.push((E) => { for (const f of inner) f(E); refit(); });
      const RO = doc.defaultView && doc.defaultView.ResizeObserver;
      if (typeof RO === "function") { const ro = new RO(() => refit()); ro.observe(wrap); ctx.onDestroy(() => ro.disconnect()); }
      return wrap;
    }
    /* plot / chart (EvoUI v2). The svg is as wide as its box: the viewBox width is the box's width in px
       (measured at the first draw and on every resize through ResizeObserver; 360 without one), so ticks
       and labels stay 11–12 px at any width; the height is the plot's `height` (a chart: 220, 260 from
       480 px wide). Data tones are the data palette (accent → --viz-1, warning → --viz-2, success →
       --known, danger → --gap; evoui.css), chrome stays monochrome. A re-draw glides (tweenSvg).
       A chart keeps a render-local view (never in progress): the series its legend hides, the hovered
       point (`hover` mirrors it into a number state, at most 10 times a second), the scroll window's first
       point (`visible_points`), the hovered pie slice. Hover-only changes repaint an overlay, not the chart. */
    function plotNode(n, U) {
      const wrap = h("div", "evoui-plotwrap"), isChart = n.kind === "chart";
      const v = { w: 0, E: null, key: null, gid: "evoui-g" + (++GRAD_ID), hidden: new Set(), hover: -1, off: 0, end: true, slice: -1, paint: null, geo: null, warned: new Set() };
      const P = isChart ? chartShell(n, wrap, v, ctx.life(wrap)) : null;
      let stop = null;
      const draw = (E, glide) => {
        v.E = E;
        if (!v.w && wrap.isConnected) v.w = Math.round(wrap.clientWidth);
        const W = Math.max(160, v.w || 360), old = wrap.querySelector("svg.evoui-plot");
        let svg, extra = [];
        if (isChart) { svg = chart(n, E, v, P, W); if (!svg) { if (v.paint) v.paint(); return; } }
        else [svg, ...extra] = plot(n, E, W, v.gid);
        if (stop) { stop(); stop = null; }
        if (isChart) { if (old) old.replaceWith(svg); else P.box.prepend(svg); } else wrap.replaceChildren(svg, ...extra);
        if (old && glide) stop = tweenSvg(old, svg, moveMs());
        if (v.paint) v.paint();
      };
      v.redraw = (glide) => { if (!v.E) return; v.key = null; try { draw(v.E, glide); } catch (x) { fallback(x); } };
      U.push((E) => draw(E, true));
      const RO = doc.defaultView && doc.defaultView.ResizeObserver;
      if (typeof RO === "function") {          // stops observing once the plot is gone (unmounted, or a leaving ghost)
        const gone = ctx.life(wrap), ro = new RO((es) => { if (gone()) { ro.disconnect(); return; } const w = Math.round(es[es.length - 1].contentRect.width); if (w > 0 && w !== v.w) { v.w = w; v.redraw(false); } });
        ro.observe(wrap); ctx.onDestroy(() => ro.disconnect());
      }
      return wrap;
    }
    const svgEl = (s, tag, attrs, txt) => { const e = doc.createElementNS(SVGNS, tag); for (const [k, x] of Object.entries(attrs)) uiAttr(e, k, () => x); if (txt != null) uiProp(e, "textContent", () => txt); if (s) s.append(e); return e; };
    /* a vertical gradient of a tone's colour (stops use currentColor: the gradient element carries the tone class) */
    const gradient = (defs, id, tone, top, bottom) => {
      const g = svgEl(defs, "linearGradient", { id, x1: 0, y1: 0, x2: 0, y2: 1, class: tone });
      svgEl(g, "stop", { offset: 0, style: `stop-color:currentColor;stop-opacity:${top}` }); svgEl(g, "stop", { offset: 1, style: `stop-color:currentColor;stop-opacity:${bottom}` });
    };
    /* A plot keeps its v1 proportions (360 × height): at most 720 px wide (centred), the height grows with the
       width (height · W / 360, never below `height`, at most 560), the viewBox is the pixel size. */
    function plot(n, E, boxW, gid) {
      const H0 = Math.min(420, Math.max(160, n.height || 260)), W = Math.min(boxW, 720), H = Math.round(Math.min(560, Math.max(H0, H0 * W / 360))), pad = 22, sx = (W - 2 * pad) / (n.xmax - n.xmin), sy = (H - 2 * pad) / (n.ymax - n.ymin);
      const X = (x) => pad + (x - n.xmin) * sx, Y = (y) => H - pad - (y - n.ymin) * sy;
      const s = svgEl(null, "svg", { viewBox: `0 0 ${W} ${H}`, class: "evoui-plot" });
      const el = (tag, attrs) => svgEl(s, tag, attrs);
      const defs = el("defs", {}), grads = new Set();
      const num = (v) => { const r = typeof v === "string" ? evalExpr(v, E) : v; if (!isFinite(r)) fail("plot coordinate is not a number"); return r; };
      if (n.grid) { for (let x = Math.ceil(n.xmin); x <= n.xmax; x++) el("line", { x1: X(x), y1: Y(n.ymin), x2: X(x), y2: Y(n.ymax), class: "grid" }); for (let y = Math.ceil(n.ymin); y <= n.ymax; y++) el("line", { x1: X(n.xmin), y1: Y(y), x2: X(n.xmax), y2: Y(y), class: "grid" }); }
      if (n.xmin <= 0 && n.xmax >= 0) el("line", { x1: X(0), y1: Y(n.ymin), x2: X(0), y2: Y(n.ymax), class: "axis" });
      if (n.ymin <= 0 && n.ymax >= 0) el("line", { x1: X(n.xmin), y1: Y(0), x2: X(n.xmax), y2: Y(0), class: "axis" });
      for (const it of n.items) {
        const tone = "t-" + (it.tone || "default"), dash = it.dashed ? " dashed" : "";
        if (it.shape === "arrow" || it.shape === "segment") {
          const x1 = X(num(it.x1)), y1 = Y(num(it.y1)), x2 = X(num(it.x2)), y2 = Y(num(it.y2));
          el("line", { x1, y1, x2, y2, class: "stroke " + tone + dash });
          if (it.shape === "arrow") { const a = Math.atan2(y2 - y1, x2 - x1), L = 9; el("path", { d: `M${x2},${y2} L${x2 - L * Math.cos(a - 0.4)},${y2 - L * Math.sin(a - 0.4)} L${x2 - L * Math.cos(a + 0.4)},${y2 - L * Math.sin(a + 0.4)} Z`, class: "fill " + tone }); }
          if (it.label) { const t = el("text", { x: x2 + 6, y: y2 - 6, class: "lbl " + tone }); uiProp(t, "textContent", () => template(it.label, E)); }
        } else if (it.shape === "point") { const x = X(num(it.x)), y = Y(num(it.y)); el("circle", { cx: x, cy: y, r: 4.5, class: "fill dot " + tone }); if (it.label) { const t = el("text", { x: x + 7, y: y - 7, class: "lbl " + tone }); uiProp(t, "textContent", () => template(it.label, E)); } }
        else if (it.shape === "polyline") { const pts = evalExpr(it.points, E); if (!Array.isArray(pts)) fail("polyline points must be an array"); el("polyline", { points: pts.map(([x, y]) => `${X(x)},${Y(y)}`).join(" "), class: "stroke nofill " + tone + dash }); }
        else if (it.shape === "label") { const t = el("text", { x: X(num(it.x)), y: Y(num(it.y)), class: "lbl " + tone }); uiProp(t, "textContent", () => template(it.text, E)); }
        else if (it.shape === "text") { const t = el("text", { x: X(num(it.x)), y: Y(num(it.y)), "text-anchor": it.anchor || "middle", "dominant-baseline": "middle", class: `lbl ${it.size === "md" ? "z-md " : ""}${tone}` }); uiProp(t, "textContent", () => template(it.text, E)); }
        else if (it.shape === "rect") {
          const x = num(it.x), y = num(it.y), w = num(it.w), hh = num(it.h);
          const xa = X(Math.min(x, x + w)), xb = X(Math.max(x, x + w)), ya = Y(Math.max(y, y + hh)), yb = Y(Math.min(y, y + hh));
          const solid = it.fill && !it.dashed;          // a filled bar: the tone's colour fading top -> bottom (a dashed one is a soft zone)
          if (solid && !grads.has(tone)) { grads.add(tone); gradient(defs, `${gid}-${tone}`, tone, 0.74, 0.36); }
          const r = el("rect", { x: xa, y: ya, width: Math.max(0, xb - xa), height: Math.max(0, yb - ya), rx: solid ? 5 : 3, class: `shape ${tone}${it.fill ? (solid ? " solid" : " filled") : ""}${dash}` });
          if (solid) r.style.fill = `url(#${gid}-${tone}) currentColor`;      // the colour when the gradient is gone (a leaving ghost)
          if (it.label) { const t = el("text", { x: (xa + xb) / 2, y: (ya + yb) / 2, "text-anchor": "middle", "dominant-baseline": "middle", class: "lbl " + (solid ? "onbar" : tone) }); uiProp(t, "textContent", () => template(it.label, E)); }
        }
        else if (it.shape === "circle") {
          const cx = num(it.cx), cy = num(it.cy), r = Math.abs(num(it.r)) * sx;
          el("circle", { cx: X(cx), cy: Y(cy), r, class: `shape ${tone}${it.fill ? " filled" : ""}${dash}` });
          if (it.label) { const t = el("text", { x: X(cx), y: Y(cy), "text-anchor": "middle", "dominant-baseline": "middle", class: "lbl " + tone }); uiProp(t, "textContent", () => template(it.label, E)); }
        }
      }
      const out = [s];
      if (n.legend) { const lg = h("div", "evoui-legend"); for (const l of n.legend) { const i = h("span", "evoui-lg t-" + (l.tone || "default")); i.append(h("i"), uiNode(() => l.label)); lg.append(i); } out.push(lg); }
      return out;
    }
    /* The chart's persistent frame (built once; a re-draw swaps only the svg): y label, the box (svg +
       tooltip; pointer / keyboard / wheel live here), x label, the scroll row (`visible_points`), the legend,
       the caption. Pie: the box and a vertical legend side by side. */
    function chartShell(n, wrap, v, gone) {
      const cart = CARTESIAN.includes(n.chart), pieChart = n.chart === "pie";
      wrap.classList.add("evoui-chart"); wrap.dataset.chart = n.chart;
      const P = { yl: h("div", "evoui-text s-sm t-secondary evoui-ylabel"), main: h("div", "evoui-chartmain"), box: h("div", "evoui-chartbox"),
        tip: h("div", "evoui-tip"), xl: h("div", "evoui-text s-sm t-secondary evoui-xlabel"), legend: h("div", "evoui-legend"), cap: h("div", "evoui-text s-sm t-secondary"),
        scroll: null, range: null };
      P.tip.hidden = true; uiAttr(P.tip, "aria-hidden", () => "true"); P.box.append(P.tip);
      if (cart && n.visible_points) {                 // the scroll row exists only for a windowed chart (no stray range input otherwise)
        P.scroll = h("label", "evoui-ch-scroll"); P.range = h("input");
        P.range.type = "range"; P.range.min = "0"; P.range.step = "1"; uiAttr(P.range, "aria-label", uiValue(() => ui("拖动查看更多")));
        P.scroll.append(h("span", "", uiValue(() => ui("拖动查看更多"))), P.range); P.scroll.hidden = true;
      }
      if (pieChart) { P.main.classList.add("pie"); P.legend.classList.add("evoui-pielegend"); P.main.append(P.box, P.legend); wrap.append(P.yl, P.main, P.xl, P.cap); }
      else { P.main.append(P.box, P.xl, ...(P.scroll ? [P.scroll] : []), P.legend); wrap.append(P.yl, P.main, P.cap); }
      if (cart) {
        P.box.tabIndex = 0; uiAttr(P.box, "role", () => "group");
        uiAttr(P.box, "aria-label", uiValue(() => ui("图表：用 ← → 逐个查看数据点") + (n.visible_points ? ui("，Shift + 滚轮或拖动左右移动") : "")));
      }
      let hTimer = null, hPend = false, drag = null;
      const writeHover = () => { if (state[n.hover] === v.hover) return; state[n.hover] = v.hover; ctx.change({ lazy: true }); update(); };
      const pushHover = () => { if (hTimer) { hPend = true; return; } if (gone()) return; writeHover(); hTimer = setTimeout(() => { hTimer = null; if (hPend && !gone()) { hPend = false; pushHover(); } else hPend = false; }, 100); };
      const setHover = (i) => { if (i === v.hover) return; v.hover = i; if (v.paint) v.paint(); if (n.hover) pushHover(); };
      const scrollTo = (off) => { const g = v.geo; if (!g) return; off = Math.max(0, Math.min(g.N - g.win, off)); if (off === v.off) return; v.off = off; v.end = off === g.N - g.win; if (P.range) P.range.value = String(off); v.redraw(false); };
      if (P.range) P.range.oninput = () => scrollTo(Number(P.range.value));
      const idxAt = (ev) => {
        const g = v.geo, sv = P.box.querySelector("svg.evoui-plot"); if (!g || !sv) return -1;
        const r = sv.getBoundingClientRect(); if (!r.width) return -1;
        const px = (ev.clientX - r.left) * g.W / r.width, k = g.bar ? Math.floor((px - g.pl) / g.band) : g.win > 1 ? Math.round((px - g.pl - g.inset) / g.dx) : 0;
        return g.i0 + Math.max(0, Math.min(g.win - 1, k));
      };
      if (cart) {
        P.box.addEventListener("pointerdown", (ev) => {
          const g = v.geo; if (!g) return;
          if (g.win < g.N && ev.button === 0) { drag = { x: ev.clientX, off: v.off, moved: false }; try { P.box.setPointerCapture(ev.pointerId); } catch (x) { /* synthetic pointer */ } }
          setHover(idxAt(ev));
        });
        P.box.addEventListener("pointermove", (ev) => {
          const g = v.geo; if (!g) return;
          if (drag && ev.buttons) {
            const sv = P.box.querySelector("svg.evoui-plot"), w = sv ? sv.getBoundingClientRect().width : 0, d = w ? (ev.clientX - drag.x) * g.W / w : 0;
            if (Math.abs(d) > 4) drag.moved = true;
            if (drag.moved) { setHover(-1); scrollTo(drag.off - Math.round(d / ((g.bar ? g.band : g.dx) || 1))); return; }
          }
          setHover(idxAt(ev));
        });
        const endDrag = (ev) => {            // a drag that ended outside the box leaves no hover behind
          const was = drag; drag = null; if (!was) return;
          const r = P.box.getBoundingClientRect();
          if (ev.type === "pointercancel" || ev.clientX < r.left || ev.clientX > r.right || ev.clientY < r.top || ev.clientY > r.bottom) setHover(-1);
        };
        P.box.addEventListener("pointerup", endDrag); P.box.addEventListener("pointercancel", endDrag);
        P.box.addEventListener("pointerleave", () => { if (!drag) setHover(-1); });
        P.box.addEventListener("wheel", (ev) => {
          const g = v.geo; if (!g || g.win >= g.N) return;
          const d = Math.abs(ev.deltaX) > Math.abs(ev.deltaY) ? ev.deltaX : ev.shiftKey ? ev.deltaY : 0; if (!d) return;
          ev.preventDefault(); const sp = (g.bar ? g.band : g.dx) || 1; v.wheel = (v.wheel || 0) + d;
          const st = Math.trunc(v.wheel / sp); if (st) { v.wheel -= st * sp; scrollTo(v.off + st); }
        }, { passive: false });
        P.box.addEventListener("keydown", (ev) => {
          const g = v.geo; if (!g) return; let i = v.hover;
          if (ev.key === "ArrowRight" || ev.key === "ArrowLeft") i = i < 0 ? g.i1 - 1 : Math.max(0, Math.min(g.N - 1, i + (ev.key === "ArrowRight" ? 1 : -1)));
          else if (ev.key === "Home") i = 0; else if (ev.key === "End") i = g.N - 1; else if (ev.key === "Escape") i = -1; else return;
          ev.preventDefault();
          if (i >= 0 && (i < g.i0 || i >= g.i1)) scrollTo(i < g.i0 ? i : i - g.win + 1);
          setHover(i);
        });
        P.box.addEventListener("blur", () => setHover(-1));
      }
      if (pieChart) {                          // a slice or its legend item under the pointer / focus: that slice stands out
        const at = (ev) => { const t = ev.target && ev.target.closest ? ev.target.closest("[data-i]") : null; return t ? Number(t.dataset.i) : -1; };
        const hi = (i) => { if (i !== v.slice) { v.slice = i; if (v.paint) v.paint(); } };
        for (const el of [P.box, P.legend]) {
          el.addEventListener("pointerover", (ev) => hi(at(ev)));
          el.addEventListener("pointerleave", () => hi(-1));
          el.addEventListener("focusin", (ev) => hi(at(ev)));
          el.addEventListener("focusout", () => hi(-1));
        }
      }
      return P;
    }
    /* legend items: buttons that hide / show their series (at least one stays shown) when there are 2+ */
    function chartLegend(n, P, v) {
      const many = n.series.length > 1, act = doc.activeElement, foc = P.legend.contains(act) ? [...P.legend.children].indexOf(act) : -1;
      P.legend.replaceChildren(...n.series.map((sr, si) => {
        const on = !v.hidden.has(si), b = h(many ? "button" : "span", "evoui-lg " + SERIES_TONES[si % 4] + (on ? "" : " off"));
        b.append(h("i"), uiNode(() => sr.label));
        if (many) {
          b.type = "button"; uiAttr(b, "aria-pressed", () => String(on)); uiProp(b, "title", uiValue(() => on ? ui("点一下隐藏这条") : ui("点一下显示这条")));
          b.onclick = () => { if (on && n.series.length - v.hidden.size <= 1) return; if (on) v.hidden.add(si); else v.hidden.delete(si); v.redraw(true); };
        }
        return b;
      }));
      if (foc >= 0 && P.legend.children[foc]) P.legend.children[foc].focus();
    }
    function chart(n, E, v, P, W) {
      const data = evalExpr(n.data, E); if (!Array.isArray(data) || !data.length) fail("chart data must be a non-empty array");
      const texts = [n.y_label, n.x_label, n.caption].map((t) => (t == null ? null : template(t, E)));
      const ann = (n.annotations || []).map((a) => { const ax = has(a, "x") ? "x" : "y"; return { ax, v: evalExpr(a[ax], E), label: template(a.label, E), tone: "t-" + (a.tone || "secondary") }; });
      const key = JSON.stringify([data, texts, ann, W, [...v.hidden].sort(), v.end ? -1 : v.off]);
      if (key === v.key && P.box.querySelector("svg.evoui-plot")) return null;
      v.key = key;
      [[P.yl, texts[0]], [P.xl, texts[1]], [P.cap, texts[2]]].forEach(([el, t]) => { el.hidden = t == null; uiProp(el, "textContent", () => t ?? ""); });
      if (n.chart === "pie") return pie(n, data, v, P);
      chartLegend(n, P, v);
      return n.chart === "scatter" ? scatter(n, data, W, v, P) : cartesian(n, data, W, v, P, ann);
    }
    /* y axis (ticks + grid) inside one <g> (its tick count may change: it jumps, the data glides) */
    function yAxis(s, sc, Y, pl, x2) {
      const g = svgEl(s, "g", { class: "axes" });
      sc.ticks.forEach((t) => { svgEl(g, "line", { x1: pl, y1: r2(Y(t)), x2, y2: r2(Y(t)), class: "grid" }); svgEl(g, "text", { x: pl - 8, y: r2(Y(t) + 4), class: "tick end" }, numText(t, sc.dec)); });
      return g;
    }
    function cartesian(n, data, W, v, P, ann) {
      const bar = n.chart === "bar", area = n.chart === "area", N = data.length, curve = n.curve || "linear";
      const win = isInt(n.visible_points) && N > n.visible_points ? n.visible_points : N;
      if (v.end) v.off = N - win; v.off = Math.max(0, Math.min(N - win, v.off)); v.end = v.off === N - win;
      const i0 = v.off, i1 = i0 + win;
      if (P.scroll) { P.scroll.hidden = win >= N; P.range.max = String(N - win); P.range.value = String(i0); }
      const H = W < 480 ? 220 : 260, pt = 12, pb = 26, pr = 14;
      const xs = data.map((d) => member(d, n.x));
      const vis = n.series.map((sr, si) => ({ key: sr.key, label: sr.label, si, tone: SERIES_TONES[si % 4] })).filter((s) => !v.hidden.has(s.si));
      const raw = (d, s) => { const x = Number(member(d, s.key)); return isFinite(x) ? x : null; };
      const lay = vis.map(() => new Array(N));           // [bottom | null, top] per visible series and point (stacked: running totals)
      for (let i = 0; i < N; i++) {
        let pos = 0, neg = 0;
        vis.forEach((s, k) => {
          const y = raw(data[i], s);
          if (!n.stack) { lay[k][i] = y == null ? null : [null, y]; return; }
          const val = y ?? 0; if (bar && val < 0) { lay[k][i] = [neg, neg + val]; neg += val; } else { lay[k][i] = [pos, pos + val]; pos += val; }
        });
      }
      const ys = []; lay.forEach((L) => L.forEach((p) => { if (p) { ys.push(p[1]); if (p[0] != null) ys.push(p[0]); } }));
      ann.forEach((a) => { if (a.ax === "y" && isNum(a.v)) ys.push(a.v); });
      const fixed = isNum(n.y_min), sc = niceScale(fixed ? n.y_min : Math.min(0, ...ys), Math.max(fixed ? n.y_min : 0, ...ys), fixed), lo = sc.lo, hi = sc.hi;
      const pl = Math.max(30, 12 + Math.max(...sc.ticks.map((t) => textW(numText(t, sc.dec)))));
      const plotW = W - pl - pr, band = plotW / win, inset = bar ? 0 : Math.min(10, plotW / 20), dx = win > 1 ? (plotW - 2 * inset) / (win - 1) : 0;
      const X = bar ? (i) => pl + (i - i0 + 0.5) * band : (i) => (win > 1 ? pl + inset + (i - i0) * dx : pl + plotW / 2);
      const Y = (y) => Math.max(pt - 2, Math.min(H - pb, H - pb - (y - lo) / (hi - lo) * (H - pb - pt)));
      const base = Y(Math.max(lo, Math.min(hi, 0)));
      const s = svgEl(null, "svg", { viewBox: `0 0 ${W} ${H}`, class: "evoui-plot evoui-ch" + (win < N ? " windowed" : ""), "aria-hidden": "true" });
      const defs = svgEl(s, "defs", {});
      vis.forEach((sr) => { if (bar) gradient(defs, `${v.gid}-${sr.si}`, sr.tone, 1, 0.72); else if (area && n.gradient) gradient(defs, `${v.gid}-${sr.si}`, sr.tone, 0.36, 0.02); });
      yAxis(s, sc, Y, pl, W - pr);
      svgEl(s, "line", { x1: pl, y1: r2(base), x2: W - pr, y2: r2(base), class: "axis" });
      const under = svgEl(s, "g", { class: "under" });
      const range = []; for (let i = i0; i < i1; i++) range.push(i);
      if (bar) {
        const gw = Math.min(band * (n.stack ? 0.62 : 0.74), n.stack ? 64 : 64 * vis.length), bw = n.stack ? gw : gw / Math.max(1, vis.length), gap = !n.stack && vis.length > 1 ? Math.min(3, bw * 0.15) : 0;
        const tops = range.map((i) => { let t = -1; vis.forEach((sr, k) => { const p = lay[k][i]; if (p && p[1] > (p[0] ?? 0)) t = k; }); return t; });
        vis.forEach((sr, k) => range.forEach((i, j) => {
          const p = lay[k][i], x = X(i) - gw / 2 + (n.stack ? 0 : k * bw) + gap / 2;
          let yTop = base, yBot = base, up = true;              // a missing value: a zero-height bar (the shapes stay the same for the glide)
          if (p) { const b0 = p[0] == null ? Y(Math.max(lo, Math.min(hi, 0))) : Y(p[0]), t = Y(p[1]); yTop = Math.min(t, b0); yBot = Math.max(t, b0); up = t <= b0; }
          const round = up && (!n.stack || tops[j] === k) ? Math.min(6, (bw - gap) / 3) : 0;
          const e = svgEl(s, "path", { d: barPath(x, yTop, Math.max(0, bw - gap), Math.max(0, yBot - yTop), round), class: "bar " + sr.tone + (n.stack ? " stacked" : "") });
          e.style.fill = `url(#${v.gid}-${sr.si}) currentColor`;
        }));
      } else {
        const pts = (k) => range.filter((i) => lay[k][i]).map((i) => [X(i), Y(lay[k][i][1])]);
        vis.forEach((sr, k) => {
          const P1 = pts(k); if (!P1.length) return;
          if (area) {
            const lowP = range.filter((i) => lay[k][i]).map((i) => [X(i), lay[k][i][0] == null ? base : Y(lay[k][i][0])]);
            const a = svgEl(s, "path", { d: curvePath(P1, curve) + curveBack(lowP, curve) + "Z", class: "area " + sr.tone + (n.gradient ? " grad" : "") });
            if (n.gradient) a.style.fill = `url(#${v.gid}-${sr.si}) currentColor`;
          }
          svgEl(s, "path", { d: curvePath(P1, curve), class: "stroke line " + sr.tone });
          if (!area && win <= 16 && dx >= 22) P1.forEach((p) => svgEl(s, "circle", { cx: r2(p[0]), cy: r2(p[1]), r: 3, class: "fill dot pt " + sr.tone }));
        });
      }
      const ag = svgEl(s, "g", { class: "annots" });
      const xIndex = (val) => { let k = xs.findIndex((x) => x === val || (x != null && val != null && typeof val !== "object" && String(x) === String(val))); if (k < 0 && isInt(val) && val >= 0 && val < N) k = val; return k; };
      ann.forEach((a) => {
        const warnOnce = (m) => { if (!v.warned.has(m)) { v.warned.add(m); console.warn("EvoUI chart: " + m); } };
        if (a.ax === "y") {
          if (!isNum(a.v)) return warnOnce(`annotation y ${JSON.stringify(a.v)} is not a number; ignored`);
          const y = r2(Y(a.v)); svgEl(ag, "line", { x1: pl, y1: y, x2: W - pr, y2: y, class: "ann " + a.tone });
          if (a.label) svgEl(ag, "text", { x: W - pr - (bar ? 2 : 22), y: y - 6, class: "annlbl end " + a.tone }, a.label);   // clear of the last-point dot
          return;
        }
        const i = xIndex(a.v); if (i < 0) return warnOnce(`annotation x ${JSON.stringify(a.v)} matches no data point; ignored`);
        if (i < i0 || i >= i1) return;
        const x = r2(X(i)); svgEl(ag, "line", { x1: x, y1: pt - 4, x2: x, y2: H - pb, class: "ann " + a.tone });
        if (a.label) { const right = x > W - pr - Math.min(140, textW(a.label, 11.5) + 8); svgEl(ag, "text", { x: right ? x - 5 : x + 5, y: pt + 8, class: "annlbl " + (right ? "end " : "") + a.tone }, a.label); }
      });
      const xg = svgEl(s, "g", { class: "xticks" }), labW = Math.max(...range.map((i) => textW(String(xs[i] ?? "")))) + 12;
      const every = Math.max(1, Math.ceil(labW / Math.max(1, bar ? band : dx || plotW)));
      range.forEach((i) => { if (i % every) return; const t = String(xs[i] ?? ""), w = textW(t); svgEl(xg, "text", { x: r2(Math.max(w / 2 + 2, Math.min(W - w / 2 - 2, X(i)))), y: H - 8, class: "tick mid" }, t); });
      const ov = svgEl(s, "g", { class: "ov" });
      v.geo = { bar, i0, i1, win, N, W, pl, band, inset, dx };
      v.paint = () => {
        under.replaceChildren(); ov.replaceChildren();
        const i = v.hover, T = P.tip;
        if (i >= i0 && i < i1) {
          if (bar) svgEl(under, "rect", { x: r2(X(i) - band / 2), y: pt - 4, width: r2(band), height: H - pb - pt + 4, rx: 4, class: "band" });
          else svgEl(ov, "line", { x1: r2(X(i)), y1: pt - 4, x2: r2(X(i)), y2: H - pb, class: "cursor" });
          if (!bar) vis.forEach((sr, k) => { const p = lay[k][i]; if (p) svgEl(ov, "circle", { cx: r2(X(i)), cy: r2(Y(p[1])), r: 4.5, class: "fill dot hov " + sr.tone }); });
          T.replaceChildren(h("div", "evoui-tip-x", String(xs[i] ?? "")));
          vis.forEach((sr) => { const y = raw(data[i], sr), r = h("div", "evoui-tip-row " + sr.tone); r.append(h("i"), h("span", "", sr.label), h("b", "", y == null ? "–" : numText(y))); T.append(r); });
          T.hidden = false;
          const sv = P.box.querySelector("svg.evoui-plot"), rw = sv ? sv.getBoundingClientRect().width : 0, k = rw ? rw / W : 1;
          const bw = P.box.clientWidth, tw = T.offsetWidth, x = X(i) * k;
          let left = x + 14; if (left + tw > bw - 4) left = x - 14 - tw;
          T.style.left = Math.max(4, Math.min(bw - tw - 4, left)) + "px";
        } else {
          T.hidden = true;
          if (!bar && i1 === N) vis.forEach((sr, k) => { const p = lay[k][N - 1]; if (!p) return; svgEl(ov, "circle", { cx: r2(X(N - 1)), cy: r2(Y(p[1])), r: 8, class: "fill halo " + sr.tone }); svgEl(ov, "circle", { cx: r2(X(N - 1)), cy: r2(Y(p[1])), r: 4, class: "fill dot last " + sr.tone }); });
        }
      };
      return s;
    }
    function scatter(n, data, W, v, P) {
      const H = W < 480 ? 220 : 260, pb = 26, pt = 12, pr = 14, s = svgEl(null, "svg", { viewBox: `0 0 ${W} ${H}`, class: "evoui-plot evoui-ch", "aria-hidden": "true" });
      svgEl(s, "defs", {});
      const vis = n.series.map((sr, si) => ({ key: sr.key, si, tone: SERIES_TONES[si % 4] })).filter((x) => !v.hidden.has(x.si));
      const xs = data.map((d) => Number(member(d, n.x))), ys = data.flatMap((d) => vis.map((sr) => Number(member(d, sr.key))));
      const fx = xs.filter(isFinite), fy = ys.filter(isFinite); if (!fx.length || !fy.length) fail("scatter needs numbers");
      const fixed = isNum(n.y_min), sy = niceScale(fixed ? n.y_min : Math.min(0, ...fy), Math.max(fixed ? n.y_min : 0, ...fy), fixed), sxs = niceScale(Math.min(...fx), Math.max(...fx), false);
      const pl = Math.max(30, 12 + Math.max(...sy.ticks.map((t) => textW(numText(t, sy.dec)))));
      const X = (x) => pl + (x - sxs.lo) / (sxs.hi - sxs.lo) * (W - pl - pr), Y = (y) => Math.max(pt - 2, Math.min(H - pb, H - pb - (y - sy.lo) / (sy.hi - sy.lo) * (H - pb - pt)));
      yAxis(s, sy, Y, pl, W - pr);
      svgEl(s, "line", { x1: pl, y1: H - pb, x2: W - pr, y2: H - pb, class: "axis" });
      const xg = svgEl(s, "g", { class: "xticks" }); sxs.ticks.forEach((t) => { const tx = numText(t, sxs.dec), w = textW(tx); svgEl(xg, "text", { x: r2(Math.max(w / 2 + 2, Math.min(W - w / 2 - 2, X(t)))), y: H - 8, class: "tick mid" }, tx); });
      vis.forEach((sr) => data.forEach((d) => { const x = Number(member(d, n.x)), y = Number(member(d, sr.key)); if (isFinite(x) && isFinite(y)) svgEl(s, "circle", { cx: r2(X(x)), cy: r2(Y(y)), r: 4, class: "fill dot sc " + sr.tone }); }));
      v.paint = null; v.geo = null;
      return s;
    }
    function pie(n, data, v, P) {
      const S = 200, R = 92, c = 100, ri = n.donut ? 60 : 0, s = svgEl(null, "svg", { viewBox: `0 0 ${S} ${S}`, class: "evoui-plot pie" + (n.donut ? " donut" : "") });
      const key = n.series[0].key, vals = data.map((d) => Math.max(0, Number(member(d, key)) || 0)), total = vals.reduce((a, b) => a + b, 0);
      if (!(total > 0)) fail("pie values must add up to more than 0");
      const tones = ["t-accent", "t-warning", "t-success", "t-danger", "t-secondary", "t-default"];
      const pc = (a, r) => pxy([c + r * Math.cos(a), c + r * Math.sin(a)]);
      let a0 = -Math.PI / 2; const slices = [], items = [];
      vals.forEach((x, i) => {
        const tone = tones[i % tones.length], name = String(member(data[i], n.x)), pct = fmt(100 * x / total, 0) + "%";
        let a1 = a0 + x / total * 2 * Math.PI; const full = a1 - a0 >= 2 * Math.PI - 1e-9; if (full) a1 = a0 + 2 * Math.PI - 1e-4;
        if (x > 0) {
          const big = a1 - a0 > Math.PI ? 1 : 0;
          const d = ri ? `M${pc(a0, R)}A${R},${R} 0 ${big} 1 ${pc(a1, R)}L${pc(a1, ri)}A${ri},${ri} 0 ${big} 0 ${pc(a0, ri)}Z` : `M${c},${c}L${pc(a0, R)}A${R},${R} 0 ${big} 1 ${pc(a1, R)}Z`;
          slices.push(svgEl(s, "path", { d, class: "fill slice " + tone, "data-i": i }));
        }
        const it = h("span", "evoui-lg " + tone); it.dataset.i = String(i); it.tabIndex = 0; uiAttr(it, "role", () => "button"); uiAttr(it, "aria-label", () => `${name} ${pct}`);
        it.append(h("i"), h("span", "evoui-lg-name", name), h("span", "evoui-lg-pct", pct)); items.push([it, name, pct]);
        a0 = a1;
      });
      const mid = ri ? [svgEl(s, "text", { x: c, y: c - 2, class: "pie-big" }), svgEl(s, "text", { x: c, y: c + 18, class: "pie-sub" })] : null;
      P.legend.replaceChildren(...items.map((x) => x[0]));
      v.geo = null;
      v.paint = () => {
        const k = v.slice;
        slices.forEach((p) => p.classList.toggle("dim", k >= 0 && Number(p.dataset.i) !== k));
        items.forEach(([it], i) => { it.classList.toggle("hi", i === k); it.classList.toggle("dim", k >= 0 && i !== k); });
        if (mid) { uiProp(mid[0], "textContent", () => k >= 0 ? items[k][2] : numText(total)); uiProp(mid[1], "textContent", uiValue(() => k >= 0 ? items[k][1] : ui("合计"))); }
      };
      return s;
    }
    /* The list quiz (layout absent or "list": one fieldset per question, radio options — checkboxes for
       a `multi` question — and one 「检查答案」). Progress {answers: [option index | sorted index list
       (multi) | null per question], checked, attempt (presses that judged a changed set of answers),
       done (sticky)} plus, when a question has a `hint`, hints / hintUsed ([bool per question]: open
       now / opened during this attempt). A press after a change starts a new attempt: an item event per
       question + one summary; an unchanged re-press only repeats onQuizResult (as before). latencyMs /
       timeMs count from the start of the attempt (the quiz shown, or the previous judged press).
       onQuizResult / results() carry single-answer questions only (their `chosen` is one option). */
    function quiz(n) {
      if (n.layout === "paged") return pagedQuiz(n);
      const key = ctx.nodeKey(n), qn = QUIZ_NO.get(n) ?? 0, hinted = n.questions.some((q) => has(q, "hint"));
      const p = ctx.nodeProgress(key, () => Object.assign({ answers: n.questions.map(() => null), checked: false, attempt: 0, done: false },
        hinted ? { hints: n.questions.map(() => false), hintUsed: n.questions.map(() => false) } : {}), (r) => quizProgressFits(n, r));
      if (hinted && !Array.isArray(p.hints)) { p.hints = n.questions.map(() => false); p.hintUsed = n.questions.map(() => false); }
      let shown = ctx.now();
      const d = h("div", "evoui-quiz"), uid = Math.random().toString(36).slice(2, 7);
      if (n.title) d.append(h("div", "evoui-quiz-title", n.title));
      const boxes = n.questions.map((q, qi) => {
        const box = h("fieldset", "q"), multi = q.type === "multi"; box.append(h("legend", "", `${qi + 1}. ${q.q}`));
        let hint = null;
        if (has(q, "hint")) {
          const hb = h("button", "evoui-hintbtn"); hb.type = "button"; hint = { hb, text: h("div", "evoui-hint", q.hint) };
          hb.onclick = () => { p.hints[qi] = !p.hints[qi]; if (p.hints[qi]) p.hintUsed[qi] = true; ctx.change(); paint(); };
          box.append(hb, hint.text);
        }
        const rows = q.options.map((o, oi) => {
          const l = h("label", "opt"), r = h("input"); r.type = multi ? "checkbox" : "radio"; r.name = `q${qi}-${uid}`;
          r.checked = multi ? (p.answers[qi] || []).includes(oi) : p.answers[qi] === oi;
          r.onchange = () => {
            if (multi) { const s = new Set(p.answers[qi] || []); if (r.checked) s.add(oi); else s.delete(oi); p.answers[qi] = s.size ? [...s].sort((a, b) => a - b) : null; }
            else p.answers[qi] = oi;
            p.checked = false; ctx.change(); paint(); };
          l.append(r, uiNode(() => o)); box.append(l);
          const fb = has(q, "feedback") ? h("div", "evoui-optfb", q.feedback[oi]) : null; if (fb) box.append(fb);
          return { l, fb, r };
        });
        const why = h("div", "evoui-text s-sm"); box.append(why); d.append(box); return { q, rows, why, hint };
      });
      const b = h("button", "evoui-check", uiValue(() => ui("检查答案"))); b.type = "button"; b.onclick = () => { const fresh = !p.checked; p.checked = true; paint();
        const single = n.questions.map((q, qi) => [q, qi]).filter(([q]) => q.type !== "multi");
        single.forEach(([, qi]) => answered.set(qn + "." + qi, { quiz: qn, question: qi, chosen: p.answers[qi] }));
        if (typeof opts.onQuizResult === "function") single.forEach(([q, qi]) => { try { opts.onQuizResult({ quiz: qn, question: qi, chosen: p.answers[qi], correct: p.answers[qi] === q.correct, case: q.case ?? null, cell: q.cell ?? null }); } catch (x) { console.error(x); } });
        if (!fresh) return;
        p.attempt += 1; p.done = true; ctx.change();
        const ms = Math.max(0, Math.round(ctx.now() - shown)), missed = []; let hintsUsed = 0;
        n.questions.forEach((q, qi) => { const correct = quizRight(q, p.answers[qi]), hintUsed = !!(p.hintUsed && p.hintUsed[qi]); if (!correct) missed.push(qi); if (hintUsed) hintsUsed += 1;
          ctx.emit({ type: "item", node: key, kind: "quiz", item: qi, attempt: p.attempt, response: p.answers[qi], correct, hintUsed, latencyMs: ms }); });
        ctx.emit({ type: "summary", node: key, kind: "quiz", attempt: p.attempt, total: n.questions.length, right: n.questions.length - missed.length, missed, timeMs: ms, hintsUsed });
        shown = ctx.now();                                       // the next attempt starts now
        if (p.hintUsed) { p.hintUsed = p.hints.slice(); ctx.change(); }   // a hint still open counts for the next attempt too
        ctx.markDone(key); }; d.append(b);
      const summary = h("div", "evoui-quizsum"); d.append(summary);
      function paint() {
        const checked = p.checked, answers = p.answers;
        b.disabled = answers.some((a) => a === null);
        boxes.forEach(({ q, rows, why, hint }, qi) => {
          const chosen = (oi) => (q.type === "multi" ? (answers[qi] || []).includes(oi) : answers[qi] === oi);
          const isKey = (oi) => (q.type === "multi" ? q.correct.includes(oi) : q.correct === oi);
          rows.forEach(({ l, fb, r }, oi) => {
            if (r.checked !== chosen(oi)) r.checked = chosen(oi);      // the inputs always show the progress
            l.classList.toggle("right", checked && chosen(oi) && isKey(oi)); l.classList.toggle("wrong", checked && chosen(oi) && !isKey(oi));
            l.classList.toggle("missed", checked && q.type === "multi" && !chosen(oi) && isKey(oi));
            if (fb) fb.hidden = !(checked && chosen(oi));
          });
          const ok = quizRight(q, answers[qi]);
          why.hidden = !checked; why.className = `evoui-text s-sm ${ok ? "t-success" : "t-danger"}`; uiProp(why, "textContent", uiValue(() => checked ? (ok ? ui("对。") : ui("不对。")) + q.why : ""));
          if (hint) { const open = !!p.hints[qi]; hint.text.hidden = !open; uiProp(hint.hb, "textContent", uiValue(() => open ? ui("收起提示") : ui("提示"))); uiAttr(hint.hb, "aria-expanded", () => String(open)); }
        });
        summary.hidden = !checked; summary.replaceChildren();
        if (!checked) return;
        const right = n.questions.filter((q, qi) => quizRight(q, answers[qi])).length;
        summary.append(h("div", "evoui-text w-bold", uiValue(() => ui("答对 {0} / {1}", [right, n.questions.length]))));
        for (const [text, ok] of caseLines(n, n.questions.map((_, qi) => qi), (qi) => quizRight(n.questions[qi], answers[qi])))
          summary.append(h("div", `evoui-text s-sm ${ok ? "t-success" : "t-warning"}`, text));
      }
      paint(); if (p.done) ctx.markDone(key, { restored: true }); return d;
    }
    /* The paged quiz (layout "paged"; ChatGPT LearningQuiz): one question per page in a rounded card —
       the stem on the left, 「‹ n/N ›」 and a hint toggle on the right, option rows with a round
       indicator (letter for single / tf, check for multi). single / tf are judged on click, multi on
       「提交答案」; a judged page locks and shows the feedback of the chosen and the right options; ›
       only after answering, ‹ reviews, the hint closes on page change. After the last page a results
       page (score, one row per question) with 「重新开始」, 「只做错题」 (both attempt + 1) and 「再来一套」
       (a request event). Progress {layout: "paged", phase: "quiz" | "results", attempt (≥ 1),
       order: [question indices of this attempt], current (position in order), answers: [option index |
       sorted index list (multi; also an unsubmitted pick) | null per question], judged: [bool],
       hintOpen, hintUsed: [bool], seenMs: [ms each question was visible before its answer], done
       (sticky: an attempt was finished), celebrated / requested (the attempt that got confetti / asked
       for more; 0 = none)}. Each judged question: one item event (latencyMs = seenMs) and, for a
       single-answer question, onQuizResult as in the list quiz; the last one of an attempt: a summary
       (timeMs = the attempt's seenMs summed). A perfect attempt throws confetti once, on reaching the
       results page by a press — never on restore. */
    function pagedQuiz(n) {
      const key = ctx.nodeKey(n), qn = QUIZ_NO.get(n) ?? 0, Q = n.questions;
      const fresh = (attempt, order, done) => ({ layout: "paged", phase: "quiz", attempt, order, current: 0, answers: Q.map(() => null), judged: Q.map(() => false),
        hintOpen: false, hintUsed: Q.map(() => false), seenMs: Q.map(() => 0), done, celebrated: 0, requested: 0 });
      const p = ctx.nodeProgress(key, () => fresh(1, Q.map((_, i) => i), false), (r) => pagedProgressFits(n, r));
      let since = ctx.now();                                     // when the current page became visible
      const root = h("div", "evoui-pq"); uiAttr(root, "role", () => "group"); uiAttr(root, "aria-label", uiValue(() => n.title || ui("测验")));
      const view = h("div", "evoui-pq-view"), live = h("div", "evoui-sr"); uiAttr(live, "aria-live", () => "polite"); root.append(view, live);
      const cur = () => p.order[p.current];
      const finished = () => p.order.every((i) => p.judged[i]);
      const icon = (d, cls) => { const s = doc.createElementNS(SVGNS, "svg"); uiAttr(s, "viewBox", () => "0 0 16 16"); uiAttr(s, "aria-hidden", () => "true"); if (cls) uiAttr(s, "class", () => cls); const pa = doc.createElementNS(SVGNS, "path"); uiAttr(pa, "d", () => d); s.append(pa); return s; };
      const btn = (cls, label, f, fn) => { const b = h("button", cls, label); b.type = "button"; b.dataset.f = f; b.onclick = fn; return b; };
      function account() { const t = ctx.now(), i = cur(); if (p.phase === "quiz" && !p.judged[i]) p.seenMs[i] += Math.max(0, Math.round(t - since)); since = t; }
      function judge(i, response) {
        if (p.phase !== "quiz" || p.judged[i] || response === null) return;
        account(); p.answers[i] = response; p.judged[i] = true;
        const q = Q[i], correct = quizRight(q, response), end = finished(); if (end) p.done = true;
        ctx.change();
        if (q.type !== "multi") {
          answered.set(qn + "." + i, { quiz: qn, question: i, chosen: response });
          if (typeof opts.onQuizResult === "function") { try { opts.onQuizResult({ quiz: qn, question: i, chosen: response, correct, case: q.case ?? null, cell: q.cell ?? null }); } catch (x) { console.error(x); } }
        }
        ctx.emit({ type: "item", node: key, kind: "quiz", item: i, attempt: p.attempt, response, correct, hintUsed: p.hintUsed[i], latencyMs: p.seenMs[i] });
        if (end) {
          const missed = p.order.filter((j) => !quizRight(Q[j], p.answers[j]));
          ctx.emit({ type: "summary", node: key, kind: "quiz", attempt: p.attempt, total: p.order.length, right: p.order.length - missed.length, missed,
            timeMs: p.order.reduce((s, j) => s + p.seenMs[j], 0), hintsUsed: p.order.filter((j) => p.hintUsed[j]).length });
          ctx.markDone(key);
        }
        uiProp(live, "textContent", uiValue(() => correct ? ui("答对了") : ui("答错了")));
        draw(false, "next");
      }
      function go(pos) {
        if (p.phase !== "quiz" || pos < 0) return;
        if (pos > p.current && !p.judged[cur()]) return;          // › only after answering
        account(); p.hintOpen = false;
        if (pos >= p.order.length && !finished()) pos = p.order.findIndex((j) => !p.judged[j]);   // results only when all are judged
        if (pos >= p.order.length) {
          p.phase = "results"; const perfect = p.order.every((j) => quizRight(Q[j], p.answers[j])), cheer = perfect && p.celebrated !== p.attempt;
          if (cheer) p.celebrated = p.attempt;
          ctx.change(); draw(true, "restart"); if (cheer) celebrate(doc); return;
        }
        p.current = pos; ctx.change(); draw(true);
      }
      function restart(onlyWrong) {
        const wrong = p.order.filter((j) => !quizRight(Q[j], p.answers[j]));
        Object.assign(p, fresh(p.attempt + 1, onlyWrong && wrong.length ? wrong : Q.map((_, i) => i), p.done));
        since = ctx.now(); uiProp(live, "textContent", () => ""); ctx.change(); draw(true, "opt-0");
      }
      function more() { if (p.requested === p.attempt) return; p.requested = p.attempt; ctx.change(); ctx.emit({ type: "request", node: key, what: "more" }); draw(false, "restart"); }
      function draw(animate, focus) {
        const had = root.contains(doc.activeElement) && doc.activeElement.dataset ? doc.activeElement.dataset.f : null;
        const el = p.phase === "results" ? resultsPage() : quizPage();
        if (animate) el.classList.add("evoui-enter");
        view.replaceChildren(el);
        const want = (had || animate) ? [had, focus, "opt-0", "next", "restart"] : [];   // keep keyboard focus in the card
        for (const f of want) { const t = f && el.querySelector(`[data-f="${f}"]`); if (t && !t.disabled) { t.focus({ preventScroll: true }); break; } }
      }
      function quizPage() {
        const i = cur(), q = Q[i], multi = q.type === "multi", judged = p.judged[i], a = p.answers[i];
        const page = h("div", "evoui-pq-page");
        const head = h("div", "evoui-pq-head"), stem = h("div", "evoui-pq-stem" + (p.hintOpen ? " hinted" : ""));
        stem.append(h("div", "evoui-pq-q", q.q));
        if (p.hintOpen && has(q, "hint")) stem.append(h("div", "evoui-pq-hint", q.hint));
        const tools = h("div", "evoui-pq-tools"), nav = h("div", "evoui-pq-nav");
        const prev = btn("evoui-pq-icon", null, "prev", () => go(p.current - 1)); prev.append(icon("M10 3.5 5.5 8l4.5 4.5")); uiAttr(prev, "aria-label", uiValue(() => ui("上一题"))); prev.disabled = p.current === 0;
        const next = btn("evoui-pq-icon", null, "fwd", () => go(p.current + 1)); next.append(icon("M6 3.5 10.5 8 6 12.5")); uiAttr(next, "aria-label", uiValue(() => p.current === p.order.length - 1 ? ui("查看结果") : ui("下一题"))); next.disabled = !judged;
        nav.append(prev, h("span", "evoui-pq-count", `${p.current + 1}/${p.order.length}`), next); tools.append(nav);
        if (has(q, "hint")) {
          const hb = btn("evoui-pq-icon evoui-pq-hintbtn" + (p.hintOpen ? " on" : ""), null, "hint", () => { p.hintOpen = !p.hintOpen; if (p.hintOpen && !p.judged[cur()]) p.hintUsed[cur()] = true; ctx.change(); draw(false); });
          hb.append(icon("M8 1.9a4.3 4.3 0 0 0-2.55 7.77c.47.35.75.86.75 1.42v.66h3.6v-.66c0-.56.28-1.07.75-1.42A4.3 4.3 0 0 0 8 1.9ZM6.4 14.1h3.2"));
          uiAttr(hb, "aria-label", uiValue(() => p.hintOpen ? ui("收起提示") : ui("提示"))); uiAttr(hb, "aria-pressed", () => String(p.hintOpen)); uiProp(hb, "title", uiValue(() => ui("提示")));
          const util = h("div", "evoui-pq-util"); util.append(hb); tools.append(util);
        }
        head.append(stem, tools); if (n.title) page.append(h("div", "evoui-pq-title", n.title)); page.append(head);
        const list = h("div", "evoui-pq-opts"); uiAttr(list, "role", () => "group"); uiAttr(list, "aria-label", () => q.q);
        const chosen = (oi) => (multi ? (a || []).includes(oi) : a === oi), isKey = (oi) => (multi ? q.correct.includes(oi) : q.correct === oi);
        q.options.forEach((o, oi) => {
          const sel = chosen(oi), right = isKey(oi);
          const mark = !judged ? (sel ? "sel" : "") : right ? (multi && !sel ? "missed" : "right") : sel ? "wrong" : "off";
          const row = btn("evoui-pq-opt", null, "opt-" + oi, () => {
            if (p.judged[i]) return;
            if (!multi) return judge(i, oi);
            const s = new Set(p.answers[i] || []); if (s.has(oi)) s.delete(oi); else s.add(oi);
            p.answers[i] = s.size ? [...s].sort((x, y) => x - y) : null; ctx.change(); draw(false);
          });
          if (mark) row.dataset.mark = mark; row.disabled = judged;
          if (multi) uiAttr(row, "aria-pressed", () => String(sel));
          const ind = h("span", "evoui-pq-ind");
          if (!multi) uiProp(ind, "textContent", () => String.fromCharCode(65 + oi));
          else if (mark === "sel" || mark === "right" || mark === "missed") ind.append(icon(ICON_PATHS.check));
          else if (mark === "wrong") ind.append(icon(ICON_PATHS.cross));
          const body = h("span", "evoui-pq-body"); body.append(h("span", "evoui-pq-label", o));
          if (judged && (sel || right)) { const t = has(q, "feedback") ? q.feedback[oi] : right ? q.why : ""; if (t) body.append(h("span", "evoui-pq-fb", t)); }
          row.append(ind, body); list.append(row);
        });
        page.append(list);
        const foot = h("div", "evoui-pq-foot");
        if (multi && !judged) foot.append(h("span", "evoui-pq-tip", uiValue(() => ui("选出所有适用项"))));
        const last = p.current === p.order.length - 1;
        const act = judged ? btn("evoui-pq-btn", last ? ui("查看结果") : ui("下一题"), "next", () => go(p.current + 1))
          : multi ? btn("evoui-pq-btn", ui("提交答案"), "submit", () => judge(i, p.answers[i])) : btn("evoui-pq-btn", last ? ui("查看结果") : ui("下一题"), "next", () => {});
        if (!judged) act.disabled = !multi || a === null;
        foot.append(act); page.append(foot);
        page.addEventListener("keydown", (ev) => {
          if (ev.altKey || ev.ctrlKey || ev.metaKey) return;
          const k = ev.key, digit = /^[1-8]$/.test(k) ? Number(k) - 1 : /^[a-h]$/i.test(k) ? k.toLowerCase().charCodeAt(0) - 97 : -1;
          if (digit >= 0 && digit < q.options.length && !p.judged[i]) { ev.preventDefault(); const o = page.querySelector(`[data-f="opt-${digit}"]`); if (o) { o.focus({ preventScroll: true }); o.click(); } }
          else if (k === "ArrowRight" && p.judged[i]) { ev.preventDefault(); go(p.current + 1); }
          else if (k === "ArrowLeft" && p.current > 0) { ev.preventDefault(); go(p.current - 1); }
        });
        return page;
      }
      function resultsPage() {
        const pg = h("div", "evoui-pq-results"), total = p.order.length, right = p.order.filter((j) => quizRight(Q[j], p.answers[j])).length;
        const head = h("div", "evoui-pq-rhead"); head.append(h("span", "", uiValue(() => n.title || ui("测验结果"))));
        if (p.attempt > 1) head.append(h("span", "evoui-pq-round", uiValue(() => ui("第 {0} 轮", [p.attempt]))));
        const score = h("div", "evoui-pq-score"); score.append(h("span", "evoui-pq-num", `${right}/${total}`),
          h("span", "evoui-pq-verdict", uiValue(() => right === total ? ui("全部答对") : right === 0 ? ui("这一轮没有答对的题") : ui("答对 {0} 题，错 {1} 题", [right, total - right]))));
        const rows = h("ol", "evoui-pq-rows");
        for (const j of p.order) {
          const q = Q[j], a = p.answers[j], ok = quizRight(q, a), multi = q.type === "multi", li = h("li", "evoui-pq-row");
          const ind = h("span", "evoui-pq-ind"); ind.dataset.mark = ok ? "right" : "wrong"; ind.append(icon(ok ? ICON_PATHS.check : ICON_PATHS.cross));
          uiAttr(ind, "role", () => "img"); uiAttr(ind, "aria-label", uiValue(() => ok ? ui("答对") : ui("答错")));
          const body = h("div", "evoui-pq-rbody"); body.append(h("div", "evoui-pq-rq", q.q));
          const keys = multi ? q.correct.slice().sort((x, y) => x - y) : [q.correct], letter = (oi) => `${String.fromCharCode(65 + oi)}. ${q.options[oi]}`;
          if (ok) body.append(h("div", "evoui-pq-rsub", has(q, "feedback") ? keys.map((k) => q.feedback[k]).join(" ") : q.why));
          else {
            body.append(h("div", "evoui-pq-rsub", uiValue(() => ui("正确答案：{0}", [keys.map(letter).join("；")]))));
            const picks = multi ? a || [] : [a], wrongPicks = picks.filter((oi) => !keys.includes(oi)), missed = keys.filter((oi) => !picks.includes(oi));
            const abc = (xs) => xs.map((oi) => String.fromCharCode(65 + oi)).join("、");
            const why = has(q, "feedback") && wrongPicks.length ? wrongPicks.map((oi) => q.feedback[oi]).join(" ") : q.why;
            body.append(h("div", "evoui-pq-rsub", uiValue(() => ui("你选了 {0}{1}。{2}", [abc(picks), multi && missed.length ? ui("，漏了 {0}", [abc(missed)]) : "", why]))));
          }
          li.append(ind, body); rows.append(li);
        }
        const diag = h("div", "evoui-pq-cases");
        for (const [text, ok] of caseLines(n, p.order, (j) => quizRight(Q[j], p.answers[j]))) { const l = h("div", "evoui-pq-case", text); l.dataset.ok = String(ok); diag.append(l); }
        const foot = h("div", "evoui-pq-rfoot"), left = h("div", "evoui-pq-rbtns");
        left.append(btn("evoui-pq-btn", ui("重新开始"), "restart", () => restart(false)));
        if (right < total) left.append(btn("evoui-pq-btn", ui("只做错题"), "wrong", () => restart(true)));
        const asked = p.requested === p.attempt, m = btn("evoui-pq-btn primary", asked ? ui("已请求新的一套") : ui("再来一套"), "more", more); m.disabled = asked;
        foot.append(left, m); pg.append(head, score); if (diag.childElementCount) pg.append(diag); pg.append(rows, foot);
        return pg;
      }
      draw(false); if (p.done) ctx.markDone(key, { restored: true }); return root;
    }
    /* Flashcards (ChatGPT LearningFlashcards): a deck of cards, the current one on top of two stacked
       preview cards. Click / Space flips it (300 ms 3D); ✗ / ← / swipe left = 没记住, ✓ / → / swipe
       right = 记住 (the card flies off in 240 ms with an orange / green edge, the next one rises from
       the stack). A pointer drag moves and tilts the card and lights ✗ / ✓ in proportion to the drag
       distance (lit 240 ms after a commit); a click within 350 ms after a drag is ignored. ↶ / z undoes
       the last rating (the card comes back from its side, 300 ms); the shuffle button (`shuffle`,
       default true) reorders only the cards not yet rated, never putting the current one first again.
       Keys work while the deck has focus (the card is tabindex 0): Space / Enter flip, ← →, z, h (hint).
       The first card shows a tip that changes every 3 s. After the last card a summary: counts,
       「重新开始」 (attempt + 1, all cards) and 「复习 N 张没记住的」 (attempt + 1, the missed cards only);
       all known → 「更多卡片」 (a request event, once per attempt) and confetti (once per attempt, never
       on restore). Progress {order: [card indices of this round], index (position in order; == length
       in the summary), flipped, missed: [card indices rated 没记住, in order], history: [{card,
       flipped, dir: "left" | "right", ms}] (one per rated position), phase: "cards" | "summary",
       round: "all" | "missed", attempt (≥ 1), hint (the current card's hint is open), hintUsed: [card
       indices whose hint was opened this attempt], done (sticky), celebrated / requested (the attempt
       that got confetti / asked for more; 0 = none)}. Ratings are recorded when a round ends: one item
       event per card of the round with its final rating (undo can change it before; response "know" /
       "missed", latencyMs = time the card was shown), then the summary. An abandoned round sends nothing
       (its ratings stay in the progress and are sent when it is finished, also after a reload). */
    function flashcards(n) {
      const key = ctx.nodeKey(n), C = n.cards, all = () => C.map((_, i) => i);
      const fresh = (attempt, order, round, done) => ({ order, index: 0, flipped: false, missed: [], history: [], phase: "cards", round, attempt,
        hint: false, hintUsed: [], done, celebrated: 0, requested: 0 });
      const p = ctx.nodeProgress(key, () => fresh(1, all(), "all", false), (r) => cardsProgressFits(n, r));
      const win = doc.defaultView, TIPS = [ui("点击翻面"), ui("左滑没记住"), ui("右滑记住"), ui("键盘：空格 ← → z")];
      const calm = () => { try { return !!(win && win.matchMedia && win.matchMedia("(prefers-reduced-motion: reduce)").matches); } catch (x) { return false; } };
      const T = (ms) => (calm() ? 0 : ms);
      const icon = (d) => { const s = doc.createElementNS(SVGNS, "svg"); uiAttr(s, "viewBox", () => "0 0 16 16"); uiAttr(s, "aria-hidden", () => "true"); const pa = doc.createElementNS(SVGNS, "path"); uiAttr(pa, "d", () => d); s.append(pa); return s; };
      let shownAt = ctx.now(), dragEnd = -1e9, drag = null, anim = null, litTimer = null, tipTimer = null, tipStop = null, tipStep = 0;
      let tipGone = p.attempt > 1 || p.history.length > 0;
      const btn = (cls, f, label, d, fn) => { const b = h("button", cls); b.type = "button"; b.dataset.f = f; uiAttr(b, "aria-label", () => label); uiProp(b, "title", () => label); if (d) b.append(icon(d)); b.onclick = () => { if (ctx.now() - dragEnd < 350) return; fn(); }; return b; };
      const root = h("div", "evoui-fc"); uiAttr(root, "role", () => "group"); uiAttr(root, "aria-label", uiValue(() => n.title || ui("闪卡")));
      const col = h("div", "evoui-fc-col"); root.append(col);
      if (n.title) col.append(h("div", "evoui-fc-title", n.title));
      const deck = h("div", "evoui-fc-deck"), sumHost = h("div", "evoui-fc-sumhost"), live = h("div", "evoui-sr"); uiAttr(live, "aria-live", () => "polite");
      col.append(deck, sumHost); root.append(live);
      /* the deck: two preview cards, the card (flip: front / back faces), the bar */
      const stage = h("div", "evoui-fc-stage"); stage.tabIndex = 0; stage.dataset.f = "stage"; uiAttr(stage, "role", () => "button");
      uiAttr(stage, "aria-keyshortcuts", () => "Space ArrowLeft ArrowRight Z");
      const faceOf = (cls) => { const f = h("div", "evoui-fc-face " + cls), sc = h("div", "evoui-fc-scroll"), t = h("div", "evoui-fc-text"); sc.append(t); f.append(sc); return [f, t]; };
      const peeks = [2, 1].map((depth) => { const pk = h("div", "evoui-fc-peek"); pk.dataset.depth = String(depth); uiAttr(pk, "aria-hidden", () => "true"); const [f, t] = faceOf("front"); pk.append(f); stage.append(pk); return { el: pk, text: t }; }).reverse();
      const card = h("div", "evoui-fc-card"), flip = h("div", "evoui-fc-flip");
      const [front, frontText] = faceOf("front"), [back, backText] = faceOf("back");
      const cardHint = h("div", "evoui-fc-cardhint"), tip = h("div", "evoui-fc-tip", TIPS[0]); uiAttr(tip, "aria-hidden", () => "true");
      front.append(cardHint, tip); flip.append(front, back); card.append(flip); stage.append(card);
      const bar = h("div", "evoui-fc-bar"), count = h("span", "evoui-fc-count"), rate = h("div", "evoui-fc-rate"), tools = h("div", "evoui-fc-tools");
      const missB = btn("evoui-fc-btn miss", "miss", ui("没记住"), ICON_PATHS.cross, () => mark("left"));
      const knowB = btn("evoui-fc-btn know", "know", ui("记住了"), ICON_PATHS.check, () => mark("right"));
      rate.append(missB, knowB);
      const hintB = btn("evoui-pq-icon evoui-pq-hintbtn", "hint", ui("提示"), "M8 1.9a4.3 4.3 0 0 0-2.55 7.77c.47.35.75.86.75 1.42v.66h3.6v-.66c0-.56.28-1.07.75-1.42A4.3 4.3 0 0 0 8 1.9ZM6.4 14.1h3.2", () => toggleHint());
      const undoB = btn("evoui-pq-icon", "undo", ui("撤销（z）"), "M5.2 3.6 2.3 6.5l2.9 2.9M2.6 6.5h7a3.6 3.6 0 0 1 0 7.2H7", () => undo());
      const shufB = n.shuffle === false ? null : btn("evoui-pq-icon", "shuffle", ui("洗牌"), "M1.8 4.6h2.1c3.6 0 4.6 6.8 8.2 6.8h2M1.8 11.4h2.1c1.3 0 2.2-.9 3-2.2M9.2 6.8c.8-1.3 1.7-2.2 3-2.2h2M12.4 2.7l1.9 1.9-1.9 1.9M12.4 9.5l1.9 1.9-1.9 1.9", () => shuffle());
      tools.append(hintB, undoB); if (shufB) tools.append(shufB);
      bar.append(count, rate, tools); deck.append(stage, bar);

      function look(el, t, isBack) {         // adaptive size: 1 char 48–56, >200 12, >120 15, >56 17, >20 20, else 22 px
        const len = Array.from(t).length, one = Array.from(t.trim()).length === 1, list = /(^|\n) {0,3}([-+*]|[0-9]+[.)]) +/.test(t), multi = t.includes("\n");
        uiProp(el, "textContent", () => t); el.dataset.z = one ? "one" : len > 200 ? "xs" : len > 120 ? "s" : len > 56 || (isBack && multi) ? "m" : len > 20 ? "l" : "xl";
        el.classList.toggle("top", list || (isBack && multi)); el.classList.toggle("list", list);
      }
      function paintDeck() {
        const pos = p.index, c = C[p.order[pos]];
        look(frontText, c.front, false); look(backText, c.back, true);
        card.classList.toggle("flipped", p.flipped); uiAttr(front, "aria-hidden", () => String(p.flipped)); uiAttr(back, "aria-hidden", () => String(!p.flipped));
        cardHint.hidden = !(p.hint && has(c, "hint")); uiProp(cardHint, "textContent", () => has(c, "hint") ? c.hint : "");
        peeks.forEach((pk, d) => { const j = p.order[pos + 1 + d]; pk.el.hidden = j === undefined; if (j !== undefined) look(pk.text, C[j].front, false); });
        paintBar();
      }
      function paintBar() {                  // the bar follows the state at once (the card may still be moving)
        if (p.phase !== "cards") return;
        const pos = p.index, c = C[p.order[pos]], last = pos === p.order.length - 1, hinted = has(c, "hint");
        uiProp(count, "textContent", () => `${pos + 1}/${p.order.length}`);
        uiAttr(knowB, "aria-label", uiValue(() => last ? ui("记住了（最后一张）") : ui("记住了"))); uiProp(knowB, "title", () => knowB.getAttribute("aria-label"));
        undoB.disabled = !p.history.length; if (shufB) shufB.disabled = p.order.length - pos < 2;
        hintB.hidden = !hinted; hintB.disabled = p.flipped; hintB.classList.toggle("on", p.hint); uiAttr(hintB, "aria-pressed", () => String(p.hint));
        uiAttr(stage, "aria-label", uiValue(() => ui("闪卡 {0}/{1}，{2}：{3}。空格翻面，← 没记住，→ 记住", [pos + 1, p.order.length, p.flipped ? ui("背面") : ui("正面"), p.flipped ? c.back : c.front])));
        paintTip();
      }
      function paintTip() {
        const on = !tipGone && p.phase === "cards" && p.index === 0 && !p.flipped && !p.hint;
        tip.hidden = !on;
        if (on && tipTimer === null) tipTimer = setInterval(() => {
          if (!root.isConnected) { clearInterval(tipTimer); tipTimer = null; return; }
          tipStep = (tipStep + 1) % TIPS.length; tip.classList.add("swap");
          setTimeout(() => { uiProp(tip, "textContent", () => TIPS[tipStep]); tip.classList.remove("swap"); }, T(160));
        }, 3000);
        if (!on && tipTimer !== null) { clearInterval(tipTimer); tipTimer = null; }
        if (on && !tipStop) tipStop = ctx.onDestroy(() => { if (tipTimer !== null) { clearInterval(tipTimer); tipTimer = null; } });
      }
      const tipOff = () => { if (!tipGone) { tipGone = true; paintTip(); } };
      /* motion: one animation at a time; a new action first finishes the running one (state is never late) */
      function settle() { if (anim) { const a = anim; anim = null; clearTimeout(a.timer); a.done(); } }
      function after(ms, done) { if (ms <= 0) { done(); return; } const a = { done, timer: setTimeout(() => { if (anim === a) { anim = null; done(); } }, ms) }; anim = a; }
      function still(fn) { stage.classList.add("still"); fn(); void stage.offsetWidth; stage.classList.remove("still"); }
      function edge(dir, k) { if (dir) card.dataset.edge = dir; else delete card.dataset.edge; card.style.setProperty("--edge", String(k)); }
      function setLit(l, r) { for (const [b, v] of [[missB, l], [knowB, r]]) { b.style.setProperty("--lit", String(Math.round(v * 100) / 100)); b.dataset.hot = String(v >= 0.5); } }
      function lightUp(dir) { clearTimeout(litTimer); setLit(dir === "left" ? 1 : 0, dir === "right" ? 1 : 0); litTimer = setTimeout(() => setLit(0, 0), 240); }
      const reach = () => Math.max(60, Math.min(120, (stage.clientWidth || 360) * 0.28));   // drag distance that commits
      function place(dx, dy) {
        const k = Math.min(Math.abs(dx) / reach(), 1);
        card.style.transform = `translate(${dx}px, ${dy}px) rotate(${Math.max(-14, Math.min(14, dx * 0.07))}deg)`;
        edge(dx < 0 ? "left" : "right", k); setLit(dx < 0 ? k : 0, dx > 0 ? k : 0);
        peeks[0].el.style.transform = `translateY(${9 * (1 - k)}px) scale(${0.95 + 0.05 * k})`;
      }
      function flipCard() {
        if (p.phase !== "cards") return;
        settle(); p.flipped = !p.flipped; if (p.flipped) p.hint = false; tipOff(); ctx.change(); paintDeck();
        uiProp(live, "textContent", uiValue(() => p.flipped ? ui("背面：") + C[p.order[p.index]].back : ui("正面")));
      }
      function toggleHint() {
        const ci = p.order[p.index];
        if (p.phase !== "cards" || p.flipped || !has(C[ci], "hint")) return;
        p.hint = !p.hint; if (p.hint && !p.hintUsed.includes(ci)) p.hintUsed.push(ci); tipOff(); ctx.change(); paintDeck();
      }
      function mark(dir, from) {
        if (p.phase !== "cards") return;
        settle();
        const ci = p.order[p.index], know = dir === "right", ms = Math.max(0, Math.round(ctx.now() - shownAt));
        p.history.push({ card: ci, flipped: p.flipped, dir, ms }); if (!know) p.missed.push(ci);
        p.index += 1; p.flipped = false; p.hint = false;
        const end = p.index === p.order.length; let cheer = false;
        if (end) { p.phase = "summary"; p.done = true; if (!p.missed.length && p.celebrated !== p.attempt) { p.celebrated = p.attempt; cheer = true; } }
        ctx.change();
        if (end) {                           // the round's final ratings (after any undo): one item event per card
          for (const x of p.history) ctx.emit({ type: "item", node: key, kind: "flashcard", item: x.card, attempt: p.attempt, response: x.dir === "right" ? "know" : "missed",
            correct: x.dir === "right", hintUsed: p.hintUsed.includes(x.card), latencyMs: x.ms });
          ctx.emit({ type: "summary", node: key, kind: "flashcard", attempt: p.attempt, total: p.order.length, right: p.order.length - p.missed.length, missed: p.missed.slice(),
            timeMs: p.history.reduce((s, x) => s + x.ms, 0), hintsUsed: p.hintUsed.length });
          ctx.markDone(key);
        }
        tipOff(); lightUp(dir); paintBar(); uiProp(live, "textContent", uiValue(() => know ? ui("记住了") : ui("没记住")));
        const s = know ? 1 : -1, w = stage.clientWidth || 360, y = from ? from.y : 0;
        root.classList.remove("dragging"); edge(dir, 1); stage.classList.add("advance"); card.classList.add("fly");
        peeks[0].el.style.transform = "";
        card.style.transform = `translate(${s * (w + 90)}px, ${y + 28}px) rotate(${s * 22}deg)`;
        after(T(240), () => {
          still(() => { stage.classList.remove("advance"); card.classList.remove("fly"); card.style.transform = ""; edge(null, 0); if (!end) paintDeck(); });
          shownAt = ctx.now();
          if (end) { showSummary(true); if (cheer) celebrate(doc); }
        });
      }
      function undo() {
        if (p.phase !== "cards" || !p.history.length) return;
        settle();
        const last = p.history.pop(); p.index -= 1; p.flipped = last.flipped; p.hint = false;
        if (last.dir === "left") { const k = p.missed.lastIndexOf(last.card); if (k >= 0) p.missed.splice(k, 1); }
        ctx.change(); clearTimeout(litTimer); setLit(0, 0); uiProp(live, "textContent", uiValue(() => ui("已撤销")));
        const s = last.dir === "right" ? 1 : -1, w = stage.clientWidth || 360;
        still(() => { paintDeck(); card.style.transform = `translate(${s * (w + 90)}px, 28px) rotate(${s * 22}deg)`; edge(last.dir, 1); });
        card.classList.add("return"); card.style.transform = ""; edge(last.dir, 0);
        shownAt = ctx.now();
        after(T(300), () => { card.classList.remove("return"); edge(null, 0); });
      }
      function shuffle() {
        if (p.phase !== "cards" || !shufB) return;
        const rest = p.order.slice(p.index); if (rest.length < 2) return;
        settle();
        const cur = rest[0];
        for (let i = rest.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [rest[i], rest[j]] = [rest[j], rest[i]]; }
        if (rest[0] === cur) rest.push(rest.shift());               // never the current card first again
        p.order = p.order.slice(0, p.index).concat(rest); p.flipped = false; p.hint = false; tipOff(); ctx.change();
        still(() => paintDeck()); stage.classList.add("shuffling"); uiProp(live, "textContent", uiValue(() => ui("已洗牌")));
        shownAt = ctx.now();
        after(T(300), () => stage.classList.remove("shuffling"));
      }
      function restart(missedOnly) {
        const only = missedOnly && p.missed.length > 0, order = only ? p.missed.slice() : all();
        Object.assign(p, fresh(p.attempt + 1, order, only ? "missed" : "all", p.done));
        ctx.change(); uiProp(live, "textContent", () => "");
        const focus = root.contains(doc.activeElement);
        sumHost.replaceChildren(); deck.hidden = false; still(() => paintDeck());
        deck.classList.remove("evoui-enter"); void deck.offsetWidth; if (!calm()) deck.classList.add("evoui-enter");
        shownAt = ctx.now(); if (focus) stage.focus({ preventScroll: true });
      }
      function more() { if (p.requested === p.attempt) return; p.requested = p.attempt; ctx.change(); ctx.emit({ type: "request", node: key, what: "more" }); showSummary(false); }
      function showSummary(animate) {
        const focus = root.contains(doc.activeElement), had = focus && doc.activeElement.dataset ? doc.activeElement.dataset.f : null;
        const total = p.order.length, missed = p.missed.length, right = total - missed;
        const pg = h("div", "evoui-fc-sum"), box = h("div", "evoui-fc-sumcard");
        const tag = p.attempt > 1 ? ui("第 {0} 轮", [p.attempt]) + (p.round === "missed" ? ui(" · 复习没记住的") : "") : "";
        if (tag) box.append(h("div", "evoui-fc-round", tag));
        box.append(h("div", "evoui-fc-verdict", uiValue(() => !missed ? ui("全部记住了") : right * 2 < total ? ui("还不太熟，再练一轮") : ui("记住了 {0}%", [Math.round(100 * right / total)]))));
        const tally = h("div", "evoui-fc-tally");
        for (const [k, num, d, label] of [["miss", missed, ICON_PATHS.cross, ui("没记住")], ["know", right, ICON_PATHS.check, ui("记住了")]]) {
          const chip = h("span", "evoui-fc-chip"); chip.dataset.k = k; uiAttr(chip, "aria-label", uiValue(() => ui("{0} {1} 张", [label, num])));
          const disc = h("i"); disc.append(icon(d)); chip.append(disc, h("span", "", String(num))); tally.append(chip);
        }
        box.append(tally);
        const foot = h("div", "evoui-fc-sumbar"), again = h("button", "evoui-pq-btn", uiValue(() => ui("重新开始"))); again.type = "button"; again.dataset.f = "restart"; again.onclick = () => restart(false);
        let main;
        if (missed) { main = h("button", "evoui-pq-btn primary", uiValue(() => ui("复习 {0} 张没记住的", [missed]))); main.dataset.f = "review"; main.onclick = () => restart(true); }
        else { const asked = p.requested === p.attempt; main = h("button", "evoui-pq-btn primary", uiValue(() => asked ? ui("已请求更多卡片") : ui("更多卡片"))); main.dataset.f = "more"; main.disabled = asked; main.onclick = more; }
        main.type = "button"; foot.append(again, main); pg.append(box, foot);
        if (animate && !calm()) pg.classList.add("evoui-enter");
        deck.hidden = true; sumHost.replaceChildren(pg); paintTip();
        if (focus) { const t = (had && pg.querySelector(`[data-f="${had}"]:not(:disabled)`)) || (main.disabled ? again : main); t.focus({ preventScroll: true }); }
      }
      /* pointer drag (mouse and touch): pointer capture on the card; a vertical touch gesture is left to the page */
      card.addEventListener("pointerdown", (ev) => {
        if (p.phase !== "cards" || ev.button !== 0 || drag) return;
        settle();
        drag = { id: ev.pointerId, x0: ev.clientX, y0: ev.clientY, dx: 0, dy: 0, moved: false, vx: 0, lx: ev.clientX, lt: ctx.now() };
        try { card.setPointerCapture(ev.pointerId); } catch (x) { /* capture is best effort */ }
      });
      card.addEventListener("pointermove", (ev) => {
        if (!drag || ev.pointerId !== drag.id) return;
        const dx = ev.clientX - drag.x0, dy = ev.clientY - drag.y0;
        if (!drag.moved) {
          if (Math.hypot(dx, dy) < 6) return;
          if (ev.pointerType !== "mouse" && Math.abs(dy) > Math.abs(dx) * 1.2) { release(false); return; }
          drag.moved = true; root.classList.add("dragging"); tipOff();
        }
        const t = ctx.now(); drag.vx = 0.7 * ((ev.clientX - drag.lx) / Math.max(1, t - drag.lt)) + 0.3 * drag.vx; drag.lx = ev.clientX; drag.lt = t;
        drag.dx = dx; drag.dy = dy * 0.25; place(drag.dx, drag.dy);
      });
      function release(commit) {
        const d = drag; drag = null; if (!d) return;
        try { if (card.hasPointerCapture && card.hasPointerCapture(d.id)) card.releasePointerCapture(d.id); } catch (x) { /* already released */ }
        if (!d.moved) return;
        dragEnd = ctx.now();
        const far = Math.abs(d.dx) >= reach(), fling = Math.abs(d.vx) > 0.6 && Math.abs(d.dx) > 40 && Math.sign(d.vx) === Math.sign(d.dx);
        if (commit && (far || fling)) { mark(d.dx < 0 ? "left" : "right", { y: d.dy }); return; }
        root.classList.remove("dragging"); card.classList.add("spring"); card.style.transform = ""; edge(card.dataset.edge, 0); setLit(0, 0); peeks[0].el.style.transform = "";
        after(T(220), () => { card.classList.remove("spring"); edge(null, 0); });
      }
      card.addEventListener("pointerup", (ev) => { if (drag && ev.pointerId === drag.id) release(true); });
      card.addEventListener("pointercancel", (ev) => { if (drag && ev.pointerId === drag.id) release(false); });
      card.addEventListener("lostpointercapture", (ev) => { if (drag && ev.pointerId === drag.id && drag.moved) release(false); });
      card.addEventListener("click", () => { if (ctx.now() - dragEnd < 350) return; flipCard(); });
      root.addEventListener("keydown", (ev) => {
        if (ev.altKey || ev.ctrlKey || ev.metaKey || p.phase !== "cards" || !deck.contains(ev.target)) return;
        const k = ev.key, onButton = ev.target !== stage && ev.target.tagName === "BUTTON";
        if ((k === " " || k === "Enter") && !onButton) { ev.preventDefault(); if (!ev.repeat) flipCard(); }
        else if (k === "ArrowLeft" || k === "ArrowRight") { ev.preventDefault(); if (!ev.repeat) mark(k === "ArrowLeft" ? "left" : "right"); }
        else if (!ev.repeat && (k === "z" || k === "Z")) { ev.preventDefault(); undo(); }
        else if (!ev.repeat && (k === "h" || k === "H")) { ev.preventDefault(); toggleHint(); }
      });
      if (p.phase === "summary") showSummary(false); else paintDeck();
      if (p.done) ctx.markDone(key, { restored: true });
      return root;
    }
    /* reveal (EvoUI v2): content behind a soft striped placeholder. mode "click" (default): one press
       shows every child; "steps": each press shows the next child (「下一步 (k/N)」); "after": shown when
       the practice node (PRACTICE: quiz / flashcards) whose id is `after` reaches done. `label`: the
       button text (after: the placeholder text). Progress {shown: number of children shown}. A newly
       shown child enters (180 ms + 4 px rise), the placeholder slides down (200 ms) or leaves (120 ms);
       「再遮住」 hides everything again (click / steps). Restored, or the target already done: shown at once. */
    const EYE = ["M1.6 8s2.4-4.4 6.4-4.4 6.4 4.4 6.4 4.4-2.4 4.4-6.4 4.4S1.6 8 1.6 8Z", "M8 6.1a1.9 1.9 0 1 0 0 3.8 1.9 1.9 0 1 0 0-3.8Z"];
    const LOCK = ["M5 7.2V5.4a3 3 0 0 1 6 0v1.8", "M3.8 7.2h8.4v6.3H3.8Z"];
    function reveal(n, U) {
      const key = ctx.nodeKey(n), N = n.children.length, mode = n.mode || "click";
      const p = ctx.nodeProgress(key, () => ({ shown: 0 }), (r) => isObj(r) && Object.keys(r).length === 1 && isInt(r.shown) && r.shown >= 0 && r.shown <= N
        && (mode === "steps" || r.shown === 0 || r.shown === N));
      const root = h("div", "evoui-rv"); root.dataset.mode = mode;
      const body = h("div", "evoui-rv-body"), live = h("div", "evoui-sr"); uiAttr(live, "aria-live", () => "polite");
      const steps = n.children.map((c, i) => { const w = h("div", "evoui-rv-step"); w.tabIndex = -1; w.dataset.step = String(i + 1); w.append(node(c, U)); body.append(w); return w; });
      const mask = h("div", "evoui-rv-mask"); let btn = null, again = null, text = null, label = "";
      if (mode === "after") { text = h("span", "evoui-rv-label"); mask.append(svgIcon(LOCK, "evoui-rv-icon"), text); }
      else {
        btn = h("button", "evoui-rv-btn"); btn.type = "button"; btn.dataset.f = "reveal"; btn.onclick = () => show(mode === "steps" ? p.shown + 1 : N, true);
        mask.append(btn); mask.addEventListener("click", (ev) => { if (ev.target === mask) btn.click(); });   // the whole placeholder is a target
        again = h("button", "evoui-rv-again", uiValue(() => ui("再遮住"))); again.type = "button"; again.dataset.f = "hide";
        again.onclick = () => { p.shown = 0; ctx.change(); paint(); uiProp(live, "textContent", uiValue(() => ui("已遮住"))); btn.focus({ preventScroll: true }); };
      }
      root.append(body, mask); if (again) root.append(again); root.append(live);
      U.push((E) => { label = has(n, "label") ? template(n.label, E) : ""; paintMask(); });
      function paintMask() {
        if (text) uiProp(text, "textContent", uiValue(() => label || ui("完成上面的练习后显示")));
        if (!btn) return;
        const k = Math.min(p.shown + 1, N), t = mode === "steps" ? ui("{0} ({1}/{2})", [label || ui("下一步"), k, N]) : label || ui("显示");
        btn.replaceChildren(svgIcon(EYE, "evoui-rv-icon"), h("span", "", t));
      }
      function paint() {
        steps.forEach((s, i) => { s.hidden = i >= p.shown; });
        mask.hidden = p.shown >= N; if (again) again.hidden = p.shown < N;
        root.dataset.shown = String(p.shown); paintMask();
      }
      function show(k, animate) {
        k = Math.max(0, Math.min(N, k)); if (k <= p.shown) return;
        const from = p.shown, moving = animate && !calm() && root.isConnected;
        const mr = moving ? mask.getBoundingClientRect() : null, before = moving ? flipFirst([mask], () => "m") : null;
        const ghost = moving && k >= N ? ghostOf(mask) : null;
        const hadFocus = btn && doc.activeElement === btn;
        p.shown = k; if (animate) ctx.change(); paint();
        if (moving) {
          for (let i = from; i < k; i++) enterEl(steps[i], "rise", (i - from) * 50);
          if (ghost) { root.insertBefore(ghost, mask); leaveEl(ghost, mr); } else flipLast([mask], () => "m", before);
        }
        uiProp(live, "textContent", uiValue(() => k >= N ? ui("已全部显示") : ui("已显示第 {0} 步，共 {1} 步", [k, N])));
        if (hadFocus && k >= N) steps[from].focus({ preventScroll: true });
      }
      if (mode === "after" && p.shown < N) {
        if (ctx.isDone(n.after)) p.shown = N;                 // done before this node was built: shown, no motion
        else {
          if (afterOffs.has(n)) afterOffs.get(n)();                // an earlier build of this node stops listening
          const off = ctx.onDone(n.after, (_, info) => {
            off(); afterOffs.delete(n);
            if (!building && (!root.isConnected || leaving(root))) return;   // this reveal is gone
            if (info.restored || !root.isConnected) { p.shown = N; paint(); } else show(N, true);
          });
          afterOffs.set(n, off);
        }
      }
      paint(); return root;
    }
    /* timeline (after DeepTutor's TimelineBlock, HKUDS/DeepTutor, Apache-2.0: ideas only): ordered events on a
       vertical rail. An event with `when` (a date, a stage) shows it above its title on a small dot; one without
       is numbered in a round badge (like reveal steps). `tone` colours the dot (a key moment). Plain text only;
       multi-line `text` keeps its line breaks. */
    function timeline(n) {
      const ol = h("ol", "evoui-tl");
      n.events.forEach((x, i) => {
        const li = h("li", `evoui-tl-ev${x.when ? " dated" : ""}`);
        const dot = h("span", `evoui-tl-dot t-${x.tone || "default"}`, x.when ? null : String(i + 1)); uiAttr(dot, "aria-hidden", () => "true");
        const body = h("div", "evoui-tl-body");
        if (x.when) body.append(h("div", "evoui-tl-when", x.when));
        body.append(h("div", "evoui-tl-title", x.title));
        if (x.text) body.append(h("div", "evoui-tl-text", x.text));
        li.append(dot, body); ol.append(li);
      });
      return ol;
    }
    /* deep-dive (after DeepTutor's DeepDiveBlock, there a "Go deeper" card that spawns a sub-page): here an
       optional deeper part folded under one summary line (`label`, default 「深入一点」) the reader opens on
       demand — a native <details>, so keyboard and screen readers work as everywhere. Opening plays the enter
       motion; nothing is recorded. */
    const CHEVRON = "M6 3.5 10.5 8 6 12.5";
    function deepDive(n, U) {
      const d = h("details", "evoui-dd"), sum = h("summary", "evoui-dd-sum"), body = h("div", "evoui-box g2 evoui-dd-body");
      sum.append(svgIcon(CHEVRON, "evoui-dd-icon"), h("span", "", uiValue(() => has(n, "label") ? n.label : ui("深入一点"))));
      n.children.forEach((c) => body.append(node(c, U)));
      d.addEventListener("toggle", () => { if (d.open && !calm() && d.isConnected) enterEl(body, "rise", 0); });
      d.append(sum, body); return d;
    }
    const clockText = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
    const PLAY = "M5 3.2v9.6L12.6 8Z", PAUSE = ["M5.5 3.5v9", "M10.5 3.5v9"], RESET = "M3.2 8a4.8 4.8 0 1 0 1.5-3.5M3.2 2.8v2.6h2.6";
    const roundBtn = (cls, f, label, d, fn) => { const b = h("button", cls); b.type = "button"; b.dataset.f = f; uiAttr(b, "aria-label", () => label); uiProp(b, "title", () => label); b.append(svgIcon(d)); b.onclick = fn; return b; };
    const setIcon = (b, d, label) => { if (b.dataset.icon !== label) { b.replaceChildren(svgIcon(d)); b.dataset.icon = label; } uiAttr(b, "aria-label", () => label); uiProp(b, "title", () => label); };
    /* timer (EvoUI v2, after ChatGPT's DigitalTimer): mode "countdown" (`seconds`) or "stopwatch"; start /
       pause / reset; big tabular digits (the stopwatch with tenths), a countdown shows 「/ 5:00」 and a
       draining bar, and turns green with 「时间到」 at 0. `bind`: a number state that receives the elapsed
       whole seconds (at most one change a second). `autostart`: starts when it first appears. Progress
       {elapsedMs, running, done}; restored or rebuilt it is paused (never runs on by itself after a
       reload). While it runs, summary events of practice nodes carry its elapsed ms as `timerMs`
       (ctx.clocks). Its tick stops when the node leaves the document. */
    function timer(n, U) {
      const key = ctx.nodeKey(n), down = n.mode === "countdown", total = down ? n.seconds * 1000 : 0;
      const p = ctx.nodeProgress(key, () => ({ elapsedMs: 0, running: false, done: false }), (r) => isObj(r) && Object.keys(r).length === 3 && isInt(r.elapsedMs)
        && r.elapsedMs >= 0 && (!down || r.elapsedMs <= total) && typeof r.running === "boolean" && typeof r.done === "boolean" && (!r.done || (down && r.elapsedMs === total)));
      p.running = false;
      const root = h("div", "evoui-tm"); uiAttr(root, "role", () => "group"); root.dataset.mode = n.mode;
      const lab = h("div", "evoui-tm-label"), read = h("div", "evoui-tm-read");
      const digits = h("span", "evoui-tm-digits"), frac = h("span", "evoui-tm-frac"), of = h("span", "evoui-tm-of"), live = h("div", "evoui-sr");
      uiAttr(digits, "role", () => "timer"); uiAttr(live, "aria-live", () => "polite"); read.append(digits, frac, of);
      const go = roundBtn("evoui-tm-btn primary", "start", ui("开始"), PLAY, () => (p.running ? pause() : start(false)));
      const rs = roundBtn("evoui-tm-btn", "reset", ui("重置"), RESET, () => reset());
      const btns = h("div", "evoui-tm-btns"); btns.append(go, rs);
      const fill = down ? h("i") : null, bar = down ? h("div", "evoui-tm-bar") : null; if (bar) { uiAttr(bar, "aria-hidden", () => "true"); bar.append(fill); }
      root.append(lab, read, btns); if (bar) root.append(bar); root.append(live);
      let since = null, iv = null, lastSec = Math.floor(p.elapsedMs / 1000), lastSave = lastSec;
      const gone = ctx.life(root);
      const elapsed = () => Math.min(down ? total : Infinity, p.elapsedMs + (p.running && since !== null ? ctx.now() - since : 0));
      for (const c of ctx.clocks) if (c.dead()) ctx.clocks.delete(c);          // earlier builds that are gone
      const clock = { dead: gone, alive: () => root.isConnected && !leaving(root), running: () => p.running, elapsed }; ctx.clocks.add(clock);
      const rebase = () => { p.elapsedMs = Math.round(elapsed()); since = p.running ? ctx.now() : null; };
      const setBind = (sec) => { if (!n.bind || state[n.bind] === sec) return false; state[n.bind] = sec; return true; };
      const stopLoop = () => { if (iv !== null) { clearInterval(iv); iv = null; } };
      ctx.onDestroy(() => { if (p.running && iv !== null) { rebase(); ctx.change(); } stopLoop(); });   // the elapsed time so far reaches the last progress
      function start(quiet) {
        if (p.running || p.done) return;
        p.running = true; since = ctx.now(); if (!quiet) ctx.change();
        stopLoop(); iv = setInterval(tick, down ? 100 : 50); paint();
      }
      function pause() { if (!p.running) return; rebase(); p.running = false; since = null; stopLoop(); ctx.change(); paint(); }
      function reset() {
        stopLoop(); p.running = false; since = null; p.elapsedMs = 0; p.done = false; lastSec = 0; lastSave = 0;
        const bound = setBind(0); ctx.change(); if (bound) update(); paint(); uiProp(live, "textContent", () => "");
      }
      function tick() {
        if (gone()) { stopLoop(); ctx.clocks.delete(clock); return; }   // the live progress belongs to the next build
        const e = elapsed();
        if (down && e >= total) {
          stopLoop(); p.elapsedMs = total; p.running = false; p.done = true; since = null;
          const bound = setBind(n.seconds); ctx.change(); if (bound) update(); paint(); uiProp(live, "textContent", uiValue(() => ui("时间到"))); return;
        }
        const sec = Math.floor(e / 1000);
        if (sec !== lastSec) {                       // a whole second: progress catches up, the bound state follows
          lastSec = sec; rebase();
          if (setBind(sec)) { ctx.change({ lazy: true }); update(); }
          else if (sec - lastSave >= 10) { lastSave = sec; ctx.change({ lazy: true }); }
        }
        paint();
      }
      function paint() {
        const e = elapsed(), name = has(n, "label") ? lab.textContent : "";
        root.dataset.state = p.done ? "done" : p.running ? "running" : e > 0 ? "paused" : "idle";
        if (p.done) { uiProp(digits, "textContent", uiValue(() => ui("时间到"))); uiProp(frac, "textContent", () => ""); uiProp(of, "textContent", () => ""); }
        else if (down) { uiProp(digits, "textContent", () => clockText(Math.ceil((total - e) / 1000))); uiProp(frac, "textContent", () => ""); uiProp(of, "textContent", () => p.running || e > 0 ? "/ " + clockText(n.seconds) : ""); }
        else { const t = Math.floor(e / 100); uiProp(digits, "textContent", () => clockText(Math.floor(t / 10))); uiProp(frac, "textContent", () => "." + (t % 10)); uiProp(of, "textContent", () => ""); }
        go.hidden = p.done;
        if (p.running) setIcon(go, PAUSE, ui("暂停")); else setIcon(go, PLAY, e > 0 ? ui("继续") : ui("开始"));
        rs.disabled = e === 0 && !p.done; rs.classList.toggle("primary", p.done);
        if (fill) fill.style.transform = `scaleX(${Math.max(0, (total - e) / total)})`;
        uiAttr(root, "aria-label", uiValue(() => (name ? name + "，" : "") + (down ? ui("倒计时") : ui("秒表"))));
      }
      U.push((E) => { uiProp(lab, "textContent", () => has(n, "label") ? template(n.label, E) : ""); lab.hidden = !has(n, "label"); paint(); });
      if (n.autostart && !p.done && p.elapsedMs === 0) start(true);
      paint(); return root;
    }
    /* player (EvoUI v2): steps the number state `bind` from min to max, one `step` every interval_ms
       (default 700) ÷ speed — stepwise algorithm animations and parameter sweeps. ▶ / ❚❚, ‹ › one step
       (also while paused; a step pauses a playing player), ↺ back to min, a scrubber, speed 0.5× 1× 2×.
       At max it stops (`loop`: wraps to min); ▶ at max starts again from min. Plots, charts and tables
       that follow the state glide between steps (shorter when it plays fast). Progress {speed,
       playing}; restored or rebuilt it is paused (never plays by itself after a reload). It is a
       control: the render check's automatic probe sets its state min -> max. */
    const STEP_BACK = ["M4 3.5v9", "M12 3.5 6.5 8l5.5 4.5Z"], STEP_FWD = ["M12 3.5v9", "M4 3.5 9.5 8 4 12.5Z"], REPLAY = "M12.8 8a4.8 4.8 0 1 1-1.5-3.5M12.8 2.8v2.6h-2.6";
    function player(n, U) {
      const key = ctx.nodeKey(n), b = n.bind, SPEEDS = [0.5, 1, 2], base = n.interval_ms || 700;
      const N = Math.max(1, Math.floor((n.max - n.min) / n.step + 1e-9));
      const p = ctx.nodeProgress(key, () => ({ speed: 1, playing: false }), (r) => isObj(r) && Object.keys(r).length === 2 && SPEEDS.includes(r.speed) && typeof r.playing === "boolean");
      p.playing = false;
      const idx = () => { const v = Number(state[b]); return isFinite(v) ? Math.max(0, Math.min(N, Math.round((v - n.min) / n.step))) : 0; };
      const valAt = (k) => Math.min(n.max, Math.max(n.min, Number((n.min + k * n.step).toFixed(10))));
      const root = h("div", "evoui-pl"); uiAttr(root, "role", () => "group");
      const top = h("div", "evoui-pl-top"), lab = h("div", "evoui-pl-label"), count = h("span", "evoui-pl-count"); top.append(lab, count);
      const scrub = h("input", "evoui-pl-scrub"); Object.assign(scrub, { type: "range", min: 0, max: N, step: 1 }); scrub.dataset.f = "scrub";
      scrub.oninput = () => { if (p.playing) pause(); to(Number(scrub.value)); };
      const restart = roundBtn("evoui-pl-btn", "restart", ui("回到开头"), RESET, () => { if (p.playing) pause(); to(0); });
      const back = roundBtn("evoui-pl-btn", "back", ui("上一步"), STEP_BACK, () => step(-1));
      const play = roundBtn("evoui-pl-btn primary", "play", ui("播放"), PLAY, () => (p.playing ? pause() : start()));
      const fwd = roundBtn("evoui-pl-btn", "fwd", ui("下一步"), STEP_FWD, () => step(1));
      const transport = h("div", "evoui-pl-transport"); transport.append(restart, back, play, fwd);
      const speeds = h("div", "evoui-pl-speed"); uiAttr(speeds, "role", () => "group"); uiAttr(speeds, "aria-label", uiValue(() => ui("速度")));
      const sb = SPEEDS.map((s) => { const x = h("button", "", s + "×"); x.type = "button"; x.dataset.f = "speed-" + s; x.onclick = () => setSpeed(s); speeds.append(x); return [x, s]; });
      const bar = h("div", "evoui-pl-bar"); bar.append(transport, speeds);
      root.append(top, scrub, bar);
      root.addEventListener("keydown", (ev) => {                       // ← → on the buttons step (the scrubber has its own)
        if (ev.altKey || ev.ctrlKey || ev.metaKey || ev.target === scrub) return;
        if (ev.key === "ArrowLeft" || ev.key === "ArrowRight") { ev.preventDefault(); step(ev.key === "ArrowLeft" ? -1 : 1); }
      });
      let iv = null; const gone = ctx.life(root);
      const period = () => base / p.speed;
      const me = {};
      const stopLoop = () => { if (iv !== null) { clearInterval(iv); iv = null; } setPace(me, null); };
      const loop = () => { stopLoop(); iv = setInterval(tick, period()); setPace(me, period()); };
      ctx.onDestroy(stopLoop);
      function to(k, lazy) {
        const v = valAt(Math.max(0, Math.min(N, k)));
        if (state[b] === v) { paint(); return; }
        state[b] = v; ctx.change(lazy ? { lazy: true } : undefined); update();
      }
      function start() { if (p.playing) return; if (idx() >= N && !n.loop) to(0); p.playing = true; ctx.change(); loop(); paint(); }
      function pause() { if (!p.playing) return; p.playing = false; stopLoop(); ctx.change(); paint(); }
      function step(d) { if (p.playing) pause(); to(idx() + d); }
      function setSpeed(s) { if (p.speed === s) return; p.speed = s; ctx.change(); if (p.playing) loop(); paint(); }
      function tick() {
        if (gone()) { stopLoop(); return; }                          // the live progress belongs to the next build
        const k = idx();
        if (k >= N) { if (n.loop) to(0, true); else pause(); return; }
        to(k + 1, true);
        if (k + 1 >= N && !n.loop) pause();
      }
      function paint() {
        const k = idx(), end = k >= N && !n.loop;
        uiProp(count, "textContent", () => `${k} / ${N}`); scrub.value = String(k); scrub.style.setProperty("--pct", (k / N * 100) + "%");
        uiAttr(scrub, "aria-valuetext", uiValue(() => ui("第 {0} 步，共 {1} 步", [k, N]))); uiAttr(scrub, "aria-label", uiValue(() => lab.textContent || ui("进度")));
        if (p.playing) setIcon(play, PAUSE, ui("暂停")); else if (end) setIcon(play, REPLAY, ui("从头播放")); else setIcon(play, PLAY, ui("播放"));
        back.disabled = k <= 0; fwd.disabled = k >= N; restart.disabled = k <= 0;
        sb.forEach(([x, s]) => uiAttr(x, "aria-pressed", () => String(p.speed === s)));
        root.dataset.playing = String(p.playing); uiAttr(root, "aria-label", uiValue(() => lab.textContent || ui("播放器")));
      }
      U.push((E) => { uiProp(lab, "textContent", () => has(n, "label") ? template(n.label, E) : ""); lab.hidden = !has(n, "label"); paint(); });
      paint(); return root;
    }
    /* ---------- EvoUI v2 practice cards (ChatGPT LearningCard family): fill-blank, sentence, pager; route ---------- */
    const LINES = ["M2.5 4.2h11", "M2.5 8h11", "M2.5 11.8h6.5"], BUILD = ["M2.2 4.5h5.2v3H2.2Z", "M8.6 4.5h5.2v3H8.6Z", "M2.2 9.5h3.6v3H2.2Z", "M7 9.5h6.8"];
    const UNDO = ["M5.8 3.6 2.6 6.8 5.8 10", "M2.8 6.8h6.7a3.6 3.6 0 0 1 0 7.2H7.6"], CHEV_L = "M10 3.5 5.5 8l4.5 4.5", CHEV_R = "M6 3.5 10.5 8 6 12.5";
    const JUDGE_MS = 260;                  // a pick shows grey in the slot this long before it turns green / red
    /* data-practice marks a practice node's root (a gated pager finds the practice nodes of its page by it) */
    const practiceEl = (n, el) => { el.dataset.practice = ctx.nodeKey(n); return el; };
    /* the card shell: icon + title head, then the parts the node fills in */
    function card(cls, iconPaths, title) {
      const root = h("div", "evoui-pc " + cls); uiAttr(root, "role", () => "group"); uiAttr(root, "aria-label", () => title);
      const head = h("div", "evoui-pc-head"); head.append(svgIcon(iconPaths, "evoui-pc-icon"), h("span", "evoui-pc-title", title));
      const live = h("div", "evoui-sr"); uiAttr(live, "aria-live", () => "polite");
      root.append(head); return { root, live };
    }
    /* the sentence with its slots: text runs and pill slots inline (a long sentence wraps like text). */
    function sentenceView(sentence, multi) {
      const box = h("div", "evoui-sx" + (multi ? " multi" : "")), parts = sentence.split(BLANK), slots = [];
      parts.forEach((part, i) => {
        if (part) box.append(h("span", "evoui-sx-text", part));
        if (i < parts.length - 1) { const s = h("span", "evoui-sx-slot"), t = h("span", "evoui-sx-word"); s.append(t); s.dataset.s = "empty"; box.append(s); slots.push({ s, t }); }
      });
      return { box, slots };
    }
    /* the status line (a wrong answer: with 「再试一次」 at its end), the correct sentence (wrong) and the
       explanations */
    function feedback(el, ok, correct, why, again) {
      el.replaceChildren();
      const st = h("div", "evoui-fbk-status"); st.dataset.ok = String(ok);
      st.append(svgIcon(ok ? ICON_PATHS.check : ICON_PATHS.cross), h("span", "", uiValue(() => ok ? ui("答对了") : ui("不对"))));
      if (!ok) st.append(again);
      el.append(st);
      if (!ok) { el.append(h("div", "evoui-fbk-label", uiValue(() => ui("正确答案"))), h("div", "evoui-fbk-correct", correct)); }
      for (const t of why) if (t) el.append(h("div", "evoui-fbk-why", t));
    }
    const RETRY = ["M12.8 8a4.8 4.8 0 1 1-1.5-3.5", "M12.8 2.8v2.6h-2.6"];
    const retryBtn = (fn) => { const b = h("button", "evoui-pq-btn evoui-retry"); b.type = "button"; b.dataset.f = "retry"; b.append(svgIcon(RETRY), h("span", "", uiValue(() => ui("再试一次")))); b.onclick = fn; return b; };
    const filled = (sentence, words) => { const parts = sentence.split(BLANK); return parts.map((p0, i) => p0 + (i < words.length ? words[i] : "")).join(""); };
    /* fly(el, from): el (now in its new place) slides in from the rect `from` (a pill it came from), 200 ms */
    function fly(el, from) {
      if (!from || calm() || typeof el.animate !== "function" || !el.isConnected) return;
      const r = el.getBoundingClientRect(), dx = from.left + from.width / 2 - (r.left + r.width / 2), dy = from.top + from.height / 2 - (r.top + r.height / 2);
      try { el.animate([{ transform: `translate(${dx}px, ${dy}px)`, opacity: 0.4 }, { transform: "none", opacity: 1 }], { duration: MOVE_MS, easing: EASE }); } catch (x) { /* no WAAPI */ }
    }
    /* fill-blank (ChatGPT LearningFillBlankCard): a sentence with one dashed pill slot and the options as pills
       below. A pick fills the slot (grey), then it turns green / red (260 ms); every option dims and locks.
       Wrong: 「正确答案」 + the full correct sentence + the chosen option's `why` + the node's `why`; right: the
       option's `why` (else the node's). 「再试一次」 (wrong only) starts attempt + 1. One item event per attempt
       (item 0, response = option index) + a summary (total 1); done (sticky) at the first judgement. Keys
       (focus inside the card): 1–8 pick. Progress {attempt (≥ 1), answer (option index | null), done, ms
       (visible ms before the answer of this attempt)}. */
    function fillBlank(n) {
      const key = ctx.nodeKey(n), O = n.options;
      const p = ctx.nodeProgress(key, () => ({ attempt: 1, answer: null, done: false, ms: 0 }), (r) => fillProgressFits(n, r));
      let since = ctx.now(), judgeTimer = null;
      const { root, live } = card("evoui-fb", LINES, ui("填空"));
      const view = sentenceView(n.sentence, false), slot = view.slots[0];
      const opts = h("div", "evoui-pills"); uiAttr(opts, "role", () => "group"); uiAttr(opts, "aria-label", uiValue(() => ui("选项")));
      const pills = O.map((o, i) => { const b = h("button", "evoui-pill"); b.type = "button"; b.dataset.f = "opt-" + i; b.append(h("span", "evoui-pill-label", o.label)); b.onclick = () => pick(i); return b; });
      const shown = seededOrder(O.length, key); shown.forEach((i) => opts.append(pills[i]));
      const fb = h("div", "evoui-fbk"); uiAttr(fb, "role", () => "status");
      const again = retryBtn(() => retry());
      root.append(view.box, h("div", "evoui-pc-rule"), opts, fb, live);
      root.addEventListener("keydown", (ev) => {
        if (ev.altKey || ev.ctrlKey || ev.metaKey || !/^[1-8]$/.test(ev.key)) return;
        const i = shown[Number(ev.key) - 1]; if (i !== undefined && p.answer === null) { ev.preventDefault(); pills[i].focus({ preventScroll: true }); pick(i); }
      });
      function pick(i) {
        if (p.answer !== null) return;
        const from = pills[i].getBoundingClientRect(), t = ctx.now();
        p.ms += Math.max(0, Math.round(t - since)); since = t; p.answer = i; p.done = true;
        const correct = i === n.correct; ctx.change();
        ctx.emit({ type: "item", node: key, kind: "fill-blank", item: 0, attempt: p.attempt, response: i, correct, hintUsed: false, latencyMs: p.ms });
        ctx.emit({ type: "summary", node: key, kind: "fill-blank", attempt: p.attempt, total: 1, right: correct ? 1 : 0, missed: correct ? [] : [0], timeMs: p.ms, hintsUsed: 0 });
        ctx.markDone(key);
        paint(!calm()); fly(slot.t, from);
        if (!calm()) judgeTimer = setTimeout(() => { judgeTimer = null; paint(false, true); }, JUDGE_MS);
        uiProp(live, "textContent", uiValue(() => correct ? ui("答对了") : ui("不对，正确答案是") + O[n.correct].label));
        if (doc.activeElement && root.contains(doc.activeElement) && doc.activeElement.disabled) root.focus({ preventScroll: true });
      }
      function retry() {
        if (p.answer === null) return;
        clearTimeout(judgeTimer); judgeTimer = null;
        p.attempt += 1; p.answer = null; p.ms = 0; since = ctx.now(); ctx.change(); paint(false);
        uiProp(live, "textContent", uiValue(() => ui("再试一次"))); pills[0].focus({ preventScroll: true });
      }
      /* pending: the pick is in the slot, not judged yet (grey); enter: the verdict appears now (animated) */
      function paint(pending, enter) {
        const a = p.answer, judged = a !== null && !pending, ok = a === n.correct;
        uiProp(slot.t, "textContent", () => a === null ? "" : O[a].label);
        slot.s.dataset.s = a === null ? "active" : pending ? "filled" : ok ? "right" : "wrong";
        uiAttr(slot.s, "aria-label", uiValue(() => a === null ? ui("空") : (judged ? (ok ? ui("对：") : ui("错：")) : "") + O[a].label));
        pills.forEach((b, i) => { b.disabled = a !== null; b.dataset.dim = String(a !== null); uiAttr(b, "aria-pressed", () => String(a === i)); });
        fb.hidden = !judged;
        if (judged) {
          const own = has(O[a], "why") ? O[a].why : "";
          feedback(fb, ok, filled(n.sentence, [O[n.correct].label]), ok ? [own || n.why] : [own, n.why], again);
          if (enter) enterEl(fb);
        }
        root.dataset.state = a === null ? "open" : !judged ? "pending" : ok ? "right" : "wrong";
      }
      root.tabIndex = -1;
      paint(false); if (p.done) ctx.markDone(key, { restored: true });
      return practiceEl(n, root);
    }
    /* sentence builder (ChatGPT LearningSentenceBuilderCard): a sentence with N slots and a word bank. A
       word click fills the next empty slot (the word flies there); used words dim and show their order
       number; ↶ (or Backspace / z) takes the last word back. When every slot is filled it is judged at
       once: all right → green slots; wrong → only the wrong slots red, then 「正确答案」 + the full correct
       sentence + `why`. 「再试一次」 (wrong only) starts attempt + 1. One item event per attempt (item 0,
       response = the word indices in blank order) + a summary (total 1); done (sticky) at the first
       judgement. Progress {attempt (≥ 1), picks: [word indices placed, in order], done, ms}. */
    function sentenceBuilder(n) {
      const key = ctx.nodeKey(n), W = n.words, A = n.answer, N = A.length;
      const p = ctx.nodeProgress(key, () => ({ attempt: 1, picks: [], done: false, ms: 0 }), (r) => sentenceProgressFits(n, r));
      let since = ctx.now(), judgeTimer = null;
      const { root, live } = card("evoui-sb", BUILD, ui("拼句"));
      const view = sentenceView(n.sentence, true);
      const undo = h("button", "evoui-pq-icon evoui-sb-undo"); undo.type = "button"; undo.dataset.f = "undo"; undo.append(svgIcon(UNDO)); uiAttr(undo, "aria-label", uiValue(() => ui("撤销上一个词"))); uiProp(undo, "title", uiValue(() => ui("撤销上一个词")));
      undo.onclick = () => back();
      const top = h("div", "evoui-sb-top"); top.append(view.box, undo);
      const bank = h("div", "evoui-pills"); uiAttr(bank, "role", () => "group"); uiAttr(bank, "aria-label", uiValue(() => ui("词库")));
      const pills = W.map((w, i) => { const b = h("button", "evoui-pill"), num = h("span", "evoui-pill-n"); uiAttr(num, "aria-hidden", () => "true"); b.type = "button"; b.dataset.f = "word-" + i; b.append(num, h("span", "evoui-pill-label", w)); b.onclick = () => put(i); return { b, num }; });
      seededOrder(W.length, key).forEach((i) => bank.append(pills[i].b));   // the writer lists the answer's words first
      const fb = h("div", "evoui-fbk"); uiAttr(fb, "role", () => "status");
      const again = retryBtn(() => retry());
      root.append(top, h("div", "evoui-pc-rule"), bank, fb, live);
      root.addEventListener("keydown", (ev) => {
        if (ev.altKey || ev.ctrlKey || ev.metaKey) return;
        if ((ev.key === "Backspace" || ev.key === "z") && p.picks.length && p.picks.length < N) { ev.preventDefault(); back(); }
      });
      const right = () => p.picks.length === N && p.picks.every((x, i) => x === A[i]);
      function put(i) {
        if (p.picks.length >= N || p.picks.includes(i)) return;
        const from = pills[i].b.getBoundingClientRect(), at = p.picks.length;
        p.picks.push(i);
        const full = p.picks.length === N;
        if (full) { const t = ctx.now(); p.ms += Math.max(0, Math.round(t - since)); since = t; p.done = true; }
        ctx.change();
        if (full) {
          const ok = right();
          ctx.emit({ type: "item", node: key, kind: "sentence", item: 0, attempt: p.attempt, response: p.picks.slice(), correct: ok, hintUsed: false, latencyMs: p.ms });
          ctx.emit({ type: "summary", node: key, kind: "sentence", attempt: p.attempt, total: 1, right: ok ? 1 : 0, missed: ok ? [] : [0], timeMs: p.ms, hintsUsed: 0 });
          ctx.markDone(key);
          uiProp(live, "textContent", uiValue(() => ok ? ui("答对了") : ui("不对，正确的句子是：") + filled(n.sentence, A.map((k) => W[k]))));
        } else uiProp(live, "textContent", uiValue(() => ui("第 {0} 个空：{1}", [at + 1, W[i]])));
        paint(full && !calm()); fly(view.slots[at].t, from);
        if (full && !calm()) judgeTimer = setTimeout(() => { judgeTimer = null; paint(false, true); }, JUDGE_MS);
        if (full && doc.activeElement && root.contains(doc.activeElement) && doc.activeElement.disabled) root.focus({ preventScroll: true });
      }
      function back() {
        if (!p.picks.length || p.picks.length >= N) return;
        const i = p.picks.pop(); ctx.change(); paint(false); uiProp(live, "textContent", uiValue(() => ui("撤销了「") + W[i] + "」"));
        if (!p.picks.length && doc.activeElement === undo) pills[i].b.focus({ preventScroll: true });
      }
      function retry() {
        if (p.picks.length < N) return;
        clearTimeout(judgeTimer); judgeTimer = null;
        p.attempt += 1; p.picks = []; p.ms = 0; since = ctx.now(); ctx.change(); paint(false);
        uiProp(live, "textContent", uiValue(() => ui("再试一次"))); pills[0].b.focus({ preventScroll: true });
      }
      function paint(pending, enter) {
        const full = p.picks.length === N, judged = full && !pending, ok = right(), active = full ? -1 : p.picks.length;
        view.slots.forEach(({ s, t }, k) => {
          const w = k < p.picks.length ? p.picks[k] : null, good = w === A[k];
          uiProp(t, "textContent", () => w === null ? "" : W[w]);
          s.dataset.s = w === null ? (k === active ? "active" : "empty") : !judged ? "filled" : ok ? "right" : good ? "filled" : "wrong";
          uiAttr(s, "aria-label", uiValue(() => w === null ? ui("第 {0} 个空", [k + 1]) : (judged && !ok ? (good ? ui("对：") : ui("错：")) : "") + W[w]));
        });
        pills.forEach(({ b, num }, i) => {
          const at = p.picks.indexOf(i), used = at >= 0;
          uiProp(num, "textContent", () => used ? String(at + 1) : ""); b.dataset.used = String(used); b.dataset.dim = String(used || full);
          b.disabled = used || full; uiAttr(b, "aria-label", () => used ? `${at + 1}. ${W[i]}` : W[i]);
        });
        undo.hidden = full; undo.disabled = !p.picks.length;
        fb.hidden = !judged;
        if (judged) {
          feedback(fb, ok, filled(n.sentence, A.map((k) => W[k])), [n.why], again);
          if (enter) enterEl(fb);
        }
        root.dataset.state = !full ? "open" : !judged ? "pending" : ok ? "right" : "wrong";
      }
      root.tabIndex = -1;
      paint(false); if (p.done) ctx.markDone(key, { restored: true });
      return practiceEl(n, root);
    }
    /* pager (ChatGPT practice pager): its children are pages, one at a time, in a card — 「‹ n/N ›」 on top
       (with a step bar) and a pill 「下一题」 / 「完成」 below. A page change slides (old content leaves 120 ms,
       the new page enters 180 ms + 4 px). `gate`: › and 「下一题」 / 「完成」 only once every practice node on
       the page (quiz, flashcards, fill-blank, sentence) is done; a page without one is always complete.
       「完成」 on the last page: done (sticky) + a summary (kind "pager", total / right over every practice
       item inside its pages, missed = the keys of the practice nodes not all right, timeMs = time it was
       shown) + confetti when every item was right (once, never on restore). ← → on its buttons page.
       Progress {page, done, celebrated, ms}. */
    function pager(n, U) {
      const key = ctx.nodeKey(n), N = n.children.length;
      const p = ctx.nodeProgress(key, () => ({ page: 0, done: false, celebrated: false, ms: 0 }), (r) => isObj(r) && Object.keys(r).length === 4 && isInt(r.page) && r.page >= 0 && r.page < N
        && typeof r.done === "boolean" && typeof r.celebrated === "boolean" && (!r.celebrated || r.done) && isInt(r.ms) && r.ms >= 0);
      let since = ctx.now(), inner = [], pageEl = null;
      const root = h("div", "evoui-pg"); uiAttr(root, "role", () => "group"); uiAttr(root, "aria-label", uiValue(() => ui("练习")));
      const head = h("div", "evoui-pg-head"), bar = h("div", "evoui-pg-bar"); uiAttr(bar, "aria-hidden", () => "true");
      const segs = n.children.map(() => { const s = h("i"); bar.append(s); return s; });
      const nav = h("div", "evoui-pq-nav");
      const prev = h("button", "evoui-pq-icon"); prev.type = "button"; prev.dataset.f = "prev"; prev.append(svgIcon(CHEV_L)); uiAttr(prev, "aria-label", uiValue(() => ui("上一页"))); prev.onclick = () => go(p.page - 1);
      const next = h("button", "evoui-pq-icon"); next.type = "button"; next.dataset.f = "fwd"; next.append(svgIcon(CHEV_R)); uiAttr(next, "aria-label", uiValue(() => ui("下一页"))); next.onclick = () => go(p.page + 1);
      const count = h("span", "evoui-pq-count"); uiAttr(count, "role", () => "status");
      nav.append(prev, count, next); head.append(bar, nav);
      const slot = h("div", "evoui-pg-view");
      const foot = h("div", "evoui-pg-foot"), tip = h("span", "evoui-pg-tip", uiValue(() => ui("做完这一页的练习再继续")));
      const act = h("button", "evoui-pq-btn"); act.type = "button"; act.onclick = () => (p.page < N - 1 ? go(p.page + 1) : finish());
      foot.append(tip, act);
      const live = h("div", "evoui-sr"); uiAttr(live, "aria-live", () => "polite");
      root.append(head, slot, foot, live);
      root.addEventListener("keydown", (ev) => {
        if (ev.altKey || ev.ctrlKey || ev.metaKey || !(head.contains(ev.target) || foot.contains(ev.target))) return;
        if (ev.key === "ArrowLeft") { ev.preventDefault(); go(p.page - 1); } else if (ev.key === "ArrowRight") { ev.preventDefault(); go(p.page + 1); }
      });
      /* the practice nodes of the current page that are not done (ghosts and nested practice parts skipped) */
      const blockers = () => {
        if (!pageEl) return [];
        return [...pageEl.querySelectorAll("[data-practice]")].filter((el) => !leaving(el) && !(el.parentElement && el.parentElement.closest("[data-practice]") && pageEl.contains(el.parentElement.closest("[data-practice]")))
          && !ctx.isDone(el.dataset.practice));
      };
      const open = () => !n.gate || !blockers().length;
      const account = () => { const t = ctx.now(); if (!p.done) p.ms += Math.max(0, Math.round(t - since)); since = t; };
      function show(first) {
        inner = [];
        let el;
        try { el = h("div", "evoui-pg-page"); el.tabIndex = -1; el.append(build(n.children[p.page], inner)); } catch (x) { fallback(x); return; }
        pageEl = el; swapIn(slot, el, { instant: first });
      }
      function go(k) {
        if (k < 0 || k >= N || k === p.page || (k > p.page && !open())) return;
        account(); p.page = k; ctx.change(); show(false); update();
        uiProp(live, "textContent", uiValue(() => ui("第 {0} 页，共 {1} 页", [k + 1, N])));
        const a = doc.activeElement; if (!a || !root.contains(a) || a.disabled) (pageEl || root).focus({ preventScroll: true });
      }
      function finish() {
        if (p.done || p.page !== N - 1 || !open()) return;
        account(); p.done = true;
        const s = score(), cheer = s.total > 0 && s.right === s.total && !p.celebrated; if (cheer) p.celebrated = true;
        ctx.change();
        ctx.emit({ type: "summary", node: key, kind: "pager", attempt: 1, total: s.total, right: s.right, missed: s.missed, timeMs: p.ms, hintsUsed: s.hints });
        ctx.markDone(key); paint();
        uiProp(live, "textContent", uiValue(() => s.total ? ui("完成：答对 {0} / {1}", [s.right, s.total]) : ui("完成")));
        if (cheer) celebrate(doc);
      }
      /* total / right / hints over every practice item inside the pages, from their progress */
      function score() {
        const out = { total: 0, right: 0, hints: 0, missed: [] };
        (function walk(m, inIf) {
          if (!isObj(m)) return;
          if (m !== n && PRACTICE.includes(m.kind) && m.kind !== "pager") {
            const k = ctx.nodeKey(m), s = scoreOf(m, k);
            if (s && !(inIf && s.unseen)) { out.total += s.total; out.right += s.right; out.hints += s.hints; if (s.right < s.total) out.missed.push(k); }
          }
          for (const c of Array.isArray(m.children) ? m.children : []) walk(c, inIf);
          walk(m.then, true); walk(m.else, true);
        })(n, false);
        return out;
      }
      function paint() {
        const last = p.page === N - 1, ok = open();
        uiProp(count, "textContent", () => `${p.page + 1}/${N}`); uiAttr(count, "aria-label", uiValue(() => ui("第 {0} 页，共 {1} 页", [p.page + 1, N])));
        prev.disabled = p.page === 0; next.disabled = last || !ok;
        segs.forEach((s, i) => { s.dataset.on = i === p.page ? "now" : i < p.page ? "past" : "next"; });
        act.dataset.f = last ? "finish" : "next";
        if (last && p.done) { act.replaceChildren(svgIcon(ICON_PATHS.check, "evoui-pg-ok"), uiNode(uiValue(() => ui("已完成")))); act.disabled = true; act.classList.remove("primary"); }
        else { uiProp(act, "textContent", uiValue(() => last ? ui("完成") : ui("下一题"))); act.disabled = !ok; act.classList.toggle("primary", last && ok); }
        if (p.done) { const s = score(); uiProp(tip, "textContent", uiValue(() => s.total ? ui("答对 {0} / {1}", [s.right, s.total]) : ui("已完成"))); tip.dataset.done = "true"; tip.hidden = false; }
        else { uiProp(tip, "textContent", uiValue(() => ui("做完这一页的练习再继续"))); tip.dataset.done = "false"; tip.hidden = ok; }
        root.dataset.page = String(p.page); root.dataset.open = String(ok);
      }
      const offs = pagerOffs.get(n); if (offs) offs();                 // an earlier build of this node stops listening
      const off = ctx.onDone("*", () => { if (!building && (!root.isConnected || leaving(root))) { off(); return; } paint(); });
      pagerOffs.set(n, off);
      U.push((E) => { inner.forEach((f) => f(E)); paint(); });
      show(true); paint(); if (p.done) ctx.markDone(key, { restored: true });
      return practiceEl(n, root);
    }
    /* a practice node's {total, right, hints, unseen} from its live (or restored, if it fits) progress */
    function scoreOf(m, k) {
      const fits = m.kind === "quiz" ? (m.layout === "paged" ? pagedProgressFits : quizProgressFits) : m.kind === "flashcards" ? cardsProgressFits : m.kind === "fill-blank" ? fillProgressFits : sentenceProgressFits;
      const r = ctx.peek(k); let p = null; try { if (r != null && fits(m, r)) p = r; } catch (x) { p = null; }
      const unseen = p === null;
      if (m.kind === "quiz") {
        const Q = m.questions;
        if (m.layout === "paged") { const order = p ? p.order : Q.map((_, i) => i); return { unseen, total: order.length, right: p ? order.filter((j) => p.judged[j] && quizRight(Q[j], p.answers[j])).length : 0, hints: p ? order.filter((j) => p.hintUsed[j]).length : 0 }; }
        return { unseen, total: Q.length, right: p && p.attempt > 0 ? Q.filter((q, i) => quizRight(q, p.answers[i])).length : 0, hints: p && p.hintUsed ? p.hintUsed.filter(Boolean).length : 0 };
      }
      if (m.kind === "flashcards") return { unseen, total: p ? p.order.length : m.cards.length, right: p ? p.history.filter((x) => x.dir === "right").length : 0, hints: p ? p.hintUsed.length : 0 };
      if (m.kind === "fill-blank") return { unseen, total: 1, right: p && p.answer === m.correct ? 1 : 0, hints: 0 };
      return { unseen, total: 1, right: p && p.picks.length === m.answer.length && p.picks.every((x, i) => x === m.answer[i]) ? 1 : 0, hints: 0 };
    }
    /* route (choice router; ChatGPT MultipleChoiceBlockV2 branch mode / HomeworkHelper): a prompt and 2–6
       option cards (label + desc). A choice is highlighted (the others dim) and sends a route event
       {option: value, next, preference}; choosing another one later sends another. `bind`: the state
       is set to the chosen value. The highlight is the reader's own choice (progress), never the bound
       state: before a choice nothing is marked, even if the state already equals a value, and choosing
       that option still sends the event. Progress {chosen: option index | null}. Arrow keys move between the options. */
    /* app (EvoUI v2 tier 4): a sandboxed HTML mini-app — the one node whose content is markup. It runs in
       <iframe sandbox="allow-scripts"> (never allow-same-origin / top-navigation / popups / forms / modals)
       whose srcdoc is appSrcdoc(): CSP meta, host tokens, bridge script, then the app's html. The bridge
       (window.evowiki inside the frame) talks to this node by postMessage on a per-render random channel;
       a message is accepted only from this iframe's window, on that channel, of a known type and shape,
       ≤ APP.message characters (a save ≤ APP.save), within APP.events accepted messages per render.
       Anything else is dropped with console.warn and counted (handle.rejected()). Messages from other
       windows that do not name this channel are not ours and are ignored silently.
       Accepted: ready (the app is up; the fallback stays hidden), item(id, response, correct, hintUsed,
       latencyMs) -> D3 item event {kind: "app", item: id, attempt: round}, summary({total, right,
       missed}) -> D3 summary (closes the round: round + 1), save(progress) -> node progress `saved`,
       resize(h) -> frame height clamped to APP.minResize–APP.maxResize. An item id is accepted at most
       APP.itemReports times per render, a report identical to the previous one (id, response, correct) is
       skipped silently, and latencyMs is the app's own only when ≤ APP.latency (else measured, also ≤ it).
       The parent posts {type: "init", progress: saved, theme} and later {type: "theme", theme} (when the
       host theme changes) only while the frame still shows its first load (the srcdoc). A later load
       means the frame navigated or reloaded itself: it is removed (src about:blank first, counted in
       rejected()) and the fallback shows with 「重新载入」, which builds a fresh frame (new channel) from
       the same srcdoc and sends init with the saved progress again. No ready within APP.readyMs, or no way
       to run a frame here: the block's fallback text shows under the frame (the frame stays). Theme
       listeners and the frame stop once the node is gone (lifeOf, checked every 2 s and on each event) or
       at handle.destroy(). A node detached (stopped) and put back again: its frame reloads with nobody
       listening, so that frame is removed and the fallback shows with 「重新载入」 (which re-arms the
       watchers and launches a fresh frame); after destroy() nothing is offered.
       Progress {round (≥ 1), saved (JSON ≤ APP.save | null)}. */
    function app(n, U) {
      const key = ctx.nodeKey(n), declared = new Set((n.items || []).map((it) => it.id));
      const p = ctx.nodeProgress(key, () => ({ round: 1, saved: null }), appProgressFits);
      const wrap = h("div", "evoui-app"); wrap.dataset.node = key; wrap.dataset.state = "loading"; wrap.dataset.ready = "false";
      const fb = h("div", "evoui-app-fallback"); fb.hidden = true; uiAttr(fb, "role", () => "note");
      const again = h("button", "evoui-btn evoui-app-reload", uiValue(() => ui("重新载入"))); again.type = "button"; again.hidden = true;
      fb.append(h("div", "evoui-app-fallback-label", uiValue(() => ui("小程序没有加载出来，这是文字版："))), h("div", "evoui-app-fallback-text", block.fallback), again);
      wrap.append(fb);
      const pictures = new Map((n.images || []).map((im) => [im.id, im]));
      if (pictures.size) {
        const credit = h("div", "evoui-app-credit");
        for (const im of pictures.values()) credit.append(h("div", "", uiValue(() => [ui("图"), im.title, im.author, im.license, im.source].filter(Boolean).join(" · "))));
        wrap.append(credit);
      }
      const showFallback = (why, reload) => { fb.hidden = false; again.hidden = !reload; wrap.dataset.state = "fallback"; wrap.dataset.fallback = why; };
      const reports = new Map();                  // item id -> accepted reports this render (≤ APP.itemReports)
      let lastItem = null;                        // the last accepted item report [id, response, correct] (JSON)
      let started = false;
      U.push(() => { if (started) return; started = true; try { start(); } catch (x) { console.error(x); showFallback("error"); } });
      return wrap;

      function start() {
        const win = doc.defaultView, cr = win && win.crypto;
        if (!win || !cr || typeof cr.getRandomValues !== "function" || !("srcdoc" in doc.createElement("iframe")) || typeof win.addEventListener !== "function") return showFallback("unsupported");
        const gone = ctx.life(wrap);
        let theme = appTheme(wrap), live = null;  // live: the current frame instance ({retheme, stop})
        /* theme listeners belong to the node, not to one frame; they (and the frame) stop once the node is gone */
        const mq = win.matchMedia ? win.matchMedia("(prefers-color-scheme: dark)") : null;
        const mo = typeof win.MutationObserver === "function" ? new win.MutationObserver(() => retheme()) : null;
        let watch = null, armed = false;
        const stopAll = () => {
          if (live) live.stop();
          if (!armed) return; armed = false;
          if (mq && mq.removeEventListener) mq.removeEventListener("change", retheme); if (mo) mo.disconnect(); clearInterval(watch); watch = null;
        };
        /* the node's watchers (theme, the 2 s gone check); armed again when 「重新载入」 relaunches a node that
           was detached (stopAll) and has come back */
        const arm = () => {
          if (armed) return; armed = true; theme = appTheme(wrap);
          if (mq && mq.addEventListener) mq.addEventListener("change", retheme);
          if (mo) for (const el of [doc.documentElement, doc.body]) if (el) mo.observe(el, { attributes: true, attributeFilter: ["class", "data-theme"] });
          watch = setInterval(() => { if (gone()) stopAll(); }, 2000);
        };
        function retheme() {
          if (gone()) return stopAll();
          const t = appTheme(wrap); if (JSON.stringify(t) === JSON.stringify(theme)) return;
          theme = t; if (live) live.retheme(t);
        }
        ctx.onDestroy(stopAll);
        arm();
        again.onclick = () => { if (gone()) return; arm(); fb.hidden = true; again.hidden = true; wrap.dataset.state = "loading"; delete wrap.dataset.fallback; launch(); };
        launch();

        /* one frame: a new channel nonce, a new iframe from the same srcdoc. Its first load is the srcdoc;
           any later load means the frame navigated (or reloaded) itself: the frame is dropped (counted in
           rejected()) and the fallback offers 「重新载入」, which launches a fresh frame. */
        function launch() {
          const bytes = new Uint8Array(16); cr.getRandomValues(bytes);
          const nonce = "evoui-" + Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
          const f = h("iframe", "evoui-app-frame");
          let last = ctx.now(), roundAt = last, hints = 0, ready = false, loads = 0, seen = 0, timer = null, cw = null, dead = false;
          uiAttr(f, "sandbox", () => APP.sandbox); uiAttr(f, "referrerpolicy", () => "no-referrer"); uiAttr(f, "title", () => block.title);
          f.style.height = (n.height ?? APP.height) + "px"; f.style.colorScheme = theme.name;
          f.srcdoc = appSrcdoc(n.html, nonce, theme, language);
          if (n.html.includes("data-evowiki-locale=")) managedFrames.add(f);
          wrap.dataset.ready = "false";
          const post = (m) => { if (dead || loads !== 1 || !cw) return; try { cw.postMessage(Object.assign({ channel: nonce }, m), "*"); } catch (x) { console.warn("EvoUI app " + key + ": " + x); } };
          const stop = () => { if (dead) return; dead = true; managedFrames.delete(f); win.removeEventListener("message", onMsg); clearTimeout(timer); };
          live = { stop, retheme: (t) => { f.style.colorScheme = t.name; post({ type: "theme", theme: t }); } };
          const SHAPES = { ready: [], item: ["id", "response", "correct", "hintUsed", "latencyMs"], summary: ["total", "right", "missed"], save: ["progress"], resize: ["height"], image: ["req", "id"] };
          function why(d, ours) {
            if (!ours) return "not from this app's frame";
            if (!isObj(d)) return "not an object";
            if (d.channel !== nonce) return "wrong channel";
            if (appAccepted >= APP.events) return "more than " + APP.events + " messages in this render";
            const spec = has(SHAPES, d.type) ? SHAPES[d.type] : null;
            if (!spec) return "unknown type " + String(d.type).slice(0, 24);
            for (const k of Object.keys(d)) if (k !== "channel" && k !== "type" && !spec.includes(k)) return "unknown field " + k.slice(0, 24);
            if (d.type === "item") {
              if (!(isStr(d.id) && declared.has(d.id))) return "item " + String(d.id).slice(0, 40) + " is not one of the app's items";
              if (has(d, "correct") && d.correct !== null && typeof d.correct !== "boolean") return "correct must be true / false / null";
              if (has(d, "hintUsed") && typeof d.hintUsed !== "boolean") return "hintUsed must be true / false";
              if (has(d, "latencyMs") && d.latencyMs !== null && !(isInt(d.latencyMs) && d.latencyMs >= 0)) return "latencyMs must be a whole number ≥ 0";
              if ((reports.get(d.id) || 0) >= APP.itemReports) return "item " + d.id + " already reported " + APP.itemReports + " times in this render";
            } else if (d.type === "summary") {
              if (!(isInt(d.total) && d.total >= 0 && d.total <= 10000)) return "total must be a whole number 0–10000";
              if (!(isInt(d.right) && d.right >= 0 && d.right <= d.total)) return "right must be a whole number 0–total";
              if (has(d, "missed") && !(Array.isArray(d.missed) && d.missed.length <= APP.items && d.missed.every((m) => isStr(m) && declared.has(m)) && new Set(d.missed).size === d.missed.length)) return "missed must list the app's item ids";
            } else if (d.type === "image") {
              if (!(isInt(d.req) && isStr(d.id) && pictures.has(d.id))) return "image " + String(d.id).slice(0, 40) + " is not one of the app's images";
            } else if (d.type === "resize") {
              if (!isNum(d.height)) return "height must be a number";
            } else if (d.type === "save" && !has(d, "progress")) return "save needs a progress";
            if (!jsonOnly(d)) return "not plain JSON (or too big)";
            if (d.type === "save") { if (JSON.stringify(d.progress).length > APP.save) return "progress larger than " + APP.save + " characters"; }
            else if (JSON.stringify(d).length > APP.message) return "larger than " + APP.message + " characters";
            return null;
          }
          function take(d) {
            const t = ctx.now();
            if (d.type === "ready") { if (!ready) { ready = true; wrap.dataset.ready = "true"; wrap.dataset.state = "ready"; fb.hidden = true; clearTimeout(timer); } }
            else if (d.type === "resize") f.style.height = Math.round(Math.min(APP.maxResize, Math.max(APP.minResize, d.height))) + "px";
            else if (d.type === "save") { p.saved = clone(d.progress); ctx.change(); }
            else if (d.type === "image") {
              const answer = (got) => post(got ? { type: "image", req: d.req, mime: got.mime, bytes: got.bytes } : { type: "image", req: d.req });
              Promise.resolve().then(() => ctx.image(pictures.get(d.id))).then(answer, (x) => { console.warn("EvoUI app " + key + ": " + x); answer(null); });
            }
            else if (d.type === "item") {
              const response = has(d, "response") ? d.response : null, correct = typeof d.correct === "boolean" ? d.correct : null;
              const same = JSON.stringify([d.id, response, correct]);
              if (same === lastItem) return;                          // the same report again: once is enough
              lastItem = same; reports.set(d.id, (reports.get(d.id) || 0) + 1);
              const own = isInt(d.latencyMs) && d.latencyMs <= APP.latency;
              const lat = own ? d.latencyMs : Math.min(APP.latency, Math.max(0, Math.round(t - last))), hint = d.hintUsed === true;
              last = t; if (hint) hints += 1;
              ctx.change();
              ctx.emit({ type: "item", node: key, kind: "app", item: d.id, attempt: p.round, response, correct, hintUsed: hint, latencyMs: lat });
            } else if (d.type === "summary") {
              last = t; ctx.change();
              ctx.emit({ type: "summary", node: key, kind: "app", attempt: p.round, total: d.total, right: d.right, missed: has(d, "missed") ? d.missed.slice() : [],
                timeMs: Math.max(0, Math.round(t - roundAt)), hintsUsed: hints });
              p.round += 1; hints = 0; roundAt = t; lastItem = null; ctx.change(); ctx.markDone(key);
            }
          }
          function onMsg(ev) {
            const d = ev.data, ours = ev.source != null && ev.source === f.contentWindow;
            if (!ours && !(isObj(d) && d.channel === nonce)) return;        // someone else's message
            if (gone()) return stopAll();
            if ((seen += 1) > APP.events * 4) { if (seen === APP.events * 4 + 1) console.warn("EvoUI app " + key + ": too many messages; no longer listening"); appRejected += 1; return stop(); }
            const bad = why(d, ours);
            if (bad) { appRejected += 1; console.warn("EvoUI app " + key + ": dropped a message — " + bad); return; }
            if (d.type !== "resize") appAccepted += 1;               // the frame following its content is not the app talking
            take(d);
          }
          f.addEventListener("load", () => {
            if (dead) {
              // a stopped frame loaded again: the node was detached (its bridge stopped) and put back, so the
              // frame re-ran its srcdoc with nobody to talk to — drop it and offer 「重新载入」 (not after destroy)
              if (!f.isConnected) return;
              f.removeAttribute("srcdoc"); f.src = "about:blank"; f.remove();
              wrap.dataset.ready = "false";
              if (!gone()) { console.warn("EvoUI app " + key + ": the frame came back after its node was detached; offering a reload"); showFallback("detached", true); }
              return;
            }
            loads += 1;
            if (loads === 1) {
              if (gone()) return stopAll();
              cw = f.contentWindow; last = roundAt = ctx.now();
              post({ type: "init", progress: clone(p.saved), theme });
              return;
            }
            // a later load: the frame navigated or reloaded itself — drop it, never talk to what it shows now
            stop(); appRejected += 1;
            console.warn("EvoUI app " + key + ": the frame navigated away; removed it");
            f.removeAttribute("srcdoc"); f.src = "about:blank"; f.remove();
            wrap.dataset.ready = "false"; showFallback("navigated", true);
          });
          win.addEventListener("message", onMsg);
          timer = setTimeout(() => { timer = null; if (!ready && !dead && !gone()) showFallback("timeout"); }, APP.readyMs);
          wrap.prepend(f);
        }
      }
    }
    function route(n, U) {
      const key = ctx.nodeKey(n), O = n.options, bound = has(n, "bind");
      const p = ctx.nodeProgress(key, () => ({ chosen: null }), (r) => isObj(r) && Object.keys(r).length === 1 && (r.chosen === null || (isInt(r.chosen) && r.chosen >= 0 && r.chosen < O.length)));
      const root = h("div", "evoui-rt"), prompt = h("div", "evoui-rt-prompt"), list = h("div", "evoui-rt-opts");
      uiAttr(list, "role", () => "group");
      const live = h("div", "evoui-sr"); uiAttr(live, "aria-live", () => "polite");
      const bs = O.map((o, i) => {
        const b = h("button", "evoui-rt-opt"); b.type = "button"; b.dataset.f = "route-" + i;
        const body = h("span", "evoui-rt-body"); body.append(h("span", "evoui-rt-label", o.label)); if (has(o, "desc")) body.append(h("span", "evoui-rt-desc", o.desc));
        const mark = h("span", "evoui-rt-mark"); uiAttr(mark, "aria-hidden", () => "true");
        b.append(body, mark); b.onclick = () => choose(i); list.append(b); return { b, mark };
      });
      root.append(prompt, list, live);
      list.addEventListener("keydown", (ev) => {
        if (ev.altKey || ev.ctrlKey || ev.metaKey) return;
        const d = ev.key === "ArrowDown" || ev.key === "ArrowRight" ? 1 : ev.key === "ArrowUp" || ev.key === "ArrowLeft" ? -1 : 0;
        const i = bs.findIndex(({ b }) => b === doc.activeElement); if (!d || i < 0) return;
        ev.preventDefault(); bs[(i + d + O.length) % O.length].b.focus({ preventScroll: true });
      });
      const current = () => p.chosen ?? -1;      // the reader's own choice (a bound state that already equals a value is not one)
      function choose(i) {
        if (i === p.chosen) return;
        const o = O[i]; p.chosen = i; if (bound) state[n.bind] = o.value;
        ctx.change();
        ctx.emit({ type: "route", node: key, option: o.value, next: o.next, preference: n.preference ?? null });
        uiProp(live, "textContent", uiValue(() => ui("已选：") + o.label));
        if (bound) update(); else paint();
      }
      function paint() {
        const c = current();
        bs.forEach(({ b, mark }, i) => {
          const st = c < 0 ? "idle" : c === i ? "chosen" : "other";
          if (b.dataset.state !== st) { b.dataset.state = st; mark.replaceChildren(svgIcon(st === "chosen" ? ICON_PATHS.check : CHEV_R)); }
          uiAttr(b, "aria-pressed", () => String(c === i));
        });
        root.dataset.chosen = String(c);
      }
      U.push((E) => { uiProp(prompt, "textContent", () => template(n.prompt, E)); uiAttr(list, "aria-label", () => prompt.textContent); paint(); });
      paint(); return root;
    }
    function fallback(x) { host.replaceChildren(h("div", "evoui-error", uiValue(() => ui("交互出错，改显示文字：") + block.fallback))); console.error(x); }
    function update() { try { const E = env(); ups.forEach((f) => f(E)); } catch (x) { fallback(x); } }
    try { ups = []; host.replaceChildren(build(block.body, ups)); update(); }
    catch (x) { fallback(x); }
    const results = () => ({ state: JSON.parse(JSON.stringify(state)), answers: [...answered.values()] });
    return { state, setLanguage, set: (k, v) => { ctx.sliding = true; set(k, v); ctx.sliding = false; }, results, progress: ctx.progress, events: ctx.events, ack: ctx.ack, flush: ctx.flush, rejected: () => appRejected,
      destroy: () => { bindings.clear(); managedFrames.clear(); return ctx.destroy(); } };
  }

  /* ---------- progress, events, node keys (EvoUI v2 D1–D3; see the render comment) ---------- */
  const clone = (x) => JSON.parse(JSON.stringify(x));
  const jsonType = (x) => (x === null ? "null" : Array.isArray(x) ? "array" : typeof x === "number" ? (isFinite(x) ? "number" : "bad") : typeof x);
  /* node -> key for every stateful node, in document order (children, then `then`, then `else`). */
  function keyMap(body) {
    const out = new Map(), count = {};
    (function walk(n) {
      if (!isObj(n)) return;
      if (STATEFUL.includes(n.kind)) { const i = count[n.kind] = (count[n.kind] ?? -1) + 1; out.set(n, isStr(n.id) ? n.id : `${n.kind}#${i}`); }
      if (Array.isArray(n.children)) n.children.forEach(walk);
      walk(n.then); walk(n.else);
    })(body);
    return out;
  }
  const nodeKeys = (block) => [...keyMap(isObj(block) ? block.body : null).values()];
  /* a question's response is right: the option (single / tf), or exactly the right set (multi, sorted) */
  function quizRight(q, a) {
    if (q.type !== "multi") return a === q.correct;
    const k = q.correct.slice().sort((x, y) => x - y);
    return Array.isArray(a) && a.length === k.length && a.every((x, i) => x === k[i]);
  }
  /* a diagnostic quiz's per-case lines over the questions `qis` (each case that has one of them):
     [「label：right / total[ — advice]」 (advice only when not all right), all right] */
  function caseLines(n, qis, right) {
    const out = [];
    for (const c of n.cases || []) {
      const qs = qis.filter((qi) => n.questions[qi].case === c.id); if (!qs.length) continue;
      const ok = qs.filter(right).length;
      out.push([`${c.label}：${ok} / ${qs.length}` + (ok < qs.length && c.advice ? ` — ${c.advice}` : ""), ok === qs.length]);
    }
    return out;
  }
  /* one question's stored response: null, an option index, or (multi) a sorted non-empty index list */
  function responseFits(q, a) {
    if (a === null) return true;
    const ok = (x) => isInt(x) && x >= 0 && x < q.options.length;
    if (q.type !== "multi") return ok(a);
    return Array.isArray(a) && a.length > 0 && a.every((x, i) => ok(x) && (i === 0 || x > a[i - 1]));
  }
  const boolsOf = (x, len) => Array.isArray(x) && x.length === len && x.every((b) => typeof b === "boolean");
  function quizProgressFits(n, r) {
    const Q = n.questions, hinted = Q.some((q) => has(q, "hint"));
    return isObj(r) && Array.isArray(r.answers) && r.answers.length === Q.length
      && r.answers.every((a, qi) => responseFits(Q[qi], a))
      && typeof r.checked === "boolean" && typeof r.done === "boolean" && isInt(r.attempt) && r.attempt >= 0
      && !(r.checked && r.answers.some((a) => a === null)) && !(r.done && r.attempt < 1)
      && (hinted ? (!has(r, "hints") && !has(r, "hintUsed")) || (boolsOf(r.hints, Q.length) && boolsOf(r.hintUsed, Q.length)) : !has(r, "hints") && !has(r, "hintUsed"));
  }
  function pagedProgressFits(n, r) {
    const Q = n.questions, N = Q.length;
    if (!(isObj(r) && r.layout === "paged" && (r.phase === "quiz" || r.phase === "results") && isInt(r.attempt) && r.attempt >= 1)) return false;
    if (!(Array.isArray(r.order) && r.order.length >= 1 && r.order.every((i) => isInt(i) && i >= 0 && i < N) && new Set(r.order).size === r.order.length)) return false;
    if (!(isInt(r.current) && r.current >= 0 && r.current < r.order.length)) return false;
    if (!(Array.isArray(r.answers) && r.answers.length === N && r.answers.every((a, i) => responseFits(Q[i], a)))) return false;
    if (!(boolsOf(r.judged, N) && r.judged.every((j, i) => !j || r.answers[i] !== null) && boolsOf(r.hintUsed, N))) return false;
    if (!(Array.isArray(r.seenMs) && r.seenMs.length === N && r.seenMs.every((x) => isInt(x) && x >= 0))) return false;
    if (!(typeof r.hintOpen === "boolean" && typeof r.done === "boolean")) return false;
    if (!(isInt(r.celebrated) && r.celebrated >= 0 && r.celebrated <= r.attempt && isInt(r.requested) && r.requested >= 0 && r.requested <= r.attempt)) return false;
    const finished = r.order.every((i) => r.judged[i]);
    return (r.phase !== "results" || finished) && (!finished || r.done);
  }
  /* how a state of numbers nests ("n", "[n]", "[[n]]"; null: it holds something else): guard.js proves a block from its
     initial state, so a restored state keeps that shape */
  const numberShape = (v) => {
    if (typeof v === "number" || typeof v === "boolean") return "n";
    if (!Array.isArray(v)) return null;
    const inner = v.map(numberShape);
    return inner.includes(null) || new Set(inner).size > 1 ? null : `[${inner[0] ?? "n"}]`;
  };
  /* a restored fill-blank / sentence builder progress fits the node (see fillBlank() / sentenceBuilder()) */
  function fillProgressFits(n, r) {
    return isObj(r) && Object.keys(r).length === 4 && isInt(r.attempt) && r.attempt >= 1 && typeof r.done === "boolean" && isInt(r.ms) && r.ms >= 0
      && (r.answer === null || (isInt(r.answer) && r.answer >= 0 && r.answer < n.options.length && r.done)) && (!r.done || r.attempt > 1 || r.answer !== null);
  }
  function sentenceProgressFits(n, r) {
    const N = n.answer.length;
    return isObj(r) && Object.keys(r).length === 4 && isInt(r.attempt) && r.attempt >= 1 && typeof r.done === "boolean" && isInt(r.ms) && r.ms >= 0
      && Array.isArray(r.picks) && r.picks.length <= N && r.picks.every((x) => isInt(x) && x >= 0 && x < n.words.length) && new Set(r.picks).size === r.picks.length
      && (r.picks.length < N || r.done) && (!r.done || r.attempt > 1 || r.picks.length === N);
  }
  /* a restored flashcards progress fits the node (see flashcards() in render for the shape) */
  function cardsProgressFits(n, r) {
    const N = n.cards.length, card = (i) => isInt(i) && i >= 0 && i < N;
    if (!(isObj(r) && (r.phase === "cards" || r.phase === "summary") && (r.round === "all" || r.round === "missed") && isInt(r.attempt) && r.attempt >= 1)) return false;
    if (!(Array.isArray(r.order) && r.order.length >= 1 && r.order.every(card) && new Set(r.order).size === r.order.length && (r.round !== "all" || r.order.length === N))) return false;
    if (!(isInt(r.index) && r.index >= 0 && r.index <= r.order.length && (r.index === r.order.length) === (r.phase === "summary"))) return false;
    if (!(Array.isArray(r.history) && r.history.length === r.index && r.history.every((x, i) => isObj(x) && x.card === r.order[i] && typeof x.flipped === "boolean"
      && (x.dir === "left" || x.dir === "right") && isInt(x.ms) && x.ms >= 0))) return false;
    const missed = r.history.filter((x) => x.dir === "left").map((x) => x.card);
    if (!(Array.isArray(r.missed) && r.missed.length === missed.length && r.missed.every((c, i) => c === missed[i]))) return false;
    if (!(typeof r.flipped === "boolean" && typeof r.hint === "boolean" && typeof r.done === "boolean")) return false;
    if (r.phase === "summary" ? r.flipped || r.hint || !r.done : r.hint && (r.flipped || !has(n.cards[r.order[r.index]], "hint"))) return false;
    if (!(Array.isArray(r.hintUsed) && r.hintUsed.every((c) => r.order.includes(c)) && new Set(r.hintUsed).size === r.hintUsed.length)) return false;
    return isInt(r.celebrated) && r.celebrated >= 0 && r.celebrated <= r.attempt && isInt(r.requested) && r.requested >= 0 && r.requested <= r.attempt;
  }
  /* Confetti over the whole window (a fixed canvas that ignores the pointer, removed when it is done),
     in the host's data and mastery colours. Nothing under prefers-reduced-motion. Shared by every node
     that celebrates (callers fire it only at a not-done -> done moment, never on restore). */
  function celebrate(doc) {
    const win = doc && doc.defaultView;
    if (!win || !doc.body || typeof win.requestAnimationFrame !== "function") return false;
    try { if (win.matchMedia && win.matchMedia("(prefers-reduced-motion: reduce)").matches) return false; } catch (x) { /* no matchMedia: animate */ }
    const okColor = (v) => { try { return !!v && (!win.CSS || win.CSS.supports("color", v)); } catch (x) { return false; } };
    const css = [doc.body, doc.documentElement].map((el) => win.getComputedStyle(el));   // Obsidian sets its theme variables on body
    const colors = [["--viz-1", "#2f6fdf"], ["--viz-2", "#e07b2a"], ["--known", "#2f9e5b"], ["--shaky", "#d99a1e"], ["--gap", "#d9483b"]]
      .map(([k, fb]) => { for (const c of css) { const v = c.getPropertyValue(k).trim(); if (okColor(v)) return v; } return fb; });
    const c = doc.createElement("canvas"); c.className = "evoui-confetti"; c.setAttribute("aria-hidden", "true");
    Object.assign(c.style, { position: "fixed", left: "0", top: "0", width: "100%", height: "100%", pointerEvents: "none", zIndex: "2147483000" });
    const g = c.getContext && c.getContext("2d"); if (!g) return false;
    doc.body.append(c);
    const dpr = Math.min(2, win.devicePixelRatio || 1); let W = 0, H = 0;
    const size = () => { W = win.innerWidth; H = win.innerHeight; c.width = Math.round(W * dpr); c.height = Math.round(H * dpr); g.setTransform(dpr, 0, 0, dpr, 0, 0); };
    size(); win.addEventListener("resize", size);
    const R = Math.random, count = W < 520 ? 110 : 190, lift = Math.sqrt(2 * 0.32 * H * 0.8), parts = [];
    for (let k = 0; k < count; k++) {
      const left = k % 2 === 0, delay = R() < 0.75 ? 0 : 8 + R() * 22;
      parts.push({ x: left ? -8 : W + 8, y: H * (0.62 + R() * 0.12), vx: (left ? 1 : -1) * (0.25 + R() * 0.85) * Math.max(4, W / 95), vy: -(0.55 + R() * 0.5) * lift,
        w: 6 + R() * 6, h: 3 + R() * 4, rot: R() * Math.PI, spin: (R() - 0.5) * 0.35, wob: R() * 6.28, color: colors[k % colors.length], round: R() < 0.22, delay });
    }
    const LIFE = 3600, FADE = 900, t0 = win.performance.now(); let last = t0;
    const stop = () => { win.removeEventListener("resize", size); c.remove(); };
    const frame = (now) => {
      const t = now - t0, dt = Math.min(2.5, (now - last) / 16.67); last = now;
      if (t > LIFE || !c.isConnected) return stop();
      g.clearRect(0, 0, W, H); g.globalAlpha = t > LIFE - FADE ? Math.max(0, (LIFE - t) / FADE) : 1; let alive = 0;
      for (const q of parts) {
        if (q.delay > 0) { q.delay -= dt; alive += 1; continue; }
        q.vx *= Math.pow(0.985, dt); q.vy = q.vy * Math.pow(0.985, dt) + 0.32 * dt; q.x += q.vx * dt; q.y += q.vy * dt; q.rot += q.spin * dt; q.wob += 0.12 * dt;
        if (q.y > H + 20) continue; alive += 1;
        g.save(); g.translate(q.x + Math.sin(q.wob) * 2, q.y); g.rotate(q.rot); g.scale(1, Math.cos(q.wob)); g.fillStyle = q.color;
        if (q.round) { g.beginPath(); g.arc(0, 0, q.h * 0.8, 0, 6.283); g.fill(); } else g.fillRect(-q.w / 2, -q.h / 2, q.w, q.h);
        g.restore();
      }
      if (!alive && t > 600) return stop();
      win.requestAnimationFrame(frame);
    };
    win.requestAnimationFrame(frame);
    return true;
  }
  /* ---------- app (EvoUI v2 tier 4): srcdoc, host tokens, bridge ---------- */
  /* Plain JSON only (what JSON.stringify keeps unchanged): null, true/false, finite numbers, text, arrays,
     plain objects — within a budget (4000 values, 40000 characters of text, 32 levels), so a huge message
     is refused before it is serialized. */
  function jsonOnly(x) {
    let nodes = 0, chars = 0;
    const ok = (v, depth) => {
      if ((nodes += 1) > 4000 || depth > 32) return false;
      if (v === null || typeof v === "boolean") return true;
      if (typeof v === "number") return isFinite(v);
      if (typeof v === "string") return (chars += v.length) <= 40000;
      if (Array.isArray(v)) { for (let i = 0; i < v.length; i++) if (!ok(v[i], depth + 1)) return false; return true; }
      if (typeof v !== "object") return false;
      const pr = Object.getPrototypeOf(v); if (pr !== Object.prototype && pr !== null) return false;
      for (const k of Object.keys(v)) { if ((chars += k.length) > 40000 || !ok(v[k], depth + 1)) return false; }
      return true;
    };
    return ok(x, 0);
  }
  /* a restored app progress fits: {round (≥ 1), saved (plain JSON ≤ APP.save characters, or null)} */
  function appProgressFits(r) {
    return isObj(r) && Object.keys(r).length === 2 && isInt(r.round) && r.round >= 1 && has(r, "saved") && jsonOnly(r.saved) && JSON.stringify(r.saved).length <= APP.save;
  }
  /* The host tokens an app sees as :root CSS variables (name, the evoui.css variable it is read from,
     light and dark fallbacks). Read through a probe element's computed colour, so every value is a plain
     colour (no var()); anything that does not look like one falls back. */
  const APP_TOKENS = [["--viz-1", "--eu-blue", "#2f6fdf", "#6ea2ff"], ["--viz-2", "--eu-miss", "#e07b2a", "#f2a35c"],
    ["--known", "--eu-right", "#2f9e5b", "#4cc27a"], ["--shaky", "--eu-shaky", "#d99a1e", "#e0a43a"], ["--gap", "--eu-wrong", "#d9483b", "#ec6a5e"],
    ["--text", "--eu-fg", "#222222", "#e6e6ea"], ["--text-2", "--eu-fg2", "#5c5c5c", "#b0b0b8"], ["--bg", "--eu-bg", "#ffffff", "#1e1e21"],
    ["--surface", "--eu-surface", "#f5f5f7", "#26262a"], ["--line", "--eu-line", "#e3e3e3", "#3d3d44"], ["--accent", "--eu-accent", "#6b4fd6", "#a48ff2"]];
  const SAFE_COLOR = /^[a-z0-9#(),.%\s/-]{1,80}$/i;
  function appTheme(el) {
    const doc = el.ownerDocument, win = doc && doc.defaultView, got = {};
    let dark = false;
    try { dark = !!(win && win.matchMedia && win.matchMedia("(prefers-color-scheme: dark)").matches); } catch (x) { dark = false; }
    if (el.isConnected && win && typeof win.getComputedStyle === "function") {
      const probe = doc.createElement("span"); probe.style.display = "none"; el.append(probe);
      try { for (const [k, src] of APP_TOKENS) { probe.style.color = ""; probe.style.color = "var(" + src + ")"; const v = String(win.getComputedStyle(probe).color || "").trim(); if (v && SAFE_COLOR.test(v)) got[k] = v; } }
      finally { probe.remove(); }
      const lum = (c) => { const m = /^rgba?\((\d+)[,\s]+(\d+)[,\s]+(\d+)/.exec(c || ""); return m ? (0.2126 * m[1] + 0.7152 * m[2] + 0.0722 * m[3]) / 255 : null; };
      const lt = lum(got["--text"]), lb = lum(got["--bg"]);
      if (lt !== null && lb !== null && lt !== lb) dark = lt > lb; else if (lt !== null) dark = lt > 0.5;
    }
    const tokens = {}; for (const [k, , light, darkV] of APP_TOKENS) tokens[k] = got[k] || (dark ? darkV : light);
    /* the host's font, so a program sits in its note; only what cannot leave the declaration */
    const family = el.isConnected && win && typeof win.getComputedStyle === "function" ? String(win.getComputedStyle(el).fontFamily || "") : "";
    return { name: dark ? "dark" : "light", tokens, font: /^[^<>{};\\]{1,300}$/.test(family) ? family : "" };
  }
  /* The token stylesheet inside the frame (also serialized into the bridge, which re-applies it when the
     theme changes — so it uses nothing from this scope). ChatGPT-style aliases (--viz-text, --viz-muted,
     --viz-panel, --viz-card, --viz-border) point at the same colours. html / body defaults have zero
     specificity (:where), so the app's own CSS always wins. */
  function appTokenCss(t) {
    var out = ":root{";
    for (var k in t.tokens) if (Object.prototype.hasOwnProperty.call(t.tokens, k)) out += k + ":" + t.tokens[k] + ";";
    return out + "--viz-text:var(--text);--viz-muted:var(--text-2);--viz-panel:var(--surface);--viz-card:var(--bg);--viz-border:var(--line);"
      + "color-scheme:" + (t.name === "dark" ? "dark" : "light") + "}"
      // no scrollbar: it narrows the content, which makes it taller, and the frame following it never settles
      + ":where(html){background:transparent;color:var(--text);scrollbar-width:none}"
      + ":where(body){margin:0;font:14px/1.55 " + (t.font || "system-ui,-apple-system,\"Segoe UI\",\"PingFang SC\",\"Microsoft YaHei\",sans-serif") + "}"
      // what a program gets without writing it: the host's quiet look. Zero specificity, its own CSS always wins.
      + ":root{--viz-1-soft:color-mix(in srgb,var(--viz-1) 45%,var(--bg));--viz-2-soft:color-mix(in srgb,var(--viz-2) 45%,var(--bg))}"
      + ":where(*){box-sizing:border-box}"
      + ":where(h1,h2,h3,h4){margin:0 0 6px;font-size:15px;line-height:1.4;font-weight:600}:where(p){margin:0 0 6px}"
      + ":where(button){font:inherit;font-size:13px;min-height:34px;padding:4px 12px;color:var(--text);background:var(--bg);border:1px solid var(--line);border-radius:8px;cursor:pointer}"
      + ":where(button:hover){background:var(--surface)}:where(button:disabled){opacity:.45;cursor:default}"
      + ":where(button[aria-pressed=true]){border-color:var(--accent);background:color-mix(in srgb,var(--accent) 12%,var(--bg));font-weight:600}"
      + ":where(input,select){font:inherit;font-size:13px;color:var(--text);background:var(--bg);border:1px solid var(--line);border-radius:6px}"
      + ":where(input[type=range]){accent-color:var(--accent);border:0;background:transparent}"
      + ":where(table){border-collapse:collapse;font-size:13px}:where(td,th){border:1px solid var(--line);padding:3px 8px;text-align:center}:where(th){font-weight:600;background:var(--surface)}"
      + ":where(.panel){border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin:0 0 8px}"
      + ":where(.seg){display:flex;flex-wrap:wrap;gap:6px;margin:0 0 6px}:where(.seg>button){flex:1 1 90px}"
      + ":where(.stat){display:flex;flex-direction:column;gap:1px;padding:7px 10px;background:var(--surface);border-radius:8px}"
      + ":where(.stat small){font-size:12px;color:var(--text-2)}:where(.stat b){font-size:20px;font-weight:600;line-height:1.25;font-variant-numeric:tabular-nums}"
      + ":where(.calc){font:12.5px/1.6 ui-monospace,Consolas,monospace,\"Microsoft YaHei\";padding:6px 10px;background:var(--surface);border-radius:6px;white-space:pre-wrap;margin:0 0 6px}"
      + ":where(.note){border-left:3px solid var(--accent);padding:2px 0 2px 10px;margin:0 0 6px}"
      + ":where(.cap){font-size:12px;line-height:1.5;color:var(--text-2)}";
  }
  /* The bridge: runs first inside the frame (serialized with Function.prototype.toString, so it uses
     nothing from this scope). window.evowiki = {progress, theme, tokens, onInit(fn), ready(), item(id,
     response, correct, hintUsed, latencyMs), summary({total, right, missed}), save(progress), resize(h)}.
     It keeps its own reference to the parent window, sends on the render's channel and takes only
     init / theme messages from the parent on that channel. */
  function appBridge(CH, THEME, css) {
    "use strict";
    var P = window.parent, inits = [], got = false, fixed = false, shown = -1, fits = 0, waiting = {}, nreq = 0, urls = {};
    function send(m) { m.channel = CH; try { P.postMessage(m, "*"); return true; } catch (x) { console.warn("evowiki: the message could not be sent", x); return false; } }
    function height() {
      var b = document.body, d = document.documentElement; if (!b) return d.scrollHeight;
      // the root's box holds a last child's margin, which collapses through the body
      // what sticks out of the frame counts whatever box it hangs from
      return Math.ceil(Math.max(d.getBoundingClientRect().height, d.scrollHeight > window.innerHeight ? d.scrollHeight : 0, b.getBoundingClientRect().bottom + window.scrollY + (parseFloat(getComputedStyle(b).marginBottom) || 0)));
    }
    /* the frame follows its content until the app sets a height itself (like iframe-resizer) */
    function fit() {
      if (fixed || fits >= 60) return;
      var h = height();
      if (Math.abs(h - shown) < 2) return;
      shown = h; fits += 1; send({ type: "resize", height: h });
    }
    var api = {
      progress: null, theme: THEME.name, tokens: THEME.tokens,
      onInit: function (fn) { if (typeof fn !== "function") return; if (got) { try { fn(api.progress); } catch (x) { console.error(x); } } else inits.push(fn); },
      ready: function () {
        var ok = send({ type: "ready" });
        fit();
        if (window.ResizeObserver && document.body) new ResizeObserver(fit).observe(document.body);
        ["click", "pointerup", "input"].forEach(function (t) { window.addEventListener(t, function () { setTimeout(fit, 60); setTimeout(fit, 500); }, { capture: true, passive: true }); });
        return ok;
      },
      image: function (id) {
        if (!urls[id]) urls[id] = new Promise(function (resolve, reject) {
          nreq += 1; var req = nreq; waiting[req] = { resolve: resolve, reject: reject };
          /* the host answers once it has spoken to this frame (its first load is over) */
          var ask = function () { send({ type: "image", req: req, id: id }); };
          if (got) ask(); else inits.push(ask);
        });
        return urls[id];
      },
      item: function (id, response, correct, hintUsed, latencyMs) {
        var m = { type: "item", id: id, response: response === undefined ? null : response, correct: correct === undefined ? null : correct, hintUsed: !!hintUsed };
        if (latencyMs !== undefined && latencyMs !== null) m.latencyMs = latencyMs;
        return send(m);
      },
      summary: function (s) { s = s || {}; return send({ type: "summary", total: s.total, right: s.right, missed: s.missed || [] }); },
      /* before init the app has not been given what was saved: its first draw must not overwrite that */
      save: function (progress) { return got ? send({ type: "save", progress: progress === undefined ? null : progress }) : false; },
      resize: function (h) { if (typeof h === "number") fixed = true; return send({ type: "resize", height: typeof h === "number" ? h : height() }); }
    };
    function theme(t) {
      api.theme = t.name; api.tokens = t.tokens;
      document.documentElement.setAttribute("data-theme", t.name);
      var s = document.getElementById("evowiki-tokens"); if (s) s.textContent = css(t);
      try { window.dispatchEvent(new CustomEvent("evowiki:theme", { detail: t.name })); } catch (x) { /* old engine */ }
    }
    window.addEventListener("message", function (e) {
      var d = e.data;
      if (e.source !== P || !d || typeof d !== "object" || d.channel !== CH) return;
      if (d.type === "init" && !got) {
        got = true; api.progress = d.progress === undefined ? null : d.progress;
        if (d.theme) theme(d.theme);
        var fs = inits; inits = [];
        for (var i = 0; i < fs.length; i++) { try { fs[i](api.progress); } catch (x) { console.error(x); } }
        try { window.dispatchEvent(new CustomEvent("evowiki:init", { detail: api.progress })); } catch (x) { /* old engine */ }
      } else if (d.type === "theme" && d.theme) theme(d.theme);
      else if (d.type === "image" && waiting[d.req]) {
        var w = waiting[d.req]; delete waiting[d.req];
        if (d.bytes) w.resolve(URL.createObjectURL(new Blob([d.bytes], { type: d.mime || "image/webp" })));
        else w.reject(new Error("no such image"));
      }
    });
    Object.defineProperty(window, "evowiki", { value: api, enumerable: true });
  }
  /* The frame's document: the CSP meta, the token style and the bridge come first in <head>, before
     anything of the app's html is parsed (the app's own doctype / <html> / <head> tags then merge into
     this document or are ignored by the HTML parser), so no app markup can precede or disable them. */
  const SCRIPT_OPEN = "<scr" + "ipt>", SCRIPT_CLOSE = "</scr" + "ipt>";      // never a literal end tag here (harnesses inline this file)
  function appSrcdoc(html, nonce, theme, language = "zh") {
    const locale = html.includes("data-evowiki-locale=") ? ' lang="' + (language === "en" ? "en" : "zh") + '"' : "";
    return "<!doctype html><html" + locale + " data-theme=\"" + (theme.name === "dark" ? "dark" : "light") + "\"><head>"
      + "<meta http-equiv=\"Content-Security-Policy\" content=\"" + APP.csp + "\">"
      + "<style id=\"evowiki-tokens\">" + appTokenCss(theme) + "</style>"
      + SCRIPT_OPEN + "(" + appBridge.toString() + ")(" + JSON.stringify(nonce) + "," + JSON.stringify(theme) + "," + appTokenCss.toString() + ");" + SCRIPT_CLOSE
      + html;
  }
  function makeCtx(state, KEYS, opts) {
    const keys = new Set(KEYS.values()), blockId = opts.blockId ?? null;
    const live = {}, pending = {}, log = [], done = new Set(), listeners = new Map();
    let revision = 0, timer = null, dirty = false, destroyed = false;
    const cleanups = new Set();                // ctx.onDestroy: what handle.destroy() stops
    const now = () => (global.performance && typeof global.performance.now === "function" ? global.performance.now() : Date.now());
    const warn = (m) => console.warn("EvoUI progress: " + m);
    /* this render's session: 16 random hex chars on every event (the server's dedup key includes it,
       so a render that did not restore progress — attempt / revision start over — is never taken for
       a resend of an earlier render's events). */
    const session = (() => {
      const b = new Uint8Array(8), cr = global.crypto;
      if (cr && typeof cr.getRandomValues === "function") cr.getRandomValues(b); else for (let i = 0; i < b.length; i++) b[i] = Math.floor(Math.random() * 256);
      return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
    })();
    const r = opts.progress;
    if (r != null) {
      if (!isObj(r) || r.v !== 2) warn("not a v2 progress object; ignored");
      else {
        if (isInt(r.revision) && r.revision >= 0) revision = r.revision; else warn("bad revision; counting from 0");
        if (has(r, "state") && !isObj(r.state)) warn("state is not an object; ignored");
        if (isObj(r.state)) for (const [k, v] of Object.entries(r.state)) {
          if (!has(state, k)) continue;                                        // unknown keys are ignored
          if (jsonType(v) !== jsonType(state[k]) || numberShape(v) !== numberShape(state[k])) { warn(`state ${k} has the wrong type; kept the block's value`); continue; }
          state[k] = clone(v);
        }
        if (has(r, "nodes") && !isObj(r.nodes)) warn("nodes is not an object; ignored");
        if (isObj(r.nodes)) for (const [k, v] of Object.entries(r.nodes)) if (keys.has(k)) pending[k] = clone(v);
      }
    }
    const progress = () => {
      const nodes = {}; for (const k of keys) { if (has(live, k)) nodes[k] = live[k]; else if (has(pending, k)) nodes[k] = pending[k]; }
      return clone({ v: 2, revision, state, nodes });
    };
    /* running timers (timer nodes register here): a summary emitted while one runs carries its elapsed
       ms as `timerMs` (the first running one in build order). */
    const clocks = new Set();
    const timerMs = () => {
      for (const c of [...clocks]) { if (c.dead && c.dead()) { clocks.delete(c); continue; } if (c.alive() && c.running()) return Math.max(0, Math.round(c.elapsed())); }
      return null;
    };
    const send = () => { timer = null; if (!dirty) return; dirty = false; if (typeof opts.onProgress === "function") { try { opts.onProgress(progress()); } catch (x) { console.error(x); } } };
    return {
      image: (im) => (typeof opts.image === "function" ? opts.image(im) : null),
      nodeKey: (n) => KEYS.get(n),
      session,
      nodeProgress(key, init, check) {
        if (has(live, key)) return live[key];
        if (has(pending, key)) {
          const cand = pending[key]; delete pending[key]; let ok = false;
          try { ok = !!check(cand); } catch (x) { ok = false; }
          if (ok) return (live[key] = cand);
          warn(`progress for ${key} does not fit the node; discarded`);
        }
        return (live[key] = init());
      },
      change(o) {
        if (destroyed) return;
        revision += 1; dirty = true;
        if (opts.immediateProgress) { if (timer !== null) clearTimeout(timer); timer = null; send(); return; }
        if (o && o.lazy) { if (timer === null) timer = setTimeout(send, 1000); return; }   // ticks: at most one onProgress a second
        if (timer !== null) clearTimeout(timer); timer = setTimeout(send, 250);
      },
      clocks,
      pace: null,
      emit(ev) {
        if (destroyed) return null;              // a destroyed render reports nothing more
        const t = ev.type === "summary" ? timerMs() : null;
        const out = clone(Object.assign({ type: ev.type, block: blockId, session }, ev, t === null ? {} : { timerMs: t }, { revision }));
        // the server's bound (BlockEventIn.latency_ms ≤ 1 h, contracts.MAX_APP_LATENCY_MS) for every kind: an item
        // left open longer reports exactly the bound instead of failing the whole post (timeMs / timerMs are unbounded)
        if (out.type === "item" && isNum(out.latencyMs)) out.latencyMs = Math.max(0, Math.min(APP.latency, Math.round(out.latencyMs)));
        log.push(out);
        if (typeof opts.onEvent === "function") { try { opts.onEvent(clone(out)); } catch (x) { console.error(x); } }
        return out;
      },
      now,
      /* life(el): lifeOf(el) that also says gone once the render is destroyed */
      life(el) { const g = lifeOf(el, now); return () => destroyed || g(); },
      /* onDestroy(fn): fn runs once at handle.destroy() (at once if already destroyed); returns an unsubscribe */
      onDestroy(fn) {
        if (destroyed) { try { fn(); } catch (x) { console.error(x); } return () => {}; }
        cleanups.add(fn); return () => { cleanups.delete(fn); };
      },
      destroyed: () => destroyed,
      /* destroy(): stop every timer / player / observer / app bridge now, then send the last progress
         (flush) and go quiet: no more onProgress, events or state writes from this render. Idempotent. */
      destroy() {
        if (destroyed) return progress();
        for (const fn of [...cleanups]) { try { fn(); } catch (x) { console.error(x); } }
        cleanups.clear();
        if (timer !== null) { clearTimeout(timer); timer = null; }
        send(); destroyed = true;
        return progress();
      },
      isDone: (key) => done.has(key),
      markDone(key, info = {}) {
        if (done.has(key)) return; done.add(key);
        for (const fn of [...(listeners.get(key) || []), ...(listeners.get("*") || [])]) { try { fn(key, { restored: !!info.restored }); } catch (x) { console.error(x); } }
      },
      peek: (key) => (has(live, key) ? live[key] : has(pending, key) ? pending[key] : null),
      onDone(key, fn) { if (!listeners.has(key)) listeners.set(key, new Set()); listeners.get(key).add(fn); return () => { listeners.get(key).delete(fn); }; },
      progress,
      events: () => clone(log),
      ack(n) { const k = Math.max(0, Math.min(log.length, Math.floor(Number(n) || 0))); log.splice(0, k); },
      flush() { if (timer !== null) { clearTimeout(timer); timer = null; } if (!destroyed) send(); return progress(); },
    };
  }
  global.EvoUI = { render, validate, nodeKeys, APP, appSrcdoc, evalExpr: (src, env) => { resetSteps(); return evalExpr(src, env); }, EvoUIError };
})(window);
