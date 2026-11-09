"""Usage accounting contract (plan rev 3.2 §9.4). Every model call — plain JSON calls and the
assistant's tool-calling turns — reports here, and checks the budget here before it starts.

Lane G implements a persistent meter and wires it into `Gateway.call`; lane F1's tool-calling
path must call the same meter. The account never enters `LLMRequest` (that would change every
input hash and break recorded replays); it travels as a separate argument."""

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

PurposeGroup = Literal["page", "assistant", "curator", "diagnostic", "review", "other",
                      "proactive", "rsi"]


class Account(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: str = Field(min_length=1)
    group: PurposeGroup
    tier_pin: Literal["fast", "good"] | None = None   # H10 (harness pack); the user's own choice still wins


class BudgetExceededError(RuntimeError):
    """Raised by `UsageMeter.check` before any provider call. Callers fail closed: no silent
    downgrade to a cheaper model, no partial retry."""

    def __init__(self, account: Account, period: Literal["day", "month", "page"], limit_usd: float,
                 resets: str = ""):
        self.account, self.period, self.limit_usd = account, period, limit_usd
        self.error_code = "budget_exceeded"
        self.public_message = {
            "day": "Today's spending reached the ${limit} limit. This action was not completed. You can raise the limit in settings.",
            "month": "This month's spending reached the ${limit} limit. This action was not completed. You can raise the limit in settings.",
            "page": "Spending on this page reached the ${limit} limit. This action was not completed. You can raise the limit in settings.",
        }[period]
        self.params = {"period": period, "limit": f"{limit_usd:.2f}"}
        self.resets = resets            # 「明天 0 点恢复」, in the user's time; the meter knows it
        super().__init__(self.line("这次没有完成"))

    def line(self, outcome: str) -> str:
        """What the learner reads: which limit, how much, what did not happen, how it recovers."""
        name = {"day": "今天", "month": "这个月", "page": "这一页"}[self.period]
        head = f"{name}的花费到了上限 ${self.limit_usd:.2f}，{outcome}。"
        return f"{head}{self.resets}，也可以在设置里调高。" if self.resets else f"{head}可以在设置里调高。"


class Usage(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    model: str
    input_tokens: int = Field(ge=0)     # billed at the full input rate (uncached remainder)
    output_tokens: int = Field(ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)     # prompt-cache read
    cache_write_tokens: int = Field(default=0, ge=0)    # prompt-cache write (5-minute + 1-hour)
    cost_usd: float | None = None       # None when the price table has no entry
    call_id: str | None = None          # the llm_call it paid for (None: a failed call)


class UsageMeter(Protocol):
    def check(self, account: Account) -> None: ...

    def record(self, account: Account, purpose: str, usage: Usage, *, replayed: bool) -> None: ...


class NullMeter:
    """Default: no budget, nothing recorded (tests, offline tools)."""

    def check(self, account: Account) -> None:
        return None

    def record(self, account: Account, purpose: str, usage: Usage, *, replayed: bool) -> None:
        return None
