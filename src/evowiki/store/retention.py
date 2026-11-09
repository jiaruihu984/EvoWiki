"""Tiered trace retention (observability plan 2026-10-29 §8 ruling 5; migration 0092).

Two tiers, both counted back from the run's own time:

  text   full model inputs, replies and tool results: `llm_request` rows and the `llm_text` rows
         only they or expired steps pointed at, tool-step args / results and the run's `input`
         (`agent_step`, `agent_run`), `llm_call.raw_output` / `parsed_output`, `llm_attempt.raw_output`
         / `error_message`. Expired in place: the metadata stays, each row gets `text_expired_at`.
         DBOS's records (schema dbos) of finished workflows started before the cutoff: deleted,
         with their children (`_expire_dbos`); a tree with a running member waits. So a
         deterministic workflow id is NOT a long-term dedup key: once its workflow is gone, the
         same id enqueued again runs again (today's re-enqueues are recent sweeps, dated slot ids
         and user actions, none of which outlive the text window).
  meta   what is left: `agent_run` / `agent_step` rows, `llm_call` / `llm_attempt` rows (purpose,
         model, cost, latency, hashes), `usage_record`. Deleted.

A run whose page has a signal recorded within the text window (`trace.signal_run_ids`) keeps its texts
for up to `signal_text_days`, never past the metadata expiry. An `llm_call` another table points at by
foreign key (old evidence / render-plan rows) keeps its metadata row; its text still
expires. User-saved pages, rules and Memory live in other tables and are never touched here; the
offline eval set is a file (`harness.offline.evalset`), managed with its own source column.

Everything runs in one transaction through 0092's retention gate: `evowiki_retention_open` records the
run with both cutoffs, the guard on `llm_call` / `llm_attempt` / `usage_record` lets through only
deletes of rows before the meta cutoff and redactions of rows before the text cutoff, and every
statement is counted in `retention_audit`. The database refuses a cutoff less than 30 days ago
(RETENTION_FLOOR_DAYS), whatever the policy says. On any error the transaction rolls back and the
failure is recorded (`evowiki_retention_failed`) before the error is re-raised.

Replay: a run with `text_expired_at` cannot be replayed counterfactually; `trace.deep_runs_for_page`
reports the mark and the harness turns such a run into the unusable reason `cases.TRACE_EXPIRED`; an
expired `llm_call` is no longer the replayable row of its hash (`replayable = false`), so record-mode
replay of it raises `MissingRecordingError` and the gateway records the next execution under a fresh
id."""

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

import psycopg
from psycopg.types.json import Jsonb

from evowiki.store import trace
from evowiki.store.db import fetch_all

RETENTION_FLOOR_DAYS = 30          # 0092's evowiki_retention_open refuses anything shorter

ENV_TEXT_DAYS = "EVOWIKI_RETENTION_TEXT_DAYS"
ENV_META_DAYS = "EVOWIKI_RETENTION_META_DAYS"
ENV_SIGNAL_TEXT_DAYS = "EVOWIKI_RETENTION_SIGNAL_TEXT_DAYS"
ENV_PHOENIX_DAYS = "EVOWIKI_OTEL_RETENTION_DAYS"   # session D's exporter reads the same variable


@dataclass(frozen=True)
class RetentionPolicy:
    text_days: int = 90
    meta_days: int = 180
    # a run a recent signal points at keeps its texts this long (text_days..meta_days)
    signal_text_days: int = 180
    # the Phoenix exporter's own retention (observability session D); not applied here
    phoenix_days: int = 7

    def __post_init__(self) -> None:
        if self.text_days < RETENTION_FLOOR_DAYS:
            raise ValueError(f"text retention must be at least {RETENTION_FLOOR_DAYS} days")
        if self.meta_days < self.text_days:
            raise ValueError("metadata must be kept at least as long as the texts")
        if not self.text_days <= self.signal_text_days <= self.meta_days:
            raise ValueError("signal_text_days must be between text_days and meta_days")
        if self.phoenix_days < 1:
            raise ValueError("Phoenix retention must be at least 1 day")

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "RetentionPolicy":
        """The defaults, overridden by the EVOWIKI_RETENTION_* / EVOWIKI_OTEL_RETENTION_DAYS
        variables; a malformed or out-of-range value raises (fail closed: no silent default)."""
        def days(var: str, default: int) -> int:
            raw = env.get(var)
            if raw is None or raw.strip() == "":
                return default
            try:
                return int(raw)
            except ValueError:
                raise ValueError(f"{var} must be a whole number of days, got {raw!r}") from None
        meta = days(ENV_META_DAYS, cls.meta_days)
        return cls(text_days=days(ENV_TEXT_DAYS, cls.text_days), meta_days=meta,
                   signal_text_days=days(ENV_SIGNAL_TEXT_DAYS, min(cls.signal_text_days, meta)),
                   phoenix_days=days(ENV_PHOENIX_DAYS, cls.phoenix_days))


@dataclass
class RetentionReport:
    run_id: str
    text_cutoff: datetime
    meta_cutoff: datetime
    counts: dict[str, int] = field(default_factory=dict)


def _referencing(conn: psycopg.Connection, table: str, skip: tuple[str, ...]) -> list[tuple[str, str]]:
    """(table, column) of every single-column foreign key pointing at `table`, except `skip`."""
    rows = fetch_all(
        conn, "select c.conrelid::regclass::text as t, a.attname as col from pg_constraint c "
              "join pg_attribute a on a.attrelid = c.conrelid and a.attnum = c.conkey[1] "
              "where c.contype = 'f' and c.confrelid = %s::regclass and cardinality(c.conkey) = 1",
        (table,))
    return [(r["t"], r["col"]) for r in rows if r["t"] not in skip]


def _unreferenced(conn: psycopg.Connection, table: str, alias: str, key: str,
                  skip: tuple[str, ...] = ()) -> str:
    return "".join(f" and not exists (select 1 from {t} x where x.{col} = {alias}.{key})"
                   for t, col in _referencing(conn, table, skip))


class _DryRun(Exception):
    def __init__(self, report: RetentionReport) -> None:
        self.report = report


DBOS_SCHEMA = "dbos"
# DBOS 3.2 SystemDatabase.delete_workflows: these first, then workflow_status, which cascades into
# streams, workflow_events(_history) and notifications
DBOS_PAYLOAD_TABLES = ("workflow_input", "workflow_output", "operation_outputs")
DBOS_ACTIVE = ("PENDING", "ENQUEUED")


def _expire_dbos(conn: psycopg.Connection, cutoff: datetime) -> dict[str, int]:
    """Delete every workflow tree (a top-level workflow and its descendants) whose root was created
    before `cutoff` and none of whose members is still PENDING / ENQUEUED. {} when DBOS never
    launched on this database. A deterministic workflow id deleted here runs again if re-enqueued."""
    if fetch_all(conn, "select to_regclass(%s) as t", (f"{DBOS_SCHEMA}.workflow_status",)
                 )[0]["t"] is None:
        return {}
    rows = fetch_all(
        conn, f"with recursive tree as (select workflow_uuid as root, workflow_uuid, status "
              f"from {DBOS_SCHEMA}.workflow_status where parent_workflow_id is null "
              "and created_at < %s union select t.root, s.workflow_uuid, s.status "
              f"from {DBOS_SCHEMA}.workflow_status s join tree t "
              "on s.parent_workflow_id = t.workflow_uuid) select * from tree",
        (int(cutoff.timestamp() * 1000),))
    busy = {r["root"] for r in rows if r["status"] in DBOS_ACTIVE}
    ids = [r["workflow_uuid"] for r in rows if r["root"] not in busy]
    if not ids:
        return {}
    n = {f"dbos.{t}": conn.execute(f"delete from {DBOS_SCHEMA}.{t} where workflow_uuid = any(%s)",
                                   (ids,)).rowcount for t in DBOS_PAYLOAD_TABLES}
    n["dbos.workflow_status"] = conn.execute(
        f"delete from {DBOS_SCHEMA}.workflow_status where workflow_uuid = any(%s)", (ids,)).rowcount
    return n


def run(conn: psycopg.Connection, policy: RetentionPolicy, *, now: datetime,
        requested_by: str, dry_run: bool = False) -> RetentionReport:
    """Apply `policy` as of `now` in one transaction (module doc). Returns what was done;
    `dry_run`: the same work, rolled back (the report says what a real run would do)."""
    text_cut = now - timedelta(days=policy.text_days)
    meta_cut = now - timedelta(days=policy.meta_days)
    signal_cut = now - timedelta(days=policy.signal_text_days)
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise RuntimeError("retention.run needs a connection outside a transaction")
    try:
        with conn.transaction():
            report = _run(conn, policy, now, text_cut, meta_cut, signal_cut, requested_by)
            if dry_run:
                raise _DryRun(report)
    except _DryRun as dry:
        return dry.report
    except Exception as exc:
        with conn.transaction():
            conn.execute("select evowiki_retention_failed(%s, %s, %s, %s, %s)",
                         (requested_by, Jsonb(asdict(policy)), text_cut, meta_cut,
                          f"{type(exc).__name__}: {exc}"[:2000]))
        raise
    return report


def _run(conn: psycopg.Connection, policy: RetentionPolicy, now: datetime, text_cut: datetime,
         meta_cut: datetime, signal_cut: datetime, requested_by: str) -> RetentionReport:
    conn.execute("select " + trace.PRUNE_LOCK)           # no sink write in flight (trace module doc)
    rid = conn.execute("select evowiki_retention_open(%s, %s, %s, %s) as rid",
                       (requested_by, Jsonb(asdict(policy)), text_cut, meta_cut)).fetchone()["rid"]
    report = RetentionReport(rid, text_cut, meta_cut)
    n = report.counts

    def note(step: str, rows: int) -> None:
        n[step] = rows
        conn.execute("select evowiki_retention_note(%s, %s, %s)", (rid, step, rows))

    # rows the metadata tier deletes (older than its cutoff, no foreign key pointing at them) are
    # not redacted first; a row a durable record keeps past it still loses its texts
    run_gone = ("r.started_at < %(meta)s"
                + _unreferenced(conn, "agent_run", "r", "run_id", skip=("agent_step",)))
    call_gone = "c.ts < %(meta)s" + _unreferenced(conn, "llm_call", "c", "id")
    args = {"text": text_cut, "meta": meta_cut, "signal": signal_cut, "now": now,
            "kept": list(trace.signal_run_ids(conn, older_than=text_cut))}

    # --- the text tier ---
    expiring = (f"select r.run_id from agent_run r where r.started_at < %(text)s "
                f"and r.text_expired_at is null and not ({run_gone}) "
                f"and not (r.run_id = any(%(kept)s) and r.started_at >= %(signal)s)")
    # calls of runs whose texts stay (a signal-kept run): their texts stay too
    args["kept_calls"] = [r["call_id"] for r in fetch_all(
        conn, "select s.call_id from agent_step s join agent_run r using (run_id) "
              "where s.call_id is not null and r.started_at >= %(signal)s "
              "and r.run_id = any(%(kept)s)", args)]
    note("text:tool_steps", conn.execute(
        "update agent_step set payload = jsonb_build_object("
        "'tool_call_id', payload->'tool_call_id', 'name', payload->'name', 'expired', true) "
        f"where kind = 'tool' and run_id in ({expiring})", args).rowcount)
    # a model step keeps its hashes and settings; the reply (`turn`) and error sit inline
    note("text:model_steps", conn.execute(
        "update agent_step set payload = (payload - 'turn' - 'error') || '{\"expired\": true}' "
        f"where kind = 'model' and run_id in ({expiring})", args).rowcount)
    note("text:runs", conn.execute(
        "update agent_run set input = '{}'::jsonb, text_expired_at = %(now)s "
        f"where run_id in ({expiring})", args).rowcount)
    expired_calls = (f"select c.id from llm_call c where c.ts < %(text)s "
                     f"and c.text_expired_at is null and not ({call_gone}) "
                     f"and c.id <> all(%(kept_calls)s)")
    note("text:requests", conn.execute(
        f"delete from llm_request where call_id in ({expired_calls}) "
        "or call_id in (select s.call_id from agent_step s join agent_run r using (run_id) "
        "where r.text_expired_at is not null)", args).rowcount)
    conn.execute("update llm_call set raw_output = '', parsed_output = null, replayable = false, "
                 f"text_expired_at = %(now)s where id in ({expired_calls})", args)
    conn.execute(
        "update llm_attempt set raw_output = null, error_message = null, text_expired_at = %(now)s "
        "where ts < %(text)s and ts >= %(meta)s and text_expired_at is null and input_hash not in ("
        "select input_hash from llm_call where id = any(%(kept_calls)s))", args)

    # --- the metadata tier ---
    old_runs = f"select r.run_id from agent_run r where {run_gone}"
    note("meta:steps", conn.execute(
        f"delete from agent_step where run_id in ({old_runs})", args).rowcount)
    note("meta:runs", conn.execute(
        f"delete from agent_run where run_id in ({old_runs})", args).rowcount)
    old_calls = f"select c.id from llm_call c where {call_gone}"
    conn.execute(f"delete from llm_request where call_id in ({old_calls})", args)
    conn.execute(f"delete from llm_call where id in ({old_calls})", args)
    conn.execute("delete from llm_attempt where ts < %(meta)s", args)
    conn.execute("delete from usage_record where ts < %(meta)s", args)

    # --- DBOS's records of finished workflows (inputs, step outputs, streams) ---
    for table, rows in _expire_dbos(conn, text_cut).items():
        note(table, rows)

    # --- texts nothing live points at any more ---
    note("text:texts", conn.execute(
        "delete from llm_text t where not exists (select 1 from ("
        "select payload->>'system_hash' as h from agent_step s join agent_run r using (run_id) "
        "where r.text_expired_at is null "
        "union all select payload->>'tools_hash' from agent_step s join agent_run r using (run_id) "
        "where r.text_expired_at is null "
        "union all select jsonb_array_elements_text(payload->'messages') from agent_step s "
        "join agent_run r using (run_id) where r.text_expired_at is null "
        "and jsonb_typeof(payload->'messages') = 'array' "
        "union all select system_hash from llm_request "
        "union all select prompt_hash from llm_request) refs where refs.h = t.hash)").rowcount)
    for row in fetch_all(conn, "select step, sum(rows)::int as rows from retention_audit "
                               "where run_id = %s and step like '%%:%%' and step not like 'text:%%' "
                               "and step not like 'meta:%%' group by step", (rid,)):
        n[row["step"]] = row["rows"]
    conn.execute("select evowiki_retention_close(%s)", (rid,))
    return report
