"""Splitting and undo keep the distinct goal, graph and path projections usable."""

import pytest

from evowiki.goals.path_edit import AddEdit, UnreplaceEdit
from evowiki.goals.regions import MapNode
from evowiki.wikiapp.events import GoalMapSaved, PathSaved, WikiEvent
from evowiki.wikiapp.goal_groups import goal_groups
from evowiki.wikiapp.graph_changes import GraphChanges, GraphUndoRefused
from evowiki.wikiapp.llm import require
from evowiki.wikiapp.node_split import split_node
from evowiki.wikiapp.path_edits import (
    commit_path_edit,
    off_path_changed,
    on_path_changed,
    undo_split_row,
)
from evowiki.wikiapp.path_order import shown
from evowiki.wikiapp.paths import next_path_page
from evowiki.wikiapp.repo import Commit, MemoryRepo, WikiConflict
from evowiki.wikiapp.state import replay
from tests.wikiapp.script import Script
from tests.wikiapp.split_answers import PURPOSE
from tests.wikiapp.test_path_edits import MAP_ID, NAMES, PATH_ID, Ids, seed_events
from tests.wikiapp.test_path_edits_split import (
    ANSWER,
    NOW,
    P1,
    P2,
    P3,
    rec,
    split,
)


@pytest.fixture
def env():
    repo, ids = MemoryRepo(["u1"]), Ids()
    aspect, wiki = seed_events(ids)
    repo.commit(repo.load("u1"), Commit(aspect=aspect, wiki=wiki))
    return repo, ids


@pytest.fixture
def seen():
    calls = []

    def hook(*args):
        calls.append(args)

    on_path_changed(hook)
    yield calls
    off_path_changed(hook)


@pytest.mark.parametrize("entrance", ["graph", "path"])
def test_split_undo_updates_the_path_and_goal_graph_together(env, seen, entrance, monkeypatch):
    repo, ids = env
    groups = goal_groups(repo.load("u1").state)
    out = split(env)
    added = set(repo.load("u1").state.graph.children("n2"))
    assert added <= set(goal_groups(repo.load("u1").state)[0].members)
    # The goal has since displayed the split's parts on its map as well.
    state = repo.load("u1").state
    m = state.maps[MAP_ID].replace(
        MapNode(id="n2", name=NAMES["n2"], kind="concept", parent="r"),
        *(MapNode(id=i, name=state.graph.node(i).name, kind="concept", parent="n2") for i in added))
    save_map(env, m)
    commit_path_edit(repo, "u1", PATH_ID, [AddEdit(page_key="later", title="后加的一页")],
                     now=NOW, new_id=ids, source="user")
    seen.clear()
    commits = []
    commit = repo.commit

    def record_commit(snap, change):
        commits.append(change)
        return commit(snap, change)

    monkeypatch.setattr(repo, "commit", record_commit)
    if entrance == "graph":
        GraphChanges(repo, ids).undo("u1", out.undo, now=NOW)
    else:
        undo_split_row(repo, "u1", PATH_ID, P2, out.undo, now=NOW, new_id=ids)
    done = repo.load("u1")
    assert [i.key for i in shown(rec(repo))] == [P1, P2, P3, "later"]
    assert all(done.state.graph.node(i) is None for i in added)
    assert done.state.maps[MAP_ID].children("n2") == ()
    assert goal_groups(done.state) == groups
    assert len(seen) == 1 and seen[0][-1] == "user"
    assert len(commits) == 1
    assert {e.body.type for e in commits[0].wiki} >= {"graph_saved", "goal_map_saved", "path_saved"}
    assert replay(repo.wiki_log("u1")) == done.state


def map_split(env):
    repo, ids = env
    snap = repo.load("u1")
    m = snap.state.maps[MAP_ID].replace(MapNode(id="n2", name=NAMES["n2"], kind="concept", parent="r"))
    save_map(env, m)
    gw = require(Script().push(PURPOSE, ANSWER).gateway())
    return split_node(repo, gw, "u1", "n2", reason="requested", source="continue",
                       map_id=MAP_ID, now=NOW, new_id=ids)


def save_map(env, m):
    repo, ids = env
    repo.commit(repo.load("u1"), Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=NOW,
                                            body=GoalMapSaved(record=m, action="edited"))]))


def test_graph_undo_cleans_map_references_and_keeps_unrelated_edits(env):
    repo, ids = env
    out = map_split(env)
    m = repo.load("u1").state.maps[MAP_ID]
    assert {n.id for n in m.children("n2")} == set(out.children)
    root = m.node("r")
    m = m.replace(root.model_copy(update={"members": (*root.members, *out.children),
                                         "prereqs": out.children}))
    save_map(env, m.model_copy(update={"title": "后来改的目标名"}))
    GraphChanges(repo, ids).undo("u1", out.change.change_id, now=NOW)
    done = repo.load("u1")
    m = done.state.maps[MAP_ID]
    assert m.title == "后来改的目标名" and m.children("n2") == ()
    assert not set(out.children).intersection((*m.node("r").members, *m.node("r").prereqs))
    assert not set(out.children).intersection(goal_groups(done.state)[0].members)
    assert replay(repo.wiki_log("u1")) == done.state


def test_graph_undo_refuses_later_path_use_without_partial_writes(env):
    repo, ids = env
    out = split(env)
    snap = repo.load("u1")
    child = snap.state.graph.children("n2")[0]
    other = rec(repo).model_copy(update={"path": rec(repo).path.model_copy(update={"path_id": "other"})})
    repo.commit(snap, Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=NOW,
                                            body=PathSaved(record=other, action="planned"))]))
    commit_path_edit(repo, "u1", "other", [UnreplaceEdit(page_key=P2),
                                           AddEdit(page_key="another", title="再次学", node_id=child)],
                     now=NOW, new_id=ids, source="user")
    before = repo.load("u1")
    with pytest.raises(GraphUndoRefused, match="后来路径"):
        GraphChanges(repo, ids).undo("u1", out.undo, now=NOW)
    after = repo.load("u1")
    assert (after.state, after.wiki_seq, after.aspect_seq) == (before.state, before.wiki_seq, before.aspect_seq)


def test_graph_undo_refuses_a_map_child_that_has_started_learning(env):
    repo, ids = env
    out = map_split(env)
    m = repo.load("u1").state.maps[MAP_ID]
    save_map(env, m.replace(m.node(out.children[0]).model_copy(update={"path_id": PATH_ID})))
    before = repo.load("u1")
    with pytest.raises(GraphUndoRefused, match="后来已经学习"):
        GraphChanges(repo, ids).undo("u1", out.change.change_id, now=NOW)
    after = repo.load("u1")
    assert (after.state, after.wiki_seq, after.aspect_seq) == (before.state, before.wiki_seq, before.aspect_seq)


@pytest.mark.parametrize("entrance", ["graph", "path"])
def test_both_undo_entrances_restore_every_path_and_keep_reused_points(env, seen, entrance):
    repo, ids = env
    existing = repo.load("u1").state.graph.node("n5")
    out = split(env, answer={"parts": [
        {"name": NAMES["n5"], "existing_id": "n5", "atomic": True},
        {"name": "新的一部分", "summary": "本次才创建", "atomic": True}]})
    snap = repo.load("u1")
    other = rec(repo).model_copy(update={"path": rec(repo).path.model_copy(update={"path_id": "other"})})
    repo.commit(snap, Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=NOW,
                                            body=PathSaved(record=other, action="planned"))]))
    seen.clear()
    if entrance == "graph":
        GraphChanges(repo, ids).undo("u1", out.undo, now=NOW)
    else:
        undo_split_row(repo, "u1", PATH_ID, P2, out.undo, now=NOW, new_id=ids)
    done = repo.load("u1")
    assert done.state.graph.node("n5") == existing
    assert done.state.graph.children("n2") == ()
    assert {call[1] for call in seen} == {PATH_ID, "other"}
    assert len(seen) == 2
    for path_id in (PATH_ID, "other"):
        assert [i.key for i in shown(done.state.paths[path_id])] == [P1, P2, P3]
    assert replay(repo.wiki_log("u1")) == done.state


@pytest.mark.parametrize("entrance", ["graph", "path"])
@pytest.mark.parametrize("started_path", [PATH_ID, "other"])
def test_split_undo_refuses_started_replacement_pages_on_any_path(env, seen, entrance, started_path):
    repo, ids = env
    out = split(env)
    if started_path == "other":
        original = rec(repo)
        other = original.model_copy(update={"path": original.path.model_copy(update={"path_id": "other"})})
        repo.commit(repo.load("u1"), Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=NOW,
                                                body=PathSaved(record=other, action="planned"))]))
    next_path_page(repo, "u1", started_path, now=NOW, new_id=ids)  # original first row
    started = next_path_page(repo, "u1", started_path, now=NOW, new_id=ids)
    before = repo.load("u1")
    assert started.page_id in before.state.pages
    assert set(before.state.pages[started.page_id].targets) <= set(before.state.graph.children("n2"))
    seen.clear()
    error = GraphUndoRefused if entrance == "graph" else WikiConflict
    with pytest.raises(error, match="已经开始学习"):
        if entrance == "graph":
            GraphChanges(repo, ids).undo("u1", out.undo, now=NOW)
        else:
            undo_split_row(repo, "u1", PATH_ID, P2, out.undo, now=NOW, new_id=ids)
    after = repo.load("u1")
    assert (after.state, after.wiki_seq, after.aspect_seq) == (before.state, before.wiki_seq, before.aspect_seq)
    assert seen == []
