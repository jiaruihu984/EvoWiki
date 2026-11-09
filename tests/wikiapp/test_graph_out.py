"""GET /wiki/graph's builder (plan T5, `wikiapp.graph_out`): direct and aggregate evidence kept
apart, every parent, plain evidence lines, kind phrases, positions, groups with stored / inherited /
fallback names, goals with shares and the path as shown. 0 LLM calls (no gateway at all)."""

import pytest

from evowiki.belief.aspects import AddNote, Declare, DefineConcept
from evowiki.goals.contracts import cell_id
from evowiki.goals.regions import GoalMap, MapNode
from evowiki.nodes import NodeRec, Relation, cluster, member_key
from evowiki.wikiapp.events import GoalMapSaved, GraphSaved, GroupName, GroupsNamed, WikiEvent
from evowiki.wikiapp.goal_groups import outside_goals
from evowiki.wikiapp.graph_out import (
    evidence_line,
    fallback_name,
    graph_out,
    inherited_name,
)
from evowiki.wikiapp.path_edits import SKIP_WORDS, edit_path, skip_plan
from evowiki.wikiapp.paths import path_items
from evowiki.wikiapp.repo import Commit, MemoryRepo
from evowiki.wikiapp.wiki_schemas import PathMoveIn
from tests.wikiapp.test_path_edits import ASPECTS, MAP_ID, NOW, PATH_ID, Ids, seed_events


@pytest.fixture
def env():
    repo, ids = MemoryRepo(["u1"]), Ids()
    aspect, wiki = seed_events(ids)
    aspect.append(DefineConcept(event_id=ids("aev"), ts=NOW, concept_id="r", aspects=ASPECTS))
    repo.commit(repo.load("u1"), Commit(aspect=aspect, wiki=wiki))
    return repo, ids


def out(env):
    snap = env[0].load("u1")
    return graph_out(snap.state, snap.k, NOW)


def by_id(g):
    return {n.id: n for n in g.nodes}


def declare(env, node, value, note=True):
    repo, ids = env
    ev = []
    for a in ASPECTS:
        c = cell_id(node, a)
        ev.append(Declare(event_id=ids("aev"), ts=NOW, cell=c,
                          value=None if value == "clear" else value))
        if note:
            ev.append(AddNote(event_id=ids("aev"), ts=NOW, cell=c, text=value,
                              evidence_ref=f"declare:{c}"))
    repo.commit(repo.load("u1"), Commit(aspect=ev))


def save_graph(env, mutate):
    repo, ids = env
    snap = repo.load("u1")
    g = mutate(snap.state.graph)
    repo.commit(snap, Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=NOW,
                                             body=GraphSaved(graph=g, action="user_edit"))]))


def test_every_node_and_relation_is_served(env):
    g = out(env)
    assert sorted(by_id(g)) == ["n1", "n2", "n3", "n4", "n5", "r"]
    assert {(r.kind, r.src, r.dst) for r in g.relations} >= {
        ("part_of", "n1", "r"), ("prerequisite", "n1", "n2"), ("prerequisite", "n2", "n3")}
    assert next(r for r in g.relations if r.src == "n1" and r.dst == "n2").phrase == "先有样本"
    assert by_id(g)["r"].can_split is False and by_id(g)["n1"].can_split is True


def test_direct_and_aggregate_are_kept_apart(env):
    declare(env, "n1", "know")
    g = by_id(out(env))
    n1, r = g["n1"], g["r"]
    assert n1.direct.get("known", 0) + n1.direct.get("likely", 0) == pytest.approx(1)
    assert n1.aggregate == {}                     # no descendants
    assert r.direct == {"known": 0, "likely": 0, "shaky": 0, "gap": 0, "none": 1.0}
    # r aggregates the cells of n1..n4 (8 cells), 2 of which are n1's
    assert r.aggregate["none"] == pytest.approx(6 / 8)
    assert sum(r.mix.values()) == pytest.approx(1)
    assert g["n5"].direct["none"] == 1.0 and g["n5"].band == "none"

    declare(env, "r", "dont")                     # direct evidence on the big node itself
    g2 = by_id(out(env))
    assert g2["r"].direct["gap"] + g2["r"].direct["shaky"] == pytest.approx(1)
    assert g2["r"].aggregate == r.aggregate       # part_of never propagates: children unchanged
    assert g2["n1"].direct == n1.direct and g2["n2"].band == g["n2"].band


def test_a_node_without_cells_has_empty_shares(env):
    save_graph(env, lambda g: g.with_node(NodeRec(id="x", name="新点", x=0.5, y=0.5)))
    x = by_id(out(env))["x"]
    assert x.direct == {} and x.aggregate == {} and x.mix == {} and x.band == "none"
    assert x.evidence == []


def test_all_parents_and_kind_phrase(env):
    def more(g):
        g = g.with_node(NodeRec(id="r2", name="贝叶斯统计", x=-0.5, y=0.2))
        g = g.with_relation(Relation(kind="part_of", src="n5", dst="r2"))
        g = g.with_relation(Relation(kind="part_of", src="n5", dst="r"))
        g = g.with_relation(Relation(kind="kind_of", src="n4", dst="n3"))
        return g.with_relation(Relation(kind="kind_of", src="n2", dst="n1", phrase="一种分布"))
    save_graph(env, more)
    g = by_id(out(env))
    assert sorted(g["n5"].parents) == ["r", "r2"]
    assert g["n4"].kind == "一种置信区间" and g["n2"].kind == "一种分布" and g["n1"].kind == ""


def test_evidence_lines_are_plain_words_once(env):
    repo, ids = env
    plan = skip_plan(repo, None, "u1", "n2", None, now=NOW, new_id=ids)
    repo.commit(plan.snap, Commit(aspect=list(plan.aspect), wiki=list(plan.wiki)))
    declare(env, "n1", "know")
    declare(env, "n3", "clear")
    repo.commit(repo.load("u1"), Commit(aspect=[
        AddNote(event_id=ids("aev"), ts=NOW, cell=cell_id("n4", "term"), text="8/3",
                evidence_ref="quiz:d1:0"),
        AddNote(event_id=ids("aev"), ts=NOW, cell=cell_id("n4", "purpose"), text="x",
                evidence_ref="weird:1")]))
    g = by_id(out(env))
    assert g["n2"].evidence == [SKIP_WORDS]
    assert g["n1"].evidence == ["你说：我会"]
    assert g["n3"].evidence == []                 # 清除声明 is not evidence
    assert g["n4"].evidence == ["答了一道题，选了「8/3」"]
    for n in g.values():
        for line in n.evidence:
            assert "n1" not in line and "cell" not in line and ":" not in line.replace("：", "")


def test_evidence_line_prefers_the_label():
    class N:
        text, evidence_ref, label = "8/3", "quiz:d:0", "测验答对：两个向量的点积"
    assert evidence_line(N()) == "测验答对：两个向量的点积"


def test_positions_present_and_deterministic(env):
    save_graph(env, lambda g: g.with_node(NodeRec(id="m", name="没有位置")).with_relation(
        Relation(kind="part_of", src="m", dst="r")))
    snap = env[0].load("u1")
    assert snap.state.graph.node("m").x is None
    a, b = out(env), out(env)
    assert a == b
    m = by_id(a)["m"]
    assert -1 <= m.x <= 1 and -1 <= m.y <= 1
    assert all(-1 <= n.x <= 1 and -1 <= n.y <= 1 for n in a.nodes)
    assert by_id(a)["n1"].x == pytest.approx(0.0) and by_id(a)["n1"].y == pytest.approx(0.1)


def test_a_goal_is_its_blocks_and_the_points_on_no_goal_get_fallback_names(env):
    g = out(env)
    goal = next(x for x in g.groups if x.level == 2)
    block = next(x for x in g.groups if x.parent == goal.id)
    assert (goal.id, goal.map_id, goal.name) == (f"goal-{MAP_ID}", MAP_ID, g.goals[0].name)
    assert block.map_id == MAP_ID and block.name == by_id(g)["r"].name
    assert sorted(block.members) == ["n1", "n2", "n3", "n4"]        # r is the picture of what it contains
    rest = [x for x in g.groups if x.map_id is None]
    assert sorted(m for x in rest for m in x.members) == ["n5"]
    assert all(x.id.endswith(member_key(x.members)) and x.summary == "" for x in rest)
    assert rest[0].name == "贝叶斯"
    pos = {n.id: (n.x, n.y) for n in g.nodes}
    for x in g.groups:
        assert x.x == pytest.approx(sum(pos[m][0] for m in x.members) / len(x.members))
        assert x.y == pytest.approx(sum(pos[m][1] for m in x.members) / len(x.members))


def test_fallback_name_uses_the_two_most_central_members(env):
    graph = env[0].load("u1").state.graph
    # r has 4 part_of relations, n2 has two prerequisites (n1→n2→n3) and part_of r
    assert fallback_name(graph, ["n1", "n2", "n3", "n4", "r"]) == "统计推断、抽样分布等"
    assert fallback_name(graph, ["n1", "n2"]) == "总体与样本、抽样分布"
    assert fallback_name(graph, ["n5"]) == "贝叶斯"


def test_stored_and_inherited_group_names(env):
    repo, ids = env
    snap = repo.load("u1")
    first = cluster(outside_goals(snap.state))[0]
    repo.commit(snap, Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=NOW, body=GroupsNamed(
        names=(GroupName(key=member_key(first.members), name="统计推断", summary="从样本推断总体",
                         level=first.level, members=first.members),)))]))
    named = next(x for x in out(env).groups if x.id == first.id)
    assert (named.name, named.summary) == ("统计推断", "从样本推断总体")

    # a near copy (one member more) inherits it; a different level does not
    names = {"k": GroupName(key="k", name="统计推断", summary="", level=1, members=("a", "b", "c"))}
    near = ("a", "b", "c", "zz")
    assert inherited_name(near, 1, names).name == "统计推断"
    assert inherited_name(near, 2, names) is None
    assert inherited_name(("other1", "other2"), 1, names) is None


def test_goals_shares_and_path_as_shown(env):
    declare(env, "n1", "know")
    g = out(env)
    (goal,) = g.goals
    assert goal.map_id == MAP_ID and goal.goal == "学统计推断"
    assert goal.nodes == ["r"]
    assert sum(goal.shares.values()) == pytest.approx(1)
    assert goal.path_id == PATH_ID
    snap = env[0].load("u1")
    assert goal.path == path_items(snap.state, snap.state.paths[PATH_ID], snap.k, NOW)

    edit_path(env[0], "u1", PATH_ID, PathMoveIn(op="move", **{"from": 2}, to=0), now=NOW,
              new_id=env[1])
    moved = out(env).goals[0].path
    assert [p.key for p in moved] == [f"{PATH_ID}-p3", f"{PATH_ID}-p1", f"{PATH_ID}-p2"]


def test_goal_subset_is_the_maps_graph_nodes_and_a_goal_without_path(env):
    repo, ids = env
    gmap = GoalMap(map_id="map-2", goal="学贝叶斯", created_at=NOW,
                   nodes=(MapNode(id="n5", kind="concept", name="贝叶斯"),
                          MapNode(id="ghost", kind="concept", name="不在图里")))
    repo.commit(repo.load("u1"), Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=NOW,
                                                        body=GoalMapSaved(record=gmap,
                                                                          action="planned"))]))
    goals = out(env).goals
    assert [x.map_id for x in goals] == [MAP_ID, "map-2"]
    assert goals[1].nodes == ["n5"] and goals[1].path_id is None and goals[1].path == []
    assert goals[1].shares == {"known": 0, "shaky": 0, "gap": 0, "none": 1.0}


def test_empty_graph():
    repo = MemoryRepo(["u1"])
    snap = repo.load("u1")
    g = graph_out(snap.state, snap.k, NOW)
    assert g.nodes == [] and g.relations == [] and g.groups == [] and g.goals == []


def test_goal_name_is_the_intake_title_then_the_sentence_never_a_target_block():
    from evowiki.wikiapp.graph_out import goal_name
    mk = {"map_id": "m", "created_at": NOW}
    target = GoalMap(goal="我要学孔雀鱼", **mk, nodes=(
        MapNode(id="a", kind="region", name="鱼缸与水质", target=True),))
    assert goal_name(target) == "孔雀鱼"
    titled = GoalMap(goal="复杂网络中的渗流", title="复杂网络", **mk, nodes=(
        MapNode(id="a", kind="region", name="随机图", target=True),))
    assert goal_name(titled) == "复杂网络"
    assert goal_name(GoalMap(goal="我要学机器学习", **mk, nodes=())) == "机器学习"


def test_goal_out_carries_both_the_name_and_the_sentence(env):
    (goal,) = out(env).goals
    assert goal.goal == "学统计推断" and goal.name
