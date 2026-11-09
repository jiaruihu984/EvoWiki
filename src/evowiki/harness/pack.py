"""A user's harness pack (design 2026-10-21 §3): the values that replace the product's defaults
for that user, and the version they were read at. Immutable; read once per agent run. A getter
takes the default from the caller (the module's own constant), so an empty pack is exactly
today's behaviour. Unknown names raise KeyError (a typo in code fails loudly); a stored value
that is no longer valid, or a stored name that is no longer a key, raises HarnessInvalid (fail
closed, never a silent default)."""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from evowiki.harness import tool_names
from evowiki.harness.keys import Key, key_of

Value = str | int | bool | list[Any] | dict[str, Any]


class HarnessInvalid(ValueError):
    """A key or value the pack may not hold; the message is the plain reason."""


def validate(name: str, value: object) -> Value:
    k = key_of(name)
    if k is None:
        raise HarnessInvalid(f"没有这个设置：{name}")
    if k.dim == "H2":
        agent, tool, *arg = name.split(".")[1:]
        if not tool_names.known(agent, tool, arg[0] if arg else None):
            raise HarnessInvalid(f"没有这个设置：{name}")
    if k.kind in _STRUCTURED:
        return _STRUCTURED[k.kind](k, value)
    if k.kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise HarnessInvalid(f"{name} 要是整数")
        if not (k.lo <= value <= k.hi):  # type: ignore[operator]
            raise HarnessInvalid(f"{name} 要在 {k.lo} 到 {k.hi} 之间")
        return value
    if not isinstance(value, str):
        raise HarnessInvalid(f"{name} 要是文字")
    t = value.strip()
    if not t:
        raise HarnessInvalid(f"{name} 不能是空的")
    if len(t) > k.hi:  # type: ignore[operator]
        raise HarnessInvalid(f"{name} 最多 {k.hi} 个字")
    if "⟦" in t or "⟧" in t:     # guidance marker brackets (llm/prompt_steps.py)
        raise HarnessInvalid(f"{name} 里不能有 ⟦ 或 ⟧")
    return t


def _bool(k: Key, value: object) -> Value:
    if not isinstance(value, bool):
        raise HarnessInvalid(f"{k.name} 要是 true 或 false")
    return value


def _enum(k: Key, value: object) -> Value:
    if not isinstance(value, str) or value not in k.options:
        raise HarnessInvalid(f"{k.name} 只能是 {'、'.join(k.options)} 之一")
    return value


def _list(k: Key, value: object) -> Value:
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise HarnessInvalid(f"{k.name} 要是文字列表")
    bad = [x for x in value if x not in k.options]
    if bad:
        raise HarnessInvalid(f"{k.name} 只能含 {'、'.join(k.options)}")
    if len(set(value)) != len(value):
        raise HarnessInvalid(f"{k.name} 里有重复的项")
    if not (k.lo <= len(value) <= k.hi):  # type: ignore[operator]
        raise HarnessInvalid(f"{k.name} 要有 {k.lo} 到 {k.hi} 项")
    return list(value)


def _steps(k: Key, value: object) -> Value:
    from evowiki.harness.steps import parse_steps  # steps imports HarnessInvalid from here
    return [s.model_dump(mode="json") for s in parse_steps(k.name, value)]


def _json(k: Key, value: object) -> Value:
    from evowiki.harness.steps import parse_examples, parse_presets
    if k.dim == "H12":
        return _h12(k.name, value)
    if k.name == "H8.deep.evoui_examples":
        items = parse_examples(value)
    elif k.name.startswith("H8.component.") and k.name.endswith(".presets"):
        items = parse_presets(value)
    else:
        raise HarnessInvalid(f"没有这个设置：{k.name}")
    # an example without a group is stored as before (no "group": "")
    return [x.model_dump(mode="json", exclude_defaults=k.name == "H8.deep.evoui_examples")
            for x in items]


_CID = re.compile(r"^fc_[A-Za-z0-9]{1,40}$")
_SHA = re.compile(r"^[0-9a-f]{7,40}$")


def _h12(name: str, value: object) -> Value:
    if not isinstance(value, dict) or not _CID.fullmatch(str(value.get("candidate_id", ""))):
        raise HarnessInvalid(f"{name} 要写明是哪个候选")
    if name.startswith("H12.patch."):
        if value["candidate_id"] != name.removeprefix("H12.patch."):
            raise HarnessInvalid(f"{name} 的候选和键名不一致")
        if set(value) != {"candidate_id", "commit"} or not _SHA.fullmatch(str(value["commit"])):
            raise HarnessInvalid(f"{name} 要有候选和提交号")
        return dict(value)
    extra = set(value) - {"candidate_id", "registry_version"}
    rv = value.get("registry_version")
    if extra or (rv is not None and (isinstance(rv, bool) or not isinstance(rv, int) or rv < 1)):
        raise HarnessInvalid(f"{name} 只能有候选和注册表版本")
    return dict(value)


_STRUCTURED = {"bool": _bool, "enum": _enum, "list": _list, "steps": _steps, "json": _json}


@dataclass(frozen=True)
class HarnessPack:
    version: int
    values: Mapping[str, Value] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))

    @property
    def empty(self) -> bool:
        return not self.values

    def get(self, name: str) -> Value | None:
        if key_of(name) is None:
            raise KeyError(name)
        v = self.values.get(name)
        return None if v is None else validate(name, v)

    def int(self, name: str, default: int) -> int:
        v = self.get(name)
        return default if v is None else v  # type: ignore[return-value]

    def text(self, name: str, default: str) -> str:
        v = self.get(name)
        return default if v is None else v  # type: ignore[return-value]

    def _of_kind(self, name: str, kinds: tuple[str, ...]) -> Value | None:
        k = key_of(name)
        if k is None:
            raise KeyError(name)
        if k.kind not in kinds:
            raise TypeError(f"{name} is a {k.kind} key, not {'/'.join(kinds)}")
        return self.get(name)

    def flag(self, name: str, default: bool) -> bool:
        v = self._of_kind(name, ("bool",))
        return default if v is None else v  # type: ignore[return-value]

    def choice(self, name: str, default: str | None) -> str | None:
        v = self._of_kind(name, ("enum",))
        return default if v is None else v  # type: ignore[return-value]

    def items(self, name: str) -> list[Any] | None:
        """A list / steps / json value (normalised), None when unset."""
        return self._of_kind(name, ("list", "steps", "json"))  # type: ignore[return-value]

    def guidance(self) -> dict[str, str]:
        """H1 values as {step key: text} — the shape the gateway's `overrides` reads."""
        return {n[3:]: str(validate(n, v)) for n, v in self.values.items() if n.startswith("H1.")}

    def with_prefix(self, prefix: str) -> dict[str, Value]:
        """The stored values under `prefix` (stored names: one with no key is HarnessInvalid)."""
        return {n: validate(n, v) for n, v in self.values.items() if n.startswith(prefix)}


EMPTY = HarnessPack(0, {})
