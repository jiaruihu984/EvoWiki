"""EvoUI block validator in Python — the same rules as `EvoUI.validate` in `evoui.js`, rule for rule
(`tests/deep/test_evoui_parity.py` runs one corpus of valid and invalid blocks through both and requires
the same verdict). `validate(block)` returns a list of problems; empty = valid.

The expression grammar is the renderer's: a hand-written lexer and recursive-descent parser over
literals, names, `+ - * / % **`, comparisons, `&& || !`, `?:`, member access, calls and one-parameter
arrows. Nothing is ever evaluated here; names must be a state, an earlier derived value, a builtin or
an arrow parameter."""

import json
import re
from collections.abc import Callable, Iterator
from typing import Any

from evowiki.deep.evoui.contracts import (
    ANCHORS,
    ANIMATE_EFFECTS,
    ANNOTATION_FIELDS,
    APP_CDN_ALLOWLIST,
    APP_FORBIDDEN,
    APP_IMAGE_FIELDS,
    APP_ITEM_FIELDS,
    APP_URL_EXTERNAL,
    APP_URL_LOCAL,
    APP_URL_PATTERNS,
    APP_URL_TRIM,
    BLANK,
    BOOL_CONTROLS,
    BOX_TONES,
    BUILTIN_NAMES,
    CARD_FIELDS,
    CELL_RE,
    CHART_CURVES,
    CHART_KINDS,
    CHART_OPTION_KINDS,
    COLOR_ATTRS,
    COLOR_RE,
    CONTROL_KINDS,
    FILL_OPTION_FIELDS,
    ICONS,
    MAX_ANNOTATIONS,
    MAX_APP_HEIGHT,
    MAX_APP_HTML,
    MAX_APP_IMAGES,
    MAX_APP_ITEMS,
    MAX_APP_LABEL,
    MAX_BLANKS,
    MAX_BLOCK_CHARS,
    MAX_CASES,
    MAX_COLUMNS,
    MAX_DEPTH,
    MAX_DERIVED,
    MAX_EXPR_CHARS,
    MAX_FALLBACK_CHARS,
    MAX_INTERVAL_MS,
    MAX_NODES,
    MAX_OPTIONS,
    MAX_PLOT_ITEMS,
    MAX_REVEAL_STEPS,
    MAX_ROUTE_VALUE_CHARS,
    MAX_STATE,
    MAX_TEX_CHARS,
    MAX_TEXT_CHARS,
    MAX_TIMER_SECONDS,
    MAX_WORD_CHARS,
    MAX_WORDS,
    MIN_APP_HEIGHT,
    MIN_CARDS,
    MIN_EVENTS,
    MIN_INTERVAL_MS,
    MIN_ROUTE_OPTIONS,
    MIN_WINDOW,
    NODE_FIELDS,
    NODE_ID_RE,
    NUMBER_CONTROLS,
    OPTION_CONTROLS,
    PATH_RE,
    PRACTICE_KINDS,
    QUESTION_FIELDS,
    QUESTION_TYPES,
    QUIZ_LAYOUTS,
    REVEAL_MODES,
    ROUTE_OPTION_FIELDS,
    SHAPE_FIELDS,
    SMIL_ATTRS,
    STATEFUL_KINDS,
    SVG_ATTRS,
    SVG_BOOL,
    SVG_COLORS,
    SVG_ELS,
    SVG_KIDS,
    TEXT_SIZES,
    TIMELINE_EVENT_FIELDS,
    TIMER_MODES,
    TITLE_SIZES,
    TONES,
    VIEWBOX_RE,
)

MAX_PROBES = 6
TOP = ("type", "title", "question", "state", "derived", "body", "fallback", "probes")
TOP_REQUIRED = ("type", "title", "question", "state", "derived", "body", "fallback")
_NAME = re.compile(r"^[A-Za-z_一-鿿][A-Za-z0-9_一-鿿]*$")
_ID_START = re.compile(r"[A-Za-z_一-鿿]")
_ID_CHAR = re.compile(r"[A-Za-z0-9_一-鿿]")
_OPS = ("===", "!==", "**", "==", "!=", "<=", ">=", "&&", "||", "=>")
_SINGLE = "+-*/%<>!?:.,()[]{}"
_PREC = (("||",), ("&&",), ("==", "!="), ("<", "<=", ">", ">="), ("+", "-"), ("*", "/", "%"), ("**",))
_TEMPLATE = re.compile(r"\{([^{}]+)\}")
_BUILT = frozenset(BUILTIN_NAMES)
_RESERVED = frozenset({"true", "false", "null"})
_CASE_ID = re.compile(r"^[a-z0-9_-]{1,32}$")
_BOX_TONED = ("box", "grid-item")
BLOCKED_MEMBERS = frozenset({"constructor", "__proto__", "prototype"})


class ExprError(ValueError):
    pass


# ---- expressions ----

def lex(s: str) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c.isspace():
            i += 1
            continue
        if c.isdigit() and c.isascii() or (c == "." and i + 1 < n and s[i + 1].isascii()
                                          and s[i + 1].isdigit()):
            j = i
            while j < n and (s[j] == "." or (s[j].isascii() and s[j].isdigit())):
                j += 1
            if j < n and s[j] in "eE":
                j += 1
                if j < n and s[j] in "+-":
                    j += 1
                while j < n and s[j].isascii() and s[j].isdigit():
                    j += 1
            out.append(("num", s[i:j]))
            i = j
            continue
        if c in "\"'":
            j, v = i + 1, []
            while j < n and s[j] != c:
                if s[j] == "\\":
                    j += 1
                if j < n:
                    v.append(s[j])
                j += 1
            if j >= n:
                raise ExprError("unterminated string")
            out.append(("str", "".join(v)))
            i = j + 1
            continue
        if _ID_START.match(c):
            j = i
            while j < n and _ID_CHAR.match(s[j]):
                j += 1
            out.append(("id", s[i:j]))
            i = j
            continue
        for op in _OPS:
            if s.startswith(op, i):
                out.append(("op", "==" if op == "===" else "!=" if op == "!==" else op))
                i += len(op)
                break
        else:
            if c in _SINGLE:
                out.append(("op", c))
                i += 1
            else:
                raise ExprError(f"unexpected character {c!r}")
    out.append(("eof", None))
    return out


def parse(src: str) -> tuple:
    """The AST (nested tuples): ("lit",) ("id", name) ("arr", items) ("obj", [(k, v)]) ("neg", e)
    ("not", e) ("cond", c, a, b) ("mem", o, k) ("call", f, args) ("fn", param, body)
    ("bin", op, l, r). ExprError on anything the renderer would not parse."""
    tk = lex(str(src))
    p = 0

    def is_op(v: str) -> bool:
        return tk[p][0] == "op" and tk[p][1] == v

    def eat(v: str | None = None) -> tuple[str, Any]:
        nonlocal p
        t = tk[p]
        if v is not None and not (t[0] == "op" and t[1] == v):
            raise ExprError(f"expected {v}")
        p += 1
        return t

    def expr() -> tuple:
        nonlocal p
        if tk[p][0] == "id" and tk[p + 1][0] == "op" and tk[p + 1][1] == "=>":
            name = tk[p][1]
            p += 2
            return ("fn", name, expr())
        if (is_op("(") and tk[p + 1][0] == "id" and tk[p + 2][:2] == ("op", ",") and tk[p + 3][0] == "id"
                and tk[p + 4][:2] == ("op", ")") and tk[p + 5][:2] == ("op", "=>")):
            first, second = tk[p + 1][1], tk[p + 3][1]
            p += 6
            return ("fn", first, expr(), second)
        c = binary(0)
        if is_op("?"):
            eat("?")
            a = expr()
            eat(":")
            b = expr()
            return ("cond", c, a, b)
        return c

    def binary(lv: int) -> tuple:
        if lv >= len(_PREC):
            return unary()
        left = binary(lv + 1)
        while tk[p][0] == "op" and tk[p][1] in _PREC[lv]:
            op = eat()[1]
            right = binary(lv) if lv == len(_PREC) - 1 else binary(lv + 1)
            left = ("bin", op, left, right)
        return left

    def unary() -> tuple:
        if is_op("-"):
            eat()
            return ("neg", unary())
        if is_op("!"):
            eat()
            return ("not", unary())
        if is_op("+"):
            eat()
            return unary()
        return postfix(primary())

    def postfix(e: tuple) -> tuple:
        while True:
            if is_op("."):
                eat()
                t = eat()
                if t[0] != "id":
                    raise ExprError("bad member")
                e = ("mem", e, ("lit", t[1]))
            elif is_op("["):
                eat("[")
                i = expr()
                eat("]")
                e = ("mem", e, i)
            elif is_op("("):
                eat("(")
                args = []
                if not is_op(")"):
                    args.append(expr())
                    while is_op(","):
                        eat(",")
                        args.append(expr())
                eat(")")
                e = ("call", e, args)
            else:
                return e

    def primary() -> tuple:
        nonlocal p
        t = tk[p]
        if t[0] in ("num", "str"):
            p += 1
            return ("lit", t[1]) if t[0] == "str" else ("lit",)
        if t[0] == "id":
            p += 1
            return ("lit",) if t[1] in _RESERVED else ("id", t[1])
        if is_op("("):
            eat("(")
            e = expr()
            eat(")")
            return e
        if is_op("["):
            eat("[")
            items = []
            if not is_op("]"):
                items.append(expr())
                while is_op(","):
                    eat(",")
                    if is_op("]"):
                        break
                    items.append(expr())
            eat("]")
            return ("arr", items)
        if is_op("{"):
            eat("{")
            props = []
            if not is_op("}"):
                while True:
                    if is_op("}"):
                        break
                    kt = eat()
                    if kt[0] not in ("str", "id"):
                        raise ExprError("bad key")
                    eat(":")
                    props.append((kt[1], expr()))
                    if is_op(","):
                        eat(",")
                        continue
                    break
            eat("}")
            return ("obj", props)
        raise ExprError(f"unexpected {t[1] if t[1] is not None else t[0]}")

    ast = expr()
    if tk[p][0] != "eof":
        raise ExprError("trailing input")
    return ast


def free_names(ast: tuple, bound: frozenset[str] = frozenset()) -> Iterator[str]:
    k = ast[0]
    if k == "id":
        if ast[1] not in bound:
            yield ast[1]
    elif k == "arr":
        for x in ast[1]:
            yield from free_names(x, bound)
    elif k == "obj":
        for _, v in ast[1]:
            yield from free_names(v, bound)
    elif k in ("neg", "not"):
        yield from free_names(ast[1], bound)
    elif k == "cond":
        for x in ast[1:]:
            yield from free_names(x, bound)
    elif k == "mem":
        yield from free_names(ast[1], bound)
        yield from free_names(ast[2], bound)
        key = ast[2]
        if key[0] == "lit" and len(key) > 1 and key[1] in BLOCKED_MEMBERS:
            yield f"member:{key[1]}"
        if key[0] in ("arr", "obj"):
            yield "member:computed"
    elif k == "call":
        yield from free_names(ast[1], bound)
        for x in ast[2]:
            yield from free_names(x, bound)
    elif k == "fn":
        yield from free_names(ast[2], bound | {ast[1], *ast[3:]})
    elif k == "bin":
        yield from free_names(ast[2], bound)
        yield from free_names(ast[3], bound)


_FN_NAMES = frozenset(n for n in BUILTIN_NAMES if n != "PI")


def fn_use(ast: tuple, bound: frozenset[str] = frozenset(), ctx: str | None = None) -> Iterator[str]:
    """Functions never become values: a builtin function name may only be a callee, an arrow only a
    direct call argument (same rule as evoui.js `fnUse`)."""
    k = ast[0]
    if k == "id":
        if ast[1] not in bound and ast[1] in _FN_NAMES and ctx != "callee":
            yield f"fn-value:{ast[1]}"
    elif k == "fn":
        if ctx != "arg":
            yield "fn-arrow"
        yield from fn_use(ast[2], bound | {ast[1], *ast[3:]})
    elif k == "call":
        yield from fn_use(ast[1], bound, "callee")
        for x in ast[2]:
            yield from fn_use(x, bound, "arg")
    elif k == "arr":
        for x in ast[1]:
            yield from fn_use(x, bound)
    elif k == "obj":
        for _, v in ast[1]:
            yield from fn_use(v, bound)
    elif k in ("neg", "not"):
        yield from fn_use(ast[1], bound)
    elif k == "cond":
        for x in ast[1:]:
            yield from fn_use(x, bound)
    elif k == "mem":
        yield from fn_use(ast[1], bound)
        yield from fn_use(ast[2], bound)
    elif k == "bin":
        yield from fn_use(ast[2], bound)
        yield from fn_use(ast[3], bound)


# ---- the block ----

def _is_obj(x: Any) -> bool:
    return isinstance(x, dict)


def _is_num(x: Any) -> bool:
    return isinstance(x, int | float) and not isinstance(x, bool)


def _is_int(x: Any) -> bool:
    return _is_num(x) and float(x).is_integer()


def _is_str(x: Any) -> bool:
    return isinstance(x, str)


def _js_len(s: str) -> int:
    """Length in UTF-16 code units, as JavaScript counts it."""
    return len(s.encode("utf-16-le")) // 2


def _route_value(v: Any) -> tuple[str, Any] | None:
    """A route option value's kind and value (None: not text / a number / true-false): 1, true and "1"
    differ, 1 equals 1.0 (as in JavaScript)."""
    if isinstance(v, bool):
        return ("b", v)
    if _is_num(v):
        return ("n", float(v))
    if _is_str(v):
        return ("s", v)
    return None


def _flags(f: str) -> int:
    return re.ASCII | (re.IGNORECASE if "i" in f else 0)


_APP_FORBIDDEN = [(re.compile(p, _flags(f)), m) for p, f, m in APP_FORBIDDEN]
_APP_URLS = [re.compile(p, _flags(f)) for p, f in APP_URL_PATTERNS]
_APP_EXTERNAL = re.compile(APP_URL_EXTERNAL, re.ASCII)
_APP_LOCAL = re.compile(APP_URL_LOCAL, re.ASCII | re.IGNORECASE)
_APP_TRIM = re.compile(APP_URL_TRIM, re.ASCII)


def app_html_problems(html: str) -> list[str]:
    """The static scan of an `app` node's html (evoui.js `appHtmlProblems`, same regexes): forbidden
    tags / storage / parent access, and every URL in src= / href= / url( / import that names a scheme
    or a //host other than data: / blob: / javascript: / about: or an allowlisted CDN. Best effort:
    the iframe sandbox and the CSP are the boundary."""
    out = [m for rx, m in _APP_FORBIDDEN if rx.search(html)]
    bad: list[str] = []
    for rx in _APP_URLS:
        for mt in rx.finditer(html):
            u = _APP_TRIM.sub("", mt.group(1))
            if (_APP_EXTERNAL.match(u) and not _APP_LOCAL.match(u)
                    and not u.lower().startswith(APP_CDN_ALLOWLIST) and u not in bad):
                bad.append(u)
    out += [f"external URL {u[:80]} (only {' '.join(APP_CDN_ALLOWLIST)})" for u in bad]
    return out


def without_app_html(x: Any, apps: set[int]) -> Any:
    """x with the `html` of the app nodes the validator's walk found (`apps`: their id()s) replaced by ""
    (the block cap does not count an app's html). Anything else shaped like an app node (a state value,
    say) keeps its html and counts."""
    if isinstance(x, dict):
        return {k: "" if k == "html" and id(x) in apps and _is_str(v) else without_app_html(v, apps)
                for k, v in x.items()}
    if isinstance(x, list):
        return [without_app_html(v, apps) for v in x]
    return x


def validate(block: Any) -> list[str]:
    errs: list[str] = []
    e = errs.append
    if not _is_obj(block):
        return ["block is not an object"]
    if block.get("type") != "evoui":
        e("type must be evoui")
    for k in block:
        if k not in TOP:
            e(f"unknown field {k}")
    for k in TOP_REQUIRED:
        if k not in block:
            e(f"missing {k}")

    def text(v: Any, where: str, mx: int) -> None:
        if not _is_str(v):
            e(f"{where}: must be text")
            return
        if not v.strip():
            e(f"{where}: empty")
        if _js_len(v) > mx:
            e(f"{where}: longer than {mx}")

    if "title" in block:
        text(block["title"], "title", 120)
    if "question" in block:
        text(block["question"], "question", 300)
    if "fallback" in block:
        text(block["fallback"], "fallback", MAX_FALLBACK_CHARS)
    state: dict = {}
    derived: dict = {}
    if "state" in block:
        if _is_obj(block["state"]):
            state = block["state"]
        else:
            e("state must be an object")
    if "derived" in block:
        if _is_obj(block["derived"]):
            derived = block["derived"]
        else:
            e("derived must be an object")
    if len(state) > MAX_STATE:
        e(f"more than {MAX_STATE} states")
    if len(derived) > MAX_DERIVED:
        e(f"more than {MAX_DERIVED} derived values")
    scope: set[str] = set()

    def good_name(k: str, where: str) -> None:
        if not _NAME.match(k) or k in _BUILT or k in _RESERVED:
            e(f"{where}: bad name {k}")
        elif k in scope:
            e(f"{where}: name {k} used twice")
        scope.add(k)

    for k in state:
        good_name(k, "state")

    def expr(src: Any, where: str) -> None:
        if _is_num(src):
            return
        if not _is_str(src):
            e(f"{where}: expression must be text or a number")
            return
        if _js_len(src) > MAX_EXPR_CHARS:
            e(f"{where}: expression longer than {MAX_EXPR_CHARS}")
            return
        try:
            ast = parse(src)
        except (ExprError, IndexError) as x:
            e(f"{where}: {x}")
            return
        for name in (*free_names(ast), *fn_use(ast)):
            if name == "member:computed":
                e(f"{where}: a member key cannot be an array or object")
            elif name == "fn-arrow":
                e(f"{where}: an arrow function can only be an argument of a call")
            elif name.startswith("fn-value:"):
                e(f"{where}: {name[9:]} can only be called, not used as a value")
            elif name.startswith("member:"):
                e(f"{where}: member {name[7:]} is not allowed")
            elif name not in scope and name not in _BUILT:
                e(f"{where}: unknown name {name}")

    def tpl(s: Any, where: str) -> None:
        if not _is_str(s):
            e(f"{where}: template must be text")
            return
        if _js_len(s) > MAX_TEXT_CHARS:
            e(f"{where}: longer than {MAX_TEXT_CHARS}")
            return
        for m in _TEMPLATE.finditer(s):
            expr(m.group(1), where)
        if "{" in _TEMPLATE.sub("", s):
            e(f"{where}: a {{ that never closes")

    for k, v in derived.items():
        if not _is_str(v):
            e(f"derived.{k}: must be an expression")
        else:
            expr(v, f"derived.{k}")
        good_name(k, "derived")

    def fields(o: dict, req: tuple[str, ...], opt: tuple[str, ...], where: str) -> None:
        for f in req:
            if f not in o:
                e(f"{where}: missing {f}")
        for f in o:
            if f not in ("kind", "shape") and f not in req and f not in opt:
                e(f"{where}: unknown field {f}")

    def boolean(o: dict, f: str, where: str) -> None:
        if f in o and not isinstance(o[f], bool):
            e(f"{where}.{f}: must be true/false")

    def one_of(o: dict, f: str, vals: tuple[str, ...], where: str) -> None:
        if f in o and not (_is_str(o[f]) and o[f] in vals):
            e(f"{where}.{f}: must be one of {'/'.join(vals)}")

    count = 0
    node_ids: set[str] = set()
    id_nodes: dict[str, dict] = {}                  # id -> node (first one), for reveal `after`
    reveal_refs: list[tuple[dict, str]] = []        # (reveal node, where) with an `after` to resolve
    pagers: list[tuple[dict, str]] = []             # (pager node, where): no pager inside another
    apps: list[str] = []                            # where each app node is (at most one per block)
    app_ids: set[int] = set()                       # id() of each app node the walk reached (block cap)

    in_each = 0

    def scoped(names: list[str], fn: Callable[[], None]) -> None:
        fresh = [x for x in names if x not in scope]
        scope.update(fresh)
        fn()
        scope.difference_update(fresh)

    def each_names(n: dict, where: str) -> list[str]:
        names = [n["as"] if _is_str(n.get("as")) else "d", n["index"] if _is_str(n.get("index")) else "i"]
        for x in names:
            if not _NAME.match(x) or x in _BUILT or x in _RESERVED:
                e(f"{where}: bad name {x}")
            elif x in scope:
                e(f"{where}: {x} is already a name here: give this each another one")
        if "in" in n:
            expr(n["in"], f"{where}.in")
        return names

    def state_map(m: Any, where: str, names: list[str]) -> None:
        if not _is_obj(m) or not m:
            e(f"{where}: {{state: value}}")
            return
        for k, v in m.items():
            if k not in state:
                e(f"{where}: {k} is not a state")
            if _is_str(v):
                scoped(names, lambda v=v, k=k: expr(v, f"{where}.{k}"))
            elif not (_is_num(v) or isinstance(v, bool)):
                e(f"{where}.{k}: a number, true/false or an expression")

    def pictures(n: dict, where: str) -> None:
        ims = n["images"]
        if not isinstance(ims, list) or len(ims) > MAX_APP_IMAGES:
            e(f"{where}.images: a list of up to {MAX_APP_IMAGES} images")
            return
        seen: set[str] = set()
        for i, im in enumerate(ims):
            w = f"{where}.images[{i}]"
            if not _is_obj(im):
                e(f"{w}: an image")
                continue
            fields(im, APP_IMAGE_FIELDS[0], APP_IMAGE_FIELDS[1], w)
            for k, v in im.items():
                text(v, f"{w}.{k}", MAX_APP_LABEL if k not in ("source_url", "license_url") else MAX_TEXT_CHARS)
            if "id" not in im:
                continue
            if not (_is_str(im["id"]) and NODE_ID_RE.fullmatch(im["id"])):
                e(f"{w}.id: [a-z0-9_-], 1–32")
            elif im["id"] in seen:
                e(f"{w}.id: used twice")
            else:
                seen.add(im["id"])

    def mark_attr(k: str, v: Any, n: dict, where: str) -> None:
        if _is_num(v):
            return
        if isinstance(v, bool):
            if k not in SVG_BOOL:
                e(f"{where}.{k}: a number or text, not true/false")
            return
        if isinstance(v, list):
            plot_range = n.get("el") == "plot" and k in ("x", "y") and len(v) == 2 and all(_is_num(x) for x in v)
            pts = k == "points" and all(isinstance(p, list) and len(p) == 2 and all(_is_num(x) for x in p) for p in v)
            if not plot_range and not pts:
                e(f"{where}.{k}: not a list here")
            return
        if not _is_str(v):
            e(f"{where}.{k}: a number or text")
            return
        tpl(v, f"{where}.{k}")
        fixed = "{" not in v
        if fixed and k in COLOR_ATTRS and v not in SVG_COLORS and not COLOR_RE.match(v):
            e(f"{where}.{k}: {v} is not a colour: use {' '.join(SVG_COLORS[1:17])}")
        if fixed and k == "d" and not PATH_RE.match(v):
            e(f"{where}.d: only path commands and numbers")
        if fixed and k == "clip-path" and not re.match(r"^url\(#[a-z0-9_-]+\)$", v, re.IGNORECASE):
            e(f"{where}.clip-path: url(#id)")
        if fixed and k == "attributeName" and v not in SMIL_ATTRS:
            e(f"{where}.attributeName: {v} cannot be animated")
        if k == "id" and not (fixed and NODE_ID_RE.fullmatch(v)):
            e(f"{where}.id: [a-z0-9_-], 1–32")

    def mark(n: Any, path: str, depth: int, images: set[Any]) -> None:
        nonlocal count
        count += 1
        if count > MAX_NODES:
            if count == MAX_NODES + 1:
                e(f"more than {MAX_NODES} nodes")
            return
        if depth > MAX_DEPTH:
            e(f"{path}: nested deeper than {MAX_DEPTH}")
            return
        if not _is_obj(n):
            e(f"{path}: not an element")
            return
        kind = n.get("kind")
        if kind == "each":
            where = f"{path}.each"
            fields(n, *NODE_FIELDS["each"], where)
            names = each_names(n, where)
            kids = n.get("children")
            if not isinstance(kids, list):
                if "children" in n:
                    e(f"{where}.children: must be a list")
                return

            def inside() -> None:
                for i, c in enumerate(kids):
                    mark(c, f"{where}[{i}]", depth + 1, images)
            scoped(names, inside)
            return
        if kind == "if":
            where = f"{path}.if"
            fields(n, *NODE_FIELDS["if"], where)
            if "cond" in n:
                expr(n["cond"], f"{where}.cond")
            if "then" in n:
                mark(n["then"], f"{where}.then", depth + 1, images)
            if "else" in n and n["else"] is not None:
                mark(n["else"], f"{where}.else", depth + 1, images)
            return
        if "kind" in n:
            e(f"{path}: {kind} cannot go inside svg: only drawing elements, each and if")
            return
        el = n.get("el")
        if not (_is_str(el) and el in SVG_ELS):
            e(f"{path}: unknown element {el}: use {' '.join(list(SVG_ELS)[:14])}")
            return
        where = f"{path}.{el}"
        for f in SVG_ELS[el]:
            if f not in n:
                e(f"{where}: missing {f}")
        for k, v in n.items():
            if k in ("el", "children"):
                continue
            if k in ("click", "drag"):
                state_map(v, f"{where}.{k}", ["mx", "my"] if k == "drag" else [])
                continue
            if k not in SVG_ATTRS:
                e(f"{where}: unknown attribute {k}")
                continue
            mark_attr(k, v, n, where)
        if el == "image" and _is_str(n.get("src")) and n["src"] not in images:
            e(f"{where}.src: {n['src']} is not one of this svg's images")
        if "children" in n:
            if not isinstance(n["children"], list):
                e(f"{where}.children: must be a list")
                return
            if el not in SVG_KIDS:
                e(f"{where}: {el} takes no children")
                return
            for i, c in enumerate(n["children"]):
                mark(c, f"{where}[{i}]", depth + 1, images)

    def walk(n: Any, path: str, depth: int) -> None:
        nonlocal count, in_each
        count += 1
        if count > MAX_NODES:
            if count == MAX_NODES + 1:
                e(f"more than {MAX_NODES} nodes")
            return
        if depth > MAX_DEPTH:
            e(f"{path}: nested deeper than {MAX_DEPTH}")
            return
        if not _is_obj(n):
            e(f"{path}: not a node")
            return
        kind = n.get("kind")
        if not (_is_str(kind) and kind in NODE_FIELDS):
            e(f"{path}: unknown kind {kind}")
            return
        req, opt = NODE_FIELDS[kind]
        where = f"{path}.{kind}"
        fields(n, req, opt, where)
        if "tone" in n:
            one_of(n, "tone", BOX_TONES if kind in _BOX_TONED else TONES, where)
        if "gap" in n and not (_is_int(n["gap"]) and 0 <= n["gap"] <= 4):
            e(f"{where}.gap: 0–4")
        for f in ("columns", "span"):
            if kind in ("grid", "grid-item") and f in n and not (_is_int(n[f]) and 1 <= n[f] <= MAX_COLUMNS):
                e(f"{where}.{f}: 1–{MAX_COLUMNS}")
        one_of(n, "justify", ("start", "between", "center", "end"), where)
        if kind == "caption":
            one_of(n, "align", ("start", "center", "end"), where)
        one_of(n, "size", TITLE_SIZES if kind == "title" else TEXT_SIZES, where)
        one_of(n, "weight", ("normal", "medium", "bold"), where)
        one_of(n, "style", ("segmented", "buttons"), where)
        if in_each and kind in STATEFUL_KINDS:
            e(f"{where}: {kind} keeps its own progress: it cannot go inside each")
        if "background" in opt:
            for f in ("background", "width", "height", "flex", "radius", "padding", "color"):
                if f in n:
                    mark_attr(f, n[f], n, where)
            one_of(n, "align", ("start", "center", "end", "stretch"), where)
            boolean(n, "clip", where)
            if "click" in n:
                state_map(n["click"], f"{where}.click", [])
        if kind in ("text", "title") and "color" in n:
            mark_attr("color", n["color"], n, where)
        if kind == "text":
            one_of(n, "align", ("start", "center", "end"), where)
            boolean(n, "rich", where)
        if kind in STATEFUL_KINDS and "id" in n:
            nid = n["id"]
            if not (_is_str(nid) and NODE_ID_RE.fullmatch(nid)):
                e(f"{where}.id: [a-z0-9_-], 1–32")
            elif nid in node_ids:
                e(f"{where}.id: used twice")
            else:
                node_ids.add(nid)
                id_nodes[nid] = n
        for f in ("border", "wrap", "grid", "display"):
            boolean(n, f, where)
        if kind == "svg":
            if "viewBox" in n and not (_is_str(n["viewBox"]) and VIEWBOX_RE.match(n["viewBox"])):
                e(f'{where}.viewBox: four numbers, "0 0 640 300"')
            if "tween" in n and not (_is_int(n["tween"]) and 0 <= n["tween"] <= 2000):
                e(f"{where}.tween: 0–2000 ms")
            if "images" in n:
                pictures(n, where)
            images = {im.get("id") for im in n["images"] if _is_obj(im)} if isinstance(n.get("images"), list) else set()
            if "children" in n:
                if not isinstance(n["children"], list):
                    e(f"{where}.children: must be a list")
                else:
                    for i, c in enumerate(n["children"]):
                        mark(c, f"{where}[{i}]", depth + 1, images)
            return
        if kind == "each":
            names = each_names(n, where)
            kids = n.get("children")
            if not isinstance(kids, list):
                if "children" in n:
                    e(f"{where}.children: must be a list")
                return

            def inside() -> None:
                for i, c in enumerate(kids):
                    walk(c, f"{where}[{i}]", depth + 1)
            in_each += 1
            scoped(names, inside)
            in_each -= 1
            return
        if "children" in n:
            if not isinstance(n["children"], list):
                e(f"{where}.children: must be a list")
            else:
                for i, c in enumerate(n["children"]):
                    walk(c, f"{where}[{i}]", depth + 1)
        if kind in CONTROL_KINDS and "bind" in n and not (_is_str(n["bind"]) and n["bind"] in state):
            e(f"{where}: bind {n['bind']} is not a state")
        b = n.get("bind")
        bound = _is_str(b) and b in state
        if kind in NUMBER_CONTROLS and bound and not _is_num(state[b]):
            e(f"{where}: bind must be a number state")
        if kind in BOOL_CONTROLS and bound and not isinstance(state[b], bool) and state[b] not in (0, 1):
            e(f"{where}: bind must be a true/false state, or 0 / 1")
        if kind in ("text", "badge", "title", "caption"):
            if "text" in n:
                tpl(n["text"], f"{where}.text")
        elif kind == "math":
            if "tex" in n:
                text(n["tex"], f"{where}.tex", MAX_TEX_CHARS)
        elif kind == "icon":
            one_of(n, "name", ICONS, where)
        elif kind == "table":
            _table(n, where)
        elif kind == "button":
            if "label" in n:
                tpl(n["label"], f"{where}.label")
            st = n.get("set")
            if "set" in n:
                if not _is_obj(st) or not st or len(st) > MAX_STATE:
                    e(f"{where}.set: {{state: expression}}, 1–{MAX_STATE}")
                else:
                    for k, v in st.items():
                        if k not in state:
                            e(f"{where}.set: {k} is not a state")
                        expr(v, f"{where}.set.{k}")
        elif kind in NUMBER_CONTROLS:
            if kind == "player":
                iv = n.get("interval_ms")
                if "interval_ms" in n and not (_is_int(iv) and MIN_INTERVAL_MS <= iv <= MAX_INTERVAL_MS):
                    e(f"{where}.interval_ms: a whole number {MIN_INTERVAL_MS}–{MAX_INTERVAL_MS}")
                boolean(n, "loop", where)
            for f in ("min", "max", "step"):
                if f in n and not _is_num(n[f]):
                    e(f"{where}.{f}: must be a number")
            if _is_num(n.get("min")) and _is_num(n.get("max")) and not n["min"] < n["max"]:
                e(f"{where}: min must be below max")
            if _is_num(n.get("step")) and not n["step"] > 0:
                e(f"{where}: step must be positive")
            if "label" in n:
                tpl(n["label"], f"{where}.label")
        elif kind in OPTION_CONTROLS:
            if "label" in n:
                tpl(n["label"], f"{where}.label")
            opts = n.get("options")
            if not isinstance(opts, list) or not opts or len(opts) > MAX_OPTIONS:
                if "options" in n:
                    e(f"{where}.options: 1–{MAX_OPTIONS} options")
            else:
                for i, o in enumerate(opts):
                    if not _is_obj(o) or "label" not in o or "value" not in o or len(o) != 2:
                        e(f"{where}.options[{i}]: {{label, value}}")
                    else:
                        text(o["label"], f"{where}.options[{i}].label", MAX_TEXT_CHARS)
        elif kind in BOOL_CONTROLS:
            if "label" in n:
                tpl(n["label"], f"{where}.label")
        elif kind == "if":
            if "cond" in n:
                expr(n["cond"], f"{where}.cond")
            if "then" in n:
                walk(n["then"], f"{where}.then", depth + 1)
            if "else" in n and n["else"] is not None:
                walk(n["else"], f"{where}.else", depth + 1)
        elif kind == "plot":
            _plot(n, where)
        elif kind == "chart":
            one_of(n, "chart", CHART_KINDS, where)
            if "data" in n:
                if not _is_str(n["data"]):
                    e(f"{where}.data: must be an expression")
                else:
                    expr(n["data"], f"{where}.data")
            if "x" in n and not _is_str(n["x"]):
                e(f"{where}.x: must be a field name")
            if "series" in n:
                ss = n["series"]
                if not isinstance(ss, list) or not ss:
                    e(f"{where}.series: at least 1 series")
                elif n.get("chart") == "pie" and len(ss) != 1:
                    e(f"{where}.series: a pie has exactly one series")
                else:
                    for i, sr in enumerate(ss):
                        if not (_is_obj(sr) and _is_str(sr.get("key")) and _is_str(sr.get("label"))
                                and len(sr) == 2):
                            e(f"{where}.series[{i}]: {{key, label}}")
            for f in ("caption", "x_label", "y_label"):
                if f in n:
                    tpl(n[f], f"{where}.{f}")
            if "y_min" in n and not _is_num(n["y_min"]):
                e(f"{where}.y_min: must be a number")
            _chart_options(n, where)
        elif kind == "reveal":
            ch = n.get("children")
            if isinstance(ch, list) and not 1 <= len(ch) <= MAX_REVEAL_STEPS:
                e(f"{where}.children: 1–{MAX_REVEAL_STEPS} steps")
            one_of(n, "mode", REVEAL_MODES, where)
            if "label" in n:
                tpl(n["label"], f"{where}.label")
            if n.get("mode") == "after":
                if "after" not in n:
                    e(f"{where}.after: mode after needs the id of a practice node")
                elif not _is_str(n["after"]):
                    e(f"{where}.after: must be a node id")
                else:
                    reveal_refs.append((n, where))
            elif "after" in n:
                e(f"{where}.after: only with mode after")
        elif kind == "animate":
            one_of(n, "effect", ANIMATE_EFFECTS, where)
        elif kind == "transition":
            if "key" in n:
                expr(n["key"], f"{where}.key")
        elif kind == "celebrate":
            if "when" in n:
                expr(n["when"], f"{where}.when")
        elif kind == "timer":
            one_of(n, "mode", TIMER_MODES, where)
            sec = n.get("seconds")
            if n.get("mode") == "countdown":
                if not (_is_int(sec) and 1 <= sec <= MAX_TIMER_SECONDS):
                    e(f"{where}.seconds: a countdown needs seconds, a whole number 1–{MAX_TIMER_SECONDS}")
            elif n.get("mode") == "stopwatch" and "seconds" in n:
                e(f"{where}.seconds: only for a countdown")
            if "bind" in n:
                b = n["bind"]
                if not (_is_str(b) and b in state):
                    e(f"{where}: bind {b} is not a state")
                elif not _is_num(state[b]):
                    e(f"{where}: bind must be a number state")
            boolean(n, "autostart", where)
            if "label" in n:
                tpl(n["label"], f"{where}.label")
        elif kind == "pager":
            ch = n.get("children")
            if isinstance(ch, list) and not ch:
                e(f"{where}.children: at least 1 page")
            boolean(n, "gate", where)
            pagers.append((n, where))
        elif kind == "fill-blank":
            if "sentence" in n:
                text(n["sentence"], f"{where}.sentence", MAX_TEXT_CHARS)
                if _is_str(n["sentence"]) and n["sentence"].count(BLANK) != 1:
                    e(f"{where}.sentence: exactly one blank {BLANK}")
            opts = n.get("options")
            n_opts = 0
            if not isinstance(opts, list) or not 2 <= len(opts) <= MAX_OPTIONS:
                if "options" in n:
                    e(f"{where}.options: 2–{MAX_OPTIONS} options {{label, why}}")
            else:
                n_opts = len(opts)
                labels: set[str] = set()
                for i, o in enumerate(opts):
                    w = f"{where}.options[{i}]"
                    if not _is_obj(o):
                        e(f"{w}: {{label, why}}")
                        continue
                    fields(o, FILL_OPTION_FIELDS[0], FILL_OPTION_FIELDS[1], w)
                    for f in ("label", "why"):
                        if f in o:
                            text(o[f], f"{w}.{f}", MAX_TEXT_CHARS)
                    if _is_str(o.get("label")):
                        if o["label"].strip() in labels:
                            e(f"{w}.label: used twice")
                        labels.add(o["label"].strip())
            if "correct" in n and not (_is_int(n["correct"]) and 0 <= n["correct"] < n_opts):
                e(f"{where}.correct: an option number")
            if "why" in n:
                text(n["why"], f"{where}.why", MAX_TEXT_CHARS)
            if "cell" in n and not (_is_str(n["cell"]) and CELL_RE.fullmatch(n["cell"])):
                e(f"{where}.cell: a cell id like c1.interpret")
        elif kind == "sentence":
            nb = 0
            if "sentence" in n:
                text(n["sentence"], f"{where}.sentence", MAX_TEXT_CHARS)
                if _is_str(n["sentence"]):
                    nb = n["sentence"].count(BLANK)
                    if not 1 <= nb <= MAX_BLANKS:
                        e(f"{where}.sentence: 1–{MAX_BLANKS} blanks {BLANK}")
            words = n.get("words")
            n_words = 0
            if not isinstance(words, list) or not max(1, nb) <= len(words) <= MAX_WORDS:
                if "words" in n:
                    e(f"{where}.words: one word per blank or more, at most {MAX_WORDS}")
            else:
                n_words = len(words)
                seen: set[str] = set()
                for i, wd in enumerate(words):
                    text(wd, f"{where}.words[{i}]", MAX_WORD_CHARS)
                    if _is_str(wd):
                        if wd.strip() in seen:
                            e(f"{where}.words[{i}]: used twice")
                        seen.add(wd.strip())
            if "answer" in n:
                a = n["answer"]
                if not (isinstance(a, list) and a and all(_is_int(x) and 0 <= x < n_words for x in a)
                        and len(set(a)) == len(a)):
                    e(f"{where}.answer: distinct word numbers, one per blank")
                elif 1 <= nb <= MAX_BLANKS and len(a) != nb:
                    e(f"{where}.answer: {nb} blanks need {nb} word numbers")
            if "why" in n:
                text(n["why"], f"{where}.why", MAX_TEXT_CHARS)
            if "cell" in n and not (_is_str(n["cell"]) and CELL_RE.fullmatch(n["cell"])):
                e(f"{where}.cell: a cell id like c1.interpret")
        elif kind == "route":
            if "prompt" in n:
                tpl(n["prompt"], f"{where}.prompt")
            pref = n.get("preference")
            if "preference" in n and not (_is_str(pref) and NODE_ID_RE.fullmatch(pref)):
                e(f"{where}.preference: [a-z0-9_-], 1–32")
            opts = n.get("options")
            if not isinstance(opts, list) or len(opts) < MIN_ROUTE_OPTIONS:
                if "options" in n:
                    e(f"{where}.options: at least {MIN_ROUTE_OPTIONS} options "
                      "{label, value, next, desc}")
                return
            sk = _route_value(state.get(b)) if bound else None
            values: set[tuple[str, Any]] = set()
            for i, o in enumerate(opts):
                w = f"{where}.options[{i}]"
                if not _is_obj(o):
                    e(f"{w}: {{label, value, next, desc}}")
                    continue
                fields(o, ROUTE_OPTION_FIELDS[0], ROUTE_OPTION_FIELDS[1], w)
                for f in ("label", "next", "desc"):
                    if f in o:
                        text(o[f], f"{w}.{f}", MAX_TEXT_CHARS)
                if "value" not in o:
                    continue
                v = o["value"]
                vk = _route_value(v)
                if vk is None or (vk[0] == "s" and not 1 <= _js_len(v) <= MAX_ROUTE_VALUE_CHARS):
                    e(f"{w}.value: text (1–{MAX_ROUTE_VALUE_CHARS}), a number or true/false")
                    continue
                if vk in values:
                    e(f"{w}.value: used twice")
                values.add(vk)
                if bound and (sk is None or sk[0] != vk[0]):
                    e(f"{w}.value: must have the type of the state {b}")
        elif kind == "app":
            apps.append(where)
            app_ids.add(id(n))
            _app(n, where)
        elif kind == "timeline":
            evs = n.get("events")
            if not isinstance(evs, list) or len(evs) < MIN_EVENTS:
                if "events" in n:
                    e(f"{where}.events: at least {MIN_EVENTS} events {{title, when, text, tone}}")
                return
            for i, x in enumerate(evs):
                w = f"{where}.events[{i}]"
                if not _is_obj(x):
                    e(f"{w}: {{title, when, text, tone}}")
                    continue
                fields(x, TIMELINE_EVENT_FIELDS[0], TIMELINE_EVENT_FIELDS[1], w)
                for f in ("title", "when", "text"):
                    if f in x:
                        text(x[f], f"{w}.{f}", MAX_TEXT_CHARS)
                one_of(x, "tone", TONES, w)
        elif kind == "deep-dive":
            if isinstance(n.get("children"), list) and not n["children"]:
                e(f"{where}.children: at least 1 node")
            if "label" in n:
                text(n["label"], f"{where}.label", MAX_TEXT_CHARS)
        elif kind == "flashcards":
            boolean(n, "shuffle", where)
            if "title" in n:
                text(n["title"], f"{where}.title", MAX_TEXT_CHARS)
            cards = n.get("cards")
            if not isinstance(cards, list) or len(cards) < MIN_CARDS:
                if "cards" in n:
                    e(f"{where}.cards: at least {MIN_CARDS} cards")
                return
            for i, c in enumerate(cards):
                w = f"{where}.cards[{i}]"
                if not _is_obj(c):
                    e(f"{w}: not a card")
                    continue
                fields(c, CARD_FIELDS[0], CARD_FIELDS[1], w)
                for f in ("front", "back", "hint"):
                    if f in c:
                        text(c[f], f"{w}.{f}", MAX_TEXT_CHARS)
                if "cell" in c and not (_is_str(c["cell"]) and CELL_RE.fullmatch(c["cell"])):
                    e(f"{w}.cell: a cell id like c1.interpret")
        elif kind == "quiz":
            qs = n.get("questions")
            if not isinstance(qs, list) or not qs:
                if "questions" in n:
                    e(f"{where}.questions: at least 1 question")
                return
            cases = _cases(n, where)
            one_of(n, "layout", QUIZ_LAYOUTS, where)
            if "title" in n:
                text(n["title"], f"{where}.title", MAX_TEXT_CHARS)
            for i, q in enumerate(qs):
                w = f"{where}.q{i}"
                if not _is_obj(q):
                    e(f"{w}: not a question")
                    continue
                fields(q, QUESTION_FIELDS[0], QUESTION_FIELDS[1], w)
                one_of(q, "type", QUESTION_TYPES, w)
                if "hint" in q:
                    text(q["hint"], f"{w}.hint", MAX_TEXT_CHARS)
                if cases is not None and q.get("case") not in cases:
                    e(f"{w}.case: must be one of the quiz's cases")
                if cases is None and "case" in q:
                    e(f"{w}.case: the quiz has no cases")
                if "cell" in q and not (_is_str(q["cell"]) and CELL_RE.match(q["cell"])):
                    e(f"{w}.cell: a cell id like c1.interpret")
                if "q" in q:
                    text(q["q"], f"{w}.q", MAX_TEXT_CHARS)
                if "why" in q:
                    text(q["why"], f"{w}.why", MAX_TEXT_CHARS)
                opts = q.get("options")
                if (not isinstance(opts, list) or len(opts) < 2 or len(opts) > MAX_OPTIONS
                        or not all(_is_str(o) for o in opts)):
                    e(f"{w}.options: 2–{MAX_OPTIONS} texts")
                n_opts = len(opts) if isinstance(opts, list) else 0
                if q.get("type") == "tf" and n_opts != 2:
                    e(f"{w}.options: a true/false question has exactly 2 options")
                c = q.get("correct")
                if q.get("type") == "multi":
                    if not (isinstance(c, list) and c and all(_is_int(x) and 0 <= x < n_opts for x in c)
                            and len(set(c)) == len(c)):
                        e(f"{w}.correct: a multi question's correct is a list of distinct option numbers")
                elif not (_is_int(c) and 0 <= c < n_opts):
                    e(f"{w}: correct out of range")
                if "feedback" in q:
                    fb = q["feedback"]
                    if not isinstance(fb, list) or len(fb) != n_opts:
                        e(f"{w}.feedback: one text per option")
                    else:
                        for j, t in enumerate(fb):
                            text(t, f"{w}.feedback[{j}]", MAX_TEXT_CHARS)

    def _app(n: dict, where: str) -> None:
        if "html" in n:
            h = n["html"]
            text(h, f"{where}.html", MAX_APP_HTML)
            if _is_str(h) and h.strip() and _js_len(h) <= MAX_APP_HTML:
                for m in app_html_problems(h):
                    e(f"{where}.html: {m}")
        if "height" in n and not (_is_int(n["height"]) and MIN_APP_HEIGHT <= n["height"] <= MAX_APP_HEIGHT):
            e(f"{where}.height: a whole number {MIN_APP_HEIGHT}–{MAX_APP_HEIGHT}")
        if "images" in n:
            ims = n["images"]
            if not isinstance(ims, list) or len(ims) > MAX_APP_IMAGES:
                e(f"{where}.images: a list of up to {MAX_APP_IMAGES} images")
            else:
                seen: set[str] = set()
                for i, im in enumerate(ims):
                    w = f"{where}.images[{i}]"
                    if not _is_obj(im):
                        e(f"{w}: an image")
                        continue
                    fields(im, APP_IMAGE_FIELDS[0], APP_IMAGE_FIELDS[1], w)
                    for k, v in im.items():
                        text(v, f"{w}.{k}", MAX_APP_LABEL if k not in ("source_url", "license_url") else MAX_TEXT_CHARS)
                    iid = im.get("id")
                    if "id" not in im:
                        continue
                    if not (_is_str(iid) and NODE_ID_RE.fullmatch(iid)):
                        e(f"{w}.id: [a-z0-9_-], 1–32")
                    elif iid in seen:
                        e(f"{w}.id: used twice")
                    else:
                        seen.add(iid)
        if "items" not in n:
            return
        its = n["items"]
        if not isinstance(its, list) or len(its) > MAX_APP_ITEMS:
            e(f"{where}.items: a list of up to {MAX_APP_ITEMS} {{id, label, cell}}")
            return
        ids: set[str] = set()
        for i, it in enumerate(its):
            w = f"{where}.items[{i}]"
            if not _is_obj(it):
                e(f"{w}: {{id, label, cell}}")
                continue
            fields(it, APP_ITEM_FIELDS[0], APP_ITEM_FIELDS[1], w)
            if "id" in it:
                iid = it["id"]
                if not (_is_str(iid) and NODE_ID_RE.fullmatch(iid)):
                    e(f"{w}.id: [a-z0-9_-], 1–32")
                elif iid in ids:
                    e(f"{w}.id: used twice")
                else:
                    ids.add(iid)
            if "label" in it:
                text(it["label"], f"{w}.label", MAX_APP_LABEL)
            if "cell" in it and not (_is_str(it["cell"]) and CELL_RE.fullmatch(it["cell"])):
                e(f"{w}.cell: a cell id like c1.interpret")

    def _cases(n: dict, where: str) -> set[str] | None:
        if "cases" not in n:
            return None
        cs = n["cases"]
        ids: set[str] = set()
        if not isinstance(cs, list) or not cs or len(cs) > MAX_CASES:
            e(f"{where}.cases: 1–{MAX_CASES} cases")
            return ids
        for i, c in enumerate(cs):
            w = f"{where}.cases[{i}]"
            if (not _is_obj(c) or any(k not in ("id", "label", "advice") for k in c)
                    or "id" not in c or "label" not in c):
                e(f"{w}: {{id, label, advice}}")
                continue
            if not (_is_str(c["id"]) and _CASE_ID.match(c["id"])):
                e(f"{w}.id: [a-z0-9_-], 1–32")
            elif c["id"] in ids:
                e(f"{w}.id: used twice")
            else:
                ids.add(c["id"])
            text(c["label"], f"{w}.label", MAX_TEXT_CHARS)
            if "advice" in c:
                text(c["advice"], f"{w}.advice", MAX_TEXT_CHARS)
        return ids

    def _table(n: dict, where: str) -> None:
        cols = n.get("columns")
        if "columns" in n:
            if not isinstance(cols, list) or not cols:
                e(f"{where}.columns: at least 1 text")
                cols = None
            else:
                for i, c in enumerate(cols):
                    text(c, f"{where}.columns[{i}]", MAX_TEXT_CHARS)
        if "caption" in n:
            tpl(n["caption"], f"{where}.caption")
        rows = n.get("rows")
        if "rows" not in n:
            return
        if _is_str(rows):
            expr(rows, f"{where}.rows")
            return
        if not isinstance(rows, list):
            e(f"{where}.rows: an expression or a list of rows")
            return
        for i, r in enumerate(rows):
            if not isinstance(r, list) or (isinstance(cols, list) and len(r) != len(cols)):
                e(f"{where}.rows[{i}]: one cell per column")
                continue
            for j, c in enumerate(r):
                tpl(c, f"{where}.rows[{i}][{j}]")

    def _chart_options(n: dict, where: str) -> None:
        for f, kinds in CHART_OPTION_KINDS.items():
            if f in n and n.get("chart") in CHART_KINDS and n["chart"] not in kinds:
                e(f"{where}.{f}: only for {'/'.join(kinds)} charts")
        for f in ("stack", "gradient", "donut"):
            boolean(n, f, where)
        one_of(n, "curve", CHART_CURVES, where)
        if "hover" in n:
            hv = n["hover"]
            if not (_is_str(hv) and hv in state):
                e(f"{where}.hover: {hv} is not a state")
            elif not _is_num(state[hv]):
                e(f"{where}.hover: must be a number state")
        if "visible_points" in n and not (_is_int(n["visible_points"]) and n["visible_points"] >= MIN_WINDOW):
            e(f"{where}.visible_points: a whole number, at least {MIN_WINDOW}")
        if "annotations" in n:
            an = n["annotations"]
            if not isinstance(an, list) or len(an) > MAX_ANNOTATIONS:
                e(f"{where}.annotations: a list of at most {MAX_ANNOTATIONS}")
                return
            for i, a in enumerate(an):
                w = f"{where}.annotations[{i}]"
                if not _is_obj(a) or any(k not in ANNOTATION_FIELDS for k in a) or ("x" in a) == ("y" in a) \
                        or "label" not in a:
                    e(f"{w}: {{x or y, label, tone}}")
                    continue
                expr(a["x" if "x" in a else "y"], f"{w}.{'x' if 'x' in a else 'y'}")
                tpl(a["label"], f"{w}.label")
                one_of(a, "tone", TONES, w)

    def _plot(n: dict, where: str) -> None:
        for f in ("xmin", "xmax", "ymin", "ymax"):
            if f in n and not _is_num(n[f]):
                e(f"{where}.{f}: must be a number")
        if _is_num(n.get("xmin")) and _is_num(n.get("xmax")) and not n["xmin"] < n["xmax"]:
            e(f"{where}: xmin must be below xmax")
        if _is_num(n.get("ymin")) and _is_num(n.get("ymax")) and not n["ymin"] < n["ymax"]:
            e(f"{where}: ymin must be below ymax")
        if "height" in n and not (_is_num(n["height"]) and 160 <= n["height"] <= 420):
            e(f"{where}.height: 160–420")
        if "legend" in n:
            lg = n["legend"]
            if not isinstance(lg, list):
                e(f"{where}.legend: must be a list")
            else:
                for i, item in enumerate(lg):
                    w = f"{where}.legend[{i}]"
                    if not _is_obj(item) or "label" not in item or any(
                            k not in ("label", "tone") for k in item):
                        e(f"{w}: {{label, tone}}")
                    else:
                        text(item["label"], f"{w}.label", MAX_TEXT_CHARS)
                        one_of(item, "tone", TONES, w)
        items = n.get("items")
        if not isinstance(items, list):
            if "items" in n:
                e(f"{where}.items: must be a list")
            return
        if len(items) > MAX_PLOT_ITEMS:
            e(f"{where}.items: more than {MAX_PLOT_ITEMS}")
        for i, it in enumerate(items):
            w = f"{where}.items[{i}]"
            if not _is_obj(it):
                e(f"{w}: not an item")
                continue
            shape = it.get("shape")
            if not (_is_str(shape) and shape in SHAPE_FIELDS):
                e(f"{w}: unknown shape {shape}")
                continue
            req, opt = SHAPE_FIELDS[shape]
            fields(it, req, opt, w)
            one_of(it, "tone", TONES, w)
            one_of(it, "anchor", ANCHORS, w)
            one_of(it, "size", ("sm", "md"), w)
            boolean(it, "dashed", w)
            boolean(it, "fill", w)
            for f in req:
                if f in it and f != "text":
                    expr(it[f], f"{w}.{f}")
            if "text" in it:
                tpl(it["text"], f"{w}.text")
            if "label" in it:
                tpl(it["label"], f"{w}.label")

    def _inside(n: Any) -> Iterator[Any]:
        """Every node below n (children, then, else)."""
        if not _is_obj(n):
            return
        for c in (n.get("children") if isinstance(n.get("children"), list) else ()):
            yield c
            yield from _inside(c)
        for f in ("then", "else"):
            if _is_obj(n.get(f)):
                yield n[f]
                yield from _inside(n[f])

    if "body" in block:
        walk(block["body"], "body", 1)
    for rv, where in reveal_refs:                  # after the walk: a target may come later in the block
        target = id_nodes.get(rv["after"])
        if target is None:
            e(f"{where}.after: no node has the id {rv['after']}")
        elif target.get("kind") not in PRACTICE_KINDS:
            e(f"{where}.after: {rv['after']} is a {target.get('kind')}, not a practice node "
              f"({' / '.join(PRACTICE_KINDS)})")
        elif any(x is target for x in _inside(rv)):
            e(f"{where}.after: {rv['after']} is inside this reveal (it would never open)")
    for pg, where in pagers:                       # a pager's pages hold no other pager
        if any(_is_obj(x) and x.get("kind") == "pager" for x in _inside(pg)):
            e(f"{where}: a pager cannot hold another pager")
    for where in apps[1:]:
        e(f"{where}: at most one app per block")
    if _js_len(json.dumps(without_app_html(block, app_ids), ensure_ascii=False, separators=(",", ":"))) > MAX_BLOCK_CHARS:
        e(f"block larger than {MAX_BLOCK_CHARS} characters")
    if "probes" in block:
        ps = block["probes"]
        if not isinstance(ps, list) or len(ps) > MAX_PROBES:
            e(f"probes: a list of up to {MAX_PROBES}")
        else:
            for i, pr in enumerate(ps):
                if not (_is_obj(pr) and _is_obj(pr.get("set")) and pr["set"] and len(pr) == 1):
                    e(f"probes[{i}]: {{set: {{state: value}}}}")
                    continue
                for k in pr["set"]:
                    if k not in state:
                        e(f"probes[{i}]: {k} is not a state")
    return errs
