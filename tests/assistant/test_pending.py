"""assistant.pending (stage 3 T5): the op shapes, offer's checks and confirm's rules, without a
turn (the turn-level flows are in test_s3_scenarios)."""

import pytest
from pydantic import ValidationError

from evowiki.agent.tools import ActionCard, Pause, Reply, ToolContext
from evowiki.assistant.pending import (
    NO_PENDING,
    NOT_A_YES,
    NOT_QUOTED,
    ConfirmTool,
    OfferTool,
    PendingOp,
    args_hash,
)
from evowiki.assistant.tools.actions import Grounded
from evowiki.memory.contracts import EvidenceRef, MemoryScope
from tests.assistant.test_grounded_actions import Undo


def ctx(text="好"):
    return ToolContext(user_id="u1", session_id="s1", current_words=(text,), user_words=(text,))


CTX = ctx()


def test_evidence_is_exactly_one_message_or_event():
    EvidenceRef(message_id="m1", session_id="s1", role="user", quote="好")
    EvidenceRef(event_id="ev1")
    for bad in ({}, {"message_id": "m1", "event_id": "e"}, {"message_id": "m1", "role": "user"},
                {"message_id": "m1", "session_id": "s1"}, {"event_id": "e", "role": "user"},
                {"event_id": "e", "quote": "x"}):
        with pytest.raises(ValidationError):
            EvidenceRef(**bad)
    with pytest.raises(ValidationError):
        MemoryScope(scope_type="page")                     # a page scope names its page


def _undo_tool():
    inner = Undo()
    return inner, Grounded(inner)


def _call_op(args, *, pid="po_m0_1", digest=None, reasked=False) -> PendingOp:
    return PendingOp(pending_id=pid, session_id="s1",
                     proposed_in=EvidenceRef(message_id="a0", session_id="s1", role="assistant"),
                     kind="call", tool="undo", args=args,
                     args_hash=digest or args_hash("undo", args), title="撤销上一步",
                     reasked=reasked)


def _offer(tools, allowed=lambda n: True, waiting=None):
    return OfferTool(lambda: waiting or {}, tools.get, allowed, lambda n: f"先载入 {n}",
                     lambda t: "po_m1_1")


def test_offer_checks_the_tool_tier_and_its_schema_and_runs_nothing():
    inner, undo = _undo_tool()
    offer = _offer({"undo": undo})
    ok = offer.run({"tool": "undo", "args": {"action_id": "last"}, "title": "撤销上一步"}, CTX)
    assert ok.error is None and ok.offer["pending_id"] == "po_m1_1" and inner.calls == []
    assert ok.offer["args"] == {"action_id": "last"} and ok.offer["kind"] == "call"
    assert "action_id" in ok.offer["title"]                 # the title carries the key args
    bad = offer.run({"tool": "undo", "args": {"action_id": 3}, "title": "x"}, CTX)
    assert bad.error is not None
    assert offer.run({"tool": "nope", "args": {}, "title": "x"}, CTX).error is not None
    assert offer.run({"title": "x"}, CTX).error is not None          # no tool
    # a draft card is bound by its own call; offer(action_id) only binds a
    # preference waiting for his yes (T6)
    assert offer.run({"action_id": "act_7"}, CTX).error is not None
    card = ActionCard(action_id="act_7", kind="pending_preference", title="以后先给例子",
                      status="proposed", confirm=True)
    ok = _offer({}, waiting={"act_7": card}).run({"action_id": "act_7"}, CTX)
    assert ok.error is None and ok.offer["kind"] == "ledger" and ok.offer["action_id"] == "act_7"


def test_offer_refuses_a_tool_this_turn_may_not_run_yet():
    """M1: a direct tool of a Skill not loaded (bot_settings in settings) cannot be offered."""
    _, undo = _undo_tool()
    offer = _offer({"bot_settings": undo}, allowed=lambda n: False)
    res = offer.run({"tool": "bot_settings", "args": {"action_id": "x"}, "title": "改设置"}, CTX)
    assert res.error is not None and "先载入 bot_settings" in res.content


def test_confirm_runs_the_one_op_and_bypasses_grounded():
    inner, undo = _undo_tool()
    op = _call_op({"action_id": "last"})
    c = ConfirmTool(lambda: (op,), {"undo": undo}.get, None, message_id=lambda: "m2")
    assert c.run({"pending_id": "po_other", "asked": "好"}, CTX).error is not None
    res = c.run({"pending_id": op.pending_id, "asked": "好"}, CTX)
    assert res.error is None and inner.calls == [{"action_id": "last"}]
    assert res.closes == (op.pending_id,)


@pytest.mark.parametrize("message,asked,why", [
    ("不好", "不好", NOT_A_YES),            # a negation first
    ("不好意思", "不好意思", NOT_A_YES),
    ("好难", "好", NOT_QUOTED),             # a one-character piece of a longer message
    ("我觉得好", "好", NOT_QUOTED),
])
def test_confirm_needs_a_yes_of_this_message(message, asked, why):
    inner, undo = _undo_tool()
    op = _call_op({"action_id": "last"})
    c = ConfirmTool(lambda: (op,), {"undo": undo}.get, None, message_id=lambda: "m2")
    res = c.run({"pending_id": op.pending_id, "asked": asked}, ctx(message))
    assert res.error is not None and why in res.content and inner.calls == []


def test_confirm_with_no_op_refuses_and_rechecks_the_tool_is_allowed():
    c = ConfirmTool(tuple, {}.get, None, message_id=lambda: "m2")
    assert NO_PENDING in c.run({"pending_id": "po_x", "asked": "好"}, CTX).content
    inner, undo = _undo_tool()
    op = _call_op({"action_id": "last"})
    c2 = ConfirmTool(lambda: (op,), {"undo": undo}.get, None, message_id=lambda: "m2",
                     allowed=lambda n: False, not_loaded=lambda n: "未载入")
    res = c2.run({"pending_id": op.pending_id, "asked": "好"}, CTX)
    assert res.error is not None and "未载入" in res.content and inner.calls == []


def test_args_changed_since_offer_are_refused():
    inner, undo = _undo_tool()
    op = _call_op({"action_id": "last"}, digest=args_hash("undo", {"action_id": "act_9"}))
    c = ConfirmTool(lambda: (op,), {"undo": undo}.get, None, message_id=lambda: "m2")
    res = c.run({"pending_id": op.pending_id, "asked": "好"}, CTX)
    assert res.error is not None and "不一样" in res.content and inner.calls == []


def test_a_pick_by_the_options_exact_text_runs_that_op():
    """M4: an option picked on the card is explicit (irreversible ops included, Q12)."""
    inner, undo = _undo_tool()
    a = _call_op({"action_id": "act_1"}, pid="po_m0_1")
    b = _call_op({"action_id": "act_2"}, pid="po_m0_2")
    c = ConfirmTool(lambda: (a, b), {"undo": undo}.get, None, message_id=lambda: "m2")
    paused = c.run({"pending_id": a.pending_id, "asked": "好"}, CTX)
    assert isinstance(paused.pause, Pause) and inner.calls == []
    res = c.resume({}, CTX, paused.pause, Reply(text="2. 撤销上一步"))
    assert res.error is None and inner.calls == [{"action_id": "act_2"}]


def test_ops_carried_again_after_which_one_bind_by_pending_id():
    """M3: after an answer asked which one, 「第一个」 (the model's pending_id) runs it."""
    inner, undo = _undo_tool()
    a = _call_op({"action_id": "act_1"}, pid="po_m0_1", reasked=True)
    b = _call_op({"action_id": "act_2"}, pid="po_m0_2", reasked=True)
    c = ConfirmTool(lambda: (a, b), {"undo": undo}.get, None, message_id=lambda: "m3")
    res = c.run({"pending_id": a.pending_id, "asked": "第一个"}, ctx("第一个"))
    assert res.error is None and inner.calls == [{"action_id": "act_1"}]


def test_two_ops_always_pause_on_a_pick_and_anything_but_an_option_runs_nothing():
    a, b = _call_op({"action_id": "x"}, pid="po_1"), _call_op({"action_id": "y"}, pid="po_2")
    c = ConfirmTool(lambda: (a, b), {}.get, None, message_id=lambda: "m2")
    assert c.spec.pauses
    paused = c.run({"pending_id": "po_1", "asked": "好"}, CTX)
    assert paused.pause is not None and paused.pause.kind == "confirm_pick"
    res = c.resume({}, CTX, paused.pause, Reply(text="先别动，讲讲第二个"))
    assert res.error is None and "什么都没做" in res.content and "先别动" in res.content
