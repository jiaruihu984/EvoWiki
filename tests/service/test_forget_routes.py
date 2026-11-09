"""GET /assistant/sessions/{id}/forget-preview, DELETE /assistant/sessions/{id}, DELETE
/assistant/memory/{id} (memory plan Task 13) over a fake delete flow (the flow itself:
tests/memory/test_forget.py). No database, no model call."""

from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient

from evowiki.llm.chat import ChatTurn
from evowiki.memory.forget import (
    RETRY_DETAIL,
    UNAVAILABLE,
    FactRow,
    ForgetOutcome,
    ForgetPreview,
    ForgetUnavailable,
    MaterialRow,
    RuleRow,
    TaintedSession,
)
from evowiki.service.app import create_app
from tests.service.test_assistant_api import U1, U2, service

T0 = datetime(2026, 10, 28, 9, tzinfo=UTC)


class FakeForget:
    available = True

    def __init__(self, state="committed", item="committed"):
        self.state, self.item = state, item
        self.calls: list = []

    def preview(self, uid, sid):
        if uid != "u1":
            raise LookupError(sid)
        return ForgetPreview(rules=(RuleRow("si1", "先给例子", "user"),),
                             facts=(FactRow("k1", "我是谁", "在读研"),),
                             tainted_sessions=(TaintedSession("s2", "接着说", 1),),
                             memory_counts={"episodes": 2, "items": 1, "tainted_turns": 1},
                             materials=(MaterialRow("src1", "讲义", ("page:p1",)),
                                        MaterialRow("src2", "截图", ())))

    def forget_session(self, uid, sid, **kw):
        if uid != "u1":
            raise LookupError(sid)
        self.calls.append((uid, sid, kw))
        return ForgetOutcome("r1", self.state, None if self.state == "committed" else "x")

    def forget_item(self, uid, item_id, *, requested_by):
        self.calls.append((uid, item_id, requested_by))
        return self.item

    def pending(self, uid):
        return [{"session_id": "s9", "state": "failed", "requested_at": T0}]


def client(tmp_path, forget):
    return TestClient(create_app(SimpleNamespace(assistant=service(tmp_path, [ChatTurn(text="答")]),
                                                 forget=forget)))


def _open(c):
    c.app.state.memory_ready = True          # the lifespan's reconcile, which TestClient skips
    return c


def test_preview_lists_rules_facts_and_tainted_sessions(tmp_path):
    c = _open(client(tmp_path, FakeForget()))
    p = c.get("/assistant/sessions/s1/forget-preview", headers=U1).json()
    assert p["rules"] == [{"item_id": "si1", "text": "先给例子", "authority": "user"}]
    assert p["facts"] == [{"key": "k1", "section": "我是谁", "text": "在读研"}]
    assert p["tainted_sessions"] == [{"session_id": "s2", "title": "接着说", "turns": 1}]
    assert p["materials"] == [
        {"source_id": "src1", "title": "讲义", "in_use_elsewhere": True, "used_by": ["page:p1"]},
        {"source_id": "src2", "title": "截图", "in_use_elsewhere": False, "used_by": []}]
    assert "继续生效" in p["note"]
    assert c.get("/assistant/sessions/s1/forget-preview", headers=U2).status_code == 404


def test_delete_session_200_then_202_when_the_attempt_failed(tmp_path):
    f = FakeForget()
    c = _open(client(tmp_path, f))
    r = c.request("DELETE", "/assistant/sessions/s1", headers=U1,
                  json={"revoke_rule_ids": ["si1"], "revoke_fact_keys": ["k1"],
                        "delete_source_ids": ["src2"]})
    assert r.status_code == 200 and r.json() == {"state": "committed", "detail": ""}
    assert f.calls[0][2] == {"revoke_rule_ids": ["si1"], "revoke_fact_keys": ["k1"],
                             "delete_source_ids": ["src2"], "requested_by": "api:u1"}
    f.state = "failed"
    r = c.delete("/assistant/sessions/s1", headers=U1)
    assert r.status_code == 202 and r.json() == {"state": "failed", "detail": RETRY_DETAIL}
    assert c.delete("/assistant/sessions/s1", headers=U2).status_code == 404
    bad = c.request("DELETE", "/assistant/sessions/s1", headers=U1, json={"x": 1})
    assert bad.status_code == 422


def test_delete_memory_statuses(tmp_path):
    f = FakeForget()
    c = _open(client(tmp_path, f))
    assert c.delete("/assistant/memory/i1", headers=U1).status_code == 200
    for out, code in (("failed", 202), ("not_found", 404), ("elsewhere", 409)):
        f.item = out
        assert c.delete("/assistant/memory/i1", headers=U1).status_code == code


def test_not_wired_or_unavailable_is_503(tmp_path):
    c = _open(client(tmp_path, None))
    for r in (c.get("/assistant/sessions/s1/forget-preview", headers=U1),
              c.delete("/assistant/sessions/s1", headers=U1),
              c.delete("/assistant/memory/i1", headers=U1)):
        assert r.status_code == 503 and r.json()["detail"] == UNAVAILABLE
    f = FakeForget()
    f.available = False
    c = _open(client(tmp_path, f))
    assert c.delete("/assistant/sessions/s1", headers=U1).status_code == 503
    assert c.delete("/assistant/memory/i1", headers=U1).status_code == 200   # needs neither

    class Raising(FakeForget):
        def forget_session(self, uid, sid, **kw):
            raise ForgetUnavailable(UNAVAILABLE)
    c = _open(client(tmp_path, Raising()))
    r = c.delete("/assistant/sessions/s1", headers=U1)
    assert r.status_code == 503 and r.json()["detail"] == UNAVAILABLE


def test_sessions_being_deleted_are_listed_apart(tmp_path):
    c = _open(client(tmp_path, FakeForget()))
    assert c.get("/assistant/forget-requests", headers=U1).json() == {"sessions": [
        {"session_id": "s9", "state": "failed", "requested_at": "2026-10-28T09:00:00Z"}]}
    assert all(r["session_id"] != "s9"
               for r in c.get("/assistant/sessions", headers=U1).json()["sessions"])
    c = _open(client(tmp_path, None))
    assert c.get("/assistant/forget-requests", headers=U1).json() == {"sessions": []}


def test_closed_until_reconcile(tmp_path):
    c = client(tmp_path, FakeForget())                  # lifespan not run: not reconciled
    assert c.get("/assistant/sessions", headers=U1).status_code == 503
