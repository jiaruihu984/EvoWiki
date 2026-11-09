import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from evowiki.assistant.contracts import DECISION_INTERNAL, MessageIn, SessionCreateIn
from evowiki.assistant.loop import TurnDeps
from evowiki.assistant.service import AssistantService, SessionBusy
from evowiki.assistant.session import MemorySessionStore
from evowiki.assistant.wiki import STALE_DETAIL, ApplyRefused
from evowiki.llm.chat import ChatTurn, ToolCall
from evowiki.llm.gateway import Gateway
from evowiki.llm.log import CallLog
from evowiki.llm.models import GatewayConfig, ModelRef
from evowiki.llm.scripted import ScriptedChatProvider
from evowiki.llm.usage import Account, BudgetExceededError
from evowiki.service.app import create_app
from tests.assistant.fakes import T0, Data, Ids, tools

REF = ModelRef(provider="scripted", model="m")
U1, U2 = {"X-EvoWiki-User": "u1"}, {"X-EvoWiki-User": "u2"}


class Applier:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail     # fail: False, True, or the exception to raise

    def apply(self, user_id, draft):
        self.calls.append((user_id, draft))
        if isinstance(self.fail, BaseException):
            raise self.fail
        if self.fail:
            raise RuntimeError("patch rejected")
        return ("s9",)


def service(tmp_path, script, applier=None):
    outs = iter(script)

    def answer(m, r):
        out = next(outs)
        if isinstance(out, BaseException):
            raise out
        return out
    gw = Gateway(GatewayConfig(generator=REF, verifier=REF, allow_single_provider=True),
                 CallLog(tmp_path / "c.jsonl"), mode="record",
                 providers={"scripted": ScriptedChatProvider("scripted", answer)})
    deps = TurnDeps(gateway=gw, tools=tools(), store=MemorySessionStore(), data=Data(),
                    new_id=Ids(), clock=lambda: T0)
    return AssistantService(deps, applier or Applier())


def make(tmp_path, script, applier=None, assistant=True):
    # create_app only stores the services object and mounts routers, so a namespace with just
    # `assistant` is enough here; this also checks app.py mounts the router and maps errors.
    return TestClient(create_app(SimpleNamespace(
        assistant=service(tmp_path, script, applier) if assistant else None)))


def events(resp):
    out = []
    for block in resp.text.strip().split("\n\n"):
        ev, data = block.split("\n")
        out.append((ev.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return out


def test_full_round_trip(tmp_path):
    c = make(tmp_path, [ChatTurn(tool_calls=(ToolCall(id="c1", name="propose_page_edit"),)),
                        ChatTurn(text="建议加一段")])
    sid = c.post("/assistant/sessions", json={"page_id": "p1"}, headers=U1).json()["session_id"]
    r = c.post(f"/assistant/sessions/{sid}/messages", json={"text": "加进页面"}, headers=U1)
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache"          # FastAPI's SSE headers
    assert r.headers["x-accel-buffering"] == "no"
    evs = events(r)
    assert [e for e, _ in evs][-1:] == ["done"]
    pid = next(d for e, d in evs if e == "proposal")["proposal_id"]
    s = c.get(f"/assistant/sessions/{sid}", headers=U1).json()
    assert [m["role"] for m in s["messages"]] == ["user", "assistant"]
    assert s["messages"][1]["proposals"] == [pid]
    ok = c.post(f"/assistant/proposals/{pid}/accept", headers=U1).json()
    assert ok["status"] == "accepted" and ok["applied"] == ["s9"]
    again = c.post(f"/assistant/proposals/{pid}/reject", headers=U1).json()
    assert again["status"] == "already_decided"


def test_session_replays_the_selection_on_the_user_message(tmp_path):
    c = make(tmp_path, [ChatTurn(text="答一"), ChatTurn(text="答二")])
    sid = c.post("/assistant/sessions", json={"page_id": "p1"}, headers=U1).json()["session_id"]
    sel = {"text": "z=-1 表示比均值低一个标准差", "heading": "z 分数怎么读"}
    c.post(f"/assistant/sessions/{sid}/messages", json={"text": "这句什么意思", "selection": sel},
           headers=U1)
    c.post(f"/assistant/sessions/{sid}/messages", json={"text": "再问"}, headers=U1)
    msgs = c.get(f"/assistant/sessions/{sid}", headers=U1).json()["messages"]
    assert [(m["role"], m["selection"]) for m in msgs] == [
        ("user", {**sel, "artifact_id": None, "kind": "text"}), ("assistant", None), ("user", None),
        ("assistant", None)]


def test_failed_apply_can_be_retried(tmp_path):
    ap = Applier(fail=True)
    c = make(tmp_path, [ChatTurn(tool_calls=(ToolCall(id="c1", name="propose_page_edit"),)),
                        ChatTurn(text="建议")], applier=ap)
    sid = c.post("/assistant/sessions", json={"page_id": "p1"}, headers=U1).json()["session_id"]
    pid = next(d for e, d in events(c.post(f"/assistant/sessions/{sid}/messages",
                                           json={"text": "加"}, headers=U1))
               if e == "proposal")["proposal_id"]
    assert c.post(f"/assistant/proposals/{pid}/accept", headers=U1).json()["status"] == "failed"
    ap.fail = False
    assert c.post(f"/assistant/proposals/{pid}/accept", headers=U1).json()["status"] == "accepted"


def _proposal(c):
    sid = c.post("/assistant/sessions", json={"page_id": "p1"}, headers=U1).json()["session_id"]
    pid = next(d for e, d in events(c.post(f"/assistant/sessions/{sid}/messages",
                                           json={"text": "加"}, headers=U1))
               if e == "proposal")["proposal_id"]
    return sid, pid


def _decisions(c, sid):
    return c.get(f"/assistant/sessions/{sid}", headers=U1).json()["decisions"]


def test_unexpected_apply_error_detail_is_the_internal_code_and_comes_back_on_get(tmp_path):
    ap = Applier(fail=KeyError("sec_42 secret"))
    c = make(tmp_path, [ChatTurn(tool_calls=(ToolCall(id="c1", name="propose_page_edit"),)),
                        ChatTurn(text="建议")], applier=ap)
    sid, pid = _proposal(c)
    r = c.post(f"/assistant/proposals/{pid}/accept", headers=U1).json()
    assert r["status"] == "failed" and r["detail"] == DECISION_INTERNAL == "internal"
    assert _decisions(c, sid) == [{"proposal_id": pid, "status": "failed", "applied": [],
                                   "detail": "internal", "action_id": "", "can_undo": False}]
    # the GET also returns the proposal itself (id + draft), so a resumed session shows the same card
    props = c.get(f"/assistant/sessions/{sid}", headers=U1).json()["proposals"]
    assert [p["proposal_id"] for p in props] == [pid] and props[0]["draft"]["page_id"] == "p1"
    # retry succeeds -> the latest decision replaces the failed one
    ap.fail = False
    assert c.post(f"/assistant/proposals/{pid}/accept", headers=U1).json()["status"] == "accepted"
    assert _decisions(c, sid) == [{"proposal_id": pid, "status": "accepted", "applied": ["s9"],
                                   "detail": "", "action_id": "", "can_undo": False}]


def test_apply_refused_reason_is_the_detail_on_post_and_get(tmp_path):
    ap = Applier(fail=ApplyRefused(STALE_DETAIL))
    c = make(tmp_path, [ChatTurn(tool_calls=(ToolCall(id="c1", name="propose_page_edit"),)),
                        ChatTurn(text="建议")], applier=ap)
    sid, pid = _proposal(c)
    r = c.post(f"/assistant/proposals/{pid}/accept", headers=U1).json()
    assert r["status"] == "failed" and r["detail"] == STALE_DETAIL
    assert _decisions(c, sid) == [{"proposal_id": pid, "status": "failed", "applied": [],
                                   "detail": STALE_DETAIL, "action_id": "", "can_undo": False}]


def test_reject_comes_back_on_get(tmp_path):
    c = make(tmp_path, [ChatTurn(tool_calls=(ToolCall(id="c1", name="propose_page_edit"),)),
                        ChatTurn(text="建议")])
    sid, pid = _proposal(c)
    assert _decisions(c, sid) == []
    assert c.post(f"/assistant/proposals/{pid}/reject", headers=U1).json()["status"] == "rejected"
    assert _decisions(c, sid) == [{"proposal_id": pid, "status": "rejected", "applied": [],
                                   "detail": "", "action_id": "", "can_undo": False}]


def test_budget_exceeded_while_applying_is_429_and_logs_no_decision(tmp_path):
    ap = Applier(fail=BudgetExceededError(Account(user_id="u1", group="assistant"), "day", 1.0))
    c = make(tmp_path, [ChatTurn(tool_calls=(ToolCall(id="c1", name="propose_page_edit"),)),
                        ChatTurn(text="建议")], applier=ap)
    sid, pid = _proposal(c)
    assert c.post(f"/assistant/proposals/{pid}/accept", headers=U1).status_code == 429
    assert _decisions(c, sid) == []


def test_other_users_get_403_unknown_gets_404(tmp_path):
    c = make(tmp_path, [])
    sid = c.post("/assistant/sessions", json={}, headers=U1).json()["session_id"]
    assert c.get(f"/assistant/sessions/{sid}", headers=U2).status_code == 403
    assert c.post(f"/assistant/sessions/{sid}/messages", json={"text": "x"},
                  headers=U2).status_code == 403
    assert c.get("/assistant/sessions/nope", headers=U1).status_code == 404
    assert c.post("/assistant/sessions", json={"page_id": "nope"}, headers=U1).status_code == 404


def test_bad_body_is_422_and_missing_assistant_is_503(tmp_path):
    c = make(tmp_path, [])
    sid = c.post("/assistant/sessions", json={}, headers=U1).json()["session_id"]
    assert c.post(f"/assistant/sessions/{sid}/messages", json={"text": ""},
                  headers=U1).status_code == 422
    assert make(tmp_path, [], assistant=False).post(
        "/assistant/sessions", json={}, headers=U1).status_code == 503


def test_other_user_cannot_decide_and_unknown_proposal_is_404(tmp_path):
    ap = Applier()
    c = make(tmp_path, [ChatTurn(tool_calls=(ToolCall(id="c1", name="propose_page_edit"),)),
                        ChatTurn(text="建议")], applier=ap)
    sid = c.post("/assistant/sessions", json={"page_id": "p1"}, headers=U1).json()["session_id"]
    pid = next(d for e, d in events(c.post(f"/assistant/sessions/{sid}/messages",
                                           json={"text": "加"}, headers=U1))
               if e == "proposal")["proposal_id"]
    assert c.post(f"/assistant/proposals/{pid}/accept", headers=U2).status_code == 403
    assert c.post("/assistant/proposals/nope/accept", headers=U1).status_code == 404
    assert ap.calls == []
    assert c.post(f"/assistant/proposals/{pid}/reject", headers=U1).json()["status"] == "rejected"
    assert c.post(f"/assistant/proposals/{pid}/accept", headers=U1).json()["status"] == \
        "already_decided"
    assert ap.calls == []


def test_second_send_while_a_turn_runs_is_busy(tmp_path):
    svc = service(tmp_path, [ChatTurn(text="答一"), ChatTurn(text="答二")])
    sid = svc.create_session("u1", SessionCreateIn()).session_id
    first = svc.send("u1", sid, MessageIn(text="问一"))
    next(first)
    with pytest.raises(SessionBusy):
        svc.send("u1", sid, MessageIn(text="问二"))
    first.close()
    second = svc.send("u1", sid, MessageIn(text="问二"))
    assert [e.type for e in second][-1] == "done"


def test_busy_session_maps_to_409(tmp_path):
    svc = service(tmp_path, [ChatTurn(text="答一")])
    c = TestClient(create_app(SimpleNamespace(assistant=svc)))
    sid = c.post("/assistant/sessions", json={}, headers=U1).json()["session_id"]
    first = svc.send("u1", sid, MessageIn(text="问一"))
    next(first)
    r = c.post(f"/assistant/sessions/{sid}/messages", json={"text": "x"}, headers=U1)
    assert r.status_code == 409 and r.json()["code"] == "session_busy"
    first.close()


def test_unstarted_stream_releases_the_lock_when_dropped(tmp_path):
    svc = service(tmp_path, [ChatTurn(text="答")])
    sid = svc.create_session("u1", SessionCreateIn()).session_id
    first = svc.send("u1", sid, MessageIn(text="问一"))
    del first                       # never iterated (e.g. client gone before streaming)
    assert [e.type for e in svc.send("u1", sid, MessageIn(text="问二"))][-1] == "done"


def test_decision_while_a_turn_runs_is_busy(tmp_path):
    svc = service(tmp_path, [ChatTurn(tool_calls=(ToolCall(id="c1", name="propose_page_edit"),)),
                             ChatTurn(text="建议"), ChatTurn(text="答二")])
    sid = svc.create_session("u1", SessionCreateIn(page_id="p1")).session_id
    pid = next(e.proposal_id for e in svc.send("u1", sid, MessageIn(text="加"))
               if e.type == "proposal")
    turn = svc.send("u1", sid, MessageIn(text="问二"))
    next(turn)
    with pytest.raises(SessionBusy):
        svc.decide("u1", pid, accept=True)
    turn.close()
    assert svc.decide("u1", pid, accept=True).status == "accepted"


def test_client_disconnect_mid_stream_closes_the_turn_and_frees_the_session(tmp_path):
    from tests.service.conftest import leave_mid_stream

    svc = service(tmp_path, [ChatTurn(text="答" * 500), ChatTurn(text="答二")])
    sid = svc.create_session("u1", SessionCreateIn()).session_id
    app = create_app(SimpleNamespace(assistant=svc))
    sent = leave_mid_stream(app, "POST", f"/assistant/sessions/{sid}/messages", headers=U1,
                            body=json.dumps({"text": "问一"}).encode())
    assert sent[0]["status"] == 200 and any(m.get("body") for m in sent[1:])
    # the lock must already be free when the response has ended
    second = svc.send("u1", sid, MessageIn(text="问二"))
    assert [e.type for e in second][-1] == "done"
    entries = [e.type for e in svc._deps.store.entries(sid)]
    assert entries.count("assistant") == 2


def test_provider_transient_error_ends_turn_with_retryable_llm_unavailable(tmp_path):
    from evowiki.llm.providers import ProviderTransientError
    c = make(tmp_path, [ProviderTransientError("deepseek request failed: APITimeoutError")])
    sid = c.post("/assistant/sessions", json={}, headers=U1).json()["session_id"]
    ev = events(c.post(f"/assistant/sessions/{sid}/messages", json={"text": "hi"}, headers=U1))
    assert ev[-1][0] == "error"
    assert (ev[-1][1]["code"], ev[-1][1]["retryable"]) == ("llm_unavailable", True)


def test_provider_rejected_error_ends_turn_with_non_retryable_llm_unavailable(tmp_path):
    from evowiki.llm.providers import ProviderRejectedError
    c = make(tmp_path, [ProviderRejectedError("deepseek rejected: HTTP 402", status_code=402)])
    sid = c.post("/assistant/sessions", json={}, headers=U1).json()["session_id"]
    ev = events(c.post(f"/assistant/sessions/{sid}/messages", json={"text": "hi"}, headers=U1))
    assert ev[-1][0] == "error"
    assert (ev[-1][1]["code"], ev[-1][1]["retryable"]) == ("llm_unavailable", False)
    assert "HTTP 402" in ev[-1][1]["detail"]


def _fake_turn_client(turn):
    from evowiki.assistant.contracts import SessionCreateOut  # noqa: F401
    fake = SimpleNamespace(send=lambda uid, sid, body: turn)
    return TestClient(create_app(SimpleNamespace(assistant=fake)))


def test_turn_iterator_raising_still_ends_with_error_event():
    from evowiki.assistant.contracts import TextDelta

    def turn():
        yield TextDelta(text="partial")
        raise RuntimeError("boom")

    ev = events(_fake_turn_client(turn()).post("/assistant/sessions/s/messages",
                                               json={"text": "x"}, headers=U1))
    assert [e for e, _ in ev] == ["text_delta", "error"]
    assert (ev[-1][1]["code"], ev[-1][1]["retryable"]) == ("internal", True)


def test_turn_iterator_ending_without_done_gets_error_event():
    ev = events(_fake_turn_client(iter([])).post("/assistant/sessions/s/messages",
                                                 json={"text": "x"}, headers=U1))
    assert [e for e, _ in ev] == ["error"]
    assert ev[-1][1]["detail"] == "stream ended without done"


# ---- GET /assistant/sessions (history list) ----

def _fill(store, sid, uid, page_id, texts, base_s, selection=None):
    from datetime import timedelta

    from evowiki.assistant.session import SessionInfo
    store.create(SessionInfo(session_id=sid, user_id=uid, page_id=page_id, note_concept_id=None,
                             created_at=T0 + timedelta(seconds=base_s)))
    n = 0
    for i, t in enumerate(texts):
        for typ, p in (("user", {"message_id": f"{sid}m{i}", "text": t, "selection": selection}),
                       ("assistant", {"message_id": f"{sid}a{i}", "text": f"回答{t}",
                                      "sources": [], "proposals": []})):
            n += 1
            store.append(sid, f"{sid}e{n}", typ, p, T0 + timedelta(seconds=base_s + n))


def test_list_sessions_order_filters_and_fields(tmp_path):
    from evowiki.assistant.session import SessionInfo
    c = make(tmp_path, [])
    st = c.app.state.services.assistant._deps.store
    _fill(st, "a", "u1", "p1", ["什么是 z 分数？"], 0)
    _fill(st, "b", "u1", "gone", ["第二个。", "追问"], 100)           # newest; page missing
    _fill(st, "c", "u2", None, ["别人的"], 50)                        # other user
    st.create(SessionInfo(session_id="e", user_id="u1", page_id=None, note_concept_id=None,
                          created_at=T0))                             # empty -> hidden
    _fill(st, "d", "u1", None, ["a" * 40 + "?"], 10)
    body = c.get("/assistant/sessions", headers=U1).json()
    assert [s["session_id"] for s in body["sessions"]] == ["b", "d", "a"]
    b, d, a = body["sessions"]
    assert set(a) == {"session_id", "title", "page_id", "page_title", "note_concept_id",
                      "created_at", "last_activity_at", "message_count"}
    assert (a["title"], a["page_title"], a["message_count"]) == ("什么是 z 分数", "z 分数", 2)
    assert (b["title"], b["page_title"], b["message_count"]) == ("第二个", "", 4)
    assert d["title"] == "a" * 24                                      # capped at 24
    assert b["last_activity_at"] > b["created_at"]
    assert [s["session_id"] for s in c.get("/assistant/sessions?limit=1",
                                           headers=U1).json()["sessions"]] == ["b"]
    assert c.get("/assistant/sessions?limit=0", headers=U1).status_code == 422
    assert [s["session_id"] for s in c.get("/assistant/sessions", headers=U2).json()["sessions"]] \
        == ["c"]


def test_list_sessions_query_matches_title_page_title_and_message_text(tmp_path):
    c = make(tmp_path, [])
    st = c.app.state.services.assistant._deps.store
    _fill(st, "a", "u1", "p1", ["Alpha"], 0)
    _fill(st, "b", "u1", None, ["第二个", "追问XYZ"], 10)

    def ids(q):
        return [s["session_id"] for s in c.get("/assistant/sessions", params={"q": q},
                                               headers=U1).json()["sessions"]]
    assert ids("ALPH") == ["a"]                  # title, case-insensitive
    assert ids("分数") == ["a"]                  # page title
    assert ids("xyz") == ["b"]                   # user message text
    assert ids("回答追问") == ["b"]              # assistant message text
    assert ids("   ") == ["b", "a"]              # blank = no filter
    assert ids("nope") == []


def test_list_sessions_quote_only_title_and_503(tmp_path):
    c = make(tmp_path, [])
    st = c.app.state.services.assistant._deps.store
    _fill(st, "q", "u1", None, [""], 0, selection={"text": "被引用的句子", "heading": "h"})
    assert c.get("/assistant/sessions", headers=U1).json()["sessions"][0]["title"] == "被引用的句子"
    assert make(tmp_path, [], assistant=False).get("/assistant/sessions",
                                                   headers=U1).status_code == 503


def test_a_durable_assistant_without_a_launched_dbos_is_503(tmp_path):
    from evowiki.assistant.turn_runs import DbosTurnRuns
    svc = service(tmp_path, [ChatTurn(text="答")])
    svc = AssistantService(svc.turn_deps, Applier(), DbosTurnRuns())
    c = TestClient(create_app(SimpleNamespace(assistant=svc)))
    sid = c.post("/assistant/sessions", json={"page_id": "p1"}, headers=U1).json()["session_id"]
    r = c.post(f"/assistant/sessions/{sid}/messages", json={"text": "问"}, headers=U1)
    assert r.status_code == 503 and r.json() == {"detail": "请切换模型重试", "code": "AssistantUnavailable", "params": {}}
    assert c.get(f"/assistant/sessions/{sid}", headers=U1).status_code == 200   # reads still work


# ---- resume by SSE id (spec 2026-10-15 §3) ----

class _Cursored:
    """A durable turn's follower as the route sees it: events plus a moving `cursor`."""

    def __init__(self, items, message_id="msg_1"):
        self._items = iter(items)
        self.cursor = 0
        self.message_id = message_id

    def __iter__(self):
        return self

    def __next__(self):
        ev, moves = next(self._items)
        self.cursor += moves
        return ev


def _blocks(resp):
    return [dict(line.split(": ", 1) for line in b.splitlines())
            for b in resp.text.strip().split("\n\n")]


def test_a_durable_turn_carries_its_resume_id_and_resumes_from_it():
    from evowiki.assistant.contracts import Done, TextDelta

    seen = {}

    def resume(uid, sid, mid, after):
        seen["args"] = (uid, sid, mid, after)
        t = _Cursored([(TextDelta(at=0, text="答案"), 1), (Done(message_id="a", tool_calls=0,
                                                              llm_calls=1), 1)], mid)
        t.cursor = after
        return t

    live = [(TextDelta(at=0, text="答"), 0), (TextDelta(at=0, text="答案"), 1),
            (Done(message_id="a", tool_calls=0, llm_calls=1), 1)]
    fake = SimpleNamespace(send=lambda uid, sid, body: _Cursored(live), resume=resume)
    c = TestClient(create_app(SimpleNamespace(assistant=fake)))
    blocks = _blocks(c.post("/assistant/sessions/s/messages", json={"text": "x"}, headers=U1))
    assert [b["id"] for b in blocks] == ["msg_1:0", "msg_1:0", "msg_1:1", "msg_1:2"]   # the resume point at once; live keeps 0
    r = c.get("/assistant/sessions/s/events", params={"last_event_id": "msg_1:1"}, headers=U1)
    assert seen["args"] == ("u1", "s", "msg_1", 1)
    assert [b["id"] for b in _blocks(r)] == ["msg_1:2", "msg_1:3"]
    # the standard header works too
    c.get("/assistant/sessions/s/events", headers={**U1, "Last-Event-ID": "msg_1:0"})
    assert seen["args"] == ("u1", "s", "msg_1", 0)


def test_resume_needs_a_well_formed_id_and_an_inline_assistant_has_nothing_to_resume(tmp_path):
    c = make(tmp_path, [ChatTurn(text="答")])
    sid = c.post("/assistant/sessions", json={"page_id": "p1"}, headers=U1).json()["session_id"]
    assert c.get(f"/assistant/sessions/{sid}/events", headers=U1).status_code == 422
    for bad in ("nocolon", "m:x", ":3"):
        r = c.get(f"/assistant/sessions/{sid}/events", params={"last_event_id": bad}, headers=U1)
        assert r.status_code == 422, bad
    r = c.get(f"/assistant/sessions/{sid}/events", params={"last_event_id": "m:0"}, headers=U1)
    assert r.status_code == 404
    # an inline turn's frames carry no id
    r = c.post(f"/assistant/sessions/{sid}/messages", json={"text": "问"}, headers=U1)
    assert all("id" not in b for b in _blocks(r))
