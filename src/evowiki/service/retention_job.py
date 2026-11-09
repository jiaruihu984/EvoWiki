"""The daily trace-retention job (store.retention, migration 0092) as a DBOS scheduled workflow.

Why DBOS: the service already runs its one DBOS launch and the Bot's minute tick on it; a schedule
there needs no other scheduler (cron / Windows Task Scheduler), runs as the service's own role
(evowiki_app, through 0092's gate), survives restarts (`apply_schedules` replaces by name, so a
restart never duplicates it) and records every run (workflow status, the step's result). Missed
days are not backfilled: the next run covers them, since every cutoff is counted back from now.

Only a process that fires stored schedules registers it (`DurableRuntime.schedules`, true for the
service with the Bot on). DBOS fires a stored schedule in every such process, so two processes may
run it the same night: the retention lock serializes them and the second finds nothing to do. A
process that has not installed the job (`install`) ends such a run cleanly, touching nothing."""

import logging
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from dbos import DBOS
from psycopg_pool import ConnectionPool

from evowiki.store import retention

log = logging.getLogger(__name__)

SCHEDULE_NAME = "trace:retention"
CRON = "17 3 * * *"                 # 03:17 UTC daily, off the hour
REQUESTED_BY = "schedule:" + SCHEDULE_NAME

_runtime: tuple[ConnectionPool, retention.RetentionPolicy] | None = None
_scheduled = False


def install(pool: ConnectionPool, policy: retention.RetentionPolicy) -> None:
    global _runtime
    _runtime = (pool, policy)


def register() -> None:
    """After the DBOS launch: create or replace the daily schedule."""
    global _scheduled
    DBOS.apply_schedules([{"schedule_name": SCHEDULE_NAME, "workflow_fn": trace_retention,
                           "schedule": CRON, "context": None, "automatic_backfill": False}])
    _scheduled = True


def uninstall() -> None:
    """Undo `install` + `register` (a failed start); never raises."""
    global _runtime, _scheduled
    if _scheduled:
        try:
            DBOS.delete_schedule(SCHEDULE_NAME)
        except Exception:
            log.warning("deleting the trace retention schedule failed", exc_info=True)
    _runtime, _scheduled = None, False


def add_hook(durable: Any, pool: ConnectionPool, policy: retention.RetentionPolicy) -> None:
    """Install now (recovery at launch may run a pending job) and register after the launch."""
    install(pool, policy)
    durable.after_launch.append(register)
    durable.on_failed_start.append(uninstall)


def apply(pool: ConnectionPool, policy: retention.RetentionPolicy, *, now: datetime,
          requested_by: str) -> dict[str, Any]:
    """One retention run on a fresh connection; its report as JSON."""
    with pool.connection() as conn:
        rep = retention.run(conn, policy, now=now, requested_by=requested_by)
    return {"run_id": rep.run_id, "text_cutoff": rep.text_cutoff.isoformat(),
            "meta_cutoff": rep.meta_cutoff.isoformat(), "counts": rep.counts,
            "policy": asdict(policy)}


@DBOS.step()
def _apply_step() -> dict[str, Any]:
    pool, policy = _runtime  # type: ignore[misc]  # checked by the workflow
    out = apply(pool, policy, now=datetime.now(UTC), requested_by=REQUESTED_BY)
    log.info("trace retention %s: %s", out["run_id"], out["counts"])
    return out


@DBOS.workflow()
def trace_retention(scheduled_at: Any, context: Any = None) -> dict[str, Any]:
    if _runtime is None:
        log.warning("trace retention %s skipped: not installed in this process", DBOS.workflow_id)
        return {}
    return _apply_step()
