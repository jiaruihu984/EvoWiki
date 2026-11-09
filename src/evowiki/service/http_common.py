"""HTTP-layer helpers shared by the routers: response headers and the LLM-error mapping around a
flow call."""

from collections.abc import Iterator
from contextlib import contextmanager

from evowiki.deep.render import ASSET_CSP
from evowiki.wikiapp.llm import (
    LLM_FAILED_DETAIL,
    LLM_FAILURES,
    LLM_UNAVAILABLE,
    LLMFailed,
    LLMUnavailable,
)

ASSET_HEADERS = {"Content-Security-Policy": ASSET_CSP, "X-Content-Type-Options": "nosniff",
                 "Referrer-Policy": "no-referrer", "Cache-Control": "private, max-age=3600"}


@contextmanager
def llm_flow() -> Iterator[None]:
    """Around a JSON endpoint's flow call: a failed LLM call is 502 / an unreachable provider 503,
    never 500 (and a missing recording, a LookupError, never 404)."""
    try:
        yield
    except LLM_UNAVAILABLE as exc:
        raise LLMUnavailable("请切换模型重试") from exc
    except LLM_FAILURES as exc:
        raise LLMFailed(LLM_FAILED_DETAIL) from exc
