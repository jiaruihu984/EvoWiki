"""One knowledge point in two goals (user report 2026-11-04: 黑壳虾, then 孔雀鱼): one node, one K, and
each goal says what the point is about there. The live run wrote 孔雀鱼's 《鱼缸选择》 about 黑壳虾,
from the definition the first goal gave the node."""

import pytest

from evowiki.goals.blocks import _blocks, _BlocksOut, _RawBlock
from evowiki.goals.contracts import OutlineSection, PageOutline, SectionRole, SectionText
from evowiki.goals.fold import Page
from evowiki.goals.intake import ConceptRef, _known_rows
from evowiki.goals.regions import GoalMap, MapNode
from evowiki.nodes.model import NodeGraph, NodeRec
from evowiki.wikiapp.events import ConceptName, PageRecord
from evowiki.wikiapp.goal import _reused_page
from evowiki.wikiapp.goal_nodes import related_nodes
from evowiki.wikiapp.kwrite import goal_definition
from evowiki.wikiapp.node_state import foreign_parts as _foreign_parts
from evowiki.wikiapp.node_state import live_page
from evowiki.wikiapp.state import WikiState

NOW = "2026-11-04T00:00:00+00:00"


def _map(map_id: str, goal: str, *nodes: MapNode) -> GoalMap:
    return GoalMap(map_id=map_id, goal=goal, created_at=NOW, nodes=nodes)


def _state() -> WikiState:
    tank = "c11"
    fish = _map("m-fish", "孔雀鱼", MapNode(id="c31", kind="region", name="鱼缸与水质", scope="鱼缸选择、换水"),
                MapNode(id=tank, kind="concept", name="鱼缸选择", parent="c31", page_id="pg-fish",
                        scope="养孔雀鱼选多大的缸"))
    shrimp = _map("m-shrimp", "黑壳虾", MapNode(id="c2", kind="region", name="饲养环境", scope="鱼缸选择"),
                  MapNode(id=tank, kind="concept", name="鱼缸选择", parent="c2", page_id="pg-shrimp"))
    return WikiState(
        maps={m.map_id: m for m in (shrimp, fish)},
        pages={p: PageRecord(page_id=p, title="鱼缸选择", goal=g, concepts=(tank,), targets=(tank,), created_at=NOW)
               for p, g in (("pg-fish", "孔雀鱼 · 鱼缸选择"), ("pg-shrimp", "黑壳虾 · 鱼缸选择"))})


def test_a_page_reads_what_the_point_is_about_in_its_own_goal():
    s = _state()
    assert goal_definition(s, "pg-fish", "c11") == "养孔雀鱼选多大的缸"
    assert goal_definition(s, "pg-shrimp", "c11") == ""     # the first goal: the node's own definition


def test_a_blocks_covers_are_not_what_a_page_is_about():
    s = _state()
    assert goal_definition(s, "pg-fish", "c31") == ""


def test_the_model_adjudicates_on_what_a_node_is_about():
    row = _known_rows([ConceptRef(id="c15", name="水温", definition="适宜的水温范围")], {})[0]
    assert row == "c15 水温：适宜的水温范围"


def test_reusing_a_block_keeps_the_nodes_name():
    out = _BlocksOut(blocks=[_RawBlock(name="把水养好", existing_id="c3")])
    blocks, dropped = _blocks(out, [ConceptRef(id="c3", name="水质与维护")])
    assert [(b.name, b.existing_id) for b in blocks] == [("水质与维护", "c3")] and not dropped


class Words:
    """Cosine 1 for a text with the query word, else 0."""

    id, model_version, dim = "fake", "1", 2

    def embed_documents(self, texts):
        return [[1.0, 0.0] if "水" in t else [0.0, 1.0] for t in texts]

    def embed_query(self, text):
        return [1.0, 0.0] if "水" in text else [0.0, 1.0]


def test_one_hit_brings_the_goals_other_blocks_and_nothing_else():
    names = {"a": "水质", "b": "日常照顾", "b1": "喂食", "x": "巨分量"}
    from evowiki.nodes.model import Relation
    g = NodeGraph(nodes=tuple(NodeRec(id=i, name=n) for i, n in names.items()),
                  relations=(Relation(kind="part_of", src="b1", dst="b", source="intake"),))
    m = _map("m", "孔雀鱼", MapNode(id="a", kind="region", name="水质"), MapNode(id="b", kind="region", name="日常照顾"))
    other = _map("n", "渗流", MapNode(id="x", kind="region", name="巨分量"))
    s = WikiState(graph=g, maps={"m": m, "n": other},
                  names={i: ConceptName(concept_id=i, name=n) for i, n in names.items()})
    assert related_nodes(s, "水", embedder=Words()) == {"a", "b", "b1"}


# --- an existing page on another goal's point (goals/page_fit.py) ---------------------------------

def _written(pid: str, goal: str, tgt: str = "c11", body: str = "正文") -> PageRecord:
    o = PageOutline(page_id=pid, goal=goal, concepts=(tgt,), targets=(tgt,), sections=(
        OutlineSection(id="s1", heading="讲", role=SectionRole.EXPLAIN),))
    page = Page(outline=o, states={}, texts={"s1": SectionText(section_id="s1", cache_key="k", body=body)}, snapshot={})
    return PageRecord(page_id=pid, title="鱼缸选择", goal=goal, concepts=(tgt,), targets=(tgt,), created_at=NOW, page=page)


class _W:
    """What `_reused_page` uses of a request's work."""

    def __init__(self, state: WikiState):
        self.state, self.saved = state, []

    def llm(self):
        return None

    def add(self, body):
        self.saved.append(body.record)
        self.state = self.state.model_copy(update={"page_fits": {
            **self.state.page_fits, f"{body.record.map_id}:{body.record.node_id}:{body.record.page_id}": body.record}})


POINT = MapNode(id="c11", kind="concept", name="鱼缸选择", parent="c31", scope="养孔雀鱼选多大的缸")
FISH = _map("m-fish", "孔雀鱼", MapNode(id="c31", kind="region", name="鱼缸与水质"), POINT)


def _with_page(rec: PageRecord) -> WikiState:
    shrimp = _map("m-shrimp", "黑壳虾", MapNode(id="c11", kind="concept", name="鱼缸选择", parent="c2", page_id=rec.page_id))
    return WikiState(maps={"m-fish": FISH, "m-shrimp": shrimp}, pages={rec.page_id: rec}, page_order=(rec.page_id,))


def _judged(monkeypatch, *verdicts):
    calls = []
    answers = list(verdicts)

    def fake(llm, **kw):
        calls.append(kw)
        return answers.pop(0), "", ("孔雀鱼要 24 度" if verdicts[0] == "partial" else "")
    monkeypatch.setattr("evowiki.wikiapp.goal.judge_fit", fake)
    return calls


@pytest.mark.parametrize(("verdict", "opened"), [("full", "pg1"), ("partial", "pg1"), ("none", None)])
def test_a_page_is_opened_only_when_it_fits_and_the_judgement_is_kept(monkeypatch, verdict, opened):
    calls = _judged(monkeypatch, verdict)
    w = _W(_with_page(_written("pg1", "黑壳虾 · 鱼缸选择")))
    assert _reused_page(w, FISH, POINT) == opened
    assert calls[0]["written_for"] == "黑壳虾" and calls[0]["goal"] == "孔雀鱼"
    assert _reused_page(w, FISH, POINT) == opened and len(calls) == 1      # read back, no second call


def test_a_changed_page_or_goal_is_judged_again(monkeypatch):
    calls = _judged(monkeypatch, "full", "none", "none")
    w = _W(_with_page(_written("pg1", "黑壳虾 · 鱼缸选择")))
    assert _reused_page(w, FISH, POINT) == "pg1"
    edited = _written("pg1", "黑壳虾 · 鱼缸选择", body="改过的正文")
    w.state = w.state.model_copy(update={"pages": {"pg1": edited}})
    assert _reused_page(w, FISH, POINT) is None and len(calls) == 2
    other = POINT.model_copy(update={"scope": "别的范围"})
    assert _reused_page(w, FISH.replace(other), other) is None and len(calls) == 3


def test_only_written_pages_of_other_goals_are_candidates(monkeypatch):
    calls = _judged(monkeypatch)
    rec = _written("pg1", "黑壳虾 · 鱼缸选择")
    trashed = _with_page(rec).model_copy(update={"trashed_pages": frozenset({"pg1"})})
    unwritten = _with_page(rec.model_copy(update={"page": None}))
    on_map = FISH.replace(POINT.model_copy(update={"page_id": "pg1"}))
    assert _reused_page(_W(trashed), FISH, POINT) is None and _reused_page(_W(unwritten), FISH, POINT) is None
    assert _reused_page(_W(_with_page(rec)), on_map, POINT) is None and not calls


# --- a shared node's parts ------------------------------------------------------------------------

def _two_goals() -> tuple[WikiState, GoalMap, GoalMap]:
    water = MapNode(id="w", kind="region", name="水温")
    shrimp = _map("m-shrimp", "黑壳虾", water.model_copy(update={"state": "expanded"}),
                  MapNode(id="t", kind="concept", name="测温", parent="w"),
                  MapNode(id="r", kind="concept", name="黑壳虾的适宜水温", parent="w"))
    fish = _map("m-fish", "孔雀鱼", water)
    return WikiState(maps={"m-shrimp": shrimp, "m-fish": fish}), shrimp, fish


def test_parts_only_another_goal_made_stay_its_own():
    s, shrimp, fish = _two_goals()
    assert _foreign_parts(s, fish, fish.node("w")) == {"t", "r"}
    assert _foreign_parts(s, shrimp, shrimp.node("w")) == set()
    both = fish.replace(MapNode(id="t", kind="concept", name="测温", parent="w"))
    s2 = s.model_copy(update={"maps": {"m-shrimp": shrimp, "m-fish": both}})
    assert _foreign_parts(s2, both, both.node("w")) == {"r"}
    assert _foreign_parts(s2, shrimp, shrimp.node("w")) == set()     # the first goal is not given anything


def test_a_judgement_that_no_longer_holds_shows_no_page_so_opening_judges_again(monkeypatch):
    _judged(monkeypatch, "full")
    w = _W(_with_page(_written("pg1", "黑壳虾 · 鱼缸选择")))
    assert _reused_page(w, FISH, POINT) == "pg1"
    on_map = POINT.model_copy(update={"page_id": "pg1"})
    assert live_page(w.state, FISH, on_map) == "pg1"
    edited = w.state.model_copy(update={"pages": {"pg1": _written("pg1", "黑壳虾 · 鱼缸选择", body="改过的正文")}})
    assert live_page(edited, FISH, on_map) is None
    assert live_page(w.state.model_copy(update={"trashed_pages": frozenset({"pg1"})}), FISH, on_map) is None
