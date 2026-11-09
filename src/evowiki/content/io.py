import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from evowiki.content.graph import ContentGraph
from evowiki.content.models import (
    Concept,
    Connection,
    ContentVersion,
    Edge,
    MisconceptionFix,
    Passage,
    Perspective,
    QuizItem,
    Segment,
    Source,
)
from evowiki.content.segments import SegmentIndex


def graph_from_document(data: Mapping[str, Any]) -> ContentGraph:
    """Build a ContentGraph from an already-parsed snapshot document.

    Older documents without `misconception_fixes` load with an empty list.
    """
    cv = data.get("content_version")
    return ContentGraph(
        [Concept.model_validate(c) for c in data["concepts"]],
        [Edge.model_validate(e) for e in data["edges"]],
        [QuizItem.model_validate(q) for q in data.get("quiz_items", [])],
        sources=[Source.model_validate(s) for s in data.get("sources", [])],
        passages=[Passage.model_validate(p) for p in data.get("passages", [])],
        segments=[Segment.model_validate(s) for s in data.get("segments", [])],
        perspectives=[Perspective.model_validate(p) for p in data.get("perspectives", [])],
        connections=[Connection.model_validate(c) for c in data.get("connections", [])],
        misconception_fixes=[MisconceptionFix.model_validate(m)
                             for m in data.get("misconception_fixes", [])],
        content_version=ContentVersion.model_validate(cv) if cv else None,
    )


def load_graph(path: Path) -> ContentGraph:
    return graph_from_document(json.loads(Path(path).read_text(encoding="utf-8")))


def load_segments(path: Path, graph: ContentGraph) -> SegmentIndex:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return SegmentIndex(
        [Segment.model_validate(s) for s in data["segments"]],
        [Connection.model_validate(c) for c in data.get("connections", [])],
        graph,
    )
