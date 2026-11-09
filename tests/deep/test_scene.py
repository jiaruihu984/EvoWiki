"""The declarative route: a reply goes through markup, validator and render check; a failure goes back once, then the
program route takes over."""

import base64
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from evowiki.deep.contracts import BlockLook, RenderCheck
from evowiki.deep.scene import SceneStep, parse_reply
from evowiki.deep.visual import Brief, VisualFailed, VisualStep
from evowiki.llm.chat import ImagesNotSupported

BRIEF = Brief(block_id="v1", section_id="s1", heading="标题", body="正文", invisible="看不见的", draw="画", touch="动")
GOOD = ('<design>想法</design><block title="短" question="为什么？">{@state n = 1}'
        '<svg viewBox="0 0 640 100"><rect x={n} width=9 height=9 onClick={n = n + 1}/></svg></block><fallback>文字版</fallback>')


@dataclass
class Gw:
    replies: list[str]
    asked: int = 0

    def converse(self, role, request, account=None):
        self.asked += 1
        self.last = request
        call = SimpleNamespace(raw_output="", cost=0, latency=0)
        return SimpleNamespace(text=self.replies.pop(0), echo=None), call


class Checker:
    def __init__(self, ok=True, shots=(), fail_from=None):
        self.ok, self.shots, self.fail_from = ok, shots, fail_from

    def check_block(self, block_id, block, images=None, touch=()):
        self.seen = getattr(self, "seen", 0) + 1
        ok = self.ok if self.fail_from is None or self.seen < self.fail_from else False
        return RenderCheck(interactive_id=block_id, ok=ok, detail="" if ok else "没变化")

    def look_block(self, block_id, block, probes, images=None):
        return BlockLook(shots=tuple((n, base64.b64encode(b"x").decode()) for n in self.shots))


def step(gw, ok=True):
    return SceneStep(gw=gw, account=None, checker=Checker(ok), page_id="p", title="T", goal="G", known="K")


def test_a_reply_becomes_a_block():
    b = parse_reply(GOOD, BRIEF)
    assert b["title"] == "短" and b["question"] == "为什么？" and b["fallback"] == "文字版" and b["body"]["kind"] == "svg"


def test_draws_on_the_first_reply():
    gw = Gw([GOOD])
    d = step(gw).draw(BRIEF)
    assert gw.asked == 1 and d.block is not None and d.design == "想法" and d.notes == ()


def test_a_bad_reply_goes_back_once_with_the_reason():
    gw = Gw(['<block title="t" question="q"><nope/></block>', GOOD])
    d = step(gw).draw(BRIEF)
    assert gw.asked == 2 and "nope" in gw.last.messages[-1].text and "nope" in d.notes[0]


def test_two_bad_replies_fail_closed():
    gw = Gw(["没有块", "还是没有"])
    with pytest.raises(VisualFailed, match="block"):
        step(gw).draw(BRIEF)


def test_a_check_that_fails_counts_too():
    with pytest.raises(VisualFailed, match="没变化"):
        step(Gw([GOOD, GOOD]), ok=False).draw(BRIEF)


def _visual(gw, **kw):
    return VisualStep(gw=gw, account=None, checker=Checker(), images=None, page_id="p", title="T", goal="G", known="K", **kw)


def test_the_program_route_takes_over_when_the_scene_fails(monkeypatch):
    v = _visual(Gw(["无", "无"]))
    monkeypatch.setattr(VisualStep, "_draw", lambda self, b: "program")
    assert v.draw(BRIEF) == "program"


def test_a_section_with_photos_goes_straight_to_a_program(monkeypatch):
    gw = Gw([])
    monkeypatch.setattr(VisualStep, "_draw", lambda self, b: "program")
    assert _visual(gw).draw(Brief(**{**BRIEF.__dict__, "photos": ("q",)})) == "program"
    assert gw.asked == 0


def test_a_block_is_revised_as_a_block():
    gw = Gw([GOOD, GOOD])
    first = step(gw).draw(BRIEF)
    got = _visual(gw).revise(BRIEF, first.block, "加粗", {})
    assert got.block is not None and "加粗" in gw.last.messages[0].text


def _looking(gw, **kw):
    return SceneStep(gw=gw, account=None, checker=Checker(shots=("刚打开",), **kw), page_id="p", title="T", goal="G", known="K")


REVISED = GOOD.replace("<design>想法</design>", "<review>字压线</review><design>改了</design>")


def test_the_author_sees_its_picture_and_may_keep_it():
    gw = Gw([GOOD, "<review>够了</review><keep/>"])
    d = _looking(gw).draw(BRIEF)
    assert gw.asked == 2 and d.design == "想法" and d.review == "够了" and gw.last.messages[-1].parts[0].name == "刚打开"


def test_the_author_sees_its_picture_and_may_revise_it():
    gw = Gw([GOOD, REVISED])
    d = _looking(gw).draw(BRIEF)
    assert d.design == "改了" and d.review == "字压线"


def test_a_revision_that_fails_goes_back_once_then_what_passed_stands():
    gw = Gw([GOOD, REVISED, REVISED])
    d = _looking(gw, fail_from=2).draw(BRIEF)
    assert gw.asked == 3 and "没变化" in gw.last.messages[-1].text and d.design == "想法" and d.review == "字压线"


def test_the_repaired_revision_counts():
    gw = Gw([GOOD, "<review>字压线</review><block title='x'><nope/></block>", REVISED])
    d = _looking(gw).draw(BRIEF)
    assert gw.asked == 3 and d.design == "改了"


def test_a_model_without_eyes_keeps_what_passed():
    class Blind(Gw):
        def converse(self, role, request, account=None):
            if request.messages[-1].parts:
                raise ImagesNotSupported("no eyes")
            return super().converse(role, request, account)
    assert _looking(Blind([GOOD])).draw(BRIEF).design == "想法"


def test_blocks_are_the_default_route():
    gw = Gw([GOOD])
    assert _visual(gw).draw(BRIEF).block.block.body["kind"] == "svg" and gw.last.purpose == "deep.scene"


def test_a_disagreement_with_the_text_is_reported_not_swallowed():
    d = step(Gw([GOOD + "<conflict>正文说 3 倍，算出来 4 倍</conflict>"])).draw(BRIEF)
    assert d.conflict == "正文说 3 倍，算出来 4 倍"


def test_a_try_after_a_failure_says_why_it_failed():
    gw = Gw([GOOD])
    step(gw).draw(Brief(**{**BRIEF.__dict__, "tried": "文字跑出了画布：↓"}))
    assert "文字跑出了画布：↓" in gw.last.messages[0].text


def test_visual_passes_frozen_english_language_to_scene_repairs_without_extra_calls():
    class Recorded(Gw):
        def converse(self, role, request, account=None):
            self.requests = [*getattr(self, "requests", []), request]
            return super().converse(role, request, account)

    english = GOOD.replace("短", "Equal fractions").replace("为什么？", "Why are they equal?").replace("文字版", "The two fractions represent the same amount.")
    gw = Recorded(["<block><nope/></block>", english])
    visual = _visual(gw, language="en")
    scene = visual._scene()
    assert scene.language == "en"
    drawn = scene.draw(BRIEF)
    assert drawn.block.block.title == "Equal fractions"
    assert gw.asked == 2
    for request in gw.requests:
        assert "All learner-facing text must be written in English" in request.system
        assert "All learner-facing text must be written in English" in request.messages[-1].text
        assert "HTML/EvoUI" in request.messages[-1].text
    assert "nope" in gw.requests[-1].messages[-1].text


def test_visual_decision_and_program_requests_keep_the_same_language():
    from evowiki.llm.chat import ChatMessage

    gw = Gw(["<decide>skip</decide>", "<fallback>English text</fallback>"])
    visual = _visual(gw, language="en")
    visual.decide("An English heading", "English body", (), ())
    assert "written in English" in gw.last.system
    assert "written in English" in gw.last.messages[-1].text
    original = ChatMessage(role="user", text="原始开发说明")
    visual._ask([original])
    assert "written in English" in gw.last.system
    assert "written in English" in gw.last.messages[-1].text
    assert original.text == "原始开发说明"
