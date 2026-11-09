"""The group naming after a graph change (`wikiapp.group_naming`, spec 2026-10-29 retire-curate §5):
a graph commit enqueues it, it waits until the groups stop changing, names what code can (one
member, a group that is its parent's children) and the rest in ONE `graph.group_naming` call, and
commits only `GroupsNamed`. Scripted LLM only, no paid call."""

import json
import re

import pytest

from evowiki.nodes import NodeRec, Relation
from evowiki.store.wiki import wiki_events
from evowiki.wikiapp import group_naming
from evowiki.wikiapp.events import GraphSaved, LanguageSet, WikiEvent
from evowiki.wikiapp.graph_groups import PURPOSE as NAMING
from evowiki.wikiapp.graph_sync import same_structure
from evowiki.wikiapp.group_naming import NamingDeps, run_naming, unnamed_keys
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.repo import Commit, PgRepo, listen_graph
from tests.llm_cases.graph_demo import ml_graph
from tests.runtime.helpers import make_user
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids

NAMES = "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥"


def numbers(prompt: str) -> list[int]:
    return [int(n) for n in re.findall(r"^第 (\d+) 组（", prompt, re.MULTILINE)]


def answer_all(prompt: str) -> dict:
    return {"groups": [{"n": n, "name": f"主题{NAMES[n - 1]}", "summary": "讲这一组的内容"}
                       for n in numbers(prompt)]}


def graph_event(ids, graph=None, action="goal_nodes") -> WikiEvent:
    return WikiEvent(event_id=ids("wev"), ts=NOW,
                     body=GraphSaved(graph=graph if graph is not None else ml_graph(),
                                     action=action))


def grown(graph, i: int):
    """`graph` plus two linked nodes of their own (a new small group)."""
    g = graph.with_node(NodeRec(id=f"x{i}", name=f"新点{i}", x=0.9, y=0.1 * i))
    g = g.with_node(NodeRec(id=f"y{i}", name=f"新点{i}的细节", x=0.95, y=0.1 * i))
    return g.with_relation(Relation(kind="part_of", src=f"y{i}", dst=f"x{i}"))


@pytest.fixture
def env(pool, monkeypatch):
    """PgRepo with the graph listener on; enqueued runs are collected, not started."""
    make_user(pool, "u1")
    repo, s, ids = PgRepo(pool), Script(), Ids()
    queued: list[tuple[str, str]] = []
    monkeypatch.setattr(group_naming, "_enqueue", lambda wid, uid: queued.append((wid, uid)))
    listen_graph(group_naming.request_naming)
    s.always(NAMING, answer_all)
    sleeps: list[float] = []
    deps = NamingDeps(repo=repo, gateway=lambda uid: CountingGateway(s.gateway(), uid),
                      clock=lambda: NOW, new_id=ids, sleeper=sleeps.append)
    yield repo, s, ids, queued, deps
    listen_graph(None)


def numbers_text(prompt: str) -> str:
    """The asked groups' part of the prompt (from 「第 1 组」 on)."""
    return prompt[prompt.index("第 1 组"):]


def run_queued(env) -> int:
    """Each enqueued run, one after the other (the per-user partition runs one at a time)."""
    _, _, _, queued, deps = env
    return sum(run_naming(uid, deps) for _, uid in dict.fromkeys(queued))


def groups_named(repo) -> list:
    with repo.pool.connection() as conn:
        return [e for e in wiki_events(conn, "u1") if e.payload["type"] == "groups_named"]


def naming_calls(s) -> int:
    """The calls that name groups (the same job's domain call is `group_domains`')."""
    return sum(r.prompt_version == "group-naming-v1" for r in s.requests(NAMING))


def test_a_graph_change_names_its_groups_with_one_call(env):
    repo, s, ids, queued, _ = env
    repo.commit(repo.load("u1"), Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=NOW,
                                                        body=LanguageSet(language="zh"))]))
    assert queued == []                                     # no graph change: nothing
    graph = ml_graph()
    repo.commit(repo.load("u1"), Commit(wiki=[graph_event(ids)]))
    assert len(queued) == 1
    stored = run_queued(env)
    st = repo.load("u1").state
    assert naming_calls(s) == 1 and stored == len(st.groups) > 0
    assert unnamed_keys(st) == []
    names = {g.name for g in st.groups.values()}
    assert {"线性代数", "监督学习", "概率统计"} <= names           # code's: the parent's name
    assert any(n.startswith("主题") for n in names)            # the model's
    assert same_structure(st.graph, graph)                    # never a graph change
    [req] = [r for r in s.requests(NAMING) if r.prompt_version == "group-naming-v1"]
    assert req.subject == "user:u1" and req.prompt_version == "group-naming-v1"
    asked = [ln for ln in numbers_text(req.prompt).splitlines() if ln.startswith("知识点：")]
    assert not any("矩阵乘法" in ln for ln in asked)            # code named 线性代数's group
    assert "- 线性代数" in req.prompt                           # ... shown by its name in a big one


def test_domain_result_does_not_commit_after_its_input_graph_changes(env, monkeypatch):
    from evowiki.wikiapp.group_domains import tops_now

    repo, _, ids, _, deps = env
    repo.commit(repo.load("u1"), Commit(wiki=[graph_event(ids)]))
    old_tops = tuple(sorted(g.id for g in tops_now(repo.load("u1").state)[0]))

    def delayed_result(gateway, state, user_id):
        repo.commit(repo.load(user_id), Commit(wiki=[
            graph_event(ids, grown(state.graph, 1), action="user_edit")]))
        return old_tops, ()

    monkeypatch.setattr(group_naming, "group_domains", delayed_result)
    group_naming.group_into_domains(deps, "u1")
    state = repo.load("u1").state
    assert tuple(sorted(g.id for g in tops_now(state)[0])) != old_tops
    assert state.domains_for != old_tops


def test_a_burst_of_graph_commits_gives_one_call(env):
    repo, s, ids, queued, _ = env
    g = ml_graph()
    for i in range(4):
        g = grown(g, i)
        repo.commit(repo.load("u1"), Commit(wiki=[graph_event(ids, g)]))
    assert len(set(queued)) == 4                            # one run per unnamed set ...
    run_queued(env)
    assert naming_calls(s) == 1                             # ... but one call names them all
    assert unnamed_keys(repo.load("u1").state) == [] and len(groups_named(repo)) == 1


def test_a_change_while_waiting_is_named_in_the_same_call(env):
    repo, s, ids, _, deps = env
    repo.commit(repo.load("u1"), Commit(wiki=[graph_event(ids)]))
    landed = []

    def sleeper(_seconds):                                  # the next commit of the burst
        if not landed:
            landed.append(repo.commit(repo.load("u1"), Commit(
                wiki=[graph_event(ids, grown(ml_graph(), 1), action="user_edit")])))

    run_naming("u1", NamingDeps(repo=deps.repo, gateway=deps.gateway, clock=deps.clock,
                                new_id=deps.new_id, sleeper=sleeper))
    assert landed and naming_calls(s) == 1
    st = repo.load("u1").state                    # the late group is its parent's: code names it
    assert "新点1" in {g.name for g in st.groups.values()} and unnamed_keys(st) == []


def test_anything_else_in_the_reply_is_never_applied(env):
    repo, s, ids, _, _ = env
    s.always(NAMING, lambda p: {**answer_all(p), "proposals": [
        {"op": "hub", "name": "乱来", "members": ["vec", "mul"], "why": "x"}]})
    repo.commit(repo.load("u1"), Commit(wiki=[graph_event(ids)]))
    run_queued(env)
    st = repo.load("u1").state
    assert same_structure(st.graph, ml_graph()) and "乱来" not in {g.name for g in st.groups.values()}


@pytest.mark.parametrize("reply", [
    lambda p: "not json",
    lambda p: json.dumps({**answer_all(p), "groups": answer_all(p)["groups"][:-1]}),
])
def test_an_unusable_reply_stores_only_codes_names(env, reply):
    repo, s, ids, _, _ = env
    s.always(NAMING, reply)
    repo.commit(repo.load("u1"), Commit(wiki=[graph_event(ids)]))
    run_queued(env)
    st = repo.load("u1").state
    assert {g.name for g in st.groups.values()} == {"线性代数", "监督学习", "概率统计", "模型评估", "工具"}
    assert unnamed_keys(st) != []                            # the rest keep their fallback names


def test_no_gateway_names_nothing(env):
    repo, s, ids, _, deps = env
    repo.commit(repo.load("u1"), Commit(wiki=[graph_event(ids)]))
    no_llm = NamingDeps(repo=deps.repo, gateway=lambda uid: None, clock=deps.clock,
                        new_id=deps.new_id, sleeper=deps.sleeper)
    assert run_naming("u1", no_llm) == 5 and s.calls() == 0     # code's names only
    assert unnamed_keys(repo.load("u1").state) != []


def test_a_confirmed_goal_gets_its_group_names_with_one_call(env):
    """After 确认 the goal's nodes land in the graph, all on the goal: no naming call."""
    from tests.wikiapp.flat_goal import flat_goal, flat_page
    from tests.wikiapp.test_goal import BACKGROUND, GOAL, INTAKE_1, script_diag
    repo, s, ids, _queued, _ = env
    s.push("goals.intake", INTAKE_1)
    script_diag(s)
    res = flat_goal(repo, CountingGateway(s.gateway()), "u1", GOAL, BACKGROUND, now=NOW,
                    new_id=ids)
    flat_page(repo, CountingGateway(s.gateway()), "u1", res.draft.draft_id, now=NOW, new_id=ids)
    run_queued(env)
    st = repo.load("u1").state
    assert s.calls(NAMING) == 0 and unnamed_keys(st) == []     # all on the goal: its blocks have their real names
