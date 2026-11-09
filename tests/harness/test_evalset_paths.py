"""Portable paths, explicit paid-run opt-in, and secret-free child protection."""

import sys
from pathlib import Path

import pytest

from evowiki.forge.patch.commands import child_env
from evowiki.harness import evalset_paths, evalset_redlines
from tests.llm_cases.evalset import run


def test_paths_are_checkout_local_and_explicit_overrides_resolve_from_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(evalset_paths, "ROOT", tmp_path)
    assert evalset_paths.data_dir({}) == tmp_path / ".evowiki-run/evalset"
    assert evalset_paths.sealed_dir({}) == tmp_path / ".evowiki-run/evalset-sealed"
    assert evalset_paths.data_dir({"EVOWIKI_EVALSET_DATA_DIR": "评测 data"}) == tmp_path / "评测 data"
    assert evalset_paths.sealed_dir({"EVOWIKI_SEALED_ROOT": str(tmp_path / "sealed")}) == tmp_path / "sealed"


def test_runner_reads_current_checkout_env_and_process_overrides(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "ROOT", tmp_path)
    monkeypatch.delenv("EVOWIKI_ENV_FILE", raising=False)
    (tmp_path / ".env").write_text('QA_VALUE="中文 value"\nQA_OVERRIDE=file\n', encoding="utf-8")
    monkeypatch.setenv("QA_OVERRIDE", "process")
    assert run.env_file()["QA_VALUE"] == "中文 value"
    assert run.env_file()["QA_OVERRIDE"] == "process"
    (tmp_path / "explicit.env").write_text("QA_VALUE=explicit\n", encoding="utf-8")
    monkeypatch.setenv("EVOWIKI_ENV_FILE", "explicit.env")
    assert run.env_file()["QA_VALUE"] == "explicit"
    assert run.MAIN_PY == Path(sys.executable)


def test_missing_env_fails_without_borrowing_another_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "ROOT", tmp_path)
    monkeypatch.delenv("EVOWIKI_ENV_FILE", raising=False)
    with pytest.raises(FileNotFoundError):
        run.env_file()


def test_paid_runner_requires_explicit_opt_in():
    assert evalset_redlines._python({}) is None
    assert evalset_redlines._python({"EVOWIKI_EVALSET_PYTHON": sys.executable}) == sys.executable
    assert evalset_redlines.harness_redlines({}) is None


def test_child_protects_same_resolved_sealed_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("EVOWIKI_SEALED_ROOT", str(tmp_path / "sealed 数据"))
    env = child_env(worktree=tmp_path / "candidate", tmp=tmp_path / "temp",
                    test_db_url="postgresql://127.0.0.1:1/test", python=sys.executable)
    assert env["EVOWIKI_SEALED_ROOT"] == str(evalset_paths.sealed_dir())
