"""A path edit against the page runs still queued: they are written in the order the learner (or the
assistant, or the Bot) now sees; a run that has not begun is dropped and queued again, its `before`
read from the new order. Inline runs, scripted writer, fake ledger; no model."""

import pytest

from evowiki.goals.path_edit import MoveEdit
from evowiki.wikiapp import path_order
from evowiki.wikiapp.path_edits import (
    bot_edited,
    commit_path_edit,
    off_path_changed,
    on_path_changed,
    save_path,
)
from evowiki.wikiapp.paths import next_path_page
from evowiki.wikiapp.repo import WikiConflict, WikiInvalid
from evowiki.wikiapp.write_queue import predecessors
from tests.wikiapp.test_k1_replay import PAGE1, PAGE2, PAGE3
from tests.wikiapp.test_path_change_runs import in_flight, keys, move_first
from tests.wikiapp.test_write_queue import (  # noqa: F401
    NOW,
    FakeLedger,
    build,
    no_idle,
    script_page,
    world,
)


@pytest.fixture
def hooked():
    registered = []

    def hook(q):
        registered.append(on_path_changed(q.path_changed))
    yield hook
    for fn in registered:
        off_path_changed(fn)


def started(repo, ids, path_id, n=3):
    return [next_path_page(repo, "u1", path_id, now=NOW, new_id=ids).page_id for _ in range(n)]


def begins(runs):
    """The pages in the order their runs begin."""
    order: list[str] = []
    real = runs.store.begin

    def begin(run_id, at):
        order.append(runs.store.get(run_id).page_id)
        real(run_id, at)
    runs.store.begin = begin
    return order


def live_pages(runs):
    return [r.page_id for r in runs.store.live_of("u1")]


def drain(jobs):
    while jobs:
        jobs.pop()()


def queue_three(world, hooked, ledger=None):  # noqa: F811
    """Three started pages requested in path order: the first is the run in flight (its work is
    the one captured job), the other two wait."""
    repo, s, ids, path_id = world
    jobs: list = []
    q, runs, _ = build(repo, s, ids, jobs=jobs, env=no_idle(), ledger=ledger)
    hooked(q)
    ps = started(repo, ids, path_id)
    old = q.request("u1", ps)
    runs.store.begin(old[0], NOW)         # (inline runs start when the test runs the job)
    order = begins(runs)
    return repo, s, ids, path_id, jobs, q, runs, ps, order, old


@pytest.mark.parametrize("source", ["user", "assistant", "bot"])
def test_queued_runs_follow_a_move(world, hooked, source):  # noqa: F811
    repo, s, ids, path_id, jobs, _q, runs, ps, order, old = queue_three(world, hooked)
    move_first(repo, ids, path_id, keys(repo, path_id)[2], source)       # page 3 goes to the top
    assert runs.store.get(old[0]).discarded_at is None         # (the one that began stays)
    assert live_pages(runs) == [ps[0], ps[2], ps[1]]
    assert all(runs.store.get(r).discarded_at is not None for r in old[1:])
    script_page(s, PAGE1)
    script_page(s, PAGE3)
    script_page(s, PAGE2)
    drain(jobs)
    assert order == [ps[0], ps[2], ps[1]]
    assert [runs.store.get(r).status for r in old[1:]] == ["failed", "failed"]   # never wrote
    # page 3 moved while it was queued: written for its new place, not out of date
    assert repo.load("u1").state.reorder_marks == {}


def test_the_new_run_reads_before_from_the_new_order(world, hooked):  # noqa: F811
    repo, _s, ids, path_id, _jobs, _q, runs, ps, _order, _old = queue_three(world, hooked)
    move_first(repo, ids, path_id, keys(repo, path_id)[2], "user")
    want, _ = predecessors(repo.load("u1").state, ps[2])
    new = next(r for r in runs.store.live_of("u1") if r.page_id == ps[2])
    assert want == () and tuple(new.job["before"]) == want          # first on the path now
    assert new.origin == "open" and new.n == 2


def test_an_edit_that_keeps_the_queue_order_touches_nothing(world, hooked):  # noqa: F811
    repo, _s, ids, path_id, _jobs, _q, runs, ps, _order, old = queue_three(world, hooked)
    move_first(repo, ids, path_id, keys(repo, path_id)[0], "user")   # page 1: already first
    assert [r.run_id for r in runs.store.live_of("u1")] == old
    assert live_pages(runs) == ps


def test_idle_runs_keep_their_origin_and_stay_behind_the_users(world, hooked):  # noqa: F811
    repo, s, ids, path_id = world
    jobs: list = []
    q, runs, _ = build(repo, s, ids, jobs=jobs, env=no_idle())
    hooked(q)
    ps = started(repo, ids, path_id)
    q.request("u1", [ps[0]])
    q.request("u1", [ps[1]], origin="idle")
    q.request("u1", [ps[2]], origin="idle")
    move_first(repo, ids, path_id, keys(repo, path_id)[2], "user")
    assert [(r.page_id, r.origin) for r in runs.store.live_of("u1")] == [
        (ps[0], "open"), (ps[2], "idle"), (ps[1], "idle")]


@pytest.mark.parametrize("source", ["user", "assistant", "bot"])
def test_an_in_flight_page_whose_predecessors_changed_is_marked(world, hooked, source):  # noqa: F811
    repo, s, ids, path_id = world
    jobs: list = []
    q, _runs, _ = build(repo, s, ids, jobs=jobs, env=no_idle())
    hooked(q)
    ps = started(repo, ids, path_id, 2)
    script_page(s, PAGE2)
    key = keys(repo, path_id)[1]
    in_flight(q, lambda: move_first(repo, ids, path_id, key, source))
    run = q.request("u1", [ps[1]])[0]
    jobs.pop()()
    assert repo.load("u1").state.reorder_marks[ps[1]].job_id == run


def test_a_bot_move_before_a_queued_page_is_followed_not_marked(world, hooked):  # noqa: F811
    """The weekly review puts an unstarted page before a queued one (`save_path` with source bot,
    the executor's route): the queued page is written after it, so it is not out of date."""
    repo, s, ids, path_id = world
    jobs: list = []
    q, _runs, _ = build(repo, s, ids, jobs=jobs, env=no_idle())
    hooked(q)
    ps = started(repo, ids, path_id, 2)                   # page 3 is not started
    script_page(s, PAGE2)
    q.request("u1", [ps[1]])
    snap = repo.load("u1")
    rec = snap.state.paths[path_id]
    ks = [i.key for i in path_order.shown(rec)]
    content = {"path_id": path_id, "op": "move", "page_key": ks[2], "before_key": ks[1]}
    try:
        save_path(repo, snap, bot_edited(snap.state, rec, content), now=NOW, new_id=ids,
                  source="bot")
    except (WikiConflict, WikiInvalid):                   # a prerequisite rule refuses this move
        commit_path_edit(repo, "u1", path_id, [MoveEdit(page_key=ks[2], to_index=1, by="bot")],
                         now=NOW, new_id=ids, source="bot")
    assert keys(repo, path_id)[1] == ks[2]
    jobs.pop()()
    assert repo.load("u1").state.reorder_marks == {}


class AskedLedger(FakeLedger):
    def __init__(self):
        super().__init__()
        self.asked = {}

    def asked_for(self, user_id, page_id, before):
        return self.asked.get(page_id)


def test_a_requeued_run_still_credits_the_asking_action(world, hooked):  # noqa: F811
    ledger = AskedLedger()
    repo, s, ids, path_id, jobs, _q, _runs, ps, _order, _old = queue_three(world, hooked, ledger)
    ledger.asked = {p: f"助手：写 {i}" for i, p in enumerate(ps)}
    move_first(repo, ids, path_id, keys(repo, path_id)[2], "user")
    script_page(s, PAGE1)
    script_page(s, PAGE3)
    script_page(s, PAGE2)
    drain(jobs)
    rows = {r["page_id"]: r for r in ledger.rows}
    assert set(rows) == set(ps)
    for i, p in enumerate(ps):
        assert rows[p]["content"]["asked"] == f"助手：写 {i}"
        assert rows[p]["why"] == f"你要的页面：助手：写 {i}"


def test_a_second_real_write_is_still_the_learners_own(world, hooked):  # noqa: F811
    ledger = AskedLedger()
    repo, s, ids, path_id = world
    jobs: list = []
    q, _runs, _ = build(repo, s, ids, jobs=jobs, env=no_idle(), ledger=ledger)
    hooked(q)
    ps = started(repo, ids, path_id, 1)
    ledger.asked = {ps[0]: "助手：写页"}
    script_page(s, PAGE1)
    run = q.request("u1", ps)[0]
    drain(jobs)
    _runs.store.consume(run, NOW)                         # read to the end
    script_page(s, PAGE1)
    q.request("u1", ps)                                   # 重新写
    drain(jobs)
    first, second = (r["content"] for r in ledger.rows)
    assert "asked" in first and "asked" not in second
