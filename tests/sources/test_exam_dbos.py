"""The exam extraction on real DBOS (Postgres): started from inside the import's last step
(`on_ready`), one workflow per import run, its steps (the step values go through DBOS
serialization), a second start with the same id reruns nothing; and the durable runtime
installs its deps before launch. FakeLLM only: no model is ever called."""

from itertools import count

import pytest

from evowiki.belief.aspects import DefineConcept
from evowiki.retrieval.embed import HashingEmbedder
from evowiki.sources import exam_flow, exam_store, flows, store
from evowiki.sources.exam_flow import ExamDeps, start_exam, workflow_id
from evowiki.sources.importer import ImportDeps, ImportJob
from evowiki.sources.runs import DbosSourceRuns
from evowiki.sources.vision import NoVision
from evowiki.wikiapp.events import GoalMapSaved, WikiEvent
from evowiki.wikiapp.repo import Commit, PgRepo
from tests.runtime.helpers import make_user
from tests.sources._fakes import FakeLLM
from tests.sources.test_exam_flow import ITEMS, PAPER, PICKS, _map
from tests.sources.test_importer import NOW, add_source


@pytest.fixture
def dbos_env(db_url, pool):
    from evowiki.agent import dbos_app

    make_user(pool, "u1")
    repo = PgRepo(pool)
    repo.commit(repo.load("u1"), Commit(
        aspect=[DefineConcept(event_id=f"aev-{c}", ts=NOW, concept_id=c,
                              aspects=("term", "procedure")) for c in ("c1", "c2")],
        wiki=[WikiEvent(event_id="wev-m1", ts=NOW, body=GoalMapSaved(
            record=_map("m1", ("c1", "均值"), ("c2", "中位数")), action="planned"))]))
    c = count(1)
    n = count(1)
    box = {"llm": FakeLLM(ITEMS, PICKS)}
    flows.install(ImportDeps(pool=pool, embedder=HashingEmbedder(), llm_for=lambda _u: None,
                             local=NoVision(), claude=NoVision(),
                             assistant_sees_images=lambda _u: False, clock=lambda: NOW,
                             new_id=lambda: f"d{next(c):05d}", on_ready=start_exam))
    exam_flow.install(ExamDeps(pool=pool, repo=repo, embedder=HashingEmbedder(),
                               llm_for=lambda _u: box["llm"], clock=lambda: NOW,
                               new_id=lambda prefix: f"{prefix}-{next(n)}"))
    dbos_app.launch(db_url, app_version="t")
    yield pool, repo, box
    dbos_app.shutdown()
    flows.uninstall()
    exam_flow.uninstall()


def test_a_finished_import_starts_the_exam_extraction_once(dbos_env):
    from dbos import DBOS

    pool, _repo, box = dbos_env
    add_source(pool, "s1", PAPER, kind="md")
    with pool.connection() as conn:
        store.add_use(conn, "u1", "s1", "exam", goal_id="m1", now=NOW)
    runs = DbosSourceRuns(pool)
    wid = runs.start(ImportJob(user_id="u1", source_id="s1"))
    assert list(runs.follow("u1", "s1"))[-1][0] == "done"
    steps = [st["function_name"] for st in DBOS.list_workflow_steps(wid)]
    assert steps[steps.index("run.finish") + 1] == "source.match_start"     # the on_ready step
    with pool.connection() as conn:
        gen = store.get_source(conn, "u1", "s1").run_gen
    eid = workflow_id("u1", "s1", gen)
    assert DBOS.retrieve_workflow(eid).get_result() == "done"
    assert [st["function_name"] for st in DBOS.list_workflow_steps(eid)] == [
        "exam.load", "exam.extract", "exam.attach", "exam.commit"]
    with pool.connection() as conn:
        got = exam_store.items_of_source(conn, "u1", "s1")
        exam = store.get_source(conn, "u1", "s1").progress["exam"]
    assert [(it.number, it.status, it.attach) for it in got][:3] == [
        ("1", "ok", "attached"), ("2", "ok", "attached"), ("3", "ok", "unattached")]
    assert exam["state"] == "done" and exam["items"] == 5
    assert len(box["llm"].calls) == 2                       # one extraction, one attach call
    start_exam("u1", "s1")                                  # the same id: nothing reruns
    assert DBOS.retrieve_workflow(eid).get_result() == "done"
    assert len(DBOS.list_workflow_steps(eid)) == 4 and len(box["llm"].calls) == 2


def test_the_durable_runtime_installs_the_exam_deps_before_launch(db_url, pool, monkeypatch):
    from evowiki.agent import dbos_app
    from evowiki.service.deps import DurableRuntime
    from evowiki.wikiapp import page_flows

    deps = ExamDeps(pool=pool, repo=PgRepo(pool), embedder=HashingEmbedder(),
                    llm_for=lambda _u: None, clock=lambda: NOW, new_id=lambda p: f"{p}-1")
    rt = DurableRuntime(db_url, executor_id="test")
    rt.exams = deps
    seen = []
    real = dbos_app.launch

    def spy(*a, **kw):             # recovery runs at launch: the deps must be there already
        seen.append(exam_flow._deps)
        real(*a, **kw)
    monkeypatch.setattr(dbos_app, "launch", spy)
    try:
        rt.start(object())          # type: ignore[arg-type]
        assert seen == [deps] and exam_flow.installed() is deps
    finally:
        rt.stop()
        page_flows._deps = None
    assert exam_flow._deps is None and not dbos_app.launched()
