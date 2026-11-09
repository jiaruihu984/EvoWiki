from datetime import UTC, datetime

import pytest

from evowiki.agent.tools import ToolContext
from evowiki.belief.aspects import AspectConfig, AspectK, DefineConcept, Observe
from evowiki.goals.contracts import (
    Aspect,
    Observation,
    OutlineSection,
    PageOutline,
    SectionRole,
    SectionState,
    SectionText,
)
from evowiki.goals.fold import Page
from evowiki.wikiapp.page_reader import InMemoryPageStore, WikiPage

T0 = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)


def make_page(page_id="p1", goal="学会 z-score", body2="z = (x - μ) / σ，" * 3) -> WikiPage:
    outline = PageOutline(
        page_id=page_id, goal=goal, concepts=("zscore", "stdev"), targets=("zscore",),
        sections=(
            OutlineSection(id="s1", heading="为什么要标准化", role=SectionRole.EXPLAIN,
                           cells=("zscore.purpose",)),
            OutlineSection(id="s2", heading="z-score 怎么算", role=SectionRole.EXPLAIN,
                           cells=("zscore.procedure", "stdev.notation")),
            OutlineSection(id="s3", heading="还没写的段", role=SectionRole.EXPLAIN,
                           cells=("zscore.interpret",)),
        ))
    texts = {
        "s1": SectionText(section_id="s1", cue="不同量纲放到一起比", body="不同单位的分数没法直接比较。",
                          cache_key="k1"),
        "s2": SectionText(section_id="s2", cue="减均值除标准差", body=body2, cache_key="k2"),
    }
    states = {"s1": SectionState(section_id="s1", folded=True),
              "s2": SectionState(section_id="s2"), "s3": SectionState(section_id="s3")}
    return WikiPage(page_id=page_id, title=goal,
                    page=Page(outline=outline, states=states, texts=texts))


@pytest.fixture
def page() -> WikiPage:
    return make_page()


@pytest.fixture
def store(page) -> InMemoryPageStore:
    other = make_page("p2", "学会方差", body2="方差是离均差平方的平均。")
    return InMemoryPageStore({"u1": [page, other], "u2": [make_page("p9", "别人的页")]})


@pytest.fixture
def ctx() -> ToolContext:
    return ToolContext(user_id="u1", session_id="sess1", page_id="p1")


@pytest.fixture
def aspect_k() -> AspectK:
    k = AspectK(AspectConfig(prior_a=0.6, prior_b=1.4))
    k.apply(DefineConcept(event_id="d1", ts=T0, concept_id="stdev",
                          aspects=(Aspect.TERM, Aspect.NOTATION)))
    k.apply(DefineConcept(event_id="d2", ts=T0, concept_id="zscore",
                          aspects=(Aspect.PURPOSE, Aspect.PROCEDURE, Aspect.INTERPRET),
                          prereqs=("stdev",)))
    k.apply(Observe(event_id="o1", ts=T0, observation=Observation(
        cell="zscore.procedure", polarity="+", strength=0.9, kind="quiz", evidence_ref="q1")))
    return k
