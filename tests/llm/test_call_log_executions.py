"""Plan 7.1-api Task 2b: JSONL CallLog / MemoryCallLog as an execution log.

Replayable rows stay unique per input_hash (the replay corpus served by get); non-replayable
rows are audit-only executions, several may share a hash, ids are always unique."""

from datetime import UTC, datetime

import pytest

from evowiki.llm.log import CallLog
from evowiki.llm.models import LLMCall
from tests.runtime.llm_helpers import MemoryCallLog

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
H = "a" * 64


def _call(cid: str, *, replayable: bool, h: str = H, raw: str = "{}") -> LLMCall:
    return LLMCall(id=cid, input_hash=h, purpose="unit", prompt_version="unit.v1", subject="x",
                   provider="gen", model="g-1", model_version=None, params={}, raw_output=raw,
                   parsed_output={}, cost=None, latency=0.1, ts=T0, replayable=replayable)


def test_llm_call_replayable_defaults_true():
    c = _call("llm_x", replayable=True)
    assert LLMCall.model_validate(c.model_dump(exclude={"replayable"})).replayable is True


def test_non_replayable_rows_share_a_hash_and_round_trip(tmp_path):
    path = tmp_path / "c.jsonl"
    r1, r2 = _call("llm_r1", replayable=False), _call("llm_r2", replayable=False)
    log = CallLog(path)
    log.append(r1)
    log.append(r2)
    assert log.get(H) is None and log.calls() == [r1, r2] and len(log) == 2
    reloaded = CallLog(path)
    assert reloaded.get(H) is None and reloaded.calls() == [r1, r2] and len(reloaded) == 2


def test_replayable_row_coexists_with_executions_in_file_order(tmp_path):
    path = tmp_path / "c.jsonl"
    r1, p, r2 = (_call("llm_r1", replayable=False), _call("llm_p", replayable=True),
                 _call("llm_r2", replayable=False))
    log = CallLog(path)
    for c in (r1, p, r2):
        log.append(c)
    reloaded = CallLog(path)
    assert reloaded.get(H) == p and reloaded.calls() == [r1, p, r2]
    lines = path.read_text(encoding="utf-8").splitlines()
    assert '"replayable"' not in lines[1] and '"replayable":false' in lines[0]


def test_duplicate_replayable_hash_still_raises(tmp_path):
    log = CallLog(tmp_path / "c.jsonl")
    log.append(_call("llm_p", replayable=True))
    with pytest.raises(ValueError, match="duplicate input_hash"):
        log.append(_call("llm_q", replayable=True))


@pytest.mark.parametrize("first,second", [(False, False), (True, False), (False, True)])
def test_duplicate_id_raises_on_append_and_on_load(tmp_path, first, second):
    path = tmp_path / "c.jsonl"
    log = CallLog(path)
    log.append(_call("llm_same", replayable=first))
    with pytest.raises(ValueError, match="duplicate llm_call id"):
        log.append(_call("llm_same", replayable=second, h="b" * 64))
    other = _call("llm_same", replayable=second, h="b" * 64)
    path.write_text(path.read_text(encoding="utf-8") + other.model_dump_json() + "\n",
                    encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate llm_call id"):
        CallLog(path)


def test_memory_call_log_keeps_executions_and_replayable_setdefault():
    log = MemoryCallLog()
    p, r1, r2 = (_call("llm_p", replayable=True), _call("llm_r1", replayable=False),
                 _call("llm_r2", replayable=False))
    for c in (r1, p, r2):
        log.append(c)
    log.append(_call("llm_p2", replayable=True))  # unchanged setdefault semantics: dropped
    assert log.get(H) == p and log.calls() == [r1, p, r2] and len(log) == 3
