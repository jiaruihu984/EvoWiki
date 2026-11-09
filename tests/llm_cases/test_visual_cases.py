"""deep.visual, deep.visual.decide and deep.scene (the visualization step behind draw_visual,
`deep.visual.VisualStep` / `deep.scene.SceneStep`) offline cases. Scripted gateways and fake render
checkers only (the fakes of tests/deep/test_visual.py and tests/deep/test_scene.py).

What each call carries and what its answer becomes: a drawing call (`deep.visual`) is told the page
title, goal, what the learner knows, the section and the writer's three answers (what is invisible,
what to draw, what to touch), and its program becomes an `app` block; the decision call
(`deep.visual.decide`) is told the section, the sections already decided to draw and the writer's
hints, and answers draw / skip with the three answers, an unreadable answer decides nothing; the
scene call (`deep.scene`) gets the same brief and its markup is compiled into a block, a bad reply
goes back once with the reason."""

from evowiki.deep.scene import SceneStep
from evowiki.deep.visual import DECIDE_PURPOSE, PURPOSE
from tests.deep.test_scene import BRIEF as SCENE_BRIEF
from tests.deep.test_scene import GOOD, Checker, Gw
from tests.deep.test_visual import BRIEF, HTML, Eyes, Model, reply, step

PURPOSES = ("deep.visual", "deep.visual.decide", "deep.visual.recheck", "deep.scene")


def _text(request) -> str:
    return "".join(m.text or "" for m in request.messages)


def test_the_drawing_call_carries_the_page_the_section_and_the_writers_three_answers():
    assert PURPOSE == "deep.visual"
    m = Model("说不清", "还是说不清", reply(), "<keep/>")   # the markup route fails, a program is drawn, the look keeps it
    got = step(m, Eyes()).draw(BRIEF)
    first = next(r for r in m.requests if r.purpose == "deep.visual")
    text = _text(first)
    for needed in ("度分布", "懂度", "刚开始学", BRIEF.heading, BRIEF.body,
                   BRIEF.invisible, BRIEF.draw, BRIEF.touch):
        assert needed in text
    assert got.block.block.body["kind"] == "app" and got.block.block.body["html"] == HTML


def test_the_decision_call_carries_the_section_the_earlier_picks_and_the_hints():
    assert DECIDE_PURPOSE == "deep.visual.decide"
    answer = ("<decide>draw</decide><why>有结构</why><invisible>边数</invisible><show>网络</show>"
              "<touch>点节点</touch><photos>[\"guppy\"]</photos>")
    m = Model(answer)
    d = step(m, Eyes()).decide("度", "度是连着的边数。", ("写页面的人想画网络",), ("s0 度的定义",))
    (req,) = m.requests
    assert req.purpose == "deep.visual.decide"
    text = _text(req)
    for needed in ("度分布", "懂度", "刚开始学", "度是连着的边数。", "s0 度的定义", "写页面的人想画网络"):
        assert needed in text
    assert d is not None and d.draw and (d.invisible, d.show, d.touch, d.photos) == (
        "边数", "网络", "点节点", ("guppy",))


def test_the_decision_can_be_skip_and_an_unreadable_one_decides_nothing():
    skip = step(Model("<decide>skip</decide><why>只是过渡</why>"), Eyes()).decide("过渡", "接下来。", (), ())
    assert skip is not None and not skip.draw and skip.why == "只是过渡"
    d = step(Model("说不清", "还是说不清"), Eyes()).decide("过渡", "接下来。", (), ())
    assert d is not None and not d.draw and d.unanswered


def test_the_scene_call_carries_the_brief_and_its_markup_becomes_a_block():
    gw = Gw([GOOD])
    got = SceneStep(gw=gw, account=None, checker=Checker(), page_id="p", title="度分布", goal="懂度",
                    known="刚开始学").draw(SCENE_BRIEF)
    request = gw.last
    assert request.purpose == "deep.scene"
    text = _text(request)
    for needed in ("度分布", "懂度", "刚开始学", SCENE_BRIEF.heading, SCENE_BRIEF.body,
                   SCENE_BRIEF.invisible, SCENE_BRIEF.draw, SCENE_BRIEF.touch):
        assert needed in text
    assert got.block.block.title == "短" and got.block.block.body["kind"] == "svg"
