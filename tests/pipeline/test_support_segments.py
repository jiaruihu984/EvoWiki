import copy
from dataclasses import replace

from evowiki.content.models import CitedSentence, ReviewStatus, SegmentKind
from evowiki.llm.gateway import Gateway
from evowiki.llm.log import CallLog
from evowiki.llm.scripted import ScriptedProvider
from evowiki.pipeline.review import ReviewState, ReviewTarget
from evowiki.pipeline.segments import (
    PURPOSE,
    REPAIR_PROMPT_VERSION,
    REPAIR_PURPOSE,
    generate_base_segments,
)
from evowiki.pipeline.support import (
    SUPPORT_PURPOSE,
    segment_id,
    verify_support,
    zh_id,
)
from tests.pipeline.helpers import GW_CONFIG, NOW, respond


def _s(text, *cites):
    return {"text": text, "citations": list(cites)}


GEN = {(PURPOSE, "pca"): {"segments": [
    {"kind": "one_line", "sentences": [_s("PCA finds directions of maximal variance.", "p_pca2")]},
    {"kind": "definition", "sentences": [
        _s("PCA projects data onto top eigenvectors of the covariance matrix.", "p_pca1"),
        _s("PCA was invented in 1901.", "p_zzz")]},
    {"kind": "intuition", "sentences": [_s("PCA always improves accuracy.", "p_pca2")]},
    {"kind": "one_line", "sentences": [_s("Duplicate.", "p_pca2")]},
    {"kind": "misconception_fix", "sentences": [_s("Not a base kind.", "p_pca2")]},
]}}
VER = {
    (SUPPORT_PURPOSE, "pca.one_line.en"): {"verdicts": [{"index": 0, "supported": True}]},
    (SUPPORT_PURPOSE, "pca.definition.en"): {"verdicts": [{"index": 0, "supported": True}]},
    (SUPPORT_PURPOSE, "pca.intuition.en"): {"verdicts": [{"index": 0, "supported": False,
                                                          "reason": "overclaim"}]},
}


def test_ids():
    assert segment_id("pca", SegmentKind.DEFINITION) == "pca.definition.en"
    assert segment_id("pca", SegmentKind.PERSPECTIVE, slug="variance") == \
        "pca.perspective.variance.en"
    assert zh_id("pca.definition.en") == "pca.definition.zh"


def test_generate_base_segments_vets_every_sentence(make_ctx):
    ctx = make_ctx(GEN, VER)
    segs, report = generate_base_segments(ctx, ctx.concepts["pca"])
    by_kind = {s.kind: s for s in segs}
    assert set(by_kind) == {SegmentKind.ONE_LINE, SegmentKind.DEFINITION}
    assert by_kind[SegmentKind.ONE_LINE].review_status is ReviewStatus.AUTO_VERIFIED
    definition = by_kind[SegmentKind.DEFINITION]
    assert definition.review_status is ReviewStatus.FLAGGED
    assert [s.citations for s in definition.sentences] == [["p_pca1"]]
    assert definition.license_obligations.attribution == ["fx"]
    [item] = ctx.queue.items(target=ReviewTarget.SEGMENT)
    assert item.target_id == "pca.definition.en" and item.reason == "unsupported_sentences"
    dropped = "\n".join(report.dropped)
    assert "pca.intuition.en: no supported sentences" in dropped
    assert "citation outside retrieved passages" in dropped
    assert "duplicate or non-base kind" in dropped
    assert report.created == ["pca.one_line.en"] and report.flagged == ["pca.definition.en"]
    assert len(report.llm_call_ids) == 4


ONLY_ONE_LINE = {(PURPOSE, "pca"): {"segments": [GEN[(PURPOSE, "pca")]["segments"][0]]}}


def test_missing_required_kind_is_repaired(make_ctx):
    seen = []

    def repair(req):
        seen.append(req)
        return {"segments": [
            {"kind": "definition", "sentences": [
                _s("PCA projects data onto top eigenvectors of the covariance matrix.",
                   "p_pca1")]},
            {"kind": "one_line", "sentences": [_s("Not requested.", "p_pca2")]}]}

    ctx = make_ctx({**ONLY_ONE_LINE, (REPAIR_PURPOSE, "pca"): repair}, VER)
    segs, report = generate_base_segments(ctx, ctx.concepts["pca"])
    by_kind = {s.kind: s for s in segs}
    assert set(by_kind) == {SegmentKind.ONE_LINE, SegmentKind.DEFINITION}
    assert by_kind[SegmentKind.ONE_LINE].body == "PCA finds directions of maximal variance."
    assert by_kind[SegmentKind.DEFINITION].review_status is ReviewStatus.AUTO_VERIFIED
    assert not any("missing required kind" in d for d in report.dropped)
    assert "pca.one_line: duplicate or not requested by repair" in report.dropped
    assert len(report.llm_call_ids) == 4          # generate, support, repair, support
    [req] = seen
    assert req.prompt_version == REPAIR_PROMPT_VERSION == "segments.repair.v1"
    assert "Write only these segments: definition." in req.prompt
    assert "[p_pca1]" in req.prompt


def test_repair_is_skipped_when_nothing_is_missing(make_ctx):
    ctx = make_ctx(GEN, VER)   # no repair response scripted: a repair call would raise
    segs, report = generate_base_segments(ctx, ctx.concepts["pca"])
    assert {s.kind for s in segs} == {SegmentKind.ONE_LINE, SegmentKind.DEFINITION}
    assert not any("missing required kind" in d for d in report.dropped)


def test_failed_repair_keeps_missing_note(make_ctx):
    repair = {"segments": [{"kind": "definition", "sentences": [
        _s("PCA was invented by aliens.", "p_zzz")]}]}   # cites outside retrieved passages
    ctx = make_ctx({**ONLY_ONE_LINE, (REPAIR_PURPOSE, "pca"): repair}, VER)
    segs, report = generate_base_segments(ctx, ctx.concepts["pca"])
    assert {s.kind for s in segs} == {SegmentKind.ONE_LINE}
    assert "pca: missing required kind definition" in report.dropped

    ctx = make_ctx({**ONLY_ONE_LINE, (REPAIR_PURPOSE, "pca"): {"segments": []}}, VER)
    _, report = generate_base_segments(ctx, ctx.concepts["pca"])
    assert "pca: missing required kind definition" in report.dropped


def test_spot_checks_sample_auto_segments(make_ctx):
    ctx = make_ctx(GEN, VER, spot=1.0)
    generate_base_segments(ctx, ctx.concepts["pca"])
    spots = [i.target_id for i in ctx.queue.items(target=ReviewTarget.SEGMENT) if i.spot_check]
    assert spots == ["pca.one_line.en"]


def test_verify_support_missing_index_counts_as_unsupported(make_ctx):
    ctx = make_ctx(ver_table={(SUPPORT_PURPOSE, "subj"): {"verdicts": [
        {"index": 1, "supported": True}]}})
    flags, call_id = verify_support(ctx, "subj", [CitedSentence(text="a", citations=["p_vec"]),
                                                  CitedSentence(text="b", citations=["p_vec"])])
    assert flags == [False, True] and call_id.startswith("llm_")
    assert verify_support(ctx, "empty", []) == ([], None)


def _fresh_gateway(path, gen_table):
    return Gateway(GW_CONFIG, CallLog(path), mode="record",
                   providers={"gen": ScriptedProvider("gen", respond(gen_table)),
                              "ver": ScriptedProvider("ver", respond(VER))},
                   clock=lambda: NOW)


def test_human_approval_binds_to_segment_content(make_ctx, tmp_path):
    ctx = make_ctx(GEN, VER)
    generate_base_segments(ctx, ctx.concepts["pca"])
    [item] = ctx.queue.items(target=ReviewTarget.SEGMENT)
    ctx.queue.decide(item.id, ReviewState.APPROVED, decided_by="h", now=NOW)

    same = replace(ctx, gateway=_fresh_gateway(tmp_path / "same.jsonl", GEN))
    segs, _ = generate_base_segments(same, same.concepts["pca"])
    by_kind = {s.kind: s for s in segs}
    assert by_kind[SegmentKind.DEFINITION].review_status is ReviewStatus.HUMAN_APPROVED

    changed = copy.deepcopy(GEN)
    changed[(PURPOSE, "pca")]["segments"][1]["sentences"][0]["text"] = \
        "PCA projects data onto the leading eigenvectors of the covariance matrix."
    new = replace(ctx, gateway=_fresh_gateway(tmp_path / "changed.jsonl", changed))
    segs, _ = generate_base_segments(new, new.concepts["pca"])
    by_kind = {s.kind: s for s in segs}
    assert by_kind[SegmentKind.DEFINITION].review_status is ReviewStatus.FLAGGED
    open_items = ctx.queue.items(state=ReviewState.OPEN, target=ReviewTarget.SEGMENT)
    assert [i.target_id for i in open_items] == ["pca.definition.en"]
