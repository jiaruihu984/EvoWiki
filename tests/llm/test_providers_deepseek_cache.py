"""DeepSeek context-cache accounting (https://api-docs.deepseek.com/guides/kv_cache): the cache is
on by default and a response reports `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`. When
both are present, input_tokens is the miss count, cache_read_tokens the hit count, and
cache_write_tokens 0 (no write charge); absent or None, the old reading holds (input_tokens =
prompt_tokens, no cache tokens). Complete, chat and the paid-failure paths; through the Gateway the
meter receives the counts and the cost uses the hit rate. Fake clients only, no network."""

from types import SimpleNamespace as NS

import pytest
from pydantic import BaseModel

from evowiki.llm.chat import ChatMessage, ChatRequest
from evowiki.llm.gateway import Gateway, ProviderOutputError
from evowiki.llm.log import CallLog
from evowiki.llm.models import GatewayConfig, LLMRequest, ModelRef
from evowiki.llm.prices import load_prices
from evowiki.llm.providers import DeepSeekProvider, ProviderEmptyOutputError, deepseek_tokens
from evowiki.llm.usage import Account
from evowiki.llm.usage_control import UsageControl

REQ = LLMRequest(purpose="p", prompt_version="v1", subject="s", system="SYS", prompt="PROMPT")
SCHEMA = {"type": "object", "properties": {"value": {"type": "integer"}}}
CHAT = ChatRequest(purpose="p", prompt_version="v1", subject="s", system="SYS",
                   messages=(ChatMessage(role="user", text="hi"),))
HIT_MISS = {"prompt_tokens": 60_000, "completion_tokens": 1_000,
            "prompt_cache_hit_tokens": 55_000, "prompt_cache_miss_tokens": 5_000}
PLAIN = {"prompt_tokens": 60_000, "completion_tokens": 1_000}


class Client:
    def __init__(self, usage, *, content='{"value": 1}', finish="stop", calls=None,
                 sdk=False):
        if sdk:
            openai_types = pytest.importorskip("openai.types.chat")
            body = {"id": "c1", "object": "chat.completion", "created": 0,
                    "model": "deepseek-v4-pro",
                    "choices": [{"index": 0, "finish_reason": finish,
                                 "message": {"role": "assistant", "content": content}}],
                    "usage": {**usage, "total_tokens": usage["prompt_tokens"]
                              + usage["completion_tokens"]}}
            self.resp = openai_types.ChatCompletion.model_validate(body)
        else:
            self.resp = NS(model="deepseek-v4-pro", usage=NS(**usage),
                           choices=[NS(finish_reason=finish, message=NS(
                               content=content, reasoning_content=None, tool_calls=calls))])
        self.chat = NS(completions=NS(create=lambda **kw: self.resp))


def _ds(usage, **kw):
    return DeepSeekProvider(client=Client(usage, **kw), env={}, max_tokens=100)


def _tokens(x):
    return (x.input_tokens, x.output_tokens, x.cache_read_tokens, x.cache_write_tokens)


# --- the reading ----------------------------------------------------------------------------

def test_hit_and_miss_split_the_prompt():
    assert deepseek_tokens(NS(**HIT_MISS)) == {
        "input_tokens": 5_000, "output_tokens": 1_000, "cache_read_tokens": 55_000,
        "cache_write_tokens": 0}


@pytest.mark.parametrize("usage", [
    PLAIN,
    {**PLAIN, "prompt_cache_hit_tokens": None, "prompt_cache_miss_tokens": None},
    {**PLAIN, "prompt_cache_hit_tokens": 55_000},                      # one field only
    {**PLAIN, "prompt_cache_hit_tokens": 55_000, "prompt_cache_miss_tokens": None},
])
def test_without_both_fields_the_old_reading_holds(usage):
    assert deepseek_tokens(NS(**usage)) == {
        "input_tokens": 60_000, "output_tokens": 1_000, "cache_read_tokens": 0,
        "cache_write_tokens": 0}


def test_no_usage_reports_none_and_no_cache():
    assert deepseek_tokens(None) == {"input_tokens": None, "output_tokens": None,
                                     "cache_read_tokens": 0, "cache_write_tokens": 0}


# --- complete / chat and their paid failures --------------------------------------------------

@pytest.mark.parametrize("sdk", [False, True])
def test_complete_reports_hits_as_cache_reads(sdk):
    r = _ds(HIT_MISS, sdk=sdk).complete("deepseek-v4-pro", REQ, SCHEMA)
    assert _tokens(r) == (5_000, 1_000, 55_000, 0)


@pytest.mark.parametrize("sdk", [False, True])
def test_complete_without_cache_fields_keeps_the_old_behaviour(sdk):
    r = _ds(PLAIN, sdk=sdk).complete("deepseek-v4-pro", REQ, SCHEMA)
    assert _tokens(r) == (60_000, 1_000, 0, 0)


@pytest.mark.parametrize(("kw", "status"), [({"finish": "length", "content": '{"va'}, "truncated"),
                                            ({"content": "  "}, "empty_output")])
def test_complete_paid_failures_carry_the_split(kw, status):
    with pytest.raises(ProviderEmptyOutputError) as info:
        _ds(HIT_MISS, **kw).complete("deepseek-v4-pro", REQ, SCHEMA)
    assert info.value.status == status and _tokens(info.value) == (5_000, 1_000, 55_000, 0)


def test_chat_reports_hits_on_the_response_and_the_turn():
    out = _ds(HIT_MISS, content="答").chat("deepseek-v4-pro", CHAT)
    assert _tokens(out) == (5_000, 1_000, 55_000, 0)
    assert _tokens(out.turn) == (5_000, 1_000, 55_000, 0)


def test_chat_without_cache_fields_keeps_the_old_behaviour():
    out = _ds(PLAIN, content="答").chat("deepseek-v4-pro", CHAT)
    assert _tokens(out) == (60_000, 1_000, 0, 0) and _tokens(out.turn) == (60_000, 1_000, 0, 0)


def test_chat_paid_failures_carry_the_split():
    with pytest.raises(ProviderEmptyOutputError) as info:
        _ds(HIT_MISS, content="半", finish="length").chat("m", CHAT)
    assert _tokens(info.value) == (5_000, 1_000, 55_000, 0)
    # a strict call without its one answer: paid, nothing usable
    with pytest.raises(ProviderOutputError) as info:
        DeepSeekProvider._strict_answer("p/s", "text", [], None, {
            "input_tokens": 5_000, "output_tokens": 1_000, "cache_read_tokens": 55_000,
            "cache_write_tokens": 0})
    assert info.value.status == "parse_error"
    assert _tokens(info.value) == (5_000, 1_000, 55_000, 0)
    # arguments that are not JSON no longer fail the call (they go back to the model): still metered
    bad = [NS(id="c1", function=NS(name="t", arguments="{bad"))]
    got = _ds(HIT_MISS, content=None, calls=bad).chat("m", CHAT)
    assert got.turn.tool_calls[0].unparsed == "{bad"
    assert _tokens(got) == (5_000, 1_000, 55_000, 0)


# --- through the Gateway: the meter gets the counts, the cost uses the hit rate -----------------

class Answer(BaseModel):
    value: int


class Meter:
    def __init__(self):
        self.rows = []

    def check(self, account):
        return None

    def record(self, account, purpose, usage, *, replayed):
        self.rows.append((purpose, usage, replayed))

    def chosen_tier(self, account):
        return None


CFG = GatewayConfig(generator=ModelRef(provider="deepseek", model="deepseek-v4-pro"),
                    verifier=ModelRef(provider="deepseek", model="deepseek-v4-pro"),
                    allow_single_provider=True)
ACCOUNT = Account(user_id="u1", group="page")
HIT_COST = (5_000 * 1.32 + 1_000 * 3.96 + 55_000 * 0.044) / 1e6        # $0.01298
MISS_COST = (60_000 * 1.32 + 1_000 * 3.96) / 1e6                        # $0.08316


def _gw(tmp_path, usage, **kw):
    meter = Meter()
    gw = Gateway(CFG, CallLog(tmp_path / "c.jsonl"), mode="record",
                 providers={"deepseek": _ds(usage, **kw)}, record_max_attempts=1,
                 usage=UsageControl(meter, load_prices()))
    return gw, meter


def test_gateway_call_meters_the_split_and_prices_hits_at_the_hit_rate(tmp_path):
    gw, meter = _gw(tmp_path, HIT_MISS)
    out, _ = gw.call("generator", REQ, Answer, account=ACCOUNT)
    assert out == Answer(value=1)
    [(purpose, u, replayed)] = meter.rows
    assert purpose == "p" and not replayed
    assert _tokens(u) == (5_000, 1_000, 55_000, 0)
    assert u.cost_usd == pytest.approx(HIT_COST) and HIT_COST == pytest.approx(0.01298)


def test_gateway_call_without_cache_fields_prices_everything_at_the_miss_rate(tmp_path):
    gw, meter = _gw(tmp_path, PLAIN)
    gw.call("generator", REQ, Answer, account=ACCOUNT)
    [(_, u, _)] = meter.rows
    assert _tokens(u) == (60_000, 1_000, 0, 0) and u.cost_usd == pytest.approx(MISS_COST)


def test_gateway_converse_meters_the_split(tmp_path):
    gw, meter = _gw(tmp_path, HIT_MISS, content="答")
    turn, _ = gw.converse("generator", CHAT, account=ACCOUNT)
    assert turn.text == "答"
    [(_, u, _)] = meter.rows
    assert _tokens(u) == (5_000, 1_000, 55_000, 0) and u.cost_usd == pytest.approx(HIT_COST)


def test_gateway_meters_a_paid_failure_with_the_split(tmp_path):
    gw, meter = _gw(tmp_path, HIT_MISS, finish="length", content='{"va')
    with pytest.raises(ProviderEmptyOutputError):
        gw.call("generator", REQ, Answer, account=ACCOUNT)
    assert len(meter.rows) == 2         # cut, then cut again with thinking off: both are paid
    for _, u, _ in meter.rows:
        assert _tokens(u) == (5_000, 1_000, 55_000, 0) and u.cost_usd == pytest.approx(HIT_COST)
