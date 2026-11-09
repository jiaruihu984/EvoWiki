"""The import on real DBOS (Postgres): workflow id, step names, idempotent start, replay."""

from itertools import count
from pathlib import Path
from types import SimpleNamespace

import pytest

from evowiki.retrieval.embed import HashingEmbedder
from evowiki.sources import flows
from evowiki.sources.importer import ImportDeps, ImportJob, SourceDone, SourceFailed
from evowiki.sources.runs import MSG_QUEUED, DbosSourceRuns
from evowiki.sources.vision import NoVision
from tests.runtime.helpers import make_user
from tests.sources.test_importer import NOW, add_source, src

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "sources"


@pytest.fixture
def dbos_env(db_url, pool):
    from evowiki.agent import dbos_app

    make_user(pool, "u1")
    c = count(1)
    flows.install(ImportDeps(pool=pool, embedder=HashingEmbedder(), llm_for=lambda _u: None,
                             local=NoVision(), claude=NoVision(),
                      assistant_sees_images=lambda _u: False, clock=lambda: NOW,
                             new_id=lambda: f"d{next(c):05d}"))
    dbos_app.launch(db_url, app_version="t")
    yield pool
    dbos_app.shutdown()
    flows.uninstall()


def test_dbos_import_runs_once_replays_and_records_its_steps(dbos_env):
    from dbos import DBOS

    pool = dbos_env
    add_source(pool, "s1", (FIX / "outline.pdf").read_bytes())
    runs = DbosSourceRuns(pool)
    job = ImportJob(user_id="u1", source_id="s1")
    wid = runs.start(job)
    assert wid == "source:u1:s1"
    items = [i for i in runs.follow("u1", "s1")
             if not (i[0] == "progress" and i[1].message == MSG_QUEUED)]   # told while waiting only
    assert items[-1][0] == "done" and isinstance(items[-1][1], SourceDone)
    assert DBOS.retrieve_workflow(wid).get_result() == "ready"
    steps = [st["function_name"] for st in DBOS.list_workflow_steps(wid)]
    assert [s for s in steps if s.startswith("source.")] == [
        "source.parse", "source.layout", "source.vision", "source.toc", "source.chunk",
        "source.glossary", "source.embed"]
    assert src(pool, "s1").status == "ready"

    assert steps == ["source.parse", "source.layout", "source.vision", "source.toc",
                     "source.chunk", "source.glossary", "DBOS.writeStream", "source.embed", "run.finish",
                     "DBOS.writeStream", "DBOS.closeStream"]
    assert [n for n, _ in items if n != "progress"] == ["usable", "done"]
    assert runs.start(job) == wid                          # no second workflow: nothing reran
    assert [st["function_name"] for st in DBOS.list_workflow_steps(wid)] == steps
    assert list(runs.follow("u1", "s1")) == items          # replayed from the start


def test_dbos_start_later_starts_the_run_from_its_durable_retries(dbos_env):
    from dbos import DBOS

    from evowiki.sources.runs import start_workflow_id
    pool = dbos_env
    add_source(pool, "s1", (FIX / "outline.pdf").read_bytes())
    runs = DbosSourceRuns(pool)
    job = ImportJob(user_id="u1", source_id="s1")
    runs.start_later(job)
    assert DBOS.retrieve_workflow(start_workflow_id(job)).get_result() == "started"
    assert list(runs.follow("u1", "s1"))[-1][0] == "done"
    steps = [st["function_name"] for st in DBOS.list_workflow_steps(start_workflow_id(job))]
    assert steps == ["source_start.try"]
    runs.start_later(job)                       # the same run: the first workflow, no rerun
    assert [st["function_name"]
            for st in DBOS.list_workflow_steps(start_workflow_id(job))] == steps


def test_dbos_follow_of_an_unknown_run_is_told_from_the_state(dbos_env):
    runs = DbosSourceRuns(dbos_env)
    assert list(runs.follow("u1", "nope")) == [("error", SourceFailed(detail="找不到这份资料", code="source_not_found"))]
    runs.cancel("u1", "nope")                               # nothing to cancel: no error


def test_dbos_runs_fail_closed_when_dbos_is_not_launched(pool):
    with pytest.raises(RuntimeError, match="not launched"):
        DbosSourceRuns(pool).start(ImportJob(user_id="u1", source_id="s1"))


def test_installed_fails_closed_without_install(monkeypatch):
    monkeypatch.setattr(flows, "_deps", None)
    with pytest.raises(RuntimeError, match="install"):
        flows.installed()


def test_the_durable_runtime_installs_the_import_deps_before_launch(db_url, pool, monkeypatch):
    from evowiki.agent import dbos_app
    from evowiki.service.deps import DurableRuntime
    from evowiki.wikiapp import page_flows

    deps = ImportDeps(pool=pool, embedder=HashingEmbedder(), llm_for=lambda _u: None,
                      local=NoVision(), claude=NoVision(),
                      assistant_sees_images=lambda _u: False, clock=lambda: NOW, new_id=lambda: "x")
    rt = DurableRuntime(db_url, executor_id="test")
    rt.sources = deps
    seen = []
    real = dbos_app.launch

    def spy(*a, **kw):             # recovery runs at launch: the deps must be there already
        seen.append(flows._deps)
        real(*a, **kw)
    monkeypatch.setattr(dbos_app, "launch", spy)
    try:
        rt.start(object())          # type: ignore[arg-type]
        assert seen == [deps] and flows.installed() is deps
    finally:
        rt.stop()
        page_flows._deps = None
    assert flows._deps is None and not dbos_app.launched()


def test_dbos_follow_waits_for_a_run_that_was_just_asked_for(dbos_env):
    import threading
    import time

    from evowiki.sources import store
    pool = dbos_env
    add_source(pool, "s1", (FIX / "outline.pdf").read_bytes())
    runs = DbosSourceRuns(pool)
    runs.start(ImportJob(user_id="u1", source_id="s1"))
    assert list(runs.follow("u1", "s1"))[-1][0] == "done"
    with pool.connection() as conn:          # what a route does: bump, fresh record, then start
        gen = store.bump_run(conn, "u1", "s1")
        store.set_progress(conn, "u1", "s1", {"stage": "parse", "done": 0, "total": 1,
                                              "message": "正在解析"})
    later = threading.Timer(0.5, lambda: runs.start(ImportJob(user_id="u1", source_id="s1",
                                                              gen=gen)))
    later.start()
    t0 = time.monotonic()
    items = list(runs.follow("u1", "s1"))
    later.join()
    assert items[-1][0] == "done" and time.monotonic() - t0 >= 0.4


# A durable assistant turn runs its action executors inside a DBOS step (`agent.durable.step`);
# `mark_source_use` / a goal from a file start the import from there (found by the T11a walkthrough).
_IN_STEP: dict = {}


def _start_from_a_step(source_id: str) -> str:
    from evowiki.agent import durable
    return durable.step("test.start_import", lambda: _IN_STEP["runs"].start(
        ImportJob(user_id="u1", source_id=source_id)))


def _turn_like_workflow(source_id: str) -> str:
    return _start_from_a_step(source_id)


def test_dbos_start_from_inside_a_step_of_another_workflow_runs_the_import(dbos_env):
    from dbos import DBOS

    pool = dbos_env
    add_source(pool, "s1", (FIX / "outline.pdf").read_bytes())
    runs = DbosSourceRuns(pool)
    _IN_STEP["runs"] = runs
    wf = DBOS.workflow(name="test.sources.turn_like")(_turn_like_workflow)
    handle = DBOS.start_workflow(wf, "s1")
    assert handle.get_result() == "source:u1:s1"
    assert list(runs.follow("u1", "s1"))[-1][0] == "done"
    assert src(pool, "s1").status == "ready"
    assert DBOS.retrieve_workflow("source:u1:s1").get_result() == "ready"


def test_dbos_start_runs_in_an_empty_context_even_where_threads_inherit_it(pool, monkeypatch):
    """M2: the start thread gets a fresh context explicitly, not by relying on new threads not
    inheriting the caller's (a free-threaded build with thread_inherit_context would copy a
    step's DBOS context, where DBOS refuses to start a workflow)."""
    import contextlib
    import contextvars

    import dbos

    from evowiki.agent import dbos_app
    from evowiki.sources import runs as runs_mod

    caller = contextvars.ContextVar("caller", default=None)
    seen = []
    monkeypatch.setattr(dbos_app, "launched", lambda: True)
    monkeypatch.setattr(dbos, "SetWorkflowID", lambda _wid: contextlib.nullcontext())
    monkeypatch.setattr(runs_mod, "source_queue", lambda _name: SimpleNamespace(
        enqueue=lambda *_a, **_k: seen.append(caller.get())))
    real_submit = runs_mod.ThreadPoolExecutor.submit

    def inheriting_submit(self, fn, *args, **kwargs):     # what an inheriting build would do
        return real_submit(self, contextvars.copy_context().run, fn, *args, **kwargs)
    monkeypatch.setattr(runs_mod.ThreadPoolExecutor, "submit", inheriting_submit)
    caller.set("inside a step")
    DbosSourceRuns(pool).start(ImportJob(user_id="u1", source_id="s1"))
    assert seen == [None]


# ---- one import at a time ----------------------------------------------------------------------

class _GatedEmbedder(HashingEmbedder):
    """Blocks the first `embed_documents` call until released (an import that takes a while)."""

    def __init__(self):
        super().__init__()
        import threading
        self.entered = threading.Event()
        self.release = threading.Event()

    def embed_documents(self, texts):
        self.entered.set()
        assert self.release.wait(60)
        return super().embed_documents(texts)


def test_dbos_imports_run_one_at_a_time_and_the_waiting_one_says_queued(db_url, pool):
    from dbos import DBOS

    from evowiki.agent import dbos_app
    from evowiki.sources.runs import SOURCE_QUEUE

    make_user(pool, "u1")
    make_user(pool, "u2")
    c = count(1)
    emb = _GatedEmbedder()
    flows.install(ImportDeps(pool=pool, embedder=emb, llm_for=lambda _u: None,
                             local=NoVision(), claude=NoVision(),
                             assistant_sees_images=lambda _u: False, clock=lambda: NOW,
                             new_id=lambda: f"d{next(c):05d}"))
    dbos_app.launch(db_url, app_version="t")
    try:
        add_source(pool, "s1", (FIX / "outline.pdf").read_bytes())
        add_source(pool, "s2", (FIX / "outline.pdf").read_bytes(), user="u2")
        runs = DbosSourceRuns(pool)
        w1 = runs.start(ImportJob(user_id="u1", source_id="s1"))
        assert emb.entered.wait(60)                       # s1 is in its embed step
        w2 = runs.start(ImportJob(user_id="u2", source_id="s2"))
        assert DBOS.get_workflow_status(w1).status == "PENDING"
        assert DBOS.get_workflow_status(w1).queue_name == SOURCE_QUEUE
        assert DBOS.get_workflow_status(w2).status == "ENQUEUED"   # not started: s1 holds the slot
        first = next(iter(runs.follow("u2", "s2")))
        assert first[0] == "progress" and first[1].message == MSG_QUEUED
        assert src(pool, "s2", "u2").status == "reading"
        emb.release.set()
        assert list(runs.follow("u1", "s1"))[-1][0] == "done"
        items = list(runs.follow("u2", "s2"))
        assert items[-1][0] == "done"
        assert DBOS.retrieve_workflow(w2).get_result() == "ready"
    finally:
        emb.release.set()
        dbos_app.shutdown()
        flows.uninstall()


def test_queued_item_only_while_enqueued():
    from evowiki.sources.runs import queued_item
    assert queued_item("PENDING") is None and queued_item(None) is None
    assert queued_item("SUCCESS") is None
    name, p = queued_item("ENQUEUED")
    assert name == "progress" and p.message == MSG_QUEUED and p.stage == "parse"
