"""The DBOS entry points of a source import (design §5): `evowiki.source_import`, id
`source:<user>:<source>[:<gen>]` (`runs.workflow_id`), the `ImportJob` JSON its only argument, the
service's deps from `install`; and `evowiki.source_start`, the durable retries of a run that could
not start at once (`runs.DbosSourceRuns.start_later`, id `start:<run id>`). Registered by the
decorators at import: import this module before `dbos_app.launch` (`DurableRuntime.start` does),
or recovery cannot find them."""

import logging
from collections.abc import Callable

from dbos import DBOS

from evowiki.sources.importer import ImportDeps, ImportJob, execute
from evowiki.sources.runs import encode_item

log = logging.getLogger("evowiki.sources.flows")

STREAM_KEY = "items"
# the waits between the tries of a run that could not start (seconds): five tries in ~20 minutes,
# then the source shows the plain failure
START_WAITS_S = (10.0, 60.0, 300.0, 900.0)

_deps: ImportDeps | None = None


def install(deps: ImportDeps) -> None:
    global _deps
    _deps = deps


def uninstall() -> None:
    global _deps
    _deps = None


def installed() -> ImportDeps:
    if _deps is None:
        raise RuntimeError("sources.flows.install(deps) was not called: no deps for imports")
    return _deps


@DBOS.workflow(name="evowiki.source_import")
def source_workflow(job: dict) -> str:
    try:
        return execute(ImportJob.model_validate(job),
                       lambda name, p: DBOS.write_stream(STREAM_KEY, encode_item(name, p)),
                       installed())
    finally:
        DBOS.close_stream(STREAM_KEY)


# ---- durable start ---------------------------------------------------------------------------

def retry_start(try_start: Callable[[], str], give_up: Callable[[], bool],
                wait: Callable[[float], None]) -> str:
    """The body of `evowiki.source_start`: `try_start()` ("started", "gone": no longer wanted,
    "error") up to len(START_WAITS_S) + 1 times with `wait`s between; after the last error
    `give_up()` records the failure. Returns "started", "gone" or "failed"."""
    for i in range(len(START_WAITS_S) + 1):
        if i:
            wait(START_WAITS_S[i - 1])
        got = try_start()
        if got != "error":
            return got
    give_up()
    return "failed"


def _try_start(job: ImportJob) -> str:
    from evowiki.sources import store
    from evowiki.sources.runs import DbosSourceRuns
    from evowiki.sources.uses import unstarted
    pool = installed().pool
    with pool.connection() as conn:
        row = store.get_source(conn, job.user_id, job.source_id)
    if not unstarted(row, job):
        return "gone"
    try:
        DbosSourceRuns(pool).start(job)
    except Exception:
        log.exception("source run %d of %s/%s: start failed again", job.gen, job.user_id,
                      job.source_id)
        return "error"
    return "started"


@DBOS.step(name="source_start.try")
def _try_step(job: dict) -> str:
    return _try_start(ImportJob.model_validate(job))


@DBOS.step(name="source_start.give_up")
def _give_up_step(job: dict) -> bool:
    from evowiki.sources.uses import record_not_started
    return record_not_started(installed().pool, ImportJob.model_validate(job))


@DBOS.workflow(name="evowiki.source_start")
def source_start_workflow(job: dict) -> str:
    return retry_start(lambda: _try_step(job), lambda: _give_up_step(job), DBOS.sleep)
