import pytest
from pydantic import TypeAdapter, ValidationError

from evowiki.agent.tools import ProposalDraft, ToolSpec
from evowiki.assistant.contracts import AssistantEvent, Done, MessageIn, MessageOut


def test_tool_names_are_snake_case():
    ToolSpec(name="search_wiki", description="d", parameters={"type": "object"}, effect="read")
    with pytest.raises(ValidationError):
        ToolSpec(name="Search-Wiki", description="d", parameters={}, effect="read")
    with pytest.raises(ValidationError):
        ToolSpec(name="x_y", description="d", parameters={}, effect="write")


def test_events_round_trip_by_type():
    ad = TypeAdapter(AssistantEvent)
    ev = ad.validate_python({"type": "tool_start", "call_id": "c1", "name": "web_search",
                             "effect": "network", "summary": "z-score"})
    assert ev.type == "tool_start"
    assert ad.validate_json(Done(message_id="m", tool_calls=1, llm_calls=2).model_dump_json()).type == "done"


def test_message_and_proposal_bounds():
    with pytest.raises(ValidationError):
        MessageIn(text="")
    with pytest.raises(ValidationError):
        ProposalDraft(kind="delete_section", page_id="p", heading="h", text="t", why="w")


def test_message_out_selection_defaults_to_none_and_round_trips():
    m = MessageOut(message_id="m1", role="assistant", text="答")
    assert m.selection is None and m.model_dump(mode="json")["selection"] is None
    u = MessageOut.model_validate({"message_id": "m2", "role": "user", "text": "问",
                                   "selection": {"text": "选中的话", "heading": "小节"}})
    assert u.selection.text == "选中的话" and u.selection.heading == "小节"
    assert MessageOut.model_validate_json(u.model_dump_json()) == u
    with pytest.raises(ValidationError):
        MessageOut(message_id="m3", role="user", text="问", selection={"text": ""})


def test_session_out_decisions_default_empty_and_serializes():
    from evowiki.assistant.contracts import DECISION_INTERNAL, ProposalStateOut, SessionOut

    s = SessionOut(session_id="s", page_id=None, messages=())
    assert s.decisions == ()
    s2 = s.model_copy(update={"decisions": (ProposalStateOut(
        proposal_id="p", status="failed", detail=DECISION_INTERNAL),)})
    assert s2.model_dump()["decisions"][0] == {"proposal_id": "p", "status": "failed",
                                               "applied": (), "detail": "internal",
                                               "action_id": "", "can_undo": False}


def test_proposal_state_status_excludes_already_decided():
    from evowiki.assistant.contracts import ProposalStateOut

    with pytest.raises(ValidationError):
        ProposalStateOut(proposal_id="p", status="already_decided")
