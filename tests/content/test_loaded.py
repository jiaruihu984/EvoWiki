import copy
import json
from pathlib import Path

import pytest

from evowiki.content.io import graph_from_document, load_graph
from evowiki.content.loaded import LoadedContent
from evowiki.content.models import Lang, SegmentKind
from evowiki.content.relations import RelationCatalogError, build_relation_catalog
from tests.fixtures.devcontent import dev_document

SNAP = Path(__file__).resolve().parents[1] / "fixtures" / "runtime" / "content_snapshot.json"


def _doc() -> dict:
    return json.loads(SNAP.read_text(encoding="utf-8"))


def test_snapshot_is_up_to_date():
    from tests.fixtures.runtime.build_snapshot import build
    assert json.loads(json.dumps(build(), sort_keys=True)) == _doc()


def test_loaded_content_from_document():
    lc = LoadedContent.from_document(_doc())
    assert lc.version_id == "cv_runtimefixture"
    assert lc.graph.segments and all(s.sentences for s in lc.graph.segments)
    assert lc.segments.renderable("pca", SegmentKind.ONE_LINE, Lang.EN)
    assert all(q.answer_key for q in (lc.graph.item(i) for i in lc.graph.item_ids))


def test_load_graph_delegates(tmp_path):
    p = tmp_path / "g.json"
    p.write_text(SNAP.read_text(encoding="utf-8"), encoding="utf-8")
    assert load_graph(p).concept_ids == graph_from_document(_doc()).concept_ids


def test_neighbors_and_accessors():
    g = graph_from_document(_doc())
    assert "eigenvalue" in g.neighbors("pca") or "covariance" in g.neighbors("pca")
    assert list(g.segments) == sorted(g.segments, key=lambda s: s.id)


def test_from_document_requires_version():
    d = _doc()
    del d["content_version"]
    with pytest.raises(ValueError, match="content_version"):
        LoadedContent.from_document(d)


def test_relation_catalog_is_built_once_at_load():
    lc = LoadedContent.from_document(_doc())
    assert lc.relations.entries == build_relation_catalog(lc.graph).entries


def test_bad_relation_catalog_fails_at_load_time():
    doc = copy.deepcopy(dev_document())
    doc.setdefault("content_version", {"id": "cv_bad"})
    for p in doc["perspectives"]:
        if p["segment_id"] == "pca.perspective.linear_autoencoder.en":
            p["covers_relation_ids"] = ["rel:lora~neural_network"]   # no published connection
            p["covers_review_status"] = "human_approved"
    with pytest.raises(RelationCatalogError):
        LoadedContent.from_document(doc)
