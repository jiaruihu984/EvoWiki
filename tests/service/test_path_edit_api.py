"""/wiki/paths/{id}/edits, /wiki/paths/{id}/replan, /wiki/nodes/{id}/want (plan T7)
over HTTP: wire shapes (plan §4), status codes, one commit per action."""

import pytest

from evowiki.wikiapp.repo import Commit, PgRepo
from evowiki.wikiapp.wiki_schemas import PathEditOut
from tests.service.conftest import H, signup
from tests.wikiapp.script import Script
from tests.wikiapp.split_answers import PURPOSE as SPLIT
from tests.wikiapp.split_answers import keep
from tests.wikiapp.test_path_edits import MAP_ID, PATH_ID, seed_events


@pytest.fixture
def seeded(services, client):
    signup(client, "u1")
    repo = PgRepo(services.pool)
    aspect, wiki = seed_events(services.new_id)
    repo.commit(repo.load("u1"), Commit(aspect=aspect, wiki=wiki))
    return client, services


def post(client, url, body=None):
    return client.post(url, headers=H("u1"), json=body if body is not None else {})


def test_edits_move_remove_add(seeded):
    client, _ = seeded
    r = post(client, f"/wiki/paths/{PATH_ID}/edits", {"op": "move", "from": 2, "to": 0})
    assert r.status_code == 200, r.text
    out = PathEditOut.model_validate(r.json())
    assert [i.key for i in out.path] == [f"{PATH_ID}-p3", f"{PATH_ID}-p1", f"{PATH_ID}-p2"]
    assert set(r.json()["path"][0]) == {"key", "title", "summary", "question", "node_ids", "status",
                                        "skipped", "mine", "page_id", "trashed", "skippable",
                                        "opened", "sections", "exam", "state", "progress"}
    r = post(client, f"/wiki/paths/{PATH_ID}/edits", {"op": "remove", "page_key": f"{PATH_ID}-p1"})
    assert [i["key"] for i in r.json()["path"]] == [f"{PATH_ID}-p3", f"{PATH_ID}-p2"]
    r = post(client, f"/wiki/paths/{PATH_ID}/edits", {"op": "add", "request_id": "api-test-1", "title": "贝叶斯",
                                                      "node_id": "n5"})
    assert r.status_code == 200 and r.json()["path"][-1]["mine"] is True
    r = post(client, f"/wiki/paths/{PATH_ID}/edits", {"op": "add", "request_id": "api-test-2", "title": "读一篇综述"})
    assert len(r.json()["path"][-1]["node_ids"]) == 1               # a node of its own (D34)
    got = client.get(f"/wiki/paths/{PATH_ID}", headers=H("u1")).json()
    assert [p["index"] for p in got["pages"]] == [3, 2, 4, 5]       # stored indices, shown order


def test_edit_errors(seeded):
    client, _ = seeded
    assert post(client, "/wiki/paths/path-nope/edits", {"op": "remove", "page_key": "missing"}) \
        .status_code == 404
    assert post(client, f"/wiki/paths/{PATH_ID}/edits", {"op": "remove", "page_key": "missing"}) \
        .status_code == 422
    assert post(client, f"/wiki/paths/{PATH_ID}/edits", {"op": "jump", "index": 0}) \
        .status_code == 422
    assert post(client, f"/wiki/paths/{PATH_ID}/edits", {"op": "add", "request_id": "api-test-3", "title": " "}) \
        .status_code == 422
    assert post(client, f"/wiki/paths/{PATH_ID}/edits", {"op": "add", "request_id": "api-test-4", "title": "x",
                                                         "node_id": "zz"}).status_code == 404


def test_want(seeded):
    client, _ = seeded
    r = post(client, "/wiki/nodes/n5/want", {"map_id": MAP_ID})
    assert r.status_code == 200 and r.json()["path"][-1]["node_ids"] == ["n5"]
    assert post(client, "/wiki/nodes/zz/want", {"map_id": MAP_ID}).status_code == 404
    assert post(client, "/wiki/nodes/n5/want", {}).status_code == 422          # map_id required


def test_replan_needs_a_gateway_and_keeps_edits(seeded):
    client, services = seeded
    assert post(client, f"/wiki/paths/{PATH_ID}/replan").status_code == 503
    post(client, f"/wiki/paths/{PATH_ID}/edits", {"op": "remove", "page_key": f"{PATH_ID}-p2"})
    s = Script()
    services.gateway = s.gateway()
    s.push("goals.path", {"pages": [
        {"concepts": ["n1"], "objective": "学完这页你能说清样本"},
        {"concepts": ["n2"], "objective": "学完这页你能说清抽样分布"},
        {"concepts": ["n3", "n4"], "objective": "学完这页你能算区间、读 p 值"}]})
    r = post(client, f"/wiki/paths/{PATH_ID}/replan")
    assert r.status_code == 200, r.text
    assert [i["key"] for i in r.json()["path"]] == [f"{PATH_ID}-p1", f"{PATH_ID}-p3"]
    assert post(client, "/wiki/paths/path-nope/replan").status_code == 404


SPLIT_ANSWER = {"parts": [
    {"name": "抽样的随机性", "summary": "d", "aspects": ["term"]},
    {"name": "样本均值的分布", "summary": "d", "aspects": ["term"]}],
    "recommended": [{"from": "抽样的随机性", "to": "样本均值的分布", "why": "先弄清随机"}]}


def test_add_at_index_and_split_row_with_undo(seeded):
    client, services = seeded
    r = post(client, f"/wiki/paths/{PATH_ID}/edits", {"op": "add", "request_id": "api-test-5", "title": "插入", "to_index": 1})
    assert r.status_code == 200 and r.json()["path"][1]["title"] == "插入"
    assert r.json()["undo"] is None
    assert post(client, f"/wiki/paths/{PATH_ID}/edits",
                {"op": "add", "request_id": "api-test-6", "title": "x", "to_index": 99}).status_code == 422
    row = f"/wiki/paths/{PATH_ID}/rows/{PATH_ID}-p2/split"
    assert post(client, row).status_code == 503                      # no gateway
    s = Script()
    services.gateway = s.gateway()
    s.push(SPLIT, SPLIT_ANSWER)
    r = post(client, row)
    assert r.status_code == 200, r.text
    out = PathEditOut.model_validate(r.json())
    assert [i.title for i in out.path] == ["第 1 页", "插入", "抽样的随机性", "样本均值的分布",
                                           "第 3 页"]
    assert out.undo
    several = post(client, f"/wiki/paths/{PATH_ID}/rows/{PATH_ID}-p3/split")      # n3 + n4
    assert several.status_code == 200 and several.json()["undo"] == "rows"
    assert [i["title"] for i in several.json()["path"]][-2:] == ["置信区间", "p 值"]
    r = post(client, row + "/undo", {"change_id": out.undo})
    assert r.status_code == 200 and "抽样的随机性" not in [i["title"] for i in r.json()["path"]]
    assert post(client, row + "/undo", {"change_id": out.undo}).status_code == 422


def test_graph_change_undo_restores_a_split_path_in_postgres(seeded):
    from evowiki.service.deps import graph_change_sources
    from evowiki.wikiapp.goal_groups import goal_groups

    client, services = seeded
    repo = PgRepo(services.pool)
    groups = goal_groups(repo.load("u1").state)
    services.change_sources = graph_change_sources(services.pool, services.new_id)
    services.gateway = Script().push(SPLIT, SPLIT_ANSWER).gateway()
    row = f"/wiki/paths/{PATH_ID}/rows/{PATH_ID}-p2/split"
    split = post(client, row)
    assert split.status_code == 200, split.text
    change_id = split.json()["undo"]
    kids = set(repo.load("u1").state.graph.children("n2"))
    assert kids <= set(goal_groups(repo.load("u1").state)[0].members)
    result = post(client, f"/wiki/rsi/patches/{change_id}/undo")
    assert result.status_code == 200, result.text
    done = repo.load("u1")
    assert done.state.graph.children("n2") == ()
    assert goal_groups(done.state) == groups
    path = client.get(f"/wiki/paths/{PATH_ID}", headers=H("u1")).json()
    assert [p["key"] for p in path["pages"]] == [f"{PATH_ID}-p{i}" for i in (1, 2, 3)]
    assert post(client, row + "/undo", {"change_id": change_id}).status_code == 422


def test_split_row_left_whole_keeps_the_path_and_says_why(seeded):
    client, services = seeded
    s = Script()
    services.gateway = s.gateway()
    s.push(SPLIT, keep("它已经是一件事"))
    def rows():
        return [(x["key"], x["title"], x["state"]) for x in
                client.get(f"/wiki/paths/{PATH_ID}", headers=H("u1")).json()["pages"]]
    before = rows()
    r = post(client, f"/wiki/paths/{PATH_ID}/rows/{PATH_ID}-p2/split")
    assert r.status_code == 200, r.text
    out = PathEditOut.model_validate(r.json())
    assert out.kept == "它已经是一件事" and out.undo is None
    assert [i.key for i in out.path] == [f"{PATH_ID}-p{i}" for i in (1, 2, 3)]
    assert rows() == before
    assert PgRepo(services.pool).load("u1").state.names["n2"].atomic is True


def test_unremove_puts_a_removed_row_back_where_it_was_and_refuses_others(seeded):
    client, _ = seeded
    url = f"/wiki/paths/{PATH_ID}/edits"
    before = [i["key"] for i in post(client, url, {"op": "move", "from": 2, "to": 0}).json()["path"]]
    gone = post(client, url, {"op": "remove", "page_key": f"{PATH_ID}-p1"}).json()["path"]
    assert len(gone) == 2
    r = post(client, url, {"op": "unremove", "page_key": before[1]})
    assert r.status_code == 200, r.text
    assert [i["key"] for i in r.json()["path"]] == before
    assert r.json()["path"][1]["opened"] is False
    # a row that is shown, an unknown key, a second unremove: refused, nothing stored
    for key in (before[0], "u-nope", before[1]):
        assert post(client, url, {"op": "unremove", "page_key": key}).status_code == 422
