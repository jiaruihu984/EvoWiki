"""goals.rough_typed: what he typed over the 摸底 grid becomes the grid's marks. Scripted model; a number the reply makes up is
no mark, a model failure is no marks. live/test_live_rough_typed.py is the live side."""

from evowiki.assistant.tools.rough import TYPED_PURPOSE, typed_marks
from evowiki.wikiapp.llm import CountingGateway
from tests.wikiapp.script import Script

BLOCKS = [("c1", "习性"), ("c2", "鱼缸与水质"), ("c3", "日常照顾")]


def _gw(*outputs):
    s = Script()
    s.push(TYPED_PURPOSE, *outputs)
    return CountingGateway(s.gateway())


def test_goals_rough_typed_marks_by_number():
    marks = typed_marks(_gw({"marks": [{"n": 2, "mark": "forgot"}, {"n": 1, "mark": "dont"}]}), "u", "水质忘了，习性没碰过", BLOCKS)
    assert marks == {"c2": "forgot", "c1": "dont"}


def test_a_made_up_number_is_no_mark():
    assert typed_marks(_gw({"marks": [{"n": 9, "mark": "know"}]}), "u", "都会", BLOCKS) == {}


def test_an_unusable_reply_is_no_marks():
    assert typed_marks(_gw("不是 JSON"), "u", "都会", BLOCKS) == {}
