"""The compact form a model writes a block in: markup with `{#each}`, `{#if}`, `{@state}`, `{@const}`, real SVG inside `<svg>`.
`compile_markup` turns it into the block JSON the validators and the renderer already know, so what is stored and drawn is
data as before; this module is only ever on the server's side of a block. A failure is a `MarkupError` whose text tells the
model how to fix it.

Models know JSX, Svelte, SVG and JavaScript, so what they write out of habit is read rather than refused: `strokeWidth`,
`Math.floor(x)`, `xs.map((x, i) => ...)`, `x.toFixed(2)`, `` `${n}%` ``, `value={x}` + `onChange`, `const [x, setX] = useState(1)`.
Each is rewritten to the block's own expression language at compile time; nothing of JavaScript reaches the renderer.
A `{@const f = x => ...}` is a macro: every call is replaced by its body."""

import json
import re
from dataclasses import dataclass, field
from typing import Any

from evowiki.deep.evoui.contracts import EVOUI_TYPE, ICONS, NODE_FIELDS, SVG_COLORS, SVG_ELS


class MarkupError(ValueError):
    pass


# ---- expressions ----

_NUM = re.compile(r"(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?")
_ID = re.compile(r"[A-Za-z_$一-鿿][A-Za-z0-9_$一-鿿]*")
_OPS = ("===", "!==", "**", "==", "!=", "<=", ">=", "&&", "||", "=>", "??", "?.")
_SINGLE = "+-*/%<>!?:.,()[]{}"
_PREC = (("||", "??"), ("&&"), ("==", "!="), ("<", "<=", ">", ">="), ("+", "-"), ("*", "/", "%"))
_MATH = {"sin", "cos", "tan", "atan2", "sqrt", "abs", "min", "max", "round", "floor", "ceil", "exp", "log", "pow", "asin",
         "acos", "atan", "hypot", "sign", "log2", "log10"}
_FUNCS = ["sin", "cos", "tan", "atan2", "sqrt", "abs", "min", "max", "round", "floor", "ceil", "exp", "log", "pow", "asin", "acos", "atan", "hypot", "sign", "log2", "log10", "clamp", "lerp", "remap", "rand", "fmt", "deg2rad", "range", "map", "filter", "fold", "iterate", "sum", "len", "slice", "concat", "reverse", "sort", "put", "swap", "repeat", "indexOf", "has", "join", "str", "arc"]


def _lex(s: str) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c.isspace():
            i += 1
            continue
        if c.isascii() and c.isdigit() or (c == "." and i + 1 < n and s[i + 1].isdigit()):
            m = _NUM.match(s, i)
            assert m
            out.append(("num", m.group(0)))
            i = m.end()
            continue
        if c in "\"'":
            j, v = i + 1, []
            while j < n and s[j] != c:
                if s[j] == "\\" and j + 1 < n:
                    j += 1
                    v.append({"n": "\n", "t": "\t"}.get(s[j], s[j]))
                else:
                    v.append(s[j])
                j += 1
            if j >= n:
                raise MarkupError(f"字符串没有结束：{s[:60]}")
            out.append(("str", "".join(v)))
            i = j + 1
            continue
        if c == "`":
            j, parts, buf = i + 1, [], []
            while j < n and s[j] != "`":
                if s.startswith("${", j):
                    k = _close(s, j + 1)
                    parts += ["".join(buf), ("expr", s[j + 2:k])]
                    buf = []
                    j = k + 1
                else:
                    buf.append(s[j])
                    j += 1
            if j >= n:
                raise MarkupError(f"模板字符串没有结束：{s[:60]}")
            parts.append("".join(buf))
            out.append(("tpl", parts))
            i = j + 1
            continue
        m = _ID.match(s, i)
        if m:
            word = {"and": "&&", "or": "||", "not": "!"}.get(m.group(0))        # a habit from Python
            out.append(("op", word) if word else ("id", m.group(0)))
            i = m.end()
            continue
        for op in _OPS:
            if s.startswith(op, i):
                out.append(("op", {"===": "==", "!==": "!=", "??": "||", "?.": "."}.get(op, op)))
                i += len(op)
                break
        else:
            if c == "." and s.startswith("...", i):
                raise MarkupError("不支持展开写法 ...：用 concat(a, b)、put(a, i, v)")
            if c in _SINGLE:
                out.append(("op", c))
                i += 1
            else:
                raise MarkupError(f"表达式里不能有 {c!r}：{s[:60]}")
    out.append(("eof", None))
    return out


def _parse(src: str) -> tuple:
    tk = _lex(src)
    p = 0

    def is_op(v: str, at: int | None = None) -> bool:
        t = tk[p if at is None else at]
        return t[0] == "op" and t[1] == v

    def eat(v: str) -> None:
        nonlocal p
        if not is_op(v):
            raise MarkupError(f"这里应该是 {v}：{src[:80]}")
        p += 1

    def expr() -> tuple:
        nonlocal p
        if tk[p][0] == "id" and is_op("=>", p + 1):
            name = tk[p][1]
            p += 2
            return ("fn", [name], arrow_body())
        if is_op("("):                                 # (a), (a, b), ()
            q, names = p + 1, []
            while tk[q][0] == "id":
                names.append(tk[q][1])
                q += 1
                if is_op(",", q):
                    q += 1
                else:
                    break
            if is_op(")", q) and is_op("=>", q + 1):
                p = q + 2
                return ("fn", names, arrow_body())
        c = binary(0)
        if is_op("?"):
            p += 1
            a = expr()
            eat(":")
            return ("cond", c, a, expr())
        return c

    def arrow_body() -> tuple:
        if is_op("{"):
            q = p
            depth = 0
            while tk[q][0] != "eof":
                if is_op("{", q):
                    depth += 1
                elif is_op("}", q):
                    depth -= 1
                    if not depth:
                        break
                q += 1
            if tk[p + 1][0] == "id" and tk[p + 1][1] in ("return", "const", "let", "var", "if", "for"):
                raise MarkupError("箭头函数体只能是一个表达式：把中间值写成 {@const}，分支写成 a ? b : c")
        return expr()

    def binary(lv: int) -> tuple:
        nonlocal p
        if lv >= len(_PREC):
            return power()
        left = binary(lv + 1)
        ops = _PREC[lv] if isinstance(_PREC[lv], tuple) else (_PREC[lv],)
        while tk[p][0] == "op" and tk[p][1] in ops:
            op = tk[p][1]
            p += 1
            left = ("bin", op, left, binary(lv + 1))
        return left

    def power() -> tuple:
        nonlocal p
        left = unary()
        if is_op("**"):
            p += 1
            return ("bin", "**", left, power())
        return left

    def unary() -> tuple:
        nonlocal p
        if is_op("-"):
            p += 1
            return ("neg", unary())
        if is_op("!"):
            p += 1
            return ("not", unary())
        if is_op("+"):
            p += 1
            return unary()
        return postfix(primary())

    def postfix(e: tuple) -> tuple:
        nonlocal p
        while True:
            if is_op("."):
                p += 1
                t = tk[p]
                p += 1
                if t[0] != "id":
                    raise MarkupError(f". 后面应该是名字：{src[:80]}")
                e = ("mem", e, t[1])
            elif is_op("["):
                p += 1
                i = expr()
                eat("]")
                e = ("idx", e, i)
            elif is_op("("):
                p += 1
                args = []
                if not is_op(")"):
                    args.append(expr())
                    while is_op(","):
                        p += 1
                        if is_op(")"):
                            break
                        args.append(expr())
                eat(")")
                e = ("call", e, args)
            else:
                return e

    def primary() -> tuple:
        nonlocal p
        t = tk[p]
        if t[0] == "num":
            p += 1
            return ("num", t[1])
        if t[0] == "str":
            p += 1
            return ("str", t[1])
        if t[0] == "tpl":
            p += 1
            out: tuple | None = None
            for part in t[1]:
                x = ("str", part) if isinstance(part, str) else _parse(part[1])
                if isinstance(part, str) and not part:
                    continue
                out = x if out is None else ("bin", "+", out, x)
            if out is None:
                return ("str", "")
            return out if out[0] == "bin" or out[0] == "str" else ("bin", "+", ("str", ""), out)
        if t[0] == "id":
            p += 1
            if t[1] in ("true", "false", "null", "undefined"):
                return ("lit", {"true": True, "false": False}.get(t[1]))
            return ("id", t[1])
        if is_op("("):
            p += 1
            e = expr()
            eat(")")
            return ("group", e)
        if is_op("["):
            p += 1
            items = []
            while not is_op("]"):
                items.append(expr())
                if is_op(","):
                    p += 1
                elif not is_op("]"):
                    raise MarkupError(f"数组里缺逗号：{src[:80]}")
            p += 1
            return ("arr", items)
        if is_op("{"):
            p += 1
            props = []
            while not is_op("}"):
                k = tk[p]
                p += 1
                if k[0] not in ("id", "str", "num"):
                    raise MarkupError(f"对象的键写错了：{src[:80]}")
                if is_op(":"):
                    p += 1
                    props.append((str(k[1]), expr()))
                else:
                    props.append((str(k[1]), ("id", k[1])))
                if is_op(","):
                    p += 1
                elif not is_op("}"):
                    raise MarkupError(f"对象里缺逗号：{src[:80]}")
            p += 1
            return ("obj", props)
        raise MarkupError(f"表达式读不懂：{src[:80]}")

    ast = expr()
    if tk[p][0] != "eof":
        raise MarkupError(f"表达式读不懂（多了 {tk[p][1]}）：{src[:80]}")
    return ast


def _fold(a: tuple, env: dict[str, Any]) -> tuple[bool, Any]:
    """The value of an expression over constants at compile time, for what has to be fixed (a control's options)."""
    k = a[0]
    if k in ("num", "str", "lit"):
        return _constant(a)
    if k == "id":
        return (a[1] in env, env.get(a[1]))
    if k == "group":
        return _fold(a[1], env)
    if k == "arr" or k == "obj":
        items = a[1] if k == "arr" else [v for _, v in a[1]]
        got = [_fold(x, env) for x in items]
        if not all(ok for ok, _ in got):
            return False, None
        vals = [v for _, v in got]
        return True, vals if k == "arr" else dict(zip((n for n, _ in a[1]), vals, strict=True))
    if k == "mem":
        ok, o = _fold(a[1], env)
        return (True, o[a[2]]) if ok and isinstance(o, dict) and a[2] in o else (False, None)
    if k == "idx":
        ok, o = _fold(a[1], env)
        oki, i = _fold(a[2], env)
        return (True, o[i]) if ok and oki and isinstance(o, list) and isinstance(i, int) and 0 <= i < len(o) else (False, None)
    if k == "bin" and a[1] == "+":
        ok, x = _fold(a[2], env)
        oky, y = _fold(a[3], env)
        return (True, x + y) if ok and oky and type(x) is type(y) else (False, None)
    if k == "call" and a[1] == ("id", "map") and len(a[2]) == 2 and a[2][1][0] == "fn":
        ok, xs = _fold(a[2][0], env)
        if not ok or not isinstance(xs, list):
            return False, None
        names, body = a[2][1][1], a[2][1][2]
        out = []
        for i, x in enumerate(xs):
            scope = {**env, names[0]: x, **({names[1]: i} if len(names) > 1 else {})}
            ok, v = _fold(body, scope)
            if not ok:
                return False, None
            out.append(v)
        return True, out
    return False, None


def _subst(a: Any, env: dict[str, tuple]) -> Any:
    if isinstance(a, tuple) and a:
        if a[0] == "id":
            return ("group", env[a[1]]) if a[1] in env else a
        if a[0] == "fn":
            return ("fn", a[1], _subst(a[2], {k: v for k, v in env.items() if k not in a[1]}))
        return tuple(_subst(x, env) for x in a)
    if isinstance(a, list):
        return [_subst(x, env) for x in a]
    return a


@dataclass
class _Scope:
    macros: dict[str, tuple] = field(default_factory=dict)
    locals: dict[str, tuple] = field(default_factory=dict)


def _rewrite(a: Any, sc: _Scope, depth: int = 0) -> Any:
    """JavaScript habits -> the block's expression language."""
    if depth > 40:
        raise MarkupError("{@const} 函数互相调用太深")
    rw = lambda x: _rewrite(x, sc, depth + 1)
    if not isinstance(a, tuple) or not a:
        return a
    k = a[0]
    if k in ("num", "str", "lit"):
        return a
    if k == "id":
        if a[1] == "PI":
            return a
        if a[1] in sc.locals:
            return ("group", sc.locals[a[1]])
        if a[1] in sc.macros:
            raise MarkupError(f"{a[1]} 是函数，只能调用，不能当值")
        if a[1] == "$":
            raise MarkupError("名字里不能有 $")
        return a
    if k == "group":
        return ("group", rw(a[1]))
    if k == "neg" or k == "not":
        return (k, rw(a[1]))
    if k == "cond":
        return ("cond", rw(a[1]), rw(a[2]), rw(a[3]))
    if k == "bin":
        return ("bin", a[1], rw(a[2]), rw(a[3]))
    if k == "arr":
        return ("arr", [rw(x) for x in a[1]])
    if k == "obj":
        return ("obj", [(n, rw(v)) for n, v in a[1]])
    if k == "fn":
        names = a[1]
        if len(names) > 2:
            raise MarkupError("箭头函数最多两个参数")
        if not names:
            raise MarkupError("箭头函数要有参数")
        return ("fn", names, rw(a[2]))
    if k == "idx":
        return ("idx", rw(a[1]), rw(a[2]))
    if k == "mem":
        o = a[1]
        if o == ("id", "Math"):
            if a[2] == "PI":
                return ("id", "PI")
            if a[2] == "E":
                return ("num", "2.718281828459045")
            if a[2] == "SQRT2":
                return ("num", "1.4142135623730951")
            raise MarkupError(f"Math.{a[2]} 不能当值")
        return ("mem", rw(o), a[2])
    if k == "call":
        return _call(a[1], [x for x in a[2]], sc, rw)
    raise MarkupError(f"表达式读不懂：{k}")


_COMPARATOR_ASC = (("fn", ["a", "b"], ("bin", "-", ("id", "a"), ("id", "b"))),)


def _call(f: tuple, args: list[tuple], sc: _Scope, rw: Any) -> tuple:
    def call(name: str, *xs: tuple) -> tuple:
        return ("call", ("id", name), list(xs))

    # a {@const} function passed by name: iterate(n, s0, step), xs.map(f)
    args = [sc.macros[a[1]] if a[0] == "id" and a[1] in sc.macros else a for a in args]

    if f[0] == "id":
        name = f[1]
        if name in sc.macros:
            m = sc.macros[name]
            if len(args) != len(m[1]):
                raise MarkupError(f"{name} 要 {len(m[1])} 个参数")
            return rw(("group", _subst(m[2], dict(zip(m[1], args, strict=True)))))
        if name in ("Number", "parseFloat", "parseInt") and args:
            return rw(("group", args[0]))
        if name == "String" and args:
            return call("str", rw(args[0]))
        if name == "Array" and len(args) == 1:
            return ("arr_n", rw(args[0]))
        if name not in _FUNCS:
            raise MarkupError(f"没有函数 {name}：可用 {' '.join(_FUNCS)}；或者先写 {{@const {name} = x => ...}}")
        return call(name, *[rw(x) for x in args])
    if f[0] == "mem":
        o, m = f[1], f[2]
        if o == ("id", "Math"):
            if m in _MATH:
                return call(m, *[rw(x) for x in args])
            if m == "trunc":
                return call("floor", *[rw(x) for x in args])
            if m == "expm1" and len(args) == 1:
                return ("group", ("bin", "-", call("exp", rw(args[0])), ("num", "1")))
            if m == "log1p" and len(args) == 1:
                return call("log", ("group", ("bin", "+", rw(args[0]), ("num", "1"))))
            if m == "random":
                raise MarkupError("Math.random 每次重画都变：用 rand(i)，同一个 i 永远同一个 0–1 的数")
            raise MarkupError(f"没有 Math.{m}")
        if o == ("id", "Array") and m == "from" and args and args[0][0] == "obj":
            props = dict(args[0][1])
            if "length" not in props:
                raise MarkupError("Array.from 只读 {length: n}")
            base = call("range", rw(props["length"]))
            return call("map", base, rw(args[1])) if len(args) > 1 else base
        if o == ("id", "Object") or o == ("id", "JSON") or o == ("id", "Date"):
            raise MarkupError(f"{o[1]}.{m} 不能用：可用 {' '.join(_FUNCS)}")
        if o[0] == "call" and o[1] == ("id", "Array") and m == "fill" and len(args) == 1:
            return call("repeat", rw(args[0]), rw(o[2][0]))
        x = rw(o)
        ra = [rw(y) for y in args]
        table = {"map": "map", "filter": "filter", "slice": "slice", "concat": "concat", "reverse": "reverse", "join": "join",
                 "indexOf": "indexOf", "includes": "has", "toString": "str"}
        if m in table:
            return call(table[m], x, *ra)
        if m == "toFixed":
            return call("fmt", x, *(ra or [("num", "0")]))
        if m == "toLocaleString":
            return call("str", x)
        if m == "reduce":
            if len(args) != 2:
                raise MarkupError("reduce 要写初值：a.reduce((s, x) => ..., 0)")
            return call("fold", x, ra[1], ra[0])
        if m == "sort":
            if args and args[0] != _COMPARATOR_ASC[0]:
                raise MarkupError("sort 只能从小到大排数字：sort(a)，要倒序再 reverse(sort(a))")
            return call("sort", x)
        if m == "at":
            return ("idx", x, ra[0])
        if m == "charAt":
            return ("idx", x, ra[0])
        if m == "some":
            return ("group", ("bin", ">", call("len", call("filter", x, ra[0])), ("num", "0")))
        if m == "every":
            return ("group", ("bin", "==", call("len", call("filter", x, ra[0])), call("len", x)))
        raise MarkupError(f".{m}() 不支持：可用 map filter reduce slice concat reverse sort join includes indexOf toFixed")
    raise MarkupError("只能调用函数名")


def _out(a: Any) -> str:
    k = a[0]
    if k == "num":
        return a[1]
    if k == "str":
        return '"' + a[1].replace("\\", "\\\\").replace('"', '\\"') + '"'
    if k == "lit":
        return {True: "true", False: "false", None: "null"}[a[1]]
    if k == "id":
        return a[1]
    if k == "group":
        return f"({_out(a[1])})" if a[1][0] in ("bin", "cond", "neg", "not", "fn") else _out(a[1])
    if k == "neg":
        return f"-{_out(a[1])}"
    if k == "not":
        return f"!{_out(a[1])}"
    if k == "cond":
        return f"{_out(a[1])}?{_out(a[2])}:{_out(a[3])}"
    if k == "bin":
        return f"{_paren(a[2])}{a[1]}{_paren(a[3])}"
    if k == "arr":
        return "[" + ",".join(_out(x) for x in a[1]) + "]"
    if k == "obj":
        return "{" + ",".join(f"{n}:{_out(v)}" for n, v in a[1]) + "}"
    if k == "fn":
        head = a[1][0] if len(a[1]) == 1 else f"({a[1][0]},{a[1][1]})"
        return f"{head}=>{_out(a[2])}"
    if k == "idx":
        return f"{_paren(a[1], True)}[{_out(a[2])}]"
    if k == "mem":
        return f"{_paren(a[1], True)}.{a[2]}"
    if k == "call":
        return f"{_out(a[1])}({','.join(_out(x) for x in a[2])})"
    raise MarkupError(f"这种写法不支持：{k}")


def _paren(a: tuple, strict: bool = False) -> str:
    need = a[0] in ("cond", "fn") or (strict and a[0] in ("bin", "neg", "not"))
    return f"({_out(a)})" if need else _out(a)


def _constant(a: tuple) -> tuple[bool, Any]:
    k = a[0]
    if k == "num":
        v = float(a[1])
        return True, int(v) if v.is_integer() else v
    if k == "str":
        return True, a[1]
    if k == "lit":
        return True, a[1]
    if k == "group":
        return _constant(a[1])
    if k == "neg":
        ok, v = _constant(a[1])
        return (True, -v) if ok and isinstance(v, int | float) else (False, None)
    if k == "arr":
        out = []
        for x in a[1]:
            ok, v = _constant(x)
            if not ok:
                return False, None
            out.append(v)
        return True, out
    if k == "obj":
        o = {}
        for n, x in a[1]:
            ok, v = _constant(x)
            if not ok:
                return False, None
            o[n] = v
        return True, o
    return False, None


# ---- the document ----

_SVG_CAMEL = {"strokeWidth": "stroke-width", "strokeDasharray": "stroke-dasharray", "strokeDashoffset": "stroke-dashoffset",
              "strokeLinecap": "stroke-linecap", "strokeLinejoin": "stroke-linejoin", "strokeOpacity": "stroke-opacity",
              "fillOpacity": "fill-opacity", "textAnchor": "text-anchor", "dominantBaseline": "dominant-baseline",
              "fontSize": "font-size", "fontWeight": "font-weight", "fontStyle": "font-style", "clipPath": "clip-path"}
_DROP = {"key", "xmlns", "role", "tabindex", "tabularNums", "tabular", "block", "variant", "maxWidth", "minWidth",
         "maxLines", "strokeMiterlimit", "stroke-miterlimit", "pointerEvents", "pointer-events", "shapeRendering",
         "shape-rendering", "vectorEffect", "vector-effect", "preserveAspectRatio", "lineHeight", "onChange", "aria-label",
         "aria-hidden", "fontFamily", "font-family", "paintOrder", "paint-order", "textLength", "cursor", "className", "class",
         "disabled", "lineThrough", "underline", "italic", "minHeight", "maxHeight"}
_ALIASES = {"label": "text", "col": "box", "vstack": "box", "hstack": "row", "segmented-control": "choice", "radio-group": "choice",
            "pressable": "box", "spacer": "box", "card": "box"}
_EXPR_FIELDS = {"cond", "data", "key", "when", "rows"}
_TEMPLATE_FIELDS = {"text", "label", "caption", "x_label", "y_label", "prompt", "background", "width", "height", "flex",
                    "radius", "padding", "color"}
_TEXT_KINDS = {"text", "title", "caption", "badge"}
_TITLE_SIZES = {"xs": "md", "sm": "md", "3xl": "2xl", "4xl": "2xl"}
_TEXT_SIZES = {"xl": "lg", "2xl": "lg", "base": "md"}
_TONES = {"blue": "accent", "info": "accent", "orange": "warning", "yellow": "warning", "green": "success", "red": "danger",
          "gray": "secondary", "muted": "secondary", "purple": "accent", "teal": "success", "subtle": "secondary",
          "text": "default", "discovery": "accent", "primary": "default"}
_TONED = {"text", "title", "badge", "icon", "button"}
_NO_TAGS = {"Cite", "cite"}
_SVG_ALIASES = {"weight": "font-weight", "anchor": "text-anchor"}
_WEIGHTS = {"semibold": "bold", "heavy": "bold", "600": "bold", "700": "bold", "500": "medium", "400": "normal", "regular": "normal"}


@dataclass
class _Local:
    """A `{@const}` inside `{#each}` or `{#if}`: holds for the rest of that block (its uses are replaced by its value)."""
    name: str
    source: str


@dataclass
class _Tag:
    name: str
    attrs: dict[str, tuple[str, str]]         # name -> ("str" | "expr" | "bare" | "flag", text)
    kids: list[Any]
    line: int


@dataclass
class _Each:
    source: str
    names: list[str]
    kids: list[Any]
    line: int


@dataclass
class _If:
    cond: str
    kids: list[Any]
    orelse: list[Any] | None
    line: int


def _close(s: str, i: int) -> int:
    """The index of the `}` closing the `{` at s[i]."""
    depth, n = 0, len(s)
    while i < n:
        c = s[i]
        if c in "\"'":
            j = i + 1
            while j < n and s[j] != c:
                j += 2 if s[j] == "\\" else 1
            i = j
        elif c == "`":
            j = i + 1
            while j < n and s[j] != "`":
                if s.startswith("${", j):
                    j = _close(s, j + 1)
                j += 1
            i = j
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise MarkupError(f"有一个 {{ 没有配对的 }}：{s[max(0, i - 200):i][:60]}")


class _Reader:
    def __init__(self, src: str) -> None:
        self.s = src
        self.i = 0
        self.decls: list[tuple[str, str, str]] = []     # ("state" | "const", name, source)
        self.scoped = 0                                 # how many {#each} / {#if} we are inside

    def line(self, at: int | None = None) -> int:
        return self.s.count("\n", 0, self.i if at is None else at) + 1

    def nodes(self, closers: tuple[str, ...] = ()) -> tuple[list[Any], str]:
        """Up to a closer (`</name>`, `{/each}`, `{:else...}`, `{/if}`) or the end; returns what closed it."""
        out: list[Any] = []
        s = self.s
        while self.i < len(s):
            if s.startswith("<!--", self.i):
                j = s.find("-->", self.i)
                self.i = len(s) if j < 0 else j + 3
                continue
            if s.startswith("</", self.i):
                j = s.find(">", self.i)
                name = s[self.i + 2:j].strip()
                if f"</{name}>" not in closers:
                    raise MarkupError(f"第 {self.line()} 行多了 </{name}>")
                self.i = j + 1
                return out, f"</{name}>"
            if s[self.i] == "<" and self.i + 1 < len(s) and (s[self.i + 1].isalpha()):
                out.append(self.tag())
                continue
            if s.startswith("{#each", self.i) or s.startswith("{#if", self.i) or s.startswith("{:", self.i) \
                    or s.startswith("{/", self.i) or s.startswith("{@", self.i):
                k = _close(s, self.i)
                inner = s[self.i + 1:k].strip()
                at = self.line()
                self.i = k + 1
                if inner.startswith("#each"):
                    out.append(self.each(inner[5:].strip(), at))
                elif inner.startswith("#if"):
                    out.append(self.iff(inner[3:].strip(), at))
                elif inner.startswith("@"):
                    local = self.decl(inner[1:].strip(), at)
                    if local is not None:
                        out.append(local)
                else:
                    key = "{:else}" if inner.startswith(":else") else "{" + inner + "}"
                    if key not in closers:
                        raise MarkupError(f"第 {at} 行多了 {{{inner}}}")
                    self.tail = inner
                    return out, key
                continue
            out.append(self.text())
        if closers:
            raise MarkupError(f"少了 {closers[0]}")
        return out, ""

    def text(self) -> str:
        s = self.s
        j = self.i
        buf = []
        while j < len(s):
            c = s[j]
            if c == "<" and j + 1 < len(s) and (s[j + 1].isalpha() or s[j + 1] in "/!"):
                break
            if c == "{":
                if j + 1 < len(s) and s[j + 1] in "#:/@":
                    break
                k = _close(s, j)
                buf.append(s[j:k + 1])
                j = k + 1
                continue
            buf.append(c)
            j += 1
        self.i = max(j, self.i + (0 if buf else 1))
        return "".join(buf)

    def tag(self) -> _Tag:
        s = self.s
        at = self.line()
        self.i += 1
        m = re.compile(r"[A-Za-z][\w-]*").match(s, self.i)
        assert m
        name = m.group(0)
        self.i = m.end()
        attrs: dict[str, tuple[str, str]] = {}
        while True:
            while self.i < len(s) and s[self.i].isspace():
                self.i += 1
            if self.i >= len(s):
                raise MarkupError(f"第 {at} 行的 <{name}> 没有结束")
            if s.startswith("/>", self.i):
                self.i += 2
                return _Tag(name, attrs, [], at)
            if s[self.i] == ">":
                self.i += 1
                break
            a = re.compile(r"[A-Za-z_][\w:-]*").match(s, self.i)
            if not a:
                raise MarkupError(f"第 {at} 行 <{name}> 的属性写错了：{s[self.i:self.i + 30]}")
            key = a.group(0)
            self.i = a.end()
            while self.i < len(s) and s[self.i].isspace():
                self.i += 1
            if self.i < len(s) and s[self.i] == "=":
                self.i += 1
                while self.i < len(s) and s[self.i].isspace():
                    self.i += 1
                c = s[self.i]
                if c in "\"'":
                    j = s.index(c, self.i + 1)
                    attrs[key] = ("str", s[self.i + 1:j])
                    self.i = j + 1
                elif c == "{":
                    k = _close(s, self.i)
                    attrs[key] = ("expr", s[self.i + 1:k])
                    self.i = k + 1
                else:
                    b = re.compile(r"(?:[^\s/>]|/(?!>))+").match(s, self.i)
                    if not b:
                        raise MarkupError(f"第 {at} 行 {key}= 后面没有值")
                    # x=a/b*2+1 without braces: an expression; a word, a number or blue-soft stays what it is
                    attrs[key] = ("expr" if re.search(r"[*/+()]", b.group(0)) else "bare", b.group(0))
                    self.i = b.end()
            else:
                attrs[key] = ("flag", "")
        kids, _ = self.nodes((f"</{name}>",))
        return _Tag(name, attrs, kids, at)

    def each(self, head: str, at: int) -> _Each:
        m = re.match(r"(.*)\s+as\s+([\w一-鿿]+)\s*(?:,\s*([\w一-鿿]+))?\s*$", head, re.DOTALL)
        if not m:
            raise MarkupError(f"第 {at} 行：写成 {{#each 列表 as x, i}}")
        self.scoped += 1
        kids, _ = self.nodes(("{/each}",))
        self.scoped -= 1
        return _Each(m.group(1).strip(), [m.group(2)] + ([m.group(3)] if m.group(3) else []), kids, at)

    def iff(self, cond: str, at: int) -> _If:
        self.scoped += 1
        kids, how = self.nodes(("{/if}", "{:else}"))
        orelse = None
        if how == "{:else}":
            rest = self.tail[5:].strip()
            if rest.startswith("if"):
                orelse = [self.iff(rest[2:].strip(), self.line())]
            else:
                orelse, _ = self.nodes(("{/if}",))
        self.scoped -= 1
        return _If(cond, kids, orelse, at)

    def decl(self, text: str, at: int) -> "_Local | None":
        m = re.match(r"body\s+(.*)$", text, re.DOTALL)
        if m:
            text = m.group(1).strip()
        for hook in re.finditer(r"(?:const|let|var)?\s*\[\s*([\w一-鿿]+)\s*,\s*[\w一-鿿]+\s*\]\s*=\s*(?:DIL\.)?useState\(", text):
            start = hook.end() - 1
            depth, j = 0, start
            while j < len(text):
                depth += text[j] in "([{"
                depth -= text[j] in ")]}"
                if depth == 0:
                    break
                j += 1
            self.decls.append(("state", hook.group(1), text[start + 1:j]))
        if "useState(" in text:
            return None
        m = re.match(r"(state|const|let|var)\s+([\w一-鿿]+)\s*=\s*(.*)$", text, re.DOTALL)
        if not m:
            raise MarkupError(f"第 {at} 行：写成 {{@state x = 1}} 或 {{@const y = x * 2}}")
        src = m.group(3).strip().rstrip(";")
        if m.group(1) != "state" and self.scoped:
            return _Local(m.group(2), src)
        self.decls.append(("state" if m.group(1) == "state" else "const", m.group(2), src))
        return None


class _Compiler:
    def __init__(self) -> None:
        self.sc = _Scope()
        self.states: set[str] = set()
        self.constants: dict[str, Any] = {}
        self.fresh = 0

    def expr(self, src: str) -> tuple:
        return _rewrite(_parse(src), self.sc)

    def canon(self, src: str) -> str:
        return _out(self.expr(src))

    def template_of(self, text: str) -> str:
        """A text with `{expr}` in it: each expression in the block's own language."""
        out, i = [], 0
        while True:
            j = text.find("{", i)
            if j < 0:
                out.append(text[i:])
                return "".join(out)
            k = _close(text, j)
            out.append(text[i:j])
            out.append("{" + self.braced(text[j + 1:k]) + "}")
            i = k + 1

    def braced(self, src: str) -> str:
        c = self.canon(src)
        if "{" in c:
            raise MarkupError(f"文字和属性里的表达式不能写对象 {{…}}，先放进 {{@const}}：{src[:60]}")
        return c

    def value(self, how: str, text: str) -> tuple[str, Any]:
        """("const", v) or ("dyn", canonical source)."""
        if how == "flag":
            return "const", True
        if how == "str":
            if "{" in text:
                return "tpl", self.template_of(text)
            return "const", _number(text)
        if how == "bare":
            return "const", {"true": True, "false": False}.get(text, _number(text))
        a = self.expr(text)
        ok, v = _constant(a)
        return ("const", v) if ok else ("dyn", _out(a))

    def handler(self, text: str, line: int) -> dict[str, Any]:
        src = text.strip()
        m = re.match(r"^\(\s*\w*\s*\)\s*=>\s*", src)
        if m:
            src = src[m.end():].strip()
        if src.startswith("{") and src.endswith("}"):
            src = src[1:-1].strip()
        out: dict[str, Any] = {}
        for part in _split_statements(src):
            part = part.strip()
            if not part:
                continue
            m = re.match(r"^set([A-Z一-鿿][\w一-鿿]*)\((.*)\)$", part, re.DOTALL)
            if m:
                name = m.group(1)
                low = name[0].lower() + name[1:]
                k = low if low in self.states else name
                val = m.group(2)
            else:
                m = re.match(r"^([\w一-鿿]+)\s*=(?!=)\s*(.*)$", part, re.DOTALL)
                if not m:
                    raise MarkupError(f"第 {line} 行：事件写成 onClick={{x = x + 1; y = 0}}，读不懂 {part[:40]}")
                k, val = m.group(1), m.group(2)
            a = self.expr(val)
            ok, v = _constant(a)
            out[k] = v if ok and isinstance(v, int | float | bool) else _out(a)
        if not out:
            raise MarkupError(f"第 {line} 行：事件是空的")
        return out

    # ---- nodes ----

    def children(self, kids: list[Any], svg: bool, where: str = "") -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for k in kids:
            if isinstance(k, _Local):
                a = _parse(k.source)
                if a[0] == "fn":
                    self.sc.macros[k.name] = a
                else:
                    self.sc.locals[k.name] = _rewrite(a, self.sc)
                continue
            if isinstance(k, _Tag) and k.name in _NO_TAGS:
                continue
            if isinstance(k, str):
                t = " ".join(k.split())
                if not t:
                    continue
                if svg:
                    raise MarkupError(f"<svg> 里的文字要放进 <text x y>：{t[:30]}")
                out.append(self.text_node(self.template_of(t)))
            elif isinstance(k, _Each):
                out.append(self.each(k, svg))
            elif isinstance(k, _If):
                out.append(self.iff(k, svg))
            else:
                out.append(self.mark(k) if svg else self.node(k))
        return out

    def text_node(self, text: str) -> dict[str, Any]:
        n: dict[str, Any] = {"kind": "text", "text": text}
        if "**" in text:
            n["rich"] = True
        return n

    def each(self, e: _Each, svg: bool) -> dict[str, Any]:
        saved = dict(self.sc.locals)
        for name in e.names:                     # the loop's own names win over an outer {@const}
            self.sc.locals.pop(name, None)
        n: dict[str, Any] = {"kind": "each", "in": self.canon(e.source), "as": e.names[0]}
        if len(e.names) > 1:
            n["index"] = e.names[1]
        else:
            self.fresh += 1
            n["index"] = f"i_{self.fresh}"
        n["children"] = self.children(e.kids, svg)
        self.sc.locals = saved
        return n

    def one(self, kids: list[Any], svg: bool) -> dict[str, Any]:
        xs = self.children(kids, svg)
        if len(xs) == 1:
            return xs[0]
        return {"el": "g", "children": xs} if svg else {"kind": "box", "gap": 2, "children": xs}

    def iff(self, x: _If, svg: bool) -> dict[str, Any]:
        cond = self.canon(x.cond)
        saved = dict(self.sc.locals)
        then = self.one(x.kids, svg)
        self.sc.locals = dict(saved)
        other = self.one(x.orelse, svg) if x.orelse is not None else None
        self.sc.locals = saved
        return {"kind": "if", "cond": cond, "then": then, "else": other}

    def flat_text(self, kids: list[Any]) -> str:
        parts = []
        for k in kids:
            if isinstance(k, str):
                parts.append(k)
            elif isinstance(k, _Tag) and k.name in ("bold", "b", "strong"):
                parts.append("**" + self.flat_text(k.kids) + "**")
            elif isinstance(k, _Tag) and k.name in ("code", "italic", "i", "em", "span", "tspan", "underline", "text"):
                parts.append(self.flat_text(k.kids))
            elif isinstance(k, _If):                       # a choice of words inside a sentence
                yes = self.flat_text(k.kids)
                no = self.flat_text(k.orelse) if k.orelse is not None else ""
                parts.append("{" + f"({k.cond})?{_words(yes)}:{_words(no)}" + "}")
            elif isinstance(k, _Local):
                raise MarkupError("文字里不能写 {@const}：放到外面")
            elif isinstance(k, _Tag) and k.name in _NO_TAGS:
                continue
            else:
                raise MarkupError("文字里只能放文字和 {表达式}")
        return " ".join("".join(parts).split())

    def mark(self, t: _Tag) -> dict[str, Any]:
        if t.name == "svg":
            raise MarkupError(f"第 {t.line} 行：svg 不能套 svg，用 <g transform=...>")
        if t.name not in SVG_ELS:
            raise MarkupError(f"第 {t.line} 行：<svg> 里没有 <{t.name}>：可用 {' '.join(list(SVG_ELS)[:14])}")
        n: dict[str, Any] = {"el": t.name}
        for key, (how, text) in t.attrs.items():
            if key in _DROP or key.startswith("data-"):
                continue
            if key in ("style", "href", "xlink:href"):
                raise MarkupError(f"第 {t.line} 行：<{t.name}> 不能写 {key}，直接写 fill、stroke 这些属性")
            low = key.lower()
            if low in ("onclick", "click", "ondrag", "drag", "onpointerdown"):
                n["drag" if "drag" in low else "click"] = self.handler(text, t.line)
                continue
            name = _SVG_CAMEL.get(key, key)
            if name == "size" and t.name in ("text", "node"):
                name = "font-size"
            name = _SVG_ALIASES.get(name, name)
            if name == "points" and how == "str" and text.lstrip().startswith("["):      # the list written in quotes
                how = "expr"
            k, v = self.value(how, text)
            n[name] = v if k != "dyn" else "{" + _no_brace(v, key) + "}"
        if t.name in ("text", "node"):
            body = self.flat_text(t.kids)
            if body:
                n["text"] = self.template_of(body)
            elif t.name == "text" and "text" not in n:
                raise MarkupError(f"第 {t.line} 行：<text> 是空的")
        elif t.kids:
            kids = self.children([k for k in t.kids if not (isinstance(k, str) and not k.strip())], True)
            if kids:
                n["children"] = kids
        return n

    def node(self, t: _Tag) -> dict[str, Any]:
        name = _ALIASES.get(t.name, t.name)
        if name == "svg":
            return self.svg(t)
        if name in SVG_ELS and name not in ("text", "image"):
            raise MarkupError(f"第 {t.line} 行：<{t.name}> 要放在 <svg viewBox=...> 里")
        if name in ("bold", "b", "strong"):
            return self.text_node("**" + self.template_of(self.flat_text(t.kids)) + "**")
        if name == "list-item":
            return self.text_node("· " + self.template_of(self.flat_text(t.kids)))
        if name == "list":
            name = "box"
        if name not in NODE_FIELDS:
            raise MarkupError(f"第 {t.line} 行：没有 <{t.name}>：可用 box row grid text title caption badge slider choice select "
                              "toggle button svg table chart math player quiz")
        req, opt = NODE_FIELDS[name]
        allowed = set(req) | set(opt)
        n: dict[str, Any] = {"kind": name}
        attrs = dict(t.attrs)
        if t.name == "box" and attrs.get("direction", ("", ""))[1] == "row":
            n["kind"] = name = "row"
            req, opt = NODE_FIELDS[name]
            allowed = set(req) | set(opt)
        attrs.pop("direction", None)
        if "value" in attrs and "bind" in allowed and name != "math":
            how, text = attrs.pop("value")
            attrs["bind"] = ("bare", text.strip())
        if name == "math" and "value" in attrs:
            attrs["tex"] = attrs.pop("value")
        if name == "math" and "block" in attrs:
            attrs["display"] = attrs.pop("block")
        if "textAlign" in attrs:
            attrs["align"] = attrs.pop("textAlign")
        if name in ("box", "row", "grid-item") and "size" in attrs:      # a square
            attrs["width"] = attrs["height"] = attrs.pop("size")
        if name == "icon":
            attrs.pop("size", None)
        if name == "divider" or (name in ("choice", "select", "button", "slider", "icon") and "size" in attrs):
            attrs.pop("color" if name == "divider" else "size", None)
        if name in _TONED and "color" in attrs and "color" not in allowed:
            attrs["tone"] = attrs.pop("color")
        if name == "choice" and "options" not in attrs and any(isinstance(k, _Tag) and k.name == "radio" for k in t.kids):
            opts = [{"label": self.flat_text(k.kids) or str(self.value(*k.attrs.get("label", ("str", "")))[1]), "value": self.value(*k.attrs["value"])[1]}
                    for k in t.kids if isinstance(k, _Tag) and k.name == "radio" and "value" in k.attrs]
            attrs["options"] = ("json", json.dumps(opts, ensure_ascii=False))
            t = _Tag(t.name, attrs, [], t.line)
        if "checked" in attrs and "bind" in allowed:
            attrs["bind"] = ("bare", attrs.pop("checked")[1].strip())
        for key, (how, text) in attrs.items():
            if key in _DROP or key.startswith("data-"):
                continue
            low = key.lower()
            if low in ("onclick", "click"):
                n["set" if name == "button" else "click"] = self.handler(text, t.line)
                continue
            if key == "bind":
                n["bind"] = text.strip()
                continue
            if key == "tex":
                n["tex"] = self.value(how, text)[1] if how == "expr" else text
                continue
            if how == "flag" and key in SVG_COLORS and "color" in allowed:      # <text muted>
                n["color"] = key
                continue
            if key not in allowed:
                raise MarkupError(f"第 {t.line} 行：<{t.name}> 没有属性 {key}：可用 {' '.join(sorted(allowed - {'children'}))}")
            if how == "json":
                n[key] = json.loads(text)
                continue
            n[key] = self.field(key, how, text, name, t.line)
        self.tidy(n, name)
        has_kids = "children" in req or "children" in opt
        if name == "button":
            body = self.flat_text(t.kids)
            if body:
                n["label"] = self.template_of(body)
        elif name in _TEXT_KINDS:
            body = self.flat_text(t.kids)
            if body:
                n["text"] = self.template_of(body)
            if "**" in n.get("text", "") and name == "text":
                n["rich"] = True
            elif name != "text" and "**" in n.get("text", ""):      # already emphasised as a whole
                n["text"] = n["text"].replace("**", "")
        elif has_kids:
            n["children"] = self.children(t.kids, False)
        elif any(not isinstance(k, str) or k.strip() for k in t.kids):
            raise MarkupError(f"第 {t.line} 行：<{t.name}> 不能有子内容")
        return n

    def field(self, key: str, how: str, text: str, kind: str, line: int) -> Any:
        k, v = self.value(how, text)
        if k == "const":
            return v
        if k == "tpl":
            return v
        if key in _EXPR_FIELDS:
            return v
        if key == "border":
            return True
        if key == "options":
            ok, got = _fold(self.expr(text), self.constants)
            if ok:
                return got
            raise MarkupError(f"第 {line} 行：<{kind}> 的选项要固定；选项随状态变，就用 {{#each}} 加 <button onClick=...>")
        if key in _TEMPLATE_FIELDS:
            return "{" + _no_brace(v, key) + "}"
        raise MarkupError(f"第 {line} 行：<{kind}> 的 {key} 要写固定值，不能是表达式")

    def tidy(self, n: dict[str, Any], kind: str) -> None:
        if isinstance(n.get("gap"), str) and (m := re.match(r"(\d+)px$", n["gap"])):
            n["gap"] = round(int(m.group(1)) / 4)
        if isinstance(n.get("gap"), int | float):
            n["gap"] = max(0, min(4, int(n["gap"])))
        if kind == "icon" and n.get("name") not in ICONS:
            n["name"] = "dot"
        if "wrap" in n and not isinstance(n["wrap"], bool):
            n["wrap"] = True
        if "border" in n and not isinstance(n["border"], bool):
            n["border"] = True
        if "size" in n and kind == "title" and n["size"] in _TITLE_SIZES:
            n["size"] = _TITLE_SIZES[n["size"]]
        if "size" in n and kind == "text" and n["size"] in _TEXT_SIZES:
            n["size"] = _TEXT_SIZES[n["size"]]
        if "tone" in n and kind in _TONED and n["tone"] in _TONES:
            n["tone"] = _TONES[n["tone"]]
        if not isinstance(n.get("border", False), bool):
            n["border"] = True
        if "weight" in n:
            n["weight"] = _WEIGHTS.get(str(n["weight"]), n["weight"])
        if kind in ("choice", "select") and isinstance(n.get("options"), list):
            n["options"] = [{"label": str(o), "value": o} if not isinstance(o, dict) else o for o in n["options"]]
        if "padding" in n and isinstance(n["padding"], dict):
            n["padding"] = next(iter(n["padding"].values()), 0)
        if n.get("justify") == "around":
            n["justify"] = "between"

    def svg(self, t: _Tag) -> dict[str, Any]:
        if "viewBox" not in t.attrs:
            raise MarkupError(f'第 {t.line} 行：<svg> 要写 viewBox="0 0 640 300"')
        n: dict[str, Any] = {"kind": "svg", "viewBox": t.attrs["viewBox"][1]}
        if "tween" in t.attrs:
            n["tween"] = _number(t.attrs["tween"][1])
        if "images" in t.attrs:
            n["images"] = self.value(*t.attrs["images"])[1]
        kids = [k for k in t.kids if not (isinstance(k, str) and not k.strip())]
        n["children"] = self.children(kids, True)
        return n


def _words(text: str) -> str:
    """Text with `{expr}` in it as one expression."""
    parts, i = [], 0
    while True:
        j = text.find("{", i)
        if j < 0:
            if text[i:] or not parts:
                parts.append(_out(("str", text[i:])))
            return "(" + "+".join(parts) + ")" if len(parts) > 1 else parts[0]
        k = _close(text, j)
        if text[i:j] or not parts:
            parts.append(_out(("str", text[i:j])))
        parts.append("(" + text[j + 1:k] + ")")
        i = k + 1


def _no_brace(c: str, key: str) -> str:
    if "{" in c:
        raise MarkupError(f"{key} 的表达式不能写对象 {{…}}，先放进 {{@const}}")
    return c


def _number(s: str) -> Any:
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d*\.\d+", s):
        return float(s)
    return s


def _split_statements(src: str) -> list[str]:
    out, buf, depth, quote = [], [], 0, ""
    for c in src:
        if quote:
            quote = "" if c == quote else quote
        elif c in "\"'":
            quote = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        if c == ";" and not depth and not quote:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(c)
    out.append("".join(buf))
    return out


def compile_markup(source: str, *, title: str, question: str, fallback: str) -> dict[str, Any]:
    """The block JSON of a markup document. `MarkupError` when it cannot be read; the block's own validators run afterwards."""
    reader = _Reader(source.strip())
    kids, _ = reader.nodes()
    c = _Compiler()
    state: dict[str, Any] = {}
    derived: dict[str, str] = {}
    for how, name, src in reader.decls:          # functions first: one may use one written later
        a = _parse(src)
        if a[0] == "fn":
            if how == "state":
                raise MarkupError(f"{name} 是函数：写成 {{@const {name} = x => ...}}")
            c.sc.macros[name] = a
    for how, name, src in reader.decls:
        a = _parse(src)
        if a[0] == "fn":
            continue
        a = _rewrite(a, c.sc)
        ok, v = _constant(a)
        if how == "state":
            if not ok:
                raise MarkupError(f"{{@state {name}}} 的初值要是固定值；算出来的写 {{@const}}")
            state[name] = v
            c.states.add(name)
        elif ok:
            state[name] = v
            c.constants[name] = v
        else:
            derived[name] = _out(a)
    tops = c.children([k for k in kids if not (isinstance(k, str) and not k.strip())], False)
    if not tops:
        raise MarkupError("块是空的")
    body = tops[0] if len(tops) == 1 else {"kind": "box", "gap": 2, "children": tops}
    return {"type": EVOUI_TYPE, "title": title, "question": question, "state": state, "derived": derived, "body": body,
            "fallback": fallback}


def size_of(block: dict[str, Any]) -> int:
    return len(json.dumps(block, ensure_ascii=False, separators=(",", ":")))
