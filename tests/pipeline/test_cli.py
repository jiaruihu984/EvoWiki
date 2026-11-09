import io
import json
import shutil
import urllib.error
from datetime import timedelta
from pathlib import Path

import pytest

from evowiki.content.models import Source
from evowiki.pipeline.cli import main
from evowiki.pipeline.review import ReviewState, ReviewTarget
from evowiki.pipeline.sources import SourceDocument, SourceRegistration
from evowiki.pipeline.workspace import Workspace
from tests.pipeline.helpers import FIXTURE_SOURCE, GW_CONFIG, MINI_CONCEPTS, MINI_PASSAGES, NOW

FIXTURE_LOG = Path(__file__).resolve().parents[1] / "fixtures" / "pipeline" / "llm_calls.jsonl"


def _run(root, *argv, env=None, fetch=None):
    out = io.StringIO()
    kwargs = {"env": env or {}, "clock": lambda: NOW, "out": out}
    if fetch is not None:
        kwargs["fetch"] = fetch
    code = main(["--root", str(root), *argv], **kwargs)
    return code, out.getvalue()


def _fixture_ws(root: Path) -> Workspace:
    ws = Workspace(root)
    ws.save_registry([SourceRegistration(source=FIXTURE_SOURCE, license_marker="CC BY 4.0")])
    ws.save("core", MINI_CONCEPTS)
    ws.save_passages(MINI_PASSAGES)
    ws.save("glossary", [])
    (root / "pipeline.json").write_text(json.dumps({
        "gateway": GW_CONFIG.model_dump(mode="json"), "spot_check_fraction": 0.0,
        "demo_spine": [c.id for c in MINI_CONCEPTS], "spine_paths": [["pca", "svd", "lora"]],
        "perspective_concepts": ["pca"]}), encoding="utf-8")
    return ws


def test_status_never_prints_key_values(tmp_path):
    _fixture_ws(tmp_path)
    code, out = _run(tmp_path, "status", env={"ANTHROPIC_API_KEY": "sk-secret-value"})
    assert code == 0
    assert "ANTHROPIC_API_KEY: set" in out and "DEEPSEEK_API_KEY: unset" in out
    assert "sk-secret-value" not in out


def test_record_mode_without_keys_names_env_vars(tmp_path):
    ws = Workspace(tmp_path)
    ws.save("core", MINI_CONCEPTS)
    code, out = _run(tmp_path, "run", "--spine", "--mode", "record")
    assert code == 2
    assert "ANTHROPIC_API_KEY" in out and "DEEPSEEK_API_KEY" in out


def test_sources_verify_and_ingest(tmp_path):
    src = FIXTURE_SOURCE.model_copy(update={"license_verified_at": None})
    reg = SourceRegistration(source=Source.model_validate(src.model_dump()),
                             license_marker="CC BY 4.0",
                             documents=[SourceDocument(locator="tests://doc", title="Doc")])
    Workspace(tmp_path).save_registry([reg])
    pages = {"tests://fx/LICENSE": "Licensed under CC BY 4.0.",
             "tests://doc": "<p>A vector can be scaled.</p>"}
    code, out = _run(tmp_path, "sources", "verify", fetch=pages.__getitem__)
    assert code == 0 and "OK fx" in out
    assert Workspace(tmp_path).registry()[0].source.license_verified_at == NOW
    code, out = _run(tmp_path, "ingest", fetch=pages.__getitem__)
    assert code == 0 and [p.text for p in Workspace(tmp_path).load_passages()] == [
        "A vector can be scaled."]


def test_sources_verify_reports_failures(tmp_path):
    src = FIXTURE_SOURCE.model_copy(update={"license_verified_at": None})
    Workspace(tmp_path).save_registry([SourceRegistration(
        source=Source.model_validate(src.model_dump()), license_marker="CC BY 4.0")])
    code, out = _run(tmp_path, "sources", "verify", fetch=lambda url: "all rights reserved")
    assert code == 1 and "MISMATCH fx" in out


def test_review_approve_and_reject(tmp_path):
    ws = Workspace(tmp_path)
    item = ws.queue().enqueue(ReviewTarget.EDGE, "a|prerequisite_of|b", "single_provider")
    code, out = _run(tmp_path, "review", "list", "--state", "open")
    assert item.id in out
    with pytest.raises(SystemExit):
        _run(tmp_path, "review", "approve", item.id)
    code, _ = _run(tmp_path, "review", "approve", item.id, "--by", "jerry", "--note", "ok")
    assert code == 0 and ws.queue().get(item.id).state is ReviewState.APPROVED


def test_run_replay_and_snapshot(tmp_path):
    _fixture_ws(tmp_path)
    shutil.copyfile(FIXTURE_LOG, tmp_path / "llm_calls.jsonl")
    code, _ = _run(tmp_path, "run", "--all")
    assert code == 0
    code, out = _run(tmp_path, "snapshot")
    assert code == 0 and out.startswith("cv_")


def _verified_registry(root: Path) -> None:
    Workspace(root).save_registry([SourceRegistration(source=FIXTURE_SOURCE,
                                                      license_marker="CC BY 4.0")])
    assert Workspace(root).registry()[0].source.license_verified_at == NOW


def _run_at(root, when, *argv, fetch):
    out = io.StringIO()
    code = main(["--root", str(root), *argv], env={}, clock=lambda: when, out=out, fetch=fetch)
    return code, out.getvalue()


def test_sources_verify_mismatch_keeps_history_and_excludes_source(tmp_path):
    _verified_registry(tmp_path)
    later = NOW + timedelta(days=7)
    code, out = _run_at(tmp_path, later, "sources", "verify",
                        fetch=lambda url: "all rights reserved")
    assert code == 1 and "MISMATCH fx" in out
    src = Workspace(tmp_path).registry()[0].source
    assert src.license_check_status == "mismatch" and src.license_checked_at == later
    assert src.license_verified_at == NOW
    code, out = _run(tmp_path, "status")
    assert "sources: 0 eligible / 1 registered" in out


def test_sources_verify_http_404_marks_unavailable_then_recovers(tmp_path):
    _verified_registry(tmp_path)
    later = NOW + timedelta(days=7)

    def gone(url):
        raise urllib.error.HTTPError(url, 404, "Not Found", None, None)  # type: ignore[arg-type]

    code, out = _run_at(tmp_path, later, "sources", "verify", fetch=gone)
    assert code == 1 and "UNAVAILABLE fx: " in out and "404" in out
    src = Workspace(tmp_path).registry()[0].source
    assert src.license_check_status == "unavailable" and src.license_checked_at == later
    assert src.license_verified_at == NOW
    assert "sources: 0 eligible / 1 registered" in _run(tmp_path, "status")[1]

    recovered = later + timedelta(days=1)
    code, out = _run_at(tmp_path, recovered, "sources", "verify",
                        fetch=lambda url: "Licensed under CC BY 4.0.")
    assert code == 0 and "OK fx" in out
    src = Workspace(tmp_path).registry()[0].source
    assert src.license_check_status == "verified" and src.license_verified_at == recovered
    assert "sources: 1 eligible / 1 registered" in _run(tmp_path, "status")[1]


def test_review_decision_errors_are_one_line(tmp_path):
    ws = Workspace(tmp_path)
    item = ws.queue().enqueue(ReviewTarget.EDGE, "a|prerequisite_of|b", "single_provider")
    code, out = _run(tmp_path, "review", "approve", "rv_nope", "--by", "jerry")
    assert code == 1 and out == "error: no review item rv_nope\n"
    assert _run(tmp_path, "review", "reject", item.id, "--by", "jerry")[0] == 0
    code, out = _run(tmp_path, "review", "approve", item.id, "--by", "jerry")
    assert code == 1 and out.startswith("error: ") and "already decided" in out
    assert out.count("\n") == 1


def test_run_operator_errors_are_one_line(tmp_path):
    _fixture_ws(tmp_path)
    code, out = _run(tmp_path, "run", "--concept", "nope")
    assert code == 1 and out.startswith("error: ") and "nope" in out and out.count("\n") == 1
    code, out = _run(tmp_path, "run", "--concept", "pca")  # empty call log: cache miss
    assert code == 1 and out.startswith("error: no recorded llm_call") and out.count("\n") == 1


def test_snapshot_validation_failure_is_one_line_and_cleans_up(tmp_path, monkeypatch):
    _fixture_ws(tmp_path)
    shutil.copyfile(FIXTURE_LOG, tmp_path / "llm_calls.jsonl")
    assert _run(tmp_path, "run", "--all")[0] == 0

    def invalid(path):
        raise ValueError("invalid graph")

    monkeypatch.setattr("evowiki.pipeline.snapshot.load_graph", invalid)
    code, out = _run(tmp_path, "snapshot")
    assert code == 1 and out.startswith("error: ") and "invalid graph" in out
    assert out.count("\n") == 1
    assert list((tmp_path / "versions").iterdir()) == []


def test_status_reports_llm_attempts_by_status_without_key_values(tmp_path):
    from evowiki.llm.models import LLMAttempt

    ws = _fixture_ws(tmp_path)
    base = {"input_hash": "h" * 64, "purpose": "unit", "prompt_version": "v1", "subject": "x",
                "provider": "anthropic", "model": "m", "model_version": None, "params": {},
                "raw_output": None, "error_type": None, "error_message": None, "cost": None, "latency": 0.0,
                "ts": NOW}
    log = ws.attempt_log()
    log.append(LLMAttempt(id="att_1", status="accepted", **base))
    assert (tmp_path / "llm_attempts.jsonl").exists()
    log.append(LLMAttempt(id="att_2", status="parse_error", **base))
    log.append(LLMAttempt(id="att_3", status="parse_error", **base))
    code, out = _run(tmp_path, "status", env={"DEEPSEEK_API_KEY": "sk-ds-secret"})
    assert code == 0 and "sk-ds-secret" not in out
    assert "llm attempts: {'accepted': 1, 'parse_error': 2}" in out


def test_record_run_writes_attempt_log(tmp_path):
    from evowiki.llm.gateway import LLMParseError
    from evowiki.llm.scripted import ScriptedProvider

    _fixture_ws(tmp_path)
    failing = {name: ScriptedProvider(name, lambda m, r: "not json")
               for name in (GW_CONFIG.generator.provider, GW_CONFIG.verifier.provider)}
    with pytest.raises(LLMParseError):
        main(["--root", str(tmp_path), "run", "--all", "--mode", "record"], env={},
             clock=lambda: NOW, providers=failing, out=io.StringIO())
    attempts = Workspace(tmp_path).attempt_log().attempts()
    assert attempts and {a.status for a in attempts} == {"parse_error"}
    assert len(Workspace(tmp_path).call_log()) == 0
