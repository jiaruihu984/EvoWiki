"""Forged tools from the harness improver (harness 3b, Task 7).

The improver's `forge_tool` asks the forge to build one tool (`forge.api.propose`, kind "tool",
agent "deep", user scope; at most FORGE_PER_ROUND per improver run) on the round's recording
gateway, so its calls are in the round's cost. A built candidate is only a candidate: the improver
proposes it like any other key change, as the pair `H3.deep.<name>=true` +
`H12.tool.deep.<name>={"candidate_id": …}`. The round's final probe and gate decide; the gate row
(`harness_gate`, subject `tool:<cid>`) is written before anything applies, and the apply promotes
the candidate in the forge registry (`promote_for`, `by` "harness:<round id>", inside the pack
transaction) and writes the H12 value with the registry version it got. Undo goes back through the
registry (`undo_tool`, from `change_source.undo_change`); the forge's own change-list row of such a
promote is hidden (`harness_promotes`, `change_source.HarnessForgeFilter`): the user sees and undoes
the harness change ("h_…") only.

`build_hooks` is the service's wiring over the forge runtime (`forge.run.ForgeRuntime`)."""

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from evowiki.forge import registry
from evowiki.forge.models import (
    Evidence,
    ForgeForbidden,
    ForgeNotFound,
    ForgeResult,
    PromoteRefused,
    ToolRequest,
)
from evowiki.harness.diff import KeyOp, PackDiff, h12_tool
from evowiki.harness.forge_entries import ForgedTools, read_all
from evowiki.harness.keys import h2_key
from evowiki.llm.gateway import Gateway
from evowiki.llm.usage import Account
from evowiki.store import forge as forge_store
from evowiki.store import harness_rsi

if TYPE_CHECKING:
    from evowiki.forge.api import ForgeDeps
    from evowiki.forge.run import ForgeRuntime
    from evowiki.harness.pack import HarnessPack

log = logging.getLogger("evowiki.harness.forging")

FORGE_PER_ROUND = 1             # forge_tool calls per improver run (not an anchor)
BY = "harness:"                 # registry `by` of a promote made by a harness round
UNDO_GONE = "这个工具的上线记录找不到了，不能撤销"
UNDO_REFUSED = "这个工具不能在这里撤销"


def _opted_in(conn: Any, user_id: str, agent: str, names: Sequence[str],
              pack_values: Mapping[str, Any]) -> frozenset[str]:
    from evowiki.forge import optin
    return optin.opted_in(conn, user_id, agent, names, pack_values)


@dataclass(frozen=True)
class ForgeHooks:
    """What a round needs from the forge. None in RoundDeps = no forged tools (the improver's
    `forge_tool` is not offered).
    `deps_for(gateway, account, pack)`: a ForgeDeps whose converse is ForgeConverse(gateway,
    account), with `pack` (the round's base pack) lowering the coder's cap. `enabled(conn, user)`:
    the forge's tool switch. `read(conn, user, agent)`: the user's registry entries, each loaded
    on its own (`forge_entries.read_all`: the argument names and H2 descriptions of those that
    load, a plain reason for each that does not; a failed read of the heads raises). `propose`: None = `forge.api.propose` (tests swap it). `opted_in(conn, user, agent,
    names, pack values)`: which of `names` the agent's runs offer now (`forge.optin.opted_in`: the
    pack's H3 opt-in, else the forge's own opt-in settings), the on/off state read_pack shows."""

    deps_for: Callable[[Gateway, Account, "HarnessPack | None"], "ForgeDeps"]
    enabled: Callable[[Any, str], bool]
    read: Callable[[Any, str, str], ForgedTools] = field(default=read_all)
    propose: Callable[..., ForgeResult] | None = None
    opted_in: Callable[[Any, str, str, Sequence[str], Mapping[str, Any]], frozenset[str]] = field(
        default=_opted_in)


def forge_tool(hooks: ForgeHooks, gw: Gateway, account: Account, user_id: str, req: ToolRequest,
               citations: Sequence[str], *, pack: "HarnessPack | None" = None,
               note: str = "") -> ForgeResult:
    """Build one candidate: `propose("tool", req, Evidence(citations), user_id=, deps=
    hooks.deps_for(gw, account, pack))`. ForgeDisabled / ForgeMisconfigured propagate (the improver
    turns them into a tool error); a model or sandbox error propagates too (fail closed)."""
    propose = hooks.propose
    if propose is None:
        from evowiki.forge.api import propose
    return propose("tool", req, Evidence(citations=tuple(citations), note=note), user_id=user_id,
                   deps=hooks.deps_for(gw, account, pack))


def tool_docs(schema: Mapping[str, Any], agent: str, name: str) -> dict[str, str]:
    """H2 key -> description of a forged tool and each argument its schema describes."""
    out: dict[str, str] = {}
    if isinstance(schema.get("description"), str):
        out[h2_key(agent, name)] = schema["description"]
    props = schema.get("properties")
    for arg, sub in (props.items() if isinstance(props, Mapping) else ()):
        if isinstance(sub, Mapping) and isinstance(sub.get("description"), str):
            out[h2_key(agent, name, arg)] = sub["description"]
    return out


def candidate_schema(conn: Any, user_id: str, candidate_id: str) -> Mapping[str, Any]:
    """The schema.json of the user's tool candidate ({} when it has none that parses)."""
    c = forge_store.get_candidate(conn, candidate_id)
    if c is None or c.user_id != user_id or c.kind != "tool":
        return {}
    try:
        schema = json.loads(c.files.get("schema.json", ""))
    except ValueError:
        return {}
    return schema if isinstance(schema, dict) else {}


def promote_for(conn: Any, user_id: str, round_id: str, diff: PackDiff, gate_ref: str, *,
                why: str, now: datetime) -> PackDiff:
    """When `diff` has an H12 tool op: promote its candidate (`registry.promote`, by
    "harness:<round_id>"; the gate must be this user's `pass` row on subject "tool:<cid>") and
    return the diff with that op's value {"candidate_id", "registry_version"}. No H12 tool op:
    `diff` unchanged. PromoteRefused (or ForgeNotFound) when the candidate is not this user's built
    tool of that name for that agent (its spec's "agent"), or the gate does not say pass. Runs on the caller's transaction."""
    t = h12_tool(diff)
    if t is None:
        return diff
    agent, tool, cid = t
    c = forge_store.get_candidate(conn, cid)
    spec_agent = c.spec.get("agent") if c is not None and isinstance(c.spec, dict) else None
    if (c is None or c.user_id != user_id or c.kind != "tool" or c.name != tool
            or spec_agent != agent):
        raise PromoteRefused(f"candidate {cid!r} is not {user_id}'s {agent} tool {tool!r}")

    def gate_ok(ref: str) -> bool:
        g = harness_rsi.get_gate(conn, user_id, ref)
        return g is not None and g.verdict == "pass" and g.subject == f"tool:{cid}"

    version = registry.promote(conn, cid, gate_ref=gate_ref, by=f"{BY}{round_id}", why=why,
                               gate_ok=gate_ok, now=now)
    value = {"candidate_id": cid, "registry_version": version}
    ops = tuple(KeyOp(o.name, value) if o.name.startswith("H12.tool.") else o for o in diff.ops)
    return PackDiff(ops, diff.spec_ops)


def undo_tool(conn: Any, user_id: str, value: Mapping[str, Any]) -> None:
    """Roll back the user-scope promote an applied H12 tool value records (`forge.undo.undo`,
    by "harness"; already rolled back: nothing to do). ValueError (plain reason) when the
    promote is not found, is not that candidate's, or may not be undone here."""
    from evowiki.forge import undo as forge_undo
    rv, cid = value.get("registry_version"), value.get("candidate_id")
    if isinstance(rv, bool) or not isinstance(rv, int):
        raise ValueError(UNDO_GONE)  # noqa: TRY004 - the change list's refusal type (409)
    change = registry.change_id(user_id, rv)
    try:
        _, row, _ = registry.promote_row(conn, user_id, change)
        if registry.promoted_name(row)[0] != cid:
            raise ValueError(UNDO_GONE)
        forge_undo.undo(conn, user_id, change, by="harness", operator=False)
    except ForgeNotFound as e:
        raise ValueError(UNDO_GONE) from e
    except ForgeForbidden as e:
        raise ValueError(UNDO_REFUSED) from e


def harness_promotes(conn: Any, user_id: str) -> frozenset[str]:
    """Forge change ids ("forge:<user>:<n>") of the user's promotes made by a harness round (their
    `by` starts with "harness:"): the change list hides these forge rows (P6)."""
    history = forge_store.registry_history(conn, user_id, limit=100000)
    return frozenset(registry.change_id(user_id, r.version) for r in history
                     if r.change.get("op") == "promote"
                     and str(r.change.get("by") or "").startswith(BY))


def _enabled(conn: Any, user_id: str) -> bool:
    from evowiki.forge import switch
    return switch.enabled(conn, user_id, "tool")


def build_hooks(runtime: "ForgeRuntime") -> ForgeHooks:
    """The service's ForgeHooks over the forge runtime: its pool, sandbox and trace; the converse
    is the round's (`deps_for`)."""
    from evowiki.forge.api import ForgeDeps
    from evowiki.forge.binding import ForgeConverse

    def deps_for(gateway: Gateway, account: Account, pack: "HarnessPack | None") -> ForgeDeps:
        return ForgeDeps(pool=runtime.pool, converse=ForgeConverse(gateway=gateway, account=account),
                         sandbox=runtime.sandbox, trace=runtime.trace, pack=pack)

    return ForgeHooks(deps_for=deps_for, enabled=_enabled)
