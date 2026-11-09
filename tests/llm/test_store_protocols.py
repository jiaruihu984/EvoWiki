from evowiki.llm.log import AttemptLog, AttemptStore, CallLog, CallStore


def test_jsonl_logs_satisfy_store_protocols(tmp_path):
    assert isinstance(CallLog(tmp_path / "c.jsonl"), CallStore)
    assert isinstance(AttemptLog(tmp_path / "a.jsonl"), AttemptStore)
