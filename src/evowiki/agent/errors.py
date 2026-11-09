"""One error classification for every agent (spec 2026-10-14 §2).

Codes and retryability are the reading assistant's, unchanged. The page writer maps the codes
it exposes (`budget_exceeded`, `llm_unavailable`, `cancelled`) and folds the rest into `internal`."""

import logging
from dataclasses import dataclass

from evowiki.goals.context import ContextOverBudget
from evowiki.llm.deadline import RequestCancelled
from evowiki.llm.gateway import (
    LLMParseError,
    MissingRecordingError,
    ProviderOutputError,
    ProviderUnavailableError,
)
from evowiki.llm.providers import ProviderNoticeError, ProviderRejectedError
from evowiki.llm.usage import BudgetExceededError

log = logging.getLogger(__name__)


class ToolLimitExceeded(RuntimeError):
    """The model called tools on the forced-answer call (`tool_choice="none"`)."""


class FirstToolRefused(RuntimeError):
    """A tool call the run made itself, before any model call (`run_loop(first=...)`), returned an
    error: the run stops there. `detail` = the tool's reason (the text to show)."""

    def __init__(self, name: str, detail: str):
        super().__init__(f"{name} refused: {detail}")
        self.name, self.detail = name, detail


class PauseUnavailable(RuntimeError):
    """A tool that does not declare `pauses` / has no `resume` returned a pause, a call the run
    made itself (`first`) asked the learner, or a resume names no pausing tool: a wiring error."""

    def __init__(self, name: str):
        super().__init__(f"{name} asked the learner, but this run cannot be resumed with a reply")
        self.name = name


class Cancelled(RuntimeError):
    """The run was cancelled (client gone, or the caller asked to stop)."""


@dataclass(frozen=True)
class ErrorInfo:
    code: str
    retryable: bool


def classify(exc: BaseException) -> ErrorInfo:
    if isinstance(exc, ToolLimitExceeded):
        return ErrorInfo("tool_limit", False)
    if isinstance(exc, Cancelled | RequestCancelled):
        return ErrorInfo("cancelled", False)
    if isinstance(exc, FirstToolRefused):
        return ErrorInfo("refused", False)
    if isinstance(exc, BudgetExceededError):
        return ErrorInfo("budget_exceeded", False)
    if isinstance(exc, ProviderRejectedError):
        return ErrorInfo("llm_unavailable", False)
    if isinstance(exc, ProviderNoticeError):      # llm_busy (429 / queued) or llm_timeout
        return ErrorInfo(exc.code, True)
    if isinstance(exc, MissingRecordingError | ProviderUnavailableError | ProviderOutputError
                  | LLMParseError):
        return ErrorInfo("llm_unavailable", True)
    if isinstance(exc, ContextOverBudget):
        return ErrorInfo("bad_request", False)
    log.exception("agent run failed")
    return ErrorInfo("internal", False)
