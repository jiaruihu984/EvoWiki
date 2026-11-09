import pytest
from pydantic import ValidationError

from evowiki.memory.contracts import EvidenceRef, MemoryScope


def test_evidence_is_exactly_one_of_message_or_event():
    EvidenceRef(message_id="m1", session_id="s1", role="user", quote="以后都先给例子")
    EvidenceRef(event_id="ev1")
    with pytest.raises(ValidationError):
        EvidenceRef()
    with pytest.raises(ValidationError):
        EvidenceRef(message_id="m1", session_id="s1", role="user", event_id="ev1")


def test_message_evidence_needs_role_and_session_quote_only_with_message():
    with pytest.raises(ValidationError):
        EvidenceRef(message_id="m1", session_id="s1")                # no role
    with pytest.raises(ValidationError):
        EvidenceRef(message_id="m1", role="user")                    # no session
    with pytest.raises(ValidationError):
        EvidenceRef(event_id="ev1", quote="x")                       # quote without message
    with pytest.raises(ValidationError):
        EvidenceRef(event_id="ev1", role="user")


def test_scope_global_has_no_id_others_need_one():
    MemoryScope(scope_type="global")
    MemoryScope(scope_type="page", scope_id="p1")
    with pytest.raises(ValidationError):
        MemoryScope(scope_type="global", scope_id="x")
    with pytest.raises(ValidationError):
        MemoryScope(scope_type="concept")
