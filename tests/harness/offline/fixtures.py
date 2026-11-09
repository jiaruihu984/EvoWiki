"""Shared bits for the offline-eval tests: a few cases and a DeepService on the in-memory world.
Scripted providers only; nothing reaches a real model."""

from dataclasses import replace

from evowiki.deep.store import MemoryAssetStore, MemoryJobStore
from evowiki.harness.offline.evalset import OfflineCase, RubricItem, profiles
from evowiki.wikiapp.refine_service import DeepService
from evowiki.wikiapp.refine_spec import WikiSpecSource
from tests.deep.fakes import (
    RULES,
    DeepScript,
    FakeChecker,
    finish,
    library_empty,
    section,
    turn,
)
from tests.wikiapp.test_goal import NOW, Ids

USER = "offline-u"


def case(i: int, topic: str, *, source: str = "C", said: str = "这段看不懂") -> OfflineCase:
    return OfflineCase(id=f"c{i}", topic=topic, source=source, heading=f"概念 {i}",
                       learner="零基础，学过高中数学", said="" if source == "D" else said,
                       original=f"概念 {i} 的旧讲解。", rubric=(RubricItem("先讲是什么", True),))


def service(repo, ds: DeepScript, cases=()) -> DeepService:
    """The writer's gateways carry the cases' profiles (`evalset.profiles`), as online."""
    llm = replace(ds.llm(), profile=profiles(cases, user_id=USER))
    return DeepService(repo=repo, jobs=MemoryJobStore(), assets=MemoryAssetStore(),
                       specs=WikiSpecSource(repo), checker=FakeChecker(), llm=llm,
                       clock=lambda: NOW, new_id=Ids(), library=library_empty())


def one_write(ds: DeepScript, body: str, i: int = 1) -> DeepScript:
    """Script one replay: a plan, one write of s1 with `body`, finish."""
    return ds.plan().write(turn(section("s1", i, body=body, heading="h", cue="线索")),
                           turn(finish(i, checks=[{"rule": r, "ok": True, "section_id": "s1", "note": ""}
                                                    for r in RULES])))
