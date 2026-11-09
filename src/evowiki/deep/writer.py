"""The page writer (single writer, 2026-10-10): every page and every section rewrite is written by the deep
agent (`deep.agent.refine`) — Claude when an Anthropic key is configured, DeepSeek's good tier as the
fallback (or the only writer without that key). This module runs one write: it builds the writers from the
deep tier (`DeepLLM.gateways`: primary + fallback, one JobMeter on the job's budget, `llm.budget`),
runs the agent with the caller's listeners and returns the outcome with what it cost and who wrote it.
It never touches the wiki or the asset store; callers commit (`wikiapp.page_flows`, `wikiapp.kchange`).
`practice` runs a practice-mode run (`deep.agent.practice`: one interactive for a written page, the
assistant's add_practice) on the same writers and budget.

Fail closed: no deep tier → `RefineFailed("llm_unavailable")`; every failure is a `RefineFailed` carrying
the spend so far (`cost_usd`) and the writer that was working (`model`)."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event
from typing import TYPE_CHECKING

from evowiki.deep.agent import (
    Change,
    PracticeOutcome,
    RefineFailed,
    RefineInput,
    RefineOutcome,
    model_of,
    practice,
    refine,
)
from evowiki.deep.images import ImageSearch
from evowiki.deep.library import ComponentLibrary
from evowiki.deep.llm import GROUP, DeepLLM, forge_opener
from evowiki.deep.tools import Checker, Outlined
from evowiki.llm.budget import JobBudget
from evowiki.llm.usage import Account
from evowiki.sources.reader import SourceReader

if TYPE_CHECKING:
    from evowiki.forge.run import ForgeRuntime

log = logging.getLogger("evowiki.deep.writer")

WriterListener = Callable[[str, bool, str], None]     # (model, fallback, reason)


@dataclass
class WriteResult:
    outcome: RefineOutcome
    cost_usd: float
    model: str                  # the writer that finished ("provider:model")
    fallback_reason: str        # "" = the primary writer wrote it
    calls: int                  # LLM calls the agent made


@dataclass
class PracticeResult:
    outcome: PracticeOutcome
    cost_usd: float
    model: str                  # the writer that finished ("provider:model")
    calls: int


class WriteFailed(RefineFailed):
    """A RefineFailed with the spend and the writer at the time it stopped."""

    def __init__(self, inner: RefineFailed, cost_usd: float, model: str):
        super().__init__(inner.failure, inner.detail, inner.draft)
        self.cost_usd, self.model = cost_usd, model


@dataclass
class PageWriter:
    llm: DeepLLM | None                       # None: no deep tier configured → llm_unavailable
    checker: Checker
    library: ComponentLibrary | None = None
    forge: "ForgeRuntime | None" = None       # forged tools (coding-agent RSI phase 2); None = none
    images: ImageSearch | None = None         # find_image / place_image; None = no image tools

    @property
    def model(self) -> str:
        return self.llm.model if self.llm is not None else ""

    def write(self, inp: RefineInput, *, budget: JobBudget | None = None,
              on_writer: WriterListener = lambda model, fallback, reason: None,
              on_change: Change | None = None,
              on_spend: Callable[[float, int], None] | None = None,
              on_outline: Outlined | None = None,
              cancel: Event | None = None,
              reader: SourceReader | None = None) -> WriteResult:
        """Raises WriteFailed (cost, model) on any failure. `on_spend(cost so far, calls so far)`
        is called whenever a section changes (what the run's row shows while it writes).
        `reader`: the user's materials for the writer's read_source / search_source tools (None:
        no material tools). `budget`: the job's own budget (「每页最多花」; a page job shares it
        with every call of the job), None = no cap; the result's cost is what this write spent.
        `on_outline`: the page plan_page outlined (`refine`)."""
        gws, roster, meter, spent, who, note = self._writers(inp, budget, on_writer)
        calls: list = []
        listener = on_change
        if on_spend is not None:
            def listener(ws, sid) -> None:      # the spend so far, then the section
                on_spend(spent(), len(calls))
                if on_change is not None:
                    on_change(ws, sid)
        try:
            outcome = refine(inp, gws[0], Account(user_id=inp.user_id, group=GROUP), self.checker,
                             calls=calls, library=self.library, fallbacks=gws[1:],
                             on_writer=note, on_change=listener, cancel=cancel, reader=reader,
                             trace=self.llm.trace, roster=roster,
                             forge=forge_opener(self.forge, inp, meter.charge),
                             images=self.images, on_outline=on_outline)
        except RefineFailed as exc:
            raise WriteFailed(exc, spent(), who["model"]) from exc
        return WriteResult(outcome=outcome, cost_usd=spent(), model=who["model"],
                           fallback_reason=who["reason"], calls=len(calls))

    def practice(self, inp: RefineInput, *, budget: JobBudget | None = None,
                 on_writer: WriterListener = lambda model, fallback, reason: None,
                 cancel: Event | None = None) -> PracticeResult:
        """A practice run (`deep.agent.practice`, `inp.mode == "practice"`) on the same writers,
        job budget and meter as `write`. Raises WriteFailed (cost, model) on any failure."""
        gws, roster, _meter, spent, who, note = self._writers(inp, budget, on_writer)
        calls: list = []
        try:
            outcome = practice(inp, gws[0], Account(user_id=inp.user_id, group=GROUP), self.checker,
                               calls=calls, library=self.library, fallbacks=gws[1:],
                               on_writer=note, cancel=cancel, trace=self.llm.trace,  # type: ignore[union-attr]
                               roster=roster, images=self.images)
        except RefineFailed as exc:
            raise WriteFailed(exc, spent(), who["model"]) from exc
        return PracticeResult(outcome=outcome, cost_usd=spent(), model=who["model"],
                              calls=len(calls))

    def _writers(self, inp: RefineInput, budget: JobBudget | None, on_writer: WriterListener):
        """(writers, roster or None, meter, spent(), who, note): the deep tier's writers for the
        user on the job's budget (None = no cap), what this run spent so far, and the listener
        that records the writer at every switch. WriteFailed without a deep tier."""
        if self.llm is None:
            raise WriteFailed(RefineFailed("llm_unavailable", "没有配置写作模型"), 0.0, "")
        roster: dict | None = None
        budget = budget if budget is not None else JobBudget()
        spent0 = budget.spent              # what the job spent before (its planning calls)
        if self.llm.tiers is not None:      # the pack's H10 step tiers and hook steps
            roster, meter = self.llm.roster(None, inp.user_id, budget=budget)
            gws = roster["deep"]
        else:
            gws, meter = self.llm.gateways(None, inp.user_id, budget=budget)

        def spent() -> float:
            return round(meter.spent - spent0, 6)
        who = {"model": model_of(gws[0]), "reason": ""}

        def note(model: str, fallback: bool, reason: str) -> None:
            who["model"] = model
            if fallback:
                who["reason"] = reason
            on_writer(model, fallback, reason)
        return gws, roster, meter, spent, who, note
