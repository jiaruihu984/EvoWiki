/**
 * The EvoUI safety guard, one copy for both sides: the Obsidian plugin imports this file (evoui/block.ts) and refuses to
 * draw a block it rejects; the server's render check (deep/render.py) runs it in the check page, so the writer gets the same
 * refusal at write time. A browser script like evoui.js, and independent of it: it never calls the renderer.
 * It registers `window.EvoUIGuard`. Hardened against escape payloads (engine-safety.ts, block.test.ts): do not loosen. The one
 * allowance: a name whose value is proven to hold only numbers can be indexed and passed to map (see "proven numbers" below).
 */
(function () {
"use strict";

/** Names that reach the prototype chain or a global: never allowed as an identifier, a string value, a state name or a JSON key. */
const FORBIDDEN = /__\w+__|\bconstructor\b|\bprototype\b|\bgetPrototypeOf\b|\bReflect\b|\bProxy\b|\bglobalThis\b|\bwindow\b|\bdocument\b|\beval\b|\bFunction\b|\bself\b/;
/** State values are data, so only the names that lead to Function / the prototype chain are refused in them. */
const STATE_FORBIDDEN = /__\w+__|\bconstructor\b|\bprototype\b|\bgetPrototypeOf\b|\bFunction\b|\beval\b|\bReflect\b|\bProxy\b|\bglobalThis\b/;
const MAX_JSON_DEPTH = 64;
const MAX_EXPR_NESTING = 24;
/** Fields whose whole string is an expression. */
const EXPR_KEYS = new Set(["cond", "data", "points", "x", "y", "x1", "y1", "x2", "y2", "w", "h", "cx", "cy", "r"]);
/** Fields that are templates (`{expression}` inside text); every other string is plain text and is not scanned. */
const TEMPLATE_KEYS = new Set(["text", "label", "caption", "x_label", "y_label", "prompt", "background", "color", "flex", "radius", "padding", "width", "height"]);
const BUILTINS = new Set(["sin", "cos", "tan", "atan2", "sqrt", "abs", "min", "max", "round", "floor", "ceil", "exp", "log", "pow", "PI",
  "fmt", "deg2rad", "range", "map", "sum", "len", "iterate", "asin", "acos", "atan", "hypot", "sign", "log2", "log10", "clamp", "lerp", "remap", "rand",
  "filter", "fold", "slice", "concat", "reverse", "sort", "put", "swap", "repeat", "indexOf", "has", "join", "str", "arc"]);
/** The list functions that take an arrow: where it is passed. */
const LOOP_FNS = new Set(["map", "iterate", "filter", "fold"]);
/** The largest work one expression may be estimated at (iterations), and all of a block's expressions together. */
const MAX_EXPR_COST = 200_000;
const MAX_BLOCK_COST = 2_000_000;
const LOOP_CAP = 2000;                  // evoui.js clamps range / iterate / map to 2000 items

/** The tokens of evoui.js `lex` (same rules, same string-escape handling), or null where evoui.js would throw. */
function lexExpr(s) {
  const out = [];
  const ops = ["===", "!==", "**", "==", "!=", "<=", ">=", "&&", "||", "=>"];
  let i = 0;
  outer: while (i < s.length) {
    const c = s[i];
    if (/\s/.test(c)) { i++; continue; }
    if (/[0-9]/.test(c) || (c === "." && /[0-9]/.test(s[i + 1] ?? ""))) {
      let j = i;
      while (j < s.length && /[0-9.]/.test(s[j])) j++;
      if (/[eE]/.test(s[j] ?? "")) { j++; if (/[+-]/.test(s[j] ?? "")) j++; while (/[0-9]/.test(s[j] ?? "")) j++; }
      out.push({ t: "num", v: parseFloat(s.slice(i, j)) });
      i = j;
      continue;
    }
    if (c === '"' || c === "'") {
      let j = i + 1, v = "";
      while (j < s.length && s[j] !== c) { if (s[j] === "\\") j++; v += s[j] ?? ""; j++; }
      if (j >= s.length) return null;
      out.push({ t: "str", v });
      i = j + 1;
      continue;
    }
    if (/[A-Za-z_一-鿿]/.test(c)) {
      let j = i;
      while (j < s.length && /[\w一-鿿]/.test(s[j])) j++;
      out.push({ t: "id", v: s.slice(i, j) });
      i = j;
      continue;
    }
    for (const op of ops) if (s.startsWith(op, i)) { out.push({ t: "op", v: op }); i += op.length; continue outer; }
    if ("+-*/%<>!?:.,()[]{}".includes(c)) { out.push({ t: "op", v: c }); i++; continue; }
    return null;
  }
  return out;
}

const INDEX_OPS = new Set(["+", "-", "*", "/", "%", "(", ")", ".", ","]);
const isOp = (t, v) => t?.t === "op" && t.v === v;

/** Estimated iterations of one expression (loops multiply; an unknown array counts as LOOP_CAP). */
function exprCost(tk) {
  let cost = 1;
  for (const [i, t] of tk.entries()) {
    if (t.t !== "id" || !isOp(tk[i + 1], "(")) continue;
    if (t.v === "range" || t.v === "iterate") {
      const n = tk[i + 2];
      cost *= n?.t === "num" ? Math.max(1, Math.min(LOOP_CAP, Math.floor(n.v))) : LOOP_CAP;
    } else if (t.v === "map" || t.v === "filter" || t.v === "fold") {
      const first = tk[i + 2];
      if (!(first?.t === "id" && first.v === "range")) cost *= LOOP_CAP;
    }
    if (cost > 1e12) return 1e12;
  }
  return cost;
}

/**
 * Token-level guards on top of the spec's own checks, a second layer that does not rely on D1's evaluator. The evaluator holds
 * on its own: a member key is text or a number, the forbidden names are refused on the key as it is read, only own properties
 * are read, and only its builtins and lambdas can be called (engine-safety.ts draws nothing if a renderer loses that; it once
 * coerced an array key after the check, so `sin[["constructor"]]` reached Function). Together with taintProblem, a key built
 * from text never gets as far as the evaluator. Works on the tokens evoui.js itself would produce, never on the source text,
 * so escapes cannot hide a name.
 * Rejected: any identifier or string whose value is a forbidden name; a computed member whose index holds anything but numbers, names, arithmetic and
 * builtin calls (no text, no brackets, no arrow), or a call of its result; a member access on a builtin; bracket nesting over MAX_EXPR_NESTING; more than 2 range / iterate / map /
 * arrow per expression; a string inside `iterate`; a loop-cost estimate over MAX_EXPR_COST. Returns the problem or null.
 */
function expressionProblem(src, costOut) {
  const tk = lexExpr(src);
  if (tk === null) return "cannot be read";
  let depth = 0, max = 0, loops = 0, maps = 0, arrows = 0, iterate = false, str = false;
  for (const [i, t] of tk.entries()) {
    const prev = tk[i - 1];
    if ((t.t === "id" || t.t === "str") && FORBIDDEN.test(t.v)) return "forbidden name";
    if (t.t === "str") str = true;
    if (t.t === "id") {
      if (t.v === "range" || t.v === "iterate") loops++;
      if (t.v === "iterate") iterate = true;
      if (t.v === "fold") iterate = true;
      if (t.v === "map" || t.v === "filter" || t.v === "fold") maps++;
    }
    if (t.t !== "op") continue;
    if (t.v === "=>") arrows++;
    if (t.v === "(" || t.v === "[" || t.v === "{") max = Math.max(max, ++depth);
    else if (t.v === ")" || t.v === "]" || t.v === "}") depth--;
    if (t.v === ".") {
      if (prev?.t === "id" && BUILTINS.has(prev.v)) return "member of a builtin";
      if (tk[i + 1]?.t !== "id") return "bad member";
    }
    if (t.v === "[") {
      const postfix = prev !== undefined && (prev.t === "id" || prev.t === "num" || prev.t === "str" || isOp(prev, ")") || isOp(prev, "]"));
      if (!postfix) continue;                                   // an array literal
      if (prev.t === "id" && BUILTINS.has(prev.v)) return "member of a builtin";
      // the index: numbers, names and arithmetic (steps[step], d[k].t, s.a[s.j + 1], bt[max(step - 1, 0)]: the writer's own
      // templates); the evaluator takes a text or number key only, own properties only, and refuses the forbidden names
      let j = i + 1, open = 0;
      for (; j < tk.length; j++) {
        const x = tk[j];
        // a member of something inside the index (xs[e[0]]) is one more index; an array literal never is
        const inner = isOp(x, "[") && (tk[j - 1]?.t === "id" || isOp(tk[j - 1], "]") || isOp(tk[j - 1], ")"));
        if (x.t === "str" || (x.t === "op" && !INDEX_OPS.has(x.v) && !inner && !(x.v === "]"))) return "computed member";
        if (x.t === "id" && BUILTINS.has(x.v) && !isOp(tk[j + 1], "(")) return "computed member";
        if (isOp(x, "(") || inner) open++;
        if (isOp(x, ")")) open--;
        if (isOp(x, "]")) { if (open === 0) break; open--; }
      }
      if (j === i + 1 || !isOp(tk[j], "]") || isOp(tk[j + 1], "(")) return "computed member";
    }
  }
  if (max > MAX_EXPR_NESTING) return "nested too deep";
  if (loops > 2 || maps > 2 || arrows > 2) return "too many loops";
  if (iterate && str) return "text inside iterate";
  const cost = exprCost(tk);
  if (cost > MAX_EXPR_COST) return "too much work";
  if (costOut) { costOut.cost += cost; costOut.toks?.push(tk); }
  return null;
}

/** A state -> expression map (a button's set, a mark's click or drag). */
function scanSet(m, total, where) {
  if (m === null || typeof m !== "object") return null;
  for (const [sk, sv] of Object.entries(m)) {
    if (FORBIDDEN.test(sk)) return `key ${sk}`;
    if (typeof sv === "string") { const p = expressionProblem(sv, total); if (p) return `${where}${sk}: ${p}`; }
  }
  return null;
}

/** Walks the block once: key names, depth, and every string that is an expression. Returns the first problem or null. */
function scan(v, key, depth, total) {
  if (depth > MAX_JSON_DEPTH) return "nested too deep";
  if (typeof v === "string") {
    if (key !== null && EXPR_KEYS.has(key)) { const p = expressionProblem(v, total); if (p) return `${key}: ${p}`; }
    if (key !== null && TEMPLATE_KEYS.has(key)) {
      for (const m of v.matchAll(/\{([^{}]+)\}/g)) {
        if (lexExpr(m[1]) === null) continue;                   // not an expression at all: D1 rejects or never evaluates it
        const p = expressionProblem(m[1], total);
        if (p) return `{${m[1].slice(0, 30)}}: ${p}`;
      }
    }
    return null;
  }
  if (Array.isArray(v)) {
    for (const x of v) { const p = scan(x, key, depth + 1, total); if (p) return p; }
    return null;
  }
  if (v !== null && typeof v === "object") {
    if (typeof v.el === "string") {         // a drawing's element: every attribute is a template
      for (const [k, x] of Object.entries(v)) {
        if (FORBIDDEN.test(k)) return `key ${k}`;
        if (k === "children") { const p = scan(x, null, depth + 1, total); if (p) return p; continue; }
        if (k === "click" || k === "drag") { const p = scanSet(x, total, `${k}.`); if (p) return p; continue; }
        const p = scan(x, "text", depth + 1, total);
        if (p) return p;
      }
      return null;
    }
    for (const [k, x] of Object.entries(v)) {
      if (FORBIDDEN.test(k)) return `key ${k}`;
      if (k === "id" && typeof x === "string" && FORBIDDEN.test(x)) return `id ${x}`;
      if (depth === 0 && k === "state") continue;     // data, never evaluated: stateProblem reads it
      if (k === "in" && v.kind === "each") { if (typeof x === "string") { const p = expressionProblem(x, total); if (p) return `in: ${p}`; } continue; }
      if (k === "click") { const p = scanSet(x, total, "click."); if (p) return p; continue; }
      // Expressions only on one node kind (elsewhere `when` and `key` are plain text; a probe's `set` holds values).
      if (typeof x === "string" && ((k === "when" && v.kind === "celebrate") || (k === "key" && v.kind === "transition"))) {
        const p = expressionProblem(x, total); if (p) return `${k}: ${p}`;
      }
      if (k === "set" && v.kind === "button" && x !== null && typeof x === "object") {
        for (const [sk, sv] of Object.entries(x)) {
          if (FORBIDDEN.test(sk)) return `key ${sk}`;
          if (typeof sv === "string") { const p = expressionProblem(sv, total); if (p) return `set.${sk}: ${p}`; }
        }
        continue;
      }
      // A table's rows: one expression, or rows of template cells.
      if (k === "rows") {
        if (typeof x === "string") { const p = expressionProblem(x, total); if (p) return `rows: ${p}`; continue; }
        const p = scan(x, "text", depth + 1, total);
        if (p) return p;
        continue;
      }
      if (k === "derived" && x !== null && typeof x === "object" && !Array.isArray(x)) {
        for (const [dk, dv] of Object.entries(x)) {
          if (FORBIDDEN.test(dk)) return `key ${dk}`;
          if (typeof dv === "string") { const p = expressionProblem(dv, total); if (p) return `derived.${dk}: ${p}`; }
        }
        continue;
      }
      const p = scan(x, k, depth + 1, total);
      if (p) return p;
    }
  }
  return null;
}

// ---- taint: names that may hold a function, text that names a key, or something taken out of a computed index ----

const NUMERIC = /^-?\d+(\.\d+)?$/;
const obj = (v) => (v !== null && typeof v === "object" && !Array.isArray(v) ? v : {});

/** Strings (keys and values) anywhere in a state value that name something Function-reaching. */
function stateProblem(v, depth) {
  if (depth > MAX_JSON_DEPTH) return "state nested too deep";
  if (typeof v === "string") return STATE_FORBIDDEN.test(v) ? "forbidden text in a state" : null;
  if (Array.isArray(v)) { for (const x of v) { const p = stateProblem(x, depth + 1); if (p) return p; } return null; }
  if (v !== null && typeof v === "object") {
    for (const [k, x] of Object.entries(v)) {
      if (STATE_FORBIDDEN.test(k)) return "forbidden key in a state";
      const p = stateProblem(x, depth + 1);
      if (p) return p;
    }
  }
  return null;
}

/** A state value that is text (or holds text): it could be used as a key, so the name is opaque. */
const opaqueValue = (v, depth = 0) => {
  if (typeof v === "string") return !NUMERIC.test(v);
  if (depth > 8) return true;
  if (Array.isArray(v)) return v.some((x) => opaqueValue(x, depth + 1));
  if (v !== null && typeof v === "object") return Object.values(v).some((x) => opaqueValue(x, depth + 1));
  return false;
};

/** The function whose `(` most closely encloses token `at` (null: a grouping, an array or an object). */
function enclosing(tk, at) {
  let depth = 0;
  for (let i = at - 1; i >= 0; i--) {
    const t = tk[i];
    if (t.t !== "op") continue;
    if (t.v === ")" || t.v === "]" || t.v === "}") depth++;
    else if (t.v === "(" || t.v === "[" || t.v === "{") {
      if (depth === 0) { const f = tk[i - 1]; return t.v === "(" && f?.t === "id" ? f.v : null; }
      depth--;
    }
  }
  return null;
}

/**
 * Whether the value of an expression may be a function, text that names a key, or something taken out of a computed
 * index: it mentions an opaque name or a text literal, uses a builtin as a value, indexes by anything but a number, or has an
 * arrow that is not the direct argument of map / iterate. (D1's validators now also refuse function values.)
 */
function opaqueExpr(tk, opaque) {
  for (const [i, t] of tk.entries()) {
    const prev = tk[i - 1], next = tk[i + 1];
    if (t.t === "str" && !NUMERIC.test(t.v)) return true;
    if (t.t === "id") {
      if (opaque.has(t.v)) return true;
      if (BUILTINS.has(t.v) && t.v !== "PI" && !isOp(next, "(")) return true;
    }
    if (t.t !== "op") continue;
    if (t.v === "[" && prev !== undefined && (prev.t === "id" || isOp(prev, ")") || isOp(prev, "]")) && !(next?.t === "num" && isOp(tk[i + 2], "]"))) return true;
    if (t.v === "=>") {
      let open = i - 1;
      if (isOp(tk[open], ")")) while (open > 0 && !isOp(tk[open], "(")) open--;   // (a, b) => ...
      const f = enclosing(tk, open);
      const body = tk[i + 1], after = tk[i + 2];
      if (!LOOP_FNS.has(f) || (body?.t === "id" && isOp(after, "=>"))) return true;
    }
  }
  return false;
}

/** Whether token `at` sits inside a computed index (through any parentheses). */
function inIndex(tk, at) {
  let depth = 0;
  for (let i = at - 1; i >= 0; i--) {
    const t = tk[i];
    if (t.t !== "op") continue;
    if (t.v === ")" || t.v === "]" || t.v === "}") depth++;
    else if (t.v === "(" || t.v === "[" || t.v === "{") {
      if (depth > 0) { depth--; continue; }
      if (t.v === "{") return false;
      if (t.v === "[") { const p = tk[i - 1]; return p !== undefined && (p.t === "id" || p.t === "num" || p.t === "str" || isOp(p, ")") || isOp(p, "]")); }
    }
  }
  return false;
}

/** An opaque name may not be called, indexed by anything but a number, appear inside an index, or be passed to map / iterate. */
function useProblem(tk, opaque) {
  for (const [i, t] of tk.entries()) {
    if (t.t !== "id" || !opaque.has(t.v)) continue;
    if (inIndex(tk, i)) return `${t.v}: used as an index`;
    const f = enclosing(tk, i);
    if (LOOP_FNS.has(f) && (isOp(tk[i - 1], ",") || isOp(tk[i - 1], "(")) && (isOp(tk[i + 1], ",") || isOp(tk[i + 1], ")"))) {
      return `${t.v}: passed to ${f}`;
    }
    let j = i + 1;
    for (;;) {
      const n = tk[j];
      if (isOp(n, "(")) return `${t.v}: called`;
      if (isOp(n, "[")) { if (tk[j + 1]?.t === "num" && isOp(tk[j + 2], "]")) { j += 3; continue; } return `${t.v}: computed member`; }
      if (isOp(n, ".") && tk[j + 1]?.t === "id") { j += 2; continue; }
      break;
    }
  }
  return null;
}

// ---- proven numbers: the one relaxation of the taint rule, for a name whose value holds nothing but numbers ----

/*
 * Type of a value, proven without running it: "n" (a number, true / false, null or undefined: no text, no object, no
 * function), "[T]" (an array of T, or undefined when read out of range), "x" (anything else). "+" on two "n" can only give a
 * number, so no text can grow out of "n" ("[1] + []" is text in JS, which is why "+" on an array is "x"); an array typed "[T]"
 * can only be indexed, read for its length, or passed to map / iterate / sum / len.
 * null: the expression cannot be allowed at all (an index that is not "n", a function used as a value, a call of a result).
 */
const MATH_FNS = new Set(["sin", "cos", "tan", "atan2", "sqrt", "abs", "min", "max", "round", "floor", "ceil", "exp", "log", "pow", "deg2rad", "asin", "acos", "atan", "hypot", "sign", "log2", "log10", "clamp", "lerp", "remap", "rand"]);
const COMPARE = new Set(["==", "===", "!=", "!==", "<", "<=", ">", ">="]);   // the evaluator's == is ===, and every one gives true / false
const PROOF_NODES = 20_000;
const proven = (t) => !t.includes("x");
/* A record: "{a:n,b:x}", keys sorted. Reading a field gives its type; a field it does not have reads undefined ("n"). Only keys that are
   plain names get a type: anything else makes the whole record "x". Reading a record's field by a number gives undefined. */
const PLAIN_KEY = /^[A-Za-z_][A-Za-z0-9_]*$/;
const isRecord = (t) => t[0] === "{";
const recordType = (entries) => (entries.every(([k]) => PLAIN_KEY.test(k)) && new Set(entries.map(([k]) => k)).size === entries.length
  ? `{${entries.map(([k, t]) => `${k}:${t}`).sort().join(",")}}` : "x");
/** The parts between the outermost separators of a record or a tuple. */
function partsOf(t) {
  const out = [];
  let depth = 0, start = 1;
  for (let i = 1; i < t.length; i++) {
    const c = t[i];
    if (c === "[" || c === "{" || c === "(") depth++;
    else if ((c === "]" || c === "}" || c === ")") && i < t.length - 1) depth--;
    if ((c === "," && depth === 0) || i === t.length - 1) { out.push(t.slice(start, i)); start = i + 1; }
  }
  return out;
}
function fieldOf(t, name) {
  for (const part of partsOf(t)) { const at = part.indexOf(":"); if (part.slice(0, at) === name) return part.slice(at + 1); }
  return "n";
}
/* A tuple: "(n,[n],x)", an array whose items differ, read by a written number. Written as [list, i, j] in a simulation's state. */
const isTuple = (t) => t[0] === "(";
const tupleItem = (t, i) => partsOf(t)[i] ?? "n";
const arrayType = (ts) => (!ts.length ? "[n]" : ts.every((t) => t === ts[0]) ? `[${ts[0]}]` : `(${ts.join(",")})`);
const isArr = (t) => t[0] === "[";
const join = (a, b) => (a === b ? a : "x");

class ParseFailure extends Error {}

/** The AST of evoui.js `parse`, same grammar and precedence, over lexExpr's tokens. Throws ParseFailure where evoui.js would. */
function parseExpr(tokens) {
  const tk = [...tokens, { t: "eof" }];
  let p = 0;
  const bad = () => { throw new ParseFailure(); };
  const at = (v) => isOp(tk[p], v);
  const eat = (v) => { if (!at(v)) bad(); p++; };
  const PREC = [["||"], ["&&"], ["==", "===", "!=", "!=="], ["<", "<=", ">", ">="], ["+", "-"], ["*", "/", "%"], ["**"]];
  function expr() {
    if (tk[p].t === "id" && isOp(tk[p + 1], "=>")) { const param = tk[p].v; p += 2; return { k: "fn", param, body: expr() }; }
    if (at("(") && tk[p + 1].t === "id" && isOp(tk[p + 2], ",") && tk[p + 3].t === "id" && isOp(tk[p + 4], ")") && isOp(tk[p + 5], "=>")) {
      const param = tk[p + 1].v, param2 = tk[p + 3].v; p += 6; return { k: "fn", param, param2, body: expr() };
    }
    const c = binary(0);
    if (!at("?")) return c;
    p++;
    const a = expr();
    eat(":");
    return { k: "cond", c, a, b: expr() };
  }
  function binary(lv) {
    if (lv >= PREC.length) return unary();
    let l = binary(lv + 1);
    while (tk[p].t === "op" && PREC[lv].includes(tk[p].v)) {
      const op = tk[p++].v;
      l = { k: "bin", op, l, r: binary(lv === PREC.length - 1 ? lv : lv + 1) };
    }
    return l;
  }
  function unary() {
    if (at("-")) { p++; return { k: "neg", e: unary() }; }
    if (at("!")) { p++; return { k: "not", e: unary() }; }
    if (at("+")) { p++; return unary(); }
    return postfix(primary());
  }
  function postfix(e0) {
    for (let e = e0; ;) {
      if (at(".")) { p++; const n = tk[p++]; if (n.t !== "id") bad(); e = { k: "mem", o: e, name: { k: "lit", v: n.v } }; }
      else if (at("[")) { p++; const name = expr(); eat("]"); e = { k: "mem", o: e, name }; }
      else if (at("(")) {
        p++;
        const args = [];
        if (!at(")")) { args.push(expr()); while (at(",")) { p++; args.push(expr()); } }
        eat(")");
        e = { k: "call", f: e, args };
      } else return e;
    }
  }
  function primary() {
    const t = tk[p];
    if (t.t === "num" || t.t === "str") { p++; return { k: "lit", v: t.v }; }
    if (t.t === "id") {
      p++;
      if (t.v === "true") return { k: "lit", v: true };
      if (t.v === "false") return { k: "lit", v: false };
      return t.v === "null" ? { k: "lit", v: null } : { k: "id", v: t.v };
    }
    if (at("(")) { p++; const e = expr(); eat(")"); return e; }
    if (at("[")) {
      p++;
      const items = [];
      if (!at("]")) { items.push(expr()); while (at(",")) { p++; if (at("]")) break; items.push(expr()); } }
      eat("]");
      return { k: "arr", items };
    }
    if (at("{")) {
      p++;
      const props = [];
      if (!at("}")) {
        do {
          if (at("}")) break;
          const key = tk[p++];
          if (key.t !== "str" && key.t !== "id") bad();
          eat(":");
          props.push([key.v, expr()]);
        } while (at(",") && ++p);
      }
      eat("}");
      return { k: "obj", props };
    }
    return bad();
  }
  const ast = expr();
  if (tk[p].t !== "eof") bad();
  return ast;
}

/** See "Type of a value" above. `env`: name -> type; a block's name that is not proven is "x". `left`: the node budget. */
function typeOf(a, env, left) {
  if (--left.n < 0) throw new RangeError("proof too long");
  const ty = (e) => typeOf(e, env, left);
  const all = (es) => { const ts = es.map(ty); return ts.includes(null) ? null : ts; };
  switch (a.k) {
    case "lit": return typeof a.v === "string" ? "x" : "n";
    case "id": return a.v in env ? env[a.v] : a.v === "PI" ? "n" : BUILTINS.has(a.v) ? null : "x";
    case "arr": {
      const ts = all(a.items);
      if (ts === null) return null;
      return arrayType(ts);
    }
    case "obj": {
      const ts = all(a.props.map(([, e]) => e));
      return ts === null ? null : recordType(a.props.map(([k], i) => [k, ts[i]]));
    }
    case "neg": { const t = ty(a.e); return t === null ? null : t === "n" ? "n" : "x"; }
    case "not": return ty(a.e) === null ? null : "n";
    case "cond": { const ts = all([a.c, a.a, a.b]); return ts === null ? null : join(ts[1], ts[2]); }
    case "bin": {
      const ts = all([a.l, a.r]);
      if (ts === null) return null;
      if (COMPARE.has(a.op)) return "n";
      if (a.op === "&&" || a.op === "||") return join(ts[0], ts[1]);
      return ts[0] === "n" && ts[1] === "n" ? "n" : "x";
    }
    case "mem": {
      const o = ty(a.o);
      if (o === null) return null;
      if (a.name.k === "lit" && typeof a.name.v === "string") {
        if (isRecord(o)) return fieldOf(o, a.name.v);
        return a.name.v === "length" && isArr(o) ? "n" : "x";
      }
      if (ty(a.name) !== "n") return null;                      // the whole point: an index is a proven number
      if (isTuple(o)) return a.name.k === "lit" ? tupleItem(o, a.name.v) : "x";
      return isArr(o) ? o.slice(1, -1) : "x";
    }
    case "call": return callType(a, env, left);
    default: return null;                                       // an arrow outside map / iterate
  }
}

function callType(a, env, left) {
  const f = a.f.k === "id" && !(a.f.v in env) ? a.f.v : null;
  const ty = (e, en = env) => typeOf(e, en, left);
  const bind = (fn, t, t2 = "n") => { const e = Object.assign(Object.create(null), env, { [fn.param]: t }); if (fn.param2) e[fn.param2] = t2; return e; };
  if (LOOP_FNS.has(f)) {
    const fn = a.args[a.args.length - 1];
    if (a.args.length !== (f === "map" || f === "filter" ? 2 : 3) || fn.k !== "fn") return null;
    const first = ty(a.args[0]);
    if (first === null) return null;
    const item = isArr(first) ? first.slice(1, -1) : "x";
    if (f === "map") {
      const u = ty(fn.body, bind(fn, item));
      return u === null ? null : `[${u}]`;
    }
    if (f === "filter") return ty(fn.body, bind(fn, item)) === null ? null : first;
    const init = ty(a.args[1]);
    if (init === null) return null;
    if (f === "fold") {                     // (running value, item)
      const u = ty(fn.body, bind(fn, init, item));
      if (u === null) return null;
      if (u === init) return init;
      return ty(fn.body, bind(fn, "x", item)) === null ? null : "x";
    }
    const u = ty(fn.body, bind(fn, init));
    if (u === null) return null;
    if (u === init) return `[${init}]`;
    return ty(fn.body, bind(fn, "x")) === null ? null : "[x]";
  }
  if (f === null || !BUILTINS.has(f)) return null;              // only a builtin can be called
  const ts = a.args.map((e) => ty(e));
  if (ts.includes(null)) return null;
  if (MATH_FNS.has(f)) return ts.every((t) => t === "n") ? "n" : "x";
  if (f === "range") return ts[0] === "n" ? "[n]" : "x";
  if (f === "sum" || f === "len") return isArr(ts[0] ?? "x") ? "n" : "x";
  if (f === "indexOf" || f === "has") return "n";
  if (f === "slice" || f === "reverse" || f === "sort" || f === "swap") return isArr(ts[0] ?? "x") ? ts[0] : "x";
  if (f === "concat") return isArr(ts[0] ?? "x") && ts[0] === ts[1] ? ts[0] : "x";
  if (f === "put") return isArr(ts[0] ?? "x") && ts[2] === ts[0].slice(1, -1) ? ts[0] : "x";
  if (f === "repeat") return `[${ts[0] ?? "x"}]`;
  return "x";                                                   // fmt, join, str, arc: text
}

/** The type of a JSON state value. */
function stateType(v) {
  if (typeof v === "number" || typeof v === "boolean") return "n";
  if (v !== null && typeof v === "object" && !Array.isArray(v)) return recordType(Object.entries(v).map(([k, x]) => [k, stateType(x)]));
  if (!Array.isArray(v)) return "x";
  return arrayType(v.map(stateType));
}

/** The type of an expression's text, or null where it cannot be allowed (one that does not parse included). */
function exprType(src, env) {
  const tk = lexExpr(src);
  if (tk === null) return null;
  try { return typeOf(parseExpr(tk), env, { n: PROOF_NODES }); } catch { return null; }
}

/** Everything in the body that writes a state after the render: controls, a button's `set`, a chart's `hover`. */
function stateWriters(v, depth, out, locals = []) {
  if (depth > MAX_JSON_DEPTH || v === null || typeof v !== "object") return;
  if (Array.isArray(v)) { for (const x of v) stateWriters(x, depth + 1, out, locals); return; }
  const options = Array.isArray(v.options) ? v.options.map((o) => stateType(obj(o).value)).reduce(join, "n") : "n";
  if (typeof v.bind === "string") out.fixed.push([v.bind, options]);
  if (typeof v.hover === "string") out.fixed.push([v.hover, "n"]);
  for (const [k, x] of Object.entries(obj(v.set))) out.set.push([k, x, locals]);
  for (const [k, x] of Object.entries(obj(v.click))) out.set.push([k, x, locals]);
  for (const [k, x] of Object.entries(obj(v.drag))) out.set.push([k, x, [...locals, ["mx", null], ["my", null]]]);
  const inner = v.kind === "each" ? [...locals, [typeof v.as === "string" ? v.as : "d", v.in], [typeof v.index === "string" ? v.index : "i", null]] : locals;
  for (const x of Object.values(v)) stateWriters(x, depth + 1, out, inner);
}

/**
 * name -> type for the states and derived values. A state's type is its initial value's only when every writer of it gives
 * the same type; otherwise it is "x", and every derived value is worked out again from that.
 */
function blockTypes(block, state, derived) {
  const writers = { fixed: [], set: [] };
  stateWriters(obj(block).body, 0, writers);
  for (const p of Array.isArray(obj(block).probes) ? block.probes : []) {
    for (const [k, x] of Object.entries(obj(obj(p).set))) writers.fixed.push([k, stateType(x)]);
  }
  const states = Object.create(null);
  for (const [k, v] of Object.entries(state)) states[k] = stateType(v);
  for (const [k, t] of writers.fixed) if (k in states && states[k] !== t) states[k] = "x";
  for (;;) {
    const env = Object.assign(Object.create(null), states);
    for (const [k, src] of Object.entries(derived)) env[k] = (typeof src === "string" ? exprType(src, env) : null) ?? "x";
    let changed = false;
    for (const [k, x, locals] of writers.set) {
      if (!(k in states) || states[k] === "x") continue;
      const t = typeof x === "number" || typeof x === "boolean" ? "n" : typeof x === "string" ? exprType(x, withLocals(env, locals)) : null;
      if (t !== states[k]) { states[k] = "x"; changed = true; }
    }
    if (!changed) return env;
  }
}

/** env plus an `each`'s names: its item (what its list holds) and its index, or the pointer (no source: a number). */
function withLocals(env, locals) {
  let e = env;
  for (const [name, src] of locals) {
    e = Object.assign(Object.create(null), e);
    if (src === null || src === undefined) { e[name] = "n"; continue; }
    const t = typeof src === "string" ? exprType(src, e) : "n";
    e[name] = t === null || !isArr(t) ? (t === "n" ? "n" : "x") : t.slice(1, -1);
  }
  return e;
}

/** Every string of a value that the renderer could evaluate: whole, and each `{...}` in it, with the env in force there. */
function eachExpression(v, depth, visit, env) {
  if (depth > MAX_JSON_DEPTH) return;
  if (typeof v === "string") {
    visit(v, env);
    for (const m of v.matchAll(/\{([^{}]+)\}/g)) visit(m[1], env);
  } else if (v !== null && typeof v === "object") {
    let e = env;
    if (!Array.isArray(v) && v.kind === "each") {
      if (typeof v.in === "string") visit(v.in, env);
      e = withLocals(env, [[typeof v.as === "string" ? v.as : "d", v.in], [typeof v.index === "string" ? v.index : "i", null]]);
      for (const x of Array.isArray(v.children) ? v.children : []) eachExpression(x, depth + 1, visit, e);
      return;
    }
    for (const [k, x] of Object.entries(v)) eachExpression(x, depth + 1, visit, k === "drag" ? withLocals(e, [["mx", null], ["my", null]]) : e);
  }
}

/**
 * An expression that mentions a relaxed name, anywhere in the block, must itself pass typeOf, whatever field it is in: with
 * the taint gone from the name, `z[dx + dy]` would otherwise reach the evaluator with a text key. Where one does not, the
 * names it mentions lose the relaxation: they are opaque again, as they always were, and the tokens are returned so the
 * taint rule sees that expression too.
 */
function dropUnproven(block, relaxed, env) {
  const failed = [];
  const visit = (src, en) => {
    const tk = lexExpr(src);
    if (!tk?.some((t) => t.t === "id" && relaxed.has(t.v))) return;
    let t = null;
    try { t = typeOf(parseExpr(tk), en, { n: PROOF_NODES }); } catch (e) {
      if (e instanceof ParseFailure) return;                    // the evaluator cannot read it either: plain text that holds the name
    }
    if (t !== null) return;
    failed.push(tk);
    for (const x of tk) if (x.t === "id") relaxed.delete(x.v);
  };
  eachExpression(obj(block).body, 0, visit, env);
  eachExpression(obj(block).derived, 0, visit, env);
  return failed;
}

/** Each `each` in the body: [its item's name, its list's source]. */
function eachItems(v, depth, out) {
  if (depth > MAX_JSON_DEPTH || v === null || typeof v !== "object") return out;
  if (!Array.isArray(v) && v.kind === "each") out.push([typeof v.as === "string" ? v.as : "d", v.in]);
  for (const x of Object.values(v)) eachItems(x, depth + 1, out);
  return out;
}

/** The expressions (tokens as JSON) that pass the proof wherever they appear. */
function provenExpressions(block, derived, state, env0) {
  const env = Object.assign(Object.create(null), env0);
  for (const [k, src] of Object.entries(derived)) if (typeof src !== "string" || exprType(src, env) === null) env[k] = null;
  const ok = new Map();
  const visit = (src, en) => {
    const tk = lexExpr(src);
    if (!tk) return;
    let t = null;
    try { t = typeOf(parseExpr(tk), en, { n: PROOF_NODES }); } catch { t = null; }
    const key = JSON.stringify(tk);
    ok.set(key, (ok.get(key) ?? true) && t !== null);
  };
  eachExpression(obj(block).body, 0, visit, env);
  eachExpression(derived, 0, visit, env);
  for (const p of Array.isArray(obj(block).probes) ? block.probes : []) eachExpression(p, 0, visit, env);
  return new Set([...ok].filter(([, v]) => v).map(([k]) => k));
}

/** The block-level checks: forbidden text in states, then taint across every derived value and body expression. */
function taintProblem(block, toks) {
  const state = obj(obj(block).state), derived = obj(obj(block).derived);
  const sp = stateProblem(state, 0);
  if (sp) return sp;
  const opaque = new Set(Object.entries(state).filter(([, v]) => opaqueValue(v)).map(([k]) => k));
  const dtk = Object.entries(derived).map(([k, src]) => [k, typeof src === "string" ? lexExpr(src) : null]);
  const items = eachItems(obj(block).body, 0, []);          // an each's item is opaque when its list is
  dtk.push(...items.map(([name, src]) => [name, typeof src === "string" ? lexExpr(src) : null]));
  for (let changed = true; changed;) {
    changed = false;
    for (const [k, tk] of dtk) if (tk !== null && !opaque.has(k) && opaqueExpr(tk, opaque)) { opaque.add(k); changed = true; }
  }
  // the relaxation: a name the rule above made opaque is free again only when its value is proven to hold nothing but numbers
  const env = blockTypes(block, state, derived);
  const relaxed = new Set([...opaque].filter((k) => k in env && proven(env[k])));
  const failed = dropUnproven(block, relaxed, env);
  for (const k of relaxed) opaque.delete(k);
  // An expression proven whole (every index a number, every call a builtin, no function as a value) cannot turn text into a
  // key or a callee, whatever its names hold: the taint rule is for what the proof could not cover. A derived value the proof
  // refuses (a stored arrow, a builtin as a value) poisons every expression that mentions it.
  const safe = provenExpressions(block, derived, state, env);
  for (const tk of [...failed, ...toks]) {
    if (safe.has(JSON.stringify(tk))) continue;
    const p = useProblem(tk, opaque);
    if (p) return p;
  }
  return null;
}

/** The adapter's own checks, before and apart from D1's validator: token rules, cost caps, state text, taint. Null = none. */
function adapterProblem(block) {
  const total = { cost: 0, toks: [] };
  const problem = scan(block, null, 0, total);
  if (problem !== null) return problem;
  if (total.cost > MAX_BLOCK_COST) return "too much work";
  return taintProblem(block, total.toks);
}


window.EvoUIGuard = Object.freeze({ lexExpr, expressionProblem, taintProblem, adapterProblem, MAX_EXPR_COST, MAX_BLOCK_COST });
})();
