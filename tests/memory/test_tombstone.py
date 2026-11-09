"""The out-of-database forget ledger and the startup reconcile (plan Task 14). Postgres; fakes for
RSI's and S3's steps (tests/memory/forget_world.py); no model call."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from evowiki.memory.forget import PLACEHOLDER
from evowiki.memory.tombstone import (
    CLOSED_DETAIL,
    Ledger,
    LedgerError,
    ReconcileError,
    ledger_path,
    reconcile,
)
from evowiki.service.app import create_app
from tests.memory.forget_world import TAINTED, build

BACKED_UP = ("memory_dependency", "memory_suppression_closure", "dream_summary",
             "dream_source", "dream_pending", "assistant_session", "assistant_entry", "memory_episode", "memory_item")


@pytest.fixture
def w(pool, tmp_path):
    return build(pool, tmp_path)


def _state(w, rid):
    with w.pool.connection() as conn:
        return conn.execute("select state from forget_request where request_id = %s",
                            (rid,)).fetchone()["state"]


def test_ledger_rows_are_appended_and_read_back(tmp_path):
    led = Ledger(tmp_path / "sub" / "l.jsonl")
    led.append("r1", "u1", "A", None, "requested", revoke_rule_ids=["si1"])
    led.append("r1", "u1", "A", None, "committed")
    led.append("r2", "u1", None, "it", "requested")
    got = led.requests()
    assert got["r1"].state == "committed" and got["r1"].revoke_rule_ids == ("si1",)
    assert got["r2"].item_id == "it" and led.revoke_of("r2") == ((), ())
    assert len((tmp_path / "sub" / "l.jsonl").read_text(encoding="utf-8").splitlines()) == 3
    assert ledger_path({"EVOWIKI_FORGET_LEDGER": str(tmp_path / "x")}) == tmp_path / "x"
    assert ledger_path({}).name == "forget-ledger.jsonl"


def test_a_corrupt_ledger_fails_closed(tmp_path, pool):
    (tmp_path / "l.jsonl").write_text('{"request_id": "r1"\n', encoding="utf-8")
    with pytest.raises(LedgerError):
        Ledger(tmp_path / "l.jsonl").requests()


def test_unwritable_ledger_leaves_no_request(pool, tmp_path):
    w = build(pool, tmp_path)
    (tmp_path / "dir.jsonl").mkdir()
    w.service._ledger = Ledger(tmp_path / "dir.jsonl")      # a directory: open() fails
    with pytest.raises(LedgerError):
        w.service.forget_session("u1", "A", requested_by="test")
    assert w.count("select 1 from forget_request") == 0
    assert w.count("select 1 from assistant_entry where session_id = 'A'") == 4


def test_reconcile_finishes_a_request_left_requested(w):
    rid = w.service.request_session("u1", "A", requested_by="test")    # the process died here
    report = reconcile(w.pool, w.ledger, w.service)
    assert report.run == [rid] and report.verified == 1
    assert _state(w, rid) == "committed" and w.ledger.requests()[rid].state == "committed"
    assert w.count("select 1 from assistant_session where session_id = 'A'") == 0


def test_reconcile_raises_while_a_delete_keeps_failing(w):
    w.harness.fail = RuntimeError("rsi down")
    rid = w.service.request_session("u1", "A", requested_by="test")
    with pytest.raises(ReconcileError):
        reconcile(w.pool, w.ledger, w.service)
    assert _state(w, rid) == "failed"
    w.harness.fail = None
    assert reconcile(w.pool, w.ledger, w.service).run == [rid]


def _backup(admin_pool) -> None:
    with admin_pool.connection() as conn:
        for t in BACKED_UP:
            conn.execute(f"create table bk_{t} as select * from {t}")


def restore_backup(admin_pool) -> None:
    """The database as the backup holds it (from before the delete); the ledger is outside it."""
    with admin_pool.connection() as conn, conn.transaction():
        conn.execute("set local session_replication_role = replica")   # triggers off (restore)
        for t in reversed(BACKED_UP):
            conn.execute(f"delete from {t}")
        for t in BACKED_UP:
            conn.execute(f"insert into {t} select * from bk_{t}")
        conn.execute("delete from forget_frontier")
        conn.execute("delete from forget_audit")
        conn.execute("delete from forget_request")
        conn.execute("delete from forgotten_session")


def test_restoring_an_old_backup_is_undone_by_reconcile(w, admin_pool):
    _backup(admin_pool)
    first = w.service.forget_session("u1", "A", requested_by="test")
    assert first.state == "committed"
    restore_backup(admin_pool)
    assert w.count("select 1 from assistant_entry where session_id = 'A'") == 4
    assert w.turn_rows("B", "b2")[1]["payload"]["text"] != PLACEHOLDER

    report = reconcile(w.pool, w.ledger, w.service)
    assert len(report.recreated) == 1 and report.run == report.recreated
    assert w.count("select 1 from assistant_session where session_id = 'A'") == 0
    assert w.count("select 1 from memory_episode where session_id = 'A'") == 0
    for sid, mid in TAINTED:
        assert w.turn_rows(sid, mid)[1]["payload"]["text"] == PLACEHOLDER
    rows = w.ledger.requests()
    assert rows[first.request_id].state == "superseded"
    new = rows[report.recreated[0]]
    assert new.state == "committed" and new.replaces == first.request_id
    assert _state(w, report.recreated[0]) == "committed"
    # again on a clean database: nothing to do
    again = reconcile(w.pool, w.ledger, w.service)
    assert again.recreated == [] and again.run == [] and again.verified == 1


def test_assistant_is_closed_until_reconcile_passes():
    closed = TestClient(create_app(SimpleNamespace(assistant=None, forget=object(),
                                                   forget_ledger=None)), base_url="http://127.0.0.1")
    r = closed.get("/assistant/sessions", headers={"X-EvoWiki-User": "u1"})
    assert r.status_code == 503 and r.json()["detail"] == CLOSED_DETAIL
    open_ = TestClient(create_app(SimpleNamespace(assistant=None)), base_url="http://127.0.0.1")
    assert open_.get("/assistant/sessions",
                     headers={"X-EvoWiki-User": "u1"}).json()["detail"] != CLOSED_DETAIL
