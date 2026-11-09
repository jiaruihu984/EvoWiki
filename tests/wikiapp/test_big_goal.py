"""Big goals (big-goal design + reuse rulings 2026-10-11; unified node graph 2026-10-13): a region
is a graph node whose children (part_of) are its knowledge points, created with the goal and shown
on the map from the start; decompose sees the graph's
topics (nodes with children), concepts and evidence and reuses ids (one node for one thing); a
region is a K concept with direct evidence of its own next to its members'; nothing K
already has evidence for is asked about; a region K shows learned (every cell it covers likely or
known; read from K every time, never stored) is known and skipped; entering a region
only starts learning it and plans its pages; the path model sees the existing pages. No
verification call. Decompose (intake@v14) writes one kind of node and relations with phrases; the
nodes with parts are the map's regions and keep their node ids. Scripted LLM only."""

import pytest

from evowiki.belief.aspects import AspectKView, Observe
from evowiki.goals.contracts import Band, Observation
from evowiki.goals.observe import SELF_DEFAULT_STRENGTH
from evowiki.goals.path import goal_key
from evowiki.goals.selfreport import SELF_CLAIM
from evowiki.nodes.ksync import DEFAULT_ASPECTS, in_step
from evowiki.nodes.model import NodeGraph, NodeRec
from evowiki.wikiapp.events import (
    DraftClosed,
    DraftCreated,
    GoalMapSaved,
    GraphSaved,
    ProbeAnswered,
)
from evowiki.wikiapp.goal import enter_region, stop_draft
from evowiki.wikiapp.kwrite import observation_events
from evowiki.wikiapp.llm import JSON_PROMPT_VERSION, PURPOSE_GROUPS, CountingGateway
from evowiki.wikiapp.maps import map_cards, map_out
from evowiki.wikiapp.paths import next_path_page, path_folder, path_out
from evowiki.wikiapp.progress import progress
from evowiki.wikiapp.repo import Commit, MemoryRepo, WikiConflict
from evowiki.wikiapp.state import replay
from evowiki.wikiapp.views import goal_draft_out
from tests.wikiapp.flat_goal import flat_goal, region_goal
from tests.wikiapp.goal_script import (
    PROBE,
    answer_all,
    answer_round,
    auto_probe,
    push_no_question,
    push_one_page,
    push_pages,
    push_round,
    question_of,
    right,
    wrong,
)
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.verify import verify_prereqs

GOAL = "我要学机器学习"
BACKGROUND = "会写 Python，大学学过高数，别的忘得差不多了。"


def rel(kind, src, dst, phrase=""):
    return {"kind": kind, "from": src, "to": dst, "phrase": phrase}


# Written as intake@v14 nodes + relations, answered as the intake@v20 outline (tests.goals.fakes_intake
# .to_outline; its prerequisites become unverified `recommended` edges): the four areas are the first-level blocks, ids in learning order (depth-first) —
# c1 线性代数 {c2 矩阵, c3 矩阵乘法}, c4 概率论 {c5 条件概率, c6 贝叶斯公式}, c7 优化 {c8 梯度下降,
# c9 学习率}, c10 监督学习 {c11 线性回归, c12 损失函数} (the target: its leaves are the targets).
ML = {"title": "机器学习", "nodes": [
    {"id": "n1", "name": "矩阵", "aspects": ["term"]},
    {"id": "n2", "name": "矩阵乘法", "aspects": ["procedure"]},
    {"id": "n3", "name": "条件概率", "aspects": ["term"]},
    {"id": "n4", "name": "贝叶斯公式", "aspects": ["procedure"]},
    {"id": "n5", "name": "梯度下降", "aspects": ["procedure"]},
    {"id": "n6", "name": "线性回归", "target": True, "aspects": ["purpose"]},
    {"id": "n7", "name": "损失函数", "aspects": ["term"]},
    {"id": "n8", "name": "学习率", "aspects": ["term"]},
    {"id": "r1", "name": "线性代数"}, {"id": "r2", "name": "概率论"}, {"id": "r3", "name": "优化"},
    {"id": "r4", "name": "监督学习", "target": True}],
    "relations": [
        rel("prerequisite", "n1", "n2", "矩阵乘法是在矩阵上做的运算"),
        rel("prerequisite", "n3", "n4", "贝叶斯公式由条件概率推出"),
        rel("prerequisite", "n2", "n6", "线性回归的预测是矩阵乘法"),
        rel("prerequisite", "n5", "n6", "线性回归靠梯度下降训练"),
        rel("part_of", "n1", "r1", "矩阵是线性代数的基本对象"), rel("part_of", "n2", "r1"),
        rel("part_of", "n3", "r2"), rel("part_of", "n4", "r2"), rel("part_of", "n5", "r3"), rel("part_of", "n8", "r3"),
        rel("part_of", "n6", "r4"), rel("part_of", "n7", "r4"),
        rel("prerequisite", "r1", "r3", "优化里的梯度是向量"), rel("prerequisite", "r1", "r4"),
        rel("prerequisite", "r2", "r4"), rel("prerequisite", "r3", "r4")],
    "observations": []}
MAP = f"map-{goal_key(GOAL, ['c11', 'c12'])}"
# ML's prerequisites with their final ids, for the tests that need them to feed inference: intake
# writes them as unverified recommended edges, so a test verifies them (tests.wikiapp.verify).
ML_PREREQS = [("c2", "c3"), ("c5", "c6"), ("c3", "c11"), ("c8", "c11"),
              ("c1", "c7"), ("c1", "c10"), ("c4", "c10"), ("c7", "c10")]
LA, PR, OPT, SL = "c1", "c4", "c7", "c10"
SELFS = [{"kind": "self_report", "target": LA, "stem": "学过线性代数吗？",
          "options": ["学过，矩阵乘法还会算", "学过，但忘得差不多了", "没学过", "说不清"],
          "means": ["know", "partly", "dont", "unsure"]},
         {"kind": "self_report", "target": PR, "stem": "学过概率论吗？",
          "options": ["学过", "没学过"], "means": ["know", "dont"]}]


@pytest.fixture
def env():
    s = Script()
    auto_probe(s)                 # the probe model writes whatever item the diagnosis picked
    return MemoryRepo(["u1"]), s, Ids()


def gw(env):
    return CountingGateway(env[1].gateway())


def state(env):
    return env[0].load("u1").state


def part_of(env) -> set[tuple[str, str, str]]:
    return {(r.src, r.dst, r.source) for r in state(env).graph.relations if r.kind == "part_of"}


def mapped(env, map_id=None):
    snap = env[0].load("u1")
    mid = map_id or next(iter(snap.state.maps))
    return map_out(snap.state, snap.k, mid, NOW)


def by_name(view):
    return {n.name: n for n in view.nodes}


def start(env, selfs=SELFS):
    """The map and its first round: the diagnosis asks a coarse self-report per region; the
    model writes two of them (`selfs`; the others are simply not asked). With `selfs` empty the
    model writes nothing usable: the diagnosis ends at once (fail closed) and the map is done."""
    _repo, s, _ids = env
    s.push("goals.intake", ML)
    if selfs:
        push_round(s, selfs=selfs)
    else:
        push_no_question(s)
    return scripted_map(env, GOAL, BACKGROUND)


def scripted_map(env, goal, description):
    """The next pushed goals.intake fixture as a map with its regions' members (the eager map:
    tests.wikiapp.flat_goal.region_goal)."""
    repo, s, ids = env
    return region_goal(repo, gw(env), "u1", goal, description, s.pop_mapper(), now=NOW,
                       new_id=ids)


def answer(env, draft_id, answers):
    repo, _, ids = env
    return answer_round(repo, gw(env), "u1", draft_id, answers, now=NOW, new_id=ids)


def stop(env, draft_id):
    repo, _, ids = env
    return stop_draft(repo, gw(env), "u1", draft_id, now=NOW, new_id=ids)


def learn(env, cells, n=3):
    """Strong quiz evidence on `cells`, committed directly."""
    repo, _, ids = env
    obs = [Observation(cell=c, polarity="+", strength=1.0, kind="quiz", evidence_ref=f"t:{c}:{i}")
           for c in cells for i in range(n)]
    repo.commit(repo.load("u1"), Commit(aspect=observation_events(
        obs, ts=NOW, new_id=ids, note_text=lambda o: "对")))


def test_versions_and_no_verifier_purpose():
    # the diagnosis's questions are the assistant's (stage 3 T11): no goals.probe call any more
    assert PROBE not in JSON_PROMPT_VERSION and PROBE not in PURPOSE_GROUPS
    assert JSON_PROMPT_VERSION["goals.intake"] == "intake@v38"
    assert "goals.diagnostic.verify" not in JSON_PROMPT_VERSION


def test_regions_become_graph_nodes_with_their_members_shown_from_the_start(env):
    repo, _, _ = env
    res = start(env)
    assert res.open and res.calls == env[1].calls() and env[1].calls(PROBE) == 0
    st = state(env)
    view = mapped(env)
    assert view.map_id == MAP
    nodes = by_name(view)
    la, opt = nodes["线性代数"], nodes["优化"]
    assert (la.id, la.kind, la.state, la.members, la.scope) == (
        LA, "region", "collapsed", ["c2", "c3"], "矩阵、矩阵乘法")
    assert opt.id == OPT and opt.members == ["c8", "c9"]
    assert nodes["监督学习"].target and nodes["监督学习"].prereqs == []   # intake@v20: none
    # members are concept nodes under their region, grey (no evidence) from the start
    assert sorted((n.id, n.parent) for n in view.nodes if n.kind == "concept") == sorted([
        ("c2", LA), ("c3", LA), ("c5", PR), ("c6", PR), ("c8", OPT), ("c9", OPT), ("c11", SL), ("c12", SL)])
    assert nodes["矩阵乘法"].prereqs == [] and la.mix["none"] == 1.0
    assert not la.reused and view.next_region == LA
    # the graph: every concept and region a node, members part_of, recommended edges (intake
    # writes no prerequisite, relation layers), one commit
    g = st.graph
    assert {n.id for n in g.nodes} == {LA, PR, OPT, SL, *(f"c{i}" for i in (2, 3, 5, 6, 8, 9, 11, 12))}
    assert part_of(env) == {("c2", LA, "intake"), ("c3", LA, "intake"), ("c5", PR, "intake"),
                            ("c6", PR, "intake"), ("c8", OPT, "intake"), ("c9", OPT, "intake"), ("c11", SL, "intake"),
                            ("c12", SL, "intake")}
    assert g.prereq_edges() == ()
    rec = {(r.src, r.dst) for r in g.relations if r.kind == "recommended"}
    assert {(LA, OPT), (LA, SL), (PR, SL), (OPT, SL), ("c2", "c3"), ("c3", "c11")} <= rec
    assert all(r.source == "intake" and not r.verified for r in g.relations)   # as is, unverified
    phrase = {(r.kind, r.src, r.dst): r.phrase for r in g.relations}
    assert phrase[("part_of", "c2", LA)] == "矩阵是线性代数的基本对象"
    assert phrase[("recommended", "c3", "c11")] == "线性回归的预测是矩阵乘法"
    assert phrase[("recommended", LA, OPT)] == "优化里的梯度是向量" and phrase[("part_of", "c3", LA)] == ""
    snap = repo.load("u1")
    assert in_step(g, snap.k) and snap.k.aspects(LA) == DEFAULT_ASPECTS   # a region: K concept
    assert snap.k.prereqs(SL) == () and st.names[LA].name == "线性代数"   # nothing mirrored
    saved = [e.body for e in repo.wiki_log("u1") if isinstance(e.body, GraphSaved)]
    assert [b.action for b in saved] == ["goal_nodes"]
    out = goal_draft_out(res, map_out=view)
    pick = res.draft.rounds[0].pick
    assert out.questions == [] and out.round == 1          # asked in the chat (ask_check)
    assert (pick.intent, pick.node, pick.name, pick.cell) == ("self_report", LA, "线性代数", "")


def test_region_self_report_is_weak_evidence_on_its_members_then_quizzes_then_known(env):
    repo, s, _ = env
    res = start(env)
    did = res.draft.draft_id
    k0 = len(repo.aspect_log("u1"))
    probes = s.calls(PROBE)
    r2 = answer(env, did, [0, 1])                        # 线性代数 还会算; 概率论 没学过
    assert r2.open and s.calls(PROBE) == probes          # no model call: the assistant asks
    obs = [e.observation for e in repo.aspect_log("u1")[k0:] if isinstance(e, Observe)]
    # the region is a K concept: its own cells get the evidence too, next to its members'
    assert {(o.cell, o.polarity, o.strength) for o in obs} == {
        ("c2.term", "+", SELF_CLAIM["can_do"][1]), ("c3.procedure", "+", SELF_CLAIM["can_do"][1]),
        (f"{LA}.term", "+", SELF_CLAIM["can_do"][1]), (f"{LA}.purpose", "+", SELF_CLAIM["can_do"][1]),
        ("c5.term", "-", SELF_DEFAULT_STRENGTH), ("c6.procedure", "-", SELF_DEFAULT_STRENGTH),
        (f"{PR}.term", "-", SELF_DEFAULT_STRENGTH), (f"{PR}.purpose", "-", SELF_DEFAULT_STRENGTH)}
    out = goal_draft_out(r2)
    assert out.round == 3 and [x.means for x in out.results] == ["know", "dont"]   # a pick a round
    assert [x.intent for x in out.results] == ["self_report", "self_report"]
    assert out.questions == [] and r2.open          # the next pick (优化's self-report, then quizzes)
    # 线性代数 and its members answered right, everything else wrong
    la_side = {LA, "c2", "c3"}
    res, rounds = answer_all(r2, lambda q: right(q) if q.node in la_side else wrong(q),
                             lambda d, a: answer(env, d, a))
    assert not res.open and res.map_id == res.draft.map_id
    assert len({q.node for r in rounds for q in r.questions}) == len(
        [q for r in rounds for q in r.questions])               # no node asked twice
    nodes = by_name(mapped(env))
    # the diagnosis decided 线性代数 known (its quiz right + 会), but whether a block is known is
    # K's alone: its purpose and its members rest on the self-report only (shaky) → still to learn
    assert nodes["线性代数"].state == "collapsed"
    assert nodes["概率论"].state == "collapsed" and nodes["监督学习"].state == "collapsed"
    final = state(env).drafts[did].rounds[-1]
    decided = {d.node: d.decision for d in final.decisions}
    assert decided[LA] == decided["c2"] == decided["c3"] == "known"
    assert decided[PR] == decided["c5"] == "teach"
    m = state(env).maps[res.map_id]
    assert any(line.startswith("摸底第 2 轮 ") for line in m.log)          # every question logged
    assert any(line.startswith(f"摸底结论 {LA} 线性代数：会（") for line in m.log)
    log = [e.body for e in repo.wiki_log("u1")]
    assert [type(b) for b in log[-3:]] == [ProbeAnswered, GoalMapSaved, DraftClosed]


def test_a_block_the_diagnosis_called_known_but_k_shows_shaky_is_still_to_learn(env):
    """The `me` case (user ruling 2026-10-28 「整块已会只按 K 算」): the diagnosis decides 线性代数
    known (its quiz right + 会), K has its term at likely and its members at shaky (the self-report only).
    It is not known anywhere — map, catalog card, progress — and it can be entered; entering asks
    nothing again (every member has the opening's decision) and plans its pages."""
    repo, s, ids = env
    res = start(env)
    r2 = answer(env, res.draft.draft_id, [0, 1])
    done, _ = answer_all(r2, lambda q: right(q) if q.node in {LA, "c2", "c3"} else wrong(q),
                         lambda d, a: answer(env, d, a))
    final = state(env).drafts[res.draft.draft_id].rounds[-1]
    assert {d.node: d.decision for d in final.decisions}[LA] == "known"
    snap = repo.load("u1")
    kv = AspectKView(snap.k, NOW)
    assert kv.cell(f"{LA}.term").band == Band.LIKELY and kv.cell(f"{LA}.purpose").band == Band.KNOWN
    view = mapped(env)
    assert by_name(view)["线性代数"].state == "collapsed" and view.next_region == LA
    assert [c.known for c in map_cards(snap.state, snap.k, NOW)] == [0]
    (g,) = progress(snap.state, kv, {}).goals
    assert {b.region_id: b.state for b in g.blocks}[LA] == "not_started"
    push_pages(s, ["c2", "c3"])
    calls, probes = s.calls(), s.calls(PROBE)
    out = enter_region(repo, gw(env), "u1", done.map_id, LA, now=NOW, new_id=ids)
    assert out.path_id is not None and s.calls() == calls + 1 and s.calls(PROBE) == probes


def test_a_block_k_shows_learned_is_known_everywhere_until_k_drops_a_cell(env):
    """Nothing on the map says known: K alone. Every cell 线性代数 covers likely+ → known on the
    map, the catalog card and progress, skipped by next_region; he can still enter it, and that
    leaves K alone. A wrong quiz drops one cell → a block to learn again."""
    repo, s, ids = env
    res = start(env, selfs=[])
    learn(env, ["c2.term", "c3.procedure", f"{LA}.term", f"{LA}.purpose"])

    def views():
        snap = repo.load("u1")
        (g,) = progress(snap.state, AspectKView(snap.k, NOW), {}).goals
        return (by_name(mapped(env))["线性代数"].state, mapped(env).next_region,
                map_cards(snap.state, snap.k, NOW)[0].known,
                {b.region_id: b.state for b in g.blocks}[LA])

    assert views() == ("known", PR, 1, "known")
    assert all(n.state != "known" for n in state(env).maps[res.map_id].nodes)    # never stored
    before = AspectKView(repo.load("u1").k, NOW).cell("c3.procedure").band
    push_pages(s, ["c2", "c3"])
    assert enter_region(repo, gw(env), "u1", res.map_id, LA, now=NOW, new_id=ids).path_id
    assert AspectKView(repo.load("u1").k, NOW).cell("c3.procedure").band == before
    obs = [Observation(cell="c3.procedure", polarity="-", strength=1.0, kind="quiz",
                       evidence_ref=f"t:wrong:{i}") for i in range(6)]
    repo.commit(repo.load("u1"), Commit(aspect=observation_events(
        obs, ts=NOW, new_id=ids, note_text=lambda o: "错")))
    assert views()[2] == 0                                # K dropped: no longer known


def test_entering_a_region_starts_learning_it_without_rediagnosis_and_plans_its_pages(env):
    repo, s, ids = env
    res = start(env)
    answer(env, res.draft.draft_id, [1, 1])              # both regions: some evidence now
    stop(env, res.draft.draft_id)                        # 够了，开始学
    push_pages(s, ["c5"], ["c6"])
    calls = s.calls()
    out = enter_region(repo, gw(env), "u1", res.draft.map_id, PR, now=NOW, new_id=ids)
    # its members already have evidence (the self-report): no intake, no probe, just the path
    assert out.path_id is not None and s.calls() == calls + 1
    nodes = by_name(mapped(env))
    assert nodes["概率论"].state == "expanded" and nodes["概率论"].path_id == out.path_id
    path = state(env).paths[out.path_id].path
    assert len(path.pages) == 2 and path.goal == f"{GOAL} · 概率论"
    with pytest.raises(WikiConflict):
        enter_region(repo, gw(env), "u1", res.draft.map_id, PR, now=NOW, new_id=ids)


def test_a_goals_later_pages_stay_in_the_folder_of_its_first_page(env):
    """Groups are renamed as the graph grows; a goal's new page goes where its first page is."""
    repo, s, ids = env
    res = start(env)
    answer(env, res.draft.draft_id, [1, 1])
    stop(env, res.draft.draft_id)
    push_pages(s, ["c5"], ["c6"])
    out = enter_region(repo, gw(env), "u1", res.draft.map_id, PR, now=NOW, new_id=ids)
    first = next_path_page(repo, "u1", out.path_id, now=NOW, new_id=ids)
    snap = repo.load("u1")
    moved = {first.page_id: snap.state.pages[first.page_id].model_copy(update={"folder": "旧的组名"})}
    state2 = snap.state.model_copy(update={"pages": {**snap.state.pages, **moved}})
    assert path_folder(state2, out.path_id) == "旧的组名"
    assert path_folder(state2, "no-such-path") is None


def test_he_picks_any_row_of_a_path_and_a_one_concept_page_has_no_made_up_aim(env):
    """POST next with a row's key makes that row's page, not the first; unknown key: LookupError."""
    repo, s, ids = env
    res = start(env)
    answer(env, res.draft.draft_id, [1, 1])
    stop(env, res.draft.draft_id)
    push_pages(s, ["c5"], ["c6"])
    out = enter_region(repo, gw(env), "u1", res.draft.map_id, PR, now=NOW, new_id=ids)
    second = state(env).paths[out.path_id].path.pages[1].page_id
    assert next_path_page(repo, "u1", out.path_id, now=NOW, new_id=ids, key=second).index == 2
    with pytest.raises(LookupError):
        next_path_page(repo, "u1", out.path_id, now=NOW, new_id=ids, key="nope")
    rec = state(env).paths[out.path_id]
    single = rec.model_copy(update={"path": rec.path.model_copy(update={"source": "single"})})
    snap = repo.load("u1")
    shown = path_out(snap.state.model_copy(update={"paths": {out.path_id: single}}), snap.k, out.path_id, NOW)
    assert {p.aim for p in shown.pages} == {""}


def test_entering_a_region_probes_only_members_without_evidence(env):
    repo, s, ids = env
    res = start(env, selfs=[])
    assert res.map_id is not None and not res.open
    learn(env, ["c11.purpose"], n=1)
    push_round(s, [("c11.purpose", "线性回归用来干嘛？", ["预测连续值", "分类", "聚类"], 0),
                   ("c12.term", "损失函数是？", ["衡量预测错多少", "数据集", "模型"], 0)])
    entered = enter_region(repo, gw(env), "u1", res.map_id, SL, now=NOW, new_id=ids)
    assert entered.open and entered.draft.evidenced == ("c11",)
    r = entered.draft.rounds[0]
    # c11 already had evidence: the diagnosis never picks it
    assert (r.pick.node, r.pick.cell) == ("c12", "c12.term") and question_of(r).target == "c12.term"


def test_a_reused_region_k_shows_mastered_is_known_and_skipped(env):
    repo, s, _ids = env
    start(env, selfs=[])
    learn(env, ["c2.term", "c3.procedure", f"{LA}.term", f"{LA}.purpose"])
    s.push("goals.intake", {"title": "t", "concepts": [
        {"id": "c2"}, {"id": "c3"}, {"id": "n1", "name": "主成分分析", "target": True,
                                     "prereqs": ["c3"], "aspects": ["procedure"]},
        {"id": "n2", "name": "协方差矩阵", "aspects": ["term"]}],
        "regions": [{"id": LA, "members": ["c2", "c3"]},
                    {"id": "r2", "name": "降维", "target": True, "members": ["n1", "n2"],
                     "prereqs": [LA]}], "observations": []})
    out = scripted_map(env, "学 PCA", "")
    view = map_out(state(env), repo.load("u1").k, out.draft.map_id, NOW)   # its diagnosis is open
    nodes = by_name(view)
    assert nodes["线性代数"].state == "known"              # every cell it covers likely+ in K
    assert "known" not in {n.state for n in state(env).maps[out.draft.map_id].nodes}  # not stored
    assert view.next_region == nodes["降维"].id


def test_the_user_stops_and_the_same_goal_again_reuses_its_map(env):
    repo, s, _ids = env
    res = start(env)
    out = stop(env, res.draft.draft_id)
    assert out.map_id == res.draft.map_id
    assert repo.wiki_log("u1")[-1].body.outcome == "stopped"
    s.push("goals.intake", ML)                           # no probe scripted: must not be called
    again = scripted_map(env, GOAL, "")
    assert again.map_id == res.draft.map_id and again.calls == 0   # its map, no diagnosis again
    assert state(env).map_order == (res.draft.map_id,)
    saved = [e.body for e in repo.wiki_log("u1") if isinstance(e.body, GraphSaved)]
    assert len(saved) == 1                               # no second set of nodes


def test_the_path_model_sees_the_existing_pages(env):
    repo, s, ids = env
    s.push("goals.intake", {"title": "z", "concepts": [{"id": "n1", "name": "z 分数",
                                                        "target": True}]})
    push_no_question(s)
    push_one_page(s, ["c1"])
    z = flat_goal(repo, gw(env), "u1", "z 分数是什么", now=NOW, new_id=ids)
    next_path_page(repo, "u1", z.path_id, now=NOW, new_id=ids)    # 《z》 started, not written
    res = start(env, selfs=[])
    push_no_question(s)                                  # no usable item inside the region
    push_one_page(s, ["c3", "c4"])                      # 矩阵, 矩阵乘法 (z 分数 is c1)
    view = mapped(env, res.map_id or res.draft.map_id)
    enter_region(repo, gw(env), "u1", res.map_id, by_name(view)["线性代数"].id, now=NOW,
                 new_id=ids)
    p = s.requests("goals.path")[-1].prompt
    # the page 《z》 was made but its body never written: it teaches nothing yet (D04), so it is not listed
    assert "页《z》（在学）" not in p


def test_old_region_without_members_still_expands_by_a_scoped_intake(env):
    from evowiki.goals.regions import GoalMap, MapNode
    from evowiki.wikiapp.events import WikiEvent
    repo, s, ids = env
    old = GoalMap(map_id="map-old", goal="旧目标", created_at=NOW,
                  nodes=(MapNode(id="c9", kind="region", name="旧区域", scope="甲、乙"),))
    repo.commit(repo.load("u1"), Commit(wiki=[WikiEvent(event_id="w1", ts=NOW, body=GoalMapSaved(
        record=old, action="planned"))]))
    s.push("goals.intake", {"parts": [{"name": "甲", "aspects": ["term"], "atomic": True},
                                      {"name": "乙", "atomic": True}]})
    out = enter_region(repo, gw(env), "u1", "map-old", "c9", now=NOW, new_id=ids)
    assert out.map_id == "map-old" and out.path_id is None
    m = state(env).maps["map-old"]
    assert [n.id for n in m.children("c9")] == ["c1", "c2"] and m.node("c9").state == "expanded"


def test_runtime_state_equals_replay(env):
    repo = env[0]
    res = start(env)
    stop(env, res.draft.draft_id)
    assert replay(repo.wiki_log("u1")).to_json() == repo.load("u1").state.to_json()
    assert any(isinstance(e.body, DraftCreated) for e in repo.wiki_log("u1"))


def test_a_region_has_direct_evidence_of_its_own_next_to_its_members_both_counted(env):
    from evowiki.nodes import node_evidence
    repo, _, _ = env
    start(env, selfs=[])
    learn(env, [f"{LA}.term"])                    # a right answer on 线性代数 itself
    snap = repo.load("u1")
    la = by_name(mapped(env))["线性代数"]
    # its cells: its own two (term, purpose) and its members' (c2.term, c3.procedure)
    assert sum(la.counts.values()) == 4 and la.counts["known"] == 1 and la.mix["known"] == 0.25
    kv = snap.kview(NOW)
    ev = node_evidence(snap.state.graph, LA,
                       lambda c: [kv.cell(f"{c}.{a.value}").band.value for a in kv.aspects(c)])
    assert ev.direct["known"] == 0.5 and ev.direct["none"] == 0.5          # its own cells
    assert ev.aggregate["none"] == 1.0                    # the members: no evidence, not inferred
    c1 = by_name(mapped(env))["矩阵"]
    assert c1.counts["known"] == 0                        # part_of never propagates evidence


def test_a_scoped_expansion_writes_its_concepts_under_the_region_in_the_graph(env):
    from evowiki.goals.regions import GoalMap, MapNode
    from evowiki.wikiapp.events import WikiEvent
    repo, s, ids = env
    old = GoalMap(map_id="map-old", goal="旧目标", created_at=NOW,
                  nodes=(MapNode(id="c9", kind="region", name="旧区域", scope="甲、乙"),))
    repo.commit(repo.load("u1"), Commit(wiki=[WikiEvent(event_id="w1", ts=NOW, body=GoalMapSaved(
        record=old, action="planned"))]))
    s.push("goals.intake", {"parts": [{"name": "甲", "aspects": ["term"], "atomic": True},
                                      {"name": "乙", "atomic": True}]})
    enter_region(repo, gw(env), "u1", "map-old", "c9", now=NOW, new_id=ids)
    g = state(env).graph
    assert [g.node(k).name for k in g.children("c9")] == ["甲", "乙"]
    assert in_step(g, repo.load("u1").k)
    assert all(r.source == "intake" for r in g.relations if r.dst == "c9")
    assert [e.body.action for e in repo.wiki_log("u1") if isinstance(e.body, GraphSaved)] == ["goal_nodes"]


def test_a_scoped_outline_hangs_its_first_level_under_the_region_one_level_only(env):
    """intake@v19: only the scoped outline's first-level blocks are part_of the region. intake@v26
    (chat-first blocks): a region with no members is split ONE level — a part marked page:false
    stays a leaf (the learner splits it further with 拆成多页), no split call."""
    from evowiki.goals.regions import GoalMap, MapNode
    from evowiki.wikiapp.events import WikiEvent
    repo, s, ids = env
    old = GoalMap(map_id="map-old", goal="旧目标", created_at=NOW,
                  nodes=(MapNode(id="c9", kind="region", name="旧区域", scope="甲、乙"),))
    repo.commit(repo.load("u1"), Commit(wiki=[  # the map and its region node, as goal creation writes them
        WikiEvent(event_id="w1", ts=NOW, body=GoalMapSaved(record=old, action="planned")),
        WikiEvent(event_id="w2", ts=NOW, body=GraphSaved(
            graph=NodeGraph(nodes=(NodeRec(id="c9", name="旧区域"),)), action="goal_nodes"))]))
    s.push("goals.intake", {"blocks": [
        {"name": "甲", "page": False, "target": True}, {"name": "乙", "page": True}],
        "_parts": {"name:甲": [{"name": "甲一", "page": True}, {"name": "甲二", "page": True}]}})
    enter_region(repo, gw(env), "u1", "map-old", "c9", now=NOW, new_id=ids)
    g = state(env).graph
    by = {n.name: n.id for n in g.nodes}
    assert set(g.children("c9")) == {by["甲"], by["乙"]}
    assert "甲一" not in by and s.calls("goals.enter") == 1          # one level only
    assert "「旧区域」（甲、乙） ← 这一块" in s.requests("goals.enter")[0].prompt


# --- the opening's decisions carry into a region ---

# (The gap call's levels as the prior of an older region goal are gone with the gap call, S2: an
# entry builds no draft; S1's blocks still carry `said_level` priors, tests/wikiapp/test_goal_blocks.)


def _until_c2_right(env, draft_id) -> None:
    """说不清 on the coarse round, 不知道 on every quiz the diagnosis picks before c3 矩阵乘法
    (probe@v6: the half-split order is the algorithm's), then c3's key."""
    res = answer(env, draft_id, [3])                          # 说不清: no evidence
    while (q := question_of(res.draft.rounds[-1])).node != "c3":
        res = answer(env, draft_id, [-1])
    answer(env, draft_id, [q.answer])


def test_entering_a_region_keeps_the_openings_decisions_even_inferred_ones(env):
    """The opening asks 矩阵乘法 (after whatever half-split picks first, answered 不知道) and gets
    the key: 矩阵 (its VERIFIED prerequisite) is decided known by inference — K has no direct
    evidence on it. Entering 线性代数 asks nothing (有证据就不再问): the opening's decisions are
    kept, then the pages are planned."""
    repo, s, ids = env
    res = start(env, selfs=[SELFS[0]])
    verify_prereqs(repo, "u1", ML_PREREQS, now=NOW, new_id=ids)
    _until_c2_right(env, res.draft.draft_id)
    opening = state(env).drafts[res.draft.draft_id].rounds[-1].decisions
    assert {x.node: (x.decision, x.source) for x in opening}["c2"] == ("known", "inferred")
    stop(env, res.draft.draft_id)
    push_pages(s, ["c2"], ["c3"])
    probes = s.calls(PROBE)
    out = enter_region(repo, gw(env), "u1", res.draft.map_id, LA, now=NOW, new_id=ids)
    assert out.path_id is not None and s.calls(PROBE) == probes      # no re-diagnosis
    log = state(env).maps[res.draft.map_id].log
    assert "摸底结论 c2 矩阵：会" in "\n".join(log)
    assert any(line.startswith("摸底结论 c2 ") and "依据：大目标开头的摸底" in line for line in log)


def test_entering_a_region_asks_only_members_without_any_answer(env):
    """Only members the opening had no answer on may be asked in the region (here: those of
    监督学习 the opening did not reach); members it decided are fixed."""
    from evowiki.wikiapp.goal import _opening_decisions
    repo, _, ids = env
    res = start(env, selfs=[SELFS[0]])
    verify_prereqs(repo, "u1", ML_PREREQS, now=NOW, new_id=ids)
    _until_c2_right(env, res.draft.draft_id)
    stop(env, res.draft.draft_id)
    m = state(env).maps[res.draft.map_id]
    kept = _opening_decisions(state(env), m)
    assert kept["c2"] == kept["c3"] == "known"
    unanswered = {"c11", "c12"} - set(kept)
    assert unanswered
    sl = by_name(mapped(env))["监督学习"].id
    entered = enter_region(repo, gw(env), "u1", res.draft.map_id, sl, now=NOW, new_id=ids)
    assert entered.open
    asked = {entered.draft.rounds[0].pick.node}
    assert asked and asked <= unanswered


def test_nested_areas_and_a_shared_part_are_one_node_and_one_item_each(env):
    """intake@v14 at any granularity: 数学 ⊃ 线性代数 ⊃ 矩阵乘法, 梯度下降 a part of 数学 and of
    机器学习 (two parents). Every node is one graph node and one probe item; 数学's parts include
    the area 线性代数; the graph keeps both parents with the model's phrases."""
    _repo, s, _ids = env
    s.push("goals.intake", {"title": "机器学习", "nodes": [
        {"id": "n1", "name": "矩阵乘法", "aspects": ["procedure"]},
        {"id": "n4", "name": "矩阵", "aspects": ["term"]},
        {"id": "n2", "name": "梯度下降", "aspects": ["procedure"]},
        {"id": "n3", "name": "线性回归", "target": True, "aspects": ["purpose"]},
        {"id": "a1", "name": "线性代数"}, {"id": "a2", "name": "数学"},
        {"id": "a3", "name": "机器学习", "target": True}],
        "relations": [rel("part_of", "n1", "a1"), rel("part_of", "n4", "a1"),
                      rel("part_of", "a1", "a2", "线性代数是数学的一支"),
                      rel("part_of", "n2", "a2", "梯度下降是数值优化方法"),
                      rel("part_of", "n2", "a3", "训练模型靠梯度下降"), rel("part_of", "n3", "a3"),
                      rel("prerequisite", "a2", "a3", "机器学习的模型都写成数学")]})
    push_no_question(s)
    res = scripted_map(env, GOAL, "")
    st = state(env)
    by = {n.name: n.id for n in st.graph.nodes}
    assert set(st.graph.parents(by["梯度下降"])) == {by["数学"], by["机器学习"]}
    assert st.graph.parents(by["线性代数"]) == (by["数学"],)
    phrases = {(r.src, r.dst): r.phrase for r in st.graph.relations}
    assert phrases[(by["数学"], by["机器学习"])] == "机器学习的模型都写成数学"
    view = by_name(mapped(env, res.map_id))
    assert set(view["数学"].members) == {by["线性代数"], by["梯度下降"]}
    final = st.drafts[res.draft.draft_id].rounds[-1] if res.draft.draft_id in st.drafts \
        else res.draft.rounds[-1]
    decided = [d.node for d in final.decisions]
    assert len(decided) == len(set(decided)) and set(decided) == set(by.values())
    # a region that is a part of another hangs under it (the 黑壳虾 run: every region had parent
    # None, so 线性代数 and 数学 were two rows); the top-level regions are 数学 and 机器学习
    assert view["线性代数"].parent == by["数学"] and view["线性代数"].kind == "region"
    assert {n.name for n in view.values() if n.kind == "region" and n.parent is None} == \
        {"数学", "机器学习"}


def test_probe_asks_only_top_level_regions_as_regions(env):
    """With a nested region, the opening round's region items are the top-level regions; the
    nested one is a concept item (its own cells), never a second region item."""
    from evowiki.wikiapp.goal import _probe_nodes, _Work

    repo, s, ids = env
    s.push("goals.intake", {"title": "机器学习", "nodes": [
        {"id": "n1", "name": "矩阵乘法", "aspects": ["procedure"]},
        {"id": "n2", "name": "梯度下降", "aspects": ["procedure"]},
        {"id": "n3", "name": "线性回归", "target": True, "aspects": ["purpose"]},
        {"id": "n4", "name": "矩阵", "aspects": ["term"]},
        {"id": "n5", "name": "损失函数", "aspects": ["term"]},
        {"id": "a1", "name": "线性代数"}, {"id": "a2", "name": "数学"},
        {"id": "a3", "name": "机器学习", "target": True}],
        "relations": [rel("part_of", "n1", "a1"), rel("part_of", "n4", "a1"),
                      rel("part_of", "a1", "a2"), rel("part_of", "n2", "a2"),
                      rel("part_of", "n3", "a3"), rel("part_of", "n5", "a3")]})
    push_no_question(s)
    res = scripted_map(env, GOAL, "")
    snap = repo.load("u1")
    m = snap.state.maps[res.map_id]
    items = _probe_nodes(_Work(snap, None, "u1", now=NOW, new_id=ids), res.draft, m)
    names = {n.id: n.name for n in m.nodes}
    assert [names[n.id] for n in items if n.region] == ["数学", "机器学习"]
    assert {names[n.id] for n in items if not n.region} == {
        "线性代数", "矩阵乘法", "矩阵", "梯度下降", "线性回归", "损失函数"}


def test_unverified_prerequisite_infers_nothing_a_verified_one_does(env):
    """Relation layers: the model's (unverified) edge — here the recommended c2 → c3 — never feeds
    the KST surmise relation, so nothing is ever inferred KNOWN through it; it is used only
    downward (probe §Failed below, user ruling 2026-10-24): 不知道 on 矩阵 (c2) drops the question
    on 矩阵乘法 (c3), decided teach by inference. (A right answer on c3 not inferring c2 known:
    tests/goals/test_probe.py, the unverified-edge cases.)"""
    res = start(env, selfs=[SELFS[0]])
    r = answer(env, res.draft.draft_id, [3])                  # 说不清: no evidence
    asked = []
    while r.open and r.draft.rounds[-1].pick.intent == "self_report":   # one block a round
        r = answer(env, res.draft.draft_id, [3])
    while r.open:
        asked.append(r.draft.rounds[-1].pick.node)
        r = answer(env, res.draft.draft_id, [-1])
    assert asked[0] == "c2" and "c3" not in asked
    decided = {x.node: (x.decision, x.source)
               for x in state(env).drafts[res.draft.draft_id].rounds[-1].decisions}
    assert decided["c3"] == ("teach", "inferred")
    assert all(d != "known" for d, _ in decided.values() if _ == "inferred")


def test_the_graph_lists_all_of_a_goals_paths(env):
    """A goal split on demand has a path per block opened; the 知识图谱 lists each, not only the last."""
    from evowiki.wikiapp.graph_out import graph_out
    repo, s, ids = env
    res = start(env)
    answer(env, res.draft.draft_id, [1, 1])
    stop(env, res.draft.draft_id)
    push_pages(s, ["c5"], ["c6"])
    first = enter_region(repo, gw(env), "u1", res.draft.map_id, PR, now=NOW, new_id=ids)
    push_pages(s, ["c2", "c3"])
    second = enter_region(repo, gw(env), "u1", res.draft.map_id, LA, now=NOW, new_id=ids)
    snap = repo.load("u1")
    goal = next(g for g in graph_out(snap.state, snap.k, NOW).goals if g.map_id == res.draft.map_id)
    assert {p.path_id for p in goal.paths} == {first.path_id, second.path_id}
    assert all(p.path and p.name for p in goal.paths)


def test_a_page_he_adds_is_a_point_under_the_block(env):
    from evowiki.wikiapp.path_edits import edit_path
    from evowiki.wikiapp.wiki_schemas import PathAddIn
    repo, s, ids = env
    res = start(env)
    answer(env, res.draft.draft_id, [1, 1])
    stop(env, res.draft.draft_id)
    push_pages(s, ["c5"], ["c6"])
    out = enter_region(repo, gw(env), "u1", res.draft.map_id, PR, now=NOW, new_id=ids)
    edit_path(repo, "u1", out.path_id, PathAddIn(op="add", request_id="test-1", title="我加的一页"), now=NOW, new_id=ids)
    graph = state(env).graph
    added = next(n for n in graph.nodes if n.name == "我加的一页")
    assert graph.parents(added.id) == (PR,) or list(graph.parents(added.id)) == [PR]
