"""GET /wiki/bot/why and /wiki/bot/recent. Fakes only."""
from datetime import timedelta

from evowiki.bot.pipeline import dispatch
from evowiki.store import bot as store
from tests.runtime.helpers import T0
from tests.service.conftest import H, signup


def put_trigger(pool, tid, created):
    with pool.connection() as conn:
        store.insert_trigger(conn, store.TriggerRow(
            trigger_id=tid, user_id="u1", kind="review_due", rule_id="review_due", rule_version=1,
            source_key=tid, dedup_key=tid, route="direct", evidence=[], payload={"count": 2},
            priority=50, reason="r", created_at=created))


def test_routes(client, services):
    signup(client)
    signup(client, "u2")
    pool = services.pool
    put_trigger(pool, "t1", T0 - timedelta(minutes=5))
    d = dispatch(pool, "t1", now=T0, new_id=lambda p: f"{p}_1")
    h = H("u1")
    r = client.get(f"/wiki/bot/why?suggestion_id={d.suggestion_id}", headers=h)
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"trigger", "gate", "outputs", "reaction"}
    assert client.get("/wiki/bot/why?trigger_id=t1", headers=h).json() == r.json()
    assert client.get("/wiki/bot/why", headers=h).status_code == 422
    assert client.get(f"/wiki/bot/why?trigger_id=t1&suggestion_id={d.suggestion_id}",
                      headers=h).status_code == 422
    assert client.get("/wiki/bot/why?trigger_id=zz", headers=h).status_code == 404
    assert client.get("/wiki/bot/why?trigger_id=t1", headers=H("u2")).status_code == 404
    r = client.get("/wiki/bot/recent?days=14&skipped=false", headers=h)
    assert r.status_code == 200 and [a["trigger_id"] for a in r.json()] == ["t1"]
    assert r.json()[0]["result"] == "suggested"
    assert client.get("/wiki/bot/recent", headers=H("u2")).json() == []
    assert client.get("/wiki/bot/recent?days=0", headers=h).status_code == 422


def test_new_text_fields(client, services):
    signup(client)
    pool = services.pool
    with pool.connection() as conn:
        store.insert_trigger(conn, store.TriggerRow(
            trigger_id="t9", user_id="u1", kind="review_due", rule_id="review_due", rule_version=1,
            source_key="t9", dedup_key="t9", route="direct", evidence=[], payload={"count": 2},
            priority=50, reason="r", created_at=T0 - timedelta(minutes=5),
            title="复习 2 张到期的卡"))
    dispatch(pool, "t9", now=T0, new_id=lambda p: f"{p}_9")
    h = H("u1")
    gate = client.get("/wiki/bot/why?trigger_id=t9", headers=h).json()["gate"]
    assert gate["detail"] == "今日第 1 / 5 项"
    [a] = client.get("/wiki/bot/recent?skipped=true", headers=h).json()
    assert a["title"] == "安排了今日复习（2 张卡片）" and a["gate_detail"] == gate["detail"]
