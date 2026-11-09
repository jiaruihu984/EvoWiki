"""GET /wiki/graph over HTTP (plan T5, wire contract §4) on the
Postgres service fixtures: a user with a goal (seeded path), a legacy two-tier user converted by the
an empty wiki."""

import pytest

from evowiki.wikiapp.events import GraphSaved, WikiEvent
from evowiki.wikiapp.repo import Commit, PgRepo
from evowiki.wikiapp.wiki_schemas import GraphOut
from tests.llm_cases.graph_demo import ml_graph
from tests.service.conftest import H, signup
from tests.wikiapp.script import Script
from tests.wikiapp.test_path_edits import MAP_ID, PATH_ID, seed_events

NODE_KEYS = {"id", "name", "definition", "band", "mix", "direct", "aggregate", "parents",
             "evidence", "kind", "x", "y", "can_split"}


@pytest.fixture
def seeded(services, client):
    signup(client, "u1")
    repo = PgRepo(services.pool)
    aspect, wiki = seed_events(services.new_id)
    repo.commit(repo.load("u1"), Commit(aspect=aspect, wiki=wiki))
    return client, services


def get(client):
    r = client.get("/wiki/graph", headers=H("u1"))
    assert r.status_code == 200, r.text
    return r.json()


def test_graph_of_a_user_with_a_goal(seeded):
    client, _ = seeded
    body = get(client)
    out = GraphOut.model_validate(body)
    assert set(body) == {"nodes", "relations", "groups", "goals", "domains"}
    assert set(body["nodes"][0]) == NODE_KEYS
    assert set(body["relations"][0]) == {"kind", "src", "dst", "phrase", "verified", "split"}
    assert all(r["verified"] is False for r in body["relations"])     # nothing verifies yet
    assert set(body["groups"][0]) - {"at"} == {"id", "level", "name", "summary", "members",
                                               "parent", "x", "y", "map_id", "copies"}   # `at`: only when placed
    assert set(body["goals"][0]) == {"map_id", "goal", "name", "nodes", "shares", "path_id", "paths",
                                     "path", "sources", "reading"}
    assert sorted(n.id for n in out.nodes) == ["n1", "n2", "n3", "n4", "n5", "r"]
    (goal,) = out.goals
    assert goal.map_id == MAP_ID and goal.path_id == PATH_ID and len(goal.path) == 3
    assert sum(goal.shares.values()) == pytest.approx(1)


def test_an_empty_wiki(services, client):
    signup(client, "u1")
    assert get(client) == {"nodes": [], "relations": [], "groups": [], "goals": [], "domains": []}


def test_a_graph_read_makes_no_model_call(services, client):
    """Audit 2026-10-27 D12: reading the graph used to name unnamed groups after the response (a
    paid call that also raced the learning flow). A read only reads: fallback names are served
    until the groups are named (the naming run a graph change enqueues)."""
    signup(client, "u1")
    repo = PgRepo(services.pool)
    repo.commit(repo.load("u1"), Commit(wiki=[WikiEvent(
        event_id=services.new_id("wev"), ts=services.clock(),
        body=GraphSaved(graph=ml_graph(), action="goal_nodes"))]))
    s = Script()
    services.gateway = s.gateway()
    first = get(client)
    assert get(client) == first and s.calls() == 0
    assert repo.load("u1").state.groups == {}
    assert all(g["summary"] == "" for g in first["groups"])


def test_a_graph_placed_by_the_old_layout_is_laid_out_again_once(services, client):
    """Bug 2026-10-15: a user migrated by the old centre spiral (no layout version) gets one
    `relayout` on the first GET; groups are apart; the next GET changes nothing."""
    from tests.nodes.synthetic_shape import migrated_graph
    signup(client, "u1")
    repo = PgRepo(services.pool)
    repo.commit(repo.load("u1"), Commit(wiki=[WikiEvent(
        event_id=services.new_id("wev"), ts=services.clock(),
        body=GraphSaved(graph=migrated_graph(), action="migrated"))]))
    first = GraphOut.model_validate(get(client))
    assert repo.latest_graph_action("u1") == "relayout"
    centres = [(g.x * 1.15, g.y) for g in first.groups if g.level == 1]
    assert len(centres) == 3
    assert min(((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5
               for i, a in enumerate(centres) for b in centres[:i]) > 1.0
    n = repo.load("u1").wiki_seq
    assert GraphOut.model_validate(get(client)) == first and repo.load("u1").wiki_seq == n
