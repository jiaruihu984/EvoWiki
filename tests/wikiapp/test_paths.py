"""Learning paths in the wiki app (K1 Task 4, big-goal): a goal the path model splits into
several pages becomes a path when its diagnosis ends, pages are created only on demand, 「学完」 on
a path page, paths on the catalog."""

import pytest

from evowiki.belief.aspects import Declare
from evowiki.llm.gateway import Gateway
from evowiki.llm.usage import BudgetExceededError
from evowiki.wikiapp.kwrite import concept_roles
from evowiki.wikiapp.llm import JSON_PROMPT_VERSION, PURPOSE_GROUPS, CountingGateway
from evowiki.wikiapp.paths import next_path_page, path_out, path_page_done
from evowiki.wikiapp.repo import Commit, MemoryRepo, WikiConflict
from evowiki.wikiapp.views import catalog_out, goal_draft_out
from tests.runtime.helpers import T0
from tests.runtime.llm_helpers import GW_CONFIG
from tests.wikiapp.flat_goal import flat_goal
from tests.wikiapp.goal_script import PROBE, push_no_question
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids, goal1
from tests.wikiapp.test_progress import write_body

ASPECTS4 = ["term", "purpose", "notation", "interpret"]


def _concept(n: int, name: str, *, prereq: int | None = None, target: bool = False) -> dict:
    return {"id": f"n{n}", "name": name, "target": target,
            "prereqs": [f"n{prereq}"] if prereq else [], "aspects": ASPECTS4}


# 6 concepts × 4 aspects, no evidence. Chains c1 → c2 → c3, c4 → c5 → c6.
BIG_INTAKE = {
    "title": "统计推断",
    "concepts": [
        _concept(1, "总体与样本"),
        _concept(2, "抽样分布", prereq=1),
        _concept(3, "置信区间", prereq=2, target=True),
        _concept(4, "零假设"),
        _concept(5, "检验统计量", prereq=4),
        _concept(6, "p 值", prereq=5, target=True),
    ],
    "observations": [],
}
BIG_PATH = {"pages": [
    {"concepts": ["c1", "c2", "c4"], "objective": "学完这页你能说清样本、抽样分布和零假设"},
    {"concepts": ["c3", "c5"], "objective": "学完这页你能算置信区间和检验统计量"},
    {"concepts": ["c6"], "objective": "学完这页你能解读 p 值"},
]}
GOAL = "学统计推断"


@pytest.fixture
def env():
    return MemoryRepo(["u1"]), Script(), Ids()


def big_goal(env, gw=None):
    repo, s, ids = env
    s.push("goals.intake", BIG_INTAKE)
    push_no_question(s)               # no usable item: the diagnosis ends at once (fail closed)
    s.push("goals.path", BIG_PATH)
    gw = gw or CountingGateway(s.gateway())
    return flat_goal(repo, gw, "u1", GOAL, "", now=NOW, new_id=ids), gw


def test_path_purpose_is_versioned_and_metered_as_page():
    assert JSON_PROMPT_VERSION["goals.path"] == "path@v10"
    assert PURPOSE_GROUPS["goals.path"] == "page"


def test_big_goal_builds_a_path_without_questions_or_page(env):
    repo, s, _ = env
    res, _ = big_goal(env)
    assert res.path_id is not None and res.page_id is None and res.draft is not None
    assert s.calls(PROBE) == 0 and s.calls("goals.path") == 1        # asks nothing (push_no_question)
    assert res.calls == 2                  # the outline (no gap: blank), path
    st = repo.load("u1").state
    assert st.pages == {} and st.drafts == {}
    rec = st.paths[res.path_id]
    assert rec.page_ids == (None, None, None) and rec.path.source == "llm"
    assert [p.targets for p in rec.path.pages] == [("c1", "c2", "c4"), ("c3", "c5"), ("c6",)]
    assert st.names["c3"].name == "置信区间"                  # names committed on this path too
    out = goal_draft_out(res)
    assert out.path_id == res.path_id and out.draft_id is None and out.page_id is None
    assert out.questions == [] and out.title == "统计推断"


def test_small_goal_unchanged(env):
    # the existing INTAKE_1 + script_diag flow still gives a draft with questions, no path
    res, _ = goal1(env)
    assert res.path_id is None and res.open and res.draft.rounds[0].pick is not None
    assert goal_draft_out(res).path_id is None


def test_same_goal_again_reuses_the_path(env):
    repo, s, ids = env
    first, _ = big_goal(env)
    s.push("goals.intake", BIG_INTAKE)                  # no goals.path scripted: must not be called
    push_no_question(s)
    again = flat_goal(repo, CountingGateway(s.gateway()), "u1", GOAL, "", now=NOW,
                        new_id=ids)
    assert again.path_id == first.path_id
    assert s.calls("goals.path") == 1
    assert repo.load("u1").state.path_order == (first.path_id,)


def test_next_creates_one_page_on_demand(env):
    repo, s, ids = env
    res, _ = big_goal(env)
    calls = s.calls()
    out = next_path_page(repo, "u1", res.path_id, now=NOW, new_id=ids)
    assert out.index == 1 and out.path_id == res.path_id and s.calls() == calls   # 0 LLM calls
    st = repo.load("u1").state
    pg = st.pages[out.page_id]
    assert pg.page is None and pg.targets == ("c1", "c2", "c4")
    assert "第 1/3 页" in pg.goal and st.current_page == out.page_id
    assert pg.title and "（" not in pg.title                    # the row's title, no position
    assert concept_roles(st).get("c1") == "learn"               # targets are 在学
    view = path_out(st, repo.load("u1").k, res.path_id, NOW)
    assert [p.status for p in view.pages] == ["learning", "not_started", "not_started"]
    assert view.next_index == 2 and view.pages[0].page_id == out.page_id
    assert view.pages[1].page_id is None and view.source == "llm" and view.goal == GOAL
    assert view.pages[0].aim == BIG_PATH["pages"][0]["objective"]
    # each row carries its path-edit key (学点新的 edits a block's pages by it)
    assert [p.key for p in view.pages] == [f"{res.path_id}-p{i}" for i in (1, 2, 3)]
    # page 2 = targets c3, c5 with their prerequisites c2, c4 from page 1 first
    assert [(c.concept_id, c.target) for c in view.pages[1].concepts] == [
        ("c2", False), ("c4", False), ("c3", True), ("c5", True)]
    assert view.pages[1].concepts[2].name == "置信区间"
    second = next_path_page(repo, "u1", res.path_id, now=NOW, new_id=ids)
    assert second.index == 2 and second.page_id != out.page_id
    rec = repo.load("u1").state.paths[res.path_id]
    assert rec.page_ids == (out.page_id, second.page_id, None)


def test_path_out_unknown_is_lookup_error(env):
    repo, _, _ = env
    snap = repo.load("u1")
    with pytest.raises(LookupError):
        path_out(snap.state, snap.k, "path-nope", NOW)
    with pytest.raises(LookupError):
        next_path_page(repo, "u1", "path-nope", now=NOW, new_id=Ids())


def test_next_when_all_started_is_conflict(env):
    repo, _, ids = env
    res, _ = big_goal(env)
    for _ in range(3):
        next_path_page(repo, "u1", res.path_id, now=NOW, new_id=ids)
    with pytest.raises(WikiConflict, match="这条路径的页都已经开始了"):
        next_path_page(repo, "u1", res.path_id, now=NOW, new_id=ids)


def test_done_on_started_page_and_refusal_on_not_started(env):
    repo, _, ids = env
    res, _ = big_goal(env)
    out = next_path_page(repo, "u1", res.path_id, now=NOW, new_id=ids)
    with pytest.raises(WikiConflict, match="这一页还没开始"):
        path_page_done(repo, "u1", res.path_id, 2, now=NOW, new_id=ids)
    with pytest.raises(WikiConflict, match="还没写好"):               # no body yet (D39)
        path_page_done(repo, "u1", res.path_id, 1, now=NOW, new_id=ids)
    write_body(env, out.page_id)
    done = path_page_done(repo, "u1", res.path_id, 1, now=NOW, new_id=ids)
    assert done.path.pages[0].status == "done" and done.path.next_index == 2
    assert repo.load("u1").state.pages[out.page_id].done
    with pytest.raises(LookupError):
        path_page_done(repo, "u1", res.path_id, 9, now=NOW, new_id=ids)
    with pytest.raises(LookupError):
        path_page_done(repo, "u1", res.path_id, 0, now=NOW, new_id=ids)


def test_learning_page_becomes_done_when_k_learns_it(env):
    # declare every target cell of page 1 known → a written page shows done (mastered), no write
    repo, _, ids = env
    res, _ = big_goal(env)
    first = next_path_page(repo, "u1", res.path_id, now=NOW, new_id=ids)
    write_body(env, first.page_id)
    snap = repo.load("u1")
    decls = [Declare(event_id=f"decl_{c}_{a}", ts=NOW, cell=f"{c}.{a}", value="know")
             for c in ("c1", "c2", "c4") for a in ASPECTS4]
    repo.commit(snap, Commit(aspect=decls))
    n_wiki = len(repo.wiki_log("u1"))
    after = repo.load("u1")
    view = path_out(after.state, after.k, res.path_id, NOW)
    assert [p.status for p in view.pages] == ["done", "not_started", "not_started"]
    assert [p.state for p in view.pages] == ["mastered", "planned", "planned"]
    assert view.next_index == 2
    assert len(repo.wiki_log("u1")) == n_wiki                    # derived, nothing written
    assert not after.state.pages[view.pages[0].page_id].done


def test_catalog_lists_paths(env):
    repo, _, ids = env
    res, _ = big_goal(env)
    snap = repo.load("u1")
    cat = catalog_out(snap.state, snap.k, NOW)
    assert [(c.path_id, c.goal, c.pages, c.done, c.next_index) for c in cat.paths] == [
        (res.path_id, GOAL, 3, 0, 1)]
    first = next_path_page(repo, "u1", res.path_id, now=NOW, new_id=ids)
    write_body(env, first.page_id)
    path_page_done(repo, "u1", res.path_id, 1, now=NOW, new_id=ids)
    snap = repo.load("u1")
    cat = catalog_out(snap.state, snap.k, NOW)
    assert [(c.pages, c.done, c.next_index) for c in cat.paths] == [(3, 1, 2)]


class _PageGroupOverBudget:
    """Over budget for the "page" usage group only: intake and probe (diagnostic) pass,
    goals.path not."""

    def check(self, account):
        if account.group == "page":
            raise BudgetExceededError(account, "day", 1.0)

    def record(self, account, purpose, usage, *, replayed):
        return None


def _budget_gateway(s):
    from evowiki.llm.prices import PriceTable
    from evowiki.llm.scripted import ScriptedProvider
    from evowiki.llm.usage_control import UsageControl
    from tests.wikiapp.script import PROVIDERS

    return Gateway(GW_CONFIG, s.log, mode="record", clock=lambda: T0,
                   providers={p: ScriptedProvider(p, s._responder(p)) for p in PROVIDERS},
                   record_max_attempts=1,
                   usage=UsageControl(_PageGroupOverBudget(), PriceTable({})))


def test_path_budget_exceeded_propagates(env):
    # a metered gateway over budget: create_goal raises BudgetExceededError (route → 429);
    # nothing committed
    repo, s, _ = env
    n_wiki, n_aspect = len(repo.wiki_log("u1")), len(repo.aspect_log("u1"))
    with pytest.raises(BudgetExceededError):
        big_goal(env, gw=CountingGateway(_budget_gateway(s), "u1"))
    assert s.calls("goals.enter") == 1 and s.calls("goals.path") == 0
    st = repo.load("u1").state
    assert st.paths == {} and st.names == {}
    assert (len(repo.wiki_log("u1")), len(repo.aspect_log("u1"))) == (n_wiki, n_aspect)


def test_path_rows_carry_the_exam_count_only_from_the_frequent_threshold(env):
    from evowiki.sources.exam_store import EXAM_FREQUENT
    from evowiki.wikiapp.paths import path_items

    repo, _, _ = env
    res, _ = big_goal(env)
    snap = repo.load("u1")
    rec = snap.state.paths[res.path_id]
    assert all(p.exam == 0 for p in path_out(snap.state, snap.k, res.path_id, NOW).pages)
    counts = {"c3": EXAM_FREQUENT + 2, "c1": EXAM_FREQUENT - 1, "c6": EXAM_FREQUENT}
    out = path_out(snap.state, snap.k, res.path_id, NOW, exam_counts=counts)
    assert [p.exam for p in out.pages] == [0, EXAM_FREQUENT + 2, EXAM_FREQUENT]
    rows = path_items(snap.state, rec, snap.k, NOW, exam_counts=counts)
    assert [r.exam for r in rows] == [0, EXAM_FREQUENT + 2, EXAM_FREQUENT]
    assert all(r.exam == 0 for r in path_items(snap.state, rec, snap.k, NOW))
