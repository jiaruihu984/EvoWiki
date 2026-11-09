"""Shared plumbing for agent tools: argument validation, output caps, never-raise wrapper.

A tool's result goes back into the model's context verbatim, so every tool has its own cap well
under `ToolResult.content`'s hard limit; failures come back as `error` results the model can react
to (Claude Code's is_error tool results), never as exceptions that would end the turn. Neutral
home for the assistant, writer and forge tools alike."""

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ValidationError

from evowiki.agent.tools import ToolCategory, ToolEffect, ToolResult, ToolSpec, ToolTier

HARD_CAP = 11000             # below ToolResult.content's 12000, leaves room for the cut marker
ERROR_CHARS = 300

SNIPPET_CHARS = 240
TITLE_CHARS = 300
PAGE_OUTLINE_CHARS = 4000
SECTION_CHARS = 6000
K_CHARS = 4000
SEARCH_CHARS = 4000
FETCH_CHARS = 6000


def clip(text: str, limit: int) -> str:
    """`text` if it fits, else a prefix plus a marker saying how long the original was."""
    if len(text) <= limit:
        return text
    marker = f"\n…[截断，原文共 {len(text)} 字]"
    return text[: max(0, limit - len(marker))] + marker


def fail(message: str) -> ToolResult:
    msg = clip(message, ERROR_CHARS)
    return ToolResult(content=f"错误：{msg}", error=msg)


def spec(name: str, description: str, args_model: type[BaseModel],
         effect: ToolEffect, category: ToolCategory | None = None, *,
         timeout_s: float | None = None, parallel: bool = False, acts: bool = False,
         pauses: bool = False, tier: ToolTier | None = None) -> ToolSpec:
    """`category=None` derives from `effect` (read/network → read, propose → action); so does
    `tier=None` (read/network → read, propose → draft)."""
    return ToolSpec(name=name, description=description,
                    parameters=args_model.model_json_schema(), effect=effect, category=category,
                    timeout_s=timeout_s, parallel=parallel, acts=acts, pauses=pauses, tier=tier)


def _errors(e: ValidationError) -> str:
    return "；".join(f"{'.'.join(map(str, x['loc'])) or '参数'}: {x['msg']}"
                    for x in e.errors(include_url=False))


def run_guarded[A: BaseModel](args_model: type[A], args: dict[str, Any],
                              body: Callable[[A], ToolResult]) -> ToolResult:
    """Validate `args`, run `body`, and turn any failure into an error result.

    The `HARD_CAP` backstop only covers content of 11001–12000 chars: anything longer already
    fails `ToolResult` validation inside `body`, which comes back here as an error result."""
    try:
        parsed = args_model.model_validate(args)
    except ValidationError as e:
        return fail(f"参数不合法：{_errors(e)}")
    try:
        r = body(parsed)
    except Exception as e:  # noqa: BLE001 — a tool must never raise into the loop
        return fail(f"{type(e).__name__}: {e}")
    if len(r.content) > HARD_CAP:
        r = r.model_copy(update={"content": clip(r.content, HARD_CAP)})
    return r
