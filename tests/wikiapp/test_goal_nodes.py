"""A new goal's scope comes from its own words (bug 2026-11-03: `python` with a percolation profile
came back as seven percolation blocks): the profile block of goals.intake / goals.enter carries no
other goal's 想学什么 / 还不会的, and the prompt's existing-node rows list what is related to this goal —
while ids and reuse still see every node. A fake embedder (topic words → axes), scripted LLM."""

import pytest

from evowiki.goals.blocks import blocks_prompt
from evowiki.goals.intake import ConceptRef, IntakeRelation, IntakeScope, _outline_prompt
from evowiki.goals.regions import GoalMap, MapNode
from evowiki.nodes.model import NodeGraph, NodeRec, Relation
from evowiki.profile.context import RULE, context_for
from evowiki.profile.note import GAPS, HOW, WANT, WHERE, WHO
from evowiki.wikiapp.events import ConceptName
from evowiki.wikiapp.goal_nodes import entered_nodes, related_nodes
from evowiki.wikiapp.state import WikiState

SECTIONS = {WHO: ("学过高数，微积分，线性代数",), WANT: ("复杂网络中的渗流",), HOW: ("先给例子",),
            WHERE: ("渗流：1/7 块",), GAPS: ("巨分量与阈值：还不清楚是什么",)}


@pytest.mark.parametrize("purpose", ["goals.intake", "goals.enter"])
def test_a_decomposition_reads_who_he_is_not_his_other_goals(purpose):
    block = context_for(SECTIONS, purpose)
    assert "学过高数" in block and "先给例子" in block
    assert "渗流" not in block and "巨分量" not in block and "想学什么" not in block
    assert RULE not in block            # the rule is about 想学什么, which is not here


def test_the_other_purposes_keep_their_sections():
    assert "复杂网络中的渗流" in context_for(SECTIONS, "assistant.turn")
    assert "复杂网络中的渗流" in context_for(SECTIONS, "goals.reading.unit")
    assert RULE in context_for(SECTIONS, "deep.write")


class Topics:
    """One axis per topic word, plus one every node text has a little (real embeddings are never
    orthogonal): cosine is high only when a text and the query share a topic."""

    id, model_version, dim = "fake", "1", 4
    AXES = (("python", "编程"), ("网络", "渗流", "级联", "巨分量", "无标度", "连通"), ("概率",))

    def _vec(self, text: str) -> list[float]:
        low = text.casefold()
        return [*(1.0 if any(w in low for w in ws) else 0.0 for ws in self.AXES), 0.2]

    def embed_documents(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return [*self._vec(text)[:3], 0.0]


NET = "c2"
NAMES = {"c1": "概率与生成函数", NET: "网络表示与基础量", "c5": "无标度网络", "c8": "节点与边",
         "c10": "连通分量", "c20": "水质与环境"}


def _state(**up) -> WikiState:
    g = NodeGraph(nodes=tuple(NodeRec(id=i, name=n) for i, n in NAMES.items()),
                  relations=(Relation(kind="part_of", src="c8", dst=NET, source="intake"),
                             Relation(kind="part_of", src="c10", dst=NET, source="intake")))
    return WikiState(graph=g, names={i: ConceptName(concept_id=i, name=n)
                                     for i, n in NAMES.items()}, **up)


def _refs():
    return [ConceptRef(id=i, name=n) for i, n in NAMES.items()]


def test_python_lists_no_percolation_node():
    shown = related_nodes(_state(), "python", words=("什么都不会",), embedder=Topics())
    assert shown == frozenset()
    p = blocks_prompt("python", ("python", "什么都不会"), _refs(), shown=shown)
    assert "知识图谱里已经有这些节点" not in p and "网络表示与基础量" not in p and "c10" not in p


def test_a_related_goal_lists_its_nodes_with_their_neighbours_only():
    shown = related_nodes(_state(), "复杂网络的级联失效", embedder=Topics())
    assert {NET, "c5", "c8", "c10"} <= shown and not {"c1", "c20"} & shown
    p = blocks_prompt("复杂网络的级联失效", (), _refs(), shown=shown)
    assert "c2 网络表示与基础量" in p and "c5 无标度网络" in p
    assert "概率与生成函数" not in p and "水质与环境" not in p


def test_the_same_words_count_without_the_meaning():
    """BM25 over the goal and his words: he named a node's words."""
    class Far(Topics):
        def embed_query(self, text):
            return [1.0, 0.0, 0.0, 0.0]

        def embed_documents(self, texts):
            return [[0.0, 1.0, 0.0, 0.0] for _ in texts]
    shown = related_nodes(_state(), "python", words=("我学过概率与生成函数",), embedder=Far())
    assert "c1" in shown and "c20" not in shown


def test_entering_sees_its_goal_its_place_and_another_goals_related_nodes():
    m = GoalMap(map_id="m", goal="养虾", created_at="2026-11-03T00:00:00Z",
                nodes=(MapNode(id="c20", kind="region", name="水质与环境"),))
    scope = IntakeScope(goal="养虾 · 水质与环境", region_id="c20", region_name="水质与环境",
                        scope="", parent_goal="养虾", siblings=(("水质与环境", ""),))
    assert entered_nodes(_state(), m, "c20", scope, embedder=Topics()) == {"c20"}
    other = scope.model_copy(update={"scope": "概率"})
    assert "c1" in entered_nodes(_state(), m, "c20", other, embedder=Topics())   # another goal's
    on_net = MapNode(id="c8", kind="concept", name="节点与边", parent=NET)
    m2 = GoalMap(map_id="m2", goal="养虾", created_at="2026-11-03T00:00:00Z", nodes=(on_net,))
    plain = IntakeScope(goal="g", region_id="c8", region_name="节点与边", parent_goal="养虾")
    assert {"c8", NET} <= entered_nodes(_state(), m2, "c8", plain, embedder=Topics())   # its parent


def test_rows_shrink_but_a_row_still_names_its_neighbours():
    scope = IntakeScope(goal="g", region_id="c20", region_name="水质与环境", parent_goal="g",
                        siblings=(("水质与环境", ""),))
    rel = [IntakeRelation(kind="part_of", src="c8", dst=NET)]
    p = _outline_prompt("g", _refs(), scope, graph_relations=rel, shown={NET})
    assert "c2 网络表示与基础量（" in p and "包含：c8 节点与边" in p
    assert "\nc8 节点与边" not in p and "c1 概率与生成函数" not in p
    assert "c1 概率与生成函数" in _outline_prompt("g", _refs(), scope)     # None: every node
