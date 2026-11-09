"""A concept's scope as the goal's split defined it (product audit 2026-10-27, D09).

The outline / split writes each concept's summary (kept as the node's definition) and its aspects
(K's cells); the aspects it does not have are out of scope. D09: 「特征值概念」, defined without the
计算过程 layer, became a page whose goal was to compute eigenvalues, because the path saw only the
names. `concept_scope` renders that existing data as one line; the path (path@v9) and the writer
(deep@v27) receive it and stay within it. Nothing is re-derived.

What the learner said about himself is not here: that is his profile 「我」 (evowiki.profile),
which the gateway sends with every call.

Pure: no LLM, no storage."""

from collections.abc import Sequence
from dataclasses import dataclass

from evowiki.goals.contracts import Aspect
from evowiki.goals.guardrails import ASPECT_ZH


def concept_scope(name: str, definition: str, aspects: Sequence[Aspect | str]) -> str:
    """One concept's scope line: its definition, the layers it covers and the ones it does not
    (in Aspect order). No aspects known: the definition only."""
    have = [Aspect(a) for a in aspects]
    line = name + (f"：{definition.strip()}" if definition.strip() else "")
    if have:
        line += "；讲的层面：" + "、".join(ASPECT_ZH[a] for a in Aspect if a in have)
        out = [ASPECT_ZH[a] for a in Aspect if a not in have]
        if out:
            line += "；不讲：" + "、".join(out)
    return line


@dataclass(frozen=True)
class ConceptScope:
    """The parts of `concept_scope`, for a caller that renders it later (the writer leaves the
    definition out on a page written from materials: there the materials say what is in, and a
    definition can carry a material's words)."""

    name: str
    definition: str
    aspects: tuple[Aspect, ...]

    def line(self, *, definition: bool = True) -> str:
        return concept_scope(self.name, self.definition if definition else "", self.aspects)
