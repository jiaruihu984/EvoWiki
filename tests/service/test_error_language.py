from evowiki.bot.prompts import EMPTY, PromptInvalid
from evowiki.llm.usage import Account, BudgetExceededError
from evowiki.service.app import _detail, _public_error
from evowiki.service.rsi_runs import CycleBusy
from tests.service.conftest import H, signup
from tests.service.test_assistant_models_api import OPENAI, api  # noqa: F401


def test_public_validation_preserves_semantic_reason_in_both_languages():
    error = PromptInvalid(EMPTY)
    assert _detail(error, lang="zh") == EMPTY
    assert _detail(error, lang="en") == "Guidance cannot be empty."
    assert error.error_code == "prompt_empty"


def test_raw_exception_is_never_treated_as_public_because_of_its_language():
    assert _detail(LookupError("内部密钥 secret"), lang="en") == "This item was not found. It may have been deleted."
    assert _detail(LookupError("secret key"), lang="en") == "This item was not found. It may have been deleted."


def test_wrapped_public_error_keeps_structured_identity():
    original = PromptInvalid(EMPTY)
    wrapper = RuntimeError("private wrapper details")
    wrapper.__cause__ = original
    assert _public_error(wrapper) is original
    assert _detail(wrapper, lang="en") == "Guidance cannot be empty."


def test_budget_has_period_and_amount_and_rsi_busy_has_stable_code():
    error = BudgetExceededError(Account(user_id="u", group="assistant"), "day", 1.5)
    assert error.params == {"period": "day", "limit": "1.50"}
    assert "$1.50" in _detail(error, lang="en")
    assert "今天" in _detail(error, lang="zh")
    assert CycleBusy().error_code == "rsi_busy"


def test_english_validation_hides_raw_input(api):  # noqa: F811
    signup(api, lang="en")
    response = api.post("/wiki/guidelines", headers=H("u1"), json={"text": ""})
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_request"
    assert response.json()["params"] == {"fields": ["text"]}
    assert response.json()["detail"] == "Check the highlighted fields and try again."
    secret = "fake-sensitive-input-do-not-echo"
    response = api.post("/assistant/model", headers=H("u1"), json={"reasoning": secret})
    assert secret not in response.text and "input" not in response.json()


def test_english_model_reasoning_failure_has_stable_code(api):  # noqa: F811
    signup(api, lang="en")
    response = api.post("/assistant/model", headers=H("u1"), json={"model": OPENAI, "reasoning": "max"})
    assert response.status_code == 422
    assert response.json() == {"code": "reasoning_unsupported", "params": {},
                               "detail": "This model does not support this reasoning level."}


def test_durable_initial_status_is_localized_without_a_model_call():
    import json

    from evowiki.assistant.contracts import Done
    from evowiki.i18n import language_context
    from evowiki.service.routes_assistant import _turn_events

    class Turn:
        cursor = 0
        def __iter__(self):
            yield Done(message_id="answer", tool_calls=0, llm_calls=0)

    for language, expected in (("en", "Thinking…"), ("zh", "正在思考…")):
        with language_context(language):
            dependency = _turn_events(Turn(), "message", announce=True)
            stream = next(dependency)
            initial = next(stream)
            assert json.loads(initial.raw_data)["text"] == expected
            assert initial.id == "message:0"
            dependency.close()
