"""A replay-free gateway over a scripted provider, for the harness tests (same shape as the
gateway builder of tests/llm/test_prompt_steps.py)."""

from datetime import UTC, datetime

import pytest
from pydantic import BaseModel

from evowiki.llm.gateway import Gateway
from evowiki.llm.log import CallLog
from evowiki.llm.models import GatewayConfig, LLMRequest, ModelRef
from evowiki.llm.prices import PriceTable
from evowiki.llm.scripted import ScriptedProvider
from evowiki.llm.usage import Account
from evowiki.llm.usage_control import UsageControl

REF = ModelRef(provider="scripted", model="m")
CFG = GatewayConfig(generator=REF, verifier=REF, allow_single_provider=True)
ACC = Account(user_id="u1", group="page")


class _Meter:
    def check(self, account):
        pass

    def record(self, account, purpose, usage, *, replayed):
        pass


class Out(BaseModel):
    text: str


def request() -> LLMRequest:
    return LLMRequest(purpose="goals.intake", prompt_version="intake@v30",
                      subject="s", system="只回复 JSON。⟦g:decompose⟧系统里的规则。⟦/g⟧",
                      prompt="目标：x\n⟦g:decompose⟧1. 说具体。⟦/g⟧\n只回复 JSON。")


@pytest.fixture
def make_gateway(tmp_path):
    def make(**kw):
        prov = ScriptedProvider("scripted", lambda m, r: '{"text": "好"}')
        gw = Gateway(CFG, CallLog(tmp_path / "calls.jsonl"), mode="record",
                     providers={"scripted": prov},
                     usage=UsageControl(meter=_Meter(), prices=PriceTable({})),
                     clock=lambda: datetime(2026, 10, 21, tzinfo=UTC), **kw)
        gw.provider = prov
        return gw
    return make
