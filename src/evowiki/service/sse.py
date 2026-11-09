"""Server-sent events over FastAPI's native SSE (`fastapi.sse`).

A streaming route is a generator path operation with `response_class=EventSourceResponse`:
FastAPI frames each `ServerSentEvent`, sends a `: ping` comment after 15 s without an event (a
page write can be silent for minutes; a proxy or the plugin's transport would otherwise see a dead
connection) and sets the no-cache / no-buffering headers.

What has to happen before the stream opens (403 / 404 / 409 / 503 checks, starting the run) lives
in the route's dependency, which ends with `yield from closing_stream(events)`: an error there is
a real HTTP status, not an event, and the dependency's exit closes the iterator once the response
has ended for any reason - finished, failed or the client gone. (FastAPI stops pulling from a sync
generator when the client goes but never closes it; its exit stack runs the dependency's exit
after the stream's producer has stopped, so the close never races a running `next`.)"""

import json
from collections.abc import Callable, Iterator, Mapping
from typing import Any

from fastapi.sse import ServerSentEvent
from starlette.responses import Response

from evowiki.llm.providers import notice_error
from evowiki.store.usage import BudgetNotConfirmed
from evowiki.wikiapp.llm import LLM_FAILURES, LLM_UNAVAILABLE, retryable


def event(name: str, data: Mapping[str, Any], id: str | None = None) -> ServerSentEvent:
    """One `event:` line and one JSON `data:` line (UTF-8, not \\u-escaped); `id`: the SSE event id
    (a resume cursor), omitted when None."""
    return ServerSentEvent(event=name, raw_data=json.dumps(dict(data), ensure_ascii=False), id=id)


def closing_stream(events: Iterator[ServerSentEvent],
                   *sources: object) -> Iterator[Iterator[ServerSentEvent]]:
    """The tail of a stream dependency: yields `events` to the route; when the response has ended,
    closes `events`, then each of `sources` (the iterators `events` reads: closing an unstarted
    generator does not run its `finally`, so what it would close is closed here). Anything
    without a `close` is skipped; every close runs even when an earlier one raises."""
    try:
        yield events
    finally:
        _close_all([events, *sources])


def _close_all(items: list[object]) -> None:
    if not items:
        return
    try:
        close: Callable[[], object] | None = getattr(items[0], "close", None)
        if close is not None:
            close()
    finally:
        _close_all(items[1:])


class Answered(Exception):
    """Raised by a stream's dependency to answer with `response` instead of the stream (a JSON
    result, a refusal with a `code`); the app's handler returns it as is."""

    def __init__(self, response: Response):
        super().__init__(response.status_code)
        self.response = response


def error_payload(exc: BaseException) -> dict[str, Any]:
    """`error` event data for a stream that failed after it opened: LLM failures (including a
    transient provider error) are `llm_unavailable`, anything else `internal`; both retryable.
    A busy provider (429 / queued) is `llm_busy`, a request that timed out `llm_timeout`; both
    retryable, their detail the text to show (「请切换模型重试」 / 「请切换模型重试」)."""
    if isinstance(exc, BudgetNotConfirmed):
        return {"code": "refused", "retryable": False, "detail": str(exc)}
    notice = notice_error(exc)
    if notice is not None:
        return {"code": notice.code, "retryable": True, "detail": notice.user_text}
    code = "llm_unavailable" if isinstance(exc, (*LLM_UNAVAILABLE, *LLM_FAILURES)) else "internal"
    return {"code": code, "retryable": retryable(exc), "detail": f"{type(exc).__name__}: {exc}"[:500]}
