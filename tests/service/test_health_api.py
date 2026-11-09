import pytest
from fastapi.testclient import TestClient

from evowiki.agent import lifecycle
from evowiki.service.app import create_app


@pytest.fixture(autouse=True)
def _undrained():
    lifecycle.DRAINED.clear()
    yield
    lifecycle.DRAINED.clear()


def test_health_reports_in_flight_and_commit(services, monkeypatch):
    monkeypatch.setenv("EVOWIKI_COMMIT", "abc123")
    app = create_app(services)
    app.state.in_flight = lambda: 2
    r = TestClient(app, base_url="http://127.0.0.1").get("/health").json()
    assert r == {"ok": True, "commit": "abc123", "app_version": r["app_version"], "in_flight": 2,
                 "instance_root": "", "protocol_version": 3,
                 "drained": False}


def test_health_without_durable_is_idle(services, monkeypatch):
    monkeypatch.delenv("EVOWIKI_COMMIT", raising=False)
    r = TestClient(create_app(services), base_url="http://127.0.0.1").get("/health").json()
    assert r["in_flight"] == 0 and r["commit"] == ""


def test_ready_refuses_unreconciled_forgetting(services):
    app = create_app(services)
    app.state.memory_ready = False
    r = TestClient(app, base_url="http://127.0.0.1").get("/health/ready")
    assert r.status_code == 503
    assert r.json()["code"] == "memory_recovering"


def test_health_identifies_instance_and_protocol(services, monkeypatch):
    monkeypatch.setenv("EVOWIKI_INSTANCE_ROOT", "D:/isolated checkout")
    r = TestClient(create_app(services), base_url="http://127.0.0.1").get("/health").json()
    assert r["instance_root"] == "D:/isolated checkout"
    assert r["protocol_version"] == 3


def test_ready_checks_current_schema_and_memory(services, admin_pool):
    app = create_app(services)
    c = TestClient(app, base_url="http://127.0.0.1")
    assert c.get("/health/ready").status_code == 200
    with admin_pool.connection() as conn:
        conn.execute("insert into schema_migrations(id) values ('9999_future_version')")
    assert c.get("/health/ready").status_code == 503


def test_drain_localhost_only(services):
    app = create_app(services)
    c = TestClient(app, client=("127.0.0.1", 5000), base_url="http://127.0.0.1")
    assert c.post("/health/drain", json={"on": True}).json() == {"drained": True}
    assert lifecycle.drained() is True
    assert c.get("/health").json()["drained"] is True
    c.post("/health/drain", json={"on": False})
    assert lifecycle.drained() is False
    far = TestClient(app, client=("10.0.0.2", 5000), base_url="http://127.0.0.1")
    assert far.post("/health/drain", json={"on": True}).status_code == 403
    assert lifecycle.drained() is False


def test_ready_requires_the_configured_durable_runtime_to_have_started(services, monkeypatch):
    from evowiki.agent import dbos_app

    services.durable = object()
    monkeypatch.setattr(dbos_app, "launched", lambda: False)
    response = TestClient(create_app(services), base_url="http://127.0.0.1").get("/health/ready")
    assert response.status_code == 503 and response.json()["code"] == "runtime_recovering"


def test_ready_reports_database_failure_without_exposing_connection_details(services, monkeypatch):
    import psycopg

    def unavailable():
        raise psycopg.OperationalError("password=private-value database=personal")
    monkeypatch.setattr(services.pool, "connection", unavailable)
    response = TestClient(create_app(services), base_url="http://127.0.0.1").get("/health/ready")
    assert response.status_code == 503 and response.json()["code"] == "database_unavailable"
    assert "private-value" not in response.text and "personal" not in response.text


def test_failed_backup_does_not_close_core_readiness_and_retry_is_local_only(services, monkeypatch):
    from evowiki.service import backup

    monkeypatch.setattr(backup.BackupConfig, "from_env", lambda *a: object())
    monkeypatch.setattr(backup, "status", lambda cfg: {"warning": "previous backup failed"})
    calls = []
    def failed(*args, **kwargs):
        calls.append(True)
        raise backup.BackupFailed("password=must-not-be-returned")
    monkeypatch.setattr(backup, "run_once", failed)
    app = create_app(services)
    client = TestClient(app, client=("127.0.0.1", 5000), base_url="http://127.0.0.1")
    assert client.get("/health/backup").json()["warning"] == "previous backup failed"
    assert client.get("/health/ready").status_code == 200
    response = client.post("/health/backup/run")
    assert response.status_code == 503 and "must-not-be-returned" not in response.text
    far = TestClient(app, client=("10.0.0.2", 5000), base_url="http://127.0.0.1")
    assert far.post("/health/backup/run").status_code == 403
    assert calls == [True]


def test_backup_lock_busy_is_reported_as_running_without_a_new_failure(services, monkeypatch, tmp_path):
    from evowiki.service import backup

    cfg = backup.BackupConfig("postgresql://127.0.0.1:1/qa", "qa", tmp_path / "backups",
                              tmp_path / "control" / "backup.json")
    monkeypatch.setattr(backup.BackupConfig, "from_env", lambda *args: cfg)
    client = TestClient(create_app(services), client=("127.0.0.1", 5000), base_url="http://127.0.0.1")
    with backup._backup_lock(cfg):
        response = client.post("/health/backup/run")
        assert response.status_code == 200 and response.json()["running"] is True
        assert response.json()["last_failure_at"] is None
        assert not cfg.state_file.exists()
