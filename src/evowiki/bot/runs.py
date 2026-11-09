"""Bot run records (table `bot_run`, one row per Bot DBOS workflow) and the failure pause.
Plain functions over a pool; each is its own transaction and safe to re-run."""

import logging
from collections.abc import Sequence
from datetime import datetime

from evowiki.bot import labels
from evowiki.bot.schedule import next_daily_slot
from evowiki.bot.settings import BotSettings
from evowiki.store import bot as store
from evowiki.store.locks import xact_lock

log = logging.getLogger(__name__)
PAUSED_DETAIL = labels.PAUSED_DETAIL
STATUSES = ("ok", "no_action", "failed")


def start_run(pool, *, run_id: str, user_id: str, kind: str, trigger_id: str | None,
              now: datetime) -> None:
    """Insert the `running` row; a recovered workflow (same run_id) does not duplicate it."""
    with pool.connection() as conn, conn.transaction():
        store.insert_run(conn, run_id=run_id, user_id=user_id, kind=kind,
                         trigger_id=trigger_id, now=now)


def finish_run(pool, run_id: str, *, status: str, now: datetime, calls: int = 0,
               cost_usd: float = 0.0, reads: Sequence = (), detail: str = "") -> None:
    if status not in STATUSES:
        raise ValueError(f"unknown run status {status!r}")
    with pool.connection() as conn, conn.transaction():
        if not store.update_run(conn, run_id, status=status, now=now, calls=calls,
                                cost_usd=cost_usd, reads=reads, detail=detail):
            raise LookupError(f"unknown bot run {run_id!r}")




def pause_after_failure(pool, user_id: str, kind: str, *, now: datetime,
                        settings: BotSettings) -> datetime | None:
    """After a failed run: when the last `failure_streak` runs of this kind all failed, the kind
    pauses until the user's next daily slot (`bot_kind_pause`). Only logged - the user is not
    told (system faults are not theirs to handle). Returns the pause end, or None."""
    n = settings.failure_streak
    with pool.connection() as conn, conn.transaction():
        xact_lock(conn, f"bot:{user_id}")
        rows = store.last_runs(conn, user_id, kind, n)
        if len(rows) < n or any(r.status != "failed" for r in rows):
            return None
        until = next_daily_slot(now, settings)
        store.put_kind_pause(conn, user_id, kind, until)
    log.warning("bot %s runs of %s failed %d times in a row (%s); paused until %s", kind,
                user_id, n, rows[0].detail[:200], until.isoformat())
    return until


def paused_until(pool, user_id: str, kind: str, *, now: datetime) -> datetime | None:
    with pool.connection() as conn:
        return store.kind_paused_until(conn, user_id, kind, now)
