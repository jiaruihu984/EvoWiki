"""Real-LLM test recordings: synthetic inputs only, replayed by default (decision 12).

Recording is opt-in (EVOWIKI_RECORD_LIVE=1) and must only be fed hard-coded synthetic test
strings. Runtime calls from real users are audited in Postgres, never in repo files.

Recordings use the pinned DeepSeek single-provider config (FIXTURE_ENV, the user-approved
spec §8 downgrade), so record and replay hash identically whatever the caller's env says.
Each MANIFEST.json entry names the provider/model that produced the call."""

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evowiki.llm.gateway import Gateway
from evowiki.llm.log import AttemptLog, CallLog
from evowiki.runtime.llm import (
    RUNTIME_RECORD_MAX_ATTEMPTS,
    runtime_gateway_config,
    runtime_providers,
)

LIVE = os.environ.get("EVOWIKI_RECORD_LIVE") == "1"
CALLS, ATTEMPTS, MANIFEST = "llm_calls.jsonl", "llm_attempts.jsonl", "MANIFEST.json"
FIXTURE_ENV = {"EVOWIKI_RUNTIME_PROVIDER": "deepseek"}  # fixed: never read from os.environ


def write_manifest(fix_dir: Path, *, single_provider: bool) -> None:
    """single_provider: the recording config's generator and verifier share a provider (spec §8
    downgrade); such recordings are marked non-reportable."""
    entries = sorted(({"input_hash": c.input_hash, "purpose": c.purpose,
                       "prompt_version": c.prompt_version, "provider": c.provider,
                       "model": c.model, "model_version": c.model_version}
                      for c in CallLog(fix_dir / CALLS).calls()),
                     key=lambda e: e["input_hash"])
    doc = {"synthetic_fixture_only": True,
           "single_provider": single_provider,
           "reportable": not single_provider,
           "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
           "entries": entries}
    (fix_dir / MANIFEST).write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")


@contextmanager
def fixture_gateway(fix_dir: Path) -> Iterator[Gateway]:
    cfg = runtime_gateway_config(FIXTURE_ENV)  # fixed config: hashes do not depend on env
    if LIVE:
        fix_dir.mkdir(parents=True, exist_ok=True)
        yield Gateway(cfg, CallLog(fix_dir / CALLS), mode="record",
                      providers=runtime_providers(), attempts=AttemptLog(fix_dir / ATTEMPTS),
                      record_max_attempts=RUNTIME_RECORD_MAX_ATTEMPTS)
        write_manifest(fix_dir, single_provider=cfg.single_provider)
        return
    if not (fix_dir / CALLS).exists():
        pytest.fail(f"missing recording in {fix_dir}; record once with EVOWIKI_RECORD_LIVE=1")
    yield Gateway(cfg, CallLog(fix_dir / CALLS), mode="replay")
