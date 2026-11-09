"""R1 Task 5 routes over PgRepo: POST /wiki/pages/{id}/feedback, POST /wiki/pages/{id}/hand-edit,
GET /wiki/teach/signals (stored signals + assistant selections derived from Pg sessions)."""

import re
from datetime import timedelta

import pytest

from evowiki.assistant.pg_store import PgSessionStore
from evowiki.assistant.session import SessionInfo
from evowiki.bot.actions import ActionLedger
from evowiki.bot.change_source import LedgerChangeSource
from evowiki.bot.executors_hand import HandEditExecutor
from evowiki.teach.contracts import PageFeedbackIn
from evowiki.teach.signal_kinds import polarity
from evowiki.wikiapp.feedback import page_feedback_flow
from evowiki.wikiapp.hand_edit import KIND as HAND_EDIT
from evowiki.wikiapp.repo import PgRepo
from evowiki.wikiapp.understand import PURPOSE as UNDERSTAND
from evowiki.wikiapp.understand import REPAIR_TURNS
from evowiki.wikiapp.wiki_schemas import SignalsOut
from tests.runtime.helpers import T0
from tests.service.conftest import H
from tests.service.test_wiki_api import organized_page
from tests.wikiapp.script import Script

URL_SAFE = re.compile(r"[A-Za-z0-9._-]+")


@pytest.fixture
def wiki(services, client):
    script = Script()
    services.gateway = script.gateway()
    return client, script


def signals(client, query: str = "", status: int = 200) -> SignalsOut | dict:
    r = client.get(f"/wiki/teach/signals{query}", headers=H("u1"))
    assert r.status_code == status, r.text
    return SignalsOut.model_validate(r.json()) if status == 200 else r.json()


def test_page_fit_records_a_helpful_signal_with_no_model_call(wiki, services):
    client, script = wiki
    pid = organized_page(wiki)
    calls = script.calls()
    r = client.post(f"/wiki/pages/{pid}/fit", headers=H("u1"))
    assert r.status_code == 201, r.text
    assert script.calls() == calls
    sig = next(s for s in signals(client).items if s.signal_id == r.json()["signal_id"])
    assert sig.kind == "page_fit" and sig.page_id == pid and polarity(sig) == 1
    assert client.post("/wiki/pages/pg_nope/fit", headers=H("u1")).status_code == 404


def test_page_feedback_and_hand_edit_endpoints(wiki, services):
    client, script = wiki
    pid = organized_page(wiki)
    url = f"/wiki/pages/{pid}"
    script.push(UNDERSTAND, *["not json"] * (1 + REPAIR_TURNS))  # B1: fails closed (re-asked) → signal only
    r = client.post(f"{url}/feedback", headers=H("u1"),
                    json={"chips": ["unclear", "too_hard"], "text": "看不懂", "section_id": "s2"})
    assert r.status_code == 201, r.text
    sid = r.json()["signal_id"]
    assert r.json()["understood"] is False and r.json()["feedback_id"] is None
    for body, status in [({}, 422), ({"text": "  "}, 422), ({"chips": ["nope"]}, 422),
                         ({"text": "x", "extra": 1}, 422),
                         ({"text": "x", "section_id": "s9"}, 422)]:
        assert client.post(f"{url}/feedback", headers=H("u1"), json=body).status_code == status
    assert client.post("/wiki/pages/pg_nope/feedback", headers=H("u1"),
                       json={"text": "x"}).status_code == 404

    edit = {"sections": [{"section_id": "s3", "body": "改了例子", "note_body": "改了例子"}],
            "summary": "改了例子"}
    r = client.post(f"{url}/hand-edit", headers=H("u1"), json=edit)
    assert r.status_code == 200, r.text
    assert r.json()["page"]["hand_edit"]["sections"] == ["s3"]
    for body, status in [({"summary": "x" * 2001}, 422), ({}, 422),
                         ({"sections": [{"section_id": "s9", "body": "b", "note_body": "n"}],
                           "summary": "x"}, 409)]:
        assert client.post(f"{url}/hand-edit", headers=H("u1"), json=body).status_code == status
    assert client.post("/wiki/pages/pg_nope/hand-edit", headers=H("u1"),
                       json=edit).status_code == 404
    undone = client.post(f"{url}/hand-edit/{r.json()['edit_id']}/undo", headers=H("u1"))
    assert undone.status_code == 200, undone.text
    assert undone.json()["page"]["hand_edit"] is None
    assert client.post(f"{url}/hand-edit/{r.json()['edit_id']}/undo",
                       headers=H("u1")).status_code == 409

    usage = PgRepo(services.pool).load("u1").state.usage
    assert [(u.action, u.heading, u.text) for u in usage[-2:]] == [
        ("整页反馈", "z 分数怎么读", "不像人话、太难：看不懂"), ("改了笔记", None, "改了例子")]
    out = signals(client)
    assert [(s.signal_id, s.kind, s.section_id, s.text) for s in out.items] == [
        (sid, "page_feedback", "s2", "看不懂"), (out.items[1].signal_id, "note_edit", "s3", "改了例子")]
    assert out.items[0].chips == ("unclear", "too_hard") and out.items[0].spec_version == 1
    assert out.next is None


def test_signals_paging_kinds_and_assistant_selection(wiki, services):
    client, script = wiki
    pid = organized_page(wiki)
    script.push(UNDERSTAND, *["not json"] * 3 * (1 + REPAIR_TURNS))
    for i in range(3):                     # same clock time: ordered by signal id
        assert client.post(f"/wiki/pages/{pid}/feedback", headers=H("u1"),
                           json={"text": f"第 {i} 条"}).status_code == 201
    store = PgSessionStore(services.pool)
    store.create(SessionInfo(session_id="as1", user_id="u1", page_id=pid, note_concept_id=None,
                             created_at=T0))
    store.append("as1", "ent1", "user", {"message_id": "m1", "text": "问",
                                         "selection": {"text": "低一个标准差",
                                                       "heading": "z 分数怎么读"}},
                 T0 + timedelta(hours=1))
    store.append("as1", "ent2", "user", {"message_id": "m2", "text": "问", "selection": None},
                 T0 + timedelta(hours=2))
    full = signals(client)
    assert [s.kind for s in full.items] == ["page_feedback"] * 3 + ["assistant_selection"]
    asel = full.items[-1]
    assert (asel.signal_id, asel.page_id, asel.section_id, asel.text, asel.spec_version) == (
        "asel:ent1", pid, "s2", "低一个标准差", None)
    assert PgRepo(services.pool).load("u1").state.signals_recorded == 3     # asel not stored

    first = signals(client, "?limit=2")
    assert len(first.items) == 2 and first.next is not None
    assert URL_SAFE.fullmatch(first.next)                    # goes into the URL as is
    second = signals(client, "?limit=2&after=" + first.next)
    assert [*first.items, *second.items] == full.items and second.next is None
    assert signals(client, "?after=" + second.cursor).items == []

    # committed after the reader's last call, stamped before everything it has seen (I1)
    page_feedback_flow(PgRepo(services.pool), "u1", pid, PageFeedbackIn(text="晚到的"),
                       now=T0 - timedelta(days=1), new_id=services.new_id)
    store.append("as1", "ent3", "user", {"message_id": "m3", "text": "问",
                                         "selection": {"text": "晚到的选中", "heading": None}},
                 T0 - timedelta(days=2))
    late = signals(client, "?after=" + second.cursor)
    assert [(s.kind, s.text) for s in late.items] == [
        ("assistant_selection", "晚到的选中"), ("page_feedback", "晚到的")]
    assert late.next is None and URL_SAFE.fullmatch(late.cursor)
    assert signals(client, "?after=" + late.cursor).items == []
    paged = signals(client, "?limit=1&kinds=assistant_selection&after=" + second.cursor)
    assert [s.text for s in paged.items] == ["晚到的选中"] and paged.next is None
    only = signals(client, "?kinds=assistant_selection")
    assert [s.signal_id for s in only.items] == ["asel:ent3", "asel:ent1"]     # by (at, id)
    assert [s.kind for s in signals(client, "?kinds=page_feedback").items] == ["page_feedback"] * 4
    for q in ("?kinds=nope", "?after=garbage", "?limit=0", "?limit=501", "?after=w1.a12",
              "?after=w1.a12-zz", "?after=w1.a12-6"):          # bad Pg assistant positions
        signals(client, q, status=422)
    assert client.get("/wiki/teach/signals", headers=H("ghost")).status_code == 404


def test_a_hand_edit_is_a_row_of_the_activity_log_and_its_undo_is_the_edits(wiki, services):
    client, _ = wiki
    ledger = ActionLedger(services.pool, services.clock, services.new_id, executors={})
    ledger.register(HAND_EDIT, HandEditExecutor(PgRepo(services.pool), services.clock, services.new_id))
    services.actions = ledger
    services.change_sources = (LedgerChangeSource(ledger),)
    pid = organized_page(wiki)
    edit = {"sections": [{"section_id": "s3", "body": "我的话", "note_body": "我的话"}], "summary": "改了"}
    url = f"/wiki/pages/{pid}/hand-edit"

    def rows():
        return [c for c in client.get("/wiki/rsi/changes", headers=H("u1")).json() if c["kind"] == "page_body"]

    def body():
        page = client.get(f"/wiki/pages/{pid}", headers=H("u1")).json()
        return next(s for s in page["sections"] if s["id"] == "s3")

    before = body()["body"]
    assert client.post(url, headers=H("u1"), json=edit).status_code == 200
    [row] = rows()
    assert row["status"] == "personal_accepted" and row["can_undo"] and "改了「" in row["summary"]
    r = client.post(f"/wiki/rsi/patches/{row['patch_id']}/undo", headers=H("u1"))
    assert r.status_code == 200, r.text
    assert body()["body"] == before and not body()["pinned"]
    assert rows()[0]["status"] == "rolled_back"
    # the top line's 撤销 moves the row too
    edit_id = client.post(url, headers=H("u1"), json=edit).json()["edit_id"]
    assert client.post(f"{url}/{edit_id}/undo", headers=H("u1")).status_code == 200
    assert [c["status"] for c in rows()] == ["rolled_back", "rolled_back"]
    # a section changed since: the row refuses, the newer text stays
    client.post(url, headers=H("u1"), json=edit)
    newer = {**edit, "sections": [{**edit["sections"][0], "body": "又改了", "note_body": "又改了"}]}
    client.post(url, headers=H("u1"), json=newer)
    older = next(c for c in rows() if c["status"] == "personal_accepted" and c["at"] == min(
        x["at"] for x in rows() if x["status"] == "personal_accepted"))
    assert client.post(f"/wiki/rsi/patches/{older['patch_id']}/undo", headers=H("u1")).status_code == 409
    assert body()["body"] == "又改了"


@pytest.mark.parametrize("operation", ["delete", "edit"])
def test_stale_hand_edit_version_has_machine_code_and_preserves_current_content(wiki, operation):
    client, _ = wiki
    pid = organized_page(wiki)
    url = f"/wiki/pages/{pid}"
    before = client.get(url, headers=H("u1")).json()
    old = next(section for section in before["sections"] if section["id"] == "s3")["fingerprint"]
    newer = {"section_id": "s3", "body": "NEWER CONTENT", "note_body": "NEWER CONTENT"}
    assert client.post(f"{url}/hand-edit", headers=H("u1"), json={"sections": [newer], "summary": "new"}).status_code == 200
    body = ({"removed": [{"section_id": "s3", "fingerprint": old}], "summary": "delete"}
            if operation == "delete" else {"sections": [{**newer, "body": "stale", "fingerprint": old}], "summary": "edit"})
    response = client.post(f"{url}/hand-edit", headers=H("u1"), json=body)
    assert response.status_code == 409, response.text
    assert response.json().get("code") == "hand_edit_stale", response.text
    current = client.get(url, headers=H("u1")).json()
    assert next(section for section in current["sections"] if section["id"] == "s3")["body"] == "NEWER CONTENT"
