"""Per-user call-log keys and logical calls on a cache hit (observability plan §8 P0)."""
from datetime import UTC, datetime

from pydantic import BaseModel

from evowiki.llm.chat import ChatMessage, ChatRequest, ChatResponse, ChatTurn
from evowiki.llm.gateway import Gateway
from evowiki.llm.log import AttemptLog, CallLog
from evowiki.llm.models import GatewayConfig, LLMRequest, ModelRef, ProviderResponse
from evowiki.llm.prices import PriceTable
from evowiki.llm.providers import ProviderEmptyOutputError
from evowiki.llm.usage import Account
from evowiki.llm.usage_control import UsageControl

T0 = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
CFG = GatewayConfig(generator=ModelRef(provider="gen", model="g-1"),
                    verifier=ModelRef(provider="ver", model="v-1"))
U1 = Account(user_id="u1", group="page")
U2 = Account(user_id="u2", group="page")


class Answer(BaseModel):
    value: int


def _req(**over) -> LLMRequest:
    base = {"purpose": "unit", "prompt_version": "unit.v1", "subject": "x", "system": "sys",
            "prompt": "p"}
    return LLMRequest(**(base | over))


class Meter:
    def __init__(self):
        self.records = []

    def check(self, account):
        pass

    def record(self, account, purpose, usage, *, replayed):
        self.records.append((account.user_id, usage.call_id, replayed))


class Provider:
    def __init__(self, script):
        self.script = list(script)
        self.sent = 0

    def complete(self, model, request, json_schema):
        self.sent += 1
        out = self.script.pop(0)
        if isinstance(out, Exception):
            raise out
        return ProviderResponse(raw_output=out, model_version=model, input_tokens=10,
                                output_tokens=2)

    def chat(self, model, request):
        self.sent += 1
        return ChatResponse(turn=ChatTurn(text=self.script.pop(0)), model_version=model,
                            input_tokens=10, output_tokens=2)


def _gw(tmp_path, provider, meter):
    return Gateway(CFG, CallLog(tmp_path / "calls.jsonl"), mode="record",
                   providers={"gen": provider}, clock=lambda: T0,
                   attempts=AttemptLog(tmp_path / "att.jsonl"),
                   usage=UsageControl(meter, PriceTable({})))


def test_two_users_never_share_a_recorded_reply(tmp_path):
    p = Provider(['{"value": 1}', '{"value": 2}'])
    gw = _gw(tmp_path, p, Meter())
    a, ca = gw.call("generator", _req(), Answer, account=U1)
    b, cb = gw.call("generator", _req(), Answer, account=U2)
    assert (a.value, b.value, p.sent) == (1, 2, 2)
    assert ca.input_hash != cb.input_hash
    assert (ca.user_id, cb.user_id) == ("u1", "u2")


def test_a_hit_is_a_logical_call_of_its_own(tmp_path):
    meter, p = Meter(), Provider(['{"value": 1}'])
    gw = _gw(tmp_path, p, meter)
    _, first = gw.call("generator", _req(), Answer, account=U1)
    out, hit = gw.call("generator", _req(), Answer, account=U1)
    assert out.value == 1 and p.sent == 1
    assert hit.id != first.id and hit.id.startswith(first.id)
    assert (hit.served_from, hit.user_id, hit.cost, hit.replayable) == (first.id, "u1", 0.0,
                                                                         False)
    log = CallLog(tmp_path / "calls.jsonl")          # both rows persisted, the cache unchanged
    assert [c.id for c in log.calls()] == [first.id, hit.id]
    assert log.get(first.input_hash) == first
    # one paid record for the call, one zero replay for the hit: nothing billed twice
    assert meter.records == [("u1", first.id, False), ("u1", hit.id, True)]


def test_a_failed_call_bills_without_a_call_id(tmp_path):
    meter = Meter()
    p = Provider([ProviderEmptyOutputError("x", raw_output="", input_tokens=10, output_tokens=0,
                                           status="refusal")])
    gw = _gw(tmp_path, p, meter)
    try:
        gw.call("generator", _req(), Answer, account=U1)
    except ProviderEmptyOutputError:
        pass
    assert meter.records == [("u1", None, False)]


def test_converse_is_scoped_and_logged_the_same_way(tmp_path):
    meter, p = Meter(), Provider(["a", "b"])
    gw = _gw(tmp_path, p, meter)
    req = ChatRequest(purpose="unit.chat", prompt_version="v1", subject="s", system="sys",
                      messages=(ChatMessage(role="user", text="hi"),))
    t1, c1 = gw.converse("generator", req, account=U1)
    t2, c2 = gw.converse("generator", req, account=U2)
    t3, c3 = gw.converse("generator", req, account=U1)
    assert (t1.text, t2.text, t3.text, p.sent) == ("a", "b", "a", 2)
    assert c3.served_from == c1.id and c3.cost == 0.0
    assert meter.records == [("u1", c1.id, False), ("u2", c2.id, False), ("u1", c3.id, True)]


def test_without_a_user_a_hit_writes_nothing(tmp_path):
    p = Provider(['{"value": 1}'])
    gw = Gateway(CFG, CallLog(tmp_path / "calls.jsonl"), mode="record", providers={"gen": p},
                 clock=lambda: T0)
    _, first = gw.call("generator", _req(), Answer)
    _, again = gw.call("generator", _req(), Answer)
    assert again == first and len(CallLog(tmp_path / "calls.jsonl")) == 1
    assert '"user_id"' not in (tmp_path / "calls.jsonl").read_text()
