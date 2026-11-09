# tests/store/test_retention.py — 0092's retention gate and store.retention's tiered run. `pool`
# connects as evowiki_app, as the service does. The database bounds every cutoff by its own now(),
# so these tests date their rows back from the real time.
import json
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from dbos import DBOS, SetWorkflowID
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from evowiki.agent import dbos_app
from evowiki.agent.tools import ToolResult
from evowiki.llm.chat import ChatMessage, ChatRequest, ChatTurn
from evowiki.llm.gateway import Gateway
from evowiki.llm.models import GatewayConfig, LLMAttempt, LLMCall, LLMRequest, ModelRef
from evowiki.llm.scripted import ScriptedProvider
from evowiki.store import retention, trace
from evowiki.store.llm_logs import PgAttemptLog, PgCallLog
from evowiki.store.retention import RetentionPolicy

NOW = datetime.now(UTC).replace(microsecond=0)
OLD_TEXT = NOW - timedelta(days=100)      # texts expired, metadata kept
OLD_META = NOW - timedelta(days=200)      # everything expired
RECENT = NOW - timedelta(days=10)
POLICY = RetentionPolicy()


def _user(pool, uid="u1"):
    with pool.connection() as conn:
        conn.execute("insert into app_user (user_id, background, condition, lang, created_at) "
                     "values (%s, 'none', 'S2', 'en', %s) on conflict do nothing", (uid, NOW))


def _call(pool, cid, ts, *, h=None):
    call = LLMCall(id=cid, input_hash=h or f"h_{cid}", purpose="p", prompt_version="v",
                   subject="s", provider="deepseek", model="m", model_version=None, params={},
                   raw_output="私密回复", parsed_output={"x": "私密"}, cost=0.01, latency=1.0,
                   ts=ts, replayable=False)
    PgCallLog(pool).append(call)
    PgAttemptLog(pool).append(LLMAttempt(
        id=f"a_{cid}", input_hash=call.input_hash, purpose="p", prompt_version="v", subject="s",
        provider="deepseek", model="m", model_version=None, params={}, raw_output="私密回复",
        status="accepted", error_type=None, error_message="私密错误", cost=0.01, latency=1.0, ts=ts))
    return call


def _usage(pool, ts):
    with pool.connection() as conn:
        conn.execute("insert into usage_record (user_id, grp, purpose, provider, model, "
                     "input_tokens, output_tokens, cost_usd, replayed, ts) values "
                     "('u1', 'page', 'p', 'deepseek', 'm', 1, 1, 0.01, false, %s)", (ts,))


def _run(pool, rid, at, *, subject="p1", call_id=None):
    sink = trace.PgTraceSink(pool, clock=lambda: at)
    sink.start(rid, user_id="u1", agent="deep.write", subject=subject, pack_version=0,
               input={"page": "私密页面"})
    req = ChatRequest(purpose="deep.write", prompt_version="v", subject=subject, system="系统",
                      messages=(ChatMessage(role="user", text=f"{rid} 的私密话"),))
    sink.model(rid, 1, req, ChatTurn(text="t"), call_id, None)
    sink.tool(rid, 2, "tc1", "read_page", {"q": "私密参数"}, ToolResult(content="私密结果"))
    sink.finish(rid, stop="finished", error=None)


def _one(pool, sql, params=()):
    with pool.connection() as conn:
        return conn.execute(sql, params).fetchone()


def _apply(pool, **kw):
    with pool.connection() as conn:
        return retention.run(conn, POLICY, now=NOW, requested_by="test", **kw)


# --- the gate -----------------------------------------------------------------------------------

def _open(conn, text=OLD_TEXT + timedelta(days=5), meta=OLD_META + timedelta(days=5)):
    return conn.execute("select evowiki_retention_open('test', '{}', %s, %s) as rid",
                        (text, meta)).fetchone()["rid"]


def test_setting_the_variable_alone_does_not_open_the_gate(pool):
    _call(pool, "c1", OLD_META)
    with pool.connection() as conn, conn.transaction():
        conn.execute("select set_config('evowiki.retention', 'made-up', true)")
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute("delete from llm_call where id = 'c1'")


def test_a_gate_does_not_carry_over_to_the_next_transaction(pool):
    _call(pool, "c1", OLD_META)
    with pool.connection() as conn:
        with conn.transaction():
            rid = _open(conn)
        with conn.transaction():
            conn.execute("select set_config('evowiki.retention', %s, true)", (rid,))
            with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
                conn.execute("delete from llm_call where id = 'c1'")


def test_the_open_gate_reaches_only_old_rows_of_its_three_tables(pool):
    _user(pool)
    _call(pool, "old", OLD_META)
    _call(pool, "mid", OLD_TEXT)
    _call(pool, "new", RECENT)
    with pool.connection() as conn:
        conn.execute("insert into assistant_session (session_id, user_id, created_at) "
                     "values ('s1', 'u1', %s)", (OLD_META,))
        conn.commit()
        refused = ["delete from assistant_session where session_id = 's1'",
                   "delete from llm_call where id = 'mid'",               # after the meta cutoff
                   "update llm_call set raw_output = '' where id = 'new'",  # after the text cutoff
                   "update llm_call set purpose = 'x' where id = 'old'"]    # not a redaction column
        for sql in refused:
            with conn.transaction():
                _open(conn)
                with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
                    conn.execute(sql)
        with conn.transaction():
            rid = _open(conn)
            conn.execute("update llm_call set raw_output = '', text_expired_at = now() "
                          "where id = 'mid'")
            assert conn.execute("delete from llm_call where id = 'old'").rowcount == 1
            conn.execute("select evowiki_retention_close(%s)", (rid,))
        steps = [r["step"] for r in conn.execute(
            "select step from retention_audit where run_id = %s order by id", (rid,))]
    assert steps == ["open", "update:llm_call", "delete:llm_call", "close"]


def test_the_forget_gate_never_deletes_usage_records(pool):
    _user(pool)
    _usage(pool, OLD_META)
    with pool.connection() as conn:
        conn.execute("insert into assistant_session (session_id, user_id, created_at) "
                     "values ('s1', 'u1', %s)", (NOW,))
        rid = conn.execute("select evowiki_forget_request('u1', 's1', null, 'test') as r"
                           ).fetchone()["r"]
        conn.commit()
        with conn.transaction():
            conn.execute("select evowiki_forget_open(%s)", (rid,))
            with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
                conn.execute("delete from usage_record")


def test_cutoffs_must_be_old_enough_and_in_order(pool):
    with pool.connection() as conn:
        for text, meta, why in ((NOW - timedelta(days=10), OLD_META, "at least 30 days"),
                                (OLD_TEXT, NOW - timedelta(days=10), "at least 30 days"),
                                (OLD_META, OLD_TEXT, "metadata cannot expire")):
            with conn.transaction(), pytest.raises(psycopg.errors.RaiseException, match=why):
                _open(conn, text, meta)


def test_the_two_gates_never_open_together(pool):
    _user(pool)
    with pool.connection() as conn:
        conn.execute("insert into assistant_session (session_id, user_id, created_at) "
                     "values ('s1', 'u1', %s)", (NOW,))
        rid = conn.execute("select evowiki_forget_request('u1', 's1', null, 'test') as r"
                           ).fetchone()["r"]
        conn.commit()
        with conn.transaction():
            conn.execute("select evowiki_forget_open(%s)", (rid,))
            with pytest.raises(psycopg.errors.RaiseException, match="another gate"):
                _open(conn)
        with conn.transaction():
            _open(conn)
            with pytest.raises(psycopg.errors.RaiseException, match="retention gate is open"):
                conn.execute("select evowiki_forget_open(%s)", (rid,))


def test_the_app_role_cannot_write_runs_or_audit_directly(pool):
    with pool.connection() as conn:
        for sql in (("insert into retention_run (run_id, requested_by, policy, text_cutoff, "
                     "meta_cutoff, state, started_at, updated_at) values ('x', 't', '{}', now(), "
                     "now(), 'running', now(), now())"),
                    "insert into retention_audit (run_id, step, rows) values ('x', 's', 0)"):
            with conn.transaction(), pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(sql)


# --- the run ------------------------------------------------------------------------------------

def test_texts_expire_at_90_days_and_metadata_at_180(pool):
    _user(pool)
    for cid, at in (("c_meta", OLD_META), ("c_text", OLD_TEXT), ("c_new", RECENT)):
        _call(pool, cid, at)
        _run(pool, f"r_{cid}", at, call_id=cid)
        trace.PgRequestLog(pool).record(cid, f"{cid} 系统", f"{cid} 私密题目", "S")
        _usage(pool, at)
    rep = _apply(pool)

    # metadata tier: gone
    assert _one(pool, "select count(*) as n from llm_call where id = 'c_meta'")["n"] == 0
    assert _one(pool, "select count(*) as n from agent_run where run_id = 'r_c_meta'")["n"] == 0
    assert _one(pool, "select count(*) as n from usage_record")["n"] == 2
    assert _one(pool, "select count(*) as n from llm_attempt where id = 'a_c_meta'")["n"] == 0
    # text tier: metadata kept, texts gone
    c = _one(pool, "select * from llm_call where id = 'c_text'")
    assert (c["raw_output"], c["parsed_output"], c["cost"]) == ("", None, 0.01)
    assert c["text_expired_at"] is not None and c["replayable"] is False
    a = _one(pool, "select * from llm_attempt where id = 'a_c_text'")
    assert (a["raw_output"], a["error_message"]) == (None, None) and a["text_expired_at"]
    r = _one(pool, "select * from agent_run where run_id = 'r_c_text'")
    assert r["input"] == {} and r["text_expired_at"] is not None and r["stop"] == "finished"
    tool = _one(pool, "select payload from agent_step where run_id = 'r_c_text' and seq = 2")
    assert tool["payload"] == {"tool_call_id": "tc1", "name": "read_page", "expired": True}
    model = _one(pool, "select payload from agent_step where run_id = 'r_c_text' and seq = 1"
                 )["payload"]
    assert model["expired"] and "turn" not in model and model["system_hash"]
    with pool.connection() as conn:
        assert [s["kind"] for s in trace.steps(conn, "r_c_text")] == ["model", "tool"]
        with pytest.raises(LookupError, match="expired"):
            trace.request_at(conn, "r_c_text", 1)
    assert _one(pool, "select count(*) as n from llm_request where call_id = 'c_text'")["n"] == 0
    # recent: untouched
    assert _one(pool, "select raw_output from llm_call where id = 'c_new'")["raw_output"] == "私密回复"
    assert _one(pool, "select input from agent_run where run_id = 'r_c_new'")["input"]
    texts = [r["text"] for r in _fetch(pool, "select text from llm_text")]
    assert not any("c_meta" in t or "c_text" in t for t in texts)
    assert any("r_c_new 的私密话" in t for t in texts) and "c_new 私密题目" in texts
    assert "系统" in texts                                    # shared with the recent run
    with pool.connection() as conn:
        assert trace.steps(conn, "r_c_new")[0]["messages"]
    # audited
    assert rep.counts["delete:llm_call"] == 1 and rep.counts["update:llm_call"] == 1
    assert rep.counts["delete:usage_record"] == 1 and rep.counts["text:runs"] == 1
    run = _one(pool, "select * from retention_run where run_id = %s", (rep.run_id,))
    assert run["state"] == "committed" and run["policy"]["text_days"] == 90

    again = _apply(pool)                                      # nothing left to do
    assert again.counts["text:runs"] == again.counts["meta:runs"] == 0


def _fetch(pool, sql, params=()):
    with pool.connection() as conn:
        return conn.execute(sql, params).fetchall()


def test_a_run_a_recent_signal_points_at_keeps_its_texts_only_within_the_policy(pool):
    _user(pool)
    _call(pool, "c_sig", OLD_TEXT)
    _run(pool, "r_sig", OLD_TEXT, subject="p1", call_id="c_sig")
    _run(pool, "r_sig_old", OLD_META, subject="p1")
    with pool.connection() as conn:
        conn.execute("insert into wiki_event (user_id, seq, event_id, occurred_at, type, payload) "
                     "values ('u1', 1, 'e1', %s, 'signal_recorded', %s)",
                     (RECENT, Jsonb({"type": "signal_recorded",
                                     "signal": {"signal_id": "s1", "page_id": "p1"}})))
    _apply(pool)
    assert _one(pool, "select text_expired_at from agent_run where run_id = 'r_sig'"
                )["text_expired_at"] is None
    assert _one(pool, "select raw_output from llm_call where id = 'c_sig'")["raw_output"] == "私密回复"
    # past the metadata expiry a signal keeps nothing
    assert _one(pool, "select count(*) as n from agent_run where run_id = 'r_sig_old'")["n"] == 0


def test_a_call_a_durable_record_points_at_keeps_its_metadata(pool, admin_pool):
    _call(pool, "c_ref", OLD_META)
    with admin_pool.connection() as conn:            # a table only the owner can create
        conn.execute("create table keeps_call (id text primary key, "
                     "llm_call_id text references llm_call (id))")
        conn.execute("insert into keeps_call values ('k', 'c_ref')")
    _apply(pool)
    c = _one(pool, "select raw_output, text_expired_at from llm_call where id = 'c_ref'")
    assert c["raw_output"] == "" and c["text_expired_at"] is not None


def test_a_dry_run_changes_nothing(pool):
    _call(pool, "c1", OLD_META)
    rep = _apply(pool, dry_run=True)
    assert rep.counts["delete:llm_call"] == 1
    assert _one(pool, "select count(*) as n from llm_call")["n"] == 1
    assert _one(pool, "select count(*) as n from retention_run")["n"] == 0


def test_a_failed_run_changes_nothing_and_is_recorded(pool, monkeypatch):
    _call(pool, "c1", OLD_META)

    def boom(conn, *, older_than):
        raise RuntimeError("boom")
    monkeypatch.setattr(trace, "signal_run_ids", boom)
    with pytest.raises(RuntimeError, match="boom"):
        _apply(pool)
    assert _one(pool, "select count(*) as n from llm_call")["n"] == 1
    run = _one(pool, "select state, error, text_cutoff from retention_run")
    assert run["state"] == "failed" and "boom" in run["error"]
    assert run["text_cutoff"] == NOW - timedelta(days=90)


def test_retention_waits_for_a_sink_write_in_flight(pool):
    """A sink that reused an existing text has not written its step yet: retention must not run
    past it (it would delete the text the step is about to point at)."""
    with pool.connection() as sink_conn:
        sink_conn.execute("select pg_advisory_xact_lock_shared(hashtext('trace:prune'))")
        with pool.connection() as conn:
            conn.execute("set lock_timeout = '300ms'")
            conn.commit()
            with pytest.raises(psycopg.errors.LockNotAvailable):
                retention.run(conn, POLICY, now=NOW, requested_by="test")


def test_an_expired_turn_cannot_be_replayed(pool):
    from evowiki.assistant.loop import PURPOSE
    from evowiki.harness.replay_assistant import (
        TRACE_EXPIRED,
        ReplaySourceMissing,
        load_turn,
    )
    _user(pool)
    with pool.connection() as conn:
        conn.execute("insert into agent_run (run_id, user_id, agent, subject, pack_version, input, "
                     "started_at, text_expired_at) values ('t1', 'u1', %s, '', 0, '{}', %s, %s)",
                     (PURPOSE, OLD_TEXT, NOW))
        with pytest.raises(ReplaySourceMissing) as e:
            load_turn(conn, "t1")
    assert e.value.reason == TRACE_EXPIRED


# --- the gateway after a cache row expired ------------------------------------------------------

class Answer(BaseModel):
    value: int


def test_the_gateway_records_a_fresh_execution_after_its_cache_row_expired(pool):
    config = GatewayConfig(generator=ModelRef(provider="gen", model="g-1"),
                           verifier=ModelRef(provider="ver", model="v-1"))
    providers = {"gen": ScriptedProvider("gen", lambda m, r: '{"value": 1}'),
                 "ver": ScriptedProvider("ver", lambda m, r: '{"value": 2}')}
    req = LLMRequest(purpose="unit", prompt_version="unit.v1", subject="x", system="s", prompt="p")
    log = PgCallLog(pool)
    _, first = Gateway(config, log, mode="record", providers=providers,
                       clock=lambda: OLD_TEXT).call("generator", req, Answer)
    _apply(pool)
    assert log.get(first.input_hash) is None and log.taken(first.id)
    gw = Gateway(config, log, mode="record", providers=providers, clock=lambda: NOW)
    out, second = gw.call("generator", req, Answer)
    assert out == Answer(value=1) and second.id.startswith(first.id + "_e") and second.replayable
    assert gw.call("generator", req, Answer)[1] == second       # the new row is the cache row
    assert len(providers["gen"].calls) == 2


# --- the policy ---------------------------------------------------------------------------------

def test_policy_from_env():
    assert RetentionPolicy.from_env({}) == RetentionPolicy(90, 180, 180, 7)
    p = RetentionPolicy.from_env({"EVOWIKI_RETENTION_TEXT_DAYS": "60",
                                  "EVOWIKI_RETENTION_META_DAYS": "120",
                                  "EVOWIKI_OTEL_RETENTION_DAYS": "3"})
    assert p == RetentionPolicy(60, 120, 120, 3)
    for bad in ({"EVOWIKI_RETENTION_TEXT_DAYS": "7"}, {"EVOWIKI_RETENTION_META_DAYS": "60"},
                {"EVOWIKI_RETENTION_TEXT_DAYS": "ninety"},
                {"EVOWIKI_RETENTION_SIGNAL_TEXT_DAYS": "400"}):
        with pytest.raises(ValueError):
            RetentionPolicy.from_env(bad)


def test_the_job_reports_its_run(pool):
    from evowiki.service import retention_job
    _call(pool, "c1", OLD_META)
    out = retention_job.apply(pool, POLICY, now=NOW, requested_by="test")
    assert out["counts"]["delete:llm_call"] == 1 and json.dumps(out)


# --- DBOS's records ---------------------------------------------------------------------------

@DBOS.workflow()
def _retention_probe(x: str) -> str:
    return _retention_probe_step(x)


@DBOS.step()
def _retention_probe_step(x: str) -> str:
    return f"{x} 的私密回复"


def test_finished_dbos_workflows_expire_with_the_texts(pool, app_db_url):
    try:
        dbos_app.launch(app_db_url, app_version="retention-test")
        for wid in ("wf-old", "wf-busy", "wf-new"):
            with SetWorkflowID(wid):
                _retention_probe(wid)
    finally:
        dbos_app.shutdown()
    with pool.connection() as conn:
        conn.execute("update dbos.workflow_status set created_at = %s "
                     "where workflow_uuid in ('wf-old', 'wf-busy')",
                     (int(OLD_TEXT.timestamp() * 1000),))
        conn.execute("update dbos.workflow_status set status = 'PENDING' "
                     "where workflow_uuid = 'wf-busy'")
    rep = _apply(pool)
    left = {r["workflow_uuid"] for r in _fetch(pool, "select workflow_uuid from dbos.workflow_status")}
    assert left == {"wf-busy", "wf-new"}
    assert _one(pool, "select count(*) as n from dbos.operation_outputs "
                      "where workflow_uuid = 'wf-old'")["n"] == 0
    assert rep.counts["dbos.workflow_status"] == 1
