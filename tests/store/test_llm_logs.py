from datetime import UTC, datetime, timedelta, timezone

import psycopg
import pytest
from pydantic import BaseModel

from evowiki.llm.gateway import RECORD_MAX_ATTEMPTS, Gateway, LLMParseError
from evowiki.llm.log import AttemptLog, AttemptStore, CallLog, CallStore
from evowiki.llm.models import GatewayConfig, LLMRequest, ModelRef
from evowiki.llm.scripted import ScriptedProvider
from evowiki.store.llm_logs import PgAttemptLog, PgCallLog, import_jsonl

CFG = GatewayConfig(generator=ModelRef(provider="anthropic", model="m-gen"),
                    verifier=ModelRef(provider="deepseek", model="m-ver"))
NOW = datetime(2026, 10, 2, tzinfo=UTC)
REQ = LLMRequest(purpose="t", prompt_version="t.v1", subject="s", system="sys", prompt="p")


class Out(BaseModel):
    answer: str


def _gw(log, attempts, raw):
    return Gateway(CFG, log, mode="record", attempts=attempts, clock=lambda: NOW,
                   providers={"anthropic": ScriptedProvider("anthropic", lambda m, r: raw)})


def test_pg_logs_satisfy_protocols_and_round_trip(pool):
    calls, attempts = PgCallLog(pool), PgAttemptLog(pool)
    assert isinstance(calls, CallStore) and isinstance(attempts, AttemptStore)
    out, call = _gw(calls, attempts, '{"answer": "42"}').call("generator", REQ, Out)
    assert out.answer == "42"
    assert calls.get(call.input_hash) == call
    assert len(calls) == 1 and [a.status for a in attempts.attempts()] == ["accepted"]
    replay = Gateway(CFG, PgCallLog(pool), mode="replay")
    assert replay.call("generator", REQ, Out)[0].answer == "42"


def test_a_refused_answer_stays_as_a_row_but_is_asked_again(pool):
    calls = PgCallLog(pool)
    gw = _gw(calls, None, '{"answer": "bad"}')
    _, first = gw.call("generator", REQ, Out)
    assert calls.get(first.input_hash) == first
    gw.refuse(first)
    assert calls.get(first.input_hash) is None and calls.taken(first.id)
    gw.refuse(first)                                    # again: nothing to change
    _, second = gw.call("generator", REQ, Out)
    assert second.id != first.id and calls.get(first.input_hash) == second
    with pool.connection() as conn:                     # anything else on the row stays forbidden
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"), conn.transaction():
            conn.execute("update llm_call set cost = 1 where id = %s", (first.id,))
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"), conn.transaction():
            conn.execute("update llm_call set replayable = false, refused_at = now(), cost = 1 "
                         "where id = %s", (second.id,))
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"), conn.transaction():
            conn.execute("delete from llm_call where id = %s", (first.id,))


def test_parse_failures_are_audited_not_cached(pool):
    calls, attempts = PgCallLog(pool), PgAttemptLog(pool)
    with pytest.raises(LLMParseError):
        _gw(calls, attempts, "not json").call("generator", REQ, Out)
    statuses = [a.status for a in attempts.attempts()]
    assert len(calls) == 0 and statuses == ["parse_error"] * RECORD_MAX_ATTEMPTS


def test_append_identical_is_noop_conflicting_raises(pool):
    calls = PgCallLog(pool)
    _, call = _gw(calls, None, '{"answer": "1"}').call("generator", REQ, Out)
    calls.append(call)
    with pytest.raises(ValueError, match="duplicate input_hash"):
        calls.append(call.model_copy(update={"raw_output": '{"answer": "2"}'}))


def test_import_jsonl_is_idempotent(pool, tmp_path):
    jl, ja = CallLog(tmp_path / "c.jsonl"), AttemptLog(tmp_path / "a.jsonl")
    _gw(jl, ja, '{"answer": "x"}').call("generator", REQ, Out)
    first = import_jsonl(pool, calls=tmp_path / "c.jsonl", attempts=tmp_path / "a.jsonl")
    again = import_jsonl(pool, calls=tmp_path / "c.jsonl", attempts=tmp_path / "a.jsonl")
    assert (first.calls_inserted, first.attempts_inserted) == (1, 1)
    assert (again.calls_identical, again.attempts_identical) == (1, 1)
    assert PgCallLog(pool).calls() == jl.calls()


def test_import_conflict_rolls_back_whole_import(pool, tmp_path):
    jl, ja = CallLog(tmp_path / "c.jsonl"), AttemptLog(tmp_path / "a.jsonl")
    _gw(jl, ja, '{"answer": "x"}').call("generator", REQ, Out)
    (attempt,) = ja.attempts()
    PgAttemptLog(pool).append(attempt.model_copy(update={"raw_output": "different"}))
    with pytest.raises(ValueError, match="duplicate attempt id"):
        import_jsonl(pool, calls=tmp_path / "c.jsonl", attempts=tmp_path / "a.jsonl")
    assert len(PgCallLog(pool)) == 0  # the call inserted before the conflict was rolled back


def test_tables_are_append_only(pool):
    _gw(PgCallLog(pool), PgAttemptLog(pool), '{"answer": "1"}').call("generator", REQ, Out)
    for table in ("llm_call", "llm_attempt"):
        for sql in (f"update {table} set subject = 'x'", f"delete from {table}"):
            with pool.connection() as conn, pytest.raises(psycopg.errors.RaiseException,
                                                          match="append-only"):
                conn.execute(sql)


def test_non_utc_ts_and_float_params_round_trip_exactly(pool):
    calls = PgCallLog(pool)
    _, call = _gw(calls, None, '{"answer": "1"}').call("generator", REQ, Out)
    other = call.model_copy(update={
        "id": "c-other", "input_hash": "h-other", "cost": 0.1 + 0.2, "latency": 1e-7,
        "params": {"temperature": 0.7, "top_p": 1.0, "n": 3, "stop": ["a", None]},
        "ts": datetime(2026, 10, 2, 9, 30, 0, 123456, tzinfo=timezone(timedelta(hours=8)))})
    calls.append(other)
    got = calls.get("h-other")
    assert got == other and got.ts.utcoffset() == timedelta(0)


def test_import_missing_path_raises_and_writes_nothing(pool, tmp_path):
    jl, ja = CallLog(tmp_path / "c.jsonl"), AttemptLog(tmp_path / "a.jsonl")
    _gw(jl, ja, '{"answer": "x"}').call("generator", REQ, Out)
    with pytest.raises(FileNotFoundError):
        import_jsonl(pool, calls=tmp_path / "missing.jsonl", attempts=tmp_path / "a.jsonl")
    with pytest.raises(FileNotFoundError):
        import_jsonl(pool, calls=tmp_path / "c.jsonl", attempts=tmp_path / "missing.jsonl")
    assert len(PgCallLog(pool)) == 0 and len(PgAttemptLog(pool)) == 0


# --- Plan 7.1-api Task 2b: execution identity (llm_call.replayable, migration 0010) ---------

SEL = "the exact selected passage"
RAW_SEL = f'{{"answer": "{SEL}"}}'


def _redacted_req():
    return REQ.model_copy(update={"redact": (SEL,)})


def test_redacted_executions_of_one_hash_are_separate_rows(pool):
    calls, attempts = PgCallLog(pool), PgAttemptLog(pool)
    gw = _gw(calls, attempts, RAW_SEL)
    out1, c1 = gw.call("generator", _redacted_req(), Out)
    out2, c2 = gw.call("generator", _redacted_req(), Out)
    assert out1.answer == SEL and out2.answer == SEL and SEL in c1.raw_output
    assert c1.input_hash == c2.input_hash and c1.id != c2.id
    assert f"llm_{c1.input_hash[:20]}" not in (c1.id, c2.id)
    assert calls.get(c1.input_hash) is None and len(calls) == 2
    rows = calls.calls()
    assert {r.id for r in rows} == {c1.id, c2.id} and all(not r.replayable for r in rows)
    with pool.connection() as conn:
        dump = conn.execute("select row_to_json(c)::text as j from llm_call c "
                            "union all select row_to_json(a)::text from llm_attempt a").fetchall()
    assert dump and not any(SEL in r["j"] for r in dump)


def test_redacted_execution_does_not_disturb_the_replayable_row(pool):
    calls = PgCallLog(pool)
    _, plain = _gw(calls, None, '{"answer": "1"}').call("generator", REQ, Out)
    _, red = _gw(calls, None, RAW_SEL).call("generator", _redacted_req(), Out)
    assert red.input_hash == plain.input_hash and red.id != plain.id
    assert calls.get(plain.input_hash) == plain and len(calls) == 2
    replay = Gateway(CFG, PgCallLog(pool), mode="replay")
    assert replay.call("generator", REQ, Out)[0].answer == "1"
    [stored] = [r for r in calls.calls() if r.id == red.id]
    calls.append(stored)  # identical re-append of the stored execution is a no-op
    assert len(calls) == 2
    with pytest.raises(ValueError, match="duplicate llm_call id"):
        calls.append(red)  # the live (unredacted) copy differs from the stored row


def test_non_replayable_row_cannot_take_the_canonical_id(pool):
    calls = PgCallLog(pool)
    _, plain = _gw(calls, None, '{"answer": "1"}').call("generator", REQ, Out)
    bad = plain.model_copy(update={"id": f"llm_{'f' * 20}", "input_hash": "f" * 64,
                                   "replayable": False})
    with pytest.raises(psycopg.errors.CheckViolation, match="llm_call_execution_id_distinct"):
        calls.append(bad)
    second = plain.model_copy(update={"id": "llm_other_replayable"})
    with pytest.raises(ValueError, match="duplicate input_hash"):
        calls.append(second)  # llm_call_replay_key: one replayable row per hash
    assert len(calls) == 1


def test_replayable_column_is_append_only_too(pool, admin_pool):
    _gw(PgCallLog(pool), None, RAW_SEL).call("generator", _redacted_req(), Out)
    for sql in ("update llm_call set replayable = true", "truncate llm_call cascade"):
        with admin_pool.connection() as conn, pytest.raises(psycopg.errors.RaiseException,
                                                            match="append-only"):
            conn.execute(sql)
    assert PgCallLog(pool).calls()[0].replayable is False


def test_migration_0010_keeps_existing_rows_replayable(pg_admin_url, tmp_path, monkeypatch):
    import shutil

    from evowiki.store import migrate as migrate_mod
    from evowiki.store.db import make_pool
    from tests.pg import blank_database

    old_dir = tmp_path / "migrations"
    old_dir.mkdir()
    for p in sorted(migrate_mod.MIGRATIONS_DIR.glob("*.sql")):
        if p.stem < "0010":
            shutil.copy(p, old_dir / p.name)
    url, name = blank_database(pg_admin_url)
    try:
        with psycopg.connect(url) as conn:
            monkeypatch.setattr(migrate_mod, "MIGRATIONS_DIR", old_dir)
            assert migrate_mod.migrate(conn)[-1] == "0009_truncate_guards"
            for i in (1, 2):
                conn.execute(
                    "insert into llm_call (id, input_hash, purpose, prompt_version, subject, "
                    "provider, model, model_version, params, raw_output, parsed_output, cost, "
                    "latency, ts) values (%s, %s, 't', 't.v1', 's', 'anthropic', 'm', null, "
                    "'{}', %s, %s, null, 0.5, %s)",
                    (f"llm_{str(i) * 20}", str(i) * 64, f'{{"answer": "{i}"}}',
                     f'{{"answer": "{i}"}}', NOW))
            conn.commit()
            monkeypatch.undo()
            applied = migrate_mod.migrate(conn)
            assert applied[0] == "0010_llm_call_executions"
            assert applied == [p.stem for p in sorted(migrate_mod.MIGRATIONS_DIR.glob("*.sql"))
                               if p.stem >= "0010"]
            idx = conn.execute("select indexdef from pg_indexes where tablename = 'llm_call' "
                               "and indexname = 'llm_call_replay_key'").fetchone()
            assert idx is not None and "UNIQUE" in idx[0] and "WHERE replayable" in idx[0]
            cons = {r[0] for r in conn.execute(
                "select conname from pg_constraint where conrelid = 'llm_call'::regclass")}
            assert "llm_call_execution_id_distinct" in cons
            assert "llm_call_input_hash_key" not in cons
        pool = make_pool(url, min_size=1, max_size=2)
        try:
            log = PgCallLog(pool)
            rows = log.calls()
            assert [r.id for r in rows] == [f"llm_{'1' * 20}", f"llm_{'2' * 20}"]
            assert all(r.replayable for r in rows)
            assert log.get("1" * 64) == rows[0] and log.get("2" * 64) == rows[1]
        finally:
            pool.close()
    finally:
        with psycopg.connect(pg_admin_url, autocommit=True) as conn:
            conn.execute(f"drop database if exists {name} with (force)")


def test_escaped_echoes_never_reach_postgres(pool):
    import json

    sel = 'first line marmalade\nsecond "quoted" line \\ backslash 中文选段'
    calls, attempts = PgCallLog(pool), PgAttemptLog(pool)
    req = REQ.model_copy(update={"redact": (sel,)})
    for ascii_ in (True, False):
        raw = json.dumps({"answer": sel}, ensure_ascii=ascii_)
        out, call = _gw(calls, attempts, raw).call("generator", req, Out)
        assert out.answer == sel and call.raw_output == raw
    with pool.connection() as conn:
        dump = conn.execute("select row_to_json(c)::text as j from llm_call c "
                            "union all select row_to_json(a)::text from llm_attempt a").fetchall()
    assert len(dump) == 4
    tokens = ("marmalade", "选段", json.dumps("选段")[1:-1])
    assert not any(t in r["j"] for r in dump for t in tokens)
