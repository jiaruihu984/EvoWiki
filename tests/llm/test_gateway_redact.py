"""Plan 7.1-api Task 2b: audit-only redaction hook (LLMRequest.redact) and execution identity.

Redacted requests are never served from the cache or replayed; each accepted redacted execution
gets its own llm_call.id (non-replayable row) whose PERSISTED copy is redacted, while the caller
receives the live, unredacted output."""

import json
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel

from evowiki.llm.gateway import (
    REDACTED,
    Gateway,
    LLMParseError,
    MissingRecordingError,
    input_hash,
    redact_text,
)
from evowiki.llm.log import AttemptLog, CallLog
from evowiki.llm.models import GatewayConfig, LLMRequest, ModelRef
from evowiki.llm.providers import ProviderEmptyOutputError
from evowiki.llm.scripted import ScriptedProvider
from evowiki.runtime.llm import RUNTIME_RECORD_MAX_ATTEMPTS
from tests.runtime.llm_helpers import MemoryAttemptLog, MemoryCallLog

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
CONFIG = GatewayConfig(generator=ModelRef(provider="gen", model="g-1"),
                       verifier=ModelRef(provider="ver", model="v-1"))
SEL = "the exact selected passage about eigenvectors"
HEAD = "Heading: Principal Components"


class Echo(BaseModel):
    sentence: str
    nested: dict[str, list[str]]
    n: int


RAW = json.dumps({"sentence": SEL, "nested": {"ctx": [f"see {HEAD}", "plain", SEL]}, "n": 3})


def _req(**over) -> LLMRequest:
    base = {"purpose": "unit", "prompt_version": "unit.v1", "subject": "x",
            "system": "sys", "prompt": "p"}
    base.update(over)
    return LLMRequest(**base)


def _stores(kind, tmp_path):
    if kind == "jsonl":
        return CallLog(tmp_path / "c.jsonl"), AttemptLog(tmp_path / "a.jsonl")
    return MemoryCallLog(), MemoryAttemptLog()


STORES = pytest.mark.parametrize("kind", ["jsonl", "memory"])


def _gw(log, attempts, provider, mode="record", max_attempts=RUNTIME_RECORD_MAX_ATTEMPTS):
    # Redacted requests only run through the runtime/service gateway, which is single-attempt
    # (RUNTIME_RECORD_MAX_ATTEMPTS); multi-attempt behaviour is tested explicitly below.
    return Gateway(CONFIG, log, mode=mode, providers={"gen": provider}, clock=lambda: T0,
                   attempts=attempts, record_max_attempts=max_attempts)


class _RaisingProvider:
    def __init__(self, exc: Exception):
        self.exc = exc
        self.calls = 0

    def complete(self, model, request, json_schema):
        self.calls += 1
        raise self.exc


def _shape(obj):
    if isinstance(obj, dict):
        return {k: _shape(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_shape(v) for v in obj]
    return type(obj).__name__


def _clean(text: str | None) -> bool:
    return text is not None and SEL not in text and HEAD not in text


# --- helpers --------------------------------------------------------------------------------

def test_redact_text_replaces_longest_first_and_ignores_empty():
    assert REDACTED == "[client context redacted]"
    assert redact_text("a abc b", ("ab", "abc", "")) == f"a {REDACTED} b"
    assert redact_text("nothing here", ()) == "nothing here"
    assert redact_text("x", ("",)) == "x"


# --- live output vs persisted copies ----------------------------------------------------------

@STORES
def test_echo_is_live_but_not_persisted(tmp_path, kind):
    log, attempts = _stores(kind, tmp_path)
    prov = ScriptedProvider("gen", lambda m, r: RAW)
    out, call = _gw(log, attempts, prov).call("generator", _req(redact=(SEL, HEAD)), Echo)
    assert out.sentence == SEL and SEL in call.raw_output and HEAD in call.raw_output
    assert call.parsed_output["sentence"] == SEL and call.replayable is False
    [stored] = log.calls()
    assert stored.id == call.id and stored.input_hash == call.input_hash
    assert stored.replayable is False
    assert _clean(stored.raw_output) and REDACTED in stored.raw_output
    dumped = json.dumps(stored.parsed_output)
    assert _clean(dumped) and REDACTED in dumped
    assert _shape(stored.parsed_output) == _shape(call.parsed_output)
    assert stored.parsed_output["n"] == 3 and stored.parsed_output["nested"]["ctx"][1] == "plain"
    atts = attempts.attempts()
    assert [a.status for a in atts] == ["accepted"] and all(_clean(a.raw_output) for a in atts)
    if kind == "jsonl":
        text = (tmp_path / "c.jsonl").read_text(encoding="utf-8")
        text += (tmp_path / "a.jsonl").read_text(encoding="utf-8")
        assert SEL not in text and HEAD not in text
        line = json.loads((tmp_path / "c.jsonl").read_text(encoding="utf-8").splitlines()[0])
        assert line["replayable"] is False


@STORES
def test_provider_errors_are_redacted_in_the_audit_but_reraised_unchanged(tmp_path, kind):
    log, attempts = _stores(kind, tmp_path)
    exc = TimeoutError(f"timed out while sending {SEL} / {HEAD}")
    prov = _RaisingProvider(exc)
    with pytest.raises(TimeoutError) as ei:
        _gw(log, attempts, prov).call("generator", _req(redact=(SEL, HEAD)), Echo)
    assert ei.value is exc and SEL in str(ei.value)
    [att] = attempts.attempts()
    assert att.status == "provider_error" and _clean(att.error_message)
    assert REDACTED in att.error_message and log.calls() == []


@STORES
def test_provider_output_errors_redact_raw_output_and_message(tmp_path, kind):
    log, attempts = _stores(kind, tmp_path)
    exc = ProviderEmptyOutputError(f"cut off at {SEL}", raw_output=f'{{"sentence": "{SEL}',
                                   status="truncated")
    with pytest.raises(ProviderEmptyOutputError) as ei:
        _gw(log, attempts, _RaisingProvider(exc)).call("generator", _req(redact=(SEL,)), Echo)
    assert ei.value is exc and SEL in str(ei.value) and SEL in ei.value.raw_output
    atts = attempts.attempts()          # the retry with thinking off is redacted too
    assert len(atts) == 2
    for att in atts:
        assert att.status == "truncated" and _clean(att.raw_output) and _clean(att.error_message)


@STORES
def test_parse_errors_redact_the_audited_raw_output(tmp_path, kind):
    log, attempts = _stores(kind, tmp_path)
    prov = ScriptedProvider("gen", lambda m, r: f"not json {SEL}")
    with pytest.raises(LLMParseError):
        _gw(log, attempts, prov).call("generator", _req(redact=(SEL,)), Echo)
    [att] = attempts.attempts()
    assert att.status == "parse_error" and _clean(att.raw_output) and log.calls() == []


def test_redact_is_not_hashed_and_not_sent(tmp_path):
    ref = CONFIG.generator
    req = _req()
    assert input_hash(ref, req, "X") == input_hash(ref, req.model_copy(update={"redact": (SEL,)}),
                                                   "X")
    prov = ScriptedProvider("gen", lambda m, r: RAW)
    _gw(MemoryCallLog(), None, prov).call("generator", _req(redact=(SEL, HEAD)), Echo)
    [sent] = prov.calls
    assert sent.system == "sys" and sent.prompt == "p"  # providers build payloads from these only


# --- no redact, no change -------------------------------------------------------------------

@STORES
@pytest.mark.parametrize("redact", [(), ("",)])
def test_no_redact_is_byte_identical_to_today(tmp_path, kind, redact):
    log, attempts = _stores(kind, tmp_path)
    prov = ScriptedProvider("gen", lambda m, r: RAW)
    gw = _gw(log, attempts, prov)
    req = _req(redact=redact)
    out, call = gw.call("generator", req, Echo)
    h = input_hash(CONFIG.generator, req, "Echo")
    assert call.id == f"llm_{h[:20]}" and call.replayable is True
    [stored] = log.calls()
    assert stored == call and stored.raw_output == RAW and out.sentence == SEL
    assert attempts.attempts()[0].raw_output == RAW
    _, call2 = gw.call("generator", req, Echo)  # cache hit: no provider call, no attempt
    assert call2 == call and len(prov.calls) == 1 and len(attempts) == 1
    if kind == "jsonl":
        line = (tmp_path / "c.jsonl").read_text(encoding="utf-8").splitlines()[0]
        assert "replayable" not in json.loads(line)
        assert line == call.model_dump_json(
            exclude={"replayable", "user_id", "run_id", "served_from"})


# --- execution identity ---------------------------------------------------------------------

@STORES
def test_identical_redacted_requests_are_two_executions(tmp_path, kind):
    log, attempts = _stores(kind, tmp_path)
    prov = ScriptedProvider("gen", lambda m, r: RAW)
    gw = _gw(log, attempts, prov)
    req = _req(redact=(SEL, HEAD))
    h = input_hash(CONFIG.generator, req, "Echo")
    out1, c1 = gw.call("generator", req, Echo)
    out2, c2 = gw.call("generator", req, Echo)
    assert len(prov.calls) == 2
    assert out1.sentence == SEL and out2.sentence == SEL
    assert SEL in c1.raw_output and SEL in c2.raw_output
    assert c1.id != c2.id and f"llm_{h[:20]}" not in (c1.id, c2.id)
    for c in (c1, c2):
        assert c.id.startswith(f"llm_{h[:20]}_r") and len(c.id) == len(f"llm_{h[:20]}_r") + 32
    rows = log.calls()
    assert [r.id for r in rows] == [c1.id, c2.id] and len(log) == 2
    assert {r.input_hash for r in rows} == {h} and all(r.replayable is False for r in rows)
    assert all(_clean(r.raw_output) and _clean(json.dumps(r.parsed_output)) for r in rows)
    atts = attempts.attempts()
    assert [a.status for a in atts] == ["accepted", "accepted"]
    assert {a.input_hash for a in atts} == {h} and atts[0].id != atts[1].id
    assert log.get(h) is None  # non-replayable rows are never served
    if kind == "jsonl":
        reloaded = CallLog(tmp_path / "c.jsonl")
        assert reloaded.calls() == rows and reloaded.get(h) is None


@STORES
def test_redacted_request_never_consumes_a_replayable_row(tmp_path, kind):
    log, attempts = _stores(kind, tmp_path)
    prov = ScriptedProvider("gen", lambda m, r: RAW)
    gw = _gw(log, attempts, prov)
    _, plain = gw.call("generator", _req(), Echo)
    out, red = gw.call("generator", _req(redact=(SEL,)), Echo)
    assert len(prov.calls) == 2 and out.sentence == SEL
    assert red.input_hash == plain.input_hash and red.id != plain.id
    assert log.get(plain.input_hash) == plain
    _, again = gw.call("generator", _req(), Echo)  # non-redacted: still a cache hit
    assert again == plain and len(prov.calls) == 2
    assert [r.id for r in log.calls()] == [plain.id, red.id]


@STORES
def test_replay_mode_never_serves_redacted_requests(tmp_path, kind):
    log, attempts = _stores(kind, tmp_path)
    rec = ScriptedProvider("gen", lambda m, r: RAW)
    _gw(log, attempts, rec).call("generator", _req(), Echo)  # a replayable row exists
    _gw(log, attempts, rec).call("generator", _req(redact=(SEL,)), Echo)
    n_calls, n_att = len(log), len(attempts)
    prov = ScriptedProvider("gen", lambda m, r: RAW)
    replay = _gw(log, attempts, prov, mode="replay")
    with pytest.raises(MissingRecordingError, match="redacted calls are never replayable"):
        replay.call("generator", _req(redact=(SEL,)), Echo)
    assert prov.calls == [] and len(log) == n_calls and len(attempts) == n_att
    assert replay.call("generator", _req(), Echo)[0].sentence == SEL  # plain replay unchanged


# --- escaped echoes (review fix round 1): multi-line, quoted, backslash, non-ASCII selections ---

SEL_ESC = 'first line marmalade\nsecond "quoted" line \\ with a backslash\t中文选段'
ESC_TOKENS = ("marmalade", "选段", json.dumps("选段")[1:-1])  # fragments only SEL_ESC carries


def _esc_forms(s: str) -> tuple[str, ...]:
    return (s, json.dumps(s)[1:-1], json.dumps(s, ensure_ascii=False)[1:-1], repr(s)[1:-1])


def _leaks(text: str | None) -> list[str]:
    if text is None:
        return []
    return [t for t in (*_esc_forms(SEL_ESC), *ESC_TOKENS) if t in text]


def test_redact_text_public_contract_is_literal_only():
    escaped = json.dumps({"a": SEL_ESC})
    assert redact_text(escaped, (SEL_ESC,)) == escaped  # plain-string helper: no escaped forms
    assert redact_text(SEL_ESC, (SEL_ESC,)) == REDACTED


@STORES
@pytest.mark.parametrize("ascii_", [True, False])
def test_escaped_echoes_are_redacted_in_every_persisted_copy(tmp_path, kind, ascii_):
    log, attempts = _stores(kind, tmp_path)
    raw = json.dumps({"sentence": SEL_ESC, "nested": {"ctx": [SEL_ESC, "plain"]}, "n": 1},
                     ensure_ascii=ascii_)
    prov = ScriptedProvider("gen", lambda m, r: raw)
    out, call = _gw(log, attempts, prov).call("generator", _req(redact=(SEL_ESC,)), Echo)
    assert out.sentence == SEL_ESC and call.raw_output == raw  # live copy untouched
    [stored] = log.calls()
    assert _leaks(stored.raw_output) == [] and REDACTED in stored.raw_output
    assert json.loads(stored.raw_output)["nested"]["ctx"] == [REDACTED, "plain"]
    assert _leaks(json.dumps(stored.parsed_output, ensure_ascii=False)) == []
    assert _leaks(json.dumps(stored.parsed_output)) == []
    assert all(_leaks(a.raw_output) == [] for a in attempts.attempts())
    if kind == "jsonl":
        for name in ("c.jsonl", "a.jsonl"):
            assert _leaks((tmp_path / name).read_text(encoding="utf-8")) == []


@STORES
def test_escaped_forms_in_error_messages_and_output_errors_are_redacted(tmp_path, kind):
    log, attempts = _stores(kind, tmp_path)
    exc = ValueError(f"bad request body {SEL_ESC!r} / {json.dumps(SEL_ESC)}")
    with pytest.raises(ValueError) as ei:
        _gw(log, attempts, _RaisingProvider(exc)).call("generator", _req(redact=(SEL_ESC,)), Echo)
    assert ei.value is exc
    out_exc = ProviderEmptyOutputError(f"cut at {SEL_ESC}",
                                       raw_output=json.dumps({"sentence": SEL_ESC})[:-2],
                                       status="truncated")
    with pytest.raises(ProviderEmptyOutputError):
        _gw(log, attempts, _RaisingProvider(out_exc)).call(
            "generator", _req(redact=(SEL_ESC,)), Echo)
    atts = attempts.attempts()
    assert [a.status for a in atts] == ["provider_error", "truncated", "truncated"]
    for a in atts:
        assert _leaks(a.error_message) == [] and _leaks(a.raw_output) == []
        assert REDACTED in a.error_message


# --- master's record-mode retry loop (record_max_attempts) x redaction -----------------------

class _SequenceProvider:
    """Plays a fixed script: an Exception is raised, a str is returned as raw output."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = 0

    def complete(self, model, request, json_schema):
        from evowiki.llm.models import ProviderResponse
        step = self.steps[self.calls]
        self.calls += 1
        if isinstance(step, Exception):
            raise step
        return ProviderResponse(raw_output=step, model_version="g-1-seq", cost=0.0)


def _retry_script():
    raw_ok = json.dumps({"sentence": SEL_ESC, "nested": {"ctx": [SEL, "plain"]}, "n": 2},
                        ensure_ascii=False)
    return _SequenceProvider(
        ProviderEmptyOutputError(f"cut off at {SEL} / {SEL_ESC!r}",
                                 raw_output=json.dumps({"sentence": SEL_ESC})[:-2],
                                 status="truncated"),
        f"not json {SEL} {json.dumps(SEL_ESC)}",
        raw_ok,
    ), raw_ok


@STORES
def test_redacted_multi_attempt_redacts_every_attempt_and_ids_only_the_accepted(tmp_path, kind):
    log, attempts = _stores(kind, tmp_path)
    prov, raw_ok = _retry_script()
    out, call = _gw(log, attempts, prov, max_attempts=3).call(
        "generator", _req(redact=(SEL, SEL_ESC)), Echo)
    assert prov.calls == 3
    # live output and live LLMCall are unredacted
    assert out.sentence == SEL_ESC and out.nested["ctx"][0] == SEL and call.raw_output == raw_ok
    # the accepted execution has its own non-replayable id; never the canonical cache id
    h = call.input_hash
    assert call.id.startswith(f"llm_{h[:20]}_r") and len(call.id) == len(f"llm_{h[:20]}_r") + 32
    assert call.replayable is False
    [stored] = log.calls()
    assert stored.id == call.id and stored.replayable is False and log.get(h) is None
    assert _clean(stored.raw_output) and _leaks(stored.raw_output) == []
    assert _leaks(json.dumps(stored.parsed_output, ensure_ascii=False)) == []
    # failed attempts stay only in llm_attempt, and EVERY persisted attempt copy is redacted
    atts = attempts.attempts()
    assert [a.status for a in atts] == ["truncated", "parse_error", "accepted"]
    for a in atts:
        assert a.input_hash == h
        assert _leaks(a.raw_output) == [] and _leaks(a.error_message) == []
        assert a.raw_output is None or SEL not in a.raw_output
        assert a.error_message is None or SEL not in a.error_message
    assert REDACTED in atts[0].error_message and REDACTED in atts[1].raw_output
    if kind == "jsonl":
        for name in ("c.jsonl", "a.jsonl"):
            text = (tmp_path / name).read_text(encoding="utf-8")
            assert SEL not in text and _leaks(text) == []


@STORES
def test_redacted_multi_attempt_executions_each_get_their_own_id(tmp_path, kind):
    log, attempts = _stores(kind, tmp_path)
    _, c1 = _gw(log, attempts, _retry_script()[0], max_attempts=3).call(
        "generator", _req(redact=(SEL, SEL_ESC)), Echo)
    _, c2 = _gw(log, attempts, _retry_script()[0], max_attempts=3).call(
        "generator", _req(redact=(SEL, SEL_ESC)), Echo)
    assert c1.input_hash == c2.input_hash and c1.id != c2.id
    assert {c.id for c in log.calls()} == {c1.id, c2.id}
    assert [a.status for a in attempts.attempts()].count("accepted") == 2


@STORES
def test_redacted_requests_respect_record_max_attempts_one(tmp_path, kind):
    log, attempts = _stores(kind, tmp_path)
    prov, _ = _retry_script()
    # truncated -> the one retry with thinking off (beyond the attempts) -> not json: no more
    with pytest.raises(LLMParseError):
        _gw(log, attempts, prov, max_attempts=1).call(
            "generator", _req(redact=(SEL, SEL_ESC)), Echo)
    assert prov.calls == 2 and log.calls() == []
    att, parsed = attempts.attempts()
    assert (att.status, parsed.status) == ("truncated", "parse_error")
    assert _leaks(att.raw_output) == [] and _clean(parsed.raw_output)
    assert _leaks(att.error_message) == [] and SEL not in att.error_message

    log2, attempts2 = _stores("memory", tmp_path)
    parse_fail = _SequenceProvider(f"not json {SEL}", RAW)
    with pytest.raises(LLMParseError):
        _gw(log2, attempts2, parse_fail, max_attempts=1).call(
            "generator", _req(redact=(SEL,)), Echo)
    assert parse_fail.calls == 1 and log2.calls() == []
    assert [a.status for a in attempts2.attempts()] == ["parse_error"]
    assert _clean(attempts2.attempts()[0].raw_output)


def _truncated_echo(i: int) -> ProviderEmptyOutputError:
    """A retryable output error whose message and raw_output carry the selection literally and
    in its JSON-escaped (ASCII and non-ASCII) and repr-escaped forms."""
    return ProviderEmptyOutputError(
        f"attempt {i} cut at {SEL} / {SEL_ESC!r} / {json.dumps(SEL_ESC)}",
        raw_output=(f'{{"sentence": "{SEL}", "a": {json.dumps(SEL_ESC)}, '
                    f'"b": {json.dumps(SEL_ESC, ensure_ascii=False)}'),
        status="truncated")


def _assert_attempts_clean(atts):
    for a in atts:
        assert _leaks(a.raw_output) == [] and _leaks(a.error_message) == []
        assert SEL not in (a.raw_output or "") and SEL not in (a.error_message or "")


@STORES
def test_exhausted_retryable_output_errors_redact_every_attempt(tmp_path, kind):
    log, attempts = _stores(kind, tmp_path)
    errs = [_truncated_echo(i) for i in (1, 2, 3)]
    prov = _SequenceProvider(*errs)
    with pytest.raises(ProviderEmptyOutputError) as ei:
        _gw(log, attempts, prov, max_attempts=3).call(
            "generator", _req(redact=(SEL, SEL_ESC)), Echo)
    # the second try is the one with thinking off: after it, it fails
    assert ei.value is errs[1] and SEL in str(ei.value) and SEL in ei.value.raw_output
    assert prov.calls == 2 and log.calls() == []
    atts = attempts.attempts()
    assert [a.status for a in atts] == ["truncated"] * 2
    _assert_attempts_clean(atts)
    assert all(REDACTED in a.error_message and REDACTED in a.raw_output for a in atts)
    if kind == "jsonl":
        text = (tmp_path / "a.jsonl").read_text(encoding="utf-8")
        assert SEL not in text and _leaks(text) == []


@STORES
def test_retryable_output_errors_then_accepted_redact_every_attempt(tmp_path, kind):
    log, attempts = _stores(kind, tmp_path)
    raw_ok = json.dumps({"sentence": SEL_ESC, "nested": {"ctx": [SEL]}, "n": 1})
    prov = _SequenceProvider(_truncated_echo(1), f"not json {SEL}", raw_ok)
    out, call = _gw(log, attempts, prov, max_attempts=3).call(
        "generator", _req(redact=(SEL, SEL_ESC)), Echo)
    assert prov.calls == 3
    assert out.sentence == SEL_ESC and out.nested["ctx"] == [SEL] and call.raw_output == raw_ok
    h = call.input_hash
    assert call.id.startswith(f"llm_{h[:20]}_r") and len(call.id) == len(f"llm_{h[:20]}_r") + 32
    assert call.replayable is False
    [stored] = log.calls()
    assert stored.id == call.id and stored.replayable is False and log.get(h) is None
    assert _leaks(stored.raw_output) == [] and SEL not in stored.raw_output
    atts = attempts.attempts()
    assert [a.status for a in atts] == ["truncated", "parse_error", "accepted"]
    _assert_attempts_clean(atts)
    assert REDACTED in atts[0].error_message and REDACTED in atts[0].raw_output
    assert REDACTED in atts[1].raw_output
