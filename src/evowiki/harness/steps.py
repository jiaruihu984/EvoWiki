"""Structured pack values (design 2026-10-21 §3.3 / §3.4): H9 / H11 hook steps, H8 component
presets and extra EvoUI examples. Pure: shapes and allow-lists only (harness/keys.py anchors).
Whether a component exists, or a preset config / example block is valid EvoUI, is checked where the
value is applied. Every refusal is HarnessInvalid with a plain reason (fail closed)."""

from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, field_validator

from evowiki.harness.keys import (
    HOOK_MAX_TOOL_CALLS,
    HOOK_TEXT_MAX,
    HOOKS,
    SKILL_NAME_MAX,
    SKILL_USE_MAX,
)
from evowiki.harness.pack import HarnessInvalid


class _M(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _text(v: str, label: str, hi: int) -> str:
    t = v.strip()
    if not t:
        raise ValueError(f"{label}不能是空的")
    if len(t) > hi:
        raise ValueError(f"{label}最多 {hi} 个字")
    return t


class HookStep(_M):
    at: str
    kind: Literal["delegate", "self_check"]
    tier: Literal["fast", "good", "deep"]
    tools: tuple[str, ...] = ()
    instruction: str
    output_to: str
    max_tool_calls: StrictInt = Field(ge=0, le=HOOK_MAX_TOOL_CALLS)

    @field_validator("instruction")
    @classmethod
    def _instruction(cls, v: str) -> str:
        t = _text(v, "说明", HOOK_TEXT_MAX)
        if "⟦" in t or "⟧" in t:
            raise ValueError("说明里不能有 ⟦ 或 ⟧")
        return t


class _Named(_M):
    name: str           # 1..SKILL_NAME_MAX after strip
    use: str            # 1..SKILL_USE_MAX after strip: when to pick it

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _text(v, "名字", SKILL_NAME_MAX)

    @field_validator("use")
    @classmethod
    def _use(cls, v: str) -> str:
        return _text(v, "用途", SKILL_USE_MAX)


class Preset(_Named):
    """An H8 component variant: a named config of a configurable component."""
    config: dict[str, Any]


class Example(_Named):
    """An extra EvoUI template, same fields as deep/evoui/examples.json entries. `group`: the EvoUI
    reference it goes with (deep@v32, `deep.evoui.GROUPS`); "" or unknown = evoui/core."""
    block: dict[str, Any]
    group: str = ""


_FIELD = {"at": "位置", "kind": "种类", "tier": "档位", "tools": "工具", "instruction": "说明",
          "output_to": "结果去处", "max_tool_calls": "最多调用几次工具", "name": "名字",
          "use": "用途", "config": "配置", "block": "交互块",
          "group": "类别"}
_TYPE = {"int_type": "整数", "string_type": "文字", "bool_type": "true 或 false",
         "tuple_type": "列表", "list_type": "列表", "dict_type": "键值对象"}


def _reason(model: type[BaseModel], err: Any, *, own_text: bool = True) -> str:
    """Pydantic's first error as a plain Chinese reason naming the field. `own_text=False` for a
    model whose validators are not ours (their messages are English): a value error then reads
    「…」不合法 instead of the validator's own message."""
    loc = err.get("loc") or ()
    field = str(loc[0]) if loc else ""
    label = _FIELD.get(field, field)
    t, ctx = err["type"], err.get("ctx") or {}
    if t == "missing":
        return f"缺少「{label}」"
    if t == "extra_forbidden":
        return f"多了不认识的字段「{field}」"
    if t == "value_error":
        if not own_text:
            return f"「{label}」不合法"
        return str(err["msg"]).removeprefix("Value error, ")
    if t in ("too_short", "string_too_short"):
        return f"「{label}」不能是空的"
    if t in ("too_long", "string_too_long"):
        return f"「{label}」太长了"
    if t == "less_than_equal":
        return f"「{label}」最多是 {ctx.get('le')}"
    if t == "greater_than_equal":
        return f"「{label}」最少是 {ctx.get('ge')}"
    if t == "literal_error":
        f = model.model_fields.get(field)
        allowed = "、".join(map(str, get_args(f.annotation))) if f else ""
        return f"「{label}」只能是 {allowed} 之一"
    if t in _TYPE:
        return f"「{label}」要是{_TYPE[t]}"
    return f"「{label}」的格式不对"


_KIND_OF_DIM = {"H9": "delegate", "H11": "self_check"}


def _list(name: str, value: object) -> list[Any]:
    if not isinstance(value, list) or not value:
        raise HarnessInvalid(f"{name} 要是一个不为空的列表")
    if not all(isinstance(x, dict) for x in value):
        raise HarnessInvalid(f"{name} 的每一项都要是键值对象")
    return value


def _models(name: str, value: object, model: type[_M]) -> list[Any]:
    out = []
    for i, item in enumerate(_list(name, value), 1):
        try:
            out.append(model.model_validate(item))
        except ValidationError as exc:
            raise HarnessInvalid(f"{name} 第 {i} 项不对：{_reason(model, exc.errors()[0])}") from exc
    return out


def parse_steps(key: str, value: object) -> tuple[HookStep, ...]:
    dim, _, at = key.partition(".")
    hook = HOOKS.get(at)
    kind = _KIND_OF_DIM.get(dim)
    if hook is None or kind is None or kind not in hook.kinds:
        raise HarnessInvalid(f"没有这个设置：{key}")
    steps: list[HookStep] = _models(key, value, HookStep)
    for i, s in enumerate(steps, 1):
        where = f"{key} 第 {i} 步"
        if s.at != at:
            raise HarnessInvalid(f"{where}：位置要是 {at}")
        if s.kind != kind:
            raise HarnessInvalid(f"{where}：这里只能是 {kind}")
        if s.tier not in hook.tiers:
            raise HarnessInvalid(f"{where}：这里不能用 {s.tier} 这一档")
        if len(set(s.tools)) != len(s.tools):
            raise HarnessInvalid(f"{where}：工具重复了")
        bad = [t for t in s.tools if t not in hook.tools]
        if bad:
            raise HarnessInvalid(f"{where}：这里不能用工具 {'、'.join(bad)}")
        if s.output_to != hook.output_to:
            raise HarnessInvalid(f"{where}：结果要放到 {hook.output_to}")
    return tuple(steps)


def _unique(name: str, items: list[Any]) -> None:
    names = [x.name for x in items]
    if len(set(names)) != len(names):
        raise HarnessInvalid(f"{name} 里有重名的项")


def parse_presets(value: object) -> tuple[Preset, ...]:
    out: list[Preset] = _models("组件预设", value, Preset)
    _unique("组件预设", out)
    return tuple(out)


def parse_examples(value: object) -> tuple[Example, ...]:
    out: list[Example] = _models("交互块示例", value, Example)
    _unique("交互块示例", out)
    return tuple(out)
