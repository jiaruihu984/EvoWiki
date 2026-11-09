"""The level above goals (ruling 「大类让模型分」): the model's grouping, no level invented, the last grouping standing on a failure."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from evowiki.goals.regions import GoalMap, MapNode
from evowiki.nodes import NodeGraph, NodeRec, place_missing
from evowiki.wikiapp.events import Domain
from evowiki.wikiapp.group_domains import (
    DomainReply,
    DomainsReply,
    domain_request,
    domains_of,
    domains_out,
    group_domains,
    needs_grouping,
)
from evowiki.wikiapp.state import WikiState
from evowiki.wikiapp.wiki_schemas import GroupOut

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def group(name, members=()):
    return GroupOut(id=name, level=2, name=name, summary="", members=list(members), parent=None, x=0, y=0)


TOPS = [group("黑壳虾"), group("孔雀鱼"), group("神经网络")]


def state_of(*titles):
    maps = [GoalMap(map_id=t, goal="g", title=t, created_at=T0, nodes=(MapNode(id=f"{t}-a", kind="region", name=f"{t}块"),)) for t in titles]
    graph = place_missing(NodeGraph(nodes=tuple(NodeRec(id=m.nodes[0].id, name=m.nodes[0].name, definition="") for m in maps), relations=()))
    return WikiState(graph=graph, maps={m.map_id: m for m in maps}, map_order=[m.map_id for m in maps])


class Gateway:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def call(self, role, request, schema):
        self.calls.append(request)
        return self.reply, SimpleNamespace()


def test_groups_of_one_domain_are_kept_and_a_group_alone_stands_as_itself():
    [d] = domains_of([DomainReply(name="生物", summary="s", groups=[1, 2]), DomainReply(name="计算机", groups=[3])], TOPS)
    assert (d.name, d.kids) == ("生物", ("黑壳虾", "孔雀鱼"))


def test_all_groups_in_one_domain_is_no_level():
    assert domains_of([DomainReply(name="生物", groups=[1, 2, 3])], TOPS) == ()


@pytest.mark.parametrize("reply", [[DomainReply(name="x", groups=[4, 1])], [DomainReply(name="x", groups=[1, 2]), DomainReply(name="y", groups=[2, 3])],
                                   [DomainReply(name=" ", groups=[1, 2])]])
def test_a_reply_that_cannot_be_used_is_refused(reply):
    with pytest.raises(ValueError):
        domains_of(reply, TOPS)


def test_the_call_shows_each_group_and_what_it_holds():
    r = domain_request([group("黑壳虾", ["a"])], [], {"a": "蜕皮"}, user_id="u")
    assert "1. 黑壳虾：蜕皮" in r.prompt


def test_two_groups_ask_nothing_of_the_model():
    gw = Gateway(DomainsReply())
    state = state_of("黑壳虾", "孔雀鱼")
    assert needs_grouping(state)
    tops, domains = group_domains(gw, state)
    assert domains == () and gw.calls == [] and len(tops) == 2


def test_a_grouping_stands_until_the_top_groups_change():
    state = state_of("黑壳虾", "孔雀鱼", "神经网络")
    tops, domains = group_domains(Gateway(DomainsReply(domains=[DomainReply(name="生物", groups=[1, 2])])), state)
    done = state.model_copy(update={"domains": domains, "domains_for": tops})
    assert not needs_grouping(done) and len(domains) == 1
    assert needs_grouping(done.model_copy(update={"domains_for": ()}))


def test_a_domain_shows_the_groups_that_still_stand():
    state = WikiState(domains=(Domain(id="d", name="生物", kids=("黑壳虾", "孔雀鱼")), Domain(id="e", name="旧的", kids=("孔雀鱼", "没了"))))
    [d] = domains_out(state, TOPS)
    assert d.id == "d" and d.kids == ["黑壳虾", "孔雀鱼"]


def test_a_failed_call_leaves_the_last_grouping_and_says_so(caplog, monkeypatch):
    from evowiki.wikiapp import group_naming
    from evowiki.wikiapp.events import DomainsGrouped, GoalMapSaved, GraphSaved, WikiEvent
    from evowiki.wikiapp.repo import Commit, MemoryRepo

    start = state_of("黑壳虾", "孔雀鱼", "神经网络")
    repo = MemoryRepo(["u"])
    last = Domain(id="d", name="生物", kids=("goal-黑壳虾", "goal-孔雀鱼"))
    events = [WikiEvent(event_id="g", ts=T0, body=GraphSaved(graph=start.graph, action="goal_nodes")),
              *(WikiEvent(event_id=f"m{i}", ts=T0, body=GoalMapSaved(record=m, action="planned")) for i, m in enumerate(start.maps.values())),
              WikiEvent(event_id="d", ts=T0, body=DomainsGrouped(tops=("旧的",), domains=(last,)))]
    repo.commit(repo.load("u"), Commit(wiki=events))
    deps = group_naming.NamingDeps(repo=repo, gateway=lambda _: object(), clock=lambda: T0, new_id=lambda p: p + "1")

    def refused(*_a, **_k):
        raise ValueError("the reply puts group 9 in a domain")

    monkeypatch.setattr(group_naming, "group_domains", refused)
    assert group_naming.group_into_domains(deps, "u") == 0
    assert repo.load("u").state.domains == (last,)
    assert "the last grouping stands" in caplog.text
