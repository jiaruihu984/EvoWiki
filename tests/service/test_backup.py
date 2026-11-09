"""The periodic database backup (service/backup.py): pg_dump is replaced by a function that writes a file."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from evowiki.service.backup import KEEP, BackupBusy, BackupConfig, BackupFailed, run_once


def cfg(tmp_path: Path, **o) -> BackupConfig:
    return BackupConfig("postgresql://u@h:1/evowiki", "evowiki", tmp_path / "backups",
                        tmp_path / "control" / "backup.json", **o)


def dump(c: BackupConfig, out: Path) -> None:
    out.write_bytes(b"PGDMP")


def test_a_dump_is_written_once_a_day_and_the_latest_14_are_kept(tmp_path):
    c = cfg(tmp_path)
    day = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
    for i in range(20):
        made = run_once(c, day + timedelta(days=i), dump)
        assert made is not None and made.name == f"evowiki-{(day + timedelta(days=i)):%Y%m%d-%H%M%S}.dump"
    names = sorted(p.name for p in c.folder.glob("*.dump"))
    assert len(names) == KEEP == 14
    assert names[0].startswith("evowiki-20261107") and names[-1].startswith("evowiki-20261120")


def test_a_restart_within_the_day_does_not_dump_again(tmp_path):
    c = cfg(tmp_path)
    now = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
    assert run_once(c, now, dump) is not None
    assert run_once(c, now + timedelta(hours=3), dump) is None
    assert run_once(c, now + timedelta(hours=21), dump) is not None
    assert len(list(c.folder.glob("*.dump"))) == 2


def test_a_failed_dump_leaves_nothing_that_looks_whole_and_keeps_the_old_ones(tmp_path):
    c = cfg(tmp_path)
    old = run_once(c, datetime(2026, 11, 1, tzinfo=UTC), dump)

    def broken(_c: BackupConfig, out: Path) -> None:
        out.write_bytes(b"half")
        raise BackupFailed("disk full")

    with pytest.raises(BackupFailed, match="disk full"):
        run_once(c, datetime(2026, 11, 2, tzinfo=UTC), broken)
    assert [p.name for p in c.folder.glob("*.dump")] == [old.name]
    assert not list(c.folder.glob("*.part"))


def test_other_databases_dumps_are_not_counted_or_removed(tmp_path):
    c = cfg(tmp_path, keep=1)
    c.folder.mkdir(parents=True)
    other = c.folder / "other-20260101-000000.dump"
    other.write_bytes(b"x")
    for d in (1, 2):
        run_once(c, datetime(2026, 11, d, tzinfo=UTC), dump)
    assert other.exists() and len(list(c.folder.glob("evowiki-*.dump"))) == 1


def test_only_the_database_the_env_file_names_is_backed_up(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("EVOWIKI_DATABASE_URL=postgresql://evowiki_app:pw@127.0.0.1:15432/evowiki", encoding="utf-8")
    env = {"EVOWIKI_DATABASE_URL": "postgresql://evowiki_app:pw@127.0.0.1:15432/evowiki",
           "EVOWIKI_ADMIN_DATABASE_URL": "postgresql://postgres:pw@127.0.0.1:15432/evowiki"}
    c = BackupConfig.from_env(env, env_file)
    assert c is not None and c.url.startswith("postgresql://postgres:") and c.dbname == "evowiki"
    assert c.folder == tmp_path / ".evowiki-run" / "backups"
    external = BackupConfig.from_env({**env, "EVOWIKI_BACKUP_DIR": str(tmp_path / "external" / "dumps")}, env_file)
    assert external is not None and external.state_file == c.state_file
    assert c.state_file.is_relative_to(tmp_path / ".evowiki-run" / "backup-state")
    assert BackupConfig.from_env({**env, "EVOWIKI_BACKUP_DIR": "off"}, env_file) is None
    assert BackupConfig.from_env({}, env_file) is None
    # a rig / audit / test database, or the same name on another port, is never backed up
    for other in ("postgresql://postgres@127.0.0.1:15433/audit_x", "postgresql://postgres@127.0.0.1:15433/evowiki_t_1_ab",
                  "postgresql://postgres@127.0.0.1:15433/evowiki", "postgresql://postgres@127.0.0.1:15432/evowiki_copy"):
        assert BackupConfig.from_env({"EVOWIKI_DATABASE_URL": other, "EVOWIKI_ADMIN_DATABASE_URL": other}, env_file) is None
    assert BackupConfig.from_env(env, tmp_path / "missing.env") is None


def test_the_password_never_reaches_a_message(tmp_path, monkeypatch):
    import subprocess

    from evowiki.service import backup

    c = BackupConfig("postgresql://postgres:s3cret!@127.0.0.1:1/evowiki", "evowiki", tmp_path,
                     tmp_path / "control" / "backup.json")
    seen = {}

    def fake_run(cmd, env, **_kw):
        seen.update(cmd=cmd, env=env, service=Path(env["PGSERVICEFILE"]).read_text(encoding="utf-8"))
        return subprocess.CompletedProcess(cmd, 1, "", 'FATAL: password "s3cret!" rejected for dbname=evowiki password=s3cret!')

    monkeypatch.setattr(backup, "pg_dump_path", lambda: "pg_dump")
    monkeypatch.setattr(backup.subprocess, "run", fake_run)
    with pytest.raises(BackupFailed) as e:
        backup._pg_dump(c, tmp_path / "x.part")
    assert "s3cret" not in str(e.value) and "***" in str(e.value)
    assert "s3cret" not in " ".join(seen["cmd"]) and "password=s3cret!" in seen["service"]


def test_pg_dump_is_found_from_config_path_or_the_stable_copy_and_never_by_a_guess(tmp_path, monkeypatch):
    import sys

    from evowiki.service import backup

    exe = "pg_dump.exe" if sys.platform == "win32" else "pg_dump"
    monkeypatch.setattr(backup.shutil, "which", lambda _n: None)
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    with pytest.raises(BackupFailed, match="EVOWIKI_PG_DUMP"):
        backup.pg_dump_path({}, env_file)
    stable = tmp_path / ".pg18" / "bin"
    stable.mkdir(parents=True)
    (stable / exe).write_text("", encoding="utf-8")
    assert backup.pg_dump_path({}, env_file) == str(stable / exe)
    monkeypatch.setattr(backup.shutil, "which", lambda _n: "C:/pg/bin/pg_dump.exe")
    assert backup.pg_dump_path({}, env_file) == "C:/pg/bin/pg_dump.exe"
    assert backup.pg_dump_path({"EVOWIKI_PG_DUMP": "D:/x/pg_dump.exe"}, env_file) == "D:/x/pg_dump.exe"


def test_status_exposes_newer_failure_and_recovers_from_corrupt_state(tmp_path):
    from evowiki.service import backup
    c = cfg(tmp_path)
    day = datetime(2026, 11, 1, tzinfo=UTC)
    first = run_once(c, day, dump)

    def broken(_cfg, _out):
        raise BackupFailed("disk full")

    with pytest.raises(BackupFailed):
        run_once(c, day + timedelta(days=1), broken)
    state = backup.status(c)
    assert state["last_success_file"] == str(first)
    assert state["warning"] == "disk full" and not state["running"]
    run_once(c, day + timedelta(days=2), dump)
    assert backup.status(c)["warning"] is None
    c.state_file.write_text("broken", encoding="utf-8")
    assert backup.status(c)["last_success_file"].endswith("20261103-000000.dump")
    assert backup.status(None)["disabled"] is True


def test_running_backup_is_fenced_and_interrupted_status_is_reconciled(tmp_path):
    from evowiki.service import backup
    c = cfg(tmp_path)
    now = datetime(2026, 11, 1, tzinfo=UTC)
    with backup._backup_lock(c):
        assert backup.status(c)["running"]
        with pytest.raises(BackupBusy, match="already running"):
            run_once(c, now, dump)
        assert backup.status(c)["last_failure_at"] is None
        assert not c.state_file.exists()
    c.state_file.parent.mkdir(parents=True, exist_ok=True)
    backup.atomic_json(c.state_file, {"running": True})
    assert backup.status(c)["running"] is False


def test_ledger_is_bundled_and_missing_ledger_fails_without_a_complete_dump(tmp_path):
    from evowiki.service import backup
    ledger = tmp_path / "forget-ledger.jsonl"
    ledger.write_text("", encoding="utf-8")
    c = cfg(tmp_path, ledger=ledger)
    day = datetime(2026, 11, 1, tzinfo=UTC)
    made = run_once(c, day, dump)
    assert made.with_suffix(".ledger.jsonl").read_text(encoding="utf-8") == ""
    ledger.unlink()
    with pytest.raises(BackupFailed, match="ledger is missing"):
        run_once(c, day + timedelta(days=1), dump)
    assert len(list(c.folder.glob("*.dump"))) == 1
    assert "ledger" in backup.status(c)["warning"]


@pytest.mark.parametrize("obstruction", ["destination", "parent", "lock"])
def test_filesystem_failure_keeps_success_and_failure_until_complete_retry(tmp_path, obstruction):
    from dataclasses import replace

    from evowiki.service import backup

    c = replace(cfg(tmp_path), folder=tmp_path / "external" / "backups")
    day = datetime(2026, 11, 1, tzinfo=UTC)
    first = run_once(c, day, dump)
    original = first.read_bytes()
    blocked = c.folder if obstruction == "destination" else c.folder.parent if obstruction == "parent" else c.folder / "evowiki.lock"
    hold = blocked.with_name(blocked.name + "-hold")
    blocked.rename(hold)
    if obstruction == "lock":
        blocked.mkdir()
    else:
        blocked.write_text("owned blocker", encoding="utf-8")
    try:
        with pytest.raises(BackupFailed):
            run_once(c, day + timedelta(hours=1), dump, force=True)
        state = backup.status(c)
        assert state["last_success_file"] == str(first)
        assert state["last_failure_at"] == (day + timedelta(hours=1)).isoformat()
        assert state["last_failure_reason"] and state["warning"]
        assert not state["running"]
        # Reconstructing the config reads durable control state, not an in-memory cache.
        assert backup.status(replace(c))["last_failure_reason"] == state["last_failure_reason"]
    finally:
        blocked.rmdir() if obstruction == "lock" else blocked.unlink()
        hold.rename(blocked)
    assert first.read_bytes() == original
    assert backup.status(c)["last_failure_reason"] == state["last_failure_reason"]
    assert run_once(c, day + timedelta(hours=2), dump) is None
    assert backup.status(c)["last_failure_at"] is not None
    run_once(c, day + timedelta(hours=3), dump, force=True)
    state = backup.status(c)
    assert state["last_success_at"] == (day + timedelta(hours=3)).isoformat()
    assert state["last_failure_at"] is None and state["last_failure_reason"] is None
    assert state["warning"] is None


def test_automatic_failure_is_visible_after_destination_returns(tmp_path, monkeypatch):
    from threading import Event

    from evowiki.service import backup

    c = cfg(tmp_path)
    day = datetime(2026, 11, 1, tzinfo=UTC)
    first = run_once(c, day, dump)
    hold = c.folder.with_name("backups-hold")
    c.folder.rename(hold)
    c.folder.write_text("owned blocker", encoding="utf-8")
    reported = Event()
    monkeypatch.setattr(backup.log, "error", lambda *_args: reported.set())
    try:
        worker, stop = backup.start_background(c, lambda: day + timedelta(days=1))
        try:
            assert reported.wait(5)
        finally:
            stop.set()
            worker.join(5)
        assert not worker.is_alive()
        assert backup.status(c)["last_failure_reason"]
    finally:
        c.folder.unlink()
        hold.rename(c.folder)
    state = backup.status(c)
    assert state["last_success_file"] == str(first)
    assert state["warning"] == state["last_failure_reason"]


def test_control_state_write_failure_is_explicit_and_never_reported_as_success(tmp_path):
    from evowiki.service import backup

    c = cfg(tmp_path)
    c.state_file.parent.write_text("owned control blocker", encoding="utf-8")
    with pytest.raises(BackupFailed, match="cannot lock backup status"):
        run_once(c, datetime(2026, 11, 1, tzinfo=UTC), dump)
    assert "backup status lock" in backup.status(c)["warning"]
    assert not list(c.folder.glob("*.part"))


def test_result_write_failure_under_control_lock_is_not_silently_ignored(tmp_path, monkeypatch):
    import errno

    from evowiki.service import backup

    c = cfg(tmp_path)
    write = backup.atomic_json

    def full_state_disk(path, state):
        if path == c.state_file:
            raise OSError(errno.ENOSPC, "control disk full")
        write(path, state)

    monkeypatch.setattr(backup, "atomic_json", full_state_disk)
    with pytest.raises(BackupFailed, match="cannot save backup status: .*control disk full"):
        run_once(c, datetime(2026, 11, 1, tzinfo=UTC), dump)
    assert not list(c.state_file.parent.glob("*.part"))


def test_lock_io_failure_is_not_mistaken_for_another_running_backup(tmp_path, monkeypatch):
    import errno
    import sys

    from evowiki.service import backup

    c = cfg(tmp_path)
    day = datetime(2026, 11, 1, tzinfo=UTC)
    first = run_once(c, day, dump)

    def broken_lock(*_args):
        raise OSError(errno.EIO, "lock I/O failed")

    if sys.platform == "win32":
        import msvcrt
        monkeypatch.setattr(msvcrt, "locking", broken_lock)
    else:
        import fcntl
        monkeypatch.setattr(fcntl, "flock", broken_lock)
    with pytest.raises(BackupFailed, match="lock I/O failed"):
        run_once(c, day + timedelta(days=1), dump)
    state = backup.status(c)
    assert not state["running"] and "lock I/O failed" in state["warning"]
    assert state["last_success_file"] == str(first)


def test_old_failure_cannot_overwrite_a_new_success(tmp_path, monkeypatch):
    from threading import Event, Thread, current_thread

    from evowiki.service import backup

    c = cfg(tmp_path)
    day = datetime(2026, 11, 1, tzinfo=UTC)
    run_once(c, day, dump)
    hold = c.folder.with_name("backups-hold")
    c.folder.rename(hold)
    c.folder.write_text("owned blocker", encoding="utf-8")
    paused, resume = Event(), Event()
    save = backup._save_status
    failures = []

    def gated_save(config, state):
        if current_thread().name == "old-failure" and state["last_failure_at"]:
            paused.set()
            assert resume.wait(5)
        save(config, state)

    def first_attempt():
        try:
            run_once(c, day + timedelta(hours=1), dump, force=True)
        except BackupFailed as exc:
            failures.append(str(exc))

    monkeypatch.setattr(backup, "_save_status", gated_save)
    worker = Thread(target=first_attempt, name="old-failure")
    worker.start()
    try:
        assert paused.wait(5)
        c.folder.unlink()
        hold.rename(c.folder)
        with pytest.raises(BackupBusy):
            run_once(c, day + timedelta(hours=2), dump, force=True)
        assert backup.status(c)["running"]
    finally:
        resume.set()
        worker.join(5)
        if hold.exists():
            c.folder.unlink()
            hold.rename(c.folder)
    assert not worker.is_alive() and failures
    run_once(c, day + timedelta(hours=3), dump, force=True)
    state = backup.status(c)
    assert state["last_success_at"] == (day + timedelta(hours=3)).isoformat()
    assert state["last_failure_at"] is None and state["warning"] is None


def test_instances_with_separate_control_state_still_share_destination_lock(tmp_path):
    from dataclasses import replace

    from evowiki.service import backup

    c = cfg(tmp_path)
    other = replace(c, state_file=tmp_path / "other-instance" / "backup.json")
    with backup._backup_lock(c):
        with pytest.raises(BackupBusy):
            run_once(other, datetime(2026, 11, 1, tzinfo=UTC), dump)
        assert backup.status(other)["running"]
        assert not other.state_file.exists()


@pytest.mark.parametrize("operation", ["dump", "restore"])
def test_database_tools_preserve_all_libpq_options_without_password_argv(tmp_path, monkeypatch, operation):
    import subprocess

    import evowiki_backup
    from psycopg.conninfo import make_conninfo

    from evowiki.service import backup

    options = {"host": "db.example", "hostaddr": "192.0.2.1", "port": "5433", "dbname": "archive",
                   "user": "backup", "password": "fake-db-secret", "sslmode": "verify-full",
                   "sslrootcert": "D:/证书/root.pem", "sslcert": "D:/cert/client.pem", "sslkey": "D:/cert/client.key",
                   "sslpassword": "fake-key-secret", "options": "-c statement_timeout=4321",
                   "keepalives_idle": "23", "application_name": "backup test"}
    seen = {}
    def fake_run(cmd, env, **kwargs):
        assert "fake-db-secret" not in " ".join(cmd)
        assert "fake-key-secret" not in " ".join(cmd)
        if "PGSERVICEFILE" in env:
            service = Path(env["PGSERVICEFILE"])
            seen["service"] = service
            content = service.read_text(encoding="utf-8")
            seen["options"] = dict(line.split("=", 1) for line in content.splitlines() if "=" in line)
        else:
            seen["options"] = {"host": env.get("PGHOST"), "sslmode": env.get("PGSSLMODE")}
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(backup.subprocess, "run", fake_run)
    monkeypatch.setenv("PGSSLMODE", "disable")
    url = make_conninfo(**options)
    if operation == "dump":
        backup._pg_dump(BackupConfig(url, "archive", tmp_path, tmp_path / "state", pg_dump="pg_dump"), tmp_path / "x")
    else:
        evowiki_backup._pg_restore(url, tmp_path / "x", pg_dump="pg_dump")
    assert seen["options"] == options
    assert not seen["service"].exists()


@pytest.mark.parametrize("operation", ["dump", "restore"])
def test_database_tools_resolve_named_service_with_libpq(db_url, tmp_path, monkeypatch, operation):
    import subprocess

    import evowiki_backup
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    from evowiki.service import backup

    source = tmp_path / "original-service.conf"
    original = {**conninfo_to_dict(db_url), "application_name": "named-service-backup",
                "options": "-c statement_timeout=4321", "password": "fake-test-password"}
    source.write_text("[test_service]\n" + "".join(f"{k}={v}\n" for k, v in original.items()), encoding="utf-8")
    monkeypatch.setenv("PGSERVICEFILE", str(source))
    def fake_run(cmd, env, **kwargs):
        path = Path(env["PGSERVICEFILE"])
        assert path != source
        values = dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines() if "=" in line)
        assert "service" not in values
        assert values.items() >= original.items()
        assert "fake-test-password" not in " ".join(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(backup.subprocess, "run", fake_run)
    url = make_conninfo(service="test_service")
    if operation == "dump":
        backup._pg_dump(BackupConfig(url, original["dbname"], tmp_path, tmp_path / "state", pg_dump="pg_dump"), tmp_path / "x")
    else:
        evowiki_backup._pg_restore(url, tmp_path / "x", pg_dump="pg_dump")
    assert source.exists()
