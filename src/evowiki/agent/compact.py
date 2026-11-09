"""Session compaction, the agent-agnostic part: when a long conversation outgrows its token budget
the oldest whole turns are folded into a summary and the recent ones stay verbatim.

Pure functions over token counts and messages — no LLM, no storage. Callers own the summarising
prompt (and the model call that writes the summary) and the log that records where each cut fell.
Cuts are always between whole turns, never inside one, so a tool call is never separated from its
result (`tool_pairs_intact` lets a caller check that before sending a history)."""

from collections.abc import Sequence
from dataclasses import dataclass

from evowiki.llm.chat import ChatMessage


@dataclass(frozen=True)
class CompactPolicy:
    """`history_budget`: tokens of summary + live turns above which a cut is made. `keep_turns` /
    `keep_budget`: the most recent turns kept verbatim, and the tokens they may take together.
    `hard_cap`: beyond this even after folding, the oldest turns are dropped outright
    (`overflow_drop`). `summary_max_tokens`: the room one summary may take — the hard cap must
    leave it free on top of a full budget."""

    history_budget: int = 20_000
    keep_turns: int = 8
    keep_budget: int = 16_000
    hard_cap: int = 30_000
    summary_max_tokens: int = 4_000

    def __post_init__(self) -> None:
        if self.keep_turns < 1:
            raise ValueError("keep_turns must be at least 1")
        if not 0 < self.keep_budget <= self.history_budget:
            raise ValueError("keep_budget must be in (0, history_budget]")
        if self.summary_max_tokens <= 0:
            raise ValueError("summary_max_tokens must be positive")
        if self.hard_cap < self.history_budget + self.summary_max_tokens:
            raise ValueError("hard_cap must be at least history_budget + summary_max_tokens")


def choose_cut(turn_tokens: Sequence[int], summary_tokens: int, policy: CompactPolicy) -> int | None:
    """Where to fold. `turn_tokens` are the turns after the last cut, oldest first. None when all
    fits the budget, or when keeping the recent turns leaves nothing to fold; otherwise `cut` such
    that turns `[:cut]` are folded and `[cut:]` kept — as many as `keep_turns` and `keep_budget`
    allow, possibly none (a single huge recent turn is folded too rather than blowing the budget)."""
    n = len(turn_tokens)
    if summary_tokens + sum(turn_tokens) <= policy.history_budget:
        return None
    k = min(policy.keep_turns, n)
    while k > 0 and sum(turn_tokens[n - k:]) > policy.keep_budget:
        k -= 1
    return n - k if n - k > 0 else None


def overflow_drop(turn_tokens: Sequence[int], summary_tokens: int, policy: CompactPolicy) -> int:
    """The fallback when the summary could not be made (or the live turns still exceed the hard
    cap): how many oldest turns to drop. 0 while under `hard_cap`; past it, enough to get back
    under `history_budget`, which may be all of them."""
    if summary_tokens + sum(turn_tokens) <= policy.hard_cap:
        return 0
    n = len(turn_tokens)
    for d in range(n + 1):
        if summary_tokens + sum(turn_tokens[d:]) <= policy.history_budget:
            return d
    return n


def tool_pairs_intact(messages: Sequence[ChatMessage]) -> bool:
    """Whether every tool call is answered exactly once, right after the assistant message that made
    it, and no tool result stands without its call — what the provider APIs require of a history."""
    pending: set[str] | None = None      # call ids of the open assistant message still unanswered
    for m in messages:
        if m.role == "tool":
            if pending is None or m.tool_call_id not in pending:
                return False
            pending.discard(m.tool_call_id)
            continue
        if pending:
            return False
        pending = None
        if m.role == "assistant" and m.tool_calls:
            pending = {c.id for c in m.tool_calls}
    return not pending
