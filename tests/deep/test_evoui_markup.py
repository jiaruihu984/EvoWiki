"""Block markup -> block JSON: what a model writes out of habit is read, what cannot be read says how to fix it."""

import pytest

from evowiki.deep.evoui import validate
from evowiki.deep.evoui.markup import MarkupError, compile_markup


def block(src: str) -> dict:
    b = compile_markup(src, title="t", question="q", fallback="f")
    assert validate(b) == [], b
    return b


def test_state_const_and_a_loop_of_shapes():
    b = block("""{@state s = 2}{@const xs = [1, 2]}{@const k = s * 2}
    <svg viewBox="0 0 640 100">{#each xs as x, i}<rect x={i*5} width=9 height={x+k} fill={i==s?"blue":"gray"} onClick={s = i}/>{/each}</svg>""")
    assert b["state"] == {"s": 2, "xs": [1, 2]}
    assert b["derived"] == {"k": "s*2"}
    each = b["body"]["children"][0]
    assert each["kind"] == "each" and (each["as"], each["index"]) == ("x", "i")
    r = each["children"][0]
    assert r == {"el": "rect", "x": "{i*5}", "width": 9, "height": "{x+k}", "fill": '{i==s?"blue":"gray"}', "click": {"s": "i"}}


@pytest.mark.parametrize(("js", "ours"), [
    ("Math.floor(x/2)", "floor(x/2)"),
    ("Math.PI*2", "PI*2"),
    ("xs.map((a,j)=>a+j)", "map(xs,(a,j)=>a+j)"),
    ("xs.filter(a=>a>1).length", "filter(xs,a=>a>1).length"),
    ("xs.reduce((s,a)=>s+a,0)", "fold(xs,0,(s,a)=>s+a)"),
    ("x.toFixed(2)", "fmt(x,2)"),
    ("Array.from({length:3},(_,j)=>j*2)", "map(range(3),(_,j)=>j*2)"),
    ("Array(3).fill(0)", "repeat(0,3)"),
    ("xs.includes(1)", "has(xs,1)"),
    ("`${x}%`", 'x+"%"'),
    ("x===1?2:3", "x==1?2:3"),
    ("x > 1 and not x > 3 or x == 0", "x>1&&!x>3||x==0"),
])
def test_javascript_habits(js: str, ours: str):
    b = block("{@state x = 1}{@const xs = [1,2]}{@const y = " + js + "}<text>{y}</text>")
    assert b["derived"]["y"] == ours


def test_a_const_function_is_a_macro():
    b = block("{@state x = 1}{@const px = (v, w) => v * w + 8}{@const y = px(x + 1, 2)}<text>{y}</text>")
    assert b["derived"] == {"y": "((x+1)*2+8)"}


def test_jsx_habits_in_a_drawing():
    b = block('''{@body const [mu, setMu] = DIL.useState(5)}<svg viewBox="0 0 10 10" xmlns="http://www.w3.org/2000/svg">
      <path d="M0 0" strokeWidth="2" key={1} onClick={()=>{setMu(1)}}/></svg><slider value={mu} onChange={setMu} min={0} max={9} step={1}/>''')
    p = b["body"]["children"][0]["children"][0]
    assert p == {"el": "path", "d": "M0 0", "stroke-width": 2, "click": {"mu": 1}}
    assert b["body"]["children"][1]["bind"] == "mu"


def test_a_function_passed_by_name_a_bare_false_and_points_in_quotes():
    b = block("""{@state k = 0}{@const step = (s) => s + 1}{@const xs = iterate(3, 0, step)}<box>
      <svg viewBox="0 0 9 9"><polygon points="[[0,0],[1,1],[2,0]]"/></svg><text>{xs[k]}</text>
      <player bind=k min=0 max=2 step=1 loop=false/></box>""")
    assert b["derived"]["xs"] == "iterate(3,0,s=>s+1)"
    assert b["body"]["children"][0]["children"][0]["points"] == [[0, 0], [1, 1], [2, 0]]
    assert b["body"]["children"][2]["loop"] is False


def test_options_from_a_constant_table_and_an_expression_written_bare():
    b = block("""{@state m = 0}{@const ms = [{name:"甲"},{name:"乙"}]}<box><choice bind=m options={ms.map((x, i) => ({label: x.name, value: i}))}/>
      <svg viewBox="0 0 9 9"><text x=m/2*3+1 y=3 size=12>字 <text color=blue>套</text></text></svg></box>""")
    assert b["body"]["children"][0]["options"] == [{"label": "甲", "value": 0}, {"label": "乙", "value": 1}]
    t = b["body"]["children"][1]["children"][0]
    assert t["x"] == "{m/2*3+1}" and t["text"] == "字 套" and t["font-size"] == 12


def test_options_that_change_say_what_to_write_instead():
    with pytest.raises(MarkupError, match="each"):
        compile_markup("{@state m = 0}<choice bind=m options={[{label:str(m),value:0}]}/>", title="t", question="q", fallback="f")


def test_if_else_chains_and_several_children():
    b = block("{@state s = 0}{#if s == 0}<text>a</text><text>b</text>{:else if s == 1}<text>c</text>{:else}<text>d</text>{/if}")
    n = b["body"]
    assert n["kind"] == "if" and n["then"]["kind"] == "box" and n["else"]["kind"] == "if" and n["else"]["else"]["text"] == "d"


def test_text_with_bold_is_rich():
    b = block("{@state s = 0}<box>值是 **{s}** 个</box>")
    t = b["body"]["children"][0]
    assert t["text"] == "值是 **{s}** 个" and t["rich"] is True


@pytest.mark.parametrize(("src", "says"), [
    ("<svg></svg>", "viewBox"),
    ('<svg viewBox="0 0 1 1">字</svg>', "<text"),
    ('<svg viewBox="0 0 1 1"><div/></svg>', "div"),
    ('<svg viewBox="0 0 1 1"><rect style="x"/></svg>', "style"),
    ("<rect/>", "<svg"),
    ("<nope/>", "nope"),
    ("<box>", "</box>"),
    ("{@const r = Math.random()}<text>{r}</text>", "rand"),
    ("{@const r = nope(1)}<text>{r}</text>", "nope"),
    ("{@const r = [1].push(2)}<text>{r}</text>", ".push"),
    ("{@const f = x => { return x }}<text>{f(1)}</text>", "表达式"),
    ("{@const f = x => x}<text>{f}</text>", "函数"),
    ("{@state s = 1 + nope}<text>a</text>", "固定值"),
    ("{#each [1]}<text>a</text>{/each}", "as"),
    ("", "空"),
])
def test_what_cannot_be_read_says_how_to_fix_it(src: str, says: str):
    with pytest.raises(MarkupError, match=says):
        compile_markup(src, title="t", question="q", fallback="f")
