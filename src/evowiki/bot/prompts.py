"""Per-user prompt guidance (plan 2026-10-21 参数 redesign, D1 / D3): the user's own wording for a
model step's guidance span (llm/prompt_steps.py). Every change also writes an already-executed
ledger row (kind `prompt_override`, scope `teaching_params`), so it shows in 最近改了什么 under
讲解习惯 and is undone there (`bot.executors.PromptOverrideExecutor`). The gateway reads them
through the user's harness pack (`harness.source.reader`)."""

from collections.abc import Callable
from datetime import datetime

from evowiki.bot.actions import ActionLedger
from evowiki.llm.prompt_steps import STEP_BY_KEY, Step
from evowiki.store import prompts as store

KIND = "prompt_override"
SCOPE = "teaching_params"
SOURCE = "bot"
WHY = "你在 Prompt 设置里改的"
MAX_CHARS = 4000

EMPTY = "写法不能是空的。"
TOO_LONG = f"写法太长了，最多 {MAX_CHARS} 个字。"
MARKERS = "写法里不能有 ⟦ 或 ⟧。"
FORMAT = "这里只写怎么做；输出的格式是固定的，不能在这里改。"
_FORMAT_WORDS = ("json", "字段", "输出格式", "格式要求", "output format", "every field", "each field")


class PromptInvalid(ValueError):
    """A declared public validation reason (422), localized only when presented."""

    def __init__(self, message: str):
        super().__init__(message)
        self.public_message = message
        self.error_code = {EMPTY: "prompt_empty", TOO_LONG: "prompt_too_long",
                           MARKERS: "prompt_markers", FORMAT: "prompt_format"}[message]
        self.params = {}



def step(key: str) -> Step:
    """LookupError (404) for an unknown step."""
    s = STEP_BY_KEY.get(key)
    if s is None:
        raise LookupError(f"unknown prompt step {key!r}")
    return s


def validate(text: str) -> str:
    """The text to store (stripped), or PromptInvalid with the plain reason (plan D1)."""
    t = text.strip()
    if not t:
        raise PromptInvalid(EMPTY)
    if len(t) > MAX_CHARS:
        raise PromptInvalid(TOO_LONG)
    if "⟦" in t or "⟧" in t:
        raise PromptInvalid(MARKERS)
    low = t.lower()
    if any(w in low for w in _FORMAT_WORDS):
        raise PromptInvalid(FORMAT)
    return t


def summary(name: str, text: str | None) -> str:
    return f"「{name}」恢复了默认写法" if text is None else f"改了「{name}」的写法"


def change(pool, ledger: ActionLedger, user_id: str, key: str, text: str | None, *,
           default: str, now: datetime, new_id: Callable[[str], str]) -> str | None:
    """Set (`text`) or clear (None) the user's guidance of step `key`; returns the override now in
    force (None: the default). A text equal to the default clears the override. A change that
    changes nothing writes nothing; otherwise the override and its ledger row (content: step,
    before, after) commit together."""
    s = step(key)
    new = None if text is None else validate(text)
    if new == default.strip():
        new = None
    with pool.connection() as conn, conn.transaction():
        store.lock_step(conn, user_id, s.key)
        before = store.get_override(conn, user_id, s.key)
        if before == new:
            return before
        store.set_override(conn, user_id, s.key, new, now)
        ledger.record_in(conn, user_id, kind=KIND, scope=SCOPE, source=SOURCE,
                         summary=summary(s.name, new), why=WHY,
                         content={"step": s.key, "before": before, "after": new},
                         key=f"prompt:{new_id('prompt')}")
    return new
