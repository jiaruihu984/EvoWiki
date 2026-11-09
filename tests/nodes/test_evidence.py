import pytest

from evowiki.nodes import NodeGraph, NodeRec, Relation
from evowiki.nodes.evidence import aggregate_ids, band_of, mix_of, node_evidence, shares


def test_mix_of():
    m = mix_of(["known", "gap", "none", "known"])
    assert m == {"known": 0.5, "likely": 0.0, "shaky": 0.0, "gap": 0.25, "none": 0.25}
    assert sum(mix_of([]).values()) == 0


@pytest.mark.parametrize(
    ("direct", "aggregate", "band"),
    [
        ([], [], "none"),
        (["none"], ["none", "none"], "none"),
        (["known"], [], "known"),
        ([], ["gap"], "gap"),
        (["likely"], [], "likely"),
        (["shaky"], ["none"], "shaky"),
        # direct known (2×1.0) + aggregate gap,gap (2×.08) → 2.16/4 = .54 → shaky
        (["known"], ["gap", "gap"], "shaky"),
        # direct known counts twice: (2×1 + .08)/3 = .69 → likely
        (["known"], ["gap"], "likely"),
        # aggregate alone would be gap; direct known lifts it
        (["known", "known"], ["gap", "gap"], "likely"),
    ],
)
def test_band_of(direct, aggregate, band):
    assert band_of(direct, aggregate) == band


def test_band_of_rejects_unknown_band():
    with pytest.raises(ValueError):
        band_of(["great"], [])


def test_node_evidence_direct_and_aggregate_both_counted():
    graph = NodeGraph.of(
        [NodeRec(id=i, name=i) for i in "pcd"],
        [Relation(kind="part_of", src="c", dst="p"), Relation(kind="part_of", src="d", dst="c")],
    )
    cells = {"p": ["known"], "c": ["gap"], "d": ["gap", "none"]}
    ev = node_evidence(graph, "p", lambda i: cells[i])
    assert set(aggregate_ids(graph, "p")) == {"c", "d"}
    assert ev.direct["known"] == 1.0
    assert ev.aggregate == {"known": 0, "likely": 0, "shaky": 0, "gap": 2 / 3, "none": 1 / 3}
    assert ev.band == "shaky"  # (2 + .08 + .08) / 4 = .54
    assert pytest.approx(sum(ev.mix.values())) == 1.0
    # a right answer on the big node does not change its children
    child = node_evidence(graph, "c", lambda i: cells[i])
    assert child.band == "gap"


def test_shares():
    s = shares(["known", "likely", "shaky", "gap", "none"])
    assert s == {"known": 0.4, "shaky": 0.2, "gap": 0.2, "none": 0.2}
    assert pytest.approx(sum(s.values())) == 1.0
    assert shares([]) == {"known": 0.0, "shaky": 0.0, "gap": 0.0, "none": 0.0}
