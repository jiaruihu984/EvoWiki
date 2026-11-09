"""graph.group_naming (group-naming-v1, step `graph_group_naming`) offline cases.

Information flow (the product specification), rule GROUPS: code
names what it can (one member: its name; a group that is its part_of parent's children: the
parent's name, never a much wider parent); the call sees the rest by their members' NAMES and the
relations among them, never an id, and no page rules, K bands or directory standard; it answers by
group number with a name and one sentence; code maps the numbers back and refuses an unusable
answer. The call's subject is the user (its call log, and so its cache, is the user's own)."""

import pytest

from evowiki.llm.prompt_steps import STEP_BY_KEY, marked, step_of, strip_marks
from evowiki.nodes import NodeGraph, NodeRec, Relation, cluster
from evowiki.wikiapp import graph_groups
from evowiki.wikiapp.graph_groups import (
    GroupAsk,
    GroupNameReply,
    asks,
    group_labels,
    name_groups,
    naming_request,
    settle,
    to_name,
)
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.state import WikiState
from tests.llm_cases.graph_demo import ML_NODES, ml_graph
from tests.wikiapp.script import Script

PURPOSE = graph_groups.PURPOSE          # "graph.group_naming"
ASCII_IDS = [i for i in ML_NODES if len(i) > 2]


def ml_state() -> WikiState:
    return WikiState(graph=ml_graph())


def rest_of(state: WikiState):
    unnamed, groups = to_name(state)
    mine, rest = settle(state, unnamed)
    return mine, rest, groups


# --- what code names without a call ---------------------------------------------------------------

def test_code_names_a_group_that_is_its_parents_children():
    mine, _, _ = rest_of(ml_state())
    assert {x.name for x in mine} == {"线性代数", "监督学习", "概率统计", "模型评估", "工具"}


def test_a_one_member_group_takes_its_members_name_and_definition():
    g = NodeGraph(nodes=(NodeRec(id="z", name="z 分数", definition="离均值几个标准差"),))
    state = WikiState(graph=g)
    unnamed, _ = to_name(state)
    mine, rest = settle(state, unnamed)
    assert rest == [] and [(x.name, x.summary) for x in mine] == [("z 分数", "离均值几个标准差")]


def test_a_parent_much_wider_than_the_group_is_not_its_name():
    """「数学基础」 has many children; a group of the z-score pieces under it is not 「数学基础」."""
    kids = [("mean", "均值"), ("sd", "标准差"), ("z", "z 分数"), ("nd", "正态分布")]
    other = [(f"o{i}", f"别的数学{i}") for i in range(8)]
    nodes = [NodeRec(id="base", name="数学基础"), *(NodeRec(id=i, name=n) for i, n in kids + other)]
    rels = [Relation(kind="part_of", src=i, dst="base") for i, _ in kids + other]
    rels += [Relation(kind="prerequisite", src="mean", dst="sd"),
             Relation(kind="prerequisite", src="sd", dst="z"),
             Relation(kind="prerequisite", src="z", dst="nd")]
    state = WikiState(graph=NodeGraph(nodes=tuple(nodes), relations=tuple(rels)))
    unnamed, _ = to_name(state)
    mine, rest = settle(state, unnamed)
    z_groups = [g for g in unnamed if "z" in g.members and "base" not in g.members]
    assert z_groups and all(g in rest for g in z_groups)        # asked, not 「数学基础」
    assert "数学基础" not in {x.name for x in mine}


# --- what the call sees --------------------------------------------------------------------------

def test_the_prompt_shows_names_and_relations_never_ids():
    state = ml_state()
    mine, rest, groups = rest_of(state)
    p = naming_request(asks(state, rest, groups, mine), user_id="u1").prompt
    assert "知识点：微积分、导数、梯度、数学" in p
    assert "先学「导数」再学「梯度」（梯度是每个方向上的导数排在一起）" in p
    assert "- 线性代数" in p and "第 3 组（这次一起起名）" in p      # a level-2 group's parts
    for i in ASCII_IDS:
        assert f"「{i}」" not in p and f"、{i}、" not in p, i


def test_the_prompt_has_no_page_rules_k_bands_or_directory_standard():
    state = ml_state()
    mine, rest, groups = rest_of(state)
    p = strip_marks(naming_request(asks(state, rest, groups, mine), user_id="u1").prompt)
    for gone in ("确定会", "没证据", "目录标准", "proposals", "可用操作", "整理", "All learner-facing"):
        assert gone not in p, gone
    assert "用中文写，不要中英文夹杂" in p
    assert "本组包括均值、标准差、z 分数和正态分布" in p             # the bad example, named as bad
    assert "不要加入组里没有的知识" in p


def test_an_english_learner_gets_the_language_rule():
    p = naming_request([GroupAsk(members=("a", "b"), level=1, body="知识点：甲、乙")],
                       language="en", user_id="u1").prompt
    assert "English" in p and "用中文写" not in p


def test_the_subject_is_the_user():
    ask = [GroupAsk(members=("a", "b"), level=1, body="知识点：甲、乙")]
    a, b = (naming_request(ask, user_id=u) for u in ("u1", "u2"))
    assert (a.subject, b.subject) == ("user:u1", "user:u2") and a.prompt == b.prompt
    assert a.purpose == PURPOSE and a.prompt_version == "group-naming-v1"


def test_the_guidance_is_the_steps_own_span():
    step = STEP_BY_KEY["graph_group_naming"]
    assert step_of(PURPOSE) is step
    p = naming_request([GroupAsk(members=("a", "b"), level=1, body="知识点：甲、乙")]).prompt
    assert set(marked(p)) == {"graph_group_naming"}
    assert "只回复 JSON" not in marked(p)["graph_group_naming"]   # the output format stays outside


# --- what code does with the answer ---------------------------------------------------------------

ASK = [GroupAsk(members=("a", "b"), level=1, body="x"), GroupAsk(members=("c", "d"), level=1, body="y")]


def test_the_answer_maps_back_by_number():
    out = group_labels([GroupNameReply(n=2, name=" 乙组 ", summary="s2"),
                        GroupNameReply(n=1, name="甲组", summary="s1")], ASK)
    assert [(x.members, x.name, x.summary) for x in out] == [(("a", "b"), "甲组", "s1"),
                                                              (("c", "d"), "乙组", "s2")]


@pytest.mark.parametrize("reply", [
    [GroupNameReply(n=1, name="甲")],                                       # a group left out
    [GroupNameReply(n=1, name="甲"), GroupNameReply(n=1, name="乙")],       # repeated
    [GroupNameReply(n=1, name="甲"), GroupNameReply(n=3, name="丙")],       # not asked
    [GroupNameReply(n=1, name="甲"), GroupNameReply(n=2, name="  ")],       # empty name
])
def test_an_unusable_answer_is_refused(reply):
    with pytest.raises(ValueError):
        group_labels(reply, ASK)


def test_one_call_names_the_rest():
    state = ml_state()
    mine, rest, groups = rest_of(state)
    s = Script().push(PURPOSE, {"groups": [{"n": n, "name": f"组{n}", "summary": "一句"}
                                           for n in range(1, len(rest) + 1)]})
    out = name_groups(CountingGateway(s.gateway()), asks(state, rest, groups, mine),
                      user_id="u1")
    assert s.calls(PURPOSE) == 1 and len(out) == len(rest) == 5
    assert {x.members for x in out} == {tuple(g.members) for g in rest}


def test_nothing_to_ask_makes_no_call():
    s = Script()
    assert name_groups(CountingGateway(s.gateway()), [], user_id="u1") == ()
    assert s.calls() == 0


def test_clustering_of_the_demo_graph_is_stable():
    assert len(cluster(ml_graph())) >= 10            # the cases above rely on the demo's groups
