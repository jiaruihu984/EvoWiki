"""Durable page-correction wakeups. Signals own the words; these rows own delivery only."""

import logging
from typing import TYPE_CHECKING, Any

from dbos import DBOS, SetEnqueueOptions, SetWorkflowID

from evowiki.agent import durable, lifecycle
from evowiki.store import dreaming
from evowiki.store.db import fetch_all
from evowiki.store.rsi import PgRsiStore
from evowiki.teach.contracts import Signal

if TYPE_CHECKING:
    from evowiki.service.deps import Services

QUEUE = "rsi-feedback"
SCHEDULE = "rsi:feedback"
_svc: "Services | None" = None
_queue: Any = None
log = logging.getLogger(__name__)


def pending_ids(svc: "Services", uid: str) -> list[str]:
    with svc.pool.connection() as conn:
        return [
            r["signal_id"]
            for r in fetch_all(
                conn,
                "select signal_id from rsi_feedback where user_id=%s and eligible "
                "and processed_at is null order by signal_id",
                (uid,),
            )
        ]


def signals(svc: "Services", uid: str) -> list[Signal]:
    # Read the source afresh: a deleted/redacted source must never be reconstructed from a queue.
    with svc.pool.connection() as conn:
        rows = fetch_all(
            conn,
            "select w.payload->'signal' as signal from rsi_feedback f join wiki_event w "
            "on w.event_id=f.event_id and w.user_id=f.user_id "
            "where f.user_id=%s and f.eligible and w.type='signal_recorded' "
            "and length(coalesce(w.payload->'signal'->>'text','')) > 0 "
            "and (w.payload->'signal'->>'target_artifact_id' is null or exists "
            "(select 1 from artifact a where a.user_id=f.user_id "
            "and a.artifact_id=w.payload->'signal'->>'target_artifact_id')) order by w.seq",
            (uid,),
        )
        out = []
        for row in rows:
            signal = Signal.model_validate(row["signal"])
            sources = [f"signal:{signal.signal_id}"]
            if signal.target_artifact_id:
                sources.append(signal.target_artifact_id)
            if dreaming.sources_visible(conn, uid, sources):
                out.append(signal)
        return out


def acknowledge(svc: "Services", uid: str, ids: list[str]) -> None:
    if ids:
        with svc.pool.connection() as conn:
            conn.execute(
                "update rsi_feedback set processed_at=%s where user_id=%s "
                "and signal_id=any(%s) and processed_at is null",
                (svc.clock(), uid, ids),
            )


def reviewed(svc: "Services", uid: str, fingerprint: str) -> bool | None:
    with svc.pool.connection() as conn:
        rows = fetch_all(
            conn,
            "select reviewed_worthwhile from rsi_feedback where user_id=%s "
            "and eligible and reviewed_worthwhile is not null "
            "and reviewed_fingerprint=%s limit 1",
            (uid, fingerprint),
        )
        return rows[0]["reviewed_worthwhile"] if rows else None


def remember_review(
    svc: "Services", uid: str, ids: list[str], fingerprint: str, worthwhile: bool
) -> None:
    with svc.pool.connection() as conn:
        conn.execute(
            "update rsi_feedback set reviewed_fingerprint=%s,reviewed_worthwhile=%s, "
            "processed_at=case when %s then null else processed_at end where user_id=%s "
            "and signal_id=any(%s) and eligible",
            (fingerprint, worthwhile, worthwhile, uid, ids),
        )


def reviewed_pending_ids(svc: "Services", uid: str) -> list[str]:
    with svc.pool.connection() as conn:
        return [
            row["signal_id"]
            for row in fetch_all(
                conn,
                "select signal_id from rsi_feedback where user_id=%s and eligible "
                "and processed_at is null and reviewed_worthwhile",
                (uid,),
            )
        ]


def install(svc: "Services") -> None:
    global _svc
    _svc = svc


def register() -> None:
    global _queue
    _queue = DBOS.register_queue(QUEUE, partition_concurrency=1)
    DBOS.apply_schedules(
        [
            {
                "schedule_name": SCHEDULE,
                "workflow_fn": sweep_workflow,
                "schedule": "* * * * *",
                "context": None,
                "automatic_backfill": False,
            }
        ]
    )
    wake_pending()


def uninstall() -> None:
    global _svc, _queue
    _svc, _queue = None, None


def wake_pending() -> None:
    svc, queue = _svc, _queue
    if svc is None or queue is None or lifecycle.drained():
        return
    with svc.pool.connection() as conn:
        users = fetch_all(
            conn,
            "select distinct user_id from rsi_feedback where eligible and processed_at is null",
        )
    slot = svc.clock().strftime("%Y%m%d%H%M")
    for row in users:
        uid = row["user_id"]
        settings = PgRsiStore(svc.pool).settings(uid)
        if settings.cycle == 'manual' or not settings.harness:
            continue

        def enqueue(uid=uid):
            with (
                SetWorkflowID(f"rsi-feedback:{uid}:{slot}"),
                SetEnqueueOptions(queue_partition_key=uid),
            ):
                queue.enqueue(feedback_workflow, uid)

        durable.outside_dbos(enqueue)


def notify(uid: str) -> None:
    """After a committed chat complaint: enqueue promptly; persisted rows cover enqueue failure."""
    svc, queue = _svc, _queue
    if svc is None or queue is None or lifecycle.drained():
        return
    def enqueue():
        with SetWorkflowID(f'rsi-feedback:{uid}:{svc.new_id("wake")}'), SetEnqueueOptions(queue_partition_key=uid):
            queue.enqueue(feedback_workflow, uid)
    try:
        durable.outside_dbos(enqueue)
    except Exception:
        log.exception('could not wake RSI for %s; committed feedback remains pending', uid)


@DBOS.workflow(name="evowiki.rsi.feedback_sweep")
def sweep_workflow(scheduled_at: Any, context: Any = None) -> None:
    durable.step("rsi.feedback.sweep", wake_pending)


@DBOS.workflow(name="evowiki.rsi.feedback")
def feedback_workflow(uid: str) -> None:
    def run():
        if _svc is not None and not lifecycle.drained() and pending_ids(_svc, uid):
            from evowiki.service.rsi_runs import rsi_tick

            rsi_tick(_svc, uid)

    durable.step("rsi.feedback.tick", run)
