"""「忘掉我是大三学生」: the line goes on 确认 and comes back on 撤销 (bot.executors_profile)."""

from types import SimpleNamespace

import pytest

from evowiki.bot.actions import ActionRefused
from evowiki.bot.executors_profile import ProfileForgetExecutor
from evowiki.profile.note import FactMeta, Profile, line_key


class _Store:
    def __init__(self, p):
        self.p = p

    def update_in(self, conn, user_id, fn):
        self.p = fn(self.p)


def test_the_line_goes_and_comes_back_with_its_provenance():
    meta = FactMeta(provenance="explicit_current", origin_ref="m1", evidence="我是大三", action_id="a0")
    store = _Store(Profile(owned={"我是谁": ("大三学生", "会 C")}, fact_meta={line_key("我是谁", "大三学生"): meta}))
    ex = ProfileForgetExecutor(store)
    row = SimpleNamespace(content={"section": "我是谁", "text": "大三学生"}, result=None)
    row.result = ex.execute("u", row, lambda conn: None)
    assert store.p.owned["我是谁"] == ("会 C",) and not store.p.fact_meta
    ex.undo("u", row)
    assert set(store.p.owned["我是谁"]) == {"大三学生", "会 C"}
    assert store.p.fact_meta[line_key("我是谁", "大三学生")].evidence == "我是大三"


def test_a_line_changed_since_is_refused_at_its_confirm():
    ex = ProfileForgetExecutor(_Store(Profile(owned={"我是谁": ("研一学生",)})))
    with pytest.raises(ActionRefused):
        ex.execute("u", SimpleNamespace(content={"section": "我是谁", "text": "大三学生"}, result=None), lambda c: None)
