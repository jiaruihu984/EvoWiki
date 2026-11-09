"""The assistant's page-edit proposals go through the action ledger (spec §4): one `proposed`
row per proposal, exactly-once execution with the page version checked, and the session's
`decision` entry written by the executor whichever door the user used (assistant routes or
参数 /wiki/bot/actions). Scripted LLM only; real Postgres."""

import json
import threading
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from evowiki.assistant.loop import _replace_proposal
from evowiki.assistant.pg_store import PgSessionStore
from evowiki.assistant.service import AssistantService
from evowiki.bot.actions import STALE_DETAIL, ActionLedger
from evowiki.bot.settings import DEFAULT_SETTINGS, BotSettings
from evowiki.llm.chat import ChatTurn, ToolCall
from evowiki.llm.gateway import Gateway
from evowiki.llm.log import CallLog
from evowiki.llm.scripted import ScriptedChatProvider
from evowiki.service.app import create_app
from evowiki.service.deps import build_assistant
from evowiki.store import bot as store_bot
from evowiki.store.db import fetch_all
from evowiki.wikiapp.events import PageSaved, WikiEvent
from evowiki.wikiapp.repo import Commit, PgRepo
from tests.runtime.helpers import FixedClock
from tests.service.conftest import H, Ids, parse_sse, signup
from tests.service.conftest import services as _services_fixture
from tests.service.test_assistant_wiring import ASK_REPLY, CFG
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW
from tests.wikiapp.test_page import organized

pytestmark = pytest.mark.usefixtures("every_skill")   # tools called without a load (stage 3)

services = _services_fixture      # reuse the service tests' fixture (pool, seeded runtime)
NEW_TEXT = "比均值低一个标准差。"


def test_a_failing_title_lookup_still_saves_the_proposal_with_the_heading(flow):
    f = flow

    def boom(user_id, page_id):
        raise RuntimeError("no title")

    store = PgSessionStore(f.services.pool, ledger=f.services.actions, page_title=boom)
    [entry] = [e for e in store.entries(f.sid) if e.type == "proposal"]
    payload = {**entry.payload, "proposal_id": "prop_x"}
    store.append(f.sid, "ent_x", "proposal", payload, entry.at)
    rows = {r["idempotency_key"]: r for r in _rows(f.services)}
    assert rows["assistant:prop_x"]["summary"] == f"添加一节「{payload['draft']['heading']}」"


ASK = BotSettings(levels={"page_body": "ask"})


def _flow(services, tmp_path, settings):
    client = TestClient(create_app(services))
    signup(client, "u1")
    with services.pool.connection() as conn:
        store_bot.put_settings(conn, "u1", settings, NOW)
    repo = PgRepo(services.pool)
    ids = Ids()
    page_id = organized((repo, Script(), ids))
    outs = iter([
        ChatTurn(tool_calls=(ToolCall(id="c2", name="read_page", args={}),)),
        # S3 tool tiers: with the ledger's acting tools the turn offers them by Skill, so the
        # model reads the page-editing reference before it proposes (as a real model does)
        ChatTurn(tool_calls=(ToolCall(id="c2r", name="read_ref", args={"path": "edit-wiki/pages"}),)),
        ChatTurn(tool_calls=(ToolCall(id="c3", name="propose_page_edit", args={
            "kind": "add_section", "heading": "补充：z=-1", "text": NEW_TEXT, "cue": "z=-1 表示什么？", "answer": "比均值低一个标准差。",
            "why": "刚问过", "paragraphs": []}),)),
        ChatTurn(text="好的。")])
    gw = Gateway(CFG, CallLog(tmp_path / "c.jsonl"), mode="record", providers={
        "scripted": ScriptedChatProvider("scripted", lambda m, r: next(outs),
                                         complete=lambda m, r: json.dumps(ASK_REPLY))})
    clock = FixedClock(NOW)
    services.actions = ledger = ActionLedger(services.pool, clock, ids, {})
    services.assistant = build_assistant(services.pool, gw, services.ctx.content,
                                         services.embedder, clock, ids, ledger=ledger)
    assert isinstance(services.assistant, AssistantService)
    sid = client.post("/assistant/sessions", json={"page_id": page_id},
                      headers=H("u1")).json()["session_id"]
    r = client.post(f"/assistant/sessions/{sid}/messages", json={"text": "z=-1 是什么意思？"},
                    headers=H("u1"))
    assert r.status_code == 200, r.text
    prop = next(d for e, d in parse_sse(r.text) if e == "proposal")["proposal_id"]
    return SimpleNamespace(client=client, services=services, repo=repo, ids=ids,
                           page_id=page_id, sid=sid, prop=prop)


@pytest.fixture
def flow(services, tmp_path):
    """The user's page_body level is `ask`: a proposal is a pending row from the start."""
    return _flow(services, tmp_path, ASK)


@pytest.fixture
def auto_flow(services, tmp_path):
    """The default level (`auto_undo`): the panel's 加进去 is the approval."""
    return _flow(services, tmp_path, DEFAULT_SETTINGS)


def _rows(services):
    with services.pool.connection() as conn:
        return fetch_all(conn, "select * from agent_action order by created_at")


def _decisions(services, sid):
    with services.pool.connection() as conn:
        rows = fetch_all(conn, "select payload from assistant_entry where session_id = %s "
                               "and type = 'decision' order by seq", (sid,))
    return [r["payload"] for r in rows]


def _last_section(repo, page_id):
    page = repo.load("u1").state.pages[page_id].page
    return page.outline.sections[-1].id, page


def test_a_proposal_creates_one_proposed_row(flow):
    f = flow
    [row] = _rows(f.services)
    assert (row["kind"], row["scope"], row["level"], row["status"], row["source"]) == (
        "assistant_edit", "page_body", "ask", "proposed", "assistant")
    assert row["idempotency_key"] == f"assistant:{f.prop}"
    assert "添加一节" in row["summary"] and row["page_id"] == f.page_id
    assert row["content"]["proposal_id"] == f.prop and row["content"]["session_id"] == f.sid
    assert row["content"]["text"] == NEW_TEXT and row["page_version"]
    assert f.client.get("/wiki/bot/badge", headers=H("u1")).json()["pending_actions"] == 1


def test_a_retried_proposal_append_mirrors_once(flow):
    """A durable turn's `assistant.append` step re-run after the row landed (B1c) returns the
    stored entry and leaves the ledger alone: still one row for the proposal."""
    f = flow
    store = PgSessionStore(f.services.pool, ledger=f.services.actions)
    [entry] = [e for e in store.entries(f.sid) if e.type == "proposal"]
    again = store.append(f.sid, entry.entry_id, "proposal", dict(entry.payload), entry.at)
    assert again == entry
    assert len(_rows(f.services)) == 1


def test_accept_executes_once_then_already_decided(flow):
    f = flow
    out = f.client.post(f"/assistant/proposals/{f.prop}/accept", headers=H("u1")).json()
    assert out["status"] == "accepted" and len(out["applied"]) == 1, out
    sec, page = _last_section(f.repo, f.page_id)
    assert sec == out["applied"][0] and page.texts[sec].body == NEW_TEXT
    [row] = _rows(f.services)
    assert row["status"] == "executed" and row["result"]["applied"] == [sec]
    assert out["action_id"] == row["action_id"] and out["can_undo"] is True      # the card's 撤销
    again = f.client.post(f"/assistant/proposals/{f.prop}/accept", headers=H("u1")).json()
    assert again["status"] == "already_decided"
    assert len(_decisions(f.services, f.sid)) == 1
    assert len(f.repo.load("u1").state.pages[f.page_id].page.outline.sections) == len(
        page.outline.sections)


def test_approve_from_the_parameters_page_logs_the_session_decision(flow):
    f = flow
    [row] = _rows(f.services)
    r = f.client.post(f"/wiki/bot/actions/{row['action_id']}/approve", headers=H("u1"))
    assert r.status_code == 200 and r.json()["status"] == "executed", r.text
    sec, page = _last_section(f.repo, f.page_id)
    assert page.texts[sec].body == NEW_TEXT
    assert [d["status"] for d in _decisions(f.services, f.sid)] == ["accepted"]
    assert _decisions(f.services, f.sid)[0]["applied"] == [sec]
    assert f.client.post(f"/assistant/proposals/{f.prop}/accept",
                       headers=H("u1")).json()["status"] == "already_decided"
    assert f.client.post(f"/assistant/proposals/{f.prop}/reject",
                       headers=H("u1")).json()["status"] == "already_decided"
    assert len(_decisions(f.services, f.sid)) == 1


def test_page_edited_elsewhere_after_the_proposal_still_takes_the_added_section(flow):
    """An added section is re-anchored onto the page as it is now (WikiApplier._reanchors): the
    learner's later edit of another section stays, the proposal applies."""
    f = flow
    snap = f.repo.load("u1")
    rec = snap.state.pages[f.page_id]
    sec = rec.page.outline.sections[0].id
    newer = rec.page.texts[sec].model_copy(update={"body": "用户之后的新版本"})
    rec2 = rec.model_copy(update={"page": rec.page.model_copy(
        update={"texts": {**rec.page.texts, sec: newer}})})
    f.repo.commit(snap, Commit(wiki=[WikiEvent(event_id=f.ids("wev"), ts=NOW,
                                             body=PageSaved(record=rec2, action="edit"))]))
    out = f.client.post(f"/assistant/proposals/{f.prop}/accept", headers=H("u1")).json()
    assert out["status"] == "accepted", out
    page = f.repo.load("u1").state.pages[f.page_id].page
    assert page.texts[sec].body == "用户之后的新版本"
    assert any(NEW_TEXT in t.body for t in page.texts.values())
    [row] = _rows(f.services)
    assert row["status"] == "executed"
    assert f.client.post(f"/assistant/proposals/{f.prop}/accept",
                       headers=H("u1")).json()["status"] == "already_decided"


def test_reject_from_the_parameters_page_logs_the_session_decision(flow):
    f = flow
    [row] = _rows(f.services)
    r = f.client.post(f"/wiki/bot/actions/{row['action_id']}/reject", headers=H("u1"))
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    assert [d["status"] for d in _decisions(f.services, f.sid)] == ["rejected"]
    assert f.client.post(f"/assistant/proposals/{f.prop}/accept",
                       headers=H("u1")).json()["status"] == "already_decided"
    _, page = _last_section(f.repo, f.page_id)
    assert all(t.body != NEW_TEXT for t in page.texts.values())


def test_assistant_reject_goes_through_the_ledger(flow):
    f = flow
    out = f.client.post(f"/assistant/proposals/{f.prop}/reject", headers=H("u1")).json()
    assert out["status"] == "rejected"
    assert _rows(f.services)[0]["status"] == "rejected"
    assert [d["status"] for d in _decisions(f.services, f.sid)] == ["rejected"]


def _undo(f, action_id):
    return f.client.post(f"/wiki/bot/actions/{action_id}/undo", headers=H("u1"))


def test_undo_of_an_asked_page_edit_restores_the_page(flow):
    f = flow
    before = f.repo.load("u1").state.pages[f.page_id].page
    [row] = _rows(f.services)
    f.client.post(f"/assistant/proposals/{f.prop}/accept", headers=H("u1"))
    [row] = _rows(f.services)
    assert row["undo"]["section_id"] and row["status"] == "executed"     # stored with the edit
    listed = f.client.get("/wiki/bot/actions", headers=H("u1")).json()
    assert [a["can_undo"] for a in listed] == [True]
    r = _undo(f, row["action_id"])
    assert r.status_code == 200 and r.json()["status"] == "undone", r.text
    assert f.repo.load("u1").state.pages[f.page_id].page == before
    assert _undo(f, row["action_id"]).status_code == 409                # once


def test_a_resumed_session_says_an_accepted_card_can_be_undone_until_it_was(flow):
    f = flow
    f.client.post(f"/assistant/proposals/{f.prop}/accept", headers=H("u1"))
    [row] = _rows(f.services)
    def state():
        [d] = f.client.get(f"/assistant/sessions/{f.sid}", headers=H("u1")).json()["decisions"]
        return d["status"], d["action_id"], d["can_undo"]
    assert state() == ("accepted", row["action_id"], True)
    _undo(f, row["action_id"])
    assert state() == ("undone", "", False)


def test_undo_restores_the_changed_marks_and_moves_page_and_row_together(flow):
    f = flow
    before = f.repo.load("u1").state.pages[f.page_id]
    f.client.post(f"/assistant/proposals/{f.prop}/accept", headers=H("u1"))
    mid = f.repo.load("u1").state.pages[f.page_id]
    assert mid.changed != before.changed                       # the edit marked its section
    [row] = _rows(f.services)
    assert "mark" not in row["undo"] and row["undo"]["prev_changed"] == before.changed
    assert _undo(f, row["action_id"]).json()["status"] == "undone"
    after = f.repo.load("u1").state.pages[f.page_id]
    assert after.changed == before.changed and after.page == before.page
    assert _rows(f.services)[0]["status"] == "undone"


def test_two_concurrent_undos_of_one_edit_run_the_executor_once(flow, monkeypatch):
    f = flow
    f.client.post(f"/assistant/proposals/{f.prop}/accept", headers=H("u1"))
    [row] = _rows(f.services)
    applier = f.services.actions._executors["assistant_edit"]._applier
    calls, entered, release = [], threading.Event(), threading.Event()
    real = applier.undo_edit

    def slow(*a, **k):
        calls.append(1)
        entered.set()
        assert release.wait(10)
        return real(*a, **k)
    monkeypatch.setattr(applier, "undo_edit", slow)
    out = []
    ts = [threading.Thread(target=lambda: out.append(_undo(f, row["action_id"]).status_code))
          for _ in range(2)]
    ts[0].start()
    assert entered.wait(10)
    ts[1].start()
    release.set()
    [t.join(15) for t in ts]
    assert sorted(out) == [200, 409] and len(calls) == 1
    page = f.repo.load("u1").state.pages[f.page_id].page
    assert all(t.body != NEW_TEXT for t in page.texts.values())


def test_undo_is_refused_when_the_page_changed_since_the_edit(flow):
    f = flow
    f.client.post(f"/assistant/proposals/{f.prop}/accept", headers=H("u1"))
    [row] = _rows(f.services)
    snap = f.repo.load("u1")
    rec = snap.state.pages[f.page_id]
    sec = rec.page.outline.sections[0].id
    newer = rec.page.texts[sec].model_copy(update={"body": "用户之后的新版本"})
    rec2 = rec.model_copy(update={"page": rec.page.model_copy(
        update={"texts": {**rec.page.texts, sec: newer}})})
    f.repo.commit(snap, Commit(wiki=[WikiEvent(event_id=f.ids("wev"), ts=NOW,
                                             body=PageSaved(record=rec2, action="edit"))]))
    r = _undo(f, row["action_id"])
    assert r.status_code == 409
    assert r.json()["code"] == "action_refused"
    assert r.json()["params"] == {}
    page = f.repo.load("u1").state.pages[f.page_id].page
    assert page == rec2.page                                              # nothing overwritten
    assert _rows(f.services)[0]["status"] == "executed"


def test_default_level_creates_no_pending_row_and_accept_records_an_executed_one(auto_flow):
    f = auto_flow
    assert _rows(f.services) == []
    assert f.client.get("/wiki/bot/badge", headers=H("u1")).json()["pending_actions"] == 0
    before = f.repo.load("u1").state.pages[f.page_id].page
    out = f.client.post(f"/assistant/proposals/{f.prop}/accept", headers=H("u1")).json()
    assert out["status"] == "accepted" and len(out["applied"]) == 1, out
    [row] = _rows(f.services)
    assert (row["status"], row["level"], row["scope"]) == ("executed", "auto_undo", "page_body")
    assert row["expires_at"] is None and row["undo"]["section_id"] == out["applied"][0]
    assert [d["status"] for d in _decisions(f.services, f.sid)] == ["accepted"]
    again = f.client.post(f"/assistant/proposals/{f.prop}/accept", headers=H("u1")).json()
    assert again["status"] == "already_decided" and len(_rows(f.services)) == 1
    assert _undo(f, row["action_id"]).json()["status"] == "undone"
    assert f.repo.load("u1").state.pages[f.page_id].page == before


def test_default_level_decline_leaves_no_row_but_logs_the_decision(auto_flow):
    f = auto_flow
    out = f.client.post(f"/assistant/proposals/{f.prop}/reject", headers=H("u1")).json()
    assert out["status"] == "rejected" and _rows(f.services) == []
    assert [d["status"] for d in _decisions(f.services, f.sid)] == ["rejected"]
    assert f.client.post(f"/assistant/proposals/{f.prop}/accept",
                         headers=H("u1")).json()["status"] == "already_decided"


def test_an_old_proposal_without_a_ledger_row_is_proposed_on_decide(flow):
    f = flow
    [row] = _rows(f.services)
    old = PgSessionStore(f.services.pool)          # no ledger: stores the entry like before B3
    old.append(f.sid, "ent_old", "proposal",
               {"proposal_id": "prop_old", "message_id": "msg_old", "draft": row["content"]}, NOW)
    assert len(_rows(f.services)) == 1
    out = f.client.post("/assistant/proposals/prop_old/accept", headers=H("u1")).json()
    assert out["status"] == "accepted", out
    got = {r["idempotency_key"]: r for r in _rows(f.services)}
    assert got["assistant:prop_old"]["status"] == "executed"
    assert got["assistant:prop_old"]["page_version"] is None
    assert got[f"assistant:{f.prop}"]["status"] == "proposed"


class _RaceExecutor:
    """Wraps the real executor: the first approve (A) starts a second one (B) first; B gets past
    the ledger's status check, then waits until A has committed the page (and before A's session
    decision exists) before it runs its own execute (which sees a newer page)."""

    def __init__(self, real, ledger, user_id, action_id):
        self.real, self.ledger, self.user_id, self.action_id = real, ledger, user_id, action_id
        self.calls, self.b_in, self.a_done, self.b_out = 0, threading.Event(), threading.Event(), []

    def execute(self, user_id, action, effect):
        self.calls += 1
        if self.calls == 1:                                   # A
            t = threading.Thread(target=lambda: self.b_out.append(
                self.ledger.approve(self.user_id, self.action_id)))
            t.start()
            assert self.b_in.wait(10)
            out = self.real.execute(user_id, action, effect)
            self.a_done.set()
            t.join(10)
            return out
        self.b_in.set()                                       # B
        assert self.a_done.wait(10)
        return self.real.execute(user_id, action, effect)

    def decided(self, user_id, action):
        self.real.decided(user_id, action)

    def rejected(self, user_id, action):
        self.real.rejected(user_id, action)

    def undo(self, user_id, action):
        return self.real.undo(user_id, action)


def test_racing_approve_that_loses_does_not_write_a_failed_session_decision(flow):
    f = flow
    [row] = _rows(f.services)
    ledger = f.services.actions
    race = _RaceExecutor(ledger._executors["assistant_edit"], ledger, "u1", row["action_id"])
    ledger.register("assistant_edit", race)
    a = ledger.approve("u1", row["action_id"])
    assert a.status == "executed" and race.calls == 2
    assert [r.status for r in race.b_out] == ["executed"]
    decisions = _decisions(f.services, f.sid)
    assert [d["status"] for d in decisions] == ["accepted"], decisions
    _, page = _last_section(f.repo, f.page_id)
    assert sum(t.body == NEW_TEXT for t in page.texts.values()) == 1


def test_append_decision_once_is_atomic_per_proposal(flow):
    f = flow
    store = PgSessionStore(f.services.pool)
    def payload():
        return {"proposal_id": "p_x", "status": "accepted", "applied": [], "detail": ""}

    results = []
    ts = [threading.Thread(target=lambda i=i: results.append(
        store.append_decision_once(f.sid, f"ent_x{i}", payload(), NOW))) for i in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(results) == [False, False, False, True]


def test_an_old_pending_proposal_never_expires(flow):
    f = flow
    with f.services.pool.connection() as conn:
        conn.execute("update agent_action set created_at = %s", (NOW - timedelta(days=400),))
    [row] = _rows(f.services)
    assert row["expires_at"] is None
    out = f.client.post(f"/assistant/proposals/{f.prop}/accept", headers=H("u1")).json()
    assert out["status"] == "accepted", out


def test_reject_of_a_row_stuck_approved_is_already_decided(flow):
    f = flow
    with f.services.pool.connection() as conn:
        conn.execute("update agent_action set status = 'approved'")
    out = f.client.post(f"/assistant/proposals/{f.prop}/reject", headers=H("u1")).json()
    assert out["status"] == "already_decided", out


def test_a_resumed_session_lists_its_proposals_with_the_ledger_state(flow, monkeypatch):
    f = flow
    body = f.client.get(f"/assistant/sessions/{f.sid}", headers=H("u1")).json()
    assert [p["proposal_id"] for p in body["proposals"]] == [f.prop]
    assert body["proposals"][0]["draft"]["text"] == NEW_TEXT and body["decisions"] == []
    [row] = _rows(f.services)
    from evowiki.assistant.edit_executor import AssistantEditExecutor
    monkeypatch.setattr(AssistantEditExecutor, "mirror", lambda *a, **k: None)   # lost mirror
    assert f.client.post(f"/wiki/bot/actions/{row['action_id']}/reject",
                         headers=H("u1")).status_code == 200
    assert _decisions(f.services, f.sid) == []
    body = f.client.get(f"/assistant/sessions/{f.sid}", headers=H("u1")).json()
    assert [(d["proposal_id"], d["status"]) for d in body["decisions"]] == [(f.prop, "rejected")]
    listed = f.client.get("/assistant/sessions", headers=H("u1")).json()["sessions"]
    assert [s["session_id"] for s in listed] == [f.sid] and listed[0]["message_count"] == 2


def _draft_payload(f, pid, sec, text):
    """A rewrite of `sec` drafted now, as a turn stores it (page and section fingerprints of this moment)."""
    from evowiki.assistant.wiki import page_fingerprint, section_fingerprint
    rec = f.repo.load("u1").state.pages[f.page_id]
    return {"proposal_id": pid, "message_id": f"msg_{pid}", "change": None,
            "draft": {"kind": "rewrite_section", "page_id": f.page_id, "section_id": sec, "heading": "h",
                      "cells": [], "text": text, "why": "w", "cue": "问", "answer": "答"},
            "page_version": page_fingerprint(rec), "section_version": section_fingerprint(rec, sec)}


def _store_draft(f, payload):
    PgSessionStore(f.services.pool, ledger=f.services.actions, page_title=lambda u, p: "页").append(
        f.sid, f"ent_{payload['proposal_id']}", "proposal", payload, NOW)


def _accept(f, pid):
    return f.client.post(f"/assistant/proposals/{pid}/accept", headers=H("u1")).json()


def _body(f, sec):
    return f.repo.load("u1").state.pages[f.page_id].page.texts[sec].body


def _first_section(f):
    return f.repo.load("u1").state.pages[f.page_id].page.outline.sections[0].id


def test_a_draft_from_an_earlier_turn_never_overwrites_a_newer_draft_he_confirmed(flow):
    f, sec = flow, _first_section(flow)
    _store_draft(f, _draft_payload(f, "prop_old", sec, "上一轮的草稿"))
    _store_draft(f, _draft_payload(f, "prop_new", sec, "这一轮的草稿"))
    assert _accept(f, "prop_new")["status"] == "accepted"
    out = _accept(f, "prop_old")
    assert out["status"] != "accepted" and STALE_DETAIL in (out["detail"] or "")
    assert _body(f, sec).startswith("这一轮的草稿")


def test_a_draft_never_overwrites_what_he_edited_by_hand_after_it(flow):
    f, sec = flow, _first_section(flow)
    _store_draft(f, _draft_payload(f, "prop_old", sec, "上一轮的草稿"))
    snap = f.repo.load("u1")
    rec = snap.state.pages[f.page_id]
    mine = rec.page.texts[sec].model_copy(update={"body": "我手改的"})
    rec2 = rec.model_copy(update={"page": rec.page.model_copy(update={"texts": {**rec.page.texts, sec: mine}})})
    f.repo.commit(snap, Commit(wiki=[WikiEvent(event_id=f.ids("wev"), ts=NOW, body=PageSaved(record=rec2, action="edit"))]))
    out = _accept(f, "prop_old")
    assert out["status"] == "failed" and out["detail"] == STALE_DETAIL
    assert _body(f, sec) == "我手改的"


def test_a_replaced_draft_is_rejected_on_its_ledger_row_and_cannot_be_confirmed(flow):
    f, sec = flow, _first_section(flow)
    _store_draft(f, _draft_payload(f, "prop_old", sec, "第一版"))
    deps = SimpleNamespace(ledger=f.services.actions, store=PgSessionStore(f.services.pool), new_id=f.ids,
                           clock=lambda: NOW)
    _replace_proposal(deps, SimpleNamespace(user_id="u1", session_id=f.sid), "prop_old")
    row = next(r for r in _rows(f.services) if r["idempotency_key"] == "assistant:prop_old")
    assert row["status"] == "rejected" and row["detail"] == "replaced"
    assert [d["detail"] for d in _decisions(f.services, f.sid) if d["proposal_id"] == "prop_old"] == ["replaced"]
    before = _body(f, sec)
    assert _accept(f, "prop_old")["status"] == "already_decided"
    assert _body(f, sec) == before
