import pytest

from evowiki.content.models import Connection, Lang, Segment, SegmentKind
from evowiki.content.segments import SegmentIndex


def _s(sid, cid="pca", kind="one_line", lang="en", status="human_approved"):
    return Segment(id=sid, concept_id=cid, kind=kind, lang=lang, review_status=status)


def test_renderable_sorted_and_excludes_flagged(segments):
    got = segments.renderable("pca", SegmentKind.DERIVATION, Lang.EN)
    assert [s.id for s in got] == ["pca.derivation.en"]
    ex = segments.renderable("pca", SegmentKind.EXAMPLE, Lang.EN)
    assert [s.id for s in ex] == ["pca.example.en.1", "pca.example.en.2", "pca.example.en.3"]


def test_renderable_prefers_requested_lang_then_falls_back_to_en(segments):
    zh = segments.renderable("pca", SegmentKind.ONE_LINE, Lang.ZH)
    assert [s.id for s in zh] == ["pca.one_line.zh"]
    fallback = segments.renderable("pca", SegmentKind.INTUITION, Lang.ZH)
    assert [s.id for s in fallback] == ["pca.intuition.en"]
    assert segments.renderable("svd", SegmentKind.DERIVATION, Lang.EN) == []


def test_prereq_segment_falls_back_to_definition(segments):
    assert segments.prereq_segment("eigenvalue", Lang.EN).id == "eigenvalue.one_line.en"
    assert segments.prereq_segment("eigenvalue", Lang.ZH).id == "eigenvalue.one_line.zh"
    # covariance's one_line is flagged, so its definition is used
    assert segments.prereq_segment("covariance", Lang.EN).id == "covariance.definition.en"


def test_prereq_segment_none_when_missing(graph):
    idx = SegmentIndex([], [], graph)
    assert idx.prereq_segment("pca", Lang.EN) is None


def test_connections_covering_excludes_flagged(segments):
    got = segments.connections_covering("svd", "pca")
    assert [c.segment_id for c in got] == ["conn.pca_svd.en"]
    assert segments.connections_covering("pca", "autoencoder") == []
    assert segments.connections_covering("pca", "lora") == []


def test_segment_lookup(segments):
    assert segments.segment("pca.one_line.en").concept_id == "pca"


def test_rejects_duplicate_segment_id(graph):
    with pytest.raises(ValueError, match="duplicate segment id: a"):
        SegmentIndex([_s("a"), _s("a")], [], graph)


def test_rejects_unknown_concept(graph):
    with pytest.raises(ValueError, match="unknown concept: nope"):
        SegmentIndex([_s("a", cid="nope")], [], graph)


def test_connection_must_reference_connection_segment(graph):
    with pytest.raises(ValueError, match="connection segment"):
        SegmentIndex([_s("a")], [Connection(segment_id="a", path=("pca", "svd"))], graph)
    with pytest.raises(ValueError, match="connection segment"):
        SegmentIndex([], [Connection(segment_id="missing", path=("pca", "svd"))], graph)


def test_connection_hops_must_be_edges(graph):
    seg = _s("c", kind="connection")
    with pytest.raises(ValueError, match="no edge between pca and lora"):
        SegmentIndex([seg], [Connection(segment_id="c", path=("pca", "lora"))], graph)
    # part_of edges do not count as hops
    with pytest.raises(ValueError, match="no edge"):
        SegmentIndex([seg], [Connection(segment_id="c", path=("vector", "linear_algebra"))], graph)
    with pytest.raises(ValueError, match="no edge"):
        SegmentIndex([seg], [Connection(segment_id="c", path=("pca", "nope"))], graph)
    SegmentIndex([seg], [Connection(segment_id="c", path=("pca", "svd", "lora"))], graph)


def test_rejects_duplicate_connection(graph):
    seg = _s("c", kind="connection")
    conn = Connection(segment_id="c", path=("pca", "svd"))
    with pytest.raises(ValueError, match="duplicate connection"):
        SegmentIndex([seg], [conn, conn], graph)
