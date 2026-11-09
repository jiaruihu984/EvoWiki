"""Evidence extraction (plan rev 3.1 §2 step 1, §8.B): an LLM reads the user's own words (a
self-description at intake) and reports observations; code keeps only those that cite a real cell
on the fixed aspect list and quote the user's verbatim words. Numbers are never touched here: the K
lane's deterministic update applies them. A question asked in chat is evidence through the
assistant's own `record_evidence` tool (stage 3 T9), not through a separate call.

The LLM is injected (`JsonLLM`): goals/ never imports the LLM layer (tests/goals/test_isolation)."""

import math
from collections.abc import Collection, Mapping, Sequence
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from evowiki.goals.contracts import Aspect, KView, Observation, split_cell

Role = Literal["generator", "verifier"]
POLARITIES = ("+", "-", "~")
SELF_DEFAULT_STRENGTH = 0.5
ASK_DEFAULT_STRENGTH = 0.4    # a question is weaker evidence than an answer (artifact v5)


class JsonLLM(Protocol):
    """One LLM call returning a parsed `schema` instance. role="verifier" must be an independent
    call that shares nothing with the generator's call (in the Gateway: another provider)."""

    def __call__[T: BaseModel](self, role: Role, purpose: str, prompt: str,
                               schema: type[T], *, params: dict | None = None) -> T: ...


def refused(llm: JsonLLM, reason: str) -> None:
    """Tell `llm` that code refused its last answer (it is asked again, or the step fails): an
    adapter with a `refused` method records it on the step's trace (wikiapp.llm.GatewayJsonLLM);
    goals/ itself never imports the LLM layer."""
    note = getattr(llm, "refused", None)
    if note is not None:
        note(reason)


class Dropped(BaseModel):
    """One LLM-proposed item the code refused, with why (for audit and the UI's counts)."""

    model_config = ConfigDict(frozen=True)

    reason: str
    item: dict[str, Any]


class RawObservation(BaseModel):
    """An observation as the LLM wrote it; nothing is trusted yet."""

    model_config = ConfigDict(extra="ignore")

    concept: str | None = None
    aspect: str | None = None
    cell: str | None = None
    polarity: str | None = None
    strength: float | None = None
    quote: str | None = None
    reason: str | None = None


def _clamp(x: float | None, default: float) -> float:
    """Clamp to [0, 1]; missing, NaN or infinite -> default."""
    if x is None or not math.isfinite(x):
        return default
    return max(0.0, min(1.0, float(x)))


def _cell_problem(cell: str | None, valid_cells: Collection[str]) -> str | None:
    if not cell or "." not in cell:
        return "unknown_cell"
    try:
        split_cell(cell)
    except ValueError:
        return "bad_aspect"
    return None if cell in valid_cells else "unknown_cell"


def self_observations(raw: Sequence[RawObservation], description: str,
                      valid_cells: Collection[str], *, evidence_ref: str
                      ) -> tuple[tuple[Observation, ...], tuple[Dropped, ...]]:
    """Keep a self-report observation only if its quote is a verbatim, non-empty piece of the
    user's description, its cell exists and its polarity is one of + - ~."""
    kept: list[Observation] = []
    dropped: list[Dropped] = []
    for r in raw:
        quote = (r.quote or "").strip()
        problem = ("quote_not_verbatim" if not quote or quote not in description
                   else _cell_problem(r.cell, valid_cells)
                   or (None if r.polarity in POLARITIES else "bad_polarity"))
        if problem:
            dropped.append(Dropped(reason=problem, item=r.model_dump(exclude_none=True)))
            continue
        kept.append(Observation(cell=r.cell, polarity=r.polarity,
                                strength=_clamp(r.strength, SELF_DEFAULT_STRENGTH),
                                kind="self", evidence_ref=evidence_ref, quote=quote))
    return tuple(kept), tuple(dropped)


ASPECT_LABEL = {Aspect.TERM: "术语/定义", Aspect.PURPOSE: "用途/何时用",
                Aspect.NOTATION: "公式/记号", Aspect.PROCEDURE: "计算过程",
                Aspect.INTERPRET: "解读结果", Aspect.MECHANISM: "原理/推导"}
BAND_LABEL = {"known": "确定会", "likely": "可能会", "shaky": "模糊", "gap": "不会",
              "none": "没证据"}


def k_text(kview: KView, concept_ids: Sequence[str],
           names: Mapping[str, str] | None = None) -> str:
    """The page's K as the LLM reads it (artifact v5 `kText`, without subitems)."""
    lines: list[str] = []
    for c in concept_ids:
        lines.append(f"- {c} {(names or {}).get(c, c)}")
        notes = kview.note(c)
        for a in kview.aspects(c):
            cell = f"{c}.{a.value}"
            b = kview.cell(cell)
            ev = "；".join([n.text for n in notes if n.cell == cell][-2:])
            lines.append(f"  - {cell} {ASPECT_LABEL[a]}：{BAND_LABEL[b.band.value]}，"
                         f"m={b.m:.2f} u={b.u:.2f}" + (f"（{ev}）" if ev else ""))
    return "\n".join(lines)
