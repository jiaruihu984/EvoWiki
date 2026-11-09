import time
from types import SimpleNamespace

import pytest
from psycopg.conninfo import make_conninfo

from evowiki.service import cli
from evowiki.service.cli import main
from tests.runtime.fake_sdks import install_fake_sdks
from tests.runtime.helpers import SNAP


def _db(url: str) -> dict:
    """As deployed (0082): the service connects as evowiki_app, migrations run as the owner."""
    return {"EVOWIKI_DATABASE_URL": make_conninfo(url, user="evowiki_app"),
            "EVOWIKI_ADMIN_DATABASE_URL": url}


@pytest.fixture(autouse=True)
def _browser_preflight(monkeypatch):
    # Browser installation/launch is tested separately; CLI tests never download it.
    monkeypatch.setattr(cli, "_ensure_browser", lambda: None)
    monkeypatch.setattr(cli, "_initialize_library", lambda pool: None)


def test_cli_end_to_end(db_url, capsys, monkeypatch):
    install_fake_sdks(monkeypatch)  # no `llm` extra needed
    env = {**_db(db_url), "EVOWIKI_EMBEDDER": "hash",
           "ANTHROPIC_API_KEY": "sk-test-SECRET-VALUE", "DEEPSEEK_API_KEY": "sk-ds-SECRET-VALUE"}
    assert main(["migrate"], env) == 0
    assert main(["import-content", str(SNAP), "--activate"], env) == 0
    assert main(["index-embeddings"], env) == 0
    assert main(["status"], env) == 0
    out = capsys.readouterr().out
    assert "cv_runtimefixture" in out and "ANTHROPIC_API_KEY: set" in out
    assert "DEEPSEEK_API_KEY: set" in out
    assert "SECRET" not in out  # never print key values
    assert "embedder: hash" in out
    assert "WARNING" not in out  # default runtime config is two-provider


def test_status_warns_on_single_provider_runtime(db_url, capsys):
    env = {**_db(db_url), "EVOWIKI_EMBEDDER": "hash",
           "EVOWIKI_RUNTIME_PROVIDER": "deepseek", "DEEPSEEK_API_KEY": "sk-ds-SECRET-VALUE"}
    assert main(["status"], env) == 0
    out = capsys.readouterr().out
    assert "content: none" in out and "DEEPSEEK_API_KEY: set" in out
    assert "ANTHROPIC_API_KEY: unset" in out
    warn = [line for line in out.splitlines() if line.startswith("WARNING")]
    assert len(warn) == 1 and "single-provider" in warn[0] and "spec §8 downgrade" in warn[0]
    assert "SECRET" not in out


def test_status_rejects_unknown_runtime_provider(db_url, capsys):
    env = {**_db(db_url), "EVOWIKI_RUNTIME_PROVIDER": "unknown-provider"}
    assert main(["status"], env) == 2
    assert "EVOWIKI_RUNTIME_PROVIDER" in capsys.readouterr().err


def test_missing_selected_key_keeps_local_setup_available_without_provider_fallback(db_url, monkeypatch):
    import uvicorn
    launched = []
    def run_local(app, **_kwargs):
        from evowiki.llm.usage import Account
        svc = app.state.services
        assert svc.gateway is not None and svc.models.default.startswith("anthropic/")
        with pytest.raises(ValueError, match="默认模型"):
            svc.gateway.route_ref("generator", Account(user_id="new", group="assistant"))
        launched.append(app)
    monkeypatch.setattr(uvicorn, "run", run_local)
    install_fake_sdks(monkeypatch)
    env = {**_db(db_url), "EVOWIKI_RUNTIME_PROVIDER": "anthropic",
           "DEEPSEEK_API_KEY": "d", "EVOWIKI_EMBEDDER": "hash"}
    assert main(["serve"], env) == 0
    assert len(launched) == 1


def test_import_content_missing_file_fails(db_url, tmp_path, capsys):
    env = _db(db_url)
    assert main(["import-content", str(tmp_path / "nope.json")], env) == 1
    assert "nope.json" in capsys.readouterr().err


def test_cli_requires_database_url(capsys):
    assert main(["status"], {}) == 2
    assert "EVOWIKI_DATABASE_URL" in capsys.readouterr().err


def test_serve_refuses_non_loopback_without_override(capsys, monkeypatch):
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    env = {"EVOWIKI_DATABASE_URL": "postgresql:///unused"}
    assert main(["serve", "--host", "0.0.0.0"], env) == 2
    assert main(["serve", "--host", "192.168.1.5"], env) == 2
    assert "--unsafe-expose" in capsys.readouterr().err


@pytest.mark.parametrize(("host", "extra"), [("::1", []), ("0.0.0.0", ["--unsafe-expose"])])
def test_serve_binds_allowed_hosts(host, extra, monkeypatch):
    import uvicorn

    import evowiki.service.app as app_mod
    import evowiki.service.deps as deps_mod

    closed = []
    fake = SimpleNamespace(pool=SimpleNamespace(close=lambda: closed.append(True)))
    seen = {}
    monkeypatch.setattr(deps_mod, "build_services", lambda env, **k: fake)
    monkeypatch.setattr(cli, "_preflight", lambda dsn: None)
    monkeypatch.setattr(app_mod, "create_app", lambda svc: "APP")
    monkeypatch.setattr(uvicorn, "run", lambda app, **k: seen.update(app=app, **k))
    env = {"EVOWIKI_DATABASE_URL": "postgresql:///unused", "EVOWIKI_BOT": "off"}
    assert main(["serve", "--host", host, "--port", "8123", *extra], env) == 0
    assert seen == {"app": "APP", "host": host, "port": 8123}
    assert closed == [True]


def _serve_with_fakes(monkeypatch, env_extra, durable=...):
    """`serve` with fake services / app / uvicorn; `durable` is the fake runtime (default: one with
    an empty `after_launch`). Returns (rc, events, fake services)."""
    import uvicorn

    import evowiki.service.app as app_mod
    import evowiki.service.deps as deps_mod
    from evowiki.agent import dbos_app

    events = []
    if durable is ...:
        durable = SimpleNamespace(after_launch=[], on_failed_start=[])
    fake = SimpleNamespace(pool=SimpleNamespace(close=lambda: events.append("pool")),
                           durable=durable, new_id=lambda p: f"{p}_1", bot_notify=None, gateway=None)
    monkeypatch.setattr(deps_mod, "build_services", lambda env, **k: fake)
    monkeypatch.setattr(cli, "_preflight", lambda dsn: None)
    # the CLI never launches DBOS itself any more (B1b's lifespan is the one launch point)
    monkeypatch.setattr(dbos_app, "launch", lambda *a, **k: events.append("dbos_launch"))
    monkeypatch.setattr(dbos_app, "shutdown", lambda: events.append("dbos_shutdown"))
    monkeypatch.setattr(app_mod, "create_app", lambda svc: "APP")
    monkeypatch.setattr(uvicorn, "run", lambda app, **k: events.append("run"))
    env = {"EVOWIKI_DATABASE_URL": "postgresql:///unused", **env_extra}
    return main(["serve"], env), events, fake


def test_serve_registers_the_bot_as_an_after_launch_hook(monkeypatch):
    from evowiki.bot import durable

    installed, registered = [], []
    monkeypatch.setattr(durable, "install", installed.append)
    monkeypatch.setattr(durable, "register", lambda *a, **k: registered.append(a))
    rc, events, svc = _serve_with_fakes(monkeypatch, {})
    assert rc == 0 and events == ["run", "pool"]       # no launch / shutdown from the CLI
    # the runtime is installed before DBOS launches; the hook only registers queue / schedule
    assert len(svc.durable.after_launch) == 1 and registered == [] and svc.bot_notify is None
    assert len(installed) == 1 and installed[0].pool is svc.pool
    svc.durable.after_launch[0]()                       # what the lifespan does after launch
    assert registered == [()]
    assert svc.bot_notify is durable.notify


def test_a_raising_bot_registration_propagates_from_the_hook(monkeypatch):
    from evowiki.bot import durable

    def boom(*a, **k):
        raise RuntimeError("queue registration failed")

    monkeypatch.setattr(durable, "install", lambda rt: None)
    monkeypatch.setattr(durable, "register", boom)
    rc, _events, svc = _serve_with_fakes(monkeypatch, {})
    assert rc == 0
    with pytest.raises(RuntimeError, match="queue registration failed"):
        svc.durable.after_launch[0]()    # DurableRuntime.start then stops DBOS and fails startup
    assert svc.bot_notify is None


def test_serve_with_the_bot_off_adds_no_hook(monkeypatch):
    rc, events, svc = _serve_with_fakes(monkeypatch, {"EVOWIKI_BOT": "off"})
    assert rc == 0 and events == ["run", "pool"]
    assert svc.durable.after_launch == [] and svc.bot_notify is None


def test_serve_fails_closed_without_a_durable_runtime(monkeypatch, capsys):
    rc, events, _svc = _serve_with_fakes(monkeypatch, {}, durable=None)
    assert rc == 1 and "run" not in events and events == ["pool"]
    err = capsys.readouterr().err
    assert "no durable runtime" in err and "EVOWIKI_BOT=off" in err


def test_is_loopback():
    from evowiki.service.cli import is_loopback
    assert is_loopback("127.0.0.1") and is_loopback("::1") and is_loopback("localhost")
    assert is_loopback("127.0.0.2")
    assert not is_loopback("0.0.0.0") and not is_loopback("::") and not is_loopback("10.0.0.2")
    assert not is_loopback("example.com") and not is_loopback("")


@pytest.fixture
def blank_db_url(pg_admin_url):
    """A brand-new database with no extension and no schema (what an operator starts with)."""
    from tests.pg import blank_database, drop_database

    url, name = blank_database(pg_admin_url)
    yield url
    drop_database(pg_admin_url, name)


def test_migrate_bootstraps_a_blank_database(blank_db_url, capsys):
    env = {**_db(blank_db_url), "EVOWIKI_EMBEDDER": "hash"}
    t0 = time.monotonic()
    assert main(["status"], env) == 1  # not migrated: clear message, no pool timeout
    assert time.monotonic() - t0 < 10
    assert "migrate" in capsys.readouterr().err
    assert main(["migrate"], env) == 0
    assert "0001_base" in capsys.readouterr().out
    assert main(["migrate"], env) == 0
    assert "applied: []" in capsys.readouterr().out
    assert main(["status"], env) == 0


def test_a_superuser_connection_may_not_serve(db_url, capsys):
    assert main(["status"], {"EVOWIKI_DATABASE_URL": db_url}) == 1
    assert "superuser" in capsys.readouterr().err


def test_unreachable_database_fails_fast(capsys):
    env = {"EVOWIKI_DATABASE_URL": "postgresql://nobody@127.0.0.1:9/nowhere"}
    t0 = time.monotonic()
    assert main(["status"], env) == 1
    assert main(["migrate"], env) == 1
    assert time.monotonic() - t0 < 25  # connect_timeout, not the pool's 30 s per command
    err = capsys.readouterr().err
    assert err.count("database unreachable") == 2
