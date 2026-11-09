"""Total wall-clock deadline per provider request (llm.deadline). No paid API: real SDK clients
talk to a local HTTP server that trickles keep-alive bytes, as DeepSeek does while it works."""

import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from evowiki.agent import durable
from evowiki.llm.chat import ChatMessage, ChatRequest
from evowiki.llm.deadline import (
    WORKER_NAME,
    AbortSwitch,
    DeadlineExceeded,
    bounded_sdk_call,
    call_with_deadline,
)
from evowiki.llm.gateway import Gateway
from evowiki.llm.models import GatewayConfig, LLMRequest, ModelRef
from evowiki.llm.providers import AnthropicProvider, DeepSeekProvider, ProviderTransientError
from tests.runtime.llm_helpers import MemoryAttemptLog, MemoryCallLog

openai = pytest.importorskip("openai")
anthropic = pytest.importorskip("anthropic")

from evowiki.runtime.llm import _anthropic_http, _openai_http

REQ = LLMRequest(purpose="goals.intake", prompt_version="v1", subject="g1", system="SYS",
                 prompt="PROMPT")
CHAT = ChatRequest(purpose="deep.write", prompt_version="d@v1", subject="p1", system="SYS",
                   messages=(ChatMessage(role="user", text="hi"),))
SCHEMA = {"type": "object", "properties": {"value": {"type": "integer"}}}
SLACK = 1.5   # seconds over the deadline a caller may wait (abort + scheduling)

DS_OK = {"id": "c1", "object": "chat.completion", "created": 0, "model": "deepseek-v4-pro",
         "choices": [{"index": 0, "finish_reason": "stop",
                      "message": {"role": "assistant", "content": '{"value": 1}'}}],
         "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}
AN_OK = {"id": "m1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
         "content": [{"type": "text", "text": '{"value": 1}'}], "stop_reason": "end_turn",
         "stop_sequence": None, "usage": {"input_tokens": 3, "output_tokens": 2}}


class Answer(BaseModel):
    value: int


def workers() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == WORKER_NAME]


def wait_until(cond, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


@contextmanager
def server(mode: str, trickle_for: float = 30.0) -> Iterator[SimpleNamespace]:
    """mode "trickle": headers, then one space every 50 ms for `trickle_for` s (each byte resets
    the SDK's read timeout), then the connection closes. "ok": a valid reply at once."""
    stop = threading.Event()
    state = SimpleNamespace(disconnected=threading.Event(), requests=0)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            state.requests += 1
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if mode == "ok":
                body = json.dumps(AN_OK if "messages" in self.path else DS_OK).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "10000000")
            self.end_headers()
            end = time.monotonic() + trickle_for
            try:
                while not stop.is_set() and time.monotonic() < end:
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(0.05)
            except OSError:
                state.disconnected.set()
            self.close_connection = True

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    state.url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield state
    finally:
        stop.set()
        httpd.shutdown()
        httpd.server_close()
        t.join(5)


def deepseek(url: str, seconds: float, factory=_openai_http) -> DeepSeekProvider:
    client = openai.OpenAI(api_key="local-test", base_url=url, timeout=seconds, max_retries=0)
    return DeepSeekProvider(client=client, max_tokens=64, thinking=False,
                            timeout_for=lambda purpose: seconds, http_client_factory=factory)


def claude(url: str, seconds: float) -> AnthropicProvider:
    client = anthropic.Anthropic(api_key="local-test", base_url=url, timeout=seconds,
                                 max_retries=0)
    return AnthropicProvider(client=client, timeout_for=lambda purpose: seconds,
                             http_client_factory=_anthropic_http)


# -- root cause ---------------------------------------------------------------------------------

def test_sdk_timeout_alone_does_not_bound_a_trickling_response():
    """The bug: timeout=0.5 is per phase; bytes every 50 ms keep the call alive until the server
    stops (here after 2.5 s; live, DeepSeek kept a 60 s call alive for 720 s)."""
    with server("trickle", trickle_for=2.5) as srv:
        client = openai.OpenAI(api_key="local-test", base_url=srv.url, max_retries=0)
        start = time.monotonic()
        with pytest.raises(Exception):   # noqa: B017 - the truncated body, whatever the SDK says
            client.chat.completions.create(model="m", messages=[], timeout=0.5)
        assert time.monotonic() - start >= 2.0


# -- the deadline, end to end through the real SDKs ---------------------------------------------

@pytest.mark.parametrize("make, call", [
    (deepseek, lambda p: p.complete("deepseek-v4-pro", REQ, SCHEMA)),
    (deepseek, lambda p: p.chat("deepseek-v4-pro", CHAT)),
    (claude, lambda p: p.complete("claude-sonnet-5-5", REQ, SCHEMA)),
    (claude, lambda p: p.chat("claude-sonnet-5-5", CHAT)),
])
def test_trickling_call_fails_transient_within_the_deadline_and_leaks_nothing(make, call):
    baseline = threading.active_count()
    with server("trickle") as srv:
        provider = make(srv.url, 1.0)
        start = time.monotonic()
        with pytest.raises(ProviderTransientError, match="total deadline of 1s") as info:
            call(provider)
        took = time.monotonic() - start
        assert 1.0 <= took < 1.0 + SLACK
        assert info.value.status == "provider_error"
        assert isinstance(info.value.__cause__, DeadlineExceeded)
        assert wait_until(lambda: not workers())          # the worker thread ended
        assert srv.disconnected.wait(3)                  # and its connection was closed
        assert srv.requests == 1                         # no SDK retry
    assert wait_until(lambda: threading.active_count() <= baseline)


@pytest.mark.parametrize("make, model", [(deepseek, "deepseek-v4-pro"),
                                         (claude, "claude-sonnet-5-5")])
def test_fast_call_is_unaffected(make, model):
    with server("ok") as srv:
        provider = make(srv.url, 5.0)
        resp = provider.complete(model, REQ, SCHEMA)
        assert json.loads(resp.raw_output) == {"value": 1}
        assert (resp.input_tokens, resp.output_tokens) == (3, 2)
        assert wait_until(lambda: not workers())


def test_repeated_timeouts_do_not_accumulate_threads():
    baseline = threading.active_count()
    with server("trickle") as srv:
        provider = deepseek(srv.url, 0.3)
        for _ in range(5):
            with pytest.raises(ProviderTransientError):
                provider.complete("deepseek-v4-pro", REQ, SCHEMA)
        assert wait_until(lambda: not workers())
    assert wait_until(lambda: threading.active_count() <= baseline)


def test_runtime_providers_get_the_abortable_http_client(monkeypatch):
    from evowiki.runtime.llm import runtime_deep_providers, runtime_providers

    env = {"ANTHROPIC_API_KEY": "k-a", "DEEPSEEK_API_KEY": "k-d"}
    for got in (runtime_providers(env), runtime_deep_providers(env),
                runtime_providers({**env, "EVOWIKI_RUNTIME_PROVIDER": "deepseek"})):
        for p in got.values():
            assert p._http_client_factory in (_anthropic_http, _openai_http)
            assert p._timeout_for is not None


# -- gateway audit, deep writer fallback, durable step ------------------------------------------

def test_gateway_audits_the_deadline_as_a_provider_error_with_latency():
    ref = ModelRef(provider="deepseek-nothink", model="deepseek-v4-pro")
    attempts = MemoryAttemptLog()
    with server("trickle") as srv:
        gw = Gateway(GatewayConfig(generator=ref, verifier=ref, allow_single_provider=True),
                     MemoryCallLog(), mode="record", attempts=attempts, record_max_attempts=3,
                     providers={"deepseek-nothink": deepseek(srv.url, 0.5)})
        with pytest.raises(ProviderTransientError):
            gw.call("generator", REQ, Answer)
        assert srv.requests == 1     # a transient error is never retried by the gateway
    [row] = attempts.attempts()
    # a non-streamed request at its total deadline may be queued or still working: a timeout
    assert row.status == "provider_error" and row.error_type == "ProviderTimeoutError"
    assert "total deadline" in row.error_message
    assert 0.5 <= row.latency < 0.5 + SLACK


def test_deadline_holds_inside_a_durable_step():
    ref = ModelRef(provider="deepseek", model="deepseek-v4-pro")
    with server("trickle") as srv:
        gw = Gateway(GatewayConfig(generator=ref, verifier=ref, allow_single_provider=True),
                     MemoryCallLog(), mode="record", record_max_attempts=1,
                     providers={"deepseek": deepseek(srv.url, 0.5)})
        start = time.monotonic()
        with durable.virtual_workflow("wf"), pytest.raises(ProviderTransientError):
            gw.call("generator", REQ, Answer)
        assert time.monotonic() - start < 0.5 + SLACK
        assert wait_until(lambda: not workers())


def test_deep_writer_falls_back_when_the_primary_hits_its_deadline():
    from dataclasses import replace

    from evowiki.deep.writer import PageWriter
    from evowiki.wikiapp.repo import MemoryRepo
    from tests.deep.fakes import DeepScript, FakeChecker, library
    from tests.deep.test_writer import FALLBACK, outline_input, write_all
    from tests.wikiapp.script import Script
    from tests.wikiapp.test_goal import Ids
    from tests.wikiapp.test_page import organized

    env = (MemoryRepo(["u1"]), Script(), Ids())
    pid = organized(env)
    ds = DeepScript().plan().write(*write_all())
    with server("trickle") as srv:
        hung = claude(srv.url, 0.5)
        scripted = ds.provider("anthropic")

        class Primary:      # plans from the script, writes through the hung real SDK
            name = "anthropic"
            complete = staticmethod(scripted.complete)
            chat = staticmethod(hung.chat)

        llm = replace(ds.llm(), fallback=FALLBACK,
                      providers={"anthropic": Primary(), "deepseek": ds.provider("deepseek")})
        notes = []
        res = PageWriter(llm=llm, checker=FakeChecker(), library=library()).write(
            outline_input(env, pid), on_writer=lambda m, fb, why: notes.append((m, fb, why)))
        assert srv.requests == 1
    assert res.model == "deepseek:deepseek-v4-pro"
    assert "total deadline" in res.fallback_reason
    assert notes[1][:2] == ("deepseek:deepseek-v4-pro", True)
    assert wait_until(lambda: not workers())


# -- unit: call_with_deadline / bounded_sdk_call ------------------------------------------------

def test_call_with_deadline_aborts_a_blocked_call():
    release = threading.Event()
    start = time.monotonic()
    with pytest.raises(DeadlineExceeded):
        call_with_deadline(lambda: release.wait(30), 0.2, release.set)
    assert time.monotonic() - start < 0.2 + SLACK
    assert wait_until(lambda: not workers())


def test_call_with_deadline_passes_results_and_errors_through():
    assert call_with_deadline(lambda: 42, 1.0, lambda: None) == 42
    with pytest.raises(KeyError):
        call_with_deadline(lambda: {}["x"], 1.0, lambda: None)
    with pytest.raises(ValueError):
        call_with_deadline(lambda: 1, 0, lambda: None)


def test_bounded_sdk_call_uses_and_closes_a_per_call_client():
    made = []

    class Http:
        def __init__(self, timeout, hooks):
            self.timeout, self.hooks, self.closed = timeout, hooks, 0
            made.append(self)

        def close(self):
            self.closed += 1

    class Sdk:
        def __init__(self, http=None):
            self.http = http
            self.options = None

        def with_options(self, **kw):
            sdk = Sdk(kw["http_client"])
            sdk.options = kw
            return sdk

    seen = []
    assert bounded_sdk_call(Sdk(), lambda c: seen.append(c) or "ok", 2.0, Http) == "ok"
    [http] = made
    assert seen[0].http is http and seen[0].options["max_retries"] == 0
    assert http.timeout == 2.0 and set(http.hooks) == {"request", "response"}
    assert http.closed == 1


def test_abort_switch_hooks_refuse_after_abort():
    switch = AbortSwitch()
    closed = []
    hooks = switch.event_hooks()
    hooks["request"][0](object())                 # before the abort: fine
    hooks["response"][0](SimpleNamespace(close=lambda: closed.append("r1")))
    switch.abort()
    assert closed == ["r1"] and switch.aborted
    with pytest.raises(DeadlineExceeded):
        hooks["request"][0](object())
    with pytest.raises(DeadlineExceeded):
        hooks["response"][0](SimpleNamespace(close=lambda: closed.append("r2")))
    assert closed == ["r1", "r2"]
