"""page_run store: attach rules, the queue's view of a user's runs (live, latest, finished today),
begin / progress / finish with cost and failure."""
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from evowiki.store.migrate import migrate
from evowiki.wikiapp.page_run_store import (
    KEEP_FINISHED,
    OPEN_TRIES,
    MemoryPageRunStore,
    PgPageRunStore,
)

T0 = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)


@pytest.fixture(params=["memory", "pg"])
def store(request):
    if request.param == "memory":
        return MemoryPageRunStore()
    pool = request.getfixturevalue("pool")
    with request.getfixturevalue("admin_pool").connection() as conn:     # owner: migrate, truncate
        migrate(conn)
        conn.execute("truncate page_run")
    return PgPageRunStore(pool)


def open_(store, page="p1", kind="organize", attach=True, now=T0, user="u1", origin="open"):
    return store.open_run(user_id=user, page_id=page, kind=kind, origin=origin, job={"k": 1},
                          now=now, attach=attach)


def test_new_run_numbering_and_fields(store):
    r1, c1 = open_(store)
    r2, c2 = open_(store, attach=False)
    assert (c1, c2) == (True, True)
    assert (r1.n, r2.n) == (1, 2) and r1.run_id == "page:u1:p1:1"
    assert r1.status == "writing" and r1.job == {"k": 1} and r1.began_at is None
    assert store.get(r2.run_id) == r2 and store.get("nope") is None
    assert open_(store, page="p2")[0].n == 1


def test_writing_run_attaches(store):
    r1, _ = open_(store)
    r2, created = open_(store)
    assert (r2.run_id, created) == (r1.run_id, False)


def test_attach_false_always_creates(store):
    r1, _ = open_(store)
    r2, created = open_(store, attach=False)
    assert created and r2.run_id != r1.run_id


def test_done_unconsumed_within_keep_attaches_then_expires(store):
    r1, _ = open_(store)
    store.finish(r1.run_id, "done", T0)
    assert open_(store, now=T0 + KEEP_FINISHED - timedelta(seconds=1))[1] is False
    r3, created = open_(store, now=T0 + KEEP_FINISHED + timedelta(seconds=1))
    assert created and r3.n == 2


def test_a_page_the_idle_bot_wrote_waits_for_its_first_reader_however_old(store):
    r1, _ = open_(store, origin="idle")
    store.finish(r1.run_id, "done", T0)
    for later in (timedelta(hours=3), timedelta(days=2), timedelta(days=400)):
        got, created = open_(store, now=T0 + KEEP_FINISHED + later)
        assert (got.run_id, created) == (r1.run_id, False)      # opening never writes it again
    store.consume(r1.run_id, T0)
    assert open_(store, now=T0 + timedelta(days=401))[1] is True


def test_consumed_and_failed_do_not_attach(store):
    r1, _ = open_(store)
    store.finish(r1.run_id, "done", T0)
    store.consume(r1.run_id, T0)
    r2, created = open_(store, now=T0)
    assert created
    store.finish(r2.run_id, "failed", T0)
    r3, created = open_(store, now=T0)
    assert created and r3.n == 3


def test_a_discarded_run_is_never_attached(store):
    r1, _ = open_(store)
    store.discard(r1.run_id, T0)
    r2, created = open_(store)
    assert created and r2.n == 2


def test_feedback_rows_are_never_attached(store):
    r1, _ = open_(store, kind="feedback")
    r2, created = open_(store, kind="feedback")
    assert created and r2.run_id != r1.run_id
    r3, created = open_(store, kind="organize")       # a feedback run is not an organize run
    assert created and r3.n == 3


def test_begin_progress_and_finish_with_cost_and_failure(store):
    r, _ = open_(store, origin="idle")
    assert store.get(r.run_id).began_at is None
    store.begin(r.run_id, T0 + timedelta(seconds=5))
    store.begin(r.run_id, T0 + timedelta(seconds=9))          # the first begin stands
    store.set_progress(r.run_id, {"section": 2, "of": 4})
    got = store.get(r.run_id)
    assert got.began_at == T0 + timedelta(seconds=5) and got.progress == {"section": 2, "of": 4}
    store.finish(r.run_id, "done", T0 + timedelta(minutes=1), cost_usd=0.03, llm_calls=7)
    got = store.get(r.run_id)
    assert (got.status, got.cost_usd, got.llm_calls, got.failure) == ("done", 0.03, 7, None)
    store.finish(r.run_id, "failed", T0, failure="x")          # only a `writing` row moves
    assert store.get(r.run_id).status == "done"
    f, _ = open_(store, page="p2")
    store.finish(f.run_id, "failed", T0, failure="llm_unavailable")
    assert store.get(f.run_id).failure == "llm_unavailable"


def test_live_latest_and_finished_since(store):
    a, _ = open_(store, page="p1", origin="idle", now=T0)
    b, _ = open_(store, page="p2", origin="open", now=T0 + timedelta(seconds=1))
    c, _ = open_(store, page="p3", origin="idle", now=T0 + timedelta(seconds=2))
    other, _ = open_(store, page="p1", user="u2")
    assert [r.run_id for r in store.live_of("u1")] == [a.run_id, b.run_id, c.run_id]
    store.discard(c.run_id, T0)
    assert [r.run_id for r in store.live_of("u1")] == [a.run_id, b.run_id]
    store.finish(a.run_id, "failed", T0 + timedelta(minutes=1), failure="x")
    store.finish(b.run_id, "done", T0 + timedelta(minutes=2))
    a2, _ = open_(store, page="p1", attach=False, origin="idle", now=T0 + timedelta(minutes=3))
    store.finish(a2.run_id, "done", T0 + timedelta(minutes=4), cost_usd=0.01)
    latest = store.latest_of("u1", ["p1", "p2", "p9"])
    assert {p: r.run_id for p, r in latest.items()} == {"p1": a2.run_id, "p2": b.run_id}
    assert store.latest_of("u1", []) == {}
    assert [r.run_id for r in store.finished_since("u1", T0, origin="idle")] == [a2.run_id, a.run_id]
    assert store.finished_since("u1", T0 + timedelta(minutes=3), origin="idle")[0].run_id == a2.run_id
    assert store.finished_since("u1", T0, origin="open")[0].run_id == b.run_id
    assert store.latest_of("u2", ["p1"])["p1"].run_id == other.run_id


def test_open_run_gives_up_after_a_few_unique_violations(store, monkeypatch):
    if not isinstance(store, PgPageRunStore):
        pytest.skip("the memory store serializes openers")
    calls = []

    def always(*a, **k):
        calls.append(1)
        raise psycopg.errors.UniqueViolation("page_run_pkey")
    monkeypatch.setattr(store, "_open_once", always)
    with pytest.raises(psycopg.errors.UniqueViolation):
        open_(store)
    assert len(calls) == OPEN_TRIES


def test_discard_acts_once(store):
    q, _ = open_(store, page="p2")
    assert store.discard(q.run_id, T0) is True and store.discard(q.run_id, T0) is False
    store.finish(q.run_id, "done", T0)
    store.finish(q.run_id, "failed", T0)                  # only a `writing` row moves
    assert store.get(q.run_id).status == "done"


def test_queued_only_discard_spares_a_run_that_began(store):
    began, _ = open_(store, page="p1")
    queued, _ = open_(store, page="p2")
    store.begin(began.run_id, T0)
    assert store.discard(began.run_id, T0, queued_only=True) is False
    assert store.get(began.run_id).discarded_at is None
    assert store.discard(queued.run_id, T0, queued_only=True) is True


def test_has_begun_before_counts_only_runs_that_began(store):
    dropped, _ = open_(store)
    store.discard(dropped.run_id, T0, queued_only=True)        # re-queued: never began
    second, _ = open_(store, attach=False)
    assert store.has_begun_before("u1", "p1", second.n) is False
    store.begin(second.run_id, T0)
    third, _ = open_(store, attach=False)
    assert store.has_begun_before("u1", "p1", third.n) is True
    assert store.has_begun_before("u1", "p1", second.n) is False
