"""The visualization step (`deep.visual`) on a scripted model, and the render check of what it draws (real browser)."""

import base64
import io
import json
from typing import Any

import pytest

from evowiki.deep.contracts import AppProbe, BlockLook, RenderCheck
from evowiki.deep.evoui.contracts import MAX_APP_HTML, EvoUIBlock
from evowiki.deep.images import ImageCandidate, PreparedImage
from evowiki.deep.render import PlaywrightChecker
from evowiki.deep.visual import Brief, VisualFailed, VisualStep
from evowiki.llm.chat import ChatTurn
from evowiki.llm.gateway import ProviderOutputError
from evowiki.llm.providers import ProviderTransientError
from evowiki.wikiapp.refine_apply import stored_images

BRIEF = Brief(block_id="net", section_id="s1", heading="度", body="度是连着的边数。", invisible="边数看不见",
              draw="一张小网络", touch="点两个节点连边")
HTML = "<svg viewBox='0 0 100 40' style='width:100%'><circle id='a' cx='20' cy='20' r='10'/></svg><script>evowiki.ready()</script>"


@pytest.fixture(autouse=True)
def _program_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests are about the program route, which takes over when the markup fails (tests/deep/test_scene.py)."""
    class NoScene:
        def draw(self, b: Any) -> Any:
            raise VisualFailed("这里只测小程序")
    monkeypatch.setattr(VisualStep, "_scene", lambda self: NoScene())


def reply(html: str = HTML, extra: str = "") -> str:
    return (f"<design>网络</design><html>{html}</html><fallback>文字版</fallback>"
            f"<probes>[{{\"target\":\"#a\",\"action\":\"click\",\"see\":\"变色\"}}]</probes>{extra}")


class Model:
    """A gateway that answers from a list and keeps what it was asked."""

    def __init__(self, *answers: Any):
        self.answers = list(answers)
        self.requests: list[Any] = []

    def converse(self, role: str, request: Any, *, account: Any = None) -> tuple[ChatTurn, Any]:
        self.requests.append(request)
        got = self.answers.pop(0)
        if isinstance(got, Exception):
            raise got
        return ChatTurn(text=got), object()


    def durable_step(self, name: str, account: Any, run: Any) -> Any:
        return json.loads(json.dumps({**run(), "charged": 0.1}))       # a step's value is JSON, with what it charged


class Eyes:
    """A checker that fails what it is told to and takes one picture."""

    def __init__(self, bad: str | None = None):
        self.bad = bad
        self.pictures: list[Any] = []

    def check_block(self, block_id: str, block: EvoUIBlock, images: Any = None, touch: Any = ()) -> RenderCheck:
        self.pictures.append(images)
        html = block.body["html"]
        why = "出错了" if self.bad and self.bad in html else None
        return RenderCheck(interactive_id=block_id, ok=why is None, detail=why or "")

    def check(self, d: Any) -> RenderCheck:
        return RenderCheck(interactive_id=d.interactive_id, ok=True)

    def look_interactive(self, d: Any) -> BlockLook:
        return BlockLook(shots=(("刚打开", base64.b64encode(b"x").decode()),))

    def look_block(self, block_id: str, block: EvoUIBlock, probes: Any, images: Any = None) -> BlockLook:
        return BlockLook(shots=(("刚打开", base64.b64encode(b"x").decode()),), notes=("操作 1：做了，画面变了",))


class Gallery:
    def __init__(self) -> None:
        self.hit = ImageCandidate(provider="commons", title="Guppy", license="CC BY 4.0", author="A",
                                  source_url="https://commons.example/guppy", file_url="https://upload.wikimedia.org/x/1280px-g.jpg")

    def search(self, query: str, limit: int) -> list[ImageCandidate]:
        return [self.hit]

    def thumbnail(self, c: ImageCandidate) -> PreparedImage:
        return PreparedImage(data=b"thumb", width=3, height=2)

    def fetch(self, c: ImageCandidate) -> PreparedImage:
        return PreparedImage(data=b"full", width=30, height=20)


def step(model: Model, eyes: Eyes, images: Any = None) -> VisualStep:
    return VisualStep(gw=model, account=None, checker=eyes, images=images, page_id="p", title="度分布", goal="懂度",  # type: ignore[arg-type]
                      known="刚开始学")


def test_it_writes_checks_looks_once_and_keeps_the_revision():
    m = Model(reply(), reply(HTML.replace("r='10'", "r='18'"), "<review>圆太小</review>"))
    got = step(m, Eyes()).draw(BRIEF)
    assert len(m.requests) == 2 and got.review == "圆太小" and "r='18'" in got.block.block.body["html"]
    assert got.block.block.fallback == "文字版" and got.block.block.body["kind"] == "app"
    assert [p.kind for p in m.requests[1].messages[-1].parts] == ["image"]     # it really saw the screenshot


def test_a_revision_that_fails_goes_back_once_then_what_passed_stands():
    m = Model(reply(), reply("BROKEN"), reply(HTML.replace("r='10'", "r='18'")))
    assert "r='18'" in step(m, Eyes(bad="BROKEN")).draw(BRIEF).block.block.body["html"]
    assert "渲染检查没过" in m.requests[2].messages[-1].text
    got = step(Model(reply(), reply("BROKEN"), reply("BROKEN")), Eyes(bad="BROKEN")).draw(BRIEF)
    assert got.block.block.body["html"] == HTML


def test_a_failure_goes_back_once_then_it_fails_closed():
    m = Model(reply("BROKEN"), reply(), reply())
    assert step(m, Eyes(bad="BROKEN")).draw(BRIEF).block.block.body["html"] == HTML
    assert "渲染检查没过：" in m.requests[1].messages[-1].text and "出错了" in m.requests[1].messages[-1].text
    with pytest.raises(VisualFailed):
        step(Model(reply("BROKEN"), reply("BROKEN")), Eyes(bad="BROKEN")).draw(BRIEF)


def test_the_repair_of_a_revision_carries_the_whole_conversation_and_a_failure_keeps_what_passed():
    m = Model(reply(), reply("BROKEN"), reply(HTML.replace("r='10'", "r='18'")))
    step(m, Eyes(bad="BROKEN")).draw(BRIEF)
    assert [x.role for x in m.requests[2].messages] == ["user", "assistant", "user", "assistant", "user"]
    assert step(Model(reply(), ProviderTransientError("busy")), Eyes()).draw(BRIEF).block.block.body["html"] == HTML
    got = step(Model(reply(), reply("BROKEN"), ProviderTransientError("busy")), Eyes(bad="BROKEN")).draw(BRIEF)
    assert got.block.block.body["html"] == HTML


def test_a_program_without_its_closing_tag_still_counts():
    raw = f"<design>d</design><html>{HTML}<probes>[]</probes><fallback>文字版</fallback>"
    assert step(Model(raw, raw), Eyes()).draw(BRIEF).block.block.body["html"] == HTML


def test_too_big_a_program_fails_closed_with_text():
    huge = "<p>" + "x" * MAX_APP_HTML + "</p>"
    m = Model(reply(huge), reply(huge))
    with pytest.raises(VisualFailed):
        step(m, Eyes()).draw(BRIEF)
    assert "太长" in m.requests[1].messages[-1].text


def test_a_model_that_does_not_answer_is_a_failed_picture_not_a_failed_page():
    with pytest.raises(VisualFailed):
        step(Model(ProviderTransientError("busy")), Eyes()).draw(BRIEF)


def test_it_sees_thumbnails_and_the_app_gets_the_chosen_picture():
    pick = '<images>[{"use":"c1","id":"male","alt":"一条雄鱼"}]</images>'
    m = Model('<design>d</design><search>["Poecilia reticulata male"]</search>', reply(extra=pick), reply(extra=pick))
    eyes = Eyes()
    got = step(m, eyes, Gallery()).draw(BRIEF)
    assert [p.kind for p in m.requests[1].messages[-1].parts] == ["image"]     # the thumbnail
    im = got.block.block.body["images"][0]
    assert (im["id"], im["license"], im["author"], im["asset"]) == ("male", "CC BY 4.0", "A", "net-male")
    assert eyes.pictures[0] == {"male": got.images[0].data_b64} and got.images[0].image_id == "net-male"
    stored = stored_images(got.block.block, {"net-male": "ia_1"})
    assert stored["body"]["images"][0]["asset"] == "ia_1"


def _library() -> dict[str, Any]:
    from evowiki.deep.contracts import LibraryComponent
    from tests.deep.fakes import component
    c: LibraryComponent = component("walk")
    return {c.component_id: c}


def test_the_author_may_keep_a_library_component_after_seeing_it():
    m = Model('<design>现成的够了</design><component>{"id":"walk"}</component>', "<keep/>")
    s = step(m, Eyes())
    s.components = _library()
    got = s.draw(BRIEF)
    assert got.component == "walk" and got.block is None and got.interactive.interactive_id == "net"
    assert "walk" in m.requests[0].messages[0].text                 # it was shown the catalog
    assert [p.kind for p in m.requests[1].messages[-1].parts] == ["image"]


def test_the_author_may_drop_the_component_and_write_its_own():
    m = Model('<design>d</design><component>{"id":"walk"}</component>', reply(), reply())
    s = step(m, Eyes())
    s.components = _library()
    got = s.draw(BRIEF)
    assert got.component is None and got.block.block.body["kind"] == "app"


def test_with_its_own_step_the_writers_blocks_are_practice_not_pictures():
    from evowiki.deep.tools import _draws_a_picture
    chart = {"kind": "box", "children": [{"kind": "slider"}, {"kind": "if", "then": {"kind": "plot"}}]}
    assert _draws_a_picture(chart) and not _draws_a_picture({"kind": "box", "children": [{"kind": "quiz"}]})


def test_the_writers_search_words_put_candidates_in_front_of_the_author():
    pick = '<images>[{"use":"c1","id":"male","alt":"一条雄鱼"}]</images>'
    m = Model(reply(extra=pick), reply(extra=pick))
    b = Brief(**{**BRIEF.__dict__, "photos": ("Poecilia reticulata male",)})
    got = step(m, Eyes(), Gallery()).draw(b)
    assert [p.kind for p in m.requests[0].messages[0].parts] == ["image"] and got.images[0].image_id == "net-male"


def _ws() -> Any:
    from evowiki.deep.contracts import RefinedSection, RefinePlan
    from evowiki.deep.tools import Workspace
    from evowiki.goals.contracts import SectionRole
    from tests.deep.fakes import PLAN
    ws = Workspace(None)
    ws.plan = RefinePlan(known="k", one_at_a_time=tuple(PLAN["one_at_a_time"]), spoken_answer=PLAN["spoken_answer"])
    ws.visual_first = True
    ws.sections["s1"] = RefinedSection(section_id="s1", heading="度", role=SectionRole.EXPLAIN, depth="quick", cells=(),
                                       cue="度是什么？", answer="边数", body="度是 3。\n{{block:net}}")
    return ws


def _tool(ws: Any, *answers: Any) -> tuple[Any, Model]:
    from evowiki.deep.tools import DrawVisualTool
    m = Model(*answers)
    ws.visual = step(m, Eyes())
    return DrawVisualTool(ws), m


def _call(t: Any, **a: Any) -> Any:
    return t._run(__import__("evowiki.deep.tools", fromlist=["DrawVisualArgs"]).DrawVisualArgs(**a))


def _draw(ws: Any) -> str:
    from evowiki.deep.tools import draw_waiting
    return draw_waiting(ws)


def test_a_section_is_drawn_once_whatever_id_is_used_and_a_change_goes_through_revise():
    ws = _ws()
    t, m = _tool(ws, reply(), reply(), reply(HTML.replace("r='10'", "r='18'")), reply(HTML.replace("r='10'", "r='18'")))
    assert _call(t, block_id="net", section_id="s1", invisible="i", draw="d", touch="t").error is None and not m.requests
    _draw(ws)
    calls = len(m.requests)
    again = _call(t, block_id="other", section_id="s1", invisible="i", draw="d", touch="t")
    assert again.error and "一段只画一次" in again.content and len(m.requests) == calls and "other" not in ws.blocks
    assert _call(t, block_id="other", section_id="s1", revise="圆大一点").error is None
    assert "<current>" in m.requests[calls].messages[0].text and "r='18'" in ws.blocks["net"].block.body["html"]


def test_the_visual_models_skip_stands():
    from evowiki.deep.visual import Decision
    ws = _ws()
    t, m = _tool(ws)
    ws.decisions["s1"] = Decision(False, why="只是过渡")
    assert "不用画" in _call(t, block_id="net", section_id="s1").content and not m.requests


def test_a_decided_picture_is_drawn_without_the_writer_asking_and_lands_at_the_end_of_its_section():
    from evowiki.deep.visual import Decision
    ws = _ws()
    ws.sections["s1"] = ws.sections["s1"].model_copy(update={"body": "度是 3。"})
    _, m = _tool(ws, reply(), reply())
    ws.decisions["s1"] = Decision(True, invisible="i", show="s", touch="t")
    told = _draw(ws)
    assert "viz-s1" in ws.blocks and ws.sections["s1"].body.endswith("{{block:viz-s1}}") and "网络" in told
    assert ws.drawn_sections["s1"] == "viz-s1" and _draw(ws) == "" and len(m.requests) == 2


def test_a_placeholder_the_writer_left_is_used():
    from evowiki.deep.visual import Decision
    ws = _ws()
    _tool(ws, reply(), reply())
    ws.decisions["s1"] = Decision(True, invisible="i", show="s", touch="t")
    _draw(ws)
    assert "net" in ws.blocks and ws.sections["s1"].body.count("{{block:") == 1


def test_pictures_are_drawn_side_by_side_on_the_finished_text():
    import threading

    from evowiki.deep.visual import Decision
    ws = _ws()
    ws.sections["s2"] = ws.sections["s1"].model_copy(update={"section_id": "s2", "body": "第二段"})
    ws.sections["s1"] = ws.sections["s1"].model_copy(update={"body": "第一段改过了"})
    both = threading.Barrier(2, timeout=20)

    class Together(Model):
        def converse(self, role: str, request: Any, *, account: Any = None) -> tuple[ChatTurn, Any]:
            both.wait()       # only passes when two requests are in flight at once
            self.requests.append(request)
            return ChatTurn(text=reply()), object()
    m = Together()
    ws.visual = step(m, Eyes())
    ws.decisions = {"s1": Decision(True, invisible="i", show="s", touch="t"), "s2": Decision(True, invisible="i", show="s", touch="t")}
    told = []
    ws.on_change = lambda w, sid: told.append(sid)
    _draw(ws)
    assert set(ws.drawn_sections) == {"s1", "s2"} and set(told) == {"s1", "s2"}
    assert any("第一段改过了" in r.messages[0].text for r in m.requests)


def test_a_failed_picture_leaves_the_text_and_its_placeholder_goes():
    from evowiki.deep.visual import Decision
    ws = _ws()
    _tool(ws, "没有", "还是没有")
    ws.decisions["s1"] = Decision(True, invisible="i", show="s", touch="t")
    told = _draw(ws)
    assert "没有画成" in told and "{{block:" not in ws.sections["s1"].body and "s1" in ws.failed_draws
    assert _draw(ws) == ""


def test_a_stop_while_drawing_ends_the_run():
    from evowiki.deep.visual import Decision
    from evowiki.llm.deadline import RequestCancelled
    ws = _ws()
    _tool(ws, RequestCancelled("stop"))
    ws.decisions["s1"] = Decision(True, invisible="i", show="s", touch="t")
    with pytest.raises(RequestCancelled):
        _draw(ws)


def test_a_second_try_carries_why_the_first_failed_so_the_model_is_really_asked_again():
    """A repeated request is answered from the gateway's call log: the same reply, the same failure, no retry at all."""
    ws = _ws()
    t, m = _tool(ws, "没有程序", "还是没有", reply(), reply())
    _call(t, block_id="net", section_id="s1", invisible="i", draw="d", touch="t")
    assert "没有画成" in _draw(ws)
    first = m.requests[0].messages[0].text
    assert _call(t, block_id="net", section_id="s1", invisible="i", draw="d", touch="t").error is None
    _draw(ws)
    again = m.requests[2].messages[0].text
    assert again != first and "没有 <html>" in again and "net" in ws.blocks


def test_a_failed_revision_tried_again_carries_the_reason_too():
    ws = _ws()
    t, m = _tool(ws, reply(), reply(), "坏的", "坏的", reply(), reply())
    _call(t, block_id="net", section_id="s1", invisible="i", draw="d", touch="t")
    _draw(ws)
    assert "没有改成" in _call(t, block_id="net", section_id="s1", revise="圆大一点").content
    first = m.requests[2].messages[0].text
    assert _call(t, block_id="net", section_id="s1", revise="圆大一点").error is None
    assert m.requests[4].messages[0].text != first and "没有 <html>" in m.requests[4].messages[0].text


def test_a_disagreement_comes_back_to_the_writer_and_a_corrected_text_is_checked_again():
    from evowiki.deep.tools import _visual_note
    ws = _ws()
    said = reply(extra="<conflict>正文说 3，算出 4</conflict>")
    t, _ = _tool(ws, said, said, "<conflict>还是 3 对 4</conflict>", "<ok/>")
    _call(t, block_id="net", section_id="s1", invisible="i", draw="d", touch="t")
    assert "正文说 3，算出 4" in _draw(ws) and ws.open_conflicts["s1"]
    assert "仍然对不上" in _visual_note(ws, ws.sections["s1"]) and "还是 3 对 4" in ws.open_conflicts["s1"]
    assert "对上了" in _visual_note(ws, ws.sections["s1"]) and not ws.open_conflicts


def test_a_picture_that_stays_in_conflict_leaves_the_page():
    ws = _ws()
    t, _ = _tool(ws, reply(), reply())
    _call(t, block_id="net", section_id="s1", invisible="i", draw="d", touch="t")
    _draw(ws)
    ws.open_conflicts["s1"] = "x"
    ws.drop_drawn("s1")
    assert "net" not in ws.blocks and "{{block:net}}" not in ws.sections["s1"].body and not ws.open_conflicts


def test_each_section_gets_one_decision():
    from evowiki.deep.tools import _visual_note
    ws = _ws()
    answer = "<decide>draw</decide><why>有结构</why><invisible>i</invisible><show>s</show><touch>t</touch><photos>[]</photos>"
    _, m = _tool(ws, answer)
    assert "配一张图" in _visual_note(ws, ws.sections["s1"]) and ws.decisions["s1"].show == "s"
    assert _visual_note(ws, ws.sections["s1"]) == "" and len(m.requests) == 1


def test_the_decision_sees_what_is_already_drawn_and_not_the_writers_placeholders():
    from evowiki.deep.tools import _visual_note
    from evowiki.deep.visual import Decision
    ws = _ws()
    ws.sections["s0"] = ws.sections["s1"].model_copy(update={"section_id": "s0", "heading": "前一段"})
    ws.decisions["s0"] = Decision(True, show="一张网络图")
    ws.sections["s1"] = ws.sections["s1"].model_copy(update={"body": "度是边数。\n{{block:net}}\n后面"})
    _, m = _tool(ws, "<decide>skip</decide><why>画过了</why>")
    _visual_note(ws, ws.sections["s1"])
    said = m.requests[0].messages[0].text
    assert "前一段：一张网络图" in said and "{{block" not in said and "后面" in said


def test_an_unreadable_decision_goes_back_once_with_what_was_wrong():
    from evowiki.deep.tools import _visual_note
    ws = _ws()
    _, m = _tool(ws, "", "<decide>skip</decide><why>字说得清</why>")
    _visual_note(ws, ws.sections["s1"])
    assert ws.decisions["s1"].why == "字说得清" and len(m.requests) == 2 and "<decide>" in m.requests[1].messages[-1].text


def test_a_section_without_a_decision_is_recorded_as_not_drawn():
    from evowiki.deep.tools import _visual_note
    ws = _ws()
    _tool(ws, "说不清", "还是说不清")
    note = _visual_note(ws, ws.sections["s1"])
    assert ws.decisions["s1"].draw is False and "没有得到判断" in note
    assert "s1" not in [sid for sid, d in ws.decisions.items() if d.draw]


def test_a_model_down_on_the_decision_is_recorded_the_same_way():
    from evowiki.deep.tools import _visual_note
    ws = _ws()
    _tool(ws, ProviderOutputError("boom"))
    assert "没有得到判断" in _visual_note(ws, ws.sections["s1"]) and ws.decisions["s1"].draw is False


def test_the_text_recheck_has_its_own_purpose_so_it_does_not_count_as_a_decision():
    ws = _ws()
    _, m = _tool(ws, "<ok/>")
    assert ws.visual.conflict_left("正文", "不一致", "图") == ""
    assert m.requests[0].purpose == "deep.visual.recheck"


# ---- the render check

def app(html: str, **node: Any) -> EvoUIBlock:
    return EvoUIBlock.model_validate({"type": "evoui", "title": "t", "question": "q", "state": {}, "derived": {},
                                      "body": {"kind": "app", "html": html, **node}, "fallback": "文字版"})


@pytest.fixture(scope="module")
def checker() -> PlaywrightChecker:
    pytest.importorskip("playwright")
    return PlaywrightChecker()


def test_a_click_on_an_svg_shape_counts_and_no_text_needs_to_change(checker):
    html = ("<svg viewBox='0 0 100 40' style='width:100%'><circle id='a' cx='20' cy='20' r='10' fill='red'/></svg><script>"
            "document.getElementById('a').addEventListener('click', e => e.target.setAttribute('r', 16));evowiki.ready()</script>")
    r = checker.check_block("svg", app(html))
    assert r.ok, r.detail


def test_the_authors_own_operation_counts_when_the_blind_clicks_miss(checker):
    html = ("<div id='t' style='height:60px;background:red'></div><button>x</button><script>let n=0;"
            "document.getElementById('t').addEventListener('dblclick',()=>document.getElementById('t').style.height=++n*10+60+'px');"
            "evowiki.save;window.go=()=>{document.getElementById('t').style.width='50%'};evowiki.ready()</script>"
            "<input id='i' oninput='go()'>")
    r = checker.check_block("touch", app(html), None, [AppProbe(target="#i", action="input", value="1")])
    assert r.ok, r.detail


def test_a_program_that_redraws_everything_is_still_clicked_through(checker):
    html = ("<div id='r'></div><script>let n=0;function d(){document.getElementById('r').innerHTML="
            "'<button>a</button><button>b</button><p>'+(n>0?'x':'')+'</p>';"
            "const[a,b]=document.querySelectorAll('button');a.onclick=d;b.onclick=()=>{n++;d()}}d();evowiki.ready()</script>")
    r = checker.check_block("redraw", app(html))
    assert r.ok, r.detail


def test_a_handler_on_the_drawing_makes_its_shapes_touchable(checker):
    html = ("<svg id='s' viewBox='0 0 100 40' style='width:100%'><rect x='5' y='5' width='30' height='30'/></svg><script>"
            "document.getElementById('s').onclick = e => e.target.setAttribute('fill', 'blue');evowiki.ready()</script>")
    r = checker.check_block("deleg", app(html))
    assert r.ok, r.detail


def test_a_picture_nothing_can_touch_still_fails(checker):
    r = checker.check_block("still", app("<svg viewBox='0 0 9 9' width='80'><circle r='3'/></svg><script>evowiki.ready()</script>"))
    assert not r.ok and "没有能动手的东西" in r.detail


def test_content_taller_than_a_fixed_frame_is_clipped(checker):
    html = ("<button onclick='this.textContent=1'>b</button><div style='height:1500px'>长</div>"
            "<script>evowiki.ready()</script>")
    r = checker.check_block("tall", app(html))
    assert not r.ok and "下面的部分看不到" in r.detail


def test_the_frame_follows_its_content_so_a_medium_one_is_not_clipped(checker):
    html = ("<button onclick='this.textContent=1'>b</button><div style='height:700px'>长</div>"
            "<script>evowiki.ready()</script>")
    r = checker.check_block("medium", app(html, height=240))
    assert r.ok, r.detail


def test_an_svg_label_outside_its_drawing_is_clipped(checker):
    html = ("<svg viewBox='0 0 100 40' style='width:100%'><text x='90' y='20' font-size='20'>很长很长的一句标注</text>"
            "<circle id='a' onclick='this.setAttribute(\"r\",9)' cx='20' cy='20' r='10'/></svg><script>evowiki.ready()</script>")
    r = checker.check_block("label", app(html))
    assert not r.ok and "跑出了画布" in r.detail


def _webp() -> str:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 30, 30)).save(buf, format="WEBP")
    return base64.b64encode(buf.getvalue()).decode()


def test_evowiki_image_gives_the_app_a_picture_through_the_host_and_it_looks(checker):
    html = ("<img id='i' alt='' style='width:100px'><button id='b' onclick='this.textContent=1'>b</button><script>"
            "evowiki.image('male').then(u => { const i = document.getElementById('i'); i.onload = () => evowiki.ready(); i.src = u; })"
            "</script>")
    b = app(html, images=[{"id": "male", "title": "Guppy", "license": "CC BY 4.0", "source": "Wikimedia Commons",
                           "source_url": "https://x.example/g"}])
    pictures = {"male": _webp()}
    assert checker.check_block("pic", b, pictures).ok
    assert not checker.check_block("nopic", b).ok                 # no picture: the promise rejects, it never gets ready
    late = ("<img id='i' alt='' style='width:100px'><button onclick='this.textContent=1'>b</button><p id='p'>等</p><script>evowiki.ready();"
            "evowiki.image('male').then(u => { document.getElementById('p').textContent = '到了'; })</script>")
    assert "到了" in "".join(checker.check_block("late", app(late, images=b.body["images"]), pictures).readings)  # asked before the first load is over
    seen = checker.look_block("pic", b, [AppProbe(target="#b", see="b 变成 1")], pictures)
    assert [n for n, _ in seen.shots] == ["刚打开", "操作 1 之后：b 变成 1", "最窄的窗格，刚打开", "深色模式，刚打开"]
    assert "画面变了" in seen.notes[0]

def test_a_look_that_keeps_the_picture_costs_no_repair():
    m = Model(reply(), "<keep/>")
    got = step(m, Eyes()).draw(BRIEF)
    assert got.block.block.body["html"] == HTML and len(m.requests) == 2
