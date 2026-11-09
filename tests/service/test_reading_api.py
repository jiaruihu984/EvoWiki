"""POST /wiki/reading (B5 spec §1.6): batched reading seconds summed per page and local day;
resent batches are not counted twice; unknown pages and stale entries are dropped and counted."""

from datetime import date, timedelta

from evowiki.store import bot_goals
from tests.runtime.helpers import T0
from tests.service.conftest import H, signup
from tests.service.test_routes_bot import add_page


def post(client, body, uid="u1"):
    return client.post("/wiki/reading", json=body, headers=H(uid))


def entry(page, seconds, at):
    return {"page_id": page, "seconds": seconds, "at": at.isoformat()}


def by_page(services, first, last):
    with services.pool.connection() as conn:
        return bot_goals.reading_by_page(conn, "u1", ["pg1", "pg2"], first, last)


def test_batches_sum_per_page_and_local_day(client, services):
    signup(client)
    add_page(services, "pg1")
    add_page(services, "pg2", title="方差")
    # days are local days of the Bot settings' time zone (default Asia/Shanghai):
    # T0 is 2026-10-01 09:00 UTC = 17:00 in Shanghai; 16:30 UTC is 00:30 the next local day
    late = T0.replace(hour=16, minute=30)
    r = post(client, {"batch_id": "b-000001", "entries": [
        entry("pg1", 60, T0 - timedelta(minutes=5)), entry("pg1", 45, T0),
        entry("pg2", 30, T0), entry("nope", 60, T0)]})
    assert r.status_code == 200, r.text
    assert r.json() == {"accepted": 3, "dropped": 1, "duplicate": False}
    services.clock.t = late + timedelta(minutes=1)
    assert post(client, {"batch_id": "b-000002", "entries": [entry("pg1", 20, late)]}).json() == {
        "accepted": 1, "dropped": 0, "duplicate": False}
    assert by_page(services, date(2026, 10, 1), date(2026, 10, 1)) == {"pg1": 105, "pg2": 30}
    assert by_page(services, date(2026, 10, 2), date(2026, 10, 2)) == {"pg1": 20}
    # the same batch again changes nothing
    assert post(client, {"batch_id": "b-000002", "entries": [entry("pg1", 20, late)]}).json() == {
        "accepted": 0, "dropped": 0, "duplicate": True}
    assert by_page(services, date(2026, 10, 2), date(2026, 10, 2)) == {"pg1": 20}
    with services.pool.connection() as conn:
        assert bot_goals.first_reading_day(conn, "u1") == date(2026, 10, 1)


def test_entries_outside_the_window_are_dropped(client, services):
    signup(client)
    add_page(services, "pg1")
    r = post(client, {"batch_id": "b-000003", "entries": [
        entry("pg1", 60, T0 - timedelta(days=3)), entry("pg1", 60, T0 + timedelta(hours=1)),
        entry("pg1", 60, T0)]})
    assert r.json() == {"accepted": 1, "dropped": 2, "duplicate": False}


def test_bad_bodies_are_refused(client, services):
    signup(client)
    add_page(services, "pg1")
    assert post(client, {"batch_id": "b-000004", "entries": [entry("pg1", 601, T0)]}).status_code == 422
    assert post(client, {"batch_id": "b-000004", "entries": [entry("pg1", 0, T0)]}).status_code == 422
    assert post(client, {"batch_id": "b-000004", "entries": []}).status_code == 422
    assert post(client, {"batch_id": "x", "entries": [entry("pg1", 5, T0)]}).status_code == 422
    naive = {"page_id": "pg1", "seconds": 5, "at": "2026-10-01T09:00:00"}
    assert post(client, {"batch_id": "b-000004", "entries": [naive]}).status_code == 422
