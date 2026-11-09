from datetime import UTC, date, datetime, timedelta

from evowiki.bot.settings import DEFAULT_SETTINGS
from evowiki.bot.suppression import Counters
from evowiki.content.models import Lang
from evowiki.store import bot as store
from evowiki.store.users import UserRow, create_user

T0 = datetime(2026, 10, 14, 1, 0, tzinfo=UTC)


def _user(pool, uid="u1"):
    with pool.connection() as conn:
        create_user(conn, UserRow(user_id=uid, background="none", condition="S2", lang=Lang.EN,
                                  created_at=T0))


def _trigger(tid="t1", source_key="k1", uid="u1"):
    return store.TriggerRow(trigger_id=tid, user_id=uid, kind="review_due", rule_id="r",
                            rule_version=1, source_key=source_key, dedup_key="d", route="direct",
                            evidence=["wev_1"], payload={"a": 1}, priority=3, reason="原因",
                            created_at=T0)


def _suggestion(sid="s1", **kw):
    base = {"suggestion_id": sid, "user_id": "u1", "kind": "review_due", "title": "t",
            "why": "w", "action": {"type": "open_settings"}, "placement": "today",
            "priority": 1, "dedup_key": "d", "expires_at": T0 + timedelta(days=1),
            "created_at": T0}
    return store.SuggestionRow(**{**base, **kw})


def test_settings_roundtrip_and_default(pool):
    _user(pool)
    with pool.connection() as conn:
        assert store.get_settings(conn, "u1") == DEFAULT_SETTINGS
        s = DEFAULT_SETTINGS.model_copy(update={"today_cap": 9, "timezone": "UTC"})
        store.put_settings(conn, "u1", s, T0)
        store.put_settings(conn, "u1", s.model_copy(update={"today_cap": 7}), T0)
    with pool.connection() as conn:
        got = store.get_settings(conn, "u1")
    assert got.today_cap == 7 and got.timezone == "UTC"


def test_cursor_lock_creates_row(pool):
    _user(pool)
    with pool.connection() as conn:
        with conn.transaction():
            assert store.lock_cursor(conn, "u1", T0) == (0, 0)
            store.set_cursor(conn, "u1", 5, 2, T0)
        with conn.transaction():
            assert store.lock_cursor(conn, "u1", T0) == (5, 2)


def test_insert_trigger_is_idempotent_on_source_key(pool):
    _user(pool)
    with pool.connection() as conn:
        assert store.insert_trigger(conn, _trigger("t1")) == "t1"
        assert store.insert_trigger(conn, _trigger("t2")) is None
        assert store.get_trigger(conn, "t1") == _trigger("t1")
        assert store.get_trigger(conn, "t2") is None
        assert [t.trigger_id for t in store.new_triggers(conn, T0 + timedelta(minutes=1))] == ["t1"]
        store.set_trigger_gate(conn, "t1", status="gated", outcome="skip", reason="paused",
                               at=T0, run_id="run1")
        got = store.lock_trigger(conn, "t1")
        assert (got.status, got.gate_outcome, got.gate_reason, got.run_id) == (
            "gated", "skip", "paused", "run1")
        assert store.new_triggers(conn, T0 + timedelta(minutes=1)) == []


def test_slot_row_once(pool):
    _user(pool)
    d = date(2026, 10, 14)
    with pool.connection() as conn:
        assert store.record_slot(conn, "u1", d, "enqueued", "", T0, T0)
        assert not store.record_slot(conn, "u1", d, "missed", "late", T0, T0)
        r = conn.execute("select status from bot_slot where user_id = 'u1'").fetchone()
    assert r["status"] == "enqueued"


def test_checks_skip_dropped(pool):
    _user(pool)
    with pool.connection() as conn:
        store.record_check(conn, "u1", T0, False)
        store.record_check(conn, "u1", T0 + timedelta(seconds=5), True)
        store.record_check(conn, "u1", T0 - timedelta(minutes=5), False)
        assert store.recent_checks(conn, "u1", T0 - timedelta(seconds=60)) == [T0]


def test_suggestions_and_counters(pool):
    _user(pool)
    with pool.connection() as conn:
        store.insert_suggestion(conn, _suggestion("s1"))
        store.insert_suggestion(conn, _suggestion(
            "s2", placement="page", page_id="p1", created_at=T0 + timedelta(hours=1)))
        assert [s.suggestion_id for s in store.suggestions(conn, "u1")] == ["s2", "s1"]
        assert [s.suggestion_id for s in store.suggestions(conn, "u1", page_id="p1")] == ["s2"]
        assert store.count_suggestions(conn, "u1", since=T0, placement="today") == 1
        assert store.last_suggested_at(conn, "u1", "d") == T0 + timedelta(hours=1)
        assert store.last_suggested_at(conn, "u1", "zz") is None
        assert store.lock_suggestion(conn, "s1") == _suggestion("s1")
        store.set_suggestion(conn, "s1", status="accepted", at=T0)
        got = store.lock_suggestion(conn, "s1")
        assert got.status == "accepted" and got.reacted_at == T0
        assert store.get_counters(conn, "u1", "d") == Counters()
        c = Counters(accepted=1, not_interested=2, later=3, last_not_interested_at=T0)
        store.put_counters(conn, "u1", "d", c)
        assert store.get_counters(conn, "u1", "d") == c
        assert store.all_user_ids(conn) == ["u1"]


def test_set_suggestion_stamps_reacted_at_only_for_reactions(pool):
    _user(pool)
    with pool.connection() as conn:
        store.insert_suggestion(conn, _suggestion("s1"))
        store.set_suggestion(conn, "s1", status="shown", at=T0 + timedelta(minutes=1))
        got = store.lock_suggestion(conn, "s1")
        assert got.status == "shown" and got.reacted_at is None
        store.set_suggestion(conn, "s1", status="accepted", at=T0 + timedelta(minutes=2))
        store.set_suggestion(conn, "s1", status="done", at=T0 + timedelta(days=2))
        got = store.lock_suggestion(conn, "s1")
        assert got.status == "done" and got.reacted_at == T0 + timedelta(minutes=2)
