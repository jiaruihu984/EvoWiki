"""Token streaming of chat turns (spec 2026-10-15 §1). No paid API: the real SDK clients talk to a
local HTTP server that speaks each provider's SSE format. The streamed response folds into the same
ChatResponse as the non-streamed one (tokens, cache tokens, cost); a stream is bounded by progress:
no first event within the timeout is a queued request (ProviderBusyError; keep-alive comments do
not count), no event for that long after it started is ProviderTimeoutError, and a long answer that
keeps streaming is never cut; a stream cut before its end marker is never taken as an answer."""

import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from evowiki.llm.chat import ChatMessage, ChatRequest, ToolDef
from evowiki.llm.deadline import on_reply_started
from evowiki.llm.models import LLMRequest
from evowiki.llm.providers import (
    AnthropicProvider,
    DeepSeekProvider,
    ProviderBusyError,
    ProviderEmptyOutputError,
    ProviderTimeoutError,
    ProviderTransientError,
    anthropic_cost,
)

openai = pytest.importorskip("openai")
anthropic = pytest.importorskip("anthropic")

from evowiki.runtime.llm import _anthropic_http, _openai_http

CHAT = ChatRequest(purpose="assistant.turn", prompt_version="a@v1", subject="s1", system="SYS",
                   messages=(ChatMessage(role="user", text="hi"),),
                   tools=(ToolDef(name="read_k", description="d",
                                  parameters={"type": "object", "properties": {}}),))
SLACK = 1.5


class Sink:
    def __init__(self):
        self.chunks: list[str] = []
        self.at: list[float] = []

    def text(self, chunk):
        self.chunks.append(chunk)
        self.at.append(time.monotonic())


def _ds_chunk(delta=None, finish=None, usage=None):
    body = {"id": "c1", "object": "chat.completion.chunk", "created": 0,
            "model": "deepseek-v4-pro",
            "choices": [] if delta is None else [{"index": 0, "delta": delta,
                                                  "finish_reason": finish}]}
    if usage is not None:
        body["usage"] = usage
    return f"data: {json.dumps(body)}\n\n".encode()


DS_USAGE = {"prompt_tokens": 60, "completion_tokens": 7, "total_tokens": 67,
            "prompt_cache_hit_tokens": 50, "prompt_cache_miss_tokens": 10}


def ds_frames(text_parts, *, reasoning=(), calls=(), finish="stop"):
    out = [_ds_chunk({"role": "assistant", "content": ""})]
    out += [_ds_chunk({"reasoning_content": r}) for r in reasoning]
    out += [_ds_chunk({"content": t}) for t in text_parts]
    for i, (cid, name, arg_parts) in enumerate(calls):
        out.append(_ds_chunk({"tool_calls": [{"index": i, "id": cid, "type": "function",
                                              "function": {"name": name, "arguments": ""}}]}))
        out += [_ds_chunk({"tool_calls": [{"index": i, "function": {"arguments": a}}]})
                for a in arg_parts]
    out.append(_ds_chunk({}, finish=finish))
    out.append(_ds_chunk(usage=DS_USAGE))
    out.append(b"data: [DONE]\n\n")
    return out


def _an(event, data):
    return f"event: {event}\ndata: {json.dumps({'type': event, **data})}\n\n".encode()


def an_frames(text_parts, *, tool=None, stop="end_turn"):
    usage = {"input_tokens": 40, "output_tokens": 1, "cache_read_input_tokens": 300,
             "cache_creation_input_tokens": 20}
    out = [_an("message_start", {"message": {
        "id": "m1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
        "content": [], "stop_reason": None, "stop_sequence": None, "usage": usage}}),
        _an("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}})]
    out += [_an("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": t}})
            for t in text_parts]
    out.append(_an("content_block_stop", {"index": 0}))
    if tool is not None:
        tid, name, arg_parts = tool
        out.append(_an("content_block_start", {"index": 1, "content_block": {
            "type": "tool_use", "id": tid, "name": name, "input": {}}}))
        out += [_an("content_block_delta", {"index": 1, "delta": {
            "type": "input_json_delta", "partial_json": a}}) for a in arg_parts]
        out.append(_an("content_block_stop", {"index": 1}))
    out.append(_an("message_delta", {"delta": {"stop_reason": stop, "stop_sequence": None},
                                     "usage": {"output_tokens": 9}}))
    out.append(_an("message_stop", {}))
    return out


@contextmanager
def sse_server(frames: list[bytes], *, gap: float = 0.0, then_keepalive: float = 0.0,
               keepalive_first: float = 0.0, cut_after: int | None = None
               ) -> Iterator[SimpleNamespace]:
    """Serves `frames` as one SSE response, `gap` s apart. `keepalive_first`: SSE comments every
    50 ms for that long before the first frame (a queued request); `then_keepalive`: comments for
    that long after the last frame instead of closing; `cut_after`: close the connection after
    that many frames (no end marker)."""
    stop = threading.Event()
    state = SimpleNamespace(bodies=[], disconnected=threading.Event())

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            state.bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                end = time.monotonic() + keepalive_first
                while not stop.is_set() and time.monotonic() < end:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    time.sleep(0.05)
                for i, f in enumerate(frames):
                    if cut_after is not None and i >= cut_after:
                        break
                    self.wfile.write(f)
                    self.wfile.flush()
                    if gap:
                        time.sleep(gap)
                end = time.monotonic() + then_keepalive
                while not stop.is_set() and time.monotonic() < end:
                    self.wfile.write(b": keep-alive\n\n")
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


def deepseek(url, seconds=10.0):
    client = openai.OpenAI(api_key="local-test", base_url=url, timeout=seconds, max_retries=0)
    return DeepSeekProvider(client=client, max_tokens=64, thinking=False,
                            timeout_for=lambda purpose: seconds, http_client_factory=_openai_http)


def claude(url, seconds=10.0):
    client = anthropic.Anthropic(api_key="local-test", base_url=url, timeout=seconds,
                                 max_retries=0)
    return AnthropicProvider(client=client, timeout_for=lambda purpose: seconds,
                             http_client_factory=_anthropic_http)


# ---- DeepSeek ----

def test_deepseek_streams_content_and_folds_the_same_response():
    with sse_server(ds_frames(["方差", "大的", "方向"], reasoning=["想", "一想"])) as srv:
        sink = Sink()
        resp = deepseek(srv.url).chat("deepseek-v4-pro", CHAT, stream=sink)
        body = srv.bodies[0]
    assert sink.chunks == ["方差", "大的", "方向"]             # content only, never reasoning
    assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
    t = resp.turn
    assert t.text == "方差大的方向" and t.echo == {"reasoning_content": "想一想"}
    # DeepSeek cache hit / miss split, exactly as for a non-streamed reply (deepseek_tokens)
    assert (resp.input_tokens, resp.output_tokens, resp.cache_read_tokens,
            resp.cache_write_tokens) == (10, 7, 50, 0)
    assert (t.input_tokens, t.output_tokens, t.cache_read_tokens) == (10, 7, 50)
    assert resp.model_version == "deepseek-v4-pro" and resp.cost is None


def test_a_json_call_says_when_its_answer_starts_after_the_reasoning():
    frames = ds_frames(['{"a"', ": 1}"], reasoning=["想", "再想"])
    told_at: list[int] = []
    with sse_server(frames) as srv:
        provider = DeepSeekProvider(
            client=openai.OpenAI(api_key="local-test", base_url=srv.url, timeout=10.0, max_retries=0),
            max_tokens=64, thinking=True, timeout_for=lambda purpose: 10.0,
            http_client_factory=_openai_http)
        req = LLMRequest(purpose="goals.intake", prompt_version="v", subject="s", system="s", prompt="p")
        with on_reply_started(lambda: told_at.append(len(told_at))):
            resp = provider.complete("deepseek-v4-pro", req, {"type": "object"})
    assert resp.raw_output == '{"a": 1}' and told_at == [0]


def test_deepseek_tool_call_arguments_are_assembled_and_never_streamed():
    frames = ds_frames(["我查一下。"], calls=[("call_1", "read_k", ['{"concept', '": "c1"}'])],
                       finish="tool_calls")
    with sse_server(frames) as srv:
        sink = Sink()
        resp = deepseek(srv.url).chat("deepseek-v4-pro", CHAT, stream=sink)
    assert sink.chunks == ["我查一下。"]
    (c,) = resp.turn.tool_calls
    assert (c.id, c.name, c.args) == ("call_1", "read_k", {"concept": "c1"})


def test_deepseek_truncated_stream_is_a_paid_failure_with_its_tokens():
    with (sse_server(ds_frames(["半"], finish="length")) as srv,
          pytest.raises(ProviderEmptyOutputError) as info):
        deepseek(srv.url).chat("deepseek-v4-pro", CHAT, stream=Sink())
    e = info.value
    assert e.status == "truncated" and e.raw_output == "半"
    assert (e.input_tokens, e.output_tokens, e.cache_read_tokens) == (10, 7, 50)


def test_deepseek_stream_cut_before_its_end_is_transient_not_an_answer():
    with sse_server(ds_frames(["方差", "大的"]), cut_after=3) as srv:
        sink = Sink()
        with pytest.raises(ProviderTransientError):
            deepseek(srv.url).chat("deepseek-v4-pro", CHAT, stream=sink)
    assert sink.chunks == ["方差", "大的"]


def test_queued_keepalives_do_not_count_as_progress():
    """DeepSeek holds a queued request open with keep-alive comments: no first stream event within
    the timeout is a queued request, busy (llm_busy)."""
    with sse_server(ds_frames(["晚"]), keepalive_first=30) as srv:
        sink = Sink()
        start = time.monotonic()
        with pytest.raises(ProviderBusyError):
            deepseek(srv.url, seconds=1.0).chat("deepseek-v4-pro", CHAT, stream=sink)
        assert 1.0 <= time.monotonic() - start < 1.0 + SLACK
        assert srv.disconnected.wait(3)                       # the request was aborted
    assert sink.chunks == []


def test_a_stream_that_stalls_after_starting_times_out_and_nothing_streams_after_it():
    """Started, then only keep-alives: not queued, so a timeout (llm_timeout), not busy."""
    with sse_server(ds_frames(["先", "说"]), cut_after=3, then_keepalive=30) as srv:
        sink = Sink()
        start = time.monotonic()
        with pytest.raises(ProviderTimeoutError, match="stalled"):
            deepseek(srv.url, seconds=1.0).chat("deepseek-v4-pro", CHAT, stream=sink)
        raised = time.monotonic()
        assert raised - start < 1.0 + SLACK
        time.sleep(0.3)
    assert sink.chunks == ["先", "说"] and all(a <= raised for a in sink.at)


@pytest.mark.parametrize("calls", [False, True])
def test_a_long_answer_that_keeps_streaming_is_never_cut(calls):
    """2.4 s of stream with a 1 s timeout: every gap is 0.3 s, so it runs to the end. Tool
    arguments (never streamed to the user) count as progress too."""
    if calls:
        frames = ds_frames([], calls=[("call_1", "read_k",
                                       ['{"con', 'cept', '": ', '"c1', '"', '}'])],
                           finish="tool_calls")
    else:
        frames = ds_frames(["一", "二", "三", "四", "五", "六"])
    with sse_server(frames, gap=0.3) as srv:
        sink = Sink()
        start = time.monotonic()
        resp = deepseek(srv.url, seconds=1.0).chat("deepseek-v4-pro", CHAT, stream=sink)
        assert time.monotonic() - start > 2.0
    if calls:
        (c,) = resp.turn.tool_calls
        assert c.name == "read_k"
    else:
        assert resp.turn.text == "一二三四五六"


def test_anthropic_long_stream_is_never_cut():
    with sse_server(an_frames(["一", "二", "三", "四", "五"]), gap=0.3) as srv:
        resp = claude(srv.url, seconds=1.0).chat("claude-sonnet-5-5", CHAT, stream=Sink())
    assert resp.turn.text == "一二三四五"


def test_a_failing_sink_switches_streaming_off_but_the_answer_stands():
    class Bad(Sink):
        def text(self, chunk):
            raise RuntimeError("bus down")

    with sse_server(ds_frames(["a", "b"])) as srv:
        resp = deepseek(srv.url).chat("deepseek-v4-pro", CHAT, stream=Bad())
    assert resp.turn.text == "ab"


# ---- Anthropic ----

def test_anthropic_streams_text_and_folds_the_same_response_with_cache_tokens():
    frames = an_frames(["方差", "大"], tool=("tu1", "read_k", ['{"concept_id"', ': "c1"}']),
                       stop="tool_use")
    with sse_server(frames) as srv:
        sink = Sink()
        resp = claude(srv.url).chat("claude-sonnet-5-5", CHAT, stream=sink)
        body = srv.bodies[0]
    assert sink.chunks == ["方差", "大"] and body["stream"] is True
    t = resp.turn
    assert t.text == "方差大"
    (c,) = t.tool_calls
    assert (c.id, c.name, c.args) == ("tu1", "read_k", {"concept_id": "c1"})
    assert [b["type"] for b in t.echo] == ["text", "tool_use"]
    assert (resp.input_tokens, resp.output_tokens, resp.cache_read_tokens,
            resp.cache_write_tokens) == (40, 9, 300, 20)
    assert resp.cost == pytest.approx(anthropic_cost("claude-sonnet-5-5", 40, 9, 300, 20))


def test_anthropic_truncated_stream_is_a_paid_failure():
    with (sse_server(an_frames(["半"], stop="max_tokens")) as srv,
          pytest.raises(ProviderEmptyOutputError) as info):
        claude(srv.url).chat("claude-sonnet-5-5", CHAT, stream=Sink())
    assert info.value.status == "truncated" and info.value.cost is not None


def test_anthropic_queued_stream_hits_the_deadline_as_busy():
    with sse_server(an_frames(["晚"]), keepalive_first=30) as srv:
        start = time.monotonic()
        with pytest.raises(ProviderBusyError):
            claude(srv.url, seconds=1.0).chat("claude-sonnet-5-5", CHAT, stream=Sink())
        assert time.monotonic() - start < 1.0 + SLACK


# ---- a thinking JSON call (goals.intake over a long PDF) ----

def _thinking_deepseek(url, seconds):
    client = openai.OpenAI(api_key="local-test", base_url=url, timeout=seconds, max_retries=0)
    return DeepSeekProvider(client=client, max_tokens=64, thinking=True,
                            timeout_for=lambda purpose: seconds, http_client_factory=_openai_http)


def _json_req():
    from evowiki.llm.models import LLMRequest
    return LLMRequest(purpose="goals.intake", prompt_version="intake@v27", subject="u1",
                      system="SYS", prompt="把目标分成几大块" * 3000)   # a long (PDF-sized) prompt


def test_a_thinking_json_call_that_reasons_long_is_never_cut():
    """The blocks call over a big PDF: 2.4 s of reasoning with a 1 s timeout, a chunk every
    0.3 s. It streams (the reasoning keeps the stall clock alive) and folds to the same JSON
    response; a non-streamed call would have hit a 1 s total deadline."""
    frames = ds_frames(['{"value": ', "1}"], reasoning=["想"] * 6)
    with sse_server(frames, gap=0.3) as srv:
        start = time.monotonic()
        resp = _thinking_deepseek(srv.url, 1.0).complete("deepseek-v4-pro", _json_req(),
                                                          {"type": "object"})
        assert time.monotonic() - start > 2.0
        body = srv.bodies[0]
    assert json.loads(resp.raw_output) == {"value": 1}
    assert body["stream"] is True and body["response_format"] == {"type": "json_object"}
    assert body["thinking"] == {"type": "enabled"}
    assert (resp.input_tokens, resp.output_tokens, resp.cache_read_tokens) == (10, 7, 50)


def test_a_thinking_json_call_that_stalls_is_a_timeout():
    with sse_server(ds_frames(['{"value": 1}'], reasoning=["想"]), cut_after=2,
                    then_keepalive=30) as srv:
        start = time.monotonic()
        with pytest.raises(ProviderTimeoutError, match="stalled"):
            _thinking_deepseek(srv.url, 1.0).complete("deepseek-v4-pro", _json_req(),
                                                      {"type": "object"})
        assert time.monotonic() - start < 1.0 + SLACK
