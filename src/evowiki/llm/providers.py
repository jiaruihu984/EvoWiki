import copy
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from functools import lru_cache, partial
from typing import Any

from evowiki.llm.budget import max_output
from evowiki.llm.chat import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ChatTurn,
    ImagesNotSupported,
    StreamSink,
    ToolCall,
    memory_fenced,
)
from evowiki.llm.deadline import (
    DeadlineExceeded,
    HttpClientFactory,
    NoProgress,
    Progress,
    RequestCancelled,
    bounded_sdk_call,
    cancel_requested,
    reply_started,
)
from evowiki.llm.exposure import DROPS_STALE_THINKING
from evowiki.llm.gateway import Provider, ProviderOutputError
from evowiki.llm.models import (
    TEMPERATURE_PARAM,
    THINKING_PARAM,
    GatewayConfig,
    LLMRequest,
    ProviderResponse,
)
from evowiki.llm.prices import PriceTable, load_prices
from evowiki.tokens import estimate_tokens

ENV_KEYS: dict[str, str] = {"anthropic": "ANTHROPIC_API_KEY", "deepseek": "DEEPSEEK_API_KEY"}
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
# DeepSeek's Beta features (strict function calling) are served under this base URL only.
DEEPSEEK_BETA_BASE_URL = "https://api.deepseek.com/beta"
# "1" / "true" / "on": DeepSeek `complete` calls use strict function calling (deepseek_strict_schema)
# instead of json_object + the schema pasted into the prompt. Default off until the live check.
DEEPSEEK_STRICT_ENV = "EVOWIKI_DEEPSEEK_STRICT"
# The one tool a strict `complete` call offers; its arguments are the answer.
DEEPSEEK_STRICT_TOOL = "respond"
CLIENT_TIMEOUT_S = 300.0   # default-built SDK clients (as runtime.llm.RUNTIME_TIMEOUT_DEEP_S)
DEEPSEEK_MAX_TOKENS_ENV = "EVOWIKI_DEEPSEEK_MAX_TOKENS"
DEEPSEEK_DEFAULT_MAX_TOKENS = 32768  # reasoning models spend much of the budget before content
# Smallest prompt prefix (tokens) a model caches; a shorter one is silently not cached. A model
# not listed uses CACHE_MIN_DEFAULT.
CACHE_MIN_TOKENS: dict[str, int] = {
    "claude-opus-5-5": 512,
    "claude-sonnet-5-5": 512,
    "claude-haiku-5-5": 512,
    "claude-haiku-4-5": 4096,
}
CACHE_MIN_DEFAULT = 1024
# complete() purposes whose system prompt is the same on every call (a constant plus the
# language rule): the system block gets a 1-hour breakpoint. A purpose matches an entry equal to
# it or starting with it followed by ".".
STABLE_SYSTEM_PURPOSES = ("goals.intake", "goals.enter", "graph.group_naming", "rsi")


class ProviderRefusalError(ProviderOutputError):
    default_status = "refusal"


class ProviderEmptyOutputError(ProviderOutputError):
    """No usable text: status "truncated" (max_tokens) or "empty_output"."""

    default_status = "empty_output"


class ProviderTransientError(ProviderOutputError):
    """The SDK call timed out (per-phase SDK timeout or the total deadline of
    `llm.deadline`), could not connect, or got an HTTP 5xx: no response to bill.
    Audited as "provider_error"; callers treat it as one failed attempt (never retried by the
    SDK or, at runtime, the Gateway)."""

    default_status = "provider_error"


def _first_choice(resp: Any, where: str) -> Any:
    """The first choice of a non-streamed OpenAI-compatible response. DeepSeek sometimes answers
    200 with no `choices` (null, or the whole body null; live harness round 2026-10-28): that is
    no answer, a ProviderTransientError (one failed attempt, handled like a dropped connection),
    never an AttributeError deep in the caller."""
    choices = getattr(resp, "choices", None) if resp is not None else None
    if not choices:
        raise ProviderTransientError(f"deepseek returned no choices for {where}")
    return choices[0]


# The user-facing lines of a busy provider and of a request that timed out. They never name the
# provider (routing is not shown to the learner) and never say to switch model (there is nowhere
# to). The plugin shows exactly these lines (api/busy.ts).
BUSY_TEXT = "请切换模型重试"
TIMEOUT_TEXT = "请切换模型重试"


class ProviderNoticeError(ProviderTransientError):
    """A transient failure the user is told about in a fixed line: services report it as `code`
    with `user_text`. Still a transient error, so retry / fallback handling is unchanged."""

    code = ""
    text = ""

    def __init__(self, message: str = "", *, provider: str = "", **kw: Any):
        super().__init__(message, **kw)
        self.provider = provider
        self.user_text = self.text


class ProviderBusyError(ProviderNoticeError):
    """The provider is overloaded: HTTP 429 (rate / concurrency limit), or a streamed request got
    no stream event at all within its timeout (it sat queued: DeepSeek holds a queued request
    open with keep-alive comments for up to 10 minutes)."""

    code = "llm_busy"
    text = BUSY_TEXT


class ProviderTimeoutError(ProviderNoticeError):
    """The request timed out without being shown to be queued: a streamed one stalled after it
    started, a non-streamed one hit its total deadline (queued or still working, nothing tells
    them apart), or the SDK's own timeout fired."""

    code = "llm_timeout"
    text = TIMEOUT_TEXT


def notice_error(exc: BaseException | None) -> ProviderNoticeError | None:
    """The ProviderNoticeError behind `exc` (itself, or along its __cause__ / __context__ chain)."""
    seen = 0
    while exc is not None and seen < 20:
        if isinstance(exc, ProviderNoticeError):
            return exc
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return None


class ProviderRejectedError(ProviderOutputError):
    """The provider refused the request itself: HTTP 401/402/403 (bad key, insufficient
    balance, no access), or Anthropic's HTTP 400 "credit balance is too low". Retrying cannot
    help, so callers must not retry. Audited as "provider_error". The message never carries the API key or any header."""

    default_status = "provider_error"

    def __init__(self, message: str = "", *, status_code: int | None = None, **kw: Any):
        super().__init__(message, **kw)
        self.status_code = status_code


class ProviderBadRequestError(ProviderOutputError):
    """The provider refused this one request (any other HTTP 4xx: a thinking block, a limit, a
    prompt too long). The model and the key are fine, so it never marks a model unusable the way
    ProviderRejectedError does; the same request fails again, so it is shown as 请切换模型重试.
    `status_code` and `body` stay on it: llm.exposure reads the provider's text."""

    default_status = "provider_error"

    def __init__(self, message: str = "", *, status_code: int, body: Any = None, **kw: Any):
        super().__init__(message, **kw)
        self.status_code = status_code
        self.body = body


_REJECTED_CODES = frozenset({401, 402, 403})
# Anthropic reports an empty prepaid balance as HTTP 400 invalid_request_error ("Your credit
# balance is too low to access the Anthropic API ..."), not 402.
_CREDIT_BALANCE_PHRASE = "credit balance"
# httpx's TransportError: a streamed body that breaks off mid-read (the SDKs do not wrap it);
# StreamCut: a stream that ended without its final event.
_TRANSIENT_CLASS_NAMES = frozenset({"APITimeoutError", "APIConnectionError", "InternalServerError",
                                    "TransportError", "StreamCut"})
log = logging.getLogger("evowiki.llm")


def _is_transient(exc: BaseException) -> bool:
    """Timeout / connection error / 5xx, matched by class name in the MRO so neither SDK has to
    be imported here (APITimeoutError subclasses APIConnectionError in both)."""
    if any(c.__name__ in _TRANSIENT_CLASS_NAMES for c in type(exc).__mro__):
        return True
    if type(exc).__name__ == "APIError":     # openai: an `error` event inside a 200 stream
        return True
    code = getattr(exc, "status_code", None)
    # 200: anthropic raises an error event inside a streamed 200 response (e.g. overloaded)
    return (isinstance(code, int) and not isinstance(code, bool)
            and (code >= 500 or code == 429 or code == 200))


def _is_timeout(exc: BaseException) -> bool:
    """The SDK's own timeout (APITimeoutError in either SDK, matched by name as in _is_transient)."""
    return any(c.__name__ == "APITimeoutError" for c in type(exc).__mro__)


def _status_code(exc: BaseException) -> int | None:
    code = getattr(exc, "status_code", None)
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def _short_message(exc: BaseException) -> str:
    """The provider's own short error text (body.error.message, else exc.message), one line,
    at most 200 characters. Never reads headers or the request."""
    body = getattr(exc, "body", None)
    inner = body.get("error") if isinstance(body, dict) else None
    msg = inner.get("message") if isinstance(inner, dict) else None
    if not isinstance(msg, str) or not msg.strip():
        msg = getattr(exc, "message", None)
    if not isinstance(msg, str):
        return ""
    return " ".join(msg.split())[:200]


def _is_credit_balance_error(exc: BaseException, code: int | None) -> bool:
    """HTTP 400 (BadRequestError in either SDK) whose provider text mentions the credit balance,
    looked up in body.error.message, exc.message and str(exc), case-insensitively."""
    if code != 400:
        return False
    texts = [_short_message(exc), str(exc)]
    return any(_CREDIT_BALANCE_PHRASE in " ".join(t.split()).lower() for t in texts)


def _retry_after(exc: BaseException) -> float | None:
    """The Retry-After of an SDK's HTTP error, in seconds; None when absent or a date."""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    try:
        return float(headers.get("retry-after")) if headers is not None else None
    except (TypeError, ValueError):
        return None


def _sdk_call(provider: str, request: Any, create: Callable[..., Any], **kwargs: Any) -> Any:
    try:
        return create(**kwargs)
    except RequestCancelled:     # the run was stopped: not a provider failure (llm.deadline)
        raise
    except Exception as exc:
        where = f"{provider} request failed for {request.purpose}/{request.subject}"
        queued = isinstance(exc, NoProgress) and not exc.started
        if queued or _status_code(exc) == 429:
            why = f"NoProgress ({exc})" if queued else "HTTP 429"
            busy = ProviderBusyError(f"{BUSY_TEXT}: {where}: {why}", provider=provider)
            busy.rate_limited, busy.retry_after = not queued, None if queued else _retry_after(exc)
            raise busy from exc
        if isinstance(exc, DeadlineExceeded) or _is_timeout(exc):
            why = f"{type(exc).__name__} ({exc})"
            raise ProviderTimeoutError(f"{TIMEOUT_TEXT}: {where}: {why}",
                                       provider=provider) from exc
        if _is_transient(exc):
            raise ProviderTransientError(
                f"{provider} request failed for {request.purpose}/{request.subject}: "
                f"{type(exc).__name__}") from exc
        code = _status_code(exc)
        if code in _REJECTED_CODES or _is_credit_balance_error(exc, code):
            detail = _short_message(exc)
            raise ProviderRejectedError(
                f"{provider} rejected the request for {request.purpose}/{request.subject}: "
                f"HTTP {code}" + (f" {detail}" if detail else ""),
                status_code=code) from exc
        if code is not None and 400 <= code < 500:
            detail = _short_message(exc)
            raise ProviderBadRequestError(
                f"{provider} refused the request for {request.purpose}/{request.subject}: "
                f"HTTP {code}" + (f" {detail}" if detail else ""),
                status_code=code, body=getattr(exc, "body", None)) from exc
        raise


# Constraints Anthropic structured outputs reject (400 invalid_request_error). They are dropped from
# the schema sent to the API only; the parsed reply is still validated against the full pydantic
# schema, so a reply that breaks one is a failed attempt as before.
_UNSUPPORTED = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
                "minLength", "maxLength", "pattern", "maxItems", "uniqueItems")


_SCALARS = ("string", "number", "boolean", "null")
_SCHEMA_KEYS = ("type", "anyOf", "allOf", "oneOf", "$ref", "enum", "const", "properties", "items")


def _typeless(node: dict[str, Any]) -> bool:
    """A schema node that constrains nothing (pydantic renders `Any` as {} or {"title": ...}).
    Anthropic structured outputs reject a node without a type, so such a field is sent as any
    JSON scalar; the reply is still validated against the full pydantic schema (Any) and parsed
    leniently by the caller. A field that must hold a list or an object needs a real type."""
    return not any(k in node for k in _SCHEMA_KEYS) and set(node) <= {"title", "description"}


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(schema)

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            node.pop("default", None)
            if _typeless(node):            # a pydantic `Any` field: the API needs a type
                node["anyOf"] = [{"type": t} for t in _SCALARS]
            if node.get("type") in ("string", "number", "integer", "array"):
                for key in _UNSUPPORTED:
                    node.pop(key, None)
                if node.get("minItems", 0) > 1:      # only 0 and 1 are accepted
                    node["minItems"] = 1
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"])
            for v in node.values():
                visit(v)
        elif isinstance(node, list):
            for v in node:
                visit(v)

    visit(out)
    return out


# DeepSeek strict function calling (Beta, https://api-docs.deepseek.com/guides/tool_calls, read
# 2026-10-24): types object / string / number / integer / boolean / array / enum / anyOf; every
# object closed (additionalProperties false, every property required); string `pattern` and
# `format` in DEEPSEEK_STRICT_FORMATS; number const / minimum / maximum / exclusive* / multipleOf;
# NOT string minLength / maxLength nor array minItems / maxItems. As for Anthropic, what is dropped
# leaves the API schema only: the reply is still validated against the full pydantic schema.
DEEPSEEK_STRICT_FORMATS = frozenset({"email", "hostname", "ipv4", "ipv6", "uuid"})
_DEEPSEEK_DROPPED = ("title", "default", "minLength", "maxLength", "minItems", "maxItems",
                     "uniqueItems", "discriminator", "examples")


class StrictSchemaError(ValueError):
    """A schema DeepSeek strict mode cannot express (an open map, a recursive $ref)."""


def deepseek_strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """`schema` (pydantic's model_json_schema) as DeepSeek strict mode accepts it: `$ref`s inlined
    (the docs spell the definitions key `$def`, pydantic `$defs`: inlining avoids depending on
    either; the product's output schemas are never recursive, tests/llm/
    test_anthropic_output_schemas.py), titles / defaults / length and count limits dropped,
    `oneOf` sent as `anyOf`, a one-element `allOf` merged, a string `const` sent as a one-value
    `enum`, an untyped (`Any`) node as any JSON scalar (as `strict_schema`), every object closed
    with every property required. Raises StrictSchemaError for an open map or a recursive ref."""
    defs = schema.get("$defs", {})

    def resolve(ref: str, seen: tuple[str, ...]) -> Any:
        name = ref.rsplit("/", 1)[-1]
        if not ref.startswith("#/$defs/") or name not in defs:
            raise StrictSchemaError(f"unresolvable $ref {ref!r}")
        if name in seen:
            raise StrictSchemaError(f"recursive $ref {ref!r}")
        return conv(defs[name], (*seen, name))

    def conv(node: Any, seen: tuple[str, ...]) -> Any:
        if isinstance(node, list):
            return [conv(v, seen) for v in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            base = resolve(node["$ref"], seen)
            extra = {k: v for k, v in node.items() if k == "description"}
            return {**base, **extra}
        node = dict(node)
        if isinstance(node.get("allOf"), list) and len(node["allOf"]) == 1:
            inner = node.pop("allOf")[0]
            node = {**inner, **node}
            if "$ref" in node:
                return conv(node, seen)
        if "oneOf" in node:
            node["anyOf"] = node.pop("oneOf")
        if _typeless(node):
            node["anyOf"] = [{"type": t} for t in _SCALARS]
        if "const" in node and node.get("type") not in ("number", "integer"):
            node["enum"] = [node.pop("const")]
        if node.get("format") is not None and node["format"] not in DEEPSEEK_STRICT_FORMATS:
            node.pop("format")
        for key in _DEEPSEEK_DROPPED:
            node.pop(key, None)
        node.pop("$defs", None)
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k == "properties":
                out[k] = {name: conv(sub, seen) for name, sub in v.items()}
            elif k in ("items", "anyOf", "allOf", "prefixItems", "not"):
                out[k] = conv(v, seen)
            else:
                out[k] = v
        if out.get("type") == "object" or "properties" in out:
            if isinstance(out.get("additionalProperties"), dict) or "properties" not in out:
                raise StrictSchemaError("an object without fixed properties (an open map)")
            out["additionalProperties"] = False
            out["required"] = list(out["properties"])
        return out

    return conv(schema, ())


@lru_cache(maxsize=1)
def _default_prices() -> PriceTable:
    return load_prices()


def anthropic_cost(model: str, input_tokens: int, output_tokens: int,
                   cache_read_tokens: int = 0, cache_write_tokens: int = 0,
                   cache_write_1h_tokens: int = 0) -> float | None:
    """USD for one Anthropic response from llm/prices.toml (the single price table, via
    PriceTable); None for a model without a price. `cache_write_tokens` is every token written;
    `cache_write_1h_tokens` the part of them written with the 1-hour TTL."""
    return _default_prices().cost("anthropic", model, input_tokens, output_tokens,
                                  cache_read_tokens, cache_write_tokens, cache_write_1h_tokens)


def _cache_tokens(usage: Any) -> dict[str, int]:
    """Prompt-cache counts from an Anthropic usage object; missing or None reads as 0.
    cache_write_1h_tokens: the 1-hour part of the writes (usage.cache_creation)."""
    created = getattr(usage, "cache_creation", None)
    return {"cache_read_tokens": getattr(usage, "cache_read_input_tokens", None) or 0,
            "cache_write_tokens": getattr(usage, "cache_creation_input_tokens", None) or 0,
            "cache_write_1h_tokens": getattr(created, "ephemeral_1h_input_tokens", None) or 0}


def _anthropic_cost(resp: Any) -> float | None:
    usage = getattr(resp, "usage", None)
    if usage is None:
        return None
    return anthropic_cost(resp.model, usage.input_tokens, usage.output_tokens,
                          **_cache_tokens(usage))


def _block_dict(block: Any) -> dict[str, Any]:
    if hasattr(block, "model_dump"):
        return block.model_dump(mode="json", exclude_none=True)
    return dict(vars(block))


def _part_label(part: Any) -> str:
    if part.kind == "doc":
        return f"【附件：{part.title}（source_id={part.source_id}）】\n{part.text}"
    return part.text


def _anthropic_user_content(m: ChatMessage) -> str | list[dict[str, Any]]:
    if m.origin == "memory":
        return [{"type": "text", "text": memory_fenced(m.text)}]
    if not m.parts:
        return m.text
    blocks: list[dict[str, Any]] = []
    for p in m.parts:
        if p.kind == "image":
            blocks.append({"type": "text", "text": f"【图片：{p.name}】"})
            blocks.append({"type": "image", "source": {
                "type": "base64", "media_type": p.media_type, "data": p.data_b64}})
        else:
            blocks.append({"type": "text", "text": _part_label(p)})
    if m.text:
        blocks.append({"type": "text", "text": m.text})
    return blocks


def anthropic_messages(request: ChatRequest) -> list[dict[str, Any]]:
    """Anthropic Messages API history. An assistant message with `echo` (its own content
    blocks, thinking included) is resent verbatim; adjacent tool results become one user
    message of tool_result blocks. A recalled-memory message (`origin="memory"`) is its own
    fenced text block, merged into the user message after it (the fence first)."""
    out: list[dict[str, Any]] = []
    merging_results = False
    recalled: list[dict[str, Any]] = []      # a memory message's blocks, for the next user one
    for m in request.messages:
        if recalled and m.role != "user":
            out.append({"role": "user", "content": recalled})
            recalled = []
        if m.role == "tool":
            content: Any = m.text
            if m.parts:
                content = [{"type": "text", "text": m.text}, *(
                    {"type": "image", "source": {"type": "base64", "media_type": p.media_type, "data": p.data_b64}}
                    for p in m.parts if p.kind == "image")]
            block = {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": content,
                     "is_error": m.is_error}
            if merging_results:
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
                merging_results = True
            continue
        merging_results = False
        if m.role == "user" and m.origin == "memory":
            recalled = [*recalled, *_anthropic_user_content(m)]
        elif m.role == "user":
            content = _anthropic_user_content(m)
            if recalled:
                blocks = (content if not isinstance(content, str)
                          else [{"type": "text", "text": content}] if content else [])
                content, recalled = [*recalled, *blocks], []
            out.append({"role": "user", "content": content})
        elif isinstance(m.echo, list):
            out.append({"role": "assistant", "content": m.echo})
        else:
            content: list[dict[str, Any]] = [{"type": "text", "text": m.text}] if m.text else []
            content += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.args}
                        for c in m.tool_calls]
            out.append({"role": "assistant", "content": content})
    if recalled:
        out.append({"role": "user", "content": recalled})
    return out


_EPHEMERAL = {"type": "ephemeral"}   # default 5-minute TTL: the moving tail
_EPHEMERAL_1H = {"type": "ephemeral", "ttl": "1h"}   # stable prefixes shared across requests
_UNMARKABLE = frozenset({"thinking", "redacted_thinking"})   # the API rejects cache_control there


def _markable(block: Any) -> bool:
    if not isinstance(block, dict) or block.get("type") in _UNMARKABLE:
        return False
    return not (block.get("type") == "text" and not str(block.get("text", "")).strip())


def _mark_message(msg: dict[str, Any]) -> dict[str, Any]:
    """A copy of `msg` whose last markable content block carries the (5-minute) cache marker. A
    string content becomes one text block. Only copies are changed; the input is never mutated."""
    content = msg["content"]
    if isinstance(content, str):
        if not content.strip():
            return msg
        return {**msg, "content": [{"type": "text", "text": content,
                                    "cache_control": dict(_EPHEMERAL)}]}
    if not isinstance(content, list):
        return msg
    for i in range(len(content) - 1, -1, -1):
        if _markable(content[i]):
            blocks = list(content)
            blocks[i] = {**content[i], "cache_control": dict(_EPHEMERAL)}
            return {**msg, "content": blocks}
    return msg


def _mark_split(msg: dict[str, Any], stable: int) -> dict[str, Any]:
    """The first user message with a stable prefix: two text blocks, the stable part with a
    1-hour marker and the rest with a 5-minute one (the text is unchanged when joined). The whole
    text stable: one block with the 1-hour marker."""
    text = msg["content"]
    if isinstance(text, list):
        # attachments before the question: the stable prefix is part of the last text block
        # (the question) only, so the blocks before it stay as they are
        last = text[-1]
        if last.get("type") != "text":
            return _mark_message(msg)
        head_msg = _mark_split({**msg, "content": last["text"]}, stable)
        tail_blocks = head_msg["content"]
        if isinstance(tail_blocks, str):
            return {**msg, "content": [*text[:-1], {"type": "text", "text": tail_blocks}]}
        return {**msg, "content": [*text[:-1], *tail_blocks]}
    head, tail = text[:stable], text[stable:]
    if not head.strip():
        return _mark_message(msg)
    if not tail.strip():
        return {**msg, "content": [{"type": "text", "text": text,
                                    "cache_control": dict(_EPHEMERAL_1H)}]}
    return {**msg, "content": [
        {"type": "text", "text": head, "cache_control": dict(_EPHEMERAL_1H)},
        {"type": "text", "text": tail, "cache_control": dict(_EPHEMERAL)}]}


def cache_marked_messages(messages: list[dict[str, Any]],
                          stable: int = 0) -> list[dict[str, Any]]:
    """Breakpoints on the first user message (the round's prompt) and on the last message (the
    moving tail); one marker when they are the same message. `stable` > 0: the first user
    message's leading `stable` characters are shared across requests and get their own 1-hour
    breakpoint (`_mark_split`): at most 3 markers here, 2 without a stable prefix."""
    out = list(messages)
    first = next((i for i, m in enumerate(out) if m.get("role") == "user"), None)
    if first is not None:
        if stable > 0 and isinstance(out[first]["content"], (str, list)):
            out[first] = _mark_split(out[first], stable)
        else:
            out[first] = _mark_message(out[first])
    if out and len(out) - 1 != first:
        out[-1] = _mark_message(out[-1])
    return out


def stable_system_purpose(purpose: str) -> bool:
    return any(purpose == p or purpose.startswith(p + ".") for p in STABLE_SYSTEM_PURPOSES)


def complete_system(model: str, purpose: str, system: str) -> Any:
    """The `system` of a complete() call. A stable system prompt (STABLE_SYSTEM_PURPOSES) long
    enough for the model to cache gets a 1-hour breakpoint (no tools: it is the whole shared
    prefix); anything else is the plain string (a marker on a per-request prompt only adds the
    cache-write premium)."""
    if (not stable_system_purpose(purpose)
            or estimate_tokens(system) < CACHE_MIN_TOKENS.get(model, CACHE_MIN_DEFAULT)):
        return system
    return [{"type": "text", "text": system, "cache_control": dict(_EPHEMERAL_1H)}]


def _bounded_create(provider: str, client: Any, request: Any,
                    pick: Callable[[Any], Callable[..., Any]],
                    timeout_for: Callable[[str], float] | None,
                    http_client_factory: HttpClientFactory | None,
                    progress: Progress | None = None, **kwargs: Any) -> Any:
    """One SDK request. With timeout_for, `timeout=` (the SDK's per-phase timeout) is sent AND the
    same value bounds the request in wall-clock time (`llm.deadline.bounded_sdk_call`), however
    the server trickles keep-alive bytes: a non-streamed request as a total deadline (one call
    never exceeds the tier timeout + ABORT_GRACE_S); a streamed one (`progress`, beaten by its
    reader) as the longest wait for its first stream event and between two, so an answer that
    keeps streaming is never cut. Past it the request is aborted and raises ProviderBusyError (a
    stream that never started: queued) or ProviderTimeoutError. Without timeout_for (pipeline
    providers) nothing changes."""
    if timeout_for is None:
        return _sdk_call(provider, request, pick(client), **kwargs)
    seconds = timeout_for(request.purpose)

    def create(**kw: Any) -> Any:
        return bounded_sdk_call(client, lambda c: pick(c)(**kw), seconds, http_client_factory,
                                progress)

    return _sdk_call(provider, request, create, timeout=seconds, **kwargs)


class StreamCut(Exception):
    """A streamed reply ended without its end marker (no finish / stop reason): the connection
    was cut. Never taken as a complete answer (transient, like a dropped connection)."""


class _Gate:
    """The live text of one streamed call goes through here. Closed by the caller once the
    (deadline-bounded) request returned or raised, so a worker that is still being aborted
    never streams more. A sink that fails is logged and switched off: the live text is a
    preview, the answer itself comes from the finished response."""

    def __init__(self, sink: StreamSink):
        self._sink = sink
        self._lock = threading.Lock()
        self._open = True

    def __call__(self, chunk: str | None) -> None:
        if not chunk:
            return
        with self._lock:
            if not self._open:
                return
            try:
                self._sink.text(chunk)
            except Exception:  # see the class doc
                log.warning("live text sink failed; streaming off for this call", exc_info=True)
                self._open = False

    def close(self) -> None:
        with self._lock:
            self._open = False


def _anthropic_stream(client: Any, gate: _Gate, progress: Progress, **kw: Any) -> Any:
    """One streamed Messages request, read to the end (inside the deadline worker): every event but
    a ping beats `progress`, text deltas go to `gate`; returns the SDK's final message (the same
    shape as a non-streamed create). A cancelled run (`cancel_requested`, looked at after every
    event) leaves the `with`, which closes the response: RequestCancelled with the usage the
    stream reported so far (input and cache from message_start; output only as far as reported)."""
    with client.beta.messages.stream(**kw) as s:
        for ev in s:
            if ev.type != "ping":
                progress.beat()
            if ev.type == "content_block_delta" and getattr(ev.delta, "type", None) == "text_delta":
                gate(ev.delta.text)
            if cancel_requested():
                raise _anthropic_cancelled(s)
        final = s.get_final_message()
    if getattr(final, "stop_reason", None) is None:
        raise StreamCut("anthropic stream ended without a stop reason")
    return final


def _anthropic_cancelled(s: Any) -> RequestCancelled:
    try:
        snap = s.current_message_snapshot
    except Exception:  # noqa: BLE001 - no message_start yet: nothing reported
        return RequestCancelled()
    usage = getattr(snap, "usage", None)
    if usage is None:
        return RequestCancelled(model_version=getattr(snap, "model", None))
    return RequestCancelled(model_version=getattr(snap, "model", None),
                            input_tokens=getattr(usage, "input_tokens", None),
                            output_tokens=getattr(usage, "output_tokens", None),
                            **_cache_tokens(usage))


class _NoSink:
    """A streamed call whose text nobody watches (a JSON `complete`): only its progress counts."""

    def text(self, chunk: str) -> None:
        pass


class _DeepSeekStream:
    """A streamed chat completion folded back into what a non-streamed one carries."""

    def __init__(self) -> None:
        self.model: str | None = None
        self.content: list[str] = []
        self.reasoning: list[str] = []
        self.calls: dict[int, dict[str, Any]] = {}
        self.finish_reason: str | None = None
        self.usage: Any = None

    def add(self, chunk: Any, gate: _Gate) -> None:
        if getattr(chunk, "model", None):
            self.model = chunk.model
        if getattr(chunk, "usage", None) is not None:
            self.usage = chunk.usage
        for choice in getattr(chunk, "choices", None) or ():
            d = getattr(choice, "delta", None)
            if d is not None:
                reasoning = getattr(d, "reasoning_content", None)
                if reasoning:
                    self.reasoning.append(reasoning)
                text = getattr(d, "content", None)
                if text:
                    self.content.append(text)
                    gate(text)
                for tc in getattr(d, "tool_calls", None) or ():
                    slot = self.calls.setdefault(tc.index, {"id": None, "name": "", "args": []})
                    if getattr(tc, "id", None):
                        slot["id"] = tc.id
                    f = getattr(tc, "function", None)
                    if f is not None:
                        if getattr(f, "name", None):
                            slot["name"] += f.name
                        if getattr(f, "arguments", None):
                            slot["args"].append(f.arguments)
            if getattr(choice, "finish_reason", None):
                self.finish_reason = choice.finish_reason

    def tool_calls(self) -> list[tuple[str, str, str]]:
        return [(s["id"] or f"call_{i}", s["name"], "".join(s["args"]))
                for i, s in sorted(self.calls.items())]


def _deepseek_stream(client: Any, gate: _Gate, progress: Progress, **kw: Any) -> _DeepSeekStream:
    """Every chunk beats `progress` (reasoning and tool arguments too: work the user does not see
    streamed); keep-alive comments never reach here (the SDK drops them). A cancelled run
    (`cancel_requested`, looked at after every chunk) stops reading: the stream is closed and
    RequestCancelled raised (DeepSeek reports usage only in its last chunk, so it has none)."""
    chunks = client.chat.completions.create(**kw)
    acc = _DeepSeekStream()
    answering = False
    try:
        for chunk in chunks:
            progress.beat()
            acc.add(chunk, gate)
            if not answering and (acc.content or any(s["args"] for s in acc.calls.values())):
                answering = True
                reply_started()
            if cancel_requested():
                raise RequestCancelled(model_version=acc.model,
                                       **(deepseek_tokens(acc.usage) if acc.usage is not None
                                          else {}))
    finally:
        close = getattr(chunks, "close", None)
        if close is not None:
            close()
    if acc.finish_reason is None:
        raise StreamCut("deepseek stream ended without a finish reason")
    return acc


# Models that reject `output_config.effort` with a 400 (claude-api skill: effort "errors on
# Sonnet 4.5 / Haiku 4.5"); the app's fast tier and the harness judge for a DeepSeek writer are
# Haiku 4.5. Effort is left out for them.
NO_EFFORT_MODELS = ("claude-haiku-4-5", "claude-sonnet-4-5")


# claude-api skill, Migrating to Claude Haiku 5.5. This model has no server-side fallback ("don't add
# the `fallbacks` parameter"): a refusal stays a refusal, and fails closed here (ProviderRefusalError).
NO_FALLBACK_MODELS = ("claude-haiku-5-5",)
# It accepts thinking {"type": "disabled"} at effort low / medium / high, 400 at xhigh / max.
THINKING_DISABLE_MODELS = ("claude-haiku-5-5",)
# A thinking block is signed with the prefix that produced it (system, tools, earlier messages), and
# the assistant's system prompt changes between turns: replayed as is, 400 "bound to a different
# conversation". Chat on this model asks the API to drop such blocks (skill: "drop_block" to degrade
# instead of fail); block_binding needs adaptive thinking, so with thinking off they are stripped here.
THINKING_BINDING_BETA = "thinking-binding-controls-2026-08-01"


def _without_thinking(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The history with every thinking block taken out (the text and tool calls stay)."""
    out = []
    for m in messages:
        c = m["content"]
        out.append({**m, "content": [b for b in c if b.get("type") not in _UNMARKABLE]}
                   if m["role"] == "assistant" and isinstance(c, list) else m)
    return out


def _effort_config(model: str, effort: str) -> dict[str, str]:
    return {} if model.startswith(NO_EFFORT_MODELS) else {"effort": effort}


def _fallback_kwargs(model: str) -> dict[str, Any]:
    if model.startswith(NO_FALLBACK_MODELS):
        return {}
    return {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}


def _refusal(where: str, resp: Any, known: Mapping[str, Any]) -> "ProviderRefusalError":
    """`stop_reason: refusal`, with the classifier's category (cyber / bio / frontier_llm /
    general_harms) when the response gives one."""
    category = getattr(getattr(resp, "stop_details", None), "category", None)
    return ProviderRefusalError(f"anthropic refused {where}" + (f" ({category})" if category else ""),
                                status="refusal", **known)


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, client: Any = None, effort: str = "medium",
                 timeout_for: Callable[[str], float] | None = None,
                 http_client_factory: HttpClientFactory | None = None,
                 clock: Callable[[], float] = time.monotonic):
        """timeout_for: per-purpose seconds, sent as the SDK timeout and enforced per request as a
        total deadline, or for a streamed request as the longest wait without a stream event
        (`_bounded_create`); http_client_factory lets it abort the request (`llm.deadline`).
        `clock`: what that wait is measured on."""
        self._timeout_for = timeout_for
        self._clock = clock
        self._http_client_factory = http_client_factory
        if client is None:
            import anthropic  # optional extra `llm`

            # credentials from ANTHROPIC_API_KEY; no hidden SDK retries (max_retries=0): a retry
            # belongs to the gateway, which audits every attempt and knows what was streamed
            client = anthropic.Anthropic(timeout=CLIENT_TIMEOUT_S, max_retries=0)
        self._client = client
        self._effort = effort

    def _effort_for_request(self, model: str, request: Any) -> str:
        """Thinking off (request.params["thinking"] is False): effort "low", except on a model that
        can really turn it off (`_thinking_kwargs`), which keeps the configured effort up to the
        "high" it allows. claude-sonnet-5-5 rejects thinking {"type": "disabled"} with a 400 and the
        skill names {"type": "between_tools"} only for that one model (effort high or below), while
        this provider serves other models too; the skill's own advice is to try low effort
        first. Absent or True: the configured effort, unchanged."""
        selected = request.params.get("reasoning_effort")
        effort = selected if selected not in (None, "default", "none") else self._effort
        if request.params.get(THINKING_PARAM) is not False:
            return effort
        if not model.startswith(THINKING_DISABLE_MODELS):
            return "low"
        return "high" if effort in ("xhigh", "max") else effort

    @staticmethod
    def _thinking_kwargs(model: str, request: Any) -> dict[str, Any]:
        if request.params.get(THINKING_PARAM) is False and model.startswith(THINKING_DISABLE_MODELS):
            return {"thinking": {"type": "disabled"}}
        return {}

    def _create(self, request: Any, pick: Callable[[Any], Callable[..., Any]],
                progress: Progress | None = None, **kwargs: Any) -> Any:
        return _bounded_create(self.name, self._client, request, pick, self._timeout_for,
                               self._http_client_factory, progress, **kwargs)

    def complete(
        self, model: str, request: LLMRequest, json_schema: dict[str, Any]
    ) -> ProviderResponse:
        # Prompt caching: only a stable system prompt is marked (`complete_system`, 1-hour TTL);
        # the prompt itself is per-request, so a marker there would only add the write premium.
        resp = self._create(
            request, lambda c: c.beta.messages.create,
            model=model,
            max_tokens=max_output(16000),
            system=complete_system(model, request.purpose, request.system),
            messages=[{"role": "user", "content": request.prompt}],
            output_config={
                **_effort_config(model, self._effort_for_request(model, request)),
                "format": {"type": "json_schema", "schema": strict_schema(json_schema)},
            },
            **self._thinking_kwargs(model, request),
            **_fallback_kwargs(model),
        )
        cache = _cache_tokens(resp.usage)
        cost = anthropic_cost(resp.model, resp.usage.input_tokens, resp.usage.output_tokens,
                              **cache)
        tokens = {"input_tokens": resp.usage.input_tokens,
                  "output_tokens": resp.usage.output_tokens, **cache}
        text = next((b.text for b in resp.content if b.type == "text"), None)
        known = {"raw_output": text, "model_version": resp.model, "cost": cost, **tokens}
        if resp.stop_reason == "refusal":
            raise _refusal(f"{request.purpose}/{request.subject}", resp, known)
        if resp.stop_reason == "max_tokens":
            raise ProviderEmptyOutputError(f"anthropic output truncated at max_tokens for "
                                           f"{request.purpose}/{request.subject}",
                                           status="truncated", **known)
        if text is None:
            raise ProviderEmptyOutputError(f"anthropic returned no text for "
                                           f"{request.purpose}/{request.subject}",
                                           status="empty_output", **known)
        return ProviderResponse(raw_output=text, model_version=resp.model, cost=cost, **tokens)

    streams = True

    def chat(self, model: str, request: ChatRequest,
             stream: StreamSink | None = None) -> ChatResponse:
        """`stream`: the answer text is streamed to it while the reply is generated (the stream is
        bounded by progress, not in total: `_bounded_create`); the response is the same."""
        # Prompt caching: at most 4 cache_control breakpoints, added here only (the ChatRequest
        # and its hash are unchanged, so recordings still replay). 1-hour TTL on the prefixes
        # shared across requests, which come first: the system block (it caches tools + system;
        # the last tool instead when there is no system) and the first user message's stable
        # prefix (`ChatMessage.stable_prefix`); 5-minute TTL on the moving tail after them: the
        # end of the first user message and the last message. `request.cache` False: no
        # breakpoint at all (a one-off request, nothing would read the cache it writes).
        tool_kwargs: dict[str, Any] = {}
        has_system = bool(request.system.strip())
        if request.tools:
            tools = [{"name": t.name, "description": t.description,
                      "input_schema": t.parameters} for t in request.tools]
            if not has_system and request.cache:
                tools[-1] = {**tools[-1], "cache_control": dict(_EPHEMERAL_1H)}
            tool_kwargs["tools"] = tools
            tool_kwargs["tool_choice"] = {"type": request.tool_choice}
        system: Any = ([{"type": "text", "text": request.system,
                         "cache_control": dict(_EPHEMERAL_1H)}]
                       if has_system and request.cache else request.system)
        first_user = next((m for m in request.messages if m.role == "user"), None)
        messages = anthropic_messages(request)
        binding: dict[str, Any] = {}
        if model.startswith(DROPS_STALE_THINKING):
            if request.params.get(THINKING_PARAM) is False:
                messages = _without_thinking(messages)
            else:
                binding = {"thinking": {"type": "adaptive", "block_binding": {
                    "prefix_mismatch_behavior": "drop_block"}},
                           "betas": [THINKING_BINDING_BETA]}
        if request.cache:
            messages = cache_marked_messages(
                messages, stable=first_user.stable_prefix if first_user is not None else 0)
        kwargs: dict[str, Any] = dict(
            model=model,
            max_tokens=max_output(16000),
            system=system,
            messages=messages,
            output_config=_effort_config(model, self._effort_for_request(model, request)),
            **self._thinking_kwargs(model, request),
            **_fallback_kwargs(model),
            **binding,
            **tool_kwargs,
        )
        if stream is None:
            resp = self._create(request, lambda c: c.beta.messages.create, **kwargs)
        else:
            gate, progress = _Gate(stream), Progress(self._clock)
            try:
                resp = self._create(request,
                                    lambda c: partial(_anthropic_stream, c, gate, progress),
                                    progress, **kwargs)
            finally:
                gate.close()
        cost = _anthropic_cost(resp)
        dropped = sum(getattr(t, "type", None) == "thinking_dropped"
                      for t in getattr(resp, "input_transformations", None) or ())
        if dropped:
            log.info("%s/%s: the API dropped %d thinking blocks of the history", request.purpose,
                     request.subject, dropped)
        text = "".join(b.text for b in resp.content if b.type == "text")
        usage = getattr(resp, "usage", None)
        tokens = {"input_tokens": getattr(usage, "input_tokens", None),
                  "output_tokens": getattr(usage, "output_tokens", None),
                  **_cache_tokens(usage)}
        known = {"raw_output": text or None, "model_version": resp.model, "cost": cost,
                 **tokens}  # paid failures carry their tokens so they are billed
        where = f"{request.purpose}/{request.subject}"
        if resp.stop_reason == "refusal":
            raise _refusal(where, resp, known)
        if resp.stop_reason == "max_tokens":
            raise ProviderEmptyOutputError(f"anthropic output truncated at max_tokens for {where}",
                                           status="truncated", **known)
        calls = []
        for b in resp.content:
            if b.type != "tool_use":
                continue
            if not isinstance(b.input, dict):
                raise ProviderOutputError(f"anthropic tool_use input is not an object for {where}",
                                          status="parse_error", **known)
            calls.append(ToolCall(id=b.id, name=b.name, args=b.input))
        turn = ChatTurn(text=text, tool_calls=tuple(calls),
                        echo=[_block_dict(b) for b in resp.content],
                        input_tokens=tokens["input_tokens"] or 0,
                        output_tokens=tokens["output_tokens"] or 0,
                        cache_read_tokens=tokens["cache_read_tokens"],
                        cache_write_tokens=tokens["cache_write_tokens"],
                        stop_reason=resp.stop_reason)
        return ChatResponse(turn=turn, model_version=resp.model, cost=cost, **tokens)


def _deepseek_user_text(m: ChatMessage) -> str:
    if m.origin == "memory":
        return memory_fenced(m.text)
    if not m.parts:
        return m.text
    if any(p.kind == "image" for p in m.parts):
        raise ImagesNotSupported("deepseek 不能看图片")
    return "\n\n".join([*(_part_label(p) for p in m.parts), *([m.text] if m.text else [])])


def deepseek_messages(request: ChatRequest) -> list[dict[str, Any]]:
    """OpenAI-compatible history for DeepSeek. An assistant `echo` of
    {"reasoning_content": ...} is merged back into its message within the same turn. It is kept
    whatever this request's thinking switch (a session that moves from 思考 to 快速 sends its old
    thinking turns' echoes with thinking disabled): DeepSeek requires it back on a tool-calling
    thinking history (400 when it is missing) and otherwise ignores it, never rejects it
    (https://api-docs.deepseek.com/guides/thinking_mode). A recalled-memory message
    (`origin="memory"`) is fenced and joined, first, into the user message after it."""
    out: list[dict[str, Any]] = [{"role": "system", "content": request.system}]
    recalled: list[str] = []         # memory messages, joined fence-first into the next user one
    for m in request.messages:
        if m.role == "user" and m.origin == "memory":
            recalled.append(_deepseek_user_text(m))
            continue
        if recalled and m.role != "user":
            out.append({"role": "user", "content": "\n\n".join(recalled)})
            recalled = []
        if m.role == "user":
            text = _deepseek_user_text(m)
            if recalled:     # one user message: reasoner models refuse two in a row
                text, recalled = "\n\n".join([*recalled, *([text] if text else [])]), []
            out.append({"role": "user", "content": text})
        elif m.role == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": m.text or None}
            if m.tool_calls:
                msg["tool_calls"] = [
                    {"id": c.id, "type": "function",
                     "function": {"name": c.name,
                                  "arguments": json.dumps(c.args, ensure_ascii=False)}}
                    for c in m.tool_calls]
            if isinstance(m.echo, dict) and "reasoning_content" in m.echo:
                msg["reasoning_content"] = m.echo["reasoning_content"]
            out.append(msg)
        else:
            out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.text})
    if recalled:
        out.append({"role": "user", "content": "\n\n".join(recalled)})
    return out


def deepseek_max_tokens(env: Mapping[str, str] = os.environ) -> int:
    """max_tokens for DeepSeek calls; fails closed on anything but a positive int."""
    raw = env.get(DEEPSEEK_MAX_TOKENS_ENV)
    if raw is None or raw.strip() == "":
        return DEEPSEEK_DEFAULT_MAX_TOKENS
    try:
        value = int(raw.strip())
    except ValueError:
        raise ValueError(f"{DEEPSEEK_MAX_TOKENS_ENV} must be a positive integer, "
                         f"got {raw!r}") from None
    if value <= 0:
        raise ValueError(f"{DEEPSEEK_MAX_TOKENS_ENV} must be a positive integer, got {raw!r}")
    return value


_ON = frozenset({"1", "true", "on", "yes"})
_OFF = frozenset({"0", "false", "off", "no"})


def deepseek_strict(env: Mapping[str, str] = os.environ) -> bool:
    """EVOWIKI_DEEPSEEK_STRICT: unset / empty = off; fails closed on a value it cannot read."""
    raw = (env.get(DEEPSEEK_STRICT_ENV) or "").strip().lower()
    if raw in _ON:
        return True
    if raw == "" or raw in _OFF:
        return False
    raise ValueError(f"{DEEPSEEK_STRICT_ENV} must be 1/true/on or 0/false/off, got {raw!r}")


def deepseek_tokens(usage: Any) -> dict[str, int | None]:
    """Token counts from a DeepSeek (OpenAI-compatible) usage object. DeepSeek's context cache is on
    by default (https://api-docs.deepseek.com/guides/kv_cache): when the response reports both
    `prompt_cache_hit_tokens` and `prompt_cache_miss_tokens`, input_tokens is the miss count (the
    uncached remainder, as for Anthropic) and cache_read_tokens the hit count; DeepSeek has no
    cache write, so cache_write_tokens is 0. When either field is absent or None the old reading
    holds: input_tokens = prompt_tokens, no cache tokens. Unreported prompt / completion counts
    stay None (billed as cost None, never guessed)."""
    hit = getattr(usage, "prompt_cache_hit_tokens", None)
    miss = getattr(usage, "prompt_cache_miss_tokens", None)
    out = getattr(usage, "completion_tokens", None)
    if isinstance(hit, int) and isinstance(miss, int):
        return {"input_tokens": miss, "output_tokens": out, "cache_read_tokens": hit,
                "cache_write_tokens": 0}
    return {"input_tokens": getattr(usage, "prompt_tokens", None), "output_tokens": out,
            "cache_read_tokens": 0, "cache_write_tokens": 0}


class DeepSeekProvider:
    name = "deepseek"

    def __init__(self, client: Any = None, env: Mapping[str, str] | None = None,
                 max_tokens: int | None = None, thinking: bool | None = None,
                 timeout_for: Callable[[str], float] | None = None,
                 http_client_factory: HttpClientFactory | None = None,
                 effort_for: Callable[[str], str | None] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 strict: bool | None = None, strict_client: Any = None,
                 chat_thinking: bool | None = None):
        """An explicit max_tokens (positive int) overrides EVOWIKI_DEEPSEEK_MAX_TOKENS.

        strict: True sends every `complete` call as strict function calling (`_complete_strict`);
        None reads EVOWIKI_DEEPSEEK_STRICT from `env` (default off). Strict requests go to
        DEEPSEEK_BETA_BASE_URL: `strict_client`, else `client.with_options(base_url=...)`.
        `chat` is unchanged either way.

        thinking: True / False sends DeepSeek's thinking-mode switch
        (extra_body={"thinking": {"type": "enabled" | "disabled"}}, per
        https://api-docs.deepseek.com/guides/thinking_mode); None sends nothing (the API's
        default, thinking on).

        effort_for: purpose -> DeepSeek's `reasoning_effort` ("low" / "high" / "max", same page;
        the API's default is "high") for a `complete` call with thinking not off; None sends
        nothing. `chat` sends neither switch, unless `chat_thinking` is set: then a chat request
        without a THINKING_PARAM sends that thinking switch (the page writer: False, 2026-10-28
        「不要开思考，写页面全部开最低就够了吧」; the assistant's providers leave it None)."""
        self._timeout_for = timeout_for   # as for AnthropicProvider (and `clock`)
        self._clock = clock
        self._effort_for = effort_for
        self._http_client_factory = http_client_factory
        if thinking is not None and not isinstance(thinking, bool):
            raise ValueError(f"thinking must be True, False or None, got {thinking!r}")
        self._thinking = thinking
        if chat_thinking is not None and not isinstance(chat_thinking, bool):
            raise ValueError(f"chat_thinking must be True, False or None, got {chat_thinking!r}")
        self._chat_thinking = chat_thinking
        env = os.environ if env is None else env
        if max_tokens is None:
            self._max_tokens = deepseek_max_tokens(env)
        elif isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
            raise ValueError(f"max_tokens must be a positive integer, got {max_tokens!r}")
        else:
            self._max_tokens = max_tokens
        if client is None:
            from openai import OpenAI  # optional extra `llm`; DeepSeek is OpenAI-compatible

            client = OpenAI(api_key=env["DEEPSEEK_API_KEY"], base_url=DEEPSEEK_BASE_URL,
                            timeout=CLIENT_TIMEOUT_S, max_retries=0)  # retries: the gateway's
        self._client = client
        if strict is None:
            strict = deepseek_strict(env)
        elif not isinstance(strict, bool):
            raise ValueError(f"strict must be True, False or None, got {strict!r}")
        self._strict = strict
        if strict and strict_client is None:
            if not hasattr(client, "with_options"):
                raise ValueError("DeepSeek strict mode needs an OpenAI client (with_options) "
                                 "or an explicit strict_client")
            strict_client = client.with_options(base_url=DEEPSEEK_BETA_BASE_URL)
        self._strict_client = strict_client if strict else None

    def _create(self, request: Any, pick: Callable[[Any], Callable[..., Any]],
                progress: Progress | None = None, client: Any = None, **kwargs: Any) -> Any:
        return _bounded_create(self.name, self._client if client is None else client, request,
                               pick, self._timeout_for, self._http_client_factory, progress,
                               **kwargs)

    def complete(
        self, model: str, request: LLMRequest, json_schema: dict[str, Any]
    ) -> ProviderResponse:
        """One structured call. Default: json_object with the schema pasted into the prompt.
        Strict (`strict` / EVOWIKI_DEEPSEEK_STRICT): `_strict_kwargs`; the reply is the one
        `respond` tool call's arguments, returned as raw_output for the gateway's `_parse`, so
        validation, record / replay and repair loops are unchanged."""
        want = request.params.get(THINKING_PARAM)
        thinking = want if isinstance(want, bool) else self._thinking
        body: dict[str, Any] = ({} if thinking is None else
                                {"thinking": {"type": "enabled" if thinking
                                              else "disabled"}})
        effort = (self._effort_for(request.purpose)
                  if self._effort_for is not None and thinking is not False else None)
        if thinking is not False and request.params.get("reasoning_effort") not in (None, "none", "default"):
            effort = request.params["reasoning_effort"]
        if effort is not None:
            body["reasoning_effort"] = effort
        switch = {"extra_body": body} if body else {}
        if self._strict:
            shape = self._strict_kwargs(request, json_schema, thinking)
        else:
            prompt = (f"{request.prompt}\n\nRespond with one JSON object matching this JSON "
                      f"Schema:\n{json.dumps(json_schema, ensure_ascii=False)}")
            shape = {"messages": [{"role": "system", "content": request.system},
                                  {"role": "user", "content": prompt}],
                     "response_format": {"type": "json_object"}}
        limit = max_output(self._max_tokens)       # the job budget's cap when lower
        kwargs: dict[str, Any] = dict(model=model, max_tokens=limit, **shape, **switch)
        if TEMPERATURE_PARAM in request.params and thinking is False:
            # DeepSeek's default is 1.0, and it ignores the parameter while thinking
            kwargs["temperature"] = request.params[TEMPERATURE_PARAM]
        client = self._strict_client if self._strict else None
        try:
            if thinking is True and self._timeout_for is not None:
                # A thinking call can reason for minutes over a long prompt (a
                # goal's blocks over a 27k-character PDF): it streams, so every reasoning chunk
                # shows progress and the deadline is the longest wait without one
                # (`_bounded_create`), never a total that a long, steady call would hit.
                gate, progress = _Gate(_NoSink()), Progress(self._clock)
                try:
                    acc = self._create(request,
                                       lambda c: partial(_deepseek_stream, c, gate, progress),
                                       progress, client=client, stream=True,
                                       stream_options={"include_usage": True}, **kwargs)
                finally:
                    gate.close()
                model_version, finish = acc.model, acc.finish_reason
                tokens = deepseek_tokens(acc.usage)
                raw = "".join(acc.content) or None
                calls = acc.tool_calls()
            else:
                resp = self._create(request, lambda c: c.chat.completions.create, client=client,
                                    **kwargs)
                choice = _first_choice(resp, f"{request.purpose}/{request.subject}")
                model_version, finish = resp.model, getattr(choice, "finish_reason", None)
                # OpenAI-compatible usage plus DeepSeek's cache hit / miss split
                tokens = deepseek_tokens(getattr(resp, "usage", None))
                raw = choice.message.content
                calls = [(c.id, c.function.name, c.function.arguments)
                         for c in getattr(choice.message, "tool_calls", None) or ()]
        except Exception as exc:
            if (self._strict and _status_code(exc) == 400
                    and not isinstance(exc, ProviderRejectedError)):
                # Fail closed, no fallback to json_object: a schema (or request) strict mode
                # refuses is refused again on every retry.
                detail = _short_message(exc)
                raise ProviderRejectedError(
                    f"deepseek strict mode rejected the request for "
                    f"{request.purpose}/{request.subject}: HTTP 400"
                    + (f" {detail}" if detail else ""), status_code=400) from exc
            raise
        where = f"{request.purpose}/{request.subject}"
        if finish == "length":
            raise ProviderEmptyOutputError(f"deepseek output truncated at max_tokens="
                                           f"{limit} for {where}",
                                           raw_output=calls[0][2] if self._strict and calls
                                           else raw, model_version=model_version,
                                           status="truncated", **tokens)
        if self._strict:
            raw = self._strict_answer(where, raw, calls, model_version, tokens)
        content = raw or ""
        if not content.strip():
            raise ProviderEmptyOutputError(f"deepseek returned empty content for {where}",
                                           raw_output=raw, model_version=model_version,
                                           status="empty_output", **tokens)
        return ProviderResponse(raw_output=content, model_version=model_version, cost=None,
                                **tokens)

    def _strict_kwargs(self, request: LLMRequest, json_schema: dict[str, Any],
                       thinking: bool | None) -> dict[str, Any]:
        """Messages, the one strict tool and tool_choice. The tool is forced by name only with
        thinking off: DeepSeek answers 400 to a `required` or named tool_choice in thinking mode
        (also the API's default when no switch is sent), so there the prompt asks for the call
        and tool_choice is left out (the API's default with tools, "auto"). A schema strict mode
        cannot express is a ProviderRejectedError before any request (nothing paid)."""
        try:
            parameters = deepseek_strict_schema(json_schema)
        except StrictSchemaError as exc:
            raise ProviderRejectedError(
                f"deepseek strict mode cannot express the schema for "
                f"{request.purpose}/{request.subject}: {exc}") from exc
        prompt = (f"{request.prompt}\n\nGive your answer by calling the `{DEEPSEEK_STRICT_TOOL}` "
                  "tool exactly once: its arguments are the whole answer.")
        tool = {"type": "function",
                "function": {"name": DEEPSEEK_STRICT_TOOL, "strict": True,
                             "description": "Return the answer as this tool's arguments.",
                             "parameters": parameters}}
        out: dict[str, Any] = {"messages": [{"role": "system", "content": request.system},
                                            {"role": "user", "content": prompt}],
                               "tools": [tool]}
        if thinking is False:
            out["tool_choice"] = {"type": "function", "function": {"name": DEEPSEEK_STRICT_TOOL}}
        return out

    @staticmethod
    def _strict_answer(where: str, content: str | None, calls: list[tuple[str, str, Any]],
                       model_version: str | None, tokens: dict[str, int | None]) -> str | None:
        """The arguments of the one `respond` call. Anything else (no call, several, another
        name) is a parse_error attempt carrying what came back; the text content is never read
        as the answer."""
        if len(calls) == 1 and calls[0][1] == DEEPSEEK_STRICT_TOOL:
            args = calls[0][2]
            return args if isinstance(args, str) else None
        got = [c[1] for c in calls] or "no tool call"
        raise ProviderOutputError(
            f"deepseek strict call for {where} did not answer with one "
            f"`{DEEPSEEK_STRICT_TOOL}` tool call (got {got})",
            raw_output=content if not calls else json.dumps(
                [{"name": c[1], "arguments": c[2]} for c in calls], ensure_ascii=False),
            model_version=model_version, status="parse_error", **tokens)

    streams = True

    def chat(self, model: str, request: ChatRequest,
             stream: StreamSink | None = None) -> ChatResponse:
        """`stream`: the answer text (`content`, never `reasoning_content` or tool arguments) is
        streamed to it; the request runs with `stream=True` and usage in the last chunk, bounded
        by progress (`_bounded_create`), and folds into the same response."""
        tool_kwargs: dict[str, Any] = {}
        if request.tools:
            tool_kwargs["tools"] = [{"type": "function",
                                     "function": {"name": t.name, "description": t.description,
                                                  "parameters": t.parameters}}
                                    for t in request.tools]
            tool_kwargs["tool_choice"] = request.tool_choice
        limit = max_output(self._max_tokens)       # the job budget's cap when lower
        kwargs: dict[str, Any] = dict(model=model, messages=deepseek_messages(request),
                                      max_tokens=limit, **tool_kwargs)
        want = request.params.get(THINKING_PARAM)
        if not isinstance(want, bool):  # H10 sets it; absent = the provider's chat switch
            want = self._chat_thinking
        if isinstance(want, bool):
            body: dict[str, Any] = {"thinking": {"type": "enabled" if want else "disabled"}}
            effort = (self._effort_for(request.purpose)
                      if self._effort_for is not None and want else None)
            if want and request.params.get("reasoning_effort") not in (None, "none", "default"):
                effort = request.params["reasoning_effort"]
            if effort is not None:
                body["reasoning_effort"] = effort
            kwargs["extra_body"] = body
        if stream is None:
            resp = self._create(request, lambda c: c.chat.completions.create, **kwargs)
            choice = _first_choice(resp, f"{request.purpose}/{request.subject}")
            message = choice.message
            return self._chat_response(
                request, resp.model, message.content, getattr(message, "reasoning_content", None),
                [(c.id, c.function.name, c.function.arguments)
                 for c in getattr(message, "tool_calls", None) or ()],
                getattr(choice, "finish_reason", None), getattr(resp, "usage", None), limit)
        gate, progress = _Gate(stream), Progress(self._clock)
        try:
            acc = self._create(request, lambda c: partial(_deepseek_stream, c, gate, progress),
                               progress, stream=True, stream_options={"include_usage": True},
                               **kwargs)
        finally:
            gate.close()
        return self._chat_response(
            request, acc.model, "".join(acc.content) or None, "".join(acc.reasoning) or None,
            acc.tool_calls(), acc.finish_reason, acc.usage, limit)

    def _chat_response(self, request: ChatRequest, model_version: str | None,
                       content: str | None, reasoning: str | None,
                       tool_calls: Iterable[tuple[str, str, Any]], finish_reason: str | None,
                       usage: Any, limit: int | None = None) -> ChatResponse:
        where = f"{request.purpose}/{request.subject}"
        # paid failures carry their tokens too, cache hits included (deepseek_tokens)
        tokens = deepseek_tokens(usage)
        if finish_reason == "length":
            raise ProviderEmptyOutputError(f"deepseek output truncated at max_tokens="
                                           f"{limit or self._max_tokens} for {where}",
                                           raw_output=content, model_version=model_version,
                                           status="truncated", **tokens)
        calls = []
        for call_id, name, raw_args in tool_calls:
            args = deepseek_tool_args(raw_args, where=f"{where}/{name}")
            if not isinstance(args, dict):     # e.g. {"page_id": current}: the agent loop tells the model
                log.info("deepseek tool call %s arguments are not a JSON object for %s", name, where)
                calls.append(ToolCall(id=call_id, name=name, unparsed=str(raw_args)))
                continue
            calls.append(ToolCall(id=call_id, name=name, args=args))
        turn = ChatTurn(text=content or "", tool_calls=tuple(calls),
                        echo={"reasoning_content": reasoning} if reasoning else None,
                        input_tokens=tokens["input_tokens"] or 0,
                        output_tokens=tokens["output_tokens"] or 0,
                        cache_read_tokens=tokens["cache_read_tokens"] or 0,
                        cache_write_tokens=0, stop_reason=finish_reason)
        return ChatResponse(turn=turn, model_version=model_version, cost=None, **tokens)


STRAY_QUOTE_FIXES = 3      # stray quotes a tool call's arguments may lose (deepseek_tool_args)


def _missing_closers(text: str) -> str:
    """The `}` / `]` that `text` still owes, innermost first ("" inside a string)."""
    owed: list[str] = []
    in_string = escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            owed.append("}" if ch == "{" else "]")
        elif ch in "}]" and owed:
            owed.pop()
    return "" if in_string else "".join(reversed(owed))


def deepseek_tool_args(raw: Any, *, where: str = "") -> Any:
    """A DeepSeek tool call's arguments as JSON (None: not JSON). DeepSeek (v4-pro, thinking off)
    sometimes closes a list or object and then writes a stray `"` before the next comma: S3 live
    run 2026-10-28, both failed plan_page calls had `…（quick）"]", "practice": …`. The adapter
    drops such a quote, only where the parser stops at a `"` right after a `]` / `}` (a valid
    string can never put one there), at most STRAY_QUOTE_FIXES times. It also ends a finished call
    with a closer missing (full walk 2026-11-04, twice in a row on one page: `{"plan": {…]}`):
    the closers the text still owes are added when the parser ran out of text outside a string
    (a call cut at max_tokens never gets here). Anything else stays as it is and fails as before."""
    if not raw:
        return {}
    if not isinstance(raw, str):
        return None
    text = raw
    for _ in range(STRAY_QUOTE_FIXES + 1):
        try:
            value = json.loads(text)
        except json.JSONDecodeError as e:
            i = e.pos
            if i >= len(text):
                closers = _missing_closers(text)
                if not closers:
                    return None
                text += closers
                continue
            if not (i > 0 and text[i] == '"' and text[i - 1] in "]}"):
                return None
            text = text[:i] + text[i + 1:]
            continue
        if text is not raw:
            log.info("deepseek tool call arguments for %s: repaired (%d -> %d characters)", where,
                     len(raw), len(text))
        return value
    return None


def providers_from_env(env: Mapping[str, str] = os.environ) -> dict[str, Provider]:
    factories = {"anthropic": AnthropicProvider, "deepseek": DeepSeekProvider}
    return {name: factories[name]() for name, var in ENV_KEYS.items() if env.get(var)}


def missing_env_vars(config: GatewayConfig, env: Mapping[str, str] = os.environ) -> list[str]:
    out = []
    for ref in (config.generator, config.verifier):
        var = ENV_KEYS.get(ref.provider)
        if var and not env.get(var) and var not in out:
            out.append(var)
    return out
