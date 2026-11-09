"""Historical visibility is based on recording time and full input dependencies."""

from datetime import UTC, datetime, timedelta

from evowiki.harness.snapshot import SnapshotBoundary


def test_cutoff_and_heldout_generation_dependencies():
    cutoff = datetime(2026, 11, 8, tzinfo=UTC)
    boundary = SnapshotBoundary(cutoff, frozenset({"turn:s/heldout"}))
    assert boundary.allows(cutoff, {"turn:s/train"})
    assert not boundary.allows(cutoff + timedelta(microseconds=1), {"turn:s/train"})
    assert not boundary.allows(None, {"turn:s/train"})
    assert not boundary.allows(cutoff, {"summary:old", "turn:s/heldout"})


def test_historical_tool_schema_does_not_follow_todays_builtin():
    import pytest

    from evowiki.harness.pack import EMPTY, HarnessPack
    from evowiki.harness.replay_assistant import ReplaySourceMissing, tool_specs
    from evowiki.llm.chat import ChatMessage, ChatRequest, ToolDef

    schema = {"type": "object", "properties": {"query": {"type": "string"}},
              "required": ["query"]}
    request = ChatRequest(purpose="assistant.turn", prompt_version="v1", subject="s",
                          system="recorded", messages=(ChatMessage(role="user", text="question"),),
                          tools=(ToolDef(
                              name="web_search", description="historical", parameters=schema),))
    pack = HarnessPack(1, {"H2.assistant.web_search.query": "candidate wording"})
    [spec] = tool_specs(request, EMPTY, pack)
    assert spec.description == "historical"
    assert spec.parameters["required"] == ["query"]
    assert spec.parameters["properties"]["query"]["description"] == "candidate wording"
    with pytest.raises(ReplaySourceMissing):
        tool_specs(request, pack, EMPTY)
