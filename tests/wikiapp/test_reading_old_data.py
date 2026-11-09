"""Old 读懂 goals still work (reading-mode spec §7 item 3, §8.1 「旧数据」「迁移」; plan stage 4
Task 6). A goal the old route and S1 made is built here by hand, the way they stored it: the
reading block a region whose members are its sections as concept nodes (「《T》§2 方法」), a
`reads` source_link per section, a page on a section with a verified check, the learner's K
evidence on that section's cells. Then, with the new code: the state replays; the map, the path
and the page open through the normal read functions; rewriting the page still gives the writer
the reading reference (the old `reads` rule, `deep.agent.is_reading`); the review card is still
due; K evidence is unchanged; the `reads` rows are still there; and a replay from an emptied
wiki_snapshot gives the same state with an old and a new 读懂 goal side by side. Postgres
(PgRepo, the real source store); scripted LLM only (the opening probe)."""

from dataclasses import replace
from datetime import timedelta

import pytest

from evowiki.belief.aspects import Observe
from evowiki.deep import agent as deep_agent
from evowiki.goals.blocks import Block
from evowiki.goals.contracts import (
    CheckItem,
    Observation,
    OutlineSection,
    PageOutline,
    SectionRole,
)
from evowiki.goals.fold import Page, states_for
from evowiki.goals.guardrails import PageConcept
from evowiki.goals.path import LearningPath, PageStatus, PathPage, goal_key
from evowiki.sources import store as source_store
from evowiki.sources.excerpts import PooledSourceExcerpts
from evowiki.sources.models import LinkRow
from evowiki.store.db import fetch_all
from evowiki.store.wiki import wiki_events, wiki_snapshot
from evowiki.wikiapp.events import GoalMapSaved, PageSaved, PathRecord, PathSaved, WikiEvent
from evowiki.wikiapp.goal import create_goal_from_blocks, page_events_for
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.maps import map_out
from evowiki.wikiapp.page import open_page_flow
from evowiki.wikiapp.paths import path_out
from evowiki.wikiapp.reading import create_reading_goal
from evowiki.wikiapp.refine_apply import refine_input, with_sources
from evowiki.wikiapp.refine_spec import WikiSpecSource
from evowiki.wikiapp.repo import Commit, PgRepo, _from_row
from evowiki.wikiapp.review import review_today
from evowiki.wikiapp.state import replay
from tests.runtime.helpers import make_user
from tests.sources.test_store import _sec, _src
from tests.wikiapp.goal_script import auto_probe
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_reading_unit import PLAN, PREREQS, SECS, SRC, TITLE, TOC

OLD_TITLE = "旧论文"
OLD_GOAL = f"读懂《{OLD_TITLE}》"
LEAVES = (f"《{OLD_TITLE}》§1 引言", f"《{OLD_TITLE}》§2 方法")
OLD_SRC = replace(_src("p0", status="ready"), title=OLD_TITLE)
OLD_SECS = [replace(_sec("o1", 0, source="p0", text="〔p.1〕为什么要做这件事。"), title="1 引言",
                    page_from=1, page_to=1),
            replace(_sec("o2", 1, source="p0", text="〔p.2〕方法是这样的：先算分数再归一化。"),
                    title="2 方法", page_from=2, page_to=2)]
LATER = NOW + timedelta(days=63)
LEARN_EVENTS = 3


@pytest.fixture
def env(pool):
    make_user(pool, "u1")
    s = Script()
    auto_probe(s)
    with pool.connection() as conn:
        for src, secs in ((OLD_SRC, OLD_SECS), (SRC, SECS)):
            source_store.insert_source(conn, src, b"x", "application/pdf")
            source_store.replace_sections(conn, "u1", src.source_id, list(secs))
    return PgRepo(pool), s, Ids()


def _commit(repo, ids, *bodies, aspect=()):
    snap = repo.load("u1")
    repo.commit(snap, Commit(aspect=list(aspect), wiki=[
        WikiEvent(event_id=ids("wev"), ts=NOW, body=b) for b in bodies]))


def old_goal(env, pool) -> dict:
    """The old 读懂 goal (module doc). Returns its ids."""
    repo, s, ids = env
    blocks = [Block(name="矩阵乘法", summary="两个矩阵怎么乘", atomic=True),
              Block(name=OLD_GOAL, summary="按原文一节一节读"),
              *(Block(name=n, summary=f"读{n}", atomic=True) for n in LEAVES)]
    res = create_goal_from_blocks(repo, CountingGateway(s.gateway()), "u1", OLD_GOAL, blocks,
                                  now=NOW, new_id=ids, title=OLD_TITLE)
    state = repo.load("u1").state
    m = state.maps[res.draft.map_id]
    by_name = {n.name: n for n in m.nodes}
    block, leaves = by_name[OLD_GOAL], [by_name[n].id for n in LEAVES]
    # the old shape: the reading block's sections are its members, concept nodes under it
    path_id = "path-old-reading"
    nodes = tuple(
        n.model_copy(update={"members": tuple(leaves), "state": "expanded", "path_id": path_id})
        if n.id == block.id else
        n.model_copy(update={"kind": "concept", "parent": block.id, "state": "expanded"})
        if n.id in leaves else n
        for n in m.nodes)
    old_map = m.model_copy(update={"nodes": nodes, "sources": ("p0",)})
    _commit(repo, ids, GoalMapSaved(record=old_map, action="expanded"))
    # its sections' `reads` links (the old route wrote them; nothing writes them any more)
    with pool.connection() as conn:
        source_store.replace_links(conn, "u1", leaves, [
            LinkRow(leaf, sec, "p0", "reads", "intake") for leaf, sec in zip(leaves, ("o1", "o2"))],
            by="intake", now=NOW)
    # the page of §2 on the region's path, organized: an explain and a verified check on its cell
    snap = repo.load("u1")
    leaf = leaves[1]
    cells = [f"{leaf}.{a}" for a in snap.kview(NOW).aspects(leaf)]
    rec, evs = page_events_for(snap.state, snap.k, title=LEAVES[1], goal=f"{OLD_GOAL} · {OLD_GOAL}",
                               concepts=(leaf,), targets=(leaf,), now=NOW, new_id=ids)
    o = PageOutline(page_id=rec.page_id, goal=rec.goal, concepts=rec.concepts, targets=rec.targets,
                    sections=(
                        OutlineSection(id="s1", heading="原文怎么说", role=SectionRole.EXPLAIN,
                                       cells=(cells[0],)),
                        OutlineSection(id="s2", heading="确认", role=SectionRole.CHECK,
                                       cells=(cells[0],), check=CheckItem(
                                           cell=cells[0], stem="方法第一步是？",
                                           options=("先算分数", "先归一化", "不知道"), answer=0,
                                           verified=True))))
    page = Page(outline=o, states=states_for(o), texts={}, snapshot={})
    pages = tuple(PathPage(page_id=f"pp{i}", targets=(c,), concepts=(c,), objective=f"读{n}",
                           new_cells=1, title=n,
                           status=PageStatus.LEARNING if c == leaf else PageStatus.NOT_STARTED)
                  for i, (c, n) in enumerate(zip(leaves, LEAVES)))
    lp = LearningPath(path_id=path_id, goal=f"{OLD_GOAL} · {OLD_GOAL}",
                      goal_key=goal_key(OLD_GOAL, ()), split=True, source="llm", new_cells=2,
                      concepts=tuple(PageConcept(id=c, name=n) for c, n in zip(leaves, LEAVES)),
                      pages=pages)
    path = PathRecord(path=lp, page_ids=(None, rec.page_id), created_at=NOW, sources=("p0",))
    repo.commit(snap, Commit(wiki=[*evs, *(
        WikiEvent(event_id=ids("wev"), ts=NOW, body=b) for b in (
            PageSaved(record=rec.model_copy(update={"page": page}), action="organized"),
            PathSaved(record=path, action="page_started")))]))
    # his K evidence on that section's cells (learned at NOW: fading by LATER)
    for i in range(LEARN_EVENTS):
        _commit(repo, ids, aspect=[Observe(event_id=ids("aev"), ts=NOW, observation=Observation(
            cell=c, polarity="+", strength=1.0, kind="quiz", evidence_ref=f"quiz:old{i}"))
            for c in cells])
    return {"map_id": m.map_id, "block": block.id, "leaves": leaves, "page_id": rec.page_id,
            "path_id": path_id, "cells": cells}


def _beliefs(repo, cells, at):
    kv = repo.load("u1").kview(at)
    return {c: kv.cell(c) for c in cells}


def _full_replay(pool):
    with pool.connection() as conn:
        return replay(_from_row(e.event_id, e.occurred_at, e.payload)
                      for e in wiki_events(conn, "u1"))


def test_an_old_reading_goal_still_opens_rewrites_reviews_and_keeps_its_links(env, pool):
    repo, _, ids = env
    g = old_goal(env, pool)
    k_before = _beliefs(repo, g["cells"], NOW)
    state = repo.load("u1").state
    assert state.readings == {} and state.reading_of(g["map_id"]) is None   # no Reading for it

    # the map: the reading block with its sections as concept nodes under it
    snap = repo.load("u1")
    mo = map_out(snap.state, snap.k, g["map_id"], NOW, user_id="u1")
    dumped = mo.model_dump_json()
    assert all(n in dumped for n in LEAVES) and OLD_GOAL in dumped
    # the path and the page
    po = path_out(snap.state, snap.k, g["path_id"], NOW, user_id="u1")
    assert po.path_id == g["path_id"] and len(po.pages) == 2
    assert [p.page_id for p in po.pages] == [None, g["page_id"]]
    page = open_page_flow(repo, None, "u1", g["page_id"], now=NOW, new_id=ids)
    assert page.page_id == g["page_id"] and [s.id for s in page.sections] == ["s1", "s2"]

    # rewriting it: the writer gets the section's text and the reading reference (old `reads`)
    snap = repo.load("u1")
    rec = snap.state.pages[g["page_id"]]
    assert rec.kind == "learn" and rec.unit_id is None
    inp = with_sources(refine_input(snap, g["page_id"], NOW, WikiSpecSource(repo)),
                       rec.concepts, PooledSourceExcerpts(pool))
    assert [(e.section_id, e.role) for e in inp.sources] == [("o2", "reads")]
    assert inp.page_kind == "learn" and deep_agent.is_reading(inp)
    brief = deep_agent.page_brief(inp)
    assert "这一页是带读页" in brief and "先算分数再归一化" in brief

    # the review card is still due, on the page's check
    due = review_today(repo, "u1", now=LATER)
    assert any(i.page_id == g["page_id"] and i.cell == g["cells"][0] for i in due.items)
    # K evidence unchanged by all of the above
    assert _beliefs(repo, g["cells"], NOW) == k_before
    # the reads rows are still there
    with pool.connection() as conn:
        rows = fetch_all(conn, "select node_id, section_id, role from source_link "
                               "where user_id = 'u1' order by section_id")
    assert [(r["section_id"], r["role"]) for r in rows] == [("o1", "reads"), ("o2", "reads")]


def test_replay_from_an_emptied_snapshot_with_an_old_and_a_new_reading_goal(env, pool, admin_pool):
    repo, s, ids = env
    g = old_goal(env, pool)
    new = create_reading_goal(repo, CountingGateway(s.gateway()), "u1", f"读懂《{TITLE}》",
                              PREREQS, PLAN, now=NOW, new_id=ids, source=TOC, title=TITLE)
    new_map = new.map_id or new.draft.map_id
    loaded = repo.load("u1").state
    assert g["map_id"] in loaded.maps and new_map in loaded.maps
    assert loaded.reading_of(new_map) is not None and loaded.reading_of(g["map_id"]) is None
    with admin_pool.connection() as conn:
        conn.execute("delete from wiki_snapshot where user_id = 'u1'")
    with pool.connection() as conn:
        assert wiki_snapshot(conn, "u1") is None
    again = repo.load("u1").state
    assert again.to_json() == _full_replay(pool).to_json() == loaded.to_json()
    snap = repo.load("u1")
    for map_id in (g["map_id"], new_map):
        map_out(snap.state, snap.k, map_id, NOW, user_id="u1")       # both open
    assert again.pages[g["page_id"]].page is not None
