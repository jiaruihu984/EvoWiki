"""Materials task 7: a page lists the materials it was written from, the graph's path rows carry §
labels and each goal says which parts of its materials nothing covers. Scripted LLM only."""

import json
from dataclasses import replace

import pytest

from evowiki.sources import store
from evowiki.sources.excerpts import PooledSourceExcerpts
from evowiki.sources.models import LinkRow
from evowiki.wikiapp.repo import Commit, PgRepo
from evowiki.wikiapp.wiki_schemas import GraphOut, PageOut
from tests.service.conftest import H, signup
from tests.service.test_goal_sources_api import LEARN, add_source
from tests.wikiapp.goal_script import auto_probe, chat_goal, push_no_question, push_pages
from tests.wikiapp.script import Script
from tests.wikiapp.test_organize import OUTLINE, script_page
from tests.wikiapp.test_path_edits import seed_events
from tests.wikiapp.verify import verify_prereqs

BOOK = ("# 前言\n\n写给读者。\n\n# 1 向量\n\n向量是有方向的量。\n\n## 1.1 向量加法\n\n首尾相接。\n\n"
        "## 1.2 数乘\n\n伸缩。\n\n# 习题\n\n1. 算一算。\n")


@pytest.fixture
def env(services, client):
    s = Script()
    auto_probe(s)
    services.gateway = s.gateway()
    services.page_writing = replace(services.page_writing,
                                    sources=PooledSourceExcerpts(services.pool))
    signup(client, "u1")
    return client, s, services


def make_goal(env, with_source=True):
    """A goal learned from a material; returns (source_id | None, path_id)."""
    client, s, _ = env
    sid = add_source(client, BOOK) if with_source else None
    s.push("goals.intake", LEARN)
    out = chat_goal(client, "u1", "学向量", [sid] if sid else [], use="follow")
    # entering a block: its parts (one layer), then its pages
    s.push("goals.intake", {"title": "向量加法", "blocks": [
        {"name": "首尾相接", "summary": "两个向量怎么加", "page": True},
        {"name": "平行四边形法则", "summary": "另一种加法的画法", "page": True}]})
    push_no_question(s)
    push_pages(s, ["c3"], ["c4"])
    if out["draft_id"] is not None:          # S1's map: its opening round, skipped
        r = client.post(f"/wiki/goals/{out['draft_id']}/answers", headers=H("u1"),
                        json={"skip": True})
        assert r.status_code == 201, r.text
        out = r.json()
    if out["path_id"] is None:
        m = out["map"]
        block = next(n["id"] for n in m["nodes"] if n["kind"] == "region")
        r = client.post(f"/wiki/maps/{m['map_id']}/regions/{block}/enter", headers=H("u1"))
        assert r.status_code == 201, r.text        # spec 2026-10-28: entering only splits
        # opening the second part (the first one its prerequisite) plans the path over both
        svc = env[2]
        verify_prereqs(PgRepo(svc.pool), "u1", [("c3", "c4")], now=svc.clock(),
                       new_id=svc.new_id)
        r = client.post(f"/wiki/maps/{m['map_id']}/regions/c4/enter", headers=H("u1"))
        assert r.status_code == 201, r.text
    return sid, r.json()["path_id"]


PAGE1 = json.loads(json.dumps(OUTLINE).replace("c2.term", "c3.term").replace(
    "c1.procedure", "c3.term").replace("c1.interpret", "c3.term"))   # page 1 teaches c3 only


def first_page(env, path_id) -> str:
    client, s, _ = env
    r = client.post(f"/wiki/paths/{path_id}/next", headers=H("u1"))
    assert r.status_code == 200, r.text
    pid = r.json()["page_id"]
    script_page(s, PAGE1)
    r = client.post(f"/wiki/pages/{pid}/organize", headers=H("u1"))
    assert r.status_code == 200 and "event: done" in r.text, r.text
    return pid


def page(client, pid) -> PageOut:
    r = client.post(f"/wiki/pages/{pid}/open", headers=H("u1"))
    assert r.status_code == 200, r.text
    return PageOut.model_validate(r.json())


def graph(client) -> GraphOut:
    r = client.get("/wiki/graph", headers=H("u1"))
    assert r.status_code == 200, r.text
    return GraphOut.model_validate(r.json())


def test_a_page_lists_its_materials_through_rename_and_delete(env):
    client, _, svc = env
    sid, path = make_goal(env)
    # S1's blocks carry no section links (stage 2 brings them back on entry): the page's point
    # teaches §1.1, written here as intake would
    with svc.pool.connection() as conn:
        secs = {x.title: x for x in store.sections(conn, "u1", sid)}
        store.replace_links(conn, "u1", ["c3"], [
            LinkRow("c3", secs["1.1 向量加法"].section_id, sid, "teaches", "intake")],
            by="intake", now=svc.clock())
    pid = first_page(env, path)
    out = page(client, pid)
    assert [x.model_dump() for x in out.sources] == [
        {"id": sid, "title": "线性代数讲义", "current_title": "线性代数讲义", "kind": "md",
         "origin": None, "deleted": False}]
    assert client.patch(f"/wiki/sources/{sid}", headers=H("u1"),
                        json={"title": "新名字"}).status_code == 200
    (src,) = page(client, pid).sources
    assert (src.title, src.current_title, src.deleted) == ("线性代数讲义", "新名字", False)
    assert client.delete(f"/wiki/sources/{sid}", headers=H("u1")).status_code == 204
    out = page(client, pid)
    assert out.organized and out.sources[0].deleted is True


def seed_goal(env, *, with_source=True):
    """A goal map with a 3-page path (n1 | n2 | n3, n4); with a material: n1 teaches §1.1, n2 §1.2
    (the 前言 and 习题 sections nothing covers; 1 向量 is covered through its children)."""
    client, _, svc = env
    sid = add_source(client, BOOK) if with_source else None
    aspect, wiki = seed_events(svc.new_id)
    if sid:
        for i, ev in enumerate(wiki):
            rec = getattr(ev.body, "record", None)
            if rec is not None and hasattr(rec, "sources"):
                body = ev.body.model_copy(update={"record": rec.model_copy(
                    update={"sources": (sid,)})})
                wiki[i] = ev.model_copy(update={"body": body})
    repo = PgRepo(svc.pool)
    repo.commit(repo.load("u1"), Commit(aspect=aspect, wiki=wiki))
    if sid:
        with svc.pool.connection() as conn:
            secs = {s.title: s for s in store.sections(conn, "u1", sid)}
            store.replace_links(conn, "u1", ["n1", "n2"], [
                LinkRow("n1", secs["1.1 向量加法"].section_id, sid, "teaches", "intake"),
                LinkRow("n2", secs["1.2 数乘"].section_id, sid, "teaches", "intake")],
                by="intake", now=svc.clock())
    return sid


def test_path_rows_carry_section_labels_and_goals_their_coverage(env):
    client, _, _ = env
    sid = seed_goal(env)
    (goal,) = graph(client).goals
    assert [it.sections for it in goal.path] == [["§1.1"], ["§1.2"], []]
    (src,) = goal.sources
    assert (src.id, src.title, src.deleted) == (sid, "线性代数讲义", False)
    assert src.uncovered == ["前言", "习题"]
    assert client.delete(f"/wiki/sources/{sid}", headers=H("u1")).status_code == 204
    (goal,) = graph(client).goals
    (src,) = goal.sources
    assert src.deleted is True and src.uncovered == []
    assert [it.sections for it in goal.path] == [[], [], []]


def test_a_goal_without_materials_is_unchanged(env):
    client, _, _ = env
    seed_goal(env, with_source=False)
    (goal,) = graph(client).goals
    assert goal.sources == [] and all(it.sections == [] for it in goal.path)


def test_a_page_of_a_goal_without_materials_lists_none(env):
    client, _, _ = env
    _, path = make_goal(env, with_source=False)
    pid = first_page(env, path)
    assert page(client, pid).sources == []


def test_path_edit_and_node_action_rows_carry_section_labels_too(env):
    client, _, _ = env
    seed_goal(env)
    (goal,) = graph(client).goals
    r = client.post(f"/wiki/paths/{goal.path_id}/edits", headers=H("u1"),
                    json={"op": "move", "from": 0, "to": 1})
    assert r.status_code == 200, r.text
    assert [it["sections"] for it in r.json()["path"]] == [["§1.2"], ["§1.1"], []]
