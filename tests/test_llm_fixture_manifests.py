"""Every committed real-LLM recording is synthetic, listed in its manifest, and key-free."""

import json
import re
from datetime import datetime
from pathlib import Path

import pytest

from evowiki.llm.log import CallLog
from tests.llm_fixtures import CALLS, MANIFEST

FIXTURES = Path(__file__).parent / "fixtures"
KEY_LIKE = re.compile(r"sk-[A-Za-z0-9_-]{8,}")


def _scripted_only(fix_dir: Path) -> bool:
    """ScriptedProvider output (model_version '<provider>-scripted'), no real LLM involved."""
    calls = CallLog(fix_dir / CALLS).calls()
    return bool(calls) and all((c.model_version or "").endswith("-scripted") for c in calls)


ALL_DIRS = sorted(p.parent for p in FIXTURES.rglob(CALLS))
RECORDED = [d for d in ALL_DIRS if not _scripted_only(d)]


def test_the_fixture_glob_finds_the_recording_dirs():
    # every directory holding an llm_calls.jsonl; real-LLM ones (RECORDED) are checked below
    assert ALL_DIRS, "no llm_calls.jsonl found under tests/fixtures"


@pytest.mark.parametrize("fix_dir", RECORDED, ids=lambda p: p.name)
def test_recording_is_synthetic_and_listed(fix_dir: Path):
    manifest = json.loads((fix_dir / MANIFEST).read_text(encoding="utf-8"))
    assert manifest["synthetic_fixture_only"] is True
    assert isinstance(manifest["single_provider"], bool)
    assert isinstance(manifest["reportable"], bool)
    if manifest["single_provider"]:  # spec §8 downgrade: never reported as results
        assert manifest["reportable"] is False
    datetime.fromisoformat(manifest["generated_at"])
    listed = {e["input_hash"]: e for e in manifest["entries"]}
    calls = CallLog(fix_dir / CALLS).calls()
    assert {c.input_hash for c in calls} == set(listed)
    for c in calls:
        e = listed[c.input_hash]
        assert (e["purpose"], e["prompt_version"], e["provider"], e["model"]) == (
            c.purpose, c.prompt_version, c.provider, c.model)


@pytest.mark.parametrize("fix_dir", ALL_DIRS, ids=lambda p: p.name)
def test_recording_has_no_key_material(fix_dir: Path):
    for f in fix_dir.iterdir():
        if f.is_file():
            assert not KEY_LIKE.search(f.read_text(encoding="utf-8")), f
