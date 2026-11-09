"""Forgetting a session also deletes what DBOS keeps of its workflows (memory/forget.py
`delete_dbos_workflows`): the assistant turns of the session and of its tainted turns, their
children, the import workflows of a ticked material; a still-running one fails the request (it
is retried). DBOS is launched once on the test DB for its real schema; the workflow rows are
written directly. Postgres; RSI / S3 fakes; no model call."""

import psycopg
import pytest

from evowiki.agent import dbos_app
from evowiki.memory import forget as forget_mod
from evowiki.memory.forget import DBOS_PAYLOAD_TABLES, residue
from tests.memory.forget_world import build
from tests.memory.test_forget import _source

ALL = (*DBOS_PAYLOAD_TABLES, "workflow_status", "streams", "workflow_events",
       "workflow_events_history")          # notifications: destination_uuid, cascades too


@pytest.fixture
def w(pool, tmp_path, app_db_url):
    try:
        dbos_app.launch(app_db_url, app_version="forget-dbos-test")
    finally:
        dbos_app.shutdown()             # the schema stays; nothing runs while the test writes
    return build(pool, tmp_path)


def _workflow(conn, wid: str, *, status: str = "SUCCESS", parent: str | None = None) -> None:
    conn.execute("insert into dbos.workflow_status (workflow_uuid, status, name, inputs, output, "
                 "priority, parent_workflow_id) values (%s, %s, 'evowiki.assistant_turn', "
                 "'他问的原话', '模型的回答', 0, %s)", (wid, status, parent))
    conn.execute("insert into dbos.workflow_input (workflow_uuid, inputs, retention_timestamp) "
                 "values (%s, '他问的原话', 0)", (wid,))
    conn.execute("insert into dbos.workflow_output (workflow_uuid, output, retention_timestamp) "
                 "values (%s, '模型的回答', 0)", (wid,))
    conn.execute("insert into dbos.operation_outputs (workflow_uuid, function_id, function_name, "
                 "output, retention_timestamp) values (%s, 1, 'assistant.memory.recall', "
                 "'召回的内容', 0)", (wid,))
    conn.execute('insert into dbos.streams (workflow_uuid, key, value, "offset", function_id) '
                 "values (%s, 'events', '流式输出', 0, 2)", (wid,))
    conn.execute("insert into dbos.workflow_events (workflow_uuid, key, value) "
                 "values (%s, 'k', 'v')", (wid,))


def _left(w, wid: str) -> dict[str, int]:
    with w.pool.connection() as conn:
        return {t: conn.execute(f"select count(*) as n from dbos.{t} where workflow_uuid = %s",
                                (wid,)).fetchone()["n"] for t in ALL}


def test_every_dbos_table_with_a_workflow_id_is_deleted_explicitly_or_by_cascade(w, app_db_url):
    """The delete mirrors DBOS's SystemDatabase.delete_workflows: a table it does not delete must
    cascade from workflow_status. A DBOS upgrade adding a table fails here."""
    with psycopg.connect(app_db_url) as conn:
        tables = {r[0] for r in conn.execute(
            "select table_name from information_schema.columns where table_schema = 'dbos' "
            "and column_name = 'workflow_uuid'")}
        cascade = {r[0] for r in conn.execute(
            "select c.conrelid::regclass::text from pg_constraint c where c.contype = 'f' "
            "and c.confrelid = 'dbos.workflow_status'::regclass and c.confdeltype = 'c'")}
    cascade = {t.removeprefix("dbos.") for t in cascade}
    assert tables - {"workflow_status"} - set(DBOS_PAYLOAD_TABLES) <= cascade, (tables, cascade)


def test_session_turns_tainted_turns_children_and_ticked_material_imports_go(w):
    keep = ["turn:D:d1", "turn:B:b1", "source:u1:src_shared", "bot:check:u1:1:1"]
    gone = ["turn:A:a1", "turn:A:a2", "turn:B:b2", "turn:C:c2", "child-of-a1",
            "source:u1:src_only", "source:u1:src_only:2", "start:source:u1:src_only"]
    with w.pool.connection() as conn:
        _source(conn, "src_only", ("A", None))
        _source(conn, "src_shared", ("A", None), ("C", None))
        for wid in keep + [g for g in gone if g != "child-of-a1"]:
            _workflow(conn, wid)
        _workflow(conn, "child-of-a1", parent="turn:A:a1")
    out = w.service.forget_session("u1", "A", delete_source_ids=["src_only"], requested_by="test")
    assert out.state == "committed", out
    for wid in gone:
        assert sum(_left(w, wid).values()) == 0, (wid, _left(w, wid))
    for wid in keep:
        assert _left(w, wid)["workflow_status"] == 1 and _left(w, wid)["streams"] == 1, wid
    with w.pool.connection() as conn:
        steps = {r["step"]: r["rows"] for r in conn.execute(
            "select step, rows from forget_audit where request_id = %s", (out.request_id,))}
        assert residue(conn, "u1", "A") == []
    assert steps["dbos.workflow_status"] == len(gone)
    assert steps["dbos.operation_outputs"] == len(gone)


@pytest.mark.parametrize("status", ["PENDING", "ENQUEUED"])
def test_a_running_workflow_fails_the_request_until_it_finishes(w, status):
    with w.pool.connection() as conn:
        _workflow(conn, "turn:A:a2", status=status)
    out = w.service.forget_session("u1", "A", requested_by="test")
    assert out.state == "failed" and "ForgetBusy" in out.error
    assert _left(w, "turn:A:a2")["workflow_status"] == 1
    assert w.count("select 1 from assistant_entry where session_id = 'A'") == 4
    with w.pool.connection() as conn:
        conn.execute("update dbos.workflow_status set status = 'SUCCESS' "
                     "where workflow_uuid = 'turn:A:a2'")
    assert w.service.run(out.request_id).state == "committed"
    assert sum(_left(w, "turn:A:a2").values()) == 0


def test_a_workflow_left_behind_is_residue(w):
    assert w.service.forget_session("u1", "A", requested_by="test").state == "committed"
    with w.pool.connection() as conn:
        _workflow(conn, "turn:A:a9")        # e.g. restored from an old backup
        assert residue(conn, "u1", "A") == ["dbos.workflow_status"]
        assert forget_mod.dbos_workflows(conn, [], ["turn:A:"])[0]["workflow_uuid"] == "turn:A:a9"
