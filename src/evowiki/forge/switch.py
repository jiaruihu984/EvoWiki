"""The forge kill switch (design 2026-10-22 §5.4, plan T7/T8). Fail closed, in this order:

1. env `EVOWIKI_FORGE` in ("off", "0", "false") turns every level off and wins over everything;
2. a global "off" (`forge_setting` row `("global", "enabled.<level>")`) wins over any user setting;
3. the user's own row decides, else the global row, else the default: L1 tools on (ruling R1),
   L2 patches off until phase 3 ships.

A stored value that is not a JSON boolean counts as off. Switching off deletes nothing: the
registry and archive stay, `view(enabled=False)` just offers no tools, and switching back on brings
them back."""

import os
from datetime import UTC, datetime
from typing import Literal

import psycopg

from evowiki.forge import ids
from evowiki.store import forge as store

Level = Literal["tool", "patch"]
ENV = "EVOWIKI_FORGE"
OFF = ("off", "0", "false")
GLOBAL = ids.GLOBAL
DEFAULTS: dict[str, bool] = {"tool": True, "patch": False}


def key(level: Level) -> str:
    if level not in DEFAULTS:
        raise ValueError(f"unknown forge level {level!r}")
    return f"enabled.{level}"


def enabled(conn: psycopg.Connection, user_id: str | None, level: Level) -> bool:
    if user_id is not None:
        ids.check_user(user_id)
    k = key(level)
    if os.environ.get(ENV, "").strip().lower() in OFF:
        return False
    global_value = store.get_setting(conn, GLOBAL, k)
    if global_value is not None and global_value is not True:
        return False
    user_value = store.get_setting(conn, user_id, k) if user_id not in (None, GLOBAL) else None
    if user_value is not None:
        return user_value is True
    if global_value is not None:
        return True
    return DEFAULTS[level]


def set_enabled(conn: psycopg.Connection, user_id: str | None, level: Level, on: bool,
                now: datetime | None = None) -> None:
    """`user_id=None` flips the global switch."""
    if user_id is not None:
        ids.check_user(user_id)
    if not isinstance(on, bool):
        raise TypeError("on must be a bool")
    store.set_setting(conn, GLOBAL if user_id is None else user_id, key(level), on,
                      now or datetime.now(UTC))
