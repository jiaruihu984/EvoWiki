"""The page writer's skill (deep@v32, design 2026-10-28 A4 / A5): `skills/write-page`. SKILL.md is
the writer's system prompt; its references are read with `read_ref` or put in the first message
by code when the run surely needs them (`preload`): teaching always, outline + assessment for a
page without an outline, sources with materials, the EvoUI index always. Interaction and the
EvoUI groups are read only when the writer decides on an interactive.

The single sources of the included text stay in code: the teaching standard's sections
(`teach.standard`), the item-writing rules (`goals.itemflaws.ITEM_RULES_ZH`, shared with the
follow-up check, cards, patch and the assistant's quiz) and the plain-text check rule; the EvoUI
templates are `deep/evoui/examples.json` (by `group`), plus the pack's H8 templates."""

from collections.abc import Callable, Mapping, Sequence
from functools import cache
from typing import Any

from evowiki.agent.skills import Includes, ReadRefTool, Skill, load_skill, read_ref
from evowiki.deep.evoui import GROUPS, group_templates
from evowiki.goals.itemflaws import ITEM_RULES_ZH
from evowiki.goals.outline_context import PLAIN_CHECK
from evowiki.llm.prompt_steps import apply_marks
from evowiki.teach.standard import sections

NAME = "write-page"
EVOUI = "evoui/"

INCLUDES: Includes = {
    "standard": lambda *keys: sections(*keys),
    "item_rules": lambda: ITEM_RULES_ZH,
    "plain_check": lambda: PLAIN_CHECK + "。",
}


@cache
def skill() -> Skill:
    return load_skill(NAME)


Extra = Callable[[], Sequence[Mapping[str, Any]]]


def _appendix(extra: Extra) -> Callable[[str], str]:
    """An EvoUI group's templates after its ref (`extra()`: the pack's H8 templates, read when
    the ref is read: the workspace gets them after its tools are built)."""
    def add(ref: str) -> str:
        group = ref.removeprefix(EVOUI)
        if ref.startswith(EVOUI) and (group in GROUPS or group == "core"):
            return group_templates(group, extra())
        return ""
    return add


def ref(name: str, extra: Sequence[Mapping[str, Any]] = ()) -> str:
    """One reference of the skill as `read_ref` returns it (no 【name】 head)."""
    return read_ref(skill(), name, INCLUDES, _appendix(lambda: extra))


def read_ref_tool(extra: Extra = tuple, *, guidance: Mapping[str, str] | None = None) -> ReadRefTool:
    pinned = dict(guidance or {})
    return ReadRefTool([skill()], INCLUDES, _appendix(extra),
                       transform=lambda text: apply_marks(text, pinned)[0])


def system_text() -> str:
    return skill().body


@cache
def version() -> str:
    """What a writer run used, for its agent_run record (a replay pins it): the skill's files and
    the EvoUI templates. The included text is code, versioned with deep@ / standard@ and the item
    rules' consumers."""
    from hashlib import sha256

    from evowiki.deep.evoui import EXAMPLES_PATH
    return f"{skill().version}+examples@{sha256(EXAMPLES_PATH.read_bytes()).hexdigest()[:12]}"
