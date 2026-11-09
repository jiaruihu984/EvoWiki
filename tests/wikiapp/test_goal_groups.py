"""The graph's groups inside a goal and where a page's file lives (decision 2026-11-04): blocks as the map has them, one id on
several goals, the clustering only of the points on no goal, and folders that no grouping decides."""

from datetime import UTC, datetime

from evowiki.goals.regions import GoalMap, MapNode
from evowiki.nodes import NodeGraph, NodeRec, Relation
from evowiki.wikiapp.events import GoalSettings, PageRecord
from evowiki.wikiapp.goal_groups import goal_groups, outside_goals
from evowiki.wikiapp.graph_groups import to_name
from evowiki.wikiapp.page_folder import goal_folder, page_folder
from evowiki.wikiapp.state import WikiState

T0 = datetime(2026, 1, 1, tzinfo=UTC)
T1 = datetime(2026, 2, 1, tzinfo=UTC)


def region(i, name=None, **kw):
    return MapNode(id=i, kind="region", name=name or i, **kw)


def concept(i, parent):
    return MapNode(id=i, kind="concept", name=i, parent=parent, state="expanded")


def state_of(ids, relations, *maps, **kw):
    graph = NodeGraph.of([NodeRec(id=i, name=i, definition="") for i in ids],
                         [Relation(kind=k, src=a, dst=b) for k, a, b in relations])
    return WikiState(graph=graph, maps={m.map_id: m for m in maps}, map_order=[m.map_id for m in maps], **kw)


def coffee(map_id="m1", created_at=T0):
    # 手冲咖啡's shape: the assistant moved 研磨粗细 under 器具 on the graph; on the map it stays in 咖啡豆, as 学点新的 lists it.
    # 磨豆机 was split: its parts are on the graph only.
    return GoalMap(map_id=map_id, goal="我要学手冲咖啡", title="手冲咖啡", created_at=created_at, nodes=(
        region("认识"), region("咖啡豆", state="expanded"), region("器具"),
        concept("研磨粗细", "咖啡豆"), concept("磨豆机", "咖啡豆")))


COFFEE_NODES = ["认识", "咖啡豆", "器具", "研磨粗细", "磨豆机", "刀盘式", "刀片式"]
COFFEE_RELATIONS = [("part_of", "研磨粗细", "器具"), ("part_of", "磨豆机", "咖啡豆"),
                    ("part_of", "刀盘式", "磨豆机"), ("part_of", "刀片式", "磨豆机")]


def test_a_goal_groups_by_its_blocks_with_their_real_names_not_by_its_relations():
    [goal] = goal_groups(state_of(COFFEE_NODES, COFFEE_RELATIONS, coffee()))
    assert (goal.name, goal.map_id) == ("手冲咖啡", "m1")
    blocks = {b.name: b.members for b in goal.blocks}
    # 咖啡豆's node holds points: the block is the picture of it. 研磨粗细 stays where the map has it, once.
    assert blocks == {"认识": ("认识",), "咖啡豆": ("研磨粗细", "磨豆机", "刀片式", "刀盘式"), "器具": ("器具",)}


def test_a_region_listing_its_points_in_members_holds_nested_regions_in_its_top_block():
    # regression's map: sub-regions have no parent, the region above lists them
    m = GoalMap(map_id="m", goal="g", title="黑壳虾", created_at=T0, nodes=(
        region("生物学特征", members=("形态结构", "甲壳纲")), region("形态结构", members=("外部形态",)),
        concept("甲壳纲", "生物学特征"), concept("外部形态", "形态结构")))
    [goal] = goal_groups(state_of(["生物学特征", "形态结构", "甲壳纲", "外部形态"], [], m))
    assert {b.name: b.members for b in goal.blocks} == {"生物学特征": ("甲壳纲", "外部形态")}


def test_a_point_on_two_goals_is_in_a_block_of_each():
    second = GoalMap(map_id="m2", goal="我要学意式", title="意式", created_at=T1, nodes=(
        region("意式基础", members=("研磨粗细",)), concept("研磨粗细", "意式基础")))
    groups = goal_groups(state_of(COFFEE_NODES + ["意式基础"], COFFEE_RELATIONS, coffee(), second))
    holding = [(g.name, b.name) for g in groups for b in g.blocks if "研磨粗细" in b.members]
    assert holding == [("手冲咖啡", "咖啡豆"), ("意式", "意式基础")]


def test_only_the_points_on_no_goal_are_clustered():
    state = state_of(COFFEE_NODES + ["黑壳虾", "蜕皮"], [*COFFEE_RELATIONS, ("related", "黑壳虾", "蜕皮"), ("related", "蜕皮", "认识")], coffee())
    rest = outside_goals(state)
    assert [n.id for n in rest.nodes] == ["蜕皮", "黑壳虾"]
    assert [{r.src, r.dst} for r in rest.relations] == [{"黑壳虾", "蜕皮"}]
    assert [g.members for g in to_name(state)[1]] == [("蜕皮", "黑壳虾")]        # no model call names a block


def test_a_goal_taken_back_goes_back_to_the_clustering():
    state = state_of(COFFEE_NODES, COFFEE_RELATIONS, coffee(), goal_settings={"m1": GoalSettings(goal_id="m1", archived=True)})
    assert goal_groups(state) == []
    assert len(outside_goals(state).nodes) == len(COFFEE_NODES)


def page(*targets, folder=""):
    return PageRecord(page_id="p", title="t", goal="g", concepts=targets, targets=targets, folder=folder, created_at=T0)


def test_a_new_page_goes_to_its_goals_folder_named_after_the_goal():
    state = state_of(COFFEE_NODES, COFFEE_RELATIONS, coffee())
    assert page_folder(state, page("研磨粗细")) == "手冲咖啡"
    assert page_folder(state, page("刀盘式")) == "手冲咖啡"       # a split child: through its parent on the map


def test_a_goal_keeps_the_folder_its_first_page_has():
    m = coffee()
    first = page("认识", folder="网络表示与基础量、巨分量等").model_copy(update={"page_id": "p1", "goal": m.goal})
    m = m.replace(m.node("认识").model_copy(update={"page_id": "p1"}))
    state = state_of(COFFEE_NODES, COFFEE_RELATIONS, m, pages={"p1": first})
    assert goal_folder(state, m) == "网络表示与基础量、巨分量等"
    assert page_folder(state, page("器具")) == "网络表示与基础量、巨分量等"


def test_a_page_on_no_goal_goes_under_the_top_of_its_part_of_chain_or_the_root():
    state = state_of(["数学", "线代", "矩阵", "孤点"], [("part_of", "线代", "数学"), ("part_of", "矩阵", "线代")])
    assert page_folder(state, page("矩阵")) == "数学"
    assert page_folder(state, page("孤点")) == ""
    assert page_folder(state, page("没有这个点")) == ""


def test_a_shared_point_written_for_no_goal_goes_with_the_oldest_goal():
    late = coffee("m2", T1).model_copy(update={"title": "晚来的目标"})
    state = state_of(COFFEE_NODES, COFFEE_RELATIONS, late, coffee("m1", T0))
    assert page_folder(state, page("认识")) == "手冲咖啡"


def test_a_shared_nodes_older_page_is_not_the_goals_first_page():
    """Live 2026-11-04: 读懂《sample lecture…》 reused a block of 渗流 and its pages went into 渗流's folder."""
    m = coffee()
    other = page("认识", folder="别的目标").model_copy(update={"page_id": "p1", "goal": "别的目标 · 认识"})
    m = m.replace(m.node("认识").model_copy(update={"page_id": "p1"}))
    state = state_of(COFFEE_NODES, COFFEE_RELATIONS, m, pages={"p1": other})
    assert goal_folder(state, m) != "别的目标"
