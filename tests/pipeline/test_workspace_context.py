import pytest

from evowiki.content.models import Concept
from evowiki.pipeline.context import StageContext
from evowiki.pipeline.review import ReviewQueue
from evowiki.pipeline.sources import IneligibleSourceError
from evowiki.pipeline.workspace import Workspace
from tests.pipeline.helpers import FIXTURE_SOURCE, MINI_CONCEPTS, MINI_PASSAGES


def test_workspace_round_trips_models_and_passages(tmp_path):
    ws = Workspace(tmp_path / "content")
    assert ws.load("core", Concept) == []
    ws.save("core", MINI_CONCEPTS)
    assert ws.load("core", Concept) == MINI_CONCEPTS
    ws.save_passages(MINI_PASSAGES)
    assert sorted(ws.load_passages(), key=lambda p: p.id) == sorted(MINI_PASSAGES,
                                                                    key=lambda p: p.id)
    assert not list((tmp_path / "content").glob("*.tmp"))
    assert ws.versions_dir == tmp_path / "content" / "versions"


def test_context_rejects_passages_from_ineligible_sources(make_ctx):
    unverified = FIXTURE_SOURCE.model_copy(update={"license_verified_at": None})
    ctx = make_ctx()
    with pytest.raises(IneligibleSourceError):
        StageContext.build(ctx.gateway, ReviewQueue(), MINI_CONCEPTS, MINI_PASSAGES,
                           [unverified])


def test_context_retrieve_uses_concept_names(make_ctx):
    ctx = make_ctx()
    hits = ctx.retrieve([ctx.concepts["pca"]])
    assert hits[0].id in {"p_pca1", "p_pca2"}
    assert ctx.report("x").stage == "x"
