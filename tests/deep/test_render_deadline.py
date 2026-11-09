"""A block must not stall a write job: the render check has an overall deadline, and the evaluator a budget for native work."""

import threading
import time

import pytest

from evowiki.deep import render
from evowiki.deep.evoui.contracts import EvoUIBlock

BOMB = {"a": "range(2000)", "b": "map(a, i => a)", "c": "map(a, i => b)", "d": "n == 37 ? map(a, i => sum(c)) : 0"}


def _block(derived: dict[str, str], text: str = "{d}") -> EvoUIBlock:
    return EvoUIBlock.model_validate({
        "type": "evoui", "title": "t", "question": "q", "fallback": "f", "state": {"n": 37}, "derived": derived,
        "body": {"kind": "box", "children": [{"kind": "slider", "bind": "n", "min": 0, "max": 100, "step": 1, "label": "n = {n}"},
                                             {"kind": "text", "text": text}]},
    })


def test_a_stuck_check_answers_failed_at_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("playwright")
    monkeypatch.setattr(render, "CHECK_DEADLINE_S", 0.5)
    release = threading.Event()

    class Stuck:
        def new_browser_cdp_session(self) -> None:
            raise RuntimeError("no CDP")

        def close(self) -> None:
            pass

    checker = render.PlaywrightChecker(launch=lambda pw: Stuck())
    started = time.monotonic()
    got = checker._run(lambda b: release.wait(30) and None, "ia_1", 2)  # type: ignore[arg-type,return-value]
    release.set()
    assert not got.ok
    assert "超时" in got.detail
    assert time.monotonic() - started < 15


def test_the_deadline_kills_a_real_hung_page(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("playwright")
    monkeypatch.setattr(render, "CHECK_DEADLINE_S", 4)
    started = time.monotonic()
    got = render.PlaywrightChecker()._run(lambda b: b.new_page().evaluate("() => { for (;;); }"), "ia_1", 1)
    assert not got.ok
    assert "超时" in got.detail
    assert time.monotonic() - started < 20


@pytest.mark.parametrize("d", ["n == 37 ? map(a, i => sum(c)) : 0", "n == 37 ? map(a, i => abs(c)) : 0", "n == 37 ? map(a, i => c + 1) : 0"])
def test_native_coercion_of_a_big_value_is_refused_not_slow(d: str) -> None:
    pytest.importorskip("playwright")
    started = time.monotonic()
    got = render.PlaywrightChecker().check_block("ia_1", _block({**BOMB, "d": d}))
    assert time.monotonic() - started < 60
    assert not got.ok


def test_a_big_table_that_fits_still_draws() -> None:
    pytest.importorskip("playwright")
    got = render.PlaywrightChecker().check_block("ia_1", _block(
        {"a": "range(500)", "b": "map(a, i => a)", "d": "sum(map(a, i => len(b[0]) + n))"}))
    assert "too large" not in got.detail and "step limit" not in got.detail
