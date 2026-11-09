"""B0 probes on Postgres (spec 2026-10-14 §8 B0): a hard crash (os._exit) between steps, a crash
after a committed DB effect but before its step returned, and a stream written from the workflow
body across a crash. Each scenario: run the child with CRASH_AT, then run it again to recover."""
import os
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

from evowiki.store.migrate import migrate

ROOT = Path(__file__).resolve().parents[2]


def run_child(scenario: str, mode: str, db: str, log: Path, crash_at: str = "") -> int:
    env = {**os.environ, "CRASH_AT": crash_at}
    return subprocess.run([sys.executable, "-m", "tests.agent.dbos_child", scenario, mode, db,
                           str(log)], cwd=ROOT, env=env, timeout=120, check=False).returncode


def lines(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").splitlines()


@pytest.fixture
def db(db_url):
    with psycopg.connect(db_url) as c:
        migrate(c)
        c.execute("create table probe_target (n int)")
        c.commit()
    return db_url


def test_completed_step_is_not_rerun_after_a_crash(db, tmp_path):
    log = tmp_path / "log"
    assert run_child("steps", "crash", db, log, "after_step1") == 1
    assert run_child("steps", "resume", db, log) == 0
    assert lines(log) == ["step1", "crash after_step1", "step2", "result 2"]


def test_committed_effect_happens_once_when_its_step_reruns(db, tmp_path):
    log = tmp_path / "log"
    assert run_child("effect", "crash", db, log, "after_effect_commit") == 1
    assert run_child("effect", "resume", db, log) == 0
    # the step ran twice (at-least-once) but the effect applied once
    assert lines(log).count("effect") == 2
    with psycopg.connect(db) as c:
        assert c.execute("select count(*) from probe_target").fetchone()[0] == 1


def test_stream_written_from_the_workflow_is_not_duplicated(db, tmp_path):
    log = tmp_path / "log"
    assert run_child("stream", "crash", db, log, "after_stream_one") == 1
    assert run_child("stream", "resume", db, log) == 0
    from dbos import DBOS

    from evowiki.agent import dbos_app
    dbos_app.launch(db, app_version="b0-probe")
    try:
        assert list(DBOS.read_stream("probe-stream", "progress")) == ["one", "two"]
    finally:
        dbos_app.shutdown()
