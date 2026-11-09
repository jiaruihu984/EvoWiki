"""read_k: the user's aspect K for some concepts, in the same text the organizer and `ask` read
(`goals/observe.k_text`): band, m, u and the last two evidence notes per cell. Read-only — K only
changes through the deterministic update."""

from collections.abc import Mapping
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from evowiki.agent.tool_helpers import K_CHARS, clip, fail, run_guarded, spec
from evowiki.agent.tools import ToolContext, ToolResult
from evowiki.goals.contracts import KView
from evowiki.goals.observe import k_text

DESCRIPTION = ("看用户对某些概念的掌握情况：每个层面（术语、用途、记号、计算、解读、原理）的档位"
               "（确定会/可能会/模糊/不会/没证据）、m、u 和最近证据。决定讲多深、跳过什么之前用。"
               "对他说学到哪了时用他看到的词：确定会说掌握，可能会和模糊说熟悉，不会说不会，没证据说还没学；数字不说。"
               "一次最多 8 个概念 id，结果最多 4000 字。")


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")

    concepts: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        min_length=1, max_length=8, description="概念 id，见 read_page 大纲的「概念」行")


class ReadKTool:
    def __init__(self, kview: KView, names: Mapping[str, str] | None = None):
        self._kview, self._names = kview, dict(names or {})
        self.spec = spec("read_k", DESCRIPTION, Args, "read")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(Args, args, self._run)

    def _run(self, a: Args) -> ToolResult:
        known = [c for c in dict.fromkeys(a.concepts) if self._kview.aspects(c)]
        unknown = [c for c in dict.fromkeys(a.concepts) if c not in known]
        if not known:
            return fail(f"没有这些概念的 K：{', '.join(unknown)}")
        text = k_text(self._kview, known, self._names)
        if unknown:
            text += "\n没有 K 的概念：" + ", ".join(unknown)
        # the tool line names the points it read (the first three)
        return ToolResult(content=clip(text, K_CHARS),
                          summary="、".join(self._names.get(c, c) for c in known[:3])[:200])
