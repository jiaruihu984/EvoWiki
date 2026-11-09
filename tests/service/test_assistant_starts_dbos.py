"""The assistant's add_source / revise_page on real DBOS (Postgres): the tools run inside the turn's
DBOS step, where DBOS refuses to start a workflow; `assistant_import` / `assistant_revise` start
the import workflow and the page run from outside it (`durable.outside_dbos`)."""

from typing import Any

import pytest
from dbos import DBOS

from evowiki.agent import durable
from evowiki.service.deps import (
    DurableRuntime,
    assistant_import,
    assistant_revise,
    import_deps,
    page_deps,
)
from evowiki.sources.runs import DbosSourceRuns
from evowiki.wikiapp import page_flows
from evowiki.wikiapp.page_run_store import PgPageRunStore
from evowiki.wikiapp.page_runs import DbosPageRuns
from tests.service.test_revise_api import Env

_box: dict[str, Any] = {}


def _both(user_id: str, page_id: str) -> dict[str, Any]:
    """What the assistant's two action tools do, from inside a step."""
    from evowiki.assistant.tools.actions import ImportRequest
    svc = _box["svc"]
    req = ImportRequest(url=None, text="老师讲：z=-1 表示低一个标准差。", title="课堂",
                        use="supplement", page_id=page_id, session_id="s1")
    got = assistant_import(svc, user_id, req)
    row, run_id = assistant_revise(svc, user_id, page_id, [got.source_id], "revise:u1:k1")
    return {"source_id": got.source_id, "status": got.status, "action_id": row.action_id,
            "run_id": run_id}


@DBOS.workflow(name="evowiki.test_assistant_starts")
def _turn(user_id: str, page_id: str) -> dict[str, Any]:
    return durable.step("assistant.tool", lambda: _both(user_id, page_id))


@pytest.fixture
def dbos_env(services, client, db_url, pool):
    env = Env(services, client)
    services.runs = DbosPageRuns(PgPageRunStore(pool))
    services.sources = DbosSourceRuns(pool)
    rt = DurableRuntime(db_url, executor_id="test")
    services.durable = rt
    rt.sources = import_deps(services)
    _box["svc"] = services
    rt.start(page_deps(services))
    yield env
    rt.stop()
    page_flows._deps = None
    _box.clear()


def test_add_source_and_revise_page_start_their_workflows_from_inside_a_step(dbos_env):
    env = dbos_env
    got = DBOS.start_workflow(_turn, "u1", env.pid).get_result()
    sid = got["source_id"]
    assert got["status"] != "failed"
    # the import workflow ran (not refused inside the step): the material is ready
    assert DBOS.retrieve_workflow(f"source:u1:{sid}").get_result() == "ready"
    # the page run is a root workflow (not a child of the turn) and writes the draft
    st = DBOS.get_workflow_status(got["run_id"])
    assert st is not None and st.parent_workflow_id is None
    DBOS.retrieve_workflow(got["run_id"]).get_result()
    rv = env.action(got["action_id"])["revise"]
    assert rv["state"] == "ready" and [s["source_id"] for s in rv["sources"]] == [sid]
