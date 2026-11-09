import json
from datetime import UTC, datetime

import pytest

from evowiki.llm.chat import ChatMessage, ChatRequest, ChatTurn, ToolCall, ToolDef, chat_input_hash
from evowiki.llm.gateway import Gateway, MissingRecordingError, ProviderOutputError
from evowiki.llm.log import AttemptLog, CallLog
from evowiki.llm.models import GatewayConfig, ModelRef
from evowiki.llm.prices import Price, PriceTable
from evowiki.llm.scripted import ScriptedChatProvider
from evowiki.llm.usage import Account, BudgetExceededError, Usage
from evowiki.llm.usage_control import UsageControl

REF = ModelRef(provider="scripted", model="m")
CFG = GatewayConfig(generator=REF, verifier=REF, allow_single_provider=True)
ACC = Account(user_id="u1", group="assistant")
PRICES = PriceTable({"scripted:m": Price(1.0, 2.0), "scripted:m-fast": Price(0.1, 0.2)})
TOOL = ToolDef(name="read_k", description="看 K", parameters={"type": "object", "properties": {}})


def req(text="z-score 是什么？", **kw) -> ChatRequest:
    return ChatRequest(purpose="assistant.turn", prompt_version="assistant@v1", subject="s1",
                       system="SYS", messages=(ChatMessage(role="user", text=text),),
                       tools=(TOOL,), **kw)


class Meter:
    def __init__(self, limit_hit=False):
        self.limit_hit, self.checks, self.records = limit_hit, [], []

    def check(self, account):
        self.checks.append(account)
        if self.limit_hit:
            raise BudgetExceededError(account, "day", 1.0)

    def record(self, account, purpose, usage, *, replayed):
        self.records.append((account, purpose, usage, replayed))


def gw(tmp_path, chat, *, mode="record", meter=None, attempts=None, max_attempts=3,
       tiers=None):
    """Metered by default (a spy Meter), so tests pass ACC; meter=False builds no usage
    control at all."""
    prov = ScriptedChatProvider("scripted", chat)
    usage = None if meter is False else UsageControl(
        meter=Meter() if meter is None else meter, prices=PRICES, tier_configs=tiers)
    g = Gateway(CFG, CallLog(tmp_path / "calls.jsonl"), mode=mode,
                providers={"scripted": prov}, usage=usage, attempts=attempts,
                record_max_attempts=max_attempts,
                clock=lambda: datetime(2026, 10, 8, tzinfo=UTC))
    return g, prov


def test_record_then_serve_from_log(tmp_path):
    turn = ChatTurn(text="", tool_calls=(ToolCall(id="c1", name="read_k", args={"x": 1}),),
                    input_tokens=10, output_tokens=3)
    g, prov = gw(tmp_path, lambda m, r: turn)
    a, call = g.converse("generator", req(), account=ACC)
    b, _ = g.converse("generator", req(), account=ACC)
    assert a == b == turn and len(prov.chat_calls) == 1
    assert call.purpose == "assistant.turn" and call.prompt_version == "assistant@v1"
    assert ChatTurn.model_validate(call.parsed_output) == turn


def test_replay_mode_needs_a_recording_and_replays_it(tmp_path):
    g, _ = gw(tmp_path, lambda m, r: ChatTurn(text="答"))
    g.converse("generator", req(), account=ACC)
    replay = Gateway(CFG, CallLog(tmp_path / "calls.jsonl"), mode="replay",
                     usage=UsageControl(meter=Meter(), prices=PRICES))
    assert replay.converse("generator", req(), account=ACC)[0].text == "答"
    with pytest.raises(MissingRecordingError):
        replay.converse("generator", req("另一个问题"), account=ACC)


def test_account_is_not_in_the_request_but_keys_the_log(tmp_path):
    g, prov = gw(tmp_path, lambda m, r: ChatTurn(text="答"))
    g.converse("generator", req(), account=ACC)
    g.converse("generator", req(), account=Account(user_id="u2", group="page"))
    assert len(prov.chat_calls) == 2          # never another user's recording (plan §8 P0)
    assert "u1" not in prov.chat_calls[0].model_dump_json()


def test_hash_covers_messages_tools_choice_and_version():
    base = chat_input_hash(REF, req())
    assert base != chat_input_hash(REF, req("别的"))
    assert base != chat_input_hash(REF, req(tool_choice="none"))
    assert base != chat_input_hash(REF, req().model_copy(update={"tools": ()}))
    assert base != chat_input_hash(REF, req().model_copy(update={"prompt_version": "assistant@v2"}))


def test_meter_checked_before_and_recorded_after_with_replayed_flag(tmp_path):
    # Mirrors Gateway.call: the budget is checked only before a provider call; a paid turn is
    # priced from the price table; a turn served from the log is a zero-token replay.
    meter = Meter()
    g, _ = gw(tmp_path, lambda m, r: ChatTurn(text="答", input_tokens=7, output_tokens=2),
              meter=meter)
    g.converse("generator", req(), account=ACC)
    g.converse("generator", req(), account=ACC)
    assert meter.checks == [ACC]
    assert [(a, p, u.model_copy(update={"cost_usd": None, "call_id": None}), rep)
            for a, p, u, rep in meter.records] == [
        (ACC, "assistant.turn", Usage(provider="scripted", model="m", input_tokens=7,
                                      output_tokens=2), False),
        (ACC, "assistant.turn", Usage(provider="scripted", model="m", input_tokens=0,
                                      output_tokens=0), True)]
    assert meter.records[0][2].cost_usd == pytest.approx(11e-6)   # (7 * 1 + 2 * 2) / 1e6
    assert meter.records[1][2].cost_usd is None                   # replays are never billed


def test_no_account_is_never_metered(tmp_path):
    meter = Meter()
    g, prov = gw(tmp_path, lambda m, r: ChatTurn(text="答", input_tokens=7), meter=meter)
    assert g.metered
    assert g.converse("generator", req())[0].text == "答"
    assert meter.checks == [] and meter.records == [] and len(prov.chat_calls) == 1


def test_account_without_usage_control_fails_closed(tmp_path):
    g, prov = gw(tmp_path, lambda m, r: ChatTurn(text="答"), meter=False)
    assert not g.metered
    with pytest.raises(ValueError, match="no usage control"):
        g.converse("generator", req(), account=ACC)
    assert prov.chat_calls == []
    assert g.converse("generator", req())[0].text == "答"     # unmetered use is unchanged


def test_tier_routing_applies_to_converse(tmp_path):
    fast = GatewayConfig(generator=ModelRef(provider="scripted", model="m-fast"),
                         verifier=REF, allow_single_provider=True)
    meter, models = Meter(), []

    def chat(model, r):
        models.append(model)
        return ChatTurn(text="答", input_tokens=10, output_tokens=5)

    g, _ = gw(tmp_path, chat, meter=meter, tiers={"fast": fast, "good": CFG})
    _, fast_call = g.converse("generator", req(), account=Account(user_id="u1", group="curator"))
    _, good_call = g.converse("generator", req(), account=ACC)
    assert models == ["m-fast", "m"]
    assert (fast_call.model, good_call.model) == ("m-fast", "m")
    assert fast_call.input_hash != good_call.input_hash        # the hash uses the routed ref
    assert [r[2].cost_usd for r in meter.records] == [pytest.approx(2e-6),
                                                      pytest.approx(20e-6)]


def test_every_paid_chat_attempt_is_billed_including_failures(tmp_path):
    outs = iter([ProviderOutputError("bad args", status="parse_error", input_tokens=4,
                                     output_tokens=1),
                 ChatTurn(text="  ", input_tokens=3, output_tokens=1),
                 ChatTurn(text="答", input_tokens=7, output_tokens=2)])

    def chat(m, r):
        out = next(outs)
        if isinstance(out, Exception):
            raise out
        return out

    meter = Meter()
    g, _ = gw(tmp_path, chat, meter=meter)
    g.converse("generator", req(), account=ACC)
    assert [(u.input_tokens, u.output_tokens, rep) for _, _, u, rep in meter.records] == [
        (4, 1, False), (3, 1, False), (7, 2, False)]


def test_a_paid_chat_failure_is_recorded_even_when_the_turn_fails(tmp_path):
    def refuse(m, r):
        raise ProviderOutputError("no", status="refusal", input_tokens=9, output_tokens=0)

    meter = Meter()
    g, _ = gw(tmp_path, refuse, meter=meter)
    with pytest.raises(ProviderOutputError):
        g.converse("generator", req(), account=ACC)
    assert [(u.input_tokens, u.cost_usd, rep) for _, _, u, rep in meter.records] == [
        (9, pytest.approx(9e-6), False)]


def test_a_provider_error_without_a_response_is_not_billed(tmp_path):
    def down(m, r):
        raise ConnectionError("down")

    meter = Meter()
    g, _ = gw(tmp_path, down, meter=meter)
    with pytest.raises(ConnectionError):
        g.converse("generator", req(), account=ACC)
    assert meter.checks == [ACC] and meter.records == []


def test_budget_exceeded_stops_before_the_provider(tmp_path):
    g, prov = gw(tmp_path, lambda m, r: ChatTurn(text="答"), meter=Meter(limit_hit=True))
    with pytest.raises(BudgetExceededError):
        g.converse("generator", req(), account=ACC)
    assert prov.chat_calls == []


def test_empty_turn_is_retried_and_audited(tmp_path):
    outs = iter([ChatTurn(text="  "), ChatTurn(text="答")])
    attempts = AttemptLog(tmp_path / "att.jsonl")
    g, _ = gw(tmp_path, lambda m, r: next(outs), attempts=attempts)
    assert g.converse("generator", req(), account=ACC)[0].text == "答"
    assert [a.status for a in attempts.attempts()] == ["empty_output", "accepted"]


def test_retryable_provider_error_gives_up_after_max_attempts(tmp_path):
    def boom(m, r):
        raise ProviderOutputError("bad args", status="parse_error")
    g, prov = gw(tmp_path, boom, max_attempts=2)
    with pytest.raises(ProviderOutputError):
        g.converse("generator", req(), account=ACC)
    assert len(prov.chat_calls) == 2 and len(g._log) == 0


def test_provider_without_chat_is_unavailable(tmp_path):
    from evowiki.llm.gateway import ProviderUnavailableError
    from evowiki.llm.scripted import ScriptedProvider
    meter = Meter()
    g = Gateway(CFG, CallLog(tmp_path / "c.jsonl"), mode="record",
                providers={"scripted": ScriptedProvider("scripted", lambda m, r: "{}")},
                usage=UsageControl(meter=meter, prices=PRICES))
    with pytest.raises(ProviderUnavailableError):
        g.converse("generator", req(), account=ACC)
    assert meter.checks == []                 # nothing to call: the budget is not consulted


def test_call_log_line_is_json_with_turn_as_parsed_output(tmp_path):
    g, _ = gw(tmp_path, lambda m, r: {"text": "答", "input_tokens": 1})
    g.converse("generator", req(), account=ACC)
    row = json.loads((tmp_path / "calls.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert row["parsed_output"]["text"] == "答"


def test_a_falsy_meter_is_still_enforced(tmp_path):
    class EmptyMeter(Meter):
        def __len__(self):            # falsy, e.g. a meter that counts its accounts
            return 0

    meter = EmptyMeter(limit_hit=True)
    assert not meter
    g, prov = gw(tmp_path, lambda m, r: ChatTurn(text="答"), meter=meter)
    with pytest.raises(BudgetExceededError):
        g.converse("generator", req(), account=ACC)
    assert prov.chat_calls == []


def test_a_chat_turn_with_unreported_usage_is_billed_cost_none(tmp_path):
    # G's rule for usage_of: unreported tokens are recorded as 0 with cost None (visible,
    # never guessed), even though the turn's own token fields default to 0.
    from evowiki.llm.chat import ChatResponse

    class NoUsage:
        def chat(self, model, request):
            return ChatResponse(turn=ChatTurn(text="答"), model_version="m")

    meter = Meter()
    g = Gateway(CFG, CallLog(tmp_path / "c.jsonl"), mode="record",
                providers={"scripted": NoUsage()},
                usage=UsageControl(meter=meter, prices=PRICES))
    g.converse("generator", req(), account=ACC)
    [(_, _, u, replayed)] = meter.records
    assert (u.input_tokens, u.output_tokens, u.cost_usd, replayed) == (0, 0, None, False)


def test_chat_cache_tokens_reach_the_meter_and_replays_record_zero(tmp_path):
    meter = Meter()
    g, _ = gw(tmp_path, lambda m, r: ChatTurn(text="答", input_tokens=7, output_tokens=2,
                                              cache_read_tokens=1000, cache_write_tokens=300),
              meter=meter)
    turn, _ = g.converse("generator", req(), account=ACC)
    assert (turn.cache_read_tokens, turn.cache_write_tokens) == (1000, 300)   # in the log too
    g.converse("generator", req(), account=ACC)
    (_, _, paid, rep1), (_, _, replay, rep2) = meter.records
    assert (paid.cache_read_tokens, paid.cache_write_tokens, rep1) == (1000, 300, False)
    # scripted:m has no cache rates: cache tokens at its input rate
    assert paid.cost_usd == pytest.approx((7 + 1000 + 300) * 1e-6 + 2 * 2e-6)
    assert (replay.cache_read_tokens, replay.cache_write_tokens, rep2) == (0, 0, True)


def test_a_paid_chat_failure_carries_its_cache_tokens(tmp_path):
    def refuse(m, r):
        raise ProviderOutputError("no", status="refusal", input_tokens=9, output_tokens=0,
                                  cache_read_tokens=500, cache_write_tokens=40)

    meter = Meter()
    g, _ = gw(tmp_path, refuse, meter=meter)
    with pytest.raises(ProviderOutputError):
        g.converse("generator", req(), account=ACC)
    [(_, _, u, rep)] = meter.records
    assert (u.input_tokens, u.cache_read_tokens, u.cache_write_tokens, rep) == (9, 500, 40, False)
    assert u.cost_usd == pytest.approx(549e-6)


def test_a_cache_heavy_conversation_costs_less_than_uncached(tmp_path):
    # A 59k-character first message (~30000 tokens) re-sent for 10 turns on claude-sonnet-5-5:
    # turn 1 writes it to the cache, turns 2-10 read it. Each turn adds 500 fresh input tokens
    # and 400 output tokens. Prices from the default price file.
    from evowiki.llm.prices import load_prices

    sonnet = ModelRef(provider="anthropic", model="claude-sonnet-5-5")
    cfg = GatewayConfig(generator=sonnet, verifier=sonnet, allow_single_provider=True)
    n = iter(range(10))

    def chat(m, r):
        i = next(n)
        return ChatTurn(text=f"答{i}", input_tokens=500, output_tokens=400,
                        cache_write_tokens=30_000 if i == 0 else 0,
                        cache_read_tokens=0 if i == 0 else 30_000)

    meter = Meter()
    g = Gateway(cfg, CallLog(tmp_path / "c.jsonl"), mode="record",
                providers={"anthropic": ScriptedChatProvider("anthropic", chat)},
                usage=UsageControl(meter=meter, prices=load_prices()))
    for i in range(10):
        g.converse("generator", req(f"第 {i} 轮"), account=ACC)
    cached = sum(u.cost_usd for _, _, u, _ in meter.records)
    # turn 1: 500*2 + 400*10 + 30000*2.50 = 80000; turns 2-10: 500*2 + 400*10 + 30000*0.20
    # = 11000 each; total (80000 + 9*11000) / 1e6
    assert cached == pytest.approx(0.179)
    uncached = 10 * (30_500 * 2.0 + 400 * 10.0) / 1e6
    assert uncached == pytest.approx(0.65)
    assert cached < uncached / 3
