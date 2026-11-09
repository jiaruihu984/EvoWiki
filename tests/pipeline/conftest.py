import pytest

from evowiki.content.models import (
    CitedSentence,
    Lang,
    LicenseObligations,
    ReviewStatus,
    Segment,
    SegmentKind,
)
from evowiki.llm.gateway import Gateway
from evowiki.llm.log import CallLog
from evowiki.llm.scripted import ScriptedProvider
from evowiki.pipeline.context import StageContext
from evowiki.pipeline.review import ReviewQueue
from tests.pipeline.helpers import (
    FIXTURE_SOURCE,
    GW_CONFIG,
    MINI_CONCEPTS,
    MINI_PASSAGES,
    NOW,
    Table,
    respond,
)


@pytest.fixture
def make_ctx(tmp_path):
    def _make(gen_table: Table | None = None, ver_table: Table | None = None, *,
              concepts=None, passages=None, spot: float = 0.0) -> StageContext:
        gateway = Gateway(
            GW_CONFIG, CallLog(tmp_path / "llm_calls.jsonl"), mode="record",
            providers={"gen": ScriptedProvider("gen", respond(gen_table or {})),
                       "ver": ScriptedProvider("ver", respond(ver_table or {}))},
            clock=lambda: NOW)
        return StageContext.build(
            gateway, ReviewQueue(), concepts or MINI_CONCEPTS, passages or MINI_PASSAGES,
            [FIXTURE_SOURCE], spot_check_fraction=spot)

    return _make


@pytest.fixture
def seg_factory():
    def _make(sid: str, concept_id: str, cite: str = "p_pca2") -> Segment:
        text = next(p.text for p in MINI_PASSAGES if p.id == cite)
        return Segment(id=sid, concept_id=concept_id, kind=SegmentKind.PERSPECTIVE, lang=Lang.EN,
                       sentences=[CitedSentence(text=text, citations=[cite])],
                       license_obligations=LicenseObligations(
                           attribution=["fx"], share_alike=False, non_commercial=False),
                       review_status=ReviewStatus.AUTO_VERIFIED)

    return _make
