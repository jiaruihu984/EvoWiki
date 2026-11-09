"""Setup projections and permissions use synthetic authorities; no database/model calls."""
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from evowiki.bot.settings import BotSettings
from evowiki.service import routes_setup as routes
from evowiki.teach.contracts import RSI_SWITCH_OFF


class Connection:
    def transaction(self):
        return nullcontext()


def fake_svc():
    conn = Connection()
    return SimpleNamespace(pool=SimpleNamespace(connection=lambda: nullcontext(conn)),
                           clock=lambda: datetime(2026, 11, 9, tzinfo=UTC), gateway=None, deep=None)


def test_rsi_missing_learning_and_improver_model_does_not_look_available():
    assert routes.rsi_blockers(fake_svc(), {}) == ["learning_model_missing", "improver_model_missing"]


def test_rsi_requires_other_family_judge():
    svc = fake_svc()
    svc.gateway = object()
    svc.deep = SimpleNamespace(llm=SimpleNamespace(config=SimpleNamespace(generator=SimpleNamespace(provider="anthropic"))))
    assert routes.rsi_blockers(svc, {"ANTHROPIC_API_KEY": "fake"}) == ["judge_model_missing"]
    assert routes.rsi_blockers(svc, {"ANTHROPIC_API_KEY": "fake", "DEEPSEEK_API_KEY": "fake"}) == []


def test_forge_global_off_is_authoritative_and_preserves_requested_permissions(monkeypatch):
    monkeypatch.setattr(routes.forge_store, "get_setting", lambda conn, scope, key: True if scope == "alice" else None)
    monkeypatch.setattr(routes.switch, "enabled", lambda *_: False)
    result = routes.forge_state(Connection(), "alice", {"EVOWIKI_FORGE": "off"})
    assert result["requested_tool"] and result["requested_patch"]
    assert not result["tool"] and not result["patch"]
    assert result["blockers"] == ["forge_globally_disabled"]


def test_put_forge_checks_global_blocker_before_any_write(monkeypatch):
    monkeypatch.setattr(routes, "check_user", lambda *_: None)
    monkeypatch.setattr(routes, "forge_state", lambda *_: {"global_allowed": False, "blockers": ["forge_globally_disabled"]})
    monkeypatch.setattr(routes.switch, "set_enabled", lambda *_: pytest.fail("blocked write"))
    with pytest.raises(HTTPException) as error:
        routes.put_forge(routes.ForgePermissions(tool=True, patch=False), fake_svc(), "alice")
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "forge_globally_disabled"


def test_setup_projects_real_bot_pause_and_backup_authorities(monkeypatch):
    monkeypatch.setattr(routes, "check_user", lambda *_: None)
    monkeypatch.setattr(routes.bot_store, "get_settings", lambda *_: BotSettings(paused="always"))
    monkeypatch.setattr(routes, "forge_state", lambda *_: {"tool": False, "patch": False})
    monkeypatch.setattr(routes, "rsi_blockers", lambda *_: ["learning_model_missing"])
    monkeypatch.setattr(routes, "PgRsiStore", lambda *_: SimpleNamespace(settings=lambda *_: RSI_SWITCH_OFF))
    monkeypatch.setattr(routes.BackupConfig, "from_env", lambda *_: SimpleNamespace(folder="D:/fake-backups", keep=7, every_s=86400))
    result = routes.get_setup(fake_svc(), "alice")
    assert result["background"] == {"paused": True, "includes_memory": True, "idle_write": False}
    assert result["backup"] == {"enabled": True, "directory": "D:/fake-backups", "keep": 7, "every_seconds": 86400}
    assert not result["rsi"]["available"] and not result["rsi"]["on"]


def test_rsi_enable_is_refused_when_setup_is_incomplete(monkeypatch):
    from evowiki.service.routes_rsi import _require_available
    monkeypatch.setattr(routes, "rsi_blockers", lambda *_: ["improver_model_missing"])
    with pytest.raises(HTTPException) as error:
        _require_available(fake_svc(), True)
    assert error.value.detail["code"] == "rsi_setup_incomplete"
    _require_available(fake_svc(), False)


def test_backup_retention_env_changes_actual_backup_config(tmp_path, monkeypatch):
    from evowiki.service import backup
    monkeypatch.setattr(backup, "real_database", lambda *_: True)
    env = {"EVOWIKI_DATABASE_URL": "postgresql://app@localhost:15432/fake", "EVOWIKI_BACKUP_KEEP": "7"}
    config = backup.BackupConfig.from_env(env, tmp_path / ".env")
    assert config.keep == 7
    assert config.folder == tmp_path / ".evowiki-run" / "backups"
    with pytest.raises(ValueError, match="between 1 and 365"):
        backup.BackupConfig.from_env({**env, "EVOWIKI_BACKUP_KEEP": "0"}, tmp_path / ".env")


def test_setup_timezone_projection_uses_offset_instead_of_localized_os_name(monkeypatch):
    monkeypatch.setattr(routes, "check_user", lambda *_: None)
    monkeypatch.setattr(routes.bot_store, "get_settings", lambda *_: BotSettings())
    monkeypatch.setattr(routes, "forge_state", lambda *_: {})
    monkeypatch.setattr(routes, "rsi_blockers", lambda *_: [])
    monkeypatch.setattr(routes, "PgRsiStore", lambda *_: SimpleNamespace(settings=lambda *_: RSI_SWITCH_OFF))
    monkeypatch.setattr(routes.BackupConfig, "from_env", lambda *_: None)
    monkeypatch.setattr(routes, "usage_tz", lambda: timezone(timedelta(hours=8), "中国标准时间"))
    assert routes.get_setup(fake_svc(), "alice")["usage_timezone"] == "UTC+08:00"
