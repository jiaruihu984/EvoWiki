import pytest

from evowiki.content.models import CitedSentence, ReviewStatus, SegmentKind
from evowiki.pipeline import edges, segments, support
from evowiki.pipeline.edges import EdgeProposalBatch, propose_edges
from evowiki.pipeline.perspectives import PerspectiveDraftBatch, PresumeVerdicts
from evowiki.pipeline.quiz import QuizDraftBatch
from evowiki.pipeline.segments import SegmentDraftBatch, generate_base_segments
from evowiki.pipeline.skeleton import ConceptExpansion
from evowiki.pipeline.support import SupportVerdicts, vet_sentences
from evowiki.pipeline.translate import SentenceTranslations

GOOD_EDGE = {"src": "eigenvalue", "dst": "pca", "relation": "prerequisite_of",
             "kind": "formal", "strength": 0.9, "citations": ["p_pca1"], "rationale": "r"}
BAD_EDGE = {**GOOD_EDGE, "src": "matrix", "relation": "part_of", "kind": None}

# (batch class, items field, valid element, invalid element)
CASES = [
    (ConceptExpansion, "concepts",
     {"id": "linear_map", "name_en": "Linear Map", "name_zh": "线性映射", "domain": "la",
      "difficulty": 2, "passage_ids": ["p_mat"], "rationale": "r"},
     {"id": "Not An Id", "name_en": "x", "name_zh": "x", "domain": "la", "difficulty": 2,
      "passage_ids": ["p_mat"], "rationale": "r"}),
    (EdgeProposalBatch, "edges", GOOD_EDGE, BAD_EDGE),
    (SegmentDraftBatch, "segments",
     {"kind": "definition", "sentences": [{"text": "t", "citations": ["p_pca1"]}]},
     {"kind": "bogus", "sentences": [{"text": "t", "citations": ["p_pca1"]}]}),
    (PerspectiveDraftBatch, "perspectives",
     {"framing_label": "variance", "framing_phase": "ML",
      "sentences": [{"text": "t", "citations": ["p_pca2"]}]},
     {"framing_label": "", "sentences": []}),
    (PresumeVerdicts, "verdicts", {"concept_id": "variance", "level": "required"},
     {"concept_id": "variance", "level": "essential"}),
    (QuizDraftBatch, "items",
     {"stem": "s", "answer_key": "k", "kind": "recall", "difficulty": 2,
      "citations": ["p_pca1"]},
     {"stem": "s", "answer_key": "k", "kind": "recall", "difficulty": 9,
      "citations": ["p_pca1"]}),
    (SupportVerdicts, "verdicts", {"index": 0, "supported": True},
     {"index": "first", "supported": True}),
    (SentenceTranslations, "sentences", {"index": 0, "text": "向量"}, {"index": 0}),
]


@pytest.mark.parametrize(("batch", "field", "good", "bad"), CASES,
                         ids=[c[0].__name__ for c in CASES])
def test_one_invalid_element_keeps_the_valid_ones(batch, field, good, bad):
    out = batch.model_validate({field: [bad, good]})
    assert len(getattr(out, field)) == 1
    [invalid] = out.invalid_items
    assert invalid.index == 0 and invalid.error
    # the stored form round-trips (replay sees the same rejections)
    assert batch.model_validate(out.model_dump(mode="json")) == out
    # valid batches dump exactly as before and the provider schema is unchanged
    clean = batch.model_validate({field: [good]})
    assert set(clean.model_dump(mode="json")) == {field}
    assert set(batch.model_json_schema()["properties"]) == {field}


def test_propose_edges_keeps_valid_edges_and_reports_invalid_ones(make_ctx):
    ctx = make_ctx({(edges.PURPOSE, "pca"): {"edges": [BAD_EDGE, GOOD_EDGE]}})
    records, report = propose_edges(ctx, "generator", ctx.concepts["pca"])
    assert [(r.edge.src, r.edge.dst) for r in records] == [("eigenvalue", "pca")]
    [note] = report.dropped
    assert note.startswith("edges: invalid item 0: ")


def test_generate_segments_keeps_valid_segments_and_reports_invalid_ones(make_ctx):
    sentence = {"text": "PCA finds directions of maximal variance.", "citations": ["p_pca2"]}
    gen = {(segments.PURPOSE, "pca"): {"segments": [
        {"kind": "one_line", "sentences": [sentence]},
        {"kind": "bogus", "sentences": [sentence]},
        {"kind": "definition", "sentences": [sentence]}]}}
    ver = {(support.SUPPORT_PURPOSE, sid): {"verdicts": [
        {"index": "zero", "supported": False}, {"index": 0, "supported": True}]}
        for sid in ("pca.one_line.en", "pca.definition.en")}
    ctx = make_ctx(gen, ver)
    segs, report = generate_base_segments(ctx, ctx.concepts["pca"])
    assert {s.kind for s in segs} == {SegmentKind.ONE_LINE, SegmentKind.DEFINITION}
    assert all(s.review_status is ReviewStatus.AUTO_VERIFIED for s in segs)
    assert any(d.startswith("segments: invalid item 1: ") for d in report.dropped)
    assert any(d.startswith("pca.one_line.en support: invalid item 0: ")
               for d in report.dropped)


def test_invalid_support_verdict_does_not_flag_a_fully_supported_segment(make_ctx):
    ver = {(support.SUPPORT_PURPOSE, "subj"): {"verdicts": [
        {"index": 0, "supported": True}, {"supported": False}]}}
    ctx = make_ctx(ver_table=ver)
    vetted = vet_sentences(ctx, "subj", [support.DraftSentence(text="a", citations=["p_vec"])],
                           {"p_vec"})
    assert vetted.all_ok and vetted.kept == [CitedSentence(text="a", citations=["p_vec"])]
    assert vetted.notes and vetted.notes[0].startswith("support: invalid item 1: ")
