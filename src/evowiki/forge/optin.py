"""Which forged tools an agent may offer in a run (design 2026-10-22 §2, §6.2).

An agent offers a forged tool (or the mid-run forging tool `request_tool`) only when it is opted
in. Harness 3b will own the switch as pack keys `H3.<agent>.<tool>`; until then the forge-local
settings decide. One resolver reads both, pack first, so the harness keys take over with no change
here. Default off: phase 2 ships dark until the user or the harness opts in. Fails closed: a value
that is present but not a bool counts as off."""

import re
from collections.abc import Collection, Mapping
from datetime import datetime
from typing import Any

import psycopg

from evowiki.forge import ids
from evowiki.forge.models import NAME
from evowiki.harness.tool_names import TOOLS
from evowiki.store import forge as store

META = "request_tool"                     # opt-in name of the mid-run forging tool


def _key(agent: str, name: str = "") -> str:
    return f"opt_in.{agent}.{name}"


def _decide(value: Any) -> bool | None:
    """True / False when the value decides; None when it is unset (fall through to the next)."""
    if value is None:
        return None
    return value if isinstance(value, bool) else False


def opted_in(conn: psycopg.Connection, user_id: str, agent: str, names: Collection[str],
             pack_values: Mapping[str, Any]) -> frozenset[str]:
    """Names (forged tools and/or META) the agent may offer this run. Per name, first hit wins:
    1. pack_values[f"H3.{agent}.{name}"] when set (harness 3b; the pack read is the run's pinned
       pack, so a recovered run sees the same values);
    2. forge_setting (user, f"opt_in.{agent}.{name}") when set;
    3. forge_setting ("global", same key) when set;
    4. False.
    A set value that is not a bool counts as off (fail closed); None (JSON null) counts as unset.
    Reads `pack_values` raw: never calls HarnessPack.get, which raises for names the 3a key table
    does not know yet."""
    ids.check_user(user_id)
    prefix = _key(agent)
    mine = store.settings_with_prefix(conn, user_id, prefix)
    shared = store.settings_with_prefix(conn, ids.GLOBAL, prefix)
    on: set[str] = set()
    for name in names:
        key = _key(agent, name)
        for value in (pack_values.get(f"H3.{agent}.{name}"), mine.get(key), shared.get(key)):
            decided = _decide(value)
            if decided is not None:
                if decided:
                    on.add(name)
                break
    return frozenset(on)


def request_tool_on(conn: psycopg.Connection, user_id: str, agent: str,
                    pack_values: Mapping[str, Any]) -> bool:
    """Whether the agent may offer `request_tool` (mid-run forging) this run."""
    return META in opted_in(conn, user_id, agent, (META,), pack_values)


def set_local(conn: psycopg.Connection, user_id: str | None, agent: str, name: str, on: bool,
              now: datetime) -> None:
    """user_id=None writes the global row. Used by promote paths (round) and scripts/forge_optin."""
    scope = ids.GLOBAL if user_id is None else ids.check_user(user_id)
    if agent not in TOOLS:
        raise ValueError(f"unknown agent {agent!r}")
    if name != META and not re.fullmatch(NAME, name):
        raise ValueError(f"not a forged tool name: {name!r}")
    if not isinstance(on, bool):
        raise TypeError(f"opt-in must be True or False, got {on!r}")
    store.set_setting(conn, scope, _key(agent, name), on, now)
