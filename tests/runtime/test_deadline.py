import time

import pytest

from evowiki.runtime.deadline import DeadlineExceeded, call_with_deadline


def test_call_with_deadline_returns_and_times_out():
    assert call_with_deadline(lambda: 7, 1.0) == 7
    with pytest.raises(DeadlineExceeded):
        call_with_deadline(lambda: time.sleep(0.5), 0.05)
    assert issubclass(DeadlineExceeded, TimeoutError)


def test_call_with_deadline_propagates_errors():
    def boom():
        raise ValueError("x")
    with pytest.raises(ValueError, match="x"):
        call_with_deadline(boom, 1.0)
