"""Read setup authorities; permission writes use the existing forge switch."""
import importlib.util
import os
from collections.abc import Mapping
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from evowiki.bot.settings import effective_paused
from evowiki.forge import switch
from evowiki.service.backup import EVERY_S, KEEP, BackupConfig
from evowiki.service.deps import Services, check_user, current_user, get_services
from evowiki.store import bot as bot_store
from evowiki.store import forge as forge_store
from evowiki.store.rsi import PgRsiStore
from evowiki.store.usage import usage_tz
from evowiki.teach.contracts import rsi_on

router = APIRouter(prefix="/wiki/setup")
Svc = Annotated[Services, Depends(get_services)]
User = Annotated[str, Depends(current_user)]


def rsi_blockers(svc: Services, env: Mapping[str, str] = os.environ) -> list[str]:
    """Use the same deep/judge choices as the harness; no clients or calls."""
    from evowiki.harness.judge import JudgeUnavailable, judge_configs, judge_for
    blockers = []
    deep = getattr(svc.deep, "llm", None)
    if svc.gateway is None or deep is None:
        blockers.append("learning_model_missing")
    if not env.get("ANTHROPIC_API_KEY") or importlib.util.find_spec("anthropic") is None:
        blockers.append("improver_model_missing")
    if deep is not None:
        try:
            judge_for(deep.config.generator.provider, judge_configs(env))
        except JudgeUnavailable:
            blockers.append("judge_model_missing")
    return blockers


def forge_state(conn, uid: str, env: Mapping[str, str] = os.environ) -> dict:
    globally_on = env.get(switch.ENV, "").strip().lower() not in switch.OFF
    blockers = [] if globally_on else ["forge_globally_disabled"]
    out = {"global_allowed": globally_on, "blockers": blockers}
    for level in ("tool", "patch"):
        global_value = forge_store.get_setting(conn, switch.GLOBAL, switch.key(level))
        if global_value is not None and global_value is not True:
            blockers.append(f"{level}_globally_disabled")
        requested = forge_store.get_setting(conn, uid, switch.key(level))
        out[f"requested_{level}"] = requested if isinstance(requested, bool) else switch.DEFAULTS[level]
        out[level] = switch.enabled(conn, uid, level)
    return out


@router.get("")
def get_setup(svc: Svc, uid: User) -> dict:
    check_user(svc, uid)
    with svc.pool.connection() as conn:
        bot = bot_store.get_settings(conn, uid)
        forge = forge_state(conn, uid)
    blockers = rsi_blockers(svc)
    backup = BackupConfig.from_env(os.environ)
    timezone = usage_tz()
    zone = getattr(timezone, "key", None)
    if zone is None:
        offset = svc.clock().astimezone(timezone).utcoffset()
        assert offset is not None
        minutes = int(offset.total_seconds() / 60)
        hours, remainder = divmod(abs(minutes), 60)
        zone = f"UTC{'+' if minutes >= 0 else '-'}{hours:02d}:{remainder:02d}"
    return {"model_configured": svc.gateway is not None,
            "rsi": {"on": rsi_on(PgRsiStore(svc.pool).settings(uid)), "available": not blockers, "blockers": blockers},
            "forge": forge,
            "background": {"paused": effective_paused(bot, svc.clock()), "includes_memory": True,
                           "idle_write": bot.kinds["idle_write"]},
            "backup": {"enabled": backup is not None, "directory": str(backup.folder) if backup else None,
                       "keep": backup.keep if backup else KEEP, "every_seconds": backup.every_s if backup else EVERY_S},
            "usage_timezone": zone}


class ForgePermissions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    tool: bool
    patch: bool


@router.put("/forge")
def put_forge(body: ForgePermissions, svc: Svc, uid: User) -> dict:
    check_user(svc, uid)
    with svc.pool.connection() as conn, conn.transaction():
        before = forge_state(conn, uid)
        for level, on in body.model_dump().items():
            if on and (not before["global_allowed"] or f"{level}_globally_disabled" in before["blockers"]):
                raise HTTPException(409, detail={"code": "forge_globally_disabled", "params": {"level": level}})
        for level, on in body.model_dump().items():
            switch.set_enabled(conn, uid, level, on, svc.clock())
        return forge_state(conn, uid)
