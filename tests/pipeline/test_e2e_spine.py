import json
import os
import shutil
from pathlib import Path

import pytest

from evowiki.content.io import load_graph
from evowiki.content.models import Lang, ReviewStatus, Segment, SegmentKind
from evowiki.content.relations import build_relation_catalog
from evowiki.llm.gateway import Gateway
from evowiki.llm.scripted import ScriptedProvider
from evowiki.pipeline.config import PipelineConfig
from evowiki.pipeline.review import ReviewState, ReviewTarget
from evowiki.pipeline.run import run_pipeline, snapshot_workspace
from evowiki.pipeline.sources import SourceRegistration
from evowiki.pipeline.workspace import Workspace
from tests.pipeline.helpers import FIXTURE_SOURCE, GW_CONFIG, MINI_CONCEPTS, MINI_PASSAGES, NOW
from tests.pipeline.spine_script import SpineScript

FIXTURE_LOG = Path(__file__).resolve().parents[1] / "fixtures" / "pipeline" / "llm_calls.jsonl"
CONFIG = PipelineConfig(gateway=GW_CONFIG, spot_check_fraction=0.0,
                        demo_spine=[c.id for c in MINI_CONCEPTS],
                        spine_paths=[["pca", "svd", "lora"]], perspective_concepts=["pca"])


def _workspace(root: Path) -> Workspace:
    ws = Workspace(root)
    ws.save_registry([SourceRegistration(source=FIXTURE_SOURCE, license_marker="CC BY 4.0")])
    ws.save("core", MINI_CONCEPTS)
    ws.save_passages(MINI_PASSAGES)
    ws.save("glossary", [])
    return ws


def _human_approves_core_keys(ws: Workspace) -> None:
    q = ws.queue()
    for item in q.items(state=ReviewState.OPEN, target=ReviewTarget.QUIZ_ANSWER):
        if item.reason == "core_answer_key":
            q.decide(item.id, ReviewState.APPROVED, decided_by="fixture-reviewer", now=NOW)


def _assert_spine_graph(path: Path) -> None:
    g = load_graph(path)
    assert set(g.concept_ids) == {c.id for c in MINI_CONCEPTS}
    assert "svd" in g.prerequisite_closure("lora", 1)
    assert {"eigenvalue", "covariance"} <= g.prerequisite_closure("pca", 1)
    kinds = {s.kind for s in g.segments_for("pca")}
    assert {SegmentKind.ONE_LINE, SegmentKind.DEFINITION, SegmentKind.PERSPECTIVE} <= kinds
    assert g.segments_for("pca", Lang.ZH)
    views = {p.framing_label: p for p in g.perspectives_for("pca")}
    assert set(views) == {"Variance maximization", "Linear autoencoder"}
    assert "variance" in views["Variance maximization"].presumes_required
    pca_views = g.perspectives_for("pca")
    assert any(p.covers_relation_ids for p in pca_views)
    cat = build_relation_catalog(g)
    assert all(set(p.covers_relation_ids) <= set(cat.ids()) for p in pca_views)
    assert views["Linear autoencoder"].covers_relation_ids == ["rel:autoencoder~pca"]
    assert {v: p.framing_phase for v, p in views.items()} == {
        "Variance maximization": "ML", "Linear autoencoder": "DL"}
    assert all(p.framing_phase_status is ReviewStatus.AUTO_VERIFIED for p in pca_views)
    assert "conn.pca.svd.lora.en" in {c.segment_id for c in g.connections_touching("lora")}
    for cid in g.concept_ids:
        for seg in [*g.segments_for(cid), *g.segments_for(cid, Lang.ZH)]:
            assert all(s.citations for s in seg.sentences)
    items = [g.item(q) for q in _quiz_ids(path)]
    assert any(q.core for q in items)
    assert all(q.answer_verified_by_human for q in items if q.core)
    assert any(q.tests_connection_id for q in items)
    assert {q.transfer_kind for q in items if q.tests_connection_id} == {"seen", "near"}


def _quiz_ids(path: Path) -> list[str]:
    return [q["id"] for q in json.loads(path.read_text(encoding="utf-8"))["quiz_items"]]


def test_record_then_offline_replay_yield_identical_snapshot(tmp_path):
    rec = _workspace(tmp_path / "rec")
    script = SpineScript()
    gw = Gateway(GW_CONFIG, rec.call_log(), mode="record",
                 providers={"gen": ScriptedProvider("gen", script.responder),
                            "ver": ScriptedProvider("ver", script.responder)},
                 clock=lambda: NOW)
    reports = run_pipeline(rec, gw, CONFIG, NOW)
    assert any(r.stage == "edges" for r in reports)
    _human_approves_core_keys(rec)
    cv = snapshot_workspace(rec, CONFIG, NOW)
    _assert_spine_graph(rec.root / cv.snapshot_ref)

    if os.environ.get("EVOWIKI_RECORD_FIXTURES") == "1":
        FIXTURE_LOG.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(rec.root / "llm_calls.jsonl", FIXTURE_LOG)
    if not FIXTURE_LOG.exists():
        pytest.fail("missing recorded fixture; run once with EVOWIKI_RECORD_FIXTURES=1")

    rep = _workspace(tmp_path / "rep")
    shutil.copyfile(FIXTURE_LOG, rep.root / "llm_calls.jsonl")
    offline = Gateway(GW_CONFIG, rep.call_log(), mode="replay")  # no providers at all
    run_pipeline(rep, offline, CONFIG, NOW)
    _human_approves_core_keys(rep)
    assert snapshot_workspace(rep, CONFIG, NOW).id == cv.id


def test_spine_subset_and_unknown_concept(tmp_path):
    ws = _workspace(tmp_path / "w")
    shutil.copyfile(FIXTURE_LOG, ws.root / "llm_calls.jsonl")
    offline = Gateway(GW_CONFIG, ws.call_log(), mode="replay")
    with pytest.raises(ValueError, match="unknown concept"):
        run_pipeline(ws, offline, CONFIG, NOW, concept_ids=["nope"])


class _RegeneratedVectorScript(SpineScript):
    """Second-run script: vector.definition comes back different and partly unsupported."""

    def _segments_generate(self, req):
        out = super()._segments_generate(req)
        if req.subject == "vector":
            out["segments"][1]["sentences"].reverse()
        return out

    def _support_verify(self, req):
        out = super()._support_verify(req)
        if req.subject == "vector.definition.en":
            out["verdicts"][0]["supported"] = False
        return out


def _record_gateway(ws: Workspace, script: SpineScript) -> Gateway:
    return Gateway(GW_CONFIG, ws.call_log(), mode="record",
                   providers={"gen": ScriptedProvider("gen", script.responder),
                              "ver": ScriptedProvider("ver", script.responder)},
                   clock=lambda: NOW)


def test_regenerated_en_segment_drops_stale_zh(tmp_path):
    ws = _workspace(tmp_path / "w")
    run_pipeline(ws, _record_gateway(ws, SpineScript()), CONFIG, NOW, concept_ids=["vector"])
    before = {s.id: s for s in ws.load("segments", Segment)}
    assert {"vector.definition.zh", "vector.one_line.zh"} <= set(before)

    (ws.root / "llm_calls.jsonl").unlink()  # e.g. a prompt bump: the model answers anew
    run_pipeline(ws, _record_gateway(ws, _RegeneratedVectorScript()), CONFIG, NOW,
                 concept_ids=["vector"])
    after = {s.id: s for s in ws.load("segments", Segment)}
    assert after["vector.definition.en"].review_status is ReviewStatus.FLAGGED
    assert after["vector.definition.en"].sentences != before["vector.definition.en"].sentences
    assert "vector.definition.zh" not in after  # stale translation of the old en is gone
    assert after["vector.one_line.zh"] == before["vector.one_line.zh"]  # unchanged en keeps zh
