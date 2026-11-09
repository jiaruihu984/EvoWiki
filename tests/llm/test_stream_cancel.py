"""「停止」 aborts the model call in flight (llm.deadline cancel scopes). No paid API: fake
OpenAI-compatible clients stand in for DeepSeek. A cancelled run stops reading its stream and
closes it (or closes the per-call HTTP client of a blocked request), raises RequestCancelled, and
that is never a provider failure: no retry, no attempt row, no fallback; reported usage is billed."""

import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from evowiki.agent import dbos_app, durable
from evowiki.agent.errors import Cancelled, classify
from evowiki.agent.loop import run_loop
from evowiki.agent.spec import AgentSpec, LoopLimits
from evowiki.agent.tools import ToolContext
from evowiki.llm.chat import ChatMessage, ChatRequest
from evowiki.llm.deadline import (
    BackgroundPoll,
    RequestCancelled,
    cancel_requested,
    cancel_scope,
)
from evowiki.llm.gateway import Gateway
from evowiki.llm.log import AttemptLog, CallLog
from evowiki.llm.models import GatewayConfig, ModelRef
from evowiki.llm.prices import Price, PriceTable
from evowiki.llm.providers import DeepSeekProvider
from evowiki.llm.usage import Account
from evowiki.llm.usage_control import UsageControl

REQ = ChatRequest(purpose="deep.write", prompt_version="w@v1", subject="p1", system="SYS",
                  messages=(ChatMessage(role="user", text="写"),), tools=())


def _chunk(text=None, finish=None, usage=None):
    choices = [] if text is None and finish is None else [SimpleNamespace(
        delta=SimpleNamespace(content=text, reasoning_content=None, tool_calls=None),
        finish_reason=finish)]
    return SimpleNamespace(model="deepseek-v4-pro", usage=usage, choices=choices)


class FakeStream:
    """An SDK stream: counts the chunks pulled, records close()."""

    def __init__(self, n: int, on_pull=None):
        self.n, self.pulled, self.closed = n, 0, False
        self._on_pull = on_pull or (lambda i: None)

    def __iter__(self):
        while self.pulled < self.n:
            self.pulled += 1
            self._on_pull(self.pulled)
            yield _chunk(text=f"t{self.pulled}")
        yield _chunk(finish="stop")

    def close(self):
        self.closed = True


class FakeClient:
    def __init__(self, create):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))
        self.calls = 0


class Sink:
    def __init__(self):
        self.chunks: list[str] = []

    def text(self, chunk):
        self.chunks.append(chunk)


# ---- the chunk loop ----

def test_a_cancel_between_chunks_stops_reading_and_closes_the_stream():
    stop = threading.Event()
    stream = FakeStream(100, on_pull=lambda i: stop.set() if i == 3 else None)
    provider = DeepSeekProvider(client=FakeClient(lambda **kw: stream), env={})
    sink = Sink()
    with cancel_scope(stop.is_set), pytest.raises(RequestCancelled) as info:
        provider.chat("deepseek-v4-pro", REQ, stream=sink)
    assert stream.pulled == 3                  # not one chunk read past the cancel
    assert stream.closed
    assert sink.chunks == ["t1", "t2", "t3"]
    assert not info.value.reported             # DeepSeek's usage comes only in the last chunk


def test_without_a_cancel_the_stream_is_read_to_its_end():
    stream = FakeStream(5)
    provider = DeepSeekProvider(client=FakeClient(lambda **kw: stream), env={})
    with cancel_scope(lambda: False):
        resp = provider.chat("deepseek-v4-pro", REQ, stream=Sink())
    assert stream.pulled == 5 and resp.turn.text == "t1t2t3t4t5"


# ---- a blocked request: the caller-side wait aborts it ----

class AbortableClient:
    """`with_options(http_client=...)` like the SDKs: the request blocks until its per-call HTTP
    client is closed (what the abort does), then fails like a cut socket."""

    def __init__(self, stream: bool):
        self.entered = threading.Event()
        self.http = None
        self.ended = threading.Event()
        self._stream = stream

    def with_options(self, *, http_client, max_retries):
        self.http = http_client
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=self._create)))

    def _create(self, **kw):
        self.entered.set()
        try:
            if not self.http.closed.wait(30):
                raise AssertionError("never aborted")
            raise ConnectionError("socket closed")
        finally:
            self.ended.set()


class FakeHttp:
    def __init__(self):
        self.closed = threading.Event()

    def close(self):
        self.closed.set()


@pytest.mark.parametrize("streamed", [True, False])
def test_a_cancel_aborts_a_blocked_request_well_before_its_timeout(streamed):
    client = AbortableClient(streamed)
    provider = DeepSeekProvider(client=client, env={}, timeout_for=lambda p: 60.0,
                                http_client_factory=lambda t, hooks: FakeHttp())
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    t0 = time.monotonic()
    with cancel_scope(stop.is_set), pytest.raises(RequestCancelled):
        provider.chat("deepseek-v4-pro", REQ, stream=Sink() if streamed else None)
    assert time.monotonic() - t0 < 3.0         # the 60 s timeout never came into play
    assert client.http.closed.is_set()         # the connection was closed under the request
    assert client.ended.wait(2)                # and the worker ended


# ---- gateway: not a provider failure ----

REF = ModelRef(provider="scripted", model="m")
CFG = GatewayConfig(generator=REF, verifier=REF, allow_single_provider=True)
ACC = Account(user_id="u1", group="page")


class Meter:
    def __init__(self):
        self.records = []

    def check(self, account):
        pass

    def record(self, account, purpose, usage, *, replayed):
        self.records.append(usage)


class CancelledProvider:
    streams = False

    def __init__(self, exc):
        self.exc, self.calls = exc, 0

    def chat(self, model, request, stream=None):
        self.calls += 1
        raise self.exc


def _gateway(tmp_path, provider, meter, attempts):
    return Gateway(CFG, CallLog(tmp_path / "calls.jsonl"), mode="record",
                   providers={"scripted": provider}, record_max_attempts=3,
                   attempts=attempts, clock=lambda: datetime(2026, 10, 28, tzinfo=UTC),
                   usage=UsageControl(meter, PriceTable({"scripted:m": Price(1.0, 2.0)})))


def test_the_gateway_never_retries_a_cancel_or_logs_it_as_a_provider_error(tmp_path):
    provider = CancelledProvider(RequestCancelled(input_tokens=1000, output_tokens=40,
                                                  cache_read_tokens=0, cache_write_tokens=0,
                                                  model_version="m-1"))
    meter, attempts = Meter(), AttemptLog(tmp_path / "attempts.jsonl")
    gw = _gateway(tmp_path, provider, meter, attempts)
    with pytest.raises(RequestCancelled):
        gw.converse("generator", REQ, account=ACC)
    assert provider.calls == 1
    assert attempts.attempts() == []
    # what the stream reported before the cut is still billed
    assert [(u.input_tokens, u.output_tokens) for u in meter.records] == [(1000, 40)]
    assert meter.records[0].cost_usd is not None


def test_a_cancel_with_nothing_reported_bills_nothing(tmp_path):
    meter = Meter()
    gw = _gateway(tmp_path, CancelledProvider(RequestCancelled()), meter,
                  AttemptLog(tmp_path / "attempts.jsonl"))
    with pytest.raises(RequestCancelled):
        gw.converse("generator", REQ, account=ACC)
    assert meter.records == []


def test_a_cancel_is_classified_cancelled_not_llm_unavailable():
    assert classify(RequestCancelled()).code == "cancelled"


def test_the_writer_does_not_switch_to_a_fallback_on_a_cancel():
    from evowiki.deep.agent import _attempt
    tried = []

    def step(gw):
        tried.append(gw)
        raise RequestCancelled()
    st = [0, False]
    with pytest.raises(RequestCancelled):
        _attempt(step, ["w1", "w2"], st, None, "p1")
    assert tried == ["w1"] and st == [0, False]


# ---- the agent loop ----

def test_the_loop_turns_a_cancelled_call_into_cancelled_without_retry():
    stop = threading.Event()
    calls = []

    def converse(req):
        calls.append(req)
        stop.set()                              # 「停止」 lands while the call is in flight
        assert cancel_requested()               # the spec's cancel reaches the call's scope
        raise RequestCancelled()

    spec = AgentSpec(name="t", tools=(), request=lambda st: REQ, cancelled=stop.is_set,
                     limits=LoopLimits(max_tool_calls=4, on_cap="stop", text_is_answer=True))
    gen = run_loop(spec, converse, ToolContext(user_id="u1", session_id="s1"),
                   [ChatMessage(role="user", text="写")])
    with pytest.raises(Cancelled):
        for _ in gen:
            pass
    assert len(calls) == 1


# ---- the background poll ----

def test_background_poll_never_blocks_and_latches_yes():
    gate = threading.Event()
    answers = iter([False, True])

    def probe():
        gate.wait(5)
        return next(answers)
    poll = BackgroundPoll(probe, every=0.0)
    t0 = time.monotonic()
    assert poll() is False                     # the slow probe runs on its own thread
    assert time.monotonic() - t0 < 0.5
    gate.set()
    deadline = time.monotonic() + 5
    while not poll() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert poll() is True and poll() is True


# ---- DBOS: cancel_workflow aborts the step's request ----

@pytest.fixture
def dbos_db(db_url):
    yield db_url
    dbos_app.shutdown()


def test_cancelling_the_dbos_workflow_aborts_its_model_call_in_flight(dbos_db, tmp_path):
    from dbos import DBOS, SetWorkflowID

    client = AbortableClient(stream=False)
    provider = DeepSeekProvider(client=client, env={}, timeout_for=lambda p: 60.0,
                                http_client_factory=lambda t, hooks: FakeHttp())
    cfg = GatewayConfig(generator=ModelRef(provider="deepseek", model="deepseek-v4-pro"),
                        verifier=ModelRef(provider="deepseek", model="deepseek-v4-pro"),
                        allow_single_provider=True)
    attempts = AttemptLog(tmp_path / "attempts.jsonl")
    gw = Gateway(cfg, CallLog(tmp_path / "calls.jsonl"), mode="record",
                 providers={"deepseek": provider}, record_max_attempts=3, attempts=attempts)
    outcome: list[BaseException] = []

    @DBOS.workflow()
    def write_page() -> None:
        assert durable.on_dbos()
        try:
            gw.converse("generator", REQ)
        except BaseException as exc:
            outcome.append(exc)
            raise

    dbos_app.launch(dbos_db, app_version="cancel-test")
    with SetWorkflowID("run-cancel-1"):
        handle = DBOS.start_workflow(write_page)
    assert client.entered.wait(10), "the model call never started"
    t0 = time.monotonic()
    DBOS.cancel_workflow("run-cancel-1")
    assert client.http.closed.wait(5), "the request in flight was not aborted"
    assert time.monotonic() - t0 < 4.0          # poll every 1 s, well before the 60 s timeout
    assert client.ended.wait(2)
    deadline = time.monotonic() + 10
    while not outcome and time.monotonic() < deadline:
        time.sleep(0.05)
    # the step ends with RequestCancelled; DBOS then refuses to checkpoint a cancelled workflow's
    # step, so the body sees DBOS's own error (as it did before, only after the whole reply)
    assert outcome
    assert attempts.attempts() == []           # one request, no retry, no provider_error row
    assert handle.get_status().status == "CANCELLED"
