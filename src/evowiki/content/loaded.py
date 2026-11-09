from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from evowiki.content.graph import ContentGraph
from evowiki.content.io import graph_from_document
from evowiki.content.relations import RelationCatalog, build_relation_catalog
from evowiki.content.segments import SegmentIndex


@dataclass(frozen=True)
class LoadedContent:
    """One published content version as the runtime sees it. The relation catalog is built
    once per load, so a bad catalog fails at load time rather than mid-request."""

    version_id: str
    graph: ContentGraph
    segments: SegmentIndex
    relations: RelationCatalog

    @classmethod
    def from_document(cls, doc: Mapping[str, Any]) -> "LoadedContent":
        cv = doc.get("content_version")
        if not cv or not cv.get("id"):
            raise ValueError("snapshot document lacks content_version.id")
        graph = graph_from_document(doc)
        return cls(cv["id"], graph,
                   SegmentIndex(list(graph.segments), list(graph.connections), graph),
                   build_relation_catalog(graph))
