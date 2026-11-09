"""A streamed request is bounded by progress, not in total (llm.deadline.call_with_watchdog). Fake
SDK clients and a fake clock, no network: queued (no stream event) is busy, a stream that stalls
after it started is a timeout, a long stream that keeps going is never cut, HTTP 429 is busy."""

import threading
from types import SimpleNamespace

import pytest

from evowiki.agent.errors import classify
from evowiki.llm.chat import ChatMessage, ChatRequest
from evowiki.llm.deadline import NoProgress, Progress, call_with_watchdog
from evowiki.llm.providers import (
    BUSY_TEXT,
    TIMEOUT_TEXT,
    DeepSeekProvider,
    ProviderBusyError,
    ProviderTimeoutError,
    notice_error,
)

S = 60.0     # the per-purpose timeout (fake seconds)
CHAT = ChatRequest(purpose="assistant.turn", prompt_version="a@v1", subject="s1", system="SYS",
                   messages=(ChatMessage(role="user", text="帮我整理这一篇"),))


class Clock:
    def __init__(self):
        self.now = 1000.0
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self.now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.now += seconds


class Sink:
    def __init__(self):
        self.chunks: list[str] = []

    def text(self, chunk):
        self.chunks.append(chunk)


def chunk(content=None, finish=None, usage=None):
    delta = SimpleNamespace(content=content, reasoning_content=None, tool_calls=None)
    return SimpleNamespace(model="deepseek-v4-pro", usage=usage,
                           choices=[SimpleNamespace(delta=delta, finish_reason=finish)])


USAGE = SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15,
                        prompt_cache_hit_tokens=0, prompt_cache_miss_tokens=10)


class FakeClient:
    """client.chat.completions.create(**kw) -> `stream(kw)` (an iterator of chunks)."""

    def __init__(self, stream):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: stream(kw)))


def provider(stream, clock):
    return DeepSeekProvider(client=FakeClient(stream), max_tokens=64, thinking=False,
                            timeout_for=lambda purpose: S, clock=clock)


@pytest.fixture
def release():
    ev = threading.Event()
    yield ev
    ev.set()          # lets a blocked fake worker end


# ---- the watchdog ----

def test_watchdog_queued_never_started(release):
    clock, aborted = Clock(), []
    progress = Progress(clock)

    def fn():
        clock.advance(S + 1)       # nothing arrives
        release.wait(5)

    with pytest.raises(NoProgress) as info:
        call_with_watchdog(fn, S, lambda: aborted.append(1), progress, poll=0.01, grace=0.01)
    assert info.value.started is False and aborted == [1]


def test_watchdog_progressing_runs_past_the_timeout():
    clock = Clock()
    progress = Progress(clock)

    def fn():
        for _ in range(10):        # 10 x 0.6 S = 6 S in total, never S without a beat
            clock.advance(S * 0.6)
            progress.beat()
        return "done"

    assert call_with_watchdog(fn, S, lambda: None, progress, poll=0.01) == "done"


def test_watchdog_stalled_after_starting(release):
    clock = Clock()
    progress = Progress(clock)

    def fn():
        progress.beat()
        clock.advance(S + 1)
        release.wait(5)

    with pytest.raises(NoProgress) as info:
        call_with_watchdog(fn, S, lambda: None, progress, poll=0.01, grace=0.01)
    assert info.value.started is True


# ---- through the provider: queued / progressing / stalled / 429 ----

def test_queued_stream_is_busy(release):
    clock = Clock()

    def stream(kw):
        clock.advance(S + 1)
        release.wait(5)
        yield from ()

    with pytest.raises(ProviderBusyError) as info:
        provider(stream, clock).chat("deepseek-v4-pro", CHAT, stream=Sink())
    assert info.value.user_text == BUSY_TEXT and info.value.code == "llm_busy"
    assert "queued" in str(info.value)
    assert classify(info.value).code == "llm_busy"


def test_long_progressing_stream_is_never_cut():
    clock = Clock()
    parts = [f"第{i}段" for i in range(20)]

    def stream(kw):
        for p in parts:            # 20 x 0.5 S = 10 S, well past S in total
            clock.advance(S * 0.5)
            yield chunk(p)
        yield chunk(finish="stop")
        yield SimpleNamespace(model="deepseek-v4-pro", usage=USAGE, choices=[])

    sink = Sink()
    resp = provider(stream, clock).chat("deepseek-v4-pro", CHAT, stream=sink)
    assert resp.turn.text == "".join(parts) and sink.chunks == parts


def test_stream_that_stalls_after_starting_is_a_timeout(release):
    clock = Clock()

    def stream(kw):
        yield chunk("先")
        clock.advance(S + 1)
        release.wait(5)

    sink = Sink()
    with pytest.raises(ProviderTimeoutError) as info:
        provider(stream, clock).chat("deepseek-v4-pro", CHAT, stream=sink)
    e = info.value
    assert e.user_text == TIMEOUT_TEXT and classify(e).code == "llm_timeout"
    assert not isinstance(e, ProviderBusyError) and sink.chunks == ["先"]


def test_http_429_is_busy():
    class RateLimited(Exception):
        status_code = 429

    def stream(kw):
        raise RateLimited("too many")

    with pytest.raises(ProviderBusyError) as info:
        provider(stream, Clock()).chat("deepseek-v4-pro", CHAT, stream=Sink())
    assert "HTTP 429" in str(info.value)


def test_notice_error_finds_busy_and_timeout_along_the_chain():
    try:
        try:
            raise ProviderTimeoutError("t")
        except ProviderTimeoutError as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        n = notice_error(outer)
    assert n is not None and n.code == "llm_timeout" and n.user_text == TIMEOUT_TEXT
    assert notice_error(RuntimeError("x")) is None
