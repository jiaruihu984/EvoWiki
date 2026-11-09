"""A failed Bot start leaves no half install (fail closed): `durable.register` undoes itself when
DBOS raises mid-way, and a DurableRuntime start that fails after the Bot hook (or anywhere else)
undoes the whole Bot install — notify off, tick schedule deleted, runtime dropped — before DBOS is
shut down, and re-raises the original error. Fakes only: no DBOS, no database."""

from types import SimpleNamespace

import pytest

from evowiki.agent import dbos_app
from evowiki.bot import durable
from evowiki.service.cli import _add_bot_hook
from evowiki.service.deps import DurableRuntime
from evowiki.wikiapp import page_flows


class FakeDBOS:
    def __init__(self, fail_on: str | None = None):
        self.fail_on, self.calls = fail_on, []

    def _call(self, name):
        self.calls.append(name)
        if name == self.fail_on:
            raise RuntimeError(f"{name} failed")

    def register_queue(self, name, **kw):
        self._call("register_queue")
        return SimpleNamespace(name=name)

    def apply_schedules(self, schedules):
        self._call("apply_schedules")

    def delete_schedule(self, name):
        self._call("delete_schedule")


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    durable.uninstall()
    yield
    monkeypatch.undo()
    durable.uninstall()
    page_flows.uninstall()


def _rt():
    return durable.BotRuntime(pool=None, clock=lambda: None, new_id=lambda p: p)


@pytest.mark.parametrize("fail_on", ["register_queue", "apply_schedules"])
def test_register_undoes_itself_when_dbos_raises_mid_way(monkeypatch, fail_on):
    fake = FakeDBOS(fail_on)
    monkeypatch.setattr(durable, "DBOS", fake)
    with pytest.raises(RuntimeError, match=f"{fail_on} failed"):
        durable.register(_rt())
    assert durable._runtime is None and durable._queue is None and not durable._scheduled
    assert "delete_schedule" not in fake.calls        # nothing was scheduled: nothing to delete
    with pytest.raises(RuntimeError, match="not registered"):
        durable._rt()


def test_register_then_uninstall_deletes_the_schedule_it_applied(monkeypatch):
    fake = FakeDBOS()
    monkeypatch.setattr(durable, "DBOS", fake)
    durable.register(_rt())
    assert durable._runtime is not None and durable._queue is not None
    durable.uninstall()
    assert fake.calls == ["register_queue", "apply_schedules", "delete_schedule"]
    assert durable._runtime is None and durable._queue is None


def _service(monkeypatch, fake):
    events = []
    monkeypatch.setattr(durable, "DBOS", fake)
    monkeypatch.setattr(dbos_app, "launch", lambda *a, **k: events.append("launch"))
    monkeypatch.setattr(dbos_app, "shutdown", lambda: events.append(
        f"shutdown(bot runtime {'set' if durable._runtime else 'gone'})"))
    rt = DurableRuntime("postgresql:///unused", executor_id="test")
    svc = SimpleNamespace(durable=rt, pool=None, new_id=lambda p: p, gateway=None, bot_notify=None)
    _add_bot_hook(svc, lambda: None)
    assert durable._runtime is not None                  # installed before the launch
    return svc, events


def test_a_hook_failing_after_the_bot_registered_undoes_the_bot_install(monkeypatch):
    fake = FakeDBOS()
    svc, events = _service(monkeypatch, fake)
    seen = []

    def later_hook():
        seen.append(svc.bot_notify is durable.notify)    # the Bot hook ran and turned notify on
        raise RuntimeError("a later hook failed")
    svc.durable.after_launch.append(later_hook)
    with pytest.raises(RuntimeError, match="a later hook failed"):
        svc.durable.start(SimpleNamespace())
    assert seen == [True]
    assert svc.bot_notify is None
    assert durable._runtime is None and durable._queue is None
    assert fake.calls == ["register_queue", "apply_schedules", "delete_schedule"]
    # undone while DBOS was still up, then DBOS shut down
    assert events == ["launch", "shutdown(bot runtime gone)"]


def test_a_bot_registration_failing_mid_install_fails_the_start_closed(monkeypatch):
    fake = FakeDBOS("apply_schedules")
    svc, events = _service(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="apply_schedules failed"):
        svc.durable.start(SimpleNamespace())
    assert svc.bot_notify is None and durable._runtime is None and durable._queue is None
    assert "delete_schedule" not in fake.calls
    assert events == ["launch", "shutdown(bot runtime gone)"]


def test_a_failed_launch_drops_the_bot_runtime_installed_before_it(monkeypatch):
    fake = FakeDBOS()
    svc, _events = _service(monkeypatch, fake)

    def boom(*a, **k):
        raise RuntimeError("launch failed")
    monkeypatch.setattr(dbos_app, "launch", boom)
    with pytest.raises(RuntimeError, match="launch failed"):
        svc.durable.start(SimpleNamespace())
    assert durable._runtime is None and svc.bot_notify is None and fake.calls == []


def test_a_raising_undo_step_keeps_the_original_error(monkeypatch):
    svc, _events = _service(monkeypatch, FakeDBOS())

    def bad_undo():
        raise ValueError("undo failed")
    svc.durable.on_failed_start.append(bad_undo)        # runs first (newest first)

    def boom():
        raise RuntimeError("hook failed")
    svc.durable.after_launch.append(boom)
    with pytest.raises(RuntimeError, match="hook failed"):
        svc.durable.start(SimpleNamespace())
    assert durable._runtime is None                      # the Bot's undo still ran
