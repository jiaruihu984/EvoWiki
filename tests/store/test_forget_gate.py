# tests/store/test_forget_gate.py — 0082: the controlled delete / redact channel of the append-only
# tables (forget request + SECURITY DEFINER functions + audit) and the role split it rests on. `pool`
# connects as evowiki_app, as the service does; `db_url` is the migrating superuser's.
import json
from datetime import UTC, datetime

import psycopg
import pytest

from evowiki.agent import dbos_app
from evowiki.store.db import role_problem

T0 = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)


@pytest.fixture
def seeded_session(pool):
    with pool.connection() as conn:
        for uid in ("u1", "u2"):
            conn.execute("insert into app_user (user_id, background, condition, lang, created_at) "
                         "values (%s, 'none', 'S2', 'en', %s)", (uid, T0))
        conn.execute("insert into assistant_session (session_id, user_id, created_at) "
                     "values ('s1', 'u1', %s)", (T0,))
        for seq, typ in ((1, "user"), (2, "assistant")):
            conn.execute("insert into assistant_entry (session_id, seq, entry_id, type, payload, "
                         "occurred_at) values ('s1', %s, %s, %s, %s, %s)",
                         (seq, f"e{seq}", typ, json.dumps({"text": f"t{seq}"}), T0))
    return "s1"


@pytest.fixture
def seeded_round(pool, seeded_session):
    with pool.connection() as conn:
        conn.execute("insert into harness_round (round_id, user_id, started_at, finished_at, status, "
                     "reason, finding, signal_ids, cost) values ('r1', 'u1', %s, %s, 'applied', "
                     "'因为 s1 里说了', 'f', '[\"s1\"]', 0.5)", (T0, T0))
        conn.execute("insert into harness_experience (exp_id, user_id, situation, change, evidence, "
                     "verdict, scope, created_at, weight) values ('x1', 'u1', '{}', '{}', '{}', "
                     "'effective', 'user', %s, 1)", (T0,))
    return "r1"


def _request(conn, session="s1", user="u1"):
    return conn.execute("select evowiki_forget_request(%s, %s, null, 'api:test')",
                        (user, session)).fetchone()["evowiki_forget_request"]


def _count(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()["n"]


def test_setting_the_variable_alone_does_not_open_the_gate(pool, seeded_session):
    with pool.connection() as conn, conn.transaction():
        conn.execute("select set_config('evowiki.forget', 'made-up', true)")
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute("delete from assistant_entry where session_id = %s", (seeded_session,))


def test_app_role_cannot_write_requests_or_audit_directly(pool, seeded_session):
    for sql in (("insert into forget_request (request_id, user_id, session_id, state, requested_at, "
                 "requested_by, updated_at) values ('r', 'u1', 's1', 'deleting', now(), 'x', now())"),
                "update forget_request set state = 'deleting'",
                "insert into forget_audit (request_id, step, rows) values ('r', 'x', 0)",
                "delete from forget_audit"):
        with pool.connection() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(sql)


def test_a_valid_request_opens_the_gate_for_this_transaction_only(pool, seeded_session):
    with pool.connection() as conn:
        rid = _request(conn)
        conn.commit()
        with conn.transaction():
            conn.execute("select evowiki_forget_open(%s)", (rid,))
            conn.execute("delete from assistant_entry where session_id = %s", (seeded_session,))
            conn.execute("select evowiki_forget_close(%s)", (rid,))
        with pytest.raises(psycopg.errors.RaiseException), conn.transaction():  # committed: shut
            conn.execute("select set_config('evowiki.forget', %s, true)", (rid,))
            conn.execute("delete from assistant_session where session_id = %s", (seeded_session,))
        state = conn.execute("select state from forget_request where request_id = %s",
                             (rid,)).fetchone()["state"]
        assert state == "committed"
        assert _count(conn, "select count(*) n from assistant_entry") == 0


def test_the_gate_is_bound_to_the_transaction_that_opened_it(pool, seeded_session):
    """A request left 'deleting' by a transaction that committed without closing opens nothing
    later, even with the variable set to its id; evowiki_forget_fail makes it retryable."""
    with pool.connection() as conn:
        rid = _request(conn)
        conn.commit()
        with conn.transaction():
            conn.execute("select evowiki_forget_open(%s)", (rid,))
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"), conn.transaction():
            conn.execute("select set_config('evowiki.forget', %s, true)", (rid,))
            conn.execute("delete from assistant_entry where session_id = %s", (seeded_session,))
        with pytest.raises(psycopg.errors.RaiseException, match="cannot open"), conn.transaction():
            conn.execute("select evowiki_forget_open(%s)", (rid,))
        conn.execute("select evowiki_forget_fail(%s, 'left open')", (rid,))
        conn.commit()
        with conn.transaction():
            conn.execute("select evowiki_forget_open(%s)", (rid,))
            conn.execute("delete from assistant_entry where session_id = %s", (seeded_session,))
            conn.execute("select evowiki_forget_close(%s)", (rid,))


def test_a_rolled_back_forget_deletes_nothing_and_can_be_retried(pool, seeded_session):
    with pool.connection() as conn:
        rid = _request(conn)
        conn.commit()
        with pytest.raises(RuntimeError), conn.transaction():
            conn.execute("select evowiki_forget_open(%s)", (rid,))
            conn.execute("delete from assistant_entry where session_id = %s", (seeded_session,))
            raise RuntimeError("a later step failed")
        assert _count(conn, "select count(*) n from assistant_entry") == 2
        conn.execute("select evowiki_forget_fail(%s, 'a later step failed')", (rid,))
        row = conn.execute("select state, error from forget_request where request_id = %s",
                           (rid,)).fetchone()
        assert (row["state"], row["error"]) == ("failed", "a later step failed")
        conn.commit()
        with conn.transaction():
            conn.execute("select evowiki_forget_open(%s)", (rid,))
            conn.execute("select evowiki_forget_close(%s)", (rid,))


def test_a_request_needs_the_users_own_session(pool, seeded_session):
    with pool.connection() as conn, pytest.raises(psycopg.errors.RaiseException, match="not a session"):
        _request(conn, user="u2")


def test_redaction_update_only_touches_redaction_columns(pool, seeded_round):
    """Gate open: rewriting payload / reason / evidence succeeds; also changing a result or identity
    column fails, as does any update with the gate shut."""
    ok = ["update assistant_entry set payload = '{\"text\": \"来源会话已删除\"}' where entry_id = 'e2'",
          "update harness_round set reason = '来源会话已删除', signal_ids = '[]' where round_id = 'r1'",
          ("update harness_experience set evidence = '{\"source_deleted\": true}', change = '{}' "
           "where exp_id = 'x1'")]
    bad = ["update assistant_entry set payload = '{}', type = 'user' where entry_id = 'e2'",
           "update harness_round set reason = '', status = 'failed' where round_id = 'r1'",
           "update harness_round set reason = '', cost = 0 where round_id = 'r1'",
           "update harness_experience set evidence = '{}', verdict = 'harmful' where exp_id = 'x1'",
           "update assistant_session set page_id = 'p' where session_id = 's1'"]
    with pool.connection() as conn:
        rid = _request(conn)
        conn.commit()
        for sql in bad + ok:                                  # gate shut: all refused
            with pytest.raises(psycopg.errors.RaiseException), conn.transaction():
                conn.execute(sql)
        for sql in bad:
            with pytest.raises(psycopg.errors.RaiseException), conn.transaction():
                conn.execute("select evowiki_forget_open(%s)", (rid,))
                conn.execute(sql)
        with conn.transaction():
            conn.execute("select evowiki_forget_open(%s)", (rid,))
            for sql in ok:
                conn.execute(sql)
            conn.execute("select evowiki_forget_close(%s)", (rid,))
        r = conn.execute("select reason, status, cost from harness_round").fetchone()
        assert (r["reason"], r["status"], r["cost"]) == ("来源会话已删除", "applied", 0.5)


def test_pack_history_only_redacts_before_after_why_and_never_deletes(pool, seeded_session):
    with pool.connection() as conn:
        conn.execute("insert into harness_version (user_id, version, key, before, after, source, why, "
                     "created_at) values ('u1', 1, 'H1.write', null, '\"原话\"', 'user', '原话', %s)", (T0,))
        rid = _request(conn)
        conn.commit()
        redact = ("update harness_version set before = null, after = '\"已删除\"', why = '来源会话已删除' "
                  "where version = 1")
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"), conn.transaction():
            conn.execute(redact)                                    # gate shut
        for sql in ("update harness_version set why = '', key = 'H1.probe'",
                    "update harness_version set why = '', source = 'improver'",
                    "delete from harness_version"):
            with pytest.raises(psycopg.errors.RaiseException, match="append-only"), conn.transaction():
                conn.execute("select evowiki_forget_open(%s)", (rid,))
                conn.execute(sql)
        with conn.transaction():
            conn.execute("select evowiki_forget_open(%s)", (rid,))
            conn.execute(redact)
            conn.execute("select evowiki_forget_close(%s)", (rid,))
        row = conn.execute("select key, after, why from harness_version").fetchone()
        assert (row["key"], row["after"], row["why"]) == ("H1.write", "已删除", "来源会话已删除")
        assert ("update:harness_version", 1) in [(r["step"], r["rows"]) for r in conn.execute(
            "select step, rows from forget_audit where request_id = %s", (rid,))]


def test_every_step_is_audited(pool, seeded_round):
    """After one full forget, forget_audit has request / open / close and a row per touched table
    with its row count (statement triggers; also on the tables without an append-only guard)."""
    with pool.connection() as conn:
        conn.execute("insert into agent_run (run_id, user_id, agent, subject, pack_version, input, "
                     "started_at) values ('assistant:s1:1', 'u1', 'assistant', 's1', 1, '{}', %s)", (T0,))
        rid = _request(conn)
        conn.commit()
        with conn.transaction():
            conn.execute("select evowiki_forget_open(%s)", (rid,))
            conn.execute("update harness_round set reason = '' where round_id = 'r1'")
            conn.execute("delete from agent_run where run_id like 'assistant:s1:%%'")
            conn.execute("delete from assistant_entry where session_id = 's1'")
            conn.execute("delete from assistant_session where session_id = 's1'")
            conn.execute("select evowiki_forget_note(%s, 'memory_item', 0)", (rid,))
            conn.execute("select evowiki_forget_close(%s)", (rid,))
        steps = [(r["step"], r["rows"]) for r in conn.execute(
            "select step, rows from forget_audit where request_id = %s order by id", (rid,))]
    assert steps == [("request", 0), ("open", 0), ("update:harness_round", 1), ("delete:agent_run", 1),
                     ("delete:assistant_entry", 2), ("delete:assistant_session", 1),
                     ("memory_item", 0), ("close", 0)]


def test_no_audit_row_without_the_gate(pool, seeded_session):
    with pool.connection() as conn:
        conn.execute("insert into agent_run (run_id, user_id, agent, subject, pack_version, input, "
                     "started_at) values ('run1', 'u1', 'a', 's', 1, '{}', %s)", (T0,))
        conn.execute("update agent_run set stop = 'done'")
        conn.execute("delete from agent_run")
        assert _count(conn, "select count(*) n from forget_audit") == 0
        with pytest.raises(psycopg.errors.RaiseException, match="not open"):
            conn.execute("select evowiki_forget_note('nope', 'x', 0)")


def test_truncate_is_refused_even_with_the_gate_open(pool, seeded_session):
    with pool.connection() as conn:
        rid = _request(conn)
        conn.commit()
        for t in ("assistant_entry", "agent_step", "llm_text"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
                conn.execute("select evowiki_forget_open(%s)", (rid,))
                conn.execute(f"truncate {t}")


def test_app_role_cannot_lift_a_guard_or_become_the_owner(pool):
    with pool.connection() as conn:
        assert role_problem(conn) is None
        for sql in ("alter table assistant_entry disable trigger assistant_entry_append_only",
                    "set role evowiki_forget_owner",
                    "set session_replication_role = replica",
                    ("create or replace function evowiki_forgetting() returns boolean "
                     "language sql as 'select true'")):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
                conn.execute(sql)


def test_role_problem_refuses_a_superuser(db_url):
    with psycopg.connect(db_url) as conn:
        assert "superuser" in role_problem(conn)


def test_dbos_launches_as_the_app_role(app_db_url):
    """DBOS migrates its schema `dbos` (owned by evowiki_app) at launch, as the service's role."""
    try:
        dbos_app.launch(app_db_url, app_version="forget-gate-test")
        with psycopg.connect(app_db_url) as conn:
            names = {r[0] for r in conn.execute(
                "select tablename from pg_tables where schemaname = 'dbos'")}
        assert "workflow_status" in names
    finally:
        dbos_app.shutdown()
