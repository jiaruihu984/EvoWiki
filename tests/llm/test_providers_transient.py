"""Per-request timeouts and transient SDK errors (timeout / connection / 5xx) in the providers."""

from types import SimpleNamespace
from typing import ClassVar

import pytest

from evowiki.llm.chat import ChatMessage, ChatRequest
from evowiki.llm.gateway import ProviderOutputError
from evowiki.llm.models import LLMRequest
from evowiki.llm.providers import (
    AnthropicProvider,
    DeepSeekProvider,
    ProviderBadRequestError,
    ProviderRejectedError,
    ProviderTransientError,
)

REQ = LLMRequest(purpose="goals.ask", prompt_version="v1", subject="page-1", system="SYS",
                 prompt="PROMPT")
CHAT = ChatRequest(purpose="assistant.turn", prompt_version="a@v1", subject="s", system="SYS",
                   messages=(ChatMessage(role="user", text="hi"),))
SCHEMA = {"type": "object", "properties": {"value": {"type": "integer"}}}


class _Recorder:
    """Fake SDK client (both shapes) that records create() kwargs and answers or raises."""

    def __init__(self, raises: BaseException | None = None):
        self.calls: list[dict] = []
        self._raises = raises
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._ds))
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._an))

    def _ds(self, **kw):
        self.calls.append(kw)
        if self._raises:
            raise self._raises
        return SimpleNamespace(
            model="deepseek-v4-pro", usage=None,
            choices=[SimpleNamespace(finish_reason="stop",
                                     message=SimpleNamespace(content='{"value": 1}',
                                                             tool_calls=None))])

    def _an(self, **kw):
        self.calls.append(kw)
        if self._raises:
            raise self._raises
        return SimpleNamespace(
            model="claude-sonnet-5-5", stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            content=[SimpleNamespace(type="text", text='{"value": 1}')])


def _each_call(client, timeout_for=None):
    ds = DeepSeekProvider(client=client, env={}, timeout_for=timeout_for)
    an = AnthropicProvider(client=client, timeout_for=timeout_for)
    return {
        "deepseek.complete": lambda: ds.complete("m", REQ, SCHEMA),
        "deepseek.chat": lambda: ds.chat("m", CHAT),
        "anthropic.complete": lambda: an.complete("m", REQ, SCHEMA),
        "anthropic.chat": lambda: an.chat("m", CHAT),
    }


@pytest.mark.parametrize("name", ["deepseek.complete", "deepseek.chat",
                                  "anthropic.complete", "anthropic.chat"])
def test_per_request_timeout_is_passed_from_the_purpose(name):
    client = _Recorder()
    seen = []

    def timeout_for(purpose):
        seen.append(purpose)
        return 123.0

    _each_call(client, timeout_for)[name]()
    assert client.calls[0]["timeout"] == 123.0
    assert seen == [REQ.purpose if name.endswith("complete") else CHAT.purpose]


@pytest.mark.parametrize("name", ["deepseek.complete", "deepseek.chat",
                                  "anthropic.complete", "anthropic.chat"])
def test_no_timeout_kwarg_without_timeout_for(name):
    client = _Recorder()
    _each_call(client)[name]()
    assert "timeout" not in client.calls[0]


def _http_error(sdk, kind, status=None, body=None):
    import httpx

    mod = pytest.importorskip(sdk)
    req = httpx.Request("POST", "https://example.invalid/v1")
    if kind == "timeout":
        return mod.APITimeoutError(request=req)
    if kind == "connection":
        return mod.APIConnectionError(request=req)
    resp = httpx.Response(status, request=req)
    cls = {500: mod.InternalServerError, 400: mod.BadRequestError,
           401: mod.AuthenticationError, 402: mod.APIStatusError,
           403: mod.PermissionDeniedError, 429: mod.RateLimitError}[status]
    return cls("boom", response=resp, body=body)


@pytest.mark.parametrize("sdk, names", [
    ("openai", ["deepseek.complete", "deepseek.chat"]),
    ("anthropic", ["anthropic.complete", "anthropic.chat"]),
])
@pytest.mark.parametrize("kind, status", [("timeout", None), ("connection", None),
                                          ("server", 500)])
def test_sdk_transient_errors_become_provider_transient_error(sdk, names, kind, status):
    for name in names:
        exc = _http_error(sdk, kind, status)
        with pytest.raises(ProviderTransientError) as ei:
            _each_call(_Recorder(exc))[name]()
        e = ei.value
        assert isinstance(e, ProviderOutputError) and e.status == "provider_error"
        assert e.__cause__ is exc
        assert type(exc).__name__ in str(e)
        assert ("goals.ask" if name.endswith("complete") else "assistant.turn") in str(e)
        assert e.raw_output is None and e.cost is None


@pytest.mark.parametrize("sdk, names", [
    ("openai", ["deepseek.complete", "deepseek.chat"]),
    ("anthropic", ["anthropic.complete", "anthropic.chat"]),
])
@pytest.mark.parametrize("status", [400])
def test_client_errors_are_a_refused_request_with_the_status_kept(sdk, names, status):
    for name in names:
        exc = _http_error(sdk, "server", status)
        with pytest.raises(ProviderBadRequestError) as ei:
            _each_call(_Recorder(exc))[name]()
        assert ei.value.status_code == status and ei.value.__cause__ is exc


def test_status_code_5xx_on_an_unknown_exception_is_transient_but_bugs_are_not():
    class Gateway502(Exception):
        status_code = 502

    with pytest.raises(ProviderTransientError):
        _each_call(_Recorder(Gateway502("x")))["deepseek.complete"]()
    with pytest.raises(KeyError):
        _each_call(_Recorder(KeyError("bug")))["deepseek.complete"]()


_ALL = [("openai", ["deepseek.complete", "deepseek.chat"]),
        ("anthropic", ["anthropic.complete", "anthropic.chat"])]


@pytest.mark.parametrize("sdk, names", _ALL)
def test_rate_limit_429_is_transient(sdk, names):
    for name in names:
        exc = _http_error(sdk, "server", 429)
        with pytest.raises(ProviderTransientError) as ei:
            _each_call(_Recorder(exc))[name]()
        assert ei.value.__cause__ is exc


@pytest.mark.parametrize("sdk, names", _ALL)
@pytest.mark.parametrize("status", [401, 402, 403])
def test_auth_balance_errors_become_provider_rejected_error(sdk, names, status):
    body = {"error": {"message": "Insufficient Balance", "type": "unknown_error"}}
    for name in names:
        exc = _http_error(sdk, "server", status, body=body)
        with pytest.raises(ProviderRejectedError) as ei:
            _each_call(_Recorder(exc))[name]()
        e = ei.value
        assert isinstance(e, ProviderOutputError) and e.status == "provider_error"
        assert not isinstance(e, ProviderTransientError)
        assert e.status_code == status and e.__cause__ is exc
        assert f"HTTP {status}" in str(e) and "Insufficient Balance" in str(e)
        assert ("goals.ask" if name.endswith("complete") else "assistant.turn") in str(e)
        assert "sk-" not in str(e) and "authorization" not in str(e).lower()


def test_rejected_message_without_provider_text_has_no_trailing_noise():
    class E402(Exception):
        status_code = 402
        body = None

    with pytest.raises(ProviderRejectedError) as ei:
        _each_call(_Recorder(E402("x")))["deepseek.complete"]()
    assert str(ei.value).endswith("HTTP 402")


_CREDIT_MSG = ("Your credit balance is too low to access the Anthropic API. "
               "Please go to Plans & Billing to upgrade or purchase credits.")


@pytest.mark.parametrize("sdk, names", _ALL)
def test_credit_balance_400_becomes_provider_rejected_error(sdk, names):
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": _CREDIT_MSG}}
    for name in names:
        exc = _http_error(sdk, "server", 400, body=body)
        with pytest.raises(ProviderRejectedError) as ei:
            _each_call(_Recorder(exc))[name]()
        e = ei.value
        assert not isinstance(e, ProviderTransientError)
        assert e.status_code == 400 and e.__cause__ is exc
        assert "HTTP 400" in str(e) and "credit balance is too low" in str(e)


def test_credit_balance_400_matches_without_a_body_and_ignoring_case():
    class E400(Exception):
        status_code = 400
        body = None

    with pytest.raises(ProviderRejectedError) as ei:
        _each_call(_Recorder(E400("Error code: 400 - YOUR CREDIT  BALANCE is too low")))[
            "anthropic.complete"]()
    assert ei.value.status_code == 400


def test_other_400s_and_credit_text_on_other_codes_are_refused_not_rejected():
    body = {"error": {"type": "invalid_request_error", "message": "max_tokens: too large"}}
    exc = _http_error("anthropic", "server", 400, body=body)
    with pytest.raises(ProviderBadRequestError) as ei:
        _each_call(_Recorder(exc))["anthropic.complete"]()
    assert ei.value.status_code == 400 and ei.value.__cause__ is exc

    class E404(Exception):
        status_code = 404
        body: ClassVar[dict] = {"error": {"message": "credit balance"}}

    with pytest.raises(ProviderBadRequestError) as ei:
        _each_call(_Recorder(E404("x")))["anthropic.complete"]()
    assert ei.value.status_code == 404
