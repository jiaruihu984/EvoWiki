"""Assistant-actions Task 8: a path edit (the learner's, the assistant's, the Bot's — all end in
`save_paths` and its `on_path_changed` hooks) against queued and writing page runs. A removed row
loses its write (spent money stays recorded); a page written for pages that were before it and have
changed is marked out of date when it ends, so opening it rewrites it. Inline runs, scripted writer."""

from datetime import timedelta

import pytest

from evowiki.goals.path_edit import MoveEdit, RemoveEdit, SkipEdit
from evowiki.wikiapp import path_order
from evowiki.wikiapp.events import PageRewriteFailed, PageSaved, PathSaved, WikiEvent, WriteMark
from evowiki.wikiapp.path_edits import (
    commit_path_edit,
    edit_path,
    off_path_changed,
    on_path_changed,
)
from evowiki.wikiapp.paths import next_path_page
from evowiki.wikiapp.refine_apply import path_context
from evowiki.wikiapp.repo import Commit
from evowiki.wikiapp.stale import RETRY_AFTER, reorder_due, reorder_stale, rewrite_due
from evowiki.wikiapp.wiki_schemas import PathRemoveIn
from evowiki.wikiapp.write_queue import predecessors_changed
from tests.wikiapp.test_k1_replay import PAGE1, PAGE2, PAGE3
from tests.wikiapp.test_write_queue import NOW, build, no_idle, script_page, world  # noqa: F401


@pytest.fixture
def hooked():
    registered = []

    def hook(q):
        registered.append(on_path_changed(q.path_changed))
    yield hook
    for fn in registered:
        off_path_changed(fn)


def keys(repo, path_id):
    return [i.key for i in path_order.shown(repo.load("u1").state.paths[path_id])]


def move_first(repo, ids, path_id, key, source):
    commit_path_edit(repo, "u1", path_id, [MoveEdit(page_key=key, to_index=0, by=source)],
                     now=NOW, new_id=ids, source=source)


def remove(repo, ids, path_id, key, source):
    if source == "user":
        edit_path(repo, "u1", path_id, PathRemoveIn(op="remove", page_key=key), now=NOW, new_id=ids)
    else:
        commit_path_edit(repo, "u1", path_id, [RemoveEdit(page_key=key, by=source)],
                         now=NOW, new_id=ids, source=source)


def started(repo, ids, path_id, n=3):
    return [next_path_page(repo, "u1", path_id, now=NOW, new_id=ids).page_id for _ in range(n)]


@pytest.mark.parametrize("source", ["user", "assistant", "bot"])
def test_a_removed_row_loses_its_queued_write_the_others_stay(world, hooked, source):  # noqa: F811
    repo, s, ids, path_id = world
    jobs: list = []
    q, runs, _ = build(repo, s, ids, jobs=jobs, env=no_idle())
    hooked(q)
    ps = started(repo, ids, path_id)
    script_page(s, PAGE1)
    script_page(s, PAGE3)
    running, doomed, kept = (q.request("u1", [p])[0] for p in (ps[0], ps[1], ps[2]))
    key = keys(repo, path_id)[1]
    remove(repo, ids, path_id, key, source)
    live = [r.run_id for r in runs.store.live_of("u1")]
    assert live == [running, kept]                      # the removed row's run left the queue
    assert runs.store.get(doomed).discarded_at is not None
    while jobs:
        jobs.pop()()                                    # `doomed` is dropped, never writes
    assert runs.store.get(doomed).status == "failed"
    assert runs.store.get(doomed).failure == "cancelled"
    assert runs.store.get(kept).status == "done"


def in_flight(q, change):
    """`change()` runs while the page is being written (the writer has begun, nothing is committed)."""
    q._deps().writing.writer.before_write = change


@pytest.mark.parametrize("source", ["user", "assistant", "bot"])
def test_a_page_in_flight_when_its_predecessors_changed_is_marked_and_rewritten_on_open(
        world, source):  # noqa: F811
    repo, s, ids, path_id = world
    jobs: list = []
    q, runs, _ = build(repo, s, ids, jobs=jobs, env=no_idle())
    ps = started(repo, ids, path_id, 2)
    script_page(s, PAGE2)
    key = keys(repo, path_id)[1]
    in_flight(q, lambda: move_first(repo, ids, path_id, key, source))   # page 2 goes first
    run = q.request("u1", [ps[1]])[0]
    jobs.pop()()
    st = repo.load("u1").state
    assert runs.store.get(run).status == "done"
    assert st.reorder_marks[ps[1]].job_id == run
    assert reorder_stale(st, ps[1]) and rewrite_due(st, ps[1], NOW) and reorder_due(st, ps[1], NOW)
    # the next whole-page write ends the mark
    rec = st.pages[ps[1]]
    mark = WriteMark(job_id="rewrite-1", at=NOW, model="m", sections=(), kind="rewrite")
    repo.commit(repo.load("u1"), Commit(wiki=[WikiEvent(
        event_id=ids("wev"), ts=NOW,
        body=PageSaved(record=rec.model_copy(update={"written": mark}), action="refined"))]))
    st = repo.load("u1").state
    assert not reorder_stale(st, ps[1]) and not rewrite_due(st, ps[1], NOW)


def test_a_reorder_before_the_write_begins_is_followed_not_marked(world):  # noqa: F811
    repo, s, ids, path_id = world
    jobs: list = []
    q, _runs, _ = build(repo, s, ids, jobs=jobs, env=no_idle())
    ps = started(repo, ids, path_id, 2)
    script_page(s, PAGE2)
    q.request("u1", [ps[1]])                             # queued behind page 1
    move_first(repo, ids, path_id, keys(repo, path_id)[1], "user")
    jobs.pop()()
    assert repo.load("u1").state.reorder_marks == {}


def test_a_skip_while_in_flight_does_not_mark(world):  # noqa: F811
    repo, s, ids, path_id = world
    jobs: list = []
    q, _runs, _ = build(repo, s, ids, jobs=jobs, env=no_idle())
    ps = started(repo, ids, path_id, 2)
    script_page(s, PAGE2)
    first = path_order.shown(repo.load("u1").state.paths[path_id])[0]
    skip = lambda: commit_path_edit(
        repo, "u1", path_id, [SkipEdit(page_key=first.key, node_ids=first.node_ids[:1])],
        now=NOW, new_id=ids, source="user")
    in_flight(q, skip)
    q.request("u1", [ps[1]])
    jobs.pop()()
    assert repo.load("u1").state.reorder_marks == {}


def test_an_unchanged_path_marks_nothing(world):  # noqa: F811
    repo, s, ids, path_id = world
    jobs: list = []
    q, _runs, _ = build(repo, s, ids, jobs=jobs, env=no_idle())
    ps = started(repo, ids, path_id, 2)
    script_page(s, PAGE2)
    q.request("u1", [ps[1]])
    jobs.pop()()
    assert repo.load("u1").state.reorder_marks == {}


def test_a_failing_reorder_rewrite_is_not_retried_on_every_open(world):  # noqa: F811
    repo, s, ids, path_id = world
    jobs: list = []
    q, _runs, _ = build(repo, s, ids, jobs=jobs, env=no_idle())
    ps = started(repo, ids, path_id, 2)
    script_page(s, PAGE2)
    key = keys(repo, path_id)[1]
    in_flight(q, lambda: move_first(repo, ids, path_id, key, "user"))
    q.request("u1", [ps[1]])
    jobs.pop()()
    fail = lambda t: repo.commit(repo.load("u1"), Commit(wiki=[WikiEvent(
        event_id=ids("wev"), ts=t, body=PageRewriteFailed(page_id=ps[1], spec_version=1, kind="reorder"))]))
    assert reorder_due(repo.load("u1").state, ps[1], NOW)
    fail(NOW)
    st = repo.load("u1").state
    assert not rewrite_due(st, ps[1], NOW + timedelta(hours=1))       # one failure: wait a day
    assert rewrite_due(st, ps[1], NOW + RETRY_AFTER + timedelta(minutes=1))
    fail(NOW + RETRY_AFTER)
    st = repo.load("u1").state
    assert not rewrite_due(st, ps[1], NOW + 3 * RETRY_AFTER)          # two failures: stopped


def test_the_writer_reads_the_edited_path_order(world):  # noqa: F811
    repo, _s, ids, path_id = world
    ps = started(repo, ids, path_id, 3)
    before = path_context(repo.load("u1").state, ps[2])
    move_first(repo, ids, path_id, keys(repo, path_id)[2], "user")      # page 3 goes first
    after = path_context(repo.load("u1").state, ps[2])
    assert before != after and "这是第一页" in after and "第 1/" in after


def test_a_page_another_path_still_shows_keeps_its_write(world, hooked):  # noqa: F811
    repo, s, ids, path_id = world
    jobs: list = []
    q, runs, _ = build(repo, s, ids, jobs=jobs, env=no_idle())
    hooked(q)
    ps = started(repo, ids, path_id, 2)
    script_page(s, PAGE1)
    script_page(s, PAGE2)
    q.request("u1", [ps[0]])
    run = q.request("u1", [ps[1]])[0]
    rec = repo.load("u1").state.paths[path_id]
    twin = rec.model_copy(update={"path": rec.path.model_copy(update={"path_id": "path-twin"})})
    repo.commit(repo.load("u1"), Commit(wiki=[WikiEvent(
        event_id=ids("wev"), ts=NOW, body=PathSaved(record=twin, action="user_edit"))]))
    remove(repo, ids, path_id, keys(repo, path_id)[1], "user")
    assert runs.store.get(run).discarded_at is None


def test_rows_that_got_a_page_since_are_not_a_change():
    assert not predecessors_changed((None, None), "p", ("a", "b"), "p")
    assert predecessors_changed(("a", "b"), "p", ("b", "a"), "p")
    assert predecessors_changed(("a",), "p", ("a", "b"), "p")
    assert predecessors_changed(("a",), "p", ("a",), "q")
    assert predecessors_changed(("a",), "p", None, None)
