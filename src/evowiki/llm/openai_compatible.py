"""Chat Completions adapters with explicit vendor request profiles.

No network discovery, SDK retries, or silent protocol fallback. Gemini and Kimi
use their documented OpenAI-compatible endpoints, not DeepSeek's extensions.
"""

import copy
import json
import os
import time
from collections.abc import Callable, Mapping
from functools import partial
from typing import Any

from evowiki.llm.budget import max_output
from evowiki.llm.chat import ChatRequest, ChatResponse, ChatTurn, StreamSink, ToolCall
from evowiki.llm.deadline import (
    HttpClientFactory,
    Progress,
    RequestCancelled,
    cancel_requested,
    reply_started,
)
from evowiki.llm.models import LLMRequest, ProviderResponse
from evowiki.llm.providers import (
    CLIENT_TIMEOUT_S,
    ProviderEmptyOutputError,
    ProviderRefusalError,
    ProviderTransientError,
    StreamCut,
    _bounded_create,
    _DeepSeekStream,
    _Gate,
    _part_label,
    deepseek_messages,
)

BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "kimi": "https://api.moonshot.ai/v1",
    "glm": "https://api.z.ai/api/paas/v4/",
}
KEY_NAMES = {"openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY",
             "kimi": "MOONSHOT_API_KEY", "glm": "ZAI_API_KEY",
             "openai_compatible": "EVOWIKI_COMPATIBLE_API_KEY"}


def compatible_tokens(usage: Any) -> dict[str, int | None]:
    total = getattr(usage, "prompt_tokens", None)
    details = getattr(usage, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", None)
    if cached is None:
        cached = getattr(usage, "cached_tokens", 0) or 0
    written = getattr(details, "cache_write_tokens", 0) or 0
    return {"input_tokens": max(0, total - cached - written) if total is not None else None,
            "output_tokens": getattr(usage, "completion_tokens", None),
            "cache_read_tokens": cached, "cache_write_tokens": written}


def _strict_schema(schema: dict[str, Any]) -> dict[str, Any] | None:
    """Return a strict schema only when closing its objects cannot discard fields."""
    if schema.get("type") != "object" or any(k in schema for k in ("anyOf", "oneOf", "allOf")):
        return None
    node = copy.deepcopy(schema)

    def visit(value: Any) -> bool:
        if not isinstance(value, dict) or not value:
            return False
        if "patternProperties" in value or "unevaluatedProperties" in value:
            return False
        types = value.get("type")
        if types == "object" or (isinstance(types, list) and "object" in types) or "properties" in value:
            properties = value.get("properties", {})
            if (value.get("additionalProperties", False) is not False
                    or (not properties and value.get("additionalProperties") is not False)
                    or set(value.get("required", [])) != set(properties)):
                return False
            value["additionalProperties"] = False
        value.pop("default", None)
        for key in ("properties", "$defs", "definitions"):
            if any(not visit(child) for child in value.get(key, {}).values()):
                return False
        if "items" in value and not visit(value["items"]):
            return False
        for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
            if any(not visit(child) for child in value.get(key, [])):
                return False
        return True

    return node if visit(node) else None


def compatible_messages(request: ChatRequest, *, reasoning: bool) -> list[dict[str, Any]]:
    # Reuse the established memory fencing and tool serialization; handle image
    # attachments ourselves because DeepSeek's text serializer rejects images.
    clean = request.model_copy(update={"messages": tuple(
        m.model_copy(update={"parts": (), "text": ""})
        if any(p.kind == "image" for p in m.parts) else m for m in request.messages)})
    messages = deepseek_messages(clean)
    users = iter(m for m in messages if m["role"] == "user")
    recalled = False
    # Memory entries are folded into the next user entry by deepseek_messages.
    for original in request.messages:
        if original.role == "user" and original.origin == "memory":
            recalled = True
            continue
        if original.role != "user":
            if recalled:
                next(users)
                recalled = False
            continue
        target = next(users)
        recalled = False
        images = [p for p in original.parts if p.kind == "image"]
        if images:
            blocks = ([{"type": "text", "text": target["content"]}]
                      if target["content"] else [])
            for part in original.parts:
                if part.kind == "image":
                    blocks.append({"type": "image_url", "image_url": {
                        "url": f"data:{part.media_type};base64,{part.data_b64}"}})
                else:
                    blocks.append({"type": "text", "text": _part_label(part)})
            if original.text:
                blocks.append({"type": "text", "text": original.text})
            target["content"] = blocks
    assistants = iter(m for m in messages if m["role"] == "assistant")
    for original in request.messages:
        if original.role != "assistant":
            continue
        target = next(assistants)
        if not reasoning:
            target.pop("reasoning_content", None)
        if isinstance(original.echo, dict):
            extras = original.echo.get("tool_extra_content", {})
            for call in target.get("tool_calls", []):
                if call["id"] in extras:
                    call["extra_content"] = copy.deepcopy(extras[call["id"]])
    return messages


class OpenAICompatibleProvider:
    streams = True

    def __init__(self, name: str = "openai", client: Any = None,
                 env: Mapping[str, str] | None = None, base_url: str | None = None,
                 api_key: str | None = None, max_tokens: int = 32768,
                 timeout_for: Callable[[str], float] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 http_client_factory: HttpClientFactory | None = None):
        if name not in KEY_NAMES:
            raise ValueError("Unknown OpenAI-compatible provider")
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
            raise ValueError("max_tokens must be a positive integer")
        self.name, self._max_tokens = name, max_tokens
        self._timeout_for = timeout_for or (lambda purpose: CLIENT_TIMEOUT_S)
        self._clock, self._http_client_factory = clock, http_client_factory
        if client is None:
            from openai import OpenAI

            env = os.environ if env is None else env
            url_env = {"kimi": "MOONSHOT_BASE_URL", "glm": "ZAI_BASE_URL",
                       "openai_compatible": "EVOWIKI_COMPATIBLE_BASE_URL"}.get(name)
            url = base_url or (env.get(url_env) if url_env else None) or BASE_URLS.get(name)
            if not url:
                raise ValueError("Custom OpenAI-compatible provider needs a base URL")
            client = OpenAI(api_key=api_key or env[KEY_NAMES[name]], base_url=url,
                            timeout=CLIENT_TIMEOUT_S, max_retries=0)
        self._client = client

    def _kwargs(self, model: str, request: Any) -> dict[str, Any]:
        k3 = self.name == "kimi" and model.startswith("kimi-k3")
        limit_key = "max_completion_tokens" if self.name == "openai" or k3 else "max_tokens"
        out: dict[str, Any] = {"model": model, limit_key: max_output(self._max_tokens)}
        effort = request.params.get("reasoning_effort")
        thinking = request.params.get("thinking")
        if self.name == "glm":
            native = model.startswith(("glm-5.2", "glm-5.3"))
            forced = model.startswith("glm-5.3")
            if forced and thinking is False:
                raise ValueError("GLM-5.3 always thinks; choose low/high/max reasoning effort")
            if native:
                allowed = ("low", "high", "max") if forced else ("none", "high", "max")
                if effort is not None and effort not in allowed:
                    raise ValueError(f"{model} reasoning effort must be one of {allowed}")
                if thinking is False and effort not in (None, "none"):
                    raise ValueError("thinking off conflicts with reasoning_effort")
                if effort == "none":
                    if thinking is True:
                        raise ValueError("thinking on conflicts with reasoning_effort none")
                    thinking = False
                elif effort is not None:
                    thinking = True
                    out["reasoning_effort"] = effort
            elif effort is not None:
                if effort not in ("none", "high"):
                    raise ValueError("This GLM model supports thinking off/on only")
                selected = effort == "high"
                if isinstance(thinking, bool) and thinking != selected:
                    raise ValueError("thinking conflicts with reasoning_effort")
                thinking = selected
            if isinstance(thinking, bool):
                out["extra_body"] = {"thinking": {"type": "enabled" if thinking else "disabled"}}
        elif k3:
            if thinking is False:
                raise ValueError("Kimi K3 always thinks; choose a supported reasoning effort")
            if effort is not None:
                if effort not in ("low", "high", "max"):
                    raise ValueError("Kimi K3 supports low/high/max reasoning effort")
                out["reasoning_effort"] = effort
        elif self.name == "kimi":
            if effort is not None:
                if effort not in ("none", "high"):
                    raise ValueError("Kimi supports only none/high (thinking off/on)")
                selected = effort == "high"
                if isinstance(thinking, bool) and thinking != selected:
                    raise ValueError("thinking conflicts with reasoning_effort")
                thinking = selected
            if isinstance(thinking, bool):
                out["extra_body"] = {"thinking": {"type": "enabled" if thinking else "disabled"}}
        else:
            if thinking is False:
                if effort not in (None, "none"):
                    raise ValueError("thinking off conflicts with reasoning_effort")
                effort = "none"
            if effort is not None:
                if not isinstance(effort, str) or not effort.strip():
                    raise ValueError("reasoning_effort must be a concrete provider level")
                out["reasoning_effort"] = effort
        if "temperature" in request.params:
            if k3:
                raise ValueError("Kimi K3 has fixed temperature; omit temperature")
            out["temperature"] = request.params["temperature"]
        return out

    def _create(self, request: Any, pick: Callable[..., Any], **kwargs: Any) -> Any:
        return _bounded_create(self.name, self._client, request, pick,
                               self._timeout_for, self._http_client_factory, **kwargs)

    def complete(self, model: str, request: LLMRequest,
                 json_schema: dict[str, Any]) -> ProviderResponse:
        kwargs = self._kwargs(model, request)
        prompt = request.prompt
        native = self.name in ("openai", "gemini") or (
            self.name == "kimi" and model.startswith("kimi-k3")
        )
        strict_schema = _strict_schema(json_schema) if native else None
        if strict_schema is not None:
            kwargs["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "answer", "strict": True, "schema": strict_schema}}
        else:
            prompt += ("\n\nRespond with one JSON object matching this JSON Schema:\n"
                       + json.dumps(json_schema, ensure_ascii=False))
            kwargs["response_format"] = {"type": "json_object"}
        kwargs["messages"] = [{"role": "system", "content": request.system},
                              {"role": "user", "content": prompt}]
        resp = self._create(request, lambda c: c.chat.completions.create, **kwargs)
        choice = self._choice(resp)
        tokens = compatible_tokens(getattr(resp, "usage", None))
        raw = choice.message.content
        self._check(choice.finish_reason, raw, getattr(choice.message, "refusal", None),
                    resp.model, tokens)
        if not raw or not raw.strip():
            raise ProviderEmptyOutputError(f"{self.name} returned empty content",
                                           model_version=resp.model, **tokens)
        return ProviderResponse(raw_output=raw, model_version=resp.model, cost=None, **tokens)

    def _choice(self, resp: Any) -> Any:
        if not getattr(resp, "choices", None):
            raise ProviderTransientError(f"{self.name} returned no choices")
        return resp.choices[0]

    def _check(self, finish: str | None, content: str | None, refusal: Any,
               model: str | None, tokens: dict[str, Any]) -> None:
        known = {"raw_output": content, "model_version": model, **tokens}
        if refusal or finish in ("content_filter", "sensitive"):
            raise ProviderRefusalError(f"{self.name} refused the response", **known)
        if finish in ("length", "model_context_window_exceeded"):
            raise ProviderEmptyOutputError(f"{self.name} output truncated", status="truncated", **known)
        if finish == "network_error":
            raise ProviderTransientError(f"{self.name} reported a network error", **known)

    def chat(self, model: str, request: ChatRequest,
             stream: StreamSink | None = None) -> ChatResponse:
        kwargs = self._kwargs(model, request)
        kwargs["messages"] = compatible_messages(request, reasoning=self.name in ("kimi", "glm"))
        if request.tools:
            kwargs["tools"] = [{"type": "function", "function": {
                "name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in request.tools]
            if self.name == "glm" and request.tool_choice == "none":
                # GLM documents only auto; omitting tools implements a no-tools turn.
                kwargs.pop("tools")
            else:
                kwargs["tool_choice"] = request.tool_choice
            if self.name == "glm" and stream is not None and "tools" in kwargs:
                kwargs.setdefault("extra_body", {})["tool_stream"] = True
        if stream is None:
            resp = self._create(request, lambda c: c.chat.completions.create, **kwargs)
            choice = self._choice(resp)
            message = choice.message
            calls = [(c.id, c.function.name, c.function.arguments)
                     for c in getattr(message, "tool_calls", None) or ()]
            extras = {c.id: c.extra_content for c in getattr(message, "tool_calls", None) or ()
                      if getattr(c, "extra_content", None)}
            content, reasoning = message.content, getattr(message, "reasoning_content", None)
            finish, usage, version = choice.finish_reason, getattr(resp, "usage", None), resp.model
            refusal = getattr(message, "refusal", None)
        else:
            gate, progress = _Gate(stream), Progress(self._clock)
            try:
                options = {} if self.name == "glm" else {"stream_options": {"include_usage": True}}
                acc, extras, refusal = self._create(
                    request, lambda c: partial(_stream, c, gate, progress),
                    progress=progress, stream=True, **options, **kwargs)
            finally:
                gate.close()
            calls = acc.tool_calls()
            content, reasoning = "".join(acc.content), "".join(acc.reasoning)
            finish, usage, version = acc.finish_reason, acc.usage, acc.model
        tokens = compatible_tokens(usage)
        self._check(finish, content, refusal, version, tokens)
        parsed = []
        for call_id, name, raw in calls:
            try:
                args = raw if isinstance(raw, dict) else json.loads(raw)
            except (TypeError, ValueError):
                args = None
            parsed.append(ToolCall(id=call_id, name=name, args=args) if isinstance(args, dict)
                          else ToolCall(id=call_id, name=name, unparsed=str(raw)))
        echo: dict[str, Any] = {}
        if self.name in ("kimi", "glm") and reasoning:
            echo["reasoning_content"] = reasoning
        if extras:
            echo["tool_extra_content"] = extras
        turn = ChatTurn(text=content or "", tool_calls=tuple(parsed), echo=echo or None,
                        input_tokens=tokens["input_tokens"] or 0,
                        output_tokens=tokens["output_tokens"] or 0,
                        cache_read_tokens=tokens["cache_read_tokens"] or 0,
                        cache_write_tokens=tokens["cache_write_tokens"] or 0, stop_reason=finish)
        return ChatResponse(turn=turn, model_version=version, cost=None, **tokens)


def _stream(client: Any, gate: _Gate, progress: Progress, **kwargs: Any) -> tuple[Any, dict, Any]:
    chunks = client.chat.completions.create(**kwargs)
    acc, extras, refusal = _DeepSeekStream(), {}, None
    try:
        for chunk in chunks:
            progress.beat()
            acc.add(chunk, gate)
            for choice in getattr(chunk, "choices", None) or ():
                delta = getattr(choice, "delta", None)
                refusal = getattr(delta, "refusal", None) or refusal
                for call in getattr(delta, "tool_calls", None) or ():
                    if getattr(call, "extra_content", None):
                        extras[call.index] = call.extra_content
            if acc.content or any(s["args"] for s in acc.calls.values()):
                reply_started()
            if cancel_requested():
                raise RequestCancelled(model_version=acc.model, **compatible_tokens(acc.usage))
    finally:
        chunks.close()
    if acc.finish_reason is None:
        raise StreamCut("Chat Completions stream ended without a finish reason")
    return acc, {acc.calls[i]["id"] or f"call_{i}": v for i, v in extras.items()}, refusal
