"""「建议先复习 X」 (wikiapp.review_first): only a wrong answer still standing on a weak prerequisite of the
next page; 先复习 and 跳过 close it, a new wrong answer brings it back."""

from types import SimpleNamespace

import pytest

from evowiki.belief.aspects import AspectKView, Claim, DefineConcept, Observe
from evowiki.goals.contracts import Aspect, Observation
from evowiki.wikiapp import review_first
from evowiki.wikiapp.repo import Commit, MemoryRepo
from tests.wikiapp.test_goal import NOW, Ids

ITEM = SimpleNamespace(node_ids=("giant",), page=None, title="巨分量")
CELL = "components.term"


@pytest.fixture
def env():
    repo, ids = MemoryRepo(["u1"]), Ids()
    snap = repo.load("u1")
    repo.commit(snap, Commit(aspect=[DefineConcept(event_id=ids("aev"), ts=NOW, concept_id="components",
                                                   aspects=(Aspect.TERM,)),
                                     DefineConcept(event_id=ids("aev"), ts=NOW, concept_id="giant",
                                                   aspects=(Aspect.TERM,))]))
    return repo, ids


def answer(env, polarity="-", *, ref="check:p:s", kind="check", cell=CELL, strength=1.0) -> str:
    repo, ids = env
    eid = ids("aev")
    repo.commit(repo.load("u1"), Commit(aspect=[Observe(
        event_id=eid, ts=NOW, observation=Observation(cell=cell, polarity=polarity, strength=strength,
                                                      kind=kind, evidence_ref=ref))]))
    return eid


def hint(env, needs=("components",), *, earlier=(), item=ITEM, pages=None, page_id=None):
    snap = env[0].load("u1")
    state = SimpleNamespace(
        graph=SimpleNamespace(prereqs=lambda _t: needs), page_order=[], pages=pages or {},
        trashed_pages=frozenset(), review_first_answered=snap.state.review_first_answered,
        names={"components": SimpleNamespace(name="连通分量")})
    return review_first.suggestion(state, AspectKView(snap.k, NOW), item, earlier, page_id)


ROW = SimpleNamespace(node_ids=("components",))


def _page(body):
    return {"pg": SimpleNamespace(page=SimpleNamespace(texts={"s1": SimpleNamespace(body=body)}))}


def test_an_earlier_row_the_next_page_names_counts_with_no_edge(env):
    answer(env)
    assert hint(env, needs=(), earlier=(ROW,), pages=_page("最大连通分量突然变大"), page_id="pg") is not None


def test_an_earlier_row_the_next_page_does_not_name_stays_silent(env):
    answer(env)
    assert hint(env, needs=(), earlier=(ROW,), pages=_page("完全无关的内容"), page_id="pg") is None


def test_a_wrong_answer_on_a_prerequisite_suggests_it(env):
    assert hint(env) is None
    answer(env)
    h = hint(env)
    assert h.node_id == "components" and h.name == "连通分量" and h.page_id is None


def test_nothing_when_the_next_page_does_not_build_on_it(env):
    answer(env)
    assert hint(env, needs=()) is None


def test_nothing_when_the_point_still_reads_learned(env):
    for _ in range(4):
        answer(env, "+")
    answer(env)
    assert hint(env) is None


def test_a_right_answer_after_it_closes_it(env):
    answer(env)
    for _ in range(2):
        answer(env, "+")
    assert hint(env) is None


def test_a_careless_answer_does_not_suggest(env):
    wrong = answer(env)
    repo, ids = env
    repo.commit(repo.load("u1"), Commit(aspect=[Claim(
        event_id=ids("aev"), ts=NOW, claim="slip", cell=CELL, source_event_id=wrong)]))
    assert hint(env) is None


@pytest.mark.parametrize("kind,ref", [("quiz", "probe:1"), ("recall", "review:p:s:x"),
                                      ("check", "review:p:s:x"), ("check", "confirm:f")])
def test_the_probe_and_review_answers_do_not_suggest(env, kind, ref):
    answer(env, kind=kind, ref=ref)
    assert hint(env) is None


@pytest.mark.parametrize("choice", ["review", "skip"])
def test_both_choices_close_it_and_a_new_wrong_answer_brings_it_back(env, choice):
    repo, ids = env
    answer(env)
    review_first.answer(repo, "u1", "components", choice, now=NOW, new_id=ids)
    assert hint(env) is None
    answer(env)
    assert hint(env) is not None


def test_answering_changes_no_path_and_writes_no_page(env):
    repo, ids = env
    answer(env)
    before = repo.load("u1").state
    review_first.answer(repo, "u1", "components", "review", now=NOW, new_id=ids)
    after = repo.load("u1").state
    assert after.paths == before.paths and after.pages == before.pages
