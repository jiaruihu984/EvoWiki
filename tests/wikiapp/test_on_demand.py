"""On-demand decomposition, stage 1 (spec 2026-10-28-on-demand-decomposition-design §2.2–2.4,
§6 第 1 阶段): entering a node splits it one level or shows its parts and plans nothing; parts
that are not atomic are entered again (any depth); an invalid split is asked once more, then
fails and the node stays unexpanded; opening a point diagnoses only it (and its unlearned
prerequisites) when K has no evidence, and its path hangs on that point; 继续拆细 (the same
one-level split, wikiapp.node_split) marks the node not atomic and writes only the map it came from; older data (`atomic` None) keeps its
meaning. Scripted LLM only."""

import json

import pytest

from evowiki.belief.aspects import AspectKView, Declare
from evowiki.goals.blocks import Block
from evowiki.goals.intake import IntakeError
from evowiki.goals.regions import GoalMap, MapNode
from evowiki.goals.split_check import Part, check_split
from evowiki.nodes.model import NodeRec, Relation
from evowiki.wikiapp.events import ConceptName, ConceptsNamed, GoalMapSaved, WikiEvent
from evowiki.wikiapp.goal import create_goal_from_blocks, enter_region, stop_draft
from evowiki.wikiapp.graph_sync import graph_events
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.maps import map_out
from evowiki.wikiapp.node_split import split_node
from evowiki.wikiapp.node_state import enter_kind
from evowiki.wikiapp.repo import Commit, MemoryRepo
from evowiki.wikiapp.state import replay
from tests.goals.fakes_intake import ESTIMATE_MARK
from tests.wikiapp.goal_script import PROBE, auto_probe, opening_answered, push_round
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.verify import verify_prereqs

GOAL = "我想养黑壳虾"
BLOCKS = (Block(name="认识黑壳虾", covers=("外形", "习性")),
          Block(name="把水养好", covers=("换水", "水质检测", "pH"), target=True))
INTAKE = "goals.enter"                 # S2: entering a block (enter@v1)


@pytest.fixture
def env():
    s = Script()
    auto_probe(s)
    return MemoryRepo(["u1"]), s, Ids()


def gw(env):
    return CountingGateway(env[1].gateway())


def state(env):
    return env[0].load("u1").state


def goal(env) -> str:
    repo, _, ids = env
    res = create_goal_from_blocks(repo, gw(env), "u1", GOAL, BLOCKS, now=NOW, new_id=ids,
                                  title="黑壳虾")
    stop_draft(repo, gw(env), "u1", res.draft.draft_id, now=NOW, new_id=ids)
    return res.draft.map_id


def enter(env, map_id, node):
    repo, _, ids = env
    return enter_region(repo, gw(env), "u1", map_id, node, now=NOW, new_id=ids)


def parts(*names_atomic):
    return {"parts": [{"name": n, "summary": f"{n}是什么", "atomic": a} for n, a in names_atomic]}


def kids(env, map_id, node):
    return [(n.id, n.name, n.kind, n.state) for n in state(env).maps[map_id].children(node)]


# --- entering: one level, any depth, never planned -------------------------------------------

def test_three_levels_each_entered_split_once_then_shown_without_a_call(env):
    repo, s, _ = env
    map_id = goal(env)
    assert state(env).names["c2"].atomic is False                    # a block is never atomic
    s.push(INTAKE, parts(("换水", True), ("水质", False)))
    out = enter(env, map_id, "c2")
    assert out.map_id == map_id and out.path_id is None and out.draft is None
    assert kids(env, map_id, "c2") == [("c3", "换水", "concept", "collapsed"),
                                       ("c4", "水质", "region", "collapsed")]
    s.push(INTAKE, parts(("氨氮", True), ("pH", False)))
    enter(env, map_id, "c4")                                         # level 2
    p = s.requests(INTAKE)[-1].prompt
    assert "「把水养好」下面的一部分「水质」" in p and "「把水养好」一共拆成这几部分" in p
    assert "1. 「换水」（换水是什么）\n2. 「水质」（水质是什么） ← 这一块" in p
    s.push(INTAKE, parts(("pH 是什么", True), ("怎样调 pH", True)))
    enter(env, map_id, "c6")                                         # level 3
    assert "「把水养好」›「水质」下面的一部分「pH」" in s.requests(INTAKE)[-1].prompt
    st = state(env)
    assert [n for n, *_ in kids(env, map_id, "c6")] == ["c7", "c8"]
    assert set(st.graph.children("c6")) == {"c7", "c8"} and st.graph.parents("c6") == ("c4",)
    assert st.names["c6"].atomic is False and st.names["c7"].atomic is True
    assert s.calls(PROBE) == 0 and s.calls("goals.path") == 0        # the opening asks in the chat
    assert state(env).paths == {}
    calls, log = s.calls(), len(repo.wiki_log("u1"))
    for node in ("c2", "c4", "c6"):                                  # expanded: shown, 0 calls
        assert enter(env, map_id, node).map_id == map_id
    assert s.calls() == calls and len(repo.wiki_log("u1")) == log
    view = {n.id: n for n in map_out(state(env), repo.load("u1").k, map_id, NOW).nodes}
    assert [view[c].enter for c in ("c2", "c3", "c4", "c6", "c7")] == [
        "children", "learn", "children", "children", "learn"]
    assert view["c1"].enter == "split" and view["c1"].atomic is False


def test_a_split_naming_the_block_itself_is_asked_once_more_then_fails_and_stays_unexpanded(env):
    repo, s, _ = env
    map_id = goal(env)
    bad = parts(("把 水养好", True), ("换水", True))                   # the block, by norm_name
    s.push(INTAKE, bad, bad)
    log = len(repo.wiki_log("u1"))
    with pytest.raises(IntakeError, match="至少要 2 个不同的部分"):
        enter(env, map_id, "c2")
    assert s.calls(INTAKE) == 2
    assert "上一次的拆法不能用" in s.requests(INTAKE)[1].prompt
    assert len(repo.wiki_log("u1")) == log                           # nothing written
    m = state(env).maps[map_id]
    assert m.node("c2").state == "collapsed" and m.children("c2") == ()
    assert state(env).graph.children("c2") == ()


def test_an_invalid_split_whose_retry_is_usable_is_written(env):
    _, s, _ = env
    map_id = goal(env)
    s.push(INTAKE, parts(("把水养好", False), ("换水", True)), parts(("换水", True), ("pH", True)))
    enter(env, map_id, "c2")
    assert s.calls(INTAKE) == 2
    assert [n for _, n, *_ in kids(env, map_id, "c2")] == ["换水", "pH"]


def test_a_part_that_is_an_ancestor_is_dropped_and_two_parts_reusing_one_node_are_invalid(env):
    up = [("c6", "pH"), ("c4", "水质"), ("c2", "把水养好")]
    ok = check_split(up, [Part("name:a", "甲"), Part("c4", "水质"), Part("name:b", "乙")])
    assert ok.problem is None and ok.usable == (0, 2)
    same = check_split(up, [Part("c9", "甲"), Part("c9", "甲的别名")])
    assert same.problem is not None and "只有 1 个不同的点" in same.problem
    assert check_split(up, []).problem is not None


# --- opening a point ---------------------------------------------------------------------------

def test_opening_a_point_diagnoses_only_it_and_its_unlearned_prerequisite_then_hangs_its_path(env):
    repo, s, ids = env
    map_id = goal(env)
    opening_answered(repo, map_id, now=NOW, new_id=ids)
    s.push(INTAKE, parts(("换水", True), ("水质检测", True), ("pH", True)))
    enter(env, map_id, "c2")
    verify_prereqs(repo, "u1", [("c4", "c5")], now=NOW, new_id=ids)   # 水质检测 → pH
    push_round(s, [("c5.term", "pH 是？", ["酸碱度", "温度", "硬度"], 0)])
    opened = enter(env, map_id, "c5")
    assert opened.open and opened.draft.region_id == "c5"
    assert [c.id for c in opened.draft.concepts] == ["c5", "c4"] and opened.draft.targets == ("c5",)
    pick = opened.draft.rounds[0].pick                                # only it and its prerequisite
    assert pick.node in ("c5", "c4") and "c3" not in {pick.node, *(m for m, _ in pick.members)}
    assert state(env).maps[map_id].node("c5").state == "expanded"
    assert enter(env, map_id, "c5").draft.draft_id == opened.draft.draft_id   # still open: 0 calls


def test_opening_a_point_k_has_evidence_on_skips_the_diagnosis_and_plans_only_that_point(env):
    repo, s, ids = env
    map_id = goal(env)
    s.push(INTAKE, parts(("换水", True), ("pH", True)))
    enter(env, map_id, "c2")
    sn = repo.load("u1")
    repo.commit(sn, Commit(aspect=[Declare(event_id=ids("aev"), ts=NOW, cell=f"c3.{a}",
                                           value="dont") for a in sn.k.aspects("c3")]))
    probes, calls = s.calls(PROBE), s.calls()
    out = enter(env, map_id, "c3")
    assert s.calls(PROBE) == probes and s.calls() == calls            # one concept: no call at all
    st = state(env)
    m = st.maps[map_id]
    assert out.path_id is not None and m.node("c3").path_id == out.path_id
    assert m.node("c2").path_id is None and m.node("c4").path_id is None
    path = st.paths[out.path_id].path
    assert [p.targets for p in path.pages] == [("c3",)]


# --- 继续拆细 (wikiapp.node_split, the same goals.enter call) and the other goals ---------------

def _split_answer():
    return parts(("换水频率", True), ("换水比例", True))


def test_split_marks_not_atomic_writes_only_its_own_map_and_leaves_k_alone(env):
    repo, s, ids = env
    map_id = goal(env)
    s.push(INTAKE, parts(("换水", True), ("pH", True)))
    enter(env, map_id, "c2")
    other = GoalMap(map_id="map-other", goal="另一个目标", created_at=NOW,
                    nodes=(MapNode(id="c3", kind="concept", name="换水"),))
    repo.commit(repo.load("u1"), Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=NOW, body=(
        GoalMapSaved(record=other, action="planned")))]))
    before = repo.load("u1")
    n_log = len(repo.wiki_log("u1"))
    s.push(INTAKE, _split_answer())
    done = split_node(repo, gw(env), "u1", "c3", reason="requested", source="continue", now=NOW,
                      new_id=ids, map_id=map_id)
    st = state(env)
    a, b = done.children
    assert set(st.graph.children("c3")) == {a, b} and st.names["c3"].atomic is False
    assert st.names[a].atomic is True and st.names[b].atomic is True    # the model's first call
    m = st.maps[map_id]
    assert [n.id for n in m.children("c3")] == [a, b] and m.node("c3").state == "expanded"
    assert st.maps["map-other"] == before.state.maps["map-other"]     # no event for other maps
    assert [e.body.record.map_id for e in repo.wiki_log("u1")[n_log:]
            if isinstance(e.body, GoalMapSaved)] == [map_id]
    kv0, kv1 = before.kview(NOW), repo.load("u1").kview(NOW)
    assert all(kv1.cell(f"c3.{x.value}") == kv0.cell(f"c3.{x.value}") for x in kv0.aspects("c3"))
    # the other goal does not take these parts: its first entry splits the node itself (one call)
    calls = s.calls()
    assert enter_kind(st, st.maps["map-other"], st.maps["map-other"].node("c3")) == "children"
    s.push(INTAKE, parts(("换水时机", True), ("换水水量", True)))
    enter(env, "map-other", "c3")
    assert s.calls() == calls + 1
    theirs = {n.id for n in state(env).maps["map-other"].children("c3")}
    assert len(theirs) == 2 and not theirs & {a, b}


# --- older data: atomic None ------------------------------------------------------------------

def _old_map(env, nodes, graph_nodes=(), relations=()):
    repo, _, ids = env
    snap = repo.load("u1")
    g = snap.state.graph.updated(nodes=[NodeRec(id=i, name=n) for i, n in graph_nodes],
                                 relations=list(relations))
    aspect, wiki = graph_events(snap.state, snap.k, g, "goal_nodes", now=NOW, new_id=ids)
    m = GoalMap(map_id="map-old", goal="旧目标", created_at=NOW, nodes=nodes)
    wiki.append(WikiEvent(event_id=ids("wev"), ts=NOW, body=GoalMapSaved(record=m,
                                                                         action="planned")))
    repo.commit(snap, Commit(aspect=aspect, wiki=wiki))
    return state(env).maps["map-old"]


def test_old_node_with_parts_in_the_graph_is_expanded(env):
    m = _old_map(env, (MapNode(id="r1", kind="region", name="旧块"),
                       MapNode(id="k1", kind="concept", name="甲", parent="r1", state="expanded"),
                       MapNode(id="k2", kind="concept", name="乙", parent="r1", state="expanded")),
                 [("r1", "旧块"), ("k1", "甲"), ("k2", "乙")],
                 [Relation(kind="part_of", src="k1", dst="r1"),
                  Relation(kind="part_of", src="k2", dst="r1")])
    st = state(env)
    assert st.names["r1"].atomic is None
    assert enter_kind(st, m, m.node("r1")) == "children"
    assert enter(env, "map-old", "r1").map_id == "map-old" and env[1].calls() == 0


def test_old_top_level_region_with_no_parts_is_split_when_entered(env):
    _, s, _ = env
    m = _old_map(env, (MapNode(id="r1", kind="region", name="旧块", scope="甲、乙"),),
                 [("r1", "旧块")])
    assert enter_kind(state(env), m, m.node("r1")) == "split"
    s.push(INTAKE, parts(("甲", True), ("乙", True)))
    enter(env, "map-old", "r1")
    assert [n for _, n, *_ in kids(env, "map-old", "r1")] == ["甲", "乙"]
    assert state(env).names["r1"].atomic is False


def test_old_concept_leaf_is_learned_as_one_and_never_marked_atomic(env):
    repo, s, _ = env
    m = _old_map(env, (MapNode(id="r1", kind="region", name="旧块", members=("k1", "k2")),
                       MapNode(id="k1", kind="concept", name="甲", parent="r1", state="expanded"),
                       MapNode(id="k2", kind="concept", name="乙", parent="r1", state="expanded")),
                 [("r1", "旧块"), ("k1", "甲"), ("k2", "乙")],
                 [Relation(kind="part_of", src="k1", dst="r1"),
                  Relation(kind="part_of", src="k2", dst="r1")])
    assert enter_kind(state(env), m, m.node("k1")) == "learn"
    # the all-at-once route's region (members fixed at creation) is opened as one path, as before
    assert enter_kind(state(env), m, m.node("r1")) == "learn"
    view = {n.id: n for n in map_out(state(env), repo.load("u1").k, "map-old", NOW).nodes}
    assert (view["k1"].atomic, view["k1"].enter) == (None, "learn")
    push_round(s, [("k1.term", "甲是？", ["对", "错", "错二"], 0)])
    assert enter(env, "map-old", "k1").open
    assert state(env).names["k1"].atomic is None                     # never written back


def test_an_older_concept_name_event_without_atomic_replays_unchanged(env):
    repo, _, _ = env
    goal(env)
    log = repo.wiki_log("u1")
    raw = [json.loads(e.model_dump_json()) for e in log]
    for e in raw:                       # events written before the field existed
        if e["body"]["type"] == "concepts_named":
            for n in e["body"]["names"]:
                n.pop("atomic", None)
    old = [WikiEvent.model_validate(e) for e in raw]
    st = replay(old)
    assert all(n.atomic is None for n in st.names.values())
    assert st.maps == state(env).maps
    named = ConceptsNamed(names=(ConceptName(concept_id="x", name="x"),))
    assert named.names[0].atomic is None


def test_an_atomic_block_is_opened_to_learn_and_never_split(env):
    """User ruling 2026-10-28 (Q11): atomic is the content's call at any level — a block that
    is one thing to learn (「学贝叶斯公式」) is learned when opened, with no split call."""
    repo, s, ids = env
    blocks = (Block(name="条件概率", covers=("条件概率",), atomic=True),
              Block(name="贝叶斯公式", covers=("先验", "后验"), target=True))
    res = create_goal_from_blocks(repo, gw(env), "u1", "学贝叶斯公式", blocks, now=NOW,
                                  new_id=ids, title="贝叶斯公式")
    stop_draft(repo, gw(env), "u1", res.draft.draft_id, now=NOW, new_id=ids)
    opening_answered(repo, res.draft.map_id, now=NOW, new_id=ids)
    st = state(env)
    assert (st.names["c1"].atomic, st.names["c2"].atomic) == (True, False)
    m = st.maps[res.draft.map_id]
    assert [enter_kind(st, m, n) for n in m.nodes] == ["learn", "split"]
    push_round(s, [("c1.term", "条件概率是？", ["对", "错一", "错二"], 0)])
    out = enter(env, res.draft.map_id, "c1")
    assert out.open and out.draft.region_id == "c1" and s.calls(INTAKE) == 0


# --- S2: one call per entry splits and estimates from his words (enter@v1) ----------------------

WORDS = (GOAL, "换水我自己会做，水质检测完全没学过")


def said_goal(env, blocks=BLOCKS, **kw) -> str:
    repo, _, ids = env
    res = create_goal_from_blocks(repo, gw(env), "u1", GOAL, blocks, now=NOW, new_id=ids,
                                  title="黑壳虾", words=WORDS, **kw)
    if res.open:
        stop_draft(repo, gw(env), "u1", res.draft.draft_id, now=NOW, new_id=ids)
    return res.map_id or res.draft.map_id


def test_entering_is_one_call_that_records_his_words_per_cell_and_passes_them_down(env):
    repo, s, _ = env
    map_id = said_goal(env)
    calls = {p: s.calls(p) for p in (PROBE, "goals.path")}
    s.push(INTAKE, {**parts(("换水", True), ("水质检测", False)), "known": [
        {"part": "换水", "level": "intermediate", "quote": "换水我自己会做"},
        {"part": "水质检测", "level": "unlearned", "quote": "水质检测完全没学过"},
        {"part": "pH", "level": "advanced", "quote": "pH 很熟"}]})            # no such part
    out = enter(env, map_id, "c2")
    assert s.calls(INTAKE) == 1 and out.draft is None and out.path_id is None
    assert {p: s.calls(p) for p in calls} == calls                  # no probe, no path
    p = s.requests(INTAKE)[0].prompt
    assert ESTIMATE_MARK in p and "换水我自己会做，水质检测完全没学过" in p
    st, k = state(env), AspectKView(repo.load("u1").k, NOW)
    assert all(k.cell(f"c3.{a}").direct > 0 for a in k.aspects("c3"))   # 换水: his words, + cells
    m = st.maps[map_id]
    assert m.node("c3").said == ("换水我自己会做",) and m.node("c4").said == ("水质检测完全没学过",)
    assert st.paths == {} and not [pg for pg in st.pages.values() if pg.has_body]
    # the non-atomic child: its own split estimates from its words, again one call
    s.push(INTAKE, parts(("氨氮", True), ("pH", True)))
    enter(env, map_id, "c4")
    p = s.requests(INTAKE)[1].prompt
    assert s.calls(INTAKE) == 2 and ESTIMATE_MARK in p and "水质检测完全没学过" in p
    # opening the point his words cover: K is enough, no probe; its path is planned only now
    probes = s.calls(PROBE)
    opened = enter(env, map_id, "c3")
    assert s.calls(PROBE) == probes and opened.path_id is not None
    assert state(env).maps[map_id].node("c3").path_id == opened.path_id


def test_a_block_not_to_diagnose_and_its_parts_are_split_without_an_estimate(env):
    _, s, _ = env
    blocks = (BLOCKS[0], BLOCKS[1].model_copy(update={"diagnose": False}))
    map_id = said_goal(env, blocks)
    s.push(INTAKE, {**parts(("换水", True), ("水质检测", False)),
                    "known": [{"part": "换水", "level": "advanced", "quote": "换水我自己会做"}]})
    enter(env, map_id, "c2")
    s.push(INTAKE, parts(("氨氮", True), ("pH", True)))
    enter(env, map_id, "c4")                                         # under a no-diagnose block
    for r in s.requests(INTAKE):
        assert ESTIMATE_MARK not in r.prompt and "换水我自己会做" not in r.prompt
    assert state(env).maps[map_id].node("c3").said == ()


def test_a_stated_beginners_map_gets_no_estimate(env):
    _, s, _ = env
    map_id = said_goal(env, beginner="完全零基础")
    s.push(INTAKE, parts(("换水", True), ("水质检测", False)))
    enter(env, map_id, "c2")
    assert s.calls(INTAKE) == 1 and ESTIMATE_MARK not in s.requests(INTAKE)[0].prompt


def test_a_map_without_his_words_gets_no_estimate(env):
    _, s, _ = env
    map_id = goal(env)                                               # no words, nothing said
    s.push(INTAKE, parts(("换水", True), ("水质检测", False)))
    enter(env, map_id, "c2")
    assert ESTIMATE_MARK not in s.requests(INTAKE)[0].prompt
