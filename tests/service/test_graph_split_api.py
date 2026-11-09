"""POST /wiki/nodes/{id}/split and the graph's changes in the RSI change
list (GET /wiki/rsi/changes, POST /wiki/rsi/patches/g_…/undo) over HTTP (plan T6, wire §4).
Scripted LLM only."""

from datetime import timedelta

import pytest

from evowiki.goals.contracts import Aspect
from evowiki.service.deps import graph_change_sources
from evowiki.teach.contracts import ChangeOut
from evowiki.wikiapp.graph_sync import graph_events
from evowiki.wikiapp.repo import Commit, PgRepo
from evowiki.wikiapp.wiki_schemas import SplitOut
from tests.service.conftest import H, signup
from tests.wikiapp.graph_seed import DET, LA, MM, graph
from tests.wikiapp.script import Script
from tests.wikiapp.split_answers import PURPOSE as SPLIT
from tests.wikiapp.split_answers import answer, keep, parts


@pytest.fixture
def seeded(services, client):
    signup(client, "u1")
    repo = PgRepo(services.pool)
    snap = repo.load("u1")
    aspect, wiki = graph_events(snap.state, snap.k, graph(), "goal_nodes",
                                now=services.clock() - timedelta(days=1), new_id=services.new_id,
                                aspects={MM: (Aspect.TERM, Aspect.PROCEDURE)})
    repo.commit(snap, Commit(aspect=list(aspect), wiki=wiki))
    services.change_sources = graph_change_sources(services.pool, services.new_id)
    return client, services, repo


def test_split_returns_the_children_and_the_graph_and_shows_in_the_change_list(seeded):
    client, services, repo = seeded
    s = Script().push(SPLIT, answer())
    services.gateway = s.gateway()
    r = client.post(f"/wiki/nodes/{MM}/split", headers=H("u1"), json={"words": "顺序搞不清"})
    assert r.status_code == 200, r.text
    out = SplitOut.model_validate(r.json())
    assert len(out.children) == 3 and s.calls(SPLIT) == 1
    nodes = {n.id: n for n in out.graph.nodes}
    assert all(c in nodes and nodes[c].parents == [MM] and nodes[c].band == "none"
               for c in out.children)
    assert {(x.kind, x.src, x.dst) for x in out.graph.relations} >= {
        ("part_of", c, MM) for c in out.children}
    [req] = s.requests(SPLIT)
    assert "顺序搞不清" in req.prompt and req.params.get("source") == "dont_get"
    assert out.kept == ""
    assert {(x.kind, x.src, x.dst) for x in out.graph.relations} >= {
        ("recommended", out.children[0], out.children[2])}
    rows = [ChangeOut.model_validate(x) for x in
            client.get("/wiki/rsi/changes", headers=H("u1")).json()]
    g = [x for x in rows if x.track == "G"]
    assert len(g) == 1 and g[0].patch_id.startswith("g_") and g[0].why == "你点了看不懂"
    assert g[0].summary.startswith("把「矩阵乘法」拆成了几个小点") and g[0].can_undo
    r = client.post(f"/wiki/rsi/patches/{g[0].patch_id}/undo", headers=H("u1"))
    assert r.status_code == 200, r.text
    assert ChangeOut.model_validate(r.json()).status == "rolled_back"
    assert repo.load("u1").state.graph.children(MM) == ()
    assert client.post(f"/wiki/rsi/patches/{g[0].patch_id}/undo",
                       headers=H("u1")).status_code == 409
    assert client.post("/wiki/rsi/patches/g_nothing/undo", headers=H("u1")).status_code == 404


def test_split_without_a_body_and_its_errors(seeded):
    client, services, repo = seeded
    assert client.post(f"/wiki/nodes/{MM}/split", headers=H("u1")).status_code == 503
    one = parts("只有一个")
    s = Script().push(SPLIT, answer(), one, one)
    services.gateway = s.gateway()
    r = client.post(f"/wiki/nodes/{LA}/split", headers=H("u1"))       # a node with children
    assert r.status_code == 200, r.text
    n = len(repo.load("u1").state.graph.nodes)
    r = client.post(f"/wiki/nodes/{DET}/split", headers=H("u1"))     # unusable twice
    assert r.status_code == 502 and s.calls(SPLIT) == 3
    assert len(repo.load("u1").state.graph.nodes) == n
    assert client.post(f"/wiki/nodes/{DET}/split", headers=H("u1"),
                       json={"reason": "user"}).status_code == 422
    assert client.post("/wiki/nodes/nd_nothing/split", headers=H("u1")).status_code == 404


def test_split_left_whole_says_why_and_marks_the_node_atomic(seeded):
    client, services, repo = seeded
    before = repo.load("u1").state.graph
    s = Script().push(SPLIT, keep("它已经是一件事，换个例子讲会更清楚"))
    services.gateway = s.gateway()
    r = client.post(f"/wiki/nodes/{DET}/split", headers=H("u1"), json={"reason": "requested"})
    assert r.status_code == 200, r.text
    out = SplitOut.model_validate(r.json())
    assert out.children == [] and out.kept == "它已经是一件事，换个例子讲会更清楚"
    assert s.requests(SPLIT)[0].params.get("source") == "continue"
    st = repo.load("u1").state
    assert st.graph.nodes == before.nodes and st.graph.relations == before.relations
    assert st.names[DET].atomic is True
    rows = [ChangeOut.model_validate(x) for x in
            client.get("/wiki/rsi/changes", headers=H("u1")).json()]
    assert [x for x in rows if x.track == "G"] == []                 # no graph change


def test_a_migrated_users_change_list_has_no_migration_row_but_shows_a_split(services, client):
    from evowiki.nodes import GraphChange
    signup(client, "u1")
    repo = PgRepo(services.pool)
    snap = repo.load("u1")
    at = services.clock() - timedelta(days=1)
    mig = GraphChange(change_id="g_migrate_1", at=at, kind="migrate",
                      summary="转换了 6 个节点", why="旧的「领域 + 知识点」两层结构转成统一节点图")
    aspect, wiki = graph_events(snap.state, snap.k, graph().updated(changes=[mig]), "migrated",
                                now=at, new_id=services.new_id,
                                aspects={MM: (Aspect.TERM, Aspect.PROCEDURE)})
    repo.commit(snap, Commit(aspect=list(aspect), wiki=wiki))
    services.change_sources = graph_change_sources(services.pool, services.new_id)

    def g_rows() -> list[ChangeOut]:
        rows = [ChangeOut.model_validate(x) for x in
                client.get("/wiki/rsi/changes", headers=H("u1")).json()]
        return [x for x in rows if x.track == "G"]

    assert g_rows() == []
    assert client.post("/wiki/rsi/patches/g_migrate_1/undo", headers=H("u1")).status_code == 409
    services.gateway = Script().push(SPLIT, answer()).gateway()
    assert client.post(f"/wiki/nodes/{MM}/split", headers=H("u1"), json={}).status_code == 200
    [row] = g_rows()
    assert row.summary.startswith("把「矩阵乘法」拆成了") and row.patch_id != "g_migrate_1"
    assert repo.load("u1").state.graph.changes[0].change_id == "g_migrate_1"
