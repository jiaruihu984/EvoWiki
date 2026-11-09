"""The one-time relayout of a graph placed by an older layout (`wikiapp.graph_layout`, bug
2026-10-15): recorded as `GraphSaved(action="relayout")`, applied once, replay-exact; a graph made
by the current code is never relaid; a reset starts over."""

from datetime import UTC, datetime

from evowiki.nodes.model import NodeGraph
from evowiki.nodes.place import LAYOUT_VERSION
from evowiki.wikiapp.events import GraphSaved, WikiEvent
from evowiki.wikiapp.graph_layout import ensure_layout, needs_relayout
from evowiki.wikiapp.graph_sync import graph_apply
from evowiki.wikiapp.repo import Commit, MemoryRepo
from tests.nodes.synthetic_shape import migrated_graph
from tests.wikiapp.graph_seed import replays_equal

NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)


def ids():
    n = iter(range(10_000))
    return lambda p: f"{p}_{next(n)}"


def legacy_repo():
    """A user migrated by the old code: GraphSaved without a layout version."""
    repo = MemoryRepo(["u1"])
    repo.commit(repo.load("u1"), Commit(wiki=[WikiEvent(
        event_id="wev_m", ts=NOW, body=GraphSaved(graph=migrated_graph(), action="migrated"))]))
    return repo


def test_an_old_layout_is_relaid_once_and_recorded():
    repo, new_id = legacy_repo(), ids()
    snap = repo.load("u1")
    assert snap.state.graph_layout == 0 and needs_relayout(snap.state)
    done = ensure_layout(repo, snap, now=NOW, new_id=new_id)
    last = repo.wiki_log("u1")[-1].body
    assert last.action == "relayout" and last.layout == LAYOUT_VERSION    # type: ignore[union-attr]
    assert done.state.graph == last.graph and done.state.graph_arranged == last.arranged    # type: ignore[union-attr]
    assert done.state.graph_layout == LAYOUT_VERSION
    assert done.state.graph.relations == migrated_graph().relations
    assert replays_equal(repo, done)
    n = len(repo.wiki_log("u1"))
    again = ensure_layout(repo, repo.load("u1"), now=NOW, new_id=new_id)
    assert len(repo.wiki_log("u1")) == n and again.state.graph == done.state.graph


def test_later_saves_keep_the_version_and_a_reset_starts_over():
    repo, new_id = legacy_repo(), ids()
    snap = ensure_layout(repo, repo.load("u1"), now=NOW, new_id=new_id)
    # a later save keeping the positions (layout 0 on the event) keeps the state's version
    body = graph_apply(snap.state, snap.state.graph, "user_edit")
    assert body.layout == 0
    snap = repo.commit(snap, Commit(wiki=[WikiEvent(event_id="wev_e", ts=NOW, body=body)]))
    assert snap.state.graph_layout == LAYOUT_VERSION and not needs_relayout(snap.state)
    snap = repo.commit(snap, Commit(wiki=[WikiEvent(
        event_id="wev_r", ts=NOW, body=GraphSaved(graph=NodeGraph(changes=snap.state.graph.changes), action="reset"))]))
    assert snap.state.graph_layout == 0 and not needs_relayout(snap.state)   # empty graph


def test_a_graph_placed_from_scratch_is_arranged_once_and_then_stands():
    repo = MemoryRepo(["u1"])
    snap = repo.load("u1")
    bare = migrated_graph().updated(nodes=[r.model_copy(update={"x": None, "y": None})
                                           for r in migrated_graph().nodes])
    body = graph_apply(snap.state, bare, "migrated")
    assert body.layout == LAYOUT_VERSION
    snap = repo.commit(snap, Commit(wiki=[WikiEvent(event_id="wev_1", ts=NOW, body=body)]))
    assert not needs_relayout(snap.state)
    done = ensure_layout(repo, snap, now=NOW, new_id=ids())
    assert ensure_layout(repo, done, now=NOW, new_id=ids()) is done
