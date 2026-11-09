import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, ValidationError

from evowiki.llm.gateway import (
    RECORD_MAX_ATTEMPTS,
    Gateway,
    LLMParseError,
    MissingRecordingError,
    ProviderUnavailableError,
)
from evowiki.llm.log import CallLog
from evowiki.llm.models import GatewayConfig, LLMCall, LLMRequest, ModelRef
from evowiki.llm.scripted import ScriptedProvider

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
CONFIG = GatewayConfig(generator=ModelRef(provider="gen", model="g-1"),
                       verifier=ModelRef(provider="ver", model="v-1"))


class Answer(BaseModel):
    value: int


def _req(**over) -> LLMRequest:
    base = {"purpose": "unit", "prompt_version": "unit.v1", "subject": "x",
            "system": "sys", "prompt": "p"}
    base.update(over)
    return LLMRequest(**base)


def _providers(gen_out='{"value": 1}', ver_out='{"value": 2}'):
    return {"gen": ScriptedProvider("gen", lambda m, r: gen_out),
            "ver": ScriptedProvider("ver", lambda m, r: ver_out)}


def test_config_requires_distinct_providers():
    with pytest.raises(ValidationError, match="different providers"):
        GatewayConfig(generator=ModelRef(provider="a", model="x"),
                      verifier=ModelRef(provider="a", model="y"))


def test_record_mode_calls_once_then_serves_from_log(tmp_path):
    providers = _providers()
    gw = Gateway(CONFIG, CallLog(tmp_path / "calls.jsonl"), mode="record",
                 providers=providers, clock=lambda: T0)
    out1, call1 = gw.call("generator", _req(), Answer)
    out2, call2 = gw.call("generator", _req(), Answer)
    assert out1 == out2 == Answer(value=1)
    assert call1 == call2 and len(providers["gen"].calls) == 1
    assert call1.provider == "gen" and call1.model == "g-1" and call1.ts == T0
    assert call1.parsed_output == {"value": 1} and call1.raw_output == '{"value": 1}'


def test_roles_route_to_their_provider(tmp_path):
    gw = Gateway(CONFIG, CallLog(tmp_path / "c.jsonl"), mode="record", providers=_providers(),
                 clock=lambda: T0)
    assert gw.call("verifier", _req(), Answer)[0].value == 2


def test_replay_reads_recordings_without_providers(tmp_path):
    path = tmp_path / "calls.jsonl"
    Gateway(CONFIG, CallLog(path), mode="record", providers=_providers(),
            clock=lambda: T0).call("generator", _req(), Answer)
    replay = Gateway(CONFIG, CallLog(path), mode="replay")
    assert replay.call("generator", _req(), Answer)[0] == Answer(value=1)
    with pytest.raises(MissingRecordingError, match="unit/x"):
        replay.call("generator", _req(params={"temperature": 0.5}), Answer)


def test_record_without_provider_names_the_problem(tmp_path):
    gw = Gateway(CONFIG, CallLog(tmp_path / "c.jsonl"), mode="record", providers={})
    with pytest.raises(ProviderUnavailableError, match="gen"):
        gw.call("generator", _req(), Answer)


def test_unparseable_record_output_is_not_cached_so_a_retry_reaches_the_provider(tmp_path):
    providers = _providers(gen_out="not json")
    log = CallLog(tmp_path / "c.jsonl")
    gw = Gateway(CONFIG, log, mode="record", providers=providers, clock=lambda: T0)
    for _ in range(2):
        with pytest.raises(LLMParseError):
            gw.call("generator", _req(), Answer)
    assert len(providers["gen"].calls) == 2 * RECORD_MAX_ATTEMPTS
    assert log.calls() == []


def test_replayed_legacy_parse_failure_still_raises(tmp_path):
    path = tmp_path / "c.jsonl"
    Gateway(CONFIG, CallLog(path), mode="record", providers=_providers(),
            clock=lambda: T0).call("generator", _req(), Answer)
    rec = json.loads(path.read_text(encoding="utf-8"))
    rec.update(raw_output="not json", parsed_output=None)
    path.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    with pytest.raises(LLMParseError):
        Gateway(CONFIG, CallLog(path), mode="replay").call("generator", _req(), Answer)


def test_fenced_json_is_accepted(tmp_path):
    gw = Gateway(CONFIG, CallLog(tmp_path / "c.jsonl"), mode="record",
                 providers=_providers(gen_out='```json\n{"value": 7}\n```'), clock=lambda: T0)
    assert gw.call("generator", _req(), Answer)[0].value == 7


def test_call_log_rejects_duplicate_hash(tmp_path):
    path = tmp_path / "c.jsonl"
    Gateway(CONFIG, CallLog(path), mode="record", providers=_providers(),
            clock=lambda: T0).call("generator", _req(), Answer)
    line = path.read_text(encoding="utf-8")
    path.write_text(line + line, encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate input_hash"):
        CallLog(path)


def test_llm_call_has_spec_fields():
    assert {"id", "input_hash", "purpose", "prompt_version", "provider", "model",
            "model_version", "params", "raw_output", "parsed_output", "cost", "latency",
            "ts"} <= set(LLMCall.model_fields)


def test_log_lines_are_json(tmp_path):
    path = tmp_path / "c.jsonl"
    Gateway(CONFIG, CallLog(path), mode="record", providers=_providers(),
            clock=lambda: T0).call("generator", _req(), Answer)
    rec = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert rec["purpose"] == "unit" and rec["id"].startswith("llm_")


# --- attempt audit trail (every provider call in record mode, incl. failures) ---

class _RaisingProvider:
    def __init__(self, exc: Exception):
        self.exc = exc
        self.calls = 0

    def complete(self, model, request, json_schema):
        self.calls += 1
        raise self.exc


def _gw_with_attempts(tmp_path, providers, log=None):
    from evowiki.llm.log import AttemptLog

    attempts = AttemptLog(tmp_path / "attempts.jsonl")
    log = log or CallLog(tmp_path / "c.jsonl")
    gw = Gateway(CONFIG, log, mode="record", providers=providers, clock=lambda: T0,
                 attempts=attempts)
    return gw, log, attempts


def test_accepted_call_records_attempt_and_call(tmp_path):
    gw, log, attempts = _gw_with_attempts(tmp_path, _providers())
    _, call = gw.call("generator", _req(), Answer)
    [att] = attempts.attempts()
    assert att.status == "accepted" and att.input_hash == call.input_hash
    assert att.raw_output == '{"value": 1}' and att.cost == 0.0
    assert att.model_version == "gen-scripted" and att.provider == "gen" and att.model == "g-1"
    assert att.error_type is None and att.error_message is None
    assert att.latency >= 0 and att.ts == T0 and att.id.startswith("att_")
    assert len(log) == 1


def test_parse_error_attempt_is_recorded_but_not_replayable(tmp_path):
    providers = _providers(gen_out="not json")
    gw, log, attempts = _gw_with_attempts(tmp_path, providers)
    for _ in range(2):
        with pytest.raises(LLMParseError):
            gw.call("generator", _req(), Answer)
    n = 2 * RECORD_MAX_ATTEMPTS
    assert len(providers["gen"].calls) == n and log.calls() == []
    atts = attempts.attempts()
    assert [a.status for a in atts] == ["parse_error"] * n
    assert all(a.raw_output == "not json" for a in atts)
    assert len({a.input_hash for a in atts}) == 1 and len({a.id for a in atts}) == n


@pytest.mark.parametrize("status", ["truncated", "empty_output", "refusal"])
def test_provider_output_errors_record_their_status_and_carried_fields(tmp_path, status):
    from evowiki.llm.providers import ProviderEmptyOutputError, ProviderRefusalError

    cls = ProviderRefusalError if status == "refusal" else ProviderEmptyOutputError
    exc = cls("boom", raw_output='{"val', model_version="m-2026", cost=0.25, status=status)
    gw, log, attempts = _gw_with_attempts(tmp_path, {"gen": _RaisingProvider(exc)})
    with pytest.raises(cls):
        gw.call("generator", _req(), Answer)
    atts = attempts.attempts()
    # refusals: no retry; truncated / empty: once more with thinking off, then it fails
    assert len(atts) == (1 if status == "refusal" else 2)
    assert [a.params.get("thinking") for a in atts][1:] == [False] * (len(atts) - 1)
    for att in atts:
        assert att.status == status and att.raw_output == '{"val'
        assert att.model_version == "m-2026" and att.cost == 0.25
        assert att.error_type == cls.__name__ and att.error_message == "boom"
    assert log.calls() == []


def test_generic_provider_exception_is_recorded_and_reraised(tmp_path):
    prov = _RaisingProvider(TimeoutError("read timed out " + "x" * 1000))
    gw, log, attempts = _gw_with_attempts(tmp_path, {"gen": prov})
    with pytest.raises(TimeoutError):
        gw.call("generator", _req(), Answer)
    [att] = attempts.attempts()
    assert att.status == "provider_error" and att.error_type == "TimeoutError"
    assert att.error_message.startswith("read timed out") and len(att.error_message) == 500
    assert att.raw_output is None and att.cost is None and att.latency >= 0
    assert log.calls() == []


def test_replay_and_cache_hits_create_no_attempt(tmp_path):
    gw, log, attempts = _gw_with_attempts(tmp_path, _providers())
    gw.call("generator", _req(), Answer)
    gw.call("generator", _req(), Answer)  # cache hit
    assert len(attempts) == 1
    from evowiki.llm.log import AttemptLog

    replay_attempts = AttemptLog(tmp_path / "replay_attempts.jsonl")
    replay = Gateway(CONFIG, log, mode="replay", attempts=replay_attempts)
    replay.call("generator", _req(), Answer)
    assert len(replay_attempts) == 0


# --- record-mode retries (parse_error / truncated / empty_output) ---

class _SequenceProvider:
    """Plays a script of outcomes: a str is returned as raw output, an exception is raised."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def complete(self, model, request, json_schema):
        from evowiki.llm.models import ProviderResponse

        out = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(out, BaseException):
            raise out
        return ProviderResponse(raw_output=out, model_version="seq-1", cost=0.5)


def _truncated():
    from evowiki.llm.providers import ProviderEmptyOutputError

    return ProviderEmptyOutputError("cut", raw_output='{"val', model_version="seq-1",
                                    status="truncated")


def _subdir(tmp_path, name):
    d = tmp_path / name
    d.mkdir()
    return d


def test_record_mode_max_attempts_is_three():
    assert RECORD_MAX_ATTEMPTS == 3


@pytest.mark.parametrize("first", ["parse_error", "truncated", "empty_output"])
def test_failure_then_success_records_one_call_and_two_attempts(tmp_path, first):
    from evowiki.llm.providers import ProviderEmptyOutputError

    failure = {"parse_error": "not json", "truncated": _truncated(),
               "empty_output": ProviderEmptyOutputError("empty", status="empty_output")}[first]
    prov = _SequenceProvider([failure, '{"value": 7}'])
    gw, log, attempts = _gw_with_attempts(tmp_path, {"gen": prov})
    parsed, call = gw.call("generator", _req(), Answer)
    assert parsed.value == 7 and prov.calls == 2
    assert [c.id for c in log.calls()] == [call.id] and call.raw_output == '{"value": 7}'
    assert [a.status for a in attempts.attempts()] == [first, "accepted"]
    assert len(CallLog(tmp_path / "c.jsonl").calls()) == 1
    gw.call("generator", _req(), Answer)  # now a cache hit: no further provider call
    assert prov.calls == 2 and len(attempts) == 2


def test_three_failures_raise_and_audit_three_attempts(tmp_path):
    from evowiki.llm.providers import ProviderEmptyOutputError

    prov = _SequenceProvider(["not json", _truncated(), _truncated(), '{"value": 1}'])
    gw, log, attempts = _gw_with_attempts(_subdir(tmp_path, "a"), {"gen": prov})
    with pytest.raises(ProviderEmptyOutputError):
        gw.call("generator", _req(), Answer)
    assert prov.calls == 3 and log.calls() == []
    assert [a.status for a in attempts.attempts()] == ["parse_error", "truncated", "truncated"]

    # truncated twice: the second try was the one with thinking off, so it fails there
    prov2 = _SequenceProvider([_truncated(), _truncated(), "not json", '{"value": 1}'])
    gw2, log2, attempts2 = _gw_with_attempts(_subdir(tmp_path, "b"), {"gen": prov2})
    with pytest.raises(ProviderEmptyOutputError):
        gw2.call("generator", _req(), Answer)
    assert prov2.calls == 2 and log2.calls() == []
    assert [a.status for a in attempts2.attempts()] == ["truncated", "truncated"]


def test_refusal_and_generic_errors_are_not_retried(tmp_path):
    from evowiki.llm.providers import ProviderRefusalError

    prov = _SequenceProvider([ProviderRefusalError("no"), '{"value": 1}'])
    gw, _, attempts = _gw_with_attempts(_subdir(tmp_path, "a"), {"gen": prov})
    with pytest.raises(ProviderRefusalError):
        gw.call("generator", _req(), Answer)
    assert prov.calls == 1 and [a.status for a in attempts.attempts()] == ["refusal"]

    prov2 = _SequenceProvider([TimeoutError("t"), '{"value": 1}'])
    gw2, _, attempts2 = _gw_with_attempts(_subdir(tmp_path, "b"), {"gen": prov2})
    with pytest.raises(TimeoutError):
        gw2.call("generator", _req(), Answer)
    assert prov2.calls == 1 and [a.status for a in attempts2.attempts()] == ["provider_error"]


def test_replay_mode_never_calls_providers_or_retries(tmp_path):
    from evowiki.llm.log import AttemptLog

    prov = _SequenceProvider(["not json"] * 5)
    attempts = AttemptLog(tmp_path / "a.jsonl")
    gw = Gateway(CONFIG, CallLog(tmp_path / "c.jsonl"), mode="replay",
                 providers={"gen": prov}, attempts=attempts)
    with pytest.raises(MissingRecordingError):
        gw.call("generator", _req(), Answer)
    assert prov.calls == 0 and len(attempts) == 0


def test_attempt_log_round_trip_allows_shared_hash_but_not_shared_id(tmp_path):
    from evowiki.llm.log import AttemptLog
    from evowiki.llm.models import LLMAttempt

    base = {"input_hash": "h" * 64, "purpose": "unit", "prompt_version": "unit.v1", "subject": "x",
                "provider": "gen", "model": "g-1", "model_version": None, "params": {}, "raw_output": None,
                "error_type": None, "error_message": None, "cost": None, "latency": 0.1, "ts": T0}
    a1 = LLMAttempt(id="att_1", status="provider_error", **base)
    a2 = LLMAttempt(id="att_2", status="accepted", **{**base, "raw_output": "{}"})
    path = tmp_path / "attempts.jsonl"
    log = AttemptLog(path)
    log.append(a1)
    log.append(a2)
    reloaded = AttemptLog(path)
    assert reloaded.attempts() == [a1, a2] and len(reloaded) == 2
    with pytest.raises(ValueError, match="duplicate attempt id"):
        reloaded.append(a1)
    with pytest.raises(ValidationError):
        LLMAttempt(id="att_3", status="weird", **base)
    with pytest.raises(ValidationError):
        LLMAttempt(id="att_3", status="accepted", **{**base, "ts": datetime(2026, 1, 1)})  # noqa: DTZ001


def test_single_provider_mode_must_be_explicit():
    same = {"generator": ModelRef(provider="deepseek", model="m"),
            "verifier": ModelRef(provider="deepseek", model="m")}
    with pytest.raises(ValueError):
        GatewayConfig(**same)
    cfg = GatewayConfig(**same, allow_single_provider=True)
    assert cfg.single_provider


def test_two_providers_are_not_single_provider():
    assert not CONFIG.single_provider


def test_record_max_attempts_one_makes_exactly_one_call_on_parse_error(tmp_path):
    from evowiki.llm.log import AttemptLog

    prov = _SequenceProvider(["not json", '{"value": 1}'])
    attempts = AttemptLog(tmp_path / "a.jsonl")
    log = CallLog(tmp_path / "c.jsonl")
    gw = Gateway(CONFIG, log, mode="record", providers={"gen": prov}, clock=lambda: T0,
                 attempts=attempts, record_max_attempts=1)
    with pytest.raises(LLMParseError):
        gw.call("generator", _req(), Answer)
    assert prov.calls == 1 and log.calls() == []
    assert [a.status for a in attempts.attempts()] == ["parse_error"]


@pytest.mark.parametrize("bad", [0, -1, True, 2.0])
def test_record_max_attempts_must_be_a_positive_int(tmp_path, bad):
    with pytest.raises(ValueError, match="record_max_attempts"):
        Gateway(CONFIG, CallLog(tmp_path / "c.jsonl"), mode="record",
                providers=_providers(), record_max_attempts=bad)


def test_a_parse_error_carries_the_cost_of_the_unusable_replies(tmp_path):
    providers = {"gen": ScriptedProvider("gen", lambda m, r: "not json"),
                 "ver": ScriptedProvider("ver", lambda m, r: "{}")}
    orig = providers["gen"].complete

    def priced(model, request, schema):
        return orig(model, request, schema).model_copy(update={"cost": 0.25})

    providers["gen"].complete = priced
    gw, _, _ = _gw_with_attempts(tmp_path, providers)
    with pytest.raises(LLMParseError) as e:
        gw.call("generator", _req(), Answer)
    assert e.value.cost == pytest.approx(0.25 * RECORD_MAX_ATTEMPTS)


def test_two_identical_calls_at_once_pay_once_and_both_get_the_answer(tmp_path):
    entered, release, asked = threading.Event(), threading.Event(), []

    def answer(_m, _r):
        asked.append(1)
        entered.set()
        release.wait(10)
        return '{"value": 1}'

    log = CallLog(tmp_path / "calls.jsonl")
    g = Gateway(CONFIG, log, mode="record", providers={"gen": ScriptedProvider("gen", answer)}, clock=lambda: T0)
    with ThreadPoolExecutor(2) as ex:
        first = ex.submit(g.call, "generator", _req(), Answer)
        assert entered.wait(10)
        second = ex.submit(g.call, "generator", _req(), Answer)
        time.sleep(0.05)                          # it reached the provider, were nothing to stop it
        release.set()
        assert first.result()[0] == second.result()[0] == Answer(value=1)
    assert len(asked) == 1 and len(log) == 1


def test_a_call_waiting_for_an_identical_one_that_failed_asks_the_provider_itself(tmp_path):
    entered, release, asked = threading.Event(), threading.Event(), []

    def answer(_m, _r):
        asked.append(1)
        if len(asked) == 1:
            entered.set()
            release.wait(10)
            raise RuntimeError("the first one failed")
        return '{"value": 2}'

    g = Gateway(CONFIG, CallLog(tmp_path / "calls.jsonl"), mode="record",
                providers={"gen": ScriptedProvider("gen", answer)}, clock=lambda: T0)
    with ThreadPoolExecutor(2) as ex:
        first = ex.submit(g.call, "generator", _req(), Answer)
        assert entered.wait(10)
        second = ex.submit(g.call, "generator", _req(), Answer)
        time.sleep(0.05)
        release.set()
        with pytest.raises(RuntimeError, match="the first one failed"):
            first.result()
        assert second.result()[0] == Answer(value=2)
    assert len(asked) == 2
