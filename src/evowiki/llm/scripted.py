from collections.abc import Callable
from typing import Any

from evowiki.llm.chat import ChatRequest, ChatResponse, ChatTurn, StreamSink
from evowiki.llm.models import LLMRequest, ProviderResponse

Responder = Callable[[str, LLMRequest], str]


class ScriptedProvider:
    """Deterministic provider for tests and fixture recording. Never touches the network."""

    def __init__(self, name: str, responder: Responder):
        self.name = name
        self._responder = responder
        self.calls: list[LLMRequest] = []

    def complete(
        self, model: str, request: LLMRequest, json_schema: dict[str, Any]
    ) -> ProviderResponse:
        self.calls.append(request)
        return ProviderResponse(raw_output=self._responder(model, request),
                                model_version=f"{self.name}-scripted", cost=0.0)


class ScriptedChatProvider:
    """Deterministic tool-calling provider for tests. `chat` answers each ChatRequest with a
    ChatTurn (or a dict / JSON string of one); `complete` (optional) serves plain JSON calls so
    one provider can back both Gateway.call and Gateway.converse. Never touches the network.

    Streaming (`stream_chunk=n`): the provider streams; `chat(..., stream=sink)` sends the turn's
    text to the sink in pieces of n characters before it returns, calling `on_chunk(i)` before
    piece i (tests block or fail there: a `chat` callback that raises after some pieces is a
    provider that breaks mid-stream - give it the pieces via `ScriptedStreamFailure`)."""

    def __init__(self, name: str, chat: Callable[[str, ChatRequest], ChatTurn | dict | str],
                 complete: Responder | None = None, *, stream_chunk: int | None = None,
                 on_chunk: Callable[[int], None] | None = None):
        self.name = name
        self._chat = chat
        self._complete = complete
        self._stream_chunk = stream_chunk
        self._on_chunk = on_chunk
        self.chat_calls: list[ChatRequest] = []
        self.calls: list[LLMRequest] = []
        self.streamed: list[bool] = []

    @property
    def streams(self) -> bool:
        return self._stream_chunk is not None

    def _send(self, text: str, stream: StreamSink) -> None:
        n = self._stream_chunk or 1
        for i, at in enumerate(range(0, len(text), n)):
            if self._on_chunk is not None:
                self._on_chunk(i)
            stream.text(text[at:at + n])

    def chat(self, model: str, request: ChatRequest,
             stream: StreamSink | None = None) -> ChatResponse:
        self.chat_calls.append(request)
        self.streamed.append(stream is not None)
        try:
            out = self._chat(model, request)
        except ScriptedStreamFailure as fail:
            if stream is not None:
                self._send(fail.streamed, stream)
            raise fail.error from None
        turn = (out if isinstance(out, ChatTurn) else ChatTurn.model_validate_json(out)
                if isinstance(out, str) else ChatTurn.model_validate(out))
        if stream is not None:
            self._send(turn.text, stream)
        return ChatResponse(turn=turn, model_version=f"{self.name}-scripted", cost=0.0,
                            input_tokens=turn.input_tokens, output_tokens=turn.output_tokens,
                            cache_read_tokens=turn.cache_read_tokens,
                            cache_write_tokens=turn.cache_write_tokens)

    def complete(self, model: str, request: LLMRequest,
                 json_schema: dict[str, Any]) -> ProviderResponse:
        if self._complete is None:
            raise AssertionError(f"unscripted JSON call: {request.purpose}/{request.subject}")
        self.calls.append(request)
        return ProviderResponse(raw_output=self._complete(model, request),
                                model_version=f"{self.name}-scripted", cost=0.0)


class ScriptedStreamFailure(Exception):
    """Raised by a scripted `chat` callback: the provider streams `streamed`, then raises `error`
    (a provider that breaks off mid-answer)."""

    def __init__(self, streamed: str, error: BaseException):
        super().__init__(f"after {len(streamed)} chars: {error}")
        self.streamed = streamed
        self.error = error
