from datetime import UTC, datetime, timedelta

from evowiki.bot.settings import DEFAULT_SETTINGS
from evowiki.store import bot as store
from tests.runtime.helpers import T0
from tests.service.conftest import H, signup


def put(services, sid, **kw):
    base = {"suggestion_id": sid, "user_id": "u1", "kind": "review_due", "title": "复习",
            "why": "w", "action": {"type": "open_settings"}, "placement": "today", "priority": 1,
            "dedup_key": "review_due", "expires_at": T0 + timedelta(hours=6),
            "created_at": T0 - timedelta(hours=1)}
    with services.pool.connection() as conn:
        store.insert_suggestion(conn, store.SuggestionRow(**{**base, **kw}))


def add_page(services, pid, title="均值", cell="c1.interpret"):
    from evowiki.goals.contracts import OutlineSection, PageOutline, SectionRole
    from evowiki.goals.fold import Page
    from evowiki.wikiapp.events import PageRecord, PageSaved, WikiEvent
    from evowiki.wikiapp.repo import Commit, PgRepo
    outline = PageOutline(
        page_id=pid, goal="g", concepts=("c1",), targets=("c1",),
        sections=(OutlineSection(id="s1", heading="h", role=SectionRole.EXPLAIN, cells=(cell,)),))
    rec = PageRecord(page_id=pid, title=title, goal="g", concepts=("c1",),
                     targets=("c1",), page=Page(outline=outline, states={}), created_at=T0)
    repo = PgRepo(services.pool)
    repo.commit(repo.load("u1"), Commit(wiki=[WikiEvent(
        event_id=f"wev_{pid}", ts=T0, body=PageSaved(record=rec, action="created"))]))


def test_suggestions_list_react_badge(client, services):
    signup(client)
    put(services, "a")
    add_page(services, "pg1")
    put(services, "p", placement="page", page_id="pg1")
    assert client.get("/wiki/bot/badge", headers=H("u1")).json() == {
        "today_new": 1, "pending_actions": 0}
    r = client.get("/wiki/bot/suggestions?placement=today", headers=H("u1"))
    assert r.status_code == 200, r.text
    [s] = r.json()
    assert s["suggestion_id"] == "a" and s["status"] == "shown"
    assert set(s) == {"suggestion_id", "kind", "title", "why", "action", "placement", "page_id",
                      "priority", "status", "created_at", "expires_at", "trigger_id",
                      "preference_action_id"}
    r = client.get("/wiki/bot/suggestions?placement=page&page_id=pg1", headers=H("u1"))
    assert [x["suggestion_id"] for x in r.json()] == ["p"]
    assert client.get("/wiki/bot/suggestions?placement=page", headers=H("u1")).status_code == 422
    r = client.post("/wiki/bot/suggestions/a/react", json={"reaction": "accepted"},
                    headers=H("u1"))
    assert r.status_code == 200 and r.json()["status"] == "accepted"
    assert client.get("/wiki/bot/badge", headers=H("u1")).json()["today_new"] == 0


def test_react_404_409_422(client, services):
    signup(client)
    put(services, "a")
    h = H("u1")
    assert client.post("/wiki/bot/suggestions/zz/react", json={"reaction": "accepted"},
                       headers=h).status_code == 404
    for gone in ("later", "saved"):                      # no longer reactions
        assert client.post("/wiki/bot/suggestions/a/react", json={"reaction": gone},
                           headers=h).status_code == 422
    assert client.post("/wiki/bot/suggestions/a/react", json={"reaction": "done"},
                       headers=h).status_code == 409      # not started yet
    assert client.post("/wiki/bot/suggestions/a/react", json={"reaction": "bogus"},
                       headers=h).status_code == 422
    assert client.post("/wiki/bot/suggestions/a/react", json={"reaction": "not_interested"},
                       headers=h).status_code == 200
    assert client.post("/wiki/bot/suggestions/a/react", json={"reaction": "not_interested"},
                       headers=h).status_code == 409
    signup(client, "u2")
    assert client.post("/wiki/bot/suggestions/a/react", json={"reaction": "accepted"},
                       headers=H("u2")).status_code == 404
    assert client.get("/wiki/bot/badge", headers=H("ghost")).status_code == 404


def test_settings_roundtrip_and_validation(client):
    signup(client)
    h = H("u1")
    r = client.get("/wiki/bot/settings", headers=h)
    assert r.status_code == 200 and r.json() == DEFAULT_SETTINGS.model_dump(mode="json")
    body = {**r.json(), "today_cap": 9, "timezone": "UTC"}
    r = client.put("/wiki/bot/settings", json=body, headers=h)
    assert r.status_code == 200 and r.json()["today_cap"] == 9
    assert client.get("/wiki/bot/settings", headers=h).json()["timezone"] == "UTC"
    bad = {**body, "levels": {**body["levels"], "page_body": "auto"}}
    assert client.put("/wiki/bot/settings", json=bad, headers=h).status_code == 422
    for scope in ("user_notes", "suggestion"):          # fixed
        fixed = {**body, "levels": {**body["levels"], scope: "ask"}}
        assert client.put("/wiki/bot/settings", json=fixed, headers=h).status_code == 422
    for scope in ("teaching_params", "rsi", "new_page"):            # `ask` is choosable now
        r = client.put("/wiki/bot/settings",
                       json={**body, "levels": {**body["levels"], scope: "ask"}}, headers=h)
        assert r.status_code == 200 and r.json()["levels"][scope] == "ask"
    ok = {**body, "levels": {**body["levels"], "page_body": "ask", "structure": "ask",
                             "delete_merge": "auto_undo"}}
    r = client.put("/wiki/bot/settings", json=ok, headers=h)
    assert r.status_code == 200 and r.json()["levels"]["page_body"] == "ask"
    assert client.get("/wiki/bot/settings", headers=h).json()["levels"] == ok["levels"]
    assert client.put("/wiki/bot/settings", json={**body, "timezone": "Mars/Base"},
                      headers=h).status_code == 422
    assert client.get("/wiki/bot/settings", headers=h).json()["today_cap"] == 9


def test_pause_tomorrow_forever_off(client):
    signup(client)
    h = H("u1")
    r = client.post("/wiki/bot/pause", json={"until": "tomorrow"}, headers=h)
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["paused"] == "until"
    # T0 is 17:00 Asia/Shanghai on 2026-10-01: paused until the next daily check,
    # 08:00 on 10-02 local = 00:00 UTC (what 「暂停到明天 8:00」 promises)
    assert datetime.fromisoformat(got["paused_until"]) == datetime(2026, 10, 2, 0, tzinfo=UTC)
    assert client.get("/wiki/bot/settings", headers=h).json()["paused"] == "until"
    r = client.post("/wiki/bot/pause", json={"until": "forever"}, headers=h)
    assert (r.json()["paused"], r.json()["paused_until"]) == ("always", None)
    r = client.post("/wiki/bot/pause", json={"until": "off"}, headers=h)
    assert (r.json()["paused"], r.json()["paused_until"]) == ("no", None)
    assert client.post("/wiki/bot/pause", json={"until": "soon"}, headers=h).status_code == 422


class _Ex:
    def rejected(self, user_id, action):
        pass


def test_action_routes(client, services):
    from evowiki.bot.actions import ActionLedger
    signup(client)
    h = H("u1")
    assert client.get("/wiki/bot/actions", headers=h).status_code == 503
    ledger = ActionLedger(services.pool, services.clock, services.new_id,
                          {"assistant_edit": _Ex()})
    services.actions = ledger
    assert client.put("/wiki/bot/settings", headers=h, json={
        **client.get("/wiki/bot/settings", headers=h).json(),
        "levels": {**DEFAULT_SETTINGS.levels, "page_body": "ask"}}).status_code == 200
    a = ledger.propose("u1", kind="assistant_edit", scope="page_body", source="assistant",
                       summary="改一段", why="因为", content={"x": 1}, key="k1")
    assert client.get("/wiki/bot/badge", headers=h).json()["pending_actions"] == 1
    [row] = client.get("/wiki/bot/actions?status=proposed", headers=h).json()
    assert row["action_id"] == a.action_id and row["can_undo"] is False
    assert set(row) == {"action_id", "kind", "scope", "level", "status", "source", "summary",
                        "why", "content", "page_id", "detail", "created_at", "decided_at",
                        "expires_at", "can_undo", "confirm", "cost_usd", "diff", "made", "revise",
                        "result", "section_id"}
    assert client.get("/wiki/bot/actions?status=executed", headers=h).json() == []
    r = client.post(f"/wiki/bot/actions/{a.action_id}/reject", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    assert client.post(f"/wiki/bot/actions/{a.action_id}/approve", headers=h).status_code == 409
    assert client.post("/wiki/bot/actions/nope/undo", headers=h).status_code == 404
    assert client.get("/wiki/bot/badge", headers=h).json()["pending_actions"] == 0
    b = ledger.propose("u1", kind="assistant_edit", scope="page_body", source="assistant",
                       summary="再改", why="", content={"x": 2}, key="k2")
    r = client.post(f"/wiki/bot/actions/{b.action_id}/approve", headers=h,
                    json={"draft": {"x": 3}})        # this executor has no `revise`
    assert r.status_code == 409


def test_page_suggestions_make_no_chat_chips(client, services):
    """Page chips were removed from the UI 2026-10-21: the list stays empty."""
    from evowiki.assistant.pg_store import PgSessionStore
    from evowiki.assistant.session import SessionInfo
    signup(client)
    h = H("u1")
    add_page(services, "pg1")
    store_ = PgSessionStore(services.pool)
    store_.create(SessionInfo(session_id="as1", user_id="u1", page_id="pg1", note_concept_id=None,
                              created_at=T0 - timedelta(days=1)))
    store_.append("as1", "e1", "compaction", {"summary": "还没懂的：标准差的含义"},
                  T0 - timedelta(hours=2))
    url = "/wiki/bot/suggestions?placement=page&page_id=pg1"
    r = client.get(url, headers=h)
    assert r.status_code == 200, r.text
    assert r.json() == []
    assert client.get("/wiki/bot/suggestions?placement=today", headers=h).json() == []


def test_not_interested_gives_a_preference_row_that_undoes_through_the_ledger(client, services):
    from evowiki.bot.actions import ActionLedger
    from evowiki.bot.executors import PreferenceExecutor
    signup(client)
    h = H("u1")
    services.actions = ActionLedger(services.pool, services.clock, services.new_id,
                                    {"preference": PreferenceExecutor(services.pool,
                                                                      services.clock)})
    put(services, "a")
    r = client.post("/wiki/bot/suggestions/a/react", json={"reaction": "not_interested"},
                    headers=h)
    assert r.status_code == 200
    pref = r.json()["preference_action_id"]
    assert pref
    [row] = client.get("/wiki/bot/actions?status=executed", headers=h).json()
    assert (row["action_id"], row["kind"], row["scope"], row["can_undo"], row["diff"]) == (
        pref, "preference", "preference", True, [])
    r = client.post(f"/wiki/bot/actions/{pref}/undo", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "undone"
    [s] = client.get("/wiki/bot/suggestions?placement=today", headers=h).json()
    assert s["suggestion_id"] == "a" and s["status"] == "shown"
    assert s["preference_action_id"] is None


def test_actions_carry_their_diff(client, services):
    from evowiki.bot.actions import ActionLedger
    signup(client)
    h = H("u1")
    add_page(services, "pg1")
    ledger = ActionLedger(services.pool, services.clock, services.new_id, {})
    services.actions = ledger
    with services.pool.connection() as conn:
        ledger.record_in(conn, "u1", kind="rsi_patch", scope="teaching_params",
                         source="rsi", summary="调低自我评估权重", key="rsi:u1:p1",
                         content={"patch_id": "p1", "track": "A", "diff": [
                             {"kind": "ctx", "text": "学习状态评估"},
                             {"kind": "del", "text": "自我评估权重 0.60"},
                             {"kind": "add", "text": "自我评估权重 0.42"}]})
    [row] = client.get("/wiki/bot/actions", headers=h).json()
    assert row["diff"] == [{"kind": "ctx", "text": "学习状态评估"},
                           {"kind": "del", "text": "自我评估权重 0.60"},
                           {"kind": "add", "text": "自我评估权重 0.42"}]


class _Revising(_Ex):
    def revise(self, action, draft):
        from evowiki.bot.actions import ActionRefused
        if draft.get("bad"):
            raise ActionRefused("已经有这一块了")
        return {**action.content, **draft}


def test_action_draft_route_keeps_a_card_edit_without_approving(client, services):
    """POST /wiki/bot/actions/{id}/draft (H3): the executor's revise stores the edit on the
    waiting row (still proposed); a refused edit is 409 with its text and stores nothing; a row
    that is not waiting, or a kind without revise, is 409; another user's row 404."""
    from evowiki.bot.actions import ActionLedger
    signup(client)
    h = H("u1")
    ledger = ActionLedger(services.pool, services.clock, services.new_id,
                          {"goal_new": _Revising(), "assistant_edit": _Ex()})
    services.actions = ledger
    a = ledger.propose("u1", kind="goal_new", scope="structure", source="assistant",
                       summary="分块", why="", content={"x": 1, "confirm": True}, key="kd1",
                       level="ask")
    r = client.post(f"/wiki/bot/actions/{a.action_id}/draft", headers=h, json={"draft": {"x": 2}})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "proposed" and r.json()["content"]["x"] == 2
    assert ledger.get("u1", a.action_id).content == {"x": 2, "confirm": True}
    r = client.post(f"/wiki/bot/actions/{a.action_id}/draft", headers=h,
                    json={"draft": {"bad": True}})
    assert r.status_code == 409 and r.json()["detail"] == "已经有这一块了"
    assert ledger.get("u1", a.action_id).content == {"x": 2, "confirm": True}
    b = ledger.propose("u1", kind="assistant_edit", scope="page_body", source="assistant",
                       summary="改", why="", content={"x": 1}, key="kd2", level="ask")
    assert client.post(f"/wiki/bot/actions/{b.action_id}/draft", headers=h,
                       json={"draft": {"x": 2}}).status_code == 409
    client.post(f"/wiki/bot/actions/{a.action_id}/reject", headers=h)
    assert client.post(f"/wiki/bot/actions/{a.action_id}/draft", headers=h,
                       json={"draft": {"x": 3}}).status_code == 409
    assert client.post("/wiki/bot/actions/nope/draft", headers=h,
                       json={"draft": {"x": 3}}).status_code == 404


def test_review_reminder_routes(client, services):
    from evowiki.bot.actions import ActionLedger
    from evowiki.bot.executors import PreferenceExecutor
    signup(client)
    h = H("u1")
    services.actions = ActionLedger(services.pool, services.clock, services.new_id,
                                    {"preference": PreferenceExecutor(services.pool,
                                                                      services.clock)})
    assert client.get("/wiki/bot/review-reminder", headers=h).json() == {
        "hidden": False, "preference_action_id": None}
    r = client.post("/wiki/bot/review-reminder", json={"reaction": "not_interested"},
                    headers=h).json()
    assert r["hidden"] is True and r["preference_action_id"]
    assert client.get("/wiki/bot/review-reminder", headers=h).json()["hidden"] is True
    assert client.post(f"/wiki/bot/actions/{r['preference_action_id']}/undo",
                       headers=h).status_code == 200
    assert client.get("/wiki/bot/review-reminder", headers=h).json()["hidden"] is False
    assert client.post("/wiki/bot/review-reminder", json={"reaction": "done"},
                       headers=h).status_code == 422


def test_his_window_in_front_of_him_is_what_the_bot_goes_by(client, services):
    signup(client)
    with services.pool.connection() as conn:
        assert store.last_seen_of(conn, "u1") is None         # signed up by a script: not served
    assert client.post("/me/seen", headers=H("u1")).status_code == 204
    with services.pool.connection() as conn:
        assert store.last_seen_of(conn, "u1") == services.clock()
    assert client.post("/me/seen", headers=H("ghost")).status_code == 404


def test_reading_the_ledger_takes_a_settled_goal_draft_off_the_waiting_list(client, services):
    from evowiki.bot.actions import ActionLedger
    signup(client)
    h = H("u1")
    ledger = ActionLedger(services.pool, services.clock, services.new_id, {"goal_new": _Ex()})
    services.actions = ledger
    ledger.record("u1", kind="goal_new", scope="structure", source="assistant", summary="旧",
                  content={"goal": "我要学吉他"}, key="k1", status="proposed")
    services.clock.t = services.clock() + timedelta(minutes=1)
    ledger.record("u1", kind="goal_new", scope="structure", source="assistant", summary="新",
                  content={"goal": "吉他"}, key="k2", status="proposed")
    old, new = ledger.get_by_key("k1"), ledger.get_by_key("k2")
    assert [r["action_id"] for r in client.get("/wiki/bot/actions?status=proposed", headers=h).json()] == [new.action_id]
    assert ledger.get("u1", old.action_id).status == "rejected"
    # the one left waits for his own 取消
    assert client.post(f"/wiki/bot/actions/{new.action_id}/reject", headers=h).json()["status"] == "rejected"
