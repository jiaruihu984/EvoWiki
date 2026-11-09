"""The Python EvoUI validator and the renderer's own (`EvoUI.validate` in evoui.js) must agree: one corpus
of valid and invalid blocks goes through both, and each must reach the expected verdict. The JS side runs
in headless Chromium (skipped, saying why, when Chromium is not installed)."""

import copy
import json
from collections.abc import Callable

import pytest

from evowiki.deep.evoui import JS_PATH, examples, validate
from evowiki.deep.evoui.validate import ExprError, parse

BASE = {"type": "evoui", "title": "角度和投影", "question": "转到哪里投影最长？",
        "state": {"deg": 30, "on": True, "mode": "a"},
        "derived": {"r": "deg2rad(deg)", "xs": "map(range(5), i => i * cos(r))", "top": "max(xs[4], 0)"},
        "body": {"kind": "box", "gap": 2, "children": [
            {"kind": "slider", "bind": "deg", "min": 0, "max": 180, "step": 1, "label": "角度 {deg}°"},
            {"kind": "toggle", "bind": "on", "label": "显示投影"},
            {"kind": "choice", "bind": "mode", "options": [{"label": "甲", "value": "a"},
                                                          {"label": "乙", "value": "b"}]},
            {"kind": "plot", "xmin": -1, "xmax": 5, "ymin": -1, "ymax": 1, "grid": True, "height": 200,
             "items": [{"shape": "arrow", "x1": 0, "y1": 0, "x2": "cos(r)", "y2": "sin(r)", "tone": "accent",
               "label": "v"},
              {"shape": "polyline", "points": "map(xs, x => [x, 0])", "dashed": True}],
             "legend": [{"label": "v", "tone": "accent"}]},
            {"kind": "if", "cond": "on && top > 1", "then": {"kind": "text", "text": "投影 {fmt(top, 2)}"},
             "else": None},
            {"kind": "chart", "chart": "line", "data": "map(range(5), i => {'i': i, 'y': xs[i]})",
             "x": "i", "series": [{"key": "y", "label": "投影"}], "caption": "第 {len(xs)} 个点"},
            {"kind": "quiz", "questions": [{"q": "垂直时投影是？", "options": ["0", "1"], "correct": 0,
                                            "why": "cos 90° = 0"}]}]},
        "fallback": "角度越接近 0，投影越长。",
        "probes": [{"set": {"deg": 90}}]}


def mutate(f: Callable[[dict], object]) -> dict:
    b = copy.deepcopy(BASE)
    f(b)
    return b


def drawing(*els: dict) -> dict:
    return {**BASE, "probes": [], "body": {"kind": "svg", "viewBox": "0 0 640 300", "children": list(els)}}


QZ = ["body", "children", 6]                 # BASE's quiz
Q0 = QZ + ["questions", 0]


def _q0(b: dict) -> dict:
    return b["body"]["children"][6]["questions"][0]


def kids(b: dict) -> list:
    return b["body"]["children"]


def put(path: list, value: object) -> Callable[[dict], None]:
    def f(b: dict) -> None:
        o = b
        for k in path[:-1]:
            o = o[k]
        o[path[-1]] = value
    return f


def drop(path: list) -> Callable[[dict], None]:
    def f(b: dict) -> None:
        o = b
        for k in path[:-1]:
            o = o[k]
        del o[path[-1]]
    return f


FC_CARDS = [{"front": "σ", "back": "标准差"}, {"front": "μ", "back": "均值"}]


def with_fc(**kw) -> dict:
    """BASE plus a flashcards node (fields merged in) as its last child."""
    return mutate(lambda b: kids(b).append({"kind": "flashcards", "cards": copy.deepcopy(FC_CARDS), **kw}))


def with_card(**kw) -> dict:
    """BASE plus a flashcards node whose first card gets these fields."""
    return with_fc(cards=[{**FC_CARDS[0], **kw}, FC_CARDS[1]])


T = {"kind": "text", "text": "第 {deg} 度"}


def add(*nodes: dict, quiz_id: str | None = None) -> dict:
    """BASE plus these nodes as its last children (BASE's quiz gets `quiz_id` when given)."""
    def f(b: dict) -> None:
        if quiz_id:
            kids(b)[6]["id"] = quiz_id
        kids(b).extend(copy.deepcopy(list(nodes)))
    return mutate(f)


def rv(**kw) -> dict:
    return {"kind": "reveal", "children": [T], **kw}


def tm(**kw) -> dict:
    return {"kind": "timer", "mode": "countdown", "seconds": 60, **kw}


def pl(**kw) -> dict:
    return {"kind": "player", "bind": "deg", "min": 0, "max": 180, "step": 10, **kw}


def expr(src: str) -> dict:
    return mutate(put(["derived", "z"], src))


def deep_box(n: int) -> dict:
    node: dict = {"kind": "text", "text": "x"}
    for _ in range(n):
        node = {"kind": "box", "children": [node]}
    return node


# ---- v1 nodes ----
V1 = {"type": "evoui", "title": "复利计算器", "question": "利率和年数怎样一起决定终值？",
      "state": {"p": 1000, "r": 5, "n": 10, "show": True, "mode": "y"},
      "derived": {"fv": "p * (1 + r / 100) ** n",
                  "rows": "map(range(3), i => [i + 1, fmt(p * (1 + r / 100) ** (i + 1), 0)])",
                  "pts": "map(range(n + 1), t => {'t': t, 'v': p * (1 + r / 100) ** t, 'w': p + p * r / 100 * t})"},
      "body": {"kind": "box", "gap": 3, "children": [
          {"kind": "grid", "columns": 3, "children": [
              {"kind": "input", "bind": "p", "min": 100, "max": 100000, "step": 100, "label": "本金"},
              {"kind": "input", "bind": "r", "min": 0, "max": 20, "step": 0.5, "label": "年利率 %"},
              {"kind": "select", "bind": "n", "label": "年数", "options": [
                  {"label": "5 年", "value": 5}, {"label": "10 年", "value": 10},
                  {"label": "30 年", "value": 30}]}]},
          {"kind": "grid", "columns": 2, "children": [
              {"kind": "grid-item", "span": 2, "tone": "surface", "children": [
                  {"kind": "caption", "text": "终值"},
                  {"kind": "title", "size": "2xl", "text": "{fmt(fv, 0)}"}]}]},
          {"kind": "math", "tex": "FV = P(1+r)^n", "display": True},
          {"kind": "row", "children": [{"kind": "icon", "name": "arrow-right", "tone": "accent"},
                                       {"kind": "text", "text": "多出 {fmt(fv - p, 0)}"}]},
          {"kind": "divider"},
          {"kind": "checkbox", "bind": "show", "label": "显示明细"},
          {"kind": "button", "label": "试试 30 年", "set": {"n": 30, "r": "r + 1"}, "tone": "accent"},
          {"kind": "choice", "bind": "mode", "options": [{"label": "单", "value": "y"},
                                                         {"label": "双", "value": "z"}]},
          {"kind": "if", "cond": "show", "then": {"kind": "table", "columns": ["年", "余额"],
                                                    "rows": "rows", "caption": "前 {len(rows)} 年"},
           "else": None},
          {"kind": "table", "columns": ["项", "值"], "rows": [["本金", "{p}"], ["利率", "{r}%"]]},
          {"kind": "chart", "chart": "scatter", "data": "pts", "x": "t",
           "series": [{"key": "v", "label": "复利"}, {"key": "w", "label": "单利"}],
           "x_label": "年", "y_label": "余额", "y_min": 0},
          {"kind": "chart", "chart": "pie", "data": "[{'k': '本金', 'v': p}, {'k': '利息', 'v': fv - p}]",
           "x": "k", "series": [{"key": "v", "label": "构成"}]},
          {"kind": "plot", "xmin": 0, "xmax": 10, "ymin": 0, "ymax": 6, "items": [
              {"shape": "rect", "x": 1, "y": 1, "w": 3, "h": 2, "fill": True, "label": "A",
               "tone": "accent"},
              {"shape": "circle", "cx": 7, "cy": 3, "r": 1.5, "dashed": True, "label": "B"},
              {"shape": "text", "x": 5, "y": 5, "text": "n = {n}", "anchor": "middle", "size": "md"}]},
          {"kind": "quiz", "cases": [{"id": "rate", "label": "利率", "advice": "回去看利率那段"},
                                     {"id": "time", "label": "时间"}],
           "questions": [
               {"q": "利率翻倍，利息翻倍吗？", "options": ["是", "不止"], "correct": 1, "why": "复利",
                "case": "rate", "cell": "c1.interpret"},
               {"q": "年数越长差距？", "options": ["越大", "不变"], "correct": 0, "why": "指数",
                "case": "time"}]}]},
      "fallback": "复利终值 = 本金 ×(1+利率)^年数。"}


def v1(f: Callable[[dict], object]) -> dict:
    b = copy.deepcopy(V1)
    f(b)
    return b


def v1k(i: int) -> list:
    return ["body", "children", i]


def _no_cases(b: dict) -> None:
    q = b["body"]["children"][13]
    q.pop("cases")
    for x in q["questions"]:
        x.pop("case")


V1_INVALID: dict[str, object] = {
    "grid columns 5": v1(put(v1k(0) + ["columns"], 5)),
    "grid-item span 0": v1(put(v1k(1) + ["children", 0, "span"], 0)),
    "grid-item accent tone": v1(put(v1k(1) + ["children", 0, "tone"], "accent")),
    "title size sm": v1(put(v1k(1) + ["children", 0, "children", 1, "size"], "sm")),
    "text size 2xl": v1(put(v1k(3) + ["children", 1, "size"], "2xl")),
    "caption extra field": v1(put(v1k(1) + ["children", 0, "children", 0, "tone"], "accent")),
    "math empty": v1(put(v1k(2) + ["tex"], " ")),
    "math display text": v1(put(v1k(2) + ["display"], "yes")),
    "icon unknown": v1(put(v1k(3) + ["children", 0, "name"], "rocket")),
    "divider field": v1(put(v1k(4) + ["text"], "x")),
    "checkbox on a number": v1(put(v1k(5) + ["bind"], "p")),
    "input on a text state": v1(put(v1k(0) + ["children", 0, "bind"], "mode")),
    "input min above max": v1(put(v1k(0) + ["children", 0, "min"], 10 ** 6)),
    "select empty": v1(put(v1k(0) + ["children", 2, "options"], [])),
    "button unknown state": v1(put(v1k(6) + ["set"], {"nope": 1})),
    "button empty set": v1(put(v1k(6) + ["set"], {})),
    "button bad expression": v1(put(v1k(6) + ["set", "r"], "r +")),
    "table row too short": v1(put(v1k(9) + ["rows", 0], ["本金"])),
    "table rows not a list": v1(put(v1k(9) + ["rows"], 3)),
    "table rows bad expression": v1(put(v1k(8) + ["then", "rows"], "nope")),
    "pie two series": v1(put(v1k(11) + ["series"], [{"key": "v", "label": "a"},
                                                     {"key": "v", "label": "b"}])),
    "chart y_min text": v1(put(v1k(10) + ["y_min"], "0")),
    "chart x_label unknown name": v1(put(v1k(10) + ["x_label"], "{nope}")),
    "rect missing h": v1(drop(v1k(12) + ["items", 0, "h"])),
    "circle fill text": v1(put(v1k(12) + ["items", 1, "fill"], "yes")),
    "text anchor bad": v1(put(v1k(12) + ["items", 2, "anchor"], "left")),
    "text size lg": v1(put(v1k(12) + ["items", 2, "size"], "lg")),
    "quiz case unknown": v1(put(v1k(13) + ["questions", 1, "case"], "other")),
    "quiz case missing": v1(drop(v1k(13) + ["questions", 1, "case"])),
    "quiz case without cases": v1(drop(v1k(13) + ["cases"])),
    "quiz cases empty": v1(put(v1k(13) + ["cases"], [])),
    "quiz case id upper": v1(put(v1k(13) + ["cases", 0, "id"], "Rate")),
    "quiz case id twice": v1(put(v1k(13) + ["cases", 1, "id"], "rate")),
    "quiz case extra field": v1(put(v1k(13) + ["cases", 0, "score"], 1)),
    "quiz cell bad": v1(put(v1k(13) + ["questions", 0, "cell"], "not a cell")),
}

VALID: dict[str, object] = {
    "drawing": drawing(
        {"el": "rect", "x": "{deg}", "width": 10, "height": "{deg*2}", "fill": "{on?'blue':'#336699'}", "click": {"deg": "deg+1", "on": False}},
        {"el": "path", "d": "M0 0 L{deg} 10", "stroke": "orange-soft", "stroke-width": 2, "drag": {"deg": "clamp(mx,0,10)"}},
        {"el": "polyline", "points": "{map(range(3),i=>[i,i*i])}"},
        {"kind": "each", "in": "range(1000)", "as": "a", "index": "b", "children": [
            {"el": "node", "x": "{a}", "y": "{b}", "width": 9, "height": 9, "text": "n{a}"},
            {"kind": "if", "cond": "a>b", "then": {"el": "g", "children": [{"el": "arrow", "x1": 0, "y1": 0, "x2": "{a}", "y2": 1, "bend": 0.2, "both": True, "label": "x"}]}, "else": None}]},
        {"el": "plot", "x": [0, 10], "y": "{[0,deg+1]}", "grid": True, "xlabel": "x", "children": [
            {"el": "circle", "cx": 1, "cy": "{deg}", "r": 3, "children": [{"el": "animate", "attributeName": "r", "values": "3;5;3", "dur": "1s", "repeatCount": "indefinite"}]}]},
        {"el": "defs", "children": [{"el": "clipPath", "id": "c1", "children": [{"el": "rect", "width": 10, "height": 10}]}]},
        {"el": "g", "clip-path": "url(#c1)", "transform": "rotate({deg})", "children": []}),
    "styled boxes": mutate(put(["body"], {"kind": "row", "height": 10, "radius": "sm", "clip": True, "children": [
        {"kind": "box", "background": "red", "width": "{deg+'%'}", "children": [], "click": {"deg": 1}},
        {"kind": "each", "in": "[1,2]", "as": "a", "children": [{"kind": "box", "flex": "{a}", "padding": 1, "background": "{a>1?'blue':'gray'}", "children": [
            {"kind": "text", "text": "**{a}**", "rich": True, "size": "xs", "weight": "medium", "color": "muted", "align": "center"},
            {"kind": "button", "label": "b", "set": {"deg": "a"}}]}]}]})),
    "two-argument arrows and the new functions": mutate(put(["derived", "top"], "sum(filter(map([3,1],(x,j)=>x+j),x=>x>1))+fold([1,2],0,(s,x)=>s+x)+clamp(2,0,1)+rand(1)+len(concat(slice(swap(put(sort([2,1]),0,5),0,1),0,1),repeat(1,2)))+indexOf(reverse([1]),1)")),
    "v1": V1,
    "v1 grid without columns": v1(drop(v1k(0) + ["columns"])),
    "v1 quiz without cases": v1(_no_cases),
    "v1 four series": v1(put(v1k(10) + ["series"], [{"key": "v", "label": str(i)} for i in range(4)])),
    **{f"example {i}": b for i, b in enumerate(examples())},
    "base": BASE,
    "no probes": mutate(drop(["probes"])),
    "html in text is just text": mutate(put(["body", "children", 4, "then", "text"],
                                            "<script>alert(1)</script>{top}")),
    "chinese names": mutate(put(["state", "方差"], 1)),
    "arrow shadows a builtin": expr("map(xs, sin => sin + 1)"),
    "nested arrow": expr("map(xs, a => map(xs, b => a * b))"),
    "trailing comma array": expr("[1, 2, 3,]"),
    "object trailing comma": expr("{a: 1, 'b': 2,}"),
    "power right assoc": expr("2 ** 3 ** 2"),
    "ternary chain": expr("deg < 10 ? 'a' : deg < 20 ? 'b' : 'c'"),
    "member chain": expr("xs.length + {k: {j: 1}}.k.j"),
    "index by expression": expr("xs[floor(deg / 90)]"),
    "strings with quotes": expr("'it\\'s' + \"x\""),
    "exponent number": expr("1.5e3 + .5"),
    "unary": expr("-!+deg"),
    "correct as 1.0": mutate(put(["body", "children", 6, "questions", 0, "correct"], 1.0)),
    "else node": mutate(put(["body", "children", 4, "else"], {"kind": "text", "text": "小"})),
    "row node": mutate(put(["body", "children", 1], {"kind": "row", "justify": "between",
                                                     "wrap": True, "children": []})),
    "box surface": mutate(put(["body", "tone"], "surface")),
    "deep but allowed": mutate(put(["body"], deep_box(11))),
    "number coordinate": mutate(put(["body", "children", 3, "items", 0, "x2"], 1)),
    "quiz with id": mutate(put(["body", "children", 6, "id"], "check_1-a")),
    "quiz id 32 chars": mutate(put(["body", "children", 6, "id"], "q" * 32)),
    "two quizzes, distinct ids": mutate(lambda b: (kids(b)[6].update(id="a"), kids(b).append(
        {**copy.deepcopy(kids(b)[6]), "id": "b"}))),
    "paged quiz with a title": mutate(lambda b: kids(b)[6].update(layout="paged", title="检查一下")),
    "list layout spelled out": mutate(put(QZ + ["layout"], "list")),
    "single type spelled out": mutate(put(Q0 + ["type"], "single")),
    "multi question": mutate(lambda b: _q0(b).update(type="multi", options=["0", "1", "-1"], correct=[2, 0])),
    "multi with one right option": mutate(lambda b: _q0(b).update(type="multi", correct=[1])),
    "tf question": mutate(lambda b: _q0(b).update(type="tf", options=["对", "错"], correct=1)),
    "feedback and hint": mutate(lambda b: _q0(b).update(feedback=["对，cos 90° = 0", "那是 cos 0°"],
                                                        hint="想想单位圆上 90° 的点")),
    "multi in a list quiz with feedback": mutate(lambda b: _q0(b).update(type="multi", correct=[0, 1],
                                                                        feedback=["a", "b"])),
    "15 questions": mutate(put(QZ + ["questions"], [copy.deepcopy(BASE["body"]["children"][6]["questions"][0])
                                                    for _ in range(15)])),
    "flashcards": with_fc(),
    "flashcards with every field": with_fc(id="cards-1", title="记一记", shuffle=False,
                                           cards=[{"front": "σ", "back": "标准差\n- 方差的平方根", "hint": "希腊字母",
                                                   "cell": "c1.interpret"}, {"front": "μ", "back": "均值"}]),
    "flashcards 40 cards": with_fc(cards=[{"front": f"f{i}", "back": f"b{i}"} for i in range(40)]),
    "flashcards shuffle true": with_fc(shuffle=True),
    "flashcards with an id": with_fc(id="q-x"),
    # ---- Task 5a: reveal, animate, transition, celebrate, timer, player ----
    "reveal (click by default)": add(rv()),
    "reveal click spelled out, label template": add(rv(mode="click", label="看答案（{deg}°）")),
    "reveal steps with an id": add(rv(mode="steps", id="why", children=[T, T, {"kind": "math", "tex": "x^2"}])),
    "reveal 12 steps": add(rv(mode="steps", children=[T] * 12)),
    "reveal after a quiz": add(rv(mode="after", after="chk"), quiz_id="chk"),
    "reveal after flashcards that come later": add(rv(mode="after", after="fc", label="记完再看"),
                                                    {"kind": "flashcards", "id": "fc", "cards": FC_CARDS}),
    "reveal after a quiz inside an if branch": add(rv(mode="after", after="deep"), {
        "kind": "if", "cond": "on", "then": {"kind": "quiz", "id": "deep", "questions": BASE["body"]["children"][6]["questions"]},
        "else": None}),
    "reveal holding a quiz, timer and player": add(rv(mode="steps", children=[
        copy.deepcopy(BASE["body"]["children"][6]), tm(), pl()])),
    "animate (rise by default)": add({"kind": "animate", "children": [T]}),
    "animate fade": add({"kind": "animate", "effect": "fade", "children": [T, T]}),
    "animate rise spelled out": add({"kind": "animate", "effect": "rise", "children": []}),
    "transition": add({"kind": "transition", "key": "mode", "children": [T]}),
    "transition on an expression": add({"kind": "transition", "key": "floor(deg / 30)", "children": [T, T]}),
    "celebrate": add({"kind": "celebrate", "when": "deg >= 90 && on"}),
    "timer countdown with everything": add(tm(id="clock", bind="deg", autostart=True, label="限时 {deg} 秒")),
    "timer stopwatch": add({"kind": "timer", "mode": "stopwatch"}),
    "timer stopwatch bound": add({"kind": "timer", "mode": "stopwatch", "bind": "deg", "autostart": False}),
    "timer 1 and 3600 seconds": add(tm(seconds=1), tm(seconds=3600)),
    "player": add(pl()),
    "player with everything": add(pl(id="run", interval_ms=100, loop=True, label="第 {deg} 步")),
    "player interval 5000, fractional step": add(pl(interval_ms=5000, min=0, max=1, step=0.05)),
    "player and slider on one state": add(pl(), {"kind": "slider", "bind": "deg", "min": 0, "max": 180, "step": 1}),
}

INVALID: dict[str, object] = {
    "flashcards one card": with_fc(cards=[FC_CARDS[0]]),
    # ---- Task 5a ----
    "reveal no steps": add(rv(children=[])),
    "reveal 13 steps": add(rv(mode="steps", children=[T] * 13)),
    "reveal without children": add({"kind": "reveal", "mode": "click"}),
    "reveal children not a list": add(rv(children=T)),
    "reveal mode unknown": add(rv(mode="hover")),
    "reveal after without mode after": add(rv(after="chk"), quiz_id="chk"),
    "reveal after on steps": add(rv(mode="steps", after="chk"), quiz_id="chk"),
    "reveal mode after without after": add(rv(mode="after")),
    "reveal after an unknown id": add(rv(mode="after", after="nope"), quiz_id="chk"),
    "reveal after a number": add(rv(mode="after", after=3)),
    "reveal after a timer": add(rv(mode="after", after="clock"), tm(id="clock")),
    "reveal after a player": add(rv(mode="after", after="run"), pl(id="run")),
    "reveal after itself": add(rv(mode="after", after="me", id="me")),
    "reveal after another reveal": add(rv(mode="after", after="other"), rv(id="other")),
    "reveal after a quiz inside it": add(rv(mode="after", after="inner", children=[
        {**copy.deepcopy(BASE["body"]["children"][6]), "id": "inner"}])),
    "reveal after a quiz deep inside it": add(rv(mode="after", after="inner", children=[{"kind": "box", "children": [
        {"kind": "if", "cond": "on", "then": {**copy.deepcopy(BASE["body"]["children"][6]), "id": "inner"}, "else": None}]}])),
    "reveal label not text": add(rv(label=5)),
    "reveal label bad template": add(rv(label="{nope}")),
    "reveal unknown field": add(rv(open=True)),
    "reveal id upper case": add(rv(id="Why")),
    "animate effect unknown": add({"kind": "animate", "effect": "spin", "children": [T]}),
    "animate with an id": add({"kind": "animate", "id": "a", "children": [T]}),
    "animate without children": add({"kind": "animate", "effect": "fade"}),
    "transition without key": add({"kind": "transition", "children": [T]}),
    "transition without children": add({"kind": "transition", "key": "deg"}),
    "transition key unknown name": add({"kind": "transition", "key": "nope", "children": [T]}),
    "transition key bad expression": add({"kind": "transition", "key": "deg +", "children": [T]}),
    "transition with an id": add({"kind": "transition", "id": "t", "key": "deg", "children": [T]}),
    "celebrate without when": add({"kind": "celebrate"}),
    "celebrate when unknown name": add({"kind": "celebrate", "when": "done"}),
    "celebrate when not an expression": add({"kind": "celebrate", "when": ["deg"]}),
    "celebrate with children": add({"kind": "celebrate", "when": "on", "children": [T]}),
    "timer without mode": add({"kind": "timer", "seconds": 60}),
    "timer mode unknown": add(tm(mode="clock")),
    "countdown without seconds": add({"kind": "timer", "mode": "countdown"}),
    "countdown 0 seconds": add(tm(seconds=0)),
    "countdown 3601 seconds": add(tm(seconds=3601)),
    "countdown fractional seconds": add(tm(seconds=1.5)),
    "countdown seconds as text": add(tm(seconds="60")),
    "stopwatch with seconds": add({"kind": "timer", "mode": "stopwatch", "seconds": 60}),
    "timer bind unknown state": add(tm(bind="nope")),
    "timer bind a true/false state": add(tm(bind="on")),
    "timer bind a text state": add(tm(bind="mode")),
    "timer autostart text": add(tm(autostart="yes")),
    "timer label not text": add(tm(label=1)),
    "timer id bad": add(tm(id="t 1")),
    "timer id shared with the quiz": add(tm(id="chk"), quiz_id="chk"),
    "player without bind": add({k: v for k, v in pl().items() if k != "bind"}),
    "player without step": add({k: v for k, v in pl().items() if k != "step"}),
    "player bind unknown state": add(pl(bind="nope")),
    "player bind a true/false state": add(pl(bind="on")),
    "player min not below max": add(pl(min=180, max=180)),
    "player step zero": add(pl(step=0)),
    "player max text": add(pl(max="180")),
    "player interval 99": add(pl(interval_ms=99)),
    "player interval 5001": add(pl(interval_ms=5001)),
    "player interval fractional": add(pl(interval_ms=150.5)),
    "player interval bool": add(pl(interval_ms=True)),
    "player loop text": add(pl(loop="yes")),
    "player unknown field": add(pl(speed=2)),
    "player label bad template": add(pl(label="{nope}")),
    "player ids used twice": add(pl(id="run"), pl(id="run")),
    "flashcards cards not a list": with_fc(cards={"front": "a", "back": "b"}),
    "flashcards without cards": mutate(lambda b: kids(b).append({"kind": "flashcards"})),
    "flashcards card not an object": with_fc(cards=["σ", FC_CARDS[1]]),
    "card without back": with_fc(cards=[{"front": "σ"}, FC_CARDS[1]]),
    "card without front": with_fc(cards=[{"back": "σ"}, FC_CARDS[1]]),
    "card extra field": with_card(image="x.png"),
    "card front empty": with_card(front=" "),
    "card back a number": with_card(back=1),
    "card front too long": with_card(front="长" * 601),
    "card hint empty": with_card(hint=""),
    "card hint not text": with_card(hint=["a"]),
    "card cell malformed": with_card(cell="interpret"),
    "card cell trailing newline": with_card(cell="c1.interpret\n"),
    "card cell a number": with_card(cell=1),
    "flashcards shuffle text": with_fc(shuffle="yes"),
    "flashcards title empty": with_fc(title=""),
    "flashcards title too long": with_fc(title="长" * 601),
    "flashcards unknown field": with_fc(layout="paged"),
    "flashcards id upper case": with_fc(id="Cards"),
    "flashcards id shared with the quiz": mutate(lambda b: (kids(b)[6].update(id="dup"), kids(b).append(
        {"kind": "flashcards", "id": "dup", "cards": copy.deepcopy(FC_CARDS)}))),
    "quiz layout unknown": mutate(put(QZ + ["layout"], "grid")),
    "quiz layout a number": mutate(put(QZ + ["layout"], 1)),
    "quiz title empty": mutate(put(QZ + ["title"], " ")),
    "quiz title not text": mutate(put(QZ + ["title"], ["a"])),
    "quiz title too long": mutate(put(QZ + ["title"], "长" * 601)),
    "question type unknown": mutate(put(Q0 + ["type"], "essay")),
    "tf with three options": mutate(lambda b: _q0(b).update(type="tf", options=["对", "错", "不知道"])),
    "tf with a list correct": mutate(lambda b: _q0(b).update(type="tf", correct=[0])),
    "multi correct an int": mutate(put(Q0 + ["type"], "multi")),
    "multi correct empty": mutate(lambda b: _q0(b).update(type="multi", correct=[])),
    "multi correct repeated": mutate(lambda b: _q0(b).update(type="multi", correct=[0, 0])),
    "multi correct out of range": mutate(lambda b: _q0(b).update(type="multi", correct=[0, 2])),
    "multi correct a bool": mutate(lambda b: _q0(b).update(type="multi", correct=[True])),
    "multi correct a fraction": mutate(lambda b: _q0(b).update(type="multi", correct=[0.5])),
    "single correct a list": mutate(put(Q0 + ["correct"], [0])),
    "feedback one short": mutate(put(Q0 + ["feedback"], ["只有一句"])),
    "feedback not a list": mutate(put(Q0 + ["feedback"], "一句")),
    "feedback empty text": mutate(put(Q0 + ["feedback"], ["a", ""])),
    "feedback a number": mutate(put(Q0 + ["feedback"], ["a", 1])),
    "feedback too long": mutate(put(Q0 + ["feedback"], ["a", "长" * 601])),
    "hint empty": mutate(put(Q0 + ["hint"], "")),
    "hint not text": mutate(put(Q0 + ["hint"], 1)),
    "layout on a question": mutate(put(Q0 + ["layout"], "paged")),
    "not an object": [1, 2],
    "wrong type": mutate(put(["type"], "html")),
    "extra top field": mutate(put(["html"], "<b>x</b>")),
    "missing fallback": mutate(drop(["fallback"])),
    "blank fallback": mutate(put(["fallback"], "   ")),
    "empty title": mutate(put(["title"], "")),
    "state not object": mutate(put(["state"], [1])),
    "unknown kind": mutate(put(["body", "children", 0, "kind"], "html")),
    "script kind": mutate(put(["body"], {"kind": "script", "src": "x.js"})),
    "unknown node field": mutate(put(["body", "children", 0, "onclick"], "alert(1)")),
    "bind not a state": mutate(put(["body", "children", 0, "bind"], "nope")),
    "toggle on a number": mutate(put(["body", "children", 1, "bind"], "deg")),
    "bad tone": mutate(put(["body", "children", 3, "items", 0, "tone"], "red")),
    "box tone accent": mutate(put(["body", "tone"], "accent")),
    "colour value tone": mutate(put(["body", "children", 4, "then", "tone"], "#ff0000")),
    "unknown name": expr("window.alert(1)"),
    "constructor": expr("constructor"),
    "assignment": expr("deg = 1"),
    "unterminated string": expr("'abc"),
    "bad character": expr("deg @ 2"),
    "dangling ternary": expr("deg ? 1"),
    "double dot": expr("xs..length"),
    "trailing input": expr("1 2"),
    "parenthesised arrow": expr("map(xs, (x) => x)"),
    "number key": expr("{1: 2}"),
    "expression too long": expr("+".join(["deg"] * 200)),
    "derived uses a later one": mutate(put(["derived"], {"a": "b + 1", "b": "deg"})),
    "derived uses itself": mutate(put(["derived", "self"], "self + 1")),
    "state named like a builtin": mutate(put(["state", "sin"], 1)),
    "state name with dash": mutate(put(["state", "a-b"], 1)),
    "derived repeats a state": mutate(put(["derived", "deg"], "1")),
    "bad template": mutate(put(["body", "children", 4, "then", "text"], "值 {top +}")),
    "template unknown name": mutate(put(["body", "children", 0, "label"], "{nope}")),
    "template never closed": mutate(put(["body", "children", 4, "then", "text"], "值 {top")),
    "slider min above max": mutate(put(["body", "children", 0, "min"], 200)),
    "slider step zero": mutate(put(["body", "children", 0, "step"], 0)),
    "slider min text": mutate(put(["body", "children", 0, "min"], "0")),
    "choice option without value": mutate(put(["body", "children", 2, "options", 0], {"label": "x"})),
    "choice empty": mutate(put(["body", "children", 2, "options"], [])),
    "plot xmin above xmax": mutate(put(["body", "children", 3, "xmin"], 9)),
    "plot height": mutate(put(["body", "children", 3, "height"], 100)),
    "unknown shape": mutate(put(["body", "children", 3, "items", 0, "shape"], "image")),
    "shape extra field": mutate(put(["body", "children", 3, "items", 0, "href"], "x")),
    "shape missing field": mutate(drop(["body", "children", 3, "items", 0, "y2"])),
    "legend bad tone": mutate(put(["body", "children", 3, "legend", 0, "tone"], "pink")),
    "chart kind": mutate(put(["body", "children", 5, "chart"], "radar")),
    **{f"v1: {k}": v for k, v in V1_INVALID.items()},
    "quiz id upper case": mutate(put(["body", "children", 6, "id"], "Q1")),
    "quiz id with a hash": mutate(put(["body", "children", 6, "id"], "quiz#0")),
    "quiz id empty": mutate(put(["body", "children", 6, "id"], "")),
    "quiz id 33 chars": mutate(put(["body", "children", 6, "id"], "q" * 33)),
    "quiz id trailing newline": mutate(put(["body", "children", 6, "id"], "q1\n")),
    "quiz id a number": mutate(put(["body", "children", 6, "id"], 1)),
    "quiz id used twice": mutate(lambda b: (kids(b)[6].update(id="a"), kids(b).append(
        {**copy.deepcopy(kids(b)[6]), "id": "a"}))),
    "quiz id used twice across if branches": mutate(lambda b: (
        kids(b)[6].update(id="a"),
        kids(b)[4].update(**{"else": {**copy.deepcopy(kids(b)[6]), "id": "a"}}))),
    "id on a non-stateful node": mutate(put(["body", "children", 0, "id"], "s1")),
    "quiz correct out of range": mutate(put(["body", "children", 6, "questions", 0, "correct"], 2)),
    "quiz correct negative": mutate(put(["body", "children", 6, "questions", 0, "correct"], -1)),
    "quiz correct fraction": mutate(put(["body", "children", 6, "questions", 0, "correct"], 0.5)),
    "quiz one option": mutate(put(["body", "children", 6, "questions", 0, "options"], ["只有一个"])),
    "quiz extra field": mutate(put(["body", "children", 6, "questions", 0, "answer"], 0)),
    "gap five": mutate(put(["body", "gap"], 5)),
    "gap fraction": mutate(put(["body", "gap"], 1.5)),
    "border text": mutate(put(["body", "children", 1], {"kind": "box", "border": "yes",
                                                        "children": []})),
    "probe unknown state": mutate(put(["probes"], [{"set": {"nope": 1}}])),
    "probe empty": mutate(put(["probes"], [{"set": {}}])),
    "probe extra field": mutate(put(["probes"], [{"set": {"deg": 1}, "expect": "x"}])),
    "svg unknown element": drawing({"el": "foreignObject"}),
    "svg unknown attribute": drawing({"el": "rect", "width": 1, "height": 1, "onload": "x"}),
    "svg href": drawing({"el": "image", "src": "a", "width": 1, "height": 1, "href": "http://x"}),
    "svg image not declared": drawing({"el": "image", "src": "a", "width": 1, "height": 1}),
    "svg a node inside": drawing({"kind": "text", "text": "x"}),
    "svg rect without size": drawing({"el": "rect", "x": 1}),
    "svg bad colour": drawing({"el": "rect", "width": 1, "height": 1, "fill": "url(http://x)"}),
    "svg bad path": drawing({"el": "path", "d": "M0 0 url(x)"}),
    "svg bad clip": drawing({"el": "g", "clip-path": "url(http://x)"}),
    "svg unknown name": drawing({"el": "rect", "width": "{nope}", "height": 1}),
    "svg click on a non-state": drawing({"el": "rect", "width": 1, "height": 1, "click": {"nope": 1}}),
    "svg click bad value": drawing({"el": "rect", "width": 1, "height": 1, "click": {"deg": [1]}}),
    "svg mx outside a drag": drawing({"el": "rect", "width": 1, "height": 1, "click": {"deg": "mx"}}),
    "svg animate href": drawing({"el": "rect", "width": 1, "height": 1, "children": [{"el": "animate", "attributeName": "href", "dur": "1s"}]}),
    "svg animate with children": drawing({"el": "animate", "attributeName": "r", "dur": "1s", "children": [{"el": "rect", "width": 1, "height": 1}]}),
    "svg bad viewBox": mutate(put(["body"], {"kind": "svg", "viewBox": "0 0 a b", "children": []})),
    "each name taken": mutate(put(["body"], {"kind": "each", "in": "range(2)", "as": "deg", "children": []})),
    "each unknown list": mutate(put(["body"], {"kind": "each", "in": "nope", "children": []})),
    "each with a quiz": mutate(put(["body"], {"kind": "each", "in": "range(2)", "children": [
        {"kind": "quiz", "questions": [{"q": "a", "options": ["0", "1"], "correct": 0, "why": "b"}]}]})),
    "box bad colour": mutate(put(["body"], {"kind": "box", "background": "url(http://x)", "children": []})),
    "box click on a non-state": mutate(put(["body"], {"kind": "box", "click": {"nope": 1}, "children": []})),
    "too deep": mutate(put(["body"], deep_box(16))),
    "too many nodes": mutate(put(["body"], {"kind": "box", "children": [
        {"kind": "text", "text": "x"} for _ in range(400)]})),
    "too many states": mutate(put(["state"], {f"s{i}": i for i in range(33)})),
    "if without then": mutate(drop(["body", "children", 4, "then"])),
    # sandbox escapes (reported by the Obsidian plugin lane): refused statically on both sides
    "escape via array key": expr('sin[["con"+"structor"]]("return typeof process")()'),
    "escape via dot constructor": expr("sin.constructor"),
    "escape via __proto__ key": expr('xs["__proto__"]'),
    "escape via prototype": expr("{'a': 1}.prototype"),
    "object as key": expr("xs[{'a': 1}]"),
    "indirect: arrow kept in derived": mutate(lambda b: (b["state"].update(k=["constructor"]),
                                                         b["derived"].update(f="x => x", g="f[k]",
                                                                             h='g("return 1")()'))),
    "indirect: builtin as a value": mutate(lambda b: (b["state"].update(k=["constructor"]),
                                                      b["derived"].update(m="map", g="m[k]"))),
    "arrow outside a call": expr("[x => x]"),
    "builtin passed as an argument": expr("map(xs, sin)"),
}


# ---- Task 5b: fill-blank, sentence, pager, route ----
def fb(**kw) -> dict:
    return {"kind": "fill-blank", "sentence": "z 是离均值几个 ___ 。",
            "options": [{"label": "标准差"}, {"label": "分"}], "correct": 0, "why": "z 的单位是标准差。", **kw}


def sb(**kw) -> dict:
    return {"kind": "sentence", "sentence": "(___ − ___) ÷ ___", "words": ["x", "μ", "σ", "n"],
            "answer": [0, 1, 2], "why": "先减均值再除以标准差。", **kw}


def pg(*pages: dict, **kw) -> dict:
    return {"kind": "pager", "children": list(pages) or [T], **kw}


def rt(**kw) -> dict:
    return {"kind": "route", "prompt": "想怎么学？现在 {deg} 度",
            "options": [{"label": "看图", "value": "a", "next": "请先给我看图。"},
                        {"label": "读文", "value": "b", "next": "请先讲文字。"}], **kw}


def opts(n: int, **kw) -> list:
    return [{"label": f"选项{i}", "value": f"v{i}", "next": f"请按第 {i} 种方式讲。", **kw} for i in range(n)]


VALID.update({
    "fill-blank": add(fb()),
    "fill-blank with everything": add(fb(id="fb1", cell="c1.interpret",
                                         options=[{"label": f"候选{i}", "why": f"理由{i}"} for i in range(8)], correct=7)),
    "fill-blank blank at the start, multi-line": add(fb(sentence="___ 是均值。\n第二行")),
    "sentence": add(sb()),
    "sentence 8 blanks 12 words": add(sb(sentence=" ".join(["___"] * 8), words=[f"w{i}" for i in range(12)],
                                         answer=[7, 6, 5, 4, 3, 2, 1, 0], id="s8", cell="c1.interpret")),
    "sentence one blank, words of 60": add(sb(sentence="答案是 ___", words=["a" * 60, "b"], answer=[1])),
    "sentence words exactly the blanks": add(sb(words=["x", "μ", "σ"], answer=[2, 1, 0])),
    "pager": add(pg()),
    "pager gate with practice pages": add(pg(fb(id="p1"), sb(), {"kind": "box", "children": [T, fb()]},
                                             {"kind": "quiz", "questions": [{"q": "?", "options": ["a", "b"], "correct": 0,
                                                                             "why": "w"}]}, gate=True, id="drill")),
    "pager 20 pages": add(pg(*[T] * 20, gate=False)),
    "route": add(rt()),
    "route with everything": add(rt(id="how", preference="teaching_mode", bind="mode",
                                    options=[{**o, "desc": "一句说明"} for o in opts(6)][:2]
                                    + [{"label": "甲", "value": "a", "next": "n", "desc": "d"}])),
    "route six options": add(rt(options=opts(6))),
    "route bound to a number state": add(rt(bind="deg", options=[{"label": "30", "value": 30, "next": "n"},
                                                                 {"label": "45.5", "value": 45.5, "next": "n"}])),
    "route bound to a true/false state": add(rt(bind="on", options=[{"label": "开", "value": True, "next": "n"},
                                                                     {"label": "关", "value": False, "next": "n"}])),
    "route values 1, true and '1' differ": add(rt(options=[{"label": "a", "value": 1, "next": "n"},
                                                           {"label": "b", "value": True, "next": "n"},
                                                           {"label": "c", "value": "1", "next": "n"}])),
    "route value of 64": add(rt(options=[{"label": "a", "value": "v" * 64, "next": "n"}, {"label": "b", "value": "w", "next": "n"}])),
    "route without a template prompt": add(rt(prompt="接下来？")),
    "reveal after a fill-blank, a sentence and a pager": add(fb(id="f1"), sb(id="s1"), pg(id="p1"),
                                                             rv(mode="after", after="f1"), rv(mode="after", after="s1"),
                                                             rv(mode="after", after="p1")),
})

INVALID.update({
    # fill-blank
    "fill-blank no blank": add(fb(sentence="没有空")),
    "fill-blank two blanks": add(fb(sentence="___ 和 ___")),
    "fill-blank sentence a number": add(fb(sentence=3)),
    "fill-blank sentence too long": add(fb(sentence="___" + "长" * 600)),
    "fill-blank one option": add(fb(options=[{"label": "a"}])),
    "fill-blank nine options": add(fb(options=[{"label": f"o{i}"} for i in range(9)])),
    "fill-blank options not a list": add(fb(options={"label": "a"})),
    "fill-blank plain-text options": add(fb(options=["a", "b"])),
    "fill-blank option without label": add(fb(options=[{"why": "w"}, {"label": "b"}])),
    "fill-blank option extra field": add(fb(options=[{"label": "a", "correct": True}, {"label": "b"}])),
    "fill-blank option empty label": add(fb(options=[{"label": " "}, {"label": "b"}])),
    "fill-blank option why a number": add(fb(options=[{"label": "a", "why": 1}, {"label": "b"}])),
    "fill-blank repeated labels": add(fb(options=[{"label": "a"}, {"label": "a "}])),
    "fill-blank correct out of range": add(fb(correct=2)),
    "fill-blank correct true": add(fb(correct=True)),
    "fill-blank correct text": add(fb(correct="0")),
    "fill-blank no correct": mutate(lambda b: kids(b).append({k: v for k, v in fb().items() if k != "correct"})),
    "fill-blank no why": mutate(lambda b: kids(b).append({k: v for k, v in fb().items() if k != "why"})),
    "fill-blank empty why": add(fb(why="")),
    "fill-blank bad cell": add(fb(cell="c1")),
    "fill-blank cell with a newline": add(fb(cell="c1.interpret\n")),
    "fill-blank upper-case id": add(fb(id="FB")),
    "fill-blank id shared with the quiz": add(fb(id="q1"), quiz_id="q1"),
    "fill-blank unknown field": add(fb(hint="h")),
    # sentence
    "sentence no blank": add(sb(sentence="没有空", answer=[0])),
    "sentence nine blanks": add(sb(sentence=" ".join(["___"] * 9), words=[f"w{i}" for i in range(12)],
                                   answer=list(range(9)))),
    "sentence fewer words than blanks": add(sb(words=["x", "μ"], answer=[0, 1, 1])),
    "sentence 13 words": add(sb(words=[f"w{i}" for i in range(13)])),
    "sentence words not a list": add(sb(words="x μ σ")),
    "sentence empty word": add(sb(words=["x", "", "σ"], answer=[0, 1, 2])),
    "sentence word of 61": add(sb(words=["x" * 61, "μ", "σ"])),
    "sentence word a number": add(sb(words=["x", 2, "σ"])),
    "sentence repeated words": add(sb(words=["x", "μ", "x"], answer=[0, 1, 2])),
    "sentence answer too short": add(sb(answer=[0, 1])),
    "sentence answer out of range": add(sb(answer=[0, 1, 4])),
    "sentence answer repeated": add(sb(answer=[0, 0, 1])),
    "sentence answer not a list": add(sb(answer=0)),
    "sentence answer empty": add(sb(answer=[])),
    "sentence answer with true": add(sb(answer=[True, 1, 2])),
    "sentence no why": mutate(lambda b: kids(b).append({k: v for k, v in sb().items() if k != "why"})),
    "sentence bad cell": add(sb(cell="x")),
    "sentence unknown field": add(sb(options=[])),
    # pager
    "pager no pages": add(pg(children=[])),
    "pager children not a list": add({"kind": "pager", "children": T}),
    "pager gate text": add(pg(gate="yes")),
    "pager in a pager": add(pg(pg())),
    "pager deep in a pager's if": add(pg({"kind": "if", "cond": "on", "then": {"kind": "box", "children": [pg()]},
                                          "else": None})),
    "pager title (unknown field)": add(pg(title="练习")),
    "pager bad id": add(pg(id="a b")),
    "pager missing children": add({"kind": "pager", "gate": True}),
    # route
    "route one option": add(rt(options=opts(1))),
    "route options not a list": add(rt(options="a")),
    "route option without next": add(rt(options=[{"label": "a", "value": "a"}, *opts(1)])),
    "route option without value": add(rt(options=[{"label": "a", "next": "n"}, *opts(1)])),
    "route value null": add(rt(options=[{"label": "a", "value": None, "next": "n"}, *opts(1)])),
    "route value an object": add(rt(options=[{"label": "a", "value": {"x": 1}, "next": "n"}, *opts(1)])),
    "route value empty": add(rt(options=[{"label": "a", "value": "", "next": "n"}, *opts(1)])),
    "route value of 65": add(rt(options=[{"label": "a", "value": "v" * 65, "next": "n"}, *opts(1)])),
    "route repeated values 1 and 1.0": add(rt(options=[{"label": "a", "value": 1, "next": "n"},
                                                       {"label": "b", "value": 1.0, "next": "n"}])),
    "route option extra field": add(rt(options=[{**opts(1)[0], "correct": True}, *opts(2)[1:]])),
    "route option empty label": add(rt(options=[{**opts(1)[0], "label": ""}, *opts(2)[1:]])),
    "route next a number": add(rt(options=[{**opts(1)[0], "next": 1}, *opts(2)[1:]])),
    "route next too long": add(rt(options=[{**opts(1)[0], "next": "长" * 601}, *opts(2)[1:]])),
    "route empty desc": add(rt(options=[{**opts(1)[0], "desc": ""}, *opts(2)[1:]])),
    "route prompt a number": add(rt(prompt=1)),
    "route prompt unknown name": add(rt(prompt="{nope}")),
    "route bind not a state": add(rt(bind="nope")),
    "route text values on a number state": add(rt(bind="deg")),
    "route number values on a text state": add(rt(bind="mode", options=[{"label": "a", "value": 1, "next": "n"},
                                                                        {"label": "b", "value": 2, "next": "n"}])),
    "route text 'true' on a true/false state": add(rt(bind="on", options=[{"label": "a", "value": "true", "next": "n"},
                                                                          {"label": "b", "value": False, "next": "n"}])),
    "route preference upper case": add(rt(preference="TeachingMode")),
    "route preference with a space": add(rt(preference="teaching mode")),
    "route preference a number": add(rt(preference=1)),
    "route unknown field": add(rt(style="cards")),
    "route missing prompt": mutate(lambda b: kids(b).append({k: v for k, v in rt().items() if k != "prompt"})),
    "reveal after a route (not practice)": add(rt(id="how"), rv(mode="after", after="how")),
    "reveal after a fill-blank inside it": add(rv(mode="after", after="f1", children=[fb(id="f1")])),
})



def ch(chart: str = "line", **kw) -> dict:
    """a Task 6 chart over BASE's states (deg is a number state, mode a text state)"""
    return {"kind": "chart", "chart": chart, "data": "map(range(6), i => {'t': i, 'a': i * deg, 'b': 10 - i})", "x": "t",
            "series": [{"key": "a", "label": "甲"}, {"key": "b", "label": "乙"}] if chart != "pie" else [{"key": "a", "label": "甲"}],
            **kw}


ANN_X = {"x": 3, "label": "第 {deg} 度", "tone": "danger"}
ANN_Y = {"y": "deg / 2", "label": "目标"}

VALID.update({
    "chart area": add(ch("area")),
    "chart area with everything": add(ch("area", stack=True, curve="smooth", gradient=True, hover="deg", visible_points=3,
                                         annotations=[ANN_X, ANN_Y])),
    "chart line smooth / step / linear": add(ch(curve="smooth"), ch(curve="step"), ch(curve="linear")),
    "chart bar stacked, windowed, hovered": add(ch("bar", stack=False, visible_points=4, hover="deg")),
    "chart eight annotations": add(ch(annotations=[ANN_X, ANN_Y] * 4)),
    "chart empty annotations": add(ch(annotations=[])),
    "chart annotation x a text value": add(ch(annotations=[{"x": "'3'", "label": "三"}])),
    "chart visible_points float 5.0": add(ch(visible_points=5.0)),
    "chart pie donut": add(ch("pie", donut=True)),
    "chart pie not donut": add(ch("pie", donut=False)),
})

INVALID.update({
    "chart unknown kind column": add(ch("column")),
    "chart stack on a line": add(ch(stack=True)),
    "chart stack text": add(ch("bar", stack="yes")),
    "chart curve on a bar": add(ch("bar", curve="smooth")),
    "chart curve unknown": add(ch(curve="spline")),
    "chart gradient on a line": add(ch(gradient=True)),
    "chart gradient a number": add(ch("area", gradient=1)),
    "chart hover not a state": add(ch(hover="nope")),
    "chart hover a text state": add(ch(hover="mode")),
    "chart hover a true/false state": add(ch(hover="on")),
    "chart hover a number": add(ch(hover=1)),
    "chart hover on a pie": add(ch("pie", hover="deg")),
    "chart hover on a scatter": add(ch("scatter", hover="deg")),
    "chart visible_points 2": add(ch(visible_points=2)),
    "chart visible_points 3.5": add(ch(visible_points=3.5)),
    "chart visible_points true": add(ch(visible_points=True)),
    "chart visible_points text": add(ch(visible_points="10")),
    "chart visible_points on a pie": add(ch("pie", visible_points=5)),
    "chart nine annotations": add(ch(annotations=[ANN_X] * 9)),
    "chart annotations not a list": add(ch(annotations=ANN_X)),
    "chart annotation with x and y": add(ch(annotations=[{"x": 1, "y": 2, "label": "两个"}])),
    "chart annotation with neither x nor y": add(ch(annotations=[{"label": "空"}])),
    "chart annotation without label": add(ch(annotations=[{"x": 1}])),
    "chart annotation unknown field": add(ch(annotations=[{"x": 1, "label": "a", "dashed": True}])),
    "chart annotation bad tone": add(ch(annotations=[{"y": 1, "label": "a", "tone": "red"}])),
    "chart annotation unknown name": add(ch(annotations=[{"y": "nope + 1", "label": "a"}])),
    "chart annotation label unknown name": add(ch(annotations=[{"y": 1, "label": "{nope}"}])),
    "chart annotation label a number": add(ch(annotations=[{"y": 1, "label": 3}])),
    "chart annotation not an object": add(ch(annotations=[3])),
    "chart annotations on a pie": add(ch("pie", annotations=[ANN_Y])),
    "chart donut on a bar": add(ch("bar", donut=True)),
    "chart donut text": add(ch("pie", donut="yes")),
})

# ---- app (Task 7): the sandboxed mini-app ----
APP_HTML = ("<p id=o>0</p><button onclick=\"evowiki.item('a', 1, true)\">go</button>"
            "<script>evowiki.ready()</script>")


def ap(html: str = APP_HTML, **kw) -> dict:
    return {"kind": "app", "html": html, **kw}


def items(n: int) -> list:
    return [{"id": f"i{k}", "label": f"第 {k} 项"} for k in range(n)]


def padded(chars: int) -> dict:
    """BASE + an app with a 50000-character html + plain captions, so the block without the html is
    exactly `chars` characters."""
    b = add(ap("<p>" + "x" * 50_000 + "</p>"))
    base = json.loads(json.dumps(b))
    base["body"]["children"][-1]["html"] = ""
    size = len(json.dumps(base, ensure_ascii=False, separators=(",", ":")))
    cap = {"kind": "caption", "text": ""}
    one = len(json.dumps(cap, ensure_ascii=False, separators=(",", ":"))) + 1   # + the comma
    need = chars - size
    while need > 0:
        take = min(600, need - one) if need - one > 0 else 0
        if take <= 0:
            break
        kids(b).append({"kind": "caption", "text": "y" * take})
        need -= one + take
    assert need == 0, need
    return b


VALID.update({
    "app minimal": add(ap()),
    "app with everything": add(ap(id="lab", height=900, items=[
        {"id": "a", "label": "甲", "cell": "c1.interpret"}, {"id": "b-2_x", "label": "乙"}])),
    "app height 200 and 420.0": add(ap(height=200)),
    "app height 420.0": add(ap(height=420.0)),
    "app 40 items": add(ap(items=items(40))),
    "app no items": add(ap(items=[])),
    "app item id of 32": add(ap(items=[{"id": "a" * 32, "label": "长"}])),
    "app in an if branch": add({"kind": "if", "cond": "on", "then": ap(), "else": None}),
    "app html of 60000": add(ap("<p>" + "x" * 59_993 + "</p>")),
    "app allowlisted script / style / font": add(ap(
        '<script src="https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.min.js"></script>'
        "<link rel=stylesheet href='https://cdnjs.cloudflare.com/ajax/libs/katex/0.16.9/katex.min.css'>"
        "<style>@font-face{font-family:K;src:url(https://unpkg.com/katex@0.16.9/dist/fonts/a.woff2)}</style>"
        "<script type=module>import * as d3 from 'https://cdn.jsdelivr.net/npm/d3@7/+esm';"
        "const m = await import(\"https://unpkg.com/lodash-es@4.17.21/lodash.js\");</script>")),
    "app allowlisted CDN in upper case": add(ap('<script src="HTTPS://CDN.JSDELIVR.NET/npm/x.js"></script>')),
    "app local URLs": add(ap(
        '<img src="data:image/png;base64,AAAA"><a href="#top">上</a><a href="javascript:void 0">x</a>'
        "<a href=''>y</a><svg><use href='#g'/><rect fill='url(#grad)'/></svg>"
        "<div style=\"background:url(data:image/svg+xml;utf8,abc)\"></div><a href=\"about:blank\">z</a>"
        "<script>const u = URL.createObjectURL(new Blob([])); img.src = u; img.src = 'blob:' + u;</script>")),
    "app relative / templated URLs (not external)": add(ap(
        "<script>el.innerHTML = '<img src=\"' + url + '\">'; x.src = pic; y.href = `#${id}`;</script>"
        '<img src="pic.png">')),
    "app location reads and hash": add(ap(
        "<script>const h = location.href; if (location.href === h) location.hash = 'x';"
        " const relocation = 1; obj.locations = 2; const s = location.search;</script>")),
    "app arrow parameter named location": add(ap("<script>const names = places.map(location => location.name);</script>")),
    "app look-alike names": add(ap(
        "<style>.top{margin-top:0}#a.top{top:0}.parent{x:1!important}</style>"
        "<script>const y = rect.top.toFixed(1); el.parentNode.append(x); node.parent.name; "
        "const stopper = 1; desktop.x = 2; myparent.x = 3; const s = 'important';"
        "const data = {top: 1}; ctx.font = '12px'; window.addEventListener('resize', f); "
        "self.addEventListener('x', f); const cookies = 0; document.title = 'x';</script>")),
    "app with a 40000 block besides the html": padded(40_000),
})

INVALID.update({
    "app missing html": add({"kind": "app"}),
    "app empty html": add(ap("")),
    "app blank html": add(ap("   ")),
    "app html a number": add(ap(5)),
    "app html of 60001": add(ap("<p>" + "x" * 59_994 + "</p>")),
    "app block of 40001 besides the html": padded(40_001),
    "two apps": add(ap(), ap()),
    "two apps in if branches": add({"kind": "if", "cond": "on", "then": ap(), "else": ap()}),
    "app height 199": add(ap(height=199)),
    "app height 901": add(ap(height=901)),
    "app height 420.5": add(ap(height=420.5)),
    "app height true": add(ap(height=True)),
    "app height text": add(ap(height="420")),
    "app unknown field": add(ap(width=300)),
    "app bad id": add(ap(id="Lab")),
    "app id used by the quiz too": add(ap(id="z"), quiz_id="z"),
    "app items not a list": add(ap(items={"id": "a", "label": "甲"})),
    "app 41 items": add(ap(items=items(41))),
    "app item not an object": add(ap(items=["a"])),
    "app item missing id": add(ap(items=[{"label": "甲"}])),
    "app item missing label": add(ap(items=[{"id": "a"}])),
    "app item id upper case": add(ap(items=[{"id": "A", "label": "甲"}])),
    "app item id of 33": add(ap(items=[{"id": "a" * 33, "label": "甲"}])),
    "app item id with a space": add(ap(items=[{"id": "a b", "label": "甲"}])),
    "app item id with a dot": add(ap(items=[{"id": "a.b", "label": "甲"}])),
    "app item id a number": add(ap(items=[{"id": 1, "label": "甲"}])),
    "app item id empty": add(ap(items=[{"id": "", "label": "甲"}])),
    "app item ids repeated": add(ap(items=[{"id": "a", "label": "甲"}, {"id": "a", "label": "乙"}])),
    "app item label empty": add(ap(items=[{"id": "a", "label": " "}])),
    "app item label of 121": add(ap(items=[{"id": "a", "label": "长" * 121}])),
    "app item label a number": add(ap(items=[{"id": "a", "label": 1}])),
    "app item bad cell": add(ap(items=[{"id": "a", "label": "甲", "cell": "c1"}])),
    "app item unknown field": add(ap(items=[{"id": "a", "label": "甲", "correct": True}])),
    # forbidden markup and APIs
    "app <iframe>": add(ap("<iframe src='#'></iframe>")),
    "app <IFRAME> in upper case": add(ap("<IFRAME></IFRAME>")),
    "app <object>": add(ap("<object data='x'></object>")),
    "app <embed>": add(ap("<embed type='x'>")),
    "app <base>": add(ap("<base href='#'>")),
    "app <meta http-equiv>": add(ap("<meta http-equiv=\"refresh\" content=\"0\">")),
    "app <meta HTTP-EQUIV>": add(ap("<META charset=utf-8 HTTP-EQUIV='x'>")),
    "app document.cookie": add(ap("<script>x = document.cookie</script>")),
    "app document . cookie": add(ap("<script>x = document .\n cookie</script>")),
    "app localStorage": add(ap("<script>localStorage.setItem('a', 1)</script>")),
    "app sessionStorage": add(ap("<script>const s = sessionStorage</script>")),
    "app indexedDB": add(ap("<script>indexedDB.open('x')</script>")),
    "app window.parent": add(ap("<script>window.parent.postMessage(1, '*')</script>")),
    "app window.top": add(ap("<script>window.top.location = '#'</script>")),
    "app window['parent']": add(ap("<script>window['parent'].x</script>")),
    "app self.top": add(ap("<script>self.top</script>")),
    "app globalThis.parent": add(ap("<script>globalThis . parent</script>")),
    "app window.opener": add(ap("<script>window.opener</script>")),
    "app parent.postMessage": add(ap("<script>parent.postMessage('x', '*')</script>")),
    "app top.location": add(ap("<script>top.location.href = '#'</script>")),
    "app top[...]": add(ap("<script>top['location']</script>")),
    # external URLs
    "app external script": add(ap('<script src="https://evil.example/x.js"></script>')),
    "app external http image": add(ap("<img src='http://evil.example/p.png'>")),
    "app unquoted external src": add(ap("<script src=https://evil.example/x.js></script>")),
    "app protocol-relative src": add(ap('<script src="//evil.example/x.js"></script>')),
    "app external link": add(ap('<a href="https://example.com/">外链</a>')),
    "app external stylesheet": add(ap("<link rel=stylesheet href='https://fonts.googleapis.com/css2?family=Inter'>")),
    "app external css url()": add(ap("<style>body{background:url('https://evil.example/bg.png')}</style>")),
    "app external @import": add(ap("<style>@import \"https://evil.example/a.css\";</style>")),
    "app external import from": add(ap("<script type=module>import React from 'https://esm.sh/react'</script>")),
    "app external dynamic import": add(ap("<script>import('https://evil.example/x.js')</script>")),
    "app external export from": add(ap("<script type=module>export * from \"https://evil.example/x.js\"</script>")),
    "app external img.src in js": add(ap("<script>img.src = \"https://evil.example/p.png\"</script>")),
    "app external template src in js": add(ap("<script>img.src = `https://evil.example/${n}.png`</script>")),
    "app CDN look-alike host": add(ap('<script src="https://cdn.jsdelivr.net.evil.example/x.js"></script>')),
    "app CDN with userinfo": add(ap('<script src="https://cdn.jsdelivr.net@evil.example/x.js"></script>')),
    "app CDN over http": add(ap('<script src="http://cdn.jsdelivr.net/npm/x.js"></script>')),
    "app external srcset": add(ap("<img srcset='https://evil.example/a.png 2x'>")),
    "app external poster": add(ap("<video poster=\"https://evil.example/a.png\"></video>")),
    "app external with leading blanks": add(ap('<script src="  https://evil.example/x.js"></script>')),
    "app ftp URL": add(ap("<a href='ftp://evil.example/f'>f</a>")),
    # navigation, WebRTC, resource hints (fix round 1)
    "app location =": add(ap("<script>location = 'about:blank'</script>")),
    "app location.href =": add(ap("<script>location.href = u</script>")),
    "app window.location.replace": add(ap("<script>window.location.replace(u)</script>")),
    "app document.location.reload": add(ap("<script>document.location.reload()</script>")),
    "app location.assign": add(ap("<script>location . assign(u)</script>")),
    "app location.search =": add(ap("<script>location.search = '?a'</script>")),
    "app RTCPeerConnection": add(ap("<script>new RTCPeerConnection({})</script>")),
    "app webkitRTCPeerConnection": add(ap("<script>new webkitRTCPeerConnection({})</script>")),
    "app link dns-prefetch": add(ap("<link rel='dns-prefetch' href='#x'>")),
    "app link preconnect": add(ap("<LINK REL=preconnect href='#x'>")),
    "app link prefetch": add(ap("<link rel=prefetch href='#x'>")),
    # an app-shaped object outside the node tree keeps its html in the block cap (fix round 1)
    "app-shaped state value over the cap": mutate(lambda b: b["state"].update(
        {"box": {"kind": "app", "html": "x" * 45_000}})),
})

# Item counts are prompt guidance (spec.md 数量建议), never a validator rule: only render / safety limits refuse.
VALID.update({
    "chart five series": mutate(put(["body", "children", 5, "series"], [{"key": "y", "label": "y"}] * 5)),
    "pager 21 pages": add(pg(*[T] * 21)),
    "route seven options": add(rt(options=opts(7))),
    "flashcards 41 cards": with_fc(cards=[{"front": f"f{i}", "back": f"b{i}"} for i in range(41)]),
    "16 questions": mutate(put(QZ + ["questions"], [copy.deepcopy(BASE["body"]["children"][6]["questions"][0])
                                                    for _ in range(16)])),
})


# timeline / deep-dive (after DeepTutor's book blocks): events plain text, a deep-dive folds any nodes
EV = [{"when": "1905", "title": "狭义相对论", "text": "光速不变", "tone": "accent"}, {"title": "广义相对论"}]
VALID.update({
    "timeline": add({"kind": "timeline", "events": EV}),
    "timeline numbered (no when)": add({"kind": "timeline", "events": [{"title": "读题"}, {"title": "列式"}]}),
    "deep-dive": add({"kind": "deep-dive", "label": "深入一点：为什么", "children": [T, {"kind": "timeline", "events": EV}]}),
    "deep-dive default label": add({"kind": "deep-dive", "children": [T]}),
    "deep-dive inside a deep-dive": add({"kind": "deep-dive", "children": [{"kind": "deep-dive", "children": [T]}]}),
})
INVALID.update({
    "timeline one event": add({"kind": "timeline", "events": EV[:1]}),
    "timeline events not a list": add({"kind": "timeline", "events": "1905"}),
    "timeline event without title": add({"kind": "timeline", "events": [{"when": "1905"}, EV[1]]}),
    "timeline event unknown field": add({"kind": "timeline", "events": [{**EV[0], "date": "1905"}, EV[1]]}),
    "timeline event empty when": add({"kind": "timeline", "events": [{**EV[0], "when": " "}, EV[1]]}),
    "timeline event bad tone": add({"kind": "timeline", "events": [{**EV[0], "tone": "red"}, EV[1]]}),
    "timeline event title not text": add({"kind": "timeline", "events": [{"title": 1905}, EV[1]]}),
    "timeline unknown field": add({"kind": "timeline", "events": EV, "title": "年表"}),
    "deep-dive no children": add({"kind": "deep-dive", "children": []}),
    "deep-dive empty label": add({"kind": "deep-dive", "label": "", "children": [T]}),
    "deep-dive bad child": add({"kind": "deep-dive", "children": [{"kind": "html", "text": "<b>"}]}),
    "deep-dive with id": add({"kind": "deep-dive", "id": "dd", "children": [T]}),
})


def test_python_verdicts():
    for name, b in VALID.items():
        assert validate(b) == [], name
    for name, b in INVALID.items():
        assert validate(b) != [], name


def test_parse_errors_are_expr_errors():
    for src in ("'abc", "1 2", "a ?", "x..y", "(x) => x", "{1: 2}", "a = 1"):
        with pytest.raises(ExprError):
            parse(src)
    assert parse("x => y => x")[0] == "fn"


def test_javascript_reaches_the_same_verdicts():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed (uv sync --extra deep)")
    cases = {**{f"valid/{k}": v for k, v in VALID.items()},
             **{f"invalid/{k}": v for k, v in INVALID.items()}}
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content("<!doctype html><meta charset=utf-8><body></body>")
            page.add_script_tag(content=JS_PATH.read_text(encoding="utf-8"))
            js = page.evaluate("cases => Object.fromEntries(Object.entries(cases).map("
                               "([k, b]) => [k, EvoUI.validate(b)]))", json.loads(json.dumps(cases)))
            browser.close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"chromium not available ({type(exc).__name__}); uv run playwright install chromium")
    disagree = {k: (validate(cases[k]), js[k]) for k in cases
                if (validate(cases[k]) == []) != (js[k] == [])}
    assert disagree == {}
    wrong = [k for k in cases if (js[k] == []) != k.startswith("valid/")]
    assert wrong == []


ESCAPES_ENV = {"xs": [1, 2, 3], "k": ["constructor"]}
ESCAPES = ['iterate(2, y => y, f => f)[0]', 'map([1], y => y => y)', 'sin', 'iterate(2, y => y, f => f)[0][k]', 'iterate(2, y => y, f => f)[0]["call"]', 'sin[["con"+"structor"]]("return 1")', 'sin[["con"+"structor"]]', 'sin["con"+"structor"]',
           'xs["__pro"+"to__"]', "map(xs, x => x)[[\"constructor\"]]", "fmt.call", "xs.map",
           "{'a': 1}['to'+'String']()", 'map(range(2000), i => sum(map(range(2000), j => j)))']


def test_the_renderer_refuses_escapes_at_runtime_too():
    """Bypassing the validator: the evaluator itself must refuse (EvoUIError) or yield nothing usable."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed (uv sync --extra deep)")
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"chromium not available ({type(exc).__name__})")
        page = browser.new_page()
        page.set_content("<!doctype html><meta charset=utf-8><body></body>")
        page.add_script_tag(content=JS_PATH.read_text(encoding="utf-8"))
        got = page.evaluate("""srcs => srcs.map(src => {
              try { const v = EvoUI.evalExpr(src, {xs: [1, 2, 3], k: ["constructor"]});
                    return typeof v === "function" ? "function!" : v === undefined ? "undefined" : "value"; }
              catch (e) { return e instanceof EvoUI.EvoUIError ? "blocked" : "other:" + e; } })""",
                            ESCAPES)
        ok = page.evaluate("EvoUI.evalExpr('sum(map(xs, x => x * 2)) + xs.length + xs[1]',"
                           " {xs: [1, 2, 3]})")
        browser.close()
    want = {src: "blocked" for src in ESCAPES}
    want["xs.map"] = "undefined"                                # not an own property: nothing
    assert dict(zip(ESCAPES, got, strict=True)) == want
    assert ok == 12 + 3 + 2                                    # ordinary expressions still work


def test_chart_keys_are_own_property_reads_too():
    """series[].key / x go through the same member rule: "constructor" fails the block (fallback),
    never prints a host function."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed (uv sync --extra deep)")
    block = {**BASE, "body": {"kind": "chart", "chart": "line", "data": "[{'a': 1}, {'a': 2}]",
                              "x": "a", "series": [{"key": "constructor", "label": "c"}]}}
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"chromium not available ({type(exc).__name__})")
        page = browser.new_page()
        page.set_content("<!doctype html><meta charset=utf-8><body><div id=m></div></body>")
        page.add_script_tag(content=JS_PATH.read_text(encoding="utf-8"))
        text = page.evaluate("b => { try { EvoUI.render(b, document.getElementById('m')); } catch (e) {}"
                             " return document.body.innerText + '|' + !!document.querySelector('.evoui-error'); }",
                             block)
        browser.close()
    assert "native code" not in text and text.endswith("|true"), text


def _quiz(qid: str | None = None) -> dict:
    q = {"kind": "quiz", "questions": [{"q": "?", "options": ["a", "b"], "correct": 0, "why": "w"}]}
    return {**q, "id": qid} if qid else q


# nested ifs: children, then `then`, then `else`, depth first; ids keep their place in the count
KEYED = {**copy.deepcopy(BASE), "body": {"kind": "box", "children": [
    _quiz(),
    {"kind": "if", "cond": "on", "then": {"kind": "box", "children": [
        _quiz("inner"),
        {"kind": "if", "cond": "deg > 10", "then": _quiz(), "else": _quiz("deep-else")}]},
     "else": _quiz()},
    _quiz(),
    {"kind": "if", "cond": "!on", "then": _quiz(), "else": None}]}}
KEYED_WANT = ["quiz#0", "inner", "quiz#2", "deep-else", "quiz#4", "quiz#5", "quiz#6"]


# Every D1 stateful kind (most are not in the validators yet: keyed without validating).
MIXED = {"body": {"kind": "box", "children": [
    {"kind": "flashcards", "cards": []}, {"kind": "app", "id": "lab"},
    {"kind": "pager", "children": [{"kind": "fill-blank"}, {"kind": "sentence"},
                                   {"kind": "fill-blank", "id": "fb"}]},
    {"kind": "if", "when": "true", "then": {"kind": "route"}, "else": {"kind": "timer"}},
    {"kind": "reveal"}, {"kind": "player"}, {"kind": "quiz"}, {"kind": "flashcards"},
    {"kind": "slider"}]}}
MIXED_WANT = ["flashcards#0", "lab", "pager#0", "fill-blank#0", "sentence#0", "fb", "route#0",
              "timer#0", "reveal#0", "player#0", "quiz#0", "flashcards#1"]


def test_node_keys_number_like_the_renderer():
    from evowiki.deep.evoui import node_keys
    assert validate(KEYED) == []
    assert list(node_keys(KEYED)) == KEYED_WANT
    assert list(node_keys(BASE)) == ["quiz#0"]
    assert list(node_keys(MIXED)) == MIXED_WANT
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed (uv sync --extra deep)")
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"chromium not available ({type(exc).__name__})")
        page = browser.new_page()
        page.set_content("<!doctype html><meta charset=utf-8><body></body>")
        page.add_script_tag(content=JS_PATH.read_text(encoding="utf-8"))
        js = page.evaluate("bs => bs.map(b => EvoUI.nodeKeys(b))", [KEYED, BASE, MIXED])
        browser.close()
    assert js == [KEYED_WANT, ["quiz#0"], MIXED_WANT]


def test_the_app_csp_allowlist_and_scan_are_the_same_constants_in_both():
    """The app's CSP, CDN allowlist, limits and static-scan regex sources: contracts.py == EvoUI.APP."""
    from evowiki.deep.evoui import contracts as c
    want = {"cdns": list(c.APP_CDN_ALLOWLIST), "csp": c.APP_CSP, "sandbox": c.APP_SANDBOX, "html": c.MAX_APP_HTML,
            "minHeight": c.MIN_APP_HEIGHT, "maxHeight": c.MAX_APP_HEIGHT, "height": c.DEFAULT_APP_HEIGHT,
            "minResize": c.MIN_APP_RESIZE, "maxResize": c.MAX_APP_RESIZE, "items": c.MAX_APP_ITEMS,
            "label": c.MAX_APP_LABEL, "message": c.MAX_APP_MESSAGE, "save": c.MAX_APP_SAVE,
            "events": c.MAX_APP_EVENTS, "readyMs": c.APP_READY_MS, "latency": c.MAX_APP_LATENCY_MS,
            "itemReports": c.MAX_APP_ITEM_REPORTS,
            "itemFields": [list(x) for x in c.APP_ITEM_FIELDS],
            "images": c.MAX_APP_IMAGES, "imageFields": [list(x) for x in c.APP_IMAGE_FIELDS],
            "forbidden": [list(x) for x in c.APP_FORBIDDEN], "urls": [list(x) for x in c.APP_URL_PATTERNS],
            "external": c.APP_URL_EXTERNAL, "local": c.APP_URL_LOCAL, "trim": c.APP_URL_TRIM}
    assert all(u.startswith("https://") and u.endswith("/") for u in c.APP_CDN_ALLOWLIST)
    assert all(u in c.APP_CSP for u in c.APP_CDN_ALLOWLIST) and "connect-src 'none'" in c.APP_CSP
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed (uv sync --extra deep)")
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content("<!doctype html><meta charset=utf-8><body></body>")
            page.add_script_tag(content=JS_PATH.read_text(encoding="utf-8"))
            got = page.evaluate("() => EvoUI.APP")
            browser.close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"chromium not available ({type(exc).__name__}); uv run playwright install chromium")
    assert got == want
