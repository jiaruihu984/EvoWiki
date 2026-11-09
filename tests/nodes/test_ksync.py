from datetime import UTC, datetime

import pytest

from evowiki.belief.aspects import AspectK, DefineConcept
from evowiki.goals.contracts import Aspect
from evowiki.nodes import NodeGraph, NodeRec, Relation
from evowiki.nodes.ksync import DEFAULT_ASPECTS, KSyncError, in_step, ksync_events
from evowiki.wikiapp.llm import ASPECT_CONFIG_V1

TS = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)


class Ids:
    def __init__(self):
        self.n = 0

    def __call__(self, prefix: str) -> str:
        self.n += 1
        return f"{prefix}_{self.n:03d}"


def k_with(*defs: tuple[str, tuple[Aspect, ...], tuple[str, ...]]) -> AspectK:
    k = AspectK(ASPECT_CONFIG_V1)
    for i, (cid, aspects, prereqs) in enumerate(defs):
        k.apply(DefineConcept(event_id=f"d{i}", ts=TS, concept_id=cid, aspects=aspects,
                              prereqs=prereqs))
    return k


def graph(*rels: tuple[str, str, str], nodes: str = "abcd", verified: bool = True) -> NodeGraph:
    """Prerequisites are verified unless `verified=False` (only verified ones are mirrored)."""
    return NodeGraph.of([NodeRec(id=i, name=i.upper()) for i in nodes],
                        [Relation(kind=kd, src=s, dst=d, verified=verified and kd == "prerequisite")
                         for kd, s, d in rels])


def apply(k: AspectK, evs) -> AspectK:
    k = k.copy()
    for e in evs:
        k.apply(e)
    return k


def test_new_nodes_get_default_aspects_and_their_prereqs():
    g = graph(("prerequisite", "a", "b"), ("part_of", "c", "d"))
    k = AspectK(ASPECT_CONFIG_V1)
    evs = ksync_events(g, k, ts=TS, new_id=Ids())
    assert [(e.concept_id, e.aspects, e.prereqs) for e in evs] == [
        ("a", DEFAULT_ASPECTS, ()), ("b", DEFAULT_ASPECTS, ("a",)),
        ("c", DEFAULT_ASPECTS, ()), ("d", DEFAULT_ASPECTS, ())]
    assert [e.event_id for e in evs] == ["aev_001", "aev_002", "aev_003", "aev_004"]
    after = apply(k, evs)
    assert in_step(g, after) and not in_step(g, k)
    assert after.prereqs("b") == ("a",) and after.prereqs("d") == ()   # part_of never a prereq
    assert ksync_events(g, after, ts=TS, new_id=Ids()) == []           # idempotent


def test_known_node_is_only_redefined_to_add_missing_prereqs():
    k = k_with(("a", (Aspect.PROCEDURE,), ()), ("b", (Aspect.TERM,), ("x",)),
               ("x", (Aspect.TERM,), ()))
    g = graph(("prerequisite", "a", "b"), nodes="abx")
    evs = ksync_events(g, k, ts=TS, new_id=Ids(), aspects={"a": (Aspect.PURPOSE,)})
    assert [(e.concept_id, e.aspects, e.prereqs) for e in evs] == [("b", (Aspect.TERM,), ("a",))]
    after = apply(k, evs)
    assert after.prereqs("b") == ("x", "a")       # K keeps x: it cannot drop a prereq
    assert after.aspects("a") == (Aspect.PROCEDURE,) and after.aspects("b") == (Aspect.TERM,)


def test_chosen_aspects_for_new_nodes():
    g = graph(nodes="a")
    [e] = ksync_events(g, AspectK(ASPECT_CONFIG_V1), ts=TS, new_id=Ids(),
                       aspects={"a": (Aspect.PROCEDURE,)})
    assert e.aspects == (Aspect.PROCEDURE,)


def test_dotted_node_id_is_refused():
    g = NodeGraph.of([NodeRec(id="a.b", name="AB")], [])
    with pytest.raises(KSyncError, match="may not contain '.'"):
        ksync_events(g, AspectK(ASPECT_CONFIG_V1), ts=TS, new_id=Ids())


def test_only_verified_prerequisites_are_mirrored():
    """Relation layers: an unverified prerequisite or a recommended edge never becomes a K prereq;
    in_step ignores them."""
    g = NodeGraph.of([NodeRec(id=i, name=i.upper()) for i in "abc"], [
        Relation(kind="prerequisite", src="a", dst="b"),                 # unverified (intake, …)
        Relation(kind="recommended", src="a", dst="c"),
        Relation(kind="prerequisite", src="c", dst="b", verified=True)])
    evs = ksync_events(g, AspectK(ASPECT_CONFIG_V1), ts=TS, new_id=Ids())
    assert [(e.concept_id, e.prereqs) for e in evs] == [("a", ()), ("b", ("c",)), ("c", ())]
    after = apply(AspectK(ASPECT_CONFIG_V1), evs)
    assert in_step(g, after) and after.prereqs("b") == ("c",)
