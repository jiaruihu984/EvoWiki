from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from evowiki.llm.log import AttemptLog, CallLog
from evowiki.llm.models import LLMAttempt, LLMCall
from evowiki.store.db import fetch_all, fetch_one

_CALL_COLS = ("id", "input_hash", "purpose", "prompt_version", "subject", "provider", "model",
              "model_version", "params", "raw_output", "parsed_output", "cost", "latency", "ts",
              "replayable", "user_id", "run_id", "served_from")
_ATT_COLS = ("id", "input_hash", "purpose", "prompt_version", "subject", "provider", "model",
             "model_version", "params", "raw_output", "status", "error_type", "error_message",
             "cost", "latency", "ts")
_JSON = {"params", "parsed_output"}


def _values(model, cols):
    d = model.model_dump()
    return tuple(Jsonb(d[c]) if c in _JSON and d[c] is not None else d[c] for c in cols)


def _insert(conn: psycopg.Connection, table: str, cols, values) -> bool:
    sql = (f"insert into {table} ({', '.join(cols)}) values ({', '.join(['%s'] * len(cols))}) "
           "on conflict do nothing")
    return conn.execute(sql, values).rowcount == 1


def _insert_call(conn: psycopg.Connection, call: LLMCall) -> bool:
    """True if inserted; False if an identical call exists; ValueError on a conflict.

    Conflicts come from the primary key (id) or, for replayable rows, the partial unique index
    llm_call_replay_key (input_hash where replayable); both are checked by ordered lookups."""
    if _insert(conn, "llm_call", _CALL_COLS, _values(call, _CALL_COLS)):
        return True
    if call.replayable:
        row = fetch_one(conn, "select * from llm_call where input_hash = %s and replayable",
                        (call.input_hash,))
        if row is not None:
            if LLMCall.model_validate(row) != call:
                raise ValueError(
                    f"duplicate input_hash with different content: {call.input_hash}")
            return False
    row = fetch_one(conn, "select * from llm_call where id = %s", (call.id,))
    if row is None or LLMCall.model_validate(row) != call:
        raise ValueError(f"duplicate llm_call id with different content: {call.id}")
    return False


def _insert_attempt(conn: psycopg.Connection, a: LLMAttempt) -> bool:
    if _insert(conn, "llm_attempt", _ATT_COLS, _values(a, _ATT_COLS)):
        return True
    row = fetch_one(conn, "select * from llm_attempt where id = %s", (a.id,))
    if row is None or LLMAttempt.model_validate(row) != a:
        raise ValueError(f"duplicate attempt id with different content: {a.id}")
    return False


class PgCallLog:
    """CallStore on Postgres (spec §4.2 llm_call). Each method uses its own short transaction.

    Every accepted provider execution whose output is returned to or referenced by downstream
    runtime state has its own llm_call.id. Failed/unaccepted executions remain represented only
    by llm_attempt. `get` returns the unique replayable row for a hash (llm_call_replay_key)."""

    def __init__(self, pool: ConnectionPool):
        self._pool = pool

    def get(self, input_hash: str) -> LLMCall | None:
        with self._pool.connection() as conn:
            r = fetch_one(conn, "select * from llm_call where input_hash = %s and replayable",
                          (input_hash,))
        return LLMCall.model_validate(r) if r else None

    def taken(self, call_id: str) -> bool:
        with self._pool.connection() as conn:
            return fetch_one(conn, "select 1 from llm_call where id = %s", (call_id,)) is not None

    def refuse(self, call_id: str) -> None:
        with self._pool.connection() as conn:
            conn.execute("update llm_call set replayable = false, refused_at = now() "
                         "where id = %s and replayable", (call_id,))

    def append(self, call: LLMCall) -> None:
        with self._pool.connection() as conn:
            _insert_call(conn, call)

    def calls(self) -> list[LLMCall]:
        with self._pool.connection() as conn:
            return [LLMCall.model_validate(r)
                    for r in fetch_all(conn, "select * from llm_call order by ts, id")]

    def __len__(self) -> int:
        with self._pool.connection() as conn:
            return int(fetch_one(conn, "select count(*) as n from llm_call")["n"])


class PgAttemptLog:
    def __init__(self, pool: ConnectionPool):
        self._pool = pool

    def append(self, attempt: LLMAttempt) -> None:
        with self._pool.connection() as conn:
            if not _insert_attempt(conn, attempt):
                raise ValueError(f"duplicate attempt id in attempt log: {attempt.id}")

    def attempts(self) -> list[LLMAttempt]:
        with self._pool.connection() as conn:
            return [LLMAttempt.model_validate(r)
                    for r in fetch_all(conn, "select * from llm_attempt order by ts, id")]

    def __len__(self) -> int:
        with self._pool.connection() as conn:
            return int(fetch_one(conn, "select count(*) as n from llm_attempt")["n"])


@dataclass(frozen=True)
class ImportReport:
    calls_inserted: int = 0
    calls_identical: int = 0
    attempts_inserted: int = 0
    attempts_identical: int = 0


def import_jsonl(pool: ConnectionPool, *, calls: Path | None,
                 attempts: Path | None) -> ImportReport:
    """Migrate Plan 6's llm_calls.jsonl / llm_attempts.jsonl into Postgres (one transaction)."""
    for path in (calls, attempts):
        if path is not None and not Path(path).is_file():
            raise FileNotFoundError(path)  # fail closed: a typo must not import nothing
    ci = cs = ai = asame = 0
    with pool.connection() as conn, conn.transaction():
        if calls is not None:
            for c in CallLog(calls).calls():
                if _insert_call(conn, c):
                    ci += 1
                else:
                    cs += 1
        if attempts is not None:
            for a in AttemptLog(attempts).attempts():
                if _insert_attempt(conn, a):
                    ai += 1
                else:
                    asame += 1
    return ImportReport(ci, cs, ai, asame)
