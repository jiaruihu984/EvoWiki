"""DBOS for EvoWiki (spec 2026-10-14 §1): system tables in schema `dbos` of the EvoWiki database,
an explicit application version (never DBOS's default source hash, so an edit does not orphan
pending workflows), JSON serialization (never pickle: renamed classes must not break replay).
One process launches DBOS; `shutdown` is for tests and orderly exits.

Confirmed against dbos 3.2.0: DBOSConfig has no `serialization_type` key; JSON is chosen with
`serializer=DBOSPortableJSONSerializer()` (name "portable_json"). `system_database_url` may stay
`postgresql://` (DBOS switches the driver to psycopg itself). Schedule backfill is per schedule
(`automatic_backfill`, default False), not a config switch."""

from collections.abc import Sequence
from typing import Any

from dbos import DBOSPortableJSONSerializer
from psycopg.conninfo import conninfo_to_dict
from sqlalchemy.engine import URL

SYSTEM_SCHEMA = "dbos"
# b3-10: a turn's first message no longer closes the last session in its own steps (user decision
#       11: assistant/wrapup.py); b3-9: every page / turn run records forge.pin (the turn's tool factory gets the turn's one
#       harness.pack read): coding-agent RSI phase 2; b3-8: harness phase 1 (traces) on top of
#       b3-7 page-write-speed. Bump only when a workflow's step structure changes (spec §1).
APP_VERSION = "b3-10"
_CONNINFO_KEYS = {"user", "password", "host", "port", "dbname"}


def _database_url(database_url: str) -> str:
    """DBOS wants a URL; EvoWiki callers (and the test fixtures) may hold a libpq key=value
    conninfo string instead, e.g. `user=postgres dbname=x host=h port=1`."""
    if "://" in database_url:
        return database_url
    c = conninfo_to_dict(database_url)
    extra = sorted(set(c) - _CONNINFO_KEYS)
    if extra:
        raise ValueError(f"unsupported conninfo keys for the DBOS system database: {extra} "
                         "(use a postgresql:// URL to pass them)")
    return URL.create(
        "postgresql", username=c.get("user"), password=c.get("password"), host=c.get("host"),
        port=int(c["port"]) if c.get("port") else None, database=c.get("dbname"),
    ).render_as_string(hide_password=False)


def dbos_config(database_url: str, *, app_version: str, executor_id: str = "local",
                name: str = "evowiki") -> dict[str, Any]:
    if not app_version:
        raise ValueError("app_version must be set explicitly")
    return {
        "name": name,
        "system_database_url": _database_url(database_url),
        "dbos_system_schema": SYSTEM_SCHEMA,
        "application_version": app_version,
        "executor_id": executor_id,
        "serializer": DBOSPortableJSONSerializer(),
    }


class ExecutorInUse(RuntimeError):
    """Another live process already runs this DBOS executor id on this database."""


_executor_lock: Any = None          # the connection holding this process's executor lock


def _claim_executor(database_url: str, executor_id: str) -> None:
    """Holds a session-level advisory lock named by the executor id for the life of the process (it
    goes with the process, however it ends). DBOS recovers at launch every unfinished workflow of
    its executor id, so a second process with the same id would rerun what the first is still
    running (a second LLM call per step, a second write of the same page). Another id (a test rig,
    a live script) is another lock."""
    import psycopg
    global _executor_lock
    conn = psycopg.connect(_database_url(database_url), autocommit=True)
    got = conn.execute("select pg_try_advisory_lock(hashtextextended(%s, 0))",
                       (f"evowiki.dbos.executor:{executor_id}",)).fetchone()
    if not got or not got[0]:
        conn.close()
        raise ExecutorInUse(
            f"另一个进程已经在用 DBOS executor「{executor_id}」（同一个数据库）；"
            "再启动会把它正在跑的工作流重跑一遍。先停掉那个进程，或用 EVOWIKI_DBOS_EXECUTOR 换一个名字")
    _executor_lock = conn


def _release_executor() -> None:
    global _executor_lock
    conn, _executor_lock = _executor_lock, None
    if conn is not None:
        conn.close()


def launch(database_url: str, *, app_version: str, executor_id: str = "local",
           queues: Sequence[str] | None = None, schedules: bool = False) -> None:
    _claim_executor(database_url, executor_id)
    try:
        _launch(database_url, app_version=app_version, executor_id=executor_id, queues=queues,
                schedules=schedules)
    except BaseException:
        _release_executor()
        raise


def _launch(database_url: str, *, app_version: str, executor_id: str,
            queues: Sequence[str] | None, schedules: bool) -> None:
    """`queues`: dequeue only these queues (DBOS's own internal queue is always dequeued); None =
    every queue stored in the system database, also the ones another process registered.
    `schedules`: False (default: scripts, replay, tests) = this process fires no stored schedule;
    only the service passes True. DBOS otherwise runs every stored
    schedule of the application in EVERY process; a script or a Bot-off service that does not
    register the schedule's workflow would leave PENDING records ("... is not a registered
    workflow function"). DBOS 3.2 has no switch for it, so its scheduler thread is replaced by one
    that only waits for the stop event."""
    from dbos import DBOS
    DBOS(config=dbos_config(database_url, app_version=app_version, executor_id=executor_id))
    if queues is not None:
        DBOS.listen_queues(list(queues))
    if schedules:
        DBOS.launch()
        # Schedules enqueue under the newest version in the database, queues only serve their own: a script that
        # once launched under another name would leave every tick waiting for a process that never comes.
        DBOS.set_latest_application_version(app_version)
        return
    import dbos._dbos as dbos_module
    real = dbos_module.dynamic_scheduler_loop
    dbos_module.dynamic_scheduler_loop = lambda stop_event, _interval: stop_event.wait()
    try:
        DBOS.launch()                     # the thread is created inside launch
    finally:
        dbos_module.dynamic_scheduler_loop = real


def shutdown() -> None:
    from dbos import DBOS
    try:
        DBOS.destroy(destroy_registry=False)
    finally:
        _release_executor()


def launched() -> bool:
    from dbos._dbos import _dbos_global_instance
    return _dbos_global_instance is not None and _dbos_global_instance._launched
