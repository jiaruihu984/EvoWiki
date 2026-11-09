"""/wiki big-goal routes over a map whose regions have their members (an old map's: the goal made
by tests.wikiapp.flat_goal.region_goal): …/answers → next round or the
map, …/stop, GET /wiki/maps/{id}, POST /wiki/maps/{id}/regions/{rid}/enter, catalog map cards (a
goal's regions are graph nodes). Scripted LLM only."""

import pytest

from evowiki.wikiapp.llm import require
from evowiki.wikiapp.repo import PgRepo
from evowiki.wikiapp.wiki_schemas import CatalogOut, GoalDraftOut, GoalMapOut, MapEditOut, PathOut
from tests.service.conftest import H, signup
from tests.wikiapp.flat_goal import region_goal
from tests.wikiapp.goal_script import auto_probe, push_no_question, push_pages, push_round
from tests.wikiapp.script import Script
from tests.wikiapp.test_big_goal import BACKGROUND, GOAL, LA, ML, PR, SELFS


@pytest.fixture
def wiki(services, client):
    script = Script()
    auto_probe(script)            # the probe model writes whatever item the diagnosis picked
    services.gateway = script.gateway()
    signup(client, "u1")
    return client, script


def post(client, url, body=None):
    return client.post(url, headers=H("u1"), json=body)


def first_round(wiki) -> GoalDraftOut:
    """A map whose regions have their members (the eager map: tests.wikiapp.flat_goal.region_goal,
    as stored maps and the 读懂 reading block have them) and its first round, read back through
    GET /wiki/goals/{draft_id}; the routes below act on it."""
    client, s = wiki
    s.push("goals.intake", ML)
    push_round(s, selfs=SELFS)
    svc = client.app.state.services
    res = region_goal(PgRepo(svc.pool), require(svc.gateway, "u1"), "u1", GOAL, BACKGROUND,
                      s.pop_mapper(), now=svc.clock(), new_id=svc.new_id)
    r = client.get(f"/wiki/goals/{res.draft.draft_id}", headers=H("u1"))
    assert r.status_code == 200, r.text
    return GoalDraftOut.model_validate(r.json())


def test_goal_answers_rounds_and_map(wiki):
    client, _ = wiki
    out = first_round(wiki)
    assert out.draft_id and out.round == 1 and out.map is not None and out.map_id is None
    assert out.questions == []                 # the pick is asked in the chat (ask_check)
    region = next(n for n in out.map.nodes if n.id == LA)
    assert region.members == ["c2", "c3"] and region.state == "collapsed" and not region.reused
    assert {n.id for n in out.map.nodes if n.kind == "concept"} >= {"c2", "c3"}
    bad = post(client, f"/wiki/goals/{out.draft_id}/answers", {"answers": [0]})
    assert bad.status_code == 422                      # answered in the chat, not here

    # what ask_check records (the scripted assistant's questions; key 「对」): 线性代数 and its
    # members right, everything else wrong
    from evowiki.wikiapp.llm import require
    from evowiki.wikiapp.repo import PgRepo
    from tests.wikiapp.goal_script import answer_round, question_of
    svc = client.app.state.services
    repo = PgRepo(svc.pool)
    res = None
    while True:
        d = repo.load("u1").state.drafts[out.draft_id]
        r = d.rounds[-1]
        if r.done or out.draft_id in repo.load("u1").state.closed_drafts:
            break
        q = question_of(r)
        right = q.node in (LA, "c2", "c3")
        a = (q.means.index("know") if right else q.means.index("dont")) if q.kind == "self_report" \
            else (q.answer if right else (q.answer + 1) % len(q.options))
        res = answer_round(repo, require(svc.gateway, "u1"), "u1", out.draft_id, [a],
                           now=svc.clock(), new_id=svc.new_id)
        if not res.open:
            break
    r3 = GoalDraftOut.model_validate(post(client, f"/wiki/goals/{out.draft_id}/answers",
                                          {"skip": True}).json()) if res is None or res.open \
        else GoalDraftOut.model_validate(client.get(f"/wiki/goals/{out.draft_id}",
                                                    headers=H("u1")).json())
    assert r3.map_id is not None and r3.draft_id is None
    assert {d.node_id: d.decision for d in r3.decisions}[LA] == "known"

    gm = GoalMapOut.model_validate(client.get(f"/wiki/maps/{r3.map_id}", headers=H("u1")).json())
    # known is K's alone: 线性代数's purpose and members rest on the self-report (shaky)
    assert {n.id: n.state for n in gm.nodes}[LA] == "collapsed" and gm.next_region == LA
    cat = CatalogOut.model_validate(client.get("/wiki", headers=H("u1")).json())
    assert [(c.map_id, c.regions, c.known) for c in cat.maps] == [(r3.map_id, 4, 0)]
    assert client.get("/wiki/maps/map-nope", headers=H("u1")).status_code == 404


def test_stop_then_enter_a_region_and_get_its_path(wiki):
    client, s = wiki
    out = first_round(wiki)
    r = post(client, f"/wiki/goals/{out.draft_id}/stop")
    assert r.status_code == 201, r.text
    stopped = GoalDraftOut.model_validate(r.json())
    assert stopped.map_id == out.map.map_id
    assert post(client, f"/wiki/goals/{out.draft_id}/stop").status_code == 409

    push_no_question(s)                                    # no usable item inside the region
    push_pages(s, ["c5"], ["c6"])
    r = post(client, f"/wiki/maps/{stopped.map_id}/regions/{PR}/enter")
    assert r.status_code == 201, r.text
    entered = GoalDraftOut.model_validate(r.json())
    assert entered.path_id is not None and entered.map is not None
    region = next(n for n in entered.map.nodes if n.id == PR)
    assert region.state == "expanded" and region.path_id == entered.path_id
    path = PathOut.model_validate(client.get(f"/wiki/paths/{entered.path_id}",
                                             headers=H("u1")).json())
    assert len(path.pages) == 2 and path.next_index == 1
    assert post(client, f"/wiki/maps/{stopped.map_id}/regions/{PR}/enter").status_code == 409
    assert post(client, f"/wiki/maps/{stopped.map_id}/regions/zz/enter").status_code == 404


def test_enter_without_gateway_is_503(wiki, services):
    client, _ = wiki
    out = first_round(wiki)
    post(client, f"/wiki/goals/{out.draft_id}/stop")
    services.gateway = None
    assert post(client, f"/wiki/maps/{out.map.map_id}/regions/{PR}/enter").status_code == 503


def test_get_goal_draft_open_then_finished(wiki):
    """GET /wiki/goals/{draft_id}: the open round (the learn view after a chat confirm), then what
    it finished as; 404 for an unknown draft."""
    client, _ = wiki
    out = first_round(wiki)
    r = client.get(f"/wiki/goals/{out.draft_id}", headers=H("u1"))
    assert r.status_code == 200, r.text
    got = GoalDraftOut.model_validate(r.json())
    assert got.draft_id == out.draft_id and got.round == 1 and got.questions == out.questions
    assert got.map is not None
    assert post(client, f"/wiki/goals/{out.draft_id}/stop").status_code == 201
    done = GoalDraftOut.model_validate(
        client.get(f"/wiki/goals/{out.draft_id}", headers=H("u1")).json())
    assert done.draft_id is None and done.map_id == out.map.map_id
    assert client.get("/wiki/goals/dr-nope", headers=H("u1")).status_code == 404


def test_map_edits_and_undo(wiki):
    """POST /wiki/maps/{id}/edits: the 学习路径 card's edits, all or none, `undo` = their inverse
    ops (撤销 sends them back); 422 for an id that is not a block, 404 unknown map."""
    client, _ = wiki
    out = first_round(wiki)
    mid = out.map.map_id
    top = [n.id for n in out.map.nodes if n.kind == "region" and n.parent is None]
    r = post(client, f"/wiki/maps/{mid}/edits", {"ops": [
        {"op": "reorder", "ids": [*reversed(top)]},
        {"op": "rename", "id": LA, "name": "矩阵和向量"},
        {"op": "add", "name": "造模型", "covers": ["数据", "评估"]}]})
    assert r.status_code == 200, r.text
    edited = MapEditOut.model_validate(r.json())
    regions = [n for n in edited.map.nodes if n.kind == "region" and n.parent is None]
    assert [n.id for n in regions][:len(top)] == [*reversed(top)]
    assert {n.id: n.name for n in regions}[LA] == "矩阵和向量"
    assert regions[-1].name == "造模型" and regions[-1].scope == "数据、评估"
    undo = post(client, f"/wiki/maps/{mid}/edits",
                {"ops": [o.model_dump(mode="json") for o in edited.undo]})
    assert undo.status_code == 200, undo.text
    back = MapEditOut.model_validate(undo.json())
    assert [n.id for n in back.map.nodes if n.kind == "region"] == top
    assert {n.id: n.name for n in back.map.nodes}[LA] == "线性代数"
    bad = post(client, f"/wiki/maps/{mid}/edits", {"ops": [{"op": "drop", "id": "c2"}]})
    assert bad.status_code == 422 and bad.json()["detail"] == "这张学习地图上没有这一块"
    assert post(client, "/wiki/maps/map-nope/edits",
                {"ops": [{"op": "drop", "id": LA}]}).status_code == 404


def test_blocks_entered_one_level_then_a_point_opened_over_http(wiki, services):
    """Spec 2026-10-28 §2.2 over the routes: entering a block shows its parts (`map_id`, each
    node with its computed `atomic` / `enter`, nothing planned); opening a point returns its
    path, hung on that point; 继续拆细 (POST /wiki/nodes/{id}/split with map_id) adds the
    point's parts to that map only."""
    from evowiki.goals.blocks import Block
    from evowiki.wikiapp.goal import create_goal_from_blocks, stop_draft
    from evowiki.wikiapp.llm import CountingGateway
    from evowiki.wikiapp.repo import PgRepo

    client, s = wiki
    repo, ids, now = PgRepo(services.pool), services.new_id, services.clock()
    res = create_goal_from_blocks(repo, CountingGateway(services.gateway), "u1", "我想养黑壳虾",
                                  (Block(name="认识黑壳虾", covers=("外形",)),
                                       Block(name="把水养好", covers=("换水", "pH"), target=True)),
                                  now=now, new_id=ids)
    stop_draft(repo, CountingGateway(services.gateway), "u1", res.draft.draft_id, now=now,
               new_id=ids)
    map_id = res.draft.map_id
    s.push("goals.intake", {"parts": [{"name": "换水", "atomic": True},
                                      {"name": "pH", "atomic": False}]})
    r = post(client, f"/wiki/maps/{map_id}/regions/c2/enter")
    assert r.status_code == 201, r.text
    out = GoalDraftOut.model_validate(r.json())
    assert out.map_id == map_id and out.path_id is None and out.draft_id is None and out.map
    nodes = {n.id: n for n in out.map.nodes}
    assert [(nodes[c].enter, nodes[c].atomic) for c in ("c2", "c3", "c4")] == [
        ("children", False), ("learn", True), ("split", False)]
    push_no_question(s)
    r = post(client, f"/wiki/maps/{map_id}/regions/c3/enter")
    assert r.status_code == 201, r.text
    opened = GoalDraftOut.model_validate(r.json())
    m = GoalMapOut.model_validate(client.get(f"/wiki/maps/{map_id}", headers=H("u1")).json())
    assert opened.path_id and next(n for n in m.nodes if n.id == "c3").path_id == opened.path_id
    s.push("goals.enter", {"parts": [{"name": "换水频率", "summary": "多久换一次"},
                                     {"name": "换水比例", "summary": "一次换多少"}]})
    r = post(client, "/wiki/nodes/c3/split", {"map_id": map_id, "reason": "requested"})
    assert r.status_code == 200, r.text
    m = GoalMapOut.model_validate(client.get(f"/wiki/maps/{map_id}", headers=H("u1")).json())
    kids = [n for n in m.nodes if n.parent == "c3"]
    assert [n.name for n in kids] == ["换水频率", "换水比例"]
    assert next(n for n in m.nodes if n.id == "c3").enter == "children"
    [req] = [q for q in s.requests("goals.enter") if q.params.get("source") == "continue"]
    assert "要求把这个点拆细" in req.prompt
