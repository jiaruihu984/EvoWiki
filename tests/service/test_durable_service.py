"""The service on DBOS (B1b Task 10): the app's lifespan launches DBOS, the organize route starts a
page workflow and follows its stream; without the launch, `DbosPageRuns.start` fails closed."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from evowiki.agent import dbos_app
from evowiki.service.app import create_app
from evowiki.service.deps import DurableRuntime, page_deps
from evowiki.wikiapp import page_flows
from evowiki.wikiapp.page_run_store import PgPageRunStore
from evowiki.wikiapp.page_runs import DbosPageRuns, PageJob
from tests.service.conftest import parse_sse
from tests.service.test_wiki_api import new_page, organize, script_page
from tests.wikiapp.script import Script


@pytest.fixture
def durable_svc(services, db_url, pool):
    script = Script()
    services.gateway = script.gateway()
    services.runs = DbosPageRuns(PgPageRunStore(pool))
    services.durable = DurableRuntime(db_url, executor_id="test")
    yield services, script
    if dbos_app.launched():
        dbos_app.shutdown()
    page_flows._deps = None


def test_lifespan_launches_dbos_organize_streams_and_the_run_row_is_done(durable_svc, pool):
    svc, script = durable_svc
    assert not dbos_app.launched()
    with TestClient(create_app(svc), base_url="http://127.0.0.1") as client:
        assert dbos_app.launched()
        pid = new_page((client, script))
        script_page(script)
        r = organize(client, pid)
        assert r.status_code == 200, r.text
        events = parse_sse(r.text)
        assert next(e for e, _ in events if e != "planning") == "outline" and events[-1][0] == "done"
        row = PgPageRunStore(pool).get(f"page:u1:{pid}:1")
        assert row is not None and row.status == "done" and row.consumed_at is not None
        # the first run was read to the end: a reorganize starts run 2
        script_page(script)
        assert organize(client, pid).status_code == 200
        assert PgPageRunStore(pool).get(f"page:u1:{pid}:2").status == "done"
    assert not dbos_app.launched()


def test_dbos_runs_fail_closed_when_dbos_is_not_launched(pool):
    store = PgPageRunStore(pool)
    runs = DbosPageRuns(store)
    job = PageJob(kind="organize", user_id="u1", page_id="p1",
                  now=datetime(2026, 1, 1, tzinfo=UTC))
    with pytest.raises(RuntimeError, match="not launched"):
        runs.start(job, None, attach=True)    # type: ignore[arg-type]
    assert store.live_of("u1") == []                    # no row was opened


def test_after_launch_hooks_run_in_order_and_a_raising_hook_fails_startup(services, db_url):
    seen: list[str] = []
    rt = DurableRuntime(db_url, executor_id="test")
    rt.after_launch.append(lambda: seen.append("a" if dbos_app.launched() else "not launched"))
    rt.after_launch.append(lambda: seen.append("b"))
    deps = page_deps(services)
    try:
        rt.start(deps)
        assert seen == ["a", "b"]
    finally:
        rt.stop()
        page_flows._deps = None
    bad = DurableRuntime(db_url, executor_id="test")

    def boom() -> None:
        raise RuntimeError("hook failed")
    bad.after_launch.append(boom)
    with pytest.raises(RuntimeError, match="hook failed"):
        bad.start(deps)
    assert not dbos_app.launched()
    with pytest.raises(RuntimeError, match="install"):
        page_flows.installed()


def test_a_failed_launch_leaves_dbos_stopped_and_deps_uninstalled(services, db_url, monkeypatch):
    deps = page_deps(services)
    rt = DurableRuntime(db_url, executor_id="test")

    def boom(*a, **k):
        raise RuntimeError("launch failed")
    monkeypatch.setattr(dbos_app, "launch", boom)
    with pytest.raises(RuntimeError, match="launch failed"):
        rt.start(deps)
    assert not dbos_app.launched()
    with pytest.raises(RuntimeError, match="install"):
        page_flows.installed()


def test_the_sweep_runs_after_launch_before_the_hooks(services, db_url):
    seen: list[str] = []
    rt = DurableRuntime(db_url, executor_id="test")
    rt.after_launch.append(lambda: seen.append("hook"))
    try:
        rt.start(page_deps(services), sweep=lambda: seen.append(
            "sweep launched" if dbos_app.launched() else "sweep"))
        assert seen == ["sweep launched", "hook"]
    finally:
        rt.stop()
        page_flows._deps = None


def test_lifespan_installs_the_assistant_and_a_turn_streams_on_dbos(durable_svc, tmp_path,
                                                                    monkeypatch):
    from evowiki.assistant import turn_runs
    from evowiki.assistant.loop import TurnDeps
    from evowiki.assistant.service import AssistantService
    from evowiki.assistant.session import MemorySessionStore
    from evowiki.assistant.turn_runs import DbosTurnRuns
    from evowiki.llm.chat import ChatTurn
    from evowiki.llm.gateway import Gateway
    from evowiki.llm.log import CallLog
    from evowiki.llm.models import GatewayConfig, ModelRef
    from evowiki.llm.scripted import ScriptedChatProvider
    from tests.assistant.fakes import T0, Data, Ids, tools
    from tests.service.test_assistant_api import Applier

    svc, _ = durable_svc
    ref = ModelRef(provider="scripted", model="m")
    gateway = Gateway(GatewayConfig(generator=ref, verifier=ref, allow_single_provider=True),
                      CallLog(tmp_path / "c.jsonl"), mode="record",
                      providers={"scripted": ScriptedChatProvider(
                          "scripted", lambda m, r: ChatTurn(text="答" * 100))})
    deps = TurnDeps(gateway=gateway, tools=tools(), store=MemorySessionStore(), data=Data(),
                    new_id=Ids(), clock=lambda: T0)
    svc.assistant = AssistantService(deps, Applier(), DbosTurnRuns())
    h = {"X-EvoWiki-User": "u1"}
    at_launch = {}
    real_launch = dbos_app.launch

    def spy(*args, **kw):          # what is installed at the moment DBOS launches (recovery runs then)
        at_launch["turns"] = turn_runs._service
        at_launch["pages"] = page_flows._deps
        return real_launch(*args, **kw)
    monkeypatch.setattr(dbos_app, "launch", spy)
    with TestClient(create_app(svc), base_url="http://127.0.0.1") as client:
        assert at_launch["turns"] is svc.assistant and at_launch["pages"] is not None
        assert turn_runs.installed() is svc.assistant
        sid = client.post("/assistant/sessions", json={"page_id": "p1"},
                          headers=h).json()["session_id"]
        r = client.post(f"/assistant/sessions/{sid}/messages", json={"text": "问"}, headers=h)
        assert r.status_code == 200, r.text
        names = [ev for ev, _ in parse_sse(r.text)]
        assert names[-1:] == ["done"]
        msgs = client.get(f"/assistant/sessions/{sid}", headers=h).json()["messages"]
        assert [m["role"] for m in msgs] == ["user", "assistant"]
    with pytest.raises(RuntimeError, match="install"):
        turn_runs.installed()                                  # stop() uninstalled it


def test_a_failed_launch_uninstalls_the_assistant(services, db_url, monkeypatch, tmp_path):
    from evowiki.assistant import turn_runs
    from evowiki.llm.chat import ChatTurn
    from tests.service.test_assistant_api import service as assistant_service

    a = assistant_service(tmp_path, [ChatTurn(text="x")])
    rt = DurableRuntime(db_url, executor_id="test")

    def boom(*args, **kw):
        raise RuntimeError("launch failed")
    monkeypatch.setattr(dbos_app, "launch", boom)
    with pytest.raises(RuntimeError, match="launch failed"):
        rt.start(page_deps(services), assistant=a)
    with pytest.raises(RuntimeError, match="install"):
        turn_runs.installed()
    page_flows._deps = None


def test_lifespan_startup_sweeps_a_writing_row_and_hooks_may_call_sync_dbos(durable_svc, pool):
    """Regression (a restart with a `writing` page run): the startup sweep and the after-launch
    hooks call DBOS's sync API (get_workflow_status / list_workflows / register_queue), which DBOS
    refuses on a thread with a running event loop - the lifespan must run the start off the loop."""
    from dbos import DBOS
    svc, _ = durable_svc
    store = PgPageRunStore(pool)
    job = PageJob(kind="organize", user_id="u1", page_id="p_stale",
                  now=datetime(2026, 1, 1, tzinfo=UTC))
    row, created = store.open_run(user_id="u1", page_id="p_stale", kind="organize",
                                  origin=job.origin, job=job.model_dump(mode="json"),
                                  now=job.now, attach=False)
    assert created and row.status == "writing"
    hooked: list[int] = []
    svc.durable.after_launch.append(lambda: hooked.append(len(DBOS.list_workflows(
        workflow_id_prefix="page:", load_input=False, load_output=False))))
    with TestClient(create_app(svc), base_url="http://127.0.0.1"):
        assert dbos_app.launched()
        # no workflow, older than the grace period: the sweep closed it as failed
        assert store.get(row.run_id).status == "failed"
        assert hooked == [0]
    assert not dbos_app.launched()


def test_lifespan_a_raising_start_aborts_startup_with_the_original_error(durable_svc):
    svc, _ = durable_svc

    def boom() -> None:
        raise ValueError("hook failed")
    svc.durable.after_launch.append(boom)
    with pytest.raises(ValueError, match="hook failed"), TestClient(create_app(svc), base_url="http://127.0.0.1"):
        pass
    assert not dbos_app.launched()
