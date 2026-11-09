"""Crash tests for the durable reading-assistant turn (B1c): real process deaths (os._exit) in a
child, then a second child that recovers the pending workflow. The model is scripted; no network.
Each test: the crash child exits 1, the resume child exits 0, then the child's log (one line per
provider call, tool run, log append and stream item) and the database say what ran how often."""
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from evowiki.assistant.pg_store import PgSessionStore
from evowiki.content.models import Lang
from evowiki.store.users import UserRow, create_user
from tests.assistant.fakes import INFO, T0
from tests.pg import drop_database, fresh_database

ROOT = Path(__file__).resolve().parents[2]


def run_child(mode: str, db: str, log: Path, crash_at: str = "") -> int:
    env = {**os.environ, "CRASH_AT": crash_at, "PYTHONPATH": str(ROOT / "src")}
    return subprocess.run([sys.executable, "-m", "tests.assistant.turn_crash_child", mode, db,
                           str(log)], cwd=ROOT, env=env, timeout=240, check=False).returncode


def lines(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").splitlines()


def items(log: Path) -> list[dict]:
    return [json.loads(ln[5:]) for ln in lines(log) if ln.startswith("item ")]


def shape(obj):
    """Ids differ between processes (the resume child mints its own): compare them by prefix."""
    return json.loads(re.sub("[a-z]+_[0-9]+", lambda m: m.group().split("_")[0] + "_N",
                             json.dumps(obj, ensure_ascii=False)))


def seed(pool) -> PgSessionStore:
    with pool.connection() as conn:
        create_user(conn, UserRow(user_id="u1", background="none", condition="S2",
                                  lang=Lang.ZH, created_at=T0))
    store = PgSessionStore(pool)
    store.create(INFO)
    return store


@pytest.fixture
def seeded(db_url, pool):
    return seed(pool)


@pytest.fixture
def reference(pg_admin_url, tmp_path):
    """The stream and log of the same turn without a crash, in a database of its own."""
    from evowiki.store.db import make_pool
    url, name = fresh_database(pg_admin_url)
    pool = make_pool(url, min_size=1, max_size=4)
    try:
        store = seed(pool)
        log = tmp_path / "reference"
        assert run_child("crash", url, log) == 0                    # nothing crashes
        return items(log), [(e.type, e.entry_id) for e in store.entries("s1")]
    finally:
        pool.close()
        drop_database(pg_admin_url, name)


def check_once(store, log: Path, reference) -> None:
    """The session log has one user / assistant entry, each tool entry and the
    proposal once; the stream has no duplicated event, starts at offset 0 and ends with `done`;
    both equal an uninterrupted run."""
    ref_items, ref_log = reference
    types = [e.type for e in store.entries("s1")]
    assert types.count("user") == 1 and types.count("assistant") == 1
    assert types.count("proposal") == 1
    assert types.count("turn_error") == 0
    tool_ids = [e.payload["call_id"] for e in store.entries("s1") if e.type == "tool"]
    assert sorted(tool_ids) == ["c1", "c2"]
    got = items(log)
    names = [i["type"] for i in got]
    assert names[-1] == "done" and names.count("done") == 1 and "error" not in names
    assert names.count("proposal") == 1
    assert names.count("tool_start") == 1 and names.count("tool_end") == 1   # network tool only
    assert shape(got) == shape(ref_items)         # nothing re-emitted, nothing lost
    assert shape([(e.type, e.entry_id) for e in store.entries("s1")]) == shape(ref_log)
    ids = [e.entry_id for e in store.entries("s1")]
    assert len(set(ids)) == len(ids)


def test_crash_in_the_second_model_call_after_a_network_tool(seeded, db_url, tmp_path, reference):
    log = tmp_path / "log"
    assert run_child("crash", db_url, log, "provider:2") == 1
    assert run_child("resume", db_url, log) == 0
    ls = lines(log)
    served = Counter(ln for ln in ls if ln.startswith("provider "))
    # a model call that returned is never asked again; the one that died is asked at most twice
    assert served["provider 1"] == 1 and served["provider 3"] == 1
    assert served["provider 2"] == 2
    assert "crash provider:2" in ls and "result done" in ls
    # the tool run before the crash was recorded: it did not run again; each tool ran once
    tool_runs = Counter(ln for ln in ls if ln.startswith("tool "))
    assert set(tool_runs.values()) == {1}
    assert set(tool_runs) == {"tool web_search", "tool propose_page_edit"}
    check_once(seeded, log, reference)


def test_crash_right_after_the_assistant_entry_is_appended(seeded, db_url, tmp_path, reference):
    log = tmp_path / "log"
    assert run_child("crash", db_url, log, "after_append:assistant") == 1
    assert run_child("resume", db_url, log) == 0
    ls = lines(log)
    assert "crash after_append:assistant" in ls and "result done" in ls
    # the answer was complete before the crash: no model call and no tool ran again
    served = Counter(ln for ln in ls if ln.startswith(("provider ", "tool ")))
    assert set(served.values()) == {1}
    # the assistant row landed in the dead process; the re-executed step found it, not a second row
    assert ls.count("append assistant") == 2
    check_once(seeded, log, reference)
