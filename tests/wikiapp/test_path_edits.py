"""Path edits in the wiki app (plan T7): stored as PathSaved(user_edit), replayed equal, followed by
「下一页」 / the idle page picks / path_out; 「我会了，跳过」 and 「我想学这个」; replanning keeps the
user's work. Built on a seeded path (no goal flow), 0 LLM calls except the scripted replan."""

from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter

from evowiki.belief.aspects import DefineConcept
from evowiki.bot.settings import BotSettings
from evowiki.goals.guardrails import PageConcept
from evowiki.goals.path import LearningPath, PathPage
from evowiki.goals.regions import GoalMap, MapNode
from evowiki.nodes import NodeGraph, NodeRec, Relation
from evowiki.wikiapp.events import GoalMapSaved, GraphSaved, PathRecord, PathSaved, WikiEvent
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.path_edits import edit_path, want_node
from evowiki.wikiapp.paths import (
    next_path_page,
    path_cards,
    path_items,
    path_out,
    replan,
)
from evowiki.wikiapp.repo import Commit, MemoryRepo, WikiConflict, WikiInvalid
from evowiki.wikiapp.state import replay
from evowiki.wikiapp.views import file_out
from evowiki.wikiapp.wiki_schemas import PathAddIn, PathMoveIn, PathRemoveIn, PathRetitleIn
from evowiki.wikiapp.write_queue import idle_candidates, next_idle
from tests.wikiapp.script import Script

NOW = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
PATH_ID = "path-demo"
MAP_ID = "map-demo"
ASPECTS = ("term", "purpose")
NAMES = {"n1": "总体与样本", "n2": "抽样分布", "n3": "置信区间", "n4": "p 值", "n5": "贝叶斯"}


class Ids:
    def __init__(self):
        self.n = 0

    def __call__(self, prefix: str) -> str:
        self.n += 1
        return f"{prefix}_{self.n:04d}"


def _page(i: int, targets, support=()) -> PathPage:
    return PathPage(page_id=f"{PATH_ID}-p{i}", targets=tuple(targets),
                    concepts=(*support, *targets), objective=f"学完这页你能讲清第 {i} 页",
                    new_cells=2, title=f"第 {i} 页")


def seed_events(new_id) -> tuple[list, list[WikiEvent]]:
    """K concepts n1..n5 (2 cells each), the graph (region r with n1..n4, prerequisites
    n1→n2→n3), a goal map whose region r has the path, and the path: p1 n1 | p2 n2 | p3 n3, n4."""
    aspect = [DefineConcept(event_id=new_id("aev"), ts=NOW, concept_id=c, aspects=ASPECTS,
                            prereqs={"n2": ("n1",), "n3": ("n2",)}.get(c, ()))
              for c in NAMES]
    nodes = [NodeRec(id="r", name="统计推断", x=0.0, y=0.0),
             *(NodeRec(id=c, name=n, x=0.1 * i, y=0.1) for i, (c, n) in enumerate(NAMES.items()))]
    rels = [*(Relation(kind="part_of", src=c, dst="r") for c in ("n1", "n2", "n3", "n4")),
            Relation(kind="prerequisite", src="n1", dst="n2", phrase="先有样本"),
            Relation(kind="prerequisite", src="n2", dst="n3", phrase="区间来自抽样分布")]
    gmap = GoalMap(map_id=MAP_ID, goal="学统计推断", created_at=NOW,
                   nodes=(MapNode(id="r", kind="region", name="统计推断", state="expanded",
                                  path_id=PATH_ID, members=("n1", "n2", "n3", "n4")),))
    path = LearningPath(path_id=PATH_ID, goal="学统计推断", goal_key="k-demo", split=True,
                        source="llm", new_cells=8,
                        concepts=tuple(PageConcept(id=c, name=NAMES[c],
                                                   prereqs={"n2": ("n1",), "n3": ("n2",)}
                                                   .get(c, ()))
                                       for c in ("n1", "n2", "n3", "n4")),
                        pages=(_page(1, ["n1"]), _page(2, ["n2"], ["n1"]),
                               _page(3, ["n3", "n4"], ["n2"])))
    rec = PathRecord(path=path, page_ids=(None, None, None), created_at=NOW)

    def wev(body) -> WikiEvent:
        return WikiEvent(event_id=new_id("wev"), ts=NOW, body=body)
    wiki = [wev(GraphSaved(graph=NodeGraph.of(nodes, rels), action="goal_nodes")),
            wev(GoalMapSaved(record=gmap, action="path_planned")),
            wev(PathSaved(record=rec, action="planned"))]
    return aspect, wiki


@pytest.fixture
def env():
    repo, ids = MemoryRepo(["u1"]), Ids()
    aspect, wiki = seed_events(ids)
    repo.commit(repo.load("u1"), Commit(aspect=aspect, wiki=wiki))
    return repo, ids


def edit(env, body):
    repo, ids = env
    return edit_path(repo, "u1", PATH_ID, body, now=NOW, new_id=ids).path


def rec(env) -> PathRecord:
    return env[0].load("u1").state.paths[PATH_ID]


def keys(items) -> list[str]:
    return [i.key for i in items]


def test_no_edits_shows_the_stored_path(env):
    snap = env[0].load("u1")
    items = path_items(snap.state, rec(env), snap.k, NOW)
    assert keys(items) == [f"{PATH_ID}-p{i}" for i in (1, 2, 3)]
    assert items[2].node_ids == ["n3", "n4"] and items[2].status == "not_started"
    assert items[0].summary == "学完这页你能讲清第 1 页" and not items[0].mine


def test_edits_are_stored_and_replay_equal(env):
    repo, _ = env
    edit(env, PathMoveIn(op="move", **{"from": 2}, to=0))
    edit(env, PathRemoveIn(op="remove", page_key=f"{PATH_ID}-p1"))
    shown = edit(env, PathAddIn(op="add", request_id="test-1", title="自己复习一遍"))
    assert keys(shown)[:2] == [f"{PATH_ID}-p3", f"{PATH_ID}-p2"]
    assert shown[-1].mine and shown[-1].key.startswith("u-") and len(shown[-1].node_ids) == 1   # its own node (D34)
    log = repo.wiki_log("u1")
    assert [e.body.action for e in log if isinstance(e.body, PathSaved)][-3:] == ["user_edit"] * 3
    ta = TypeAdapter(list[WikiEvent])
    again = replay(ta.validate_json(ta.dump_json(log)))
    assert again == repo.load("u1").state
    assert len(again.paths[PATH_ID].edits) == 3


def test_rename_reaches_the_row_the_page_and_its_file_and_the_old_title_takes_it_back(env):
    repo, ids = env
    key = f"{PATH_ID}-p1"
    # a row nobody started: the page made from it later carries the new title
    assert edit(env, PathRetitleIn(op="retitle", page_key=key, title="样本是什么"))[0].title == "样本是什么"
    out = next_path_page(repo, "u1", PATH_ID, now=NOW, new_id=ids)
    st = repo.load("u1").state
    assert st.pages[out.page_id].title == "样本是什么"
    # a started row: the wiki page too, the page id unchanged
    shown = edit(env, PathRetitleIn(op="retitle", page_key=key, title="  总体和样本  "))
    st = repo.load("u1").state
    assert shown[0].title == "总体和样本" and st.pages[out.page_id].title == "总体和样本"
    assert file_out(st, st.pages[out.page_id]).title == "总体和样本"
    edit(env, PathRetitleIn(op="retitle", page_key=key, title="样本是什么"))
    assert repo.load("u1").state.pages[out.page_id].title == "样本是什么"
    with pytest.raises(WikiInvalid):
        edit(env, PathRetitleIn(op="retitle", page_key="nope", title="x"))
    ta = TypeAdapter(list[WikiEvent])
    assert replay(ta.validate_json(ta.dump_json(repo.wiki_log("u1")))) == repo.load("u1").state


def test_bad_index_is_invalid_and_unknown_path_lookup(env):
    with pytest.raises(WikiInvalid):
        edit(env, PathRemoveIn(op="remove", page_key="missing"))
    with pytest.raises(WikiInvalid):
        edit(env, PathMoveIn(op="move", **{"from": 0}, to=5))
    with pytest.raises(LookupError):
        edit(env, PathAddIn(op="add", request_id="test-2", title="x", node_id="nope"))
    with pytest.raises(LookupError):
        edit_path(env[0], "u1", "path-nope", PathRemoveIn(op="remove", page_key="missing"), now=NOW,
                  new_id=env[1])


def test_next_page_follows_the_shown_order(env):
    repo, ids = env
    edit(env, PathMoveIn(op="move", **{"from": 2}, to=0))
    edit(env, PathRemoveIn(op="remove", page_key=f"{PATH_ID}-p1"))            # p1 hidden
    out = next_path_page(repo, "u1", PATH_ID, now=NOW, new_id=ids)
    assert out.index == 3                                     # stored index of p3
    st = repo.load("u1").state
    assert st.pages[out.page_id].targets == ("n3", "n4")
    assert st.pages[out.page_id].title == "第 3 页"         # the row's title, no （i/n）
    assert "（" not in st.pages[out.page_id].title
    second = next_path_page(repo, "u1", PATH_ID, now=NOW, new_id=ids)
    assert second.index == 2
    with pytest.raises(WikiConflict):                         # p1 removed: never started
        next_path_page(repo, "u1", PATH_ID, now=NOW, new_id=ids)
    snap = repo.load("u1")
    view = path_out(snap.state, snap.k, PATH_ID, NOW)
    assert [p.index for p in view.pages] == [3, 2] and view.next_index is None
    card = path_cards(snap.state, snap.k, NOW)[0]
    assert card.pages == 2 and card.next_index is None


def test_want_refuses_unknown_nodes_and_goals(env):
    repo, ids = env
    with pytest.raises(LookupError):
        want_node(repo, "u1", "zz", MAP_ID, now=NOW, new_id=ids)
    with pytest.raises(LookupError):
        want_node(repo, "u1", "n5", "map-nope", now=NOW, new_id=ids)


def test_want_adds_an_own_page_once_and_it_can_be_started(env):
    repo, ids = env
    out = want_node(repo, "u1", "n5", MAP_ID, now=NOW, new_id=ids)
    assert out.path[-1].mine and out.path[-1].node_ids == ["n5"]
    assert out.path[-1].title == "贝叶斯"
    n = len(repo.wiki_log("u1"))
    again = want_node(repo, "u1", "n5", MAP_ID, now=NOW, new_id=ids)
    assert again.path == out.path and len(repo.wiki_log("u1")) == n      # nothing changes
    same = want_node(repo, "u1", "n2", MAP_ID, now=NOW, new_id=ids)       # already on the path
    assert len(same.path) == 4 and len(repo.wiki_log("u1")) == n
    edit(env, PathMoveIn(op="move", **{"from": 3}, to=0))
    started = next_path_page(repo, "u1", PATH_ID, now=NOW, new_id=ids)
    st = repo.load("u1").state
    assert st.pages[started.page_id].targets == ("n5",)
    assert path_items(st, st.paths[PATH_ID], repo.load("u1").k, NOW)[0].page_id == started.page_id


def test_add_with_a_node_is_a_startable_page(env):
    out = edit(env, PathAddIn(op="add", request_id="test-3", title="  贝叶斯入门 ", node_id="n5"))
    assert out[-1].title == "贝叶斯入门" and out[-1].node_ids == ["n5"] and out[-1].mine


def test_the_idle_pick_follows_the_shown_order(env):
    repo = env[0]
    edit(env, PathMoveIn(op="move", **{"from": 2}, to=1))     # p1, p3, p2
    cands = idle_candidates(repo.load("u1").state)
    assert [c.index for c in cands] == [1, 3, 2]
    assert next_idle(repo.load("u1"), "u1", BotSettings(kinds={"idle_write": True})).key == f"{PATH_ID}-p1"
    assert [c.key for c in idle_candidates(repo.load("u1").state, exclude=())][1] == f"{PATH_ID}-p3"


def test_replan_keeps_the_users_work(env):
    repo, ids = env
    first = next_path_page(repo, "u1", PATH_ID, now=NOW, new_id=ids).page_id    # p1 started
    edit(env, PathRemoveIn(op="remove", page_key=f"{PATH_ID}-p2"))                               # p2 removed
    want_node(repo, "u1", "n5", MAP_ID, now=NOW, new_id=ids)
    s = Script()
    # the model now puts n2 alone and n3 / n4 on separate pages (n1 is carried as started)
    s.push("goals.path", {"pages": [
        {"concepts": ["n2"], "objective": "学完这页你能说清抽样分布"},
        {"concepts": ["n4"], "objective": "学完这页你能解读 p 值"},
        {"concepts": ["n3"], "objective": "学完这页你能算置信区间"}]})
    out = replan(repo, CountingGateway(s.gateway()), "u1", PATH_ID, now=NOW, new_id=ids).path
    assert s.calls("goals.path") == 1
    assert out[0].key == f"{PATH_ID}-p1" and out[0].page_id == first          # started, in place
    assert f"{PATH_ID}-p2" not in keys(out)                                    # still removed
    assert any(i.mine and i.node_ids == ["n5"] for i in out)                  # own page kept
    assert sorted(n for i in out for n in i.node_ids) == ["n1", "n3", "n4", "n5"]
    after = rec(env)
    assert [e.body.action for e in repo.wiki_log("u1") if isinstance(e.body, PathSaved)][-1] \
        == "replanned"
    assert after.path.path_id == PATH_ID and len(after.page_ids) == len(after.path.pages)


def test_reversed_stable_removals_delete_only_selected_rows(env):
    edit(env, PathRemoveIn(op="remove", page_key=f"{PATH_ID}-p3"))
    result = edit(env, PathRemoveIn(op="remove", page_key=f"{PATH_ID}-p1"))
    assert keys(result) == [f"{PATH_ID}-p2"]


def test_add_retry_after_lost_response_reuses_row_and_node(env):
    request = PathAddIn(op="add", title="new", request_id="intent-1")
    first = edit(env, request)
    node_count = len(env[0].load("u1").state.graph.nodes)
    second = edit(env, request)
    assert second == first
    assert len(env[0].load("u1").state.graph.nodes) == node_count
    third = edit(env, PathAddIn(op="add", title="new", request_id="intent-2"))
    assert len(third) == len(first) + 1


@pytest.mark.parametrize("same_intent", [False, True])
def test_two_clients_commit_from_one_snapshot_without_losing_or_duplicating_edits(env, monkeypatch, same_intent):
    repo, ids = env
    original = repo.commit
    interleaved = False
    def commit(snap, changes, **kwargs):
        nonlocal interleaved
        if not interleaved:
            interleaved = True
            other = (PathAddIn(op="add", title="new", request_id="shared") if same_intent
                     else PathRemoveIn(op="remove", page_key=f"{PATH_ID}-p3"))
            edit_path(repo, "u1", PATH_ID, other, now=NOW, new_id=ids)
        return original(snap, changes, **kwargs)
    monkeypatch.setattr(repo, "commit", commit)
    request = (PathAddIn(op="add", title="new", request_id="shared") if same_intent
               else PathRemoveIn(op="remove", page_key=f"{PATH_ID}-p1"))
    result = edit(env, request)
    if same_intent:
        assert len(result) == 4
        assert sum(n.name == "new" for n in repo.load("u1").state.graph.nodes) == 1
    else:
        assert keys(result) == [f"{PATH_ID}-p2"]
