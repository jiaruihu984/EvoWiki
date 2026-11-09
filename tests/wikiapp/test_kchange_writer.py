"""A K change that leaves a conflict goes to the page writer only (skill design A7 item 5, user
ruling 2026-10-28; the goals.patch model step is gone): the K-change pass makes no model call, the
section that can fix the conflict waits for the writer (`patch_pending`), and the writer's scoped
round (trigger "patch") rewrites that one section with the conflict as its instruction — the other
sections keep their text, and the conflict is cleared once fixed. Real DeepService and page writer
over a scripted gateway; no network."""

import pytest

from evowiki.belief.aspects import Observe
from evowiki.deep.contracts import RefineScope
from evowiki.deep.store import MemoryAssetStore, MemoryJobStore
from evowiki.goals.contracts import Observation, SectionRole
from evowiki.wikiapp.kchange import k_changed
from evowiki.wikiapp.kwrite import apply_in_memory
from evowiki.wikiapp.refine_service import DeepService
from evowiki.wikiapp.refine_spec import WikiSpecSource
from evowiki.wikiapp.repo import Commit, MemoryRepo
from evowiki.wikiapp.stale import patch_pending
from tests.deep.fakes import DeepScript, FakeChecker, finish, library, section, turn
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_page import LINE, gw_of, open_, organized


@pytest.fixture
def env():
    return MemoryRepo(["u1"]), Script(), Ids()


def _page(env, pid):
    return env[0].load("u1").state.pages[pid].page


def _missed(env, *, declared_too=True):
    """A LINE page (s3 「标准差提醒」 one line on c2.term), opened, then a miss on c2.term; `declared_too`:
    with page feedback that asks for the change."""
    repo, s, ids = env
    pid = organized(env, LINE)
    open_(env, pid)
    outline = _page(env, pid).outline
    calls = s.calls()
    snap = repo.load("u1")
    aspect = [Observe(event_id=ids("aev"), ts=NOW, observation=Observation(
        cell="c2.term", polarity="-", strength=1.0, kind="check", evidence_ref="check:x:y"))]
    out = k_changed(snap, apply_in_memory(snap, aspect), pid, gw_of(env), skip=None, now=NOW,
                    new_id=ids, declared_too=declared_too)
    repo.commit(snap, Commit(aspect=aspect, wiki=out.wiki))
    assert s.calls() == calls and out.change.llm_calls == 0          # no model call of its own
    assert _page(env, pid).outline == outline                         # the outline is the writer's
    return pid, out


def test_a_wrong_answer_rewrites_nothing_and_leaves_nothing_waiting(env):
    pid, out = _missed(env, declared_too=False)
    assert out.change.rewritten == [] and out.change.inserted == []
    assert [c.kind for c in out.change.conflicts] == ["line_weak"]
    assert patch_pending(env[0].load("u1").state, pid) == ()


def test_page_feedback_hands_the_conflicting_section_to_the_writer(env):
    pid, out = _missed(env)
    assert out.change.rewritten == ["s3"] and out.change.inserted == []
    page = _page(env, pid)
    assert next(s for s in page.outline.sections if s.id == "s3").role is SectionRole.LINE
    assert [c.kind for c in out.change.conflicts] == ["line_weak"]
    assert patch_pending(env[0].load("u1").state, pid) == ("s3",)


def test_the_writer_rewrites_only_that_section_and_clears_the_conflict(env):
    pid, _ = _missed(env)
    before = _page(env, pid)
    ds = DeepScript().plan().write(
        turn(section("s3", role="recap", depth="quick", heading="标准差提醒",
                     cells=["c2.term"], body="标准差：数据离平均值大概有多远。")),
        turn(finish()))
    svc = DeepService(repo=env[0], jobs=MemoryJobStore(), assets=MemoryAssetStore(),
                      specs=WikiSpecSource(env[0]), checker=FakeChecker(), llm=ds.llm(),
                      clock=lambda: NOW, new_id=env[2], library=library())
    job = svc.start("u1", pid, "patch")
    res = svc.run("u1", job, RefineScope(page_id=pid, sections=("s3",)))
    assert res.job.stage == "done" and res.job.changed_sections == ("s3",)
    first = next(r for r in ds.requests if r.purpose == "deep.write").messages[0].text
    assert "标准差" in first and "只改写这次范围内的段" in first          # the conflict, as instruction
    after = _page(env, pid)
    assert {sid: t for sid, t in after.texts.items() if sid != "s3"} == {
        sid: t for sid, t in before.texts.items() if sid != "s3"}
    assert [s.id for s in after.outline.sections] == [s.id for s in before.outline.sections]
    assert next(s for s in after.outline.sections if s.id == "s3").role is SectionRole.RECAP
    assert after.unresolved_conflicts == () and after.conflicts == ()
    assert patch_pending(env[0].load("u1").state, pid) == ()
