"""Shared request ownership: cancelling one waiter does not cancel its provider owner."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from evowiki.llm.deadline import RequestCancelled, cancel_scope
from evowiki.llm.gateway import _FLIGHTS, Gateway
from evowiki.llm.log import CallLog
from evowiki.llm.scripted import ScriptedProvider
from tests.llm.test_gateway import CONFIG, Answer, _req


@pytest.mark.parametrize("cancel_before", [True, False])
def test_cancelled_waiter_exits_while_owner_and_other_waiter_continue(tmp_path, cancel_before):
    entered, release = threading.Event(), threading.Event()
    asked = []

    def answer(*args):
        asked.append(1)
        entered.set()
        assert release.wait(5)
        return '{"value": 7}'

    gateway = Gateway(
        CONFIG,
        CallLog(tmp_path / "calls.jsonl"),
        mode="record",
        providers={"gen": ScriptedProvider("gen", answer)},
    )
    stop, checked = threading.Event(), threading.Event()
    if cancel_before:
        stop.set()

    def is_cancelled():
        checked.set()
        return stop.is_set()

    def cancelled_waiter():
        with cancel_scope(is_cancelled):
            return gateway.call("generator", _req(), Answer)

    with ThreadPoolExecutor(3) as executor:
        owner = executor.submit(gateway.call, "generator", _req(), Answer)
        assert entered.wait(5)
        cancelled = executor.submit(cancelled_waiter)
        survivor = executor.submit(gateway.call, "generator", _req(), Answer)
        try:
            assert checked.wait(1)
            stop.set()
            with pytest.raises(RequestCancelled):
                cancelled.result(timeout=1)
            assert not owner.done() and not survivor.done()
        finally:
            release.set()
        assert owner.result(timeout=5)[0] == survivor.result(timeout=5)[0] == Answer(value=7)
    assert asked == [1]
    assert not _FLIGHTS._flights
