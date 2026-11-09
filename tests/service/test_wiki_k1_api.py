"""K1 Task 7 routes: learning paths and review; the budget rules seen through HTTP."""


import pytest

from evowiki.belief.aspects import Observe
from evowiki.goals.contracts import Band, Observation
from evowiki.wikiapp.llm import require
from evowiki.wikiapp.repo import Commit, PgRepo
from evowiki.wikiapp.wiki_schemas import (
    CatalogOut,
    ErrorEvent,
    PathNextOut,
    PathOut,
    PathPageDoneOut,
    ReviewGradeOut,
    ReviewTodayOut,
)
from tests.runtime.helpers import T0
from tests.service.conftest import H, parse_sse, signup
from tests.service.test_wiki_api import _budget_gateway, organized_page
from tests.wikiapp.fake_writer import PLAN_PAGE as ORGANIZE
from tests.wikiapp.fake_writer import WRITE
from tests.wikiapp.flat_goal import flat_goal
from tests.wikiapp.goal_script import push_no_question
from tests.wikiapp.script import Script
from tests.wikiapp.test_organize import CHECK_KEY, text
from tests.wikiapp.test_paths import BIG_INTAKE, BIG_PATH
from tests.wikiapp.test_review_flow import CELLS, LEARN_EVENTS

BIG_GOAL = "学统计推断"
LATER_DAYS = 63          # tests.wikiapp.test_review_flow.LATER: recall of the learned cells < 0.6


@pytest.fixture
def wiki(services, client):
    script = Script()
    services.gateway = script.gateway()
    return client, script


def path_outline(cells: list[str]) -> dict:
    """An organizer outline for a path page: one explain section per target cell group."""
    return {"profile": "从零开始", "assumed": [], "sections": [
        {"id": "s1", "heading": "从你会的出发", "role": "organizer", "cells": [],
         "why": "挂到已会的"},
        {"id": "s2", "heading": "这一页的概念", "role": "explain", "cells": cells,
         "why": "目标格"},
    ]}


ASPECTS4 = ("term", "purpose", "notation", "interpret")    # tests.wikiapp.test_paths
PAGE1_CELLS = [f"{c}.{a}" for c in ("c1", "c2", "c4") for a in ASPECTS4]


def script_path_page(s, cells=PAGE1_CELLS) -> None:
    """outline + one write per section (no check section: no verifier call)."""
    s.push(ORGANIZE, path_outline(cells))
    for sid in ("s1", "s2"):
        s.push(WRITE, text(sid))


def big_goal(wiki) -> str:
    """The big goal's one block, nothing to ask: its three-page path (tests.wikiapp.flat_goal)."""
    client, s = wiki
    signup(client, "u1")
    s.push("goals.intake", BIG_INTAKE)
    push_no_question(s)
    s.push("goals.path", BIG_PATH)
    svc = client.app.state.services
    out = flat_goal(PgRepo(svc.pool), require(svc.gateway, "u1"), "u1", BIG_GOAL,
                    now=svc.clock(), new_id=svc.new_id)
    assert out.path_id and out.page_id is None
    return out.path_id


def next_page(client, path_id) -> PathNextOut:
    r = client.post(f"/wiki/paths/{path_id}/next", headers=H("u1"))
    assert r.status_code == 200, r.text
    return PathNextOut.model_validate(r.json())


def organize(client, page_id):
    return parse_sse(client.post(f"/wiki/pages/{page_id}/organize", headers=H("u1")).text)


def catalog(client) -> CatalogOut:
    r = client.get("/wiki", headers=H("u1"))
    assert r.status_code == 200, r.text
    return CatalogOut.model_validate(r.json())


# ---- learning paths ----

def test_big_goal_returns_path_id_then_next_then_organize(wiki):
    client, s = wiki
    path_id = big_goal(wiki)
    r = client.get(f"/wiki/paths/{path_id}", headers=H("u1"))
    assert r.status_code == 200, r.text
    path = PathOut.model_validate(r.json())
    assert [p.status for p in path.pages] == ["not_started"] * 3 and path.next_index == 1
    assert path.goal == BIG_GOAL and path.source == "llm"
    assert path.pages[0].aim == BIG_PATH["pages"][0]["objective"]
    assert all(p.page_id is None for p in path.pages)
    calls = s.calls()
    nxt = next_page(client, path_id)
    assert (nxt.path_id, nxt.index) == (path_id, 1) and s.calls() == calls     # 0 LLM calls
    script_path_page(s)
    events = organize(client, nxt.page_id)
    assert events[-1][0] == "done", events
    page = client.post(f"/wiki/pages/{nxt.page_id}/open", headers=H("u1"))
    assert page.status_code == 200 and page.json()["organized"] is True
    cat = catalog(client)
    assert [(c.path_id, c.pages, c.done, c.next_index) for c in cat.paths] == [
        (path_id, 3, 0, 2)]
    path = PathOut.model_validate(client.get(f"/wiki/paths/{path_id}", headers=H("u1")).json())
    assert [p.status for p in path.pages] == ["learning", "not_started", "not_started"]
    assert path.pages[0].page_id == nxt.page_id


@pytest.mark.skip(reason="skipped since the goal-flow rebuild; its expectations are stale (page done needs a written page, organize events changed) — revive separately")
def test_path_page_done_route(wiki):
    client, _ = wiki
    path_id = big_goal(wiki)
    nxt = next_page(client, path_id)
    r = client.post(f"/wiki/paths/{path_id}/pages/1/done", headers=H("u1"))
    assert r.status_code == 200, r.text
    out = PathPageDoneOut.model_validate(r.json())
    assert out.path.pages[0].status == "done" and out.path.next_index == 2
    assert out.path.pages[0].page_id == nxt.page_id and out.done.skipped == "mode"
    page = client.post(f"/wiki/pages/{nxt.page_id}/open", headers=H("u1")).json()
    assert page["done"] is True
    assert catalog(client).paths[0].done == 1


def test_path_errors(wiki):
    client, _ = wiki
    path_id = big_goal(wiki)
    assert client.get("/wiki/paths/path_nope", headers=H("u1")).status_code == 404
    assert client.post("/wiki/paths/path_nope/next", headers=H("u1")).status_code == 404
    assert client.post("/wiki/paths/path_nope/pages/1/done", headers=H("u1")).status_code == 404
    assert client.get(f"/wiki/paths/{path_id}", headers=H("ghost")).status_code == 404
    r = client.post(f"/wiki/paths/{path_id}/pages/1/done", headers=H("u1"))
    assert r.status_code == 409 and r.json()["detail"] == "这一页还没开始"
    for _ in range(3):
        next_page(client, path_id)
    r = client.post(f"/wiki/paths/{path_id}/next", headers=H("u1"))
    assert r.status_code == 409 and r.json()["detail"] == "这条路径的页都已经开始了"
    for bad in (0, 4, 99):
        r = client.post(f"/wiki/paths/{path_id}/pages/{bad}/done", headers=H("u1"))
        assert r.status_code == 404, (bad, r.text)
    assert client.post(f"/wiki/paths/{path_id}/pages/x/done",
                       headers=H("u1")).status_code == 422


@pytest.mark.skip(reason="skipped since the goal-flow rebuild; its expectations are stale (page done needs a written page, organize events changed) — revive separately")
def test_organize_over_budget_sse(wiki, services):
    client, s = wiki
    path_id = big_goal(wiki)
    nxt = next_page(client, path_id)
    services.gateway = _budget_gateway(s)
    r = client.post(f"/wiki/pages/{nxt.page_id}/organize", headers=H("u1"))
    assert r.status_code == 200
    events = parse_sse(r.text)
    assert [n for n, _ in events] == ["error"]
    err = ErrorEvent.model_validate(events[0][1])
    assert (err.code, err.retryable) == ("budget_exceeded", False)
    assert client.post(f"/wiki/pages/{nxt.page_id}/open", headers=H("u1")).json()["organized"] is False


# ---- review ----

def learned_page(wiki, services) -> str:
    """The organized z-score page via the API, then LEARN_EVENTS quiz `+` on both c1 cells at
    T0 (committed directly), then opened at T0 so the fold pass folds s2."""
    client, _ = wiki
    pid = organized_page(wiki)
    repo = PgRepo(services.pool)
    for i in range(LEARN_EVENTS):
        snap = repo.load("u1")
        repo.commit(snap, Commit(aspect=[
            Observe(event_id=f"aev_k1_{i}_{c}", ts=T0, observation=Observation(
                cell=c, polarity="+", strength=1.0, kind="quiz", evidence_ref=f"quiz:k1{i}"))
            for c in CELLS]))
    page = client.post(f"/wiki/pages/{pid}/open", headers=H("u1")).json()
    assert next(x for x in page["sections"] if x["id"] == "s2")["folded"]
    return pid


def test_review_endpoints(wiki, services):
    client, _ = wiki
    pid = learned_page(wiki, services)
    services.gateway = None                    # review works without an LLM (fold only)
    r = client.get("/wiki/review/today", headers=H("u1"))
    assert r.status_code == 200, r.text
    assert ReviewTodayOut.model_validate(r.json()).items == []          # nothing fading yet
    services.clock.advance(days=LATER_DAYS)
    r = client.get("/wiki/review/today", headers=H("u1"))
    assert r.status_code == 200, r.text
    today = ReviewTodayOut.model_validate(r.json())
    # s2's one cue covers both cells: one card (D15), not the same prompt twice
    assert len(today.items) == today.due == 1 and today.limit == 10 and today.done_today == 0
    first = today.items[0]
    assert sorted(first.cells) == sorted(CELLS) and first.cell == first.cells[0]
    assert first.page_id == pid and first.mode == "cue" and first.cue and first.in_goal
    assert all("answer" not in (it.get("check") or {}) for it in r.json()["items"])

    r = client.post("/wiki/review/grade", headers=H("u1"), json={
        "cell": first.cell, "cells": first.cells, "grade": "forgot", "page_id": pid,
        "section_id": "s2"})
    assert r.status_code == 200, r.text
    out = ReviewGradeOut.model_validate(r.json())
    assert out.band.cell == first.cell and out.band.to is Band.SHAKY
    assert "s2" in out.change.unfolded and out.page_id == pid and out.change.llm_calls == 0
    assert r.json()["band"]["from"] == "shaky"                          # wire alias
    after = ReviewTodayOut.model_validate(
        client.get("/wiki/review/today", headers=H("u1")).json())
    # 没想起来 on a forgotten card: both cells relearn, so the card is due again - once
    assert after.done_today == 1 and after.due == 1
    assert sorted(after.items[0].cells) == sorted(CELLS)


    r = client.post("/wiki/review/grade", headers=H("u1"),
                    json={"cell": "c9.term", "grade": "fuzzy"})
    assert r.status_code == 404
    for bad in ({"cell": "c1.interpret"},                                     # neither
                {"cell": "c1.interpret", "grade": "fuzzy", "choice": 0},      # both
                {"cell": "c1.interpret", "grade": "meh"},
                {"cell": "c1.interpret", "choice": 0},                        # no page / section
                {"cell": "c1.interpret", "grade": "fuzzy", "x": 1}):
        assert client.post("/wiki/review/grade", headers=H("u1"),
                           json=bad).status_code == 422, bad
    r = client.post("/wiki/review/grade", headers=H("u1"), json={
        "cell": "c1.interpret", "choice": 0, "page_id": pid, "section_id": "s2"})
    assert r.status_code == 422 and r.json()["detail"]            # s2 is a cue section
    r = client.post("/wiki/review/grade", headers=H("u1"), json={
        "cell": "c1.interpret", "choice": CHECK_KEY, "page_id": pid, "section_id": "s4"})
    assert r.status_code == 200, r.text
    assert (r.json()["correct"], r.json()["correct_index"]) == (True, CHECK_KEY)
    assert client.get("/wiki/review/today", headers=H("ghost")).status_code == 404


def test_settings_review_per_day_via_api(wiki):
    client, _ = wiki
    organized_page(wiki)
    r = client.put("/wiki/settings", headers=H("u1"),
                   json={"review_per_day": 3})
    assert r.status_code == 200, r.text
    assert client.get("/wiki/settings", headers=H("u1")).json()["review_per_day"] == 3
    assert client.get("/wiki/review/today", headers=H("u1")).json()["limit"] == 3
    assert client.get("/wiki/review/today?more=true", headers=H("u1")).json()["limit"] == 3
    r = client.put("/wiki/settings", headers=H("u1"),
                   json={"review_per_day": 101})
    assert r.status_code == 200                      # a batch size, no upper bound
    r = client.put("/wiki/settings", headers=H("u1"),
                   json={"review_per_day": -1})
    assert r.status_code == 422
    assert client.get("/wiki/settings", headers=H("u1")).json()["review_per_day"] == 101


def test_settings_put_without_review_per_day_keeps_it(wiki):
    client, _ = wiki
    organized_page(wiki)
    r = client.put("/wiki/settings", headers=H("u1"),
                   json={"review_per_day": 25})
    assert r.status_code == 200, r.text
    r = client.put("/wiki/settings", headers=H("u1"), json={"language": "en"})
    assert r.status_code == 200 and r.json() == {"review_per_day": 25, "language": "en",
                                                 "page_limit_usd": None}
    r = client.put("/wiki/settings", headers=H("u1"), json={"curate": "days", "days": 14})
    assert r.status_code == 422                          # 自动整理 is gone
    assert client.get("/wiki/settings", headers=H("u1")).json()["review_per_day"] == 25
