"""R4: a concept id with no recorded name is shown as 「未命名概念」 (never the raw id) and logged
as a warning with the user id and the concept id."""

import logging

import pytest

from evowiki.belief.aspects import AspectKView, DefineConcept
from evowiki.goals.contracts import Aspect
from evowiki.wiki.catalog import UNNAMED_CONCEPT
from evowiki.wikiapp.kwrite import page_spec
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.repo import Commit, MemoryRepo
from evowiki.wikiapp.views import catalog_out, cell_out, k_grid_out
from tests.wikiapp.flat_goal import flat_goal
from tests.wikiapp.goal_script import auto_probe
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids, goal1
from tests.wikiapp.test_page import organized, page_on


@pytest.fixture
def env():
    return MemoryRepo(["u1"]), Script(), Ids()


def state(env):
    return env[0].load("u1").state


def warned(caplog, cid: str) -> bool:
    return any(r.levelno == logging.WARNING and "u1" in r.getMessage() and cid in r.getMessage()
               for r in caplog.records)


def define(env, cid: str) -> None:
    repo, _, ids = env
    snap = repo.load("u1")
    repo.commit(snap, Commit(aspect=[DefineConcept(event_id=ids("aev"), ts=NOW, concept_id=cid,
                                                   aspects=(Aspect.TERM,))]))


def test_unnamed_assumption_holds_in_the_fixture(env):
    organized(env)
    assert "c9" not in state(env).names


def test_named_concepts_log_nothing(env, caplog):
    repo, _, _ = env
    organized(env)
    snap = repo.load("u1")
    caplog.set_level(logging.WARNING)
    catalog_out(snap.state, snap.k, NOW, user_id="u1")
    k_grid_out(snap.state, snap.k, NOW, user_id="u1")
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_k_grid_and_cell_without_name(env, caplog):
    repo, _, _ = env
    organized(env)
    define(env, "c9")
    snap = repo.load("u1")
    caplog.set_level(logging.WARNING)
    grid = k_grid_out(snap.state, snap.k, NOW, user_id="u1")
    assert [(c.id, c.name) for c in grid.concepts][-1] == ("c9", UNNAMED_CONCEPT)
    cell = cell_out(snap.state, AspectKView(snap.k, NOW), "c9.term", user_id="u1")
    assert (cell.concept_id, cell.concept_name) == ("c9", UNNAMED_CONCEPT)
    assert warned(caplog, "c9")


def test_page_spec_never_feeds_a_raw_id_as_a_name(env, caplog):
    repo, _, ids = env
    organized(env)
    pid = page_on(repo, ids, concepts=("c9",), title="无关")
    snap = repo.load("u1")
    caplog.set_level(logging.WARNING)
    [c] = page_spec(snap.state, snap.k, pid, user_id="u1").concepts
    assert (c.id, c.name) == ("c9", UNNAMED_CONCEPT)
    assert warned(caplog, "c9")


def test_page_spec_carries_the_language(env):
    repo, _, ids = env
    organized(env)
    pid = page_on(repo, ids, concepts=("c9",), title="无关")
    snap = repo.load("u1")
    assert page_spec(snap.state, snap.k, pid, user_id="u1").language == "zh"
    en = snap.state.model_copy(update={"language": "en"})
    assert page_spec(en, snap.k, pid, user_id="u1").language == "en"


def test_second_goal_new_prereq_only_concept_is_named(env):
    """The real-run c7 path: a second goal whose new non-target concept (no prereqs of its own)
    is reached only as the target's prereq. It must be named."""
    repo, s, ids = env
    goal1(env)
    s.push("goals.intake", {"title": "PCA", "concepts": [
        {"id": "n1", "name": "主成分分析", "target": True, "prereqs": ["c1", "n2", "n3"],
         "aspects": ["procedure"]},
        {"id": "n2", "name": "协方差矩阵", "prereqs": ["c2"], "aspects": ["term"]},
        {"id": "n3", "name": "特征值与特征向量", "prereqs": [], "aspects": ["term"]}]})
    auto_probe(s)                     # the probe writes whatever item the diagnosis picks
    res = flat_goal(repo, CountingGateway(s.gateway()), "u1", "我想学 PCA", "", now=NOW,
                      new_id=ids)
    new = [c.id for c in res.draft.concepts if not c.reused]
    snap = repo.load("u1")
    assert {c: snap.state.names[c].name for c in new} == {
        "c3": "主成分分析", "c4": "协方差矩阵", "c5": "特征值与特征向量"}


def test_catalog_keeps_a_page_with_no_node(env):
    """An assistant page on a free topic has no node (targets ()): the catalog lists it; before, every page
    open of that user failed (PageInfo needed a target; 2026-10-27 sweep, regression's 「均值」)."""
    from evowiki.wikiapp.goal import page_events_for
    repo, _, ids = env
    organized(env)
    snap = repo.load("u1")
    rec, evs = page_events_for(snap.state, snap.k, title="均值", goal="均值是什么",
                               concepts=(), targets=(), now=NOW, new_id=ids, user_id="u1")
    repo.commit(snap, Commit(wiki=evs))
    snap = repo.load("u1")
    cat = catalog_out(snap.state, snap.k, NOW, user_id="u1")
    info = {p.page_id: p for p in cat.learning}[rec.page_id]
    assert info.title == "均值"
