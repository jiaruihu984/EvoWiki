"""Contract between Plan 4 and Plan 5 on graph_from_document.

Plan 5 snapshots predate misconception_fixes; Plan 4 documents carry them.
Both must load through the same entry point, and load_graph(path) must
delegate to graph_from_document.
"""
import json
from pathlib import Path
from types import MappingProxyType

from evowiki.content.io import graph_from_document, load_graph
from tests.fixtures.devcontent import dev_document

SNAPSHOT = Path(__file__).parent.parent / "fixtures" / "runtime" / "content_snapshot.json"


def _summary(g):
    return (
        g.concept_ids,
        g.edges,
        g.item_ids,
        g.segments,
        g.connections,
        g.misconception_fixes(),
        g.content_version,
    )


def test_old_document_without_fixes_loads_with_empty_fixes():
    doc = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert "misconception_fixes" not in doc
    g = graph_from_document(doc)
    assert g.concept_ids
    assert g.misconception_fixes() == ()


def test_old_document_accepts_any_mapping():
    doc = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    g = graph_from_document(MappingProxyType(doc))
    assert _summary(g) == _summary(graph_from_document(doc))


def test_load_graph_matches_graph_from_document_for_old_file():
    doc = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert _summary(load_graph(SNAPSHOT)) == _summary(graph_from_document(doc))


def test_new_document_with_fixes_loads_them():
    doc = dev_document()
    assert doc["misconception_fixes"]
    g = graph_from_document(doc)
    fixes = g.misconception_fixes()
    assert fixes
    assert {f.segment_id for f in fixes} == {m["segment_id"] for m in doc["misconception_fixes"]}
