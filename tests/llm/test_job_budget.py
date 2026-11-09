"""「每页最多花」 (D26): one job budget meters every gateway of the job, each request's max_tokens is
what is left, a request that cannot fit is refused before it is sent, and a reply cut at a cap the
budget lowered is a budget stop. No limit: nothing changes."""
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel

from evowiki.deep.llm import DeepLLM
from evowiki.llm.budget import JobBudget, JobMeter, PageBudgetExceeded, max_output
from evowiki.llm.chat import ChatMessage, ChatRequest, ChatResponse, ChatTurn
from evowiki.llm.gateway import Gateway, ProviderOutputError
from evowiki.llm.log import CallLog
from evowiki.llm.models import GatewayConfig, LLMRequest, ModelRef, ProviderResponse
from evowiki.llm.prices import Price, PriceTable
from evowiki.llm.usage import Account, NullMeter
from evowiki.llm.usage_control import UsageControl
from evowiki.wikiapp.llm import CountingGateway

REF = ModelRef(provider="fake", model="m")
CFG = GatewayConfig(generator=REF, verifier=REF, allow_single_provider=True)
PRICES = PriceTable({"fake:m": Price(1.0, 10.0)})       # $1 / $10 per million in / out
ACC = Account(user_id="u1", group="page")
T0 = datetime(2026, 10, 28, tzinfo=UTC)
DEFAULT = 16000                                         # the provider's own max_tokens
IN, OUT = 10, 1000      # tokens a reply reports: input below any estimate, output up to the cap


class Answer(BaseModel):
    value: int


def _req(n: int) -> LLMRequest:
    return LLMRequest(purpose="unit", prompt_version="v1", subject=f"s{n}", system="sys",
                      prompt=f"p{n}")


def _chat(subject: str = "s", text: str = "hi") -> ChatRequest:
    return ChatRequest(purpose="deep.write", prompt_version="v1", subject=subject, system="S",
                       messages=(ChatMessage(role="user", text=text),))


class CapProvider:
    """Records the max_tokens each attempt sends; a reply uses IN input tokens and up to OUT
    output tokens (never more than the cap). `truncate`: replies cut at max_tokens (status
    truncated) this many times first."""

    def __init__(self, truncate: int = 0):
        self.sent: list[int] = []
        self.truncate = truncate

    def _out(self) -> int:
        limit = max_output(DEFAULT)
        self.sent.append(limit)
        if self.truncate:
            self.truncate -= 1
            raise ProviderOutputError("cut", status="truncated", input_tokens=IN,
                                      output_tokens=min(OUT, limit))
        return min(OUT, limit)

    def complete(self, model, request, json_schema):
        out = self._out()
        return ProviderResponse(raw_output='{"value": 1}', model_version=model,
                                input_tokens=IN, output_tokens=out)

    def chat(self, model, request):
        out = self._out()
        return ChatResponse(turn=ChatTurn(text="ok", input_tokens=IN, output_tokens=out),
                            model_version=model, input_tokens=IN, output_tokens=out)


def _gw(tmp_path, provider, meter, *, attempts=1, name="c.jsonl", prices=PRICES):
    return Gateway(CFG, CallLog(tmp_path / name), mode="record", providers={"fake": provider},
                   record_max_attempts=attempts, clock=lambda: T0,
                   usage=UsageControl(meter, prices))


def test_max_tokens_shrinks_as_the_budget_runs_down_then_stops_before_the_call(tmp_path):
    p = CapProvider()
    meter = JobMeter(NullMeter(), 0.05)
    gw = _gw(tmp_path, p, meter)
    n = 0
    with pytest.raises(PageBudgetExceeded):
        while n < 20:
            gw.call("generator", _req(n), Answer, account=ACC)
            n += 1
    assert n >= 4 and len(p.sent) == n                   # the refused call was never sent
    assert all(t < DEFAULT for t in p.sent)
    assert p.sent == sorted(p.sent, reverse=True) and len(set(p.sent)) == n
    assert meter.spent <= 0.05                           # never past the limit


def test_a_request_whose_input_alone_does_not_fit_is_not_sent(tmp_path):
    p = CapProvider()
    gw = _gw(tmp_path, p, JobMeter(NullMeter(), 0.05),
             prices=PriceTable({"fake:m": Price(100_000.0, 10.0)}))
    with pytest.raises(PageBudgetExceeded):
        gw.call("generator", _req(1), Answer, account=ACC)
    assert p.sent == []


def test_a_reply_cut_at_the_budget_cap_is_a_budget_stop_not_a_retry(tmp_path):
    p = CapProvider(truncate=1)
    meter = JobMeter(NullMeter(), 0.005)                 # cap ~500 tokens, below 16000
    gw = _gw(tmp_path, p, meter, attempts=3)
    with pytest.raises(PageBudgetExceeded) as info:
        gw.call("generator", _req(1), Answer, account=ACC)
    assert isinstance(info.value.__cause__, ProviderOutputError)
    assert len(p.sent) == 1 and 0 < meter.spent <= 0.005    # paid, recorded, not retried


def test_a_reply_cut_at_the_providers_own_cap_is_still_retried(tmp_path):
    p = CapProvider(truncate=1)
    gw = _gw(tmp_path, p, JobMeter(NullMeter(), 100.0), attempts=3)   # cap above 16000
    parsed, _ = gw.call("generator", _req(1), Answer, account=ACC)
    assert parsed.value == 1 and p.sent == [DEFAULT, DEFAULT]


def test_no_limit_changes_nothing(tmp_path):
    p = CapProvider()
    meter = JobMeter(NullMeter(), None)
    gw = _gw(tmp_path, p, meter)
    for n in range(5):
        gw.call("generator", _req(n), Answer, account=ACC)
    gw.converse("generator", _chat(), account=ACC)
    assert p.sent == [DEFAULT] * 6
    assert meter.spent == pytest.approx(6 * (IN * 1.0 + OUT * 10.0) / 1e6)   # still summed


def test_planning_and_writing_share_one_page_budget(tmp_path):
    """The page job's planning gateway (`CountingGateway.budgeted`) and its writer
    (`DeepLLM.gateways(budget=...)`) meter one budget: planning spend lowers the writer's cap, and
    the writer is refused before a call that no longer fits."""
    plan_p, write_p = CapProvider(), CapProvider()
    budget = JobBudget(0.02)
    planning = CountingGateway(_gw(tmp_path, plan_p, NullMeter(), name="plan.jsonl"), "u1")
    llm = DeepLLM(config=CFG, providers={"fake": write_p}, log=CallLog(tmp_path / "w.jsonl"),
                  prices=PRICES, meter=NullMeter(), clock=lambda: T0)
    (writer, *_), meter = llm.gateways(None, "u1", budget=budget)
    with planning.budgeted(budget):
        planning.call("generator", _req(1), Answer)
    planning.call("generator", _req(2), Answer)          # outside the job: not its spend
    planned = budget.spent
    assert planning.calls == 2
    assert planned == pytest.approx((IN + OUT * 10.0) / 1e6) and meter.spent == planned
    writer.converse("generator", _chat("w1"), account=ACC)
    assert write_p.sent[0] < plan_p.sent[0]               # less left for the writer's request
    assert planned < budget.spent <= 0.02
    with pytest.raises(PageBudgetExceeded):
        writer.converse("generator", _chat("w2"), account=ACC)
    assert len(write_p.sent) == 1


def test_a_job_limit_on_an_unmetered_gateway_fails_closed(tmp_path):
    gw = Gateway(CFG, CallLog(tmp_path / "c.jsonl"), mode="record", providers={})
    assert gw.with_budget(JobBudget(None)) is gw
    with pytest.raises(ValueError, match="metered"):
        gw.with_budget(JobBudget(0.1))
