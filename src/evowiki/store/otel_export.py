"""The OTel exporter's reads and its ledger (migration 0093; obs/exporter.py).

Runs are matched to ledger rows by their trace id, computed in SQL the same way `obs.spans`
computes it in Python (`TRACE_ID_SQL`), and by (user_id, started_at), which the agent_run_user
index serves. The ledger never holds a run id.

The optional columns and tables other sessions add (agent_run.step_key / code_version from the
gateway tracing work, artifact / artifact_dependency from the provenance work) are read when they
exist: a run row is read whole (`to_jsonb`), the artifact tables only if `to_regclass` finds them."""

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from evowiki.store.db import fetch_all, fetch_one

# obs.spans.trace_id in SQL: sha256 of the run id, first 32 hex digits
TRACE_ID_SQL = "substr(encode(sha256(convert_to({}, 'UTF8')), 'hex'), 1, 32)"
_TID = TRACE_ID_SQL.format("r.run_id")


def paused_users(conn) -> set[str]:
    """Users with a forget request that has not committed: nothing of theirs is exported."""
    return {r["user_id"] for r in fetch_all(
        conn, "select distinct user_id from forget_request where state <> 'committed'")}


def runs_to_export(conn, *, since: datetime, stale_before: datetime, limit: int,
                   user_id: str | None = None
                   ) -> list[dict[str, Any]]:
    """Runs started since `since` that are finished (or started before `stale_before`, never
    finished) and have no ledger row, or a row still `sent` (the send failed): oldest first.
    Users with an open forget request are left out."""
    return fetch_all(
        conn,
        f"select to_jsonb(r) as run, {_TID} as trace_id, e.state from agent_run r "
        f"left join otel_export e on e.trace_id = {_TID} "
        "where r.started_at >= %(since)s "
        "and (%(user_id)s::text is null or r.user_id = %(user_id)s) "
        "and (r.finished_at is not null or r.started_at < %(stale)s) "
        "and (e.trace_id is null or e.state = 'sent') "
        "and not exists (select 1 from forget_request f where f.user_id = r.user_id "
        "and f.state <> 'committed') "
        "order by r.started_at, r.run_id limit %(limit)s",
        {"since": since, "stale": stale_before, "limit": limit, "user_id": user_id})


def steps_of(conn, run_id: str) -> list[dict[str, Any]]:
    """The run's steps, each with its llm_call row minus the texts (`call`: raw and parsed output,
    params and subject dropped) and, once usage_record has call_id (0090), the tokens it paid."""
    usage = fetch_one(conn, "select exists (select 1 from information_schema.columns where "
                            "table_name = 'usage_record' and column_name = 'call_id') as u")["u"]
    tokens = ("(select sum(u.input_tokens) from usage_record u where u.call_id = s.call_id) "
              "as input_tokens, (select sum(u.output_tokens) from usage_record u "
              "where u.call_id = s.call_id) as output_tokens" if usage
              else "null as input_tokens, null as output_tokens")
    return fetch_all(
        conn,
        "select s.seq, s.kind, s.payload, s.call_id, s.ts, "
        "to_jsonb(c) - 'raw_output' - 'parsed_output' - 'params' - 'subject' as call, "
        f"{tokens} from agent_step s left join llm_call c on c.id = s.call_id "
        "where s.run_id = %s order by s.seq", (run_id,))


MAX_DEPTH = 32


def lineage(conn, run: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """The run's ancestors by input.parent_run (nearest first, each `run_id` and `input_session`)
    and whether the chain is whole: False when an ancestor is gone (deleted with its session) or
    the chain is longer than MAX_DEPTH."""
    out: list[dict[str, Any]] = []
    parent = (run.get("input") or {}).get("parent_run")
    while parent is not None:
        if len(out) >= MAX_DEPTH:
            return out, False
        r = fetch_one(conn, "select run_id, input ->> 'session_id' as input_session, "
                            "input ->> 'parent_run' as parent from agent_run where run_id = %s",
                      (parent,))
        if r is None:
            return out, False
        out.append(r)
        parent = r["parent"]
    return out, True


def artifacts_of(conn, run_id: str) -> list[dict[str, Any]]:
    """The artifacts the run produced with their parents' ids; [] before the artifact tables
    exist."""
    have = fetch_one(conn, "select to_regclass('artifact') is not null as a, "
                           "to_regclass('artifact_dependency') is not null as d")
    if not have["a"]:
        return []
    parents = ("coalesce((select jsonb_agg(jsonb_build_object('id', d.parent_id, 'role', d.role) "
               "order by d.parent_id) from artifact_dependency d where d.child_id = a.artifact_id),"
               " '[]'::jsonb)") if have["d"] else "'[]'::jsonb"
    return fetch_all(
        conn, f"select to_jsonb(a) as artifact, {parents} as parents from artifact a "
              "where a.produced_by_run = %s order by a.artifact_id", (run_id,))


def mark(conn, rows: Sequence[tuple[str, str, str | None, datetime]], state: str,
         now: datetime) -> None:
    """Write (trace_id, user_id, session_key, run_started_at) rows in `state`."""
    with conn.cursor() as cur:
        cur.executemany(
            "insert into otel_export (trace_id, user_id, session_key, run_started_at, state, "
            "exported_at) values (%s, %s, %s, %s, %s, %s) on conflict (trace_id) do update "
            "set state = excluded.state, exported_at = excluded.exported_at",
            [(*r, state, now) for r in rows])


def gone(conn, *, limit: int) -> list[dict[str, Any]]:
    """Ledger rows whose run is no longer in agent_run (deleted by a forget or the clean-up)."""
    return fetch_all(
        conn,
        "select e.trace_id, e.session_key from otel_export e where not exists ("
        f"select 1 from agent_run r where r.user_id = e.user_id "
        f"and r.started_at = e.run_started_at and {_TID} = e.trace_id) "
        "order by e.run_started_at limit %s", (limit,))


def expired(conn, *, before: datetime, limit: int) -> list[dict[str, Any]]:
    return fetch_all(conn, "select trace_id, session_key from otel_export "
                           "where run_started_at < %s order by run_started_at limit %s",
                     (before, limit))


def exported_runs_of(conn, user_id: str | None = None, *, nested: bool = False
                     ) -> list[dict[str, Any]]:
    """Exported runs (run id, input's session_id and parent_run) with their ledger row: the
    user's, or with `nested` every run that has a parent."""
    where = "e.user_id = %s" if user_id is not None else "true"
    if nested:
        where += " and r.input ? 'parent_run'"
    return fetch_all(
        conn,
        "select r.run_id, r.input, r.input ->> 'session_id' as input_session, e.trace_id, "
        "e.session_key "
        f"from otel_export e join agent_run r on r.user_id = e.user_id "
        f"and r.started_at = e.run_started_at and {_TID} = e.trace_id "
        f"where {where}", (user_id,) if user_id is not None else ())


def drop(conn, trace_ids: Sequence[str]) -> int:
    return conn.execute("delete from otel_export where trace_id = any(%s)",
                        (list(trace_ids),)).rowcount


def sessions_left(conn, keys: Sequence[str], dropping: Sequence[str]) -> set[str]:
    """Which of these session keys keep a ledger row once the rows `dropping` are gone."""
    return {r["session_key"] for r in fetch_all(
        conn, "select distinct session_key from otel_export where session_key = any(%s) "
              "and trace_id <> all(%s)", (list(keys), list(dropping)))}


def run_by_trace_id(conn, user_id: str, trace_id: str) -> str | None:
    """The user's run whose OTel trace id is `trace_id` (the link a span carries back)."""
    r = fetch_one(conn, f"select r.run_id from agent_run r where r.user_id = %s and {_TID} = %s",
                  (user_id, trace_id))
    return None if r is None else r["run_id"]
