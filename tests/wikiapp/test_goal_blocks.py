"""A goal from confirmed blocks (chat-first blocks, spec 2026-10-23, plan B1.2): no intake call;
each block is a graph node + K concept and a top-level region with no members; the opening probe
asks about the blocks only; entering a block runs the scoped intake with every block's covers and
one level; the 学习路径 card's edits (reorder / drop / rename / add, and the inverse ops 撤销 sends)
are one GoalMapSaved("edited"); GET of a draft's state. Scripted LLM only."""

import pytest

from evowiki.goals.blocks import Block
from evowiki.goals.path import goal_key
from evowiki.nodes.model import NodeRec
from evowiki.wikiapp.events import GoalMapSaved, GraphSaved
from evowiki.wikiapp.goal import (
    create_goal_from_blocks,
    draft_status,
    enter_region,
    stop_draft,
)
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.map_edits import edit_map
from evowiki.wikiapp.maps import map_out
from evowiki.wikiapp.repo import MemoryRepo, WikiConflict, WikiInvalid
from evowiki.wikiapp.state import replay
from evowiki.wikiapp.wiki_schemas import MapEditIn
from tests.goals.fakes_intake import ESTIMATE_MARK
from tests.wikiapp.goal_script import (
    PROBE,
    answer_round,
    auto_probe,
    opening_answered,
    push_no_question,
)
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids

GOAL = "我想养黑壳虾"
BLOCKS = (Block(name="认识黑壳虾", covers=("外形", "习性"), summary="它是什么虾"),
          Block(name="把水养好", covers=("换水", "水质检测", "pH"), summary="水的要求和管理"),
          Block(name="日常喂养", covers=("饲料", "喂食频率"), summary="喂什么、喂多少",
                target=True),
          Block(name="繁殖", covers=("抱卵", "幼虾"), target=True))


@pytest.fixture
def env():
    s = Script()
    auto_probe(s)
    return MemoryRepo(["u1"]), s, Ids()


def gw(env):
    return CountingGateway(env[1].gateway())


def state(env):
    return env[0].load("u1").state


def make(env, blocks=BLOCKS, **kw):
    repo, _, ids = env
    return create_goal_from_blocks(repo, gw(env), "u1", GOAL, blocks, now=NOW, new_id=ids,
                                   title="黑壳虾", **kw)


def edit(env, map_id, *ops):
    repo, _, ids = env
    return edit_map(repo, "u1", map_id, MapEditIn.model_validate({"ops": list(ops)}).ops,
                    now=NOW, new_id=ids)


def test_blocks_become_top_level_regions_without_members(env):
    res = make(env)
    _, s, _ = env
    assert s.calls("goals.intake") == 0 and s.calls("goals.enter") == 0
    st = state(env)
    m = st.maps[res.draft.map_id]
    assert m.map_id == f"map-{goal_key(GOAL, ['c3', 'c4'])}" and m.title == "黑壳虾"
    assert [(n.id, n.name, n.kind, n.parent, n.members, n.scope) for n in m.nodes] == [
        ("c1", "认识黑壳虾", "region", None, (), "外形、习性"),
        ("c2", "把水养好", "region", None, (), "换水、水质检测、pH"),
        ("c3", "日常喂养", "region", None, (), "饲料、喂食频率"),
        ("c4", "繁殖", "region", None, (), "抱卵、幼虾")]
    assert [n.target for n in m.nodes] == [False, False, True, True]
    # every block is a graph node (with its summary) and a K concept, so it can be asked about
    graph = {n.id: n for n in st.graph.nodes}
    assert graph["c2"].name == "把水养好" and graph["c2"].definition == "水的要求和管理"
    assert {"c1", "c2", "c3", "c4"} <= set(env[0].load("u1").k.concept_ids())
    # the opening diagnosis is about the blocks, and only them (one pick a round)
    assert res.draft.rounds[0].pick.node in ("c1", "c2", "c3", "c4")
    assert res.open and res.draft.title == "黑壳虾"
    view = map_out(st, env[0].load("u1").k, m.map_id, NOW)
    assert view.title == "黑壳虾" and view.goal == GOAL
    assert view.model_dump()["title"] == res.draft.title


def test_no_target_marked_targets_every_block(env):
    blocks = tuple(b.model_copy(update={"target": False}) for b in BLOCKS)
    first = make(env, blocks)
    m = state(env).maps[first.draft.map_id]
    assert all(n.target for n in m.nodes)


def test_same_goal_again_without_progress_replaces_the_map_with_the_new_blocks(env):
    """H4 (a): the map nobody started is replaced by the newly confirmed blocks (same map id,
    a new GoalMapSaved "planned"), its open opening is closed, and a first round runs as for a
    new goal; the draft returned is stored (GET answers it)."""
    repo, _, ids = env
    first = make(env)
    map_id = first.draft.map_id
    again = make(env)                                   # the opening still open
    assert again.open and again.draft.draft_id != first.draft.draft_id
    assert again.draft.map_id == map_id and len(state(env).maps) == 1
    assert first.draft.draft_id in state(env).closed_drafts
    assert draft_status(repo, "u1", again.draft.draft_id).open
    stop_draft(repo, gw(env), "u1", again.draft.draft_id, now=NOW, new_id=ids)
    # different non-target blocks, same target blocks → the same map id, the NEW blocks
    b2 = (*BLOCKS[:1], *BLOCKS[2:], Block(name="造景", covers=("水草",)))
    third = make(env, b2)
    assert third.draft.map_id == map_id and third.draft.draft_id in state(env).drafts
    assert [n.name for n in state(env).maps[map_id].nodes] == ["认识黑壳虾", "日常喂养", "繁殖",
                                                               "造景"]
    saved = [e.body for e in repo.wiki_log("u1") if isinstance(e.body, GoalMapSaved)
             and e.body.record.map_id == map_id]
    assert [x.action for x in saved].count("planned") == 3
    assert draft_status(repo, "u1", third.draft.draft_id).open


def test_same_goal_again_with_progress_is_refused(env):
    """H4 (b): a block entered (being learned) → WikiConflict, nothing written."""
    repo, s, ids = env
    res = make(env)
    stop_draft(repo, gw(env), "u1", res.draft.draft_id, now=NOW, new_id=ids)
    s.push("goals.intake", {"blocks": [{"name": "外形", "page": True},
                                       {"name": "习性", "page": True}]})
    enter_region(repo, gw(env), "u1", res.draft.map_id, "c1", now=NOW, new_id=ids)   # split
    log = len(repo.wiki_log("u1"))
    with pytest.raises(WikiConflict, match="这个目标已经在学了：在学习路径里改分块"):
        make(env)
    assert len(repo.wiki_log("u1")) == log


def test_two_goals_with_a_block_of_the_same_name_are_two_nodes(env):
    """H1 (the 斗鱼 / 黑壳虾 collision): 黑壳虾's 繁殖 (no existing_id) never takes over 斗鱼's
    繁殖 node with its path and K: it gets a goal-local id and starts collapsed."""
    repo, s, ids = env
    fish = (Block(name="缸与水", covers=("缸",)), Block(name="繁殖", covers=("泡巢", "配对"),
                                                    target=True))
    res = create_goal_from_blocks(repo, gw(env), "u1", "我想养斗鱼", fish, now=NOW,
                                  new_id=ids, title="斗鱼")
    stop_draft(repo, gw(env), "u1", res.draft.draft_id, now=NOW, new_id=ids)
    s.push("goals.intake", {"blocks": [{"name": "泡巢", "page": True},
                                       {"name": "配对", "page": True}]})
    fid = next(n.id for n in state(env).maps[res.draft.map_id].nodes if n.name == "繁殖")
    enter_region(repo, gw(env), "u1", res.draft.map_id, fid, now=NOW, new_id=ids)
    shrimp = (Block(name="水质与环境", covers=("pH",)), Block(name="繁殖", covers=("抱卵", "幼虾"),
                                                         target=True))
    r2 = create_goal_from_blocks(repo, gw(env), "u1", "我想养黑壳虾", shrimp, now=NOW,
                                 new_id=ids, title="黑壳虾")
    m2 = state(env).maps[r2.draft.map_id]
    breed = next(n for n in m2.nodes if n.name == "繁殖")
    assert breed.id != fid and breed.id.startswith("rg-")
    assert (breed.state, breed.path_id, breed.reused, breed.scope) == ("collapsed", None, False,
                                                                       "抱卵、幼虾")
    assert m2.children(breed.id) == ()
    fish_breed = state(env).maps[res.draft.map_id].node(fid)
    assert fish_breed.state == "expanded" and fish_breed.scope == "泡巢、配对"
    # with the id written, the block does reuse that node
    r3 = create_goal_from_blocks(repo, gw(env), "u1", "我想养孔雀鱼", (
        Block(name="繁殖", existing_id=fid, target=True), Block(name="喂养")), now=NOW,
        new_id=ids, title="孔雀鱼")
    m3 = state(env).maps[(r3.draft.map_id if r3.draft else None) or r3.map_id]
    assert m3.nodes[0].id == fid


def test_a_block_reuses_an_existing_node_and_renames_it(env):
    """Reuse only by existing_id; M1: the block's name reaches the node at confirm (a node
    rename, GraphSaved user_edit), so the map shows what the card said."""
    repo, _, ids = env
    from evowiki.wikiapp.graph_sync import graph_events
    from evowiki.wikiapp.repo import Commit

    snap = repo.load("u1")
    g = snap.state.graph.updated(nodes=[NodeRec(id="c9", name="水质")])
    aspect, wiki = graph_events(snap.state, snap.k, g, "user_edit", now=NOW, new_id=ids)
    repo.commit(snap, Commit(aspect=aspect, wiki=wiki))
    blocks = (Block(name="把水养好", covers=("换水",), existing_id="c9"),
              Block(name="日常喂养", covers=("饲料",)))
    push_no_question(env[1])
    before = len(repo.wiki_log("u1"))
    res = make(env, blocks)
    st = state(env)
    m = st.maps[res.map_id]
    assert [(n.id, n.name, n.reused) for n in m.nodes] == [("c9", "把水养好", True),
                                                            ("c2", "日常喂养", False)]
    assert st.graph.node("c9").name == "把水养好" and st.names["c9"].name == "把水养好"
    view = map_out(st, repo.load("u1").k, res.map_id, NOW)
    assert [n.name for n in view.nodes] == ["把水养好", "日常喂养"]
    assert any(isinstance(e.body, GraphSaved) and e.body.action == "user_edit"
               for e in repo.wiki_log("u1")[before:])
    with pytest.raises(WikiInvalid):
        make(env, (Block(name="甲", existing_id="c9"), Block(name="乙", existing_id="c9")))
    with pytest.raises(WikiInvalid):
        make(env, ())


def test_entering_a_block_splits_it_one_level_with_every_blocks_covers(env):
    """Spec 2026-10-28 §2.2: entering a block splits it ONE level (intake@v31: every block's
    covers, `atomic` per part) and plans nothing; the parts go into the graph under it and onto
    the map, not entered yet; entering again shows them with no call."""
    repo, s, ids = env
    res = make(env)
    stop_draft(repo, gw(env), "u1", res.draft.draft_id, now=NOW, new_id=ids)
    map_id = res.draft.map_id
    s.push("goals.intake", {"parts": [
        {"name": "换水", "summary": "多久换一次", "atomic": True},
        {"name": "水质检测", "summary": "测 pH 和氨氮", "atomic": False},
        {"name": "pH", "summary": "酸碱度", "atomic": True}]})
    probes = s.calls(PROBE)
    out = enter_region(repo, gw(env), "u1", map_id, "c2", now=NOW, new_id=ids)
    prompt = s.requests("goals.enter")[0].prompt
    assert "1. 「认识黑壳虾」（外形、习性）\n2. 「把水养好」（换水、水质检测、pH） ← 这一块" in prompt
    assert "同一层别的块包含的东西不要拆进来" in prompt and "这一次只把下面这一块往下拆一层" in prompt
    assert "一页" not in prompt and "reference" not in prompt
    assert s.calls("goals.enter") == 1 and s.calls("goals.path") == 0
    assert s.calls(PROBE) == probes
    assert out.map_id == map_id and out.path_id is None and out.draft is None
    st = state(env)
    m = st.maps[map_id]
    assert [(n.id, n.name, n.kind, n.parent, n.state) for n in m.children("c2")] == [
        ("c5", "换水", "concept", "c2", "collapsed"),
        ("c6", "水质检测", "region", "c2", "collapsed"),
        ("c7", "pH", "concept", "c2", "collapsed")]
    assert m.node("c2").state == "expanded" and m.node("c2").path_id is None
    assert set(st.graph.parents("c6")) == {"c2"}
    assert [st.names[c].atomic for c in ("c2", "c5", "c6", "c7")] == [False, True, False, True]
    view = {n.id: n for n in map_out(st, repo.load("u1").k, map_id, NOW).nodes}
    assert [(view[c].atomic, view[c].enter) for c in ("c2", "c5", "c6")] == [
        (False, "children"), (True, "learn"), (False, "split")]
    calls = s.calls()
    again = enter_region(repo, gw(env), "u1", map_id, "c2", now=NOW, new_id=ids)
    assert again.map_id == map_id and s.calls() == calls
    assert state(env).maps[map_id] == m                   # nothing new to write


def test_map_edits_and_restore(env):
    res = make(env)
    map_id = res.draft.map_id
    done = edit(env, map_id, {"op": "reorder", "ids": ["c2", "c1", "c3", "c4"]},
                {"op": "rename", "id": "c2", "name": "水质"},
                {"op": "add", "name": "造景", "covers": ["水草", " ", "水草"]},
                {"op": "drop", "id": "c4"})
    st = state(env)
    m = st.maps[map_id]
    assert [(n.name, n.scope) for n in m.nodes] == [
        ("水质", "换水、水质检测、pH"), ("认识黑壳虾", "外形、习性"), ("日常喂养", "饲料、喂食频率"),
        ("造景", "水草")]
    added = m.nodes[-1]
    assert added.id.startswith("rg_") and added.members == () and added.state == "collapsed"
    assert st.graph.node("c2").name == "水质" and st.names["c2"].name == "水质"   # one node
    assert st.graph.node("c4") is not None                  # a dropped block's node stays
    view = map_out(st, env[0].load("u1").k, map_id, NOW)
    assert [n.name for n in view.nodes] == ["水质", "认识黑壳虾", "日常喂养", "造景"]
    log = env[0].wiki_log("u1")
    saved = [e.body for e in log if isinstance(e.body, GoalMapSaved)]
    assert saved[-1].action == "edited" and done.before.node("c4") is not None
    assert any(isinstance(e.body, GraphSaved) and e.body.action == "user_edit" for e in log)
    assert replay(log).maps[map_id] == m                  # the edit replays
    # 撤销: the inverse ops, names included
    assert [o.op for o in done.undo] == ["insert", "drop", "rename", "reorder"]
    edit(env, map_id, *(o.model_dump(mode="json") for o in done.undo))
    st = state(env)
    assert [n.name for n in st.maps[map_id].nodes] == ["认识黑壳虾", "把水养好", "日常喂养", "繁殖"]
    assert st.names["c2"].name == "把水养好"


def _abc():
    from evowiki.goals.regions import GoalMap, MapNode
    nodes = (MapNode(id="a", kind="region", name="基础 A"),
             MapNode(id="b", kind="region", name="计算 B", prereqs=("a",)),
             MapNode(id="c", kind="region", name="应用 C", prereqs=("b", "a")))
    return GoalMap(map_id="m", goal="g", created_at=NOW, nodes=nodes)


def _rels(m):
    return [(n.id, n.prereqs) for n in m.nodes]


def test_undo_of_a_drop_or_reorder_puts_the_relations_back_exactly():
    """D35: dropping A took B→A and C→A off the map, and its 撤销 brought back only the block; a
    reorder dropped the backwards A→B and its 撤销 restored only the order. The inverse ops carry
    what the edit took off other blocks; a relation comes back only to a block still on the map."""
    from evowiki.wikiapp.map_edits import _apply
    from evowiki.wikiapp.wiki_schemas import MapDropIn, MapReorderIn
    m0, ids = _abc(), Ids()
    dropped, undo = _apply(m0, MapDropIn(op="drop", id="a"), {}, set(), ids)
    assert _rels(dropped) == [("b", ()), ("c", ("b",))]
    back, _ = _apply(dropped, undo, {}, set(), ids)
    assert back == m0
    moved, undo = _apply(m0, MapReorderIn(op="reorder", ids=["b", "a", "c"]), {}, set(), ids)
    assert _rels(moved) == [("b", ()), ("a", ()), ("c", ("b", "a"))]
    back, _ = _apply(moved, undo, {}, set(), ids)
    assert back == m0
    # undone after C was dropped too: B's relation comes back, nothing points at the gone C
    dropped, undo = _apply(m0, MapDropIn(op="drop", id="a"), {}, set(), ids)
    later, _ = _apply(dropped, MapDropIn(op="drop", id="c"), {}, set(), ids)
    back, _ = _apply(later, undo, {}, set(), ids)
    assert _rels(back) == [("a", ()), ("b", ("a",))]


def test_an_added_block_has_its_own_id_so_a_renamed_away_name_is_free_again(env):
    """D36: 加一块「复习」, rename it 「考前复习」, 加一块「复习」 again: refused as 「已经有一块叫」 because
    the id came from the first name."""
    res = make(env)
    map_id = res.draft.map_id
    first = edit(env, map_id, {"op": "add", "name": "复习"}).after.nodes[-1]
    edit(env, map_id, {"op": "rename", "id": first.id, "name": "考前复习"})
    again = edit(env, map_id, {"op": "add", "name": "复习"}).after.nodes[-1]
    assert again.id != first.id
    assert [n.name for n in state(env).maps[map_id].nodes][-2:] == ["考前复习", "复习"]
    with pytest.raises(WikiInvalid, match="已经有一块叫「复习」"):
        edit(env, map_id, {"op": "add", "name": "复习"})


def test_undo_of_an_older_drop_keeps_later_edits(env):
    """M2: 撤销 of a delete puts that block back (its inverse op), never reverting what was
    edited after it; a stale undo (the block is back already) is refused."""
    res = make(env)
    map_id = res.draft.map_id
    dropped = edit(env, map_id, {"op": "drop", "id": "c2"})
    edit(env, map_id, {"op": "rename", "id": "c3", "name": "喂养"},
         {"op": "add", "name": "造景"})
    undo = [o.model_dump(mode="json") for o in dropped.undo]
    assert undo[0]["op"] == "insert" and undo[0]["index"] == 1
    edit(env, map_id, *undo)
    m = state(env).maps[map_id]
    assert [n.name for n in m.nodes] == ["认识黑壳虾", "把水养好", "喂养", "繁殖", "造景"]
    with pytest.raises(WikiConflict, match="后面又改过，不能这样撤销"):
        edit(env, map_id, *undo)


def test_a_block_whose_diagnosis_is_open_cannot_be_dropped_renamed_or_moved(env):
    """H2: a point of a block was opened and its diagnosis is open; dropping the block (the
    draft would be left asking about a point that is gone) is refused, and once it is entered
    it cannot be dropped (rename and reorder stay allowed)."""
    repo, s, ids = env
    res = make(env)
    stop_draft(repo, gw(env), "u1", res.draft.draft_id, now=NOW, new_id=ids)
    map_id = res.draft.map_id
    opening_answered(repo, map_id, now=NOW, new_id=ids)
    s.push("goals.intake", {"blocks": [{"name": "外形", "page": True},
                                       {"name": "习性", "page": True}]})
    enter_region(repo, gw(env), "u1", map_id, "c1", now=NOW, new_id=ids)
    out = enter_region(repo, gw(env), "u1", map_id, "c5", now=NOW, new_id=ids)   # 外形
    assert out.open and out.draft.region_id == "c5"
    with pytest.raises(WikiConflict, match="这一块正在摸底，先答完或跳过"):
        edit(env, map_id, {"op": "drop", "id": "c1"})
    stop_draft(repo, gw(env), "u1", out.draft.draft_id, now=NOW, new_id=ids)
    with pytest.raises(WikiConflict, match="这一块已经开始学了，不能删"):
        edit(env, map_id, {"op": "drop", "id": "c1"})
    edit(env, map_id, {"op": "rename", "id": "c1", "name": "认识它"})       # still allowed
    edit(env, map_id, {"op": "reorder", "ids": ["c3", "c1", "c2", "c4"]})


def test_a_region_draft_whose_region_is_gone_is_a_conflict(env):
    """H2: older data (a region dropped while its draft was open) answers 409, never an
    AssertionError."""
    from evowiki.wikiapp.events import WikiEvent
    from evowiki.wikiapp.repo import Commit

    repo, s, ids = env
    res = make(env)
    stop_draft(repo, gw(env), "u1", res.draft.draft_id, now=NOW, new_id=ids)
    map_id = res.draft.map_id
    opening_answered(repo, map_id, now=NOW, new_id=ids)
    s.push("goals.intake", {"blocks": [{"name": "外形", "page": True},
                                       {"name": "习性", "page": True}]})
    enter_region(repo, gw(env), "u1", map_id, "c1", now=NOW, new_id=ids)
    out = enter_region(repo, gw(env), "u1", map_id, "c5", now=NOW, new_id=ids)   # a point
    assert out.open
    snap = repo.load("u1")
    m = snap.state.maps[map_id]
    gone = m.model_copy(update={"nodes": tuple(n for n in m.nodes
                                                if n.id != "c1" and n.parent != "c1")})
    ev = WikiEvent(event_id=ids("wev"), ts=NOW, body=GoalMapSaved(record=gone, action="edited"))
    repo.commit(snap, Commit(wiki=[ev]))
    answers = [0]
    with pytest.raises(WikiConflict, match="这一块已经不在学习地图上了"):
        answer_round(repo, gw(env), "u1", out.draft.draft_id, answers, now=NOW, new_id=ids)
    with pytest.raises(WikiConflict, match="这一块已经不在学习地图上了"):
        stop_draft(repo, gw(env), "u1", out.draft.draft_id, now=NOW, new_id=ids)


@pytest.mark.parametrize("op", [
    {"op": "drop", "id": "nope"},
    {"op": "rename", "id": "c1", "name": "把水养好"},
    {"op": "reorder", "ids": ["c1", "c2"]},
    {"op": "reorder", "ids": ["c1", "c1", "c2", "c3", "c4"]},
    {"op": "add", "name": "繁殖"},
])
def test_an_edit_that_cannot_apply_is_refused_whole(env, op):
    res = make(env)
    map_id = res.draft.map_id
    before = state(env).maps[map_id]
    with pytest.raises(WikiInvalid):
        edit(env, map_id, {"op": "rename", "id": "c1", "name": "认识它"}, op)
    assert state(env).maps[map_id] == before            # all or none


def test_the_last_block_stays_and_unknown_map(env):
    res = make(env, BLOCKS[:1])
    with pytest.raises(WikiInvalid):
        edit(env, res.draft.map_id, {"op": "drop", "id": "c1"})
    with pytest.raises(LookupError):
        edit(env, "map-nope", {"op": "drop", "id": "c1"})


def test_draft_status_open_then_finished(env):
    repo, _, ids = env
    res = make(env)
    open_ = draft_status(repo, "u1", res.draft.draft_id)
    assert open_.open and open_.draft.rounds == res.draft.rounds
    stop_draft(repo, gw(env), "u1", res.draft.draft_id, now=NOW, new_id=ids)
    done = draft_status(repo, "u1", res.draft.draft_id)
    assert not done.open and done.map_id == res.draft.map_id
    with pytest.raises(LookupError):
        draft_status(repo, "u1", "dr_nope")



def test_a_stated_beginner_is_asked_nothing_at_the_opening_or_on_entering_a_block(env):
    """D07 through the goal record (S1): his 「完全零基础」 sits on every block (`said`, unlearned,
    `diagnose` False). The opening asks nothing, the words are checked against HIS words (not a
    model-written background), and entering a block is one call with no estimate part (nothing
    asked, S2) and no probe; opening a point plans its pages at once."""
    repo, s, ids = env
    words = (GOAL, "我完全零基础，想尽快读到第一页")
    blocks = tuple(b.model_copy(update={"diagnose": False, "said": ("完全零基础",),
                                        "said_level": "unlearned"}) for b in BLOCKS)
    res = create_goal_from_blocks(repo, gw(env), "u1", GOAL, blocks, now=NOW, new_id=ids,
                                  title="黑壳虾", words=words, limits=("尽快读到第一页",))
    assert s.calls("goals.probe") == 0
    assert not res.open
    assert {d.decision for d in res.draft.rounds[-1].decisions} == {"teach"}
    m = state(env).maps[res.map_id]
    assert m.words == words and m.limits == ("尽快读到第一页",)
    assert all(not n.diagnose and n.said == ("完全零基础",) for n in m.nodes)
    # his words reached K as a self-report on each block's cells (term + purpose each)
    assert res.draft.from_background == 2 * len(BLOCKS)
    s.push("goals.intake", {"blocks": [{"name": "外形", "page": True},
                                       {"name": "习性", "page": True}]})
    out = enter_region(repo, gw(env), "u1", res.map_id, "c1", now=NOW, new_id=ids)
    assert out.map_id == res.map_id and s.calls("goals.enter") == 1         # split only
    prompt = s.requests("goals.enter")[0].prompt
    assert "他为这个目标提的条件：尽快读到第一页" in prompt
    assert ESTIMATE_MARK not in prompt and "我完全零基础" not in prompt   # D07: nothing estimated
    out = enter_region(repo, gw(env), "u1", res.map_id, "c5", now=NOW, new_id=ids)   # a point
    assert s.calls("goals.probe") == 0 and s.calls("goals.enter") == 1
    assert not out.open and out.path_id is not None


def test_a_said_quote_not_in_his_words_is_no_evidence(env):
    """D07's second half: a quote that is not his words is dropped like any self-report (the
    opening may then ask)."""
    repo, _, ids = env
    blocks = tuple(b.model_copy(update={"said": ("零基础的小白",), "said_level": "unlearned"})
                   for b in BLOCKS)
    res = create_goal_from_blocks(repo, gw(env), "u1", GOAL, blocks, now=NOW, new_id=ids,
                                  title="黑壳虾", words=(GOAL, "我完全零基础"))
    assert res.draft.from_background == 0
    kept = create_goal_from_blocks(
        MemoryRepo(["u1"]), gw(env), "u1", GOAL,
        tuple(b.model_copy(update={"said": ("完全零基础",), "said_level": "unlearned"})
              for b in BLOCKS), now=NOW, new_id=ids, title="黑壳虾", words=(GOAL, "我完全零基础"))
    assert kept.draft.from_background == 2 * len(BLOCKS)       # term + purpose per new block


# --- the goal record after the confirm (final review 2026-10-28) --------------------------------

def test_a_block_added_on_a_stated_beginners_map_is_not_diagnosed(env):
    """D07 after the confirm: the map keeps his beginner words (`GoalMap.beginner`), so 加一块 on
    学习路径 makes a block that is never asked about, as every block S1 wrote."""
    blocks = tuple(b.model_copy(update={"diagnose": False, "said": ("完全零基础",),
                                        "said_level": "unlearned"}) for b in BLOCKS)
    res = make(env, blocks, words=(GOAL, "完全零基础"), beginner="完全零基础")
    m = state(env).maps[(res.map_id or res.draft.map_id)]
    assert m.beginner == "完全零基础"
    added = edit(env, (res.map_id or res.draft.map_id), {"op": "add", "name": "造景"}).after.nodes[-1]
    assert (added.name, added.diagnose, added.said) == ("造景", False, ("完全零基础",))


def test_a_block_added_on_a_plain_map_is_diagnosed(env):
    res = make(env)
    assert state(env).maps[(res.map_id or res.draft.map_id)].beginner == ""
    added = edit(env, (res.map_id or res.draft.map_id), {"op": "add", "name": "造景"}).after.nodes[-1]
    assert (added.diagnose, added.said) == (True, ())


def test_a_merged_blocks_quotes_are_all_kept_and_count_once(env):
    """A chat merge keeps each part's words (`said` is a tuple of quotes, each checked verbatim);
    the block's self-report is one, on its first quote."""
    from evowiki.goals.blocks import BlockOp, apply_block_ops
    said = (BLOCKS[0].model_copy(update={"said": ("养过两年米虾",), "said_level": "intermediate"}),
            BLOCKS[1].model_copy(update={"said": ("会测 pH",), "said_level": "intermediate"}),
            *BLOCKS[2:])
    merged, dropped = apply_block_ops(said, [BlockOp(op="merge", names=("认识黑壳虾", "把水养好"))])
    assert not dropped and merged[0].said == ("养过两年米虾", "会测 pH")
    res = make(env, merged, words=(GOAL, "养过两年米虾，会测 pH"))
    node = state(env).maps[(res.map_id or res.draft.map_id)].nodes[0]
    assert node.said == ("养过两年米虾", "会测 pH")
    assert res.draft.from_background == 2              # one block's self-report (term + purpose)


def test_an_older_reading_goals_leaves_are_no_progress(env):
    """An older 读懂 goal's reading block has its sections as concept nodes from the start (no
    longer generated: reading-mode spec §6, Q6 keeps the data readable): not progress, so the same
    goal again replaces an untouched map instead of refusing it."""
    from evowiki.belief.aspects import AspectKView
    from evowiki.goals.regions import MapNode
    from evowiki.wikiapp.goal import _map_progress
    res = make(env, (BLOCKS[0], Block(name="读懂《论文》", target=True, diagnose=False)))
    snap = env[0].load("u1")
    m = snap.state.maps[(res.map_id or res.draft.map_id)]
    reading = next(n for n in m.nodes if n.name == "读懂《论文》")
    m = m.model_copy(update={"nodes": (*m.nodes, *(
        MapNode(id=f"leaf{i}", kind="concept", name=f"《论文》§{i}", parent=reading.id,
                state="expanded") for i in (1, 2)))})
    assert not _map_progress(snap.state, m, AspectKView(snap.k, NOW))


def test_same_goal_sees_his_words_and_the_title_not_an_archived_goal():
    from evowiki.goals.regions import GoalMap
    from evowiki.wikiapp.goal import same_goal
    from evowiki.wikiapp.state import GoalSettings, WikiState
    m = GoalMap(map_id="m1", goal="我要学 Python", title="Python", nodes=(), created_at=NOW)
    st = WikiState(maps={"m1": m})
    assert same_goal(st, "python") is m and same_goal(st, "我想学python") is m
    assert same_goal(st, "傅里叶") is None and same_goal(st, "") is None
    st = st.model_copy(update={"goal_settings": {"m1": GoalSettings(goal_id="m1", archived=True)}})
    assert same_goal(st, "python") is None


def test_a_goal_taken_back_and_asked_for_again_is_listed_again(env):
    """The map id comes from the words, so the second confirm lands on the taken-back map: it was
    saved and stayed hidden, with a card saying it was done (live 2026-11-04)."""
    from evowiki.wikiapp.events import GoalSettings, GoalSettingsSet, WikiEvent
    from evowiki.wikiapp.repo import Commit
    repo, _, ids = env
    map_id = make(env).draft.map_id
    settings = GoalSettings(goal_id=map_id, archived=True)
    repo.commit(repo.load("u1"), Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=NOW, body=GoalSettingsSet(settings=settings))]))
    assert state(env).goal_settings[map_id].archived
    assert make(env).draft.map_id == map_id
    assert not state(env).goal_settings[map_id].archived
