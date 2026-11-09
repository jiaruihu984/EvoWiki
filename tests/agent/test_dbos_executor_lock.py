"""One live process per DBOS executor id: a second backend with the same id would recover (rerun) the
workflows the first one is still running."""
import subprocess
import sys
import time
from pathlib import Path

import psycopg
import pytest

from evowiki.store.migrate import migrate

ROOT = Path(__file__).resolve().parents[2]


def child(executor: str, mode: str, db: str, log: Path) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "tests.agent.dbos_twin_child", executor, mode, db, str(log)],
                            cwd=ROOT)


def lines(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def wait_for(log: Path, line: str) -> None:
    end = time.time() + 60
    while time.time() < end:
        if line in lines(log):
            return
        time.sleep(0.1)
    raise AssertionError(f"{line!r} not in {lines(log)}")


@pytest.fixture
def db(db_url):
    with psycopg.connect(db_url) as c:
        migrate(c)
        c.commit()
    return db_url


def test_a_second_process_with_the_same_executor_id_does_not_start(db, tmp_path):
    log = tmp_path / "log"
    first = child("web", "start", db, log)
    try:
        wait_for(log, "slow-start")
        second = child("web", "recover", db, log)
        assert second.wait(timeout=90) == 3
        assert any(x.startswith("launch-refused") and "web" in x for x in lines(log))
        assert lines(log).count("slow-start") == 1          # not rerun by the second process
    finally:
        Path(str(log) + ".stop").write_text("x")
        first.wait(timeout=60)


def test_another_executor_id_and_a_restart_are_not_blocked(db, tmp_path):
    log = tmp_path / "log"
    Path(str(log) + ".stop").write_text("x")
    a = child("web", "recover", db, log)
    assert a.wait(timeout=90) == 0
    assert child("web", "recover", db, log).wait(timeout=90) == 0          # the lock left with the process
    first = child("web", "start", db, log)
    wait_for(log, "launched")
    assert child("rig", "recover", db, tmp_path / "log2").wait(timeout=90) == 0
    first.wait(timeout=60)
