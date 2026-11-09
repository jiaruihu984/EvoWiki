"""Goal footer completion and grading over HTTP with PostgreSQL and scripted models."""

import pytest

from evowiki.store.locks import session_lock
from tests.service.conftest import H
from tests.wikiapp.test_goal import NOW
from tests.wikiapp.test_goal_check import checks, script_questions
from tests.wikiapp.test_goal_check import env as goal_env

__all__ = ["goal_env"]


@pytest.mark.parametrize("route", ["page", "path"])
def test_completion_questions_and_answer_evidence_reach_the_graph(services, client, goal_env, route):
    repo, script, ids, pid = goal_env
    services.new_id = ids
    services.clock = lambda: NOW
    services.gateway = script.gateway()
    script_questions(goal_env)
    headers = H("u1")
    url = f"/wiki/pages/{pid}/done" if route == "page" else "/wiki/paths/goal1/pages/1/done"
    response = client.post(url, headers=headers)
    assert response.status_code == 200, response.text
    questions = checks(goal_env)
    assert len(questions) == 3
    calls = script.calls()
    seq = repo.load("u1").wiki_seq
    assert client.post(f"/wiki/pages/{pid}/done", headers=headers).status_code == 200
    assert script.calls() == calls and repo.load("u1").wiki_seq == seq
    before = client.get("/wiki/graph", headers=headers)
    assert before.status_code == 200, before.text
    # Grading needs no model when no K-driven rewrite is requested by this fixture.
    services.gateway = None
    item = questions[0]
    grade = client.post(f"/wiki/pages/{pid}/checks/{item.id}", headers=headers,
                        json={"choice": item.check.answer})
    assert grade.status_code == 200, grade.text
    assert grade.json()["correct"] is True
    after = client.get("/wiki/graph", headers=headers)
    assert after.status_code == 200, after.text
    cid = item.check.cell.split(".", 1)[0]
    previous = next(n for n in before.json()["nodes"] if n["id"] == cid)
    current = next(n for n in after.json()["nodes"] if n["id"] == cid)
    assert current["direct"] != previous["direct"]
    assert current["evidence"] != previous["evidence"]


def test_duplicate_completion_in_flight_returns_conflict_without_generating(services, client, goal_env):
    _, script, _, pid = goal_env
    services.gateway = script.gateway()
    calls = script.calls()
    with session_lock(services.pool, f"evowiki.goal-check:u1:{pid}") as acquired:
        assert acquired
        response = client.post(f"/wiki/pages/{pid}/done", headers=H("u1"))
        assert response.status_code == 409, response.text
    assert script.calls() == calls
