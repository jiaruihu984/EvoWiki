"""/wiki/bot/goals: parked or not, the latest review, reading time; park / resume (B5a)."""

from datetime import date, timedelta

from evowiki.store import bot_goals
from evowiki.wikiapp.events import PathRecord, PathSaved, WikiEvent
from evowiki.wikiapp.repo import Commit, PgRepo
from tests.runtime.helpers import T0
from tests.service.conftest import H, signup
from tests.service.test_routes_bot import add_page
from tests.wikiapp.test_k1_state import lp


def seed_path(services):
    add_page(services, "pg_1")
    rec = PathRecord(path=lp(2), page_ids=("pg_1", None),
                     created_at=T0 - timedelta(days=5))
    repo = PgRepo(services.pool)
    repo.commit(repo.load("u1"), Commit(wiki=[WikiEvent(
        event_id="wev_path", ts=T0 - timedelta(days=5),
        body=PathSaved(record=rec, action="page_started"))]))


def test_goals_park_resume_and_reading(client, services):
    signup(client)
    seed_path(services)
    h = H("u1")
    [g] = client.get("/wiki/bot/goals", headers=h).json()
    assert g == {"path_id": "path-k", "goal": "学统计", "parked": False, "parked_at": None,
                 "last_review": None, "reading_minutes": None}
    r = client.post("/wiki/bot/goals/path-k/park", headers=h)
    assert r.status_code == 200 and r.json()["parked"] is True and r.json()["parked_at"]
    assert client.post("/wiki/bot/goals/path-k/park", headers=h).json()["parked"] is True
    r = client.post("/wiki/bot/goals/path-k/resume", headers=h)
    assert r.json()["parked"] is False and r.json()["parked_at"] is None
    assert client.post("/wiki/bot/goals/nope/park", headers=h).status_code == 404

    local_today = T0.date()          # T0 is 17:00 in Asia/Shanghai, the same date
    with services.pool.connection() as conn:
        bot_goals.add_reading(conn, "u1", "b0", [("pg_1", local_today - timedelta(days=9), 60)],
                              T0)
        bot_goals.add_reading(conn, "u1", "b1", [("pg_1", local_today, 600)], T0)
        bot_goals.put_review(conn, "u1", week_start=date(2026, 9, 28), path_id="path-k",
                             situation="normal", facts={}, trigger_id=None, at=T0)
    [g] = client.get("/wiki/bot/goals", headers=h).json()
    assert g["reading_minutes"] == 10
    assert g["last_review"] == {"week_start": "2026-09-28", "situation": "normal",
                                "trigger_id": None}


def test_park_a_taken_back_goal_is_404(client, services):
    from evowiki.wikiapp.events import GoalSettings, GoalSettingsSet
    signup(client)
    seed_path(services)
    repo = PgRepo(services.pool)
    repo.commit(repo.load("u1"), Commit(wiki=[WikiEvent(
        event_id="wev_arch", ts=T0, body=GoalSettingsSet(
            settings=GoalSettings(goal_id="path-k", archived=True)))]))
    h = H("u1")
    for verb in ("park", "resume"):
        r = client.post(f"/wiki/bot/goals/path-k/{verb}", headers=h)
        assert r.status_code == 404 and "收回" in r.text
