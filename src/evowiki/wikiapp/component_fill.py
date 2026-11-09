"""Filling a library component's config: ONE structured call whose response schema is that component's own
`config_schema` (closed, every property required: `deep.components.check_schema`), checked in the same step and
repaired at most REPAIR_TURNS times.

Why a call of its own: the configs of the whole library together are far over what Anthropic structured outputs
take in one request (at most 16 union-typed parameters; the 13 seed components have 60), so a caller first lets the
model pick a component (an enum of ids, no config) and then asks for that one component's config here. The model
fills data only — the component's code is the library's.

What the schema cannot say is checked right after the call (`validate_config`: the caps; `check`: the caller's
render check on component + config, e.g. a big empty area) and sent back with the reply for a repair turn. After
the turns: `None` and the reasons (for the log; the learner sees plain words from the caller)."""

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any

from pydantic import BaseModel, WithJsonSchema, create_model

from evowiki.deep.components import ConfigInvalid, validate_config
from evowiki.deep.contracts import LibraryComponent
from evowiki.llm.gateway import LLMParseError
from evowiki.llm.models import LLMRequest
from evowiki.wikiapp.llm import JSON_SYSTEM, CountingGateway

log = logging.getLogger(__name__)

REPAIR_TURNS = 2
MAX_PROBLEMS = 8


@dataclass
class Filled:
    """`config` (None: no usable one after the turns), what went wrong on the way, what the calls cost."""

    config: dict[str, Any] | None
    tried: list[str] = field(default_factory=list)
    cost: float = 0.0


def reply_schema(component_id: str, schema: dict[str, Any]) -> type[BaseModel]:
    """`{"config": <the component's config_schema>}` as a response model (the pydantic side a plain dict:
    `validate_config` checks it against the same schema)."""
    safe = "".join(ch if ch.isalnum() else "_" for ch in component_id)
    return create_model(f"Config_{safe}", config=(Annotated[dict[str, Any], WithJsonSchema(dict(schema))], ...))


def about(comp: LibraryComponent) -> str:
    """A library component as `fill_config` names it."""
    return f"「{comp.name}」：{comp.description}\n它要回答的问题：{comp.learning_question}"


def config_prompt(component_id: str, about_line: str, context: str) -> str:
    return (f"{context}\n\n用组件库里的组件 {component_id} {about_line}\n"
            "组件代码是现成的，你只填它的 config（数据），按回复格式里 config 的字段写，"
            "内容用上面这一页自己的例子和说法。\n回复 JSON：{\"config\": {…}}")


def fill_config(gw: CountingGateway, component_id: str, schema: dict[str, Any], about_line: str, context: str,
                *, purpose: str, prompt_version: str, subject: str,
                check: Callable[[dict[str, Any]], Sequence[str]] | None = None) -> Filled:
    """The component's config for `context` (the page and what the widget is for), or `Filled(None, …)`.
    LLM failures other than an unparseable reply propagate (the caller says the model is unavailable)."""
    model = reply_schema(component_id, schema)
    first = config_prompt(component_id, about_line, context)
    prompt, out = first, Filled(None)
    for turn in range(1 + REPAIR_TURNS):
        req = LLMRequest(purpose=purpose, prompt_version=prompt_version, subject=subject,
                         system=JSON_SYSTEM, prompt=prompt, params={})
        try:
            reply, call = gw.call("generator", req, model)
        except LLMParseError as e:
            out.cost += e.cost or 0.0
            out.tried.append(f"config turn {turn}: {e}")
            prompt = first + f"\n\n上一次的回答不是要求的 JSON 结构；严格按字段给出完整的 JSON（第 {turn + 1} 次）。"
            continue
        out.cost += call.cost or 0.0
        config = reply.config  # type: ignore[attr-defined]
        try:
            config = validate_config(schema, config)
            problems = list(check(config)) if check is not None else []
        except ConfigInvalid as e:
            problems = [str(e)]
        if not problems:
            out.config = config
            return out
        out.tried.append(f"config turn {turn}: " + "；".join(problems[:MAX_PROBLEMS]))
        prompt = (f"{first}\n\n你上一次的回答：\n{json.dumps({'config': config}, ensure_ascii=False)}\n\n"
                  "它有这些问题，没法用：\n" + "\n".join(f"- {x}" for x in problems[:MAX_PROBLEMS])
                  + f"\n\n改好这些问题，重新给出完整的 JSON（第 {turn + 1} 次修改）。")
    log.warning("component %s config for %s: none usable after %d turns: %s", component_id, subject,
                1 + REPAIR_TURNS, " | ".join(out.tried))
    return out
