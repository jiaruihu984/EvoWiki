"""The supplement match on real DBOS (Postgres): started from inside the import's last step,
one workflow per source, its steps, the event; and the durable runtime installs its deps."""

from itertools import count

import pytest

from evowiki.retrieval.embed import HashingEmbedder
from evowiki.sources import flows, match_flow
from evowiki.sources.importer import ImportDeps, ImportJob
from evowiki.sources.match import MatchPlan, MatchVerdict
from evowiki.sources.match_flow import MatchDeps, start_match, workflow_id
from evowiki.sources.runs import DbosSourceRuns
from evowiki.sources.vision import NoVision
from evowiki.wikiapp.events import PageSaved
from evowiki.wikiapp.repo import PgRepo
from tests.runtime.helpers import make_user
from tests.sources._fakes import FakeLLM
from tests.sources.test_importer import NOW, add_source
from tests.sources.test_match_flow import MATERIAL, record, save


@pytest.fixture
def dbos_env(db_url, pool):
    from evowiki.agent import dbos_app

    make_user(pool, "u1")
    repo = PgRepo(pool)
    save(repo, "u1", PageSaved(record=record("pmean", "Mean", "the mean is the sum of the values "
                                             "divided by how many values there are"),
                               action="done"))
    c = count(1)
    box = {"llm": FakeLLM(MatchPlan(verdicts=[MatchVerdict(n=1, sections=[1], why="例子")])),
           "notified": []}
    flows.install(ImportDeps(pool=pool, embedder=HashingEmbedder(), llm_for=lambda _u: None,
                             local=NoVision(), claude=NoVision(),
                             assistant_sees_images=lambda _u: False, clock=lambda: NOW,
                             new_id=lambda: f"d{next(c):05d}", on_ready=start_match))
    match_flow.install(MatchDeps(pool=pool, repo=repo, embedder=HashingEmbedder(),
                                 llm_for=lambda _u: box["llm"], clock=lambda: NOW,
                                 notify=box["notified"].append))
    dbos_app.launch(db_url, app_version="t")
    yield pool, repo, box
    dbos_app.shutdown()
    flows.uninstall()
    match_flow.uninstall()


def test_a_finished_import_starts_the_match_once(dbos_env):
    from dbos import DBOS

    pool, repo, box = dbos_env
    add_source(pool, "s1", MATERIAL, kind="md")
    runs = DbosSourceRuns(pool)
    wid = runs.start(ImportJob(user_id="u1", source_id="s1"))
    assert list(runs.follow("u1", "s1"))[-1][0] == "done"
    steps = [st["function_name"] for st in DBOS.list_workflow_steps(wid)]
    assert steps[steps.index("run.finish") + 1] == "source.match_start"
    mid = workflow_id("u1", "s1")
    DBOS.retrieve_workflow(mid).get_result()
    assert [st["function_name"] for st in DBOS.list_workflow_steps(mid)] == [
        "match.uses", "match.vectors", "match.judge", "match.commit"]
    (ev,) = repo.wiki_events_of("u1", ["source_matched"])
    assert [p.page_id for p in ev.body.pages] == ["pmean"]
    assert box["notified"] == ["u1"]
    start_match("u1", "s1")                                # the same id: nothing reruns
    assert len(DBOS.list_workflow_steps(mid)) == 4 and len(box["llm"].calls) == 1


def test_the_durable_runtime_installs_the_match_deps_before_launch(db_url, pool, monkeypatch):
    from evowiki.agent import dbos_app
    from evowiki.service.deps import DurableRuntime
    from evowiki.wikiapp import page_flows

    deps = MatchDeps(pool=pool, repo=PgRepo(pool), embedder=HashingEmbedder(),
                     llm_for=lambda _u: None, clock=lambda: NOW)
    rt = DurableRuntime(db_url, executor_id="test")
    rt.matches = deps
    seen = []
    real = dbos_app.launch

    def spy(*a, **kw):             # recovery runs at launch: the deps must be there already
        seen.append(match_flow._deps)
        real(*a, **kw)
    monkeypatch.setattr(dbos_app, "launch", spy)
    try:
        rt.start(object())          # type: ignore[arg-type]
        assert seen == [deps] and match_flow.installed() is deps
    finally:
        rt.stop()
        page_flows._deps = None
    assert match_flow._deps is None and not dbos_app.launched()
