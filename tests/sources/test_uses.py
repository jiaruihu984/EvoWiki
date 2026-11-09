"""sources/uses.py apply_use: `auto` lets the material decide (via use.guess_material_use) and
goes through events.source_added and the full import exactly like an explicit use."""

from datetime import UTC, datetime

import pytest

from evowiki.sources import events, store
from evowiki.sources.flows import START_WAITS_S, retry_start
from evowiki.sources.models import SectionRow, SourceRow
from evowiki.sources.runs import MSG_INTERRUPTED
from evowiki.sources.uses import (
    MSG_NOT_STARTED,
    ReadFailed,
    RunNotStarted,
    StillReading,
    apply_use,
    record_not_started,
    start_unstarted,
)
from tests.runtime.helpers import make_user
from tests.sources.test_use import BOOK, PAPER

NOW = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)


class Runs:
    def __init__(self, fail: bool = False, existing=(), later: bool = False):
        self.started: list = []
        self.later: list = []                 # handed to the durable retries
        self.fail = fail
        self.can_later = later
        self.existing = set(existing)

    def exists(self, user_id, source_id, gen):
        return (source_id, gen) in self.existing

    def start(self, job):
        if self.fail:
            raise RuntimeError("no DBOS")
        self.started.append(job)

    def start_later(self, job):
        if not self.can_later:
            raise RuntimeError("no DBOS")
        self.later.append(job)


def add(pool, sid, title, titles=(), *, uses=("ask",), pages=10, text="正文。", user="u1"):
    row = SourceRow(source_id=sid, user_id=user, kind="pdf", title=title, sha256=sid, bytes=1,
                    pages=pages, status="ready" if titles else "reading", error=None,
                    unreadable_pages=(), toc_mode=None, progress={}, origin=None,
                    created_at=NOW, updated_at=NOW, deleted_at=None)
    with pool.connection() as conn:
        store.insert_source(conn, row, b"x", "application/pdf")
        for u in uses:
            store.add_use(conn, user, sid, u, session_id="chat1", now=NOW)
        if titles:
            store.replace_sections(conn, user, sid, [SectionRow(
                section_id=f"{sid}-{i}", source_id=sid, parent_id=None, ord=i, depth=0,
                title=t, page_from=None, page_to=None, text=text, tokens=1, summary=None,
                read_by="text") for i, t in enumerate(titles)])


def recorded(pool, sid, user="u1"):
    with pool.connection() as conn:
        return sorted(u["use"] for u in store.uses_of(conn, user, sid))


def exam_text() -> str:
    qs = [f"{i}. 下列说法正确的是（ ）\nA. 甲\nB. 乙" for i in range(1, 21)]
    return "\n".join(qs)


@pytest.fixture
def users(pool):
    make_user(pool, "u1")
    make_user(pool, "u2")
    return pool


def test_auto_guesses_paper_exam_and_lecture_notes(users, monkeypatch):
    pool = users
    add(pool, "paper", "注意力机制", PAPER)
    add(pool, "exam", "2023 年期末考试", ("一、选择题",), text=exam_text())
    add(pool, "notes", "线性代数讲义", BOOK, pages=120)
    seen = []
    real = events.source_added
    monkeypatch.setattr(events, "source_added",
                        lambda *a, **kw: seen.append(a[3]) or real(*a, **kw))
    runs = Runs()
    got = {sid: apply_use(pool, runs, "u1", sid, "auto", NOW).use
           for sid in ("paper", "exam", "notes")}
    assert got == {"paper": "understand", "exam": "exam", "notes": "learn"}
    assert seen == ["understand", "exam", "learn"]
    assert recorded(pool, "paper") == ["ask", "understand"]
    # each was a chat attachment (ask only): its first learning use starts the full import
    assert [(j.source_id, j.gen, j.mode) for j in runs.started] == [
        ("paper", 1, "full"), ("exam", 1, "full"), ("notes", 1, "full")]


def test_auto_is_the_same_as_the_explicit_use(users):
    pool = users
    add(pool, "a", "讲义", BOOK)
    add(pool, "b", "讲义", BOOK)
    runs = Runs()
    auto = apply_use(pool, runs, "u1", "a", "auto", NOW, session_id="s1")
    explicit = apply_use(pool, runs, "u1", "b", "learn", NOW, session_id="s1")
    assert auto.use == explicit.use == "learn"
    assert [j.gen for j in runs.started] == [1, 1]
    with pool.connection() as conn:
        assert {u["session_id"] for u in store.uses_of(conn, "u1", "a")} == {"chat1", "s1"}


def test_auto_while_still_reading_is_refused_and_records_nothing(users):
    pool = users
    add(pool, "r", "讲义")                         # no sections yet
    runs = Runs()
    with pytest.raises(StillReading) as e:
        apply_use(pool, runs, "u1", "r", "auto", NOW)
    assert e.value.detail == "资料还在读"
    assert recorded(pool, "r") == ["ask"] and runs.started == []


def test_a_second_learning_use_starts_nothing_once_the_full_import_is_done(users):
    pool = users
    add(pool, "a", "讲义", BOOK, uses=("ask", "learn"))
    add(pool, "old", "讲义", BOOK, uses=("ask", "learn"))      # an end from before modes
    with pool.connection() as conn:
        store.set_progress(conn, "u1", "a", {"end": "done", "mode": "full"})
        store.set_progress(conn, "u1", "old", {"end": "done"})
    runs = Runs()
    assert apply_use(pool, runs, "u1", "a", "auto", NOW).use == "learn"
    apply_use(pool, runs, "u1", "old", "exam", NOW)
    assert runs.started == []


def test_a_learning_use_while_the_full_run_is_going_starts_nothing(users):
    pool = users
    add(pool, "a", "讲义", BOOK, uses=("ask", "learn"))      # progress {}: a run is going
    runs = Runs()
    apply_use(pool, runs, "u1", "a", "learn", NOW)
    assert runs.started == []


def test_saying_it_again_after_the_full_run_could_not_start_starts_it(users):
    """The same learning use again after a run that could not start starts the full run."""
    pool = users
    add(pool, "a", "讲义", BOOK)
    with pytest.raises(RunNotStarted):
        apply_use(pool, Runs(fail=True), "u1", "a", "learn", NOW)
    runs = Runs()
    apply_use(pool, runs, "u1", "a", "learn", NOW)
    assert [(j.mode, j.gen) for j in runs.started] == [("full", 2)]
    assert recorded(pool, "a") == ["ask", "learn", "learn"]


def test_a_learning_use_after_an_interrupted_or_ask_mode_run_starts_the_full_run(users):
    pool = users
    add(pool, "cut", "讲义", BOOK, uses=("ask", "learn"))
    add(pool, "asked", "讲义", BOOK, uses=("ask", "learn"))   # only an ask-mode run finished
    with pool.connection() as conn:
        store.set_progress(conn, "u1", "cut", {"end": "error", "detail": MSG_INTERRUPTED})
        store.set_progress(conn, "u1", "asked", {"end": "done", "mode": "ask"})
    runs = Runs()
    apply_use(pool, runs, "u1", "cut", "learn", NOW)
    apply_use(pool, runs, "u1", "asked", "understand", NOW)
    assert [(j.source_id, j.mode) for j in runs.started] == [("cut", "full"), ("asked", "full")]


def test_foreign_unknown_and_deleted_sources_are_not_found(users):
    pool = users
    add(pool, "a", "讲义", BOOK)
    with pool.connection() as conn:
        store.soft_delete(conn, "u1", "a", NOW)
    add(pool, "b", "讲义", BOOK, user="u2")
    for sid in ("a", "b", "nope"):
        with pytest.raises(LookupError):
            apply_use(pool, Runs(), "u1", sid, "auto", NOW)


def test_a_run_that_cannot_start_keeps_the_use_and_says_so(users):
    pool = users
    add(pool, "a", "讲义", BOOK)
    with pytest.raises(RunNotStarted) as e:
        apply_use(pool, Runs(fail=True), "u1", "a", "auto", NOW)
    assert recorded(pool, "a") == ["ask", "learn"]
    with pool.connection() as conn:
        progress = store.get_source(conn, "u1", "a").progress
    # a plain failure: the text names no button
    assert progress["end"] == "error" and progress["detail"] == e.value.detail == MSG_NOT_STARTED
    assert "点" not in MSG_NOT_STARTED and "再跟我说" not in MSG_NOT_STARTED


def test_a_run_that_cannot_start_now_goes_to_the_durable_retries(users):
    pool = users
    add(pool, "a", "讲义", BOOK)
    runs = Runs(fail=True, later=True)
    apply_use(pool, runs, "u1", "a", "learn", NOW)             # no error: it is retried
    assert [(j.source_id, j.gen, j.mode) for j in runs.later] == [("a", 1, "full")]
    with pool.connection() as conn:
        assert "end" not in store.get_source(conn, "u1", "a").progress
    # the last try failed: only then the plain failure, once, for this run only
    assert record_not_started(pool, runs.later[0]) is True
    assert record_not_started(pool, runs.later[0]) is False     # ended already
    with pool.connection() as conn:
        assert store.get_source(conn, "u1", "a").progress["detail"] == MSG_NOT_STARTED


def test_the_durable_start_tries_with_waits_and_gives_up_last():
    tries, waits, gave_up = [], [], []

    def attempt(results):
        it = iter(results)
        return lambda: (tries.append(1), next(it))[1]
    assert retry_start(attempt(["error", "started"]), lambda: gave_up.append(1),
                       waits.append) == "started"
    assert waits == [START_WAITS_S[0]] and gave_up == []
    tries.clear()
    waits.clear()
    assert retry_start(attempt(["error"] * 9), lambda: gave_up.append(1) or True,
                       waits.append) == "failed"
    assert len(tries) == len(START_WAITS_S) + 1 and waits == list(START_WAITS_S)
    assert gave_up == [1]
    assert retry_start(attempt(["gone"]), lambda: gave_up.append(1), waits.append) == "gone"


def test_auto_on_a_failed_import_is_its_own_error(users):
    pool = users
    add(pool, "f", "讲义")                         # no sections
    with pool.connection() as conn:
        store.set_status(conn, "u1", "f", status="failed", error="加密，打不开")
    with pytest.raises(ReadFailed) as e:
        apply_use(pool, Runs(), "u1", "f", "auto", NOW)
    assert e.value.detail == "资料没有读出来" and recorded(pool, "f") == ["ask"]


def _unfinished(pool, sid, *, status="reading", progress=None, gen=0, uses=("ask",)):
    add(pool, sid, sid, uses=uses)
    with pool.connection() as conn:
        store.set_status(conn, "u1", sid, status=status, error=None)
        store.set_progress(conn, "u1", sid, progress or {})
        for _ in range(gen):
            store.bump_run(conn, "u1", sid)


def test_runs_that_never_started_are_started_at_start_up(users):
    """Nobody has to ask again: a run recorded but never started (the process died between
    the two) is started with its own generation and mode."""
    pool = users
    _unfinished(pool, "new")                                   # an upload whose start was lost
    _unfinished(pool, "rerun", status="ready", gen=1, uses=("ask", "learn"),
                progress={"stage": "parse", "done": 0, "total": 1, "message": "正在解析"})
    _unfinished(pool, "live")                                  # its run exists: left alone
    _unfinished(pool, "done", status="ready", progress={"end": "done"})
    _unfinished(pool, "failed", status="failed")
    runs = Runs(existing={("live", 0)})
    assert start_unstarted(pool, runs) == 2
    assert sorted((j.source_id, j.gen, j.mode) for j in runs.started) == [
        ("new", 0, "ask"), ("rerun", 1, "full")]
    with pool.connection() as conn:
        assert "end" not in store.get_source(conn, "u1", "new").progress
    assert MSG_INTERRUPTED                                      # still the stream's fallback
