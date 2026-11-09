from datetime import timedelta
from types import SimpleNamespace

from evowiki.assistant.session import MemorySessionStore
from evowiki.wikiapp.signals import assistant_rejections
from tests.assistant.fakes import DRAFT, INFO, T0, run


class FakeLedger:
    def __init__(self, *rows):
        self.rows = list(rows)

    def list(self, user_id, *, status=None, limit=100, writes=False):
        return [r for r in self.rows if r.status == status]


def row(action_id, status, *, source="assistant", kind="rule", at=60, key=None, page_id=None,
        content=None, summary="把路径顺序改了"):
    return SimpleNamespace(action_id=action_id, status=status, source=source, kind=kind,
                           decided_at=T0 + timedelta(seconds=at), page_id=page_id,
                           summary=summary, content=content or {},
                           idempotency_key=key or f"assistant:turn:s1:m1:{len(action_id)}7")


def test_rejected_proposal_gives_one_signal_with_the_turn_run_id():
    out = assistant_rejections(run(MemorySessionStore()), None, "u1")
    [(sig, run_id)] = out
    assert run_id == "assistant:s1:m1"
    assert (sig.signal_id, sig.kind, sig.page_id, sig.section_id) == (
        "arej:pr1", "assistant_rejected", "p1", None)
    assert sig.at == T0 + timedelta(seconds=8)
    assert "补充" in sig.text and "拒绝" in sig.text


def test_accepted_proposal_and_turn_error_give_none():
    store = MemorySessionStore()
    store.create(INFO)
    store.append("s1", "e1", "user", {"message_id": "m1", "text": "问"}, T0)
    store.append("s1", "e2", "proposal", {"proposal_id": "pr1", "message_id": "m1",
                                         "draft": DRAFT}, T0)
    store.append("s1", "e3", "decision", {"proposal_id": "pr1", "status": "accepted",
                                         "applied": [], "detail": ""}, T0)
    store.append("s1", "e4", "turn_error", {"message_id": "m1", "code": "x", "detail": "y"}, T0)
    assert assistant_rejections(store, FakeLedger(), "u1") == []


def test_rejected_and_undone_ledger_actions_give_signals():
    store = run(MemorySessionStore())
    ledger = FakeLedger(row("a1", "rejected", page_id="p1"), row("a2", "undone", at=70))
    out = assistant_rejections(store, ledger, "u1")
    by_id = {s.signal_id: (s, r) for s, r in out}
    assert set(by_id) == {"arej:pr1", "aact:a1", "aact:a2"}
    s1, r1 = by_id["aact:a1"]
    assert r1 == "assistant:s1:m1" and s1.page_id == "p1" and "拒绝" in s1.text
    assert "把路径顺序改了" in s1.text
    s2, _ = by_id["aact:a2"]
    assert "撤销" in s2.text and s2.page_id is None
    assert [s.signal_id for s, _ in out] == ["arej:pr1", "aact:a1", "aact:a2"]   # by (at, id)


def test_action_turn_is_found_from_the_chat_card():
    store = run(MemorySessionStore())
    store.append("s1", "e20", "tool", {"message_id": "m2", "call_id": "c", "name": "x",
                                       "effect": "write", "args": {}, "ok": True, "content": "",
                                       "sources": [], "action": {"action_id": "a9"}}, T0)
    out = assistant_rejections(
        store, FakeLedger(row("a9", "undone", key="assistant:s1:abc123")), "u1")
    assert ("aact:a9", "assistant:s1:m2") in [(s.signal_id, r) for s, r in out]


def test_other_sources_other_users_and_unplaceable_rows_are_skipped():
    store = run(MemorySessionStore())
    ledger = FakeLedger(
        row("b1", "rejected", source="bot"),                       # not the assistant's
        row("b2", "rejected", key="assistant:other:m1:3"),          # session is not the user's
        row("b3", "rejected", kind="assistant_edit",
            content={"proposal_id": "pr1"}))                        # source (a), not twice
    out = assistant_rejections(store, ledger, "u1")
    assert [s.signal_id for s, _ in out] == ["arej:pr1"]
    assert assistant_rejections(store, ledger, "someone-else") == []


def test_undone_accepted_proposal_counts_once_via_its_proposal():
    store = run(MemorySessionStore())
    ledger = FakeLedger(row("c1", "undone", kind="assistant_edit", key="assistant:pr1",
                            content={"proposal_id": "pr1"}))
    out = assistant_rejections(store, ledger, "u1")
    assert [(s.signal_id, r) for s, r in out if s.signal_id.startswith("aact")] == [
        ("aact:c1", "assistant:s1:m1")]


def test_since_filter_is_inclusive_and_applies_to_both_sources():
    store = run(MemorySessionStore())
    ledger = FakeLedger(row("a1", "rejected", at=60), row("a2", "undone", at=90))
    ids = lambda since: [s.signal_id for s, _ in assistant_rejections(store, ledger, "u1", since)]
    assert ids(None) == ["arej:pr1", "aact:a1", "aact:a2"]
    assert ids(T0 + timedelta(seconds=60)) == ["aact:a1", "aact:a2"]
    assert ids(T0 + timedelta(seconds=61)) == ["aact:a2"]
    assert ids(T0 + timedelta(seconds=91)) == []


def test_a_ledger_scan_that_hits_its_cap_is_logged(monkeypatch, caplog):
    from evowiki.wikiapp import signals
    monkeypatch.setattr(signals, "LEDGER_SCAN", 2)
    store = run(MemorySessionStore())
    ledger = FakeLedger(row("a1", "rejected"), row("a2", "rejected", at=70),
                        row("a3", "undone", at=80))
    with caplog.at_level("WARNING", logger="evowiki.wikiapp.signals"):
        assistant_rejections(store, ledger, "u1")
    assert "hit its cap" in caplog.text and "rejected" in caplog.text
    assert "of undone rows" not in caplog.text          # one row under the cap: no warning


# ---- assistant_rated (助手回答评价) ----

def rate(store, i, value, *, chips=(), note="", mid="a1", turn="m1", at=20):
    store.append("s1", f"r{i}", "rating", {"message_id": mid, "turn_id": turn, "value": value,
                                          "chips": list(chips), "note": note},
                 T0 + timedelta(seconds=at + i))


def test_a_down_rating_is_a_signal_with_its_turn_run_and_text():
    from evowiki.wikiapp.signals import assistant_ratings
    store = run(MemorySessionStore())
    rate(store, 1, "down", chips=("不对", "太啰嗦"), note="  公式 写错了 ")
    [(sig, run_id)] = assistant_ratings(store, "u1")
    assert run_id == "assistant:s1:m1"
    assert (sig.signal_id, sig.kind, sig.page_id, sig.section_id) == (
        "arat:a1", "assistant_rated", "p1", None)
    assert sig.text == "觉得这次回答不好：不对、太啰嗦。补充：公式 写错了"
    assert sig.at == T0 + timedelta(seconds=21)
    assert assistant_ratings(store, "someone-else") == []


def test_a_down_rating_of_a_question_names_the_question():
    from evowiki.wikiapp.signals import assistant_ratings
    store = run(MemorySessionStore())
    store.append("s1", "r1", "rating", {"message_id": "a1", "turn_id": "m1", "value": "down",
                                        "chips": [], "note": "",
                                        "asks": [{"call_id": "c1", "question": "你学过 PCA 吗？"}]},
                 T0 + timedelta(seconds=21))
    [(sig, _)] = assistant_ratings(store, "u1")
    assert sig.text == "觉得这次提问不好。问题：你学过 PCA 吗？"


def test_the_latest_rating_wins_and_up_or_clear_is_no_signal():
    from evowiki.wikiapp.signals import assistant_ratings, rated_answers
    store = run(MemorySessionStore())
    assert assistant_ratings(store, "u1") == [] and rated_answers(store, "u1") == []
    rate(store, 1, "up")
    assert assistant_ratings(store, "u1") == []                        # 👍 is recorded, no signal
    assert rated_answers(store, "u1") == [(T0 + timedelta(seconds=4), False)]
    rate(store, 2, "down")
    [(sig, _)] = assistant_ratings(store, "u1")
    assert sig.text == "觉得这次回答不好" and sig.at == T0 + timedelta(seconds=22)
    assert rated_answers(store, "u1") == [(T0 + timedelta(seconds=4), True)]   # the answer's time
    rate(store, 3, None)
    assert assistant_ratings(store, "u1") == [] and rated_answers(store, "u1") == []
    rate(store, 4, "down", chips=("其他",))
    rate(store, 5, "up")
    assert assistant_ratings(store, "u1") == []


def test_a_down_ratings_time_is_when_it_became_down_not_its_last_edit():
    from evowiki.wikiapp.signals import assistant_ratings
    store = run(MemorySessionStore())
    rate(store, 1, "down", chips=("不对",))                              # at +21 s
    rate(store, 2, "down", chips=("太啰嗦",), note="改了原因")            # an edit at +22 s
    [(sig, _)] = assistant_ratings(store, "u1")
    assert sig.at == T0 + timedelta(seconds=21) and sig.text == "觉得这次回答不好：太啰嗦。补充：改了原因"
    assert assistant_ratings(store, "u1", T0 + timedelta(seconds=22)) == []   # not new since
    rate(store, 3, "up")
    rate(store, 4, "down")                                             # a new 👎 streak at +24 s
    [(sig, _)] = assistant_ratings(store, "u1")
    assert sig.at == T0 + timedelta(seconds=24)
    rate(store, 5, None)
    rate(store, 6, "down")
    assert assistant_ratings(store, "u1")[0][0].at == T0 + timedelta(seconds=26)


def test_since_and_assistant_feedback_merge_rejections_and_ratings():
    from evowiki.wikiapp.signals import assistant_feedback, assistant_ratings
    store = run(MemorySessionStore())
    rate(store, 1, "down", at=100)
    assert assistant_ratings(store, "u1", T0 + timedelta(seconds=102)) == []
    assert len(assistant_ratings(store, "u1", T0 + timedelta(seconds=101))) == 1
    out = assistant_feedback(store, None, "u1")
    assert [(s.signal_id, r) for s, r in out] == [("arej:pr1", "assistant:s1:m1"),
                                                  ("arat:a1", "assistant:s1:m1")]
