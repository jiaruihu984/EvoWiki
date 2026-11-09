"""The unified node graph (spec 2026-10-13 §2): one kind of node, typed relations between nodes.

There is no region/concept type: a "region" is just a node that currently has children. A node
may have several parents. Relations carry a one-line phrase written by the model (shown on hover).

Relation conventions (direction matters for all kinds but `related`):
- `part_of`: `src` is a part of `dst` (child → parent). Several parents are allowed. Drives zoom
  containment and the aggregate evidence; it never propagates evidence by inference.
- `prerequisite`: strict requires — not knowing `src` makes `dst` impossible to learn. Only a
  `verified` one feeds knowledge inference (the KST surmise relation, K inference along prereqs,
  the K mirror in `nodes.ksync`) and is a hard edge of the learning order; an unverified one is a
  soft ordering hint (relation layers, user ruling 2026-10-14: a wrong prerequisite makes KST infer
  mastery wrongly and walls the path).
- `recommended`: `src` helps understanding `dst` but is not necessary. Soft ordering only; never
  inference.
- `kind_of`: `src` is a kind of `dst`. Optional, for contrasts in explanations.
- `related`: unordered; stored with `src < dst` so (a, b) and (b, a) are one relation.

Every relation carries `verified` (default False; snapshots from before it load unverified):
existence is not verification. Nothing writes verified=True yet; an `expert` source is reserved for
reviewed edges.

The code refuses only what a program cannot handle (user principle: rules serve the experience,
not the other way round): an unknown id, a `part_of` or `prerequisite` cycle, a self-loop, an
empty id or name, a duplicate node id. Nothing about the number of parents/children or depth.

The graph keeps nodes sorted by id and relations sorted by (kind, src, dst), deduplicated, so two
graphs with the same content compare (and dump) equal — that is what makes undo exact.
Pure data, no IO, no LLM."""

from collections import deque
from collections.abc import Iterable, Sequence
from typing import Literal

import networkx as nx
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    field_validator,
    model_validator,
)

RelKind = Literal["part_of", "prerequisite", "recommended", "kind_of", "related"]
RelSource = Literal["intake", "curator", "user", "split", "migrated", "expert"]
GraphErrorCode = Literal[
    "unknown_id", "part_of_cycle", "prerequisite_cycle", "self_loop", "empty", "duplicate_node"
]


class GraphError(ValueError):
    """A graph the program cannot handle. `code` names the reason (see `GraphErrorCode`)."""

    def __init__(self, code: GraphErrorCode, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class NodeRec(BaseModel):
    """`x`, `y` in the page's units (a small graph within about [-1, 1], `nodes.place`): assigned
    once when the node is placed, never recomputed (the map is stable across goals; the one
    exception is the recorded one-time `relayout` of an older layout). None until placed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    name: str
    definition: str = ""
    x: float | None = None
    y: float | None = None


class Relation(BaseModel):
    """See the module docstring for direction conventions. `source`: who created it; `verified`:
    the edge was checked (only a verified prerequisite feeds inference and hard ordering)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: RelKind
    src: str
    dst: str
    phrase: str = ""
    source: RelSource = "intake"
    verified: bool = False

    @model_validator(mode="before")
    @classmethod
    def _unordered_related(cls, data: object) -> object:
        if isinstance(data, dict) and data.get("kind") == "related":
            src, dst = data.get("src"), data.get("dst")
            if isinstance(src, str) and isinstance(dst, str) and dst < src:
                return {**data, "src": dst, "dst": src}
        return data

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.kind, self.src, self.dst)


class GraphChange(BaseModel):
    """One structural change (split / migrate / reset) with everything needed to undo it:
    undo removes `added_*` and puts `removed_*` back. `node_ids`: the nodes it is about.
    `why`: what triggered it, in plain Chinese (「你点了看不懂」); `note`: the model's one line for the
    learner (split); `words`: the learner's own words that came with it (verbatim, or "")."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    change_id: str
    at: AwareDatetime
    kind: Literal["split", "migrate", "reset"]
    summary: str
    why: str
    node_ids: tuple[str, ...] = ()
    added_nodes: tuple[NodeRec, ...] = ()
    added_relations: tuple[Relation, ...] = ()
    removed_nodes: tuple[NodeRec, ...] = ()
    removed_relations: tuple[Relation, ...] = ()
    undone: bool = False
    note: str = ""
    words: str = ""


def merge_relation(existing: Relation | None, new: Relation) -> Relation:
    """Dedupe rule on (kind, src, dst): a non-empty phrase replaces an empty one, else first wins;
    verification is never lost (verified if either is)."""
    if existing is None:
        return new
    kept = new if new.phrase and not existing.phrase else existing
    if (existing.verified or new.verified) and not kept.verified:
        kept = kept.model_copy(update={"verified": True})
    return kept


def _canonical_relations(rels: Iterable[Relation]) -> tuple[Relation, ...]:
    out: dict[tuple[str, str, str], Relation] = {}
    for r in rels:
        out[r.key] = merge_relation(out.get(r.key), r)
    return tuple(out[k] for k in sorted(out))


def check(nodes: Sequence[NodeRec], relations: Sequence[Relation]) -> None:
    """Raise GraphError if these nodes and relations do not form a graph the program can handle."""
    ids: set[str] = set()
    for rec in nodes:
        if not rec.id.strip() or not rec.name.strip():
            raise GraphError("empty", f"node {rec.id!r} has an empty id or name")
        if rec.id in ids:
            raise GraphError("duplicate_node", f"node {rec.id!r} appears twice")
        ids.add(rec.id)
    for r in relations:
        for end in (r.src, r.dst):
            if end not in ids:
                raise GraphError("unknown_id", f"{r.kind} {r.src}→{r.dst}: no node {end!r}")
        if r.src == r.dst:
            raise GraphError("self_loop", f"{r.kind} {r.src}→{r.dst}")
    cycle_codes: dict[str, GraphErrorCode] = {
        "part_of": "part_of_cycle",
        "prerequisite": "prerequisite_cycle",
    }
    for kind, code in cycle_codes.items():
        dg = nx.DiGraph((r.src, r.dst) for r in relations if r.kind == kind)
        if not nx.is_directed_acyclic_graph(dg):
            raise GraphError(code, " → ".join(a for a, _ in nx.find_cycle(dg)))


def validate(graph: "NodeGraph") -> None:
    """Raise GraphError if `graph` breaks an invariant (also for graphs built without validation)."""
    check(graph.nodes, graph.relations)


class NodeGraph(BaseModel):
    """The whole graph plus its structural change log. Updates return a new graph and validate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    nodes: tuple[NodeRec, ...] = ()
    relations: tuple[Relation, ...] = ()
    changes: tuple[GraphChange, ...] = ()

    @field_validator("nodes")
    @classmethod
    def _sort_nodes(cls, v: tuple[NodeRec, ...]) -> tuple[NodeRec, ...]:
        return tuple(sorted(v, key=lambda r: r.id))

    @field_validator("relations")
    @classmethod
    def _sort_relations(cls, v: tuple[Relation, ...]) -> tuple[Relation, ...]:
        return _canonical_relations(v)

    @model_validator(mode="after")
    def _valid(self) -> "NodeGraph":
        validate(self)
        return self

    @classmethod
    def of(
        cls,
        nodes: Iterable[NodeRec],
        relations: Iterable[Relation],
        changes: Iterable[GraphChange] = (),
    ) -> "NodeGraph":
        """Bulk constructor that raises GraphError (the plain constructor wraps it in pydantic's
        ValidationError)."""
        nodes, relations = tuple(nodes), tuple(relations)
        check(nodes, relations)
        return cls(nodes=nodes, relations=relations, changes=tuple(changes))

    def updated(
        self,
        *,
        nodes: Iterable[NodeRec] | None = None,
        relations: Iterable[Relation] | None = None,
        changes: Iterable[GraphChange] | None = None,
    ) -> "NodeGraph":
        """This graph with the given parts replaced, validated (GraphError)."""
        return NodeGraph.of(
            self.nodes if nodes is None else nodes,
            self.relations if relations is None else relations,
            self.changes if changes is None else changes,
        )

    # lookups

    def node(self, node_id: str) -> NodeRec | None:
        return next((r for r in self.nodes if r.id == node_id), None)

    def parents(self, node_id: str) -> tuple[str, ...]:
        return tuple(r.dst for r in self.relations if r.kind == "part_of" and r.src == node_id)

    def children(self, node_id: str) -> tuple[str, ...]:
        return tuple(r.src for r in self.relations if r.kind == "part_of" and r.dst == node_id)

    def has_children(self, node_id: str) -> bool:
        return any(r.kind == "part_of" and r.dst == node_id for r in self.relations)

    def prereqs(self, node_id: str) -> tuple[str, ...]:
        """Nodes that must be learned before `node_id`."""
        return tuple(r.src for r in self.relations if r.kind == "prerequisite" and r.dst == node_id)

    def prereq_edges(self) -> tuple[Relation, ...]:
        """Every prerequisite relation, verified or not."""
        return tuple(r for r in self.relations if r.kind == "prerequisite")

    def verified_prereqs(self, node_id: str) -> tuple[str, ...]:
        """The verified prerequisites of `node_id` (the only ones inference may use)."""
        return tuple(r.src for r in self.relations
                     if r.kind == "prerequisite" and r.verified and r.dst == node_id)

    def trusted_pairs(self) -> frozenset[tuple[str, str]]:
        """(prerequisite, node) of every verified prerequisite: the edges knowledge inference may
        follow (KST surmise relation, K inference, the K mirror, hard path order)."""
        return frozenset((r.src, r.dst) for r in self.relations
                         if r.kind == "prerequisite" and r.verified)

    def _walk(self, node_id: str, step: str) -> tuple[str, ...]:
        nxt = self.children if step == "down" else self.parents
        seen: dict[str, None] = {}
        todo = deque(nxt(node_id))
        while todo:
            cur = todo.popleft()
            if cur in seen or cur == node_id:
                continue
            seen[cur] = None
            todo += nxt(cur)
        return tuple(seen)

    def descendants(self, node_id: str) -> tuple[str, ...]:
        """All transitive part_of descendants (breadth first, each once)."""
        return self._walk(node_id, "down")

    def ancestors(self, node_id: str) -> tuple[str, ...]:
        return self._walk(node_id, "up")

    # immutable updates

    def with_node(self, rec: NodeRec) -> "NodeGraph":
        """Add `rec` or replace the node with its id; a replacement without a position keeps the
        old one (positions are stable)."""
        old = self.node(rec.id)
        if old is not None and rec.x is None and rec.y is None:
            rec = rec.model_copy(update={"x": old.x, "y": old.y})
        return self.updated(nodes=(*(r for r in self.nodes if r.id != rec.id), rec))

    def with_relation(self, rel: Relation) -> "NodeGraph":
        """Add `rel`, deduped on (kind, src, dst) (see `merge_relation`)."""
        old = next((r for r in self.relations if r.key == rel.key), None)
        kept = merge_relation(old, rel)
        if kept is old:
            return self
        return self.updated(relations=(*(r for r in self.relations if r.key != rel.key), kept))
