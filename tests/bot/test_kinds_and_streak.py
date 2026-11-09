"""Settings consumers of Task 15: the idle_write kind gates the Bot's idle page writing, and
the learning streak on GET /wiki/review/today counts days in the Bot time zone."""
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from evowiki.belief.aspects import DefineConcept, Observe
from evowiki.bot.settings import BotSettings
from evowiki.goals.contracts import Aspect, Observation
from evowiki.service.app import create_app
from evowiki.store import bot as store
from evowiki.wikiapp.repo import Commit, PgRepo
from evowiki.wikiapp.review import streak_days
from evowiki.wikiapp.wiki_schemas import ReviewTodayOut
from tests.service.conftest import H, signup
from tests.service.conftest import services as _services_fixture

services = _services_fixture
NOW = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)         # 17:00 in Asia/Shanghai


def test_idle_write_kind_off_stops_the_idle_page_writing(services):
    from evowiki.wikiapp.write_queue import PgIdleEnv
    client = TestClient(create_app(services))
    signup(client, "u1")
    env = PgIdleEnv(services.pool, UTC, lambda p: p)
    assert env.settings("u1").kinds["idle_write"] is False     # off by default
    off = BotSettings(kinds={"idle_write": False})
    assert off.kinds["chat"] is False and off.kinds["review_due"] is True
    with services.pool.connection() as conn:
        store.put_settings(conn, "u1", off, NOW)
    assert env.settings("u1").kinds["idle_write"] is False


def obs(day: datetime, kind: str = "recall"):
    return Observe(event_id=f"ae_{kind}_{day.isoformat()}", ts=day, observation=Observation(
        cell="c1.interpret", polarity="+", strength=0.8, kind=kind,
        evidence_ref=f"review:-:-:{day.isoformat()}"))


def test_streak_counts_consecutive_local_days_ending_today():
    ev = [obs(NOW), obs(NOW - timedelta(days=1)), obs(NOW - timedelta(days=2)),
          obs(NOW - timedelta(days=4))]                       # day 3 missing: the streak ends there
    assert streak_days(ev, NOW) == 3
    assert streak_days([], NOW) == 0


def test_streak_shows_yesterdays_while_today_has_none_and_breaks_after():
    ev = [obs(NOW - timedelta(days=1)), obs(NOW - timedelta(days=2))]
    assert streak_days(ev, NOW) == 2                          # today not done yet
    assert streak_days(ev, NOW + timedelta(days=1)) == 0      # yesterday is now missing too
    assert streak_days([obs(NOW - timedelta(days=3))], NOW) == 0


def test_streak_counts_check_answers_and_one_event_a_day_and_ignores_other_evidence():
    ev = [obs(NOW, "check"), obs(NOW + timedelta(minutes=1)), obs(NOW - timedelta(days=1), "check"),
          obs(NOW - timedelta(days=2), "quiz")]
    assert streak_days(ev, NOW + timedelta(minutes=2)) == 2


def test_streak_uses_the_given_time_zone_for_day_edges():
    late = datetime(2026, 10, 14, 17, 0, tzinfo=UTC)        # 01:00 on the 15th in Shanghai
    ev = [obs(late), obs(late - timedelta(hours=3))]        # 14:00 UTC = 22:00 CST on the 14th
    now = late + timedelta(hours=1)
    assert streak_days(ev, now, "Asia/Shanghai") == 2
    assert streak_days(ev, now, "UTC") == 1


def test_review_today_carries_the_streak_in_the_bot_time_zone(services):
    client = TestClient(create_app(services))
    signup(client, "u1")
    repo = PgRepo(services.pool)
    now = services.clock()
    repo.commit(repo.load("u1"), Commit(aspect=[DefineConcept(
        event_id="ae_def", ts=now - timedelta(days=30), concept_id="c1",
        aspects=(Aspect.INTERPRET,))]))
    repo.commit(repo.load("u1"), Commit(aspect=[obs(now), obs(now - timedelta(days=1))]))
    r = client.get("/wiki/review/today", headers=H("u1"))
    assert r.status_code == 200, r.text
    assert ReviewTodayOut.model_validate(r.json()).streak_days == 2
