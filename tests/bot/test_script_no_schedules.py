"""A process launched with `schedules=False` (scripts) fires no stored schedule,
so the Bot's `bot:tick` created by another process leaves no "not a registered workflow function"
records there. The service's own launch keeps schedules. Fakes only."""
import time

import pytest
from dbos import DBOS

from evowiki.agent import dbos_app

_fired: list[int] = []


@DBOS.workflow()
def every_second(scheduled_at, context):
    _fired.append(1)


@pytest.fixture
def stored_schedule(db_url):
    """A schedule stored in the system database by 'another process'."""
    _fired.clear()
    dbos_app.launch(db_url, app_version="s-test")
    DBOS.apply_schedules([{"schedule_name": "probe:tick", "workflow_fn": every_second,
                           "schedule": "* * * * * *", "context": None,
                           "automatic_backfill": False}])
    dbos_app.shutdown()
    _fired.clear()
    yield db_url
    dbos_app.shutdown()


def test_launch_without_schedules_fires_nothing_and_with_schedules_fires(stored_schedule):
    dbos_app.launch(stored_schedule, app_version="s-test")     # the default: no schedules
    time.sleep(4)
    assert _fired == []
    dbos_app.shutdown()
    dbos_app.launch(stored_schedule, app_version="s-test", schedules=True)
    deadline = time.monotonic() + 15
    while not _fired and time.monotonic() < deadline:
        time.sleep(0.5)
    assert _fired


def test_dbos_version_is_the_one_the_patch_was_written_for():
    """`launch(schedules=False)` swaps a private DBOS name; an upgrade must fail here, loudly."""
    from importlib.metadata import version

    import dbos
    import dbos._dbos as dbos_module
    assert version("dbos").startswith("3.2."), version("dbos")
    assert dbos.__version__.startswith("3.2.") if hasattr(dbos, "__version__") else True
    assert callable(dbos_module.dynamic_scheduler_loop)


def test_defaults_are_off():
    import inspect

    from evowiki.service import deps
    assert inspect.signature(dbos_app.launch).parameters["schedules"].default is False
    assert deps.DurableRuntime("postgresql://x", "e").schedules is False


@pytest.mark.parametrize("bot", [None, "on", "off"])
def test_service_build_keeps_feedback_recovery_independent_of_bot(db_url, pool, bot):
    from evowiki.bot.durable import QUEUE as BOT_QUEUE
    from evowiki.service.deps import build_services
    from evowiki.service.rsi_feedback import QUEUE as FEEDBACK_QUEUE
    from tests.runtime.helpers import FixedClock, seed_runtime
    from tests.wikiapp.test_goal import NOW
    seed_runtime(pool)
    env = {"EVOWIKI_DATABASE_URL": db_url, "EVOWIKI_EMBEDDER": "hash"}
    if bot is not None:
        env["EVOWIKI_BOT"] = bot
    svc = build_services(env, clock=FixedClock(NOW))
    try:
        assert svc.durable.schedules is True
        assert svc.durable.rsi_services is svc
        if bot == "off":
            assert FEEDBACK_QUEUE in svc.durable.queues
            assert BOT_QUEUE not in svc.durable.queues
        else:
            assert svc.durable.queues is None
    finally:
        svc.pool.close()


def test_durable_runtime_passes_the_flag(monkeypatch):
    from evowiki.service import deps
    from evowiki.wikiapp import page_flows
    monkeypatch.setattr(page_flows, "install", lambda _deps: None)
    seen = []
    monkeypatch.setattr(deps.dbos_app, "launch", lambda *a, **k: seen.append(k))
    for schedules in (True, False):
        rt = deps.DurableRuntime("postgresql://x", "e", schedules=schedules)
        monkeypatch.setattr(rt, "stop", lambda: None)
        rt.start(None)  # type: ignore[arg-type]
    assert [k["schedules"] for k in seen] == [True, False]
