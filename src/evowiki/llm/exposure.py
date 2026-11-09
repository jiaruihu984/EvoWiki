"""How a request's tools reach the model (design B2.6, stage 3 T8). The upper layers (the assistant
loop) compute one ordered tool set per model call (`agent.skills.activate`); the adapter picks how it
is sent:

- `mid_turn_swap`: each model call carries the tool set as it is now (a load_skill result adds the
  Skill's tools from the next call on);
- `two_phase`: the tool list stays as the turn began; a model call whose tool calls were ONLY
  loads, and that changed the set, is folded into one user-side note and the step is asked again
  with the new list (`agent.spec.Restart`); a load inside a mixed batch takes effect next turn;
- `native_deferred`: the provider's own deferred tools + tool search (off everywhere until the
  live check of T19 confirms the shape: `EVOWIKI_NATIVE_DEFERRED=1` turns it on for Anthropic).

The capability table is code. Until T19 verifies a provider live it holds: DeepSeek →
mid_turn_swap; Anthropic thinking off → mid_turn_swap; Anthropic thinking on → two_phase (a
thinking signature may not survive a changed tool list), except a model in DROPS_STALE_THINKING,
whose chat requests let the API drop a block a changed list invalidates → mid_turn_swap. Haiku 5.5
often batches a load with a read, and in two_phase that load's tools only come next turn, which it
tells the learner to come back for. A provider that rejects a changed list
mid-turn anyway (`rejects_tool_change`) is downgraded to two_phase for the rest of that turn by the
loop; a tool-search failure (native_deferred) retries that call as two_phase. The trace records
which ran (`ChatRequest.params["tool_exposure"]`)."""

import os
from collections.abc import Mapping
from enum import StrEnum


class ToolExposure(StrEnum):
    native_deferred = "native_deferred"
    mid_turn_swap = "mid_turn_swap"
    two_phase = "two_phase"


NATIVE_DEFERRED_ENV = "EVOWIKI_NATIVE_DEFERRED"
# Chat on these sends thinking.block_binding "drop_block" (llm.providers.AnthropicProvider.chat).
DROPS_STALE_THINKING = ("claude-haiku-5-5",)


def exposure_for(provider: str, model: str, *, thinking: bool,
                 env: Mapping[str, str] = os.environ) -> ToolExposure:
    p = provider.lower()
    if p.startswith("anthropic") or p == "claude":
        if env.get(NATIVE_DEFERRED_ENV) == "1":
            return ToolExposure.native_deferred
        if not thinking or model.startswith(DROPS_STALE_THINKING):
            return ToolExposure.mid_turn_swap
        return ToolExposure.two_phase
    return ToolExposure.mid_turn_swap


class ToolSearchFailed(RuntimeError):
    """The provider's own tool search (native_deferred) failed; the call is retried two_phase."""


_TOOL_REFS = ("not found", "unknown", "must be defined", "not defined", "does not exist")


def _http_400(exc: BaseException) -> BaseException | None:
    """The HTTP 400 in `exc`'s chain (an SDK BadRequestError: `status_code` 400), if any."""
    seen: set[int] = set()
    e: BaseException | None = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        code = getattr(e, "status_code", None)
        if isinstance(code, int) and not isinstance(code, bool) and code == 400:
            return e
        e = e.__cause__ or e.__context__
    return None


def _provider_text(exc: BaseException) -> str:
    body = getattr(exc, "body", None)
    inner = body.get("error") if isinstance(body, dict) else None
    msg = inner.get("message") if isinstance(inner, dict) else None
    return " ".join(str(msg if isinstance(msg, str) else exc).split()).lower()


def rejects_tool_change(exc: BaseException) -> bool:
    """A provider error that refuses a changed tool list mid-turn: our own tool-search failure, or
    an HTTP 400 whose provider text is about a thinking block's signature, or about a tool the
    history uses that the list no longer defines. Anything else is not downgraded."""
    if isinstance(exc, ToolSearchFailed):
        return True
    bad = _http_400(exc)
    if bad is None:
        return False
    text = _provider_text(bad)
    if "signature" in text and "thinking" in text:
        return True
    return "tool" in text and any(r in text for r in _TOOL_REFS)
