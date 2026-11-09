"""Evidence on a node (spec §2 证据): direct and aggregate, both kept, both shown.

- direct: the band of each of the node's OWN cells (a right answer on 线性代数's essence question
  is evidence about 线性代数 itself);
- aggregate: the bands of all cells of its part_of descendants.

`part_of` never propagates evidence: a right answer on a big node does not change its children.
K's inference follows prerequisites only (user: 精髓题答对 ≠ 整个领域都会).

Pure over plain band strings (the values of K's Band StrEnum), no K import, so it can be tested and
reused without the aspect log."""

from collections.abc import Callable, Sequence

from pydantic import BaseModel, ConfigDict

from evowiki.nodes.model import NodeGraph

Mix = dict[str, float]

BANDS = ("known", "likely", "shaky", "gap", "none")
SCORE = {"known": 1.0, "likely": 0.78, "shaky": 0.45, "gap": 0.08}
# nearest band by score: >= .89 known, >= .62 likely, >= .27 shaky, else gap
THRESHOLDS = ((0.89, "known"), (0.62, "likely"), (0.27, "shaky"))
DIRECT_WEIGHT = 2.0
AGGREGATE_WEIGHT = 1.0


def _check(bands: Sequence[str]) -> None:
    bad = sorted({b for b in bands if b not in BANDS})
    if bad:
        raise ValueError(f"unknown band(s): {bad}")


def mix_of(bands: Sequence[str]) -> Mix:
    """Share of each band (incl. "none") among `bands`; all zero when empty."""
    _check(bands)
    n = len(bands)
    return {b: (sum(1 for x in bands if x == b) / n if n else 0.0) for b in BANDS}


def _weighted(direct: Sequence[str], aggregate: Sequence[str]) -> list[tuple[str, float]]:
    _check([*direct, *aggregate])
    return [(b, DIRECT_WEIGHT) for b in direct] + [(b, AGGREGATE_WEIGHT) for b in aggregate]


def band_of(direct: Sequence[str], aggregate: Sequence[str]) -> str:
    """One band for the node: the weighted mean score of the union (direct cells count twice);
    "none" cells carry no evidence; no evidence at all → "none"."""
    pairs = [(b, w) for b, w in _weighted(direct, aggregate) if b != "none"]
    total = sum(w for _, w in pairs)
    if not total:
        return "none"
    score = sum(SCORE[b] * w for b, w in pairs) / total
    return next((band for t, band in THRESHOLDS if score >= t), "gap")


def aggregate_ids(graph: NodeGraph, node_id: str) -> tuple[str, ...]:
    """Whose cells form the aggregate evidence: all part_of descendants."""
    return graph.descendants(node_id)


class NodeEvidence(BaseModel):
    """`mix`: the colour mix, shares of the union with direct cells weighted 2:1 (incl. none)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    direct: Mix
    aggregate: Mix
    band: str
    mix: Mix


def node_evidence(
    graph: NodeGraph, node_id: str, bands_of: Callable[[str], Sequence[str]]
) -> NodeEvidence:
    """`bands_of(id)`: the band of each of that node's own cells."""
    direct = list(bands_of(node_id))
    aggregate = [b for d in aggregate_ids(graph, node_id) for b in bands_of(d)]
    pairs = _weighted(direct, aggregate)
    total = sum(w for _, w in pairs)
    mix = {b: (sum(w for x, w in pairs if x == b) / total if total else 0.0) for b in BANDS}
    return NodeEvidence(
        direct=mix_of(direct), aggregate=mix_of(aggregate), band=band_of(direct, aggregate), mix=mix
    )


def shares(bands: Sequence[str]) -> dict[str, float]:
    """Goal progress bar: known (known + likely), shaky, gap, none as fractions summing to 1
    (all zero when empty)."""
    m = mix_of(bands)
    return {
        "known": m["known"] + m["likely"],
        "shaky": m["shaky"],
        "gap": m["gap"],
        "none": m["none"],
    }
