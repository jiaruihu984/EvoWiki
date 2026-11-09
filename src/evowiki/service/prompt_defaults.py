"""The default guidance of every model step (plan 2026-10-21 参数 redesign, D3): the text between a
step's markers in the prompt of its `prompt_of` purpose, rendered by the module's own builder with
the 〈…〉 placeholders `service/engine.py` uses, so it is exactly what the model reads when the user
has no override."""

from collections.abc import Callable
from functools import cache

from evowiki.assistant import compact as compact_mod
from evowiki.assistant import loop as loop_mod
from evowiki.deep import agent as deep_agent
from evowiki.goals import path
from evowiki.goals.intake import IntakeConcept, IntakeResult
from evowiki.llm.prompt_steps import STEPS, marked
from evowiki.service import engine
from evowiki.sources import match
from evowiki.wikiapp import graph_groups


def _path() -> str:
    goal = IntakeResult(goal=engine.GOAL, title=engine.GOAL,
                        concepts=(IntakeConcept(id="c1", name=engine.CONCEPT),), targets=("c1",),
                        observations=(), dropped=(), resolutions=())
    return path.path_prompt(goal, {})




def _source_match() -> str:
    cand = match.Candidate(page_id="p1", title="〈页面标题〉", text="〈页面的开头〉", score=0.5,
                           sections=(("s1", "〈资料的一节〉", "〈这一节的开头〉"),))
    return match._prompt("〈资料的标题〉", [cand])


RENDER: dict[str, Callable[[], str]] = {
    "decompose": engine._enter,
    "source_outline": lambda: engine._intake((engine.SOURCE,)),
    "source_reference": lambda: engine._intake((engine.SOURCE,), mode="reference"),
    "understand": lambda: engine._intake((engine.SOURCE,), "understand"),
    "blocks": engine._blocks,
    "page_fit": engine._page_fit,
    "reading_unit": engine._reading_unit,
    "path": _path,
    "outline": engine._outline,
    "write": lambda: engine._deep(deep_agent.WRITE_SYSTEM),
    "check": engine._outline,
    "followup": engine._followup,
    "card": engine._card_cue,
    "assistant": lambda: loop_mod.SYSTEM,
    "compact": lambda: compact_mod.COMPACT_SYSTEM,
    "feedback": engine._understand,
    "graph_group_naming": lambda: graph_groups.naming_request(()).prompt,
    "source_match": _source_match,
}


def _default(key: str, text: str) -> str:
    out = marked(text).get(key, "").strip()
    if not out:
        raise LookupError(f"step {key!r} has no marked guidance")
    return out


@cache
def default_texts() -> dict[str, str]:
    """Step key → its default guidance (unmarked), the same for every user. Raises when a step
    has no marked span (a builder lost its markers): the prompt settings must never show an
    empty default."""
    return {s.key: _default(s.key, RENDER[s.key]()) for s in STEPS}
