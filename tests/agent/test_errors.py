from typing import ClassVar

import pytest

from evowiki.agent.errors import Cancelled, ErrorInfo, ToolLimitExceeded, classify
from evowiki.goals.context import ContextOverBudget
from evowiki.llm.gateway import (
    LLMParseError,
    MissingRecordingError,
    ProviderOutputError,
    ProviderUnavailableError,
)
from evowiki.llm.providers import ProviderRejectedError
from evowiki.llm.usage import Account, BudgetExceededError


@pytest.mark.parametrize(("exc", "info"), [
    (ToolLimitExceeded("x"), ErrorInfo("tool_limit", False)),
    (Cancelled("x"), ErrorInfo("cancelled", False)),
    (BudgetExceededError(Account(user_id="u1", group="assistant"), "day", 1.0),
     ErrorInfo("budget_exceeded", False)),
    (ProviderRejectedError("x"), ErrorInfo("llm_unavailable", False)),
    (ProviderUnavailableError("x"), ErrorInfo("llm_unavailable", True)),
    (MissingRecordingError("x"), ErrorInfo("llm_unavailable", True)),
    (LLMParseError("x"), ErrorInfo("llm_unavailable", True)),
    (ProviderOutputError("x", status="truncated"), ErrorInfo("llm_unavailable", True)),
    (ContextOverBudget("x"), ErrorInfo("bad_request", False)),
    (ValueError("x"), ErrorInfo("internal", False)),
])
def test_classify(exc, info):
    assert classify(exc) == info


def test_anthropic_credit_balance_400_classifies_as_unavailable_not_retryable():
    from evowiki.llm.models import LLMRequest
    from evowiki.llm.providers import _sdk_call

    class BadRequestError(Exception):
        status_code = 400
        body: ClassVar[dict] = {"type": "error", "error": {"type": "invalid_request_error", "message":
                "Your credit balance is too low to access the Anthropic API."}}

    def create(**_):
        raise BadRequestError("Error code: 400")

    req = LLMRequest(purpose="goals.organize", prompt_version="v1", subject="p", system="S",
                     prompt="P")
    with pytest.raises(ProviderRejectedError) as ei:
        _sdk_call("anthropic", req, create)
    assert classify(ei.value) == ErrorInfo("llm_unavailable", False)


def test_a_refused_request_says_switch_model_and_never_marks_the_model_unusable():
    """Live 2026-11-04: a 400 from the provider read 「没能完成，请再试一次。」 (code internal)."""
    from evowiki.llm.exposure import rejects_tool_change
    from evowiki.llm.models import LLMRequest
    from evowiki.llm.providers import ProviderBadRequestError, _sdk_call
    from evowiki.runtime.models import refusal_reason

    class BadRequestError(Exception):
        status_code = 400
        body: ClassVar[dict] = {"error": {"message": "Invalid `signature` in `thinking` block"}}

    def create(**_):
        raise BadRequestError("Error code: 400")

    req = LLMRequest(purpose="goals.organize", prompt_version="v1", subject="p", system="S",
                     prompt="P")
    with pytest.raises(ProviderBadRequestError) as ei:
        _sdk_call("anthropic", req, create)
    assert classify(ei.value).code == "llm_unavailable"
    assert rejects_tool_change(ei.value)
    assert refusal_reason({"error_type": type(ei.value).__name__, "error_message": str(ei.value)}) == ""
