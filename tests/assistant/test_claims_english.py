import pytest

from evowiki.agent.tools import ToolResult
from evowiki.assistant.claims import ClaimCheck, claim_in, unbacked_in


@pytest.mark.parametrize("text", [
    "I've saved it.", "I’ve updated your preferences.", "I applied the draft.",
    "I will remember this next time.", "From now on I will explain with examples.",
    "I haven't saved it. I've saved it now.",
    "Do you want that? I've saved it.", "还没有改，but I've updated it now.",
    "E.g. I've saved it.",
])
def test_completion_is_checked(text):
    assert claim_in(text)


@pytest.mark.parametrize("text", [
    "I haven't saved it.", "I did not update it.", "If you agree, I've saved it.",
    "You saved it.", "Have I saved it?", "He wrote 'I have saved it'.", 'He wrote "I have saved it".',
    "```python\n# I have saved it\n```", "I can save it.",
    "I will save it if you agree.", "I created a draft.",
])
def test_noncompletion_and_draft(text):
    assert claim_in(text, drafted=True) is None


def test_draft_does_not_authorize_application():
    assert claim_in("I created a draft. I applied it.", drafted=True)
    assert claim_in("I applied the draft.", drafted=True)


@pytest.mark.parametrize("text", ["I checked your records.", "I’ve read your notes.",
                                 "I did not check. I checked it now."])
def test_reads_need_tool_evidence(text):
    assert unbacked_in(text, looked=False, known="")
    assert unbacked_in(text, looked=True, known="") is None


@pytest.mark.parametrize("text", ["You spent $12.50 today.", "Your usage is 12.50 USD.",
                                 "You paid €12.50."])
def test_money_claims(text):
    assert unbacked_in(text, looked=True, known="")
    assert unbacked_in(text, looked=True, known="12.50") is None


def test_owing_question_uses_structured_result():
    from types import SimpleNamespace
    tool = SimpleNamespace(spec=SimpleNamespace(name="ask_check", category="action"))
    check = ClaimCheck()
    check.saw(tool, ToolResult(content="下一题：not a fact"))
    assert not check.owes
    check.saw(tool, ToolResult(content="Next question", data={"next_question_required": True}))
    assert check.owes


def test_spending_evidence_requires_an_entire_amount():
    assert unbacked_in("You spent $12.50.", looked=True, known="112.50")
    assert unbacked_in("You spent $1,250.00.", looked=True, known="1250") is None
