"""The background OTel exporter (observability plan §5 1d, rulings 1 and 5): tails committed agent
runs in Postgres and sends each as one trace (obs.spans) to a local Phoenix over OTLP/HTTP.
Read-only on the product's tables; its one table is the ledger `otel_export` (migration 0093).

Off unless configured (`ExportConfig.from_env`): EVOWIKI_OTEL_ENDPOINT is Phoenix's base URL
(e.g. http://127.0.0.1:6006; spans go to {base}/v1/traces, deletes to its REST API).

Each tick, deletes first, then exports:

1. delete in Phoenix, then drop from the ledger, the traces
   - whose run is gone from agent_run: a committed forget (memory/forget.py deletes the runs of
     the session and of its tainted turns, harness / forge runs citing it) or the trace clean-up;
     the ledger is the deleted id set, no hook in the delete flows is needed;
   - whose run an open forget request hides (`hidden_of`: the session, its tainted turns), right
     when the request exists, before it commits;
   - older than the export retention (`retention_days`, default 7);
   - nested (input.parent_run) in a run that is gone, or in one the request hides;
   and the Phoenix sessions of a hidden session, or left with no exported trace.
2. export the runs not yet exported (never one nested in a run that is gone), skipping every
   user with an open forget request (a run of theirs is exported again only after the request
   committed, when what it hid is gone). The ledger row is written before the send, so a
   trace that may be in Phoenix can be found again; a failed send leaves it `sent` and the next
   tick sends it again (same ids).

Fail closed: a delete Phoenix does not confirm keeps the ledger row and is tried again next tick
(logged); the user's export stays paused while their request is open, whatever Phoenix says."""

import logging
import os
import threading
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from psycopg_pool import ConnectionPool

from evowiki.memory.forget import ForgetBusy, Hidden, generation_guard, hidden_of
from evowiki.obs import spans as sp
from evowiki.obs.phoenix import Deleter, PhoenixDeleter, PhoenixError
from evowiki.store import otel_export as store

log = logging.getLogger(__name__)

BATCH = 100
STALE = timedelta(hours=6)     # a run never finished is exported (as unfinished) after this


@dataclass(frozen=True)
class ExportConfig:
    endpoint: str                  # Phoenix base URL
    project: str = "evowiki"
    retention_days: float = 7.0
    link_base: str = "http://127.0.0.1:8002"
    every: float = 30.0
    api_key: str | None = None     # only if Phoenix runs with auth

    @classmethod
    def from_env(cls, env: Any = os.environ) -> "ExportConfig | None":
        endpoint = (env.get("EVOWIKI_OTEL_ENDPOINT") or "").strip()
        if not endpoint:
            return None
        return cls(endpoint=endpoint.rstrip("/"),
                   project=env.get("EVOWIKI_OTEL_PROJECT") or "evowiki",
                   retention_days=float(env.get("EVOWIKI_OTEL_RETENTION_DAYS") or 7),
                   link_base=env.get("EVOWIKI_OTEL_LINK_BASE") or "http://127.0.0.1:8002",
                   every=float(env.get("EVOWIKI_OTEL_EVERY_SECONDS") or 30),
                   api_key=env.get("EVOWIKI_PHOENIX_API_KEY") or None)


@dataclass
class TickReport:
    exported: int = 0
    deleted: int = 0
    sessions_deleted: int = 0
    failed: int = 0


def _hidden_one(run_id: str, input_session: str | None, hidden: Hidden) -> bool:
    sess = sp.session_of(run_id)
    if sess is not None and hidden.episode(*sess):
        return True
    if input_session in hidden.sessions:
        return True
    return any(f"{sp.ASSISTANT_PREFIX}{s}:" in run_id for s in hidden.sessions)


def hidden_run(run: dict[str, Any], hidden: Hidden, ancestors: Sequence[dict[str, Any]] = ()
               ) -> bool:
    """Is this run (run_id, input_session), or a run it is nested in (`ancestors`,
    store.lineage), part of what an open forget request hides?"""
    return any(_hidden_one(r["run_id"], r.get("input_session"), hidden)
               for r in (run, *ancestors))


def session_in(run: dict[str, Any], ancestors: Sequence[dict[str, Any]]) -> str | None:
    """The assistant session of a run or of the nearest run it is nested in."""
    for r in (run, *ancestors):
        if (sess := sp.session_of(r["run_id"])) is not None:
            return sess[0]
        if r.get("input_session"):
            return r["input_session"]
    return None


class Exporter:
    def __init__(self, pool: ConnectionPool, config: ExportConfig, *, spans: SpanExporter,
                 deleter: Deleter, clock: Callable[[], datetime],
                 hidden: Callable[[Any, str], Hidden] = hidden_of) -> None:
        self._pool = pool
        self._cfg = config
        self._spans = spans
        self._deleter = deleter
        self._clock = clock
        self._hidden = hidden
        self._resource = sp.resource(config.project)

    def tick(self) -> TickReport:
        report = TickReport()
        self._delete(report)
        self._export(report)
        return report

    # ---------- deleting ----------

    def _delete(self, report: TickReport) -> None:
        now = self._clock()
        with self._pool.connection() as conn:
            rows = {r["trace_id"]: r["session_key"] for r in store.gone(conn, limit=BATCH * 10)}
            rows |= {r["trace_id"]: r["session_key"] for r in store.expired(
                conn, before=now - timedelta(days=self._cfg.retention_days), limit=BATCH * 10)}
            # a nested run whose parent chain is broken: its session or turn was deleted with
            # the parent (it must not outlive it in Phoenix)
            for r in store.exported_runs_of(conn, nested=True):
                if not store.lineage(conn, r)[1]:
                    rows[r["trace_id"]] = r["session_key"]
            hidden_keys: set[str] = set()
            for uid in store.paused_users(conn):
                hidden = self._hidden(conn, uid)
                for r in store.exported_runs_of(conn, uid):
                    if hidden_run(r, hidden, store.lineage(conn, r)[0]):
                        rows[r["trace_id"]] = r["session_key"]
                hidden_keys |= {sp.session_key(s) for s in hidden.sessions}
            keys = {k for k in rows.values() if k}
            # a Phoenix session goes when its session is hidden or none of its traces stays
            doomed = sorted((keys - store.sessions_left(conn, sorted(keys), sorted(rows)))
                            | (hidden_keys & keys))
        failed_keys: set[str] = set()
        if doomed:
            try:
                self._deleter.delete_sessions(doomed)     # cascades to their traces
                report.sessions_deleted += len(doomed)
            except (PhoenixError, httpx.HTTPError) as exc:   # rows kept: tried again next tick
                failed_keys = set(doomed)
                report.failed += 1
                log.warning("otel export: deleting %d Phoenix sessions failed: %s", len(doomed),
                            exc)
        done: list[str] = []
        for tid, key in rows.items():
            if key in failed_keys:
                continue
            try:     # also when its session went (cascaded there): a 404 is fine
                self._deleter.delete_trace(tid)
            except (PhoenixError, httpx.HTTPError) as exc:   # kept: tried again next tick
                report.failed += 1
                log.warning("otel export: deleting trace %s in Phoenix failed: %s", tid, exc)
                continue
            done.append(tid)
        if done:
            with self._pool.connection() as conn:
                store.drop(conn, done)
            report.deleted += len(done)

    # ---------- exporting ----------

    def _export(self, report: TickReport) -> None:
        # Schedule users, then reload payloads inside the generation/forget fence.
        # The first snapshot may already have been deleted when we acquire it.
        now = self._clock()
        with self._pool.connection() as conn:
            users = Counter(t["run"]["user_id"] for t in store.runs_to_export(
                conn, since=now - timedelta(days=self._cfg.retention_days),
                stale_before=now - STALE, limit=BATCH))
        for user_id, limit in users.items():
            try:
                with generation_guard(self._pool, user_id):
                    self._export_user(report, user_id, limit=limit)
            except ForgetBusy:
                # Its durable deletion owns the next action; retry on a later tick.
                continue

    def _export_user(self, report: TickReport, user_id: str, *, limit: int) -> None:
        now = self._clock()
        with self._pool.connection() as conn:
            todo = store.runs_to_export(
                conn, since=now - timedelta(days=self._cfg.retention_days),
                stale_before=now - STALE, limit=limit, user_id=user_id)
            batch: list[ReadableSpan] = []
            ledger: list[tuple[str, str, str | None, datetime]] = []
            for t in todo:
                run = t["run"]
                rid = run["run_id"]
                ancestors, whole = store.lineage(conn, run)
                if not whole:          # nested in a run that is gone: never exported
                    continue
                sid = session_in({"run_id": rid, "input_session": (run.get("input") or {}).get(
                    "session_id")}, ancestors)
                batch += sp.run_spans(run, store.steps_of(conn, rid), store.artifacts_of(conn, rid),
                                      resource=self._resource, link_base=self._cfg.link_base,
                                      session_id=sid)
                ledger.append((t["trace_id"], run["user_id"],
                               sp.session_key(sid) if sid else None,
                               sp.as_time(run["started_at"])))
            if not ledger:
                return
            # the row before the send: a trace that may be in Phoenix is always in the ledger
            store.mark(conn, ledger, "sent", now)
        with self._pool.connection() as conn:
            # a forget request that arrived since the runs were read: send nothing of that user
            paused = store.paused_users(conn)
        if paused:
            keep = {i for i, row in enumerate(ledger) if row[1] not in paused}
            ids = {ledger[i][0] for i in keep}
            batch = [s for s in batch if format(s.context.trace_id, "032x") in ids]
            ledger = [ledger[i] for i in sorted(keep)]
        if not ledger:
            return
        if self._spans.export(batch) is not SpanExportResult.SUCCESS:
            report.failed += 1
            log.warning("otel export: sending %d runs to Phoenix failed; retried next tick",
                        len(ledger))
            return
        with self._pool.connection() as conn:
            store.mark(conn, ledger, "exported", now)
        report.exported += len(ledger)


def build(pool: ConnectionPool, config: ExportConfig, clock: Callable[[], datetime]) -> Exporter:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    headers = {"Authorization": f"Bearer {config.api_key}"} if config.api_key else None
    return Exporter(pool, config,
                    spans=OTLPSpanExporter(endpoint=f"{config.endpoint}/v1/traces",
                                           headers=headers),
                    deleter=PhoenixDeleter(config.endpoint, api_key=config.api_key),
                    clock=clock)


def start_background(exporter: Exporter, every: float) -> tuple[threading.Thread, threading.Event]:
    stop = threading.Event()

    def loop() -> None:
        while not stop.is_set():
            try:
                exporter.tick()
            except Exception:
                log.exception("otel export tick failed; trying again")
            stop.wait(every)

    t = threading.Thread(target=loop, name="otel-export", daemon=True)
    t.start()
    return t, stop
