import pytest
from pydantic import ValidationError

from evowiki.forge.models import Evidence, ToolExample, ToolRequest


def req(**kw):
    base = {"name": "check_balanced", "title": "配平检查", "purpose": "判断化学方程式是否配平", "agent": "deep",
            "examples": (ToolExample(args={"eq": "2H2+O2=2H2O"}, expect={"ok": True}),)}
    return ToolRequest(**{**base, **kw})


def test_tool_request_needs_examples_and_a_valid_name():
    assert req().scope == "user"
    with pytest.raises(ValidationError):
        req(examples=())
    with pytest.raises(ValidationError):
        req(name="Bad-Name")


def test_evidence_needs_a_known_citation_kind():
    Evidence(citations=("signal:1", "trace:r1", "cmp:x"))
    with pytest.raises(ValidationError):
        Evidence(citations=())
    with pytest.raises(ValidationError):
        Evidence(citations=("hunch:me",))
