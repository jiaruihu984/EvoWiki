"""What lets a direct-tier tool take effect (design B3.3, stage 3 T1): the learner's explicit request
in THIS message (or his reply to a question this turn asked him), quoted verbatim in `asked`. A
quote from an earlier message, the selection, an attachment or a tool result never counts; neither
does a piece shorter than `MIN_QUOTE` characters unless it is his whole message (「考考我」 alone
counts, 「好」 out of 「好难」 does not). A yes to something the assistant proposed goes through
`confirm`, which binds it to one pending op. Code-only: no Skill, harness key or tool text
changes this check."""

import re
from typing import Any

from evowiki.agent.tools import ToolContext, ToolSpec
from evowiki.teach.textsim import negations, squash

ASKED = "asked"
# direct-tier tools that check their own grounds instead of `asked` (their `quote` must be his words
# of this message: remember_* and resolve_memory_conflict through `remember_route`; `confirm` binds
# to one pending op; `ask_check` / `ask_rough` to an open diagnosis): the only exceptions
SELF_CHECKING = frozenset({"remember_preference", "remember_about_me", "report_problem", "confirm",
                           "ask_check", "ask_rough", "resolve_memory_conflict"})
MIN_QUOTE = 4                      # characters after whitespace is removed
# the text the assistant-core-prompt session owns (B5 shortens it there)
ASKED_TEXT = "他这次要你这样做的原话，从他自己写的话里一字不改地照抄一段"


def current_words(ctx: ToolContext) -> tuple[str, ...]:
    """This message (and, in a turn that resumes a question, the message it asked in)."""
    return ctx.current_words


_NOT_TEXT = re.compile(r"[\W_]+")      # whitespace and punctuation


def bare(text: str) -> str:
    """The text without whitespace or punctuation (「考考我！」 → 考考我)."""
    return _NOT_TEXT.sub("", text)


def quoted_now(quote: Any, ctx: ToolContext, *, confirming: bool = False) -> bool:
    """`quote` is a verbatim piece (whitespace ignored) of this message or of a reply of this
    turn, and either at least MIN_QUOTE characters long or the whole of that message (bare: no
    whitespace or punctuation), so 「考考我」「撤销」 typed alone count while a two-character piece of a
    longer message does not. While a proposed op waits for his yes (`ctx.has_pending`), a short
    whole message (「好」) grounds only `confirm` (`confirming=True`), never another tool."""
    if not isinstance(quote, str):
        return False
    q = squash(quote)
    words = current_words(ctx)
    if not q or not any(q in squash(w) for w in words):
        return False
    if len(q) >= MIN_QUOTE:
        return True
    if not confirming and ctx.has_pending is not None and ctx.has_pending():
        return False
    b = bare(quote)
    return bool(b) and any(b == bare(w) for w in words)


def negated(quote: str) -> bool:
    """The quote opens with a negation (`teach.textsim.negations`: 「不好」「不好意思」「别」): it
    is no yes to a pending op."""
    b = bare(quote)
    return bool(b) and b[0] in negations(b[:2])


def grounded_spec(spec: ToolSpec) -> ToolSpec:
    """`spec` with the required `asked` argument, tier direct."""
    params = dict(spec.parameters)
    params["properties"] = {**params.get("properties", {}), ASKED: {
        "type": "string", "minLength": 1, "maxLength": 400, "description": ASKED_TEXT}}
    params["required"] = [*params.get("required", []), ASKED]
    return spec.model_copy(update={"parameters": params, "tier": "direct"})
