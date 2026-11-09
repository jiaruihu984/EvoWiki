"""Unified node graph (spec 2026-10-13): one node kind, typed relations, evidence, placement,
automatic groups and structural changes with undo. Pure: no IO, no LLM."""

from evowiki.nodes.evidence import (
    Mix,
    NodeEvidence,
    aggregate_ids,
    band_of,
    mix_of,
    node_evidence,
    shares,
)
from evowiki.nodes.groups import Group, cluster, member_key
from evowiki.nodes.model import (
    GraphChange,
    GraphError,
    NodeGraph,
    NodeRec,
    Relation,
    RelKind,
    RelSource,
    validate,
)
from evowiki.nodes.ops import (
    ChildSpec,
    UndoConflict,
    restore_relations,
    rewire,
    split,
    undo,
)
from evowiki.nodes.place import place_missing

__all__ = [
    "ChildSpec",
    "GraphChange",
    "GraphError",
    "Group",
    "Mix",
    "NodeEvidence",
    "NodeGraph",
    "NodeRec",
    "RelKind",
    "RelSource",
    "Relation",
    "UndoConflict",
    "aggregate_ids",
    "band_of",
    "cluster",
    "member_key",
    "mix_of",
    "node_evidence",
    "place_missing",
    "restore_relations",
    "rewire",
    "shares",
    "split",
    "undo",
    "validate",
]
