from evowiki.nodes import NodeGraph, NodeRec
from evowiki.wikiapp.group_places import places_with_blocks
from evowiki.wikiapp.wiki_schemas import GroupOut


class Words:                     # a block's vector: which subject its words are about
    id, model_version = "words", "1"

    def embed_documents(self, texts):
        return [[float("网络" in t), float("虾" in t), 0.05] for t in texts]


def test_a_new_goals_blocks_stand_in_free_space_beside_what_they_resemble_and_nothing_moves():
    graph = NodeGraph.of([NodeRec(id="n1", name="网络", definition="", x=0.0, y=0.0), NodeRec(id="n2", name="度", definition="", x=0.3, y=0.0),
                          NodeRec(id="s1", name="虾", definition="", x=5.0, y=0.0)], [])
    groups = [GroupOut(id="net", level=2, name="网络", summary="", members=["n1", "n2"], parent=None, x=0, y=0),
              GroupOut(id="shrimp", level=2, name="虾", summary="", members=["s1"], parent=None, x=0, y=0)]
    places, links = places_with_blocks(graph, groups, {"n1": "网络", "n2": "度", "s1": "虾"}, [("a", "网络的级联", ""), ("b", "网络的渗流", "")], Words())
    near = [abs(places[b][0] - 0.15) < abs(places[b][0] - 5.0) for b in ("a", "b")]
    assert all(near)
    for b in ("a", "b"):
        assert all(((places[b][0] - x) ** 2 + (places[b][1] - y) ** 2) ** 0.5 >= 0.15 for x, y in ((0.0, 0.0), (0.3, 0.0), (5.0, 0.0)))
    assert ("a", "net") in links and ("a", "shrimp") not in links


def test_a_block_the_map_has_stays_where_it_is():
    graph = NodeGraph.of([NodeRec(id="n1", name="网络", definition="", x=1.0, y=2.0)], [])
    places, _ = places_with_blocks(graph, [], {}, [("n1", "网络", "")], Words())
    assert places == {"n1": (1.0, 2.0)}


def test_preview_uses_real_measured_clouds_with_stable_organic_positions():
    import numpy as np

    from evowiki.wikiapp.graph_layout import _picture

    blocks = [(f"r{i}", name, "") for i, name in enumerate([
        "网络基础", "网络结构", "网络优化", "网络泛化", "网络应用"])]
    graph = NodeGraph()
    places, _ = places_with_blocks(graph, [], {}, blocks, Words())
    _, expected = _picture("", [(name, [key], 1) for key, name, _ in blocks], graph)
    assert set(places) == {key for key, _, _ in blocks}
    assert np.allclose(list(places.values()), list(expected.values()), atol=1e-6)
    assert len({y for _, y in places.values()}) >= 4
    # Confirming gives the same local geometry; only the whole goal's placement changes.
    _, confirmed = _picture("学习网络", [(name, [key], 1) for key, name, _ in blocks], graph)
    assert np.allclose(list(confirmed.values()), list(expected.values()))
    repeated, _ = places_with_blocks(graph, [], {}, blocks, Words())
    assert repeated == places
    # Scope text contributes to semantic placement, never fake satellite capacity.
    described = [(key, name, "网络、网络，网络;网络") for key, name, _ in blocks]
    same, _ = places_with_blocks(graph, [], {}, described, Words())
    assert same == places


def test_preview_keeps_confirmed_regions_fixed_when_new_regions_are_added():
    graph = NodeGraph.of([NodeRec(id="old", name="网络基础", definition="", x=1.0, y=2.0)], [])
    blocks = [("old", "网络基础", ""), ("new", "网络应用", "")]
    places, _ = places_with_blocks(graph, [], {}, blocks, Words())
    assert places["old"] == (1.0, 2.0)
    assert places["new"] != places["old"]
    assert graph.node("old").x == 1.0 and graph.node("old").y == 2.0
