"""scripts/clean_test_pg.py removes only leftover test clusters nobody uses."""

import importlib.util
import json
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "clean_test_pg.py"


@pytest.fixture(scope="module")
def ct():
    spec = importlib.util.spec_from_file_location("clean_test_pg", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cluster(tmp_path, name="evowiki-pg-abc", owners=(1,), pid_file=None, initdb=True):
    d = tmp_path / name
    d.mkdir()
    (d / ".handle_pids.json").write_text(json.dumps(list(owners)))
    if initdb:
        (d / "PG_VERSION").write_text("18")
    if pid_file is not None:
        pid, data, port = pid_file
        (d / "postmaster.pid").write_text(f"{pid}\n{data or d}\n0\n{port}\n\n127.0.0.1\n0 0\nready\n")
    return d


def judge(ct, d, *, alive=(), postgres=(), clients=None, now=None):
    return ct.judge(d, now or time.time(), alive=lambda p, born: p in alive, postgres=lambda p: p in postgres,
                    clients=lambda port: clients)


def test_a_live_owner_keeps_it(ct, tmp_path):
    v = judge(ct, cluster(tmp_path), alive={1})
    assert not v.remove and "1" in v.why


def test_dead_owner_and_no_postmaster_is_removed(ct, tmp_path):
    assert judge(ct, cluster(tmp_path)).remove


def test_a_running_postmaster_with_clients_or_unreachable_is_kept(ct, tmp_path):
    d = cluster(tmp_path, pid_file=(77, None, 6000))
    assert not judge(ct, d, postgres={77}, clients=2).remove
    assert not judge(ct, d, postgres={77}, clients=None).remove
    v = judge(ct, d, postgres={77}, clients=0)
    assert v.remove and v.postmaster == 77 and v.port == 6000


def test_a_postmaster_pid_for_another_dir_is_kept(ct, tmp_path):
    d = cluster(tmp_path, pid_file=(77, tmp_path / "elsewhere", 6000))
    assert not judge(ct, d, postgres={77}, clients=0).remove


def test_a_stale_postmaster_pid_does_not_block(ct, tmp_path):
    d = cluster(tmp_path, pid_file=(77, None, 6000))
    assert judge(ct, d, postgres=set()).remove                 # pid 77 is no postgres any more


def test_half_initialised_dirs_wait_an_hour(ct, tmp_path):
    d = cluster(tmp_path, initdb=False)
    assert not judge(ct, d).remove
    assert judge(ct, d, now=time.time() + 2 * 3600).remove


def test_unreadable_owner_list_is_kept(ct, tmp_path):
    d = cluster(tmp_path)
    (d / ".handle_pids.json").write_text("{broken")
    assert not judge(ct, d).remove


def test_only_prefixed_dirs_are_found(ct, tmp_path):
    cluster(tmp_path, "evowiki-pg-1")
    (tmp_path / "evowiki-test-pg").mkdir()
    (tmp_path / "other").mkdir()
    assert [p.name for p in ct.find_clusters([tmp_path])] == ["evowiki-pg-1"]


def test_a_dry_run_changes_nothing(ct, tmp_path, capsys):
    d = cluster(tmp_path, owners=(999999,))
    assert ct.main(["--dir", str(tmp_path)]) == 0
    assert d.exists() and "试运行" in capsys.readouterr().out
