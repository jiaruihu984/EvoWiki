"""Local edits stay local (skill design A7 item 5, user ruling 2026-10-28): 「把第三段讲简单一点」 is a
writer round scoped to that one section. The real page writer over a scripted gateway: a write of a
section outside the scope is refused, and the draft holds only the scoped section — no replan, no
whole-page rewrite. Scripted gateways only."""

import pytest

from evowiki.deep.writer import PageWriter
from evowiki.wikiapp.refine_apply import refine_input
from evowiki.wikiapp.refine_spec import WikiSpecSource
from evowiki.wikiapp.repo import MemoryRepo
from tests.deep.fakes import DeepScript, FakeChecker, finish, library, section, turn
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_page import organized

SIMPLER = "把第三段讲简单一点"


@pytest.fixture
def env():
    return MemoryRepo(["u1"]), Script(), Ids()


def test_one_section_edit_drafts_only_that_section(env):
    pid = organized(env)
    snap = env[0].load("u1")
    page = snap.state.pages[pid].page
    third = page.outline.sections[2].id
    inp = refine_input(snap, pid, NOW, WikiSpecSource(env[0]), scope=(third,),
                       mode="feedback", instruction=SIMPLER)
    ds = DeepScript().plan().write(
        turn(section("s2", heading="z 分数怎么读"),                   # outside the scope: refused
             section(third, role="recap", depth="quick", heading="标准差提醒",
                     body="标准差就是数据离平均值大概有多远。")),
        turn(finish()))
    res = PageWriter(llm=ds.llm(), checker=FakeChecker(), library=library()).write(inp)
    assert [s.section_id for s in res.outcome.draft.sections] == [third]
    refused = [m.text for r in ds.requests if r.purpose == "deep.write" for m in r.messages
               if m.role == "tool" and "不在这次精修的范围内" in m.text]
    assert refused and "s2" in refused[0]
    first = next(r for r in ds.requests if r.purpose == "deep.write").messages[0].text
    assert SIMPLER in first
    assert ds.pending() == {}
