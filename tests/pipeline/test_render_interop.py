"""A Plan 6 snapshot is directly consumable by the Plan 3 renderer's SegmentIndex."""

from evowiki.content.io import load_graph, load_segments
from evowiki.content.models import Lang, Segment, SegmentKind
from evowiki.pipeline.snapshot import snapshot
from tests.pipeline.helpers import NOW
from tests.pipeline.test_snapshot import _bundle


def test_segment_index_loads_a_pipeline_snapshot(tmp_path):
    bundle, q = _bundle()
    cv = snapshot(bundle, q, tmp_path / "versions", NOW)
    path = tmp_path / cv.snapshot_ref
    graph = load_graph(path)
    index = load_segments(path, graph)

    [en] = index.renderable("pca", SegmentKind.DEFINITION, Lang.EN)
    assert en == graph.segment("pca.definition.en")
    assert en.body == "x." and en.citations == ("p_pca1",)
    [zh] = index.renderable("pca", SegmentKind.DEFINITION, Lang.ZH)
    assert zh.id == "pca.definition.zh" and zh.translated_from == "pca.definition.en"
    # The flagged one_line segment was not published, so prereq refresh falls back to definition.
    assert index.prereq_segment("pca", Lang.EN) == en
    assert [c.segment_id for c in index.connections_covering("pca", "svd")] == ["conn.pca.svd.en"]


def test_pipeline_segment_dump_round_trips():
    bundle, _ = _bundle()
    for seg in bundle.segments:
        assert Segment.model_validate(seg.model_dump(mode="json")) == seg
