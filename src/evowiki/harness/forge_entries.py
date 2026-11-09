"""The user's forged-tool registry as the harness reads it: names first, one entry at a time
(harness 3b follow-ups, MF1).

`forge.registry.view` loads and validates every entry, so one corrupt row makes the whole view
fail. The harness needs less: which names the user's registry has (`entries`: the global head
minus the user's masks, then the user's head on top, as `registry.view` resolves them) and, only
for the tools a change touches, that the entry still loads (`load`). So:

- reading the heads or the masks fails (a database error, …): the error propagates and every
  caller fails with it (an approval errors, a round ends `error`, the improver offers no forging);
- one entry does not load (a missing or unbuilt candidate, a bad schema, a name clash with a
  built-in tool, …): `EntryBroken` for that tool only, with a plain reason, logged. Every other
  tool reads as before. A broken entry is never read as "not registered": callers refuse what
  touches it with BROKEN.

Nothing here writes; `forge/` is only read (its `masks` and `_load`, the same validation
`registry.view` applies per entry)."""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from evowiki.forge import ids, registry
from evowiki.forge.models import ForgeNotFound
from evowiki.harness.tool_names import TOOLS
from evowiki.store import forge as forge_store

log = logging.getLogger("evowiki.harness.forge_entries")

BROKEN = "这个自己造的工具的记录读不出来了，现在不能打开它"


class EntryBroken(LookupError):
    """One registry entry does not load; `reason` is plain (BROKEN), the detail is logged."""

    def __init__(self, name: str, detail: str):
        super().__init__(f"forged tool {name!r}: {detail}")
        self.name = name
        self.reason = BROKEN


def entries(conn: Any, user_id: str) -> dict[str, str]:
    """{tool name: candidate id} of the user's registry now: the global head without the entries
    the user masked, the user's head on top (as `registry.view`). Loads no candidate; a failed
    read raises."""
    ids.check_user(user_id)
    g = forge_store.registry_head(conn, ids.GLOBAL)[1]
    u = forge_store.registry_head(conn, user_id)[1]
    hidden = registry.masks(conn, user_id)
    return {**{n: c for n, c in g.items() if hidden.get(n) != c}, **u}


def load(conn: Any, user_id: str, agent: str, name: str, candidate_id: str) -> registry.ForgedTool:
    """The entry `name` -> `candidate_id`, validated as `registry.view` validates it (a built tool
    of that name with a schema; its declared host tools allowed) and not clashing with `agent`'s
    built-in or meta tools. EntryBroken otherwise (logged)."""
    try:
        tool = registry._load(conn, user_id, candidate_id)
    except ForgeNotFound as e:
        raise _broken(user_id, name, str(e)) from e
    if tool.name != name:
        raise _broken(user_id, name, f"the entry points at tool {tool.name!r}")
    if name in TOOLS.get(agent, ()) or name in registry.RESERVED:
        raise _broken(user_id, name, f"the name collides with {agent}'s built-in or meta tools")
    return tool


def _broken(user_id: str, name: str, detail: str) -> EntryBroken:
    log.warning("forged tool %r in the registry of %s does not load: %s", name, user_id, detail)
    return EntryBroken(name, detail)


@dataclass(frozen=True)
class ForgedTools:
    """Every entry of the user's registry, each loaded on its own: `args` (tool -> sorted argument
    names) and `docs` (H2 key -> description) of the entries that load, `broken` (tool -> plain
    reason) of those that do not."""

    args: Mapping[str, tuple[str, ...]]
    docs: Mapping[str, str]
    broken: Mapping[str, str]


def read_all(conn: Any, user_id: str, agent: str) -> ForgedTools:
    """`entries`, then `load` per entry; a broken entry is listed in `broken`, never dropped."""
    from evowiki.harness.forging import tool_docs

    args: dict[str, tuple[str, ...]] = {}
    docs: dict[str, str] = {}
    broken: dict[str, str] = {}
    for name, cid in sorted(entries(conn, user_id).items()):
        try:
            tool = load(conn, user_id, agent, name, cid)
        except EntryBroken as e:
            broken[name] = e.reason
            continue
        args[name] = tuple(sorted(dict(tool.schema.get("properties") or {})))
        docs.update(tool_docs(tool.schema, agent, name))
    return ForgedTools(args=args, docs=docs, broken=broken)
