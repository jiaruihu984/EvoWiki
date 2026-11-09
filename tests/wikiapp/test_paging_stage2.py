"""On-demand decomposition stage 2 (the product specification
§2.7, §6 第 2 阶段): the path model may merge two points into one page and titles it itself; a
page's targets are what it teaches (its K cells and checks), its other concepts are only
mentioned; each check is evidence for its own cell only. Scripted LLM only."""

import pytest

from evowiki.belief.aspects import Declare, Observe
from evowiki.goals.contracts import (
    CheckItem,
    OutlineSection,
    PageOutline,
    SectionRole,
)
from evowiki.goals.fold import Page, states_for
from evowiki.wikiapp.events import PageSaved, WikiEvent
from evowiki.wikiapp.kchange import answer_check
from evowiki.wikiapp.paths import next_path_page, path_out
from evowiki.wikiapp.repo import Commit
from tests.wikiapp import test_paths
from tests.wikiapp.test_goal import NOW
from tests.wikiapp.test_paths import ASPECTS4, BIG_PATH

env = test_paths.env          # the fixture: MemoryRepo + scripted LLM

TITLES = ["样本与零假设", "区间和统计量一起看", "p 值怎么读"]
PATH = {"pages": [{**p, "title": t} for p, t in zip(BIG_PATH["pages"], TITLES, strict=True)]}


def big_goal(env):
    env[1].push("goals.path", PATH)        # served before big_goal's own BIG_PATH
    res, _ = test_paths.big_goal(env)
    return res


def _check(cell: str) -> CheckItem:
    return CheckItem(cell=cell, stem=f"{cell}?", options=("对", "错", "不一定"), answer=0,
                     verified=True)


def merged_page(env):
    """Page 2 (c3 + c5 merged by the model) created and written: one explain and one check per
    taught point, and a recap of the mentioned prerequisite c2."""
    repo, _, ids = env
    res = big_goal(env)
    next_path_page(repo, "u1", res.path_id, now=NOW, new_id=ids)
    pid = next_path_page(repo, "u1", res.path_id, now=NOW, new_id=ids).page_id
    snap = repo.load("u1")
    rec = snap.state.pages[pid]
    o = PageOutline(page_id=pid, goal=rec.goal, concepts=rec.concepts, targets=rec.targets,
                    sections=(
                        OutlineSection(id="s1", heading="回顾", role=SectionRole.RECAP,
                                       cells=("c2.term",)),
                        OutlineSection(id="s2", heading="区间", role=SectionRole.EXPLAIN,
                                       cells=("c3.term",)),
                        OutlineSection(id="s3", heading="统计量", role=SectionRole.EXPLAIN,
                                       cells=("c5.term",)),
                        OutlineSection(id="s4", heading="确认区间", role=SectionRole.CHECK,
                                       cells=("c3.term",), check=_check("c3.term")),
                        OutlineSection(id="s5", heading="确认统计量", role=SectionRole.CHECK,
                                       cells=("c5.term",), check=_check("c5.term")),
                    ))
    page = Page(outline=o, states=states_for(o), texts={}, snapshot={})
    repo.commit(snap, Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=NOW, body=PageSaved(
        record=rec.model_copy(update={"page": page}), action="organized"))]))
    return res, pid


def test_two_points_merged_into_one_page_are_stored_with_the_models_title(env):
    repo, _, _ = env
    res, pid = merged_page(env)
    st = repo.load("u1").state
    path = st.paths[res.path_id].path
    assert [p.title for p in path.pages] == TITLES     # one-concept page too: not the node name
    rec = st.pages[pid]
    assert rec.targets == ("c3", "c5")                  # taught: both points
    assert set(rec.concepts) - set(rec.targets) == {"c2", "c4"}   # only mentioned
    checks = {s.id: s.check.cell for s in rec.page.outline.sections if s.check}
    assert checks == {"s4": "c3.term", "s5": "c5.term"}           # one cell each


def test_answering_one_check_updates_only_its_cell(env):
    repo, _, ids = env
    _, pid = merged_page(env)
    before = repo.load("u1").kview(NOW)
    seen = len(repo.aspect_log("u1"))
    out = answer_check(repo, None, "u1", pid, "s4", 0, now=NOW, new_id=ids)
    assert out.correct is True
    new = repo.aspect_log("u1")[seen:]
    # the observation and its note: both on the answered cell
    assert {e.observation.cell if isinstance(e, Observe) else e.cell for e in new} == {"c3.term"}
    after = repo.load("u1").kview(NOW)
    assert after.cell("c3.term").direct > before.cell("c3.term").direct
    for other in ("c5.term", "c2.term", "c4.term", "c3.purpose"):
        assert after.cell(other) == before.cell(other)


@pytest.mark.parametrize(("known", "learned"), [
    (("c3", "c5"), True),            # every taught cell known: the page is learned, c2/c4 not needed
    (("c2", "c4", "c3"), False),     # mentioned + one taught point: c5 still open
])
def test_mentioned_concepts_are_not_the_pages_cells(env, known, learned):
    repo, _, _ = env
    res, _ = merged_page(env)
    snap = repo.load("u1")
    repo.commit(snap, Commit(aspect=[
        Declare(event_id=f"decl_{c}_{a}", ts=NOW, cell=f"{c}.{a}", value="know")
        for c in known for a in ASPECTS4]))
    after = repo.load("u1")
    view = path_out(after.state, after.k, res.path_id, NOW)
    assert (view.pages[1].status == "done") is learned
    if learned:   # page learned does not make the mentioned prerequisites known
        kv = after.kview(NOW)
        assert all(kv.cell(f"{c}.{a}").direct == 0 and not kv.cell(f"{c}.{a}").declared
                   for c in ("c2", "c4") for a in ASPECTS4)
