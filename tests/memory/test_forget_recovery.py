"""Plan Task 15 Step 3: the delete's recovery and propagation, end to end. A failed delete is
retried until it commits; the A→B→C second-order spread is handled; an old backup restored
after the delete is undone; a placeholder answer gives the next consolidation nothing.
Postgres; RSI's and S3's steps are fakes (tests/memory/forget_world.py); no model call."""

from datetime import timedelta

import pytest

from evowiki.assistant.pg_store import PgSessionStore
from evowiki.assistant.session import fold
from evowiki.memory.consolidate import consolidate
from evowiki.memory.forget import PLACEHOLDER
from evowiki.memory.lander import NullPreferenceLander, NullRuleGuard
from evowiki.memory.tombstone import reconcile
from tests.memory.forget_world import CLEAN, T0, TAINTED, WORDS, build
from tests.memory.test_consolidate import _gateway, cand, ev
from tests.memory.test_tombstone import _backup, restore_backup


@pytest.fixture
def w(pool, tmp_path):
    return build(pool, tmp_path)


def _spread_handled(w, before) -> None:
    for sid, mid in TAINTED:
        user, ans = w.turn_rows(sid, mid)
        assert user["payload"]["text"] == WORDS[mid] and ans["payload"]["text"] == PLACEHOLDER
        assert w.count("select 1 from memory_episode where session_id = %s "
                       "and message_id = %s", sid, mid) == 0
    for k in CLEAN:
        assert w.turn_rows(*k) == before[k]
    assert w.count("select 1 from assistant_session where session_id = 'A'") == 0


def test_failed_delete_is_retried_until_committed(w):
    before = {k: w.turn_rows(*k) for k in CLEAN}
    w.harness.fail = RuntimeError("rsi down")
    out = w.service.forget_session("u1", "A", requested_by="test")
    assert out.state == "failed"                       # the route answers 202
    assert w.count("select 1 from assistant_entry where session_id = 'A'") == 4
    assert w.turn_rows("B", "b2")[1]["payload"]["text"] != PLACEHOLDER   # nothing deleted
    assert w.ledger.requests()[out.request_id].state == "requested"
    assert [o.state for o in w.service.retry_open()] == ["failed"]      # still broken
    w.harness.fail = None
    assert [o.state for o in w.service.retry_open()] == ["committed"]
    assert w.ledger.requests()[out.request_id].state == "committed"
    _spread_handled(w, before)


def test_second_order_spread_a_to_b_to_c(w):
    before = {k: w.turn_rows(*k) for k in CLEAN}
    assert w.service.forget_session("u1", "A", requested_by="test").state == "committed"
    _spread_handled(w, before)
    # C's c2 never read A: it is tainted through B's b2 (its search showed ep:B/b2)
    c2 = w.turn_rows("C", "c2")[1]["payload"]
    assert {r["id"] for r in c2["recalled"] if r.get("deleted")} == {"ep:B/b4"}
    assert w.count("select 1 from agent_run where run_id = 'assistant:C:c2'") == 0


def test_backup_restored_after_a_retried_delete_is_undone(w, admin_pool):
    before = {k: w.turn_rows(*k) for k in CLEAN}
    _backup(admin_pool)
    w.harness.fail = RuntimeError("rsi down")
    assert w.service.forget_session("u1", "A", requested_by="test").state == "failed"
    w.harness.fail = None
    assert reconcile(w.pool, w.ledger, w.service).verified == 1
    restore_backup(admin_pool)
    report = reconcile(w.pool, w.ledger, w.service)
    assert len(report.recreated) == 1 and report.verified == 1
    _spread_handled(w, before)


def test_placeholder_answers_give_consolidation_nothing(w, tmp_path):
    assert w.service.forget_session("u1", "A", requested_by="test").state == "committed"
    sessions = PgSessionStore(w.pool)
    state = fold(sessions.info("B"), sessions.entries("B"))
    seen: list = []
    # the model proposes what the removed answer of b2 said, citing it and b2's words
    gw = _gateway(tmp_path, [cand("decision", "用 pandas 处理数据",
                                  ev("b2-ans", "assistant"), ev("b2", quote=WORDS["b2"]))],
                  seen)
    r = consolidate(state, sessions=sessions, store=w.mem, gateway=gw, account=None,
                    lander=NullPreferenceLander(), guard=NullRuleGuard(), first=0,
                    through=len(state.turns), now=T0 + timedelta(days=2),
                    new_id=lambda p: f"{p}_new")
    prompt = seen[0].prompt
    assert "回答 b2" not in prompt and PLACEHOLDER not in prompt and "b2-ans" not in prompt
    assert not r.items_written
    assert [why for _, why in r.dropped] == ["evidence_removed"]
