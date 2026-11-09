"""One job's own spending limit (「每页最多花」, D26): a `JobBudget` is the page job's single meter
of spend — every gateway the job uses (planning, writing) is metered through a `JobMeter` sharing
it — and each request is bounded by what is left.

Per request (`output_cap`, asked by the gateway before every provider attempt): the input is
estimated before the call (`tokens.estimate_tokens`, deliberately high) and priced at the
model's dearest input rate (cache writes included); what is left after it buys the request's
`max_tokens` at the output rate (peak price: off-peak only lowers it). When the input alone
does not fit, or not even one output token does, the call is refused before it is sent
(`PageBudgetExceeded`). A provider reads the cap through `max_output(default)`; a reply cut at a
cap the budget lowered is a budget stop, never a truncation to retry.

No limit (None, the default: no default cost caps) = nothing is refused or bounded; the spend is
still summed."""

import json
import math
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Lock
from typing import Any

from evowiki.llm.prices import PriceTable
from evowiki.llm.usage import Account, BudgetExceededError, Usage, UsageMeter
from evowiki.tokens import estimate_tokens


class PageBudgetExceeded(BudgetExceededError):
    """The page's own limit (not the user's day / month limit)."""

    def __init__(self, account: Account, limit_usd: float):
        super().__init__(account, "page", limit_usd)


class JobBudget:
    """What one job spent and its own limit (None = no cap). Shared by every JobMeter of the job."""

    def __init__(self, limit_usd: float | None = None):
        self.limit_usd = limit_usd
        self.spent = 0.0
        self._lock = Lock()

    def add(self, cost_usd: float | None) -> None:
        if cost_usd:
            with self._lock:
                self.spent += cost_usd

    def remaining(self) -> float | None:
        """What is left (never below 0); None without a limit."""
        return None if self.limit_usd is None else max(0.0, self.limit_usd - self.spent)

    def check(self, account: Account) -> None:
        if self.limit_usd is not None and self.spent >= self.limit_usd:
            raise PageBudgetExceeded(account, self.limit_usd)


class JobMeter:
    """Wraps the user's meter for one job: refuses a call once the job's budget is spent, and adds
    what each call cost to it (replays are free). `budget`: the job's shared budget; None = a new
    one with `limit_usd`."""

    def __init__(self, inner: UsageMeter, limit_usd: float | None = None, *,
                 budget: JobBudget | None = None):
        self._inner = inner
        self.budget = budget if budget is not None else JobBudget(limit_usd)

    @property
    def limit_usd(self) -> float | None:
        return self.budget.limit_usd

    @property
    def spent(self) -> float:
        return self.budget.spent

    def remaining(self) -> float | None:
        return self.budget.remaining()

    def check(self, account: Account) -> None:
        self.budget.check(account)
        self._inner.check(account)

    def record(self, account: Account, purpose: str, usage: Usage, *, replayed: bool) -> None:
        self._inner.record(account, purpose, usage, replayed=replayed)
        if not replayed:
            self.budget.add(usage.cost_usd)

    def restore(self, cost_usd: float) -> None:
        """Add what a replayed durable step charged earlier (no usage row: it was recorded then)."""
        self.budget.add(cost_usd)

    def charge(self, cost_usd: float | None) -> None:
        """Add what this job's forged-tool builds cost (request_tool; their calls are metered on
        the user's meter by the forge's own gateway, so only the page's spend is added here)."""
        self.budget.add(cost_usd)


@dataclass
class OutputCap:
    """The max_tokens the budget allows for one request; `bound`: a provider used it (it was below
    the provider's own max_tokens), so a reply cut there is a budget stop."""

    tokens: int
    bound: bool = False


_cap: ContextVar[OutputCap | None] = ContextVar("evowiki_output_cap", default=None)


def max_output(default: int) -> int:
    """The max_tokens a provider sends: its own `default`, or the budget's cap when lower."""
    cap = _cap.get()
    if cap is None or cap.tokens >= default:
        return default
    cap.bound = True
    return cap.tokens


@contextmanager
def capped(cap: OutputCap | None) -> Iterator[OutputCap | None]:
    token = _cap.set(cap)
    try:
        yield cap
    finally:
        _cap.reset(token)


def request_text(request: Any, json_schema: dict[str, Any] | None = None) -> str:
    """What a request sends, as text for the estimate: a JSON call's system + prompt + schema, or
    a chat request whole (messages, tool definitions; JSON quoting only adds to the estimate)."""
    schema = "" if json_schema is None else json.dumps(json_schema, ensure_ascii=False)
    prompt = getattr(request, "prompt", None)
    if prompt is not None:
        return f"{request.system}\n{prompt}\n{schema}"
    return request.model_dump_json()


def output_cap(meter: Any, account: Account, prices: PriceTable, provider: str, model: str,
               text: str, *, pending_usd: float = 0.0) -> OutputCap | None:
    """The request's cap from what the job has left (`meter.remaining()`, minus `pending_usd`: what
    this call's earlier attempts cost, not yet recorded); None when the meter has no limit.
    PageBudgetExceeded when the input alone, or one output token more, does not fit; a ValueError
    for a model the price table does not know (the limit could not hold)."""
    remaining = getattr(meter, "remaining", None)
    left = remaining() if remaining is not None else None
    if left is None:
        return None
    price = prices.price(provider, model)
    if price is None:
        raise ValueError(f"no price for {provider}:{model}: the page limit cannot hold for it")
    in_rate = max(price.input_per_mtok, price.cache_write_per_mtok or 0.0,
                  price.cache_write_1h_per_mtok or 0.0)
    prompt = estimate_tokens(text)
    factor = price.prompt_factor(prompt)
    left -= pending_usd + prompt * in_rate * factor / 1_000_000
    tokens = (math.floor(left * 1_000_000 / (price.output_per_mtok * factor))
              if price.output_per_mtok > 0
              else 1 << 30)
    if left <= 0 or tokens < 1:
        raise PageBudgetExceeded(account, meter.limit_usd)
    return OutputCap(tokens)
